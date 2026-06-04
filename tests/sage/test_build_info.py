"""Tests for ``sage.build_info``: runtime build-identity capture and the MCP
instructions string that surfaces it.

Compute-logic tests (BLD-001..006) build throwaway git repos under ``tmp_path``
so they never depend on the state of the repo running the suite. The renderer
test (BLD-007) is pure. Wiring tests (BLD-008..010) import ``sage.mcp_server``
and inspect the actual MCP ``initialize`` handshake options, i.e. the field a
connecting client really receives.
"""

from __future__ import annotations

import importlib.metadata
import re
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
    """The rendered line carries the composed version+build verbatim and names
    the restart remedy."""
    text = build_info._render_instructions("1.0.0+abc1234")
    assert "1.0.0+abc1234" in text
    assert "restart" in text.lower()


# --------------------------------------------------------------------------
# Version resolution — _base_release / _compute_api_version / _compose_*
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "version,expected",
    [
        ("1.0.0", "1.0.0"),
        ("1.0.0.post3.dev2+g85ad722", "1.0.0"),
        ("1.2", "1.2"),
        ("2", "2"),
        ("1.0.0rc1", "1.0.0"),
        ("", build_info.UNKNOWN),
        ("abc", build_info.UNKNOWN),
    ],
)
def test_bld_012_base_release_extraction_strips_suffixes(version: str, expected: str) -> None:
    """_base_release returns the leading numeric release segment, stripping any
    pre/post/dev/local suffix, and degrades to UNKNOWN for non-versions."""
    assert build_info._base_release(version) == expected


@pytest.mark.parametrize(
    "api_version,build_identity,expected",
    [
        ("1.0.0", "cc019b8", "1.0.0+cc019b8"),
        (build_info.UNKNOWN, "cc019b8", "cc019b8"),
        ("1.0.0", build_info.UNKNOWN, "1.0.0"),
        (build_info.UNKNOWN, build_info.UNKNOWN, build_info.UNKNOWN),
    ],
)
def test_bld_013_version_with_build_composition(
    api_version: str, build_identity: str, expected: str
) -> None:
    """_compose_version_with_build joins the two parts, but never emits an
    'unknown' fragment when only one side is degraded."""
    assert build_info._compose_version_with_build(api_version, build_identity) == expected


def test_bld_011_api_version_is_base_release_segment() -> None:
    """API_VERSION is the stable base release segment of the installed
    distribution version (no .dev/.post/+local suffix), or the UNKNOWN sentinel
    outside an installed distribution."""
    api_version = build_info.API_VERSION
    if api_version == build_info.UNKNOWN:
        return
    assert re.fullmatch(r"\d+(\.\d+){0,2}", api_version), (
        f"API_VERSION carries a non-base suffix: {api_version!r}"
    )
    assert api_version == build_info._base_release(importlib.metadata.version("cas"))


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


def test_bld_010_serverinfo_version_reports_composed_version() -> None:
    """serverInfo.version reports the composed version+build, not the default
    mcp pkg version nor the bare build identity."""
    from sage import mcp_server

    server = mcp_server.build_partitioned_server("sage")
    opts = server._mcp_server.create_initialization_options()
    assert opts.server_version == build_info.VERSION_WITH_BUILD


def test_bld_014_serverinfo_version_carries_both_parts() -> None:
    """The served serverInfo.version carries both the release version and the
    build identity — the 'version+build' handshake requirement.

    Independent of BLD-010: even if VERSION_WITH_BUILD were to collapse to one
    part, this asserts both substrings are present in the served value.
    """
    if build_info.UNKNOWN in (build_info.API_VERSION, build_info.BUILD_IDENTITY):
        pytest.skip("degraded environment: API_VERSION or BUILD_IDENTITY is unknown")
    from sage import mcp_server

    server = mcp_server.build_partitioned_server("sage")
    opts = server._mcp_server.create_initialization_options()
    assert build_info.API_VERSION in opts.server_version
    assert build_info.BUILD_IDENTITY in opts.server_version
