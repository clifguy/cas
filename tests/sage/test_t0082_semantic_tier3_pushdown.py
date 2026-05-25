"""T-0082: Push tier3_metadata filters into semantic/keyword content-store pre-filter.

T-0075 pushed ``tier3_metadata`` into SQL ``json_extract`` predicates for
catalog-mode retrieval. T-0082 closes the parallel-directory gap: the
semantic and keyword retrieval paths must also resolve their tier3
filter via the same SQL pushdown instead of the legacy
``list_all_documents() + Python post-filter`` pattern.

The T-0076 sweep already migrated ``_content_filters()`` and
``_list_filtered()`` to ``query_documents()``; that diff incidentally
covers tier3 as well. T-0082's role is to lock in the tier3 path with
its own coverage so a future refactor that silently regresses tier3 to
a Python post-filter would be caught here, even if T-0076's gates
(which target project/lifecycle) still pass.

Test coverage:

* T1 — semantic-mode tier3 filter returns only matching docs
  (load-bearing single-mode correctness assertion).
* T2 — semantic-mode and catalog-mode tier3 filters return the same
  document set (the parity assertion the AC calls out).
* T2b — keyword-mode and semantic-mode tier3 filters return the same
  document set. Closes the F4 parallel-call-site gap: both modes route
  through ``_content_filters``, so tier3 must bind to both.
* T3 — typo'd tier3 key in semantic mode raises Tier3SchemaViolationError
  before any retrieval runs (symmetric with
  ``test_t0075_catalog_filter_unknown_tier3_key_raises_against_doc_type_schema``).
* T4 — anti-coincidental-pass gate: semantic-mode tier3 must not call
  ``list_all_documents()``. Symmetric with T-0076's gate but binds to
  the tier3 path specifically.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sage.adapters.interfaces import Chunk
from sage.api.errors import Tier3SchemaViolationError
from sage.config import VaultConfig
from sage.models.enums import (
    PipelineStatus,
    RetrievalMode,
    SourceType,
)
from sage.models.schemas import (
    DiscoverRequest,
    Document,
    RetrievalFilters,
)
from sage.services.retrieval import RetrievalService

# ---------------------------------------------------------------------------
# Helpers and fixtures
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


def _config_dict_with_ticket_schema(tmp_vault_dir: Path) -> dict:
    """Vault config with a metadata_schema for doc_type=ticket so the
    semantic-mode ``_validate_tier3_filter_keys`` path has a schema to
    consult. Mirrors the shape used by
    ``tests/sage/test_tier3_metadata.py::_config_dict_with_tier3``."""
    return {
        "vault": {
            "id": "test_t0082_vault",
            "name": "Test T-0082 Vault",
            "owner": "testuser",
            "storage_root": str(tmp_vault_dir / "sources"),
            "brain_root": str(tmp_vault_dir / "brain"),
            "visibility": "personal",
        },
        "document_types": {
            "doc_types": [
                {
                    "value": "ticket",
                    "label": "Ticket",
                    "metadata_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "ticket_id": {"type": "string", "pattern": "^T-\\d{4}$"},
                            "ticket_priority": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                            },
                        },
                    },
                },
            ]
        },
        "lifecycle": {
            "base_states_required": True,
            "states": [
                {"value": "active", "label": "Active"},
                {"value": "completed", "label": "Completed"},
                {"value": "archived", "label": "Archived", "is_terminal": True},
            ],
            "transitions": [
                {"from_state": "(new)", "action": "ingest", "to_state": "active"},
                {"from_state": "active", "action": "complete", "to_state": "completed"},
            ],
        },
        "source_adapters": {"adapters": [{"source_type": "markdown", "enabled": True}]},
        "metadata_extraction": {},
        "edge_inference": {},
    }


@pytest.fixture
def t0082_config(tmp_vault_dir):
    return VaultConfig.model_validate(_config_dict_with_ticket_schema(tmp_vault_dir))


@pytest.fixture
def t0082_retrieval_service(graph_store, stub_content_store, stub_embedding_provider, t0082_config):
    return RetrievalService(
        graph_store=graph_store,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        config=t0082_config,
    )


def _make_ticket_doc(
    short_name: str,
    tier3: dict | None,
    pipeline_status: PipelineStatus = PipelineStatus.ABSTRACTION_COMPLETE,
) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=_id(short_name),
        title=f"Test {short_name}",
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{short_name}.md",
        lifecycle_status="active",
        source_content_hash=_sha(short_name),
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=pipeline_status,
        doc_type="ticket",
        tier3_metadata=tier3,
    )


async def _index_marker(
    content_store,
    embedding_provider,
    document_id: str,
    doc_type: str = "ticket",
    marker: str = "alpha-marker",
) -> None:
    chunk = Chunk(
        document_id=document_id,
        heading_path="Body",
        content=f"This document contains the {marker} term.",
        chunk_index=0,
        doc_type=doc_type,
    )
    [embedding] = await embedding_provider.embed([chunk.content])
    chunk.embedding = embedding
    await content_store.index_chunks(document_id, [chunk])


async def _seed_tier3_vault(graph_store, content_store, embedding_provider):
    """Four doc_type=ticket docs with distinct tier3 shapes:

    * d_high_ticket    -- tier3={ticket_id: T-0900, ticket_priority: high}
    * d_medium_ticket  -- tier3={ticket_id: T-0901, ticket_priority: medium}
    * d_no_tier3       -- tier3=None (matches None-valued filters, not "high")
    * d_failed         -- tier3={ticket_priority: high}, pipeline=FAILED, no chunks

    Each non-failed doc indexes one chunk containing "alpha-marker" so a
    keyword/semantic query for that term retrieves them when the tier3
    filter allows.
    """
    docs = {
        "d_high_ticket": _make_ticket_doc(
            "d_high_ticket",
            tier3={"ticket_id": "T-0900", "ticket_priority": "high"},
        ),
        "d_medium_ticket": _make_ticket_doc(
            "d_medium_ticket",
            tier3={"ticket_id": "T-0901", "ticket_priority": "medium"},
        ),
        "d_no_tier3": _make_ticket_doc(
            "d_no_tier3",
            tier3=None,
        ),
        "d_failed": _make_ticket_doc(
            "d_failed",
            tier3={"ticket_priority": "high"},
            pipeline_status=PipelineStatus.FAILED,
        ),
    }
    for doc in docs.values():
        await graph_store.insert_document(doc)
    for short_name, doc in docs.items():
        if doc.pipeline_status == PipelineStatus.FAILED:
            continue
        await _index_marker(content_store, embedding_provider, doc.id)
    return docs


# ---------------------------------------------------------------------------
# T1 — semantic-mode tier3 single-mode correctness
# ---------------------------------------------------------------------------


async def test_t0082_semantic_tier3_filter_returns_only_matching_docs(
    tmp_vault_dir,
    graph_store,
    stub_content_store,
    stub_embedding_provider,
    t0082_retrieval_service,
):
    """Semantic discover with tier3={ticket_priority: high} must surface
    only d_high_ticket. d_medium_ticket is excluded by the tier3
    predicate; d_no_tier3 is excluded because ``high`` does not match
    null/absent; d_failed is structurally excluded by the FAILED pipeline
    gate (and has no chunks anyway).

    This is the primary single-mode AC: tier3 reaches the document
    allowlist that gates the LanceDB pre-filter for semantic mode."""
    docs = await _seed_tier3_vault(graph_store, stub_content_store, stub_embedding_provider)

    response = await t0082_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.SEMANTIC,
            query="alpha-marker",
            filters=RetrievalFilters(
                doc_type="ticket",
                tier3_metadata={"ticket_priority": "high"},
            ),
            limit=10,
        )
    )

    returned_ids = {hit.document.id for hit in response.results}
    assert returned_ids == {docs["d_high_ticket"].id}


# ---------------------------------------------------------------------------
# T2 — semantic/catalog parity (the load-bearing assertion)
# ---------------------------------------------------------------------------


async def test_t0082_semantic_tier3_matches_catalog_tier3_result_set(
    tmp_vault_dir,
    graph_store,
    stub_content_store,
    stub_embedding_provider,
    t0082_retrieval_service,
):
    """The same tier3 filter applied in semantic mode and catalog mode
    must return the same document set, modulo the T-0148 asymmetry on
    ``pipeline_status=failed``. T-0082 calls behavior parity between
    modes the load-bearing assertion -- divergence on any axis other
    than the deliberate pipeline-status one means the SQL surface
    called from ``_content_filters`` has drifted from the one called
    by ``_catalog``, and one of them is wrong."""
    docs = await _seed_tier3_vault(graph_store, stub_content_store, stub_embedding_provider)

    tier3_filter = {"ticket_priority": "high"}

    semantic_resp = await t0082_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.SEMANTIC,
            query="alpha-marker",
            filters=RetrievalFilters(doc_type="ticket", tier3_metadata=tier3_filter),
            limit=100,
        )
    )
    catalog_resp = await t0082_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            filters=RetrievalFilters(doc_type="ticket", tier3_metadata=tier3_filter),
            limit=100,
        )
    )

    semantic_ids = {hit.document.id for hit in semantic_resp.results}
    catalog_ids = {hit.document.id for hit in catalog_resp.results}

    # T-0148: catalog enumerates failed-pipeline docs; semantic still
    # excludes them by BH-020. The two modes match on every other axis
    # of the tier3 filter pushdown.
    failed_id = docs["d_failed"].id
    assert failed_id in catalog_ids
    assert failed_id not in semantic_ids
    assert semantic_ids == catalog_ids - {failed_id}
    # Cross-bind to a non-empty set so an accidental "both empty" pass
    # cannot satisfy this test.
    assert semantic_ids, "expected at least one tier3=high doc; got none"


# ---------------------------------------------------------------------------
# T2b — keyword/semantic parity (F4 parallel-site coverage)
# ---------------------------------------------------------------------------


async def test_t0082_keyword_tier3_matches_semantic_tier3_result_set(
    tmp_vault_dir,
    graph_store,
    stub_content_store,
    stub_embedding_provider,
    t0082_retrieval_service,
):
    """Both ``_semantic`` and ``_keyword`` route through ``_content_filters``,
    so the tier3 pushdown applies to both. T2 binds the semantic path;
    this test binds the keyword path. Without it, a future refactor that
    splits tier3 resolution into mode-specific code paths would silently
    miss the keyword side (T-0082 AC: 'Semantic AND keyword modes
    resolve RetrievalFilters.tier3 without calling list_all_documents()')."""
    await _seed_tier3_vault(graph_store, stub_content_store, stub_embedding_provider)

    tier3_filter = {"ticket_priority": "high"}

    keyword_resp = await t0082_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.KEYWORD,
            query="alpha-marker",
            filters=RetrievalFilters(doc_type="ticket", tier3_metadata=tier3_filter),
            limit=100,
        )
    )
    semantic_resp = await t0082_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.SEMANTIC,
            query="alpha-marker",
            filters=RetrievalFilters(doc_type="ticket", tier3_metadata=tier3_filter),
            limit=100,
        )
    )

    keyword_ids = {hit.document.id for hit in keyword_resp.results}
    semantic_ids = {hit.document.id for hit in semantic_resp.results}
    assert keyword_ids == semantic_ids
    assert keyword_ids, "expected at least one tier3=high doc; got none"


# ---------------------------------------------------------------------------
# T3 — typo'd tier3 key rejected in semantic mode
# ---------------------------------------------------------------------------


async def test_t0082_semantic_tier3_typo_key_raises_against_doc_type_schema(
    tmp_vault_dir,
    graph_store,
    stub_content_store,
    stub_embedding_provider,
    t0082_retrieval_service,
):
    """A tier3 key not declared in the resolved doc_type's
    metadata_schema must raise ``Tier3SchemaViolationError`` BEFORE the
    semantic query reaches retrieval. Symmetric with
    ``test_t0075_catalog_filter_unknown_tier3_key_raises_against_doc_type_schema``;
    closes the AC that says typo'd keys error regardless of mode."""
    await _seed_tier3_vault(graph_store, stub_content_store, stub_embedding_provider)

    with pytest.raises(Tier3SchemaViolationError) as excinfo:
        await t0082_retrieval_service.discover(
            DiscoverRequest(
                mode=RetrievalMode.SEMANTIC,
                query="alpha-marker",
                filters=RetrievalFilters(
                    doc_type="ticket",
                    tier3_metadata={"ticekt_priority": "high"},  # typo
                ),
                limit=10,
            )
        )
    assert excinfo.value.detail["doc_type"] == "ticket"
    assert "ticekt_priority" in str(excinfo.value.detail.get("message", "")) or (
        "ticekt_priority" in str(excinfo.value.detail.get("path", ""))
    )


# ---------------------------------------------------------------------------
# T4 — anti-coincidental-pass gate
# ---------------------------------------------------------------------------


async def test_t0082_semantic_tier3_does_not_call_list_all_documents(
    tmp_vault_dir,
    graph_store,
    stub_content_store,
    stub_embedding_provider,
    t0082_retrieval_service,
    monkeypatch,
):
    """Optimization gate for the semantic-mode tier3 path:
    ``list_all_documents()`` must never be invoked. Monkeypatched to
    raise; the test passes iff the implementation routes tier3 through
    the SQL ``query_documents()`` call instead.

    Symmetric with T-0076's gate, but binds to the tier3 path
    specifically -- T-0076's gate uses project/lifecycle filters and
    would not catch a tier3-only regression to the Python post-filter
    pattern."""
    docs = await _seed_tier3_vault(graph_store, stub_content_store, stub_embedding_provider)

    async def _forbidden(*_args, **_kwargs):
        raise AssertionError(
            "list_all_documents() must not be called from semantic-mode "
            "tier3 filter resolution (T-0082)"
        )

    monkeypatch.setattr(graph_store, "list_all_documents", _forbidden)

    response = await t0082_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.SEMANTIC,
            query="alpha-marker",
            filters=RetrievalFilters(
                doc_type="ticket",
                tier3_metadata={"ticket_priority": "high"},
            ),
            limit=10,
        )
    )
    # Cross-check: a future stub that swallows the AssertionError still
    # produces the wrong result here.
    returned_ids = {hit.document.id for hit in response.results}
    assert returned_ids == {docs["d_high_ticket"].id}
