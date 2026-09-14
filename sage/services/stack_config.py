"""The stack-wide configuration report both request surfaces serve.

Stack-wide configuration governs resources whose enforcement spans the whole
process, such as the abstraction-provider singleton; per-vault settings live in
the vault configuration. The report is the loaded configuration dumped whole,
so a field left unset is reported as null rather than omitted: answering what
the stack is configured with by leaving the unconfigured parts out would
misstate it. Its shape is defined by ``docs/fs/sage/sage_core_config.schema.json``.
"""

from __future__ import annotations

from typing import Any


def get_stack_config_report() -> dict[str, Any]:
    """Return the loaded stack configuration as JSON-ready data."""
    from sage.mcp_init import get_stack_config

    return get_stack_config().model_dump(mode="json")
