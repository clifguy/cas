"""Tests for ``sage.build_info``: runtime build-identity capture and the MCP
instructions string that surfaces it.

Compute-logic tests (BLD-001..006) build throwaway git repos under ``tmp_path``
so they never depend on the state of the repo running the suite. The renderer
test (BLD-007) is pure. Wiring tests (BLD-008..010) import ``sage.mcp_server``
and inspect the actual MCP ``initialize`` handshake options, i.e. the field a
connecting client really receives.
"""

from __future__ import annotations

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


def _make_tagged_repo(tmp_path: Path, tag: str, *, extra_commits: int) -> Path:
    """A throwaway git repo tagged ``tag`` at its first commit, plus
    ``extra_commits`` further commits so ``git describe`` reports that distance."""
    repo = tmp_path / "tagged_repo"
    repo.mkdir()
    _run_git(repo, "init")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test")
    (repo / "f.txt").write_text("0\n")
    _run_git(repo, "add", "f.txt")
    _run_git(repo, "commit", "-m", "initial")
    _run_git(repo, "tag", "-a", tag, "-m", tag)
    for i in range(extra_commits):
        (repo / "f.txt").write_text(f"{i + 1}\n")
        _run_git(repo, "add", "f.txt")
        _run_git(repo, "commit", "-m", f"c{i + 1}")
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
# Version resolution — _parse_describe / _parse_metadata_version / _major_minor
# / _compute_release_version / _resolve_release_version / _compose_*
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "describe_out,expected",
    [
        ("v1.0.0-12-g162d19b", "1.0.12"),
        ("v1.0.0-0-g162d19b", "1.0.0"),
        ("v2.3.0-4-gabc1234", "2.3.4"),
        ("v1.2-5-gdeadbee", "1.2.5"),
        ("", None),
        ("garbage", None),
    ],
)
def test_bld_019_parse_describe_patch_is_commit_distance(
    describe_out: str, expected: str | None
) -> None:
    """_parse_describe maps ``<tag>-<distance>-g<sha>`` to MAJOR.MINOR.<distance>:
    the PATCH segment is the commit distance, NOT the tag's own third segment.
    Unparseable input yields None (the caller then degrades to UNKNOWN)."""
    assert build_info._parse_describe(describe_out) == expected


@pytest.mark.parametrize(
    "meta,expected",
    [
        ("1.0.0.post1.dev12+g43dd6c4", "1.0.12"),
        ("1.0.0", "1.0.0"),
        ("2.3.0.post1.dev4", "2.3.4"),
        ("abc", None),
        ("", None),
    ],
)
def test_bld_020_parse_metadata_version_reads_dev_distance(meta: str, expected: str | None) -> None:
    """_parse_metadata_version reads the no-git fallback: MAJOR.MINOR from the
    base and the ``.devN`` distance as PATCH (0 when the tree is exactly on a
    tag). Non-versions yield None."""
    assert build_info._parse_metadata_version(meta) == expected


@pytest.mark.parametrize(
    "version,expected",
    [
        ("1.0.12", "1.0"),
        ("1.0.0", "1.0"),
        ("1.2", "1.2"),
        (build_info.UNKNOWN, build_info.UNKNOWN),
    ],
)
def test_bld_021_major_minor_extraction(version: str, expected: str) -> None:
    """_major_minor reduces a release version to its MAJOR.MINOR contract
    segment, and passes UNKNOWN through unchanged."""
    assert build_info._major_minor(version) == expected


def test_bld_015_release_version_clean_tag_is_patch_zero(tmp_path: Path) -> None:
    """A checkout sitting exactly on ``v1.0.0`` reports PATCH 0 → ``1.0.0``."""
    repo = _make_tagged_repo(tmp_path, "v1.0.0", extra_commits=0)
    assert build_info._compute_release_version(repo) == "1.0.0"


def test_bld_016_release_version_counts_commit_distance(tmp_path: Path) -> None:
    """Three commits past ``v1.0.0`` report PATCH 3 → ``1.0.3`` — the patch
    auto-increments with commit distance."""
    repo = _make_tagged_repo(tmp_path, "v1.0.0", extra_commits=3)
    assert build_info._compute_release_version(repo) == "1.0.3"


def test_bld_017_release_version_no_tag_returns_unknown(git_repo: Path) -> None:
    """A repo with commits but no ``v*`` tag cannot describe → UNKNOWN (the
    caller then falls back to distribution metadata)."""
    assert build_info._compute_release_version(git_repo) == build_info.UNKNOWN


def test_bld_018_release_version_missing_git_returns_unknown(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing git binary degrades to UNKNOWN, not a crash (mirrors BLD-006)."""

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(build_info.subprocess, "run", _boom)
    assert build_info._compute_release_version(git_repo) == build_info.UNKNOWN


def test_bld_022_resolve_falls_back_to_metadata_when_git_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_resolve_release_version uses the metadata fallback when the git path
    yields UNKNOWN — proving the fallback is wired, not dead."""
    monkeypatch.setattr(
        build_info, "_compute_release_version", lambda *_a, **_k: build_info.UNKNOWN
    )
    monkeypatch.setattr(build_info, "_release_from_metadata", lambda: "9.9.5")
    assert build_info._resolve_release_version() == "9.9.5"


def test_bld_023_resolve_prefers_git_over_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the git path resolves, _resolve_release_version returns it and does
    not consult the metadata fallback."""
    monkeypatch.setattr(build_info, "_compute_release_version", lambda *_a, **_k: "1.0.7")
    monkeypatch.setattr(build_info, "_release_from_metadata", lambda: "9.9.9")
    assert build_info._resolve_release_version() == "1.0.7"


@pytest.mark.parametrize(
    "release_version,build_identity,expected",
    [
        ("1.0.12", "cc019b8", "1.0.12+cc019b8"),
        (build_info.UNKNOWN, "cc019b8", "cc019b8"),
        ("1.0.12", build_info.UNKNOWN, "1.0.12"),
        (build_info.UNKNOWN, build_info.UNKNOWN, build_info.UNKNOWN),
    ],
)
def test_bld_013_version_with_build_composition(
    release_version: str, build_identity: str, expected: str
) -> None:
    """_compose_version_with_build joins the release version and build identity,
    but never emits an 'unknown' fragment when only one side is degraded."""
    assert build_info._compose_version_with_build(release_version, build_identity) == expected


def test_bld_011_api_version_is_major_minor_of_release() -> None:
    """API_VERSION is the bare MAJOR.MINOR contract segment of RELEASE_VERSION
    (no patch), or the UNKNOWN sentinel in a degraded environment."""
    api_version = build_info.API_VERSION
    if api_version == build_info.UNKNOWN:
        return
    assert re.fullmatch(r"\d+\.\d+", api_version), (
        f"API_VERSION is not bare MAJOR.MINOR: {api_version!r}"
    )
    assert api_version == build_info._major_minor(build_info.RELEASE_VERSION)


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
    """The served serverInfo.version carries both the full RELEASE_VERSION
    (MAJOR.MINOR.PATCH) and the build identity — the 'version+build' handshake
    requirement.

    Independent of BLD-010: even if VERSION_WITH_BUILD were to collapse to one
    part, this asserts both substrings are present in the served value.
    """
    if build_info.UNKNOWN in (build_info.RELEASE_VERSION, build_info.BUILD_IDENTITY):
        pytest.skip("degraded environment: RELEASE_VERSION or BUILD_IDENTITY is unknown")
    from sage import mcp_server

    server = mcp_server.build_partitioned_server("sage")
    opts = server._mcp_server.create_initialization_options()
    assert build_info.RELEASE_VERSION in opts.server_version
    assert build_info.BUILD_IDENTITY in opts.server_version
