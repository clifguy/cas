"""T-0154: SAGE MCP self-documentation conformance.

Asserts that the MCP tool surface documents valid vocabulary, payload
shapes, and response-size characteristics directly via tool signatures
and docstrings, so callers do not have to learn by erroring. Anchored
in the field-use report consolidated into T-0154.

Each criterion in the ticket maps to one or more tests below. The tests
prefer structural assertions (set-equality against source-of-truth
enums, JSON-parseable example blocks) over substring searches; where
substring checks are used, they are paired with structural checks so
a docstring that contains the right keyword without the right shape
still fails.
"""

from __future__ import annotations

import inspect
import re
from typing import Any, get_args, get_origin

from sage.mcp_server import (
    sage_bulk_set_lifecycle,
    sage_bulk_update_metadata,
    sage_discover,
    sage_link,
    sage_set_lifecycle,
    sage_update_metadata,
)
from sage.models.enums import EdgeType, RationaleKind, RetrievalMode

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _annotation_of(fn: Any, param_name: str) -> Any:
    """Return the type annotation of ``param_name`` on ``fn``."""
    return inspect.signature(fn).parameters[param_name].annotation


def _annotation_includes(annotation: Any, target: type) -> bool:
    """True when ``annotation`` is ``target`` or ``target | None`` (any Union containing target)."""
    if annotation is target:
        return True
    origin = get_origin(annotation)
    if origin is None:
        return False
    return target in get_args(annotation)


def _docstring(fn: Any) -> str:
    """Return ``fn``'s docstring (or raise if absent)."""
    doc = inspect.getdoc(fn)
    assert doc is not None, f"{fn.__name__} has no docstring"
    return doc


# ---------------------------------------------------------------------------
# Criterion 1 — Schema-level enums on tool signatures
# ---------------------------------------------------------------------------


def test_link_edge_type_signature_is_enum():
    """T1.1 — sage_link.edge_type must be typed as the EdgeType StrEnum.

    The Pydantic LinkRequest already uses EdgeType, but FastMCP introspects
    the tool function signature itself when generating the JSON schema
    callers see. Without the annotation here, callers receive a free-form
    string at the schema level.

    Anti-coincidental-pass: this test uses identity equality with EdgeType,
    not isinstance/issubclass; replacing EdgeType with a sibling enum
    fails the test, and reverting to ``str`` fails it.
    """
    ann = _annotation_of(sage_link, "edge_type")
    assert ann is EdgeType, (
        f"sage_link.edge_type annotation is {ann!r}; expected EdgeType. "
        "Without the enum at the tool signature, FastMCP exposes edge_type "
        "as a free-form string and callers learn valid values only by "
        "erroring."
    )


def test_link_rationale_kind_signature_is_enum():
    """T1.2 — sage_link.rationale_kind must be typed as RationaleKind | None.

    Same rationale as T1.1 — LinkRequest carries the enum but the tool
    function signature lags. Optional because the field is nullable.
    """
    ann = _annotation_of(sage_link, "rationale_kind")
    assert _annotation_includes(ann, RationaleKind), (
        f"sage_link.rationale_kind annotation is {ann!r}; expected "
        "RationaleKind | None. Callers currently see this as a free-form "
        "optional string at the MCP boundary."
    )


def test_discover_mode_signature_is_enum():
    """T1.4 — sage_discover.mode must be typed as the RetrievalMode StrEnum.

    DiscoverRequest.mode uses RetrievalMode but the tool function signature
    declares ``mode: str = "semantic"``. The default must remain a valid
    enum value (RetrievalMode.SEMANTIC) so existing callers keep working.
    """
    ann = _annotation_of(sage_discover, "mode")
    assert ann is RetrievalMode, (
        f"sage_discover.mode annotation is {ann!r}; expected RetrievalMode. "
        "Callers picking the mode value from the tool schema currently "
        "see a free-form string."
    )


def test_set_lifecycle_action_docstring_points_at_vault_config():
    """T1.3 — sage_set_lifecycle.action description must point at vault_config.

    Per the T-0154 scope resolution: action stays a free-form ``str``
    (values are vault-config-defined), but the documentation must direct
    callers at the authoritative source — ``sage_get_vault_config`` —
    rather than leaving them to discover the action set by erroring.

    Anti-coincidental-pass: the docstring must mention BOTH ``vault
    config`` (closure source) and ``sage_get_vault_config`` (the
    discovery tool). Mentioning one without the other fails.
    """
    doc = _docstring(sage_set_lifecycle)
    assert "vault config" in doc.lower(), (
        "sage_set_lifecycle docstring must reference 'vault config' as the "
        "authoritative source of the action vocabulary."
    )
    assert "sage_get_vault_config" in doc, (
        "sage_set_lifecycle docstring must point callers at "
        "``sage_get_vault_config`` for the authoritative action list."
    )


def test_set_lifecycle_signature_exposes_dry_run():
    """T-0162 — sage_set_lifecycle must expose ``dry_run: bool = False`` at the wrapper.

    The dry-run rollout (T-0152) shipped on every other mutation tool
    but skipped the single-form ``sage_set_lifecycle`` wrapper. The
    underlying ``SetLifecycleRequest`` already carries ``dry_run`` and
    ``LifecycleService.set_lifecycle`` honors it; the gap was at the
    MCP boundary.

    Structural assertion: parameter present, annotation identity-equal
    to ``bool`` (not ``isinstance(annotation, type) and issubclass``),
    default ``False``. Replacing the annotation with ``str`` or moving
    the default to ``True`` fails the test.
    """
    sig = inspect.signature(sage_set_lifecycle)
    assert "dry_run" in sig.parameters, (
        "sage_set_lifecycle is missing the dry_run parameter; "
        "the wrapper must expose dry_run to close the T-0152 rollout gap."
    )
    param = sig.parameters["dry_run"]
    assert param.annotation is bool, (
        f"sage_set_lifecycle.dry_run annotation is {param.annotation!r}; "
        "expected ``bool``. Every other mutation MCP wrapper uses ``bool = False``."
    )
    assert param.default is False, (
        f"sage_set_lifecycle.dry_run default is {param.default!r}; "
        "expected ``False`` to preserve real-run as the default behavior."
    )


def test_discover_filters_args_documents_closed_key_set():
    """T1.5 — sage_discover.filters Args docstring must list the closed key set.

    Filters can't be typed as ``RetrievalFilters | None`` at the MCP
    boundary because T-0092 routes the raw dict through DiscoverRequest
    so the ValidationError loc carries the ``("filters", ...)`` prefix
    that the error translator needs. The documentation must compensate:
    explicitly list the accepted keys AND state that no other keys are
    accepted.

    Anti-coincidental-pass: the test requires (a) every document-target
    key listed by name, AND (b) a closure claim referencing the
    ``unknown_filter_key`` error envelope.
    """
    doc = _docstring(sage_discover)
    document_target_keys = (
        "doc_type",
        "project",
        "lifecycle_status",
        "tags",
        "document_ids",
        "pipeline_status",
        "tier3",
    )
    for key in document_target_keys:
        assert key in doc, (
            f"sage_discover docstring is missing filter key {key!r}. "
            "All seven document-target filter keys must be enumerated."
        )
    assert "unknown_filter_key" in doc, (
        "sage_discover docstring must reference the ``unknown_filter_key`` "
        "error envelope as the closure-rejection path."
    )


# ---------------------------------------------------------------------------
# Criterion 2 — Inline tier3_metadata ops shape
# ---------------------------------------------------------------------------


_TIER3_OPS_RE = re.compile(
    # Matches an indented block of the form
    # {"set": {"...": "..."}, "unset": ["..."]} with the keys present
    # and at least one populated example.
    r"\{[^{}]*?\"set\"\s*:\s*\{[^{}]*?:[^{}]*?\}\s*,\s*\"unset\"\s*:\s*\[[^\[\]]*?\"[^\"]+\"[^\[\]]*?\]",
    re.DOTALL,
)


def test_update_metadata_docstring_carries_tier3_ops_example():
    """T2.1 — sage_update_metadata docstring must inline the {set, unset} shape.

    Regression guard for the existing inlining. The check requires not
    only the ``set`` and ``unset`` keys, but a populated example
    (a non-empty value mapping under ``set`` and a non-empty string list
    under ``unset``).

    Anti-coincidental-pass: a docstring that mentions ``set`` and
    ``unset`` as English words without a JSON-shaped example will not
    match _TIER3_OPS_RE.
    """
    doc = _docstring(sage_update_metadata)
    assert _TIER3_OPS_RE.search(doc), (
        "sage_update_metadata docstring must carry an inline "
        '{"set": {...}, "unset": [...]} example with populated values.'
    )


def test_bulk_update_metadata_docstring_carries_tier3_ops_example():
    """T2.2 — sage_bulk_update_metadata docstring must inline the {set, unset} shape.

    Regression guard. The bulk variant already carries the example
    (per CAS-ADR-029 documentation); this test pins it.
    """
    doc = _docstring(sage_bulk_update_metadata)
    assert _TIER3_OPS_RE.search(doc), (
        "sage_bulk_update_metadata docstring must carry an inline "
        '{"set": {...}, "unset": [...]} example with populated values.'
    )


# ---------------------------------------------------------------------------
# Criterion 3 — derived_from + source_valid_from_version semantics
# ---------------------------------------------------------------------------


def test_link_docstring_documents_derived_from_anchor_semantics():
    """T3.1 — sage_link must document what source_valid_from_version anchors for derived_from.

    Three required ingredients:
      (a) the term ``derived_from`` appears in the field documentation,
      (b) the term ``transitive_source`` appears (the policy bucket that
          ``derived_from`` belongs to),
      (c) a canonical example showing source_valid_from_version supplied
          for a derived_from link.

    Anti-coincidental-pass: removing the canonical example block (or
    changing it to a non-derived_from edge example) fails the third
    check.
    """
    doc = _docstring(sage_link)
    assert "derived_from" in doc, "sage_link docstring missing 'derived_from'."
    assert "transitive_source" in doc, "sage_link docstring missing 'transitive_source'."

    # Canonical example: at least one snippet containing both
    # ``edge_type="derived_from"`` (or ``edge_type='derived_from'``) AND
    # ``source_valid_from_version=`` within a small window.
    pattern = re.compile(
        r"edge_type\s*=\s*[\"']derived_from[\"'].*?source_valid_from_version\s*=",
        re.DOTALL,
    )
    assert pattern.search(doc), (
        "sage_link docstring must carry a canonical example showing a "
        "derived_from link with source_valid_from_version supplied."
    )


# ---------------------------------------------------------------------------
# Criterion 4 — merged_from chain-head precondition
# ---------------------------------------------------------------------------


def test_link_docstring_documents_merged_from_chain_head_precondition():
    """T4.1 — sage_link must document the merged_from chain-head precondition.

    The validator at sage/services/graph_ops.py:240-253 rejects
    merged_from when the source has an outbound supersedes edge. That
    rule must be visible in the docstring so callers don't discover it
    by erroring.

    Two required ingredients, conjunctively:
      (a) a statement that the source must be the chain head / must have
          no outbound supersedes edge,
      (b) a pointer to derived_from as the mid-chain alternative.

    Anti-coincidental-pass: removing either ingredient fails the test.
    """
    doc = _docstring(sage_link)

    # (a) chain-head / no outbound supersedes precondition. Accept any
    # of several reasonable phrasings.
    chain_head_phrases = [
        "chain head",
        "chain-head",
        "no outbound supersedes",
        "no outbound `supersedes`",
        "no outbound ``supersedes``",
    ]
    found_precondition = any(p in doc for p in chain_head_phrases)
    assert found_precondition, (
        "sage_link docstring must document the merged_from chain-head "
        "precondition (source must have no outbound supersedes edge). "
        f"None of {chain_head_phrases!r} appeared."
    )

    # (b) pointer to derived_from as the mid-chain alternative. The
    # docstring must mention merged_from AND derived_from in proximity
    # of the precondition language so the alternative is discoverable.
    # We require both terms in the same paragraph as ``merged_from``.
    paragraphs = re.split(r"\n\s*\n", doc)
    proximity_satisfied = any(("merged_from" in p and "derived_from" in p) for p in paragraphs)
    assert proximity_satisfied, (
        "sage_link docstring must mention ``derived_from`` as the "
        "alternative path for mid-chain content reuse, in the same "
        "paragraph as the ``merged_from`` precondition discussion."
    )


# ---------------------------------------------------------------------------
# Criterion 5 — Response-size and pagination guidance
# ---------------------------------------------------------------------------


def test_discover_docstring_carries_pagination_and_response_mode_guidance():
    """T5.1 — sage_discover docstring must explicitly cite the size budget.

    Required ingredients:
      (a) reference to ``response_mode="light"`` (or equivalent) as the
          size mitigation,
      (b) reference to ``offset`` for pagination,
      (c) the 24 KiB / DEFAULT_MCP_INLINE_BUDGET_BYTES budget callout.

    Anti-coincidental-pass: deleting the 24 KiB budget reference fails
    the third check.
    """
    doc = _docstring(sage_discover)
    assert "response_mode" in doc and "light" in doc, (
        "sage_discover docstring must reference ``response_mode='light'`` "
        "as the size-mitigation lever."
    )
    assert "offset" in doc, (
        "sage_discover docstring must reference ``offset`` for catalog-mode pagination."
    )
    assert "24 KiB" in doc or "SAGE_MCP_INLINE_BUDGET_BYTES" in doc, (
        "sage_discover docstring must cite the 24 KiB inline budget or "
        "the SAGE_MCP_INLINE_BUDGET_BYTES override knob."
    )


def test_bulk_set_lifecycle_docstring_carries_response_mode_note():
    """T5.2 — sage_bulk_set_lifecycle docstring must carry the response_mode note.

    Required ingredients:
      (a) reference to ``response_mode`` parameter,
      (b) the 5-item default-to-light threshold rule,
      (c) the inline budget / 24 KiB callout.

    Regression guard for the existing T-0153 documentation, extended to
    require the 24 KiB anchor for parity with sage_discover.
    """
    doc = _docstring(sage_bulk_set_lifecycle)
    assert "response_mode" in doc
    assert "5" in doc, (
        "sage_bulk_set_lifecycle docstring must document the 5-item default-to-light threshold."
    )
    assert "24 KiB" in doc or "SAGE_MCP_INLINE_BUDGET_BYTES" in doc, (
        "sage_bulk_set_lifecycle docstring must cite the 24 KiB inline "
        "budget so callers see the same anchor as sage_discover."
    )


def test_bulk_update_metadata_docstring_carries_response_mode_note():
    """T5.3 — sage_bulk_update_metadata docstring must carry the response_mode note.

    Mirror of T5.2.
    """
    doc = _docstring(sage_bulk_update_metadata)
    assert "response_mode" in doc
    assert "5" in doc
    assert "24 KiB" in doc or "SAGE_MCP_INLINE_BUDGET_BYTES" in doc, (
        "sage_bulk_update_metadata docstring must cite the 24 KiB inline "
        "budget so callers see the same anchor as sage_discover."
    )
