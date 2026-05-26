"""Unit tests for the shared sentinel-row scaffold.

The helper at ``tests/sage/_sentinel_rows.py`` is load-bearing for the
cohort's ``sqlite3.Row`` projection closure tests (,
). These tests pin the helper's contract so a refactor
or future variant cannot silently regress the row-shape semantics
that the production factories depend on.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from tests.sage._sentinel_rows import build_sentinel_row


def test_returns_real_sqlite3_row():
    """U-1: helper returns a real ``sqlite3.Row``, not a dict.

    Anti-coincidental: if the helper returns a dict-like, production
    factories' ``"col" in row.keys()`` reads would behave subtly
    differently than they do in production.
    """
    row = build_sentinel_row({"id": "x"})
    assert isinstance(row, sqlite3.Row)
    assert not isinstance(row, dict)


def test_columns_round_trip_via_subscript():
    """U-2: every column is retrievable via ``row["<name>"]`` with the
    original value preserved across mixed types.
    """
    columns = {
        "name": "sentinel string",
        "count": 42,
        "ratio": 0.875,
        "created_at": datetime(2026, 5, 21, 10, 30, tzinfo=timezone.utc).isoformat(),
    }
    row = build_sentinel_row(columns)
    for name, expected in columns.items():
        assert row[name] == expected, f"column {name!r} did not round-trip"


def test_keys_preserves_insertion_order():
    """U-3: ``row.keys()`` returns exactly the input columns in dict
    insertion order. Production factories read by name (so order is
    not load-bearing for correctness), but readability of the SELECT
    projection benefits from preserved order.
    """
    columns = {
        "id": "id-value",
        "source_id": "source-value",
        "target_id": "target-value",
    }
    row = build_sentinel_row(columns)
    assert list(row.keys()) == list(columns.keys())


def test_membership_idiom_works_for_present_and_absent_columns():
    """U-4: ``"<col>" in row.keys()`` works as production factories
    expect, returning True for present columns and False for absent
    ones. This is the load-bearing semantic that motivates using a
    real ``sqlite3.Row`` rather than a dict.
    """
    row = build_sentinel_row({"present": "x"})
    assert "present" in row.keys()
    assert "absent" not in row.keys()


def test_empty_columns_raises():
    """U-5: empty dict raises ``ValueError`` rather than producing a
    zero-column row. A zero-column SELECT is a programming error;
    fail loud rather than return a row that no production factory
    can read.
    """
    with pytest.raises(ValueError):
        build_sentinel_row({})
