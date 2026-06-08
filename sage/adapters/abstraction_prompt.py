"""Shared system prompt for semantic abstract generation.

The abstraction prompt instructs the model to produce a relevance-triage
card: a third-person description an autonomous agent reads to decide whether
to fetch the full document. It is provider-agnostic — every abstraction
provider (local and hosted) renders the same instruction so abstracts are
consistent regardless of which model produced them.
"""

SYSTEM_PROMPT_TEMPLATE = (
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


def _format_system_prompt(doc_type: str | None) -> str:
    """Render the system prompt with optional doc_type substitution."""
    if doc_type:
        clause = f', type ("{doc_type}")'
    else:
        clause = ""
    return SYSTEM_PROMPT_TEMPLATE.format(doc_type_clause=clause)
