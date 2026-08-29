"""Cloud-profile bulk-reabstract entrypoint (in-VNet Container Apps Job).

The cloud stores a vault's durable state on an Entra-only, VNet-integrated
Postgres Flexible Server a developer laptop cannot reach (CAS-ADR-042), so the
local operator scripts cannot recover a cloud vault's stranded abstracts; the
sweep runs as a short-lived in-VNet Container Apps Job dispatched through CI,
reusing the ``sage.maintenance.reabstract_bulk`` core with the cloud bindings
resolved under the workload managed identity (``AZURE_CLIENT_ID``).

The job is the right vehicle rather than a request-surface tool because the work
is long: abstraction costs seconds per document, so a several-hundred-document
sweep runs for tens of minutes -- well past any synchronous call's practical
ceiling.

The request is per-invocation and arrives as environment variables the CI
dispatch applies to the job before starting it (never baked into the job's
deployed template): the vault id, the status selector, an optional worklist cap,
the typed confirmation, and the apply flag. Everything else -- the Postgres
coordinates, the SharePoint site/drive, the Key Vault URI, and the abstraction
model -- is the deployed configuration the job's image already carries.

Unlike the purge entrypoints this one needs a live abstraction provider, so it
builds the full per-vault service stack rather than opening bare stores: a
lifespan-less ``python -m`` job populates the stack-config singleton itself and
then lets the ordinary profile seams resolve storage and abstraction
(CAS-ADR-030, CAS-ADR-042). The abstraction queue's worker starts lazily on the
first dispatch, so only its shutdown is explicit.

Safety envelope, adapted to the CI/in-cloud context:
- Dry-run is the default; ``SAGE_REABSTRACT_APPLY`` must be truthy before any
  document is dispatched, so a mistyped request costs a listing, not spend.
- Typed confirmation is preserved: CI has no interactive stdin, so the operator
  retypes the vault id as a workflow input and a mismatch refuses before the
  vault is opened. A sweep is not destructive, but it is chargeable work against
  a named vault, and a wrong-vault dispatch is the mistake worth catching.
- The status selector and the abstraction model are validated up front, so a
  misconfiguration is a usage error rather than a mid-run failure.
- Re-dispatch follows the selector: the worklist is recomputed from live
  pipeline status each run, so a ``failed`` selector resumes (documents already
  recovered drop out) while ``all`` redoes every document. This is what makes a
  platform-level retry of an interrupted sweep safe; the destructive commands
  run with no retry at all (CAS-ADR-029).
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Mapping

from sage.maintenance._cloud_env import config_from_env as _config_from_env
from sage.maintenance._cloud_env import truthy as _truthy
from sage.maintenance._internal import resolve_vault_config
from sage.maintenance.reabstract_bulk import parse_status_selector, reabstract_bulk

# Per-invocation request, applied to the job's environment by the CI dispatch
# (``az containerapp job update --set-env-vars``) immediately before each start.
_ENV_VAULT_ID = "SAGE_REABSTRACT_VAULT_ID"
_ENV_STATUSES = "SAGE_REABSTRACT_STATUSES"
_ENV_LIMIT = "SAGE_REABSTRACT_LIMIT"
_ENV_CONFIRM = "SAGE_REABSTRACT_CONFIRM"
_ENV_APPLY = "SAGE_REABSTRACT_APPLY"
_ENV_REASON = "SAGE_REABSTRACT_REASON"

# Standing deployed coordinate: the abstraction model the whole stack shares.
_ENV_ABSTRACTION_MODEL = "ABSTRACTION_MODEL"


async def _run_cloud(
    *,
    vault_id: str,
    statuses: frozenset[str],
    limit: int | None,
    reason: str,
    apply: bool,
    env: Mapping[str, str],
) -> int:
    from sage.mcp_init import (
        initialize_services,
        resolve_stack_abstraction_provider,
        resolve_stack_vault_source_store,
        set_stack_config,
    )

    stack_config = _config_from_env(env)
    # A lifespan-less job has no populated singleton, so every resolution below --
    # the vault-source store, the durable-storage provisioner the service
    # construction reaches for, and the abstraction provider -- would otherwise
    # assemble the default local profile's bindings instead of the cloud ones.
    set_stack_config(stack_config)
    source_store = resolve_stack_vault_source_store(stack_config)

    try:
        config = resolve_vault_config(source_store, vault_id)
        if config is None:
            print(f"error: vault config not found for vault {vault_id!r}", file=sys.stderr)
            return 2

        services = await initialize_services(
            config,
            abstraction_provider=resolve_stack_abstraction_provider(stack_config),
        )
        try:
            return await reabstract_bulk(
                graph_store=services.graph_store,
                ingestion_service=services.ingestion_service,
                vault_id=vault_id,
                statuses=statuses,
                limit=limit,
                reason=reason,
                apply=apply,
            )
        finally:
            await services.ingestion_service.stop_worker()
            services.close_timing()
            await services.close_storage()
    finally:
        # Short-lived job: release the source store's Graph client and the cached
        # Entra credential's aiohttp session at shutdown, so neither surfaces an
        # unclosed-session warning on exit. Runs even on the config-not-found path.
        source_store.close()
        from sage.storage.postgres.managed_identity import close_postgres_credential

        await close_postgres_credential()


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    vault_id = env.get(_ENV_VAULT_ID, "").strip()
    if not vault_id:
        print(f"refuse: {_ENV_VAULT_ID} is required.", file=sys.stderr)
        return 2
    if env.get(_ENV_CONFIRM, "").strip() != vault_id:
        print(
            f"refuse: typed confirmation did not match the vault id. Retype "
            f"{vault_id!r} to confirm. Nothing was dispatched.",
            file=sys.stderr,
        )
        return 2
    if not env.get(_ENV_ABSTRACTION_MODEL, "").strip():
        print(
            f"refuse: {_ENV_ABSTRACTION_MODEL} is not configured on this job, so "
            "no abstract can be generated.",
            file=sys.stderr,
        )
        return 2
    try:
        statuses = parse_status_selector(env.get(_ENV_STATUSES))
    except ValueError as exc:
        print(f"refuse: {exc}", file=sys.stderr)
        return 2
    limit_raw = env.get(_ENV_LIMIT, "").strip()
    limit: int | None = None
    if limit_raw:
        try:
            limit = int(limit_raw)
        except ValueError:
            print(f"refuse: {_ENV_LIMIT} must be an integer; got {limit_raw!r}.", file=sys.stderr)
            return 2
        if limit < 1:
            print(f"refuse: {_ENV_LIMIT} must be at least 1; got {limit}.", file=sys.stderr)
            return 2
    return asyncio.run(
        _run_cloud(
            vault_id=vault_id,
            statuses=statuses,
            limit=limit,
            reason=env.get(_ENV_REASON, "").strip() or "cloud reabstract (in-VNet job)",
            apply=_truthy(env.get(_ENV_APPLY), default=False),
            env=env,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
