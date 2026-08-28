"""MCP tool tests for bulk_update_metadata.

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

    services.close_timing()
    await services.graph_store.close()


async def test_mcp_tool_round_trip_returns_dict_matching_response_model(seeded_mcp_vault):
    """The returned dict must deserialize cleanly as BulkMetadataResponse."""
    vault_id, seeded_ids = seeded_mcp_vault
    items = [{"document_id": d, "tags": {"add": ["b"]}} for d in seeded_ids]

    result = await mcp_server.update_metadata(vault_id=vault_id, items=items)

    assert isinstance(result, dict)
    assert "error" not in result, f"unexpected error envelope: {result!r}"
    response = BulkMetadataResponse.model_validate(result)
    assert response.total == 2
    assert response.success_count == 2


async def test_mcp_tool_invalid_vault_id_returns_error_envelope(empty_registry):
    """A vault_id that fails the VaultIdStr adapter surfaces the structured
    invalid_vault_id (400) envelope carrying the offending value."""
    result = await mcp_server.update_metadata(vault_id="not a vault id!", items=[])

    assert isinstance(result, dict)
    assert "error" in result
    assert result["error"] == "invalid_vault_id"
    assert result["detail"]["vault_id"] == "not a vault id!"


async def test_mcp_tool_unknown_vault_returns_error_envelope(empty_registry):
    """A syntactically valid but unregistered vault_id surfaces unknown_vault."""
    result = await mcp_server.update_metadata(vault_id="ghost", items=[])

    assert isinstance(result, dict)
    assert result.get("error") == "unknown_vault"


async def test_mcp_tool_missing_identifier_is_per_item_error(seeded_mcp_vault):
    """An item supplying neither ``document_id`` nor ``doc_id`` yields a
    per-item ``missing_document_identifier`` envelope; the sibling item
    still processes (CAS-ADR-029 partial success).

    Replaces the prior up-front whole-batch rejection: id-presence is now
    resolved per item, not at shape-validation time. Genuine shape errors
    (bad ``tags``/``tier3`` form, unknown keys) still reject the whole
    batch up front -- see the legacy-form tests below."""
    vault_id, seeded_ids = seeded_mcp_vault
    items = [
        {"document_id": seeded_ids[0], "tags": {"add": ["b"]}},
        {"tags": {"add": ["b"]}},  # neither document_id nor doc_id
    ]

    result = await mcp_server.update_metadata(vault_id=vault_id, items=items)

    assert "error" not in result, (
        f"missing-id must be a per-item envelope, not a whole-call error: {result!r}"
    )
    response = BulkMetadataResponse.model_validate(result)
    assert response.total == 2
    assert response.success_count == 1
    assert response.results[0].status == "success"
    assert response.results[1].status == "error"
    assert response.results[1].error["error"] == "missing_document_identifier"
    assert set(response.results[1].error["detail"]["accepted"]) == {"document_id", "doc_id"}

    # Anti-coincidental-pass: the valid sibling DID commit -- a missing-id
    # item no longer aborts the batch.
    services = mcp_server._vaults[vault_id]
    stored = await services.graph_store.get_document(seeded_ids[0])
    assert set(stored.tags) == {"a", "b"}, (
        "Per-item resolution must let the well-formed sibling commit; only "
        "the neither-identifier item errors."
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

    result = await mcp_server.update_metadata(vault_id=vault_id, items=items)

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

    result = await mcp_server.update_metadata(vault_id=vault_id, items=items)

    assert isinstance(result, dict)
    assert result.get("error") == "legacy_form", (
        f"expected legacy_form error envelope, got {result!r}"
    )


# ---------------------------------------------------------------------------
# Response_mode parameter on bulk mutation tools
# ---------------------------------------------------------------------------

_T0153_ABSTRACT = "Test abstract used as the bulk-mode bloat probe (T-0153)."


@pytest.fixture
async def seeded_six_with_abstracts(minimal_vault_config_dict, monkeypatch, empty_registry):
    """fixture. Boot a vault, seed six active documents each
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
    items = [{"document_id": d, "tags": {"add": ["b"]}} for d in seeded_ids[:3]]

    result = await mcp_server.update_metadata(vault_id=vault_id, items=items, response_mode="light")

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


async def test_full_preserves_document_with_semantic_abstract(
    seeded_six_with_abstracts,
):
    """T2 — Explicit response_mode='full' with a 3-item all-success batch
    preserves the current per-item shape, including semantic_abstract."""
    vault_id, seeded_ids = seeded_six_with_abstracts
    items = [{"document_id": d, "tags": {"add": ["b"]}} for d in seeded_ids[:3]]

    result = await mcp_server.update_metadata(vault_id=vault_id, items=items, response_mode="full")

    assert "error" not in result, f"unexpected error envelope: {result!r}"
    assert result["success_count"] == 3
    for entry in result["results"]:
        assert entry["status"] == "success"
        # Anti-coincidental: assert the specific cited bloat field, not
        # just truthiness of `document`.
        assert entry["document"]["semantic_abstract"] == _T0153_ABSTRACT


async def test_default_above_threshold_returns_light(
    seeded_six_with_abstracts,
):
    """T3 — Default mode with a 6-item batch returns light. Critical
    anti-coincidental: `response_mode` is NOT passed; tests the default-
    resolution branch."""
    vault_id, seeded_ids = seeded_six_with_abstracts
    items = [{"document_id": d, "tags": {"add": ["b"]}} for d in seeded_ids[:6]]

    result = await mcp_server.update_metadata(vault_id=vault_id, items=items)

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
    full."""
    vault_id, seeded_ids = seeded_six_with_abstracts
    items = [{"document_id": d, "tags": {"add": ["b"]}} for d in seeded_ids[:3]]

    result = await mcp_server.update_metadata(vault_id=vault_id, items=items)

    assert "error" not in result, f"unexpected error envelope: {result!r}"
    assert result["success_count"] == 3
    for entry in result["results"]:
        assert entry["document"]["semantic_abstract"] == _T0153_ABSTRACT


async def test_error_envelope_intact_in_light_mode(
    seeded_six_with_abstracts,
):
    """T5 — An error item in light mode keeps the full error envelope,
    including the structured `detail` payload (tags_add_conflict carries
    `current_tags` in detail). Round-trip against the same shape in full
    mode; envelopes must be byte-identical."""
    vault_id, seeded_ids = seeded_six_with_abstracts
    # 'a' is already present on seeded docs, so add of 'a' raises
    # tags_add_conflict with detail={'current_tags': ['a']}. Use a
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

    light_result = await mcp_server.update_metadata(
        vault_id=vault_id, items=light_items, response_mode="light"
    )
    full_result = await mcp_server.update_metadata(
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
    assert light_err["error"]["error"] == "tags_add_conflict"
    assert "detail" in light_err["error"]


async def test_error_envelope_intact_in_full_mode(
    seeded_six_with_abstracts,
):
    """T6 — Error item in full mode (regression guard)."""
    vault_id, seeded_ids = seeded_six_with_abstracts
    items = [
        {"document_id": seeded_ids[0], "tags": {"add": ["b"]}},
        {"document_id": seeded_ids[1], "tags": {"add": ["a"]}},  # conflict
    ]

    result = await mcp_server.update_metadata(vault_id=vault_id, items=items, response_mode="full")

    assert result["results"][1]["status"] == "error"
    assert result["results"][1]["error"]["error"] == "tags_add_conflict"


async def test_mixed_batch_in_light_mode(seeded_six_with_abstracts):
    """T7 — Mixed-result batch in light mode: success items have no
    `document`, error items have full `error`."""
    vault_id, seeded_ids = seeded_six_with_abstracts
    items = [
        {"document_id": seeded_ids[0], "tags": {"add": ["b"]}},
        {"document_id": seeded_ids[1], "tags": {"add": ["a"]}},  # conflict
        {"document_id": seeded_ids[2], "tags": {"add": ["b"]}},
    ]

    result = await mcp_server.update_metadata(vault_id=vault_id, items=items, response_mode="light")

    assert result["success_count"] == 2
    assert result["error_count"] == 1
    assert result["total"] == 3
    assert result["results"][0]["status"] == "success"
    assert "document" not in result["results"][0]
    assert result["results"][1]["status"] == "error"
    assert result["results"][1]["error"]["error"] == "tags_add_conflict"
    assert result["results"][2]["status"] == "success"
    assert "document" not in result["results"][2]


async def test_invalid_response_mode_rejected_up_front(
    seeded_six_with_abstracts,
):
    """T8 — Invalid response_mode value is rejected up front; no per-item
    state changes."""
    vault_id, seeded_ids = seeded_six_with_abstracts
    items = [{"document_id": d, "tags": {"add": ["b"]}} for d in seeded_ids[:3]]

    result = await mcp_server.update_metadata(
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


async def test_empty_batch_with_explicit_light(seeded_six_with_abstracts):
    """T9 — Empty items with response_mode='light' returns empty results
    cleanly (no crash on the threshold check)."""
    vault_id, _ = seeded_six_with_abstracts

    result = await mcp_server.update_metadata(vault_id=vault_id, items=[], response_mode="light")

    assert "error" not in result
    assert result["results"] == []
    assert result["success_count"] == 0
    assert result["error_count"] == 0
    assert result["total"] == 0


# ---------------------------------------------------------------------------
# Per-item doc_id alias (extends the read-tool document_id/doc_id alias to
# the bulk write shapes)
# ---------------------------------------------------------------------------


async def test_doc_id_alias_resolves_and_mutates_right_document(seeded_mcp_vault):
    """M1 -- A per-item ``doc_id`` alias resolves to the same document as
    ``document_id`` and the patch actually commits to that document.

    Anti-coincidental-pass: asserts the stored tag set changed AND the
    echoed ``document_id`` matches. A status-only check would pass even if
    ``doc_id`` were accepted by the model but never resolved (the write
    silently skipped, or applied to the wrong document)."""
    vault_id, seeded_ids = seeded_mcp_vault
    target = seeded_ids[0]

    result = await mcp_server.update_metadata(
        vault_id=vault_id, items=[{"doc_id": target, "tags": {"add": ["b"]}}]
    )

    assert "error" not in result, f"unexpected error envelope: {result!r}"
    response = BulkMetadataResponse.model_validate(result)
    assert response.total == 1
    assert response.success_count == 1
    assert response.results[0].status == "success"
    assert response.results[0].document_id == target
    services = mcp_server._vaults[vault_id]
    stored = await services.graph_store.get_document(target)
    assert set(stored.tags) == {"a", "b"}, (
        "doc_id-only item must resolve and commit the patch to the right document"
    )


async def test_document_id_only_still_resolves_and_mutates(seeded_mcp_vault):
    """M2 -- The canonical ``document_id`` form is unchanged: it resolves
    and commits (back-compat guard against the alias work regressing the
    primary path)."""
    vault_id, seeded_ids = seeded_mcp_vault
    target = seeded_ids[0]

    result = await mcp_server.update_metadata(
        vault_id=vault_id, items=[{"document_id": target, "tags": {"add": ["b"]}}]
    )

    assert "error" not in result, f"unexpected error envelope: {result!r}"
    response = BulkMetadataResponse.model_validate(result)
    assert response.results[0].status == "success"
    assert response.results[0].document_id == target
    services = mcp_server._vaults[vault_id]
    stored = await services.graph_store.get_document(target)
    assert set(stored.tags) == {"a", "b"}


async def test_both_identifiers_equal_is_per_item_ambiguous(seeded_mcp_vault):
    """M3 -- Supplying both ``document_id`` and ``doc_id`` -- even with
    equal values -- is a per-item ``ambiguous_document_identifier`` error,
    and the document is NOT mutated.

    Anti-coincidental-pass: the no-mutation assertion catches a resolver
    that silently prefers ``document_id`` when both are present (skipping
    the ambiguity branch and committing the patch)."""
    vault_id, seeded_ids = seeded_mcp_vault
    target = seeded_ids[0]

    result = await mcp_server.update_metadata(
        vault_id=vault_id,
        items=[{"document_id": target, "doc_id": target, "tags": {"add": ["b"]}}],
    )

    assert "error" not in result, f"resolution must be per-item, not a whole-call error: {result!r}"
    response = BulkMetadataResponse.model_validate(result)
    assert response.results[0].status == "error"
    assert response.results[0].error["error"] == "ambiguous_document_identifier"
    assert set(response.results[0].error["detail"]["supplied"]) == {"document_id", "doc_id"}
    services = mcp_server._vaults[vault_id]
    stored = await services.graph_store.get_document(target)
    assert stored.tags == ["a"], "ambiguous item must not mutate the document"


async def test_both_identifiers_unequal_is_per_item_ambiguous(seeded_mcp_vault):
    """M4 -- Two different ids via the canonical name and the alias is also
    ambiguous; neither document is mutated."""
    vault_id, seeded_ids = seeded_mcp_vault
    d1, d2 = seeded_ids

    result = await mcp_server.update_metadata(
        vault_id=vault_id,
        items=[{"document_id": d1, "doc_id": d2, "tags": {"add": ["b"]}}],
    )

    assert "error" not in result, f"resolution must be per-item: {result!r}"
    response = BulkMetadataResponse.model_validate(result)
    assert response.results[0].status == "error"
    assert response.results[0].error["error"] == "ambiguous_document_identifier"
    services = mcp_server._vaults[vault_id]
    assert (await services.graph_store.get_document(d1)).tags == ["a"]
    assert (await services.graph_store.get_document(d2)).tags == ["a"]


async def test_neither_identifier_is_per_item_missing(seeded_mcp_vault):
    """M5 -- An item supplying no identifier is a per-item
    ``missing_document_identifier`` error whose echoed ``document_id`` is
    null (there is no id to echo).

    Anti-coincidental-pass: asserting the structured per-item code (not a
    generic whole-call ValidationError) catches the prior behavior where a
    required ``document_id`` failed up front and aborted the batch."""
    vault_id, _ = seeded_mcp_vault

    result = await mcp_server.update_metadata(vault_id=vault_id, items=[{"tags": {"add": ["b"]}}])

    assert "error" not in result, (
        f"missing-id must be a per-item envelope, not a whole-call error: {result!r}"
    )
    response = BulkMetadataResponse.model_validate(result)
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
        {"doc_id": d1, "tags": {"add": ["b"]}},  # doc_id-only -> success
        {"document_id": d2, "tags": {"add": ["b"]}},  # document_id-only -> success
        {"document_id": d1, "doc_id": d1, "tags": {"add": ["c"]}},  # both -> ambiguous
        {"tags": {"add": ["x"]}},  # neither -> missing
    ]

    result = await mcp_server.update_metadata(vault_id=vault_id, items=items)

    assert "error" not in result, f"unexpected whole-call error: {result!r}"
    response = BulkMetadataResponse.model_validate(result)
    assert response.total == 4
    assert response.success_count == 2
    assert response.error_count == 2
    assert [r.status for r in response.results] == ["success", "success", "error", "error"]
    assert response.results[2].error["error"] == "ambiguous_document_identifier"
    assert response.results[3].error["error"] == "missing_document_identifier"
    services = mcp_server._vaults[vault_id]
    stored_d1 = await services.graph_store.get_document(d1)
    stored_d2 = await services.graph_store.get_document(d2)
    assert set(stored_d1.tags) == {"a", "b"}, (
        "d1 got 'b' from the doc_id-only item; the ambiguous item's 'c' must NOT commit"
    )
    assert set(stored_d2.tags) == {"a", "b"}


def test_update_metadata_docstring_documents_doc_id_alias():
    """M7 -- The tool docstring documents ``doc_id`` as a per-item alias
    for ``document_id``. Anchored to the same line as ``document_id`` to
    defeat a loose ``"doc_id" in doc`` coincidental pass."""
    import re
    import textwrap

    doc = mcp_server.update_metadata.__doc__
    assert doc is not None
    dedented = textwrap.dedent(doc)
    assert re.search(r"document_id[^\n]*doc_id", dedented), (
        "update_metadata docstring must document `doc_id` as a per-item alias for `document_id`"
    )
