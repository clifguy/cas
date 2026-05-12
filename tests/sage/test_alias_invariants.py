"""Property-based tests for typed-alias regexes in sage/models/schemas.py.

Companion ticket: T-0012. The example-based tests in test_request_validators.py
cover hand-picked positive and negative cases. These property tests use
Hypothesis to fuzz each alias's accepted and rejected language and lock in
current behavior. If a validator is widened beyond its current behavior
without updating the corresponding negative strategy, the affected negative
test will start passing strings it was meant to reject, surfacing the
regression.

The latent quirks documented in T-0012's plan (re.match + $ trailing-newline
acceptance, DocumentDateStr calendar-correctness gap, EdgeIdStr non-canonical
UUID forms) are deliberately NOT exercised here as negatives. They are
accepted today by design or by Python regex semantics; including them as
negatives would fail tests against current behavior.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import AfterValidator, TypeAdapter, ValidationError

from sage.models.schemas import (
    DocumentDateStr,
    DocumentIdStr,
    EdgeIdStr,
    Sha256Str,
)

ALIAS_SETTINGS = settings(max_examples=100, deadline=200)

_HEX = "0123456789abcdef"
_HEX_UPPER = "0123456789ABCDEF"
_NON_HEX_ALPHA = "ghijklmnopqrstuvwxyz"
_SLUG = "abcdefghijklmnopqrstuvwxyz0123456789_"
_SLUG_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


# ---------------------------------------------------------------------------
# DocumentIdStr -- regex ^[0-9a-f]{8}_[a-z0-9_]+$
# ---------------------------------------------------------------------------

_doc_id_hex_prefix = st.text(alphabet=_HEX, min_size=8, max_size=8)
_doc_id_slug = st.text(alphabet=_SLUG, min_size=1, max_size=64)

DOC_ID_VALID = st.tuples(_doc_id_hex_prefix, _doc_id_slug).map(lambda t: f"{t[0]}_{t[1]}")

DOC_ID_INVALID: dict[str, st.SearchStrategy[str]] = {
    "prefix_too_short": st.tuples(
        st.text(alphabet=_HEX, min_size=0, max_size=7),
        _doc_id_slug,
    ).map(lambda t: f"{t[0]}_{t[1]}"),
    "prefix_too_long": st.tuples(
        st.text(alphabet=_HEX, min_size=9, max_size=16),
        _doc_id_slug,
    ).map(lambda t: f"{t[0]}_{t[1]}"),
    "prefix_uppercase_hex": st.tuples(
        st.text(alphabet=_HEX_UPPER, min_size=8, max_size=8).filter(
            lambda s: any(c.isupper() for c in s)
        ),
        _doc_id_slug,
    ).map(lambda t: f"{t[0]}_{t[1]}"),
    "prefix_non_hex_letter": st.tuples(
        st.lists(
            st.one_of(st.sampled_from(_HEX), st.sampled_from(_NON_HEX_ALPHA)),
            min_size=8,
            max_size=8,
        )
        .map("".join)
        .filter(lambda s: any(c in _NON_HEX_ALPHA for c in s)),
        _doc_id_slug,
    ).map(lambda t: f"{t[0]}_{t[1]}"),
    "missing_underscore": st.tuples(
        _doc_id_hex_prefix,
        st.text(alphabet=_SLUG, min_size=1, max_size=64).filter(lambda s: not s.startswith("_")),
    ).map(lambda t: f"{t[0]}{t[1]}"),
    "slug_empty": _doc_id_hex_prefix.map(lambda h: f"{h}_"),
    "slug_uppercase": st.tuples(
        _doc_id_hex_prefix,
        st.text(alphabet=_SLUG_UPPER, min_size=1, max_size=32),
    ).map(lambda t: f"{t[0]}_{t[1]}"),
    "slug_invalid_char": st.tuples(
        _doc_id_hex_prefix,
        st.text(alphabet="-. !@#$%^&*()", min_size=1, max_size=8),
    ).map(lambda t: f"{t[0]}_{t[1]}"),
    "leading_whitespace": DOC_ID_VALID.map(lambda s: f" {s}"),
    "trailing_whitespace": DOC_ID_VALID.map(lambda s: f"{s} "),
    "empty_string": st.just(""),
}


# ---------------------------------------------------------------------------
# EdgeIdStr -- uuid.UUID() constructor (not a regex).
#
# uuid.UUID() strips hyphens, the urn:uuid: prefix, and surrounding braces
# before parsing 32 hex digits. It accepts any UUID version. Valid strategies
# cover the documented input forms; negatives target near-misses against
# the post-strip 32-hex-digit requirement.
# ---------------------------------------------------------------------------


def _strip_one_hex_char(u: uuid.UUID) -> str:
    """Drop the first character of the canonical UUID string.

    Canonical form xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx has a hex digit at
    position 0; removing it leaves 31 hex digits after hyphen strip, which
    uuid.UUID() rejects.
    """
    return str(u)[1:]


def _replace_first_hex_with_non_hex(u: uuid.UUID, non_hex: str) -> str:
    """Substitute a non-hex letter into the first hex position."""
    return f"{non_hex}{str(u)[1:]}"


def _is_not_uuid(s: str) -> bool:
    try:
        uuid.UUID(s)
    except (ValueError, AttributeError):
        return True
    return False


EDGE_ID_VALID = st.one_of(
    st.uuids().map(str),
    st.uuids().map(lambda u: u.hex),
    st.uuids().map(lambda u: f"urn:uuid:{u}"),
    st.uuids().map(lambda u: f"{{{u}}}"),
)

EDGE_ID_INVALID: dict[str, st.SearchStrategy[str]] = {
    "truncated_uuid": st.uuids().map(_strip_one_hex_char),
    "extended_uuid": st.tuples(st.uuids(), st.sampled_from(_HEX)).map(lambda t: f"{t[0]}{t[1]}"),
    "non_hex_chars": st.tuples(st.uuids(), st.sampled_from(_NON_HEX_ALPHA)).map(
        lambda t: _replace_first_hex_with_non_hex(t[0], t[1])
    ),
    "wrong_separator": st.uuids().map(lambda u: str(u).replace("-", "_")),
    "random_text": st.text(
        alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E),
        min_size=1,
        max_size=50,
    ).filter(_is_not_uuid),
    "empty_string": st.just(""),
}


# ---------------------------------------------------------------------------
# Sha256Str -- regex ^sha256:[0-9a-f]{64}$
# ---------------------------------------------------------------------------

SHA256_VALID = st.text(alphabet=_HEX, min_size=64, max_size=64).map(lambda h: f"sha256:{h}")

SHA256_INVALID: dict[str, st.SearchStrategy[str]] = {
    "wrong_prefix": st.tuples(
        st.sampled_from(["md5:", "SHA256:", "sha512:", "sha256-", "sha256", ""]),
        st.text(alphabet=_HEX, min_size=64, max_size=64),
    ).map(lambda t: f"{t[0]}{t[1]}"),
    "body_too_short": st.text(alphabet=_HEX, min_size=0, max_size=63).map(lambda h: f"sha256:{h}"),
    "body_too_long": st.text(alphabet=_HEX, min_size=65, max_size=130).map(lambda h: f"sha256:{h}"),
    "body_uppercase_hex": st.text(alphabet=_HEX_UPPER, min_size=64, max_size=64)
    .filter(lambda s: any(c.isupper() for c in s))
    .map(lambda h: f"sha256:{h}"),
    "body_non_hex": st.lists(
        st.one_of(st.sampled_from(_HEX), st.sampled_from(_NON_HEX_ALPHA)),
        min_size=64,
        max_size=64,
    )
    .map("".join)
    .filter(lambda s: any(c in _NON_HEX_ALPHA for c in s))
    .map(lambda h: f"sha256:{h}"),
    "leading_whitespace": SHA256_VALID.map(lambda s: f" {s}"),
    "trailing_whitespace": SHA256_VALID.map(lambda s: f"{s} "),
    "empty_string": st.just(""),
}


# ---------------------------------------------------------------------------
# DocumentDateStr -- regex ^\d{4}-\d{2}-\d{2}$ (shape only) or None.
#
# The validator is regex-only by design (see _validate_document_date docstring
# in sage/models/schemas.py). Calendar-invalid strings like "2026-02-30" pass
# today and are intentionally NOT included as negatives.
# ---------------------------------------------------------------------------

_digit_pair = st.text(alphabet="0123456789", min_size=2, max_size=2)
_digit_year = st.text(alphabet="0123456789", min_size=4, max_size=4)

DOC_DATE_SHAPE_VALID = st.tuples(_digit_year, _digit_pair, _digit_pair).map(
    lambda t: f"{t[0]}-{t[1]}-{t[2]}"
)

DOC_DATE_VALID = st.one_of(st.none(), DOC_DATE_SHAPE_VALID)

DOC_DATE_INVALID: dict[str, st.SearchStrategy[str]] = {
    "iso_datetime": st.sampled_from(
        [
            "2026-05-10T00:00:00",
            "2026-05-10T00:00:00Z",
            "2026-05-10T00:00:00+00:00",
            "2026-05-10 00:00:00",
            "2026-05-10T12:34:56.789Z",
        ]
    ),
    "slash_separator": st.tuples(_digit_year, _digit_pair, _digit_pair).map(
        lambda t: f"{t[0]}/{t[1]}/{t[2]}"
    ),
    "single_digit_components": st.sampled_from(
        [
            "2026-5-10",
            "2026-05-1",
            "2026-5-1",
            "26-05-10",
        ]
    ),
    "year_wrong_length": st.one_of(
        st.text(alphabet="0123456789", min_size=3, max_size=3).map(lambda y: f"{y}-05-10"),
        st.text(alphabet="0123456789", min_size=5, max_size=8).map(lambda y: f"{y}-05-10"),
    ),
    "non_numeric": st.sampled_from(
        [
            "yyyy-mm-dd",
            "abcd-ef-gh",
            "2026-Ma-y0",
        ]
    ),
    "empty_string": st.just(""),
}


# ---------------------------------------------------------------------------
# Alias registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AliasSpec:
    name: str
    adapter: TypeAdapter[Any]
    valid: st.SearchStrategy[Any]
    invalid: dict[str, st.SearchStrategy[str]]


TYPED_ALIASES: tuple[AliasSpec, ...] = (
    AliasSpec("DocumentIdStr", TypeAdapter(DocumentIdStr), DOC_ID_VALID, DOC_ID_INVALID),
    AliasSpec("EdgeIdStr", TypeAdapter(EdgeIdStr), EDGE_ID_VALID, EDGE_ID_INVALID),
    AliasSpec("Sha256Str", TypeAdapter(Sha256Str), SHA256_VALID, SHA256_INVALID),
    AliasSpec(
        "DocumentDateStr",
        TypeAdapter(DocumentDateStr),
        DOC_DATE_VALID,
        DOC_DATE_INVALID,
    ),
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", TYPED_ALIASES, ids=[a.name for a in TYPED_ALIASES])
def test_valid_inputs_accepted(spec: AliasSpec) -> None:
    """Hypothesis-generated valid inputs pass the alias validator unchanged."""

    @given(spec.valid)
    @ALIAS_SETTINGS
    def inner(value: Any) -> None:
        assert spec.adapter.validate_python(value) == value

    inner()


@pytest.mark.parametrize(
    "spec, label",
    [(spec, label) for spec in TYPED_ALIASES for label in spec.invalid],
    ids=[f"{spec.name}-{label}" for spec in TYPED_ALIASES for label in spec.invalid],
)
def test_invalid_inputs_rejected(spec: AliasSpec, label: str) -> None:
    """Hypothesis-generated near-misses raise ValidationError."""
    strategy = spec.invalid[label]

    @given(strategy)
    @ALIAS_SETTINGS
    def inner(value: str) -> None:
        with pytest.raises(ValidationError):
            spec.adapter.validate_python(value)

    inner()


def test_id_helper_outputs_validate() -> None:
    """The _id(name) helper used across the test suite produces conformant IDs."""
    adapter = TypeAdapter(DocumentIdStr)
    for name in ("a", "doc_a", "fixture_42", "with_underscores"):
        synthetic = f"{hashlib.sha256(name.encode()).hexdigest()[:8]}_{name}"
        assert adapter.validate_python(synthetic) == synthetic


def test_alias_inventory_covers_schemas_module() -> None:
    """Tripwire: a new typed str alias in schemas.py must be added to TYPED_ALIASES."""
    import sage.models.schemas as schemas

    discovered: set[str] = set()
    for name, value in vars(schemas).items():
        if name.startswith("_") or not name[0].isupper():
            continue
        metadata = getattr(value, "__metadata__", None)
        if metadata is None:
            continue
        if not any(isinstance(m, AfterValidator) for m in metadata):
            continue
        discovered.add(name)

    expected = {spec.name for spec in TYPED_ALIASES}
    assert discovered == expected, (
        f"Typed-alias inventory drift. Schemas module exposes {discovered}, "
        f"test registry covers {expected}. Update TYPED_ALIASES in this file."
    )
