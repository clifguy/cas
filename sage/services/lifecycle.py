"""Lifecycle state machine and transition validation (BH-012 through BH-017).

The transition table is the central data structure, built from the vault's
lifecycle configuration. It powers both validation and the 409 valid_actions
error response.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sage.api.errors import (
    DocumentNotFoundError,
    InvalidActionError,
    InvalidLifecycleTransitionError,
    MissingFieldError,
)
from sage.config import TransitionTable, VaultConfig, build_transition_table
from sage.models.enums import EdgeType, ResolutionPolicy, TERMINAL_PIPELINE_STATUSES
from sage.models.schemas import Document, Edge, SetLifecycleRequest, SetLifecycleResponse
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
    ) -> None:
        self._store = graph_store
        self._locks = lock_manager
        self._config = config
        self._table = build_transition_table(config)

    @property
    def transition_table(self) -> TransitionTable:
        return self._table

    async def set_lifecycle(
        self, document_id: str, request: SetLifecycleRequest
    ) -> SetLifecycleResponse:
        """Execute a lifecycle state transition.

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
            result = self._table.validate_transition(
                doc.lifecycle_status, request.action
            )
            if result is None:
                valid = self._table.get_valid_actions(doc.lifecycle_status)
                raise InvalidLifecycleTransitionError(
                    doc.lifecycle_status,
                    request.action,
                    valid,
                    pipeline_status=doc.pipeline_status.value
                    if doc.pipeline_status
                    else None,
                )

            to_state, creates_edge = result

            # Supersede-specific validation (BH-016, BH-017) and atomic commit.
            # The lifecycle flip and the supersedes edge insert run in a
            # single SQLite transaction so a mid-operation failure cannot
            # leave the predecessor archived without the corresponding
            # edge (BH-135).
            if request.action == "supersede":
                if not request.new_version_id:
                    raise MissingFieldError(
                        "new_version_id", "supersede requires new_version_id"
                    )
                new_doc = await self._store.get_document(request.new_version_id)
                if new_doc is None:
                    raise DocumentNotFoundError(request.new_version_id)

                now = datetime.now(timezone.utc)
                predecessor_updates = {
                    "lifecycle_status": to_state,
                    "updated_at": now.isoformat(),
                }
                edge = Edge(
                    id=str(uuid.uuid4()),
                    source_id=request.new_version_id,
                    target_id=document_id,
                    edge_type=EdgeType.SUPERSEDES,
                    resolution_policy=ResolutionPolicy.NONE,
                    created_at=now,
                )
                updated_doc = await self._store.supersede_atomic(
                    document_id, predecessor_updates, edge
                )
            else:
                # Non-supersede actions: single-row update is naturally atomic.
                now = datetime.now(timezone.utc).isoformat()
                updates = {"lifecycle_status": to_state, "updated_at": now}
                updated_doc = await self._store.update_document(document_id, updates)

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
        result = self._table.validate_transition(
            predecessor.lifecycle_status, "supersede"
        )
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
        return SupersedeTransition(
            predecessor_updates=predecessor_updates, edge=edge
        )
