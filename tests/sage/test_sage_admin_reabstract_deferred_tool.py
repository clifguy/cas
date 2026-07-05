"""MCP tool tests for recompute_deferred_vault_abstracts (CAS-ADR-029).

Exercises the boundary contract: vault_id shape validation, registry
membership check, ReabstractReport serialization on the happy path, and
the structured 409 envelope on the in-flight rejection path.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone

from sage import mcp_server
from sage.adapters.interfaces import AbstractionProvider, Chunk
from sage.adapters.stubs import StubContentStore
from sage.config import VaultConfig
from sage.mcp_init import SAGEServices, initialize_services
from sage.models.enums import ReabstractOutcome, SourceType
from sage.models.schemas import ReabstractReport
from sage.services.vault_registry import VaultRegistryService
from tests.sage.conftest import initialize_services_for_test
from tests.sage.test_lifecycle import _id
from tests.sage.test_reabstract_deferred_service import (
    _GatedAbstractionProvider,
    _make_skipped_doc,
    _SelectivelyFailingProvider,
)


@contextlib.asynccontextmanager
async def _publish_vault(
    minimal_vault_config_dict: dict,
    *,
    abstraction_provider=None,
):
    """Async context manager that initializes a vault via the production
    path and publishes it on mcp_server._vaults so the MCP tool's
    get_vault finds it.

    Yields ``(vault_id, services)``. On exit the timing thread is stopped
    and the storage is released (via ``initialize_services_for_test``).
    The graph store is the real Postgres binding under the test-harness
    stack-config pin; the content store is a stub. Allows an optional
    abstraction provider override for the gated-lock test (and otherwise
    relies on the suite-wide ``SAGE_TEST_STUB_PROVIDERS=1`` default to
    load ``StubAbstractionProvider``).
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    registry: dict[str, SAGEServices] = {}
    registry_service = VaultRegistryService(registry, initialize_services)
    overrides: dict = {"content_store_factory": lambda _brain: StubContentStore()}
    if abstraction_provider is not None:
        overrides["abstraction_provider"] = abstraction_provider
    async with initialize_services_for_test(
        config,
        registry_service=registry_service,
        **overrides,
    ) as services:
        vault_id = config.vault.id
        registry[vault_id] = services
        mcp_server._vaults[vault_id] = services
        try:
            yield vault_id, services
        finally:
            mcp_server._vaults.pop(vault_id, None)


async def _seed_one_skipped(services: SAGEServices, *, doc_id_label: str) -> str:
    doc = _make_skipped_doc(_id(doc_id_label))
    await services.graph_store.insert_document(doc)
    chunk = Chunk(
        document_id=doc.id,
        heading_path="Body",
        content="Body content for projection.",
        chunk_index=0,
    )
    await services.content_store.index_chunks(doc.id, [chunk])
    return doc.id


async def test_sage_admin_reabstract_deferred_vault_happy_path(minimal_vault_config_dict):
    """Returns a dict that round-trips through ReabstractReport with the
    seeded document's outcome recorded."""
    async with _publish_vault(minimal_vault_config_dict) as (vault_id, services):
        doc_id = await _seed_one_skipped(services, doc_id_label="tool_happy")
        try:
            result = await mcp_server.recompute_deferred_vault_abstracts(vault_id=vault_id)

            assert isinstance(result, dict)
            assert "error" not in result, f"expected report dict, got {result!r}"
            report = ReabstractReport.model_validate(result)
            assert report.vault_id == vault_id
            assert report.reabstracted_count == 1
            assert report.failed_count == 0
            assert any(entry.document_id == doc_id for entry in report.entries)
        finally:
            await asyncio.sleep(0.1)


async def test_sage_admin_reabstract_deferred_vault_returns_structured_409(
    minimal_vault_config_dict,
):
    """Concurrent invocation returns the reabstract_already_in_flight
    envelope (not a raised exception) with the start_time in the detail
    payload. MCP tools must always return a dict; any path that would
    raise out of the wrapper is the bug this test guards against.
    """
    gated = _GatedAbstractionProvider()
    async with _publish_vault(minimal_vault_config_dict, abstraction_provider=gated) as (
        vault_id,
        services,
    ):
        await _seed_one_skipped(services, doc_id_label="tool_gated")
        try:
            before = datetime.now(timezone.utc)
            task_a = asyncio.create_task(
                mcp_server.recompute_deferred_vault_abstracts(vault_id=vault_id)
            )
            await asyncio.wait_for(gated.entered.wait(), timeout=5.0)
            after = datetime.now(timezone.utc)

            result_b = await mcp_server.recompute_deferred_vault_abstracts(vault_id=vault_id)
            assert isinstance(result_b, dict)
            assert result_b.get("error") == "reabstract_already_in_flight", (
                f"expected reabstract_already_in_flight envelope, got {result_b!r}"
            )
            assert result_b["detail"]["vault_id"] == vault_id
            start_time = datetime.fromisoformat(result_b["detail"]["start_time"])
            assert before <= start_time <= after

            gated.gate.set()
            result_a = await asyncio.wait_for(task_a, timeout=5.0)
            assert "error" not in result_a, f"expected report dict, got {result_a!r}"
            report_a = ReabstractReport.model_validate(result_a)
            assert report_a.reabstracted_count == 1
        finally:
            await asyncio.sleep(0.1)


async def test_sage_admin_reabstract_deferred_vault_unknown_vault_returns_error_envelope():
    """An unregistered vault id returns the unknown_vault envelope rather
    than raising; matches the existing migrate_vault contract."""
    result = await mcp_server.recompute_deferred_vault_abstracts(vault_id="ghost")

    assert isinstance(result, dict)
    assert result.get("error") == "unknown_vault", (
        f"expected unknown_vault envelope, got {result!r}"
    )


async def test_sage_admin_reabstract_deferred_vault_aggregates_streaming_events(
    minimal_vault_config_dict,
):
    """regression guard for the MCP-layer contract.

    The MCP tool returns a dict that round-trips as ReabstractReport even
    though the underlying service now exposes a streaming event generator.
    Seeds a mixed worklist (1 markdown success + 1 pdf skipped_pdf + 1
    failing markdown) and asserts the returned dict's aggregate counts
    match. If the MCP tool ever drifts to calling the streaming generator
    directly without aggregating (returning a list of events instead of a
    report), the model_validate call below fails on the unexpected shape.

    Anti-coincidental-pass: a contract drift where the MCP wrapper
    accidentally returned the FIRST event instead of the aggregator's
    report would fail the ``isinstance(result, dict)`` + model_validate
    flow because a ProgressEvent dict has neither ``reabstracted_count``
    nor ``entries``.
    """
    # Use _SelectivelyFailingProvider so the first dispatched markdown
    # doc lands as llm_failure; subsequent docs succeed.
    failing: AbstractionProvider = _SelectivelyFailingProvider()
    async with _publish_vault(minimal_vault_config_dict, abstraction_provider=failing) as (
        vault_id,
        services,
    ):
        # Seed: 1 markdown that will fail (first generate_abstract call),
        # 1 markdown that will succeed, 1 PDF that will be skipped_pdf.
        fail_doc = _make_skipped_doc(_id("tool_mix_fail_a"))
        ok_doc = _make_skipped_doc(_id("tool_mix_ok_b"))
        pdf_doc = _make_skipped_doc(_id("tool_mix_pdf_c"), source_type=SourceType.PDF)

        # fail_doc carries the FAILME marker so _SelectivelyFailingProvider
        # fails it on every attempt (the worker retries before terminal
        # FAILED); the others have no marker and succeed / skip.
        for doc, body in ((fail_doc, "FAILME Body."), (ok_doc, "Body."), (pdf_doc, "Body.")):
            await services.graph_store.insert_document(doc)
            await services.content_store.index_chunks(
                doc.id,
                [
                    Chunk(
                        document_id=doc.id,
                        heading_path="Body",
                        content=body,
                        chunk_index=0,
                    )
                ],
            )

        try:
            result = await mcp_server.recompute_deferred_vault_abstracts(vault_id=vault_id)

            assert isinstance(result, dict)
            assert "error" not in result, f"expected report dict, got {result!r}"
            report = ReabstractReport.model_validate(result)
            assert report.vault_id == vault_id
            assert report.reabstracted_count == 1
            assert report.skipped_pdf_count == 1
            assert report.failed_count == 1
            assert len(report.entries) == 3

            outcomes_by_id = {entry.document_id: entry.outcome for entry in report.entries}
            assert outcomes_by_id[fail_doc.id] == ReabstractOutcome.LLM_FAILURE
            assert outcomes_by_id[ok_doc.id] == ReabstractOutcome.SUCCESS
            assert outcomes_by_id[pdf_doc.id] == ReabstractOutcome.SKIPPED_PDF
        finally:
            await asyncio.sleep(0.1)
