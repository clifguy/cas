#!/usr/bin/env python3
"""Named prompt constructions for attribution measurement.

CAS-ADR-020 clause (l) rests on an apportionment: the opening-clause
breach had two live explanations -- the construction changed, or the
directive was always weak -- and only measuring the same documents under
more than one construction separates them. This module carries the
constructions so such a measurement is reproducible rather than
reconstructed by hand, and so a scorecard on disk records which arm
produced it.

The historical templates are frozen copies, kept here in the measurement
harness rather than in ``sage.adapters.abstraction_prompt``. Only one
construction is production -- ``current``, which is rendered by the
production module itself rather than restated, so this registry cannot
drift away from what the abstraction path actually sends.

The constructions differ along two axes, because the revision that
introduced the attractor moved both: the framing sentence's artifact noun
(a noun the description could echo), and whether the source is supplied
as delimited data.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import MethodType
from typing import Final

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sage.adapters.abstraction_prompt import (  # noqa: E402
    _format_system_prompt,
    wrap_source_document,
)

#: The construction in force before the framing revision: the framing
#: sentence names the artifact to produce, and the source is concatenated
#: into the user turn rather than delimited. Frozen verbatim; it is a
#: record of what was sent, so it must not be tidied.
_PRE_REVISION_TEMPLATE: Final[str] = (
    "You are producing a relevance-triage card for an autonomous agent that "
    "has discovered this document via search. The agent will read this card "
    "to decide whether to fetch the full document. Write a description of "
    'the document in third person ("This document...", "The guideline...", '
    '"The text..."). Cover what the document is about, what it claims or '
    "prescribes, what topics it covers, and where relevant what it does not "
    "cover.\n"
    "\n"
    "Do not write in the voice, style, or genre the document discusses -- "
    "describe the document, do not produce a specimen of it. Do not introduce "
    "specifics (numbers, names, dates, quotes, examples) that are not present "
    "in the source text. Do not expand acronyms or initialisms unless the "
    "expansion appears verbatim in the source text. If the meaning of an "
    "acronym is not provided in the source, leave it as-is. The document's "
    "title{doc_type_clause}, tags, and project are already visible to the "
    "agent; do not restate them. Do not open your description with a phrase "
    "that restates the document's type (for example, 'This document is an "
    "architecture decision record...', 'This document is a ticket...', "
    "'This document is a steering document...'). The document's type is "
    "already visible to the agent as metadata. Open with what the document "
    "does or describes, not with what category it belongs to. Length should "
    "be proportional to the document's complexity: longer for dense or "
    "multi-topic documents, shorter for simple or narrowly-scoped ones. "
    "Output only the description, with no preamble, labels, or commentary."
)

#: The framing sentence the pre-revision construction opened with, and the
#: current one that replaced it. The middle arm restores only this
#: sentence, holding the delimitation and the prose clause at their
#: current values, so the artifact noun's contribution can be read alone.
_PRE_REVISION_FRAMING: Final[str] = (
    "You are producing a relevance-triage card for an autonomous agent that "
    "has discovered this document via search. The agent will read this card "
    "to decide whether to fetch the full document. Write a description of "
    'the document in third person ("This document...", "The guideline...", '
    '"The text...").'
)
_CURRENT_FRAMING: Final[str] = (
    "An autonomous agent has discovered this document via search and will "
    "read what you write to decide whether to fetch the full document. Write "
    'a description of the document in third person ("This document...", '
    '"The guideline...", "The text...").'
)


def _render_current(doc_type: str | None) -> str:
    """Render exactly what the abstraction path sends today.

    Delegates rather than restating the template, so a change to the
    production prompt reaches this arm without an edit here. An arm that
    had drifted from production would silently measure a construction
    nothing runs.
    """
    return _format_system_prompt(doc_type)


def _render_framing_noun_restored(doc_type: str | None) -> str:
    """The current construction with the framing sentence's noun restored."""
    return _render_current(doc_type).replace(_CURRENT_FRAMING, _PRE_REVISION_FRAMING, 1)


def _render_pre_revision(doc_type: str | None) -> str:
    """The full construction in force before the framing revision."""
    clause = f', type ("{doc_type}")' if doc_type else ""
    return _PRE_REVISION_TEMPLATE.format(doc_type_clause=clause)


@dataclass(frozen=True)
class PromptConstruction:
    """One arm of an attribution measurement."""

    name: str
    render_system_prompt: Callable[[str | None], str]
    #: Whether the source is supplied inside explicit boundary markers.
    #: The pre-revision construction concatenated it undelimited, and the
    #: two changes landed together, so an arm that delimited the source
    #: while restoring the old wording would be neither construction.
    delimit_source: bool

    def render_source(self, text: str) -> str:
        return wrap_source_document(text) if self.delimit_source else text


PROMPT_CONSTRUCTIONS: Final[dict[str, PromptConstruction]] = {
    "current": PromptConstruction("current", _render_current, delimit_source=True),
    "framing-noun-restored": PromptConstruction(
        "framing-noun-restored", _render_framing_noun_restored, delimit_source=True
    ),
    "pre-revision": PromptConstruction("pre-revision", _render_pre_revision, delimit_source=False),
}

DEFAULT_CONSTRUCTION: Final[str] = "current"


def bind_construction(provider, construction: PromptConstruction) -> None:
    """Rebind a loaded provider's prompt assembly to ``construction``.

    Rebinds on the instance rather than subclassing because the local
    provider is a process-wide singleton holding loaded weights: two arms
    measured in one process must reuse that instance, and constructing a
    second would either reload several gigabytes or be refused outright.

    ``_template_overhead_tokens`` caches its result per doc_type, and the
    overhead is a property of the construction -- the pre-revision arm's
    system prompt is shorter and its source carries no markers. The cache
    is cleared here, without which the second arm would size its input
    budget against the first arm's prompt and truncate different
    documents than it reported.
    """

    def _build_prompt(self, text: str, doc_type: str | None) -> str:
        messages = [
            {"role": "system", "content": construction.render_system_prompt(doc_type)},
            {"role": "user", "content": construction.render_source(text)},
        ]
        return self._tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )

    provider._build_prompt = MethodType(_build_prompt, provider)
    overhead_cache = getattr(provider, "_overhead_tokens", None)
    if overhead_cache is not None:
        overhead_cache.clear()
