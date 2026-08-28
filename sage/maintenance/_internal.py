"""Shared internals for the out-of-band purge scripts.

Holds the per-document purge primitive that ``sage.maintenance.purge_document``,
``sage.maintenance.purge_batch``, and ``sage.maintenance.purge_chain`` all call,
the storage-agnostic chain-shape helpers used by ``purge_chain``, and the
store-acquisition helper the three CLIs use to obtain a live graph/content store
pair for a vault.

This module is internal to the ``sage.maintenance`` package; nothing outside it
should import it, and nothing on the SAGE request surface (``sage.mcp_server``,
``sage.api``) may import the package at all -- document removal is absent from
that surface by the No-Delete Invariant (CAS-ADR-029), and the import-topology
test enforces the boundary.

Convention: the per-document cascade writes the audit record -- through the
storage binding's purge-audit sink -- **before** it mutates either store. The
worst-case partial-failure outcome is "audit record with no delete", never
"delete with no audit record". The graph store and content store are removed in
separate coordinated operations with no cross-store atomicity (CAS-ADR-042
weakest-binding); the result object records how far the cascade got so a
partial failure is fully traceable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sage.adapters.interfaces import ContentStore, GraphStore
    from sage.models.schemas import Document
    from sage.storage_binding import PurgeAuditSink, VaultStorageHandle, VaultStorageProvisioner
    from sage.vault_source_binding import VaultSourceStore


@dataclass(frozen=True)
class _PurgeOneResult:
    """Outcome of a single per-document purge.

    ``audit_written`` / ``graph_committed`` / ``content_removed`` let the caller
    distinguish "audit-only" partial failure from "audit + graph removed, content
    chunks orphaned" partial failure when surfacing error messages.
    """

    document_id: str
    succeeded: bool
    error: str | None
    audit_written: bool
    graph_committed: bool
    content_removed: bool


def _audit_record(
    doc: Document,
    reason: str,
    operation: str,
    batch_id: str | None = None,
    chain_id: str | None = None,
) -> dict[str, Any]:
    """Build the audit record for one document purge.

    ``batch_id`` / ``chain_id`` are included only when supplied: single-document
    callers pass neither, batch callers pass a shared ``batch_id``, chain callers
    pass a shared ``chain_id``. A ``None`` id is *omitted* from the record, never
    serialized as JSON ``null``, so an auditor can distinguish the three modes.
    """
    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "operation": operation,
        "document_id": doc.id,
        "title": doc.title,
        "source_path": doc.source_path,
        "source_content_hash": doc.source_content_hash,
        "doc_type": doc.doc_type,
        "reason": reason,
    }
    if batch_id is not None:
        record["batch_id"] = batch_id
    if chain_id is not None:
        record["chain_id"] = chain_id
    return record


async def _purge_one(
    *,
    document_id: str,
    graph_store: GraphStore,
    content_store: ContentStore,
    audit_sink: PurgeAuditSink,
    reason: str,
    operation: str,
    batch_id: str | None = None,
    chain_id: str | None = None,
) -> _PurgeOneResult:
    """Audit-first per-document cascade: graph footprint, then content chunks.

    The caller owns all precondition checks (existence, staging edges, pipeline
    status, typed confirmation). This helper does not validate; it executes:
    fetch the document, append its audit record through the sink, remove its
    graph footprint in one transaction, then remove its content chunks. Each
    store removal is a separate coordinated operation (no cross-store
    atomicity, CAS-ADR-042). A failed audit append propagates before any store
    mutation -- no audit, no destruction.
    """
    doc = await graph_store.get_document(document_id)
    if doc is None:
        return _PurgeOneResult(
            document_id=document_id,
            succeeded=False,
            error="document not found",
            audit_written=False,
            graph_committed=False,
            content_removed=False,
        )

    await audit_sink.append(
        _audit_record(doc, reason, operation, batch_id=batch_id, chain_id=chain_id)
    )

    try:
        await graph_store.remove_document(document_id)
    except Exception as exc:  # noqa: BLE001 -- operator tool surfaces any store error
        return _PurgeOneResult(
            document_id=document_id,
            succeeded=False,
            error=f"graph cascade failed: {exc}",
            audit_written=True,
            graph_committed=False,
            content_removed=False,
        )

    try:
        await content_store.remove_document(document_id)
    except Exception as exc:  # noqa: BLE001 -- operator tool surfaces any store error
        return _PurgeOneResult(
            document_id=document_id,
            succeeded=False,
            error=f"content delete failed: {exc}",
            audit_written=True,
            graph_committed=True,
            content_removed=False,
        )

    return _PurgeOneResult(
        document_id=document_id,
        succeeded=True,
        error=None,
        audit_written=True,
        graph_committed=True,
        content_removed=True,
    )


async def list_staging_edge_ids_for(graph_store: GraphStore, document_id: str) -> list[str]:
    """Ids of staging edges that reference ``document_id`` at either end."""
    return [
        s.id
        for s in await graph_store.list_staging_edges()
        if document_id in (s.source_id, s.target_id)
    ]


# ─── Chain-shape helpers (storage-agnostic) ─────────────────────────
#
# These operate on the ``{"documents": [...], "edges": [...]}`` shape that
# ``GraphStore.chain_walk`` returns: each document dict carries ``doc_id`` and
# each edge dict carries ``source_id`` / ``target_id``. They compute the head,
# linearity, and a deterministic head-first order without touching the store.


def _build_adjacency(
    documents: list[dict[str, Any]], edges: list[dict[str, str]]
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Return (successors, predecessors) keyed by doc_id.

    For an edge ``source -> target``, ``target`` is a successor of ``source`` and
    ``source`` is a predecessor of ``target``. For supersedes the source is the
    newer version, so the head (newest) has no predecessors.
    """
    successors: dict[str, set[str]] = {d["doc_id"]: set() for d in documents}
    predecessors: dict[str, set[str]] = {d["doc_id"]: set() for d in documents}
    for e in edges:
        successors.setdefault(e["source_id"], set()).add(e["target_id"])
        predecessors.setdefault(e["target_id"], set()).add(e["source_id"])
    return successors, predecessors


def _chain_is_linear(documents: list[dict[str, Any]], edges: list[dict[str, str]]) -> bool:
    """True iff every node has at most one predecessor and at most one successor."""
    successors, predecessors = _build_adjacency(documents, edges)
    return all(
        len(successors[d["doc_id"]]) <= 1 and len(predecessors[d["doc_id"]]) <= 1 for d in documents
    )


def _chain_head_ids(documents: list[dict[str, Any]], edges: list[dict[str, str]]) -> list[str]:
    """Doc ids with zero inbound edges of the named type -- the chain's heads.

    For a linear supersedes chain this is exactly one id (the newest version).
    For a branched chain (multiple roots) it can be multiple ids.
    """
    _, predecessors = _build_adjacency(documents, edges)
    return sorted(d["doc_id"] for d in documents if not predecessors.get(d["doc_id"]))


def _order_chain_from_head(
    documents: list[dict[str, Any]],
    edges: list[dict[str, str]],
    head_id: str,
) -> list[str]:
    """Head-first ordering of chain member ids via a depth-first successor walk.

    Branches are visited in sorted-id order for deterministic output. Members not
    reachable from ``head_id`` are appended in sorted order so the dry-run still
    lists them.
    """
    successors, _ = _build_adjacency(documents, edges)
    ordered: list[str] = []
    visited: set[str] = set()
    stack: list[str] = [head_id]
    while stack:
        node = stack.pop()
        if node in visited:
            continue
        visited.add(node)
        ordered.append(node)
        # Reverse-sorted so smaller ids are popped (visited) first.
        for succ in sorted(successors.get(node, set()), reverse=True):
            if succ not in visited:
                stack.append(succ)
    for d in documents:
        if d["doc_id"] not in visited:
            ordered.append(d["doc_id"])
            visited.add(d["doc_id"])
    return ordered


# ─── Store acquisition ──────────────────────────────────────────────


def resolve_vault_config(source_store: VaultSourceStore, vault_id: str):
    """Locate and load ``vault_id``'s declaration through the vault-source store.

    Returns the loaded config, or ``None`` when no discovered vault matches.

    Resolution iterates ``discover()`` rather than asking for a filesystem
    locator, so it works under a pathless vault-source binding (CAS-ADR-043).
    Discovery's binding-opaque ``vault_id`` prefilters the candidates -- only a
    discovered vault that may match is config-loaded, so a sibling vault's load
    cost (or load failure) never touches the target's resolution -- and the
    loaded config's own id confirms the match.
    """
    for discovered in source_store.discover():
        if discovered.vault_id not in (None, vault_id):
            continue
        candidate = source_store.load_config(discovered)
        if candidate.vault.id == vault_id:
            return candidate
    return None


async def open_vault_stores(
    vault_id: str,
    *,
    source_store: VaultSourceStore | None = None,
    provisioner: VaultStorageProvisioner | None = None,
) -> tuple[GraphStore, ContentStore, PurgeAuditSink, VaultStorageHandle] | None:
    """Open a live graph/content store pair plus the audit sink for ``vault_id``.

    Returns ``(graph_store, content_store, audit_sink, handle)``, or ``None``
    when no discovered vault matches. The caller owns ``handle`` and must
    ``await handle.close()`` when done. The vault declaration is located and
    loaded through the vault-source store (CAS-ADR-043); the stores and the
    purge-audit sink are opened through the storage provisioner (CAS-ADR-042).
    Both bindings default to the active profile's stack resolution; a caller
    with no populated stack singleton (the in-cloud job entrypoints) injects
    its own. The declaration itself is located by ``resolve_vault_config``.
    """
    if source_store is None or provisioner is None:
        from sage.mcp_init import (
            get_stack_config,
            resolve_stack_storage_provisioner,
            resolve_stack_vault_source_store,
        )

        stack_config = get_stack_config()
        if source_store is None:
            source_store = resolve_stack_vault_source_store(stack_config)
        if provisioner is None:
            provisioner = resolve_stack_storage_provisioner(stack_config)

    config = resolve_vault_config(source_store, vault_id)
    if config is None:
        return None
    brain_root = Path(config.vault.brain_root).expanduser()

    audit_sink = provisioner.purge_audit_sink(vault_id)
    handle = await provisioner.open_vault_storage(
        vault_id, brain_root, need_graph=True, need_content=True
    )
    # need_graph/need_content are True, so both stores are present.
    return handle.graph_store, handle.content_store, audit_sink, handle
