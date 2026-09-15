"""Tools whose ``vault_id`` does not name a registered vault.

A tool carrying ``vault_id`` ordinarily resolves it against the vault registry
and refuses an unregistered id as ``vault_not_found``. The tools named here take
the parameter for another purpose, never look it up, and so have no such
refusal to declare or to exercise. Every gate that enumerates vault-addressed
tools subtracts this set, so the exemption is stated once.
"""

from __future__ import annotations

from typing import Final

NOT_REGISTRY_RESOLVED: Final[dict[str, str]] = {
    "get_default_vault_config": (
        "vault_id names the vault a caller would create; the scaffold is built "
        "without looking up a registered vault"
    ),
}
