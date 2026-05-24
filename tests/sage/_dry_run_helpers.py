"""T-0152 dry-run test helpers.

Provides ``state_snapshot`` and ``assert_state_unchanged`` — the
universal "no state was written" proof used by the per-service dry-run
test files. Captures all documents, all edges, the chunk-pushdownable
scalars in the content store (doc_type / lifecycle_status / project per
chunk), and optionally the mtime + size of a vault-config yaml file.

Pattern (in a test):

    before = await state_snapshot(graph_store, stub_content_store)
    await service.update_metadata(doc_id, request, "tester")
    after = await state_snapshot(graph_store, stub_content_store)
    assert_state_unchanged(before, after)

Built for T-0152 but reusable for any test that needs to assert a
SAGE call did not mutate persisted state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sage.adapters.stubs import StubContentStore
from sage.storage.graph_store import GraphStore


@dataclass(frozen=True)
class StateFingerprint:
    """Snapshot of SAGE store state at one point in time.

    ``updated_at`` and ``metadata_confirmed`` are peeled off into their
    own maps so an accidental ``updated_at`` advance (a common bug
    shape — stamp the field, then conditionally skip the persist)
    surfaces as a named diff rather than a giant document-dict equality
    failure.
    """

    # document_id -> serializable dict of the document, EXCLUDING the
    # peeled-off scalars below.
    documents: dict[str, dict[str, Any]] = field(default_factory=dict)
    # document_id -> updated_at ISO string
    document_updated_at: dict[str, str | None] = field(default_factory=dict)
    # document_id -> metadata_confirmed bool
    document_metadata_confirmed: dict[str, bool | None] = field(default_factory=dict)
    # edge_id -> serializable dict of the edge
    edges: dict[str, dict[str, Any]] = field(default_factory=dict)
    # document_id -> [{chunk_index, doc_type, lifecycle_status, project}, ...]
    chunk_metadata: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # path string -> (mtime_ns, size)
    file_state: dict[str, tuple[int, int]] = field(default_factory=dict)


async def state_snapshot(
    graph_store: GraphStore,
    content_store: StubContentStore | None = None,
    file_paths: list[Path] | None = None,
) -> StateFingerprint:
    """Snapshot SAGE storage state for a dry-run "no state was written" proof.

    ``graph_store``: required. Snapshots all documents (via
    ``list_all_documents``) and all edges (via ``query_edges`` with no
    filters, limit=10000). ``updated_at`` and ``metadata_confirmed``
    are peeled off documents and stored in separate maps so they show
    up as named diffs on failure.

    ``content_store``: optional. When supplied (StubContentStore only),
    captures the chunk-pushdownable scalars per chunk. Divergence proves
    a stray ``update_chunk_metadata`` call surfaced.

    ``file_paths``: optional. When supplied, captures ``(mtime_ns,
    size)`` per path. Used by ``update_vault_config`` dry-run tests to
    prove the yaml file was not rewritten.
    """
    documents: dict[str, dict[str, Any]] = {}
    document_updated_at: dict[str, str | None] = {}
    document_metadata_confirmed: dict[str, bool | None] = {}
    for doc in await graph_store.list_all_documents():
        dump = doc.model_dump()
        document_updated_at[doc.id] = dump.pop("updated_at", None)
        document_metadata_confirmed[doc.id] = dump.pop("metadata_confirmed", None)
        documents[doc.id] = dump

    edges: dict[str, dict[str, Any]] = {}
    rows, _total = await graph_store.query_edges(filters=None, limit=10000)
    for row in rows:
        edges[row.edge.id] = row.edge.model_dump()

    chunk_metadata: dict[str, list[dict[str, Any]]] = {}
    if content_store is not None:
        # StubContentStore stores chunks keyed by document_id in
        # ``_store``. The chunk-pushdownable scalars are the only ones
        # ``update_chunk_metadata`` touches; snapshotting them in chunk-
        # index order gives a stable comparison.
        for doc_id, chunks in content_store._store.items():
            chunk_metadata[doc_id] = [
                {
                    "chunk_index": c.chunk_index,
                    "doc_type": c.doc_type,
                    "lifecycle_status": c.lifecycle_status,
                    "project": c.project,
                }
                for c in sorted(chunks, key=lambda c: c.chunk_index)
            ]

    file_state: dict[str, tuple[int, int]] = {}
    if file_paths:
        for p in file_paths:
            stat = p.stat()
            file_state[str(p)] = (stat.st_mtime_ns, stat.st_size)

    return StateFingerprint(
        documents=documents,
        document_updated_at=document_updated_at,
        document_metadata_confirmed=document_metadata_confirmed,
        edges=edges,
        chunk_metadata=chunk_metadata,
        file_state=file_state,
    )


def assert_state_unchanged(
    before: StateFingerprint,
    after: StateFingerprint,
) -> None:
    """Assert two fingerprints match; raise AssertionError with a per-field diff.

    The error message names each divergence individually rather than
    dumping two giant dicts. A test that fails because dry-run silently
    persisted should see one line per affected document, edge, chunk
    metadata entry, or file.
    """
    failures: list[str] = []

    failures.extend(_diff_dict_of_dicts("document", before.documents, after.documents))
    failures.extend(
        _diff_scalar_map(
            "updated_at",
            before.document_updated_at,
            after.document_updated_at,
        )
    )
    failures.extend(
        _diff_scalar_map(
            "metadata_confirmed",
            before.document_metadata_confirmed,
            after.document_metadata_confirmed,
        )
    )
    failures.extend(_diff_dict_of_dicts("edge", before.edges, after.edges))
    failures.extend(_diff_scalar_map("chunk_metadata", before.chunk_metadata, after.chunk_metadata))
    failures.extend(_diff_scalar_map("file_state", before.file_state, after.file_state))

    if failures:
        raise AssertionError(
            "State changed across what should have been a dry-run:\n  " + "\n  ".join(failures)
        )


def _diff_dict_of_dicts(
    label: str,
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> list[str]:
    """Diff two id -> row-dict maps. Reports added / removed / per-field changes."""
    failures: list[str] = []
    added = set(after) - set(before)
    removed = set(before) - set(after)
    if added:
        failures.append(f"{label}s added: {sorted(added)}")
    if removed:
        failures.append(f"{label}s removed: {sorted(removed)}")
    for k in sorted(set(before) & set(after)):
        if before[k] != after[k]:
            failures.append(f"{label} {k} changed: {_dict_diff(before[k], after[k])}")
    return failures


def _diff_scalar_map(
    label: str,
    before: dict[str, Any],
    after: dict[str, Any],
) -> list[str]:
    """Diff two id -> scalar maps (or id -> list maps). One line per divergence."""
    failures: list[str] = []
    for k in sorted(set(before) | set(after)):
        b = before.get(k, "<absent>")
        a = after.get(k, "<absent>")
        if b != a:
            failures.append(f"{label}[{k}]: {b!r} -> {a!r}")
    return failures


def _dict_diff(before: dict[str, Any], after: dict[str, Any]) -> str:
    """Render a per-key diff between two dicts as a single string."""
    diffs = []
    for k in sorted(set(before) | set(after)):
        b = before.get(k, "<absent>")
        a = after.get(k, "<absent>")
        if b != a:
            diffs.append(f"{k}: {b!r} -> {a!r}")
    return "{" + ", ".join(diffs) + "}"
