"""Exercise the shipped CLI/configuration, with no production access."""

import dataclasses
import hashlib
import json
from unittest.mock import Mock, patch

import pytest

from scripts.sage_recovery import __main__ as cli
from scripts.sage_recovery import client, config, progress, recovery_checks


def configuration(tmp_path, recovery_settings):
    value = dataclasses.asdict(recovery_settings)
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
    recovery_checks.atomic(tmp_path / "monitor-run.json", {"run_id": "configured-run"})
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
            cli.importlib, "import_module", side_effect=AssertionError("dispatch reached")
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


def test_http_boundary_refuses_redirect_and_foreign_paths():
    with pytest.raises(ValueError):
        client.request("//foreign.invalid/credentials")
    with pytest.raises(RuntimeError, match="Redirect refused"):
        client.NoRedirect().redirect_request(None, None, 302, None, None, "https://foreign.invalid")
