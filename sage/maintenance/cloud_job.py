"""Command dispatcher for the in-VNet cloud maintenance job.

One generalized Container Apps Job runs every out-of-band cloud maintenance
operation (CAS-ADR-029's off-surface carve-out): the deployed job invokes this
module, and the CI dispatch selects the operation per invocation via the
``SAGE_MAINTENANCE_COMMAND`` environment override -- never baked into the job,
so the job as deployed performs nothing until an operator starts it with a
request. Each command's entrypoint owns its own env contract, safety envelope,
and exit codes; this module only routes.
"""

from __future__ import annotations

import os
import sys

_ENV_COMMAND = "SAGE_MAINTENANCE_COMMAND"

_PURGE_COMMANDS = ("purge_document", "purge_chain", "purge_batch")


def main(argv: list[str] | None = None) -> int:
    command = os.environ.get(_ENV_COMMAND, "").strip()
    if command == "delete_vault":
        from sage.maintenance import delete_vault_cloud

        return delete_vault_cloud.main()
    if command in _PURGE_COMMANDS:
        from sage.maintenance import purge_cloud

        return purge_cloud.main()
    known = ", ".join(("delete_vault", *_PURGE_COMMANDS))
    print(f"refuse: {_ENV_COMMAND} must be one of {known}; got {command!r}.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
