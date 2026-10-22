"""Exercise the shipped CLI/configuration, with no production access."""

import dataclasses
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from scripts.sage_recovery import __main__ as cli
from scripts.sage_recovery import client, config, progress, recovery_checks


def configuration(tmp_path, recovery_settings):
    state = tmp_path / "state"
    export = tmp_path / "export"
    state.mkdir(exist_ok=True)
    export.mkdir(exist_ok=True)
    value = dataclasses.asdict(
        dataclasses.replace(recovery_settings, root=state, evidence_out=export)
    )
    value.pop("config_path")
    value.pop("control_enabled")
    for key in ["manifest", "ledger"]:
        path = tmp_path / (key + ".json")
        path.write_text("[]")
        value[key] = str(path)
        value[key + "_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    for key in ["root", "evidence_out", "token_file"]:
        value[key] = str(value[key])
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value))
    return path, value


def test_default_and_help_have_no_external_effects(capsys):
    with (
        patch("subprocess.run", side_effect=AssertionError("external command")),
        patch.object(client, "request", side_effect=AssertionError("HTTP")),
    ):
        cli.main([])
        with pytest.raises(SystemExit) as result:
            cli.main(["--help"])
        assert result.value.code == 0
    assert "control" in capsys.readouterr().out


def test_every_operational_module_is_import_safe():
    # A fresh interpreter prevents earlier test imports from hiding startup effects.
    import subprocess
    import sys

    code = """
import importlib, pkgutil
from unittest.mock import patch
from scripts import sage_recovery
with patch('subprocess.run', side_effect=AssertionError('external command')), \
     patch('subprocess.Popen', side_effect=AssertionError('external process')), \
     patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('HTTP')):
    for item in pkgutil.iter_modules(sage_recovery.__path__):
        importlib.import_module('scripts.sage_recovery.' + item.name)
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["control"],
        ["--config", "/missing", "control"],
        ["progress", "--execute"],
        ["verify-sources"],
    ],
)
def test_invalid_command_cannot_reach_dispatch(args):
    with (
        patch.object(cli, "load", side_effect=AssertionError("must refuse before loading")),
        pytest.raises(SystemExit),
    ):
        cli.main(args)


def test_valid_progress_uses_configured_private_root(tmp_path, recovery_settings, capsys):
    path, _ = configuration(tmp_path, recovery_settings)
    loaded = config.load(path)
    recovery_checks.atomic(
        loaded.root / "monitor-run.json",
        {"run_id": "configured-run", "configuration_sha256": loaded.fingerprint},
    )
    cli.main(["--config", str(path), "progress"])
    assert json.loads(capsys.readouterr().out)["run_id"] == "configured-run"
    assert config.settings().control_enabled is False
    assert progress.summarize()["acceptance"].startswith("No acceptance claim")


@pytest.mark.parametrize(
    "changes",
    [
        {"base_url": "http://unsafe.invalid"},
        {"base_url": "https://example.invalid/path"},
        {"revision": "different-app--revision"},
        {"manifest_sha256": "0" * 64},
        {"six_ids": ["other"] * 6},
        {"unknown": True},
    ],
)
def test_invalid_target_or_source_pins_refuse_before_dispatch(tmp_path, recovery_settings, changes):
    path, value = configuration(tmp_path, recovery_settings)
    value.update(changes)
    path.write_text(json.dumps(value))
    with (
        patch.object(
            cli.importlib,
            "import_module",
            side_effect=AssertionError("dispatch reached"),
        ),
        pytest.raises(ValueError),
    ):
        cli.main(["--config", str(path), "control", "--execute"])


def test_control_consent_is_explicit_and_not_persisted(tmp_path, recovery_settings):
    path, _ = configuration(tmp_path, recovery_settings)
    fake = Mock()
    with patch.object(cli.importlib, "import_module", return_value=fake):
        cli.main(["--config", str(path), "control", "--execute"])
    fake.main.assert_called_once_with()
    assert config.settings().control_enabled is True
    assert "control_enabled" not in json.loads(path.read_text())
    assert config.load(path).control_enabled is False


def test_private_paths_refuse_git_and_symlinks(tmp_path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="outside Git"):
        config.private_path(str(tmp_path / "credentials.json"))
    with pytest.raises(ValueError, match="absolute"):
        config.private_path("relative.json")


def test_no_config_is_no_implicit_target():
    config._current = None
    with pytest.raises(ValueError, match="required"):
        config.settings()


def test_http_boundary_refuses_redirect_and_foreign_paths(recovery_settings):
    with (
        patch.object(client, "token", side_effect=AssertionError("path reached credentials")),
        pytest.raises(ValueError),
    ):
        client.request("//foreign.invalid/credentials")
    with pytest.raises(RuntimeError, match="Redirect refused"):
        client.NoRedirect().redirect_request(None, None, 302, None, None, "https://foreign.invalid")


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_http_backlog_observation_failure_pauses_without_deactivation(
    tmp_path, recovery_settings, status
):
    import types

    from scripts.sage_recovery import advance

    helper = types.SimpleNamespace(request=lambda *args, **kw: {"status": status, "body": {}})
    controller = advance.Controller(helper, root=tmp_path)
    controller.run = controller.backlog
    controller.stop = Mock(side_effect=AssertionError("diagnostic refusal deactivated production"))
    with pytest.raises(advance.ObservationUnavailable):
        advance.execute_controller(controller)
    controller.stop.assert_not_called()
    assert (tmp_path / "verification-paused.json").exists()
    assert not (tmp_path / "stop").exists()
    assert not (tmp_path / "window-validation.json").exists()


def test_observer_configuration_mismatch_pauses_without_deactivation(tmp_path, recovery_settings):
    from scripts.sage_recovery import advance
    from tests.operations.test_sage_recovery import cycle

    recovery_checks.atomic(
        tmp_path / "monitor-run.json",
        {"run_id": "run", "configuration_sha256": "different"},
    )
    recovery_checks.atomic(tmp_path / "monitor-latest.json", cycle())
    controller = advance.Controller(None, root=tmp_path)
    controller.run_id = "run"
    controller.run = controller.observer
    controller.stop = Mock(
        side_effect=AssertionError("configuration mismatch deactivated production")
    )
    with pytest.raises(ValueError, match="another recovery configuration"):
        advance.execute_controller(controller)
    controller.stop.assert_not_called()
    assert (tmp_path / "verification-paused.json").exists()


@pytest.mark.parametrize("field", ["config_path", "token_file", "manifest", "ledger"])
def test_private_input_and_output_collisions_refuse_before_dispatch(
    tmp_path, recovery_settings, field
):
    path, values = configuration(tmp_path, recovery_settings)
    if field == "config_path":
        path = Path(values["root"]) / "config.json"
    else:
        values[field] = str(Path(values["root"]) / (field + ".json"))
    path.write_text(json.dumps(values))
    with (
        patch.object(
            cli.importlib, "import_module", side_effect=AssertionError("dispatch reached")
        ),
        pytest.raises(ValueError, match="outside state"),
    ):
        cli.main(["--config", str(path), "snapshot"])


def test_snapshot_preserves_private_operator_configuration(tmp_path, recovery_settings):
    path, _ = configuration(tmp_path, recovery_settings)
    before = path.read_bytes()
    with patch.object(client, "request", return_value={"status": 200, "body": {}}):
        cli.main(["--config", str(path), "snapshot"])
    assert path.read_bytes() == before
    assert config.load(path).fingerprint == config.settings().fingerprint
    assert (config.settings().root / "vault-config.json").is_file()
    assert not (config.settings().root / "config.json").exists()


def test_csv_input_collision_refuses_before_dispatch(tmp_path, recovery_settings):
    path, values = configuration(tmp_path, recovery_settings)
    manifest = Path(values["manifest"])
    manifest.write_text(json.dumps([{"source": str(Path(values["root"]) / "input-workload.json")}]))
    values["manifest_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    path.write_text(json.dumps(values))
    with (
        patch.object(
            cli.importlib, "import_module", side_effect=AssertionError("dispatch reached")
        ),
        pytest.raises(ValueError, match="CSV inputs"),
    ):
        cli.main(["--config", str(path), "inspect-inputs"])


@pytest.mark.parametrize("subject", ["request", "snapshot", "cycle"])
def test_foreign_configuration_cannot_satisfy_probe_or_window_checks(recovery_settings, subject):
    import datetime as dt

    from tests.operations.test_sage_recovery import NOW, cycle, request

    if subject == "request":
        row = request("health")
        row["configuration_sha256"] = "different"
        with pytest.raises(ValueError, match="another recovery configuration"):
            recovery_checks.check_sample(row)
    elif subject == "snapshot":
        value = cycle()
        value["configuration_sha256"] = "different"
        with pytest.raises(ValueError, match="another recovery configuration"):
            recovery_checks.check_snapshot(value, "run", NOW)
    else:
        values = [cycle(NOW + dt.timedelta(seconds=n)) for n in range(0, 1801, 10)]
        values[1]["configuration_sha256"] = "different"
        with pytest.raises(ValueError, match="another recovery configuration"):
            recovery_checks.check_window(values, "run", NOW, NOW + dt.timedelta(minutes=30))


@pytest.mark.parametrize(
    "artifact",
    ["monitor-run.json", "monitor-latest.json", "backlog-latest.json", "replicas-latest.json"],
)
def test_progress_rejects_foreign_configuration_artifacts(tmp_path, recovery_settings, artifact):
    from scripts.sage_recovery import config as configuration_module
    from tests.operations.test_sage_recovery import NOW, cycle, documents, replica

    cfg = dataclasses.replace(recovery_settings, root=tmp_path)
    configuration_module.configure(cfg)
    data = {
        "monitor-run.json": {"run_id": "run"},
        "monitor-latest.json": cycle(),
        "backlog-latest.json": {"run_id": "run", "documents": documents()},
        "replicas-latest.json": {"run_id": "run", "replicas": replica()},
    }
    for name, value in data.items():
        recovery_checks.atomic(
            tmp_path / name,
            {**value, "at": NOW.isoformat(), "configuration_sha256": cfg.fingerprint},
        )
    baseline = progress.summarize(now=NOW)
    assert baseline["observer"] == "fresh"
    assert baseline["backlog"] == {"abstraction_complete": 16}
    assert baseline["replicas"][0]["name"] == "old"
    value = json.loads((tmp_path / artifact).read_text())
    value["configuration_sha256"] = "different"
    recovery_checks.atomic(tmp_path / artifact, value)
    result = progress.summarize(now=NOW)
    if artifact in {"monitor-run.json", "monitor-latest.json"}:
        assert result["observer"] != "fresh"
    elif artifact == "backlog-latest.json":
        assert result["backlog"] is None
    else:
        assert result["replicas"] is None
