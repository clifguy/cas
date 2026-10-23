"""Inspection binds delivered provenance and actual retained Office bytes."""

import hashlib
import json

import pytest

from sage.mcp_server import ingest_document
from sage.models.enums import PipelineStatus
from tests.helpers.pipeline_wait import await_pipeline_idle
from tests.sage.test_mcp_profile_invariance import _parse, _write_office_file
from tests.sage.test_mcp_profile_invariance import mpi_vault as _shared_mpi_vault

mpi_vault = _shared_mpi_vault


def _sha256(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize(
    "operation", ["fresh", "force", "resident_reuse", "recompute", "startup_missing_chunks"]
)
async def test_retained_office_inspection_identity_across_actual_paths(
    mpi_vault, tmp_path, monkeypatch, operation
):
    import sage.services.ingestion as ingestion

    services, config, handle = mpi_vault
    source = _write_office_file(tmp_path, "retained_identity.docx", "Retained authored heading")
    delivered = _sha256(source.read_bytes())
    packages = []
    selector = ingestion.select_discovery

    def capture(*args, **kwargs):
        package = selector(*args, **kwargs)
        packages.append(package)
        return package

    monkeypatch.setattr(ingestion, "select_discovery", capture)
    first = _parse(await ingest_document("test_vault", str(source), "docx"))
    assert "error" not in first, first
    doc_id = first["id"]
    service = services.ingestion_service
    await await_pipeline_idle(services.graph_store, doc_id, service=service)
    before = await services.graph_store.get_document(doc_id)
    if operation in {"force", "resident_reuse"}:
        selected_source = str(source)
        if operation == "resident_reuse":
            selected_source = before.source_path
            source.unlink()
            assert not source.exists()
        result = _parse(await ingest_document("test_vault", selected_source, "docx", force=True))
        assert "error" not in result, result
        assert result["id"] == doc_id
    elif operation == "recompute":
        result = await service.recompute_pipeline(doc_id)
        assert result["status"] == "recompute_pipeline_started"
    elif operation == "startup_missing_chunks":
        await services.content_store.remove_document(doc_id)
        await services.graph_store.update_document(
            doc_id,
            {
                "pipeline_status": PipelineStatus.PROJECTION_COMPLETE.value,
                "indexed_at": None,
                "semantic_abstract": None,
            },
        )
        assert await services.content_store.get_all_chunks(doc_id) == []
        assert await service.recover_incomplete_documents() == 1
    terminal = await await_pipeline_idle(services.graph_store, doc_id, service=service)
    assert terminal.pipeline_status == PipelineStatus.ABSTRACTION_COMPLETE
    actual_retained = _sha256(
        handle.retained_bytes(config.vault.storage_root, terminal.source_path)
    )
    assert terminal.source_content_hash == delivered
    assert terminal.stored_content_hash == actual_retained
    if handle.fake_client is not None:
        assert handle.fake_client.stamped_uploads > 0
        assert actual_retained != delivered, (
            "real Office retention rewrite must distinguish identities"
        )
    else:
        assert actual_retained == delivered
    inspection = terminal.processing_coverage.inspection
    assert inspection.scope == "complete"
    assert inspection.identity.source_hash == delivered
    assert "inspected_source_hash" in inspection.identity.configuration
    assert inspection.identity.configuration["inspected_source_hash"] == actual_retained
    assert inspection.selection_summary.selected_inputs > 0
    assert packages
    frozen = json.loads(packages[-1].identity.configuration_json)
    assert "inspected_source_hash" in frozen
    assert frozen["inspected_source_hash"] == actual_retained
    assert packages[-1].identity.source_hash == delivered
    assert packages[-1].identity.processing_identity() == inspection.identity
    if operation != "fresh":
        assert inspection.generation > before.processing_coverage.inspection.generation
    assert await services.content_store.get_all_chunks(doc_id)
    assert len(await services.graph_store.list_all_documents()) == 1
