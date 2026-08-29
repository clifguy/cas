"""Misplaced top-level filter keys on ``search``.

``search`` takes its scope constraints nested under ``filters={...}``. A
caller who spells one of those keys at the top level instead
(``doc_type="adr"``) previously had it discarded in silence: the MCP
client coerces arguments to the published schema and strips unknown
properties before dispatch, so the ``extra="forbid"`` framework guard
(CAS-ADR-037, ``sage._fastmcp_strict_args``) never saw them, and neither
did ``RetrievalFilters``. The call then succeeded against an *unfiltered*
vault and returned plausible rows with no signal that the constraint had
been dropped.

Publishing the flat spellings is therefore a precondition for reacting to
them at all -- an unpublished parameter cannot be rejected, only dropped.
Once published, a body-level guard raises the structured
``misplaced_filters`` envelope, the read-side sibling of the
``misplaced_metadata`` guard on ``ingest_document``.

Transport matters, in two distinct ways.

Tests that exercise the guard route through ``mcp.call_tool`` rather than
calling the tool function in process: the in-process path bypasses
FastMCP's per-tool argument model entirely, so an in-process-only test
would pass whether or not the parameters are actually published --
leaving the real client-side stripping fully live behind a green suite.

``mcp.call_tool`` is still only the *server* half. It receives the stray
kwarg and refuses it; a real client never gets that far, having stripped
the field first. ``test_client_schema_coercion_no_longer_strips_filter_keys``
closes that gap by replaying the client's coercion step against the
published schema before dispatch, which is the only shape in this module
that reproduces the original silent-wrong-data symptom.
"""

from __future__ import annotations

import asyncio
import inspect
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
from sage.mcp_server import ingest_document, mcp, search
from sage.models.schemas import RetrievalFilters
from sage.sage_api_tools import _SEARCH_FILTER_KEYS
from tests.sage.conftest import initialize_services_for_test

# Representative wrong-level values, one per protected key. Shapes vary
# deliberately -- scalar, list, and dict -- to exercise the permissive
# tripwire annotation: any well-formed value in the wrong place must earn
# the misplaced-field message rather than a shape complaint.
MISPLACED_VALUES: dict[str, object] = {
    "doc_type": "note",
    "project": "CAS",
    "lifecycle_status": "active",
    "tags": ["alpha", "beta"],
    "document_ids": ["some_document_id"],
    "pipeline_status": "abstraction_complete",
    "source_type": "markdown",
    "tier3_metadata": {"ticket_id": "T-0001"},
    "source_id": "some_document_id",
    "target_id": "another_document_id",
    "edge_type": "references",
}


@pytest.fixture
async def vault_services(minimal_vault_config_dict, tmp_vault_dir):
    """Initialize SAGE services, register them, and seed two typed documents.

    Mirrors the fixture in
    ``tests/sage/test_ingest_misplaced_metadata.py``; duplicated so this
    transport-level module stays self-contained. Registers under
    ``vault_id="test_vault"`` so the ``get_vault`` lookup inside each MCP
    tool resolves to the stub-backed services.

    The two seeded documents carry *different* doc_types on purpose: a
    correctly filtered result and an unfiltered one are otherwise
    indistinguishable, and telling them apart is the whole point of
    ``test_client_schema_coercion_no_longer_strips_filter_keys``.
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
        (test_dir / "second.md").write_text("# Second Document\n\nOther content.")

        await ingest_document(
            "test_vault",
            "test/sample.md",
            "markdown",
            metadata={"title": "A Note", "doc_type": "note"},
        )
        await ingest_document(
            "test_vault",
            "test/second.md",
            "markdown",
            metadata={"title": "A Memo", "doc_type": "memo"},
        )

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
    """Extract the SAGE envelope dict from a ``[TextContent]`` ``mcp.call_tool`` return."""
    assert isinstance(result, list), f"Expected list result; got {type(result)}"
    assert len(result) == 1, f"Expected single TextContent; got {len(result)}"
    block = result[0]
    assert isinstance(block, TextContent), f"Expected TextContent; got {type(block)}"
    return json.loads(block.text)


def _published_properties() -> dict:
    """Return the published JSON-schema properties of the ``search`` tool."""
    tool = mcp._tool_manager.get_tool("search")  # noqa: SLF001
    return tool.parameters.get("properties", {})


# ---------------------------------------------------------------------------
# Published schema
# ---------------------------------------------------------------------------


def test_filter_keys_are_published_in_the_tool_schema():
    """Every protected filter key is published as an optional top-level property.

    This is the assertion that pins the root-cause fix. A client's
    ``additionalProperties: false`` coercion strips properties absent
    from the published schema, so a server-side guard over an
    unpublished parameter is unreachable -- the value never arrives.
    Publication is what converts a silent drop into a rejectable call.

    Anti-coincidental-pass: a body-level guard tested only in process
    would pass with none of these published. This test fails in exactly
    that case.
    """
    tool = mcp._tool_manager.get_tool("search")  # noqa: SLF001
    schema = tool.parameters
    props = schema.get("properties", {})
    required = schema.get("required", [])

    missing = [key for key in _SEARCH_FILTER_KEYS if key not in props]
    assert not missing, (
        f"search must publish these filter keys as top-level properties so "
        f"the client does not strip them before dispatch; missing: {missing}"
    )
    still_required = [key for key in _SEARCH_FILTER_KEYS if key in required]
    assert not still_required, (
        f"Tripwire parameters must be optional; these are required: {still_required}"
    )
    # The strict-args substrate invariant the stripping behavior depends on.
    assert schema.get("additionalProperties") is False


def test_published_tripwires_cover_every_retrieval_filter_field():
    """The protected set is exactly the filter vocabulary, and all of it is published.

    Two halves, both fail-closed. The set equality pins the decision
    recorded alongside ``_SEARCH_FILTER_KEYS``: every ``RetrievalFilters``
    field is protected, so there is no "why this key and not that one"
    residual. The signature check catches the drift that equality alone
    would miss -- the tuple is derived from the model, but the tool
    parameters are literal, so a new filter field would silently join the
    protected set while remaining strippable in transit.
    """
    assert set(_SEARCH_FILTER_KEYS) == set(RetrievalFilters.model_fields), (
        "The protected filter-key set must track RetrievalFilters exactly."
    )

    params = inspect.signature(search).parameters
    unpublished = [key for key in _SEARCH_FILTER_KEYS if key not in params]
    assert not unpublished, (
        f"RetrievalFilters fields with no matching top-level tripwire parameter "
        f"on search; a caller spelling one of these flat is still stripped in "
        f"transit: {unpublished}"
    )


# ---------------------------------------------------------------------------
# The original symptom: client coercion
# ---------------------------------------------------------------------------


async def test_client_schema_coercion_no_longer_strips_filter_keys(vault_services):
    """A flat filter key survives the client's published-schema coercion.

    The only test here that reproduces the reported symptom rather than
    its server-side shadow. ``mcp.call_tool`` alone receives the stray
    kwarg and refuses it; a real client drops the property first and the
    call succeeds unfiltered. This replays that drop -- filtering the
    arguments through the published ``properties`` exactly as a client
    does -- and then dispatches.

    Anti-coincidental-pass: with the tripwire parameters unpublished this
    fails by returning *both* seeded documents under a successful
    envelope, which is the bug verbatim. It is not enough for the guard
    to exist; the key must reach it.
    """
    arguments = {
        "vault_id": "test_vault",
        "mode": "catalog",
        "limit": 50,
        "doc_type": "note",
    }
    allowed = _published_properties()
    coerced = {key: value for key, value in arguments.items() if key in allowed}

    envelope = _decode_envelope(await mcp.call_tool("search", coerced))
    assert envelope.get("error") == "misplaced_filters", (
        f"A flat doc_type must reach the guard. The client coercion step kept "
        f"{sorted(coerced)}; the call returned {envelope.get('total_available')} "
        f"rows against a vault holding one note and one memo, so the filter was "
        f"dropped in transit and the caller saw plausible unfiltered results."
    )


# ---------------------------------------------------------------------------
# Rejection over transport
# ---------------------------------------------------------------------------


async def test_misplaced_doc_type_returns_misplaced_filters_via_transport(vault_services):
    """A top-level ``doc_type`` is rejected, not silently dropped.

    Routed through ``mcp.call_tool`` so FastMCP's per-tool argument model
    runs first, exactly as it does for a real MCP client.

    Anti-coincidental-pass: asserts the specific ``misplaced_filters``
    envelope and that the offending field is named, not merely that some
    error occurred.
    """
    result = await mcp.call_tool(
        "search",
        {"vault_id": "test_vault", "mode": "catalog", "doc_type": "note"},
    )
    envelope = _decode_envelope(result)
    assert envelope["error"] == "misplaced_filters"
    assert envelope["detail"]["fields"] == ["doc_type"]
    # The corrected shape must be shown, not just named.
    assert "filters" in envelope["detail"]["example"]
    assert "doc_type" in envelope["detail"]["example"]


@pytest.mark.parametrize("key", _SEARCH_FILTER_KEYS)
async def test_every_filter_key_is_guarded(vault_services, key):
    """Each protected filter key is rejected at the top level.

    Regression guard against a guard that only checks the handful of keys
    the original field report happened to name. Parametrized off the
    production tuple so a widened protected set widens the coverage with it.
    """
    result = await mcp.call_tool(
        "search",
        {"vault_id": "test_vault", "mode": "catalog", key: MISPLACED_VALUES[key]},
    )
    envelope = _decode_envelope(result)
    assert envelope["error"] == "misplaced_filters", f"{key} was not guarded"
    assert envelope["detail"]["fields"] == [key]


async def test_multiple_misplaced_filters_are_reported_together(vault_services):
    """All misplaced keys come back in one envelope, not one per round-trip.

    Reporting them as a set lets the caller repair in a single retry. The
    ordering assertion matters independently: caller-supplied order varies
    by client, so the envelope pins the canonical ``RetrievalFilters``
    declaration order instead.
    """
    result = await mcp.call_tool(
        "search",
        {
            "vault_id": "test_vault",
            "mode": "catalog",
            "tags": ["alpha"],
            "source_id": "some_document_id",
            "doc_type": "note",
        },
    )
    envelope = _decode_envelope(result)
    assert envelope["error"] == "misplaced_filters"
    # Canonical key order, not caller-supplied order.
    assert envelope["detail"]["fields"] == ["doc_type", "tags", "source_id"]


async def test_no_rows_are_returned_when_the_guard_fires(vault_services):
    """The rejection carries no result payload at all.

    Anti-coincidental-pass: separates "rejects" from "rejects instead of
    searching". A guard that ran after ``discover`` and merely annotated
    the response would still satisfy the envelope tests above while
    handing the caller exactly the unfiltered rows this ticket exists to
    suppress.
    """
    result = await mcp.call_tool(
        "search",
        {"vault_id": "test_vault", "mode": "catalog", "limit": 50, "doc_type": "note"},
    )
    envelope = _decode_envelope(result)
    assert envelope["error"] == "misplaced_filters"
    assert "results" not in envelope
    assert "total_available" not in envelope


@pytest.mark.parametrize(
    "bad_vault_id",
    [
        # Well-formed but unregistered: reaches ``get_vault`` and would
        # otherwise raise ``vault_not_found``.
        "no_such_vault",
        # Malformed: ``VaultIdStr`` rejects it, so this arm is the one that
        # separates "before the vault lookup" from "before any validation".
        "Not A Vault!",
    ],
)
async def test_guard_precedes_every_other_rejection(vault_services, bad_vault_id):
    """The misplaced field is named ahead of any competing complaint.

    Pins the guard's position as the first statement inside the tool's
    ``try:``. A caller who got the vault complaint first would fix the
    vault id, retry, and only then discover the second, real mistake.

    Anti-coincidental-pass: the two arms exclude different rivals, and
    only the pair pins the position. A guard sitting *after*
    ``_VAULT_ID_ADAPTER.validate_python(vault_id)`` but before
    ``get_vault`` passes the well-formed arm -- ``"no_such_vault"``
    satisfies ``VaultIdStr`` -- and fails the malformed one, where the
    adapter would raise first.
    """
    result = await mcp.call_tool(
        "search",
        {"vault_id": bad_vault_id, "mode": "catalog", "doc_type": "note"},
    )
    envelope = _decode_envelope(result)
    assert envelope["error"] == "misplaced_filters"


# ---------------------------------------------------------------------------
# Negative controls: the canonical spelling is untouched
# ---------------------------------------------------------------------------


async def test_nested_filters_still_work(vault_services):
    """Negative control: the correct nested shape filters as it always did.

    Proves the guard rejects *misplacement* rather than the keys
    themselves. This test passes both before and after the change; if it
    ever fails, the guard is over-broad.
    """
    catalog = _parse(
        await search("test_vault", mode="catalog", filters={"doc_type": "note"}, limit=50)
    )
    assert catalog["total_available"] == 1, (
        f"Nested doc_type filter must select exactly the one note; got {catalog['total_available']}"
    )
    assert catalog["results"][0]["document"]["doc_type"] == "note"


async def test_nested_edge_filters_still_work(vault_services):
    """Negative control for the edge-target half of the filter vocabulary.

    ``source_id`` / ``target_id`` / ``edge_type`` are guarded flat like
    every other key, so the nested edge-enumeration spelling needs its own
    control -- a guard keyed on the wrong argument set could reject the
    canonical call.
    """
    envelope = _decode_envelope(
        await mcp.call_tool(
            "search",
            {
                "vault_id": "test_vault",
                "mode": "catalog",
                "target": "edges",
                "filters": {"edge_type": "references"},
            },
        )
    )
    assert "error" not in envelope, f"Nested edge filters must be unaffected; got {envelope}"


async def test_unknown_filter_key_envelope_is_unchanged(vault_services):
    """An unrecognized key *inside* ``filters`` still yields ``unknown_filter_key``.

    The two rejections cover different mistakes -- wrong level versus
    wrong name -- and must not collapse into one another. A guard that
    shadowed this would tell a caller who typo'd a key to nest something
    that is already nested.
    """
    envelope = _decode_envelope(
        await mcp.call_tool(
            "search",
            {"vault_id": "test_vault", "mode": "catalog", "filters": {"nope": 1}},
        )
    )
    assert envelope["error"] == "unknown_filter_key"
