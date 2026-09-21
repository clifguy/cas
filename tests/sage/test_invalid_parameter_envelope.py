"""Unit tests for the generic request-validation envelope.

``translate_validation_error`` is the *semantic* layer: it recognises the
mode/filters/typed-alias families and returns ``None`` for everything else.
That ``None`` used to fall through to a raw Pydantic rendering. These tests
pin ``validation_error_envelope`` -- the wrapper that guarantees every
``ValidationError`` reaches a structured envelope (CAS-ADR-028) -- and pin
the property that makes the envelope safe to show a caller: it is built
from the structured fields of ``exc.errors()``, never from ``str(exc)``,
so no model class name and no documentation URL can reach the payload.

The translator's own ``None``-for-unmatched contract is asserted here too,
because widening the translator itself into a catch-all would silently
convert every unmatched validation failure on every FastAPI router.
"""

from __future__ import annotations

import json
import re

import pytest
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, ValidationError

from sage.api.errors import (
    _ENUM_TYPED_FILTER_FIELDS,
    _FILTER_FIELD_TYPE_NAMES,
    InvalidParameterError,
    _model_for_loc,
    translate_validation_error,
    unknown_parameter_names,
    validation_error_envelope,
)
from sage.models.schemas import (
    BulkLifecycleItem,
    BulkLifecycleRequest,
    BulkMetadataRequest,
    DiscoverRequest,
    IngestRequest,
    RelocationPointer,
    RetrievalFilters,
    Tier3Patch,
)


def _discover_error(**kwargs) -> ValidationError:
    """Return the ValidationError raised by constructing DiscoverRequest."""
    with pytest.raises(ValidationError) as exc_info:
        DiscoverRequest(**kwargs)
    return exc_info.value


def _payload_text(err) -> str:
    """Serialize the caller-facing envelope fields to one searchable string."""
    return json.dumps(
        {"code": err.code, "message": err.message, "detail": err.detail},
        default=str,
    )


# ---------------------------------------------------------------------------
# A1-A2 -- the reference case: limit over its cap
# ---------------------------------------------------------------------------


def test_limit_over_cap_yields_invalid_parameter_envelope():
    """A bound violation on a non-filter field becomes a typed envelope.

    Reference case for the request-model seam: ``limit`` is capped on
    ``DiscoverRequest``, not on the tool signature, so the failure lands
    where the semantic translator has no rule.
    """
    err = validation_error_envelope(_discover_error(query="x", limit=200))

    assert isinstance(err, InvalidParameterError)
    assert err.code == "invalid_parameter"
    assert err.status_code == 422
    assert err.detail is not None
    assert err.detail["parameter"] == "limit"
    assert err.detail["value"] == 200


def test_limit_envelope_states_cap_and_hints_at_offset():
    """The envelope names the cap and the remedy.

    The cap (100) comes from the structured constraint; the ``offset``
    remedy from the hint table. Both must also reach the message, which is
    what a caller reading a flat log line sees.
    """
    err = validation_error_envelope(_discover_error(query="x", limit=200))

    assert "100" in err.detail["constraint"]
    assert "offset" in err.detail["hint"]
    assert "100" in err.message
    assert "offset" in err.message


def test_envelope_omits_model_name_and_docs_url():
    """No internal model name and no pydantic.dev URL reach the caller.

    This is the anti-coincidental guard for the test above: ``str(exc)``
    also contains "100", so an implementation that pasted the raw Pydantic
    rendering would satisfy the cap assertion while leaking the model class
    name and the documentation URL. Asserting on the whole serialized
    payload -- not just the message -- catches a leak in ``detail`` too.
    """
    text = _payload_text(validation_error_envelope(_discover_error(query="x", limit=200)))

    assert "DiscoverRequest" not in text
    assert "pydantic.dev" not in text
    assert "errors.pydantic" not in text
    assert "validation error for" not in text


def test_limit_below_zero_yields_invalid_parameter_envelope():
    """``limit`` accepts zero, the count-only request, and nothing below it.

    The constraint must name 0 as the floor: a floor left at 1 would still
    refuse -1, so the refusal alone cannot tell the two bounds apart. The
    offset remedy answers the cap, not the floor, so it is absent here.
    """
    err = validation_error_envelope(_discover_error(limit=-1))

    assert err.code == "invalid_parameter"
    assert err.status_code == 422
    assert err.detail["parameter"] == "limit"
    assert err.detail["value"] == -1
    assert "0" in err.detail["constraint"]
    assert "1" not in err.detail["constraint"]
    assert "hint" not in err.detail
    assert "offset" not in err.message


# ---------------------------------------------------------------------------
# A4-A5 -- other non-filter fields, and the optional hint
# ---------------------------------------------------------------------------


def test_wrong_typed_non_filter_field_yields_envelope():
    """A type-coercion failure on a non-filter field is normalized too."""
    err = validation_error_envelope(_discover_error(query="x", limit="abc"))

    assert err.code == "invalid_parameter"
    assert err.detail["parameter"] == "limit"
    assert err.detail["value"] == "abc"
    assert "integer" in err.detail["constraint"]


def test_offset_lower_bound_yields_envelope_without_a_hint():
    """A parameter with no hint-table entry carries no ``hint`` key.

    Guards against a one-size-fits-all hint appended unconditionally, which
    would make the ``offset``-remedy assertion above pass for the wrong
    reason. The hint is a curated remedy or it is absent.
    """
    err = validation_error_envelope(_discover_error(query="x", offset=-1))

    assert err.code == "invalid_parameter"
    assert err.detail["parameter"] == "offset"
    assert "hint" not in err.detail


def test_nested_parameter_is_reported_as_a_dotted_path():
    """A nested location keeps its full path, including the list index.

    Batch requests fail one item deep. Reporting only the leaf name would
    tell a caller which field is wrong but not which of fifty items carries
    it, so the index has to survive into the envelope.
    """
    with pytest.raises(ValidationError) as exc_info:
        BulkLifecycleRequest(items=[{"document_id": "abcd1234_sample", "action": 17}])

    err = validation_error_envelope(exc_info.value)

    assert err.code == "invalid_parameter"
    assert err.detail["parameter"] == "items.0.action"


def test_filter_scoped_code_wins_over_the_generic_one_when_nested():
    """A nested location inside `filters` still gets the specific code.

    The generic envelope is the remainder, never a replacement: a shape
    error one level down in `filters` is exactly what `invalid_filter_shape`
    exists to report, and it keeps reporting it.
    """
    # The id entry must be well-formed. ``document_ids`` carries the
    # document-id alias on its element type, so a malformed *value* raises
    # ``invalid_document_id`` ahead of the shape branch -- a different rule
    # than the one under test here.
    err = validation_error_envelope(
        _discover_error(query="x", filters={"document_ids": ["deadbeef_ok", 5]})
    )

    assert err.code == "invalid_filter_shape"


def test_malformed_element_type_and_value_take_different_branches():
    """A bad element *type* reports the filter shape; a bad element *value*
    reports the id grammar.

    Both failures sit at the same nested location. The type failure is raised
    by Pydantic before the element's AfterValidator ever runs, so it keeps the
    container-shaped envelope; the value failure comes from the validator and
    names the offending id. Stating the split makes the translator's branch
    ordering deliberate rather than emergent.

    ``expected_type`` is asserted alongside ``received_type`` because the two
    branches have to agree on what a well-formed value is. The shape envelope
    names a remedy; if the remedy were ``list[str]``, a caller who followed it
    exactly could still be refused by the value branch, and the pair of
    messages would contradict each other. Asserting only ``received_type``
    leaves that contradiction unpinned.
    """
    shape = validation_error_envelope(
        _discover_error(mode="catalog", filters={"document_ids": [42]})
    )
    assert shape.code == "invalid_filter_shape"
    assert shape.detail["field"] == "document_ids"
    assert shape.detail["received_type"] == "int"
    assert shape.detail["expected_type"] == "list[DocumentIdStr]"

    value = validation_error_envelope(
        _discover_error(mode="catalog", filters={"document_ids": ["not-a-doc-id"]})
    )
    assert value.code == "invalid_document_id"
    assert value.detail["document_id"] == "not-a-doc-id"
    # The index is not carried into the envelope on either transport; the
    # offending value alone identifies the entry. Pinned so the collection
    # form's documented contract and the envelope cannot drift apart.
    assert "index" not in value.detail, value.detail


# ---------------------------------------------------------------------------
# A7 -- FastAPI's transport-component prefix
# ---------------------------------------------------------------------------


def test_transport_prefix_is_stripped():
    """A RequestValidationError's leading "body" segment is not a parameter.

    FastAPI prepends the request component the value came from. Reporting
    ``body.limit`` back to an HTTP caller would name something they did not
    send.
    """
    inner = _discover_error(query="x", limit=200)
    wrapped = RequestValidationError(
        [{**err, "loc": ("body", *err["loc"])} for err in inner.errors()]
    )

    err = validation_error_envelope(wrapped)

    assert err.detail["parameter"] == "limit"


@pytest.mark.parametrize("name", ["query", "path", "body"])
def test_parameter_named_like_a_transport_segment_is_not_stripped(name):
    """Only a RequestValidationError carries a transport segment to strip.

    A validation error raised by a model directly -- on the MCP surface, or
    inside an operation -- has no such segment, so its first location is the
    parameter itself. Stripping it anyway turns ``query`` into no parameter at
    all, and a caller is told ``request`` failed rather than the field it sent.
    ``DiscoverRequest`` really has a ``query`` field; the other two names are
    rejected as unknown and read through ``unknown_parameter_names``.
    """
    if name == "query":
        err = validation_error_envelope(_discover_error(query=[1]))
        assert err.detail["parameter"] == "query"
    else:
        assert unknown_parameter_names(_discover_error(**{name: 1})) == [name]


@pytest.mark.parametrize("name", ["query", "path", "body"])
def test_transport_segment_stripped_once_from_a_request_validation_error(name):
    """The HTTP form keeps stripping: its first segment is the transport."""
    inner = _discover_error(**{name: 1})
    wrapped = RequestValidationError(
        [{**err, "loc": ("body", *err["loc"])} for err in inner.errors()]
    )

    if name == "query":
        assert validation_error_envelope(wrapped).detail["parameter"] == "query"
    else:
        assert unknown_parameter_names(wrapped) == [name]


# ---------------------------------------------------------------------------
# A8 -- the translator keeps its scoping
# ---------------------------------------------------------------------------


def test_translator_still_returns_none_for_unmatched():
    """The catch-all lives in the wrapper, not in the translator.

    ``translate_validation_error`` is shared by both transports and by the
    FastAPI ``RequestValidationError`` handler. Widening it into a catch-all
    would change the behavior of every router that relies on its ``None``,
    far beyond the seams under test. This asserts the seam stayed where it
    was put.
    """
    assert translate_validation_error(_discover_error(query="x", limit=200)) is None


def test_wrapper_defers_to_the_translator_when_a_rule_matches():
    """A semantically-translated error is not replaced by the generic one."""
    err = validation_error_envelope(_discover_error(query="x", filters={"nope": 1}))

    assert err.code == "unknown_filter_key"
    assert err.detail["valid_keys"]


def test_every_non_enum_filter_key_names_its_expected_type():
    """No filter key reports its remedy as ``unknown``.

    ``_FILTER_FIELD_TYPE_NAMES`` is hand-maintained rather than
    introspected, for the reason stated where it is declared. The cost of
    that is a table that goes stale silently: the lookup falls back to
    ``"unknown"``, so a key added to ``RetrievalFilters`` without an entry
    still produces a well-formed 400 -- one that tells the caller the value
    was the wrong shape and then declines to say what the right shape is.
    Nothing else in the suite reads the table's completeness, so the
    omission survives a green run.

    Enum-typed keys are excluded rather than allowlisted: a bad value on
    those raises ``invalid_filter_value`` and never reaches this table, so
    an entry for one would be dead weight that the next reader has to
    re-derive as deliberate.

    Membership only, and the limit is worth stating: an entry naming the
    *wrong* type passes here. Deriving the right name from the annotation
    is what the table declines to do, for the reason given where it is
    declared, so a gate that checked the values would have to re-derive
    exactly what was rejected. Individual entries are pinned by the
    envelope tests above, which assert a remedy a caller can follow.
    """
    expected_keys = set(RetrievalFilters.model_fields) - set(_ENUM_TYPED_FILTER_FIELDS)
    missing = sorted(expected_keys - set(_FILTER_FIELD_TYPE_NAMES))
    assert not missing, (
        f"filter keys with no expected_type entry, so an invalid-shape envelope "
        f"reports their remedy as 'unknown': {missing}"
    )
    stale = sorted(set(_FILTER_FIELD_TYPE_NAMES) - expected_keys)
    assert not stale, (
        f"expected_type entries naming no non-enum filter field; the key was "
        f"renamed, removed, or became enum-typed: {stale}"
    )


# ---------------------------------------------------------------------------
# The nested-key rule: resolving which model refused, and naming its fields
# ---------------------------------------------------------------------------


def test_model_for_loc_resolves_an_optional_nested_model():
    """An ``X | None`` field resolves to ``X``.

    The optional wrapper is the commonest nesting shape in the request
    models, and the arm that matters is the only BaseModel arm.
    """
    assert _model_for_loc(IngestRequest, ("relocated_from", "bogus")) is RelocationPointer


def test_model_for_loc_skips_list_indices():
    """An integer segment names a position, not a field, and is stepped over.

    Without this the walk would look for a field named ``0`` on the list's
    element type and give up one segment short of the model that refused.
    """
    loc = ("items", 0, "tier3_metadata", "bogus")

    assert _model_for_loc(BulkMetadataRequest, loc) is Tier3Patch


def test_model_for_loc_resolves_the_root_for_a_depth_one_loc():
    """A one-segment location resolves to the root model itself.

    The MCP surface validates each item of a batch as its own root, so the
    location it reports for an undeclared item key is one segment deep. The
    HTTP surface reports the same key as ``items.<n>.<key>``. One rule has
    to serve both, so the walk cannot key on depth.
    """
    assert _model_for_loc(BulkLifecycleItem, ("bogus",)) is BulkLifecycleItem


def test_model_for_loc_returns_none_for_an_unresolvable_path():
    """A path that runs through a non-model field resolves to nothing.

    ``None`` is the signal to leave the envelope as it was, so an
    unresolvable location costs a caller nothing it has today.
    """
    assert _model_for_loc(IngestRequest, ("source", "bogus")) is None


def test_model_for_loc_returns_none_for_a_multi_model_union():
    """A union with two model arms is not resolved, deliberately.

    Which arm refused is not knowable from the location alone -- the
    validator tried both -- so picking one would name a field set the
    caller was not refused against. No request model has this shape today;
    the walk declines it rather than growing a wrong answer for the day one
    does.
    """

    class _Left(BaseModel):
        model_config = ConfigDict(extra="forbid")
        left: str | None = None

    class _Right(BaseModel):
        model_config = ConfigDict(extra="forbid")
        right: str | None = None

    class _Either(BaseModel):
        model_config = ConfigDict(extra="forbid")
        arm: _Left | _Right | None = None

    assert _model_for_loc(_Either, ("arm", "bogus")) is None


def _ingest_nested_error() -> ValidationError:
    """The error raised by an undeclared key inside ``relocated_from``.

    Four ``missing`` errors for the pointer's required fields are reported
    ahead of the ``extra_forbidden`` one, which is why the rule scans every
    error rather than reading the first.
    """
    with pytest.raises(ValidationError) as exc_info:
        IngestRequest(source="/tmp/x.md", relocated_from={"bogus": 1})
    return exc_info.value


def test_nested_undeclared_key_names_the_refusing_models_fields():
    """The refusal carries the field set of the model that refused.

    Naming the offending key alone costs a round trip, and the operating
    rule forbids retrying a refused call with different phrasing, so the
    accepted set has to arrive with the refusal.
    """
    err = validation_error_envelope(_ingest_nested_error(), root_model=IngestRequest)

    assert err.code == "undeclared_key"
    assert err.status_code == 400
    assert err.detail["parameter"] == "relocated_from"
    assert err.detail["key"] == "bogus"
    assert err.detail["recognized"] == sorted(RelocationPointer.model_fields)


def test_the_example_is_built_from_the_recognized_names_at_the_refusing_location():
    """The example shows the accepted names at the place they go.

    Asserting only that an ``example`` is present passes against a constant
    string, and against one built from the wrong model -- which is the whole
    property the renderer claims. So the names it shows are checked against
    ``recognized``, and every name it shows is checked to be one of them: a
    fragment naming a key the refusal does not accept would send the caller
    back for a second round trip, which is the cost this envelope exists to
    remove. The location is asserted too, because the accepted names alone
    leave the object they belong to unplaced.
    """
    err = validation_error_envelope(_ingest_nested_error(), root_model=IngestRequest)
    example = err.detail["example"]
    recognized = err.detail["recognized"]

    assert example.startswith(f"{err.detail['parameter']}=")
    shown = re.findall(r'"([^"]+)":', example)
    assert shown, example
    assert set(shown) <= set(recognized), (shown, recognized)
    assert shown == recognized[: len(shown)]
    assert err.detail["key"] not in shown


def test_nested_undeclared_key_without_a_root_model_stays_invalid_parameter():
    """Without a root model the envelope is exactly what it was.

    The same exception object as the case above, so the only difference is
    the argument. A rule that fired unconditionally would still pass a test
    that built its own error and happened to be unresolvable.
    """
    err = validation_error_envelope(_ingest_nested_error())

    assert err.code == "invalid_parameter"
    assert isinstance(err, InvalidParameterError)


def test_the_nested_rule_scans_past_earlier_unrelated_errors():
    """The undeclared key wins over the ``missing`` errors reported before it.

    ``_generic_parameter_error`` reports ``errors()[0]``, which here is a
    ``missing`` complaint about a field the caller never meant to send. An
    implementation placed there would name the wrong problem, and would do
    so only for the locations where Pydantic happens to order the errors
    that way.
    """
    errors = _ingest_nested_error().errors()
    assert errors[0]["type"] != "extra_forbidden", "fixture no longer exercises the ordering"

    err = validation_error_envelope(_ingest_nested_error(), root_model=IngestRequest)

    assert err.code == "undeclared_key"
    assert "missing" not in err.message.lower()


def test_filter_scoped_code_still_wins_over_the_nested_rule():
    """``filters`` keeps its own code now that a general one exists.

    ``RetrievalFilters`` resolves through the walk like any other nested
    model, so the general rule would answer for it too and retire
    ``unknown_filter_key`` without a word. The order of the branches is
    what prevents that, and nothing else states it.
    """
    err = validation_error_envelope(
        _discover_error(query="x", filters={"nope": 1}), root_model=DiscoverRequest
    )

    assert err.code == "unknown_filter_key"
    assert err.detail["valid_keys"]


def test_a_single_undeclared_key_keeps_its_envelope():
    """The paired control: one key reads exactly as it did, plus ``keys``.

    The message is pinned literally, so a format built for several keys
    cannot leak into the single-key path unnoticed.
    """
    err = validation_error_envelope(_ingest_nested_error(), root_model=IngestRequest)
    recognized = sorted(RelocationPointer.model_fields)

    assert (err.code, err.status_code) == ("undeclared_key", 400)
    assert err.detail["parameter"] == "relocated_from"
    assert err.detail["key"] == "bogus"
    assert err.detail["keys"] == ["bogus"]
    assert err.detail["recognized"] == recognized
    assert err.message == (
        f"'relocated_from.bogus' is not a declared key. Accepted: {recognized!r}. "
        f"Example: {err.detail['example']}"
    )


def test_every_undeclared_key_in_one_object_is_named():
    """Two undeclared keys under one object are refused once, both named, sorted.

    Written in reverse sorted order, so a refusal echoing the validator's
    order, or naming only the first it lists, fails.
    """
    with pytest.raises(ValidationError) as exc_info:
        IngestRequest(source="/tmp/x.md", relocated_from={"zulu": 1, "bogus": 2})
    err = validation_error_envelope(exc_info.value, root_model=IngestRequest)

    assert err.code == "undeclared_key"
    assert err.detail["parameter"] == "relocated_from"
    assert err.detail["keys"] == ["bogus", "zulu"]
    assert err.detail["key"] == "bogus"
    assert err.message.startswith(
        "'relocated_from.bogus', 'relocated_from.zulu' are not declared keys."
    )
    assert "other locations" not in err.message


class _Leaf(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None


class _Pair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    first: _Leaf | None = None
    second: _Leaf | None = None


def test_keys_at_another_location_are_left_to_a_later_refusal_and_said_so():
    """One object per refusal; the message says others are checked after repair."""
    with pytest.raises(ValidationError) as exc_info:
        _Pair.model_validate({"first": {"zz": 1, "aa": 2}, "second": {"mm": 3}})
    err = validation_error_envelope(exc_info.value, root_model=_Pair)

    assert err.code == "undeclared_key"
    assert err.detail["parameter"] == "first"
    assert err.detail["keys"] == ["aa", "zz"]
    assert err.message.endswith(
        "Undeclared keys at other locations are reported once these are repaired."
    )
