"""Lifecycle state machine and transition validation (BH-012 through BH-017).

The transition table is the central data structure, built from the vault's
lifecycle configuration. It powers both validation and the 409 valid_actions
error response.
"""

import uuid
from datetime import datetime, timezone

from sage.config import TransitionTable, VaultConfig, build_transition_table
from sage.models.enums import PipelineStatus, TERMINAL_PIPELINE_STATUSES
from sage.models.schemas import Document, Edge, SetLifecycleRequest, SetLifecycleResponse
from sage.storage.graph_store import GraphStore
from sage.storage.locks import DocumentLockManager


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
        from sage.api.errors import (
            DocumentNotFoundError,
            InvalidActionError,
            InvalidLifecycleTransitionError,
            MissingFieldError,
        )

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

            # Supersede-specific validation (BH-016, BH-017)
            if request.action == "supersede":
                if not request.new_version_id:
                    raise MissingFieldError("new_version_id", "supersede requires new_version_id")
                new_doc = await self._store.get_document(request.new_version_id)
                if new_doc is None:
                    raise DocumentNotFoundError(request.new_version_id)

            # Execute transition
            now = datetime.now(timezone.utc).isoformat()
            updates = {"lifecycle_status": to_state, "updated_at": now}
            updated_doc = await self._store.update_document(document_id, updates)

            # Create supersedes edge if needed (BH-017)
            if creates_edge == "supersedes" and request.new_version_id:
                edge = Edge(
                    id=str(uuid.uuid4()),
                    source_id=request.new_version_id,
                    target_id=document_id,
                    edge_type="supersedes",
                    created_at=datetime.now(timezone.utc),
                )
                await self._store.insert_edge(edge)

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
