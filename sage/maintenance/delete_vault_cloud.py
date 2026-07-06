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
dispatch overrides on the job execution (never baked into the job): the vault id, a
typed confirmation that must match it, and the apply / snapshot flags. Everything
else -- the Postgres coordinates and the SharePoint site/drive -- is the deployed
configuration the job's image already carries (``get_stack_config``), resolved to
the cloud bindings under the workload managed identity (``AZURE_CLIENT_ID``).

Safety envelope, adapted to the CI/in-cloud context:
- Dry-run is the default; ``SAGE_DELETE_APPLY`` must be truthy for any state change.
- Typed confirmation is preserved: ``SAGE_DELETE_CONFIRM`` must equal the vault id
  (the core refuses otherwise). CI has no interactive stdin, so the confirmation is
  supplied as a workflow input the operator retypes, threaded here as the core's
  prompt.
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
from collections.abc import Mapping
from pathlib import Path

from sage.maintenance.delete_vault import delete_vault

# Per-invocation teardown request, supplied by the CI dispatch as
# ``az containerapp job start --env-vars`` overrides (never baked into the job).
_ENV_VAULT_ID = "SAGE_DELETE_VAULT_ID"
_ENV_CONFIRM = "SAGE_DELETE_CONFIRM"
_ENV_APPLY = "SAGE_DELETE_APPLY"
_ENV_SNAPSHOT = "SAGE_DELETE_SNAPSHOT"
_ENV_REASON = "SAGE_DELETE_REASON"

# The drive-root folder the snapshot is archived under -- a sibling of the vault
# tree (root_path), so it survives the vault-folder delete and is invisible to
# vault discovery (which enumerates only root_path's children).
_ARCHIVE_ROOT = "_teardown_archives"


def _truthy(value: str | None, *, default: bool) -> bool:
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _config_from_env(env: Mapping[str, str]):
    """Build the cloud stack config from the baked job environment.

    A lifespan-less ``python -m`` job never populates the stack-config singleton, so
    the coordinates are read straight from the environment the job carries (the same
    self-contained pattern the sibling ``cloud_bootstrap`` job uses): the private
    Postgres FQDN/database and the SAGE role to connect as, and the SharePoint
    site/drive/root the document-store binding addresses. The teardown is cloud-only,
    so the profile and vault-source backend are fixed here. A missing required
    coordinate fails loud.
    """
    from sage.config import SageCoreConfig, StackDocumentStoreConfig, StackPostgresConfig

    def _required(key: str) -> str:
        value = env.get(key)
        if not value:
            raise ValueError(f"missing required environment variable {key!r}")
        return value

    return SageCoreConfig(
        profile="cloud",
        vault_source_backend="document_store",
        postgres=StackPostgresConfig(
            host=_required("PG_FQDN"),
            database=_required("PG_DATABASE"),
            user=_required("PG_USER"),
            sslmode="require",
        ),
        document_store=StackDocumentStoreConfig(
            site_id=_required("SHAREPOINT_SITE_ID"),
            drive_id=_required("SHAREPOINT_DRIVE_ID"),
            root_path=env.get("SHAREPOINT_ROOT_PATH") or "vaults",
        ),
    )


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
        vault_root=snapshot_dir,  # unused under the document-store binding (config_locator -> None)
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


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    vault_id = env.get(_ENV_VAULT_ID, "").strip()
    if not vault_id:
        print(f"refuse: {_ENV_VAULT_ID} is required.", file=sys.stderr)
        return 2
    return asyncio.run(
        _run_cloud(
            vault_id=vault_id,
            confirm=env.get(_ENV_CONFIRM, ""),
            reason=env.get(_ENV_REASON, "").strip() or "cloud teardown (in-VNet job)",
            apply=_truthy(env.get(_ENV_APPLY), default=False),
            snapshot=_truthy(env.get(_ENV_SNAPSHOT), default=True),
        )
    )


if __name__ == "__main__":
    sys.exit(main())
