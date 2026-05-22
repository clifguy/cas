"""Shared sentinel-row scaffold for SQLite-Row closure tests (T-0145).

The T-0109 cohort's `sqlite3.Row` projection closure tests
(T-0123 ``_row_to_edge``, T-0124 parity, T-0125 ``_row_to_staging_edge``)
each need a real ``sqlite3.Row`` carrying a column->value sentinel
mapping. A real row, not a ``dict`` stand-in, is required because the
production factories use ``"<col>" in row.keys()`` defensive reads and
column-lookup semantics that differ from dict (see e.g.
``GraphStore._row_to_edge`` in ``sage/storage/graph_store.py``).

The scaffold for building such a row (open an in-memory connection, set
``row_factory=sqlite3.Row``, ``SELECT ? AS col, ? AS col, ...``, fetch
one, close) is identical across the three closure tests. Only the
column list and sentinel values vary. This module extracts the
scaffold; per the *Projection-Point Closure Cohort -- Canonical
Decisions* reference document, the sentinel *values* remain per-ticket.
"""

from __future__ import annotations

import sqlite3
from typing import Any


def build_sentinel_row(columns: dict[str, Any]) -> sqlite3.Row:
    """Build a real ``sqlite3.Row`` carrying ``columns``' key/value pairs.

    The returned row matches the production factory's row-shape
    expectations: column names are queryable via ``row["<name>"]``,
    ``row.keys()`` returns the column names in dict insertion order,
    and ``"<name>" in row.keys()`` is the canonical membership idiom
    that production factories use to guard optional CTE-stripped
    columns.

    Column names are interpolated literally into the SELECT statement;
    callers must treat the dict's keys as trusted. This is test-only
    code with internal callers, so there is no SQL-injection surface
    in practice -- but the literal-interpolation contract is worth
    naming explicitly.

    Args:
        columns: Mapping from column name to sentinel value. Order is
            preserved in the resulting ``row.keys()``. Must be
            non-empty; an empty dict raises ``ValueError`` because a
            zero-column SELECT is a programming error.

    Returns:
        A ``sqlite3.Row`` carrying every (name, value) pair.

    Raises:
        ValueError: If ``columns`` is empty.
    """
    if not columns:
        raise ValueError("build_sentinel_row requires at least one column")

    conn = sqlite3.connect(":memory:")
    try:
        conn.row_factory = sqlite3.Row
        select_clause = ", ".join(f"? AS {name}" for name in columns)
        cursor = conn.execute(f"SELECT {select_clause}", tuple(columns.values()))
        row = cursor.fetchone()
    finally:
        conn.close()
    assert row is not None
    return row
