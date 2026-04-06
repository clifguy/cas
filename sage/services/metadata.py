"""Metadata update logic for the update_metadata endpoint (BH-005, BH-006)."""

from datetime import datetime, timezone

from sage.config import VaultConfig
from sage.models.schemas import Document, UpdateMetadataRequest
from sage.storage.graph_store import GraphStore
from sage.storage.locks import DocumentLockManager


class MetadataService:
    def __init__(
        self,
        graph_store: GraphStore,
        lock_manager: DocumentLockManager,
        config: VaultConfig,
    ) -> None:
        self._store = graph_store
        self._locks = lock_manager
        self._config = config

    async def update_metadata(
        self,
        document_id: str,
        request: UpdateMetadataRequest,
        modified_by: str,
    ) -> Document:
        """Partial update of mutable metadata fields.

        Validates doc_type against vault's document_types config.

        Raises:
            DocumentNotFoundError: document_id does not exist.
            InvalidDocTypeError: doc_type not in vault config.
        """
        from sage.api.errors import DocumentNotFoundError, InvalidDocTypeError

        async with self._locks.lock(document_id):
            doc = await self._store.get_document(document_id)
            if doc is None:
                raise DocumentNotFoundError(document_id)

            updates: dict = {}
            if request.title is not None:
                updates["title"] = request.title
            if request.version_label is not None:
                updates["version_label"] = request.version_label
            if request.project is not None:
                updates["project"] = request.project
            if request.tags is not None:
                updates["tags"] = request.tags
            if request.authority_scope is not None:
                updates["authority_scope"] = request.authority_scope
            if request.doc_type is not None:
                valid_types = self._config.valid_doc_type_values()
                if request.doc_type not in valid_types:
                    raise InvalidDocTypeError(request.doc_type, valid_types)
                updates["doc_type"] = request.doc_type

            if updates:
                updates["last_modified_by"] = modified_by
                updates["updated_at"] = datetime.now(timezone.utc).isoformat()
                doc = await self._store.update_document(document_id, updates)

            return doc
