"""Runtime build and version identity for the SAGE server process.

A long-running server process holds its Python imports until it is restarted,
so it can silently serve code older than the working tree. This module
captures the git build the process was started from — a short commit SHA plus
a ``-dirty`` marker when the checked-out tree carried uncommitted *tracked*
changes — and surfaces it, together with the release version, to connecting
MCP clients.

It also single-sources the project version, derived live from VCS state. The
running ``RELEASE_VERSION`` is ``MAJOR.MINOR.PATCH`` where PATCH is the commit
distance since the most recent ``vMAJOR.MINOR.0`` tag (read from ``git
describe`` at import, with the installed distribution metadata as the fallback
when no git checkout is present). ``API_VERSION`` is its ``MAJOR.MINOR``
contract segment: the FastAPI app version, the OpenAPI document, and the
committed OpenAPI specs track it, so they stay stable between minor releases.
The MCP handshake and startup banner surface the full ``RELEASE_VERSION`` with
the build identity. There is no second read site.

Every value is computed **once at import** and frozen in module constants for
the lifetime of the process. The build identity therefore reflects the code
the running process actually loaded, never a value re-read from disk per
request: if it disagrees with the repository HEAD, the process is serving
stale code. Outside a git checkout / installed distribution (e.g. a bare
source tree) both degrade to the literal ``"unknown"`` rather than failing.
"""

from __future__ import annotations

import importlib.metadata
import re
import subprocess
from pathlib import Path

#: Directory of this package; git resolves the enclosing repo/worktree from
#: here regardless of the process working directory.
_SAGE_PKG_DIR: Path = Path(__file__).resolve().parent

#: Sentinel returned when no git build identity or release version can be
#: determined.
UNKNOWN: str = "unknown"

#: Distribution name as declared in ``pyproject.toml`` ``[project].name``.
_DIST_NAME: str = "cas"

#: Matches ``git describe --long`` output ``<tag>-<distance>-g<sha>`` and
#: captures MAJOR, MINOR, and the commit distance. The tag's own patch segment
#: (``(?:\.\d+)?``) is consumed but discarded — PATCH comes from the distance.
_DESCRIBE_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.\d+)?-(\d+)-g[0-9a-f]+", re.IGNORECASE)

#: Matches a setuptools-scm ``no-guess-dev`` distribution version and captures
#: MAJOR, MINOR, and the ``.devN`` commit distance (absent → exactly on a tag).
_METADATA_RE = re.compile(r"^(\d+)\.(\d+)(?:\.\d+)?(?:\.post\d+)?(?:\.dev(\d+))?")

_GIT_TIMEOUT_S: float = 5.0


def _compute_build_identity(repo_dir: Path) -> str:
    """Return the git build identity for ``repo_dir``.

    Format: the 7-character short HEAD SHA, with ``-dirty`` appended when the
    tree has uncommitted tracked changes (staged or unstaged; untracked files
    are ignored, matching the ``git describe --dirty`` convention). Any failure
    — not a git checkout, git binary absent, timeout — yields :data:`UNKNOWN`.
    """
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],  # noqa: S607 -- 'git' resolved from PATH; constant args, internal cwd
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
        if head.returncode != 0 or not head.stdout.strip():
            return UNKNOWN
        identity = head.stdout.strip()

        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],  # noqa: S607 -- 'git' resolved from PATH; constant args, internal cwd
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
        if status.returncode == 0 and status.stdout.strip():
            identity = f"{identity}-dirty"
        return identity
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN


def _parse_describe(describe_out: str) -> str | None:
    """Map ``git describe --long`` output to ``MAJOR.MINOR.PATCH``.

    ``"v1.0.0-12-g162d19b"`` → ``"1.0.12"``. PATCH is the commit distance since
    the tag, not the tag's own third segment, so it auto-increments per commit.
    Returns ``None`` when the string does not match (caller degrades to
    :data:`UNKNOWN`).
    """
    match = _DESCRIBE_RE.match(describe_out.strip())
    if match is None:
        return None
    major, minor, distance = match.group(1), match.group(2), match.group(3)
    return f"{major}.{minor}.{int(distance)}"


def _parse_metadata_version(version: str) -> str | None:
    """Map a setuptools-scm distribution version to ``MAJOR.MINOR.PATCH``.

    The no-git fallback: ``"1.0.0.post1.dev12+g…"`` → ``"1.0.12"`` (PATCH is the
    ``.devN`` commit distance); a clean tag ``"1.0.0"`` → ``"1.0.0"`` (PATCH 0).
    Returns ``None`` for a non-version string.
    """
    match = _METADATA_RE.match(version.strip())
    if match is None:
        return None
    major, minor, distance = match.group(1), match.group(2), match.group(3)
    return f"{major}.{minor}.{int(distance) if distance is not None else 0}"


def _major_minor(version: str) -> str:
    """Reduce ``MAJOR.MINOR.PATCH`` to its ``MAJOR.MINOR`` contract segment.

    ``"1.0.12"`` → ``"1.0"``. Passes :data:`UNKNOWN` through, and degrades a
    malformed value to :data:`UNKNOWN` rather than emitting a partial string.
    """
    if version == UNKNOWN:
        return UNKNOWN
    parts = version.split(".")
    if len(parts) < 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        return UNKNOWN
    return f"{parts[0]}.{parts[1]}"


def _compute_release_version(repo_dir: Path) -> str:
    """Return the live ``MAJOR.MINOR.PATCH`` release version for ``repo_dir``.

    Runs ``git describe`` against the version-tag history and derives PATCH from
    the commit distance. Any failure — not a git checkout, no ``v*`` tag, git
    binary absent, timeout, unparseable output — yields :data:`UNKNOWN` so the
    caller can fall back to distribution metadata.
    """
    try:
        described = subprocess.run(
            ["git", "describe", "--long", "--tags", "--match", "v*"],  # noqa: S607 -- 'git' resolved from PATH; constant args, internal cwd
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
        if described.returncode != 0 or not described.stdout.strip():
            return UNKNOWN
        parsed = _parse_describe(described.stdout.strip())
        return parsed if parsed is not None else UNKNOWN
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN


def _release_from_metadata() -> str:
    """Return the ``MAJOR.MINOR.PATCH`` release version from installed metadata.

    The fallback when the process runs outside a git checkout. Reads the version
    the build backend resolved from VCS state at install time. Degrades to
    :data:`UNKNOWN` when the package metadata is absent or unparseable.
    """
    try:
        parsed = _parse_metadata_version(importlib.metadata.version(_DIST_NAME))
    except importlib.metadata.PackageNotFoundError:
        return UNKNOWN
    return parsed if parsed is not None else UNKNOWN


def _resolve_release_version() -> str:
    """Resolve the running release version: live git first, metadata fallback.

    Prefers ``git describe`` (accurate at every restart with no reinstall);
    falls back to the installed distribution metadata when no git checkout is
    present. :data:`UNKNOWN` only when neither resolves.
    """
    from_git = _compute_release_version(_SAGE_PKG_DIR)
    if from_git != UNKNOWN:
        return from_git
    return _release_from_metadata()


def _compose_version_with_build(release_version: str, build_identity: str) -> str:
    """Compose the ``<version>+<build>`` string surfaced on the MCP handshake.

    With both parts known: ``"1.0.12+cc019b8"``. When either side is
    :data:`UNKNOWN`, fall back to the known side rather than emit an
    ``unknown`` fragment; when both are unknown, return :data:`UNKNOWN`.
    """
    version_unknown = release_version == UNKNOWN
    build_unknown = build_identity == UNKNOWN
    if version_unknown and build_unknown:
        return UNKNOWN
    if version_unknown:
        return build_identity
    if build_unknown:
        return release_version
    return f"{release_version}+{build_identity}"


def _render_instructions(version_with_build: str) -> str:
    """Render the one-line MCP ``instructions`` string carrying the running
    version and build identity (e.g. ``1.0.12+cc019b8``)."""
    return (
        f"SAGE running build: {version_with_build}. This is the running "
        "version and git build this server process loaded at startup. If the "
        "build differs from the repository HEAD (or shows '-dirty' or "
        "'unknown'), the running process may be serving stale code — restart "
        "Claude Code to reload it."
    )


#: The build identity captured once at import time, frozen for the process.
BUILD_IDENTITY: str = _compute_build_identity(_SAGE_PKG_DIR)

#: The running project version ``MAJOR.MINOR.PATCH`` (PATCH = commit distance
#: since the last tag), resolved once at import from git, metadata as fallback.
RELEASE_VERSION: str = _resolve_release_version()

#: The ``MAJOR.MINOR`` contract version (major.minor of :data:`RELEASE_VERSION`):
#: the FastAPI/OpenAPI version, stable between minor releases.
API_VERSION: str = _major_minor(RELEASE_VERSION)

#: The composed ``<release>+<build>`` string (e.g. ``1.0.12+cc019b8``) advertised
#: as the MCP ``serverInfo.version``, frozen at import.
VERSION_WITH_BUILD: str = _compose_version_with_build(RELEASE_VERSION, BUILD_IDENTITY)

#: The MCP ``instructions`` string surfacing :data:`VERSION_WITH_BUILD`, frozen
#: at import so every served server advertises the same import-time value.
SERVER_INSTRUCTIONS: str = _render_instructions(VERSION_WITH_BUILD)
