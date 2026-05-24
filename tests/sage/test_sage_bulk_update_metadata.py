"""MCP tool tests for sage_bulk_update_metadata (T-0088).

Exercises the boundary contract: vault_id and per-item shape validation,
per-item legacy-form rejection, registry membership check, and round-trip
of the BulkMetadataResponse payload through the MCP serialize path.
"""

from __future__ import annotations

import pytest

from sage import mcp_server
from sage.adapters.stubs import StubContentStore
from sage.app import _initialize_services, create_app
from sage.config import VaultConfig
from sage.models.schemas import BulkMetadataResponse
from tests.sage.test_lifecycle import _id, _make_doc


@pytest.fixture
def empty_registry(monkeypatch: pytest.MonkeyPatch):
    """Snapshot mcp_server._vaults before each test and restore after."""
    saved = dict(mcp_server._vaults)
    mcp_server._vaults.clear()
    try:
        yield
    finally:
        mcp_server._vaults.clear()
        mcp_server._vaults.update(saved)


@pytest.fixture
async def seeded_mcp_vault(minimal_vault_config_dict, monkeypatch, empty_registry):
    """Boot a vault, seed two documents with tags=['a'], publish on mcp_server._vaults."""
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    await _initialize_services(
        app,
        config,
        content_store_factory=lambda _brain: StubContentStore(),
    )
    vault_id = config.vault.id
    services = app.state.vault_registry[vault_id]
    mcp_server._vaults[vault_id] = services

    seeded_ids = [_id("doc_mcp1"), _id("doc_mcp2")]
    for doc_id in seeded_ids:
        await services.graph_store.insert_document(_make_doc(doc_id))
        await services.graph_store.update_document(
            doc_id, {"doc_type": "note", "tags": ["a"], "metadata_confirmed": True}
        )

    yield vault_id, seeded_ids

    await services.graph_store.close()


async def test_mcp_tool_round_trip_returns_dict_matching_response_model(seeded_mcp_vault):
    """The returned dict must deserialize cleanly as BulkMetadataResponse."""
    vault_id, seeded_ids = seeded_mcp_vault
    items = [{"document_id": d, "tags": {"add": ["b"]}} for d in seeded_ids]

    result = await mcp_server.sage_bulk_update_metadata(vault_id=vault_id, items=items)

    assert isinstance(result, dict)
    assert "error" not in result, f"unexpected error envelope: {result!r}"
    response = BulkMetadataResponse.model_validate(result)
    assert response.total == 2
    assert response.success_count == 2


async def test_mcp_tool_invalid_vault_id_returns_error_envelope(empty_registry):
    """A vault_id that fails the VaultIdStr adapter surfaces as the error envelope."""
    result = await mcp_server.sage_bulk_update_metadata(vault_id="not a vault id!", items=[])

    assert isinstance(result, dict)
    assert "error" in result
    assert result["error"] == "internal_error"


async def test_mcp_tool_unknown_vault_returns_error_envelope(empty_registry):
    """A syntactically valid but unregistered vault_id surfaces unknown_vault."""
    result = await mcp_server.sage_bulk_update_metadata(vault_id="ghost", items=[])

    assert isinstance(result, dict)
    assert result.get("error") == "unknown_vault"


async def test_mcp_tool_items_validation_rejects_bad_shape(seeded_mcp_vault):
    """Malformed items (missing document_id) fail shape validation BEFORE
    any per-item work runs. No partial state changes occur."""
    vault_id, seeded_ids = seeded_mcp_vault
    bad_items = [
        {"document_id": seeded_ids[0], "tags": {"add": ["b"]}},
        {"tags": {"add": ["b"]}},  # missing document_id
    ]

    result = await mcp_server.sage_bulk_update_metadata(vault_id=vault_id, items=bad_items)

    assert isinstance(result, dict)
    assert "error" in result, f"expected validation error envelope, got {result!r}"

    # Anti-coincidental-pass: the well-formed item must NOT have been
    # processed. If shape validation had run lazily inside the loop,
    # item 0 would have committed before item 1 raised.
    services = mcp_server._vaults[vault_id]
    stored = await services.graph_store.get_document(seeded_ids[0])
    assert stored.tags == ["a"], (
        "Up-front shape validation must reject the entire batch before any "
        "per-item work runs; item 0 must remain in its seeded state."
    )


async def test_mcp_tool_legacy_form_tags_rejected_per_item(seeded_mcp_vault):
    """A per-item bare-list `tags` value triggers legacy_form rejection
    before Pydantic validation runs.

    Without the per-item _check_legacy_patch_form call, Pydantic would
    raise a generic ValueError and the legacy_form discriminator (with
    the worked-example detail per CAS-ADR-028) would be lost.
    """
    vault_id, seeded_ids = seeded_mcp_vault
    items = [
        {"document_id": seeded_ids[0], "tags": {"add": ["b"]}},
        {"document_id": seeded_ids[1], "tags": ["bare", "list"]},
    ]

    result = await mcp_server.sage_bulk_update_metadata(vault_id=vault_id, items=items)

    assert isinstance(result, dict)
    assert result.get("error") == "legacy_form", (
        f"expected legacy_form error envelope, got {result!r}"
    )

    # Anti-coincidental-pass: item 0 must NOT have been committed —
    # early failure aborts the whole batch up-front.
    services = mcp_server._vaults[vault_id]
    stored0 = await services.graph_store.get_document(seeded_ids[0])
    assert stored0.tags == ["a"], (
        "legacy_form rejection must abort the batch before any per-item "
        "work runs; item 0 must remain in its seeded state."
    )


async def test_mcp_tool_legacy_form_tier3_metadata_rejected_per_item(seeded_mcp_vault):
    """A per-item bare-dict tier3_metadata value triggers legacy_form
    rejection before Pydantic validation."""
    vault_id, seeded_ids = seeded_mcp_vault
    items = [
        {
            "document_id": seeded_ids[0],
            # Bare-dict form (no set/unset wrapper).
            "tier3_metadata": {"ticket_priority": "high"},
        }
    ]

    result = await mcp_server.sage_bulk_update_metadata(vault_id=vault_id, items=items)

    assert isinstance(result, dict)
    assert result.get("error") == "legacy_form", (
        f"expected legacy_form error envelope, got {result!r}"
    )


# ---------------------------------------------------------------------------
# T-0153: response_mode parameter on bulk mutation tools
# ---------------------------------------------------------------------------

_T0153_ABSTRACT = "Test abstract used as the bulk-mode bloat probe (T-0153)."


@pytest.fixture
async def seeded_six_with_abstracts(minimal_vault_config_dict, monkeypatch, empty_registry):
    """T-0153 fixture. Boot a vault, seed six active documents each
    with doc_type='note', tags=['a'], and a populated `semantic_abstract`;
    publish on mcp_server._vaults.

    Six items so the threshold-default tests can cross the >5 boundary
    in either direction by slicing. semantic_abstract is the field the
    field-use report cited as the primary bloat source; tests assert on
    its presence (full) and absence (light)."""
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    await _initialize_services(
        app,
        config,
        content_store_factory=lambda _brain: StubContentStore(),
    )
    vault_id = config.vault.id
    services = app.state.vault_registry[vault_id]
    mcp_server._vaults[vault_id] = services

    seeded_ids = [_id(f"doc_t0153_{n}") for n in range(6)]
    for doc_id in seeded_ids:
        await services.graph_store.insert_document(_make_doc(doc_id))
        await services.graph_store.update_document(
            doc_id,
            {
                "doc_type": "note",
                "tags": ["a"],
                "metadata_confirmed": True,
                "semantic_abstract": _T0153_ABSTRACT,
            },
        )

    yield vault_id, seeded_ids

    await services.graph_store.close()


async def test_t0153_t1_light_strips_document_and_semantic_abstract(
    seeded_six_with_abstracts,
):
    """T1 — Explicit response_mode='light' with a 3-item all-success batch
    drops the per-item `document` field entirely. Batch size 3 is below
    the >5 threshold so this demonstrates the explicit-override path,
    not a default-triggered one."""
    vault_id, seeded_ids = seeded_six_with_abstracts
    items = [{"document_id": d, "tags": {"add": ["b"]}} for d in seeded_ids[:3]]

    result = await mcp_server.sage_bulk_update_metadata(
        vault_id=vault_id, items=items, response_mode="light"
    )

    assert "error" not in result, f"unexpected error envelope: {result!r}"
    assert result["success_count"] == 3
    for entry in result["results"]:
        assert entry["status"] == "success"
        # Anti-coincidental: assert on raw dict shape, not via
        # BulkMetadataResponse.model_validate(...).results[i].document is None.
        # MCP's _serialize uses exclude_none=True, so a None `document`
        # is stripped from the wire payload entirely.
        assert "document" not in entry, (
            f"light mode must strip the per-item `document` field; got {entry!r}"
        )
    assert _T0153_ABSTRACT not in str(result), (
        "light mode must not leak the semantic_abstract probe string"
    )


async def test_t0153_t2_full_preserves_document_with_semantic_abstract(
    seeded_six_with_abstracts,
):
    """T2 — Explicit response_mode='full' with a 3-item all-success batch
    preserves the current per-item shape, including semantic_abstract."""
    vault_id, seeded_ids = seeded_six_with_abstracts
    items = [{"document_id": d, "tags": {"add": ["b"]}} for d in seeded_ids[:3]]

    result = await mcp_server.sage_bulk_update_metadata(
        vault_id=vault_id, items=items, response_mode="full"
    )

    assert "error" not in result, f"unexpected error envelope: {result!r}"
    assert result["success_count"] == 3
    for entry in result["results"]:
        assert entry["status"] == "success"
        # Anti-coincidental: assert the specific cited bloat field, not
        # just truthiness of `document`.
        assert entry["document"]["semantic_abstract"] == _T0153_ABSTRACT


async def test_t0153_t3_default_above_threshold_returns_light(
    seeded_six_with_abstracts,
):
    """T3 — Default mode with a 6-item batch returns light. Critical
    anti-coincidental: `response_mode` is NOT passed; tests the default-
    resolution branch."""
    vault_id, seeded_ids = seeded_six_with_abstracts
    items = [{"document_id": d, "tags": {"add": ["b"]}} for d in seeded_ids[:6]]

    result = await mcp_server.sage_bulk_update_metadata(vault_id=vault_id, items=items)

    assert "error" not in result, f"unexpected error envelope: {result!r}"
    assert result["success_count"] == 6
    for entry in result["results"]:
        assert "document" not in entry, (
            f"default above threshold (6>5) must be light; got {entry!r}"
        )


async def test_t0153_t4_default_at_or_below_threshold_returns_full(
    seeded_six_with_abstracts,
):
    """T4 — Default mode with a 3-item batch (at-or-below threshold) is
    full."""
    vault_id, seeded_ids = seeded_six_with_abstracts
    items = [{"document_id": d, "tags": {"add": ["b"]}} for d in seeded_ids[:3]]

    result = await mcp_server.sage_bulk_update_metadata(vault_id=vault_id, items=items)

    assert "error" not in result, f"unexpected error envelope: {result!r}"
    assert result["success_count"] == 3
    for entry in result["results"]:
        assert entry["document"]["semantic_abstract"] == _T0153_ABSTRACT


async def test_t0153_t5_error_envelope_intact_in_light_mode(
    seeded_six_with_abstracts,
):
    """T5 — An error item in light mode keeps the full error envelope,
    including the structured `detail` payload (tag_add_conflict carries
    `current_tags` in detail). Round-trip against the same shape in full
    mode; envelopes must be byte-identical."""
    vault_id, seeded_ids = seeded_six_with_abstracts
    # 'a' is already present on seeded docs, so add of 'a' raises
    # tag_add_conflict with detail={'current_tags': ['a']}. Use a
    # different success-item per call so the calls don't need state
    # reset between them.
    light_items = [
        {"document_id": seeded_ids[0], "tags": {"add": ["new0"]}},
        {"document_id": seeded_ids[2], "tags": {"add": ["a"]}},
    ]
    full_items = [
        {"document_id": seeded_ids[1], "tags": {"add": ["new1"]}},
        {"document_id": seeded_ids[2], "tags": {"add": ["a"]}},
    ]

    light_result = await mcp_server.sage_bulk_update_metadata(
        vault_id=vault_id, items=light_items, response_mode="light"
    )
    full_result = await mcp_server.sage_bulk_update_metadata(
        vault_id=vault_id, items=full_items, response_mode="full"
    )

    light_err = light_result["results"][1]
    full_err = full_result["results"][1]
    assert light_err["status"] == "error"
    assert full_err["status"] == "error"
    # Anti-coincidental: envelope must round-trip identically, including
    # the `detail` payload (current_tags).
    assert light_err["error"] == full_err["error"], (
        f"light mode must not strip error envelope; light={light_err!r} full={full_err!r}"
    )
    assert light_err["error"]["error"] == "tag_add_conflict"
    assert "detail" in light_err["error"]


async def test_t0153_t6_error_envelope_intact_in_full_mode(
    seeded_six_with_abstracts,
):
    """T6 — Error item in full mode (regression guard)."""
    vault_id, seeded_ids = seeded_six_with_abstracts
    items = [
        {"document_id": seeded_ids[0], "tags": {"add": ["b"]}},
        {"document_id": seeded_ids[1], "tags": {"add": ["a"]}},  # conflict
    ]

    result = await mcp_server.sage_bulk_update_metadata(
        vault_id=vault_id, items=items, response_mode="full"
    )

    assert result["results"][1]["status"] == "error"
    assert result["results"][1]["error"]["error"] == "tag_add_conflict"


async def test_t0153_t7_mixed_batch_in_light_mode(seeded_six_with_abstracts):
    """T7 — Mixed-result batch in light mode: success items have no
    `document`, error items have full `error`."""
    vault_id, seeded_ids = seeded_six_with_abstracts
    items = [
        {"document_id": seeded_ids[0], "tags": {"add": ["b"]}},
        {"document_id": seeded_ids[1], "tags": {"add": ["a"]}},  # conflict
        {"document_id": seeded_ids[2], "tags": {"add": ["b"]}},
    ]

    result = await mcp_server.sage_bulk_update_metadata(
        vault_id=vault_id, items=items, response_mode="light"
    )

    assert result["success_count"] == 2
    assert result["error_count"] == 1
    assert result["total"] == 3
    assert result["results"][0]["status"] == "success"
    assert "document" not in result["results"][0]
    assert result["results"][1]["status"] == "error"
    assert result["results"][1]["error"]["error"] == "tag_add_conflict"
    assert result["results"][2]["status"] == "success"
    assert "document" not in result["results"][2]


async def test_t0153_t8_invalid_response_mode_rejected_up_front(
    seeded_six_with_abstracts,
):
    """T8 — Invalid response_mode value is rejected up front; no per-item
    state changes."""
    vault_id, seeded_ids = seeded_six_with_abstracts
    items = [{"document_id": d, "tags": {"add": ["b"]}} for d in seeded_ids[:3]]

    result = await mcp_server.sage_bulk_update_metadata(
        vault_id=vault_id, items=items, response_mode="verbose"
    )

    assert "error" in result, f"expected validation error envelope, got {result!r}"
    assert result["error"] == "internal_error"
    # Anti-coincidental: no per-item state was committed. If validation
    # ran AFTER per-item processing, item 0 would have gained tag 'b'.
    services = mcp_server._vaults[vault_id]
    for doc_id in seeded_ids[:3]:
        stored = await services.graph_store.get_document(doc_id)
        assert stored.tags == ["a"], (
            f"invalid response_mode must abort batch BEFORE per-item work; "
            f"{doc_id} should retain seeded tags ['a'], got {stored.tags!r}"
        )


async def test_t0153_t9_empty_batch_with_explicit_light(seeded_six_with_abstracts):
    """T9 — Empty items with response_mode='light' returns empty results
    cleanly (no crash on the threshold check)."""
    vault_id, _ = seeded_six_with_abstracts

    result = await mcp_server.sage_bulk_update_metadata(
        vault_id=vault_id, items=[], response_mode="light"
    )

    assert "error" not in result
    assert result["results"] == []
    assert result["success_count"] == 0
    assert result["error_count"] == 0
    assert result["total"] == 0
