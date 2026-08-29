"""Tests for the truncation audit's classifier and manifest writer.

The audit answers one question per document: would the abstraction provider
have to discard part of this document's text to fit it alongside the
generated abstract and the chat template? The classifier under test computes
that from the same budget function the provider spends, so a survey and a
production run cannot disagree about where the threshold sits.

Both helpers are pure. The tokenizer and the template-overhead lookup are
injected as callables, so nothing here loads model weights -- which is also
the property that lets the audit run against a live vault without competing
with the server for accelerator memory.
"""

from __future__ import annotations

import pytest

from sage.config import VaultAbstractionConfig
from scripts.audit_abstraction_truncation import (
    TruncationRecord,
    classify_truncation,
    render_manifest,
)


def _one_token_per_char(text: str) -> int:
    """Tokenizer stand-in: one token per character, so a token budget and a
    character count are the same number and every expectation below can be
    written exactly."""
    return len(text)


def _fixed_overhead(_doc_type: str | None) -> int:
    return 300


def _config(**overrides) -> VaultAbstractionConfig:
    """Abstraction config with the density curve pinned flat.

    ``tokens_per_word=0`` makes ``compute_max_tokens`` return
    ``base_abstract_tokens`` clamped by ``max_abstract_tokens``, so a test
    asserting on the threshold does not also have to model the density
    formula. Tests that care about the clamp override the ceiling.
    """
    defaults = {
        "max_abstract_tokens": 1500,
        "base_abstract_tokens": 1000,
        "tokens_per_word": 0.0,
    }
    return VaultAbstractionConfig(**{**defaults, **overrides})


def _classify(body: str, *, doc_type="note", config=None, overhead_for=_fixed_overhead):
    return classify_truncation(
        doc_id="d1",
        doc_type=doc_type,
        lifecycle_status="active",
        body_text=body,
        effective_window=10_000,
        abstraction_config=config or _config(),
        count_tokens=_one_token_per_char,
        overhead_for=overhead_for,
    )


# available = 10000 - 1000 (max_tokens) - 300 (overhead) = 8700
_AVAILABLE = 8700


# ---------------------------------------------------------------------------
# Classification against the budget.
# ---------------------------------------------------------------------------


def test_a_document_over_the_budget_is_truncated_by_the_overflow():
    record = _classify("x" * (_AVAILABLE + 1))

    assert record.truncated
    assert record.available_tokens == _AVAILABLE
    assert record.lost_tokens == 1


def test_a_document_exactly_at_the_budget_is_not_truncated():
    """Boundary: the provider's fit check admits a text equal to the budget.

    An off-by-one here does not fail loudly -- it silently moves every
    document sitting exactly on the threshold into or out of the reported
    set, and the survey's headline count is wrong by however many that is.
    """
    record = _classify("x" * _AVAILABLE)

    assert not record.truncated
    assert record.lost_tokens == 0


def test_the_abstract_ceiling_moves_the_threshold():
    """The output budget is read from the vault's config, not assumed.

    Each vault sets its own ``max_abstract_tokens``, and that number is
    subtracted from the window before the document gets what is left. A
    survey that assumed one vault's ceiling would misclassify every document
    in a vault configured differently.
    """
    body = "x" * 9_000

    generous = _classify(body, config=_config(base_abstract_tokens=500, max_abstract_tokens=500))
    stingy = _classify(body, config=_config(base_abstract_tokens=1500, max_abstract_tokens=1500))

    assert not generous.truncated  # available = 10000 - 500 - 300 = 9200
    assert stingy.truncated  # available = 10000 - 1500 - 300 = 8200


def test_each_document_type_is_measured_against_its_own_overhead():
    """The chat template costs a different number of tokens per doc_type.

    Sharing one overhead across types would shift the threshold for every
    type but the one it was measured on.
    """
    overheads = {"note": 300, "transcript": 1300}
    body = "x" * 8_000  # inside the cheap type's budget, past the costly one's

    cheap = _classify(body, doc_type="note", overhead_for=overheads.__getitem__)
    costly = _classify(body, doc_type="transcript", overhead_for=overheads.__getitem__)

    assert not cheap.truncated  # available = 8700
    assert costly.truncated  # available = 7700


def test_lost_fraction_reports_the_share_of_the_document_discarded():
    record = _classify("x" * 10_000)

    assert record.lost_tokens == 10_000 - _AVAILABLE
    assert record.lost_fraction == pytest.approx((10_000 - _AVAILABLE) / 10_000)


def test_lost_fraction_of_an_empty_document_is_zero_not_an_error():
    """Degenerate input: a document with no body text divides by zero if the
    fraction is computed naively."""
    record = _classify("")

    assert not record.truncated
    assert record.lost_fraction == 0.0


# ---------------------------------------------------------------------------
# Manifest rendering.
# ---------------------------------------------------------------------------


def _record(doc_id: str, *, body_tokens: int, available_tokens: int = 100) -> TruncationRecord:
    return TruncationRecord(
        doc_id=doc_id,
        doc_type="note",
        lifecycle_status="active",
        body_tokens=body_tokens,
        available_tokens=available_tokens,
        max_tokens=1000,
        overhead_tokens=300,
    )


def _manifest(records) -> str:
    return render_manifest(
        records,
        vault_id="example_vault",
        effective_window=10_000,
        model_id="example/model",
        measured_at="2026-01-01T00:00:00Z",
    )


def test_manifest_lists_only_truncated_documents():
    rendered = _manifest([_record("kept", body_tokens=500), _record("fits", body_tokens=50)])

    assert "kept" in rendered
    assert "fits" not in rendered


def test_manifest_order_is_deterministic_across_calls():
    """Repeatability is the point of writing the set down.

    Two documents losing the same amount are ordered by id, so a rendering
    cannot depend on enumeration order in the vault or on set iteration.
    """
    records = [
        _record("zzz", body_tokens=600),
        _record("aaa", body_tokens=600),
        _record("mmm", body_tokens=900),
    ]

    first = _manifest(records)
    second = _manifest(list(reversed(records)))

    assert first == second
    ids = [line for line in first.splitlines() if line and not line.startswith("#")]
    assert ids == ["mmm", "aaa", "zzz"]


def test_manifest_header_records_what_produced_it():
    """A bare id list is not auditable: read six months later, nothing says
    which vault it describes or what threshold produced it."""
    rendered = _manifest([_record("kept", body_tokens=500)])
    header = "\n".join(line for line in rendered.splitlines() if line.startswith("#"))

    assert "example_vault" in header
    assert "10000" in header
    assert "example/model" in header
    assert "2026-01-01T00:00:00Z" in header


def test_manifest_round_trips_into_the_reabstract_loader(tmp_path):
    """The two halves of the operation meet here: what the audit writes is
    exactly what the reabstract pass reads back."""
    from scripts.reabstract_deferred import _load_ids_file

    path = tmp_path / "ids.txt"
    path.write_text(
        _manifest([_record("doc_b", body_tokens=900), _record("doc_a", body_tokens=600)])
    )

    assert _load_ids_file(path) == ["doc_b", "doc_a"]
