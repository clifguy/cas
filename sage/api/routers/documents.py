"""GET /sage_vaults/{vault_id}/documents/{document_id} -- get_document (BH-022).

Supports the agentic read-modify-reingest round-trip via two
mutually-exclusive content-delivery modes:
- `include_content=true` returns base64 bytes inline (BH-116 through BH-119).
- `write_to_path=<path>` writes bytes to disk and returns metadata
  (BH-125 through BH-128). Preferred for files that would otherwise
  exceed MCP tool-result size ceilings.
"""

import base64
import hashlib
import os
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from sage.api.dependencies import get_config, get_graph_store, get_vault_id
from sage.api.errors import (
    ContentDeliveryConflictError,
    ContentFileMissingError,
    ContentTooLargeError,
    DocumentNotFoundError,
    WritePathExistsError,
    WritePathInvalidError,
)
from sage.config import VaultConfig
from sage.models.schemas import Document, DocumentWithContent
from sage.storage.graph_store import GraphStore

router = APIRouter(tags=["Document Metadata"])


DEFAULT_MAX_INLINE_CONTENT_BYTES = 100 * 1024 * 1024


def _max_inline_content_bytes() -> int:
    raw = os.environ.get("SAGE_MAX_INLINE_CONTENT_BYTES")
    if raw is None:
        return DEFAULT_MAX_INLINE_CONTENT_BYTES
    return int(raw)


def attach_inline_content(
    response: DocumentWithContent,
    doc: Document,
    storage_root: Path,
) -> None:
    """Populate `content` (base64) and `content_size` from the vault file.

    Raises ContentFileMissingError or ContentTooLargeError on failure.
    """
    file_path = storage_root / doc.source_path
    if not file_path.exists():
        raise ContentFileMissingError(doc.id, doc.source_path)

    size = file_path.stat().st_size
    ceiling = _max_inline_content_bytes()
    if size > ceiling:
        raise ContentTooLargeError(doc.id, size, ceiling)

    response.content = base64.b64encode(file_path.read_bytes()).decode("ascii")
    response.content_size = size


def deliver_to_path(
    response: DocumentWithContent,
    doc: Document,
    storage_root: Path,
    write_to_path: str,
) -> None:
    """Copy the vault file to `write_to_path` and populate delivery fields.

    Raises WritePathInvalidError, WritePathExistsError, or
    ContentFileMissingError on failure.
    """
    target = Path(write_to_path)
    if not target.is_absolute():
        raise WritePathInvalidError(write_to_path, "path must be absolute")

    if target.exists():
        raise WritePathExistsError(write_to_path)

    parent = target.parent
    if not parent.exists():
        raise WritePathInvalidError(
            write_to_path, f"parent directory does not exist: {parent}"
        )
    if not parent.is_dir():
        raise WritePathInvalidError(
            write_to_path, f"parent is not a directory: {parent}"
        )
    if not os.access(parent, os.W_OK):
        raise WritePathInvalidError(
            write_to_path, f"parent directory is not writable: {parent}"
        )

    source_file = storage_root / doc.source_path
    if not source_file.exists():
        raise ContentFileMissingError(doc.id, doc.source_path)

    data = source_file.read_bytes()
    target.write_bytes(data)

    response.written_to = str(target)
    response.content_size = len(data)
    response.content_hash = hashlib.sha256(data).hexdigest()


@router.get("/documents/{document_id}", response_model=DocumentWithContent)
async def get_document(
    document_id: str,
    include_content: bool = Query(default=False),
    write_to_path: str | None = Query(default=None),
    vault_id: str = Depends(get_vault_id),
    graph_store: GraphStore = Depends(get_graph_store),
    config: VaultConfig = Depends(get_config),
) -> DocumentWithContent:
    if include_content and write_to_path:
        raise ContentDeliveryConflictError()

    doc = await graph_store.get_document(document_id)
    if doc is None:
        raise DocumentNotFoundError(document_id)

    response = DocumentWithContent(**doc.model_dump())
    storage_root = Path(config.vault.storage_root).expanduser().resolve()

    if include_content:
        attach_inline_content(response, doc, storage_root)
    elif write_to_path:
        deliver_to_path(response, doc, storage_root, write_to_path)

    return response
