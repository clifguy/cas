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

A line ends at CR LF, a lone CR or a lone LF, and a blank line in any of those
endings is a paragraph boundary. Each ending is exercised beside LF, the
control, and a CR LF pair is never divided between pieces.
"""

from __future__ import annotations

import math
import random
import re

import pytest

from sage.services import passage_split
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


def _assert_no_pair_divided(pieces: list[str]) -> None:
    """No cut falls between the CR and the LF of one line end."""
    for before, after in zip(pieces, pieces[1:]):
        assert not (before.endswith("\r") and after.startswith("\n")), (before, after)


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


def test_fitting_multi_line_paragraphs_are_never_split_across_pieces() -> None:
    """A paragraph that fits stays whole even when its lines would pack tighter.

    A splitter cutting at lines alone passes every other test here, because
    their paragraphs carry no inner newline or stand alone.
    """
    # Two paragraphs overflow the bound, but one and a half do not, so a
    # line-packing splitter would carry lines of the next paragraph along.
    paragraph = "\n".join(f"line {i} " + "q" * 7 for i in range(4))
    assert N / 2 < len(paragraph) + 2 < N
    text = "\n\n".join([paragraph] * 5)
    fits = _fits_chars()

    pieces = split_passage(text, fits)

    _assert_exact_and_fitting(text, pieces, fits)
    assert len(pieces) > 1
    assert all(piece.endswith("\n\n") for piece in pieces[:-1])
    assert all(piece.rstrip("\n") == paragraph for piece in pieces)


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


ENDINGS = pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])


@ENDINGS
def test_paragraphs_divide_at_blank_lines_in_every_line_ending(ending: str) -> None:
    """A text of two-line paragraphs bounded to one paragraph divides only at blank lines.

    Two paragraphs overflow the bound and their lines would pack tighter, so
    a splitter that does not see the blank line cuts inside a paragraph.
    """
    paragraphs = [
        ending.join([f"record {i} opens " + "o" * 20, f"record {i} closes"]) for i in range(6)
    ]
    text = (ending * 2).join(paragraphs)
    # Room for a paragraph, its blank line and the next paragraph's first line.
    first_line = f"record 0 opens {'o' * 20}{ending}"
    fits = _fits_chars(len(paragraphs[0]) + 2 * len(ending) + len(first_line))

    pieces = split_passage(text, fits)

    _assert_exact_and_fitting(text, pieces, fits)
    _assert_no_pair_divided(pieces)
    assert len(pieces) == len(paragraphs)
    assert all(piece.endswith(ending * 2) for piece in pieces[:-1])
    assert [piece.removesuffix(ending * 2) for piece in pieces] == paragraphs


@ENDINGS
@pytest.mark.parametrize("room", ["run", "two-ends"])
def test_a_run_of_blank_lines_stays_with_the_text_it_ends(ending: str, room: str) -> None:
    """Two blank lines in a row are one boundary, after the whole run of line ends.

    With room for the run, each paragraph is a piece. With room for only two
    of its three line ends, a paragraph divides at its line end instead, and a
    splitter that cuts inside the run strands the rest at the head of the
    next piece, which packing cannot rejoin.
    """
    paragraphs = [
        ending.join([f"record {i} opens " + "o" * 20, f"record {i} closes"]) for i in range(6)
    ]
    text = (ending * 3).join(paragraphs)
    first_line = f"record 0 opens {'o' * 20}{ending}"
    bound = len(paragraphs[0]) + 2 * len(ending)
    fits = _fits_chars(bound + len(ending) + len(first_line) if room == "run" else bound)

    pieces = split_passage(text, fits)

    _assert_exact_and_fitting(text, pieces, fits)
    assert not any(piece.startswith(("\r", "\n")) for piece in pieces)
    assert all(piece.endswith(ending * 3) for piece in pieces if ending * 3 in piece)
    if room == "run":
        assert [piece.removesuffix(ending * 3) for piece in pieces] == paragraphs


def test_the_crlf_example_divides_at_its_blank_line() -> None:
    text = "a1\r\na2\r\n\r\nb1\r\nb2"

    pieces = split_passage(text, _fits_chars(len("a1\r\na2\r\n\r\nb1\r\n")))

    assert pieces == ["a1\r\na2\r\n\r\n", "b1\r\nb2"]


@pytest.mark.parametrize(
    "blank",
    ["\r\r\n", "\r\n\n", "\n\r\n", "\n\r"],
    ids=["cr-then-crlf", "crlf-then-lf", "lf-then-crlf", "lf-then-cr"],
)
def test_a_blank_line_of_mixed_endings_is_a_paragraph_boundary(blank: str) -> None:
    """Two consecutive line ends of any kinds make a blank line."""
    first = "a" * 30 + "\r\n" + "b" * 30
    second = "c" * 30 + "\r\n" + "d" * 30
    text = first + blank + second

    # Room for the second paragraph's first line beside the first paragraph.
    pieces = split_passage(text, _fits_chars(len(first + blank) + 32))

    assert pieces == [first + blank, second]
    _assert_no_pair_divided(pieces)


@ENDINGS
def test_lines_keep_their_whole_line_ends_in_every_line_ending(ending: str) -> None:
    """A paragraph too long to fit divides after each line's complete line end."""
    lines = [f"line {i:03d} " + "z" * 20 for i in range(12)]
    text = ending.join(lines)
    fits = _fits_chars()

    pieces = split_passage(text, fits)

    _assert_exact_and_fitting(text, pieces, fits)
    _assert_no_pair_divided(pieces)
    assert len(pieces) > 1
    assert all(piece.endswith(ending) for piece in pieces[:-1])
    assert not any(piece.startswith(("\r", "\n")) for piece in pieces)


def test_a_line_divided_by_code_points_keeps_its_crlf_whole() -> None:
    """The bound falls between the CR and the LF, so the cut moves before the pair."""
    text = "x" * 10 + "\r\n" + "y" * 10
    fits = _fits_chars(11)

    pieces = split_passage(text, fits)

    assert "".join(pieces) == text
    _assert_no_pair_divided(pieces)
    assert pieces == ["x" * 10, "\r\n", "y" * 10]


def test_a_crlf_that_does_not_fit_is_emitted_whole() -> None:
    """A pair is the smallest unit, as a code point is, even under a bound of one."""
    text = "ab\r\ncd"

    pieces = split_passage(text, _fits_chars(1))

    assert pieces == ["a", "b", "\r\n", "c", "d"]


def test_lf_text_divides_exactly_as_under_the_line_feed_only_boundaries(monkeypatch) -> None:
    """LF-only text is divided as it was before CR line ends were recognised."""
    generator = random.Random(0)
    corpus = [
        "".join(generator.choice("ab \n") for _ in range(generator.randint(0, 300)))
        for _ in range(300)
    ]
    bounds = (1, 3, 7, 20, 60)
    current = [split_passage(text, _fits_chars(bound)) for text in corpus for bound in bounds]

    monkeypatch.setattr(
        passage_split,
        "_BOUNDARIES",
        (re.compile(r"(?<=\n\n)(?!\n)"), re.compile(r"(?<=\n)(?!\n)")),
    )
    reference = [split_passage(text, _fits_chars(bound)) for text in corpus for bound in bounds]

    assert current == reference
