"""HTTP-transport tests for the legacy_form envelope on update_metadata and bulk_update_metadata.

Pins the structured ``legacy_form`` 400 envelope's reachability through the
real FastAPI request-body parsing layer, not just the in-process service
or the MCP transport. The MCP-transport mirror at
``test_update_metadata_legacy_form_via_transport.py`` covers the FastMCP
boundary; this module covers the FastAPI boundary. The in-process tests at
``test_mcp_server.py`` (``test_update_metadata_tags_legacy_form_rejected``,
``test_update_metadata_tier3_legacy_form_rejected``) call the MCP tool
function directly and bypass both boundaries entirely.

The first two tests pin the actual bug surface for ``update_metadata``:
``UpdateMetadataRequest`` declares ``tags: ListFieldPatch | None`` and
``tier3_metadata: Tier3Patch | None`` with ``extra: "forbid"``, so a
bare-list or bare-dict shape fails Pydantic field validation at body parse
time and surfaces as FastAPI's 422 envelope rather than the structured
``legacy_form`` 400 envelope. A ``@model_validator(mode='before')`` on the
request model intercepts the raw payload pre-parse and raises
``PydanticCustomError(type='legacy_form')``; Pydantic wraps it in
``ValidationError``, FastAPI re-raises as ``RequestValidationError``, the
registered ``request_validation_handler`` invokes
``translate_validation_error`` which recognises the ``legacy_form`` type
and reconstructs a ``LegacyFormError`` from the embedded ``ctx``, and the
``SAGEError`` exception handler emits the structured envelope. The
two-step indirection exists because ``sage.models`` cannot import
``sage.api.errors`` directly under the import-linter "Models are a leaf
layer" contract; the same pattern is already used for
``mode_parameter_mismatch`` on ``DiscoverRequest``. The remaining two
tests are the symmetric fix on ``BulkMetadataItem`` for the bulk endpoint.

Anti-coincidental-pass discipline: each test pins the specific
``legacy_form`` envelope shape (status 400, ``code`` field, ``detail.field``
name, ``detail.received_type`` string, worked-example substring) AND
asserts the envelope is not FastAPI's 422 validation envelope (top-level
``detail`` as a list of validation-error dicts). A test that asserted only
the absence of 422 would pass on any other error envelope; a test that
asserted only ``detail.field == "tags"`` could pass against the wrong
envelope shape if a particular field happened to be at index 0 in a
hypothetical regression. Pinning both the positive shape and the negative
shape catches both failure modes.

CAS-ADR-028 ops-object patch grammar; CAS-ADR-038 concurrency-safety
contract.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.app import _initialize_services, create_app
from sage.config import VaultConfig


@pytest.fixture
async def app(minimal_vault_config_dict, tmp_vault_dir):
    """FastAPI app with stub services and one ingestable source file.

    Mirrors the ``app`` fixture in ``tests/sage/test_api_integration.py``;
    duplicated here so this transport-level module stays self-contained
    alongside its MCP-transport sibling
    (``test_update_metadata_legacy_form_via_transport.py``).
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    await _initialize_services(
        app,
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    )
    sources = tmp_vault_dir / "sources"
    test_dir = sources / "test"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "sample.md").write_text("# Sample Document\n\nSample content.")
    yield app
    await asyncio.sleep(0.5)
    await app.state.graph_store.close()


@pytest.fixture
async def client(app):
    """Async HTTP client bound to the in-process ASGI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def doc_id(client) -> str:
    """Ingest one document into ``test_vault`` and return its id.

    The HTTP request-body parsing under test runs after URL routing
    succeeds, so the URL's ``document_id`` segment must reference a real
    document for the route handler to be reachable. Each test in this
    module needs exactly one valid id, so we ingest once per test rather
    than parameterize.
    """
    resp = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample.md", "source_type": "markdown"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["document"]["id"]


def _assert_legacy_form_envelope(
    resp,
    *,
    expected_field: str,
    expected_received_type: str,
    expected_example_substr: str,
) -> None:
    """Assert the response carries the structured legacy_form envelope.

    Anti-coincidental-pass: pin every load-bearing slot. ``code`` and
    ``status_code`` catch a wrong-envelope-class regression;
    ``detail.field`` and ``detail.received_type`` catch a copy-paste
    regression that puts the wrong field's metadata in the right shape;
    the ``detail`` is-not-a-list check rejects FastAPI's 422 validation
    envelope, whose top-level ``detail`` is ``list[dict]``.
    """
    assert resp.status_code == 400, f"Expected 400 legacy_form; got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("code") == "legacy_form", body
    assert "detail" in body, body
    assert not isinstance(body["detail"], list), (
        f"Got FastAPI 422-style envelope (detail is a list); expected structured "
        f"legacy_form envelope (detail is a dict). Body: {body}"
    )
    assert body["detail"]["field"] == expected_field, body
    assert body["detail"]["received_type"] == expected_received_type, body
    assert expected_example_substr in body["detail"]["example"], body


# ---------------------------------------------------------------------------
# update_metadata
# ---------------------------------------------------------------------------


async def test_update_metadata_tags_bare_list_returns_legacy_form_envelope_via_http(client, doc_id):
    """Bare-list ``tags`` on ``PATCH .../metadata`` returns legacy_form 400 via HTTP transport.

    Pre-fix surface: ``UpdateMetadataRequest.tags: ListFieldPatch | None``
    with ``extra: "forbid"`` rejects the bare list during Pydantic
    request-body parsing; FastAPI wraps the ``ValidationError`` in
    ``RequestValidationError`` and ``request_validation_handler`` falls
    through to the default 422 envelope shape
    (``{"detail": [{"loc": [...], ...}]}``).

    Post-fix surface: ``UpdateMetadataRequest`` carries a
    ``@model_validator(mode='before')`` that raises
    ``PydanticCustomError(type='legacy_form')`` before field validation
    runs. Pydantic wraps it in ``ValidationError`` and FastAPI re-raises
    as ``RequestValidationError``; ``request_validation_handler`` calls
    ``translate_validation_error``, which recognises the ``legacy_form``
    type, reconstructs ``LegacyFormError`` from the embedded ``ctx``,
    and the global ``SAGEError`` exception handler emits the structured
    400 envelope.
    """
    resp = await client.patch(
        f"/sage_vaults/test_vault/documents/{doc_id}/metadata",
        json={"tags": ["alpha", "beta"]},
    )
    _assert_legacy_form_envelope(
        resp,
        expected_field="tags",
        expected_received_type="list",
        expected_example_substr="add",
    )


async def test_update_metadata_tier3_bare_dict_returns_legacy_form_envelope_via_http(
    client, doc_id
):
    """Bare-dict ``tier3_metadata`` on ``PATCH .../metadata`` returns legacy_form 400 via HTTP.

    The legacy form for ``tier3_metadata`` is a dict whose keys are NOT a
    subset of ``{"set", "unset"}``. Pre-fix, this fails ``Tier3Patch``
    field validation with ``extra_forbidden`` for each unknown key.
    Post-fix, the same ``@model_validator(mode='before')`` on
    ``UpdateMetadataRequest`` intercepts the raw shape and raises
    ``PydanticCustomError(type='legacy_form')`` with
    ``field="tier3_metadata"`` in the embedded ``ctx``;
    ``translate_validation_error`` reconstructs ``LegacyFormError`` and
    the ``SAGEError`` handler emits the structured 400 envelope.
    """
    resp = await client.patch(
        f"/sage_vaults/test_vault/documents/{doc_id}/metadata",
        json={"tier3_metadata": {"some_key": "value"}},
    )
    _assert_legacy_form_envelope(
        resp,
        expected_field="tier3_metadata",
        expected_received_type="dict (bare key/value pairs)",
        expected_example_substr="set",
    )


# ---------------------------------------------------------------------------
# bulk_update_metadata
# ---------------------------------------------------------------------------


async def test_bulk_update_metadata_item_tags_bare_list_returns_legacy_form_envelope_via_http(
    client, doc_id
):
    """Bare-list per-item ``tags`` on ``POST .../metadata/bulk`` returns legacy_form 400 via HTTP.

    ``BulkMetadataRequest.items: list[BulkMetadataItem]`` parses each
    item against ``BulkMetadataItem`` at request-body parse time. The
    same validator that lives on ``UpdateMetadataRequest`` must also
    live on ``BulkMetadataItem``, otherwise a bare-list per-item ``tags``
    surfaces FastAPI's 422 validation envelope at the batch level rather
    than the structured ``legacy_form`` envelope.

    The expected failure is batch-level (status 400, single envelope),
    not per-item (status 200 with per-item error). The body-parse
    rejection happens before any item enters the per-item service loop.
    """
    resp = await client.post(
        "/sage_vaults/test_vault/metadata/bulk",
        json={"items": [{"document_id": doc_id, "tags": ["alpha", "beta"]}]},
    )
    _assert_legacy_form_envelope(
        resp,
        expected_field="tags",
        expected_received_type="list",
        expected_example_substr="add",
    )


async def test_bulk_update_metadata_item_tier3_bare_dict_returns_legacy_form_envelope_via_http(
    client, doc_id
):
    """Bare-dict per-item ``tier3_metadata`` on ``POST .../metadata/bulk`` returns legacy_form 400.

    Same per-item Pydantic-parse argument as the bare-list tags case:
    the validator on ``BulkMetadataItem`` must intercept before
    ``Tier3Patch`` field validation rejects the bare dict.
    """
    resp = await client.post(
        "/sage_vaults/test_vault/metadata/bulk",
        json={"items": [{"document_id": doc_id, "tier3_metadata": {"some_key": "value"}}]},
    )
    _assert_legacy_form_envelope(
        resp,
        expected_field="tier3_metadata",
        expected_received_type="dict (bare key/value pairs)",
        expected_example_substr="set",
    )
