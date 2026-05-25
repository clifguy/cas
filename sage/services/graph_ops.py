"""Graph operations service: link, check_preconditions, traverse.

Covers behavioral tests BH-021, BH-023, BH-031 through BH-037.
"""

import asyncio
import logging
import sqlite3
import uuid
from datetime import datetime, timezone

from sage.api.errors import (
    DocumentNotFoundError,
    EdgeAnchorPolicyViolationError,
    EdgeNotFoundError,
    MergedFromValidationError,
    PipelineIncompleteError,
    RetractTargetNotEdgeError,
    SAGEError,
    SelfReferentialEdgeError,
    SyncedFromInapplicableEdgeType,
    SyncedFromVersionNotInSourceChain,
    TBDPolicyEdgeError,
)
from sage.config import VaultConfig
from sage.models.edge_registry import EdgeTypeRegistry
from sage.models.enums import (
    LIGHT_DEFAULT_THRESHOLD,
    EdgeType,
    PipelineStatus,
    RationaleKind,
    ResolutionPolicy,
    ResponseMode,
    TraversalDirection,
)
from sage.models.schemas import (
    BulkLinkItemResult,
    BulkLinkRequest,
    BulkLinkResponse,
    ChainEntry,
    ChainRequest,
    ChainResponse,
    DocumentSummary,
    Edge,
    LinkRequest,
    LinkResponse,
    PreconditionCheck,
    PreconditionResult,
    ResolutionPathEntry,
    TraversalNode,
    TraverseRequest,
    TraverseResponse,
    UnlinkResponse,
)
from sage.services._bulk_envelope import sage_error_to_envelope
from sage.services._dry_run import DRY_RUN_SENTINEL_EDGE_ID as _DRY_RUN_SENTINEL_EDGE_ID
from sage.storage.edge_provenance import derive_rationale_kind
from sage.storage.graph_store import GraphStore, LinkReadContext

logger = logging.getLogger(__name__)


class _ResolutionPathRecorder:
    """Per-request collector for CAS-ADR-017 resolution_path debug events.

    Allocated only when `TraverseRequest.debug=True`. Emission sites guard
    with `if recorder is not None`, so the debug-off path has a single
    branch-and-skip and no payload construction.
    """

    def __init__(self) -> None:
        self.entries: list[ResolutionPathEntry] = []

    def anchor_hit(self, edge_id: str, anchor_field: str, anchor_version: str) -> None:
        self.entries.append(
            ResolutionPathEntry(
                event_type="anchor_hit",
                edge_id=edge_id,
                anchor_field=anchor_field,
                anchor_version=anchor_version,
            )
        )

    def anchor_miss(self, edge_id: str, anchor_field: str, anchor_version: str | None) -> None:
        self.entries.append(
            ResolutionPathEntry(
                event_type="anchor_miss",
                edge_id=edge_id,
                anchor_field=anchor_field,
                anchor_version=anchor_version,
            )
        )

    def retracts_applied(self, edge_id: str, retracting_edge_id: str) -> None:
        self.entries.append(
            ResolutionPathEntry(
                event_type="retracts_applied",
                edge_id=edge_id,
                retracted_edge_id=retracting_edge_id,
            )
        )

    def tombstone_applied(self, edge_id: str, tombstone_version: str) -> None:
        self.entries.append(
            ResolutionPathEntry(
                event_type="tombstone_applied",
                edge_id=edge_id,
                tombstone_version=tombstone_version,
            )
        )


class _LineageCache:
    """Per-request cache of supersedes-lineage lookups (CAS-ADR-017, Chunk 4).

    Encapsulated so the ADR-deferred process-level cache can slot in later.
    Tracks hit/miss counts for test instrumentation (CR-022).
    """

    def __init__(self, graph_store: GraphStore) -> None:
        self._store = graph_store
        self._cache: dict[str, frozenset[str]] = {}
        self.fetches = 0

    async def get(self, doc_id: str) -> frozenset[str]:
        if doc_id not in self._cache:
            lineage = await self._store.get_supersedes_lineage(doc_id)
            self._cache[doc_id] = frozenset(lineage)
            self.fetches += 1
        return self._cache[doc_id]


# Lifecycle states that satisfy depends_on preconditions (BH-033, BH-034).
# Domain-specific states like 'filed' are excluded (BH-036).
_SATISFYING_STATUSES = frozenset({"active", "completed"})


class GraphOpsService:
    def __init__(
        self,
        graph_store: GraphStore,
        config: VaultConfig,
        edge_type_registry: EdgeTypeRegistry | None = None,
    ) -> None:
        self._store = graph_store
        self._config = config
        self._edge_type_registry = edge_type_registry or EdgeTypeRegistry.default()
        # Serializes link() across concurrent callers. Rationale: SQLite
        # writes serialize at the DB layer anyway, and per-call fan-out
        # into the graph-store executor compounded under parallel load,
        # filling the pool with orphaned work when MCP clients cancelled.
        # Queueing at the asyncio layer is cheap and bounds the pool's
        # backlog to one in-flight link's worth of submissions.
        self._link_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Link (BH-031, BH-032, CAS-ADR-017)
    # ------------------------------------------------------------------

    async def link(self, request: LinkRequest) -> LinkResponse:
        """Create a typed edge between two documents.

        Applies the CAS-ADR-017 write-time invariant: the effective
        resolution_policy (from the edge-type registry) is frozen onto
        the row, the anchor / retracted_edge_id field shape must match
        the policy, and any chain-scoped anchor must sit in the
        supersedes lineage of its endpoint document.

        Document existence:
        Both ``request.source_id`` and (when set) ``request.target_id``
        must reference documents that currently exist in the vault.
        Missing endpoints raise ``DocumentNotFoundError``.

        Self-referential edges forbidden:
        ``source_id == target_id`` is rejected with
        ``SelfReferentialEdgeError`` for every edge type — no edge_type
        permits a node to point at itself.

        ``merged_from`` chain-head requirement:
        Both endpoints must be chain heads: neither ``source_id`` nor
        ``target_id`` may have an outbound ``supersedes`` edge.
        Mid-chain on either side raises ``MergedFromValidationError``.
        When the source is mid-chain and content-reuse is what's
        actually wanted, use ``derived_from`` instead — its anchor
        field ``source_valid_from_version`` captures the
        chain-visibility semantics that ``merged_from`` lacks.

        ``retracts`` field-presence rules:
        A ``retracts`` edge requires ``source_valid_from_version`` (the
        anchor in the retracting chain), forbids
        ``target_valid_from_version`` (must be null), and requires
        ``retracted_edge_id`` to reference an existing edge in the same
        vault. Violations surface as ``EdgeAnchorPolicyViolationError``
        (anchor required/forbidden) or ``RetractTargetNotEdgeError``
        (``retracted_edge_id`` does not name a known edge).

        ``synced_from_*`` field applicability (closed list):
        ``synced_from_version`` and ``synced_from_content_hash`` are
        accepted only on ``edge_type="derived_from"`` and
        ``edge_type="sync_target"``. Any other edge_type with either
        field set raises ``SyncedFromInapplicableEdgeType``. The fields
        are not silently ignored on inapplicable types — they are a
        structural error.

        ``synced_from_version`` chain-membership (T-0111):
        When set, ``synced_from_version`` must be a member of the
        target document's ``supersedes`` chain (the target itself or
        any predecessor reachable by walking outbound ``supersedes``
        edges). Out-of-chain values raise
        ``SyncedFromVersionNotInSourceChain``. The check runs only
        when ``synced_from_version`` is non-null and the edge_type
        permits the field per the closed list above.

        TBD-policy edge types (CAS-ADR-017):
        Two values appear in the ``EdgeType`` enum but are
        reserved-and-not-implemented: ``authoritative_for`` and
        ``sync_target``. Both have ``resolution_policy=TBD`` in the
        edge registry; every ``link`` call carrying either type raises
        ``TBDPolicyEdgeError`` unconditionally. The ``synced_from_*``
        closed list above lists ``sync_target`` as a legitimate carrier
        of those fields for forward compatibility; this does not
        unblock ``sync_target`` link creation today.

        Raises ``sqlite3.IntegrityError`` if an edge with the same
        natural-key triple (source_id, target_id, edge_type) already
        exists (T-0079 unique constraint). For idempotent semantics
        (no-op on duplicate, return existing edge), use
        ``link_idempotent``.

        T-0152: ``request.dry_run`` makes the call a preview — same
        validators run, but no edge is inserted. The response carries
        the would-be edge with the nil-UUID sentinel id and
        ``dry_run=True``.
        """
        return await self._link_impl(request, on_conflict="raise")

    async def link_idempotent(self, request: LinkRequest) -> tuple[Edge, bool]:
        """Idempotent variant of ``link``. Returns ``(edge, created)``.

        Under T-0079, the edges table carries a UNIQUE constraint on
        ``(source_id, target_id, edge_type)``. ``link_idempotent`` swallows
        the duplicate-key error and returns the pre-existing edge with
        ``created=False``. The caller's rationale and notes are discarded
        on a no-op; existing provenance is preserved (per the SAGE
        single-source-of-truth principle: the first rationale is canonical).

        Used by ``batch_inference.resolve_and_execute`` and
        ``identifier_mention_inference`` (which rely on idempotency for
        re-ingest of auto-inferred edges) and by the ``sage_link`` MCP
        tool (which wraps the tuple in a ``LinkResponse`` to surface
        ``existing_rationale`` and the ``dry_run`` echo).

        T-0152: when ``request.dry_run`` is True, the T-0079 natural-key
        pre-check is performed in the application layer so the dry-run
        path surfaces the no-op outcome without ever touching storage.
        The would-be edge on the create path carries the nil-UUID
        sentinel id.
        """
        response = await self._link_impl(request, on_conflict="noop")
        return response.edge, response.created

    async def bulk_link(self, request: BulkLinkRequest) -> BulkLinkResponse:
        """Apply one edge-creation request per item (T-0165).

        Each item is dispatched through ``link_idempotent`` so the
        per-item natural-key idempotency contract (T-0079) is preserved:
        a duplicate request returns the existing edge with
        ``created=False`` and ``existing_rationale`` populated, rather
        than raising. The batch is NOT atomic (CAS-ADR-029): a SAGEError
        raised by one item is wrapped into the per-item error envelope
        and does not roll back earlier-or-later successful items.
        Anything outside the SAGEError hierarchy is treated as a
        programmer or infrastructure bug and propagates out of the batch.

        Per-item validation surface:
        Each item inherits the full ``link`` precondition surface —
        document existence, ``merged_from`` chain-head requirement,
        ``retracts`` field-presence rules, ``synced_from_*``
        applicability and chain-membership, TBD-policy unconditional
        rejection, and the CAS-ADR-017 anchor policy per edge_type.
        See ``GraphOpsService.link`` for the full enumeration.

        Empty ``items`` is valid: the response carries an empty
        ``results`` array and all counts are zero. Callers building
        bulk operations programmatically may pass ``items=[]`` without
        special-casing the call site.

        The performance win versus N sequential ``sage_link`` MCP calls
        comes from eliminating per-call MCP framing overhead and the
        asyncio scheduling between items; the process-wide ``_link_lock``
        and the per-item SQLite transaction are unchanged.

        ``request.response_mode`` (T-0153) controls per-item payload
        depth. ``light`` drops the per-item ``edge`` body from success
        entries; failure entries always carry the full structured error
        envelope. The ``created`` and ``existing_rationale`` fields are
        preserved under light because they are the only signals callers
        have for the natural-key idempotency outcome. When unset, the
        default-resolution rule mirrors the sibling bulk tools: batches
        with more than ``LIGHT_DEFAULT_THRESHOLD = 5`` items default to
        ``light``, smaller batches default to ``full``.
        """
        # T-0153: resolve the effective response_mode by batch size, the
        # same rule the sibling bulk mutation tools use. Batches that
        # cross the threshold default to light so the response stays
        # inside the MCP inline-output budget; smaller batches keep the
        # full edge body for human-readable debugging.
        effective_mode = request.response_mode
        if effective_mode is None:
            effective_mode = (
                ResponseMode.LIGHT
                if len(request.items) > LIGHT_DEFAULT_THRESHOLD
                else ResponseMode.FULL
            )

        results: list[BulkLinkItemResult] = []
        for item in request.items:
            per_item_request = LinkRequest(
                source_id=item.source_id,
                target_id=item.target_id,
                edge_type=item.edge_type,
                source_valid_from_version=item.source_valid_from_version,
                target_valid_from_version=item.target_valid_from_version,
                retracted_edge_id=item.retracted_edge_id,
                notes=item.notes,
                rationale=item.rationale,
                rationale_kind=item.rationale_kind,
                synced_from_version=item.synced_from_version,
                synced_from_content_hash=item.synced_from_content_hash,
                # T-0152: propagate envelope dry_run to each per-item
                # call. Per-item override is not supported (CAS-ADR-029).
                dry_run=request.dry_run,
            )
            try:
                edge, created = await self.link_idempotent(per_item_request)
                results.append(
                    BulkLinkItemResult(
                        source_id=item.source_id,
                        target_id=item.target_id,
                        edge_type=item.edge_type,
                        status="success",
                        # T-0153 light mode: drop the edge body. Caller
                        # still has source_id/target_id/edge_type echoed,
                        # plus the created flag and existing_rationale,
                        # which are the only natural-key idempotency
                        # signals.
                        edge=(edge if effective_mode == ResponseMode.FULL else None),
                        created=created,
                        existing_rationale=(edge.rationale if not created else None),
                    )
                )
            except SAGEError as exc:
                results.append(
                    BulkLinkItemResult(
                        source_id=item.source_id,
                        target_id=item.target_id,
                        edge_type=item.edge_type,
                        status="error",
                        error=sage_error_to_envelope(exc),
                    )
                )
        success_count = sum(1 for r in results if r.status == "success")
        return BulkLinkResponse(
            results=results,
            success_count=success_count,
            error_count=len(results) - success_count,
            total=len(results),
            # T-0152: envelope echo so callers can confirm the batch ran
            # as a preview even when every per-item edge was dropped
            # under light response_mode.
            dry_run=request.dry_run,
        )

    async def _link_impl(self, request: LinkRequest, *, on_conflict: str) -> LinkResponse:
        """Shared implementation used by ``link`` and ``link_idempotent``.

        ``on_conflict="raise"`` lets the storage-layer IntegrityError
        propagate. ``on_conflict="noop"`` translates it into a return of
        the pre-existing edge with ``created=False``.

        T-0152: honors ``request.dry_run``. When True, runs every
        validator and the T-0079 natural-key pre-check, then returns
        either a ``LinkResponse`` for the no-op path (if an edge
        already exists) or for the would-be-create path (with the
        nil-UUID sentinel id). No persistence.
        """
        policy = self._edge_type_registry.policy_for(request.edge_type)

        async with self._link_lock:
            ctx = await self._store.read_link_context(request, policy)

            # Error order preserves pre-batching behavior: document
            # existence → self-ref → target existence → TBD → shape →
            # retracted-edge existence → merged_from chain positions →
            # anchor lineage.
            if not ctx.source_exists:
                raise DocumentNotFoundError(request.source_id)

            if request.edge_type != EdgeType.RETRACTS:
                if request.target_id is not None and request.source_id == request.target_id:
                    raise SelfReferentialEdgeError(request.source_id)
                if request.target_id is not None and not ctx.target_exists:
                    raise DocumentNotFoundError(request.target_id)

            if policy == ResolutionPolicy.TBD:
                raise TBDPolicyEdgeError(request.edge_type.value)

            self._validate_link_request_shape(request, policy)

            if request.edge_type == EdgeType.RETRACTS:
                if ctx.retracted_edge is None:
                    raise RetractTargetNotEdgeError(request.retracted_edge_id)

            if request.edge_type == EdgeType.MERGED_FROM:
                if ctx.has_sup_predecessor:
                    raise MergedFromValidationError(
                        "source is not the first version of its chain (has an "
                        "outbound supersedes edge)",
                        source_id=request.source_id,
                        target_id=request.target_id,
                    )
                if ctx.has_sup_successor:
                    raise MergedFromValidationError(
                        "target is not a chain head (has a newer superseding version)",
                        source_id=request.source_id,
                        target_id=request.target_id,
                    )

            self._validate_anchors_from_context(request, policy, ctx)

            # T-0111: when synced_from_version is set on a sync_target /
            # derived_from edge, verify the recorded id is a member of
            # the target_id's supersedes chain. Catches both "wrong
            # document" (id exists but not in this chain) and "dangling
            # ref" (id does not exist anywhere) in one membership
            # predicate. Runs under link_lock so the chain read serializes
            # with the edge insert; without this, a concurrent supersede
            # could orphan the recorded provenance between read and
            # write. TODO: future optimization — replace the full chain
            # walk with a chain_contains(target_id, candidate) helper
            # that early-exits when the candidate is found, dropping the
            # per-link cost from O(chain) to O(path to candidate).
            if (
                request.synced_from_version is not None
                and request.target_id is not None
                and request.edge_type in (EdgeType.SYNC_TARGET, EdgeType.DERIVED_FROM)
            ):
                chain_response = await self.chain(
                    ChainRequest(
                        document_id=request.target_id,
                        edge_type=EdgeType.SUPERSEDES,
                    )
                )
                chain_member_ids = {entry.id for entry in chain_response.chain}
                if request.synced_from_version not in chain_member_ids:
                    raise SyncedFromVersionNotInSourceChain(
                        target_id=request.target_id,
                        synced_from_version=request.synced_from_version,
                    )

            # T-0152: T-0079 natural-key pre-check — DRY-RUN ONLY. The
            # storage uniqueness constraint never fires on dry-run
            # (no insert happens), so without this pre-check a dry-run
            # would silently report ``created=True`` for what would
            # actually be a real-run no-op. Real-run does not need
            # this pre-check: the IntegrityError path below already
            # handles the dup case correctly (and adding the read
            # here on every real-run link broke the T-0079 executor-
            # submission bound). Note: ``target_id`` may be null on
            # retracts edges; the natural-key triple is meaningless
            # for those (T-0079 constraint allows null target_id), so
            # skip even in dry-run.
            if request.dry_run and request.target_id is not None:
                existing = await self._store.find_edge_by_natural_key(
                    request.source_id,
                    request.target_id,
                    request.edge_type.value,
                )
                if existing is not None:
                    if on_conflict == "noop":
                        return LinkResponse(
                            edge=existing,
                            created=False,
                            existing_rationale=existing.rationale,
                            dry_run=True,
                        )
                    # on_conflict="raise" path: real-run would hit
                    # IntegrityError on insert; dry-run preserves
                    # that contract by raising synchronously here so
                    # callers see the same shape of outcome.
                    raise sqlite3.IntegrityError(
                        "UNIQUE constraint would fail: edges natural key "
                        f"({request.source_id}, {request.target_id}, "
                        f"{request.edge_type.value})"
                    )

            # T-0080: prefer the caller-supplied rationale_kind; otherwise
            # derive from the rationale-text prefix and fall back to MANUAL.
            rationale_kind = request.rationale_kind or derive_rationale_kind(request.rationale)
            # T-0152: mint the sentinel id on dry-run so callers can
            # never mistake the preview edge for a persisted one.
            edge_id = _DRY_RUN_SENTINEL_EDGE_ID if request.dry_run else str(uuid.uuid4())
            edge = Edge(
                id=edge_id,
                source_id=request.source_id,
                target_id=request.target_id,
                edge_type=request.edge_type,
                resolution_policy=policy,
                source_valid_from_version=request.source_valid_from_version,
                target_valid_from_version=request.target_valid_from_version,
                valid_until_version=None,
                retracted_edge_id=request.retracted_edge_id,
                created_at=datetime.now(timezone.utc),
                notes=request.notes,
                rationale=request.rationale,
                rationale_kind=rationale_kind,
                synced_from_version=request.synced_from_version,
                synced_from_content_hash=request.synced_from_content_hash,
            )

            # T-0152: dry-run returns the would-be edge without writing.
            # All validators above have already run in the same order
            # as a real run.
            if request.dry_run:
                return LinkResponse(
                    edge=edge,
                    created=True,
                    existing_rationale=None,
                    dry_run=True,
                )

            if request.edge_type == EdgeType.MERGED_FROM:
                # merge_atomic is transaction-critical (couples the
                # merged_from insert with tombstone updates). The
                # T-0079 storage layer does NOT noop inside the
                # atomic op; the duplicate-key path rolls back the
                # whole transaction and we recover here by looking
                # up the existing merged_from edge to honor the
                # idempotency contract.
                try:
                    await self._store.merge_atomic(
                        edge,
                        list(ctx.tombstone_candidates),
                        request.target_id,
                    )
                except sqlite3.IntegrityError:
                    if on_conflict == "noop":
                        existing = await self._store.find_edge_by_natural_key(
                            request.source_id,
                            request.target_id,
                            request.edge_type.value,
                        )
                        if existing is not None:
                            return LinkResponse(
                                edge=existing,
                                created=False,
                                existing_rationale=existing.rationale,
                                dry_run=False,
                            )
                    raise
                return LinkResponse(
                    edge=edge,
                    created=True,
                    existing_rationale=None,
                    dry_run=False,
                )

            stored_edge, created = await self._store.insert_edge(edge, on_conflict=on_conflict)
            return LinkResponse(
                edge=stored_edge,
                created=created,
                existing_rationale=stored_edge.rationale if not created else None,
                dry_run=False,
            )

    def _validate_link_request_shape(self, request: LinkRequest, policy: ResolutionPolicy) -> None:
        """Enforce the policy-keyed field-shape invariant.

        Does not verify anchor-in-lineage membership; that check lands
        in Chunk 4 alongside the lineage accessor.
        """
        edge_type = request.edge_type
        offending: list[str] = []

        # T-0111: synced_from_* fields are only meaningful on sync_target
        # (Tier 1) and derived_from (Tier 3). Reject any other edge type
        # carrying these fields; this is a pure-field gate independent
        # of policy, so it fires before any policy branch.
        if edge_type not in (EdgeType.SYNC_TARGET, EdgeType.DERIVED_FROM):
            fields_set: list[str] = []
            if request.synced_from_version is not None:
                fields_set.append("synced_from_version")
            if request.synced_from_content_hash is not None:
                fields_set.append("synced_from_content_hash")
            if fields_set:
                raise SyncedFromInapplicableEdgeType(edge_type.value, fields_set)

        # retracts: target_id null, retracted_edge_id required,
        # source-side anchor required, target-side anchor null, no until.
        if edge_type == EdgeType.RETRACTS:
            if request.target_id is not None:
                offending.append("target_id")
            if request.retracted_edge_id is None:
                raise EdgeAnchorPolicyViolationError(
                    edge_type.value,
                    policy.value,
                    "retracts edges require retracted_edge_id",
                    ["retracted_edge_id"],
                )
            if request.source_valid_from_version is None:
                raise EdgeAnchorPolicyViolationError(
                    edge_type.value,
                    policy.value,
                    "retracts edges require source_valid_from_version",
                    ["source_valid_from_version"],
                )
            if request.target_valid_from_version is not None:
                offending.append("target_valid_from_version")
            if offending:
                raise EdgeAnchorPolicyViolationError(
                    edge_type.value,
                    policy.value,
                    f"fields must be null on retracts edges: {', '.join(offending)}",
                    offending,
                )
            return

        # Non-retracts edges: target_id required; retracted_edge_id must be null.
        if request.target_id is None:
            raise EdgeAnchorPolicyViolationError(
                edge_type.value,
                policy.value,
                "target_id is required for this edge type",
                ["target_id"],
            )
        if request.retracted_edge_id is not None:
            raise EdgeAnchorPolicyViolationError(
                edge_type.value,
                policy.value,
                "retracted_edge_id may only be set on retracts edges",
                ["retracted_edge_id"],
            )

        if policy == ResolutionPolicy.NONE:
            # supersedes, merged_from: both anchor fields must be null.
            if request.source_valid_from_version is not None:
                offending.append("source_valid_from_version")
            if request.target_valid_from_version is not None:
                offending.append("target_valid_from_version")
            if offending:
                raise EdgeAnchorPolicyViolationError(
                    edge_type.value,
                    policy.value,
                    f"anchor fields must be null for policy 'none': {', '.join(offending)}",
                    offending,
                )
            return

        if policy == ResolutionPolicy.TRANSITIVE_SOURCE:
            if request.source_valid_from_version is None:
                raise EdgeAnchorPolicyViolationError(
                    edge_type.value,
                    policy.value,
                    "source_valid_from_version is required for policy 'transitive_source'",
                    ["source_valid_from_version"],
                )
            if request.target_valid_from_version is not None:
                raise EdgeAnchorPolicyViolationError(
                    edge_type.value,
                    policy.value,
                    (
                        "target_valid_from_version must be null for policy "
                        "'transitive_source' (target anchor is frozen at derivation)"
                    ),
                    ["target_valid_from_version"],
                )
            return

        if policy == ResolutionPolicy.TRANSITIVE_TARGET:
            if request.target_valid_from_version is None:
                raise EdgeAnchorPolicyViolationError(
                    edge_type.value,
                    policy.value,
                    "target_valid_from_version is required for policy 'transitive_target'",
                    ["target_valid_from_version"],
                )
            if request.source_valid_from_version is not None:
                raise EdgeAnchorPolicyViolationError(
                    edge_type.value,
                    policy.value,
                    (
                        "source_valid_from_version must be null for policy "
                        "'transitive_target' (source anchor is frozen at derivation)"
                    ),
                    ["source_valid_from_version"],
                )
            return

        if policy == ResolutionPolicy.TRANSITIVE_BOTH:
            if request.source_valid_from_version is None:
                offending.append("source_valid_from_version")
            if request.target_valid_from_version is None:
                offending.append("target_valid_from_version")
            if offending:
                raise EdgeAnchorPolicyViolationError(
                    edge_type.value,
                    policy.value,
                    (
                        "both source_valid_from_version and target_valid_from_version "
                        "are required for policy 'transitive_both'"
                    ),
                    offending,
                )
            return

    def _validate_anchors_from_context(
        self,
        request: LinkRequest,
        policy: ResolutionPolicy,
        ctx: LinkReadContext,
    ) -> None:
        """Verify chain-scoped anchor(s) sit in the pre-fetched lineages.

        Pure Python — all DB state was gathered by `read_link_context`.
        No-op for policy `none` except for `retracts`, which carries a
        source-side anchor that must be in the retracting chain's lineage.
        """
        if policy == ResolutionPolicy.NONE:
            if (
                request.edge_type == EdgeType.RETRACTS
                and request.source_valid_from_version is not None
            ):
                self._check_anchor_in_lineage(
                    request.edge_type.value,
                    policy.value,
                    "source_valid_from_version",
                    request.source_valid_from_version,
                    request.source_id,
                    ctx.source_anchor_exists,
                    ctx.source_lineage,
                )
            return

        if request.source_valid_from_version is not None:
            self._check_anchor_in_lineage(
                request.edge_type.value,
                policy.value,
                "source_valid_from_version",
                request.source_valid_from_version,
                request.source_id,
                ctx.source_anchor_exists,
                ctx.source_lineage,
            )

        if (
            policy
            in (
                ResolutionPolicy.TRANSITIVE_TARGET,
                ResolutionPolicy.TRANSITIVE_BOTH,
            )
            and request.target_valid_from_version is not None
            and request.target_id is not None
        ):
            self._check_anchor_in_lineage(
                request.edge_type.value,
                policy.value,
                "target_valid_from_version",
                request.target_valid_from_version,
                request.target_id,
                ctx.target_anchor_exists,
                ctx.target_lineage,
            )

    @staticmethod
    def _check_anchor_in_lineage(
        edge_type: str,
        policy: str,
        field: str,
        anchor_id: str,
        endpoint_id: str,
        anchor_exists: bool,
        lineage: frozenset[str],
    ) -> None:
        if not anchor_exists:
            raise EdgeAnchorPolicyViolationError(
                edge_type,
                policy,
                f"{field}={anchor_id!r} does not reference a known document",
                [field],
            )
        if anchor_id not in lineage:
            raise EdgeAnchorPolicyViolationError(
                edge_type,
                policy,
                (f"{field}={anchor_id!r} is not in the supersedes lineage of {endpoint_id!r}"),
                [field],
            )

    # ------------------------------------------------------------------
    # Unlink (delete production edge)
    # ------------------------------------------------------------------

    async def unlink(self, edge_id: str, dry_run: bool = False) -> UnlinkResponse:
        """Delete a production edge by ID (or preview the deletion on dry-run).

        T-0152: when ``dry_run`` is True, runs the same edge-existence
        validator (raising EdgeNotFoundError when absent) but skips
        ``delete_edge``. The response carries ``deleted=False``,
        ``dry_run=True``, and ``preview_edge`` set to the edge that
        would have been deleted.
        """
        edge = await self._store.get_edge(edge_id)
        if edge is None:
            raise EdgeNotFoundError(edge_id)
        if dry_run:
            return UnlinkResponse(
                deleted=False,
                edge_id=edge_id,
                dry_run=True,
                preview_edge=edge,
            )
        await self._store.delete_edge(edge_id)
        return UnlinkResponse(
            deleted=True,
            edge_id=edge_id,
            dry_run=False,
            preview_edge=None,
        )

    # ------------------------------------------------------------------
    # Check preconditions (BH-023, BH-033 through BH-036)
    # ------------------------------------------------------------------

    async def check_preconditions(self, function_id: str) -> PreconditionResult:
        """Validate all depends_on targets for a function document."""
        function_doc = await self._store.get_document(function_id)
        if function_doc is None:
            raise DocumentNotFoundError(function_id)

        depends_on_edges = await self._store.get_edges_by_source(
            function_id, EdgeType.DEPENDS_ON.value
        )

        checks: list[PreconditionCheck] = []
        for edge in depends_on_edges:
            target = await self._store.get_document(edge.target_id)
            if target is None:
                checks.append(
                    PreconditionCheck(
                        target_id=edge.target_id,
                        required="active or completed",
                        actual="not found",
                        satisfied=False,
                    )
                )
                continue

            # Pipeline failure overrides lifecycle check (BH-023)
            if target.pipeline_status == PipelineStatus.FAILED:
                checks.append(
                    PreconditionCheck(
                        target_id=edge.target_id,
                        required="active or completed",
                        actual="failed (pipeline_incomplete)",
                        satisfied=False,
                    )
                )
                continue

            satisfied = target.lifecycle_status in _SATISFYING_STATUSES
            checks.append(
                PreconditionCheck(
                    target_id=edge.target_id,
                    required="active or completed",
                    actual=target.lifecycle_status,
                    satisfied=satisfied,
                )
            )

        return PreconditionResult(
            function_id=function_id,
            satisfied=all(c.satisfied for c in checks),
            checks=checks,
        )

    # ------------------------------------------------------------------
    # Traverse (BH-037)
    # ------------------------------------------------------------------

    async def traverse(self, request: TraverseRequest) -> TraverseResponse:
        """Graph walk with deduplication by document.

        Applies CAS-ADR-017 chain-resolution semantics: for edge types
        whose `resolution_policy` is chain-scoped (`transitive_source`,
        `transitive_both`), the seed set expands to include the start
        document's supersedes lineage on chain-scoped sides, and each
        candidate edge is filtered by its anchor's membership in the
        appropriate lineage. Policy `none` and `retracts`/`merged_from`
        resolution behavior land in Chunks 5 and 6.
        """
        start_doc = await self._store.get_document(request.start_id)
        if start_doc is None:
            raise DocumentNotFoundError(request.start_id)

        cache = _LineageCache(self._store)
        recorder = _ResolutionPathRecorder() if request.debug else None

        raw = await self._collect_raw_with_seeds(request, cache)

        # Per-edge anchor filter (honors stored resolution_policy per CAS-ADR-017).
        filtered: list[dict] = []
        for row in raw:
            if await self._edge_passes_anchor_filter(row, cache, recorder):
                filtered.append(row)

        # CAS-ADR-017 Chunk 5: retracts short-circuit. Only chain-resolved
        # edges (transitive_source, transitive_both) are suppressible; the
        # retracts primitive does not veto policy=none edges indiscriminately.
        filtered = await self._apply_retracts(filtered, request.start_id, cache, recorder)

        # CAS-ADR-017 Chunk 6: tombstone suppression. Edges whose
        # `valid_until_version` sits strictly as an ancestor of the query
        # start_id are dropped. Equal-to-start is kept (CR-034: historical
        # query at the merge point still surfaces the edge).
        filtered = await self._apply_tombstones(filtered, request.start_id, cache, recorder)

        # T-0079: collapse multi-path traversal hits by doc_id. With the
        # UNIQUE (source_id, target_id, edge_type) constraint enforced
        # at the storage layer, there is at most one edge per
        # natural-key triple, so the historical "pick most recent edge"
        # storage-dedup step is vacuous. What remains is the multi-path
        # collapse: the SQL CTE may surface the same target doc reached
        # via different paths at different depths, and we still need
        # one node per target with min(depth) plus distinct-edge counts
        # per edge_type.
        grouped: dict[str, list[dict]] = {}
        for row in filtered:
            grouped.setdefault(row["doc_id"], []).append(row)

        nodes: list[TraversalNode] = []
        for doc_id, rows in grouped.items():
            # Any row works for the document/edge fields; rows that
            # share a doc_id with multiple distinct edge_types each
            # surface different edges, but the existing TraversalNode
            # shape carries one representative edge plus the edge_counts
            # map. Pick the first row deterministically; the counts
            # below capture the full multiplicity.
            representative = rows[0]
            min_depth = min(r["depth"] for r in rows)

            # T-0118: route CTE-row -> DocumentSummary projection through
            # the single owning factory ``DocumentSummary.from_traversal_row``
            # per the *CAS Projection-Point Audit Conventions* steering
            # document. Field additions to DocumentSummary are now structurally
            # guarded by ``test_from_traversal_row_populates_every_document_summary_field``
            # in tests/sage/test_graph_ops.py; what used to be a hand-written
            # field-by-field assignment block (vulnerable to F4 drift relative
            # to ``DocumentSummary.from_document``) is collapsed to a single
            # factory call. The representative row carries the canonical
            # ``doc_id`` plus the ``d_*``-prefixed document columns the
            # traversal CTE supplies (sage/storage/graph_store.py).
            doc_summary = DocumentSummary.from_traversal_row(representative)

            # Read every storage-layer edge field that ``_traverse_sync``
            # carries through its row dict; without this, the CAS-ADR-017
            # chain-resolution fields (resolution_policy + anchors) and
            # the tombstone field (valid_until_version) silently
            # default to None on traversal-returned edges regardless of
            # what is stored. The rationale_kind drop was a documented
            # T-0080 regression; the four parallel fields had the same
            # defect, fixed here in the same pass.
            # Read every storage-layer edge field that ``_traverse_sync``
            # carries through its row dict; without this, the CAS-ADR-017
            # chain-resolution fields (resolution_policy + anchors) and
            # the tombstone field (valid_until_version) silently
            # default to None on traversal-returned edges regardless of
            # what is stored. The rationale_kind drop was a documented
            # T-0080 regression; the four parallel fields had the same
            # defect, fixed here in the same pass.
            #
            # BH-101 (T-0124): excluded projection point.
            #
            # This inline ``Edge`` construction deliberately bypasses the
            # canonical factory ``GraphStore._row_to_edge``
            # (sage/storage/graph_store.py) because the traversal hot path
            # cannot absorb a per-row ``Edge.model_validate`` allocation:
            # ``_traverse_sync`` returns one row per (multi-path, depth)
            # tuple, and a top-K traversal at depth>=3 can produce
            # thousands of rows per query. Routing each through the
            # canonical factory would re-run Pydantic validation on every
            # row -- measured prohibitive on benchmark BH-101 -- whereas
            # the storage layer has already validated the same fields on
            # insert. The inline assembly skips revalidation while still
            # building a structurally-identical ``Edge``.
            #
            # The exclusion is preserved but guarded per the *CAS
            # Projection-Point Audit Conventions* steering document
            # (cas vault, doc_type=steering_document, "Source-shape
            # exclusions"). The structural guard against field-addition
            # drift between this inline path and ``_row_to_edge`` is the
            # parity test ``test_edge_cte_row_parity_with_row_to_edge``
            # in ``tests/sage/test_graph_ops.py`` (T-0124): when a field
            # is added to ``Edge`` and wired through one path but not the
            # other, the parity test trips. Any modification to this
            # construction must keep the two paths field-equivalent or
            # update the parity test in the same change.
            resolution_policy_raw = representative.get("resolution_policy")
            edge = Edge(
                id=representative["edge_id"],
                source_id=representative["source_id"],
                target_id=representative["target_id"],
                edge_type=EdgeType(representative["edge_type"]),
                resolution_policy=(
                    ResolutionPolicy(resolution_policy_raw)
                    if resolution_policy_raw is not None
                    else None
                ),
                source_valid_from_version=representative.get("source_valid_from_version"),
                target_valid_from_version=representative.get("target_valid_from_version"),
                valid_until_version=representative.get("valid_until_version"),
                retracted_edge_id=representative.get("retracted_edge_id"),
                created_at=datetime.fromisoformat(representative["edge_created_at"]),
                notes=representative["notes"],
                rationale=representative["rationale"],
                rationale_kind=RationaleKind(representative["rationale_kind"]),
                synced_from_version=representative.get("synced_from_version"),
                synced_from_content_hash=representative.get("synced_from_content_hash"),
            )

            # Per-type edge counts (deduplicated by edge ID to avoid
            # inflation from multi-path traversal at different depths).
            # Different edge_types between the same pair produce distinct
            # rows and so must be tallied separately.
            seen_edges: dict[str, set[str]] = {}
            for r in rows:
                et = r["edge_type"]
                seen_edges.setdefault(et, set()).add(r["edge_id"])
            counts = {et: len(ids) for et, ids in seen_edges.items()}

            nodes.append(
                TraversalNode.from_traversal(
                    document=doc_summary,
                    edge=edge,
                    depth=min_depth,
                    edge_counts=counts,
                )
            )

        return TraverseResponse(
            start_id=request.start_id,
            nodes=nodes,
            resolution_path=recorder.entries if recorder is not None else None,
        )

    async def _apply_retracts(
        self,
        rows: list[dict],
        query_start_id: str,
        cache: _LineageCache,
        recorder: _ResolutionPathRecorder | None = None,
    ) -> list[dict]:
        """Drop rows whose candidate edge has an in-lineage retraction.

        Only rows for chain-resolved policies (transitive_source,
        transitive_both) are candidates for retracts suppression. A
        retracts edge suppresses its target iff the retracts
        `source_valid_from_version` is in the supersedes lineage of
        `query_start_id`. This is one-sided (retracting-chain only):
        queries from the counterpart chain naturally do not share
        lineage with the retracts anchor so are unaffected.
        """
        if not rows:
            return rows

        candidate_ids: list[str] = []
        for row in rows:
            policy = self._effective_policy(row)
            if policy in (
                ResolutionPolicy.TRANSITIVE_SOURCE,
                ResolutionPolicy.TRANSITIVE_TARGET,
                ResolutionPolicy.TRANSITIVE_BOTH,
            ):
                candidate_ids.append(row["edge_id"])

        if not candidate_ids:
            return rows

        retracts_map = await self._store.get_retracts_for_edges(candidate_ids)
        if not retracts_map:
            return rows

        start_lineage = await cache.get(query_start_id)

        kept: list[dict] = []
        for row in rows:
            retracts = retracts_map.get(row["edge_id"])
            if not retracts:
                kept.append(row)
                continue
            policy = self._effective_policy(row)
            if policy not in (
                ResolutionPolicy.TRANSITIVE_SOURCE,
                ResolutionPolicy.TRANSITIVE_TARGET,
                ResolutionPolicy.TRANSITIVE_BOTH,
            ):
                kept.append(row)
                continue
            suppressing = next(
                (
                    r
                    for r in retracts
                    if r.source_valid_from_version is not None
                    and r.source_valid_from_version in start_lineage
                ),
                None,
            )
            if suppressing is None:
                kept.append(row)
            elif recorder is not None:
                recorder.retracts_applied(row["edge_id"], suppressing.id)
        return kept

    async def _apply_tombstones(
        self,
        rows: list[dict],
        query_start_id: str,
        cache: _LineageCache,
        recorder: _ResolutionPathRecorder | None = None,
    ) -> list[dict]:
        """Drop rows tombstoned by an upstream merged_from.

        An edge is suppressed iff `valid_until_version` is a strict
        ancestor of `query_start_id` on the supersedes chain. The
        equal-to-start case is kept (CR-034): querying at the termination
        version still surfaces the edge, since the tombstone marks the
        BOUNDARY, not itself.
        """
        if not rows:
            return rows

        # Fast path: no row has a tombstone set.
        has_tombstone = any(row.get("valid_until_version") for row in rows)
        if not has_tombstone:
            return rows

        start_lineage = await cache.get(query_start_id)

        kept: list[dict] = []
        for row in rows:
            tombstone = row.get("valid_until_version")
            if not tombstone:
                kept.append(row)
                continue
            if tombstone == query_start_id:
                # Historical query at the merge boundary: not suppressed.
                kept.append(row)
                continue
            if tombstone in start_lineage:
                # Tombstone is a strict ancestor of the query start:
                # query is downstream of the termination -> suppress.
                if recorder is not None:
                    recorder.tombstone_applied(row["edge_id"], tombstone)
                continue
            kept.append(row)
        return kept

    def _effective_policy(self, row: dict) -> ResolutionPolicy:
        """Return the authoritative policy for an edge row.

        Prefer the stored column (frozen at creation per CR-012). For
        legacy rows without a stored policy (pre-ADR-017 writes) fall
        back to policy=none so the edge stays visible and retracts /
        tombstones do not apply. Promoting NULL to the registry's
        declared policy would require anchors that the legacy writer
        had no way to stamp; the result would be silent suppression
        of every legacy references / covers / derived_from edge. A
        migration that backfills resolution_policy + anchors will
        move legacy rows to full ADR-017 semantics.
        """
        stored = row.get("resolution_policy")
        if stored:
            return ResolutionPolicy(stored)
        return ResolutionPolicy.NONE

    async def _collect_raw_with_seeds(
        self, request: TraverseRequest, cache: _LineageCache
    ) -> list[dict]:
        """Run direction-split, policy-driven multi-seed traversal.

        For direction=both we split into outbound and inbound phases so
        each side can use its own seed set (relevant for
        `transitive_source` inbound, where the target is frozen and the
        seed must not expand). Rows are deduped across seeds by
        edge_id, keeping the smallest observed depth.
        """
        if request.direction == TraversalDirection.BOTH:
            phases: list[TraversalDirection] = [
                TraversalDirection.OUTBOUND,
                TraversalDirection.INBOUND,
            ]
        else:
            phases = [request.direction]

        edge_type_filter = request.edge_type.value if request.edge_type else None

        raw_by_edge: dict[str, dict] = {}
        for phase in phases:
            seeds = await self._determine_seeds(request.start_id, request.edge_type, phase, cache)
            for seed in seeds:
                rows = await self._store.traverse(
                    start_id=seed,
                    edge_type=edge_type_filter,
                    direction=phase.value,
                    depth=request.depth,
                )
                for row in rows:
                    eid = row["edge_id"]
                    existing = raw_by_edge.get(eid)
                    if existing is None or row["depth"] < existing["depth"]:
                        raw_by_edge[eid] = row

        return list(raw_by_edge.values())

    async def _determine_seeds(
        self,
        start_id: str,
        edge_type: EdgeType | None,
        direction: TraversalDirection,
        cache: _LineageCache,
    ) -> list[str]:
        """Pick seed doc_ids based on registry policy and direction.

        Chain-scoped near-side: expand to supersedes lineage of start_id.
        Frozen near-side (transitive_source inbound): seeds = [start_id].
        policy=none: seeds = [start_id] (no chain expansion needed).
        No edge_type filter: expand to lineage (most permissive; the
          per-edge anchor filter prunes false positives). Acceptable for
          mixed-type traverses; full per-policy split is not required
          until downstream chunks drive the need.
        """
        if edge_type is None:
            lineage = await cache.get(start_id)
            return list(lineage) if lineage else [start_id]

        policy = self._edge_type_registry.policy_for(edge_type)

        if policy == ResolutionPolicy.NONE:
            return [start_id]

        if policy == ResolutionPolicy.TBD:
            raise TBDPolicyEdgeError(edge_type.value)

        if policy == ResolutionPolicy.TRANSITIVE_SOURCE:
            if direction == TraversalDirection.INBOUND:
                # Target is frozen; exact-match only.
                return [start_id]
            lineage = await cache.get(start_id)
            return list(lineage) if lineage else [start_id]

        if policy == ResolutionPolicy.TRANSITIVE_TARGET:
            # Mirror of transitive_source: source is frozen, target chain
            # advances. Outbound from a source-side start is exact-match
            # (source endpoint is frozen); inbound from a target-side
            # start expands to target lineage.
            if direction == TraversalDirection.OUTBOUND:
                return [start_id]
            lineage = await cache.get(start_id)
            return list(lineage) if lineage else [start_id]

        if policy == ResolutionPolicy.TRANSITIVE_BOTH:
            lineage = await cache.get(start_id)
            return list(lineage) if lineage else [start_id]

        return [start_id]

    async def _edge_passes_anchor_filter(
        self,
        row: dict,
        cache: _LineageCache,
        recorder: _ResolutionPathRecorder | None = None,
    ) -> bool:
        """True iff the edge's anchors sit in the appropriate lineages.

        The stored `resolution_policy` column is authoritative (frozen at
        creation). For legacy rows without a stored policy, the registry
        is consulted. Missing anchor documents yield WARN + suppress
        (conservative behavior, CAS-ADR-017 Chunk 4 open-question
        resolution).
        """
        edge_type_val = row["edge_type"]
        try:
            EdgeType(edge_type_val)
        except ValueError:
            logger.warning(
                "traverse: edge %s has unknown edge_type=%r; suppressing",
                row.get("edge_id"),
                edge_type_val,
            )
            return False

        policy = self._effective_policy(row)

        if policy == ResolutionPolicy.NONE:
            return True

        if policy == ResolutionPolicy.TBD:
            logger.warning(
                "traverse: edge %s has TBD resolution_policy; suppressing",
                row.get("edge_id"),
            )
            return False

        source_anchor = row.get("source_valid_from_version")
        target_anchor = row.get("target_valid_from_version")
        source_id = row["source_id"]
        target_id = row.get("target_id")
        edge_id = row.get("edge_id")

        if policy == ResolutionPolicy.TRANSITIVE_SOURCE:
            if source_anchor is None:
                if recorder is not None:
                    recorder.anchor_miss(edge_id, "source_valid_from_version", None)
                return False
            hit = await self._anchor_in_lineage(source_anchor, source_id, cache)
            if recorder is not None:
                if hit:
                    recorder.anchor_hit(edge_id, "source_valid_from_version", source_anchor)
                else:
                    recorder.anchor_miss(edge_id, "source_valid_from_version", source_anchor)
            return hit

        if policy == ResolutionPolicy.TRANSITIVE_TARGET:
            if target_anchor is None or target_id is None:
                if recorder is not None:
                    recorder.anchor_miss(edge_id, "target_valid_from_version", target_anchor)
                return False
            hit = await self._anchor_in_lineage(target_anchor, target_id, cache)
            if recorder is not None:
                if hit:
                    recorder.anchor_hit(edge_id, "target_valid_from_version", target_anchor)
                else:
                    recorder.anchor_miss(edge_id, "target_valid_from_version", target_anchor)
            return hit

        if policy == ResolutionPolicy.TRANSITIVE_BOTH:
            if source_anchor is None or target_anchor is None or target_id is None:
                if recorder is not None:
                    missing_field = (
                        "source_valid_from_version"
                        if source_anchor is None
                        else "target_valid_from_version"
                    )
                    missing_value = (
                        source_anchor
                        if missing_field == "source_valid_from_version"
                        else target_anchor
                    )
                    recorder.anchor_miss(edge_id, missing_field, missing_value)
                return False
            source_hit = await self._anchor_in_lineage(source_anchor, source_id, cache)
            if not source_hit:
                if recorder is not None:
                    recorder.anchor_miss(edge_id, "source_valid_from_version", source_anchor)
                return False
            target_hit = await self._anchor_in_lineage(target_anchor, target_id, cache)
            if not target_hit:
                if recorder is not None:
                    # Source check passed; record both outcomes so the
                    # trace shows which side dropped the edge.
                    recorder.anchor_hit(edge_id, "source_valid_from_version", source_anchor)
                    recorder.anchor_miss(edge_id, "target_valid_from_version", target_anchor)
                return False
            if recorder is not None:
                recorder.anchor_hit(edge_id, "source_valid_from_version", source_anchor)
                recorder.anchor_hit(edge_id, "target_valid_from_version", target_anchor)
            return True

        return True

    async def _anchor_in_lineage(
        self, anchor_id: str, endpoint_id: str, cache: _LineageCache
    ) -> bool:
        lineage = await cache.get(endpoint_id)
        if anchor_id in lineage:
            return True
        if not lineage:
            logger.warning(
                "traverse: endpoint %r has empty lineage (document missing); "
                "suppressing anchor check",
                endpoint_id,
            )
            return False
        # Anchor is not in lineage. If the anchor doc itself is missing,
        # surface a specific WARN per CR-021.
        anchor_doc = await self._store.get_document(anchor_id)
        if anchor_doc is None:
            logger.warning(
                "traverse: anchor document %r is missing (referenced as "
                "anchor for endpoint %r); suppressing edge",
                anchor_id,
                endpoint_id,
            )
        return False

    # ------------------------------------------------------------------
    # Chain walk (BH-089 through BH-096)
    # ------------------------------------------------------------------

    async def chain(self, request: ChainRequest) -> ChainResponse:
        """Walk an edge chain to both ends from any starting document."""
        start_doc = await self._store.get_document(request.document_id)
        if start_doc is None:
            raise DocumentNotFoundError(request.document_id)

        raw = await self._store.chain_walk(
            start_id=request.document_id,
            edge_type=request.edge_type.value,
        )

        documents = raw["documents"]
        edges = raw["edges"]
        doc_map = {d["doc_id"]: d for d in documents}

        # Build adjacency: source_id -> set of target_ids
        # For supersedes: source supersedes target, so source is newer.
        successors: dict[str, set[str]] = {d["doc_id"]: set() for d in documents}
        predecessors: dict[str, set[str]] = {d["doc_id"]: set() for d in documents}
        for e in edges:
            successors.setdefault(e["source_id"], set()).add(e["target_id"])
            predecessors.setdefault(e["target_id"], set()).add(e["source_id"])

        # Detect linearity: every node has at most 1 predecessor and 1 successor
        is_linear = all(len(successors[d]) <= 1 and len(predecessors[d]) <= 1 for d in doc_map)

        # Topological sort: find roots (no inbound edges = no predecessors)
        # and walk forward through successors, then reverse so position 0
        # is the end of the chain (oldest/original) and position N is the
        # root (newest/head).
        #
        # For supersedes: source supersedes target, so edge direction is
        # newer->older.  Roots (no predecessors) are the newest documents.
        # Walking successors goes backward in time.  Reversing gives
        # oldest-first ordering.
        roots = [d for d in doc_map if not predecessors.get(d)]

        if is_linear and len(roots) == 1:
            # Simple linear chain: walk from root to end, then reverse
            walk: list[str] = []
            current = roots[0]
            visited: set[str] = set()
            while current and current not in visited:
                visited.add(current)
                walk.append(current)
                nexts = successors.get(current, set())
                current = next(iter(nexts), None) if nexts else None
            ordered = list(reversed(walk))
        else:
            # Non-linear (fork) or multiple roots: BFS from all roots,
            # then reverse
            walk = []
            visited = set()
            queue = list(roots) if roots else [request.document_id]
            for node in queue:
                if node in visited:
                    continue
                visited.add(node)
                walk.append(node)
                for succ in sorted(successors.get(node, set())):
                    if succ not in visited:
                        queue.append(succ)
            ordered = list(reversed(walk))

        # Build chain entries with positions
        chain_entries: list[ChainEntry] = []
        query_position = 0
        for i, doc_id in enumerate(ordered):
            d = doc_map[doc_id]
            if doc_id == request.document_id:
                query_position = i
            chain_entries.append(ChainEntry.from_chain_row(d, position=i))

        # tail = position 0 (oldest), head = position N (newest)
        tail_id = ordered[0] if ordered else request.document_id
        head_id = ordered[-1] if ordered else request.document_id

        # When chain has only the query document (no matching edges),
        # report what edge types DO exist so the caller can adjust.
        available_edge_types: list[str] | None = None
        if len(chain_entries) == 1:
            outbound = await self._store.get_edges_by_source(request.document_id)
            inbound = await self._store.get_edges_by_target(request.document_id)
            all_types = sorted({e.edge_type.value for e in outbound + inbound})
            available_edge_types = all_types

        # Apply slice if limit is specified
        total_length = len(chain_entries)
        if request.limit is not None:
            chain_entries = chain_entries[request.offset : request.offset + request.limit]
        elif request.offset > 0:
            chain_entries = chain_entries[request.offset :]

        return ChainResponse(
            chain=chain_entries,
            head_id=head_id,
            tail_id=tail_id,
            query_position=query_position,
            length=len(chain_entries),
            total_length=total_length,
            is_linear=is_linear,
            available_edge_types=available_edge_types,
        )

    # ------------------------------------------------------------------
    # Minimal discover stub for BH-021
    # ------------------------------------------------------------------

    async def check_pipeline_for_retrieval(self, document_id: str) -> None:
        """Raise PipelineIncompleteError if document has failed pipeline.

        Minimal stub for deterministic retrieval gating. Full discover
        implementation is in Slice 3.
        """
        doc = await self._store.get_document(document_id)
        if doc is None:
            raise DocumentNotFoundError(document_id)
        if doc.pipeline_status == PipelineStatus.FAILED:
            raise PipelineIncompleteError(document_id)
