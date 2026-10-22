"""Exercise lock regeneration with real uv and a small offline wheel index."""

from __future__ import annotations

import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from zipfile import ZipFile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/check_security_lockfiles.py"


def _wheel(directory: Path, name: str, version: str, dependencies: tuple[str, ...] = ()) -> None:
    dist = f"{name}-{version}.dist-info"
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    metadata += "".join(f"Requires-Dist: {dependency}\n" for dependency in dependencies)
    with ZipFile(directory / f"{name}-{version}-py3-none-any.whl", "w") as archive:
        archive.writestr(f"{dist}/METADATA", metadata)
        archive.writestr(f"{dist}/WHEEL", "Wheel-Version: 1.0\nTag: py3-none-any\n")
        archive.writestr(f"{dist}/RECORD", "")


def _compile(
    root: Path, tool: str, *, universal: bool = True, upgrade: bool = False
) -> subprocess.CompletedProcess[str]:
    version = json.loads((root / "versions.json").read_text())["python"]["version"]
    return subprocess.run(
        [
            "uv",
            "pip",
            "compile",
            f"requirements/security/{tool}.in",
            "--generate-hashes",
            *(["--universal"] if universal else []),
            *(["--upgrade"] if upgrade else []),
            "--python-version",
            version,
            "-o",
            f"requirements/security/{tool}.txt",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def locked_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    _wheel(wheels, "platform_support", "1.0")
    for version in ("1.0", "2.0"):
        _wheel(wheels, "scanner", version, ("platform-support; sys_platform == 'win32'",))
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("UV_FIND_LINKS", str(wheels))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    root = tmp_path / "repo"
    requirements = root / "requirements/security"
    requirements.mkdir(parents=True)
    (root / "versions.json").write_text(json.dumps({"python": {"version": "3.14"}}))
    (requirements / "scanner.in").write_text("scanner>=1.0\n")
    # A newer version is available, but regeneration must retain this existing pin.
    (requirements / "scanner.txt").write_text("scanner==1.0\n")
    _compile(root, "scanner")
    return root


def _check(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(root)],
        capture_output=True,
        text=True,
    )


def test_current_locks_pass_without_upgrades_or_source_changes(locked_repo: Path) -> None:
    lock = locked_repo / "requirements/security/scanner.txt"
    pinned = lock.read_bytes()
    _compile(locked_repo, "scanner", upgrade=True)
    assert "scanner==2.0" in lock.read_text()
    lock.write_bytes(pinned)
    files = {path: path.read_bytes() for path in locked_repo.rglob("*") if path.is_file()}
    result = _check(locked_repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "scanner.txt" in result.stdout
    assert "scanner==1.0" in (locked_repo / "requirements/security/scanner.txt").read_text()
    assert {path: path.read_bytes() for path in files} == files


def test_nonuniversal_lock_fails_with_file_and_regeneration_command(locked_repo: Path) -> None:
    _compile(locked_repo, "scanner", universal=False)
    path = locked_repo / "requirements/security/scanner.txt"
    before = path.read_bytes()
    assert "platform-support==" not in before.decode()
    result = _check(locked_repo)
    assert result.returncode == 1
    assert "requirements/security/scanner.txt" in result.stderr
    assert (
        "uv pip compile requirements/security/scanner.in --generate-hashes --universal "
        "--python-version 3.14 -o requirements/security/scanner.txt"
    ) in result.stderr
    assert "platform-support==1.0" in result.stderr
    assert path.read_bytes() == before


def test_new_tool_is_enumerated(locked_repo: Path) -> None:
    directory = locked_repo / "requirements/security"
    (directory / "other.in").write_text("scanner==1.0\n")
    _compile(locked_repo, "other")
    clean = _check(locked_repo)
    assert clean.returncode == 0, clean.stderr
    assert "other.txt" in clean.stdout
    (directory / "other.txt").write_text("scanner==1.0\n")
    assert _check(locked_repo).returncode == 1


@pytest.mark.parametrize("missing", ["scanner.in", "scanner.txt"])
def test_unpaired_file_is_refused(locked_repo: Path, missing: str) -> None:
    (locked_repo / "requirements/security" / missing).unlink()
    result = _check(locked_repo)
    assert result.returncode == 1
    assert missing in result.stderr


def test_empty_directory_is_refused(locked_repo: Path) -> None:
    for path in (locked_repo / "requirements/security").iterdir():
        path.unlink()
    result = _check(locked_repo)
    assert result.returncode == 1
    assert "no security lockfile pairs" in result.stderr.lower()


def test_compile_failure_is_not_a_pass(locked_repo: Path) -> None:
    (locked_repo / "requirements/security/scanner.in").write_text("missing-distribution==1.0\n")
    result = _check(locked_repo)
    assert result.returncode == 1
    assert "scanner.in" in result.stderr
    assert "No solution" in result.stderr


def test_python_version_is_read_from_manifest(locked_repo: Path) -> None:
    manifest = locked_repo / "versions.json"
    manifest.write_text(json.dumps({"python": {"version": "3.13"}}))
    _compile(locked_repo, "scanner")
    result = _check(locked_repo)
    assert result.returncode == 0, result.stderr
    manifest.write_text(json.dumps({"python": {"version": "3.14"}}))
    result = _check(locked_repo)
    assert result.returncode == 1
    assert "--python-version 3.14" in result.stderr


def test_missing_uv_is_reported(locked_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    result = _check(locked_repo)
    assert result.returncode == 1
    assert "uv" in result.stderr
    assert "Traceback" not in result.stderr


def test_fixture_disables_index_requests(
    locked_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[str] = []

    class RecordingIndex(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), RecordingIndex) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        monkeypatch.setenv("UV_INDEX_URL", f"http://127.0.0.1:{server.server_port}/simple")
        try:
            offline = _check(locked_repo)
            assert offline.returncode == 0, offline.stderr
            assert requests == []
            monkeypatch.delenv("UV_OFFLINE")
            online = _check(locked_repo)
            assert online.returncode == 0, online.stderr
            assert "/simple/scanner/" in requests
        finally:
            server.shutdown()
            thread.join()


def test_required_ci_job_runs_the_verifier() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["test"]["steps"]
    verifier_steps = [s for s in steps if "scripts.check_security_lockfiles" in s.get("run", "")]
    assert len(verifier_steps) == 1
    step = verifier_steps[0]
    assert step["run"] == ".venv/bin/python -m scripts.check_security_lockfiles"
    assert "if" not in step
    assert not step.get("continue-on-error", False)
    assert "if" not in workflow["jobs"]["test"]
    assert "continue-on-error" not in workflow["jobs"]["test"]


def test_dependabot_comment_points_to_the_verifier() -> None:
    text = (ROOT / ".github/dependabot.yml").read_text()
    assert "scripts.check_security_lockfiles" in text
    assert "check that on the first such" not in text
