"""No clause is published twice about the same parameter.

A parameter's published text is often two texts joined: the request-model field
description the REST surface shares, and a supplement only an MCP caller
needs. When the supplement restates the field, or restates the tool
description, the model is shown the rule twice and a later edit has two copies
to keep in step. Two scopes are held:

- within one parameter's published description (each dotted path, so an item
  field is its own parameter);
- between a parameter's description and its tool's description.

A repetition is a run of ``RUN_WORDS`` consecutive words occurring twice, after
case, markup and punctuation are normalized away. Comparing whole sentences
would miss the common case, a rule restated inside a longer sentence of
different wording; any repeated sentence of that length contains such a run.
Six words is the shortest run at which every finding on the surface was a
restatement rather than a stock phrase.

Text repeated across *different* parameters is not refused: a tripwire's
marking is the same on every tripwire by design, and a shared rule stated on
each parameter it governs is placed where it governs.
"""

from __future__ import annotations

import re
from typing import Final

import pytest

from tests.helpers.published_tool import parameter_descriptions, published_tools
from tests.sage.mcp_surface_pin import EXPECTED_SURFACE

#: The length of a repeated word run that counts as a repetition.
RUN_WORDS: Final[int] = 6

_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9_]+")


def word_runs(text: str | None) -> list[str]:
    """Every run of ``RUN_WORDS`` consecutive normalized words in ``text``, in order."""
    words = _WORD_RE.findall((text or "").lower())
    return [" ".join(words[i : i + RUN_WORDS]) for i in range(len(words) - RUN_WORDS + 1)]


def repetitions(description: str | None, params: dict[str, str]) -> list[str]:
    """Every repetition in one tool's published text, as readable findings.

    Each parameter is reported once per scope, naming its first repeated run,
    since overlapping runs of one restated clause are one finding.
    """
    in_description = set(word_runs(description))
    found: list[str] = []
    for path, text in params.items():
        seen: set[str] = set()
        within = next((run for run in word_runs(text) if run in seen or seen.add(run)), None)
        if within:
            found.append(f"{path} repeats itself: {within!r}")
        shared = next((run for run in word_runs(text) if run in in_description), None)
        if shared:
            found.append(f"{path} repeats the tool description: {shared!r}")
    return found


# ---------------------------------------------------------------------------
# The detector
# ---------------------------------------------------------------------------

_RULE = "No projection, indexing or abstraction runs"
_FIELD = f"When true, nothing is persisted. {_RULE} and no record is written."


def test_word_runs_normalize_case_markup_and_punctuation() -> None:
    assert word_runs("`No` Projection,  *indexing* or abstraction RUNS.") == [
        "no projection indexing or abstraction runs"
    ]
    assert word_runs("five words are too few") == []


def test_detector_catches_a_clause_restated_within_a_parameter() -> None:
    params = {"dry_run": f"{_FIELD} The source is hashed where it stands, so {_RULE.lower()}."}
    assert repetitions("An unrelated tool description.", params) == [
        "dry_run repeats itself: 'no projection indexing or abstraction runs'"
    ]


def test_detector_catches_a_parameter_restating_the_description() -> None:
    params = {"items.action": f"Lifecycle action. {_RULE}."}
    assert repetitions(f"Tool prose; {_RULE}, ever.", params) == [
        "items.action repeats the tool description: 'no projection indexing or abstraction runs'"
    ]


def test_detector_allows_one_rule_on_two_parameters() -> None:
    assert repetitions("Tool prose.", {"a": _FIELD, "b": _FIELD}) == []


def test_a_five_word_echo_is_not_a_repetition() -> None:
    assert repetitions("Supply exactly one of them.", {"a": "Supply exactly one of these."}) == []


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(EXPECTED_SURFACE))
def test_no_clause_is_published_twice(name: str) -> None:
    tool = published_tools()[name]
    found = repetitions(tool.description, parameter_descriptions(tool.parameters))
    assert not found, f"{name}:\n  " + "\n  ".join(found)
