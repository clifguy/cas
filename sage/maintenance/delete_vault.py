"""Whole-vault teardown (permanently out-of-band per CAS-ADR-029/034).

Permanently retires one SAGE vault: drops the vault's Postgres schema (storage
tenancy is one schema per vault in the shared database, CAS-ADR-042), removes its
retained-source tree and its ``vault_config.yaml`` through the vault-source port,
removes the filesystem ``brain_root``, and evicts it from a live server's in-memory
registry. It is the asymmetric opposite of ``maint_create_vault``: create is a
reachable, idempotent maintenance operation; delete is the most irreversible operation
in SAGE, so it is an out-of-band entrypoint, **not** an MCP tool or REST route
(CAS-ADR-034's uniform-auth maintenance surface carries no destructive API). This module
is unreachable from the SAGE Core API and MCP server by architectural invariant
(the import-topology test).

The ``delete_vault`` core is binding-agnostic -- the source-store and
storage-provisioner ports are injected -- so the same ordered envelope drives both
the local-profile CLI here (``main``) and the cloud in-VNet teardown job
(``sage.maintenance.delete_vault_cloud``), which injects the document-store binding
(a SharePoint library over Microsoft Graph) and the managed-identity Postgres
provisioner (CAS-ADR-043). A binding with no filesystem locator has its source tree
removed through the port by vault id, like the config; the filesystem removals below
apply only to the local binding's on-disk trees.

Safeguards:
- Dry-run is the default. ``--apply`` is required for any state change; it prints
  the exact schema, trees, and config it would remove.
- The Postgres teardown is ``DROP SCHEMA IF EXISTS ... CASCADE`` against the shared
  database -- schema-scoped by construction, structurally incapable of
  ``DROP DATABASE`` (which would destroy every vault).
- Each filesystem target is realpath-resolved and asserted to be a strict
  descendant of the process-bound vault root (``get_vault_root``, CAS-ADR-043)
  before any ``rmtree``. The guard is per-target: a target resolving outside the
  root is refused for that target only -- left in place and recorded as skipped in
  the receipt -- while the in-root work (schema drop, config and enclosing-directory
  removal) still runs. An out-of-root tree is never deleted.
- ``--apply`` requires typed confirmation of the vault_id at the prompt. The only
  auto-confirm carve-out is the literal disposable ``test`` vault.
- Snapshot-before-destroy is ON by default (``--no-snapshot`` opts out): a
  ``pg_dump`` of the schema plus a source-file manifest to a timestamped archive
  **outside** the vault root. A failed snapshot halts before any destruction.
- Audit-first: the deletion record is written (outside the vault) **before** the
  first destructive step, so the worst-case partial-failure outcome is "audit
  record, no delete", never "delete, no audit record". The graph/content schema
  drop and the filesystem removals are separate coordinated operations with no
  cross-store atomicity (CAS-ADR-042 weakest-binding).
- Idempotent and resumable: already-missing pieces are tolerated (``IF EXISTS``
  drop, guarded-but-skipped removal, ``missing_ok`` config unlink); each recursive
  removal tolerates a concurrent writer repopulating a directory mid-removal
  (bounded retry, CAS-ADR-016); and a resume with the default snapshot ON tolerates
  an already-dropped schema (the schema dump is skipped). A deletion receipt is
  written outside the vault.

Usage::

    .venv/bin/python -m sage.maintenance.delete_vault \\
        --vault VAULT --reason TEXT [--apply] [--no-snapshot] [--snapshot-dir DIR]

The standalone CLI runs in its own process and cannot see a running server's
in-memory registry; stop or restart the server after a delete so no stale pool
outlives the dropped schema (the deleted vault vanishes on the server's next
discovery). The in-process eviction path (``registry``) is for callers that run in
the same process as the server that loaded the vault.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sage.vault_source_binding import (
    DiscoveredVault,
    VaultRootEscapeError,
    remove_tree_tolerating_concurrent_writer,
    resolve_and_assert_within_root,
)

if TYPE_CHECKING:
    from sage.storage_binding import VaultStorageProvisioner
    from sage.vault_source_binding import VaultSourceStore

_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"


def default_deletions_dir() -> Path:
    """The default snapshot + receipt folder: ``~/sage_vaults_deletions``.

    A sibling of the vault tree (``~/sage_vaults``) and the Postgres backup folder
    (``~/sage_vaults_backups``), and -- critically -- **outside** the vault root,
    so a vault's snapshot and deletion receipt survive the removal of the vault
    itself.
    """
    return Path.home() / "sage_vaults_deletions"


def build_schema_dump_argv(conninfo: str, schema: str, out_file: Path) -> list[str]:
    """Argv for a schema-scoped ``pg_dump`` in custom (``pg_restore``-able) format.

    ``-Fc`` is the custom archive format; ``--schema`` restricts the dump to the
    one vault's schema so a snapshot never carries another vault's rows.
    """
    return ["pg_dump", "-Fc", "--schema", schema, "-d", conninfo, "-f", str(out_file)]


def _run_pg_dump(argv: list[str]) -> None:
    """Run ``pg_dump``, raising ``CalledProcessError`` on a non-zero exit."""
    # argv is a fixed list built by ``build_schema_dump_argv`` (no shell, no
    # untrusted-string interpolation); the schema element is a validated
    # identifier. Mirrors the operator backup runner's ``subprocess.run`` call.
    subprocess.run(argv, check=True)  # noqa: S603


def _noop_sink(run_dir: Path) -> None:
    """The default snapshot sink: a no-op.

    Under the local profile the snapshot files ``delete_vault`` writes into
    ``run_dir`` are already durable (the dir is outside the vault root), so no
    further copy is needed. A profile whose snapshot dir is ephemeral -- the
    in-cloud teardown job's container filesystem -- injects a sink that copies the
    snapshot to a durable off-box store before any destruction.
    """


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def _source_manifest(storage_root: Path) -> list[dict]:
    """A flat manifest of the files under ``storage_root`` (relpath + size).

    Records what source files existed at delete time so the snapshot is a complete
    record alongside the schema dump. Missing root -> empty manifest.
    """
    if not storage_root.exists():
        return []
    entries: list[dict] = []
    for p in sorted(storage_root.rglob("*")):
        if p.is_file():
            entries.append({"path": str(p.relative_to(storage_root)), "size": p.stat().st_size})
    return entries


async def _evict_from_registry(registry: dict[str, Any], vault_id: str) -> None:
    """Evict a loaded vault from a live in-process registry (the reload teardown sequence).

    Runs the same teardown order the registry reload uses -- stop the ingestion
    worker, close the timing flusher, then close the storage handle (releasing the
    per-vault Postgres pool) -- and drops the registry entry, so no stale pool or
    worker outlives the schema drop. Only reached when the delete runs in the same
    process as the server that loaded the vault; the standalone CLI passes no
    registry.
    """
    services = registry.get(vault_id)
    if services is None:
        return
    await services.ingestion_service.stop_worker()
    services.close_timing()
    await services.close_storage()
    registry.pop(vault_id, None)


async def delete_vault(
    *,
    vault_id: str,
    source_store: VaultSourceStore,
    provisioner: VaultStorageProvisioner,
    vault_root: Path,
    snapshot_dir: Path,
    reason: str,
    apply: bool,
    snapshot: bool = True,
    pg_conninfo: str = "",
    registry: dict[str, Any] | None = None,
    input_fn: Callable[[str], str] = input,
    dump_runner: Callable[[list[str]], None] = _run_pg_dump,
    snapshot_sink: Callable[[Path], None] = _noop_sink,
) -> int:
    """Tear one vault down through injected dependencies. Returns a process exit code.

    ``apply=False`` prints the plan and returns 0. ``apply=True`` runs the ordered
    envelope: per-target root-escape guard -> typed confirmation -> snapshot
    (default ON) -> audit record -> (in-process eviction) -> schema drop ->
    filesystem removal -> receipt. A filesystem target that escapes the bound root
    is skipped (left in place, recorded in the receipt) rather than aborting the
    teardown; the receipt marks each target removed or skipped. Every store/
    filesystem removal tolerates an already-absent target and a concurrent writer
    repopulating a directory mid-removal, and the snapshot skips its dump when the
    schema is already gone, so a re-run after a partial failure completes cleanly --
    even with the default snapshot ON.
    """
    config_path = source_store.config_locator(vault_id)
    config = None
    if config_path is not None and config_path.exists():
        config = source_store.load_config(DiscoveredVault(config_path=config_path))
    storage_root = Path(config.vault.storage_root).expanduser() if config is not None else None
    brain_root = Path(config.vault.brain_root).expanduser() if config is not None else None

    snap_desc = f"ON -> {snapshot_dir}" if snapshot else "OFF (--no-snapshot)"
    print(f"Vault:            {vault_id}")
    print(f"  schema (DROP):   {vault_id}")
    print(f"  storage_root:    {storage_root}")
    print(f"  brain_root:      {brain_root}")
    print(f"  vault_config:    {config_path}")
    print(f"  snapshot:        {snap_desc}")
    print(f"  reason:          {reason}")
    print()

    if not apply:
        print("(dry-run; pass --apply to execute)")
        return 0

    # Per-target root-escape guard, before any change. A target that resolves
    # outside the bound vault root is refused for that target only -- left in
    # place and recorded as skipped -- rather than aborting the whole teardown,
    # so the in-root work (schema drop, config and enclosing-dir removal) still
    # runs. Refusal removes nothing.
    guarded: dict[str, Path] = {}
    skipped: dict[str, str] = {}
    fs_targets: list[tuple[str, Path]] = []
    if storage_root is not None:
        fs_targets.append(("storage_root", storage_root))
    if brain_root is not None:
        fs_targets.append(("brain_root", brain_root))
    if config_path is not None:
        fs_targets.append(("vault_dir", config_path.parent))
    for name, target in fs_targets:
        try:
            guarded[name] = resolve_and_assert_within_root(target, vault_root)
        except VaultRootEscapeError as exc:
            skipped[name] = str(exc)
            print(f"skip {name}: {exc}", file=sys.stderr)

    # Typed vault-id confirmation. The only auto-confirm is the disposable
    # ``test`` vault.
    if vault_id != "test":
        typed = input_fn(f"To confirm PERMANENT deletion, retype the vault_id ({vault_id}): ")
        if typed != vault_id:
            print(
                "refuse: typed confirmation did not match vault_id. Aborting.",
                file=sys.stderr,
            )
            return 3

    run_dir = snapshot_dir / f"{vault_id}-{_now().strftime(_TIMESTAMP_FORMAT)}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Snapshot-before-destroy (default ON). A failed dump halts before any delete.
    # The schema dump is skipped when the schema is already gone (a resume after a
    # partial teardown): pg_dump errors against an absent schema, so gating on
    # schema_exists keeps a resume from having to pass --no-snapshot.
    if snapshot:
        if await provisioner.schema_exists(vault_id):
            try:
                dump_runner(build_schema_dump_argv(pg_conninfo, vault_id, run_dir / "schema.dump"))
            except Exception as exc:  # noqa: BLE001 -- operator tool surfaces any dump error
                print(
                    f"refuse: snapshot failed ({exc}); no destruction performed.",
                    file=sys.stderr,
                )
                return 4
        else:
            print(f"snapshot: schema {vault_id} already absent; skipping schema dump (resume).")
        if storage_root is not None:
            (run_dir / "sources_manifest.json").write_text(
                json.dumps(_source_manifest(storage_root), indent=2) + "\n"
            )
        # Push the snapshot to a durable off-box store when the profile's snapshot
        # dir is ephemeral (the cloud teardown job). A failed push halts before any
        # destruction, like a failed dump. The default sink is a no-op.
        try:
            snapshot_sink(run_dir)
        except Exception as exc:  # noqa: BLE001 -- operator tool surfaces any sink error
            print(
                f"refuse: snapshot sink failed ({exc}); no destruction performed.",
                file=sys.stderr,
            )
            return 4

    # Per-target disposition, carried in both the audit record and the receipt:
    # an in-root target is removed, an out-of-root target is skipped (left in
    # place), so a partially-retired vault is traceable.
    targets: dict[str, dict[str, str]] = {}
    for name, target in fs_targets:
        if name in guarded:
            targets[name] = {"path": str(target), "status": "removed"}
        else:
            targets[name] = {
                "path": str(target),
                "status": "skipped (outside bound root)",
                "detail": skipped[name],
            }
    # A binding with no filesystem locator (config_locator -> None, the document
    # store) contributes no filesystem target; record its store-level source-tree
    # removal so a cloud teardown's receipt is not misleadingly empty.
    if config_path is None:
        targets["source_tree"] = {
            "path": vault_id,
            "status": "removed",
            "detail": "document-store source tree (no filesystem target)",
        }

    # Audit-first: record intent BEFORE any destruction, outside the vault.
    record = {
        "timestamp": _now().isoformat(),
        "operation": "delete_vault",
        "vault_id": vault_id,
        "schema": vault_id,
        "storage_root": str(storage_root) if storage_root is not None else None,
        "brain_root": str(brain_root) if brain_root is not None else None,
        "vault_config": str(config_path) if config_path is not None else None,
        "targets": targets,
        "snapshot_dir": str(run_dir),
        "reason": reason,
    }
    _append_jsonl(run_dir / "deletion_audit.jsonl", record)

    try:
        # In-process eviction (only when a live registry holds the vault).
        if registry is not None and vault_id in registry:
            await _evict_from_registry(registry, vault_id)

        # Drop the Postgres schema (idempotent).
        await provisioner.drop_vault_schema(vault_id)

        # Filesystem: sources tree (via the port), brain tree, config, then the
        # enclosing vault dir. Each in-root tree is removed; a target that escaped
        # the guard is skipped (left in place). Every removal is idempotent and
        # tolerates a concurrent writer repopulating a directory mid-removal.
        #
        # The retained-source tree is removed via the port when its filesystem
        # target is in-root (filesystem binding) or when the store has no
        # filesystem locator at all (document-store binding: config_locator returns
        # None, so storage_root never entered the per-target guard). In the latter
        # case the store addresses the tree by vault id, like delete_config below.
        if "storage_root" in guarded or config_path is None:
            source_store.delete_source_tree(vault_id, storage_root)
        if "brain_root" in guarded and guarded["brain_root"].exists():
            remove_tree_tolerating_concurrent_writer(guarded["brain_root"])
        source_store.delete_config(vault_id)
        if "vault_dir" in guarded and guarded["vault_dir"].exists():
            remove_tree_tolerating_concurrent_writer(guarded["vault_dir"])
    except Exception as exc:  # noqa: BLE001 -- operator tool surfaces any teardown error
        print(
            f"error during teardown: {exc}; the deletion audit record is retained "
            f"at {run_dir}. Re-run to resume (the operation is idempotent).",
            file=sys.stderr,
        )
        return 4

    receipt = {**record, "completed_at": _now().isoformat(), "status": "deleted"}
    (run_dir / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")

    print(f"delete complete: vault {vault_id} removed. Receipt: {run_dir / 'receipt.json'}")
    return 0


async def _run(
    *,
    vault_id: str,
    reason: str,
    apply: bool,
    snapshot: bool,
    snapshot_dir: Path | None,
) -> int:
    from psycopg.conninfo import make_conninfo

    from sage.mcp_init import (
        get_stack_config,
        get_vault_root,
        resolve_stack_storage_provisioner,
        resolve_stack_vault_source_store,
    )
    from sage.storage.postgres.pool import PostgresConnectionParams, build_conn_kwargs
    from sage.vault_management import default_vault_root

    stack_config = get_stack_config()
    vault_root = get_vault_root() or default_vault_root()
    source_store = resolve_stack_vault_source_store(stack_config, vault_root=vault_root)
    provisioner = resolve_stack_storage_provisioner(stack_config)

    pg = stack_config.postgres
    params = PostgresConnectionParams(
        host=pg.host,
        port=pg.port,
        database=pg.database,
        user=pg.user,
        sslmode=pg.sslmode,
    )
    pg_conninfo = make_conninfo(**build_conn_kwargs(params))

    return await delete_vault(
        vault_id=vault_id,
        source_store=source_store,
        provisioner=provisioner,
        vault_root=vault_root,
        snapshot_dir=snapshot_dir if snapshot_dir is not None else default_deletions_dir(),
        reason=reason,
        apply=apply,
        snapshot=snapshot,
        pg_conninfo=pg_conninfo,
        registry=None,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sage.maintenance.delete_vault",
        description=(
            "Permanently delete one SAGE vault under the local profile: drop its "
            "Postgres schema, remove its filesystem trees and config, evict it from "
            "a live registry. Operator-only; dry-run by default."
        ),
    )
    parser.add_argument("--vault", required=True, help="Vault id to delete.")
    parser.add_argument(
        "--reason", required=True, help="Reason for the deletion (recorded in the receipt)."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the teardown. Without this flag, prints a plan and exits.",
    )
    parser.add_argument(
        "--no-snapshot",
        dest="snapshot",
        action="store_false",
        help="Skip the pre-destroy pg_dump + source manifest (snapshot is ON by default).",
    )
    parser.add_argument(
        "--snapshot-dir",
        type=Path,
        default=None,
        help="Where to write the snapshot + receipt (default: ~/sage_vaults_deletions).",
    )
    args = parser.parse_args(argv)
    return asyncio.run(
        _run(
            vault_id=args.vault,
            reason=args.reason,
            apply=args.apply,
            snapshot=args.snapshot,
            snapshot_dir=args.snapshot_dir,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
