"""`FieldChange` model contract tests.

`FieldChange` is the per-entry shape for the `changes` block on dry-run
responses across `UpdateMetadataResponse`, `SetLifecycleResponse`,
`BulkLifecycleItemResult`, and `BulkMetadataItemResult`. These tests pin
the model's `extra="forbid"` config and `Any`-typed before/after
round-trip behavior, both of which are part of the public contract.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sage.models.schemas import FieldChange


def test_field_change_model_rejects_extra_fields():
    """FieldChange uses extra='forbid'. An unrecognized field
    must raise ValidationError, not be silently dropped — otherwise
    callers parsing dry-run responses can stash arbitrary keys and
    we lose the contract.
    """
    with pytest.raises(ValidationError) as exc_info:
        FieldChange(path="x", before=1, after=2, extra="y")
    # Pydantic v2 reports 'extra_forbidden' as the error type.
    assert "extra" in str(exc_info.value).lower()


def test_field_change_model_round_trip_serialization():
    """Before/after are Any-typed; serialization must round-trip
    for scalar, list (the tags shape), and None (absent-key dry-run
    semantics)."""
    # Scalar variant (tier3 per-key change).
    fc_scalar = FieldChange(path="tier3_metadata.severity", before="low", after="high")
    restored_scalar = FieldChange.model_validate(fc_scalar.model_dump())
    assert restored_scalar == fc_scalar

    # List variant (tags change — full before/after lists, not patch ops).
    fc_list = FieldChange(path="tags", before=["a", "b"], after=["b", "c"])
    restored_list = FieldChange.model_validate(fc_list.model_dump())
    assert restored_list == fc_list

    # None variant (unset case — key removed by the patch).
    fc_none = FieldChange(path="tier3_metadata.owner", before="alice", after=None)
    restored_none = FieldChange.model_validate(fc_none.model_dump())
    assert restored_none == fc_none
    assert restored_none.after is None
