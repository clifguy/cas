"""Tests for ``sage.build_info``: runtime build-identity capture and the MCP
instructions string that surfaces it.

Compute-logic tests (BLD-001..006) build throwaway git repos under ``tmp_path``
so they never depend on the state of the repo running the suite. The renderer
test (BLD-007) is pure. Wiring tests (BLD-008..010) import ``sage.mcp_server``
and inspect the actual MCP ``initialize`` handshake options, i.e. the field a
connecting client really receives.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sage import build_info


def _run_git(repo: Path, *args: str) -> str:
    """Run a git command in ``repo`` and return stripped stdout (raises on error)."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A throwaway git repo with a single commit and a clean working tree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test")
    (repo / "tracked.txt").write_text("original\n")
    _run_git(repo, "add", "tracked.txt")
    _run_git(repo, "commit", "-m", "initial")
    return repo


# --------------------------------------------------------------------------
# Compute logic — _compute_build_identity
# --------------------------------------------------------------------------


def test_bld_001_clean_checkout_returns_bare_sha(git_repo: Path) -> None:
    """A clean tree yields exactly the 7-char short HEAD SHA, no marker."""
    expected = _run_git(git_repo, "rev-parse", "--short=7", "HEAD")
    identity = build_info._compute_build_identity(git_repo)
    assert identity == expected
    assert not identity.endswith("-dirty")


def test_bld_002_tracked_modification_appends_dirty(git_repo: Path) -> None:
    """An unstaged change to a tracked file flips the dirty marker; SHA unchanged."""
    clean_sha = _run_git(git_repo, "rev-parse", "--short=7", "HEAD")
    (git_repo / "tracked.txt").write_text("modified\n")
    assert build_info._compute_build_identity(git_repo) == f"{clean_sha}-dirty"


def test_bld_003_staged_uncommitted_appends_dirty(git_repo: Path) -> None:
    """A staged-but-uncommitted change is dirty too (index counts, not just worktree)."""
    clean_sha = _run_git(git_repo, "rev-parse", "--short=7", "HEAD")
    (git_repo / "tracked.txt").write_text("staged change\n")
    _run_git(git_repo, "add", "tracked.txt")
    assert build_info._compute_build_identity(git_repo) == f"{clean_sha}-dirty"


def test_bld_004_untracked_file_only_is_not_dirty(git_repo: Path) -> None:
    """An untracked file alone must NOT mark dirty (tracked-only convention)."""
    clean_sha = _run_git(git_repo, "rev-parse", "--short=7", "HEAD")
    (git_repo / "scratch.tmp").write_text("untracked\n")
    identity = build_info._compute_build_identity(git_repo)
    assert identity == clean_sha
    assert not identity.endswith("-dirty")


def test_bld_005_non_git_directory_returns_unknown(tmp_path: Path) -> None:
    """Outside any git checkout the identity degrades to the literal 'unknown'."""
    bare = tmp_path / "not_a_repo"
    bare.mkdir()
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=bare,
        capture_output=True,
        text=True,
    )
    assert probe.returncode != 0, "tmp_path unexpectedly inside a git repo; test invalid"
    assert build_info._compute_build_identity(bare) == "unknown"


def test_bld_006_missing_git_binary_returns_unknown(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing git binary (FileNotFoundError) degrades to 'unknown', not a crash."""

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(build_info.subprocess, "run", _boom)
    assert build_info._compute_build_identity(git_repo) == "unknown"


# --------------------------------------------------------------------------
# Renderer — _render_instructions
# --------------------------------------------------------------------------


def test_bld_007_render_embeds_identity_and_remedy() -> None:
    """The rendered line carries the identity verbatim and names the restart remedy."""
    text = build_info._render_instructions("abc1234-dirty")
    assert "abc1234-dirty" in text
    assert "restart" in text.lower()


# --------------------------------------------------------------------------
# Wiring / handshake — sage.mcp_server.build_partitioned_server
# --------------------------------------------------------------------------


@pytest.mark.parametrize("surface", ["sage", "sage_admin"])
def test_bld_008_identity_reaches_initialize_handshake(surface: str) -> None:
    """Both served surfaces carry the build identity in the real initialize options."""
    from sage import mcp_server

    server = mcp_server.build_partitioned_server(surface)
    opts = server._mcp_server.create_initialization_options()
    assert opts.instructions is not None
    assert build_info.BUILD_IDENTITY in opts.instructions


def test_bld_009_instructions_frozen_at_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """The served identity is the import-time constant, not a per-build recompute.

    Patching the compute function after import must not change what a freshly
    built server advertises — proving the value is frozen, not re-read.
    """
    from sage import mcp_server

    monkeypatch.setattr(build_info, "_compute_build_identity", lambda *_a, **_k: "SENTINEL-LIVE")
    server = mcp_server.build_partitioned_server("sage")
    opts = server._mcp_server.create_initialization_options()
    assert "SENTINEL-LIVE" not in (opts.instructions or "")
    assert build_info.BUILD_IDENTITY in opts.instructions


def test_bld_010_serverinfo_version_reports_identity() -> None:
    """serverInfo.version reports the build identity, not the default mcp pkg version."""
    from sage import mcp_server

    server = mcp_server.build_partitioned_server("sage")
    opts = server._mcp_server.create_initialization_options()
    assert opts.server_version == build_info.BUILD_IDENTITY
