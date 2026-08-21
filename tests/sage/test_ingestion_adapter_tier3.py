"""Ingestion threading of adapter-emitted tier3 metadata.

The markdown source adapter extracts ``adr_id`` from ``cas-adr-NNN_*``
filenames and surfaces it via ``ProjectionResult.metadata["adapter_tier3_metadata"]``.
The ingestion service merges this with caller-supplied ``tier3_metadata``
(caller wins, per CAS-ADR-021's "metadata inference is caller-authoritative"
contract). Validation runs against the resolved tier3 value regardless of
its source.

This module pins the ingestion-side seam. Adapter-side extraction is
covered by ``tests/sage/test_adapters.py::TestMarkdownAdapterADRTier3Extraction``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sage.api.errors import Tier3SchemaViolationError
from sage.config import VaultConfig
from sage.models.enums import SourceType
from sage.models.schemas import IngestRequest
from sage.services.ingestion import IngestionService
from sage.source_adapters.markdown_adapter import MarkdownAdapter


def _adr_vault_config_dict(tmp_vault_dir: Path) -> dict:
    """Minimal vault config that mirrors the cas vault's ADR schema shape.

    Declares the ``adr`` doc_type with a ``metadata_schema`` for ``adr_id``
    (pattern ``^\\d{3}$``) and ``unique_keys: [adr_id]`` per CAS-ADR-031.
    No filename_extraction block — this isolates the ADR tier3 path from
    the EXAMPLE-style filename parser exercised in
    ``test_ingestion_metadata_extraction.py``.
    """
    return {
        "vault": {
            "id": "test_adr_tier3_vault",
            "name": "Test ADR Tier3 Vault",
            "owner": "testuser",
            "storage_root": str(tmp_vault_dir / "sources"),
            "brain_root": str(tmp_vault_dir / "brain"),
            "visibility": "personal",
        },
        "document_types": {
            "doc_types": [
                {
                    "value": "adr",
                    "label": "Architectural Decision Record",
                    "metadata_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "adr_id": {"type": "string", "pattern": r"^\d{3}$"},
                        },
                    },
                    "unique_keys": ["adr_id"],
                },
                {"value": "misc", "label": "Miscellaneous"},
            ],
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
                {"from_state": "active", "action": "archive", "to_state": "archived"},
                {"from_state": "completed", "action": "archive", "to_state": "archived"},
                {"from_state": "archived", "action": "reactivate", "to_state": "active"},
            ],
        },
        "metadata_extraction": {},
        "edge_inference": {},
    }


@pytest.fixture
def adr_tier3_config(tmp_vault_dir):
    return VaultConfig.model_validate(_adr_vault_config_dict(tmp_vault_dir))


@pytest.fixture
def adr_tier3_ingestion_service(
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    adr_tier3_config,
):
    return IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=stub_abstraction_provider,
        config=adr_tier3_config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
    )


def _write_adr_md(tmp_vault_dir: Path, relative_path: str) -> Path:
    full_path = tmp_vault_dir / "sources" / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text("# ADR Title\n\nBody.\n")
    return full_path


async def test_e1_adapter_tier3_threads_to_document_on_default_ingest(
    tmp_vault_dir, adr_tier3_ingestion_service
):
    """``cas-adr-NNN_*`` ingest populates ``tier3_metadata.adr_id`` without caller help.

    Anti-coincidence: caller does NOT supply ``tier3_metadata`` and
    ``needs_review=False`` so the filename parser does not run. The
    ``adr_id`` value must come exclusively from the markdown adapter's
    extraction, threaded through the ingestion service into validation
    and persistence. A bug that ignored ``ProjectionResult.metadata
    ["adapter_tier3_metadata"]`` would land the document with
    ``tier3_metadata=None``.
    """
    _write_adr_md(tmp_vault_dir, "cas-adr-073_New_ADR.md")

    request = IngestRequest(
        source="cas-adr-073_New_ADR.md",
        source_type=SourceType.MARKDOWN,
        metadata={"doc_type": "adr"},
        needs_review=False,
        tier3_metadata=None,
    )
    result = await adr_tier3_ingestion_service.ingest(request)

    assert result.document.tier3_metadata == {"adr_id": "073"}, (
        f"Expected tier3_metadata={{'adr_id': '073'}} populated from the "
        f"adapter's filename extraction. Got "
        f"{result.document.tier3_metadata!r}. None here would mean the "
        f"ingestion service ignored ProjectionResult.metadata"
        f"['adapter_tier3_metadata']."
    )


async def test_e2_caller_tier3_overrides_adapter_extraction(
    tmp_vault_dir, adr_tier3_ingestion_service
):
    """Caller-supplied ``tier3_metadata`` wins over adapter extraction.

    Anti-coincidence: the caller's adr_id (``"074"``) differs from the
    natural filename extraction (``"073"``). A bug that always took the
    adapter's value (or merged both with adapter winning) would surface
    the wrong adr_id on the stored document.
    """
    _write_adr_md(tmp_vault_dir, "cas-adr-073_Caller_Override.md")

    request = IngestRequest(
        source="cas-adr-073_Caller_Override.md",
        source_type=SourceType.MARKDOWN,
        metadata={"doc_type": "adr"},
        needs_review=False,
        tier3_metadata={"adr_id": "074"},
    )
    result = await adr_tier3_ingestion_service.ingest(request)

    assert result.document.tier3_metadata == {"adr_id": "074"}, (
        f"Caller-supplied tier3_metadata must override adapter extraction "
        f"per CAS-ADR-021. Got {result.document.tier3_metadata!r}; "
        f"expected {{'adr_id': '074'}}."
    )


# ---------------------------------------------------------------------------
# E3 — Adapter-extracted tier3 must validate; rejection surfaces.
#
# Anti-coincidence guard for the asymmetric-strictness gap. Previously,
# tier3 validation only fired when the caller supplied tier3_metadata.
# Now it fires on adapter-extracted tier3 too — which means a vault
# whose resolved doc_type has no ``metadata_schema`` will 400 on any
# ``cas-adr-NNN_*`` ingest. This is the intended new behavior; the test
# below pins it so a future refactor that silently bypasses the
# validation surface fails closed.
# ---------------------------------------------------------------------------


def _no_schema_vault_config_dict(tmp_vault_dir: Path) -> dict:
    """Vault config that declares no ``metadata_schema`` on any doc_type."""
    cfg = _adr_vault_config_dict(tmp_vault_dir)
    for dt in cfg["document_types"]["doc_types"]:
        dt.pop("metadata_schema", None)
        dt.pop("unique_keys", None)
    return cfg


@pytest.fixture
def no_schema_ingestion_service(
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    tmp_vault_dir,
):
    config = VaultConfig.model_validate(_no_schema_vault_config_dict(tmp_vault_dir))
    return IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=stub_abstraction_provider,
        config=config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
    )


async def test_e3_adapter_tier3_validation_rejects_when_doctype_lacks_schema(
    tmp_vault_dir, no_schema_ingestion_service
):
    """Adapter-extracted tier3 is validated against the resolved doc_type's schema.

    Anti-coincidence: caller supplies no ``tier3_metadata`` (so the only
    source is the adapter) and the resolved doc_type carries no
    ``metadata_schema`` (strict no-loose-mode). The ingestion service
    must surface ``Tier3SchemaViolationError`` — confirming the adapter
    tier3 actually flows into the validator. A regression that bypassed
    validation for adapter-supplied tier3 would land the document with
    ``tier3_metadata={"adr_id": "099"}`` and no 400.
    """
    _write_adr_md(tmp_vault_dir, "cas-adr-099_Schema_Probe.md")

    request = IngestRequest(
        source="cas-adr-099_Schema_Probe.md",
        source_type=SourceType.MARKDOWN,
        metadata={"doc_type": "adr"},
        needs_review=False,
        tier3_metadata=None,
    )

    with pytest.raises(Tier3SchemaViolationError) as excinfo:
        await no_schema_ingestion_service.ingest(request)

    assert "no metadata_schema declared" in excinfo.value.detail["message"]
