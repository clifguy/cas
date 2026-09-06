"""SAGE MCP self-documentation conformance.

Asserts that the MCP tool surface documents valid vocabulary, payload
shapes, and response-size characteristics directly via tool signatures
and docstrings, so callers do not have to learn by erroring. Anchored
in the field-use report.

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

import pytest

from sage.mcp_server import (
    create_edges,
    get_filename_metadata,
    get_vault_config,
    ingest_document,
    list_directory,
    recompute_abstract,
    recompute_pipeline,
    search,
    update_lifecycles,
    update_metadata,
)
from sage.models.enums import (
    TERMINAL_PIPELINE_STATUSES,
    EdgeType,
    PipelineStatus,
    RationaleKind,
    RetrievalMode,
)
from sage.sage_api_tools import _INGEST_METADATA_KEYS, _SEARCH_FILTER_KEYS
from tests.helpers.adapter_claims import ENABLEMENT_CLAIM_MARKERS

# CAS-ADR-029 v4 plural-noun collapse: the pre-CAS-ADR-029 singleton tools
# (create_edge, update_lifecycle, bulk_update_lifecycle, bulk_update_metadata)
# folded into create_edges / update_lifecycles / update_metadata, which take
# items: list[dict] instead of flat per-call arguments. The tests below
# that introspected the flat singleton signature (edge_type / rationale_kind
# at the top level) no longer have a meaningful target — those fields now
# live inside each items[] entry's per-item schema. Tests that examined
# docstring content still apply against the consolidated tools where the
# semantic content was preserved.
bulk_update_lifecycle = update_lifecycles
bulk_update_metadata = update_metadata
update_lifecycle = update_lifecycles
create_edge = create_edges

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


def test_link_edge_type_per_item_field_is_enum():
    """T1.1 — ``BulkLinkItem.edge_type`` must carry the ``EdgeType`` StrEnum.

    Per CAS-ADR-029 v4 the ``create_edges`` MCP tool takes
    ``items: list[dict]``, with the per-item ``edge_type`` shape
    enforced by ``BulkLinkItem`` rather than by a flat parameter on
    the tool signature. The enum-typing guard moves with the field:
    a per-item ``edge_type: str`` regression would let callers pass
    arbitrary strings through to the service layer.

    Anti-coincidental-pass: identity equality with ``EdgeType``, not
    isinstance/issubclass; replacing with a sibling enum fails the
    test, and reverting to ``str`` fails it.
    """
    from sage.models.schemas import BulkLinkItem

    ann = BulkLinkItem.model_fields["edge_type"].annotation
    assert ann is EdgeType, (
        f"BulkLinkItem.edge_type annotation is {ann!r}; expected EdgeType. "
        "Without the enum at the per-item schema, callers receive a "
        "free-form string at the MCP boundary."
    )


def test_link_rationale_kind_per_item_field_is_enum():
    """T1.2 — ``BulkLinkItem.rationale_kind`` must carry
    ``RationaleKind | None``.

    Same rationale as test_link_edge_type_per_item_field_is_enum.
    Optional because the field is nullable.
    """
    from sage.models.schemas import BulkLinkItem

    ann = BulkLinkItem.model_fields["rationale_kind"].annotation
    assert _annotation_includes(ann, RationaleKind), (
        f"BulkLinkItem.rationale_kind annotation is {ann!r}; expected "
        "RationaleKind | None. Callers currently see this as a free-form "
        "optional string at the MCP boundary."
    )


def test_discover_mode_signature_is_enum():
    """T1.4 — search.mode must be typed as the RetrievalMode StrEnum.

    DiscoverRequest.mode uses RetrievalMode but the tool function signature
    declares ``mode: str = "semantic"``. The default must remain a valid
    enum value (RetrievalMode.SEMANTIC) so existing callers keep working.
    """
    ann = _annotation_of(search, "mode")
    assert ann is RetrievalMode, (
        f"search.mode annotation is {ann!r}; expected RetrievalMode. "
        "Callers picking the mode value from the tool schema currently "
        "see a free-form string."
    )


def test_set_lifecycle_action_docstring_points_at_vault_config():
    """T1.3 — ``update_lifecycles`` docstring must reference the
    vault-config-defined action vocabulary and direct callers at
    ``get_vault_config`` for the authoritative list.

    Post-CAS-ADR-029: the tool is ``update_lifecycles`` (collapsed plural-
    noun); the underlying authority pointer is ``get_vault_config``.

    Anti-coincidental-pass: the docstring must mention BOTH ``vault
    config`` (closure source) and ``get_vault_config`` (the
    discovery tool). Mentioning one without the other fails.
    """
    doc = _docstring(update_lifecycles)
    assert "vault config" in doc.lower(), (
        "update_lifecycles docstring must reference 'vault config' as "
        "the authoritative source of the action vocabulary."
    )
    assert "get_vault_config" in doc, (
        "update_lifecycles docstring must point callers at "
        "``get_vault_config`` for the authoritative action list."
    )


def test_adapter_docstrings_do_not_claim_vault_config_enablement():
    """No tool docstring conditions adapter availability on vault config.

    Adapter resolution is a lookup in the process-wide registry built by
    ``build_source_adapter_registry``; vault configuration declares no
    adapters at all. A docstring that says a source type must be
    *enabled* sends a caller diagnosing ``adapter_not_found`` to a config file
    that cannot affect it.

    Covers the two tools that raise the error and ``get_vault_config``,
    which is where a caller goes to look the answer up -- the pointer surface
    misdirects just as effectively as the error surface.
    """
    for tool in (ingest_document, get_filename_metadata, get_vault_config):
        doc = _docstring(tool)
        for marker in ENABLEMENT_CLAIM_MARKERS:
            assert marker not in doc, (
                f"{tool.__name__} docstring conditions adapter availability on "
                f"vault configuration ({marker!r}), which adapter resolution "
                "never consults."
            )


def test_adapter_error_tools_still_document_adapter_not_found():
    """The two tools that can raise ``adapter_not_found`` still name it.

    Anti-coincidental-pass partner to the test above: absence of the
    misleading wording is necessary but not sufficient, since deleting the
    error-mode entry outright would also remove the marker. Keeping the
    presence assertion separate means a deletion fails here rather than
    passing there.
    """
    for tool in (ingest_document, get_filename_metadata):
        assert "adapter_not_found" in _docstring(tool), (
            f"{tool.__name__} docstring must document ``adapter_not_found`` as an error mode."
        )


def test_ingest_document_documents_the_nested_metadata_shape():
    """``ingest_document`` names every recognized metadata key and the nesting.

    A caller who has to learn the shape by erroring is the failure this
    gate exists to prevent, and metadata is the field most often spelled
    at the wrong level. Structural rather than substring-only: the
    docstring must name the *set* of keys the guard recognizes, so a key
    added to ``_INGEST_METADATA_KEYS`` without a docs update fails here
    instead of silently becoming undocumented.
    """
    doc = _docstring(ingest_document)
    undocumented = [key for key in _INGEST_METADATA_KEYS if f"``{key}``" not in doc]
    assert not undocumented, (
        f"ingest_document docstring must name every recognized metadata key; "
        f"missing: {undocumented}"
    )
    assert "misplaced_metadata" in doc, (
        "ingest_document docstring must document ``misplaced_metadata`` as an error mode."
    )
    # The worked example, not just the prose claim.
    assert 'metadata={"title"' in doc, (
        "ingest_document docstring must show the nested metadata call shape."
    )


def test_search_documents_the_nested_filters_shape():
    """``search`` names every protected filter key and the nesting.

    The read-side counterpart of the ingest metadata gate, and the one
    with the quieter failure: a metadata key at the wrong level yields a
    mis-titled document, while a filter key at the wrong level yields
    plausible rows that were never filtered. Structural rather than
    substring-only: a key added to ``RetrievalFilters`` -- and so to
    ``_SEARCH_FILTER_KEYS`` -- without a docs update fails here instead
    of silently becoming undocumented.
    """
    doc = _docstring(search)
    undocumented = [key for key in _SEARCH_FILTER_KEYS if f"``{key}``" not in doc]
    assert not undocumented, (
        f"search docstring must name every protected filter key; missing: {undocumented}"
    )
    assert "misplaced_filters" in doc, (
        "search docstring must document ``misplaced_filters`` as an error mode."
    )
    # The worked example, not just the prose claim.
    assert 'filters={"doc_type"' in doc, "search docstring must show the nested filters call shape."


def test_ingest_document_documents_source_type_inference():
    """``ingest_document`` states that ``source_type`` may be omitted.

    The parameter is optional and inferred from the file extension; a
    docstring that presents it as mandatory sends callers to pass a value
    they do not need, which is the paper cut this documents away.
    """
    doc = _docstring(ingest_document)
    assert "inferred" in doc, (
        "ingest_document docstring must state that source_type is inferred when omitted."
    )
    assert "``.md``" in doc or ".md" in doc, (
        "ingest_document docstring must give a concrete inferred extension."
    )


def test_set_lifecycle_signature_exposes_dry_run():
    """— update_lifecycle must expose ``dry_run: bool = False`` at the wrapper.

    The dry-run rollout shipped on every other mutation tool
    but skipped the single-form ``update_lifecycle`` wrapper. The
    underlying ``SetLifecycleRequest`` already carries ``dry_run`` and
    ``LifecycleService._set_lifecycle`` honors it; the gap was at the
    MCP boundary.

    Structural assertion: parameter present, annotation identity-equal
    to ``bool`` (not ``isinstance(annotation, type) and issubclass``),
    default ``False``. Replacing the annotation with ``str`` or moving
    the default to ``True`` fails the test.
    """
    sig = inspect.signature(update_lifecycle)
    assert "dry_run" in sig.parameters, (
        "update_lifecycle is missing the dry_run parameter; "
        "the wrapper must expose dry_run to close the T-0152 rollout gap."
    )
    param = sig.parameters["dry_run"]
    assert param.annotation is bool, (
        f"update_lifecycle.dry_run annotation is {param.annotation!r}; "
        "expected ``bool``. Every other mutation MCP wrapper uses ``bool = False``."
    )
    assert param.default is False, (
        f"update_lifecycle.dry_run default is {param.default!r}; "
        "expected ``False`` to preserve real-run as the default behavior."
    )


def test_discover_filters_args_documents_closed_key_set():
    """T1.5 — search.filters Args docstring must list the closed key set.

    Filters can't be typed as ``RetrievalFilters | None`` at the MCP
    boundary because routes the raw dict through DiscoverRequest
    so the ValidationError loc carries the ``("filters", ...)`` prefix
    that the error translator needs. The documentation must compensate:
    explicitly list the accepted keys AND state that no other keys are
    accepted.

    Anti-coincidental-pass: the test requires (a) every document-target
    key listed by name, AND (b) a closure claim referencing the
    ``unknown_filter_key`` error envelope.
    """
    doc = _docstring(search)
    document_target_keys = (
        "doc_type",
        "project",
        "lifecycle_status",
        "tags",
        "document_ids",
        "pipeline_status",
        "source_type",
        "tier3_metadata",
    )
    for key in document_target_keys:
        assert key in doc, (
            f"search docstring is missing filter key {key!r}. "
            "All eight document-target filter keys must be enumerated."
        )
    assert "unknown_filter_key" in doc, (
        "search docstring must reference the ``unknown_filter_key`` "
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
    """T2.1 — update_metadata docstring must inline the {set, unset} shape.

    Regression guard for the existing inlining. The check requires not
    only the ``set`` and ``unset`` keys, but a populated example
    (a non-empty value mapping under ``set`` and a non-empty string list
    under ``unset``).

    Anti-coincidental-pass: a docstring that mentions ``set`` and
    ``unset`` as English words without a JSON-shaped example will not
    match _TIER3_OPS_RE.
    """
    doc = _docstring(update_metadata)
    assert _TIER3_OPS_RE.search(doc), (
        "update_metadata docstring must carry an inline "
        '{"set": {...}, "unset": [...]} example with populated values.'
    )


def test_bulk_update_metadata_docstring_carries_tier3_ops_example():
    """T2.2 — bulk_update_metadata docstring must inline the {set, unset} shape.

    Regression guard. The bulk variant already carries the example
    (per CAS-ADR-029 documentation); this test pins it.
    """
    doc = _docstring(bulk_update_metadata)
    assert _TIER3_OPS_RE.search(doc), (
        "bulk_update_metadata docstring must carry an inline "
        '{"set": {...}, "unset": [...]} example with populated values.'
    )


# ---------------------------------------------------------------------------
# Criterion 3 — derived_from + source_valid_from_version semantics
# ---------------------------------------------------------------------------


def test_link_docstring_documents_derived_from_anchor_semantics():
    """T3.1 — create_edge must document what source_valid_from_version anchors for derived_from.

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
    doc = _docstring(create_edge)
    assert "derived_from" in doc, "create_edge docstring missing 'derived_from'."
    assert "transitive_source" in doc, "create_edge docstring missing 'transitive_source'."

    # Canonical example: at least one snippet containing both
    # ``edge_type="derived_from"`` (or ``edge_type='derived_from'``) AND
    # ``source_valid_from_version=`` within a small window.
    pattern = re.compile(
        r"edge_type\s*=\s*[\"']derived_from[\"'].*?source_valid_from_version\s*=",
        re.DOTALL,
    )
    assert pattern.search(doc), (
        "create_edge docstring must carry a canonical example showing a "
        "derived_from link with source_valid_from_version supplied."
    )


# ---------------------------------------------------------------------------
# Criterion 4 — merged_from chain-head precondition
# ---------------------------------------------------------------------------


def test_link_docstring_documents_merged_from_chain_head_precondition():
    """T4.1 — create_edge must document the merged_from chain-head precondition.

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
    doc = _docstring(create_edge)

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
        "create_edge docstring must document the merged_from chain-head "
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
        "create_edge docstring must mention ``derived_from`` as the "
        "alternative path for mid-chain content reuse, in the same "
        "paragraph as the ``merged_from`` precondition discussion."
    )


# ---------------------------------------------------------------------------
# Criterion 5 — Response-size and pagination guidance
# ---------------------------------------------------------------------------


def test_discover_docstring_carries_pagination_and_response_mode_guidance():
    """T5.1 — search docstring must explicitly cite the size budget.

    Required ingredients:
      (a) reference to ``response_mode="light"`` (or equivalent) as the
          size mitigation,
      (b) reference to ``offset`` for pagination,
      (c) the 24 KiB / DEFAULT_MCP_INLINE_BUDGET_BYTES budget callout.

    Anti-coincidental-pass: deleting the 24 KiB budget reference fails
    the third check.
    """
    doc = _docstring(search)
    assert "response_mode" in doc and "light" in doc, (
        "search docstring must reference ``response_mode='light'`` as the size-mitigation lever."
    )
    assert "offset" in doc, (
        "search docstring must reference ``offset`` for catalog-mode pagination."
    )
    assert "24 KiB" in doc or "SAGE_MCP_INLINE_BUDGET_BYTES" in doc, (
        "search docstring must cite the 24 KiB inline budget or "
        "the SAGE_MCP_INLINE_BUDGET_BYTES override knob."
    )


def test_bulk_set_lifecycle_docstring_carries_response_mode_note():
    """T5.2 — bulk_update_lifecycle docstring must carry the response_mode note.

    Required ingredients:
      (a) reference to ``response_mode`` parameter,
      (b) the 5-item default-to-light threshold rule,
      (c) the inline budget / 24 KiB callout.

    Regression guard for the existing documentation, extended to
    require the 24 KiB anchor for parity with search.
    """
    doc = _docstring(bulk_update_lifecycle)
    assert "response_mode" in doc
    assert "5" in doc, (
        "bulk_update_lifecycle docstring must document the 5-item default-to-light threshold."
    )
    assert "24 KiB" in doc or "SAGE_MCP_INLINE_BUDGET_BYTES" in doc, (
        "bulk_update_lifecycle docstring must cite the 24 KiB inline "
        "budget so callers see the same anchor as search."
    )


def test_bulk_update_metadata_docstring_carries_response_mode_note():
    """T5.3 — bulk_update_metadata docstring must carry the response_mode note.

    Mirror of T5.2.
    """
    doc = _docstring(bulk_update_metadata)
    assert "response_mode" in doc
    assert "5" in doc
    assert "24 KiB" in doc or "SAGE_MCP_INLINE_BUDGET_BYTES" in doc, (
        "bulk_update_metadata docstring must cite the 24 KiB inline "
        "budget so callers see the same anchor as search."
    )


# ---------------------------------------------------------------------------
# Criterion 6 — filesystem-scan vs vault-document-enumeration disambiguation
# ---------------------------------------------------------------------------


def test_list_directory_docstring_redirects_to_catalog_search():
    """T6.1 — list_directory must redirect vault-document enumeration to
    catalog-mode search.

    list_directory scans a *filesystem* path for pre-ingest discovery and
    requires a ``directory`` argument; callers reaching for it to "list the
    documents in a vault" hit ``directory Required`` because the tool name
    reads like a vault-content lister. The docstring must name the catalog-
    mode enumerator so the correction rides in the published schema, not
    just human-read prose.

    Anti-coincidental-pass: the anchor is the literal redirect target
    ``search(mode="catalog"`` (plus the ``response_mode="light"`` form),
    which does NOT appear in the pre-change docstring — that docstring only
    points at ``bulk_ingest_document`` and uses the word "discovery". A
    docstring that merely repeats "directory" or "discovery" cannot pass.
    """
    doc = _docstring(list_directory)
    assert 'search(mode="catalog"' in doc, (
        "list_directory docstring must redirect vault-document enumeration "
        'to ``search(mode="catalog", response_mode="light")``.'
    )
    assert 'response_mode="light"' in doc, (
        "the redirect must name the ``light`` response_mode form so callers "
        "get the stripped enumeration payload."
    )


def test_discover_catalog_mode_named_canonical_document_enumerator():
    """T6.2 — search's catalog-mode description must name it the canonical
    vault-document enumerator.

    The surface gives no naming hint that catalog mode is how you list the
    documents already in a vault. The Modes section must say so explicitly
    so schema-reading clients pick catalog over the filesystem scanner.

    Anti-coincidental-pass: the catalog-mode block is isolated before the
    check. The word ``canonical`` already appears elsewhere in the docstring
    (the ``response_mode`` arg: "Canonical payload-depth selector"), so a
    whole-docstring substring check would pass without the change. Block
    isolation is what makes this test fail pre-edit.
    """
    doc = _docstring(search)
    match = re.search(r"\bcatalog:.*?(?=\n\s*deterministic:)", doc, re.DOTALL)
    assert match is not None, (
        "search docstring must retain a ``catalog:`` mode description block in the Modes section."
    )
    catalog_block = match.group(0)
    assert "canonical" in catalog_block.lower(), (
        "the catalog-mode description must name catalog mode the canonical "
        "vault-document enumerator (the word 'canonical' must appear in the "
        "``catalog:`` block, not merely elsewhere in the docstring)."
    )


def test_discover_docstring_documents_source_type_vocabulary():
    """The source_type filter must publish its closed vocabulary.

    source_type is typed against the SourceType enum, so an unlisted
    value is refused rather than silently returning zero rows. That is
    only useful if the caller can read the accepted set without a probe
    round-trip.

    Anti-coincidental-pass: requires (a) every one of the eight source
    types by name, AND (b) the ``invalid_filter_value`` envelope that
    names the rejection path. Listing the key alone -- which the closed
    key-set test already checks -- would not satisfy this.
    """
    doc = _docstring(search)
    for value in (
        "markdown",
        "docx",
        "pdf",
        "email",
        "onenote",
        "teams_chat",
        "xlsx",
        "pptx",
    ):
        assert value in doc, (
            f"search docstring is missing source_type value {value!r}. "
            "The closed vocabulary must be enumerated so a caller can "
            "self-correct without probing."
        )
    assert "invalid_filter_value" in doc, (
        "search docstring must reference the ``invalid_filter_value`` "
        "error envelope as the out-of-vocabulary rejection path."
    )


def test_discover_docstring_documents_every_retrieval_target():
    """Every RetrievalTarget member must appear as target="<value>" in
    the search docstring.

    Enum-driven so the gate fails closed: adding another target member
    without documenting it fails here with no test edit required.
    """
    from sage.models.enums import RetrievalTarget

    doc = _docstring(search)
    for member in RetrievalTarget:
        needle = f'target="{member.value}"'
        assert needle in doc, (
            f"search docstring must document {needle} alongside the other "
            "retrieval targets; every RetrievalTarget member needs a "
            "documented dispatch."
        )


def test_discover_facet_block_documents_facet_field_vocabulary():
    """The Facet enumeration block must enumerate all five facet fields.

    Anti-coincidental-pass: every facet field name also appears in the
    filters arg documentation, so a whole-docstring substring check
    would pass without the block existing at all. The block is isolated
    first, mirroring the catalog-block isolation above.
    """
    doc = _docstring(search)
    match = re.search(r"Facet enumeration:.*?(?=\n\s*Response-mode semantics)", doc, re.DOTALL)
    assert match is not None, (
        "search docstring must carry a ``Facet enumeration:`` section "
        "between the edge-enumeration example and the response-mode matrix."
    )
    facet_block = match.group(0)
    for field in (
        "doc_type",
        "lifecycle_status",
        "source_type",
        "pipeline_status",
        "tags",
    ):
        assert field in facet_block, (
            f"the Facet enumeration block must name facet field {field!r} "
            "(inside the block, not merely elsewhere in the docstring)."
        )
    # The truncation contract lives in the same block: the cap parameter
    # and the truncation-visibility field must be documented where the
    # facet caller reads, so a future doc edit cannot silently drop the
    # bounded-by-default promise.
    for term in ("facet_value_limit", "total_distinct"):
        assert term in facet_block, (
            f"the Facet enumeration block must document {term!r} -- the "
            "value cap and its visibility are part of the facet contract."
        )


# ---------------------------------------------------------------------------
# Async-dispatch wait guidance
# ---------------------------------------------------------------------------

#: The tools that dispatch pipeline stages as a background task and return
#: with a non-terminal ``pipeline_status``, paired with the terminal states
#: each one can actually reach. Their docstrings are the only place a caller
#: learns how to observe the outcome, so the guidance they carry decides
#: whether a caller waits once or polls per turn.
#:
#: The per-tool sets differ because the paths differ: ``recompute_abstract``
#: abstracts from stored chunks via ``_execute_abstract_from_chunks``, which
#: stamps ``abstraction_complete`` or raises, so the ``abstraction_skipped``
#: branches (abstraction disabled, empty projection text) are unreachable
#: from it. The other two run ``_execute_pipeline_stages``, which carries
#: both of those branches.
_ASYNC_DISPATCH_TOOLS: tuple[tuple[str, Any, frozenset[PipelineStatus]], ...] = (
    (
        "ingest_document",
        ingest_document,
        frozenset(
            {
                PipelineStatus.ABSTRACTION_COMPLETE,
                PipelineStatus.ABSTRACTION_SKIPPED,
                PipelineStatus.ABSTRACTION_INTERRUPTED,
                PipelineStatus.FAILED,
            }
        ),
    ),
    (
        "recompute_abstract",
        recompute_abstract,
        frozenset(
            {
                PipelineStatus.ABSTRACTION_COMPLETE,
                PipelineStatus.ABSTRACTION_INTERRUPTED,
                PipelineStatus.FAILED,
            }
        ),
    ),
    (
        "recompute_pipeline",
        recompute_pipeline,
        frozenset(
            {
                PipelineStatus.ABSTRACTION_COMPLETE,
                PipelineStatus.ABSTRACTION_SKIPPED,
                PipelineStatus.ABSTRACTION_INTERRUPTED,
                PipelineStatus.FAILED,
            }
        ),
    ),
)

_ASYNC_DISPATCH_IDS = [name for name, _, _ in _ASYNC_DISPATCH_TOOLS]

#: The phrasing this guidance retired. An LLM caller reads "poll X" as an
#: instruction to call X once per turn, which is the behavior these tools
#: must not invite.
_RETIRED_POLL_PHRASE = "poll ``get_document``"

#: The canonical direction that replaces it.
_WAIT_PHRASE = "wait for a terminal"

#: Tokens that belong to a specific caller's environment rather than to the
#: tool contract. A docstring is read by callers with no shell, no harness,
#: and no access to this project's internal guidance, so the executable
#: recipe lives outside the published surface.
_CALLER_ENVIRONMENT_TOKENS: tuple[str, ...] = (
    "run_in_background",
    "curl",
    "sleep ",
    "sage mcp tool surface",
)


def _normalized_docstring(fn: Any) -> str:
    """``fn``'s docstring, whitespace-collapsed and lowercased.

    Docstring prose wraps, so a phrase under test can straddle a line
    break; collapsing first makes the assertions independent of where the
    wrap happens to fall.
    """
    return re.sub(r"\s+", " ", _docstring(fn)).lower()


def test_async_dispatch_reachable_statuses_cover_the_terminal_enum():
    """The per-tool reachable sets together account for every terminal status.

    Guards the table above against the enum moving underneath it. Without
    this, a terminal status added to ``TERMINAL_PIPELINE_STATUSES`` would
    simply go unmentioned by every tool and the per-tool gate below would
    stay green -- the table would silently stop being a partition of the
    thing it claims to partition. Failing here forces the author to decide
    which tools can reach the new status rather than defaulting to none.
    """
    covered = frozenset().union(*(reachable for _, _, reachable in _ASYNC_DISPATCH_TOOLS))
    assert covered == TERMINAL_PIPELINE_STATUSES, (
        "the per-tool reachable-status table must account for every member of "
        f"TERMINAL_PIPELINE_STATUSES; unaccounted: "
        f"{sorted(s.value for s in TERMINAL_PIPELINE_STATUSES - covered)}, "
        f"not terminal: {sorted(s.value for s in covered - TERMINAL_PIPELINE_STATUSES)}"
    )


@pytest.mark.parametrize(
    ("tool_name", "tool", "reachable"), _ASYNC_DISPATCH_TOOLS, ids=_ASYNC_DISPATCH_IDS
)
def test_async_dispatch_tools_name_every_reachable_terminal_status(
    tool_name: str, tool: Any, reachable: frozenset[PipelineStatus]
):
    """Each async-dispatch tool enumerates the terminal states it can reach.

    A caller writing a wait needs the full exit condition for *that* tool.
    Naming only the status the happy path reaches produces a wait that hangs
    on the others.

    Scoped per tool rather than to the whole enum on purpose. A uniform
    assertion against ``TERMINAL_PIPELINE_STATUSES`` reads as stricter but
    is worse: it forces every tool to name ``abstraction_skipped``, which
    ``recompute_abstract`` cannot produce, so the gate would be satisfied
    only by documenting an outcome that never occurs. A test that can be
    satisfied by a false statement is not a gate.
    """
    doc = _normalized_docstring(tool)
    missing = sorted(status.value for status in reachable if status.value not in doc)
    assert not missing, (
        f"{tool_name} docstring must name every terminal pipeline_status it can reach "
        f"so a caller can write a complete exit condition; missing: {missing}"
    )


@pytest.mark.parametrize(
    ("tool_name", "tool", "reachable"), _ASYNC_DISPATCH_TOOLS, ids=_ASYNC_DISPATCH_IDS
)
def test_async_dispatch_tools_direct_callers_to_a_caller_side_wait(
    tool_name: str, tool: Any, reachable: frozenset[PipelineStatus]
):
    """Each async-dispatch tool directs callers to a wait, not a poll.

    These tools return before the pipeline finishes, so the docstring has
    to say how to observe the terminal outcome. Saying "poll" invites one
    call per turn; an agent caller that reads it literally spends a turn
    and a result payload per tick for the whole duration of the work. The
    replacement directs the caller to wait for a terminal status instead,
    leaving where that wait runs to the caller's environment.
    """
    doc = _normalized_docstring(tool)
    assert _RETIRED_POLL_PHRASE not in doc, (
        f"{tool_name} docstring still carries the retired per-turn poll direction "
        f"{_RETIRED_POLL_PHRASE!r}; direct the caller to wait for a terminal status instead."
    )
    assert _WAIT_PHRASE in doc, (
        f"{tool_name} docstring must direct the caller to {_WAIT_PHRASE!r} "
        "pipeline_status, so the disclosure survives the removal of the poll phrasing."
    )


@pytest.mark.parametrize(
    ("tool_name", "tool", "reachable"), _ASYNC_DISPATCH_TOOLS, ids=_ASYNC_DISPATCH_IDS
)
def test_async_dispatch_docstrings_carry_no_caller_environment_recipe(
    tool_name: str, tool: Any, reachable: frozenset[PipelineStatus]
):
    """The wait guidance stays a contract, not a recipe.

    Regression guard rather than a driver: it passes on the pre-rewrite
    docstrings too. It exists because the natural way to write good wait
    guidance is to show the caller a working loop, and a loop is only
    workable for a caller that shares this project's shell, transport
    reachability, and internal guidance documents. The tool contract is
    read by callers that share none of those.
    """
    doc = _normalized_docstring(tool)
    found = sorted(token for token in _CALLER_ENVIRONMENT_TOKENS if token in doc)
    assert not found, (
        f"{tool_name} docstring carries caller-environment specifics {found}; "
        "the published contract names the endpoint and the terminal statuses, "
        "and leaves the mechanics to the caller."
    )


#: Words that direct a caller to repeat a status read.
_POLL_WORD = re.compile(r"\bpoll(?:s|ed|ing)?\b")

#: The document-status surfaces a caller would be told to poll.
_STATUS_SURFACE = re.compile(r"get_document|get /documents|/documents/\{document_id\}")

#: How close a poll word and a status surface must be to read as one direction
#: rather than two unrelated sentences.
_POLL_PROXIMITY_CHARS = 90


def _poll_directions(doc: str) -> list[str]:
    """Spans where a poll word sits near a document-status surface.

    Matches on proximity rather than on a fixed phrase because the direction
    was written four different ways across the surface -- ``poll
    get_document``, ``poll `GET /documents/{id}` ``, and ``get_document polls
    terminal ...`` among them. A fixed-phrase check retires whichever spelling
    it was written against and leaves the rest, which is how the same guidance
    survived on a sibling tool surface after the first sweep.
    """
    hits: list[str] = []
    for match in _POLL_WORD.finditer(doc):
        window = doc[
            max(0, match.start() - _POLL_PROXIMITY_CHARS) : match.end() + _POLL_PROXIMITY_CHARS
        ]
        if _STATUS_SURFACE.search(window):
            hits.append(window.strip())
    return hits


def _registered_mcp_tools() -> dict[str, Any]:
    """Every registered MCP tool across both surfaces, keyed by tool name."""
    from sage import mcp_server

    tools: dict[str, Any] = {}
    for attr in ("_sage_tools", "_app_tools"):
        tools.update(getattr(mcp_server, attr))
    return tools


def test_no_registered_mcp_tool_directs_callers_to_poll_for_status():
    """No tool on either surface tells a caller to poll a document for status.

    Surface-wide rather than enumerated, because the enumerated form is what
    let this guidance persist: a sweep keyed to a list of known async-dispatch
    tools left the same direction standing on ``bulk_ingest_document``, whose
    docstring is on the sibling app surface and phrased it in the opposite
    word order. Walking the live registry means a tool added later, or one
    nobody thought to list, is covered without anyone remembering to add it.

    Deliberately not flagged: guidance to poll a surface that is genuinely
    cheap to poll (vault stats, the liveness probe) and descriptions of the
    server's own internal wait loops, neither of which pairs a poll word with
    a document-status read.
    """
    offenders: dict[str, list[str]] = {}
    for name, tool in _registered_mcp_tools().items():
        doc = inspect.getdoc(tool)
        if doc is None:
            continue
        hits = _poll_directions(re.sub(r"\s+", " ", doc).lower())
        if hits:
            offenders[name] = hits

    assert not offenders, (
        "MCP tool docstring(s) direct the caller to poll a document for status; "
        "direct them to wait for a terminal pipeline_status instead:\n"
        + "\n".join(f"  {name}: {hits}" for name, hits in sorted(offenders.items()))
    )
