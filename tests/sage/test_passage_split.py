"""Splitting a section into passages the embedder can see whole.

``split_passage`` is pure: it takes the text and a ``fits`` predicate and
returns contiguous slices of the text. Two properties are asserted on every
case, because each is what a plausible wrong splitter violates while still
producing pieces that fit:

* the pieces concatenate back to the input exactly -- a split that drops or
  repeats the boundary unit, or strips a newline, fails it;
* every piece satisfies ``fits`` -- a splitter that returns its input whole
  fails it.

The sliver bound on the far-over case guards the third plausible wrong
splitter: one that emits a code point per piece satisfies both properties.
"""

from __future__ import annotations

import math

from sage.services.passage_split import split_passage

N = 100


def _fits_chars(limit: int = N):
    return lambda s: len(s) <= limit


def _fits_bytes(limit: int = N):
    return lambda s: len(s.encode("utf-8")) <= limit


def _assert_exact_and_fitting(text: str, pieces: list[str], fits) -> None:
    assert "".join(pieces) == text
    assert all(piece for piece in pieces) or text == ""
    assert all(fits(piece) for piece in pieces)


def test_a_section_exactly_at_the_ceiling_is_one_piece() -> None:
    text = "x" * N
    fits = _fits_chars()

    pieces = split_passage(text, fits)

    assert pieces == [text]


def test_one_character_over_splits_at_the_paragraph_boundary() -> None:
    first = "a" * 60
    second = "b" * 39
    text = f"{first}\n\n{second}"  # 101 characters
    assert len(text) == N + 1
    fits = _fits_chars()

    pieces = split_passage(text, fits)

    assert pieces == [f"{first}\n\n", second]
    _assert_exact_and_fitting(text, pieces, fits)


def test_far_over_the_ceiling_fits_joins_exactly_and_makes_no_slivers() -> None:
    paragraphs = [("p%d " % i) * (3 + i % 17) for i in range(400)]
    text = "\n\n".join(paragraphs)
    assert len(text) > 50 * N
    fits = _fits_chars()

    pieces = split_passage(text, fits)

    _assert_exact_and_fitting(text, pieces, fits)
    assert len(pieces) <= 2 * math.ceil(len(text) / N)


def test_an_oversize_paragraph_splits_at_line_boundaries_not_mid_line() -> None:
    lines = [f"line {i:03d} " + "z" * 20 for i in range(12)]
    text = "\n".join(lines)  # one paragraph, no blank line
    fits = _fits_chars()

    pieces = split_passage(text, fits)

    _assert_exact_and_fitting(text, pieces, fits)
    assert len(pieces) > 1
    # Every piece but the last ends exactly at a line boundary.
    assert all(piece.endswith("\n") for piece in pieces[:-1])


def test_a_single_long_line_is_hard_split() -> None:
    text = "```\n" + "0123456789" * 50 + "\n```"
    body_line = "0123456789" * 50
    fits = _fits_chars()

    pieces = split_passage(text, fits)

    _assert_exact_and_fitting(text, pieces, fits)
    assert any(body_line not in piece and "0123456789" in piece for piece in pieces)


def test_multibyte_text_splits_on_code_points() -> None:
    text = "漢字かな🙂" * 60  # no newlines, 4-byte emoji throughout
    fits = _fits_bytes()

    pieces = split_passage(text, fits)

    _assert_exact_and_fitting(text, pieces, fits)
    for piece in pieces:
        assert piece.encode("utf-8").decode("utf-8") == piece


def test_a_predicate_rejecting_everything_still_terminates() -> None:
    text = "abc\n\ndef"

    pieces = split_passage(text, lambda s: False)

    assert "".join(pieces) == text
    assert all(len(piece) == 1 for piece in pieces)


def test_a_run_of_three_newlines_is_preserved() -> None:
    text = "a" * 70 + "\n\n\n" + "b" * 70
    fits = _fits_chars()

    pieces = split_passage(text, fits)

    _assert_exact_and_fitting(text, pieces, fits)
    assert pieces[0] == "a" * 70 + "\n\n\n"


def test_an_empty_section_is_one_empty_piece() -> None:
    assert split_passage("", _fits_chars()) == [""]


def test_stub_embedders_declare_a_byte_counted_bound() -> None:
    from sage.adapters.stubs import SeededEmbeddingProvider, StubEmbeddingProvider

    for provider_type in (StubEmbeddingProvider, SeededEmbeddingProvider):
        assert provider_type().max_input_tokens == 2048
        bounded = provider_type(max_input_tokens=64)
        assert bounded.max_input_tokens == 64
        assert bounded.count_tokens("漢a") == 4
