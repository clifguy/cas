"""Cloud-profile document purge entrypoint (in-VNet Container Apps Job).

The cloud stores a vault's durable state on an Entra-only, VNet-integrated
Postgres Flexible Server a developer laptop cannot reach (CAS-ADR-042), so the
out-of-band purge tooling cannot run from the local CLI there; it runs as a
short-lived in-VNet Container Apps Job dispatched through CI, reusing the
purge cores and safety envelope (``sage.maintenance.purge_document`` /
``purge_chain`` / ``purge_batch``) with the cloud bindings injected. Like the
local CLIs, this is an out-of-band operator entrypoint -- not an MCP tool or
REST route (CAS-ADR-029's No-Delete Invariant keeps document removal off the
request surface).

The purge request is per-invocation and arrives as environment variables the CI
dispatch applies to the job before starting it (never baked into the job's
deployed template): the mode
selector, the vault id, the target, the typed confirmation(s), and the apply
flag. Everything else -- the Postgres coordinates and the SharePoint
site/drive -- is the deployed configuration the job's image already carries,
resolved to the cloud bindings under the workload managed identity
(``AZURE_CLIENT_ID``).

Safety envelope, adapted to the CI/in-cloud context:
- Dry-run is the default; ``SAGE_PURGE_APPLY`` must be truthy for any state change.
- Typed confirmation is preserved: CI has no interactive stdin, so the
  confirmation values are workflow inputs the operator retypes, threaded here
  as the cores' prompts (``SAGE_PURGE_CONFIRM``, plus
  ``SAGE_PURGE_CONFIRM_LENGTH`` for the chain mode's second prompt).
- The audit-first record lands in the vault schema's ``purge_audit`` Postgres
  table before any removal, exactly as it does under the local profile.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable, Mapping
from datetime import datetime, timezone

from sage.maintenance._cloud_env import config_from_env as _config_from_env
from sage.maintenance._cloud_env import truthy as _truthy
from sage.maintenance._internal import open_vault_stores
from sage.maintenance.purge_batch import purge_batch
from sage.maintenance.purge_chain import purge_chain
from sage.maintenance.purge_document import purge_document

# The mode selector, shared with the dispatcher that routes to this entrypoint.
_ENV_COMMAND = "SAGE_MAINTENANCE_COMMAND"

# Per-invocation purge request, applied to the job's environment by the CI
# dispatch (``az containerapp job update --set-env-vars``) immediately before
# each start — never baked into the job's deployed template.
_ENV_VAULT_ID = "SAGE_PURGE_VAULT_ID"
_ENV_REASON = "SAGE_PURGE_REASON"
_ENV_APPLY = "SAGE_PURGE_APPLY"
_ENV_CONFIRM = "SAGE_PURGE_CONFIRM"
_ENV_DOCUMENT_ID = "SAGE_PURGE_DOCUMENT_ID"
_ENV_HEAD_ID = "SAGE_PURGE_HEAD_ID"
_ENV_EDGE_TYPE = "SAGE_PURGE_EDGE_TYPE"
_ENV_ALLOW_BRANCHED = "SAGE_PURGE_ALLOW_BRANCHED"
_ENV_CONFIRM_LENGTH = "SAGE_PURGE_CONFIRM_LENGTH"
_ENV_INGESTED_SINCE = "SAGE_PURGE_INGESTED_SINCE"
_ENV_INGESTED_UNTIL = "SAGE_PURGE_INGESTED_UNTIL"

_MODES = ("purge_document", "purge_chain", "purge_batch")

# Which env var carries each mode's target (validated before any resolution).
_MODE_TARGET_ENV = {
    "purge_document": _ENV_DOCUMENT_ID,
    "purge_chain": _ENV_HEAD_ID,
    "purge_batch": _ENV_INGESTED_SINCE,
}


def _parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp; a naive input is treated as UTC."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _sequential_input_fn(values: list[str]) -> Callable[[str], str]:
    """Answer each prompt with the next pre-supplied confirmation value."""
    answers = iter(values)
    return lambda _prompt: next(answers, "")


async def _run_cloud(*, command: str, vault_id: str, env: Mapping[str, str]) -> int:
    from sage.storage_binding import build_stack_storage_provisioner
    from sage.vault_source_binding import build_stack_vault_source_store

    stack_config = _config_from_env(env)
    source_store = build_stack_vault_source_store(stack_config, managed_identity=True)
    provisioner = build_stack_storage_provisioner(stack_config, managed_identity=True)

    opened = await open_vault_stores(vault_id, source_store=source_store, provisioner=provisioner)
    if opened is None:
        print(f"error: vault config not found for vault {vault_id!r}", file=sys.stderr)
        return 2
    graph_store, content_store, audit_sink, handle = opened

    reason = env.get(_ENV_REASON, "").strip() or "cloud purge (in-VNet job)"
    apply = _truthy(env.get(_ENV_APPLY), default=False)
    confirm = env.get(_ENV_CONFIRM, "")

    try:
        if command == "purge_document":
            return await purge_document(
                graph_store=graph_store,
                content_store=content_store,
                audit_sink=audit_sink,
                document_id=env[_ENV_DOCUMENT_ID].strip(),
                reason=reason,
                apply=apply,
                input_fn=_sequential_input_fn([confirm]),
            )
        if command == "purge_chain":
            return await purge_chain(
                graph_store=graph_store,
                content_store=content_store,
                audit_sink=audit_sink,
                head_id=env[_ENV_HEAD_ID].strip(),
                reason=reason,
                edge_type=env.get(_ENV_EDGE_TYPE, "").strip() or "supersedes",
                apply=apply,
                allow_branched=_truthy(env.get(_ENV_ALLOW_BRANCHED), default=False),
                input_fn=_sequential_input_fn([confirm, env.get(_ENV_CONFIRM_LENGTH, "")]),
            )
        # purge_batch -- the selector was validated in main().
        until_raw = env.get(_ENV_INGESTED_UNTIL, "").strip()
        return await purge_batch(
            graph_store=graph_store,
            content_store=content_store,
            audit_sink=audit_sink,
            since=_parse_timestamp(env[_ENV_INGESTED_SINCE].strip()),
            until=_parse_timestamp(until_raw) if until_raw else None,
            reason=reason,
            apply=apply,
            input_fn=_sequential_input_fn([confirm]),
        )
    finally:
        await handle.close()


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    command = env.get(_ENV_COMMAND, "").strip()
    if command not in _MODES:
        print(
            f"refuse: {_ENV_COMMAND} must be one of {', '.join(_MODES)}; got {command!r}.",
            file=sys.stderr,
        )
        return 2
    vault_id = env.get(_ENV_VAULT_ID, "").strip()
    if not vault_id:
        print(f"refuse: {_ENV_VAULT_ID} is required.", file=sys.stderr)
        return 2
    target_env = _MODE_TARGET_ENV[command]
    if not env.get(target_env, "").strip():
        print(f"refuse: {target_env} is required for {command}.", file=sys.stderr)
        return 2
    if command == "purge_batch":
        # Validate the window before any resolution, so a malformed timestamp
        # is a usage error rather than a mid-run failure.
        try:
            _parse_timestamp(env[_ENV_INGESTED_SINCE].strip())
            if env.get(_ENV_INGESTED_UNTIL, "").strip():
                _parse_timestamp(env[_ENV_INGESTED_UNTIL].strip())
        except ValueError as exc:
            print(f"refuse: invalid ISO-8601 timestamp ({exc}).", file=sys.stderr)
            return 2
    return asyncio.run(_run_cloud(command=command, vault_id=vault_id, env=env))


if __name__ == "__main__":
    sys.exit(main())
