"""Save read-only served-contract and inventory evidence after explicit invocation."""

from . import client
from .config import settings
from .recovery_checks import atomic, require


def main():
    cfg = settings()
    for name, path in [
        ("openapi", "/openapi.json"),
        ("config", f"/sage_vaults/{cfg.vault_id}/config"),
        ("health", "/health"),
        ("inventory", "/sage_vaults"),
    ]:
        response = client.request(path, timeout=30)
        require(response["status"] == 200, "Read-only snapshot failed: " + name)
        atomic(cfg.root / (name + ".json"), response)
