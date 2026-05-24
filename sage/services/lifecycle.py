"""Lifecycle state machine and transition validation (BH-012 through BH-017).

The transition table is the central data structure, built from the vault's
lifecycle configuration. It powers both validation and the 409 valid_actions
error response.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sage.adapters.interfaces import ContentStore
from sage.api.errors import (
    DocumentNotFoundError,
    InvalidActionError,
    InvalidLifecycleTransitionError,
    MissingFieldError,
    SAGEError,
)
from sage.config import TransitionTable, VaultConfig, build_transition_table
from sage.models.enums import (
    LIGHT_DEFAULT_THRESHOLD,
    TERMINAL_PIPELINE_STATUSES,
    EdgeType,
    ResolutionPolicy,
    ResponseMode,
)
from sage.models.schemas import (
    BulkLifecycleItemResult,
    BulkLifecycleRequest,
    BulkLifecycleResponse,
    Document,
    Edge,
    SetLifecycleRequest,
    SetLifecycleResponse,
)
from sage.services._bulk_envelope import sage_error_to_envelope
from sage.services._dry_run import DRY_RUN_SENTINEL_EDGE_ID as _DRY_RUN_SENTINEL_EDGE_ID
from sage.storage.graph_store import GraphStore
from sage.storage.locks import DocumentLockManager


@dataclass
class SupersedeTransition:
    """Pre-built supersede transition ready for atomic commit.

    Produced by LifecycleService.prepare_supersede and consumed by
    IngestionService when a supersede is bundled with a new document
    insert (BH-136).
    """

    predecessor_updates: dict
    edge: Edge


class LifecycleService:
    def __init__(
        self,
        graph_store: GraphStore,
        lock_manager: DocumentLockManager,
        config: VaultConfig,
        content_store: ContentStore | None = None,
    ) -> None:
        self._store = graph_store
        self._locks = lock_manager
        self._config = config
        self._content = content_store
        self._table = build_transition_table(config)

    @property
    def transition_table(self) -> TransitionTable:
        return self._table

    async def set_lifecycle(
        self, document_id: str, request: SetLifecycleRequest
    ) -> SetLifecycleResponse:
        """Execute a lifecycle state transition (or preview it on dry-run).

        T-0152: when ``request.dry_run`` is True, runs every validator in
        the same order as a real run but skips the persistence call
        (``update_document`` / ``supersede_atomic``) and the chunk-store
        sync. The response carries the would-be document and, for
        ``supersede``, a would-be ``created_edge`` with sentinel id
        ``<dry-run>`` (the real id is non-deterministic at commit time).

        Raises:
            DocumentNotFoundError: document_id does not exist.
            InvalidActionError: action is unknown (400).
            InvalidLifecycleTransitionError: action invalid from current state (409).
            DocumentNotFoundError: new_version_id does not exist (supersede).
        """
        async with self._locks.lock(document_id):
            doc = await self._store.get_document(document_id)
            if doc is None:
                raise DocumentNotFoundError(document_id)

            # Validate action is known (400 vs 409 distinction)
            if not self._table.is_known_action(request.action):
                raise InvalidActionError(request.action)

            # Validate transition from current state
            result = self._table.validate_transition(doc.lifecycle_status, request.action)
            if result is None:
                valid = self._table.get_valid_actions(doc.lifecycle_status)
                raise InvalidLifecycleTransitionError(
                    doc.lifecycle_status,
                    request.action,
                    valid,
                    pipeline_status=doc.pipeline_status.value if doc.pipeline_status else None,
                )

            to_state, creates_edge = result

            created_edge: Edge | None = None

            # Supersede-specific validation (BH-016, BH-017) and atomic commit.
            # The lifecycle flip and the supersedes edge insert run in a
            # single SQLite transaction so a mid-operation failure cannot
            # leave the predecessor archived without the corresponding
            # edge (BH-135).
            if request.action == "supersede":
                if not request.new_version_id:
                    raise MissingFieldError("new_version_id", "supersede requires new_version_id")
                new_doc = await self._store.get_document(request.new_version_id)
                if new_doc is None:
                    raise DocumentNotFoundError(request.new_version_id)

                now = datetime.now(timezone.utc)
                predecessor_updates = {
                    "lifecycle_status": to_state,
                    "updated_at": now.isoformat(),
                }
                # T-0152: on dry-run, build the would-be edge with a
                # sentinel id so callers can never mistake it for a
                # persisted edge. On real-run, mint the real uuid up
                # front so it can be returned alongside the document.
                edge_id = _DRY_RUN_SENTINEL_EDGE_ID if request.dry_run else str(uuid.uuid4())
                edge = Edge(
                    id=edge_id,
                    source_id=request.new_version_id,
                    target_id=document_id,
                    edge_type=EdgeType.SUPERSEDES,
                    resolution_policy=ResolutionPolicy.NONE,
                    created_at=now,
                )
                if request.dry_run:
                    # Compute the would-be predecessor without persisting.
                    updated_doc = doc.model_copy(update=predecessor_updates)
                    created_edge = edge
                else:
                    updated_doc = await self._store.supersede_atomic(
                        document_id, predecessor_updates, edge
                    )
                    created_edge = edge
            else:
                # Non-supersede actions: single-row update is naturally atomic.
                now = datetime.now(timezone.utc).isoformat()
                updates = {"lifecycle_status": to_state, "updated_at": now}
                if request.dry_run:
                    updated_doc = doc.model_copy(update=updates)
                else:
                    updated_doc = await self._store.update_document(document_id, updates)

            # Sync the new lifecycle_status to the chunk store so LanceDB
            # pre-filter pushdown (T-0077) stays accurate after the
            # transition. Best-effort: legacy wiring that omits
            # content_store falls through as a no-op.
            #
            # T-0152: skipped on dry-run — the chunk-store sync is a
            # persistence side effect and must not run when the caller
            # asked for a preview.
            if self._content is not None and not request.dry_run:
                await self._content.update_chunk_metadata(
                    document_id, {"lifecycle_status": to_state}
                )

            # Generate warnings (BH-014, BH-015)
            warnings: list[str] = []
            if updated_doc and updated_doc.pipeline_status not in TERMINAL_PIPELINE_STATUSES:
                warnings.append(
                    f"Document pipeline is {updated_doc.pipeline_status}; "
                    f"lifecycle transition completed but pipeline is still in progress."
                )

            return SetLifecycleResponse(
                document=updated_doc,
                warnings=warnings if warnings else None,
                dry_run=request.dry_run,
                created_edge=created_edge,
            )

    async def bulk_set_lifecycle(self, request: BulkLifecycleRequest) -> BulkLifecycleResponse:
        """Apply one lifecycle transition per item; per-item lock and
        per-item transaction.

        The batch is NOT atomic (CAS-ADR-029). A SAGEError raised by one
        item does not roll back earlier-or-later successful items; the
        item's per-document lock and per-item transaction provide
        isolation. Anything outside the SAGEError hierarchy is treated
        as a programmer or infrastructure bug and propagates out of the
        batch.

        The performance win versus N sequential ``sage_set_lifecycle``
        MCP calls comes from eliminating per-call MCP framing overhead
        and asyncio scheduling between items; the per-document lock and
        the per-item SQLite transaction are unchanged.

        ``request.response_mode`` (T-0153) controls per-item payload
        depth. ``light`` drops the per-item ``document`` body from
        success entries; failure entries always carry the full structured
        error envelope. When unset, the default-resolution rule mirrors
        ``sage_discover``: batches with more than
        ``LIGHT_DEFAULT_THRESHOLD`` items default to ``light``, smaller
        batches default to ``full``.
        """
        # T-0153: resolve the effective response_mode the same way
        # ``RetrievalService._edges`` does, but driven by ``len(items)``
        # instead of ``total_count``. The default-threshold rule is
        # courtesy for the human-readable single-item call; the bulk
        # endpoint exists precisely for the case where ``full`` becomes
        # ruinous (28-item batch in the field report ~79K chars).
        effective_mode = request.response_mode
        if effective_mode is None:
            effective_mode = (
                ResponseMode.LIGHT
                if len(request.items) > LIGHT_DEFAULT_THRESHOLD
                else ResponseMode.FULL
            )

        results: list[BulkLifecycleItemResult] = []
        for item in request.items:
            single = SetLifecycleRequest(
                action=item.action,
                new_version_id=item.new_version_id,
                # T-0152: propagate envelope dry_run to each per-item
                # call. Per-item override is not supported.
                dry_run=request.dry_run,
            )
            try:
                response = await self.set_lifecycle(item.document_id, single)
                results.append(
                    BulkLifecycleItemResult(
                        document_id=item.document_id,
                        status="success",
                        # T-0153 light mode: drop the document body. The
                        # caller already knows the document_id (they
                        # passed it); the body's primary bloat field
                        # (semantic_abstract) and the rest are stripped.
                        document=(
                            response.document if effective_mode == ResponseMode.FULL else None
                        ),
                        warnings=response.warnings,
                    )
                )
            except SAGEError as exc:
                results.append(
                    BulkLifecycleItemResult(
                        document_id=item.document_id,
                        status="error",
                        error=sage_error_to_envelope(exc),
                    )
                )
        success_count = sum(1 for r in results if r.status == "success")
        return BulkLifecycleResponse(
            results=results,
            success_count=success_count,
            error_count=len(results) - success_count,
            total=len(results),
            # T-0152: envelope echo so callers can confirm the batch ran
            # as a preview even when every per-item document was
            # dropped under light response_mode.
            dry_run=request.dry_run,
        )

    def prepare_supersede(
        self,
        predecessor: Document,
        new_version_id: str,
    ) -> SupersedeTransition:
        """Validate the supersede transition and build the writes for it
        without committing. Used by IngestionService to bundle the
        predecessor flip and edge insert into the same SQLite transaction
        as the new document insert (BH-136).

        The caller is responsible for ensuring `predecessor` is freshly
        loaded and that the calling context holds whatever lock is
        appropriate.
        """
        result = self._table.validate_transition(predecessor.lifecycle_status, "supersede")
        if result is None:
            valid = self._table.get_valid_actions(predecessor.lifecycle_status)
            raise InvalidLifecycleTransitionError(
                predecessor.lifecycle_status,
                "supersede",
                valid,
                pipeline_status=predecessor.pipeline_status.value
                if predecessor.pipeline_status
                else None,
            )
        to_state, _ = result
        now = datetime.now(timezone.utc)
        predecessor_updates = {
            "lifecycle_status": to_state,
            "updated_at": now.isoformat(),
        }
        edge = Edge(
            id=str(uuid.uuid4()),
            source_id=new_version_id,
            target_id=predecessor.id,
            edge_type=EdgeType.SUPERSEDES,
            resolution_policy=ResolutionPolicy.NONE,
            created_at=now,
        )
        return SupersedeTransition(predecessor_updates=predecessor_updates, edge=edge)
