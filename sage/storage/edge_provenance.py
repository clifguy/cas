"""Rationale-prefix to ``RationaleKind`` mapping and derivation helper.

CAS-ADR-019 introduced a rationale-text prefix convention as the
provenance discriminator for auto-inferred edges. T-0080 promoted that
convention to a typed, indexed column on the edges table.

This module is the single source of truth for the prefix → kind map.
The storage layer's one-time backfill SQL in ``sage.storage.migrations``
is generated from the same mapping (verified by
``tests/sage/test_rationale_kind.py::test_t2_backfill_sql_mirrors_helper_map``)
so the two surfaces cannot drift.
"""

from __future__ import annotations

from sage.models.enums import RationaleKind

RATIONALE_PREFIX_TO_KIND: dict[str, RationaleKind] = {
    "[version_chain]": RationaleKind.VERSION_CHAIN,
    "[references_mention]": RationaleKind.REFERENCES_MENTION,
    "[filename_code_match]": RationaleKind.FILENAME_CODE_MATCH,
}


def derive_rationale_kind(rationale: str | None) -> RationaleKind:
    """Map a rationale string to a ``RationaleKind`` via prefix matching.

    Returns ``RationaleKind.MANUAL`` when ``rationale`` is None, empty,
    or starts with no recognized prefix. The match is strict-prefix:
    an embedded prefix substring does not count.
    """
    if not rationale:
        return RationaleKind.MANUAL
    for prefix, kind in RATIONALE_PREFIX_TO_KIND.items():
        if rationale.startswith(prefix):
            return kind
    return RationaleKind.MANUAL
