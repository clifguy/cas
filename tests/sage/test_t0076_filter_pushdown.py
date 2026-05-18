"""T-0076: Push retrieval-service filters into SQL.

Two hot paths in ``sage/services/retrieval.py`` previously fetched the
entire vault via ``list_all_documents()`` and applied filters in Python:

* ``_content_filters()`` — resolves doc-level filters into a
  ``document_id`` allowlist that pre-filters the chunk store.
* ``_list_filtered()`` — enumerates docs for keyword query ``"*"``.

Both must push ``doc_type`` / ``project`` / ``lifecycle_status`` /
``pipeline_status`` / ``tags`` / ``document_ids`` filters into the SQL
``query_documents()`` call instead.

Test coverage:

* T1-T3: behavior equivalence across the AC-named filters.
* T4: ``scope=AUTHORITATIVE`` must continue to work via the Python
  post-pass (``authority_scope`` is not a SQL column predicate today).
* T5-T6: anti-coincidental-pass gates -- ``list_all_documents()`` must
  no longer be called from either site. Monkeypatched to raise; the
  optimization tests fail if a future change silently reverts to the
  full-vault fetch.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

import pytest

from sage.adapters.interfaces import Chunk
from sage.models.enums import (
    PipelineStatus,
    RetrievalMode,
    RetrievalScope,
    SourceType,
)
from sage.models.schemas import (
    DiscoverRequest,
    Document,
    RetrievalFilters,
)
from sage.services.retrieval import RetrievalService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DOC_ID_RE = re.compile(r"^[0-9a-f]{8}_[a-z0-9_]+$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _id(name: str) -> str:
    if _DOC_ID_RE.fullmatch(name):
        return name
    return f"{hashlib.sha256(name.encode()).hexdigest()[:8]}_{name}"


def _sha(name: str) -> str:
    if _SHA256_RE.fullmatch(name):
        return name
    return "sha256:" + hashlib.sha256(f"sage-test-hash:{name}".encode()).hexdigest()


def _make_doc(
    short_name: str,
    lifecycle_status: str = "active",
    pipeline_status: PipelineStatus = PipelineStatus.ABSTRACTION_COMPLETE,
    project: str | None = None,
    doc_type: str | None = None,
    authority_scope: str | None = None,
    tags: list[str] | None = None,
) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=_id(short_name),
        title=f"Test {short_name}",
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{short_name}.md",
        lifecycle_status=lifecycle_status,
        source_content_hash=_sha(short_name),
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=pipeline_status,
        project=project,
        doc_type=doc_type,
        authority_scope=authority_scope,
        tags=tags or [],
    )


async def _index_marker(
    content_store,
    embedding_provider,
    document_id: str,
    marker: str = "alpha-marker",
) -> None:
    """Index a single chunk containing the marker term so BM25 finds it."""
    chunk = Chunk(
        document_id=document_id,
        heading_path="Body",
        content=f"This document contains the {marker} term.",
        chunk_index=0,
    )
    [embedding] = await embedding_provider.embed([chunk.content])
    chunk.embedding = embedding
    await content_store.index_chunks(document_id, [chunk])


@pytest.fixture
def t0076_retrieval_service(
    graph_store, stub_content_store, stub_embedding_provider, minimal_config
):
    return RetrievalService(
        graph_store=graph_store,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        config=minimal_config,
    )


async def _seed_mixed_vault(graph_store, content_store, embedding_provider):
    """Five docs spanning the dimensions T-0076 needs to exercise:

    * d_active_alpha     -- lifecycle=active,    project=alpha, authority=None
    * d_completed_alpha  -- lifecycle=completed, project=alpha, authority=None
    * d_active_beta      -- lifecycle=active,    project=beta,  authority=None
    * d_authoritative    -- lifecycle=active,    project=alpha, authority="alpha-domain"
    * d_failed           -- lifecycle=active,    project=alpha, authority=None,
                            pipeline_status=FAILED (must never appear in results)

    Each non-failed doc gets one chunk containing "alpha-marker" so a
    keyword search for that term retrieves them when the filter allows.
    """
    docs = {
        "d_active_alpha": _make_doc(
            "d_active_alpha",
            lifecycle_status="active",
            project="alpha",
            doc_type="note",
        ),
        "d_completed_alpha": _make_doc(
            "d_completed_alpha",
            lifecycle_status="completed",
            project="alpha",
            doc_type="note",
        ),
        "d_active_beta": _make_doc(
            "d_active_beta",
            lifecycle_status="active",
            project="beta",
            doc_type="note",
        ),
        "d_authoritative": _make_doc(
            "d_authoritative",
            lifecycle_status="active",
            project="alpha",
            doc_type="note",
            authority_scope="alpha-domain",
        ),
        "d_failed": _make_doc(
            "d_failed",
            lifecycle_status="active",
            project="alpha",
            doc_type="note",
            pipeline_status=PipelineStatus.FAILED,
        ),
    }
    for doc in docs.values():
        await graph_store.insert_document(doc)
    for short_name, doc in docs.items():
        if doc.pipeline_status == PipelineStatus.FAILED:
            # Failed docs have no chunks; matches the production
            # invariant that chunking is gated on a healthy pipeline.
            continue
        await _index_marker(content_store, embedding_provider, doc.id)
    return docs


# ---------------------------------------------------------------------------
# T1, T2 — _content_filters() behavior equivalence
# ---------------------------------------------------------------------------


async def test_t0076_content_filters_resolves_project_and_lifecycle(
    graph_store, stub_content_store, stub_embedding_provider, t0076_retrieval_service
):
    """Keyword search with project=alpha + lifecycle_status=active must
    only surface the two docs that satisfy both filters. The completed
    alpha doc and the active beta doc are excluded; the failed doc is
    structurally excluded (no chunks).

    Exercises _content_filters() at retrieval.py:179 end-to-end: filter
    resolution -> document_id allowlist -> chunk-store pre-filter ->
    response."""
    docs = await _seed_mixed_vault(graph_store, stub_content_store, stub_embedding_provider)

    response = await t0076_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.KEYWORD,
            query="alpha-marker",
            filters=RetrievalFilters(
                project="alpha",
                lifecycle_status="active",
            ),
            limit=10,
        )
    )

    returned_ids = {hit.document.id for hit in response.results}
    assert returned_ids == {docs["d_active_alpha"].id, docs["d_authoritative"].id}


async def test_t0076_content_filters_zero_match_short_circuits_with_hints(
    graph_store, stub_content_store, stub_embedding_provider, t0076_retrieval_service
):
    """When the filter set matches zero docs in SQL, the service must
    short-circuit to an empty response with hints surfacing the active
    filters. has_doc_constraints is True (filters were present) but
    matching ids are empty -> caller sees what filtered out."""
    await _seed_mixed_vault(graph_store, stub_content_store, stub_embedding_provider)

    response = await t0076_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.KEYWORD,
            query="alpha-marker",
            filters=RetrievalFilters(project="nonexistent-project"),
            limit=10,
        )
    )

    assert response.results == []
    assert response.total_available == 0
    assert response.hints is not None
    assert response.hints.get("total_before_filtering") == 0
    active = response.hints.get("active_filters") or {}
    assert active.get("project") == "nonexistent-project"


# ---------------------------------------------------------------------------
# T3, T4 — _list_filtered() behavior equivalence + AUTHORITATIVE scope
# ---------------------------------------------------------------------------


async def test_t0076_list_filtered_returns_filter_matched_hits_excluding_failed(
    graph_store, stub_content_store, stub_embedding_provider, t0076_retrieval_service
):
    """Keyword query '*' triggers _list_filtered(). With
    lifecycle_status=active, the response must include the three active
    non-failed docs and exclude the completed and failed ones."""
    docs = await _seed_mixed_vault(graph_store, stub_content_store, stub_embedding_provider)

    response = await t0076_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.KEYWORD,
            query="*",
            filters=RetrievalFilters(lifecycle_status="active"),
            limit=100,
        )
    )

    returned_ids = {hit.document.id for hit in response.results}
    assert returned_ids == {
        docs["d_active_alpha"].id,
        docs["d_active_beta"].id,
        docs["d_authoritative"].id,
    }
    assert docs["d_completed_alpha"].id not in returned_ids
    assert docs["d_failed"].id not in returned_ids


async def test_t0076_list_filtered_authoritative_scope_survives_refactor(
    graph_store, stub_content_store, stub_embedding_provider, t0076_retrieval_service
):
    """``scope=AUTHORITATIVE`` is the one filter that cannot be expressed
    as a SQL column predicate today; the Python post-pass on
    ``authority_scope`` must survive the refactor. Without it, this
    test returns all active docs instead of just the authoritative one."""
    docs = await _seed_mixed_vault(graph_store, stub_content_store, stub_embedding_provider)

    response = await t0076_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.KEYWORD,
            query="*",
            scope=RetrievalScope.AUTHORITATIVE,
            limit=100,
        )
    )

    returned_ids = {hit.document.id for hit in response.results}
    assert returned_ids == {docs["d_authoritative"].id}


# ---------------------------------------------------------------------------
# T5, T6 — anti-coincidental-pass gates
# ---------------------------------------------------------------------------


async def test_t0076_content_filters_does_not_call_list_all_documents(
    graph_store,
    stub_content_store,
    stub_embedding_provider,
    t0076_retrieval_service,
    monkeypatch,
):
    """Optimization gate for _content_filters(): list_all_documents()
    must never be invoked from this path. Monkeypatched to raise; the
    test passes iff the implementation does not call it.

    If this fails but the equivalence tests still pass, the
    implementation has been silently reverted to the full-vault fetch.
    """
    await _seed_mixed_vault(graph_store, stub_content_store, stub_embedding_provider)

    async def _forbidden(*_args, **_kwargs):
        raise AssertionError(
            "list_all_documents() must not be called from _content_filters() (T-0076)"
        )

    monkeypatch.setattr(graph_store, "list_all_documents", _forbidden)

    response = await t0076_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.KEYWORD,
            query="alpha-marker",
            filters=RetrievalFilters(
                project="alpha",
                lifecycle_status="active",
            ),
            limit=10,
        )
    )
    # The call returns -- list_all_documents was never invoked.
    # Cross-check via the result set so a future stub that swallows
    # the AssertionError still produces a wrong result here.
    assert response.results, "filter resolution returned an empty allowlist"


async def test_t0076_list_filtered_does_not_call_list_all_documents(
    graph_store,
    stub_content_store,
    stub_embedding_provider,
    t0076_retrieval_service,
    monkeypatch,
):
    """Optimization gate for _list_filtered(): list_all_documents() must
    never be invoked from the keyword '*' path. Same reasoning as
    test_t0076_content_filters_does_not_call_list_all_documents."""
    await _seed_mixed_vault(graph_store, stub_content_store, stub_embedding_provider)

    async def _forbidden(*_args, **_kwargs):
        raise AssertionError(
            "list_all_documents() must not be called from _list_filtered() (T-0076)"
        )

    monkeypatch.setattr(graph_store, "list_all_documents", _forbidden)

    response = await t0076_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.KEYWORD,
            query="*",
            filters=RetrievalFilters(lifecycle_status="active"),
            limit=100,
        )
    )
    assert response.results, "_list_filtered returned an empty hit set"
