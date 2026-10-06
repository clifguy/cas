"""Behavioral tests for ``deploy/azure-federated-signin.sh``.

The helper renews a GitHub Actions job's federated Azure sign-in from inside a
long-running step. A federated sign-in stores a short-lived client assertion,
and the Azure CLI re-presents it whenever it mints a token: for a new scope, or
when a cached token expires. Renewing the sign-in keeps the stored assertion
young enough to be accepted whenever that happens.

The script runs against a stub GitHub token endpoint and a recording stand-in
for ``az``, so every case is offline. Each case runs under ``/bin/bash`` where
it exists as well as under the ``bash`` on ``PATH``, because macOS ships bash
3.2 as ``/bin/bash`` and the deploy scripts must stay within it.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import time
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.deploy._stub_server import serve_threaded

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "azure-federated-signin.sh"
STAMP_NAME = "azure-federated-signin-at"
FEDERATED_TOKEN = "federated-assertion-value"
REQUEST_TOKEN = "runtime-request-token"


def _bash_interpreters() -> list[str]:
    found = [shutil.which("bash")]
    if Path("/bin/bash").exists():
        found.append("/bin/bash")
    return sorted({b for b in found if b})


@dataclass
class TokenEndpoint:
    url: str
    requests: list[dict[str, str]] = field(default_factory=list)
    status: int = 200


@pytest.fixture
def token_endpoint() -> Iterator[TokenEndpoint]:
    holder: dict[str, TokenEndpoint] = {}

    def factory(base_url: str) -> type[http.server.BaseHTTPRequestHandler]:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 -- http.server dispatch name
                endpoint = holder["endpoint"]
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                endpoint.requests.append(
                    {
                        "authorization": self.headers.get("Authorization", ""),
                        "audience": ";".join(query.get("audience", [])),
                        "run": ";".join(query.get("run", [])),
                    }
                )
                body = json.dumps({"value": FEDERATED_TOKEN}).encode()
                self.send_response(endpoint.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        return Handler

    with serve_threaded(factory) as base_url:
        # The runner's request URL already carries a query string; the script
        # must append the audience to it rather than start a new one.
        holder["endpoint"] = TokenEndpoint(url=f"{base_url}/token?run=7")
        yield holder["endpoint"]


@dataclass
class Run:
    returncode: int
    stdout: str
    stderr: str
    az_calls: list[list[str]]
    stamp: Path


def _fake_az(bin_dir: Path, calls: Path) -> None:
    az = bin_dir / "az"
    az.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "sys.exit(int(os.environ.get('FAKE_AZ_EXIT', '0')))\n"
    )
    az.chmod(0o755)


def _run(
    tmp_path: Path,
    bash: str,
    *args: str,
    endpoint: TokenEndpoint | None,
    stamp_age: int | None = None,
    drop: tuple[str, ...] = (),
    az_exit: int = 0,
) -> Run:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "az-calls.jsonl"
    calls.write_text("")
    _fake_az(bin_dir, calls)
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir(exist_ok=True)
    stamp = runner_temp / STAMP_NAME
    if stamp_age is not None:
        stamp.write_text(f"{int(time.time()) - stamp_age}\n")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "RUNNER_TEMP": str(runner_temp),
        "AZURE_CLIENT_ID": "client-coordinate",
        "AZURE_TENANT_ID": "tenant-coordinate",
        "AZURE_SUBSCRIPTION_ID": "subscription-coordinate",
        "FAKE_AZ_EXIT": str(az_exit),
    }
    if endpoint is not None:
        env["ACTIONS_ID_TOKEN_REQUEST_URL"] = endpoint.url
        env["ACTIONS_ID_TOKEN_REQUEST_TOKEN"] = REQUEST_TOKEN
    for key in drop:
        env.pop(key, None)
    proc = subprocess.run(
        [bash, str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60
    )
    az_calls = [json.loads(line) for line in calls.read_text().splitlines() if line]
    return Run(proc.returncode, proc.stdout, proc.stderr, az_calls, stamp)


BASHES = pytest.mark.parametrize("bash", _bash_interpreters())


def test_script_is_executable_strict_and_parses() -> None:
    assert os.access(SCRIPT, os.X_OK), "the helper must be executable"
    text = SCRIPT.read_text()
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
    for bash in _bash_interpreters():
        proc = subprocess.run([bash, "-n", str(SCRIPT)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck absent")
def test_script_lints_clean() -> None:
    proc = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout


@BASHES
def test_outside_github_actions_it_does_nothing(tmp_path: Path, bash: str) -> None:
    """An operator's own az session is left alone: no token endpoint, no sign-in."""
    run = _run(tmp_path, bash, endpoint=None)
    assert run.returncode == 0, run.stderr
    assert run.az_calls == []
    assert not run.stamp.exists()


@BASHES
def test_signs_in_with_a_fresh_federated_token(
    tmp_path: Path, bash: str, token_endpoint: TokenEndpoint
) -> None:
    run = _run(tmp_path, bash, endpoint=token_endpoint)
    assert run.returncode == 0, run.stderr
    assert token_endpoint.requests == [
        {
            "authorization": f"bearer {REQUEST_TOKEN}",
            "audience": "api://AzureADTokenExchange",
            "run": "7",
        }
    ]
    assert run.az_calls == [
        [
            "login",
            "--service-principal",
            "--username",
            "client-coordinate",
            "--tenant",
            "tenant-coordinate",
            "--federated-token",
            FEDERATED_TOKEN,
            "--output",
            "none",
        ],
        ["account", "set", "--subscription", "subscription-coordinate"],
    ]
    recorded = int(run.stamp.read_text())
    assert abs(recorded - time.time()) < 30, "the stamp records when the sign-in happened"


@BASHES
def test_the_token_reaches_the_log_only_as_a_mask_command(
    tmp_path: Path, bash: str, token_endpoint: TokenEndpoint
) -> None:
    run = _run(tmp_path, bash, endpoint=token_endpoint)
    assert run.returncode == 0, run.stderr
    lines = (run.stdout + run.stderr).splitlines()
    leaking = [line for line in lines if FEDERATED_TOKEN in line]
    assert leaking == [f"::add-mask::{FEDERATED_TOKEN}"], (
        "the token may be written only as the runner's mask command, which keeps "
        f"it out of every later log line; got {leaking!r}"
    )
    assert REQUEST_TOKEN not in run.stdout + run.stderr


@BASHES
def test_if_stale_skips_a_recent_sign_in(
    tmp_path: Path, bash: str, token_endpoint: TokenEndpoint
) -> None:
    run = _run(tmp_path, bash, "--if-stale", endpoint=token_endpoint, stamp_age=30)
    assert run.returncode == 0, run.stderr
    assert token_endpoint.requests == []
    assert run.az_calls == []


@BASHES
@pytest.mark.parametrize("stamp_age", [None, 600])
def test_if_stale_renews_an_old_or_unrecorded_sign_in(
    tmp_path: Path, bash: str, token_endpoint: TokenEndpoint, stamp_age: int | None
) -> None:
    run = _run(tmp_path, bash, "--if-stale", endpoint=token_endpoint, stamp_age=stamp_age)
    assert run.returncode == 0, run.stderr
    assert len(token_endpoint.requests) == 1
    assert [call[0] for call in run.az_calls] == ["login", "account"]


@BASHES
@pytest.mark.parametrize("content", ["", "not-a-time\n", "-5\n"])
def test_if_stale_renews_when_the_stamp_is_unreadable(
    tmp_path: Path, bash: str, token_endpoint: TokenEndpoint, content: str
) -> None:
    """A stamp that does not hold a time cannot prove a recent sign-in."""
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    (runner_temp / STAMP_NAME).write_text(content)
    run = _run(tmp_path, bash, "--if-stale", endpoint=token_endpoint)
    assert run.returncode == 0, run.stderr
    assert [call[0] for call in run.az_calls] == ["login", "account"]


@BASHES
def test_staleness_threshold_sits_inside_the_assertion_lifetime(
    tmp_path: Path, bash: str, token_endpoint: TokenEndpoint
) -> None:
    """A sign-in just under four minutes old is kept and one just over is renewed.

    The assertion is accepted for five minutes; renewing at four leaves a poll
    interval and an Azure CLI call of margin before a refresh would present an
    expired one.
    """
    kept = _run(tmp_path, bash, "--if-stale", endpoint=token_endpoint, stamp_age=230)
    assert kept.returncode == 0, kept.stderr
    assert kept.az_calls == []
    renewed = _run(tmp_path, bash, "--if-stale", endpoint=token_endpoint, stamp_age=250)
    assert renewed.returncode == 0, renewed.stderr
    assert [call[0] for call in renewed.az_calls] == ["login", "account"]


@BASHES
def test_without_if_stale_it_always_signs_in(
    tmp_path: Path, bash: str, token_endpoint: TokenEndpoint
) -> None:
    run = _run(tmp_path, bash, endpoint=token_endpoint, stamp_age=5)
    assert run.returncode == 0, run.stderr
    assert [call[0] for call in run.az_calls] == ["login", "account"]


@BASHES
def test_a_failed_token_request_fails_without_signing_in(
    tmp_path: Path, bash: str, token_endpoint: TokenEndpoint
) -> None:
    token_endpoint.status = 500
    run = _run(tmp_path, bash, endpoint=token_endpoint, stamp_age=600)
    assert run.returncode != 0
    assert run.az_calls == []
    assert abs(int(run.stamp.read_text()) - (time.time() - 600)) < 30, "stamp left alone"


@BASHES
def test_a_failed_sign_in_fails_and_keeps_the_old_stamp(
    tmp_path: Path, bash: str, token_endpoint: TokenEndpoint
) -> None:
    run = _run(tmp_path, bash, "--if-stale", endpoint=token_endpoint, stamp_age=600, az_exit=1)
    assert run.returncode != 0
    assert [call[0] for call in run.az_calls] == ["login"]
    assert abs(int(run.stamp.read_text()) - (time.time() - 600)) < 30, (
        "a failed sign-in must not be recorded as a fresh one"
    )


@BASHES
@pytest.mark.parametrize(
    "missing",
    [
        "AZURE_CLIENT_ID",
        "AZURE_TENANT_ID",
        "AZURE_SUBSCRIPTION_ID",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "RUNNER_TEMP",
    ],
)
def test_inside_github_actions_a_missing_coordinate_fails_loudly(
    tmp_path: Path, bash: str, token_endpoint: TokenEndpoint, missing: str
) -> None:
    run = _run(tmp_path, bash, endpoint=token_endpoint, drop=(missing,))
    assert run.returncode != 0
    assert missing in run.stderr
    assert token_endpoint.requests == []
    assert run.az_calls == []


@BASHES
def test_an_unknown_argument_is_a_usage_error(
    tmp_path: Path, bash: str, token_endpoint: TokenEndpoint
) -> None:
    run = _run(tmp_path, bash, "--if-older-than", endpoint=token_endpoint)
    assert run.returncode == 2
    assert "usage" in run.stderr
    assert run.az_calls == []
