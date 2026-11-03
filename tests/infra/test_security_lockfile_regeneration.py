"""Exercise the security-tool lock check with real uv and a small offline wheel index."""

from __future__ import annotations

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


def _project(root: Path, tool: str, dependencies: tuple[str, ...]) -> Path:
    directory = root / "requirements/security" / tool
    directory.mkdir(parents=True)
    listed = "".join(f'    "{dependency}",\n' for dependency in dependencies)
    (directory / "pyproject.toml").write_text(
        "[project]\n"
        f'name = "security-{tool}"\n'
        'version = "0"\n'
        'requires-python = "==3.14.*"\n'
        f"dependencies = [\n{listed}]\n"
        "\n[tool.uv]\n"
        "package = false\n"
    )
    return directory


def _lock(directory: Path) -> None:
    subprocess.run(
        ["uv", "lock", "--project", str(directory)], capture_output=True, text=True, check=True
    )


@pytest.fixture
def locked_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    _wheel(wheels, "platform_support", "1.0")
    _wheel(wheels, "helper", "1.0")
    for version in ("1.0", "2.0"):
        _wheel(
            wheels,
            "scanner",
            version,
            ("helper>=1.0", "platform-support; sys_platform == 'win32'"),
        )
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("UV_FIND_LINKS", str(wheels))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    root = tmp_path / "repo"
    _lock(_project(root, "scanner", ("scanner==1.0",)))
    # A newer transitive release appears after locking; the check must keep the
    # locked version rather than resolving it forward.
    _wheel(wheels, "helper", "2.0")
    return root


def _check(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(root)],
        capture_output=True,
        text=True,
    )


def _snapshot(root: Path) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_current_locks_pass_without_upgrades_or_source_changes(locked_repo: Path) -> None:
    project = locked_repo / "requirements/security/scanner"
    upgrade = subprocess.run(
        ["uv", "lock", "--project", str(project), "--upgrade", "--dry-run"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "helper v1.0 -> v2.0" in upgrade.stderr, upgrade.stderr
    assert 'name = "helper"\nversion = "1.0"' in (project / "uv.lock").read_text()
    files = _snapshot(locked_repo)
    result = _check(locked_repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "requirements/security/scanner/uv.lock" in result.stdout
    assert _snapshot(locked_repo) == files


def test_lock_keeps_marker_dependencies(locked_repo: Path) -> None:
    lock = (locked_repo / "requirements/security/scanner/uv.lock").read_text()
    assert 'name = "platform-support"' in lock
    assert "sys_platform == 'win32'" in lock


def test_pin_edited_without_relock_fails_with_project_and_command(locked_repo: Path) -> None:
    project = locked_repo / "requirements/security/scanner/pyproject.toml"
    project.write_text(project.read_text().replace("scanner==1.0", "scanner==2.0"))
    files = _snapshot(locked_repo)
    result = _check(locked_repo)
    assert result.returncode == 1
    assert "requirements/security/scanner/uv.lock" in result.stderr
    assert "uv lock --project requirements/security/scanner" in result.stderr
    assert _snapshot(locked_repo) == files


def test_new_tool_is_enumerated(locked_repo: Path) -> None:
    other = _project(locked_repo, "other", ("helper==1.0",))
    _lock(other)
    clean = _check(locked_repo)
    assert clean.returncode == 0, clean.stderr
    assert "requirements/security/other/uv.lock" in clean.stdout
    project = other / "pyproject.toml"
    project.write_text(project.read_text().replace("helper==1.0", "helper==2.0"))
    assert _check(locked_repo).returncode == 1


def test_project_without_lock_is_refused(locked_repo: Path) -> None:
    (locked_repo / "requirements/security/scanner/uv.lock").unlink()
    result = _check(locked_repo)
    assert result.returncode == 1
    assert "Missing security lockfile: requirements/security/scanner/uv.lock" in result.stderr
    assert "uv lock --project requirements/security/scanner" in result.stderr
    assert not (locked_repo / "requirements/security/scanner/uv.lock").exists()


@pytest.mark.parametrize("stray", ["scanner.txt", "scanner.in", "notes/README.md"])
def test_entry_outside_a_tool_project_is_refused(locked_repo: Path, stray: str) -> None:
    path = locked_repo / "requirements/security" / stray
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("scanner==1.0\n")
    result = _check(locked_repo)
    assert result.returncode == 1
    assert stray.split("/")[0] in result.stderr


def test_empty_directory_is_refused(locked_repo: Path) -> None:
    for path in sorted((locked_repo / "requirements/security").rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    result = _check(locked_repo)
    assert result.returncode == 1
    assert "no security tool projects" in result.stderr.lower()


def test_missing_directory_is_refused(locked_repo: Path) -> None:
    result = _check(locked_repo / "requirements")
    assert result.returncode == 1
    assert "no security tool projects" in result.stderr.lower()


def test_resolve_failure_is_not_a_pass(locked_repo: Path) -> None:
    project = locked_repo / "requirements/security/scanner/pyproject.toml"
    project.write_text(project.read_text().replace("scanner==1.0", "missing-distribution==1.0"))
    result = _check(locked_repo)
    assert result.returncode == 1
    assert "requirements/security/scanner" in result.stderr
    assert "missing-distribution" in result.stderr


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
            # A changed pin forces a resolve, which is what reaches the index.
            project = locked_repo / "requirements/security/scanner/pyproject.toml"
            project.write_text(project.read_text().replace("scanner==1.0", "scanner==2.0"))
            online = _check(locked_repo)
            assert online.returncode == 1
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
    assert "--universal" not in text
