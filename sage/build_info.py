"""Runtime build identity for the SAGE server process.

A long-running server process holds its Python imports until it is restarted,
so it can silently serve code older than the working tree. This module
captures the git build the process was started from — a short commit SHA plus
a ``-dirty`` marker when the checked-out tree carried uncommitted *tracked*
changes — and surfaces it to connecting MCP clients.

The identity is computed **once at import** and frozen in module constants for
the lifetime of the process. It therefore reflects the code the running
process actually loaded, never a value re-read from disk per request: if it
disagrees with the repository HEAD, the process is serving stale code. Outside
a git checkout (e.g. a packaged install) it degrades to the literal
``"unknown"`` rather than failing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

#: Directory of this package; git resolves the enclosing repo/worktree from
#: here regardless of the process working directory.
_SAGE_PKG_DIR: Path = Path(__file__).resolve().parent

#: Sentinel returned when no git build identity can be determined.
UNKNOWN: str = "unknown"

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


def _render_instructions(identity: str) -> str:
    """Render the one-line MCP ``instructions`` string carrying ``identity``."""
    return (
        f"SAGE running build: {identity}. This is the git build this server "
        "process loaded at startup. If it differs from the repository HEAD "
        "(or shows '-dirty' or 'unknown'), the running process may be serving "
        "stale code — restart Claude Code to reload it."
    )


#: The build identity captured once at import time, frozen for the process.
BUILD_IDENTITY: str = _compute_build_identity(_SAGE_PKG_DIR)

#: The MCP ``instructions`` string surfacing :data:`BUILD_IDENTITY`, frozen at
#: import so every served server advertises the same import-time value.
SERVER_INSTRUCTIONS: str = _render_instructions(BUILD_IDENTITY)
