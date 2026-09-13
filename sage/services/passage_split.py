"""Dividing a section into passages the embedder sees whole.

A section is what a heading addresses. A passage is what the index stores and
embeds. They coincide until a section is longer than the embedding provider's
input bound, which truncates everything past it; such a section is stored as
several consecutive passages sharing its heading path and its section index.

The division is an indexing concern, so every read that returns text or
structure treats a section's passages as the section: the passages are exact
contiguous slices of the section's text, and joining them back is plain
concatenation. Sections are joined to one another as they always were.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence

from sage.adapters.interfaces import Chunk

# The separator between sections in reconstructed text. A section's own
# passages are concatenated, because a division need not fall where this
# separator stood.
SECTION_SEPARATOR = "\n\n"

# Division points in order of preference. Each boundary sits after the run of
# newlines that forms it, so the run stays with the text it ends and every unit
# concatenates back to the text it was cut from.
_BOUNDARIES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<=\n\n)(?!\n)"),
    re.compile(r"(?<=\n)(?!\n)"),
)


def embedding_input(heading_path: str, content: str) -> str:
    """The text a passage is embedded as: its heading path, then its content.

    The path travels with the embedder input only, so a passage's vector keeps
    its section's context even where the passage does not open with the heading
    line.
    """
    return f"{heading_path}{SECTION_SEPARATOR}{content}" if heading_path else content


def split_passage(text: str, fits: Callable[[str], bool]) -> list[str]:
    """Divide ``text`` into consecutive pieces that each satisfy ``fits``.

    Returns ``[text]`` when the whole fits. Otherwise the text is cut into
    units -- paragraphs, then lines within a paragraph too long to fit, then
    code points within a line too long to fit -- and the units are packed
    greedily, each piece taking the longest run of units that fits. A unit is
    divided only when it does not fit by itself, so a paragraph that fits is
    never split across pieces.

    The pieces concatenate to ``text`` exactly. A code point is the smallest
    unit, so a piece never ends inside a UTF-8 sequence; and a code point that
    does not fit by itself is emitted alone, so the division terminates for
    any predicate.

    ``fits`` is assumed monotone -- a text that fits has every prefix fit -- in
    that the longest fitting run is found by search. Each piece returned was
    itself tested, so a predicate that is not strictly monotone can shorten
    pieces but never admit one that does not fit.
    """
    if fits(text):
        return [text]
    return _pack(_units(text, fits, 0), fits)


def _units(text: str, fits: Callable[[str], bool], level: int) -> list[str]:
    """Cut a ``text`` that does not fit into units that each do, coarsest boundary first."""
    if level == len(_BOUNDARIES):
        return _code_point_units(text, fits)
    parts = [part for part in _BOUNDARIES[level].split(text) if part]
    if len(parts) == 1:
        return _units(text, fits, level + 1)
    units: list[str] = []
    for part in parts:
        units.extend([part] if fits(part) else _units(part, fits, level + 1))
    return units


def _code_point_units(text: str, fits: Callable[[str], bool]) -> list[str]:
    """Cut a boundary-free text into the longest fitting runs of code points."""
    units: list[str] = []
    start = 0
    while start < len(text):
        length = _longest_fitting(len(text) - start, lambda n, s=start: fits(text[s : s + n]))
        units.append(text[start : start + length])
        start += length
    return units


def _pack(units: Sequence[str], fits: Callable[[str], bool]) -> list[str]:
    """Join consecutive units into the longest fitting pieces."""
    pieces: list[str] = []
    start = 0
    while start < len(units):
        count = _longest_fitting(
            len(units) - start, lambda n, s=start: fits("".join(units[s : s + n]))
        )
        pieces.append("".join(units[start : start + count]))
        start += count
    return pieces


def _longest_fitting(available: int, fits_first: Callable[[int], bool]) -> int:
    """The largest ``n`` in ``1..available`` for which ``fits_first(n)`` holds.

    Searched by doubling and then bisecting, so the cost is logarithmic in the
    answer and no candidate is more than twice the length of the one returned.
    Returns 1 when nothing fits, which is the least progress a division can make.
    """
    if available <= 1 or not fits_first(1):
        return 1
    low = 1
    high = 2
    while high <= available and fits_first(high):
        low = high
        high *= 2
    high = min(high, available + 1)
    while high - low > 1:
        middle = (low + high) // 2
        if fits_first(middle):
            low = middle
        else:
            high = middle
    return low


def group_sections(chunks: Iterable[Chunk]) -> list[list[Chunk]]:
    """Group passages in document order into their sections.

    A section's passages are consecutive, so a change of section key ends a
    group. The heading path is not consulted: two sections can render the same
    one.
    """
    groups: list[list[Chunk]] = []
    for chunk in chunks:
        if groups and groups[-1][-1].section_key == chunk.section_key:
            groups[-1].append(chunk)
        else:
            groups.append([chunk])
    return groups


def section_text(passages: Iterable[Chunk]) -> str:
    """One section's text, from its passages."""
    return "".join(passage.content for passage in passages)


def join_passages(chunks: Iterable[Chunk], separator: str = SECTION_SEPARATOR) -> str:
    """Text reconstructed from passages in document order.

    Each section's passages are concatenated and sections are joined with
    ``separator``, so the result is the same whether or not any section was
    divided.
    """
    return separator.join(section_text(group) for group in group_sections(chunks))
