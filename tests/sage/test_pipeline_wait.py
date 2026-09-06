"""Coverage for the shared pipeline-wait helper.

The helper is what every module's wait now delegates to, so its own behaviour
has to be established directly rather than inferred from the stability of the
suites that use it. The load-bearing test is
``test_blocks_while_claim_held_under_terminal_status``: it pins the document's
status terminal *underneath* a claim the test holds open, which is the window
the abstraction worker leaves between its terminal status write and the claim
release. A wait that keyed on status alone would return there, and its caller's
next call would be rejected.

Lives under ``tests/sage/`` rather than beside the helper because the fixtures
it needs -- ``ingestion_service``, ``graph_store``, ``tmp_vault_dir`` -- are
defined in ``tests/sage/conftest.py`` and do not reach ``tests/helpers/``.
"""

import asyncio
import inspect
from pathlib import Path

import pytest

from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import IngestRequest
from tests.helpers.pipeline_wait import (
    TERMINAL_PIPELINE_STATES,
    await_pipeline_idle,
    await_tool_idle,
)


def _create_test_file(tmp_vault_dir: Path, relative_path: str, content: str) -> Path:
    """Create a Markdown file in the vault's sources directory."""
    full_path = tmp_vault_dir / "sources" / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content)
    return full_path


async def _ingest(tmp_vault_dir, ingestion_service, name: str) -> str:
    """Ingest one small document and return its id."""
    _create_test_file(tmp_vault_dir, f"{name}.md", f"# {name}\n\nContent.")
    result = await ingestion_service.ingest(
        IngestRequest(source=f"{name}.md", source_type=SourceType.MARKDOWN)
    )
    return result.document.id


async def test_returns_document_once_settled(tmp_vault_dir, ingestion_service, graph_store):
    """The helper returns the document once it has settled, and no claim is
    held at the moment it returns.
    """
    doc_id = await _ingest(tmp_vault_dir, ingestion_service, "idle")

    doc = await await_pipeline_idle(graph_store, doc_id, service=ingestion_service)

    assert doc.pipeline_status.value in TERMINAL_PIPELINE_STATES
    assert doc_id not in ingestion_service._inflight


async def test_blocks_while_claim_held_under_terminal_status(
    tmp_vault_dir, ingestion_service, graph_store
):
    """A terminal status is not sufficient: while the claim is held the helper
    keeps waiting, because that is the condition the 409 guard rejects on.

    The status is forced terminal underneath a claim held open by an unset
    event, reproducing the window the worker leaves between its terminal status
    write and the claim release -- deterministically, rather than hoping to
    catch the real window. The paired status-only wait at the same instant
    returns there, and that contrast is what shows the claim arm rather than
    the status arm is doing the work.
    """
    doc_id = await _ingest(tmp_vault_dir, ingestion_service, "claimheld")
    await await_pipeline_idle(graph_store, doc_id, service=ingestion_service)

    entered = asyncio.Event()
    gate = asyncio.Event()

    async def gated_abstract(text: str, max_tokens: int, doc_type: str | None) -> str:
        entered.set()
        await gate.wait()
        return "gated abstract"

    ingestion_service._abstraction.generate_abstract = gated_abstract

    assert (await ingestion_service.reabstract(doc_id))["status"] == "reabstract_started"
    await asyncio.wait_for(entered.wait(), timeout=2.0)

    try:
        await graph_store.update_document(
            doc_id, {"pipeline_status": PipelineStatus.ABSTRACTION_COMPLETE.value}
        )
        assert doc_id in ingestion_service._inflight

        with pytest.raises(AssertionError, match="claim still held"):
            await await_pipeline_idle(
                graph_store, doc_id, service=ingestion_service, attempts=3, delay=0
            )

        # Same document, same instant, status arm only: nothing left to wait
        # for, so it returns. This is the control -- without it, a helper that
        # simply never returned would satisfy the assertion above.
        class _NoClaims:
            _inflight: dict[str, object] = {}

        observed = await await_pipeline_idle(
            graph_store, doc_id, service=_NoClaims(), attempts=3, delay=0
        )
        assert observed.pipeline_status == PipelineStatus.ABSTRACTION_COMPLETE

        # The second read path, under the same held claim. Its happy-path test
        # cannot show this: with no claim outstanding, an entry point that
        # dropped the claim arm returns the same document as one that kept it.
        async def fetch():
            doc = await graph_store.get_document(doc_id)
            return {"id": doc.id, "pipeline_status": doc.pipeline_status.value}

        with pytest.raises(AssertionError, match="claim still held"):
            await await_tool_idle(fetch, doc_id, service=ingestion_service, attempts=3, delay=0)

        assert (await fetch())["pipeline_status"] == PipelineStatus.ABSTRACTION_COMPLETE.value
    finally:
        gate.set()

    doc = await await_pipeline_idle(graph_store, doc_id, service=ingestion_service)
    assert doc.pipeline_status.value in TERMINAL_PIPELINE_STATES


async def test_timeout_names_document_status_and_claim(
    tmp_vault_dir, ingestion_service, graph_store
):
    """A document parked non-terminal produces an AssertionError naming the
    document, the last status seen and the claim state -- not a fall-through to
    whatever assertion the caller was about to make.
    """
    doc_id = await _ingest(tmp_vault_dir, ingestion_service, "parked")
    await await_pipeline_idle(graph_store, doc_id, service=ingestion_service)

    await graph_store.update_document(
        doc_id, {"pipeline_status": PipelineStatus.INDEXING_COMPLETE.value}
    )

    with pytest.raises(AssertionError) as excinfo:
        await await_pipeline_idle(
            graph_store, doc_id, service=ingestion_service, attempts=2, delay=0
        )

    message = str(excinfo.value)
    assert doc_id in message
    assert "indexing_complete" in message
    assert "no claim held" in message


async def test_tool_surface_entry_point_returns_idle_document(
    tmp_vault_dir, ingestion_service, graph_store
):
    """The second read path returns the payload in the shape it arrives in.

    A tool or HTTP route hands back parsed JSON rather than a ``Document``, so
    the status arm has to read a mapping key where the store path reads an
    attribute. This covers that shape and the happy path only: with no claim
    outstanding it cannot separate an entry point that keeps the claim arm from
    one that drops it. ``test_blocks_while_claim_held_under_terminal_status``
    exercises this same entry point under a held claim, and that is what pins
    the claim arm on this path.
    """
    doc_id = await _ingest(tmp_vault_dir, ingestion_service, "toolpath")

    async def fetch() -> dict:
        doc = await graph_store.get_document(doc_id)
        return {"id": doc.id, "pipeline_status": doc.pipeline_status.value}

    payload = await await_tool_idle(fetch, doc_id, service=ingestion_service)

    assert isinstance(payload, dict)
    assert payload["pipeline_status"] in TERMINAL_PIPELINE_STATES
    assert doc_id not in ingestion_service._inflight


@pytest.mark.parametrize("entry_point", [await_pipeline_idle, await_tool_idle])
def test_claim_arm_is_not_optional(entry_point):
    """``service`` is a required keyword argument on both entry points.

    This is the property that makes the migration mechanical: the predicate a
    caller used to have to remember to opt into is one it cannot omit. Every
    earlier instance of this defect class was a caller declining an optional
    check or copying a helper that never offered one.
    """
    parameter = inspect.signature(entry_point).parameters["service"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty

    with pytest.raises(TypeError):
        entry_point(object(), "somedoc")
