"""Shared vocabulary for the per-vault adapter-claim sweeps.

Adapter availability is a process-wide capability fixed by the installed
adapter implementations; vault configuration declares no adapters at all
(CAS-ADR-046). Caller-facing text that says otherwise sends someone
diagnosing ``adapter_not_found`` to a config file that cannot affect it --
a class of documentation drift that recurred through several correction
sweeps before the config surface itself was removed.

Two gates sweep for that wording from different directions: the OpenAPI /
Pydantic description surfaces (``tests/sage/test_openapi_conformance.py``)
and the MCP tool docstrings
(``tests/sage/test_mcp_self_documentation.py``). They share this tuple so
a marker added for one arm cannot silently leave the other narrower.
"""

from typing import Final

#: Wording that conditions adapter availability on vault configuration, or
#: that names a surface the configuration no longer has. The retired
#: section's own vocabulary counts: text still naming it, or the scan status
#: that labeled a switched-off adapter, describes something no caller can
#: act on.
ENABLEMENT_CLAIM_MARKERS: Final[tuple[str, ...]] = (
    "enabled adapter",
    "enabled source adapter",
    "enabled in the vault config",
    "adapter_disabled",
    "source_adapters",
    "adapters declared for this vault",
    "adapters configured for this vault",
)
