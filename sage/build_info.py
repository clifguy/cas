"""Runtime build and version identity for the SAGE server process.

A long-running server process holds its Python imports until it is restarted,
so it can silently serve code older than the working tree. This module
captures the git build the process was started from — a short commit SHA plus
a ``-dirty`` marker when the checked-out tree carried uncommitted *tracked*
changes — and surfaces it, together with the release version, to connecting
MCP clients.

It also single-sources the API/release version. The version is VCS-derived
(computed from git tags + commits at build time) and read once here from the
installed distribution metadata, then reduced to its stable base release
segment so the value does not drift between release tags. The FastAPI app
version, the OpenAPI document, the startup banner, and the MCP handshake all
read it from here — there is no second read site.

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

#: Matches the leading numeric release segment (``N``, ``N.N`` or ``N.N.N``) of
#: a PEP 440 version, ignoring any pre/post/dev/local suffix.
_RELEASE_RE = re.compile(r"^\d+(?:\.\d+){0,2}")

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


def _base_release(version: str) -> str:
    """Return the leading numeric release segment of a PEP 440 version.

    ``"1.0.0.post3.dev2+gabc1234"`` → ``"1.0.0"``; ``"1.2"`` → ``"1.2"``.
    Reducing to the base release keeps the value stable between release tags,
    so surfaces that report it do not churn on every commit. Returns
    :data:`UNKNOWN` when the string carries no leading numeric release
    (defensive; a VCS-derived version always does).
    """
    match = _RELEASE_RE.match(version)
    return match.group(0) if match else UNKNOWN


def _compute_api_version() -> str:
    """Return the public release version of the installed distribution.

    Reads the version the build backend resolved from VCS state at install
    time and reduces it to its base release segment. Outside an installed
    distribution (package metadata absent) it degrades to :data:`UNKNOWN`,
    mirroring the build-identity fallback.
    """
    try:
        return _base_release(importlib.metadata.version(_DIST_NAME))
    except importlib.metadata.PackageNotFoundError:
        return UNKNOWN


def _compose_version_with_build(api_version: str, build_identity: str) -> str:
    """Compose the ``<version>+<build>`` string surfaced on the MCP handshake.

    With both parts known: ``"1.0.0+cc019b8"``. When either side is
    :data:`UNKNOWN`, fall back to the known side rather than emit an
    ``unknown`` fragment; when both are unknown, return :data:`UNKNOWN`.
    """
    api_unknown = api_version == UNKNOWN
    build_unknown = build_identity == UNKNOWN
    if api_unknown and build_unknown:
        return UNKNOWN
    if api_unknown:
        return build_identity
    if build_unknown:
        return api_version
    return f"{api_version}+{build_identity}"


def _render_instructions(version_with_build: str) -> str:
    """Render the one-line MCP ``instructions`` string carrying the running
    version and build identity (e.g. ``1.0.0+cc019b8``)."""
    return (
        f"SAGE running build: {version_with_build}. This is the running "
        "version and git build this server process loaded at startup. If the "
        "build differs from the repository HEAD (or shows '-dirty' or "
        "'unknown'), the running process may be serving stale code — restart "
        "Claude Code to reload it."
    )


#: The build identity captured once at import time, frozen for the process.
BUILD_IDENTITY: str = _compute_build_identity(_SAGE_PKG_DIR)

#: The public API/release version (base release segment, stable between tags),
#: read once at import from the installed distribution metadata.
API_VERSION: str = _compute_api_version()

#: The composed ``<version>+<build>`` string (e.g. ``1.0.0+cc019b8``) advertised
#: as the MCP ``serverInfo.version``, frozen at import.
VERSION_WITH_BUILD: str = _compose_version_with_build(API_VERSION, BUILD_IDENTITY)

#: The MCP ``instructions`` string surfacing :data:`VERSION_WITH_BUILD`, frozen
#: at import so every served server advertises the same import-time value.
SERVER_INSTRUCTIONS: str = _render_instructions(VERSION_WITH_BUILD)
