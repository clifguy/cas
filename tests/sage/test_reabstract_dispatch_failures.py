"""Dispatch failures remain distinct from background generation failures."""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from sage.api.errors import (
    DocumentNotFoundError,
    NoProjectionError,
    ReabstractDocumentAlreadyInFlightError,
)
from sage.maintenance.reabstract_bulk import run_sweep
from sage.models.enums import PipelineStatus, ReabstractOutcome
from sage.models.schemas import ReabstractProgressEvent, ReabstractSummaryEvent
from scripts import reabstract_deferred as script
from tests.sage.test_lifecycle import _id
from tests.sage.test_reabstract_deferred_service import (
    _build_maintenance,
    _make_skipped_doc,
    _seed_doc_with_chunks,
)


@pytest.fixture(params=["projection", "missing", "busy", "unexpected"])
def dispatch_error(request: pytest.FixtureRequest) -> Exception:
    doc_id = _id("dispatch_rejected")
    return {
        "projection": lambda: NoProjectionError(doc_id),
        "missing": lambda: DocumentNotFoundError(doc_id),
        "busy": lambda: ReabstractDocumentAlreadyInFlightError(doc_id, datetime.now(timezone.utc)),
        "unexpected": lambda: RuntimeError("status write refused"),
    }[request.param]()


@pytest.mark.parametrize("surface", ["report", "events", "bulk", "script"])
async def test_dispatch_failure_classification_and_continuation(
    surface,
    dispatch_error,
    minimal_config,
    monkeypatch,
    request,
    capsys,
) -> None:
    rejected = _make_skipped_doc(_id("dispatch_rejected"))
    accepted = _make_skipped_doc(_id("dispatch_accepted"))
    settled = accepted.model_copy(update={"pipeline_status": PipelineStatus.ABSTRACTION_COMPLETE})
    graph = SimpleNamespace(
        get_document=AsyncMock(return_value=settled),
    )
    ingestion = SimpleNamespace(reabstract=AsyncMock(side_effect=[dispatch_error, {}]))
    if surface != "bulk":
        graph.list_all_documents = AsyncMock(return_value=[rejected, accepted])
    if surface == "script":
        graph.close = AsyncMock()
        config_path = request.getfixturevalue("tmp_path") / "config.yaml"
        config_path.touch()
        monkeypatch.setattr(script, "config_path_for_vault", lambda _vault: config_path)
        monkeypatch.setattr(script, "load_vault_config", lambda _path: minimal_config)
        monkeypatch.setattr(
            script,
            "initialize_services",
            AsyncMock(
                return_value=SimpleNamespace(
                    graph_store=graph,
                    ingestion_service=ingestion,
                )
            ),
        )
        result = await script.run(
            minimal_config.vault.id, include_all_statuses=False, include_pdf=False, poll_interval=0
        )
        output = capsys.readouterr().out
        assert result == 1
        assert "dispatch_failed" in output
        assert repr(dispatch_error) in output
        assert rejected.id in output
        assert "1/2 succeeded" in output
        graph.close.assert_awaited_once()
    else:
        if surface == "bulk":
            report = await run_sweep(
                graph_store=graph,
                ingestion_service=ingestion,
                vault_id=minimal_config.vault.id,
                worklist=[rejected, accepted],
                poll_interval=0,
            )
        else:
            maintenance = _build_maintenance(
                graph_store=graph,
                config=minimal_config,
                content_store=None,
                ingestion_service=ingestion,
            )
            if surface == "report":
                report = await maintenance.reabstract_deferred()
            else:
                events = [event async for event in maintenance.reabstract_deferred_events()]
                progress = [event for event in events if isinstance(event, ReabstractProgressEvent)]
                assert [event.status for event in progress] == [
                    "started",
                    "failed",
                    "started",
                    "completed",
                ]
                assert progress[1].outcome == "dispatch_failed"
                assert progress[1].error == f"dispatch failed: {dispatch_error!r}"
                assert progress[1].elapsed_seconds is not None
                assert progress[1].elapsed_seconds >= 0
                assert progress[1].processed == 1
                assert progress[3].processed == 2
                assert progress[3].outcome == "success"
                assert isinstance(events[-1], ReabstractSummaryEvent)
                report = events[-1]
        assert (report.failed_count, report.reabstracted_count, report.skipped_pdf_count) == (
            1,
            1,
            0,
        )
        assert [(entry.document_id, entry.outcome) for entry in report.entries] == [
            (rejected.id, "dispatch_failed"),
            (accepted.id, "success"),
        ]
        entry = report.entries[0]
        assert entry.error_message == f"dispatch failed: {dispatch_error!r}"
        assert entry.elapsed_seconds is not None and entry.elapsed_seconds >= 0
    assert [call.args[0] for call in ingestion.reabstract.await_args_list] == [
        rejected.id,
        accepted.id,
    ]
    # A rejected dispatch must never enter the polling path.
    graph.get_document.assert_awaited_once_with(accepted.id)
    if surface != "bulk":
        graph.list_all_documents.assert_awaited_once()


async def test_missing_chunks_rejected_before_provider(
    graph_store,
    stub_content_store,
    ingestion_service,
    minimal_config,
    monkeypatch,
) -> None:
    doc = _make_skipped_doc(_id("without_projection"))
    await graph_store.insert_document(doc)
    sibling = _make_skipped_doc(_id("with_projection"))
    await _seed_doc_with_chunks(graph_store, stub_content_store, sibling)
    provider = AsyncMock(return_value="abstract for sibling")
    monkeypatch.setattr(ingestion_service._abstraction, "generate_abstract", provider)
    maintenance = _build_maintenance(
        graph_store=graph_store,
        config=minimal_config,
        content_store=stub_content_store,
        ingestion_service=ingestion_service,
    )
    report = await maintenance.reabstract_deferred()
    assert report.failed_count == 1
    assert report.reabstracted_count == 1
    entries = {entry.document_id: entry for entry in report.entries}
    assert entries[doc.id].outcome == "dispatch_failed"
    assert "NoProjectionError" in entries[doc.id].error_message
    assert entries[sibling.id].outcome == "success"
    provider.assert_awaited_once()


def test_reabstract_outcome_contract_parity() -> None:
    path = Path(__file__).resolve().parents[2] / "docs/fs/sage/sage_core_api.openapi.yaml"
    schema = yaml.safe_load(path.read_text())["components"]["schemas"]["ReabstractOutcome"]
    assert set(schema["enum"]) == {outcome.value for outcome in ReabstractOutcome}
    assert "dispatch_failed" in schema["enum"]
