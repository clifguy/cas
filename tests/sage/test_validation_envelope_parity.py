"""Transport-level tests for the request-validation envelope on both surfaces.

Two seams reject a malformed parameter, and they are independent:

* The **request-model seam** fires inside the tool body, when the tool
  hands its arguments to the request model. ``search`` with ``limit=200``
  is the reference case: the cap lives on ``DiscoverRequest``, not on the
  tool signature.
* The **argument-model seam** fires *before* the tool body runs, when
  FastMCP validates the incoming arguments against the model it generates
  from the tool signature. ``search`` with ``min_relevance="high"`` is the
  reference case. A handler placed only at the request-model construction
  site does not catch it, which is why both seams are exercised here.

Every MCP test in this module drives ``mcp.call_tool()`` rather than the
registered function directly. The direct path is the easier one -- ``search``
carries its own ``except ValidationError`` arm -- so a fix that only works
in-process would pass a direct-call test and fail real callers.

The HTTP mirror pins the same envelope on the Core API surface. The two
surfaces differ in exactly two documented ways: MCP keys the code as
``error`` and HTTP as ``code``, and HTTP adds the ``read_meta`` block
(CAS-ADR-039). Everything else must match, which the parity test asserts
field by field.
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient
from mcp.types import TextContent

from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.app import create_app
from sage.config import VaultConfig
from sage.mcp_server import _vaults as _mcp_vaults
from sage.mcp_server import mcp
from sage.models.schemas import BulkLifecycleItem, canonical_fields, field_aliases
from tests.sage.conftest import initialize_services_for_test

VAULT_ID = "test_vault"


# ---------------------------------------------------------------------------
# Harnesses
# ---------------------------------------------------------------------------


@pytest.fixture
async def vault_services(minimal_vault_config_dict, tmp_vault_dir):
    """Register stub-backed services on the MCP vault registry.

    Mirrors the ``vault_services`` fixture in ``tests/sage/test_mcp_server.py``;
    duplicated so this transport-level module stays self-contained.
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    async with initialize_services_for_test(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        _mcp_vaults[VAULT_ID] = services
        try:
            yield services
        finally:
            _mcp_vaults.pop(VAULT_ID, None)


@pytest.fixture
async def http_client(minimal_vault_config_dict):
    """An ASGI client over the Core API app with stub providers.

    Mirrors the ``app``/``client`` fixture pair in
    ``tests/sage/test_api_integration.py``; the lifespan does not run under
    ASGITransport, so services are initialized explicitly.
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    async with initialize_services_for_test(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        app.state.vault_registry = {VAULT_ID: services}
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def _decode_envelope(result):
    """Extract the envelope dict from a ``[TextContent]`` call_tool return."""
    assert isinstance(result, list), f"Expected list result; got {type(result)}"
    assert len(result) == 1, f"Expected single TextContent; got {len(result)}"
    block = result[0]
    assert isinstance(block, TextContent), f"Expected TextContent; got {type(block)}"
    return json.loads(block.text)


async def _call_search(**arguments):
    """Drive ``search`` through the real MCP transport and decode the result."""
    return _decode_envelope(await mcp.call_tool("search", {"vault_id": VAULT_ID, **arguments}))


# ---------------------------------------------------------------------------
# Seam 1 -- request-model fields, reached through the transport
# ---------------------------------------------------------------------------


async def test_mcp_search_limit_over_cap_returns_envelope(vault_services):
    """``limit`` past its cap returns the envelope, not the raw rendering.

    The failure happens when the tool body constructs the request model,
    so this exercises the request-model seam through the full transport.
    """
    envelope = await _call_search(query="x", limit=200)

    assert envelope["error"] != "internal_error"
    assert envelope["error"] == "invalid_parameter"
    assert envelope["detail"]["parameter"] == "limit"
    assert envelope["detail"]["value"] == 200
    assert "100" in envelope["detail"]["constraint"]
    assert "offset" in envelope["detail"]["hint"]


async def test_mcp_non_search_tool_wrong_typed_field_returns_envelope(vault_services):
    """The fix is at the shared choke point, not inside one tool.

    ``search`` carries its own ``except ValidationError`` arm; every other
    tool relies on the shared error formatter. Driving a different tool
    proves the normalization is not local to ``search``.
    """
    envelope = _decode_envelope(
        await mcp.call_tool(
            "update_lifecycles",
            {
                "vault_id": VAULT_ID,
                "items": [{"document_id": "abcd1234_sample", "action": "archive"}],
                "response_mode": "verbose",
            },
        )
    )

    assert envelope["error"] != "internal_error"
    assert envelope["error"] == "invalid_parameter"
    assert envelope["detail"]["parameter"].endswith("response_mode")


# ---------------------------------------------------------------------------
# Seam 2 -- the generated argument model, before the tool body runs
# ---------------------------------------------------------------------------


async def test_mcp_search_facet_value_limit_zero_returns_envelope(vault_services):
    """``facet_value_limit=0`` returns the typed envelope naming the bound.

    Zero is rejected, not treated as an unlimited sentinel; the failure
    happens at the request-model seam (the tool signature accepts any
    int), so this exercises the same choke point as the limit-cap test.
    """
    envelope = await _call_search(mode="catalog", target="facets", facet_value_limit=0)

    assert envelope["error"] != "internal_error"
    assert envelope["error"] == "invalid_parameter"
    assert envelope["detail"]["parameter"] == "facet_value_limit"
    assert envelope["detail"]["value"] == 0


async def test_mcp_search_unknown_facet_field_returns_envelope(vault_services):
    """An unknown facet_fields member returns the typed envelope.

    The closed FacetField vocabulary turns a bogus name into a request-
    model enum failure rather than an internal error or a silently
    ignored selection.
    """
    envelope = await _call_search(mode="catalog", target="facets", facet_fields=["bogus"])

    assert envelope["error"] != "internal_error"
    assert envelope["error"] == "invalid_parameter"
    assert "facet_fields" in envelope["detail"]["parameter"]


@pytest.mark.parametrize("element", [1, None, "bogus"])
async def test_mcp_search_facet_field_refusal_names_the_vocabulary(vault_services, element):
    """Every facet_fields element outside the vocabulary is refused naming it.

    A non-string element and an unknown name alike come back at the
    element's position with a constraint listing the accepted facet names,
    so the refusal is repairable whichever seam raises it.
    """
    envelope = await _call_search(mode="catalog", target="facets", facet_fields=[element])

    assert envelope["error"] == "invalid_parameter", envelope
    assert envelope["detail"]["parameter"] == "facet_fields.0", envelope
    for name in ("doc_type", "lifecycle_status", "source_type", "pipeline_status", "tags"):
        assert name in envelope["detail"]["constraint"], envelope


async def test_mcp_search_wrong_typed_tool_argument_returns_envelope(vault_services):
    """A coercion failure at the argument model returns the envelope.

    ``min_relevance`` is typed ``float | None`` on the tool signature, so
    the string never reaches the tool body. Before this normalization the
    call surfaced as a bare tool-execution failure with no envelope at all.
    """
    envelope = await _call_search(min_relevance="high")

    assert envelope["error"] == "invalid_parameter"
    assert envelope["detail"]["parameter"] == "min_relevance"
    assert envelope["detail"]["value"] == "high"


async def test_no_internal_names_or_docs_urls_on_either_seam(vault_services):
    """Neither seam leaks a model class name or a documentation URL."""
    payloads = [
        json.dumps(await _call_search(query="x", limit=200)),
        json.dumps(await _call_search(min_relevance="high")),
    ]

    for text in payloads:
        assert "DiscoverRequest" not in text
        assert "Arguments" not in text
        assert "pydantic.dev" not in text
        assert "validation error for" not in text


async def test_extra_forbidden_wins_over_coercion_failure(vault_services):
    """An unknown kwarg keeps the more actionable ``unknown_parameter`` code.

    A call carrying both a rogue kwarg and a wrong-typed known kwarg raises
    a single ValidationError holding both errors. ``unknown_parameter``
    enumerates the valid parameter names, which is strictly more useful than
    a constraint on one of them, so it must win regardless of the order
    Pydantic happens to report the two failures in.
    """
    envelope = await _call_search(min_relevance="high", no_such_param=1)

    assert envelope["error"] == "unknown_parameter"
    assert envelope["detail"]["rejected_params"] == ["no_such_param"]
    assert "min_relevance" in envelope["detail"]["valid_params"]


# ---------------------------------------------------------------------------
# The HTTP mirror, and parity
# ---------------------------------------------------------------------------


async def test_http_discover_limit_over_cap_returns_envelope(http_client):
    """The Core API surface carries the same envelope in its response body.

    The status stays 422 -- request-validation failures keep FastAPI's
    status; only the body gains structure. ``detail`` becoming a dict rather
    than FastAPI's native list of error records is the observable change.
    """
    resp = await http_client.post(
        f"/sage_vaults/{VAULT_ID}/discover", json={"query": "x", "limit": 200}
    )

    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "invalid_parameter"
    assert isinstance(body["detail"], dict), "must not be FastAPI's native error list"
    assert body["detail"]["parameter"] == "limit"
    assert "offset" in body["detail"]["hint"]


async def test_http_discover_facet_value_limit_zero_returns_envelope(http_client):
    """The Core API mirror of the facet_value_limit bound: same envelope,
    same parameter naming, through the request-body path.
    """
    resp = await http_client.post(
        f"/sage_vaults/{VAULT_ID}/discover",
        json={"mode": "catalog", "target": "facets", "facet_value_limit": 0},
    )

    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "invalid_parameter"
    assert body["detail"]["parameter"] == "facet_value_limit"


async def test_envelope_shape_parity_between_surfaces(vault_services, http_client):
    """The same failure produces the same envelope on both surfaces.

    Two documented differences are excluded by construction: the code key is
    ``error`` on MCP and ``code`` on HTTP, and HTTP adds ``read_meta``. This
    would pass vacuously if both surfaces regressed together, so the two
    tests above pin the content independently.
    """
    mcp_envelope = await _call_search(query="x", limit=200)
    resp = await http_client.post(
        f"/sage_vaults/{VAULT_ID}/discover", json={"query": "x", "limit": 200}
    )
    http_body = resp.json()

    assert mcp_envelope["error"] == http_body["code"]
    assert mcp_envelope["message"] == http_body["message"]
    assert mcp_envelope["detail"] == http_body["detail"]
    # Both directions: a one-way difference would not notice a surface
    # growing a key of its own, which is the drift this test exists to see.
    assert set(http_body) - set(mcp_envelope) == {"code", "read_meta"}
    assert set(mcp_envelope) - set(http_body) == {"error"}


async def test_unknown_parameter_parity_between_surfaces(vault_services, http_client):
    """An unknown top-level name is one refusal on both surfaces.

    The valid-name set is the one field allowed to differ in value: each
    surface names what it accepts where the unknown name was sent, and the
    MCP tool accepts arguments the HTTP body carries in its path or not at
    all. Its presence and type are still held equal, and the code, message,
    tool and rejected names must match exactly.
    """
    mcp_envelope = await _call_search(query="x", bogus_field_x=1)
    resp = await http_client.post(
        f"/sage_vaults/{VAULT_ID}/discover", json={"query": "x", "bogus_field_x": 1}
    )
    http_body = resp.json()

    assert resp.status_code == 400, resp.text
    assert mcp_envelope["error"] == http_body["code"] == "unknown_parameter"
    assert mcp_envelope["message"] == http_body["message"]
    assert set(mcp_envelope["detail"]) == set(http_body["detail"])
    for key in ("tool", "rejected_params"):
        assert mcp_envelope["detail"][key] == http_body["detail"][key]
    assert http_body["detail"]["rejected_params"] == ["bogus_field_x"]
    assert isinstance(http_body["detail"]["valid_params"], list)
    assert "query" in http_body["detail"]["valid_params"]
    assert set(http_body) - set(mcp_envelope) == {"code", "read_meta"}
    assert set(mcp_envelope) - set(http_body) == {"error"}


@pytest.mark.parametrize("name", ["path", "body", "query_string"])
async def test_unknown_argument_named_like_a_transport_segment(vault_services, http_client, name):
    """An unknown name that happens to spell a request component is still unknown.

    The HTTP refusal strips the component FastAPI prepends to a location; the
    MCP argument model prepends none, so a shared helper that stripped
    unconditionally would consume an argument named ``path`` and report the
    call as an ordinary invalid value. ``query_string`` is the control: a
    name that spells nothing, refused as unknown either way.
    """
    mcp_envelope = await _call_search(query="x", **{name: 1})
    resp = await http_client.post(f"/sage_vaults/{VAULT_ID}/discover", json={"query": "x", name: 1})
    http_body = resp.json()

    assert mcp_envelope["error"] == "unknown_parameter", mcp_envelope
    assert mcp_envelope["detail"]["rejected_params"] == [name]
    assert resp.status_code == 400, resp.text
    assert http_body["code"] == "unknown_parameter"
    assert http_body["detail"]["rejected_params"] == [name]


async def test_undeclared_item_field_parity_between_surfaces(vault_services, http_client):
    """A name nested inside a batch item is refused alike on both surfaces.

    Neither surface names it an unknown parameter -- it is not one of the
    operation's parameters -- and neither lets it through. Both answer with
    the item model's own field set, which is the set the caller was refused
    against, and both locate it at the same item.

    The location is the part that has to be asserted rather than assumed. The
    MCP surface validates each item against the item model, so the validator
    reports the key one segment deep, while HTTP validates the whole body and
    reports it three deep. Equal locations here are a property of the tool
    restating the position, not of the two validators agreeing.
    """
    item = {"document_id": "00000000_absent_document", "action": "archive", "bogus": 1}
    mcp_envelope = _decode_envelope(
        await mcp.call_tool("update_lifecycles", {"vault_id": VAULT_ID, "items": [item]})
    )
    resp = await http_client.post(f"/sage_vaults/{VAULT_ID}/lifecycles", json={"items": [item]})
    http_body = resp.json()

    assert mcp_envelope["error"] == http_body["code"] == "undeclared_key"
    assert resp.status_code == 400, resp.text
    assert mcp_envelope["detail"] == http_body["detail"]
    assert http_body["detail"]["parameter"] == "items.0"
    assert http_body["detail"]["key"] == "bogus"
    assert http_body["detail"]["recognized"] == sorted(canonical_fields(BulkLifecycleItem))
    assert http_body["detail"]["aliases"] == field_aliases(BulkLifecycleItem)


async def test_every_undeclared_item_field_is_named_alike_on_both_surfaces(
    vault_services, http_client
):
    """Two undeclared names in one item are refused once, both named, on both surfaces.

    The tool rebuilds the refusal to restate the item's position, so a rebuild
    that carried only ``key`` forward would pass the single-key case above and
    fail here. Written in reverse sorted order, so echoing either validator's
    order fails too.
    """
    item = {"document_id": "00000000_absent_document", "action": "archive", "zz": 1, "aa": 2}
    mcp_envelope = _decode_envelope(
        await mcp.call_tool("update_lifecycles", {"vault_id": VAULT_ID, "items": [item]})
    )
    resp = await http_client.post(f"/sage_vaults/{VAULT_ID}/lifecycles", json={"items": [item]})
    http_body = resp.json()

    assert mcp_envelope["error"] == http_body["code"] == "undeclared_key"
    assert mcp_envelope["detail"] == http_body["detail"]
    assert mcp_envelope["message"] == http_body["message"]
    assert http_body["detail"]["keys"] == ["aa", "zz"]


async def test_a_later_items_undeclared_name_is_noted_alike_on_both_surfaces(
    vault_services, http_client
):
    """Two items each carrying an undeclared name: the first is refused, and both
    surfaces say the other is reported once it is repaired.

    The tool validates one item at a time and stops at the first that fails,
    while HTTP validates the whole body, so a tool that looked only inside the
    failing item would report the same detail with the note missing.
    """
    items = [
        {"document_id": "00000000_absent_document", "action": "archive", "zz": 1},
        {"document_id": "00000000_absent_document", "action": "archive", "aa": 1},
    ]
    mcp_envelope = _decode_envelope(
        await mcp.call_tool("update_lifecycles", {"vault_id": VAULT_ID, "items": items})
    )
    resp = await http_client.post(f"/sage_vaults/{VAULT_ID}/lifecycles", json={"items": items})
    http_body = resp.json()

    assert mcp_envelope["error"] == http_body["code"] == "undeclared_key"
    assert mcp_envelope["detail"] == http_body["detail"]
    assert http_body["detail"]["parameter"] == "items.0"
    assert mcp_envelope["message"] == http_body["message"]
    assert http_body["message"].endswith(
        "Undeclared keys at other locations are reported once this object is repaired."
    )


# ---------------------------------------------------------------------------
# Negative control -- the filter-scoped codes keep their distinct payloads
# ---------------------------------------------------------------------------

_FILTER_CASES = [
    ("unknown_filter_key", {"nope": 1}, "valid_keys"),
    ("invalid_filter_shape", {"tags": 5}, "expected_type"),
    ("invalid_filter_value", {"source_type": "bogus"}, "valid_values"),
]


@pytest.mark.parametrize(("expected_code", "filters", "detail_key"), _FILTER_CASES)
async def test_mcp_filter_scoped_codes_unchanged(
    vault_services, expected_code, filters, detail_key
):
    """Uniformity means every failure reaches an envelope, not the same one.

    These three payloads carry a valid-key list, an expected type, and a
    valid-value set respectively -- each strictly more actionable than the
    generic code. This is the trap for an implementation that normalizes by
    collapsing every validation failure into one code.
    """
    envelope = await _call_search(filters=filters)

    assert envelope["error"] == expected_code
    assert envelope["error"] != "invalid_parameter"
    assert envelope["detail"][detail_key]


@pytest.mark.parametrize(("expected_code", "filters", "detail_key"), _FILTER_CASES)
async def test_http_filter_scoped_codes_unchanged(http_client, expected_code, filters, detail_key):
    """The filter-scoped codes keep their 400 status on the HTTP surface too."""
    resp = await http_client.post(
        f"/sage_vaults/{VAULT_ID}/discover", json={"query": "x", "filters": filters}
    )

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == expected_code
    assert body["detail"][detail_key]


async def test_filter_scoped_codes_are_pairwise_distinct(vault_services):
    """The three filter codes do not collapse into one another."""
    codes = [(await _call_search(filters=f))["error"] for _, f, _ in _FILTER_CASES]

    assert len(set(codes)) == 3, codes


# ---------------------------------------------------------------------------
# mode_parameter_mismatch -- both axes, both surfaces
# ---------------------------------------------------------------------------
#
# This rejection is raised in the models layer and rebuilt at the request
# boundary, so the message and the detail cross a seam that the
# limit-over-cap case above does not exercise. Two rejections are driven
# rather than one, because the constraint sits on a different axis in each
# and only the target-constrained shape shows whether the validator's own
# wording survived.

_MODE_CONSTRAINED = {"mode": "catalog", "heading_path": "Section 1"}
_TARGET_CONSTRAINED = {"mode": "catalog", "target": "documents", "facet_fields": ["tags"]}


async def _both_surfaces(http_client, arguments):
    """Drive one rejection through both seams and return both envelopes."""
    mcp_envelope = await _call_search(**arguments)
    resp = await http_client.post(f"/sage_vaults/{VAULT_ID}/discover", json=arguments)
    return mcp_envelope, resp.json()


@pytest.mark.parametrize(
    ("label", "arguments"),
    [("mode-constrained", _MODE_CONSTRAINED), ("target-constrained", _TARGET_CONSTRAINED)],
)
async def test_mode_parameter_mismatch_parity_between_surfaces(
    vault_services, http_client, label, arguments
):
    """One rejection, one message, one detail, whichever surface asked.

    Both surfaces read the same SAGEError, so this holds structurally and
    would pass vacuously if the translator regressed for both at once --
    which is what the content test below is for.
    """
    mcp_envelope, http_body = await _both_surfaces(http_client, arguments)

    assert mcp_envelope["error"] == http_body["code"] == "mode_parameter_mismatch"
    assert mcp_envelope["message"] == http_body["message"]
    assert mcp_envelope["detail"] == http_body["detail"]
    assert set(http_body) - set(mcp_envelope) == {"code", "read_meta"}
    assert set(mcp_envelope) - set(http_body) == {"error"}


@pytest.mark.parametrize("target", ["facets", "edges"])
async def test_mode_resolution_parity_between_surfaces(vault_services, http_client, target):
    """A catalog-only target with no mode is accepted on both surfaces.

    The two seams spell "not supplied" differently -- HTTP omits the key
    while the MCP tool forwards an explicit None -- so a resolution that
    keyed on key presence would accept here and refuse there. Each
    surface is asserted on its own rather than compared to the other,
    because a comparison passes when both refuse.
    """
    arguments = {"target": target}

    mcp_envelope = await _call_search(**arguments)
    assert "error" not in mcp_envelope, mcp_envelope
    assert mcp_envelope["mode"] == "catalog"
    assert mcp_envelope["target"] == target

    resp = await http_client.post(f"/sage_vaults/{VAULT_ID}/discover", json=arguments)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "catalog"
    assert body["target"] == target


async def test_mode_parameter_mismatch_content_is_pinned_independently(vault_services, http_client):
    """A target-constrained rejection reaches the caller as one.

    The literals here are the point rather than a shortcut: they are what a
    caller reads, and the rejection they pin is the one whose delivered
    wording used to contradict itself -- refusing a mode while reporting
    that same mode as the allowed one, and never naming `target`, which is
    the parameter that has to change. Both surfaces are asserted so neither
    can drift alone behind the parity check above.
    """
    expected_message = "Parameter 'facet_fields' is valid only for target 'facets'."
    expected_detail = {
        "mode": "catalog",
        "target": "documents",
        "forbidden_param": "facet_fields",
        "allowed_targets": ["facets"],
    }

    mcp_envelope, http_body = await _both_surfaces(http_client, _TARGET_CONSTRAINED)

    for envelope in (mcp_envelope, http_body):
        assert envelope["message"] == expected_message
        assert envelope["detail"] == expected_detail
        # The rejection is on the target axis, so a mode set would name a
        # change that does not lift it.
        assert "allowed_modes" not in envelope["detail"]


async def test_top_level_field_inside_metadata_parity_between_surfaces(vault_services, http_client):
    """A top-level argument nested under ``metadata`` is refused alike on both.

    The guard sits on the request model, which both surfaces bind, so the
    parity is structural rather than two checks kept in step. Asserting it
    anyway is what catches a later move of the guard into one tool body,
    which would pass every single-surface test and leave the HTTP caller with
    the type complaint the guard exists to replace.
    """
    body = {"source": "test/sample.md", "source_type": "markdown"}
    metadata = {"tier3_metadata": "T-1"}

    mcp_envelope = _decode_envelope(
        await mcp.call_tool("ingest_document", {"vault_id": VAULT_ID, **body, "metadata": metadata})
    )
    resp = await http_client.post(
        f"/sage_vaults/{VAULT_ID}/documents", json={**body, "metadata": metadata}
    )
    http_body = resp.json()

    assert mcp_envelope["error"] == http_body["code"] == "misplaced_top_level_field"
    assert resp.status_code == 400, resp.text
    assert mcp_envelope["detail"] == http_body["detail"]
    assert http_body["detail"]["fields"] == ["tier3_metadata"]


# ---------------------------------------------------------------------------
# Every tool that builds a nesting request model refuses its nested keys
# ---------------------------------------------------------------------------


def _constructed_schema_name(node) -> str | None:
    """The schema name a call builds a model from, or ``None``.

    Both spellings a tool body uses count: ``Model(...)`` and
    ``Model.model_validate(...)``. Reading only the first was a way for the
    walk to answer "nothing to check" about a tool that does validate a
    model -- a silent pass under a docstring promising the opposite, which is
    the shape this gate exists to catch rather than to have.
    """
    import ast

    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
        return node.func.value.id
    return None


def test_the_derivation_reads_both_construction_spellings():
    """A model validated rather than constructed is still derived.

    Exercised on synthetic source, because the repository uses one of the two
    spellings at every site that matters: a test reading only real source
    passes against a walk that handles only that spelling, which is the gap.

    Run through ``_tools_in_source`` -- the walk the real derivation calls --
    rather than through the name helper it uses. Asserting the helper leaves
    the walk free to ignore what the helper returns, and a test that cannot
    go red while the walk is wrong is the shape this gate exists to catch.
    """
    derived = _tools_in_source(
        "@mcp.tool()\n"
        "async def constructs():\n"
        "    return IngestRequest(source='x')\n"
        "\n"
        "@mcp.tool()\n"
        "async def validates():\n"
        "    return IngestRequest.model_validate({})\n"
    )

    from sage.models import schemas

    assert derived == {
        "constructs": schemas.IngestRequest,
        "validates": schemas.IngestRequest,
    }, derived


def _tools_in_source(source: str) -> dict[str, type]:
    """The tools one module's source declares that build a nesting model.

    Split out so the walk itself can be exercised on source written for the
    purpose. Kept separate from the module list above for no other reason.
    """
    import ast
    import types
    import typing

    from pydantic import BaseModel

    from sage.models import schemas

    def strict_nested(model: type[BaseModel]) -> set[type[BaseModel]]:
        seen: set[type[BaseModel]] = set()
        out: set[type[BaseModel]] = set()

        def walk(current: type[BaseModel]) -> None:
            for field in current.model_fields.values():
                pending = [field.annotation]
                while pending:
                    item = pending.pop()
                    if isinstance(item, type) and issubclass(item, BaseModel):
                        if item in seen:
                            continue
                        seen.add(item)
                        out.add(item)
                        walk(item)
                        continue
                    if isinstance(item, types.UnionType) or typing.get_origin(item) is not None:
                        pending.extend(typing.get_args(item))

        walk(model)
        return {
            m
            for m in out
            if m.model_config.get("extra") == "forbid" and m is not schemas.RetrievalFilters
        }

    found: dict[str, type] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if not any(
            isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Attribute)
            and dec.func.attr == "tool"
            for dec in node.decorator_list
        ):
            continue
        for inner in ast.walk(node):
            name = _constructed_schema_name(inner)
            if name is None:
                continue
            model = getattr(schemas, name, None)
            if isinstance(model, type) and issubclass(model, BaseModel) and strict_nested(model):
                found[node.name] = model
    return found


def _tools_building_a_nesting_model() -> dict[str, type]:
    """Each MCP tool whose body builds a request model nesting a strict model.

    The argument boundary is held flat by its own gate, so a tool's arguments
    never nest a model. The models do, and a tool builds one in its own body --
    which is where the refusal has to be handed the model the request was
    validated against. A tool that skips that step looks identical from outside
    until a caller sends a nested key, and one of them shipped a docstring
    promising the refusal it did not produce.

    Derived by reading each tool module rather than listed, so a tool added
    later arrives here on its own. Models whose undeclared key another rule
    answers first are excluded by ``_SHADOWED_NESTINGS``, which states the
    rule that reaches the key instead.

    Two limits of the reading, stated because a derivation that looks total
    and is not is worse than a list. It matches a model constructed or
    validated *inside* the tool body; a tool that delegates the construction
    to a helper is not followed into it, and would be derived as building
    nothing. And the multipart batch operation binds its envelope as a form
    field rather than a body model, so it is reached through the conformance
    module's own map rather than through this walk. Neither is a live gap --
    every tool builds its model inline today, and the batch operation carries
    its own endpoint tests -- and both would need a call-graph walk rather
    than a syntactic one to close.
    """
    from pathlib import Path as _Path

    repo_root = _Path(__file__).resolve().parents[2]
    found: dict[str, type] = {}
    for module_path in ("sage/sage_api_tools.py", "sage/app_tools.py"):
        found |= _tools_in_source((repo_root / module_path).read_text())
    return found


#: Nested models whose undeclared key is answered by a more specific refusal,
#: so ``undeclared_key`` is unreachable for them by construction. Not an
#: allowlist: each is here because another rule reaches the key first and says
#: more, and each would have to lose that rule to belong in the probes.
#:
#: ``RetrievalFilters`` -- a key it refuses keeps ``unknown_filter_key``, whose
#: detail carries the valid key set and a worked example.
#:
#: ``ProvenanceFilter`` -- nested under ``RetrievalFilters``, and a key it
#: refuses keeps ``unknown_filter_key`` for the same reason, naming the dotted
#: key and the provenance filter's own key set.
#:
#: ``Tier3Patch`` -- the retired bare-dict form is detected as *any* key
#: outside ``{set, unset}`` (``sage/models/legacy_form.py``), so a patch object
#: carrying an undeclared key alongside its ops is read as that form and
#: refused as ``legacy_form`` before the model is reached. ``ListFieldPatch``
#: is deliberately not here: its retired form is a bare *list*, so a mapping
#: with an extra key does reach the model, and it is probed.
_SHADOWED_NESTINGS: frozenset[str] = frozenset(
    {"ProvenanceFilter", "RetrievalFilters", "Tier3Patch"}
)


#: One call per (tool, nested model) that plants an undeclared key at that
#: nesting. Keyed by the pair rather than by the tool, because an operation
#: nests several models and one probe per operation leaves the others
#: unchecked -- a gate can then stay green while the rule is severed for
#: exactly one of them. Hand-written because each call takes a different
#: shape; the *pairs* are derived, and a derived pair with no entry fails the
#: gate rather than being skipped.
_NESTED_KEY_PROBES: dict[tuple[str, str], dict] = {
    ("ingest_document", "RelocationPointer"): {
        "source": "test/sample.md",
        "source_type": "markdown",
        "relocated_from": {"bogus_field_x": 1},
    },
    ("update_lifecycles", "BulkLifecycleItem"): {
        "items": [{"document_id": "00000000_absent_document", "bogus_field_x": 1}]
    },
    ("update_lifecycles", "RelocationPointer"): {
        "items": [
            {
                "document_id": "00000000_absent_document",
                "action": "relocate",
                "relocated_to": {"bogus_field_x": 1},
            }
        ]
    },
    ("create_edges", "BulkLinkItem"): {
        "items": [{"source_id": "00000000_absent_document", "bogus_field_x": 1}]
    },
    ("update_metadata", "BulkMetadataItem"): {
        "items": [{"document_id": "00000000_absent_document", "bogus_field_x": 1}]
    },
    ("update_metadata", "ListFieldPatch"): {
        "items": [{"document_id": "00000000_absent_document", "tags": {"bogus_field_x": 1}}]
    },
}


def _derived_nested_pairs(roots: dict[str, type]) -> set[tuple[str, str]]:
    """Every (operation, nested model) the given roots reach.

    The probe tables are checked against this rather than against the
    operation list: an operation nests several models, and a gate that plants
    one key per operation proves only the nesting it happened to pick.
    """
    from tests.sage.test_rest_request_strictness_conformance import _nested_model_paths

    by_root: dict[type, set[str]] = {}
    for root, _path, nested in _nested_model_paths():
        by_root.setdefault(root, set()).add(nested.__name__)
    return {
        (operation, nested)
        for operation, root in roots.items()
        for nested in by_root.get(root, set())
        if nested not in _SHADOWED_NESTINGS
    }


async def test_every_tool_building_a_nesting_model_names_the_accepted_keys(vault_services):
    """A key nested inside a tool's request model is refused with the field set.

    End to end through each tool rather than against the envelope helper: the
    helper was already right, and what was missing at the site this gate was
    written for was the call into it. Asserting the helper would have stayed
    green through that.

    The derived set is asserted to be covered, so a tool that starts building a
    nesting model arrives here as a failure naming itself rather than as a
    silent pass.
    """
    import jsonschema

    from sage.models import schemas
    from sage.models.error_contract import tool_error_schema

    derived = _tools_building_a_nesting_model()
    assert derived, "no tool found building a nesting model; the walk checks nothing"
    pairs = _derived_nested_pairs(derived)
    assert pairs, "no nesting derived; the walk checks nothing"
    uncovered = sorted(pairs - set(_NESTED_KEY_PROBES))
    assert uncovered == [], f"no nested-key probe defined for {uncovered}"

    for tool_name, expected_model in sorted(pairs):
        payload = _NESTED_KEY_PROBES[(tool_name, expected_model)]
        envelope = _decode_envelope(
            await mcp.call_tool(tool_name, {"vault_id": VAULT_ID, **payload})
        )

        assert envelope["error"] == "undeclared_key", (tool_name, expected_model, envelope)
        assert envelope["detail"]["key"] == "bogus_field_x", (tool_name, envelope)
        model = getattr(schemas, expected_model)
        assert envelope["detail"]["recognized"] == sorted(canonical_fields(model)), (
            tool_name,
            expected_model,
            envelope,
        )
        assert envelope["detail"].get("aliases", {}) == field_aliases(model), envelope

        # The tool publishes an error schema through ``tools/list``; a code it
        # can raise has to be in that tool's family table or the schema it
        # advertises rejects the envelope it just returned. Registering a code
        # is a six-part act and the table is the part with no other reader,
        # so this asserts it against a refusal the tool actually produced
        # rather than against the table's own contents.
        jsonschema.validate(envelope, tool_error_schema("", tool_name))


#: One request body per (route, nested model) that plants an undeclared key at
#: that nesting, keyed by the pair for the reason the MCP table is. The route
#: name is the handler's, which is what the derivation returns and is not
#: always the published operation id.
_HTTP_NESTED_KEY_PROBES: dict[tuple[str, str], tuple[str, dict]] = {
    ("ingest", "RelocationPointer"): (
        "documents",
        {"source": "test/sample.md", "relocated_from": {"bogus_field_x": 1}},
    ),
    ("update_lifecycles", "BulkLifecycleItem"): (
        "lifecycles",
        {"items": [{"document_id": "00000000_absent_document", "bogus_field_x": 1}]},
    ),
    ("update_lifecycles", "RelocationPointer"): (
        "lifecycles",
        {
            "items": [
                {
                    "document_id": "00000000_absent_document",
                    "action": "relocate",
                    "relocated_to": {"bogus_field_x": 1},
                }
            ]
        },
    ),
    ("create_edges", "BulkLinkItem"): (
        "edges",
        {"items": [{"source_id": "00000000_absent_document", "bogus_field_x": 1}]},
    ),
    ("update_metadata", "BulkMetadataItem"): (
        "metadata",
        {"items": [{"document_id": "00000000_absent_document", "bogus_field_x": 1}]},
    ),
    ("update_metadata", "ListFieldPatch"): (
        "metadata",
        {"items": [{"document_id": "00000000_absent_document", "tags": {"bogus_field_x": 1}}]},
    ),
}


def _core_operations_with_a_nested_strict_model() -> set[str]:
    """Core API operations whose body graph nests a model refusing extras.

    Derived the way the declaration gate derives it, and for the same reason:
    an operation added later has to arrive here rather than wait to be
    remembered. ``RetrievalFilters`` is excluded because a key it refuses
    keeps ``unknown_filter_key``, whose detail says more.
    """
    from tests.sage.test_rest_request_strictness_conformance import (
        _FORM_ENCODED_BODY_MODELS,
        _core_routes,
        _operations_with_a_nested_strict_model,
    )

    reachable = _operations_with_a_nested_strict_model(_core_routes(), _FORM_ENCODED_BODY_MODELS)
    by_name = {route.name: route for route in _core_routes()}
    return {name for name, yes in reachable.items() if yes and name in by_name}


async def test_every_http_operation_with_a_nested_model_refuses_its_undeclared_keys(http_client):
    """The HTTP surface answers a nested undeclared key, operation by operation.

    Its sibling gate reads the published 400 against a derivation and never
    sends a request, so severing the rule at the exception handler for any
    subset of operations leaves it green -- measured, not supposed. The MCP
    surface had a probe gate from the start and the HTTP one did not, which is
    the asymmetry this closes: what a declaration gate proves is that the
    contract says the right thing, never that the surface does it.

    The multipart batch upload is covered by its own endpoint tests, which
    post real file parts; it is excluded here rather than given a fake body.
    """
    from fastapi.dependencies.utils import get_flat_dependant
    from fastapi.routing import APIRoute

    from sage.app import create_app
    from sage.models import schemas

    nesting = _core_operations_with_a_nested_strict_model()
    roots = {
        route.name: get_flat_dependant(route.dependant).body_params[0].field_info.annotation
        for route in create_app().routes
        if isinstance(route, APIRoute)
        and route.name in nesting
        and get_flat_dependant(route.dependant).body_params
    }
    pairs = _derived_nested_pairs(roots)
    assert pairs, "no nesting derived; the walk checks nothing"
    uncovered = sorted(pairs - set(_HTTP_NESTED_KEY_PROBES))
    assert uncovered == [], f"no nested-key probe defined for {uncovered}"

    for operation, expected_model in sorted(pairs):
        path, body = _HTTP_NESTED_KEY_PROBES[(operation, expected_model)]
        resp = await http_client.post(f"/sage_vaults/{VAULT_ID}/{path}", json=body)
        envelope = resp.json()

        assert resp.status_code == 400, (operation, expected_model, resp.text)
        assert envelope["code"] == "undeclared_key", (operation, expected_model, envelope)
        assert envelope["detail"]["key"] == "bogus_field_x", (operation, envelope)
        model = getattr(schemas, expected_model)
        assert envelope["detail"]["recognized"] == sorted(canonical_fields(model)), (
            operation,
            expected_model,
            envelope,
        )
        assert envelope["detail"].get("aliases", {}) == field_aliases(model), envelope


async def test_both_surfaces_locate_a_nested_key_at_the_same_parameter(vault_services, http_client):
    """The two surfaces report the same ``detail.parameter`` for the same key.

    The MCP surface validates each batch item against the item model and then
    restates the item's position, so the location it reports is built rather
    than read off the validator. The probe gates assert the code, the key and
    the accepted set, and a re-prefix that dropped a nested segment -- reporting
    ``items.0`` where HTTP reports ``items.0.tags`` -- satisfied all three with
    the suite green. So the location is asserted here, on every pair both
    surfaces probe, against the other surface rather than against a literal.
    """
    shared = sorted(
        (tool, model)
        for (tool, model) in _NESTED_KEY_PROBES
        if (("ingest" if tool == "ingest_document" else tool), model) in _HTTP_NESTED_KEY_PROBES
    )
    assert shared, "no pair probed on both surfaces; nothing is compared"

    for tool, model in shared:
        route = "ingest" if tool == "ingest_document" else tool
        path, body = _HTTP_NESTED_KEY_PROBES[(route, model)]
        mcp_env = _decode_envelope(
            await mcp.call_tool(tool, {"vault_id": VAULT_ID, **_NESTED_KEY_PROBES[(tool, model)]})
        )
        http_env = (await http_client.post(f"/sage_vaults/{VAULT_ID}/{path}", json=body)).json()

        assert mcp_env["detail"]["parameter"] == http_env["detail"]["parameter"], (
            tool,
            model,
            mcp_env["detail"]["parameter"],
            http_env["detail"]["parameter"],
        )


#: A real call per (tool, code) the MCP family table lists for this change's two
#: codes, producing that refusal through the tool. Keyed by the table's own
#: pairs, so an entry with no probe fails, and so does a probe for a pair the
#: table does not list.
_FAMILY_TABLE_PROBES: dict[tuple[str, str], dict] = {
    ("ingest_document", "undeclared_key"): {
        "source": "test/sample.md",
        "source_type": "markdown",
        "relocated_from": {"bogus_field_x": 1},
    },
    ("ingest_document", "misplaced_top_level_field"): {
        "source": "test/sample.md",
        "source_type": "markdown",
        "metadata": {"tier3_metadata": "T-1"},
    },
    ("update_lifecycles", "undeclared_key"): {
        "items": [{"document_id": "00000000_absent_document", "bogus_field_x": 1}]
    },
    ("update_metadata", "undeclared_key"): {
        "items": [{"document_id": "00000000_absent_document", "bogus_field_x": 1}]
    },
    ("create_edges", "undeclared_key"): {
        "items": [{"source_id": "00000000_absent_document", "bogus_field_x": 1}]
    },
    ("bulk_ingest_document", "undeclared_key"): {
        "files": [{"file_path": "test/sample.md", "bogus_field_x": 1}]
    },
}


async def test_every_family_table_entry_publishes_a_schema_its_refusal_satisfies(
    vault_services,
):
    """Each tool's published error schema accepts the refusal the tool returns.

    The table is the one part of registering a code that nothing else reads:
    omit a row and the tool still refuses correctly, but advertises through
    ``tools/list`` a schema that rejects the envelope it emits. The probe gate
    above checks this only for the tools it derives and only for the code it
    plants, so a row for another tool or another code could go missing with
    the suite green -- measured, not supposed.

    Keyed on the table's own pairs for this change's codes rather than on a
    list, so the assertion is about the table: a row with no probe fails, and a
    probe the table does not back fails too.
    """
    import jsonschema

    from sage.models.error_contract import SCHEMAS, tool_error_schema

    ours = {"undeclared_key", "misplaced_top_level_field"}
    table = SCHEMAS["ErrorResponse"]["x-mcp-tool-errors"]
    listed = {(tool, code) for tool, codes in table.items() for code in codes if code in ours}
    assert listed, "no row lists either code; the table checks nothing"
    assert listed == set(_FAMILY_TABLE_PROBES), (
        f"unprobed rows {sorted(listed - set(_FAMILY_TABLE_PROBES))}, "
        f"unbacked probes {sorted(set(_FAMILY_TABLE_PROBES) - listed)}"
    )

    for (tool, code), payload in sorted(_FAMILY_TABLE_PROBES.items()):
        envelope = _decode_envelope(await mcp.call_tool(tool, {"vault_id": VAULT_ID, **payload}))
        assert envelope["error"] == code, (tool, code, envelope)
        jsonschema.validate(envelope, tool_error_schema("", tool))
