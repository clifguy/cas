"""Which undeclared batch-entry key a refusal reports, when there are several.

Both batch surfaces -- the Core API upload route and the MCP bulk ingest tool --
report one undeclared key through ``undeclared_entry_key_error``. The order is
pinned here against literal expectations rather than by comparing the two
surfaces, so a defect in the shared rule cannot pass by agreeing with itself.
"""

from __future__ import annotations

from sage.api.errors import InvalidParameterError, undeclared_entry_key_error


def _reported(candidates: list[tuple[int, int, str, object]]) -> tuple[str, object]:
    error = undeclared_entry_key_error(candidates)
    assert isinstance(error, InvalidParameterError)
    assert error.code == "invalid_parameter"
    return error.detail["parameter"], error.detail["value"]


def test_lowest_file_index_wins_over_depth_and_key():
    """An entry-level key on a later file loses to a nested key on an earlier one."""
    assert _reported([(1, 0, "alpha", 1), (0, 1, "zeta", 2)]) == (
        "files.0.parsed_metadata.zeta",
        2,
    )


def test_entry_key_wins_over_parsed_metadata_key_on_the_same_file():
    """Within one file, the entry's own key is reported before a nested one."""
    assert _reported([(0, 1, "alpha", 1), (0, 0, "zeta", 2)]) == ("files.0.zeta", 2)


def test_first_key_in_sorted_order_wins_at_the_same_depth():
    """At one file and depth, the alphabetically first key is reported."""
    assert _reported([(2, 1, "zeta", 1), (2, 1, "alpha", 2)]) == (
        "files.2.parsed_metadata.alpha",
        2,
    )


def test_no_candidates_reports_nothing():
    assert undeclared_entry_key_error([]) is None
