"""Ingestion behavior tests for CAS-ADR-021 (TEST-AD021-001 through 009).

Validates the runtime behavior change introduced in Chunk 2 of the
ADR-021 implementation:

- needs_review (per-call, default False) gates filename inference and
  the metadata_confirmed=False setter. Default ingests skip filename
  parse entirely; needs_review=True restores prior behavior end-to-end.
- Caller-supplied metadata is authoritative per-field.
- Chain inheritance fills doc_type, project, and authority_scope from
  the predecessor when supersedes_document_id is set, the field is unset
  on the new document, and the caller did not supply it. Predecessor
  None values do not propagate.

Schema-level coverage of the new field surface lives in
tests/sage/test_ad021_schemas.py. Filename inference behavior under
needs_review=True is regressioned by tests/sage/test_ingestion_metadata_extraction.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.config import VaultConfig
from sage.models.enums import SourceType
from sage.models.schemas import IngestRequest
from sage.services.ingestion import IngestionService
from sage.services.lifecycle import LifecycleService
from sage.source_adapters.markdown_adapter import MarkdownAdapter

from tests.sage.test_ingestion_metadata_extraction import (
    _pim_metadata_extraction,
    _pim_vault_config_dict,
    _write_md,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pim_config(tmp_vault_dir):
    """PIM-style VaultConfig with filename_extraction enabled."""
    return VaultConfig.model_validate(_pim_vault_config_dict(tmp_vault_dir))


@pytest.fixture
def pim_config_review_required(tmp_vault_dir):
    """Same as pim_config but with metadata_extraction.review_required=True.

    Used to prove that the vault-level review_required flag is no longer
    consulted at ingest time (TEST-AD021-003): the config field is now
    vestigial and behavior is driven by request.needs_review.
    """
    return VaultConfig.model_validate(
        _pim_vault_config_dict(tmp_vault_dir, review_required=True)
    )


def _build_ingestion_service(config, graph_store, lock_manager):
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
    return _build_ingestion_service(pim_config, graph_store, lock_manager)


@pytest.fixture
def pim_ingestion_service_review_required(
    pim_config_review_required, graph_store, lock_manager
):
    return _build_ingestion_service(
        pim_config_review_required, graph_store, lock_manager
    )


# ---------------------------------------------------------------------------
# TEST-AD021-001: needs_review default False commits caller values; no
# filename inference; doc_type defaults to "misc".
# ---------------------------------------------------------------------------


async def test_ad021_001_default_skips_filename_inference(
    tmp_vault_dir, pim_ingestion_service
):
    # Filename matches the vault's pattern -- under the legacy ME path
    # this would populate project="PIM", document_date="2026-03-09",
    # version_label="v6.0", tags=["PV06"], doc_type="patent_draft".
    _write_md(
        tmp_vault_dir,
        "patents/2026-03-09_PIM_PV06_Claim-Set_v6.md",
        body="# A Heading\n\nBody.\n",
    )

    request = IngestRequest(
        source="patents/2026-03-09_PIM_PV06_Claim-Set_v6.md",
        adapter=SourceType.MARKDOWN,
        metadata={"title": "Caller Title"},
    )
    result = await pim_ingestion_service.ingest(request)
    doc = result.document

    # Caller-supplied title wins
    assert doc.title == "Caller Title"
    # Filename inference did NOT run
    assert doc.project is None
    assert doc.version_label is None
    assert doc.tags == []
    # Vault default applies because filename inference is skipped
    assert doc.doc_type == "misc"
    # Caller-authoritative ingest commits confirmed
    assert doc.metadata_confirmed is True


# ---------------------------------------------------------------------------
# TEST-AD021-002: needs_review=True preserves filename inference + the
# metadata_confirmed=False setter end-to-end.
# ---------------------------------------------------------------------------


async def test_ad021_002_needs_review_true_runs_filename_inference(
    tmp_vault_dir, pim_ingestion_service
):
    _write_md(
        tmp_vault_dir,
        "patents/2026-03-09_PIM_PV06_Claim-Set_v6.md",
        body="# A Heading\n\nBody.\n",
    )

    request = IngestRequest(
        source="patents/2026-03-09_PIM_PV06_Claim-Set_v6.md",
        adapter=SourceType.MARKDOWN,
        needs_review=True,
    )
    result = await pim_ingestion_service.ingest(request)
    doc = result.document

    # Filename inference ran (matches TEST-SAGE-ME-001 expectations)
    assert doc.title == "Claim-Set"
    assert doc.document_date == "2026-03-09"
    assert doc.project == "PIM"
    assert doc.tags == ["PV06"]
    assert doc.version_label == "v6.0"
    assert doc.doc_type == "patent_draft"
    # Held in review queue
    assert doc.metadata_confirmed is False


# ---------------------------------------------------------------------------
# TEST-AD021-003: needs_review=False excludes a document from the review
# queue even when the vault config carries the now-vestigial
# metadata_extraction.review_required=true flag.
# ---------------------------------------------------------------------------


async def test_ad021_003_vault_review_required_no_longer_consulted(
    tmp_vault_dir, pim_ingestion_service_review_required
):
    _write_md(
        tmp_vault_dir,
        "patents/2026-03-09_PIM_PV06_A_v1.md",
        body="# A\n\nBody.\n",
    )

    request = IngestRequest(
        source="patents/2026-03-09_PIM_PV06_A_v1.md",
        adapter=SourceType.MARKDOWN,
        # needs_review left at default False
    )
    result = await pim_ingestion_service_review_required.ingest(request)

    # Vault-level review_required=True must NOT push the doc into the
    # review queue under ADR-021. The flag is read but ignored.
    assert result.document.metadata_confirmed is True


# ---------------------------------------------------------------------------
# TEST-AD021-004: chain inheritance fills all three trio fields when the
# caller omits them and the predecessor has values.
# ---------------------------------------------------------------------------


async def test_ad021_004_chain_inheritance_fills_full_trio(
    tmp_vault_dir, pim_ingestion_service
):
    _write_md(tmp_vault_dir, "v1.md", body="# V1\n\nOriginal.")
    _write_md(tmp_vault_dir, "v2.md", body="# V2\n\nRevised.")

    v1 = await pim_ingestion_service.ingest(
        IngestRequest(
            source="v1.md",
            adapter=SourceType.MARKDOWN,
            metadata={
                "doc_type": "checklist",
                "project": "PIM",
                "authority_scope": "domain",
            },
        )
    )
    assert v1.document.doc_type == "checklist"
    assert v1.document.project == "PIM"
    assert v1.document.authority_scope == "domain"

    v2 = await pim_ingestion_service.ingest(
        IngestRequest(
            source="v2.md",
            adapter=SourceType.MARKDOWN,
            supersedes_document_id=v1.document.id,
        )
    )
    assert v2.document.doc_type == "checklist"
    assert v2.document.project == "PIM"
    assert v2.document.authority_scope == "domain"


# ---------------------------------------------------------------------------
# TEST-AD021-005: per-field independence. Caller supplies doc_type only;
# project and authority_scope inherit from the predecessor.
# ---------------------------------------------------------------------------


async def test_ad021_005_per_field_inheritance_caller_overrides_one(
    tmp_vault_dir, pim_ingestion_service
):
    _write_md(tmp_vault_dir, "v1.md", body="# V1\n\nOriginal.")
    _write_md(tmp_vault_dir, "v2.md", body="# V2\n\nRevised.")

    v1 = await pim_ingestion_service.ingest(
        IngestRequest(
            source="v1.md",
            adapter=SourceType.MARKDOWN,
            metadata={
                "doc_type": "checklist",
                "project": "PIM",
                "authority_scope": "domain",
            },
        )
    )

    v2 = await pim_ingestion_service.ingest(
        IngestRequest(
            source="v2.md",
            adapter=SourceType.MARKDOWN,
            supersedes_document_id=v1.document.id,
            metadata={"doc_type": "work_plan"},
        )
    )
    # Caller wins on the supplied field
    assert v2.document.doc_type == "work_plan"
    # Other two trio fields inherit from predecessor
    assert v2.document.project == "PIM"
    assert v2.document.authority_scope == "domain"


# ---------------------------------------------------------------------------
# TEST-AD021-006: caller supplies all three trio fields; nothing is
# inherited.
# ---------------------------------------------------------------------------


async def test_ad021_006_caller_supplies_all_three_no_inheritance(
    tmp_vault_dir, pim_ingestion_service
):
    _write_md(tmp_vault_dir, "v1.md", body="# V1\n\nOriginal.")
    _write_md(tmp_vault_dir, "v2.md", body="# V2\n\nRevised.")

    v1 = await pim_ingestion_service.ingest(
        IngestRequest(
            source="v1.md",
            adapter=SourceType.MARKDOWN,
            metadata={
                "doc_type": "checklist",
                "project": "PIM",
                "authority_scope": "domain",
            },
        )
    )

    v2 = await pim_ingestion_service.ingest(
        IngestRequest(
            source="v2.md",
            adapter=SourceType.MARKDOWN,
            supersedes_document_id=v1.document.id,
            metadata={
                "doc_type": "work_plan",
                "project": "OTHER",
                "authority_scope": "narrow",
            },
        )
    )
    assert v2.document.doc_type == "work_plan"
    assert v2.document.project == "OTHER"
    assert v2.document.authority_scope == "narrow"


# ---------------------------------------------------------------------------
# TEST-AD021-007: no supersedes_document_id -> no inheritance. Trio
# fields fall through to the doc_type=misc default and Nones for
# project/authority_scope.
# ---------------------------------------------------------------------------


async def test_ad021_007_no_predecessor_no_inheritance(
    tmp_vault_dir, pim_ingestion_service
):
    _write_md(tmp_vault_dir, "v1.md", body="# V1\n\nOriginal.")
    _write_md(tmp_vault_dir, "standalone.md", body="# Standalone\n\nBody.")

    # Seed a document that COULD be a predecessor, then ingest a
    # standalone doc without supersedes_document_id and confirm nothing
    # leaks across.
    await pim_ingestion_service.ingest(
        IngestRequest(
            source="v1.md",
            adapter=SourceType.MARKDOWN,
            metadata={
                "doc_type": "checklist",
                "project": "PIM",
                "authority_scope": "domain",
            },
        )
    )

    result = await pim_ingestion_service.ingest(
        IngestRequest(
            source="standalone.md",
            adapter=SourceType.MARKDOWN,
        )
    )
    assert result.document.doc_type == "misc"
    assert result.document.project is None
    assert result.document.authority_scope is None


# ---------------------------------------------------------------------------
# TEST-AD021-008: predecessor None for a trio field -> field stays unset
# on the new doc (no spurious inheritance of None).
# ---------------------------------------------------------------------------


async def test_ad021_008_predecessor_none_does_not_propagate(
    tmp_vault_dir, pim_ingestion_service
):
    _write_md(tmp_vault_dir, "v1.md", body="# V1\n\nOriginal.")
    _write_md(tmp_vault_dir, "v2.md", body="# V2\n\nRevised.")

    # Predecessor sets only doc_type; project and authority_scope are None.
    v1 = await pim_ingestion_service.ingest(
        IngestRequest(
            source="v1.md",
            adapter=SourceType.MARKDOWN,
            metadata={"doc_type": "checklist"},
        )
    )
    assert v1.document.doc_type == "checklist"
    assert v1.document.project is None
    assert v1.document.authority_scope is None

    v2 = await pim_ingestion_service.ingest(
        IngestRequest(
            source="v2.md",
            adapter=SourceType.MARKDOWN,
            supersedes_document_id=v1.document.id,
        )
    )
    # doc_type inherits because predecessor has a non-None value
    assert v2.document.doc_type == "checklist"
    # project / authority_scope stay None (not coerced from predecessor None)
    assert v2.document.project is None
    assert v2.document.authority_scope is None


# ---------------------------------------------------------------------------
# TEST-AD021-009: under needs_review=True, chain inheritance fires for
# trio fields the filename parse left unset. Demonstrates the layered
# precedence: caller > filename parse > chain inherit > vault default.
# ---------------------------------------------------------------------------


async def test_ad021_009_chain_inherit_fills_after_filename_parse(
    tmp_vault_dir, pim_ingestion_service
):
    # v1 is ingested under needs_review=True so filename inference runs
    # and populates project/doc_type. authority_scope is set by the caller.
    _write_md(
        tmp_vault_dir,
        "patents/2026-03-09_PIM_PV06_A_v1.md",
        body="# A\n\nBody.\n",
    )
    # v2 filename has no project/code/version segments, so the parser
    # cannot fill project, doc_type, or version_label. Chain inheritance
    # must fill the gaps.
    _write_md(
        tmp_vault_dir,
        "patents/Standalone-Note.md",
        body="# Standalone Note\n\nBody.\n",
    )

    v1 = await pim_ingestion_service.ingest(
        IngestRequest(
            source="patents/2026-03-09_PIM_PV06_A_v1.md",
            adapter=SourceType.MARKDOWN,
            needs_review=True,
            metadata={"authority_scope": "domain"},
        )
    )
    # Sanity: filename inference + caller metadata seeded the predecessor.
    assert v1.document.project == "PIM"
    assert v1.document.doc_type == "patent_draft"
    assert v1.document.authority_scope == "domain"

    v2 = await pim_ingestion_service.ingest(
        IngestRequest(
            source="patents/Standalone-Note.md",
            adapter=SourceType.MARKDOWN,
            supersedes_document_id=v1.document.id,
            needs_review=True,
        )
    )
    # The bare filename parses cleanly but yields no trio values; chain
    # inheritance must fill in from the predecessor.
    assert v2.document.project == "PIM"
    assert v2.document.doc_type == "patent_draft"
    assert v2.document.authority_scope == "domain"
