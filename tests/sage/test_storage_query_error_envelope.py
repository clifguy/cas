"""A backend query refusal reaches the caller as a typed envelope.

``search`` builds its WHERE clause from caller-supplied filters, so a
malformed predicate is refused by the database rather than by any SAGE
validator. Nothing above the storage layer used to catch that: a
``psycopg`` error is not a ``SAGEError``, a ``ValidationError``, or a
``ValueError``, so it fell past every handler in the tool body and
surfaced as a FastMCP ``ToolError`` whose text was the driver's --
quoting the failing statement and the backend's ``HINT``. The caller got
internal query shape instead of an envelope, and no error code to branch
on.

The failure that originally exposed this (a non-string ``tier3_metadata``
filter value compared against the ``text`` accessor) is fixed at the
source, so these tests inject a driver error directly. That is
deliberate: an envelope test written against a reachable driver failure
would go silently untested the moment that failure was repaired.
"""

from __future__ import annotations

import asyncio
import json

import psycopg
import pytest
from mcp.types import TextContent

from sage.adapters.interfaces import StorageQueryError
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.api.errors import StorageQueryFailedError
from sage.config import VaultConfig
from sage.mcp_server import _vaults as _mcp_vaults
from sage.mcp_server import ingest_document, mcp
from tests.sage.conftest import initialize_services_for_test

# The shape of a real driver rejection: the statement is echoed and the
# backend appends a HINT. Every fragment here is something a caller must
# never receive.
DRIVER_MESSAGE = (
    "operator does not exist: text = boolean\n"
    "LINE 1: ...nts WHERE doc_type = $1 AND tier3_metadata->>'caught_by_gate' = $2\n"
    "HINT:  No operator matches the given name and argument types. "
    "You might need to add explicit type casts."
)

# Fragments asserted absent from the caller-visible envelope.
LEAK_MARKERS = (
    "operator does not exist",
    "HINT",
    "tier3_metadata->>",
    "LINE 1",
    "$1",
)


@pytest.fixture
async def vault_services(minimal_vault_config_dict, tmp_vault_dir):
    """Register stub-backed services under ``test_vault`` and seed one document."""
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    async with initialize_services_for_test(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        _mcp_vaults["test_vault"] = services

        test_dir = tmp_vault_dir / "sources" / "test"
        test_dir.mkdir(parents=True, exist_ok=True)
        (test_dir / "sample.md").write_text("# Sample Document\n\nSample content.")
        await ingest_document(
            "test_vault",
            "test/sample.md",
            "markdown",
            metadata={"title": "A Note", "doc_type": "note"},
        )

        try:
            yield services
        finally:
            await asyncio.sleep(0.5)
            _mcp_vaults.pop("test_vault", None)


def _decode_envelope(result):
    """Extract the SAGE envelope dict from a ``[TextContent]`` ``mcp.call_tool`` return."""
    assert isinstance(result, list), f"Expected list result; got {type(result)}"
    assert len(result) == 1, f"Expected single TextContent; got {len(result)}"
    block = result[0]
    assert isinstance(block, TextContent), f"Expected TextContent; got {type(block)}"
    return json.loads(block.text)


def _fail_with_driver_error(services, monkeypatch):
    """Make the store's row fetch raise a realistic driver rejection."""

    async def raising_fetch_rows(sql, params=()):
        raise psycopg.errors.UndefinedFunction(DRIVER_MESSAGE)

    monkeypatch.setattr(services.graph_store, "_fetch_rows", raising_fetch_rows)


async def test_storage_query_refusal_returns_typed_envelope(vault_services, monkeypatch):
    """The caller gets a code to branch on, not a driver string.

    Anti-coincidental-pass: asserts the specific ``storage_query_failed``
    code and the named operation, so a generic ``internal_error`` (which
    the MCP fallback would produce, carrying ``str(exc)``) does not
    satisfy it.
    """
    _fail_with_driver_error(vault_services, monkeypatch)

    result = await mcp.call_tool("search", {"vault_id": "test_vault", "mode": "catalog"})
    envelope = _decode_envelope(result)

    assert envelope["error"] == "storage_query_failed"
    assert envelope["detail"]["operation"] == "query_documents"


async def test_storage_query_refusal_leaks_no_sql_or_driver_text(vault_services, monkeypatch):
    """No fragment of the statement or the backend hint survives to the caller.

    Scans the entire serialized envelope rather than a single field: the
    original leak arrived through the *message*, and a fix that only
    sanitized ``detail`` would look correct field-by-field while still
    shipping the statement.
    """
    _fail_with_driver_error(vault_services, monkeypatch)

    result = await mcp.call_tool("search", {"vault_id": "test_vault", "mode": "catalog"})
    serialized = json.dumps(_decode_envelope(result))

    for marker in LEAK_MARKERS:
        assert marker not in serialized, f"driver text {marker!r} leaked: {serialized}"


async def test_driver_message_is_preserved_for_the_operator(vault_services, monkeypatch, caplog):
    """Sanitizing the response must not discard the diagnostic.

    The driver's text is the only description of what actually failed. It
    belongs in the log, and a fix that simply swallowed the exception
    would pass the leak test above while leaving an operator with
    nothing.
    """
    _fail_with_driver_error(vault_services, monkeypatch)

    with caplog.at_level("ERROR", logger="sage.services.retrieval"):
        await mcp.call_tool("search", {"vault_id": "test_vault", "mode": "catalog"})

    assert "operator does not exist: text = boolean" in caplog.text


def test_storage_query_failed_error_carries_only_the_operation():
    """The public error's payload names the operation and nothing else.

    Pins the envelope's contents at the source so a later change cannot
    reintroduce the driver text by attaching it to ``detail``.
    """
    err = StorageQueryFailedError("query_documents")

    assert err.code == "storage_query_failed"
    assert err.status_code == 500
    assert err.detail == {"operation": "query_documents"}
    assert "operator does not exist" not in err.message


def test_storage_query_error_keeps_the_driver_message_for_translation():
    """The storage-layer signal carries the driver text without exposing it.

    ``str(exc)`` is what a generic handler would surface, so the wrapper's
    own message must stay free of the driver text even though it
    transports it in an attribute.
    """
    exc = StorageQueryError("query_documents", DRIVER_MESSAGE)

    assert exc.driver_message == DRIVER_MESSAGE
    assert exc.operation == "query_documents"
    assert "operator does not exist" not in str(exc)


async def test_non_driver_exception_is_not_labelled_a_storage_failure(vault_services, monkeypatch):
    """The translation is scoped to driver errors, not to every failure.

    Anti-coincidental-pass: the leak and envelope tests above are equally
    satisfied by a handler that relabels *any* exception as
    ``storage_query_failed``. That would be worse than the original leak
    -- an ordinary bug would be reported to callers as a backend refusal
    and sent to the wrong owner to investigate. Only a driver error may
    earn the code.
    """

    marker = "a defect in SAGE's own code, not the backend"

    async def raising_fetch_rows(sql, params=()):
        raise RuntimeError(marker)

    monkeypatch.setattr(vault_services.graph_store, "_fetch_rows", raising_fetch_rows)

    # It propagates rather than being converted, which is the pre-existing
    # behaviour for a genuine defect and the correct one: only the driver's
    # own refusal is a storage failure.
    with pytest.raises(Exception) as excinfo:  # noqa: B017 -- transport wraps in ToolError
        await mcp.call_tool("search", {"vault_id": "test_vault", "mode": "catalog"})

    assert marker in str(excinfo.value)
    assert "storage_query_failed" not in str(excinfo.value)
