"""Cloud-profile whole-vault teardown entrypoint (in-VNet Container Apps Job).

The cloud stores a vault's durable state in two places a developer laptop cannot
reach: the Postgres schema on an Entra-only, VNet-integrated Flexible Server, and
the retained-source tree in a SharePoint document library reached over Microsoft
Graph under the workload's managed identity (CAS-ADR-043). So the cloud teardown
cannot run from the local CLI; it runs as a short-lived in-VNet Container Apps Job
dispatched through CI, reusing the shared teardown core and safety envelope
(:func:`sage.maintenance.delete_vault.delete_vault`) with the cloud bindings
injected. Like the local CLI, this is an out-of-band operator entrypoint -- not an
MCP tool or REST route (CAS-ADR-034's uniform-auth admin surface carries no
destructive API).

The teardown request is per-invocation and arrives as environment variables the CI
dispatch applies to the job before starting it (never baked into the job's
deployed template): the vault id, a
typed confirmation that must match it, and the apply / snapshot flags. Everything
else -- the Postgres coordinates and the SharePoint site/drive -- is the deployed
configuration the job's image already carries (``get_stack_config``), resolved to
the cloud bindings under the workload managed identity (``AZURE_CLIENT_ID``).

Safety envelope, adapted to the CI/in-cloud context:
- Dry-run is the default; ``SAGE_DELETE_APPLY`` must be truthy for any state change.
- Typed confirmation is preserved, and enforced here rather than inherited:
  ``SAGE_DELETE_CONFIRM`` must equal the vault id, for every id. CI has no
  interactive stdin, so the confirmation is supplied as a workflow input the
  operator retypes, threaded on to the core as its prompt; but this arm checks it
  first and refuses a mismatch before binding anything. The check is its own
  because the core carves the literal vault id ``test`` out of the confirmation
  entirely -- correct for the interactive local path, where retyping an ephemeral
  workstation or CI vault is friction with no safety return, and wrong here, where
  the same id on a deployed tenant would otherwise tear down with
  ``SAGE_DELETE_CONFIRM`` never consulted and any value passing. Each context
  answers for itself.
- Snapshot-before-destroy is ON by default. The container filesystem is ephemeral,
  so the snapshot is pushed to a durable SharePoint archive folder -- a sibling of
  the vault tree, so it survives the vault-folder delete -- before any destruction.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from sage.maintenance._cloud_env import config_from_env as _config_from_env
from sage.maintenance._cloud_env import truthy as _truthy
from sage.maintenance.delete_vault import delete_vault

# Per-invocation teardown request, applied to the job's environment by the CI
# dispatch (``az containerapp job update --set-env-vars``) immediately before
# each start — never baked into the job's deployed template.
_ENV_VAULT_ID = "SAGE_DELETE_VAULT_ID"
_ENV_CONFIRM = "SAGE_DELETE_CONFIRM"
_ENV_APPLY = "SAGE_DELETE_APPLY"
_ENV_SNAPSHOT = "SAGE_DELETE_SNAPSHOT"
_ENV_REASON = "SAGE_DELETE_REASON"

# The drive-root folder the snapshot is archived under -- a sibling of the vault
# tree (root_path), so it survives the vault-folder delete and is invisible to
# vault discovery (which enumerates only root_path's children).
_ARCHIVE_ROOT = "_teardown_archives"


def upload_teardown_archive(client, vault_id: str, run_dir: Path) -> None:
    """Snapshot sink: manifest the vault's sources, then push every snapshot file to
    the durable SharePoint archive.

    Runs as the teardown's snapshot sink -- before any destruction -- so the source
    enumeration sees the live vault folder. Writes the manifest into ``run_dir``
    alongside the schema dump, then uploads every file in ``run_dir`` to
    ``_teardown_archives/<run_dir name>/`` at the drive root, which is a sibling of
    the vault tree and so outlives the vault-folder delete.
    """
    manifest = client.list_sources(vault_id)
    (run_dir / "sources_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    prefix = f"{_ARCHIVE_ROOT}/{run_dir.name}"
    for path in sorted(run_dir.iterdir()):
        if path.is_file():
            client.write_archive(f"{prefix}/{path.name}", path.read_bytes())


async def _cloud_dump_password() -> str:
    """Mint an Entra access token for the cloud Postgres endpoint, for pg_dump.

    The server is Entra-only, so the ``pg_dump`` subprocess cannot use a stored
    password; it is handed a fresh managed-identity token as ``PGPASSWORD`` (the
    same token the storage pool injects as the libpq password, minted for the same
    scope). Short-lived, minted once per run.
    """
    from sage.storage.postgres.managed_identity import (
        POSTGRES_AAD_SCOPE,
        get_postgres_credential,
    )

    token = await get_postgres_credential().get_token(POSTGRES_AAD_SCOPE)
    return token.token


def _build_dump_runner(password: str):
    def run(argv: list[str]) -> None:
        # argv is the fixed list ``build_schema_dump_argv`` builds (no shell, no
        # untrusted interpolation); PGPASSWORD carries the Entra token so pg_dump
        # authenticates to the Entra-only server.
        subprocess.run(argv, check=True, env={**os.environ, "PGPASSWORD": password})  # noqa: S603

    return run


async def _run_cloud(
    *, vault_id: str, confirm: str, reason: str, apply: bool, snapshot: bool
) -> int:
    from psycopg.conninfo import make_conninfo

    from sage.storage_binding import build_stack_storage_provisioner
    from sage.vault_source_binding import build_stack_vault_source_store
    from sage.vault_source_document_store import build_sharepoint_graph_client

    stack_config = _config_from_env(os.environ)
    source_store = build_stack_vault_source_store(stack_config, managed_identity=True)
    provisioner = build_stack_storage_provisioner(stack_config, managed_identity=True)

    pg = stack_config.postgres
    # No password in the conninfo: pg_dump reads the Entra token from PGPASSWORD.
    pg_conninfo = make_conninfo(
        host=pg.host, port=pg.port, dbname=pg.database, user=pg.user, sslmode="require"
    )

    # An ephemeral working dir on the (stateless) container filesystem; the sink
    # pushes its contents to the durable SharePoint archive before any destruction.
    snapshot_dir = Path(tempfile.mkdtemp(prefix="sage-teardown-"))

    archive_client = None
    try:
        if snapshot:
            dump_runner = _build_dump_runner(await _cloud_dump_password())
            archive_client = build_sharepoint_graph_client(
                stack_config.document_store, managed_identity=True
            )

            def snapshot_sink(run_dir: Path) -> None:
                upload_teardown_archive(archive_client, vault_id, run_dir)
        else:

            def dump_runner(_argv: list[str]) -> None:
                return None

            def snapshot_sink(_run_dir: Path) -> None:
                return None

        return await delete_vault(
            vault_id=vault_id,
            source_store=source_store,
            provisioner=provisioner,
            # unused under the document-store binding (config_locator -> None)
            vault_root=snapshot_dir,
            snapshot_dir=snapshot_dir,
            reason=reason,
            apply=apply,
            snapshot=snapshot,
            pg_conninfo=pg_conninfo,
            registry=None,
            input_fn=lambda _prompt: confirm,
            dump_runner=dump_runner,
            snapshot_sink=snapshot_sink,
        )
    finally:
        # Short-lived job: release the HTTP/aiohttp clients deterministically at
        # shutdown so they do not surface unclosed-session warnings on exit. The
        # archive client exists only on the snapshot path; the source store's Graph
        # client and the cached Entra credential are always built.
        if archive_client is not None:
            archive_client.close()
        source_store.close()
        from sage.storage.postgres.managed_identity import close_postgres_credential

        await close_postgres_credential()


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    vault_id = env.get(_ENV_VAULT_ID, "").strip()
    if not vault_id:
        print(f"refuse: {_ENV_VAULT_ID} is required.", file=sys.stderr)
        return 2
    apply = _truthy(env.get(_ENV_APPLY), default=False)
    confirm = env.get(_ENV_CONFIRM, "")
    # Typed confirmation, decided here rather than inherited from the shared core.
    # The core carves the literal id ``test`` out of its prompt -- right for the
    # interactive local path, where retyping an ephemeral workstation or CI vault
    # is friction with no safety return -- but this arm reaches deployed-tenant
    # state, so it refuses every mismatch, that id included, before binding
    # anything. Gated on apply like the core's own check: a dry-run destroys
    # nothing, and previewing the plan should not demand the confirmation.
    if apply and confirm != vault_id:
        print(f"refuse: {_ENV_CONFIRM} did not match {_ENV_VAULT_ID}. Aborting.", file=sys.stderr)
        return 3
    return asyncio.run(
        _run_cloud(
            vault_id=vault_id,
            confirm=confirm,
            reason=env.get(_ENV_REASON, "").strip() or "cloud teardown (in-VNet job)",
            apply=apply,
            snapshot=_truthy(env.get(_ENV_SNAPSHOT), default=True),
        )
    )


if __name__ == "__main__":
    sys.exit(main())
