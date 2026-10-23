"""An earlier snapshot cannot authorize deletion after a failed newer export."""

import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from deploy import performance_trial as subject
from tests.deploy.test_performance_trial import NOW, plan


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Snapshot:
    fail = False

    def blob(self, method, url, **kwargs):
        if kwargs.get("params", {}).get("comp") == "list":
            names = ["trial-one/old.json"] + (["trial-one/new.json"] if self.fail else [])
            return SimpleNamespace(
                content=(
                    "<EnumerationResults><Blobs>"
                    + "".join("<Blob><Name>" + x + "</Name></Blob>" for x in names)
                    + "</Blobs><NextMarker/></EnumerationResults>"
                ).encode()
            )
        if url.endswith("/new.json"):
            raise TimeoutError("latest evidence unavailable")
        return SimpleNamespace(content=b'{"measured":true}')


@pytest.fixture
def exported(tmp_path):
    p = subject.validate_plan(plan(), NOW)
    outputs = {
        "exportUrl": "https://stperfunit.blob.core.windows.net/evidence/trial-one",
        "servicePrincipalId": "service",
        "bootstrapPrincipalId": "bootstrap",
        "sourceRoleIds": [],
    }
    spec = tmp_path / "spec.json"
    approved = plan()
    for key in ["created_at", "expires_at"]:
        approved.pop(key)
    spec.write_text(json.dumps(approved))
    subject.write_json(tmp_path / "runtime-plan.json", p)
    subject.write_json(
        tmp_path / "provision-receipt.json",
        {"outputs": outputs, "plan_sha256": subject.plan_digest(p)},
    )
    exporter = Snapshot()
    subject.export_evidence(p, outputs, exporter, tmp_path)
    return p, outputs, spec, exporter


def cloud_cleanup(monkeypatch, p):
    groups = {p["resource_group"], p["controller_group"]}
    effects = []

    def az(*args, **kwargs):
        if args[:2] == ("group", "exists"):
            return args[-1] in groups
        if args[:2] == ("group", "show"):
            group = args[-1]
            return {
                "name": group,
                "id": "/subscriptions/" + p["subscription_id"] + "/resourceGroups/" + group,
                "tags": {"benchmarkRun": p["run_id"], "expiresAt": p["expires_at"]},
            }
        if args[:2] == ("resource", "list"):
            return []
        if args[:2] == ("identity", "show"):
            return {"principalId": "bootstrap" if args[-1].startswith("id-perf-pg-") else "service"}
        if args[:2] == ("group", "delete"):
            group = args[args.index("-n") + 1]
            effects.append(group)
            groups.remove(group)
            return None
        raise AssertionError(args)

    monkeypatch.setattr(subject, "Azure", lambda: az)
    monkeypatch.setattr(subject, "Rest", lambda: object())
    return effects


def cleanup_cli(monkeypatch, out, spec, receipt_digest):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "trial",
            "cleanup",
            "--spec",
            str(spec),
            "--approved-sha256",
            digest(spec),
            "--output",
            str(out),
            "--export-receipt-sha256",
            receipt_digest,
        ],
    )
    return subject.main()


def test_failed_latest_export_cannot_delete_either_group(tmp_path, exported, monkeypatch):
    p, outputs, spec, exporter = exported
    old = digest(tmp_path / "export-receipt.json")
    exporter.fail = True
    with pytest.raises(TimeoutError):
        subject.export_evidence(p, outputs, exporter, tmp_path)
    assert not (tmp_path / "export-receipt.json").exists()
    assert json.loads((tmp_path / "export-state.json").read_text())["status"] == "failed"
    effects = cloud_cleanup(monkeypatch, p)
    with pytest.raises(ValueError, match="export"):
        cleanup_cli(monkeypatch, tmp_path, spec, old)
    assert effects == []


def test_current_verified_export_allows_exact_owned_cleanup(tmp_path, exported, monkeypatch):
    p, outputs, spec, exporter = exported
    effects = cloud_cleanup(monkeypatch, p)
    assert cleanup_cli(monkeypatch, tmp_path, spec, digest(tmp_path / "export-receipt.json")) == 0
    assert effects == [p["resource_group"], p["controller_group"]]


@pytest.mark.parametrize(
    "rival",
    [
        "old_digest",
        "state_started",
        "run",
        "plan",
        "url",
        "attempt",
        "missing",
        "changed",
        "changed_size",
        "foreign",
        "duplicate",
        "symlink",
    ],
)
def test_cleanup_refuses_drift_before_any_deletion(tmp_path, exported, monkeypatch, rival):
    p, outputs, spec, exporter = exported
    path = tmp_path / "export-receipt.json"
    receipt = json.loads(path.read_text())
    state = json.loads((tmp_path / "export-state.json").read_text())
    target = tmp_path / receipt["files"][0]["path"]
    if rival == "state_started":
        state["status"] = "started"
    elif rival == "run":
        receipt["run_id"] = "other"
    elif rival == "plan":
        receipt["plan_sha256"] = "0" * 64
    elif rival == "url":
        receipt["export_url_sha256"] = "0" * 64
    elif rival == "attempt":
        state["attempt_id"] = "0" * 32
    elif rival == "missing":
        target.unlink()
    elif rival == "changed":
        raw = target.read_bytes()
        target.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
    elif rival == "changed_size":
        target.write_bytes(b"overwritten longer file")
    elif rival == "foreign":
        receipt["files"][0]["path"] = "cloud-evidence/other/old.json"
    elif rival == "duplicate":
        receipt["files"] *= 2
        receipt["blobs"] = 2
    elif rival == "symlink":
        sibling = tmp_path / "outside"
        sibling.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(sibling)
    subject.write_json(path, receipt)
    expected = digest(path)
    state["receipt_sha256"] = expected
    subject.write_json(tmp_path / "export-state.json", state)
    effects = cloud_cleanup(monkeypatch, p)
    with pytest.raises(ValueError):
        cleanup_cli(monkeypatch, tmp_path, spec, "0" * 64 if rival == "old_digest" else expected)
    assert effects == []


def test_cleanup_runtime_plan_must_match_reviewed_spec(tmp_path, exported, monkeypatch):
    p, outputs, spec, exporter = exported
    changed = {**json.loads(spec.read_text()), "location": "other-region"}
    spec.write_text(json.dumps(changed))
    effects = cloud_cleanup(monkeypatch, p)
    with pytest.raises(ValueError, match="runtime plan"):
        cleanup_cli(monkeypatch, tmp_path, spec, digest(tmp_path / "export-receipt.json"))
    assert effects == []


@pytest.mark.parametrize("failed_state", ["started", "failed"])
def test_failed_export_marker_write_cannot_restore_old_cleanup_proof(
    tmp_path, exported, monkeypatch, failed_state
):
    p, outputs, spec, exporter = exported
    old = digest(tmp_path / "export-receipt.json")
    exporter.fail = True
    write = subject.write_json

    def fail_marker(path, value):
        if path.name == "export-state.json" and value.get("status") == failed_state:
            raise OSError("state write unavailable")
        return write(path, value)

    monkeypatch.setattr(subject, "write_json", fail_marker)
    with pytest.raises(OSError):
        subject.export_evidence(p, outputs, exporter, tmp_path)
    effects = cloud_cleanup(monkeypatch, p)
    with pytest.raises(ValueError):
        cleanup_cli(monkeypatch, tmp_path, spec, old)
    assert effects == []


def test_actual_workflow_export_failure_cannot_publish_old_receipt_hash(tmp_path, exported):
    import os
    import subprocess
    from pathlib import Path

    import yaml

    p, outputs, spec, exporter = exported
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/ingest-performance.yml").read_text())
    steps = workflow["jobs"]["trial"]["steps"]
    export_step = next(x for x in steps if x.get("id") == "export")
    cleanup_step = next(
        x for x in steps if x.get("name") == "Delete only verified trial resources after export"
    )
    assert "steps.export.outcome == 'success'" in cleanup_step["if"]
    assert (
        cleanup_step["env"]["PERF_EXPORT_RECEIPT_SHA256"]
        == "${{ steps.export.outputs.receipt_sha256 }}"
    )
    sandbox = tmp_path / "ci"
    (sandbox / ".venv/bin").mkdir(parents=True)
    launcher = sandbox / ".venv/bin/python"
    # Execute the workflow's real shell body. The selected Python entry invokes
    # the real export CLI but simulates inability even to invalidate the old receipt.
    launcher.write_text(
        "#!"
        + sys.executable
        + "\n"
        + """import os,sys
if sys.argv[1:4] == ['-m','deploy.performance_trial','export']:
    from pathlib import Path
    from deploy import performance_trial as trial
    original=Path.unlink
    def unavailable(self,*args,**kwargs):
        if self.name=='export-receipt.json':raise PermissionError('cannot invalidate receipt')
        return original(self,*args,**kwargs)
    Path.unlink=unavailable
    trial.Rest=lambda:object()
    sys.argv=[sys.argv[0],*sys.argv[3:]]
    raise SystemExit(trial.main())
os.execv(sys.executable,[sys.executable,*sys.argv[1:]])
"""
    )
    launcher.chmod(0o700)
    runner = tmp_path / "runner"
    runner.mkdir()
    (runner / "perf-spec.json").write_bytes(spec.read_bytes())
    result_file = tmp_path / "github-output"
    env = {
        **os.environ,
        "PYTHONPATH": str(root),
        "RUNNER_TEMP": str(runner),
        "PERF_OUTPUT": str(tmp_path),
        "PERF_APPROVED_SHA256": digest(spec),
        "GITHUB_OUTPUT": str(result_file),
    }
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-c", export_step["run"]],
        cwd=sandbox,
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert b"cannot invalidate receipt" in result.stderr
    assert not result_file.exists()  # No old hash becomes a successful export output.
    assert (
        tmp_path / "export-receipt.json"
    ).exists()  # The difficult failure really left old bytes.


@pytest.mark.parametrize("failed_operation", ["unlink", "state_write"])
def test_independent_invalidation_blocks_direct_cleanup_after_failure(
    tmp_path, exported, monkeypatch, failed_operation
):
    from pathlib import Path

    p, outputs, spec, exporter = exported
    old = digest(tmp_path / "export-receipt.json")
    original_unlink = Path.unlink
    original_write = subject.write_json
    calls = []

    def unlink(path, *args, **kwargs):
        if path.name == "export-receipt.json":
            calls.append("unlink")
            if failed_operation == "unlink":
                raise PermissionError("receipt removal unavailable")
        return original_unlink(path, *args, **kwargs)

    def write(path, value):
        if path.name == "export-state.json":
            calls.append("state_write")
            if failed_operation == "state_write":
                raise PermissionError("state write unavailable")
        return original_write(path, value)

    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(subject, "write_json", write)
    # No remote request should start until both invalidation actions succeed.
    monkeypatch.setattr(subject, "Rest", lambda: object())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "trial",
            "export",
            "--spec",
            str(spec),
            "--approved-sha256",
            digest(spec),
            "--output",
            str(tmp_path),
        ],
    )
    with pytest.raises(PermissionError):
        subject.main()
    effects = cloud_cleanup(monkeypatch, p)
    with pytest.raises(ValueError):
        cleanup_cli(monkeypatch, tmp_path, spec, old)
    assert effects == []


def test_readonly_directory_revokes_old_proof_before_unlink_failure(
    tmp_path, exported, monkeypatch
):
    import os

    if os.geteuid() == 0:
        pytest.skip("root bypasses POSIX directory permissions")
    p, outputs, spec, exporter = exported
    old = digest(tmp_path / "export-receipt.json")
    monkeypatch.setattr(subject, "Rest", lambda: exporter)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "trial",
            "export",
            "--spec",
            str(spec),
            "--approved-sha256",
            digest(spec),
            "--output",
            str(tmp_path),
        ],
    )
    mode = tmp_path.stat().st_mode & 0o777
    tmp_path.chmod(0o500)
    try:
        with pytest.raises(PermissionError):
            subject.main()
    finally:
        tmp_path.chmod(mode)
    assert digest(tmp_path / "export-receipt.json") == old
    effects = cloud_cleanup(monkeypatch, p)
    with pytest.raises(ValueError):
        cleanup_cli(monkeypatch, tmp_path, spec, old)
    assert effects == []
    assert json.loads((tmp_path / "export-state.json").read_text())["status"] == "started"


def test_failed_final_verification_revokes_state_when_receipt_removal_fails(
    tmp_path, exported, monkeypatch
):
    from pathlib import Path

    p, outputs, spec, exporter = exported
    original = Path.unlink
    calls = 0

    def unlink(path, *args, **kwargs):
        nonlocal calls
        if path.name == "export-receipt.json":
            calls += 1
            if calls == 2:
                raise PermissionError("receipt removal unavailable after verification")
        return original(path, *args, **kwargs)

    def fail_verification(*args):
        raise ValueError("final verification unavailable")

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "unlink", unlink)
        scoped.setattr(subject, "verify_export", fail_verification)
        with pytest.raises(PermissionError):
            subject.export_evidence(p, outputs, exporter, tmp_path)
    current = digest(tmp_path / "export-receipt.json")
    effects = cloud_cleanup(monkeypatch, p)
    with pytest.raises(ValueError):
        cleanup_cli(monkeypatch, tmp_path, spec, current)
    assert effects == []
    assert json.loads((tmp_path / "export-state.json").read_text())["status"] == "failed"
