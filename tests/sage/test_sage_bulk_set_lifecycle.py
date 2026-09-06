"""MCP tool tests for bulk_update_lifecycle.

Exercises the boundary contract: vault_id and per-item shape validation,
registry membership check, and round-trip of the BulkLifecycleResponse
payload through the MCP serialize path.
"""

from __future__ import annotations

import pytest

from sage import mcp_server
from sage.adapters.stubs import StubContentStore
from sage.app import _initialize_services, create_app
from sage.config import VaultConfig
from sage.models.schemas import BulkLifecycleResponse
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
    """Boot a vault, seed two documents, publish on mcp_server._vaults."""
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

    yield vault_id, seeded_ids

    services.close_timing()
    await services.graph_store.close()


async def test_mcp_tool_round_trip_returns_dict_matching_response_model(seeded_mcp_vault):
    """The returned dict must deserialize cleanly as BulkLifecycleResponse."""
    vault_id, seeded_ids = seeded_mcp_vault
    items = [{"document_id": d, "action": "archive"} for d in seeded_ids]

    result = await mcp_server.update_lifecycles(vault_id=vault_id, items=items)

    assert isinstance(result, dict)
    assert "error" not in result, f"unexpected error envelope: {result!r}"
    response = BulkLifecycleResponse.model_validate(result)
    assert response.total == 2
    assert response.success_count == 2


async def test_mcp_tool_invalid_vault_id_returns_error_envelope(empty_registry):
    """A vault_id that fails the VaultIdStr adapter surfaces the structured
    invalid_vault_id (400) envelope carrying the offending value."""
    result = await mcp_server.update_lifecycles(vault_id="not a vault id!", items=[])

    assert isinstance(result, dict)
    assert "error" in result
    assert result["error"] == "invalid_vault_id"
    assert result["detail"]["vault_id"] == "not a vault id!"


async def test_mcp_tool_unknown_vault_returns_error_envelope(empty_registry):
    """A syntactically valid but unregistered vault_id surfaces unknown_vault."""
    result = await mcp_server.update_lifecycles(vault_id="ghost", items=[])

    assert isinstance(result, dict)
    assert result.get("error") == "unknown_vault"


async def test_mcp_tool_missing_identifier_is_per_item_error(seeded_mcp_vault):
    """An item supplying neither ``document_id`` nor ``doc_id`` yields a
    per-item ``missing_document_identifier`` envelope; the sibling item
    still processes (CAS-ADR-029 partial success).

    Replaces the prior up-front whole-batch rejection: id-presence is now
    resolved per item, not at shape-validation time."""
    vault_id, seeded_ids = seeded_mcp_vault
    items = [
        {"document_id": seeded_ids[0], "action": "archive"},
        {"action": "archive"},  # neither document_id nor doc_id
    ]

    result = await mcp_server.update_lifecycles(vault_id=vault_id, items=items)

    assert "error" not in result, (
        f"missing-id must be a per-item envelope, not a whole-call error: {result!r}"
    )
    response = BulkLifecycleResponse.model_validate(result)
    assert response.total == 2
    assert response.success_count == 1
    assert response.results[0].status == "success"
    assert response.results[1].status == "error"
    assert response.results[1].error["error"] == "missing_document_identifier"
    assert set(response.results[1].error["detail"]["accepted"]) == {"document_id", "doc_id"}

    # Anti-coincidental-pass: the valid sibling DID transition -- a
    # missing-id item no longer aborts the batch.
    services = mcp_server._vaults[vault_id]
    stored = await services.graph_store.get_document(seeded_ids[0])
    assert stored.lifecycle_status == "archived", (
        "Per-item resolution must let the well-formed sibling commit; only "
        "the neither-identifier item errors."
    )


# ---------------------------------------------------------------------------
# Response_mode parameter on bulk mutation tools
# ---------------------------------------------------------------------------

_BLOAT_PROBE_ABSTRACT = "Test abstract used as the bulk-mode bloat probe."


@pytest.fixture
async def seeded_six_with_abstracts(minimal_vault_config_dict, monkeypatch, empty_registry):
    """Fixture. Boot a vault, seed six active documents each with
    a populated `semantic_abstract`, publish on mcp_server._vaults.

    Six items so the threshold-default tests can cross the >5 boundary
    in either direction by slicing. semantic_abstract is the field the
    field-use report cited as the primary bloat source; tests assert on
    its presence (full mode) and absence (light mode)."""
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
            doc_id, {"semantic_abstract": _BLOAT_PROBE_ABSTRACT}
        )

    yield vault_id, seeded_ids

    services.close_timing()
    await services.graph_store.close()


async def test_light_strips_document_and_semantic_abstract(
    seeded_six_with_abstracts,
):
    """T1 — Explicit response_mode='light' with a 3-item all-success batch
    drops the per-item `document` field entirely. Batch size 3 is below
    the >5 threshold so this demonstrates the explicit-override path,
    not a default-triggered one."""
    vault_id, seeded_ids = seeded_six_with_abstracts
    items = [{"document_id": d, "action": "archive"} for d in seeded_ids[:3]]

    result = await mcp_server.update_lifecycles(
        vault_id=vault_id, items=items, response_mode="light"
    )

    assert "error" not in result, f"unexpected error envelope: {result!r}"
    assert result["success_count"] == 3
    for entry in result["results"]:
        assert entry["status"] == "success"
        # Anti-coincidental: assert on raw dict shape, not via
        # BulkLifecycleResponse.model_validate(...).results[i].document is None.
        # MCP's _serialize uses exclude_none=True, so a None `document`
        # is stripped from the wire payload entirely.
        assert "document" not in entry, (
            f"light mode must strip the per-item `document` field; got {entry!r}"
        )
    assert _BLOAT_PROBE_ABSTRACT not in str(result), (
        "light mode must not leak the semantic_abstract probe string"
    )


async def test_full_preserves_document_with_semantic_abstract(
    seeded_six_with_abstracts,
):
    """T2 — Explicit response_mode='full' with a 3-item all-success batch
    preserves the current per-item shape, including semantic_abstract
    (the specific field cited in the field-use report)."""
    vault_id, seeded_ids = seeded_six_with_abstracts
    items = [{"document_id": d, "action": "archive"} for d in seeded_ids[:3]]

    result = await mcp_server.update_lifecycles(
        vault_id=vault_id, items=items, response_mode="full"
    )

    assert "error" not in result, f"unexpected error envelope: {result!r}"
    assert result["success_count"] == 3
    for entry in result["results"]:
        assert entry["status"] == "success"
        # Anti-coincidental: assert the specific cited bloat field, not
        # just truthiness of `document`.
        assert entry["document"]["semantic_abstract"] == _BLOAT_PROBE_ABSTRACT


async def test_default_above_threshold_returns_light(
    seeded_six_with_abstracts,
):
    """T3 — Default mode (no response_mode arg) with a 6-item batch
    (one above the >5 threshold) returns light. Critical anti-
    coincidental: `response_mode` is NOT passed; this exercises the
    default-resolution branch."""
    vault_id, seeded_ids = seeded_six_with_abstracts
    items = [{"document_id": d, "action": "archive"} for d in seeded_ids[:6]]

    result = await mcp_server.update_lifecycles(vault_id=vault_id, items=items)

    assert "error" not in result, f"unexpected error envelope: {result!r}"
    assert result["success_count"] == 6
    for entry in result["results"]:
        assert "document" not in entry, (
            f"default above threshold (6>5) must be light; got {entry!r}"
        )


async def test_default_at_or_below_threshold_returns_full(
    seeded_six_with_abstracts,
):
    """T4 — Default mode with a 3-item batch (at-or-below threshold) is
    full. If the threshold comparison is `>=` instead of `>`, the
    boundary case shifts and behaviour at len==5 silently flips."""
    vault_id, seeded_ids = seeded_six_with_abstracts
    items = [{"document_id": d, "action": "archive"} for d in seeded_ids[:3]]

    result = await mcp_server.update_lifecycles(vault_id=vault_id, items=items)

    assert "error" not in result, f"unexpected error envelope: {result!r}"
    assert result["success_count"] == 3
    for entry in result["results"]:
        assert entry["document"]["semantic_abstract"] == _BLOAT_PROBE_ABSTRACT


async def test_error_envelope_intact_in_light_mode(
    seeded_six_with_abstracts,
):
    """T5 — An error item in light mode keeps the full error envelope.
    Round-trip against the same call in full mode; the envelope must be
    byte-identical so light does not strip actionable error structure."""
    vault_id, seeded_ids = seeded_six_with_abstracts
    ghost_id = _id("doc_ghost_t0153_t5")
    # Use different success-item indexes per call so the two calls do
    # not need state reset between them (each archives a different doc).
    light_items = [
        {"document_id": seeded_ids[0], "action": "archive"},
        {"document_id": ghost_id, "action": "archive"},
    ]
    full_items = [
        {"document_id": seeded_ids[1], "action": "archive"},
        {"document_id": ghost_id, "action": "archive"},
    ]

    light_result = await mcp_server.update_lifecycles(
        vault_id=vault_id, items=light_items, response_mode="light"
    )
    full_result = await mcp_server.update_lifecycles(
        vault_id=vault_id, items=full_items, response_mode="full"
    )

    light_err = light_result["results"][1]
    full_err = full_result["results"][1]
    assert light_err["status"] == "error"
    assert full_err["status"] == "error"
    # Anti-coincidental: envelope must round-trip identically. `==` rather
    # than just "key exists" so a silent detail-stripping bug fails.
    assert light_err["error"] == full_err["error"], (
        f"light mode must not strip error envelope; light={light_err!r} full={full_err!r}"
    )


async def test_error_envelope_intact_in_full_mode(
    seeded_six_with_abstracts,
):
    """T6 — Error item in full mode (regression guard). The error
    envelope continues to carry the SAGEError code unchanged from
    today's behaviour."""
    vault_id, seeded_ids = seeded_six_with_abstracts
    ghost_id = _id("doc_ghost_t0153_t6")
    items = [
        {"document_id": seeded_ids[0], "action": "archive"},
        {"document_id": ghost_id, "action": "archive"},
    ]

    result = await mcp_server.update_lifecycles(
        vault_id=vault_id, items=items, response_mode="full"
    )

    assert result["results"][1]["status"] == "error"
    assert result["results"][1]["error"]["error"] == "document_not_found"
    assert "message" in result["results"][1]["error"]


async def test_mixed_batch_in_light_mode(seeded_six_with_abstracts):
    """T7 — Mixed-result batch in light mode: success items have no
    `document`, error items have full `error`. Catches a per-item-loop
    bug where mode is applied to only the first item or the wrong
    branch."""
    vault_id, seeded_ids = seeded_six_with_abstracts
    ghost_id = _id("doc_ghost_t0153_t7")
    items = [
        {"document_id": seeded_ids[0], "action": "archive"},
        {"document_id": ghost_id, "action": "archive"},
        {"document_id": seeded_ids[1], "action": "archive"},
    ]

    result = await mcp_server.update_lifecycles(
        vault_id=vault_id, items=items, response_mode="light"
    )

    assert result["success_count"] == 2
    assert result["error_count"] == 1
    assert result["total"] == 3
    assert result["results"][0]["status"] == "success"
    assert "document" not in result["results"][0]
    assert result["results"][1]["status"] == "error"
    assert result["results"][1]["error"]["error"] == "document_not_found"
    assert result["results"][2]["status"] == "success"
    assert "document" not in result["results"][2]


async def test_invalid_response_mode_rejected_up_front(
    seeded_six_with_abstracts,
):
    """T8 — Invalid response_mode value is rejected up front before any
    per-item processing. State must NOT change for items that would
    have otherwise succeeded."""
    vault_id, seeded_ids = seeded_six_with_abstracts
    items = [{"document_id": d, "action": "archive"} for d in seeded_ids[:3]]

    result = await mcp_server.update_lifecycles(
        vault_id=vault_id, items=items, response_mode="verbose"
    )

    assert "error" in result, f"expected validation error envelope, got {result!r}"
    assert result["error"] == "invalid_parameter"
    assert result["detail"]["parameter"].endswith("response_mode")
    # Anti-coincidental: no per-item state was committed. If validation
    # ran AFTER per-item processing, item 0 would have archived.
    services = mcp_server._vaults[vault_id]
    for doc_id in seeded_ids[:3]:
        stored = await services.graph_store.get_document(doc_id)
        assert stored.lifecycle_status == "active", (
            f"invalid response_mode must abort batch BEFORE per-item work; "
            f"{doc_id} should still be active"
        )


async def test_empty_batch_with_explicit_light(seeded_six_with_abstracts):
    """T9 — Empty items with response_mode='light' returns empty results
    cleanly (0 ≤ threshold so default would pick full, but explicit
    light must still parse cleanly)."""
    vault_id, _ = seeded_six_with_abstracts

    result = await mcp_server.update_lifecycles(vault_id=vault_id, items=[], response_mode="light")

    assert "error" not in result
    assert result["results"] == []
    assert result["success_count"] == 0
    assert result["error_count"] == 0
    assert result["total"] == 0


# ---------------------------------------------------------------------------
# Per-item doc_id alias (extends the read-tool document_id/doc_id alias to
# the bulk write shapes)
# ---------------------------------------------------------------------------


async def test_doc_id_alias_resolves_and_transitions_right_document(seeded_mcp_vault):
    """M1 -- A per-item ``doc_id`` alias resolves to the same document as
    ``document_id`` and the transition actually commits to that document.

    Anti-coincidental-pass: asserts the stored ``lifecycle_status`` flipped
    AND the echoed ``document_id`` matches -- a status-only check would
    pass even if ``doc_id`` were accepted but never resolved."""
    vault_id, seeded_ids = seeded_mcp_vault
    target = seeded_ids[0]

    result = await mcp_server.update_lifecycles(
        vault_id=vault_id, items=[{"doc_id": target, "action": "archive"}]
    )

    assert "error" not in result, f"unexpected error envelope: {result!r}"
    response = BulkLifecycleResponse.model_validate(result)
    assert response.total == 1
    assert response.success_count == 1
    assert response.results[0].status == "success"
    assert response.results[0].document_id == target
    services = mcp_server._vaults[vault_id]
    stored = await services.graph_store.get_document(target)
    assert stored.lifecycle_status == "archived", (
        "doc_id-only item must resolve and commit the transition to the right document"
    )


async def test_document_id_only_still_resolves_and_transitions(seeded_mcp_vault):
    """M2 -- The canonical ``document_id`` form is unchanged: it resolves
    and commits (back-compat guard)."""
    vault_id, seeded_ids = seeded_mcp_vault
    target = seeded_ids[0]

    result = await mcp_server.update_lifecycles(
        vault_id=vault_id, items=[{"document_id": target, "action": "archive"}]
    )

    assert "error" not in result, f"unexpected error envelope: {result!r}"
    response = BulkLifecycleResponse.model_validate(result)
    assert response.results[0].status == "success"
    assert response.results[0].document_id == target
    services = mcp_server._vaults[vault_id]
    stored = await services.graph_store.get_document(target)
    assert stored.lifecycle_status == "archived"


async def test_both_identifiers_equal_is_per_item_ambiguous(seeded_mcp_vault):
    """M3 -- Supplying both ``document_id`` and ``doc_id`` -- even equal --
    is a per-item ``ambiguous_document_identifier`` error, and the document
    does NOT transition.

    Anti-coincidental-pass: the no-transition assertion catches a resolver
    that silently prefers ``document_id`` when both are present."""
    vault_id, seeded_ids = seeded_mcp_vault
    target = seeded_ids[0]

    result = await mcp_server.update_lifecycles(
        vault_id=vault_id,
        items=[{"document_id": target, "doc_id": target, "action": "archive"}],
    )

    assert "error" not in result, f"resolution must be per-item, not a whole-call error: {result!r}"
    response = BulkLifecycleResponse.model_validate(result)
    assert response.results[0].status == "error"
    assert response.results[0].error["error"] == "ambiguous_document_identifier"
    assert set(response.results[0].error["detail"]["supplied"]) == {"document_id", "doc_id"}
    services = mcp_server._vaults[vault_id]
    stored = await services.graph_store.get_document(target)
    assert stored.lifecycle_status == "active", "ambiguous item must not transition the document"


async def test_both_identifiers_unequal_is_per_item_ambiguous(seeded_mcp_vault):
    """M4 -- Two different ids via the canonical name and the alias is also
    ambiguous; neither document transitions."""
    vault_id, seeded_ids = seeded_mcp_vault
    d1, d2 = seeded_ids

    result = await mcp_server.update_lifecycles(
        vault_id=vault_id,
        items=[{"document_id": d1, "doc_id": d2, "action": "archive"}],
    )

    assert "error" not in result, f"resolution must be per-item: {result!r}"
    response = BulkLifecycleResponse.model_validate(result)
    assert response.results[0].status == "error"
    assert response.results[0].error["error"] == "ambiguous_document_identifier"
    services = mcp_server._vaults[vault_id]
    assert (await services.graph_store.get_document(d1)).lifecycle_status == "active"
    assert (await services.graph_store.get_document(d2)).lifecycle_status == "active"


async def test_neither_identifier_is_per_item_missing(seeded_mcp_vault):
    """M5 -- An item supplying no identifier is a per-item
    ``missing_document_identifier`` error whose echoed ``document_id`` is
    null.

    Anti-coincidental-pass: asserting the structured per-item code (not a
    whole-call ValidationError) catches the prior up-front-rejection
    behavior."""
    vault_id, _ = seeded_mcp_vault

    result = await mcp_server.update_lifecycles(vault_id=vault_id, items=[{"action": "archive"}])

    assert "error" not in result, (
        f"missing-id must be a per-item envelope, not a whole-call error: {result!r}"
    )
    response = BulkLifecycleResponse.model_validate(result)
    assert response.results[0].status == "error"
    assert response.results[0].error["error"] == "missing_document_identifier"
    assert set(response.results[0].error["detail"]["accepted"]) == {"document_id", "doc_id"}
    assert response.results[0].document_id is None


async def test_mixed_identifier_forms_resolve_independently(seeded_mcp_vault):
    """M6 -- A batch mixing doc_id-only, document_id-only, both, and neither
    resolves each item independently: the two valid forms commit, the
    ambiguous and missing items error, and one bad item does NOT abort the
    siblings (CAS-ADR-029 partial success)."""
    vault_id, seeded_ids = seeded_mcp_vault
    d1, d2 = seeded_ids
    items = [
        {"doc_id": d1, "action": "archive"},  # doc_id-only -> success
        {"document_id": d2, "action": "archive"},  # document_id-only -> success
        {"document_id": d1, "doc_id": d1, "action": "archive"},  # both -> ambiguous
        {"action": "archive"},  # neither -> missing
    ]

    result = await mcp_server.update_lifecycles(vault_id=vault_id, items=items)

    assert "error" not in result, f"unexpected whole-call error: {result!r}"
    response = BulkLifecycleResponse.model_validate(result)
    assert response.total == 4
    assert response.success_count == 2
    assert response.error_count == 2
    assert [r.status for r in response.results] == ["success", "success", "error", "error"]
    assert response.results[2].error["error"] == "ambiguous_document_identifier"
    assert response.results[3].error["error"] == "missing_document_identifier"
    services = mcp_server._vaults[vault_id]
    assert (await services.graph_store.get_document(d1)).lifecycle_status == "archived"
    assert (await services.graph_store.get_document(d2)).lifecycle_status == "archived"


def test_update_lifecycles_docstring_documents_doc_id_alias():
    """M7 -- The tool docstring documents ``doc_id`` as a per-item alias
    for ``document_id``. Anchored to the same line as ``document_id`` to
    defeat a loose ``"doc_id" in doc`` coincidental pass."""
    import re
    import textwrap

    doc = mcp_server.update_lifecycles.__doc__
    assert doc is not None
    dedented = textwrap.dedent(doc)
    assert re.search(r"document_id[^\n]*doc_id", dedented), (
        "update_lifecycles docstring must document `doc_id` as a per-item alias for `document_id`"
    )
