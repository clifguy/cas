"""Transport-layer tests for the legacy_form envelope on update_metadata and bulk_update_metadata.

Pins the body-level `_check_legacy_patch_form` guard's reachability through
the real FastMCP transport, not just the in-process path. The in-process
tests in `test_mcp_server.py` (``test_update_metadata_tags_legacy_form_rejected``
and ``test_update_metadata_tier3_legacy_form_rejected``) and the bulk in-process
tests in `test_sage_bulk_update_metadata.py` call the registered tool
functions directly and bypass FastMCP's per-tool Pydantic argument model.
This module routes through ``mcp.call_tool()`` so framework-level argument
validation runs first, exactly as it does for real MCP clients.

The first test pins the actual bug surface: with the ``tags: dict | None``
signature, FastMCP rejects a bare list at the framework boundary before
the body-level guard can fire. Widening the annotation to
``dict | list | None`` lets the bare-list shape reach the body-level guard
and surfaces the structured ``legacy_form`` envelope on real transport.

The remaining three are regression guards. The ``tier3_metadata`` legacy
form is a dict (key/value pairs whose keys are not ``{"set", "unset"}``),
so the current ``dict | None`` signature already lets the legacy shape
reach the body-level guard. ``bulk_update_metadata`` declares
``items: list[dict]``, which makes the per-item shape opaque to FastMCP's
argument validation, so the per-item legacy-form guard fires inside the
body loop regardless of caller transport. These three are pinned so a
future change to the body-level guard, the tool registration, or the
``items`` shape can't silently regress what already works.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.types import TextContent

from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.config import VaultConfig
from sage.mcp_server import _vaults as _mcp_vaults
from sage.mcp_server import ingest_document, mcp
from tests.sage.conftest import initialize_services_for_test


@pytest.fixture
async def vault_services(minimal_vault_config_dict, tmp_vault_dir):
    """Initialize SAGE services and register them on the MCP vault registry.

    Mirrors the ``vault_services`` fixture in ``tests/sage/test_mcp_server.py``;
    duplicated here so this transport-level module stays self-contained.
    Registers under ``vault_id="test_vault"`` so the ``get_vault`` lookup
    inside each MCP tool resolves to the stub-backed services this fixture
    creates.
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    async with initialize_services_for_test(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        _mcp_vaults["test_vault"] = services

        sources = tmp_vault_dir / "sources"
        test_dir = sources / "test"
        test_dir.mkdir(parents=True, exist_ok=True)
        (test_dir / "sample.md").write_text("# Sample Document\n\nSample content.")

        try:
            yield services
        finally:
            await asyncio.sleep(0.5)
            _mcp_vaults.pop("test_vault", None)


def _parse(result):
    """Parse an in-process tool result (dict or JSON string) to a dict."""
    if isinstance(result, dict):
        return result
    return json.loads(result)


def _decode_envelope(result):
    """Extract the SAGE envelope dict from a `[TextContent]` ``mcp.call_tool`` return.

    Mirrors the helper in ``tests/sage/test_fastmcp_strict_args.py``. SAGE
    tool error envelopes are dicts that FastMCP's ``_convert_to_content``
    wraps in ``[TextContent(type="text", text=<json>)]`` before they reach
    the caller. Asserts the wire shape, then JSON-decodes the body.
    """
    assert isinstance(result, list), f"Expected list result; got {type(result)}"
    assert len(result) == 1, f"Expected single TextContent; got {len(result)}"
    block = result[0]
    assert isinstance(block, TextContent), f"Expected TextContent; got {type(block)}"
    return json.loads(block.text)


# ---------------------------------------------------------------------------
# update_metadata
# ---------------------------------------------------------------------------


async def test_update_metadata_tags_bare_list_returns_legacy_form_envelope_via_transport(
    vault_services,
):
    """Bare-list per-item ``tags`` on ``update_metadata`` returns
    legacy_form via MCP transport (post-CAS-ADR-029 plural-noun shape).

    Per CAS-ADR-029 v4 the MCP tool takes ``items: list[dict]``; the
    per-item dict is opaque to FastMCP's per-tool argument model so
    the bare-list ``tags`` value inside the item reaches the body-level
    per-item legacy-form guard. The structured ``legacy_form`` envelope
    is the result.

    Anti-coincidental-pass: assertions target the specific
    ``legacy_form`` envelope shape (error code, field name, worked-
    example substring), not a generic error class.
    """
    ingest_result = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    result = await mcp.call_tool(
        "update_metadata",
        {
            "vault_id": "test_vault",
            "items": [{"document_id": doc_id, "tags": ["a", "b"]}],
        },
    )
    envelope = _decode_envelope(result)
    assert envelope["error"] == "legacy_form"
    assert envelope["detail"]["field"] == "tags"
    assert "add" in envelope["detail"]["example"]


async def test_update_metadata_tier3_bare_dict_returns_legacy_form_envelope_via_transport(
    vault_services,
):
    """Bare-dict per-item ``tier3_metadata`` on ``update_metadata``
    returns legacy_form via MCP transport (post-CAS-ADR-029 plural-noun shape).
    """
    ingest_result = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    result = await mcp.call_tool(
        "update_metadata",
        {
            "vault_id": "test_vault",
            "items": [{"document_id": doc_id, "tier3_metadata": {"some_key": "value"}}],
        },
    )
    envelope = _decode_envelope(result)
    assert envelope["error"] == "legacy_form"
    assert envelope["detail"]["field"] == "tier3_metadata"
    assert "set" in envelope["detail"]["example"]


# ---------------------------------------------------------------------------
# bulk_update_metadata
# ---------------------------------------------------------------------------


async def test_bulk_update_metadata_item_tags_bare_list_returns_legacy_form_envelope_via_transport(
    vault_services,
):
    """Bare-list per-item ``tags`` on ``bulk_update_metadata`` returns legacy_form via transport.

    ``bulk_update_metadata`` declares ``items: list[dict]``; the inner
    dict structure is opaque to FastMCP's per-tool argument model, so the
    bare-list ``tags`` value inside the item reaches the body-level
    per-item legacy-form guard regardless of caller transport. Regression
    guard: pin the wire shape.
    """
    ingest_result = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    result = await mcp.call_tool(
        "bulk_update_metadata",  # legacy name; alias-layer routes to update_metadata
        {
            "vault_id": "test_vault",
            "items": [
                {"document_id": doc_id, "title": "valid"},
                {"document_id": doc_id, "tags": ["a", "b"]},
            ],
        },
    )
    envelope = _decode_envelope(result)
    assert envelope["error"] == "legacy_form"
    assert envelope["detail"]["field"] == "tags"
    assert "add" in envelope["detail"]["example"]


async def test_bulk_update_metadata_item_tier3_bare_dict_returns_legacy_form_envelope_via_transport(
    vault_services,
):
    """Bare-dict per-item ``tier3_metadata`` on ``bulk_update_metadata`` returns legacy_form.

    Same ``items: list[dict]`` opacity argument as the bare-list tags
    case: the per-item dict reaches the body-level guard regardless of
    caller transport. Regression guard.
    """
    ingest_result = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    result = await mcp.call_tool(
        "bulk_update_metadata",  # legacy name; alias-layer routes to update_metadata
        {
            "vault_id": "test_vault",
            "items": [
                {"document_id": doc_id, "title": "valid"},
                {"document_id": doc_id, "tier3_metadata": {"some_key": "value"}},
            ],
        },
    )
    envelope = _decode_envelope(result)
    assert envelope["error"] == "legacy_form"
    assert envelope["detail"]["field"] == "tier3_metadata"
    assert "set" in envelope["detail"]["example"]
