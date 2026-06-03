"""Render the consolidated startup banner for the SAGE Core API process.

The banner is a single multi-line block emitted once at the end of server
startup. It gathers the facts most useful when troubleshooting a running
instance — the git build the process loaded (reused from
:mod:`sage.build_info`, never recomputed here), the API and interpreter
versions, the process id, the resolved vault root, the inventory of vaults
that loaded successfully and any that were skipped, and the mounted MCP
surfaces.

:func:`render_startup_banner` is a pure function: every datum is passed in,
so the output is fully determined by its arguments. The caller (the
``create_app`` lifespan) gathers the live values and logs the result; keeping
the formatting separate makes it directly testable without constructing an
app.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from sage.build_info import UNKNOWN

#: Stable header text. Callers and tests key on this substring to locate the
#: banner record among other startup log lines.
HEADER: str = "SAGE Core API ready"

_RULE: str = "─" * 60


def _identity_hint(build_identity: str) -> str:
    """Return a trailing parenthetical hint for a degraded build identity.

    An ``unknown`` identity means the process could not resolve a git build
    (e.g. a packaged install); a ``-dirty`` suffix means the served tree had
    uncommitted changes. A clean SHA gets no hint.
    """
    if build_identity == UNKNOWN:
        return "  (build identity unavailable — not a git checkout?)"
    if build_identity.endswith("-dirty"):
        return "  (uncommitted changes)"
    return ""


def render_startup_banner(
    *,
    build_identity: str,
    api_version: str,
    python_version: str,
    pid: int,
    vault_root: Path | None,
    loaded_vault_ids: Sequence[str],
    skipped_vaults: Sequence[tuple[str, str]],
    mcp_mounts: Sequence[str],
) -> str:
    """Render the multi-line startup banner.

    Args:
        build_identity: The running build identity (short SHA, optionally with
            a ``-dirty`` suffix, or the ``unknown`` sentinel).
        api_version: The FastAPI application version string.
        python_version: The interpreter version (e.g. ``"3.14.0"``).
        pid: The server process id.
        vault_root: The resolved vault-root directory, or ``None`` when the
            app was built from explicit configs rather than a discovered root.
        loaded_vault_ids: Ids of vaults that initialized successfully.
        skipped_vaults: ``(config_path, reason)`` pairs for vaults that failed
            to load and were skipped.
        mcp_mounts: Mounted MCP surface paths (e.g. ``"/mcp"``, ``"/mcp_admin"``).

    Returns:
        A multi-line banner string suitable for a single log record.
    """
    root_text = str(vault_root) if vault_root is not None else "(none)"
    loaded_text = ", ".join(loaded_vault_ids) if loaded_vault_ids else "—"
    skipped_text = (
        "; ".join(f"{path} ({reason})" for path, reason in skipped_vaults)
        if skipped_vaults
        else "none"
    )
    mounts_text = ", ".join(mcp_mounts) if mcp_mounts else "—"

    lines = [
        _RULE,
        f" {HEADER}  ·  build {build_identity}{_identity_hint(build_identity)}  ·  v{api_version}",
        f" python {python_version}  ·  pid {pid}",
        f" vault root: {root_text}",
        f" vaults loaded ({len(loaded_vault_ids)}): {loaded_text}",
        f" vaults skipped ({len(skipped_vaults)}): {skipped_text}",
        f" MCP mounts: {mounts_text}",
        _RULE,
    ]
    return "\n".join(lines)
