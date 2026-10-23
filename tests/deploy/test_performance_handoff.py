"""Execute the committed Actions handoff with real token renewal and CLI parsing."""

import base64
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from tests.deploy.test_performance_bootstrap import (
    ENVIRONMENT,
    PRINCIPAL,
    bootstrapped,  # noqa: F401 - shared real bootstrap fixture
)

ROOT = Path(__file__).resolve().parents[2]
MASK = "::add-mask::fixture-assertion-not-a-credential"


@pytest.fixture
def handoff(bootstrapped, tmp_path):  # noqa: F811 - imported pytest fixture
    source, spec_sha, output, cloud, receipt = bootstrapped
    federation = cloud("ad", "app", "federated-credential", "list")
    receipt_path = output / "bootstrap-receipt.json"
    runner = tmp_path / "runner"
    runner.mkdir()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    # Strict producers emulate only the external commands used by the real helper.
    # Any unexpected command fails before it could reach a local Azure session.
    producer = (
        f"#!{sys.executable}\n"
        + """
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["FIXTURE_CALLS"], "a") as handle:
    handle.write(json.dumps([Path(sys.argv[0]).name, *args]) + "\\n")
if Path(sys.argv[0]).name == "curl":
    assert args == ["--silent", "--show-error", "--fail", "--header",
                    "Authorization: bearer fixture-request",
                    "http://fixture.invalid?fixture=1&audience=api://AzureADTokenExchange"]
    print(json.dumps({"value": "fixture-assertion-not-a-credential"}))
elif args == ["account", "get-access-token", "--scope",
              "https://management.azure.com/.default", "--query", "accessToken",
              "-o", "tsv", "--only-show-errors"]:
    print(os.environ["FIXTURE_ARM_TOKEN"])
elif args == ["login", "--service-principal", "--username", "fixture-client",
              "--tenant", "fixture-tenant", "--federated-token",
              "fixture-assertion-not-a-credential", "--output", "none"]:
    pass
elif args == ["account", "set", "--subscription", "fixture-subscription"]:
    pass
else:
    raise AssertionError(args)
"""
    )
    for name in ("az", "curl"):
        path = binaries / name
        path.write_text(producer)
        path.chmod(0o700)
    claims = (
        base64.urlsafe_b64encode(json.dumps({"oid": PRINCIPAL, "exp": time.time() + 3600}).encode())
        .decode()
        .rstrip("=")
    )
    env = {
        # Deliberately exclude ambient cloud credentials and renewal stamps.
        "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": federation[0]["subject"].split(":")[1],
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "http://fixture.invalid?fixture=1",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "fixture-request",
        "AZURE_CLIENT_ID": "fixture-client",
        "AZURE_TENANT_ID": "fixture-tenant",
        "AZURE_SUBSCRIPTION_ID": "fixture-subscription",
        "RUNNER_TEMP": str(runner),
        "PERF_SPEC_JSON": source.read_text(),
        "PERF_APPROVED_SHA256": spec_sha,
        "PERF_BOOTSTRAP_JSON": receipt_path.read_text(),
        "PERF_BOOTSTRAP_SHA256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        "PERF_ENVIRONMENT": ENVIRONMENT,
        "PERF_OUTPUT": str(tmp_path / "ci"),
        "FIXTURE_CALLS": str(tmp_path / "calls.jsonl"),
        "FIXTURE_ARM_TOKEN": "fixture." + claims + ".unsigned",
    }
    steps = yaml.safe_load((ROOT / ".github/workflows/ingest-performance.yml").read_text())["jobs"][
        "trial"
    ]["steps"]

    def run(name):
        script = next(step["run"] for step in steps if step.get("name") == name)
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    return env, receipt, run


@pytest.mark.parametrize("signin", ["fresh", "stale", "recent"])
def test_workflow_handoff_keeps_renewal_output_out_of_principal(handoff, signin):
    env, receipt, run = handoff
    stamp = Path(env["RUNNER_TEMP"]) / "azure-federated-signin-at"
    if signin != "fresh":
        stamp.write_text(str(int(time.time()) - (300 if signin == "stale" else 0)))
    result = run("Materialize reviewed specification without shell interpretation")
    assert result.returncode == 0, result.stderr
    result = run("Verify bootstrap handoff and preserve the original trial clock")
    assert result.returncode == 0, result.stderr
    assert (MASK in result.stdout) == (signin != "recent")
    calls = [json.loads(line) for line in Path(env["FIXTURE_CALLS"]).read_text().splitlines()]
    assert sum(row[:3] == ["az", "account", "get-access-token"] for row in calls) == 1
    assert sum(row[:2] == ["az", "login"] for row in calls) == (signin != "recent")
    dest = Path(env["PERF_OUTPUT"])
    assert json.loads((dest / "runtime-plan.json").read_text()) == receipt["plan"]
    assert (dest / "bootstrap-receipt.json").read_bytes() == env["PERF_BOOTSTRAP_JSON"].encode()
    assert not (dest / "provision-receipt.json").exists()


@pytest.mark.parametrize(
    "bad,reason",
    [
        ("principal", "signed-in trial operator differs"),
        ("environment", "bootstrap receipt identity or readiness differs"),
        ("receipt-bytes", "bootstrap receipt digest differs"),
        ("spec-bytes", "spec digest differs"),
        ("receipt-digest", "bootstrap receipt digest differs"),
        ("spec-digest", "spec digest differs"),
        ("partial", "bootstrap receipt identity or readiness differs"),
        ("inventory", "bootstrap plan or assignment inventory differs"),
        ("expired", "expired"),
    ],
)
def test_workflow_handoff_refuses_unapproved_state_without_outputs(handoff, bad, reason):
    env, _, run = handoff
    result = run("Materialize reviewed specification without shell interpretation")
    assert result.returncode == 0, result.stderr
    receipt_path = Path(env["RUNNER_TEMP"]) / "perf-bootstrap.json"
    spec_path = Path(env["RUNNER_TEMP"]) / "perf-spec.json"
    if bad == "principal":
        # Same expected receipt principal; only the independently minted token differs.
        claims = (
            base64.urlsafe_b64encode(
                json.dumps(
                    {"oid": "33333333-3333-3333-3333-333333333333", "exp": time.time() + 3600}
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
        env["FIXTURE_ARM_TOKEN"] = "fixture." + claims + ".unsigned"
    elif bad == "environment":
        env["PERF_ENVIRONMENT"] = "production"
    elif bad.endswith("-bytes"):
        target = receipt_path if bad == "receipt-bytes" else spec_path
        target.write_bytes(target.read_bytes() + b"\n")
    elif bad.endswith("-digest"):
        env["PERF_BOOTSTRAP_SHA256" if bad == "receipt-digest" else "PERF_APPROVED_SHA256"] = (
            "f" * 64
        )
    else:
        receipt = json.loads(receipt_path.read_text())
        if bad == "partial":
            receipt["status"] = "intent"
        elif bad == "inventory":
            receipt["assignments"].pop()
        else:
            from datetime import UTC, datetime, timedelta

            expired = datetime.now(UTC) - timedelta(minutes=1)
            receipt["plan"]["expires_at"] = expired.isoformat()
            receipt["plan"]["created_at"] = (expired - timedelta(hours=4)).isoformat()
        receipt_path.write_text(json.dumps(receipt))
        env["PERF_BOOTSTRAP_SHA256"] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    result = run("Verify bootstrap handoff and preserve the original trial clock")
    assert result.returncode != 0
    assert reason in result.stderr
    assert MASK in result.stdout
    assert not Path(env["PERF_OUTPUT"]).exists()
