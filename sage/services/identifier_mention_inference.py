"""Identifier-mention edge inference.

Scans a document's projected body text for vault-config-declared identifier
patterns and writes Tier-1 ``references`` edges to the resolved targets.
Invoked by ``IngestionService`` after Stage 2 (indexing) completes so all
ingest pathways -- bulk ingest and per-document ``ingest_document`` -- honor
the same vault behavior.

Two public entry points:

* ``plan_identifier_mention_edges`` -- resolves identifiers in body text to
  a list of ``PlannedIdentifierMention`` records. Side-effect free. Used
  by the backfill script's dry-run mode and by tests that want to inspect
  the planner output.
* ``infer_identifier_mentions_for_document`` -- runs the planner and
  writes each resulting edge via ``GraphOpsService._create_edge`` so
  re-ingest does not duplicate edges. Manual edges with the same natural
  key are preserved by ``_create_edge``'s noop-on-conflict semantics.
  This is what ``IngestionService._infer_identifier_mention_edges``
  invokes after Stage 2.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from sage.config import pattern_is_discriminating
from sage.models.enums import EdgeType, RationaleKind
from sage.models.schemas import LinkRequest
from sage.services.graph_ops import GraphOpsService
from sage.storage.graph_store import GraphStore

logger = logging.getLogger(__name__)

# Rationale prefix stamped on each auto-inferred edge's rationale text.
# The typed ``rationale_kind`` column (RationaleKind.REFERENCES_MENTION) is
# the authoritative provenance marker; the prefix is retained so legacy
# code paths that inspect rationale text continue to classify correctly.
IDENTIFIER_MENTION_RATIONALE_PREFIX = "[references_mention]"


@dataclass
class PlannedIdentifierMention:
    """One identifier resolved to one target document id."""

    source_doc_id: str
    target_doc_id: str
    identifier: str
    evidence: str


@dataclass
class IdentifierMentionResult:
    """Per-document outcome of one identifier_mention inference pass."""

    edges_created: int = 0
    edges_existing: int = 0
    unresolved: list[str] = field(default_factory=list)
    failures: int = 0


def plan_reference_reconcile(
    existing_edges: object,
    planned_target_ids: object,
) -> tuple[set[str], set[str]]:
    """Reconcile a source's existing ``references`` edges against a freshly
    planned target set.

    Pure decision helper for the repair sweep: the live inference path is
    create-only (re-running it leaves a stale wrong-target edge in place),
    so repairing a mis-targeted graph needs an explicit delete step. Given
    the source's current ``references`` edges and the set of targets the
    fixed config now resolves the source's body to, return
    ``(targets_to_delete, targets_to_create)`` where:

    * ``targets_to_delete`` -- targets carried by an *inferred*
      (``RationaleKind.REFERENCES_MENTION``) edge that the plan no longer
      includes. Hand-curated (``MANUAL``) edges are never deleted, honoring
      the CAS-ADR-019 provenance gate.
    * ``targets_to_create`` -- planned targets not already linked by *any*
      existing edge (manual or inferred), so a target already covered by a
      manual edge is not duplicated.

    ``existing_edges`` is any iterable of objects exposing ``target_id`` and
    ``rationale_kind``; ``planned_target_ids`` is any iterable of target ids.
    """
    planned = set(planned_target_ids)
    inferred_targets = {
        e.target_id for e in existing_edges if e.rationale_kind == RationaleKind.REFERENCES_MENTION
    }
    all_targets = {e.target_id for e in existing_edges}
    targets_to_delete = inferred_targets - planned
    targets_to_create = planned - all_targets
    return targets_to_delete, targets_to_create


def _identifier_mention_rules(edge_inference_config: object) -> list[dict]:
    """Extract identifier-mention pattern dicts from vault config.

    Accepts either the parsed ``edge_inference`` block (dict) or the raw
    None/empty case. Returns a flat list of pattern dicts across every
    ``references`` tier_assignment whose ``inference_rules`` use the
    ``identifier_mention`` method. Empty list when the vault doesn't
    configure the rule.
    """
    if not edge_inference_config:
        return []
    if not isinstance(edge_inference_config, dict):
        return []
    patterns: list[dict] = []
    for assignment in edge_inference_config.get("tier_assignments", []) or []:
        if assignment.get("edge_type") != "references":
            continue
        for rule in assignment.get("inference_rules", []) or []:
            if rule.get("method") != "identifier_mention":
                continue
            for pat in rule.get("patterns", []) or []:
                if pat.get("enabled", True):
                    patterns.append(pat)
    return patterns


def _format_tag(template: str, *, identifier: str) -> str:
    """Substitute identifier-derived placeholders in tag and tier3 templates.

    Supported placeholders:

    * ``{id}`` — the full identifier literal that the regex matched
      (e.g., ``CAS-ADR-042`` or ``F12``).
    * ``{adr_num}`` — the trailing numeric run of the identifier
      (e.g., ``042`` for ``CAS-ADR-042``). Empty when the identifier
      has no trailing digits. Equivalent to ``{id}`` when the identifier
      is purely numeric.

    Templates that lack these placeholders pass through unchanged.
    """
    expanded = template.replace("{id}", identifier)
    if "{adr_num}" in expanded:
        m = re.search(r"(\d+)\s*$", identifier)
        adr_num = m.group(1) if m else ""
        expanded = expanded.replace("{adr_num}", adr_num)
    return expanded


async def _resolve_identifier(
    *,
    identifier: str,
    pattern: dict,
    graph_store: GraphStore,
) -> str | None:
    """Resolve an identifier literal to a vault document id.

    Single-pass catalog query against the graph store with tag, tier3,
    and (optional) doc_type filters. Tag and tier3-value templates may
    contain ``{id}`` or ``{adr_num}`` placeholders substituted from the
    matched identifier via :func:`_format_tag`.

    Among multiple matches, an ``active`` lifecycle status wins; among
    multiple active matches, the most recently updated wins. That tiebreak
    is safe only when the filter discriminates on the identifier (via
    ``target_tier3`` or a placeholder-bearing ``target_tags`` entry) so the
    matches are versions of one logical document. A *non-discriminating*
    pattern -- only ``target_doc_type`` and/or static ``target_tags`` --
    would match every document of the type and the tiebreak would return an
    arbitrary one (e.g., the most-recently-updated ADR for any ``CAS-ADR-NNN``
    mention). Such a pattern cannot resolve a specific identifier, so it is
    refused here rather than allowed to emit a confident-but-wrong edge.
    """
    if not pattern_is_discriminating(pattern):
        logger.warning(
            "identifier_mention: pattern %r is non-discriminating "
            "(no target_tier3 and no placeholder-bearing target_tags); "
            "refusing to resolve %r to avoid an arbitrary match",
            pattern.get("regex"),
            identifier,
        )
        return None
    target_tags = [_format_tag(t, identifier=identifier) for t in pattern.get("target_tags", [])]
    filters: dict[str, object] = {"tags": target_tags} if target_tags else {}
    if pattern.get("target_tier3"):
        filters["tier3_metadata"] = {
            key: (_format_tag(value, identifier=identifier) if isinstance(value, str) else value)
            for key, value in pattern["target_tier3"].items()
        }
    if pattern.get("target_doc_type"):
        filters["doc_type"] = pattern["target_doc_type"]
    # family: identifier-resolution filters (tags, tier3, doc_type) are
    # populated by adapters at ingest time, BEFORE abstraction runs. A
    # pipeline_status=failed target still carries valid identifier-resolution
    # metadata; opting out of BH-020 here keeps the Python active-lifecycle
    # gate below (the correct boundary) as the sole pipeline_status discriminator.
    docs, _ = await graph_store.query_documents(
        filters=filters, limit=50, default_exclude_failed=False
    )
    if not docs:
        return None

    active = [d for d in docs if d.lifecycle_status == "active"]
    pool = active or docs
    pool.sort(key=lambda d: d.updated_at, reverse=True)
    return pool[0].id


async def plan_identifier_mention_edges(
    *,
    source_doc_id: str,
    body_text: str,
    edge_inference_config: object,
    graph_store: GraphStore,
    resolution_cache: dict[str, str | None] | None = None,
) -> list[PlannedIdentifierMention]:
    """Resolve identifier mentions in body text without writing edges.

    Pure planner: returns the list of edges that *would* be written by
    ``infer_identifier_mentions_for_document``. Self-references and
    duplicate target resolutions within a single document are filtered
    out. Unresolved identifiers are logged at INFO level and omitted
    from the result.
    """
    plans: list[PlannedIdentifierMention] = []
    patterns = _identifier_mention_rules(edge_inference_config)
    if not patterns or not body_text:
        return plans
    cache: dict[str, str | None] = resolution_cache if resolution_cache is not None else {}
    seen_targets: set[str] = set()

    for pattern in patterns:
        regex = pattern.get("regex")
        if not regex:
            continue
        try:
            compiled = re.compile(regex)
        except re.error:
            logger.exception("Invalid identifier_mention regex %r; skipping", regex)
            continue
        matches = {m.group(0) for m in compiled.finditer(body_text)}
        for identifier in matches:
            cache_key = f"{regex}::{identifier}"
            if cache_key in cache:
                target_id = cache[cache_key]
            else:
                target_id = await _resolve_identifier(
                    identifier=identifier,
                    pattern=pattern,
                    graph_store=graph_store,
                )
                cache[cache_key] = target_id
            if target_id is None:
                logger.info(
                    "identifier_mention: unresolved %r in %s",
                    identifier,
                    source_doc_id,
                )
                continue
            if target_id == source_doc_id or target_id in seen_targets:
                continue
            seen_targets.add(target_id)
            evidence = f"{IDENTIFIER_MENTION_RATIONALE_PREFIX} {identifier!r} mentioned in body"
            plans.append(
                PlannedIdentifierMention(
                    source_doc_id=source_doc_id,
                    target_doc_id=target_id,
                    identifier=identifier,
                    evidence=evidence,
                )
            )
    return plans


async def infer_identifier_mentions_for_document(
    *,
    source_doc_id: str,
    body_text: str,
    edge_inference_config: object,
    graph_store: GraphStore,
    graph_ops_service: GraphOpsService,
    resolution_cache: dict[str, str | None] | None = None,
) -> IdentifierMentionResult:
    """Plan and write identifier-mention edges in one pass.

    Tier 1, edge_type ``references``, method ``identifier_mention``.
    Anchor fields are populated with the source and target doc ids so the
    ``TRANSITIVE_BOTH`` policy on ``references`` is satisfied at write
    time.

    Edges are written via ``GraphOpsService._create_edge``: a duplicate
    natural-key triple (source, target, references) returns the existing
    edge with ``created=False``, so re-ingest does not produce duplicates
    and pre-existing manual edges are preserved.

    Returns an ``IdentifierMentionResult`` for observability; the caller
    typically does not act on it.
    """
    result = IdentifierMentionResult()
    plans = await plan_identifier_mention_edges(
        source_doc_id=source_doc_id,
        body_text=body_text,
        edge_inference_config=edge_inference_config,
        graph_store=graph_store,
        resolution_cache=resolution_cache,
    )
    # plan_identifier_mention_edges already logs unresolved identifiers;
    # we don't have visibility into which specific identifiers fell out
    # here, so the result's `unresolved` list stays empty in this path.
    # Callers needing the unresolved set should use plan_identifier_mention_edges
    # directly and inspect logs.
    for plan in plans:
        try:
            _edge, created = await graph_ops_service._create_edge(
                LinkRequest(
                    source_id=plan.source_doc_id,
                    target_id=plan.target_doc_id,
                    edge_type=EdgeType.REFERENCES,
                    source_valid_from_version=plan.source_doc_id,
                    target_valid_from_version=plan.target_doc_id,
                    rationale=plan.evidence,
                    rationale_kind=RationaleKind.REFERENCES_MENTION,
                )
            )
            if created:
                result.edges_created += 1
            else:
                result.edges_existing += 1
        except Exception:
            logger.exception(
                "identifier_mention: _create_edge failed for %s -> %s",
                plan.source_doc_id,
                plan.target_doc_id,
            )
            result.failures += 1
    return result
