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

import pytest
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from sage.api.errors import (
    InvalidParameterError,
    translate_validation_error,
    validation_error_envelope,
)
from sage.models.schemas import BulkLifecycleRequest, DiscoverRequest


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
    err = validation_error_envelope(_discover_error(query="x", filters={"document_ids": ["ok", 5]}))

    assert err.code == "invalid_filter_shape"


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
