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

from sage.adapters.interfaces import GraphStore
from sage.api.errors import (
    BinaryContentRefusedError,
    ContentDeliveryConflictError,
    ContentFileMissingError,
    ContentTooLargeError,
    DocumentNotFoundError,
    DownloadUrlNotAvailableError,
    LocalOpenNotAvailableError,
    WritePathExistsError,
    WritePathInvalidError,
)
from sage.config import VaultConfig
from sage.models.enums import BINARY_CONTAINER_SOURCE_TYPES
from sage.models.schemas import (
    Document,
    DocumentDownloadUrlResponse,
    DocumentWithContent,
    OpenDocumentResponse,
    ReadMeta,
)
from sage.services.read_diagnostics import build_not_found_detail
from sage.vault_source_binding import SupportsSourceDownloadUrl, VaultSourceStore

DEFAULT_MAX_INLINE_CONTENT_BYTES = 100 * 1024 * 1024


def _max_inline_content_bytes() -> int:
    raw = os.environ.get("SAGE_MAX_INLINE_CONTENT_BYTES")
    if raw is None:
        return DEFAULT_MAX_INLINE_CONTENT_BYTES
    return int(raw)


def _attach_inline_content(
    response: DocumentWithContent,
    doc: Document,
    vault_id: str,
    storage_root: Path,
    store: VaultSourceStore,
) -> None:
    """Populate `content` (base64) and `content_size` from the vault file.

    Reads the retained source through the vault-source store so delivery is
    binding-agnostic (CAS-ADR-043). The size is checked before the bytes are
    read, so an oversize source is rejected without loading it.

    Raises ContentFileMissingError or ContentTooLargeError on failure.
    """
    if not store.source_exists(vault_id, storage_root, doc.source_path):
        raise ContentFileMissingError(doc.id, doc.source_path)

    size = store.source_size(vault_id, storage_root, doc.source_path)
    ceiling = _max_inline_content_bytes()
    if size > ceiling:
        raise ContentTooLargeError(doc.id, size, ceiling)

    data = store.read_source(vault_id, storage_root, doc.source_path)
    response.content = base64.b64encode(data).decode("ascii")
    response.content_size = size


def _deliver_to_path(
    response: DocumentWithContent,
    doc: Document,
    vault_id: str,
    storage_root: Path,
    write_to_path: str,
    store: VaultSourceStore,
) -> None:
    """Copy the vault file to `write_to_path` and populate delivery fields.

    Reads the retained source through the vault-source store (CAS-ADR-043)
    and writes the bytes to the caller-specified local path.

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

    if not store.source_exists(vault_id, storage_root, doc.source_path):
        raise ContentFileMissingError(doc.id, doc.source_path)

    data = store.read_source(vault_id, storage_root, doc.source_path)
    target.write_bytes(data)

    response.written_to = str(target)
    response.content_size = len(data)
    # Canonicalize to the Sha256Str shape (`sha256:` + 64 hex).
    # DocumentWithContent.content_hash is typed; hashlib emits raw hex.
    response.content_hash = f"sha256:{hashlib.sha256(data).hexdigest()}"


class DocumentsService:
    def __init__(self, graph_store: GraphStore, config: VaultConfig) -> None:
        self._store = graph_store
        self._config = config

    async def open_document_locally(self, document_id: str) -> OpenDocumentResponse:
        """Open the document's source file using the local OS file association.

        A local-profile convenience: invokes the host OS opener
        (open/xdg-open/startfile) fire-and-forget. It is meaningful only when the
        browser and SAGE share a machine, so it is gated to the local profile --
        under the cloud profile SAGE is a headless container and the opener is
        refused with a structured 501; a caller delivers the document to the
        browser through a download URL instead (CAS-ADR-043).
        """
        from sage.mcp_init import get_stack_config

        if get_stack_config().profile == "cloud":
            raise LocalOpenNotAvailableError()

        doc = await self._store.get_document(document_id)
        if doc is None:
            raise DocumentNotFoundError(
                document_id, await build_not_found_detail(self._store, document_id)
            )

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
            os.startfile(str(file_path))  # type: ignore[attr-defined] # noqa: S606 -- Windows shell-execute equivalent of the POSIX openers above
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
            raise DocumentNotFoundError(
                document_id, await build_not_found_detail(self._store, document_id)
            )

        # Body-form discipline (CAS-ADR-039): a binary-container source holds
        # raw package bytes, not scannable text. Refuse to inline those bytes
        # before any file I/O so a caller cannot scan a container as text and
        # read a false-clean result; the extracted text lives in the
        # projection, so direct the caller to read_projection.
        is_binary = doc.source_type in BINARY_CONTAINER_SOURCE_TYPES
        if include_content and is_binary:
            raise BinaryContentRefusedError(document_id, doc.source_type)

        response = DocumentWithContent(**doc.model_dump())
        storage_root = Path(self._config.vault.storage_root).expanduser().resolve()

        if include_content or write_to_path:
            # Deliver the retained source through the active profile's
            # vault-source store so delivery is binding-agnostic (CAS-ADR-043).
            from sage.mcp_init import get_stack_config, resolve_stack_vault_source_store

            store = resolve_stack_vault_source_store(get_stack_config())
            vault_id = self._config.vault.id
            if include_content:
                _attach_inline_content(response, doc, vault_id, storage_root, store)
            else:
                _deliver_to_path(response, doc, vault_id, storage_root, write_to_path, store)

        # Self-describing read markers (CAS-ADR-039). The content body is the
        # inlined bytes; write-to-path delivery and the default request both
        # leave `content` null, so body_length is reported only for inline.
        response.read_meta = ReadMeta(
            success=True,
            body_present=response.content is not None,
            body_length=response.content_size if response.content is not None else None,
        )
        # Positive body-form signal on every delivered response. A
        # binary-container source only reaches here when content was not
        # requested (the refusal above gates include_content); a default or
        # write-to-path read of a binary source still declares its form.
        response.body_form = "binary" if is_binary else "text"

        return response

    async def get_document_download_url(self, document_id: str) -> DocumentDownloadUrlResponse:
        """Mint a short-lived download URL for a cloud-resident document's source.

        The browser-delivery path for the cloud profile: the active vault-source
        binding issues a pre-authenticated URL the browser fetches directly from
        the backing store, so the bytes never transit SAGE (CAS-ADR-043). Only a
        binding that supports the capability can answer; the filesystem binding
        cannot, and the request is refused with a structured 501.
        """
        doc = await self._store.get_document(document_id)
        if doc is None:
            raise DocumentNotFoundError(
                document_id, await build_not_found_detail(self._store, document_id)
            )

        from sage.mcp_init import get_stack_config, resolve_stack_vault_source_store

        store = resolve_stack_vault_source_store(get_stack_config())
        if not isinstance(store, SupportsSourceDownloadUrl):
            raise DownloadUrlNotAvailableError(document_id)

        storage_root = Path(self._config.vault.storage_root).expanduser().resolve()
        vault_id = self._config.vault.id
        if not store.source_exists(vault_id, storage_root, doc.source_path):
            raise ContentFileMissingError(doc.id, doc.source_path)

        url = store.download_url(vault_id, storage_root, doc.source_path)
        if url is None:
            raise DownloadUrlNotAvailableError(document_id)

        return DocumentDownloadUrlResponse(download_url=url)
