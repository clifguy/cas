"""SAGE service-layer self-documentation conformance.

Sibling drift-guard to ``test_mcp_self_documentation.py``. Asserts that
the structural disclosures the service-layer methods carry (precondition
surfaces, side-effects, idempotency contracts, atomicity guarantees,
shared default-resolution constants) remain literally present in their
docstrings.

The MCP-layer test is the only structural gate today; the service layer
has 14 mirrored disclosures with no equivalent test. A future refactor
that touches a service-layer docstring could silently drop a disclosure
and only manual review would catch it. Each test below pins one concept
in one method; each fails when its target disclosure is silently
deleted, reworded out of recognition, or split across paragraphs.

Style mirrors ``test_mcp_self_documentation.py``: module-scope tests,
no parametrization, one test function per concept-per-method,
``_docstring(fn)`` helper, paragraph-split proximity via
``re.split(r"\\n\\s*\\n", doc)`` for two-substring co-location checks.
"""

from __future__ import annotations

import inspect
import re
from typing import Any

from sage.services.batch_ingest import BatchIngestService
from sage.services.graph_ops import GraphOpsService
from sage.services.ingestion import IngestionService
from sage.services.lifecycle import LifecycleService
from sage.services.metadata import MetadataService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _docstring(fn: Any) -> str:
    """Return ``fn``'s docstring (or raise if absent)."""
    doc = inspect.getdoc(fn)
    assert doc is not None, f"{fn.__qualname__} has no docstring"
    return doc


def _paragraphs(doc: str) -> list[str]:
    """Split ``doc`` on blank lines into paragraph blocks."""
    return re.split(r"\n\s*\n", doc)


# ---------------------------------------------------------------------------
# IngestionService.ingest
# ---------------------------------------------------------------------------


def test_ingestion_ingest_docstring_documents_tier3_unique_constraint_violation():
    """``IngestionService.ingest`` must name ``Tier3UniqueConstraintViolation``.

    Anti-coincidental-pass: deleting the line that names the error class
    from the Tier3 uniqueness paragraph (or from the Raises block) drops
    the substring entirely and fails this assertion.
    """
    doc = _docstring(IngestionService.ingest)
    assert "Tier3UniqueConstraintViolation" in doc, (
        "IngestionService.ingest docstring must name "
        "``Tier3UniqueConstraintViolation`` so callers know which error "
        "to expect when a tier3 unique constraint is violated at ingest."
    )


def test_ingestion_ingest_docstring_documents_trio_field_inheritance():
    """``IngestionService.ingest`` must document trio-field inheritance on supersede.

    Two ingredients in the same paragraph:
      (a) the term ``trio-field inheritance`` (case-insensitive),
      (b) reference to ``predecessor`` so the supersede context is bound
          to the inheritance behavior.

    Anti-coincidental-pass: dropping the section heading while leaving
    the body, or splitting the heading from the predecessor reference
    across paragraphs, fails the proximity check.
    """
    doc = _docstring(IngestionService.ingest)
    assert "trio-field inheritance" in doc.lower(), (
        "IngestionService.ingest docstring must document ``trio-field "
        "inheritance`` (the supersede-time trio inheritance behavior "
        "from CAS-ADR-021)."
    )
    proximity_satisfied = any(
        "trio-field inheritance" in p.lower() and "predecessor" in p.lower()
        for p in _paragraphs(doc)
    )
    assert proximity_satisfied, (
        "IngestionService.ingest docstring must bind ``trio-field "
        "inheritance`` to ``predecessor`` in the same paragraph so the "
        "supersede context is explicit."
    )


def test_ingestion_ingest_docstring_documents_pipeline_status_terminal_states():
    """``IngestionService.ingest`` must document ``pipeline_status`` terminal states.

    Requires ``pipeline_status`` and ``abstraction_complete`` in the same
    paragraph, so callers see the concept (the field) and its happy-path
    value together rather than as scattered mentions.

    Anti-coincidental-pass: splitting the terminal-state paragraph or
    removing ``abstraction_complete`` from it fails the proximity check.
    """
    doc = _docstring(IngestionService.ingest)
    proximity_satisfied = any(
        "pipeline_status" in p and "abstraction_complete" in p for p in _paragraphs(doc)
    )
    assert proximity_satisfied, (
        "IngestionService.ingest docstring must enumerate "
        "``pipeline_status`` terminal-state values with "
        "``abstraction_complete`` named in the same paragraph as the "
        "field reference."
    )


def test_ingestion_ingest_docstring_documents_source_file_not_found():
    """``IngestionService.ingest`` must name ``SourceFileNotFoundError``.

    The class is raised directly inside ``ingest()`` when ``request.source``
    does not resolve to a readable file. Callers need it named in the
    ``Raises:`` block so they can catch it without reading source.

    Anti-coincidental-pass: deleting the line that names the error class
    from the ``Raises:`` block drops the substring entirely and fails
    this assertion.
    """
    doc = _docstring(IngestionService.ingest)
    assert "SourceFileNotFoundError" in doc, (
        "IngestionService.ingest docstring must name "
        "``SourceFileNotFoundError`` so callers know which error to "
        "expect when ``request.source`` does not resolve to a readable "
        "file."
    )


def test_ingestion_ingest_docstring_documents_tier3_schema_violation():
    """``IngestionService.ingest`` must name ``Tier3SchemaViolationError``.

    The class propagates from ``_validate_tier3_payload`` (called unguarded
    from ``ingest()``) when ``request.tier3_metadata`` fails schema
    validation against the resolved ``doc_type``'s ``metadata_schema``, or
    when the doc_type has no schema declared and a non-empty payload was
    supplied. Distinct precondition and envelope from
    ``Tier3UniqueConstraintViolation``, so the gate names both.

    Anti-coincidental-pass: deleting the line that names the error class
    from the ``Raises:`` block drops the substring entirely and fails
    this assertion.
    """
    doc = _docstring(IngestionService.ingest)
    assert "Tier3SchemaViolationError" in doc, (
        "IngestionService.ingest docstring must name "
        "``Tier3SchemaViolationError`` so callers know which error to "
        "expect when ``request.tier3_metadata`` fails schema validation."
    )


# ---------------------------------------------------------------------------
# GraphOpsService._create_edge_strict
# ---------------------------------------------------------------------------


def test_graph_ops_link_docstring_documents_merged_from_chain_head():
    """``GraphOpsService._create_edge_strict`` must document the merged_from chain-head requirement.

    Requires ``merged_from`` and ``derived_from`` in the same paragraph
    so the mid-chain alternative is discoverable alongside the rule
    that rejects mid-chain merged_from. Mirrors the MCP-layer pattern
    in ``test_link_docstring_documents_merged_from_chain_head_precondition``.

    Anti-coincidental-pass: removing the ``derived_from`` alternative
    pointer (or splitting the paragraph between them) fails the check.
    """
    doc = _docstring(GraphOpsService._create_edge_strict)
    proximity_satisfied = any("merged_from" in p and "derived_from" in p for p in _paragraphs(doc))
    assert proximity_satisfied, (
        "GraphOpsService._create_edge_strict docstring must mention ``derived_from`` as "
        "the mid-chain alternative in the same paragraph as the "
        "``merged_from`` chain-head precondition."
    )


def test_graph_ops_link_docstring_documents_synced_from_version_chain_membership():
    """``GraphOpsService._create_edge_strict`` must document the synced_from_version chain rule.

    Requires both ``synced_from_version`` and the word ``chain`` in the
    same paragraph so the membership requirement is bound to the field.

    Anti-coincidental-pass: removing the chain-membership paragraph (or
    losing the word ``chain`` from it) fails the check.
    """
    doc = _docstring(GraphOpsService._create_edge_strict)
    proximity_satisfied = any(
        "synced_from_version" in p and "chain" in p.lower() for p in _paragraphs(doc)
    )
    assert proximity_satisfied, (
        "GraphOpsService._create_edge_strict docstring must document the "
        "``synced_from_version`` chain-membership rule — "
        "the field and the word ``chain`` must appear in the same "
        "paragraph."
    )


def test_graph_ops_link_docstring_documents_tbd_policy_rejection():
    """``GraphOpsService._create_edge_strict`` must name ``TBDPolicyEdgeError``.

    Anti-coincidental-pass: deleting the line naming the error class
    drops the substring entirely.
    """
    doc = _docstring(GraphOpsService._create_edge_strict)
    assert "TBDPolicyEdgeError" in doc, (
        "GraphOpsService._create_edge_strict docstring must name ``TBDPolicyEdgeError`` "
        "as the rejection path for ``authoritative_for`` / "
        "``sync_target`` edge types."
    )


def test_graph_ops_link_docstring_documents_retracts_field_presence_errors():
    """``GraphOpsService._create_edge_strict`` must name ``RetractTargetNotEdgeError``.

    Anti-coincidental-pass: deleting the retract-specific error name
    drops the substring entirely.
    """
    doc = _docstring(GraphOpsService._create_edge_strict)
    assert "RetractTargetNotEdgeError" in doc, (
        "GraphOpsService._create_edge_strict docstring must name "
        "``RetractTargetNotEdgeError`` as the error raised when "
        "``retracted_edge_id`` does not name a known edge."
    )


def test_graph_ops_link_docstring_documents_self_referential_rejection():
    """``GraphOpsService._create_edge_strict`` must name ``SelfReferentialEdgeError``.

    Anti-coincidental-pass: deleting the self-referential rejection
    paragraph drops the substring entirely.
    """
    doc = _docstring(GraphOpsService._create_edge_strict)
    assert "SelfReferentialEdgeError" in doc, (
        "GraphOpsService._create_edge_strict docstring must name "
        "``SelfReferentialEdgeError`` as the rejection path for "
        "``source_id == target_id``."
    )


# ---------------------------------------------------------------------------
# Bulk methods — LIGHT_DEFAULT_THRESHOLD = 5
# ---------------------------------------------------------------------------


def test_graph_ops_bulk_link_docstring_documents_light_default_threshold():
    """``GraphOpsService.create_edges`` must name the literal ``LIGHT_DEFAULT_THRESHOLD = 5``."""
    doc = _docstring(GraphOpsService.create_edges)
    assert "LIGHT_DEFAULT_THRESHOLD = 5" in doc, (
        "GraphOpsService.create_edges docstring must literally show "
        "``LIGHT_DEFAULT_THRESHOLD = 5`` (with the embedded 5) so the "
        "default-resolution threshold is visible to callers."
    )


def test_lifecycle_bulk_set_lifecycle_docstring_documents_light_default_threshold():
    """``LifecycleService.bulk_set_lifecycle`` must name the literal
    ``LIGHT_DEFAULT_THRESHOLD = 5``."""
    doc = _docstring(LifecycleService.bulk_set_lifecycle)
    assert "LIGHT_DEFAULT_THRESHOLD = 5" in doc, (
        "LifecycleService.bulk_set_lifecycle docstring must literally "
        "show ``LIGHT_DEFAULT_THRESHOLD = 5`` so the default-resolution "
        "threshold is visible to callers."
    )


def test_metadata_bulk_update_metadata_docstring_documents_light_default_threshold():
    """``MetadataService.bulk_update_metadata`` must name the literal
    ``LIGHT_DEFAULT_THRESHOLD = 5``."""
    doc = _docstring(MetadataService.bulk_update_metadata)
    assert "LIGHT_DEFAULT_THRESHOLD = 5" in doc, (
        "MetadataService.bulk_update_metadata docstring must literally "
        "show ``LIGHT_DEFAULT_THRESHOLD = 5`` so the default-resolution "
        "threshold is visible to callers."
    )


# ---------------------------------------------------------------------------
# Bulk methods — empty items is valid
# ---------------------------------------------------------------------------


def test_graph_ops_bulk_link_docstring_documents_empty_items_is_valid():
    """``GraphOpsService.create_edges`` must document that empty ``items`` is valid.

    Calibrated to the actual phrasing ``Empty ``items`` is valid``: tests
    on ``empty`` + ``items`` co-location in the same paragraph rather
    than the exact bigram so backtick formatting is not load-bearing.
    """
    doc = _docstring(GraphOpsService.create_edges)
    proximity_satisfied = any(
        "empty" in p.lower() and "items" in p.lower() for p in _paragraphs(doc)
    )
    assert proximity_satisfied, (
        "GraphOpsService.create_edges docstring must document that empty "
        "``items`` is valid — the words ``empty`` and ``items`` must "
        "co-locate in the same paragraph."
    )


def test_lifecycle_bulk_set_lifecycle_docstring_documents_empty_items_is_valid():
    """``LifecycleService.bulk_set_lifecycle`` must document that empty ``items`` is valid."""
    doc = _docstring(LifecycleService.bulk_set_lifecycle)
    proximity_satisfied = any(
        "empty" in p.lower() and "items" in p.lower() for p in _paragraphs(doc)
    )
    assert proximity_satisfied, (
        "LifecycleService.bulk_set_lifecycle docstring must document "
        "that empty ``items`` is valid — the words ``empty`` and "
        "``items`` must co-locate in the same paragraph."
    )


def test_metadata_bulk_update_metadata_docstring_documents_empty_items_is_valid():
    """``MetadataService.bulk_update_metadata`` must document that empty ``items`` is valid."""
    doc = _docstring(MetadataService.bulk_update_metadata)
    proximity_satisfied = any(
        "empty" in p.lower() and "items" in p.lower() for p in _paragraphs(doc)
    )
    assert proximity_satisfied, (
        "MetadataService.bulk_update_metadata docstring must document "
        "that empty ``items`` is valid — the words ``empty`` and "
        "``items`` must co-locate in the same paragraph."
    )


# ---------------------------------------------------------------------------
# Bulk methods — per-item validation cross-reference
# ---------------------------------------------------------------------------


def test_graph_ops_bulk_link_docstring_cross_references_link_for_validation_surface():
    """``GraphOpsService.create_edges`` must cross-reference
    ``GraphOpsService._create_edge_strict``.

    Anti-coincidental-pass: renaming the target (e.g., to
    ``GraphOpsService.link_v2``) drops the literal substring.
    """
    doc = _docstring(GraphOpsService.create_edges)
    assert "GraphOpsService._create_edge_strict" in doc, (
        "GraphOpsService.create_edges docstring must cross-reference "
        "``GraphOpsService._create_edge_strict`` by name as the source of the full "
        "per-item validation surface."
    )


def test_lifecycle_bulk_set_lifecycle_docstring_cross_references_set_lifecycle():
    """``LifecycleService.bulk_set_lifecycle`` must cross-reference
    ``LifecycleService._set_lifecycle``.

    Anti-coincidental-pass: renaming the target drops the literal
    substring.
    """
    doc = _docstring(LifecycleService.bulk_set_lifecycle)
    assert "LifecycleService._set_lifecycle" in doc, (
        "LifecycleService.bulk_set_lifecycle docstring must "
        "cross-reference ``LifecycleService._set_lifecycle`` by name as "
        "the source of the full per-item validation surface."
    )


def test_metadata_bulk_update_metadata_docstring_cross_references_update_metadata():
    """``MetadataService.bulk_update_metadata`` must cross-reference
    ``MetadataService._update_metadata``.

    Anti-coincidental-pass: renaming the target drops the literal
    substring.
    """
    doc = _docstring(MetadataService.bulk_update_metadata)
    assert "MetadataService._update_metadata" in doc, (
        "MetadataService.bulk_update_metadata docstring must "
        "cross-reference ``MetadataService._update_metadata`` by name as "
        "the source of the full per-item validation surface."
    )


# ---------------------------------------------------------------------------
# MetadataService._update_metadata
# ---------------------------------------------------------------------------


def test_metadata_update_metadata_docstring_documents_empty_call_confirmation_flip():
    """``MetadataService._update_metadata`` must document the empty-call confirmation-flip.

    Requires ``confirmation-flip`` (case-insensitive) and
    ``metadata_confirmed`` in the same paragraph so the trigger phrase
    is bound to the field it mutates.

    Anti-coincidental-pass: removing either substring or splitting the
    paragraph between them fails the proximity check.
    """
    doc = _docstring(MetadataService._update_metadata)
    proximity_satisfied = any(
        "confirmation-flip" in p.lower() and "metadata_confirmed" in p for p in _paragraphs(doc)
    )
    assert proximity_satisfied, (
        "MetadataService._update_metadata docstring must document the "
        "empty-call confirmation-flip semantics — "
        "``confirmation-flip`` and ``metadata_confirmed`` must "
        "co-locate in the same paragraph."
    )


# ---------------------------------------------------------------------------
# BatchIngestService.run
# ---------------------------------------------------------------------------


def test_batch_ingest_run_docstring_documents_needs_review_default():
    """``BatchIngestService.run`` must document the ``needs_review`` default policy.

    ``needs_review`` is a caller input that defaults to ``True`` (the
    CAS bulk-ingest confirmation-queue policy, CAS-ADR-021), not a
    substrate constant. The disclosure must bind the flag to its
    governing ADR: ``needs_review`` and ``CAS-ADR-021`` in the same
    paragraph.

    Anti-coincidental-pass: dropping the section heading or the body
    naming the flag breaks the proximity assertion.
    """
    doc = _docstring(BatchIngestService.run)
    proximity_satisfied = any(
        "needs_review" in p and "cas-adr-021" in p.lower() for p in _paragraphs(doc)
    )
    assert proximity_satisfied, (
        "BatchIngestService.run docstring must document the "
        "``needs_review`` confirmation-queue default — ``needs_review`` "
        "and ``CAS-ADR-021`` must co-locate in the same paragraph."
    )


def test_batch_ingest_run_docstring_documents_filename_parsing_always_runs():
    """``BatchIngestService.run`` must document that filename parsing always runs.

    Anti-coincidental-pass: rewording the heading (e.g., "Filename
    parsing runs unconditionally") would fail this exact-phrase check —
    a deliberate stringency since the heading is the structural anchor.
    """
    doc = _docstring(BatchIngestService.run)
    assert "filename parsing always runs" in doc.lower(), (
        "BatchIngestService.run docstring must document ``Filename "
        "parsing always runs`` as a consequence of the hard-coded "
        "``needs_review`` flip."
    )


def test_batch_ingest_run_docstring_documents_per_file_failure_isolation():
    """``BatchIngestService.run`` must document per-file failure isolation."""
    doc = _docstring(BatchIngestService.run)
    assert "per-file failure isolation" in doc.lower(), (
        "BatchIngestService.run docstring must document ``Per-file "
        "failure isolation`` (CAS-ADR-029 atomicity contract)."
    )


def test_batch_ingest_run_docstring_documents_predecessor_auto_transition():
    """``BatchIngestService.run`` must document the predecessor auto-transition.

    This is a removal-guard on the docstring, not evidence about the
    behaviour: every assertion here is satisfied by prose that *mentions*
    the right things, and a docstring carrying all three substrings could
    still misdescribe what ships. The behaviour itself is pinned
    elsewhere -- that the states are table-derived by the supersede tests
    in ``tests/sage/test_batch_inference.py``, and that a refused
    replacement withholds its chain repair by the chain-repair tests in
    ``tests/app/test_batch_ingest_service.py``. Read a failure here as
    "the documented contract drifted from the tested one", never as
    "the contract holds".

    Requiring more than the heading is still worth doing, because the
    bare-heading form of this check previously passed against a docstring
    whose heading had already been renamed: a later paragraph still
    carried a back-reference to the old wording, and one stale phrase
    anywhere in the docstring was enough to keep it green.
    """
    doc = _docstring(BatchIngestService.run).lower()
    assert "predecessor auto-transition" in doc, (
        "BatchIngestService.run docstring must document ``Predecessor "
        "auto-transition`` on Tier-1 supersedes inference."
    )
    assert "lifecycle table" in doc, (
        "The docstring must say the transition's states come from the "
        "vault's lifecycle table, not from fixed state names."
    )
    assert "supersede_target_not_transitionable" in doc, (
        "The docstring must name the outcome for a target whose state "
        "forbids supersede: no edge created, and a warning that says so."
    )


def test_batch_ingest_run_docstring_documents_tier1_provenance_gate_downgrade():
    """``BatchIngestService.run`` must document Tier-1 provenance-gate downgrade.

    Requires ``Tier-1``, ``provenance``, and ``downgrade`` (all
    case-insensitive) in the same paragraph.

    Anti-coincidental-pass: a docstring that mentions ``Tier-1`` and
    ``provenance`` elsewhere but loses the ``downgrade`` paragraph
    fails the proximity check.
    """
    doc = _docstring(BatchIngestService.run)
    proximity_satisfied = any(
        "tier-1" in p.lower() and "provenance" in p.lower() and "downgrade" in p.lower()
        for p in _paragraphs(doc)
    )
    assert proximity_satisfied, (
        "BatchIngestService.run docstring must document the Tier-1 "
        "provenance-gate downgrade — ``Tier-1``, ``provenance``, and "
        "``downgrade`` must co-locate in the same paragraph."
    )


# ---------------------------------------------------------------------------
# GraphOpsService._create_edge — cross-reference to link
# ---------------------------------------------------------------------------


def test_graph_ops_link_idempotent_docstring_cross_references_link_for_precondition_surface():
    """``GraphOpsService._create_edge`` must cross-reference
    ``_create_edge_strict`` for preconditions.

    The bulk-method cross-references bind each bulk operation to its
    single-item validation surface. ``_create_edge`` is the
    symmetric case: it inherits ``_create_edge_strict``'s entire precondition surface
    via the shared ``_create_edge_impl`` body, and the docstring must say so.

    Requires both ``per-item validation surface`` (case-insensitive)
    and ``_create_edge_impl`` in the same paragraph.

    Anti-coincidental-pass: removing the cross-reference paragraph
    drops both substrings; renaming ``_create_edge_impl`` (e.g., during a
    refactor) drops the second; rewriting the paragraph header drops
    the first.
    """
    doc = _docstring(GraphOpsService._create_edge)
    proximity_satisfied = any(
        "per-item validation surface" in p.lower() and "_create_edge_impl" in p
        for p in _paragraphs(doc)
    )
    assert proximity_satisfied, (
        "GraphOpsService._create_edge docstring must carry a "
        "``Per-item validation surface`` paragraph that names "
        "``_create_edge_impl`` as the shared body through which ``_create_edge_strict``'s "
        "preconditions are inherited."
    )
