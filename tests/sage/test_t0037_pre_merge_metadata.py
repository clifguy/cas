"""T-0037 pre-merge metadata tests.

Validates the partial-metadata window closure: IngestionService.ingest()
must compute all metadata in memory before the atomic insert, so a failure
in any caller-metadata-application path raises before any record is durable.

Test groups (mapped to the plan at /Users/clifguy/.claude/plans/...):

- Group A: Atomicity guarantees via failure injection.
- Group B: T-0036 regression (codes-as-list polymorphism), folded in.
- Group C: Precedence parity matrix entries not already covered by
  tests/sage/test_ingestion.py or tests/sage/test_ad021_ingestion.py.
- Group D: Single-write structural assertion.
- Group E: Pure-helper unit tests for the new private statics.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.config import VaultConfig
from sage.models.enums import SourceType
from sage.models.schemas import Document, IngestRequest
from sage.services.ingestion import IngestionService
from sage.services.lifecycle import LifecycleService
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from tests.sage.test_ingestion_metadata_extraction import (
    _pim_vault_config_dict,
)

# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _create_test_file(
    tmp_vault_dir: Path, relative_path: str, content: str = "# Test\n\nBody."
) -> Path:
    full_path = tmp_vault_dir / "sources" / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content)
    return full_path


def _content_hash(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode()).hexdigest()


@pytest.fixture
def pim_config(tmp_vault_dir):
    """PIM-style VaultConfig with filename_extraction enabled.

    Mirrors tests/sage/test_ad021_ingestion.py::pim_config so filename-parse
    tests have a vault that actually parses filenames.
    """
    return VaultConfig.model_validate(_pim_vault_config_dict(tmp_vault_dir))


def _build_pim_ingestion_service(config, graph_store, lock_manager):
    lifecycle = LifecycleService(graph_store, lock_manager, config)
    return IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
        config=config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
        lifecycle_service=lifecycle,
    )


@pytest.fixture
def pim_ingestion_service(pim_config, graph_store, lock_manager):
    return _build_pim_ingestion_service(pim_config, graph_store, lock_manager)


# ---------------------------------------------------------------------------
# Group A: atomicity guarantees via failure injection
# ---------------------------------------------------------------------------
#
# The injection target is _build_metadata_updates. Today it runs AFTER the
# atomic insert, so a raise inside it leaves a partial-metadata orphan
# record. After the T-0037 rework, the helper is invoked during the
# pre-merge phase, so the raise propagates BEFORE any insert touches the
# store -- no orphan is created.


def _patch_caller_metadata_to_raise(monkeypatch):
    def _raise(metadata):
        raise RuntimeError("injected: caller-metadata application failed")

    monkeypatch.setattr(
        IngestionService,
        "_build_metadata_updates",
        staticmethod(_raise),
    )


async def test_a1_failure_on_new_doc_no_predecessor_leaves_no_record(
    tmp_vault_dir, graph_store, ingestion_service, monkeypatch
):
    """A1: Injected failure on a new-doc-no-predecessor ingest must not
    leave a record behind. The atomic guarantee means find_documents_by_hashes
    returns empty for the file's content hash."""
    body = "# A1\n\nNo orphan after injected failure."
    _create_test_file(tmp_vault_dir, "a1.md", content=body)
    expected_hash = _content_hash(body)

    _patch_caller_metadata_to_raise(monkeypatch)

    request = IngestRequest(
        source="a1.md",
        adapter=SourceType.MARKDOWN,
        metadata={"title": "Caller Title"},
    )
    with pytest.raises(RuntimeError, match="injected"):
        await ingestion_service.ingest(request)

    matches = await graph_store.find_documents_by_hashes([expected_hash])
    assert matches == {}, f"orphan record leaked on no-predecessor branch: {matches}"


async def test_a2_failure_on_new_doc_with_predecessor_leaves_no_record_pred_active(
    tmp_vault_dir, graph_store, ingestion_service, monkeypatch
):
    """A2: Injected failure on a supersede ingest must roll back: no new
    record, predecessor still 'active'. Proves the atomic-supersede contract
    holds at the new boundary."""
    pred_body = "# Predecessor\n\nOriginal."
    _create_test_file(tmp_vault_dir, "pred.md", content=pred_body)
    pred_result = await ingestion_service.ingest(
        IngestRequest(
            source="pred.md",
            adapter=SourceType.MARKDOWN,
            metadata={"title": "Predecessor"},
        )
    )
    pred_id = pred_result.document.id
    assert pred_result.document.lifecycle_status == "active"

    succ_body = "# Successor\n\nNew version body."
    _create_test_file(tmp_vault_dir, "succ.md", content=succ_body)
    expected_succ_hash = _content_hash(succ_body)

    _patch_caller_metadata_to_raise(monkeypatch)

    request = IngestRequest(
        source="succ.md",
        adapter=SourceType.MARKDOWN,
        supersedes_document_id=pred_id,
        metadata={"title": "Caller Successor Title"},
    )
    with pytest.raises(RuntimeError, match="injected"):
        await ingestion_service.ingest(request)

    matches = await graph_store.find_documents_by_hashes([expected_succ_hash])
    assert matches == {}, f"orphan successor leaked on supersede branch: {matches}"

    pred_after = await graph_store.get_document(pred_id)
    assert pred_after is not None
    assert pred_after.lifecycle_status == "active", (
        "predecessor lifecycle was flipped despite failed insert"
    )


async def test_a3_failure_on_force_reingest_leaves_existing_record_unchanged(
    tmp_vault_dir, graph_store, ingestion_service, monkeypatch
):
    """A3: Injected failure during a force re-ingest must not partially
    mutate the existing record. The pre-call snapshot of metadata must
    equal the post-failure record.

    Force-reingest requires SAME content + force=True (the branch the code
    enters when ``hash_matches and request.force``). With different content
    the second ingest takes the new-doc branch instead and this test
    becomes A1, not A3."""
    body = "# A3\n\nForce-reingest body (content stays identical)."
    _create_test_file(tmp_vault_dir, "a3.md", content=body)
    first = await ingestion_service.ingest(
        IngestRequest(
            source="a3.md",
            adapter=SourceType.MARKDOWN,
            metadata={"title": "Original Title", "doc_type": "note"},
        )
    )
    doc_id = first.document.id
    pre = await graph_store.get_document(doc_id)
    pre_snapshot = {
        "title": pre.title,
        "doc_type": pre.doc_type,
        "tags": list(pre.tags or []),
        "metadata_confirmed": pre.metadata_confirmed,
        "document_date": pre.document_date,
    }

    _patch_caller_metadata_to_raise(monkeypatch)

    with pytest.raises(RuntimeError, match="injected"):
        await ingestion_service.ingest(
            IngestRequest(
                source="a3.md",
                adapter=SourceType.MARKDOWN,
                force=True,
                metadata={"title": "Mutated Title"},
            )
        )

    post = await graph_store.get_document(doc_id)
    assert post is not None
    post_snapshot = {
        "title": post.title,
        "doc_type": post.doc_type,
        "tags": list(post.tags or []),
        "metadata_confirmed": post.metadata_confirmed,
        "document_date": post.document_date,
    }
    assert post_snapshot == pre_snapshot, (
        "force-reingest partially mutated existing record on failed call"
    )


# ---------------------------------------------------------------------------
# Group B: T-0036 regression (folded into T-0037)
# ---------------------------------------------------------------------------


async def test_b1_caller_codes_list_form_ingests_cleanly(
    tmp_vault_dir, graph_store, ingestion_service
):
    """B1: caller metadata={"codes": ["A", "B"]} (list form) must complete
    without raising. IngestRequest.metadata is typed
    dict[str, str | list[str]] so the list form is a legal caller input.
    Today it raises AttributeError after the record is durable."""
    body = "# B1\n\ncodes-list happy path."
    _create_test_file(tmp_vault_dir, "b1.md", content=body)

    request = IngestRequest(
        source="b1.md",
        adapter=SourceType.MARKDOWN,
        metadata={"codes": ["A", "B"]},
    )
    result = await ingestion_service.ingest(request)
    assert result.document.tags == ["A", "B"]


async def test_b2_caller_codes_string_form_still_ingests(
    tmp_vault_dir, graph_store, ingestion_service
):
    """B2: caller metadata={"codes": "A, B"} (existing string form) must
    continue to map to tags=["A", "B"]. Protects the existing string branch
    from regression while the new list branch lands."""
    body = "# B2\n\ncodes-string regression guard."
    _create_test_file(tmp_vault_dir, "b2.md", content=body)

    request = IngestRequest(
        source="b2.md",
        adapter=SourceType.MARKDOWN,
        metadata={"codes": "A, B"},
    )
    result = await ingestion_service.ingest(request)
    assert result.document.tags == ["A", "B"]


# ---------------------------------------------------------------------------
# Group C: precedence parity matrix entries not already covered
# ---------------------------------------------------------------------------
#
# Coverage already in the suite:
#   * C1/C2 on no-pred: tests/sage/test_ad021_ingestion.py::AD021-001/002.
#   * C3/C4 on with-pred: tests/sage/test_ad021_ingestion.py::AD021-004/005.
#   * C5 on no-pred and force-reingest: tests/sage/test_ingestion.py BH-131/132.
#   * C6 on no-pred: AD021-001 (True) and AD021-002 (False).
#   * C7 on no-pred: AD021-001.
#
# Tests below fill gaps: predecessor-side and force-reingest sub-paths
# that no existing test asserts the combined-matrix entry for, plus the
# C8 timezone fallback (not previously exercised at all).


async def test_c5_with_predecessor_adapter_tag_merge(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_config,
    lifecycle_service,
):
    """C5.with-pred: adapter-tag merge runs cleanly when the new doc
    supersedes a predecessor. Adapter emits a fresh tag; caller-supplied
    tag survives; predecessor's tags are NOT carried over (chain
    inheritance excludes tags)."""
    from tests.sage.test_ingestion import _TagEmittingStubAdapter

    adapter = _TagEmittingStubAdapter(
        adapter_tags=["template:style:Z"],
        adapter_tag_prefixes=["template:"],
    )
    service = IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=stub_abstraction_provider,
        config=minimal_config,
        source_adapters={SourceType.MARKDOWN: adapter},
        lifecycle_service=lifecycle_service,
    )

    _create_test_file(tmp_vault_dir, "c5_pred.md", content="# pred\n\nbody")
    pred = await service.ingest(
        IngestRequest(
            source="c5_pred.md",
            adapter=SourceType.MARKDOWN,
            metadata={"codes": "pred-tag"},
        )
    )

    _create_test_file(tmp_vault_dir, "c5_succ.md", content="# succ\n\nbody")
    result = await service.ingest(
        IngestRequest(
            source="c5_succ.md",
            adapter=SourceType.MARKDOWN,
            supersedes_document_id=pred.document.id,
            metadata={"codes": "succ-tag"},
        )
    )

    assert "template:style:Z" in result.document.tags
    assert "succ-tag" in result.document.tags
    assert "pred-tag" not in result.document.tags


async def test_c6_metadata_confirmed_with_predecessor_default_true(
    tmp_vault_dir, graph_store, ingestion_service
):
    """C6.with-pred: metadata_confirmed=True by default (needs_review
    omitted) on the supersede branch."""
    _create_test_file(tmp_vault_dir, "c6_pred.md", content="# pred\n\nbody")
    pred = await ingestion_service.ingest(
        IngestRequest(source="c6_pred.md", adapter=SourceType.MARKDOWN)
    )
    _create_test_file(tmp_vault_dir, "c6_succ.md", content="# succ\n\nbody")
    result = await ingestion_service.ingest(
        IngestRequest(
            source="c6_succ.md",
            adapter=SourceType.MARKDOWN,
            supersedes_document_id=pred.document.id,
        )
    )
    assert result.document.metadata_confirmed is True


async def test_c6_metadata_confirmed_force_reingest_default_true(
    tmp_vault_dir, graph_store, ingestion_service
):
    """C6.force-reingest: metadata_confirmed=True by default on force
    re-ingest. Force-reingest branch requires same content + force=True."""
    _create_test_file(tmp_vault_dir, "c6_force.md", content="# force\n\nbody")
    first = await ingestion_service.ingest(
        IngestRequest(source="c6_force.md", adapter=SourceType.MARKDOWN)
    )
    result = await ingestion_service.ingest(
        IngestRequest(source="c6_force.md", adapter=SourceType.MARKDOWN, force=True)
    )
    assert result.document.id == first.document.id
    assert result.document.metadata_confirmed is True


async def test_c7_doc_type_defaults_to_misc_when_unresolved_with_predecessor_none(
    tmp_vault_dir, graph_store, ingestion_service
):
    """C7.with-pred: doc_type defaults to 'misc' when nothing in the
    precedence chain resolves it (predecessor has no doc_type either)."""
    _create_test_file(tmp_vault_dir, "c7_pred.md", content="# pred\n\nbody")
    pred = await ingestion_service.ingest(
        IngestRequest(source="c7_pred.md", adapter=SourceType.MARKDOWN)
    )
    assert pred.document.doc_type == "misc"

    _create_test_file(tmp_vault_dir, "c7_succ.md", content="# succ\n\nbody")
    result = await ingestion_service.ingest(
        IngestRequest(
            source="c7_succ.md",
            adapter=SourceType.MARKDOWN,
            supersedes_document_id=pred.document.id,
        )
    )
    assert result.document.doc_type == "misc"


async def test_c8_document_date_falls_back_to_vault_local_zone(
    tmp_vault_dir, graph_store, ingestion_service, monkeypatch
):
    """C8: document_date fallback uses vault-local-zone calendar date.
    A late-evening Pacific mtime that crosses UTC midnight must attribute
    to the local work date, not the next UTC date."""
    from sage.source_adapters.base import ProjectionResult

    body = "# C8\n\ntimezone fallback body."
    _create_test_file(tmp_vault_dir, "c8.md", content=body)

    # Spoof source_modified_at to 2026-05-13 23:30 America/Los_Angeles
    # which is 2026-05-14 06:30 UTC. The fallback must use the LA date,
    # i.e. 2026-05-13.
    spoofed_mtime = "2026-05-14T06:30:00+00:00"

    real_project = MarkdownAdapter().project

    async def project_with_spoofed_mtime(source_path, config=None):
        result = await real_project(source_path, config=config)
        new_meta = dict(result.metadata)
        new_meta["source_modified_at"] = spoofed_mtime
        return ProjectionResult(
            text=result.text,
            headings=result.headings,
            content_hash=result.content_hash,
            adapter_version=result.adapter_version,
            title=result.title,
            metadata=new_meta,
        )

    monkeypatch.setattr(
        ingestion_service._adapters[SourceType.MARKDOWN],
        "project",
        project_with_spoofed_mtime,
    )
    # Force the vault timezone to Los_Angeles so the fallback math is
    # deterministic regardless of host TZ.
    monkeypatch.setattr(ingestion_service._config.vault, "timezone", "America/Los_Angeles")

    result = await ingestion_service.ingest(
        IngestRequest(source="c8.md", adapter=SourceType.MARKDOWN)
    )
    assert result.document.document_date == "2026-05-13", (
        f"expected vault-local fallback 2026-05-13, got {result.document.document_date}"
    )


# ---------------------------------------------------------------------------
# Group D: single-write structural assertion
# ---------------------------------------------------------------------------
#
# The acceptance criterion: zero update_document() calls between the
# atomic insert and the background-pipeline dispatch on every branch.
# Tests below stub out the background pipeline so its post-insert
# update_document calls (semantic_abstract, indexed_at, etc.) do not
# pollute the spy counts.


def _spy_store_writes(monkeypatch, store):
    """Wrap the three store write primitives with call-counting spies."""
    counters = {
        "insert_document": 0,
        "insert_with_supersede_atomic": 0,
        "update_document": 0,
    }
    original = {name: getattr(store, name) for name in counters}

    def make_spy(name):
        async def spy(*args, **kwargs):
            counters[name] += 1
            return await original[name](*args, **kwargs)

        return spy

    for name in counters:
        monkeypatch.setattr(store, name, make_spy(name))
    return counters


async def test_d1_new_doc_no_predecessor_single_insert_zero_updates(
    tmp_vault_dir, graph_store, ingestion_service, monkeypatch
):
    """D1: new-doc-no-predecessor invokes insert_document exactly once
    and never calls update_document between insert and pipeline dispatch."""
    body = "# D1\n\nbody"
    _create_test_file(tmp_vault_dir, "d1.md", content=body)

    monkeypatch.setattr(ingestion_service, "_run_background_pipeline", AsyncMock())
    counters = _spy_store_writes(monkeypatch, graph_store)

    await ingestion_service.ingest(
        IngestRequest(source="d1.md", adapter=SourceType.MARKDOWN),
    )
    assert counters["insert_document"] == 1
    assert counters["insert_with_supersede_atomic"] == 0
    assert counters["update_document"] == 0


async def test_d2_new_doc_with_predecessor_single_atomic_insert_zero_updates(
    tmp_vault_dir, graph_store, ingestion_service, monkeypatch
):
    """D2: new-doc-with-predecessor invokes insert_with_supersede_atomic
    exactly once and never calls update_document between insert and
    pipeline dispatch."""
    _create_test_file(tmp_vault_dir, "d2_pred.md", content="# pred\n\nbody")
    pred = await ingestion_service.ingest(
        IngestRequest(source="d2_pred.md", adapter=SourceType.MARKDOWN)
    )

    _create_test_file(tmp_vault_dir, "d2_succ.md", content="# succ\n\nbody")

    monkeypatch.setattr(ingestion_service, "_run_background_pipeline", AsyncMock())
    counters = _spy_store_writes(monkeypatch, graph_store)

    await ingestion_service.ingest(
        IngestRequest(
            source="d2_succ.md",
            adapter=SourceType.MARKDOWN,
            supersedes_document_id=pred.document.id,
        ),
    )
    assert counters["insert_with_supersede_atomic"] == 1
    assert counters["insert_document"] == 0
    assert counters["update_document"] == 0


async def test_d3_force_reingest_single_update_zero_extra(
    tmp_vault_dir, graph_store, ingestion_service, monkeypatch
):
    """D3: force-reingest invokes update_document exactly once and never
    calls a second update before pipeline dispatch. Same content + force=True
    enters the force-reingest branch."""
    _create_test_file(tmp_vault_dir, "d3.md", content="# d3\n\nbody")
    await ingestion_service.ingest(IngestRequest(source="d3.md", adapter=SourceType.MARKDOWN))

    monkeypatch.setattr(ingestion_service, "_run_background_pipeline", AsyncMock())
    counters = _spy_store_writes(monkeypatch, graph_store)

    await ingestion_service.ingest(
        IngestRequest(source="d3.md", adapter=SourceType.MARKDOWN, force=True),
    )
    assert counters["update_document"] == 1
    assert counters["insert_document"] == 0
    assert counters["insert_with_supersede_atomic"] == 0


# ---------------------------------------------------------------------------
# Group E: pure-helper unit tests for the new private statics
# ---------------------------------------------------------------------------


def _doc(**kwargs) -> Document:
    """Construct a minimal valid Document with sensible defaults."""
    base = dict(
        id="aaaaaaaa_pred",
        title="Predecessor",
        source_type=SourceType.MARKDOWN,
        source_path="pred.md",
        lifecycle_status="active",
        source_content_hash="sha256:" + "a" * 64,
        adapter_version="0.0.1",
        created_by="testuser",
        created_at=datetime.now(timezone.utc),
        last_modified_by="testuser",
        updated_at=datetime.now(timezone.utc),
        projected_at=datetime.now(timezone.utc),
    )
    base.update(kwargs)
    return Document(**base)


def test_e1_chain_inheritance_respects_caller_keys():
    """E1: caller_keys block per-field inheritance even when the field is
    None on the new doc and non-None on the predecessor."""
    pred = _doc(doc_type="adr", project="CAS")
    out = IngestionService._compute_chain_inheritance(
        field_view={},
        predecessor=pred,
        caller_keys={"doc_type"},
    )
    assert out == {"project": "CAS"}


def test_e2_chain_inheritance_skips_predecessor_none_fields():
    """E2: predecessor None fields are not inherited."""
    pred = _doc(doc_type=None, project="CAS")
    out = IngestionService._compute_chain_inheritance(
        field_view={},
        predecessor=pred,
        caller_keys=set(),
    )
    assert out == {"project": "CAS"}


def test_e3_chain_inheritance_excludes_non_trio_fields():
    """E3: only the trio fields (doc_type, project, authority_scope)
    inherit. tags, document_date, and other fields never appear in the
    output dict."""
    pred = _doc(
        doc_type="adr",
        project="CAS",
        tags=["X"],
        document_date="2026-01-01",
        version_label="v1",
    )
    out = IngestionService._compute_chain_inheritance(
        field_view={},
        predecessor=pred,
        caller_keys=set(),
    )
    assert "tags" not in out
    assert "document_date" not in out
    assert "version_label" not in out
    assert out == {"doc_type": "adr", "project": "CAS"}


def test_e4_adapter_tag_merge_strips_then_merges():
    """E4: prefixed tags are stripped from current; adapter tags then
    merge in front of retained tags."""
    out = IngestionService._compute_adapter_tag_merge(
        current_tags=["adapter:old", "user:keep"],
        adapter_tags=["adapter:new"],
        adapter_tag_prefixes=["adapter:"],
    )
    assert out == ["adapter:new", "user:keep"]


def test_e5_adapter_tag_merge_preserves_dedupe_order():
    """E5: dedupe via dict.fromkeys preserves first-seen order."""
    out = IngestionService._compute_adapter_tag_merge(
        current_tags=["a", "b"],
        adapter_tags=["b", "c"],
        adapter_tag_prefixes=[],
    )
    assert out == ["b", "c", "a"]


def test_e6_adapter_tag_merge_empty_inputs():
    """E6: empty adapter inputs return current; empty current returns
    empty list."""
    assert IngestionService._compute_adapter_tag_merge(
        current_tags=["a"],
        adapter_tags=[],
        adapter_tag_prefixes=[],
    ) == ["a"]
    assert (
        IngestionService._compute_adapter_tag_merge(
            current_tags=[],
            adapter_tags=[],
            adapter_tag_prefixes=["adapter:"],
        )
        == []
    )
