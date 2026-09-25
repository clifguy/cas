"""How a stored adapter version is ordered against the version a repair needs.

The comparison gates which documents the ingestion backfills re-examine, so
each shape a stored version can take is pinned here rather than inferred from
the three-part versions the adapters happen to declare today.
"""

from __future__ import annotations

import pytest

from sage.services.ingestion import _projected_before


@pytest.mark.parametrize(
    ("adapter_version", "since", "expected"),
    [
        # Components are integers: a lexical comparison orders 0.10 before 0.9.
        ("0.10.0", (0, 9, 0), False),
        ("0.9.0", (0, 10, 0), True),
        # A short spelling is zero-padded rather than ordered before its extensions.
        ("0.6", (0, 6, 0), False),
        ("0.5", (0, 6, 0), True),
        ("0.6.0", (0, 6), False),
        # Equal is not before.
        ("0.6.0", (0, 6, 0), False),
        ("0.6.1", (0, 6, 0), False),
    ],
)
def test_a_readable_version_is_ordered_numerically(
    adapter_version: str, since: tuple[int, ...], expected: bool
) -> None:
    assert _projected_before(adapter_version, since) is expected


@pytest.mark.parametrize("adapter_version", [None, "", "0.6.x", "v0.6.0", "0..6"])
def test_an_unreadable_version_is_treated_as_older(adapter_version: str | None) -> None:
    """Re-examining a document costs a read; skipping one that needed repair costs the repair."""
    assert _projected_before(adapter_version, (0, 6, 0)) is True
