"""Exercise private archive and blocking deadline effect boundaries."""

import asyncio
import json
import time
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from deploy.performance_archive import decrypt, seal
from deploy.performance_limits import deadline_scope


@pytest.fixture(scope="module")
def recipient():
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    return key, key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()


def test_private_archive_roundtrip_and_public_receipt(tmp_path, recipient):
    key, pem = recipient
    private = tmp_path / "recipient.pem"
    private.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    source = tmp_path / "raw"
    source.mkdir()
    marker = b"PRIVATE-TENANT-ID-AND-RESOURCE-BINDING"
    (source / "plan.json").write_bytes(marker)
    archive = tmp_path / "evidence.enc"
    receipt = seal(source, archive, pem)
    assert marker not in archive.read_bytes()
    assert marker not in json.dumps(receipt).encode()
    target = tmp_path / "opened"
    decrypt(archive, private, target)
    assert (target / "plan.json").read_bytes() == marker
    assert target.stat().st_mode & 0o777 == 0o700
    assert archive.stat().st_mode & 0o777 == 0o600
    damaged = bytearray(archive.read_bytes())
    damaged[-20] ^= 1
    archive.write_bytes(damaged)
    with pytest.raises(InvalidTag):
        decrypt(archive, private, tmp_path / "tampered")
    assert not (tmp_path / "tampered").exists()


def test_archive_rejects_symlinks_and_nested_output(tmp_path, recipient):
    _, pem = recipient
    source = tmp_path / "raw"
    source.mkdir()
    (source / "record").write_text("evidence")
    with pytest.raises(ValueError, match="outside"):
        seal(source, source / "evidence.enc", pem)
    (source / "link").symlink_to(source / "record")
    with pytest.raises(ValueError, match="symlink"):
        seal(source, tmp_path / "evidence.enc", pem)


@pytest.mark.asyncio
async def test_real_control_thread_refuses_late_spawn_after_metadata(monkeypatch):
    from deploy import performance_driver as driver
    from scripts import azure_ingest_trial as runner

    effects = []

    def snapshot(trial):
        time.sleep(0.08)
        return {}, {"replica": "replica"}

    monkeypatch.setattr(runner, "runtime_snapshot", snapshot)
    monkeypatch.setattr(
        driver.subprocess if hasattr(driver, "subprocess") else __import__("subprocess"),
        "Popen",
        lambda *a, **kw: effects.append("spawn"),
    )
    # Cancellation of the caller does not stop a thread: inherited deadline must.
    with deadline_scope(0.02):
        task = asyncio.create_task(
            asyncio.to_thread(
                driver.exec_control,
                {"resource_group": "rg", "run_id": "trial"},
                {"target": {"revision_name": "r"}},
                "fail_provider",
                "nonce",
            )
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 0.03)
        with pytest.raises(TimeoutError):
            await task
    assert effects == []


def test_cli_refuses_late_spawn_after_authentication(monkeypatch):
    from deploy import performance_trial as trial

    effects = []
    monkeypatch.setattr(trial, "renew_signin", lambda: time.sleep(0.04))
    monkeypatch.setattr(trial.subprocess, "run", lambda *a, **kw: effects.append("spawn"))
    with deadline_scope(0.01), pytest.raises(TimeoutError):
        trial.Azure()("containerapp", "revision", "restart")
    assert effects == []


def test_original_runner_metadata_obeys_inherited_deadline(monkeypatch):
    from scripts import azure_ingest_trial as runner

    effects = []
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: effects.append("spawn"))
    with deadline_scope(0.01):
        time.sleep(0.02)
        with pytest.raises(TimeoutError):
            runner.azure("containerapp", "show")
    assert effects == []


def test_public_workflow_never_uploads_raw_evidence_or_dispatches_bindings():
    import yaml

    workflow = yaml.safe_load(Path(".github/workflows/ingest-performance.yml").read_text())
    inputs = workflow.get("on", workflow.get(True))["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"environment", "approved_sha256", "approved_bootstrap_sha256"}
    job = workflow["jobs"]["trial"]
    assert job["env"]["PERF_SPEC_JSON"] == "${{ secrets.PERF_APPROVED_SPEC_JSON }}"
    assert job["env"]["PERF_BOOTSTRAP_JSON"] == "${{ secrets.PERF_BOOTSTRAP_RECEIPT_JSON }}"
    uploads = [x for x in job["steps"] if x.get("uses", "").startswith("actions/upload-artifact@")]
    assert len(uploads) == 2
    for step in uploads:
        assert "PERF_OUTPUT" not in step["with"]["path"]
        assert step["with"]["path"].endswith(
            "/perf-sealed-" + ("initial" if step["id"] == "archive" else "final") + "/"
        )
        assert "seal" in step["if"]


def test_wrong_recipient_cannot_extract(tmp_path, recipient):
    _, pem = recipient
    source = tmp_path / "raw"
    source.mkdir()
    (source / "record").write_text("evidence")
    archive = tmp_path / "evidence.enc"
    seal(source, archive, pem)
    wrong = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    private = tmp_path / "wrong.pem"
    private.write_bytes(
        wrong.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    with pytest.raises(ValueError):
        decrypt(archive, private, tmp_path / "wrong")
    assert not (tmp_path / "wrong").exists()


@pytest.mark.parametrize(
    "method,args",
    [
        ("arm", ("POST", "/subscriptions/sub/resourceGroups/trial")),
        ("graph", ("POST", "/sites/test/permissions")),
        ("blob", ("PUT", "https://stperfabc.blob.core.windows.net/evidence/run/record")),
    ],
)
def test_rest_mutation_refuses_expiry_during_token_refresh(method, args, monkeypatch):
    from deploy import performance_trial as trial

    effects = []
    rest = trial.Rest()

    def delayed_token(scope):
        time.sleep(0.03)
        return "token"

    monkeypatch.setattr(rest, "token", delayed_token)
    monkeypatch.setattr(rest.client, "request", lambda *a, **kw: effects.append("sent"))
    with deadline_scope(0.01), pytest.raises(TimeoutError):
        getattr(rest, method)(*args)
    assert effects == []
    rest.client.close()


def test_live_deadline_allows_verified_control_launch(monkeypatch):
    import os
    import select
    import subprocess
    from types import SimpleNamespace

    from deploy import performance_driver as driver
    from scripts import azure_ingest_trial as runner

    effects = []
    polls = iter([None, 0, 0, 0])
    child = SimpleNamespace(poll=lambda: next(polls), returncode=0)

    def launch(argv, **kwargs):
        effects.append(argv)
        return child

    monkeypatch.setattr(runner, "runtime_snapshot", lambda _: ({}, {"replica": "verified-replica"}))
    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(select, "select", lambda readers, *args: (readers, [], []))
    with monkeypatch.context() as patch:
        patch.setattr(os, "read", lambda *args: b"PERF_CONTROL_ACK:nonce")
        with deadline_scope(1):
            driver.exec_control(
                {"resource_group": "rg", "run_id": "trial"},
                {"target": {"revision_name": "revision"}},
                "fail_provider",
                "nonce",
            )
    assert len(effects) == 1
    assert effects[0][effects[0].index("--replica") + 1] == "verified-replica"


def test_live_deadline_allows_cli_and_all_rest_transports(monkeypatch):
    from types import SimpleNamespace

    import httpx

    from deploy import performance_trial as trial

    launches = []
    monkeypatch.setattr(trial, "renew_signin", lambda: None)

    def cli(argv, **kwargs):
        assert 0 < kwargs["timeout"] <= 1
        launches.append(argv)
        return SimpleNamespace(returncode=0, stdout='{"observed":true}')

    monkeypatch.setattr(trial.subprocess, "run", cli)
    rest = trial.Rest()
    rest.client.close()
    sends = []

    def transport(request):
        sends.append(request)
        assert 0 < request.extensions["timeout"]["read"] <= 1
        return httpx.Response(200, json={"observed": True})

    rest.client = httpx.Client(transport=httpx.MockTransport(transport))
    monkeypatch.setattr(rest, "token", lambda _: "scoped-token")
    with deadline_scope(1):
        assert trial.Azure()("containerapp", "show") == {"observed": True}
        assert rest.arm("GET", "/subscriptions/sub/resourceGroups/trial").json()["observed"]
        assert rest.graph("GET", "/sites/test")["observed"]
        assert rest.blob(
            "GET", "https://stperfabc.blob.core.windows.net/evidence/run/record"
        ).json()["observed"]
    assert len(launches) == 1
    assert len(sends) == 3
    rest.client.close()


@pytest.mark.parametrize(
    "seal_id,upload_id", [("seal_initial", "archive"), ("seal_final", "archive_final")]
)
def test_actual_workflow_seal_produces_only_owner_decryptable_uploads(
    tmp_path, recipient, seal_id, upload_id
):
    import hashlib
    import os
    import subprocess
    import sys

    import yaml

    key, pem = recipient
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/ingest-performance.yml").read_text())
    steps = workflow["jobs"]["trial"]["steps"]
    seal_step = next(x for x in steps if x.get("id") == seal_id)
    upload = next(x for x in steps if x.get("id") == upload_id)
    sandbox = tmp_path / "ci"
    sandbox.mkdir()
    (sandbox / ".venv").symlink_to(Path(sys.prefix), target_is_directory=True)
    runner = tmp_path / "runner"
    runner.mkdir()
    raw = tmp_path / "private evidence"
    raw.mkdir()
    marker = b"PRIVATE-TENANT-AND-RAW-MEASUREMENTS"
    (raw / "runtime-plan.json").write_bytes(marker)
    if seal_id == "seal_final":
        (raw / "cleanup-readback.json").write_bytes(b'{"absent":true}')
    spec = runner / "perf-spec.json"
    spec.write_text(json.dumps({"evidence_public_key_pem": pem}))
    env = {
        **os.environ,
        "PYTHONPATH": str(root),
        "RUNNER_TEMP": str(runner),
        "PERF_OUTPUT": str(raw),
        "PERF_APPROVED_SHA256": hashlib.sha256(spec.read_bytes()).hexdigest(),
    }
    subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-c", seal_step["run"]],
        cwd=sandbox,
        env=env,
        capture_output=True,
        check=True,
        timeout=30,
    )
    # Follow the actual producer to the actual upload sink, rather than trusting its folder name.
    destination = Path(upload["with"]["path"].replace("${{ runner.temp }}", str(runner)))
    files = list(destination.rglob("*"))
    assert {p.name for p in files} == {"evidence.enc", "evidence.receipt.json"}
    assert all(p.is_file() and marker not in p.read_bytes() for p in files)
    owner = tmp_path / "owner"
    owner.mkdir(mode=0o700)
    private = owner / "recipient.pem"
    private.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    private.chmod(0o600)
    recovered = owner / "recovered"
    decrypt(destination / "evidence.enc", private, recovered)
    assert {p.name: p.read_bytes() for p in recovered.iterdir()} == {
        p.name: p.read_bytes() for p in raw.iterdir()
    }
