"""Which batch-entry defect a refusal reports, when there are several.

Both batch surfaces -- the Core API upload route and the MCP bulk ingest tool --
report one undeclared key through ``undeclared_entry_key_error``, and one
codes-and-tags conflict through ``codes_and_tags_conflict_error``. The order and
the location are pinned here against literal expectations rather than by
comparing the two surfaces, so a defect in a shared rule cannot pass by agreeing
with itself.

The accepted set is the caller's to supply and is not part of the order, so the
undeclared-key cases pass a marker pair whose members differ by depth: a rule
that read the wrong depth's set would still choose the right key and return the
wrong names.
"""

from __future__ import annotations

import pytest

from sage.api.errors import (
    InvalidParameterError,
    UndeclaredKeyError,
    codes_and_tags_conflict_error,
    undeclared_entry_key_error,
)

#: Distinguishable by depth, so the reported set proves which depth was read.
_RECOGNIZED = {0: ("entry_name",), 1: ("nested_name",)}


def _reported(candidates: list[tuple[int, int, str, object]]) -> tuple[str, str, list[str]]:
    error = undeclared_entry_key_error(candidates, recognized_by_depth=_RECOGNIZED)
    assert isinstance(error, UndeclaredKeyError)
    assert error.code == "undeclared_key"
    return error.detail["parameter"], error.detail["key"], error.detail["recognized"]


def test_lowest_file_index_wins_over_depth_and_key():
    """An entry-level key on a later file loses to a nested key on an earlier one."""
    assert _reported([(1, 0, "alpha", 1), (0, 1, "zeta", 2)]) == (
        "files.0.parsed_metadata",
        "zeta",
        ["nested_name"],
    )


def test_entry_key_wins_over_parsed_metadata_key_on_the_same_file():
    """Within one file, the entry's own key is reported before a nested one."""
    assert _reported([(0, 1, "alpha", 1), (0, 0, "zeta", 2)]) == (
        "files.0",
        "zeta",
        ["entry_name"],
    )


def test_first_key_in_sorted_order_wins_at_the_same_depth():
    """At one file and depth, the alphabetically first key is reported."""
    assert _reported([(2, 1, "zeta", 1), (2, 1, "alpha", 2)]) == (
        "files.2.parsed_metadata",
        "alpha",
        ["nested_name"],
    )


def test_no_candidates_reports_nothing():
    assert undeclared_entry_key_error([], recognized_by_depth=_RECOGNIZED) is None


def _conflict(candidates: list[tuple[int, object]]) -> tuple[str, object]:
    error = codes_and_tags_conflict_error(candidates)
    assert isinstance(error, InvalidParameterError)
    assert error.code == "invalid_parameter"
    return error.detail["parameter"], error.detail["value"]


def test_codes_and_tags_conflict_is_located_at_tags():
    """The entry supplying both is reported at its ``tags``, not its ``codes``.

    The value comes back rendered: a list is not one of the envelope's JSON
    native types, so ``InvalidParameterError`` renders it the way it renders
    every other list-valued parameter.
    """
    assert _conflict([(2, ["alpha"])]) == ("files.2.parsed_metadata.tags", "['alpha']")


def test_lowest_file_index_wins_among_conflicting_entries():
    """Several entries carrying both report the earliest one."""
    assert _conflict([(3, ["late"]), (1, ["early"])]) == (
        "files.1.parsed_metadata.tags",
        "['early']",
    )


def test_no_conflict_reports_nothing():
    assert codes_and_tags_conflict_error([]) is None


def test_the_single_document_boundary_refuses_the_same_pair():
    """``IngestRequest`` refuses codes-and-tags with the batch surfaces' envelope.

    The rationale the batch boundaries refuse on -- the two set the same field,
    so supplying both leaves the outcome to walk order -- holds verbatim of the
    single-document surface, which accepted the pair and let dict order decide.
    It refuses through a different mechanism (a model validator, since there is
    no per-entry boundary to check), so what is pinned here is that a caller
    meets the same code and the same explanation on either, located at each
    surface's own spelling.

    Anti-coincidental-pass: the control asserts each key alone still passes, so
    a validator refusing any ``tags`` at all -- or refusing every request --
    fails. The constraint is compared against the shared constant rather than a
    literal, so the two surfaces cannot drift into different wording while both
    stay green.
    """
    from pydantic import ValidationError

    from sage.api.errors import CODES_AND_TAGS_CONSTRAINT, translate_validation_error
    from sage.models.schemas import IngestRequest

    with pytest.raises(ValidationError) as raised:
        IngestRequest(source="/x.md", metadata={"codes": ["PV06"], "tags": ["alpha"]})
    error = translate_validation_error(raised.value)

    assert isinstance(error, InvalidParameterError)
    assert error.code == "invalid_parameter"
    assert error.detail["parameter"] == "metadata.tags"
    assert error.detail["constraint"] == CODES_AND_TAGS_CONSTRAINT
    # The batch surfaces report the same code and constraint, located at their
    # own spelling -- one rule, two boundaries.
    batch = codes_and_tags_conflict_error([(0, ["alpha"])])
    assert batch is not None
    assert batch.code == error.code
    assert batch.detail["constraint"] == error.detail["constraint"]
    assert batch.detail["parameter"] == "files.0.parsed_metadata.tags"

    # Control: either key alone is still accepted.
    assert IngestRequest(source="/x.md", metadata={"tags": ["alpha"]}).metadata == {
        "tags": ["alpha"]
    }
    assert IngestRequest(source="/x.md", metadata={"codes": ["PV06"]}).metadata == {
        "codes": ["PV06"]
    }
