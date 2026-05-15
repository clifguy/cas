"""Document content delivery and local-open operations.

Owns the work behind two endpoints:
- POST /documents/{document_id}/open -- open the source file via the host OS opener.
- GET /documents/{document_id} -- return document metadata, optionally with
  base64-inlined content (BH-116-BH-119) or written to a caller-supplied path
  (BH-125-BH-128).
"""

import base64
import hashlib
import os
import subprocess
import sys
from pathlib import Path

from sage.api.errors import (
    ContentDeliveryConflictError,
    ContentFileMissingError,
    ContentTooLargeError,
    DocumentNotFoundError,
    WritePathExistsError,
    WritePathInvalidError,
)
from sage.config import VaultConfig
from sage.models.schemas import Document, DocumentWithContent, OpenDocumentResponse
from sage.storage.graph_store import GraphStore

DEFAULT_MAX_INLINE_CONTENT_BYTES = 100 * 1024 * 1024


def _max_inline_content_bytes() -> int:
    raw = os.environ.get("SAGE_MAX_INLINE_CONTENT_BYTES")
    if raw is None:
        return DEFAULT_MAX_INLINE_CONTENT_BYTES
    return int(raw)


def _attach_inline_content(
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


def _deliver_to_path(
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
        raise WritePathInvalidError(write_to_path, f"parent directory does not exist: {parent}")
    if not parent.is_dir():
        raise WritePathInvalidError(write_to_path, f"parent is not a directory: {parent}")
    if not os.access(parent, os.W_OK):
        raise WritePathInvalidError(write_to_path, f"parent directory is not writable: {parent}")

    source_file = storage_root / doc.source_path
    if not source_file.exists():
        raise ContentFileMissingError(doc.id, doc.source_path)

    data = source_file.read_bytes()
    target.write_bytes(data)

    response.written_to = str(target)
    response.content_size = len(data)
    # Canonicalize to the Sha256Str shape (`sha256:` + 64 hex). Per T-0026,
    # DocumentWithContent.content_hash is typed; hashlib emits raw hex.
    response.content_hash = f"sha256:{hashlib.sha256(data).hexdigest()}"


class DocumentsService:
    def __init__(self, graph_store: GraphStore, config: VaultConfig) -> None:
        self._store = graph_store
        self._config = config

    async def open_document_locally(self, document_id: str) -> OpenDocumentResponse:
        """Open the document's source file using the local OS file association.

        Local-only convenience: invokes the host OS opener
        (open/xdg-open/startfile) fire-and-forget. If CAS is ever deployed
        beyond localhost, gate this behind a loopback check or remove it.
        """
        doc = await self._store.get_document(document_id)
        if doc is None:
            raise DocumentNotFoundError(document_id)

        storage_root = Path(self._config.vault.storage_root).expanduser().resolve()
        file_path = storage_root / doc.source_path
        if not file_path.exists():
            raise ContentFileMissingError(doc.id, doc.source_path)

        platform = sys.platform
        if platform == "darwin":
            subprocess.Popen(["open", str(file_path)])  # noqa: S603,S607 -- hardcoded macOS opener; file_path is registry-validated
        elif platform.startswith("linux"):
            subprocess.Popen(["xdg-open", str(file_path)])  # noqa: S603,S607 -- hardcoded XDG opener; file_path is registry-validated
        elif platform == "win32":
            os.startfile(str(file_path))  # type: ignore[attr-defined]  # noqa: S606 -- Windows shell-execute equivalent of the POSIX openers above
        else:
            subprocess.Popen(["xdg-open", str(file_path)])  # noqa: S603,S607 -- hardcoded XDG opener fallback; file_path is registry-validated

        return OpenDocumentResponse(opened=True, path=str(file_path))

    async def get_document_with_content(
        self,
        document_id: str,
        include_content: bool,
        write_to_path: str | None,
    ) -> DocumentWithContent:
        """Return a document, optionally with content inlined or written to disk."""
        if include_content and write_to_path:
            raise ContentDeliveryConflictError()

        doc = await self._store.get_document(document_id)
        if doc is None:
            raise DocumentNotFoundError(document_id)

        response = DocumentWithContent(**doc.model_dump())
        storage_root = Path(self._config.vault.storage_root).expanduser().resolve()

        if include_content:
            _attach_inline_content(response, doc, storage_root)
        elif write_to_path:
            _deliver_to_path(response, doc, storage_root, write_to_path)

        return response
