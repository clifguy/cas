"""Pure-data legacy-form detection for the ops-object patch grammar (CAS-ADR-028).

Detects the two pre-patch shapes that the ops-object grammar deprecated:

- Bare list for a list-valued metadata field (per
  ``LIST_VALUED_METADATA_FIELD_NAMES``, kept in sync with
  ``sage.services.metadata.MetadataService.LIST_VALUED_METADATA_FIELDS``
  by a conformance test in ``tests/sage/test_mcp_tool_conformance.py``).
- Bare dict for ``tier3_metadata`` whose keys are not a subset of
  ``{"set", "unset"}`` (a dict whose keys are a subset of that set is a
  valid ``Tier3Patch``; anything else is the legacy "this is the new
  state" form).

This module is intentionally a pure-data leaf: no imports from
``sage.api``, ``sage.services``, or any other layer. The
``sage.models`` package is constrained to leaf status by the
import-linter "Models are a leaf layer" contract, so callers in those
layers translate the returned ``LegacyFormDetails`` into their own
exception types (``sage.api.errors.LegacyFormError`` on the FastAPI /
MCP boundaries; a ``pydantic_core.PydanticCustomError(type='legacy_form')``
inside Pydantic ``model_validator(mode='before')`` calls, which
``sage.api.errors.translate_validation_error`` then converts back to
``LegacyFormError`` for the wire).
"""

from __future__ import annotations

from typing import NamedTuple

# Field names whose mutation request payload is a ``ListFieldPatch``
# (``{"add": [...], "remove": [...]}``). Kept as a name-only set so this
# module stays in the leaf layer; the full descriptor registry lives on
# ``MetadataService`` with a conformance test guarding key-set equality.
LIST_VALUED_METADATA_FIELD_NAMES: frozenset[str] = frozenset({"tags"})


class LegacyFormDetails(NamedTuple):
    """Detection result for the structured ``LegacyFormError`` envelope."""

    field: str
    received_type: str
    example: str


def detect_legacy_form(field: str, value: object) -> LegacyFormDetails | None:
    """Return details describing the legacy shape, or ``None`` if ``value`` is acceptable.

    ``None`` covers both "no value supplied" (caller-omitted field) and
    "value is already the modern ops-object form".
    """
    if value is None:
        return None
    if field in LIST_VALUED_METADATA_FIELD_NAMES:
        if isinstance(value, list):
            return LegacyFormDetails(
                field=field,
                received_type="list",
                example='{"add": [...]} or {"remove": [...]}',
            )
    elif field == "tier3_metadata":
        if isinstance(value, dict) and value and not (set(value) <= {"set", "unset"}):
            return LegacyFormDetails(
                field="tier3_metadata",
                received_type="dict (bare key/value pairs)",
                example='{"set": {"key": "value"}} or {"unset": ["key"]}',
            )
    return None
