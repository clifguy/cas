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

import asyncio
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
            await asyncio.sleep(0.5)
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
