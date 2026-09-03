"""Tests for BatchIngestService (TEST-BIS-001 through TEST-BIS-021).

Covers:
  - Service interface and return types (BIS-001, BIS-002)
  - File descriptor normalization (BIS-003, BIS-004)
  - Phase 1: Edge plan construction (BIS-005, BIS-006, BIS-007)
  - Phase 2: Per-file ingestion (BIS-008, BIS-009, BIS-010, BIS-011)
  - Phase 3: Post-ingest edge execution (BIS-012, BIS-013)
  - Progress callbacks (BIS-014, BIS-015, BIS-016, BIS-017)
  - Caller integration (BIS-018, BIS-019)
  - Summary counters (BIS-020)
  - Chunk-store lifecycle sync on supersession (BIS-021)
"""

import hashlib
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sage.adapters.interfaces import Chunk, NaturalKeyConflict
from sage.adapters.stubs import StubContentStore
from sage.api.errors import SAGEError, VaultSourcePathRefusedError
from sage.config import LifecycleTransition, TransitionTable
from sage.models.enums import EdgeType, SourceType
from sage.models.schemas import Document, Edge, IngestRequest, UnlinkResponse
from sage.services.batch_inference import EdgePlan
from sage.services.batch_ingest import (
    BatchIngestService,
    FileDescriptor,
    IngestSummary,
    ParsedMetadataInput,
)
from sage.services.ingestion import IngestResult
from sage.services.lifecycle import LifecycleService
from sage.storage.locks import DocumentLockManager


def _base_transition_table() -> TransitionTable:
    """The base vault's lifecycle table: supersede archives an active doc.

    A real table rather than a MagicMock attribute: edge execution reads
    the supersede transition off it to decide whether an edge may be
    created and what state its target lands in, so a mock would make both
    a MagicMock and silently defeat every lifecycle assertion below.
    """
    return TransitionTable(
        [
            LifecycleTransition(
                from_state="active",
                action="supersede",
                to_state="archived",
                creates_edge="supersedes",
            )
        ]
    )


_DOC_ID_RE = re.compile(r"^[0-9a-f]{8}_[a-z0-9_]+$")


def _id(name: str) -> str:
    """Translate a short test name to a shape-conformant document ID.

    The ID validator in sage/models/schemas.py requires the pattern
    ^[0-9a-f]{8}_[a-z0-9_]+$. Test fixtures use short readable names
    like "doc-existing"; this helper wraps them so the values still
    construct valid Document instances. Idempotent: an already-canonical
    id passes through unchanged.
    """
    if _DOC_ID_RE.fullmatch(name):
        return name
    slug = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "n"
    return f"{hashlib.sha256(name.encode()).hexdigest()[:8]}_{slug}"


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _sha(name: str) -> str:
    """Deterministic canonical Sha256 from a short test name.

    The Sha256Str validator requires `^sha256:[0-9a-f]{64}$`. Test
    fixtures historically used short readable strings like
    f"hash_{doc_id}" or "sha256:abc"; this helper maps any such
    name to a stable canonical Sha256. Idempotent.
    """
    if _SHA256_RE.fullmatch(name):
        return name
    return "sha256:" + hashlib.sha256(f"sage-test-hash:{name}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_document(doc_id: str, title: str = "Test", **kwargs) -> Document:
    """Create a minimal Document for testing.

    `doc_id` is wrapped with `_id()` so callers can pass short readable
    names like "doc-existing" or "v1"; the helper returns a
    shape-conformant DocumentIdStr. Already-canonical ids pass through.
    """
    now = datetime.now(timezone.utc)
    defaults = dict(
        id=_id(doc_id),
        title=title,
        source_type=SourceType.MARKDOWN,
        source_path="test.md",
        lifecycle_status="active",
        source_content_hash=_sha("abc"),
        adapter_version="1.0",
        created_by="test",
        created_at=now,
        last_modified_by="test",
        updated_at=now,
    )
    defaults.update(kwargs)
    return Document(**defaults)


def _make_ingest_result(doc_id: str, is_new: bool = True, **doc_kwargs) -> IngestResult:
    """Create an IngestResult wrapping a Document."""
    return IngestResult(document=_make_document(doc_id, **doc_kwargs), is_new=is_new)


def _make_services(
    *,
    abstraction_enabled: bool = False,
    existing_docs: list[Document] | None = None,
):
    """Create a mock SAGEServices bundle."""
    services = MagicMock()
    # A real lifecycle double and lock manager rather than MagicMock
    # attributes: edge execution validates and builds each supersession
    # through ``prepare_supersede`` and serializes on the lock manager,
    # so mocks would hand the atomic commit a MagicMock edge and defeat
    # every lifecycle assertion below. ``prepare_supersede`` and the
    # ``transition_table`` property touch only ``_table``, so bypassing
    # ``__init__`` gives the real logic without a store or config.
    lifecycle = LifecycleService.__new__(LifecycleService)
    lifecycle._table = _base_transition_table()
    services.lifecycle_service = lifecycle
    services.lock_manager = DocumentLockManager()

    # Config
    services.config.abstraction.enabled = abstraction_enabled

    # Graph store
    docs_pool: list[Document] = list(existing_docs or [])
    services.graph_store.list_all_documents = AsyncMock(return_value=docs_pool)

    async def _query_documents(
        filters=None,
        limit: int = 100,
        offset: int = 0,
        sort_by=None,
        sort_order=None,
        *,
        default_exclude_failed: bool = True,
    ):
        """Filter-aware double for GraphStore.query_documents.

        Mirrors the subset of predicate behaviour exercised by
        _build_edge_plan after app-side filter pushdown:
        lifecycle_status, project, doc_type. Other keys are passed
        through (no filter applied) so unrelated callers stay agnostic
        of fixture detail. Pagination is not implemented because no
        test depends on it. ``default_exclude_failed`` is
        accepted to match the production signature; this double does
        not model pipeline_status filtering.
        """
        result = list(docs_pool)
        if filters:
            if filters.get("lifecycle_status"):
                result = [d for d in result if d.lifecycle_status == filters["lifecycle_status"]]
            if filters.get("project"):
                result = [d for d in result if d.project == filters["project"]]
            if filters.get("doc_type"):
                result = [d for d in result if d.doc_type == filters["doc_type"]]
        return result, len(result)

    services.graph_store.query_documents = AsyncMock(side_effect=_query_documents)
    services.graph_store.get_edges_by_source = AsyncMock(return_value=[])

    # Ingestion service -- default: succeed and return new doc
    call_count = 0

    async def _ingest(request, **kwargs):
        nonlocal call_count
        call_count += 1
        doc_id = f"aaaaaaaa_doc_{call_count}"
        return _make_ingest_result(doc_id, title=request.source)

    services.ingestion_service.ingest = AsyncMock(side_effect=_ingest)

    # Graph ops (Batch_inference now calls _create_edge and
    # passes on_conflict to insert_staging_edge; both return tuples).
    from unittest.mock import MagicMock as _MM

    services.graph_ops_service._create_edge_strict = AsyncMock()
    services.graph_ops_service._create_edge = AsyncMock(return_value=(_MM(), True))
    services.graph_store.insert_staging_edge = AsyncMock(return_value=(_MM(), True))

    # Edge execution reads the supersedes target's lifecycle state to
    # settle the transition before writing the edge. These tests ingest
    # their targets in the same batch, so the double reports them present
    # and active rather than absent.
    async def _get_document(doc_id):
        doc = _MM()
        doc.id = doc_id
        doc.lifecycle_status = "active"
        return doc

    services.graph_store.get_document = AsyncMock(side_effect=_get_document)
    services.graph_store.update_document = AsyncMock()
    services.graph_store.supersede_atomic = AsyncMock(return_value=_MM())

    return services


def _fd(
    file_path: str,
    source_type: str = "markdown",
    *,
    title: str | None = None,
    date: str | None = None,
    project: str | None = None,
    codes: list[str] | None = None,
    version: str | None = None,
    doc_type: str | None = None,
) -> FileDescriptor:
    """Shorthand FileDescriptor builder."""
    pm = None
    if any(v is not None for v in (title, date, project, codes, version, doc_type)):
        pm = ParsedMetadataInput(
            title=title or Path(file_path).stem,
            date=date,
            project=project,
            codes=codes or [],
            version=version,
            doc_type=doc_type,
        )
    return FileDescriptor(file_path=file_path, source_type=source_type, parsed_metadata=pm)


# ---------------------------------------------------------------------------
# 1. Service Interface (BIS-001, BIS-002)
# ---------------------------------------------------------------------------


class TestServiceInterface:
    @pytest.mark.asyncio
    async def test_bis_001_returns_ingest_summary(self):
        """BatchIngestService returns IngestSummary with correct fields."""
        services = _make_services()
        svc = BatchIngestService()

        result = await svc.run(
            files=[
                _fd("/tmp/doc1.md", title="Doc1"),
                _fd("/tmp/doc2.md", title="Doc2"),
            ],
            vault_services=services,
            infer_edges=False,
        )

        assert isinstance(result, IngestSummary)
        assert result.docs_new == 2
        assert result.docs_version == 0
        assert result.error_count == 0
        assert result.edges_created == {}
        assert result.edges_staged == {}
        assert result.edges_dropped == 0

    @pytest.mark.asyncio
    async def test_bis_002_empty_file_list_raises(self):
        """Empty file list raises ValueError."""
        services = _make_services()
        svc = BatchIngestService()

        with pytest.raises(ValueError, match="No files selected"):
            await svc.run(files=[], vault_services=services, infer_edges=False)


# ---------------------------------------------------------------------------
# 2. File Descriptor Normalization (BIS-003, BIS-004)
# ---------------------------------------------------------------------------


class TestFileDescriptorNormalization:
    @pytest.mark.asyncio
    async def test_bis_003_file_descriptor_with_metadata(self):
        """Service accepts FileDescriptor with full metadata."""
        services = _make_services()
        svc = BatchIngestService()

        result = await svc.run(
            files=[
                _fd(
                    "/tmp/test.md",
                    title="Claim-Set",
                    date="2026-03-09",
                    project="EXAMPLE",
                    codes=["PV06"],
                    version="v7",
                    doc_type="design_spec",
                )
            ],
            vault_services=services,
            infer_edges=False,
        )

        assert result.docs_new == 1

        # Verify the IngestRequest passed to ingestion service
        call_args = services.ingestion_service.ingest.call_args
        request = call_args[0][0]
        assert isinstance(request, IngestRequest)
        assert request.metadata["title"] == "Claim-Set"
        assert request.metadata["date"] == "2026-03-09"
        assert request.metadata["project"] == "EXAMPLE"
        assert request.metadata["codes"] == "PV06"
        assert request.metadata["version_label"] == "v7"
        assert request.metadata["doc_type"] == "design_spec"

    @pytest.mark.asyncio
    async def test_bis_004_file_without_metadata(self):
        """Service handles files with no parsed_metadata."""
        services = _make_services()
        svc = BatchIngestService()

        result = await svc.run(
            files=[_fd("/tmp/bare.md")],
            vault_services=services,
            infer_edges=False,
        )

        assert result.docs_new == 1

        call_args = services.ingestion_service.ingest.call_args
        request = call_args[0][0]
        assert request.metadata is None


# ---------------------------------------------------------------------------
# 3. Phase 1: Edge Plan Construction (BIS-005, BIS-006, BIS-007)
# ---------------------------------------------------------------------------


class TestEdgePlanConstruction:
    @pytest.mark.asyncio
    async def test_bis_005_edge_plan_from_scan_and_existing(self):
        """Edge plan built from scan items and existing vault docs."""
        existing_doc = _make_document(
            "bbbbbbbb_existing_v5",
            title="Claim-Set",
            version_label="v5",
            doc_type="design_spec",
            tags=["PV06"],
            project="EXAMPLE",
        )
        services = _make_services(existing_docs=[existing_doc])
        svc = BatchIngestService()

        result = await svc.run(
            files=[
                _fd(
                    "/tmp/v6.md",
                    title="Claim-Set",
                    version="v6",
                    doc_type="design_spec",
                    codes=["PV06"],
                    project="EXAMPLE",
                ),
                _fd(
                    "/tmp/v7.md",
                    title="Claim-Set",
                    version="v7",
                    doc_type="design_spec",
                    codes=["PV06"],
                    project="EXAMPLE",
                ),
            ],
            vault_services=services,
            infer_edges=True,
        )

        # Should have created supersedes edges: v7->v6, v6->existing-v5
        assert result.edges_created.get("supersedes", 0) >= 2

    @pytest.mark.asyncio
    async def test_bis_006_edge_plan_skipped_when_disabled(self):
        """No edge plan when infer_edges=False."""
        services = _make_services()
        svc = BatchIngestService()

        result = await svc.run(
            files=[
                _fd("/tmp/v1.md", title="Doc", version="v1"),
                _fd("/tmp/v2.md", title="Doc", version="v2"),
            ],
            vault_services=services,
            infer_edges=False,
        )

        # list_all_documents should not have been called
        services.graph_store.list_all_documents.assert_not_called()
        assert result.edges_created == {}
        assert result.edges_staged == {}
        assert result.edges_dropped == 0

    @pytest.mark.asyncio
    async def test_bis_007_existing_doc_field_mapping(self):
        """Existing docs mapped with tags->codes, version_label->version."""
        existing_doc = _make_document(
            "doc-existing",
            title="Report",
            version_label="v3",
            doc_type="design_spec",
            tags=["PV06", "CF-1"],
            project="EXAMPLE",
        )
        services = _make_services(existing_docs=[existing_doc])
        svc = BatchIngestService()

        # Patch EdgeInferenceEngine to capture what it receives
        with patch("sage.services.batch_inference.EdgeInferenceEngine") as MockEngine:
            mock_engine = MockEngine.return_value
            mock_engine.build_edge_plan.return_value = EdgePlan()

            await svc.run(
                files=[_fd("/tmp/test.md", title="Test")],
                vault_services=services,
                infer_edges=True,
            )

            call_args = mock_engine.build_edge_plan.call_args
            _scan_items, existing_items = call_args[0]

            assert len(existing_items) == 1
            ei = existing_items[0]
            assert ei.ref == _id("doc-existing")
            assert ei.is_existing is True
            assert ei.parsed.title == "Report"
            assert ei.parsed.codes == ["PV06", "CF-1"]
            assert ei.parsed.version == "v3"
            assert ei.parsed.doc_type == "design_spec"
            assert ei.parsed.project == "EXAMPLE"

    # ---------------------------------------------------------------------
    # — filter pushdown for the existing-doc fetch in
    # _build_edge_plan. The site at app/backend/ingest_service.py
    # historically pulled the entire vault and filtered in Python.
    # ---------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_edge_plan_includes_active_and_chain_repair_only(self):
        """Existing-doc fetch must return exactly:

        * active docs (regardless of chain identity), and
        * non-active docs whose (title.lower(), project, doc_type) is in
          the incoming scan_chain_keys.

        Non-active docs that do NOT share a chain identity with any
        incoming versioned arrival must be excluded.
        """
        d_active = _make_document(
            "d_active_report_v1",
            title="Report",
            version_label="v1",
            project="alpha",
            doc_type="note",
            lifecycle_status="active",
        )
        d_archived_chain = _make_document(
            "d_archived_report_v2",
            title="Report",
            version_label="v2",
            project="alpha",
            doc_type="note",
            lifecycle_status="archived",
        )
        d_archived_unrelated = _make_document(
            "d_archived_memo_v1",
            title="Memo",
            version_label="v1",
            project="alpha",
            doc_type="note",
            lifecycle_status="archived",
        )
        d_completed_unrelated = _make_document(
            "d_completed_other",
            title="Other",
            project="beta",
            doc_type="note",
            lifecycle_status="completed",
        )
        services = _make_services(
            existing_docs=[
                d_active,
                d_archived_chain,
                d_archived_unrelated,
                d_completed_unrelated,
            ]
        )
        svc = BatchIngestService()

        with patch("sage.services.batch_inference.EdgeInferenceEngine") as MockEngine:
            mock_engine = MockEngine.return_value
            mock_engine.build_edge_plan.return_value = EdgePlan()

            await svc.run(
                files=[
                    _fd(
                        "/tmp/incoming-v3.md",
                        title="Report",
                        version="v3",
                        project="alpha",
                        doc_type="note",
                    )
                ],
                vault_services=services,
                infer_edges=True,
            )

            _scan_items, existing_items = mock_engine.build_edge_plan.call_args[0]
            existing_refs = {ei.ref for ei in existing_items}
            assert d_active.id in existing_refs
            assert d_archived_chain.id in existing_refs
            assert d_archived_unrelated.id not in existing_refs
            assert d_completed_unrelated.id not in existing_refs

    @pytest.mark.asyncio
    async def test_edge_plan_does_not_call_list_all_documents(self):
        """Anti-coincidental gate: list_all_documents() must not be
        invoked from _build_edge_plan. Monkeypatched to raise; the
        cross-check on the engine call confirms the SQL-pushdown path
        actually populated existing_items (so a future stub that
        silently swallowed the AssertionError would still flunk).
        """
        d_active = _make_document("d_active_only", title="Solo", lifecycle_status="active")
        services = _make_services(existing_docs=[d_active])
        services.graph_store.list_all_documents = AsyncMock(
            side_effect=AssertionError(
                "list_all_documents() must not be called from _build_edge_plan (T-0076)"
            )
        )
        svc = BatchIngestService()

        with patch("sage.services.batch_inference.EdgeInferenceEngine") as MockEngine:
            mock_engine = MockEngine.return_value
            mock_engine.build_edge_plan.return_value = EdgePlan()

            await svc.run(
                files=[_fd("/tmp/x.md", title="X", version="v1")],
                vault_services=services,
                infer_edges=True,
            )

            _scan_items, existing_items = mock_engine.build_edge_plan.call_args[0]
            assert any(ei.ref == d_active.id for ei in existing_items), (
                "SQL-pushdown path failed to surface the active doc"
            )


# ---------------------------------------------------------------------------
# 4. Phase 2: Per-File Ingestion (BIS-008, BIS-009, BIS-010, BIS-011)
# ---------------------------------------------------------------------------


class TestPerFileIngestion:
    @pytest.mark.asyncio
    async def test_bis_008_sequential_ingestion(self):
        """Files ingested sequentially in order."""
        services = _make_services()
        svc = BatchIngestService()
        paths_ingested = []

        original_ingest = services.ingestion_service.ingest.side_effect

        async def tracking_ingest(request, **kwargs):
            paths_ingested.append(request.source)
            return await original_ingest(request)

        services.ingestion_service.ingest = AsyncMock(side_effect=tracking_ingest)

        await svc.run(
            files=[
                _fd("/tmp/a.md", title="A"),
                _fd("/tmp/b.md", title="B"),
                _fd("/tmp/c.md", title="C"),
            ],
            vault_services=services,
            infer_edges=False,
        )

        assert paths_ingested == ["/tmp/a.md", "/tmp/b.md", "/tmp/c.md"]

    @pytest.mark.asyncio
    async def test_bis_009_metadata_conversion(self):
        """Codes joined as comma-separated, version mapped to version_label."""
        services = _make_services()
        svc = BatchIngestService()

        await svc.run(
            files=[
                _fd(
                    "/tmp/test.md",
                    title="Claim-Set",
                    date="2026-03-09",
                    project="EXAMPLE",
                    codes=["PV06", "CF-1"],
                    version="v7",
                    doc_type="design_spec",
                )
            ],
            vault_services=services,
            infer_edges=False,
        )

        call_args = services.ingestion_service.ingest.call_args
        request = call_args[0][0]
        assert request.metadata["codes"] == "PV06,CF-1"
        assert request.metadata["version_label"] == "v7"
        assert "version" not in request.metadata

    @pytest.mark.asyncio
    async def test_bis_010_per_file_error_isolation(self):
        """Per-file errors do not abort the batch."""
        services = _make_services()
        call_idx = 0

        async def failing_ingest(request, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 2:
                raise RuntimeError("Adapter failure on file 2")
            return _make_ingest_result(f"doc-{call_idx}")

        services.ingestion_service.ingest = AsyncMock(side_effect=failing_ingest)
        svc = BatchIngestService()

        result = await svc.run(
            files=[
                _fd("/tmp/ok1.md", title="OK1"),
                _fd("/tmp/bad.md", title="Bad"),
                _fd("/tmp/ok2.md", title="OK2"),
            ],
            vault_services=services,
            infer_edges=False,
        )

        assert result.docs_new == 2
        assert result.error_count == 1
        assert len(result.errors) == 1
        assert result.errors[0]["filename"] == "bad.md"
        assert "Adapter failure" in result.errors[0]["message"]

    @pytest.mark.asyncio
    async def test_bis_021_sage_error_entry_carries_code_and_detail(self):
        """A per-file failure that is a SAGEError reports its code and typed
        detail alongside the filename and message, and the batch continues.

        Anti-coincidental-pass: whole-dict equality. A collection that adds
        ``code`` but drops ``detail``, or that lands either under another
        key, would pass any per-key containment check; only the exact entry
        pins the published shape.
        """
        services = _make_services()
        call_idx = 0

        async def refusing_ingest(request, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 2:
                raise VaultSourcePathRefusedError("caller/x.md", "refused here")
            return _make_ingest_result(f"doc-{call_idx}")

        services.ingestion_service.ingest = AsyncMock(side_effect=refusing_ingest)
        svc = BatchIngestService()

        result = await svc.run(
            files=[_fd("/tmp/ok1.md"), _fd("/tmp/bad.md"), _fd("/tmp/ok2.md")],
            vault_services=services,
            infer_edges=False,
        )

        assert result.docs_new == 2
        assert result.error_count == 1
        assert result.errors == [
            {
                "file_index": 1,
                "filename": "bad.md",
                "source_path": "/tmp/bad.md",
                "message": "refused here",
                "code": "vault_source_path_refused",
                "detail": {"source_path": "caller/x.md"},
            }
        ]

    @pytest.mark.asyncio
    async def test_bis_022_non_sage_error_entry_stays_message_only(self):
        """An entry carries only the typed fields its error actually has: a
        failure that is not a SAGEError reports the filename, the caller's
        path, and the message and nothing else; a SAGEError that carries no
        detail reports its code and no ``detail`` key.

        Anti-coincidental-pass: whole-dict equality pins the *absence* of
        ``code`` and ``detail`` on the bare exception, and of ``detail`` on
        the detail-less SAGEError. An implementation that always emits the
        optional fields as ``None`` would invent a typed envelope for an
        error that has none; one that emits ``detail`` whenever the error is
        a SAGEError would pass the bare-exception case alone, which is why
        the second file is there.
        """
        services = _make_services()
        call_idx = 0

        async def failing_ingest(request, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                raise RuntimeError("boom")
            if call_idx == 2:
                raise SAGEError("adapter_not_found", "no adapter", 400)
            return _make_ingest_result(f"doc-{call_idx}")

        services.ingestion_service.ingest = AsyncMock(side_effect=failing_ingest)
        svc = BatchIngestService()

        result = await svc.run(
            files=[_fd("/tmp/bad.md"), _fd("/tmp/typed.md"), _fd("/tmp/ok.md")],
            vault_services=services,
            infer_edges=False,
        )

        assert result.error_count == 2
        assert result.errors == [
            {
                "file_index": 0,
                "filename": "bad.md",
                "source_path": "/tmp/bad.md",
                "message": "boom",
            },
            {
                "file_index": 1,
                "filename": "typed.md",
                "source_path": "/tmp/typed.md",
                "message": "no adapter",
                "code": "adapter_not_found",
            },
        ]

    @pytest.mark.asyncio
    async def test_bis_023_declared_source_reaches_ingest_and_names_the_entry(self):
        """A descriptor's declared source is handed to the per-file ingest as
        its ``caller_source`` and names the error entry's ``source_path``; a
        descriptor without one leaves ``caller_source`` unset and reports its
        own ``file_path``.

        Anti-coincidental-pass: the declared spelling carries a ``/./``
        segment, so an implementation that round-trips it through ``Path``
        hands back a string the caller did not send and fails the equality.
        The kwarg is asserted on the mock's recorded calls, so deriving
        ``source_path`` from ``file_path`` while never passing the declared
        source through cannot pass.
        """
        services = _make_services()

        async def failing_ingest(request, **kwargs):
            raise RuntimeError("boom")

        services.ingestion_service.ingest = AsyncMock(side_effect=failing_ingest)
        svc = BatchIngestService()

        declared = "/Users/me/./note.md"
        result = await svc.run(
            files=[
                FileDescriptor(
                    file_path="/srv/stage/abc/note.md",
                    source_type="markdown",
                    declared_source=declared,
                ),
                FileDescriptor(file_path="/srv/local/plain.md", source_type="markdown"),
            ],
            vault_services=services,
            infer_edges=False,
        )

        calls = services.ingestion_service.ingest.call_args_list
        assert calls[0].kwargs["caller_source"] == declared
        assert calls[1].kwargs["caller_source"] is None
        assert [e["source_path"] for e in result.errors] == [declared, "/srv/local/plain.md"]
        assert [e["filename"] for e in result.errors] == ["note.md", "plain.md"]

    @pytest.mark.asyncio
    async def test_bis_024_error_entry_carries_the_files_batch_position(self):
        """Each error entry names the failed file's zero-based position in the
        batch -- the same index the ``on_file_error`` callback reports -- so a
        consumer can tell apart two failures whose ``filename`` and
        ``source_path`` coincide, as two same-named uploads do.

        Anti-coincidental-pass: the failures sit at positions 0 and 2 of a
        three-file batch, so an index taken from the entry's ordinal among
        the errors reports ``[0, 1]``, and a constant reports two equal
        values; both fail the equality against the callback's indices. All
        three files share a basename, so nothing but the index separates
        the two entries.
        """
        services = _make_services()
        call_idx = 0

        async def failing_ingest(request, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if call_idx in (1, 3):
                raise RuntimeError(f"boom {call_idx}")
            return _make_ingest_result(f"doc-{call_idx}")

        services.ingestion_service.ingest = AsyncMock(side_effect=failing_ingest)
        reported: list[int] = []

        async def on_file_error(index, total, filename, message):
            reported.append(index)

        svc = BatchIngestService()
        result = await svc.run(
            files=[
                FileDescriptor(file_path="/srv/stage/0/same.md", source_type="markdown"),
                FileDescriptor(file_path="/srv/stage/1/same.md", source_type="markdown"),
                FileDescriptor(file_path="/srv/stage/2/same.md", source_type="markdown"),
            ],
            vault_services=services,
            infer_edges=False,
            on_file_error=on_file_error,
        )

        assert result.error_count == 2
        assert reported == [0, 2]
        assert [e["file_index"] for e in result.errors] == reported
        assert [e["filename"] for e in result.errors] == ["same.md", "same.md"]

    @pytest.mark.asyncio
    async def test_bis_011_abstract_tracking(self):
        """Abstract tracking uses config.abstraction.enabled."""
        # Abstraction disabled
        services = _make_services(abstraction_enabled=False)
        svc = BatchIngestService()

        result = await svc.run(
            files=[_fd("/tmp/a.md"), _fd("/tmp/b.md")],
            vault_services=services,
            infer_edges=False,
        )

        assert result.abstracts_generated == 0
        assert result.abstracts_deferred == 2

        # Abstraction enabled
        services2 = _make_services(abstraction_enabled=True)
        result2 = await svc.run(
            files=[_fd("/tmp/c.md"), _fd("/tmp/d.md")],
            vault_services=services2,
            infer_edges=False,
        )

        assert result2.abstracts_generated == 2
        assert result2.abstracts_deferred == 0


# ---------------------------------------------------------------------------
# 5. Phase 3: Post-Ingest Edge Execution (BIS-012, BIS-013)
# ---------------------------------------------------------------------------


class TestEdgeExecution:
    @pytest.mark.asyncio
    async def test_bis_012_edge_plan_executed_after_ingestion(self):
        """Edge plan resolved and executed after all files ingested."""
        services = _make_services()
        svc = BatchIngestService()

        result = await svc.run(
            files=[
                _fd("/tmp/v1.md", title="Doc", version="v1", doc_type="design_spec"),
                _fd("/tmp/v2.md", title="Doc", version="v2", doc_type="design_spec"),
            ],
            vault_services=services,
            infer_edges=True,
        )

        # Should have a supersedes edge
        assert result.edges_created.get("supersedes", 0) >= 1

    @pytest.mark.asyncio
    async def test_bis_013_edges_dropped_for_failed_files(self):
        """Edges dropped when referenced file failed ingestion."""
        services = _make_services()
        call_idx = 0

        async def partial_ingest(request, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                return _make_ingest_result("doc-v1")
            raise RuntimeError("File 2 failed")

        services.ingestion_service.ingest = AsyncMock(side_effect=partial_ingest)
        svc = BatchIngestService()

        result = await svc.run(
            files=[
                _fd("/tmp/v1.md", title="Doc", version="v1", doc_type="design_spec"),
                _fd("/tmp/v2.md", title="Doc", version="v2", doc_type="design_spec"),
            ],
            vault_services=services,
            infer_edges=True,
        )

        assert result.error_count == 1
        # The supersedes edge referencing v2 should be dropped
        assert result.edges_dropped >= 1
        assert len(result.edge_warnings) >= 1
        assert result.edge_warnings[0]["reason"] == "ingestion_failed"


# ---------------------------------------------------------------------------
# 6. Progress Callbacks (BIS-014, BIS-015, BIS-016, BIS-017)
# ---------------------------------------------------------------------------


class TestProgressCallbacks:
    @pytest.mark.asyncio
    async def test_bis_014_on_file_start_called(self):
        """on_file_start callback invoked before each file."""
        services = _make_services()
        svc = BatchIngestService()
        started = []

        async def on_start(index, total, filename):
            started.append((index, total, filename))

        await svc.run(
            files=[
                _fd("/tmp/doc1.md", title="Doc1"),
                _fd("/tmp/doc2.md", title="Doc2"),
            ],
            vault_services=services,
            infer_edges=False,
            on_file_start=on_start,
        )

        assert started == [(0, 2, "doc1.md"), (1, 2, "doc2.md")]

    @pytest.mark.asyncio
    async def test_bis_015_on_file_done_called(self):
        """on_file_done callback invoked after successful ingestion."""
        services = _make_services()
        svc = BatchIngestService()
        done = []

        async def on_done(index, total, filename, document_id):
            done.append((index, total, filename, document_id))

        await svc.run(
            files=[
                _fd("/tmp/doc1.md", title="Doc1"),
                _fd("/tmp/doc2.md", title="Doc2"),
            ],
            vault_services=services,
            infer_edges=False,
            on_file_done=on_done,
        )

        assert len(done) == 2
        assert done[0][0] == 0
        assert done[0][2] == "doc1.md"
        assert done[0][3] is not None  # document_id
        assert done[1][0] == 1
        assert done[1][2] == "doc2.md"

    @pytest.mark.asyncio
    async def test_bis_016_on_file_error_called(self):
        """on_file_error callback invoked on per-file failure."""
        services = _make_services()
        call_idx = 0

        async def failing_ingest(request, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 2:
                raise RuntimeError("Adapter crash")
            return _make_ingest_result(f"doc-{call_idx}")

        services.ingestion_service.ingest = AsyncMock(side_effect=failing_ingest)
        svc = BatchIngestService()

        done = []
        errors = []

        async def on_done(index, total, filename, document_id):
            done.append(filename)

        async def on_error(index, total, filename, error_message):
            errors.append((filename, error_message))

        await svc.run(
            files=[
                _fd("/tmp/ok1.md"),
                _fd("/tmp/bad.md"),
                _fd("/tmp/ok2.md"),
            ],
            vault_services=services,
            infer_edges=False,
            on_file_done=on_done,
            on_file_error=on_error,
        )

        assert len(done) == 2
        assert len(errors) == 1
        assert errors[0][0] == "bad.md"
        assert "Adapter crash" in errors[0][1]

    @pytest.mark.asyncio
    async def test_bis_017_callbacks_optional(self):
        """Callbacks default to None with no errors."""
        services = _make_services()
        call_idx = 0

        async def mixed_ingest(request, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 2:
                raise RuntimeError("fail")
            return _make_ingest_result(f"doc-{call_idx}")

        services.ingestion_service.ingest = AsyncMock(side_effect=mixed_ingest)
        svc = BatchIngestService()

        # No callbacks passed -- should not raise
        result = await svc.run(
            files=[_fd("/tmp/a.md"), _fd("/tmp/b.md")],
            vault_services=services,
            infer_edges=False,
        )

        assert result.docs_new == 1
        assert result.error_count == 1


# ---------------------------------------------------------------------------
# 7. Caller Integration (BIS-018, BIS-019)
# ---------------------------------------------------------------------------


class TestCallerIntegration:
    @pytest.mark.asyncio
    async def test_bis_018_summary_dict_structure(self):
        """IngestSummary.to_dict() produces the JSON structure both callers need."""
        services = _make_services()
        svc = BatchIngestService()

        result = await svc.run(
            files=[_fd("/tmp/test.md", title="Test")],
            vault_services=services,
            infer_edges=False,
        )

        d = result.to_dict()
        assert d["documents_created"] == {"new": 1, "new_version": 0}
        assert d["metadata_pending"] == 1
        assert d["edges_created"] == {}
        assert d["edges_staged"] == {}
        assert d["edges_dropped"] == 0
        assert "edge_warnings" not in d  # omitted when empty
        assert d["abstracts_deferred"] == 1
        assert d["abstracts_generated"] == 0
        assert d["error_count"] == 0
        assert d["errors"] == []

    @pytest.mark.asyncio
    async def test_bis_019_summary_with_edges_and_errors(self):
        """Summary captures edges and errors from a mixed batch."""
        services = _make_services()
        call_idx = 0

        async def mixed_ingest(request, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 3:
                raise RuntimeError("corrupt file")
            return _make_ingest_result(f"aaaaaaaa_doc_{call_idx}")

        services.ingestion_service.ingest = AsyncMock(side_effect=mixed_ingest)
        svc = BatchIngestService()

        result = await svc.run(
            files=[
                _fd("/tmp/v1.md", title="Doc", version="v1", doc_type="design_spec"),
                _fd("/tmp/v2.md", title="Doc", version="v2", doc_type="design_spec"),
                _fd("/tmp/bad.md", title="Bad"),
            ],
            vault_services=services,
            infer_edges=True,
        )

        d = result.to_dict()
        assert d["documents_created"]["new"] == 2
        assert d["error_count"] == 1
        assert len(d["errors"]) == 1
        assert d["errors"][0]["filename"] == "bad.md"
        # Should have at least one supersedes edge from the version chain
        assert d["edges_created"].get("supersedes", 0) >= 1

    @pytest.mark.asyncio
    async def test_bis_020_metadata_pending_counts_unconfirmed_only(self):
        """metadata_pending counts docs with metadata_confirmed=False, not all new docs."""
        services = _make_services()
        call_idx = 0

        async def mixed_confirm(request, **kwargs):
            nonlocal call_idx
            call_idx += 1
            confirmed = call_idx == 2  # second doc auto-confirmed
            return _make_ingest_result(f"doc-{call_idx}", metadata_confirmed=confirmed)

        services.ingestion_service.ingest = AsyncMock(side_effect=mixed_confirm)
        svc = BatchIngestService()

        result = await svc.run(
            files=[
                _fd("/tmp/a.md", title="A"),
                _fd("/tmp/b.md", title="B"),
                _fd("/tmp/c.md", title="C"),
            ],
            vault_services=services,
            infer_edges=False,
        )

        assert result.docs_new == 3
        assert result.metadata_pending == 2
        assert result.to_dict()["metadata_pending"] == 2


# ---------------------------------------------------------------------------
# 8. Chain Repair (TEST-CR-001 through TEST-CR-005)
# ---------------------------------------------------------------------------
#
# Out-of-order ingestion of versioned documents must produce a correct linear
# supersedes chain. When a new version slots between existing chain members,
# the engine removes incorrect predecessor edges and inserts the right ones.
# Provenance gate: auto-removal only when removed edges were created by
# version_chain inference; otherwise the entire group's repair is staged.


from sage.models.enums import RationaleKind  # noqa: E402
from sage.models.schemas import (  # noqa: E402 -- grouped with the version-chain test section below
    LinkRequest,
)

VERSION_CHAIN_RATIONALE_PREFIX = "[version_chain]"
FILENAME_CODE_MATCH_RATIONALE_PREFIX = "[filename_code_match]"


def _eid(name: str) -> str:
    """Deterministic canonical-UUID edge id derived from a short test name."""
    return str(uuid.uuid5(uuid.NAMESPACE_OID, f"sage-test-edge:{name}"))


_E_V3_V1 = _eid("e-v3-v1")
_E_V2_V1 = _eid("e-v2-v1")
_E_V3_V2 = _eid("e-v3-v2")


def _make_edge(
    edge_id: str,
    source_id: str,
    target_id: str,
    *,
    edge_type: EdgeType = EdgeType.SUPERSEDES,
    rationale: str | None = None,
    rationale_kind: RationaleKind = RationaleKind.MANUAL,
) -> Edge:
    """Build a minimal Edge for in-memory mock graph state.

    ``rationale_kind`` defaults to ``MANUAL`` so existing
    chain-repair fixtures that constructed pre-existing edges without
    awareness of the typed discriminator now exercise the provenance
    gate against a non-version_chain edge (the explicit pattern in
    CR-002).
    """
    return Edge(
        id=edge_id,
        source_id=source_id,
        target_id=target_id,
        edge_type=edge_type,
        created_at=datetime.now(timezone.utc),
        rationale=rationale,
        rationale_kind=rationale_kind,
    )


class _MockGraphState:
    """In-memory graph state for chain-repair tests.

    Tracks documents and edges, and exposes async methods that match the
    GraphStore / GraphOpsService surfaces the chain-repair planner uses.
    """

    def __init__(
        self,
        docs: list[Document],
        edges: list[Edge],
    ):
        self.docs: dict[str, Document] = {d.id: d for d in docs}
        self.edges: dict[str, Edge] = {e.id: e for e in edges}
        self.removed_edge_ids: list[str] = []
        self.added_link_requests: list[LinkRequest] = []
        self.superseded_commits: list[tuple[str, dict, Edge]] = []
        self._next_edge_seq = 1

    async def list_all_documents(self) -> list[Document]:
        return list(self.docs.values())

    async def query_documents(
        self,
        filters=None,
        limit: int = 100,
        offset: int = 0,
        sort_by=None,
        sort_order=None,
        *,
        default_exclude_failed: bool = True,
    ):
        """Filter-aware double mirroring the subset of predicate
        behaviour _build_edge_plan relies on after pushdown:
        lifecycle_status, project, doc_type. Returns
        (matching_documents, total_count) like the real signature.
        Pagination is not implemented because no chain-repair test
        depends on it. ``default_exclude_failed`` is accepted
        to match the production signature; this double does not model
        pipeline_status filtering.
        """
        result = list(self.docs.values())
        if filters:
            if filters.get("lifecycle_status"):
                result = [d for d in result if d.lifecycle_status == filters["lifecycle_status"]]
            if filters.get("project"):
                result = [d for d in result if d.project == filters["project"]]
            if filters.get("doc_type"):
                result = [d for d in result if d.doc_type == filters["doc_type"]]
        return result, len(result)

    async def get_edges_by_source(self, source_id: str, edge_type: str | None = None) -> list[Edge]:
        return [
            e
            for e in self.edges.values()
            if e.source_id == source_id and (edge_type is None or e.edge_type.value == edge_type)
        ]

    async def get_edges_by_target(self, target_id: str, edge_type: str | None = None) -> list[Edge]:
        return [
            e
            for e in self.edges.values()
            if e.target_id == target_id and (edge_type is None or e.edge_type.value == edge_type)
        ]

    async def get_edge(self, edge_id: str) -> Edge | None:
        return self.edges.get(edge_id)

    async def get_document(self, doc_id: str) -> Document | None:
        return self.docs.get(doc_id)

    async def update_document(self, doc_id: str, fields: dict) -> None:
        d = self.docs[doc_id]
        for k, v in fields.items():
            setattr(d, k, v)

    async def insert_staging_edge(self, staging, on_conflict: str = "raise") -> tuple:
        # Tracking only; no real staging in unit tests. Returns the
        # tuple shape: (edge, created).
        if not hasattr(self, "staged_edges"):
            self.staged_edges = []
        self.staged_edges.append(staging)
        return staging, True

    async def _create_edge_strict(self, request: LinkRequest) -> dict:
        self.added_link_requests.append(request)
        edge_id = _eid(f"edge-new-{self._next_edge_seq}")
        self._next_edge_seq += 1
        self.edges[edge_id] = _make_edge(
            edge_id,
            request.source_id,
            request.target_id,
            edge_type=request.edge_type,
            rationale=request.rationale,
            rationale_kind=request.rationale_kind or RationaleKind.MANUAL,
        )
        return {"edge_id": edge_id}

    async def _create_edge(self, request: LinkRequest) -> tuple:
        # Returns (edge, created). The mock semantics here are
        # always-created because the test scenarios never replay the
        # same natural-key triple.
        edge_dict = await self._create_edge_strict(request)
        edge_id = edge_dict["edge_id"]
        return self.edges[edge_id], True

    async def unlink(self, edge_id: str) -> UnlinkResponse:
        self.removed_edge_ids.append(edge_id)
        if edge_id in self.edges:
            del self.edges[edge_id]
        return UnlinkResponse(deleted=True, edge_id=edge_id)

    async def supersede_atomic(
        self, predecessor_id: str, predecessor_updates: dict, edge: Edge
    ) -> Document | None:
        # Mirrors the production contract the tests rely on:
        # all-or-nothing, and NaturalKeyConflict on a duplicate natural
        # key raised before anything is applied.
        for existing in self.edges.values():
            if (
                existing.source_id == edge.source_id
                and existing.target_id == edge.target_id
                and existing.edge_type == edge.edge_type
            ):
                raise NaturalKeyConflict(edge.source_id, edge.target_id, edge.edge_type.value)
        await self.update_document(predecessor_id, predecessor_updates)
        self.edges[edge.id] = edge
        self.superseded_commits.append((predecessor_id, predecessor_updates, edge))
        return self.docs.get(predecessor_id)


def _make_chain_services(
    docs: list[Document],
    edges: list[Edge],
    *,
    abstraction_enabled: bool = False,
) -> tuple[MagicMock, _MockGraphState]:
    """Build a SAGEServices mock backed by a _MockGraphState."""
    state = _MockGraphState(docs, edges)
    services = MagicMock()
    # A real in-memory content store rather than the auto-created MagicMock
    # attribute: edge execution awaits ``update_chunk_metadata`` on it after
    # a supersede lifecycle write, and awaiting a plain MagicMock raises --
    # which the best-effort sync would swallow into a spurious
    # ``chunk_lifecycle_sync_failed`` warning in every test here.
    services.content_store = StubContentStore()
    # A real lifecycle double and lock manager for the same reason (see
    # ``_make_services``): the atomic supersede path validates and builds
    # its writes through ``prepare_supersede`` and serializes on the lock
    # manager.
    lifecycle = LifecycleService.__new__(LifecycleService)
    lifecycle._table = _base_transition_table()
    services.lifecycle_service = lifecycle
    services.lock_manager = DocumentLockManager()
    services.config.abstraction.enabled = abstraction_enabled
    services.graph_store.list_all_documents = AsyncMock(side_effect=state.list_all_documents)
    services.graph_store.query_documents = AsyncMock(side_effect=state.query_documents)
    services.graph_store.get_edges_by_source = AsyncMock(side_effect=state.get_edges_by_source)
    services.graph_store.get_edges_by_target = AsyncMock(side_effect=state.get_edges_by_target)
    services.graph_store.get_edge = AsyncMock(side_effect=state.get_edge)
    services.graph_store.get_document = AsyncMock(side_effect=state.get_document)
    services.graph_store.update_document = AsyncMock(side_effect=state.update_document)
    services.graph_store.supersede_atomic = AsyncMock(side_effect=state.supersede_atomic)
    services.graph_store.insert_staging_edge = AsyncMock(side_effect=state.insert_staging_edge)
    services.graph_ops_service._create_edge_strict = AsyncMock(
        side_effect=state._create_edge_strict
    )
    services.graph_ops_service._create_edge = AsyncMock(side_effect=state._create_edge)
    services.graph_ops_service.unlink = AsyncMock(side_effect=state.unlink)

    # Standard ingestion mock: each new file becomes a fresh document.
    call_count = 0

    async def _ingest(request, **kwargs):
        nonlocal call_count
        call_count += 1
        new_id = f"eeeeeeee_doc_new_{call_count}"
        new_doc = _make_document(
            new_id,
            title=request.metadata.get("title", "Untitled") if request.metadata else "Untitled",
            project=request.metadata.get("project") if request.metadata else None,
            doc_type=request.metadata.get("doc_type") if request.metadata else None,
            version_label=request.metadata.get("version_label") if request.metadata else None,
            tags=(
                request.metadata.get("codes", "").split(",")
                if request.metadata and request.metadata.get("codes")
                else []
            ),
        )
        state.docs[new_id] = new_doc
        return IngestResult(document=new_doc, is_new=True)

    services.ingestion_service.ingest = AsyncMock(side_effect=_ingest)
    return services, state


CHAIN_KW = dict(
    title="Claim-Set",
    project="EXAMPLE",
    doc_type="design_spec",
    codes=["PV06"],
)


class TestChainRepair:
    """Out-of-order arrival rebuilds correct linear supersedes chains."""

    @pytest.mark.asyncio
    async def test_cr_001_intermediate_arrival_repairs_chain(self):
        """v2 arriving after v1/v3 fixes the chain to v1<-v2<-v3."""
        V1_ID = "cccccccc_v1"
        V3_ID = "cccccccc_v3"
        v1 = _make_document(
            V1_ID,
            title="Claim-Set",
            project="EXAMPLE",
            doc_type="design_spec",
            version_label="v1",
            tags=["PV06"],
            lifecycle_status="archived",
        )
        v3 = _make_document(
            V3_ID,
            title="Claim-Set",
            project="EXAMPLE",
            doc_type="design_spec",
            version_label="v3",
            tags=["PV06"],
            lifecycle_status="active",
        )
        existing_edge = _make_edge(
            _E_V3_V1,
            V3_ID,
            V1_ID,
            rationale=f"{VERSION_CHAIN_RATIONALE_PREFIX} v3 supersedes v1 (title: Claim-Set)",
            rationale_kind=RationaleKind.VERSION_CHAIN,
        )
        services, state = _make_chain_services([v1, v3], [existing_edge])
        svc = BatchIngestService()

        result = await svc.run(
            files=[_fd("/tmp/v2.md", version="v2", **CHAIN_KW)],
            vault_services=services,
            infer_edges=True,
        )

        # Two new edges: v3->v2 and v2->v1
        assert result.edges_created.get("supersedes", 0) == 2
        # One removal: v3->v1
        assert result.edges_removed == 1
        assert _E_V3_V1 in state.removed_edge_ids

        # Surviving edges form the correct chain
        adjacency = {(e.source_id, e.target_id) for e in state.edges.values()}
        v2_id = next(d.id for d in state.docs.values() if d.version_label == "v2")
        assert (V3_ID, v2_id) in adjacency
        assert (v2_id, V1_ID) in adjacency
        assert (V3_ID, V1_ID) not in adjacency

        # Lifecycle: v1 still archived, v2 archived, v3 active
        assert state.docs[V1_ID].lifecycle_status == "archived"
        assert state.docs[v2_id].lifecycle_status == "archived"
        assert state.docs[V3_ID].lifecycle_status == "active"

    @pytest.mark.asyncio
    async def test_cr_002_provenance_gate_stages_when_removed_edge_is_manual(self):
        """Hand-curated v3->v1 edge is preserved; repair goes to staging."""
        V1_ID = "cccccccc_v1"
        V3_ID = "cccccccc_v3"
        v1 = _make_document(
            V1_ID,
            title="Claim-Set",
            project="EXAMPLE",
            doc_type="design_spec",
            version_label="v1",
            tags=["PV06"],
            lifecycle_status="archived",
        )
        v3 = _make_document(
            V3_ID,
            title="Claim-Set",
            project="EXAMPLE",
            doc_type="design_spec",
            version_label="v3",
            tags=["PV06"],
            lifecycle_status="active",
        )
        # Non-version_chain rationale -> manual edge
        existing_edge = _make_edge(
            _E_V3_V1,
            V3_ID,
            V1_ID,
            rationale="Manually curated by Clif: v3 directly supersedes v1.",
        )
        services, state = _make_chain_services([v1, v3], [existing_edge])
        svc = BatchIngestService()

        result = await svc.run(
            files=[_fd("/tmp/v2.md", version="v2", **CHAIN_KW)],
            vault_services=services,
            infer_edges=True,
        )

        # No Tier 1 changes
        assert result.edges_created.get("supersedes", 0) == 0
        assert result.edges_removed == 0
        # Repair lands in staging instead
        assert result.edges_staged.get("supersedes", 0) >= 2
        # Existing manual edge untouched
        assert _E_V3_V1 in state.edges
        assert state.removed_edge_ids == []
        # Lifecycle untouched: v2 not auto-archived
        v2_id = next(d.id for d in state.docs.values() if d.version_label == "v2")
        assert state.docs[v2_id].lifecycle_status == "active"
        assert state.docs[V3_ID].lifecycle_status == "active"

    @pytest.mark.asyncio
    async def test_cr_003_within_batch_out_of_order_still_correct(self):
        """Regression guard: [v1, v3, v2] in one batch yields v1<-v2<-v3."""
        services, state = _make_chain_services([], [])
        svc = BatchIngestService()

        result = await svc.run(
            files=[
                _fd("/tmp/v1.md", version="v1", **CHAIN_KW),
                _fd("/tmp/v3.md", version="v3", **CHAIN_KW),
                _fd("/tmp/v2.md", version="v2", **CHAIN_KW),
            ],
            vault_services=services,
            infer_edges=True,
        )

        assert result.edges_created.get("supersedes", 0) == 2
        assert result.edges_removed == 0

        # Map version labels to assigned doc IDs
        by_ver = {d.version_label: d.id for d in state.docs.values()}
        adjacency = {(e.source_id, e.target_id) for e in state.edges.values()}
        assert (by_ver["v3"], by_ver["v2"]) in adjacency
        assert (by_ver["v2"], by_ver["v1"]) in adjacency
        # v1 and v2 archived, v3 active
        assert state.docs[by_ver["v1"]].lifecycle_status == "archived"
        assert state.docs[by_ver["v2"]].lifecycle_status == "archived"
        assert state.docs[by_ver["v3"]].lifecycle_status == "active"

    @pytest.mark.asyncio
    async def test_cr_004_new_head_arrival_no_removals(self):
        """v4 onto v1<-v2<-v3 chain adds v3<-v4 with no edge removals."""
        V1_ID = "cccccccc_v1"
        V2_ID = "cccccccc_v2"
        V3_ID = "cccccccc_v3"
        v1 = _make_document(
            V1_ID,
            title="Claim-Set",
            project="EXAMPLE",
            doc_type="design_spec",
            version_label="v1",
            tags=["PV06"],
            lifecycle_status="archived",
        )
        v2 = _make_document(
            V2_ID,
            title="Claim-Set",
            project="EXAMPLE",
            doc_type="design_spec",
            version_label="v2",
            tags=["PV06"],
            lifecycle_status="archived",
        )
        v3 = _make_document(
            V3_ID,
            title="Claim-Set",
            project="EXAMPLE",
            doc_type="design_spec",
            version_label="v3",
            tags=["PV06"],
            lifecycle_status="active",
        )
        e1 = _make_edge(
            _E_V2_V1,
            V2_ID,
            V1_ID,
            rationale=f"{VERSION_CHAIN_RATIONALE_PREFIX} v2 supersedes v1 (title: Claim-Set)",
        )
        e2 = _make_edge(
            _E_V3_V2,
            V3_ID,
            V2_ID,
            rationale=f"{VERSION_CHAIN_RATIONALE_PREFIX} v3 supersedes v2 (title: Claim-Set)",
        )
        services, state = _make_chain_services([v1, v2, v3], [e1, e2])
        svc = BatchIngestService()

        result = await svc.run(
            files=[_fd("/tmp/v4.md", version="v4", **CHAIN_KW)],
            vault_services=services,
            infer_edges=True,
        )

        assert result.edges_created.get("supersedes", 0) == 1
        assert result.edges_removed == 0
        # Original chain edges intact
        assert _E_V2_V1 in state.edges
        assert _E_V3_V2 in state.edges
        # New head linked
        v4_id = next(d.id for d in state.docs.values() if d.version_label == "v4")
        adjacency = {(e.source_id, e.target_id) for e in state.edges.values()}
        assert (v4_id, V3_ID) in adjacency
        assert state.docs[V3_ID].lifecycle_status == "archived"
        assert state.docs[v4_id].lifecycle_status == "active"

    @pytest.mark.asyncio
    async def test_cr_005_singleton_arrival_no_chain_no_edges(self):
        """Empty vault + single v1 = no supersedes edges, no errors."""
        services, state = _make_chain_services([], [])
        svc = BatchIngestService()

        result = await svc.run(
            files=[_fd("/tmp/v1.md", version="v1", **CHAIN_KW)],
            vault_services=services,
            infer_edges=True,
        )

        assert result.edges_created.get("supersedes", 0) == 0
        assert result.edges_staged.get("supersedes", 0) == 0
        assert result.edges_removed == 0
        assert state.removed_edge_ids == []

    # ------------------------------------------------------------------
    # Typed rationale_kind discriminator on edges
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_chain_repair_link_request_stamps_version_chain(self):
        """T5. Version-chain inference passes rationale_kind=VERSION_CHAIN
        on the LinkRequest it submits to _create_edge. Without this,
        the auto-inferred edge lands with the default 'manual' kind, the
        rationale-text prefix becomes load-bearing again, and the index
        added by cannot be used to identify version-chain edges.
        """
        V1_ID = "dddddddd_v1"
        V3_ID = "dddddddd_v3"
        v1 = _make_document(
            V1_ID,
            title="Claim-Set",
            project="EXAMPLE",
            doc_type="design_spec",
            version_label="v1",
            tags=["PV06"],
            lifecycle_status="archived",
        )
        v3 = _make_document(
            V3_ID,
            title="Claim-Set",
            project="EXAMPLE",
            doc_type="design_spec",
            version_label="v3",
            tags=["PV06"],
            lifecycle_status="active",
        )
        existing_edge = _make_edge(
            _E_V3_V1,
            V3_ID,
            V1_ID,
            rationale=f"{VERSION_CHAIN_RATIONALE_PREFIX} v3 supersedes v1 (title: Claim-Set)",
            rationale_kind=RationaleKind.VERSION_CHAIN,
        )
        services, state = _make_chain_services([v1, v3], [existing_edge])
        svc = BatchIngestService()

        await svc.run(
            files=[_fd("/tmp/v2.md", version="v2", **CHAIN_KW)],
            vault_services=services,
            infer_edges=True,
        )

        supersedes_requests = [
            r for r in state.added_link_requests if r.edge_type == EdgeType.SUPERSEDES
        ]
        assert supersedes_requests, "chain repair should issue at least one supersedes link"
        for req in supersedes_requests:
            assert req.rationale_kind == RationaleKind.VERSION_CHAIN, (
                f"chain-repair LinkRequest must carry rationale_kind=version_chain; "
                f"got {req.rationale_kind!r} (rationale={req.rationale!r})"
            )

    @pytest.mark.asyncio
    async def test_filename_code_match_stamps_prefix_in_evidence(self):
        """T6. The filename-code-match inference path stamps the
        ``[filename_code_match]`` rationale prefix on the staging edge's
        inference_evidence. When the staging edge is later promoted via
        confirm_staging_edge, the prefix is copied into the production
        edge's rationale and the helper derives rationale_kind=
        filename_code_match. Today the inference evidence is plain text,
        breaking the ticket's claim that this is an existing tier-2
        prefix and leaving the backfill mapping with no rows to match.
        """
        WF_ID = "eeeeeeee_workflow"
        CT_ID = "eeeeeeee_content"
        workflow_doc = _make_document(
            WF_ID,
            title="Checklist",
            project="EXAMPLE",
            doc_type="checklist",
            tags=["PV06"],
            lifecycle_status="active",
        )
        content_doc = _make_document(
            CT_ID,
            title="Claim-Set",
            project="EXAMPLE",
            doc_type="design_spec",
            tags=["PV06"],
            lifecycle_status="active",
        )
        services, state = _make_chain_services([workflow_doc, content_doc], [])
        svc = BatchIngestService()

        await svc.run(
            files=[
                _fd(
                    "/tmp/new_checklist.md",
                    title="New-Checklist",
                    project="EXAMPLE",
                    codes=["PV06"],
                    doc_type="checklist",
                )
            ],
            vault_services=services,
            infer_edges=True,
        )

        staged_covers = [
            s for s in getattr(state, "staged_edges", []) if s.edge_type == EdgeType.COVERS
        ]
        assert staged_covers, (
            "filename_code_match should produce at least one tier-2 covers staging edge"
        )
        for staging in staged_covers:
            assert staging.inference_evidence.startswith(FILENAME_CODE_MATCH_RATIONALE_PREFIX), (
                f"staging.inference_evidence must start with "
                f"{FILENAME_CODE_MATCH_RATIONALE_PREFIX!r}; got {staging.inference_evidence!r}"
            )

    @pytest.mark.asyncio
    async def test_provenance_gate_uses_rationale_kind_column(self):
        """T8. The CAS-ADR-019 provenance gate downgrades chain repair to
        Tier 2 when an existing supersedes edge has rationale_kind=manual,
        even if its rationale text happens to start with
        ``[version_chain]`` (a stale rationale string from before).
        The discriminator now lives in the typed column, not the prefix.
        """
        V1_ID = "ffffffff_v1"
        V3_ID = "ffffffff_v3"
        v1 = _make_document(
            V1_ID,
            title="Claim-Set",
            project="EXAMPLE",
            doc_type="design_spec",
            version_label="v1",
            tags=["PV06"],
            lifecycle_status="archived",
        )
        v3 = _make_document(
            V3_ID,
            title="Claim-Set",
            project="EXAMPLE",
            doc_type="design_spec",
            version_label="v3",
            tags=["PV06"],
            lifecycle_status="active",
        )
        # Stale rationale carries the prefix but the typed kind is MANUAL
        # (e.g., a hand-curated edge written before that someone
        # included the prefix in by convention but never set the column).
        existing_edge = _make_edge(
            _E_V3_V1,
            V3_ID,
            V1_ID,
            rationale=f"{VERSION_CHAIN_RATIONALE_PREFIX} Clif curated this manually",
            rationale_kind=RationaleKind.MANUAL,
        )
        services, state = _make_chain_services([v1, v3], [existing_edge])
        svc = BatchIngestService()

        result = await svc.run(
            files=[_fd("/tmp/v2.md", version="v2", **CHAIN_KW)],
            vault_services=services,
            infer_edges=True,
        )

        assert result.edges_created.get("supersedes", 0) == 0, (
            "manual-kind edge should block Tier 1 repair regardless of rationale text"
        )
        assert result.edges_removed == 0
        assert _E_V3_V1 in state.edges, "manual edge must not be removed"
        assert state.removed_edge_ids == []
        # Repair lands in Tier 2 staging
        assert result.edges_staged.get("supersedes", 0) >= 2

    @pytest.mark.asyncio
    async def test_cr_005_repair_withheld_when_replacement_add_is_gated(self):
        """A repair whose replacement add is gated leaves the chain intact.

        Chain repair removes an edge and adds the ones that replace it.
        When a replacement add cannot be created -- its target holds a state
        the vault's table neither supersedes from nor lands a supersession
        in -- executing the removal anyway would sever the chain: the edge
        that existed is gone, its target is orphaned, and the only signal is
        a warning. The group is settled as a unit instead, so the batch never
        leaves the graph holding fewer supersedes edges than it found.

        End-to-end through ``plan_batch_edges`` rather than against
        ``resolve_and_execute`` directly, because the planner is what
        produces the removal-plus-gated-add shape the gate has to reason
        about: ``plan_batch_edges`` admits a non-active document into the
        candidate set via ``in_repair_scope``, which is what makes a
        ``completed`` mid-chain predecessor reachable at all.
        """
        V1_ID = "cccccccc_v1"
        V3_ID = "cccccccc_v3"
        v1 = _make_document(
            V1_ID,
            title="Claim-Set",
            project="EXAMPLE",
            doc_type="design_spec",
            version_label="v1",
            tags=["PV06"],
            # Neither supersedable nor a supersede landing state under the
            # base lifecycle, so the v2->v1 replacement add is gated.
            lifecycle_status="completed",
        )
        v3 = _make_document(
            V3_ID,
            title="Claim-Set",
            project="EXAMPLE",
            doc_type="design_spec",
            version_label="v3",
            tags=["PV06"],
            lifecycle_status="active",
        )
        existing_edge = _make_edge(
            _E_V3_V1,
            V3_ID,
            V1_ID,
            rationale=f"{VERSION_CHAIN_RATIONALE_PREFIX} v3 supersedes v1 (title: Claim-Set)",
            rationale_kind=RationaleKind.VERSION_CHAIN,
        )
        services, state = _make_chain_services([v1, v3], [existing_edge])
        svc = BatchIngestService()

        before = {(e.source_id, e.target_id) for e in state.edges.values()}

        result = await svc.run(
            files=[_fd("/tmp/v2.md", version="v2", **CHAIN_KW)],
            vault_services=services,
            infer_edges=True,
        )

        after = {(e.source_id, e.target_id) for e in state.edges.values()}

        # The invariant: the repair took nothing away.
        assert before <= after, f"repair severed edges: {before - after}"
        assert _E_V3_V1 in state.edges
        assert state.removed_edge_ids == []
        assert result.edges_removed == 0

        # The group is settled as a unit, so the sound half of the repair
        # (v3->v2) is withheld too: keeping v3->v1 and adding v3->v2 would
        # leave v3 with two outgoing supersedes edges.
        assert result.edges_created.get("supersedes", 0) == 0

        # Both facts reach the caller: why the add was refused, and that a
        # removal was withheld as a result.
        reasons = {w["reason"] for w in result.edge_warnings}
        assert "supersede_target_not_transitionable" in reasons
        assert "chain_repair_withheld" in reasons

        # v1 is untouched -- not transitioned, and still linked.
        assert state.docs[V1_ID].lifecycle_status == "completed"

    @pytest.mark.asyncio
    async def test_cr_006_repair_withheld_when_replacement_add_fails_on_write(self):
        """A repair whose replacement fails at write time leaves the chain intact.

        The settlement-refusal half of this invariant is CR-005. This is
        the other half: an add that settles cleanly -- its target is
        active and supersedable -- and then fails when the commit runs.
        The adds are written before the removals they replace, and the
        failing group's removals are withheld, so the graph ends holding
        no fewer supersedes edges than it started with.
        """
        V1_ID = "cccccccc_v1"
        V3_ID = "cccccccc_v3"
        v1 = _make_document(
            V1_ID,
            title="Claim-Set",
            project="EXAMPLE",
            doc_type="design_spec",
            version_label="v1",
            tags=["PV06"],
            # Active and supersedable: the v2->v1 replacement settles
            # cleanly, unlike CR-005's gated target. Only the write fails.
            lifecycle_status="active",
        )
        v3 = _make_document(
            V3_ID,
            title="Claim-Set",
            project="EXAMPLE",
            doc_type="design_spec",
            version_label="v3",
            tags=["PV06"],
            lifecycle_status="active",
        )
        existing_edge = _make_edge(
            _E_V3_V1,
            V3_ID,
            V1_ID,
            rationale=f"{VERSION_CHAIN_RATIONALE_PREFIX} v3 supersedes v1 (title: Claim-Set)",
            rationale_kind=RationaleKind.VERSION_CHAIN,
        )
        services, state = _make_chain_services([v1, v3], [existing_edge])

        real_atomic = state.supersede_atomic

        async def _failing_atomic(predecessor_id, predecessor_updates, edge):
            if predecessor_id == V1_ID:
                raise RuntimeError("edge write failed")
            return await real_atomic(predecessor_id, predecessor_updates, edge)

        services.graph_store.supersede_atomic = AsyncMock(side_effect=_failing_atomic)
        svc = BatchIngestService()

        before = {(e.source_id, e.target_id) for e in state.edges.values()}

        result = await svc.run(
            files=[_fd("/tmp/v2.md", version="v2", **CHAIN_KW)],
            vault_services=services,
            infer_edges=True,
        )

        after = {(e.source_id, e.target_id) for e in state.edges.values()}

        # The invariant: the repair took nothing away.
        assert before <= after, f"repair severed edges: {before - after}"
        assert _E_V3_V1 in state.edges
        assert state.removed_edge_ids == []
        assert result.edges_removed == 0

        # Both facts reach the caller: the write failure itself, and that
        # a removal was withheld as a result. The first is also the
        # control that the injected failure actually fired.
        reasons = {w["reason"] for w in result.edge_warnings}
        assert "edge_creation_failed" in reasons
        assert "chain_repair_withheld" in reasons

        # v1 keeps its state: the failed commit landed neither half.
        assert state.docs[V1_ID].lifecycle_status == "active"


class TestChunkLifecycleSync:
    """TEST-BIS-021: supersession syncs predecessor chunks (see spec doc)."""

    @pytest.mark.asyncio
    async def test_supersede_batch_syncs_predecessor_chunks(self):
        """A batch-inferred supersession moves the predecessor's chunks too.

        End-to-end through ``BatchIngestService.run`` rather than against
        ``resolve_and_execute`` directly: the wiring under test is that the
        service threads its vault's content store into edge execution at
        all -- a sync implemented but never handed a store would pass every
        unit test and still leave production chunks stale.
        """
        V1_ID = "cccccccc_v1"
        v1 = _make_document(
            V1_ID,
            title="Claim-Set",
            project="EXAMPLE",
            doc_type="design_spec",
            version_label="v1",
            tags=["PV06"],
            lifecycle_status="active",
        )
        services, state = _make_chain_services([v1], [])
        await services.content_store.index_chunks(
            V1_ID,
            [
                Chunk(
                    document_id=V1_ID,
                    heading_path="Claims",
                    content="claim set details",
                    chunk_index=0,
                    lifecycle_status="active",
                ),
                Chunk(
                    document_id=V1_ID,
                    heading_path="Notes",
                    content="claim revision notes",
                    chunk_index=1,
                    lifecycle_status="active",
                ),
            ],
        )
        # Positive control: the predecessor's chunks answer an
        # active-filtered search before the batch runs.
        before = await services.content_store.search_bm25(
            "claim", filters={"lifecycle_status": "active"}
        )
        assert {r.document_id for r in before} == {V1_ID}

        svc = BatchIngestService()
        result = await svc.run(
            files=[_fd("/tmp/v2.md", version="v2", **CHAIN_KW)],
            vault_services=services,
            infer_edges=True,
        )

        assert result.edge_warnings == []
        assert result.edges_created.get("supersedes", 0) == 1
        # Document and chunks agree on the landing state.
        assert state.docs[V1_ID].lifecycle_status == "archived"
        chunks = await services.content_store.get_chunks_by_heading_prefix(V1_ID, "Claims")
        assert chunks and all(c.lifecycle_status == "archived" for c in chunks)
        after = await services.content_store.search_bm25(
            "claim", filters={"lifecycle_status": "active"}
        )
        assert after == []
