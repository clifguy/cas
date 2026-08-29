"""Strict-argument-validation substrate for the SAGE MCP framework boundary.

These tests pin the substrate invariant that the SAGE MCP server registers
its tools through a FastMCP whose argument-validation layer rejects
unknown caller-supplied keyword arguments at the framework boundary, and
that ``_LoggingFastMCP.call_tool`` translates the resulting Pydantic
``ValidationError`` into a structured ``unknown_parameter`` SAGE error
envelope instead of letting the underlying ``ToolError`` propagate.

Two responsibilities, two layers:

- ``T1`` / ``T2`` pin the substrate-level invariant: the per-tool argument
  model produced by FastMCP's ``func_metadata`` must inherit
  ``extra="forbid"`` from ``ArgModelBase.model_config``. The patch lives
  in ``sage._fastmcp_strict_args``; importing ``sage.mcp_server``
  transitively triggers the monkey-patch before any ``@mcp.tool()``
  decoration runs.
- ``T3``-``T9`` pin the translation behavior in ``_LoggingFastMCP.call_tool``:
  validation errors of type ``extra_forbidden`` become a SAGE
  ``unknown_parameter`` envelope wrapped in the production ``TextContent``
  shape; non-``extra_forbidden`` ``ValidationError`` (e.g. type coercion)
  continue to propagate as ``ToolError``; non-``ValidationError`` tool-body
  failures continue to propagate; aliased fields and pre-parsed JSON
  sub-models continue to work; the success path is unchanged.

Coincidental-pass discipline lives in the pairings: T3 / T5 together
guard against over-broad translation (T3 demands translation for the
``extra_forbidden`` case; T5 demands non-translation for the
type-coercion case, so widening the catch to all ValidationErrors
fails T5 even while keeping T3 green). T9 guards against
translation-of-everything (an envelope returned on the success path
fails the response-shape equality assertion). T1 and T2 are
substrate-level pins independent of the translation layer: T1 fails
loudly if the monkey-patch did not land at import time, T2 fails
loudly if the patch landed on ``ArgModelBase`` but did not propagate
to dynamically-created subclasses (the timing-constraint failure
mode the patch was authored to avoid). Each per-test docstring
restates the trap it guards in situ.
"""

from __future__ import annotations

import json
import logging

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase
from mcp.types import TextContent
from pydantic import ValidationError, create_model

# Importing ``sage.mcp_server`` triggers the import of
# ``sage._fastmcp_strict_args`` for its side effect, which mutates
# ``ArgModelBase.model_config`` before any ``@mcp.tool()`` decoration in
# the module body runs. Every test in this file relies on that side
# effect having landed, so the import sits at module scope.
import sage.mcp_server  # noqa: F401 -- substrate side-effect import
from sage.mcp_server import _LoggingFastMCP

# ---------------------------------------------------------------------------
# T1, T2 — substrate invariant
# ---------------------------------------------------------------------------


def test_t1_arg_model_base_config_carries_extra_forbid():
    """``ArgModelBase.model_config["extra"] == "forbid"`` after import.

    Pre-implementation: the upstream default is ``ConfigDict(
    arbitrary_types_allowed=True)`` with no ``extra`` key. Pydantic's
    default for missing ``extra`` is ``"ignore"`` — so the assertion
    against the literal ``"forbid"`` string fails both as a missing key
    and as the wrong key.
    """
    assert ArgModelBase.model_config.get("extra") == "forbid", (
        f"Expected ArgModelBase.model_config['extra'] == 'forbid'; "
        f"got {ArgModelBase.model_config.get('extra')!r}. "
        f"Either sage._fastmcp_strict_args did not run at import time, "
        f"or its monkey-patch did not take effect."
    )


def test_t2_dynamic_arg_model_subclass_rejects_extras_with_extra_forbidden():
    """A dynamically-created subclass of ``ArgModelBase`` raises
    ``ValidationError`` with ``type='extra_forbidden'`` when an
    undeclared key is passed.

    This mirrors the structure FastMCP uses internally:
    ``create_model("<tool>Arguments", __base__=ArgModelBase, ...)``.
    If ``extra="forbid"`` did not propagate to dynamic subclasses
    (e.g. because Pydantic snapshotted the config too early, or the
    patch came too late), ``model_validate`` would silently drop the
    ``"rogue"`` key and the test would fail at the ``pytest.raises``
    boundary.

    The error-type assertion is the second discriminator: a different
    validation failure (missing required field, wrong type) would also
    fail this test even though it raised, because the test asserts on
    the specific ``extra_forbidden`` discriminator.
    """
    ProbeModel = create_model(
        "ArgModelProbe",
        __base__=ArgModelBase,
        declared=(str, ...),
    )

    with pytest.raises(ValidationError) as excinfo:
        ProbeModel.model_validate({"declared": "x", "rogue": "y"})

    errors = excinfo.value.errors()
    extra_forbidden = [e for e in errors if e["type"] == "extra_forbidden"]
    assert extra_forbidden, (
        f"Expected at least one error of type 'extra_forbidden'; "
        f"got error types {[e['type'] for e in errors]!r}."
    )
    assert extra_forbidden[0]["loc"] == ("rogue",), (
        f"Expected the rejected location to be ('rogue',); got {extra_forbidden[0]['loc']!r}."
    )


# ---------------------------------------------------------------------------
# T3, T4 — _LoggingFastMCP.call_tool translation (single + multi kwarg)
# ---------------------------------------------------------------------------


def _decode_envelope(result):
    """Extract the SAGE envelope dict from a ``[TextContent]`` return.

    Mirrors the production wire shape: SAGE tool error envelopes are
    dicts that FastMCP's ``_convert_to_content`` wraps in
    ``[TextContent(type="text", text=<json>)]`` before they reach the
    caller. Tests use this helper rather than asserting on the raw
    TextContent objects so the assertion sites stay readable.
    """
    assert isinstance(result, list), f"Expected list result; got {type(result)}"
    assert len(result) == 1, f"Expected single TextContent; got {len(result)}"
    block = result[0]
    assert isinstance(block, TextContent), f"Expected TextContent; got {type(block)}"
    return json.loads(block.text)


async def test_t3_call_tool_translates_single_unknown_kwarg_to_envelope():
    """Single misspelled kwarg surfaces as the ``unknown_parameter`` envelope.

    Pre-translation behavior: FastMCP raises ``ValidationError``,
    ``Tool.run`` wraps it as ``ToolError(f"Error executing tool ...")``,
    ``_LoggingFastMCP.call_tool`` re-raises. The test would have to
    expect ``ToolError``. After translation, the envelope return value
    is what callers see; the test asserts the structured shape.
    """
    mcp = _LoggingFastMCP("test_t3")

    @mcp.tool()
    async def echo(declared: str) -> dict:
        return {"echoed": declared}

    result = await mcp.call_tool("echo", {"declared": "x", "rogue": "y"})

    envelope = _decode_envelope(result)
    assert envelope["error"] == "unknown_parameter"
    assert envelope["message"] == ("Tool 'echo' received unknown parameter(s): ['rogue'].")
    detail = envelope["detail"]
    assert detail["tool"] == "echo"
    assert detail["rejected_params"] == ["rogue"]
    assert detail["valid_params"] == ["declared"]


async def test_t4_call_tool_lists_all_unknown_kwargs_and_full_valid_params():
    """Multi-rogue + multi-declared: rejected lists all, valid lists from sig.

    Guards two implementation traps: (a) extracting only the first
    rejected param, (b) deriving ``valid_params`` from anywhere other
    than the tool's declared signature. Lists are asserted sorted for
    deterministic comparison.
    """
    mcp = _LoggingFastMCP("test_t4")

    @mcp.tool()
    async def multi(a: str, b: int = 0, c: bool = False) -> dict:
        return {"a": a, "b": b, "c": c}

    result = await mcp.call_tool("multi", {"a": "x", "rogue1": 1, "rogue2": 2})

    envelope = _decode_envelope(result)
    assert envelope["error"] == "unknown_parameter"
    detail = envelope["detail"]
    assert detail["tool"] == "multi"
    assert detail["rejected_params"] == sorted(["rogue1", "rogue2"])
    assert detail["valid_params"] == sorted(["a", "b", "c"])


# ---------------------------------------------------------------------------
# T5, T6 — narrow-catch discipline
# ---------------------------------------------------------------------------


async def test_t5_type_coercion_validation_error_becomes_envelope():
    """A coercion failure at the argument model returns an envelope.

    This failure happens before the tool body runs, so the body's own
    error handling cannot reach it; the framework boundary is the only
    place to normalize it. Left alone, the caller receives a bare
    tool-execution failure carrying the generated argument model's class
    name and the validator's documentation URL.

    T6 is the guard on the other side: a genuine tool-body failure must
    still propagate as a raised ``ToolError`` rather than be swallowed
    into an envelope.
    """
    mcp = _LoggingFastMCP("test_t5")

    @mcp.tool()
    async def typed(n: int) -> dict:
        return {"n": n}

    envelope = _decode_envelope(await mcp.call_tool("typed", {"n": "not-an-int"}))

    assert envelope["error"] == "invalid_parameter"
    assert envelope["detail"]["parameter"] == "n"
    assert envelope["detail"]["value"] == "not-an-int"
    assert "Arguments" not in json.dumps(envelope)


async def test_t6_non_validation_error_tool_failure_still_propagates():
    """ToolError whose cause is not ValidationError continues to propagate.

    Guards against the new ToolError arm in ``_LoggingFastMCP.call_tool``
    swallowing tool-body failures. If the implementation routes every
    ToolError through the translation helper without checking
    ``__cause__``, the body's ``ValueError`` becomes an envelope and the
    test fails at ``pytest.raises``.
    """
    mcp = _LoggingFastMCP("test_t6")

    @mcp.tool()
    async def boom(x: str) -> dict:
        raise ValueError(f"boom: {x}")

    with pytest.raises(ToolError):
        await mcp.call_tool("boom", {"x": "ok"})


# ---------------------------------------------------------------------------
# T7, T8 — regression guards for upstream FastMCP behaviors
# ---------------------------------------------------------------------------


async def test_t7_aliased_field_param_still_succeeds_under_extra_forbid():
    """Parameters named like BaseModel attributes (auto-aliased) still work.

    FastMCP's ``func_metadata`` auto-renames any parameter whose name
    shadows a callable on ``BaseModel`` (per func_metadata.py:243-247):
    the field becomes ``field_<name>`` with ``alias=<name>``. Pydantic's
    ``extra="forbid"`` must respect alias resolution and treat the
    incoming alias-keyed kwarg as a known field, not as "extra".

    Failure mode this test guards: a subtle interaction where
    ``extra="forbid"`` is applied at a layer that sees the un-aliased
    field name and rejects the alias as unknown.
    """
    mcp = _LoggingFastMCP("test_t7")

    @mcp.tool()
    async def aliased(dict: str) -> dict:
        return {"received": dict}

    # ``populate_by_name`` is False by default on ArgModelBase, so the
    # incoming kwarg must use the alias (the original parameter name).
    result = await mcp.call_tool("aliased", {"dict": "x"})
    envelope_or_payload = _decode_envelope(result)
    assert envelope_or_payload == {"received": "x"}, (
        f"Aliased-field call should round-trip cleanly; got {envelope_or_payload!r}."
    )


async def test_t8_pre_parsed_json_sub_models_still_parse_under_extra_forbid():
    """Stringified JSON sub-model is pre-parsed; extra="forbid" doesn't intercept.

    FastMCP's ``pre_parse_json`` (func_metadata.py:133) handles callers
    that pass JSON-encoded values as strings. The parsing happens
    before model validation, so ``extra="forbid"`` should not see the
    raw string as an "extra" — it sees the parsed dict matching the
    declared ``payload: dict`` parameter.
    """
    mcp = _LoggingFastMCP("test_t8")

    @mcp.tool()
    async def with_sub(payload: dict) -> dict:
        return {"received": payload}

    result = await mcp.call_tool("with_sub", {"payload": '{"k": "v"}'})
    envelope_or_payload = _decode_envelope(result)
    assert envelope_or_payload == {"received": {"k": "v"}}, (
        f"pre_parse_json should produce dict payload; got {envelope_or_payload!r}."
    )


# ---------------------------------------------------------------------------
# T9 — success path unchanged
# ---------------------------------------------------------------------------


async def test_t9_success_path_with_all_declared_kwargs_unchanged(caplog):
    """No translation occurs on a valid call; no WARNING/ERROR emitted.

    The complement to T3/T4: if the implementation incorrectly
    translates every call, the response to ``echo("x")`` would be an
    envelope rather than the tool's return value, and the test fails on
    the equality assertion. The log assertion adds a second
    discriminator: the envelope path emits a WARNING via
    ``_envelope_error_kind``, and the success path must not.
    """
    mcp = _LoggingFastMCP("test_t9")

    @mcp.tool()
    async def echo(declared: str) -> dict:
        return {"echoed": declared}

    with caplog.at_level(logging.INFO, logger="sage.mcp_server"):
        result = await mcp.call_tool("echo", {"declared": "x"})

    payload = _decode_envelope(result)
    assert payload == {"echoed": "x"}, (
        f"Success path returned {payload!r}; expected the tool's echo dict."
    )

    elevated = [
        rec
        for rec in caplog.records
        if rec.name == "sage.mcp_server" and rec.levelno >= logging.WARNING
    ]
    assert not elevated, (
        f"Success path emitted unexpected WARNING/ERROR records: "
        f"{[(rec.levelno, rec.getMessage()) for rec in elevated]!r}"
    )
