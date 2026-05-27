"""Unit tests for the generic list-field patch primitive.

Covers the ops-object applier (`MetadataService._apply_list_field_patch`)
and the parameterized conflict errors
(`ListFieldAddConflictError` / `ListFieldRemoveConflictError`) that
implement CAS-ADR-038's Primitive A across every list-valued metadata
field, not just `tags`.

The primitive operates on three pieces of state:
- the field's current value (a list[str]),
- a `ListFieldPatch` carrying `add` and `remove`,
- the field name (used to build the error code and detail keys).

Strict-conflict semantics: add-of-present and remove-of-absent both
raise. Order discipline: survivors keep their stored position; new
additions append in the order the caller supplied them.
"""

from __future__ import annotations

import pytest

from sage.api.errors import (
    ListFieldAddConflictError,
    ListFieldRemoveConflictError,
)
from sage.models.schemas import ListFieldPatch
from sage.services.metadata import MetadataService

# ---------------------------------------------------------------------------
# A1-A3, A6: happy paths
# ---------------------------------------------------------------------------


def test_list_field_patch_add_only_extends_current_value():
    """add-only: survivors keep position; additions append in input order."""
    patch = ListFieldPatch(add=["c"])
    result = MetadataService._apply_list_field_patch(
        document_id="doc_x", current=["a", "b"], patch=patch, field_name="tags"
    )
    assert result == ["a", "b", "c"]


def test_list_field_patch_remove_only_drops_specified_value():
    """remove-only: survivors keep position; named values are dropped."""
    patch = ListFieldPatch(remove=["b"])
    result = MetadataService._apply_list_field_patch(
        document_id="doc_x", current=["a", "b", "c"], patch=patch, field_name="tags"
    )
    assert result == ["a", "c"]


def test_list_field_patch_add_and_remove_compose_in_one_call():
    """add + remove in one call: removes apply, then additions append."""
    patch = ListFieldPatch(add=["c"], remove=["a"])
    result = MetadataService._apply_list_field_patch(
        document_id="doc_x", current=["a", "b"], patch=patch, field_name="tags"
    )
    assert result == ["b", "c"]


def test_list_field_patch_none_current_treated_as_empty():
    """current=None is treated as an empty list; add-only against None
    produces the additions in supplied order."""
    patch = ListFieldPatch(add=["a", "b"])
    result = MetadataService._apply_list_field_patch(
        document_id="doc_x", current=None, patch=patch, field_name="tags"
    )
    assert result == ["a", "b"]


# ---------------------------------------------------------------------------
# A4-A5: conflict envelopes (field-keyed)
# ---------------------------------------------------------------------------


def test_list_field_patch_add_conflict_raises_with_field_keyed_envelope():
    """Adding a value already present raises ListFieldAddConflictError; the
    error code is `{field}_add_conflict`, detail carries `current_{field}`
    holding the stored list and `{field}` holding the conflicting subset."""
    patch = ListFieldPatch(add=["a", "c"])
    with pytest.raises(ListFieldAddConflictError) as info:
        MetadataService._apply_list_field_patch(
            document_id="doc_x", current=["a", "b"], patch=patch, field_name="tags"
        )
    err = info.value
    assert err.code == "tags_add_conflict"
    assert err.status_code == 400
    assert err.detail["document_id"] == "doc_x"
    assert err.detail["tags"] == ["a"]
    assert err.detail["current_tags"] == ["a", "b"]


def test_list_field_patch_remove_conflict_raises_with_field_keyed_envelope():
    """Removing a value absent raises ListFieldRemoveConflictError; same
    envelope shape as add-conflict, code `{field}_remove_conflict`."""
    patch = ListFieldPatch(remove=["a", "z"])
    with pytest.raises(ListFieldRemoveConflictError) as info:
        MetadataService._apply_list_field_patch(
            document_id="doc_x", current=["a", "b"], patch=patch, field_name="tags"
        )
    err = info.value
    assert err.code == "tags_remove_conflict"
    assert err.status_code == 400
    assert err.detail["document_id"] == "doc_x"
    assert err.detail["tags"] == ["z"]
    assert err.detail["current_tags"] == ["a", "b"]


# ---------------------------------------------------------------------------
# A7: field-name propagation (anti-hard-coding probe)
# ---------------------------------------------------------------------------


def test_list_field_patch_field_name_propagates_to_add_conflict_envelope():
    """A synthetic non-`tags` field name must flow into the error code and
    the detail key. If `field_name` is not threaded through and a literal
    "tags" leaks back in, this test fails."""
    patch = ListFieldPatch(add=["alpha"])
    with pytest.raises(ListFieldAddConflictError) as info:
        MetadataService._apply_list_field_patch(
            document_id="doc_x",
            current=["alpha"],
            patch=patch,
            field_name="custom_list",
        )
    err = info.value
    assert err.code == "custom_list_add_conflict"
    assert err.detail["custom_list"] == ["alpha"]
    assert err.detail["current_custom_list"] == ["alpha"]
    assert "tags" not in err.detail
    assert "current_tags" not in err.detail


def test_list_field_patch_field_name_propagates_to_remove_conflict_envelope():
    """Mirror of the add probe for the remove path."""
    patch = ListFieldPatch(remove=["alpha"])
    with pytest.raises(ListFieldRemoveConflictError) as info:
        MetadataService._apply_list_field_patch(
            document_id="doc_x",
            current=["beta"],
            patch=patch,
            field_name="custom_list",
        )
    err = info.value
    assert err.code == "custom_list_remove_conflict"
    assert err.detail["custom_list"] == ["alpha"]
    assert err.detail["current_custom_list"] == ["beta"]
