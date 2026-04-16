"""GET /sage_vaults/{vault_id}/documents/{document_id} -- get_document (BH-022).

Supports the agentic read-modify-reingest round-trip via the optional
`include_content` query parameter (BH-116 through BH-119).
"""

import base64
import os
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from sage.api.dependencies import get_config, get_graph_store, get_vault_id
from sage.api.errors import (
    ContentFileMissingError,
    ContentTooLargeError,
    DocumentNotFoundError,
)
from sage.config import VaultConfig
from sage.models.schemas import DocumentWithContent
from sage.storage.graph_store import GraphStore

router = APIRouter(tags=["Document Metadata"])


DEFAULT_MAX_INLINE_CONTENT_BYTES = 100 * 1024 * 1024


def _max_inline_content_bytes() -> int:
    raw = os.environ.get("SAGE_MAX_INLINE_CONTENT_BYTES")
    if raw is None:
        return DEFAULT_MAX_INLINE_CONTENT_BYTES
    return int(raw)


@router.get("/documents/{document_id}", response_model=DocumentWithContent)
async def get_document(
    document_id: str,
    include_content: bool = Query(default=False),
    vault_id: str = Depends(get_vault_id),
    graph_store: GraphStore = Depends(get_graph_store),
    config: VaultConfig = Depends(get_config),
) -> DocumentWithContent:
    doc = await graph_store.get_document(document_id)
    if doc is None:
        raise DocumentNotFoundError(document_id)

    response = DocumentWithContent(**doc.model_dump())
    if include_content:
        storage_root = Path(config.vault.storage_root).expanduser().resolve()
        file_path = storage_root / doc.source_path
        if not file_path.exists():
            raise ContentFileMissingError(document_id, doc.source_path)

        size = file_path.stat().st_size
        ceiling = _max_inline_content_bytes()
        if size > ceiling:
            raise ContentTooLargeError(document_id, size, ceiling)

        response.content = base64.b64encode(file_path.read_bytes()).decode("ascii")
        response.content_size = size

    return response
