"""Token-gated transfer endpoints: the byte legs of the caller-local channel.

When the server cannot see the calling client's filesystem, the path-bearing
tools mint short-lived one-time tokens and return recipes; the caller's
environment moves the raw bytes through these two routes. The recipe token is
the sole credential here -- the routes are exempt from bearer authentication,
matching the edge -- so every check (direction, expiry, one-time redemption)
lives in the transfer store. The routes are process-scoped, not vault-scoped:
the vault binding travels inside the token.
"""

import hashlib
import mimetypes
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect

from sage.api.dependencies import get_transfer_service
from sage.api.errors import (
    ContentFileMissingError,
    TransferContentTooLargeError,
    VaultNotFoundError,
)
from sage.models.schemas import ErrorResponse, TransferUploadResult
from sage.services.transfer import (
    PendingTransfer,
    TransferStore,
    max_transfer_bytes,
)

router = APIRouter(tags=["Transfer"])

_SPOOL_CHUNK_BYTES = 65536


@router.put(
    "/upload",
    status_code=201,
    responses={
        409: {
            "model": ErrorResponse,
            "description": (
                "`transfer_token_already_used`: this transfer already holds "
                "staged bytes awaiting completion."
            ),
        },
        410: {
            "model": ErrorResponse,
            "description": (
                "`transfer_token_invalid`: the token names no redeemable "
                "pending transfer (unknown, expired, already used, or scoped "
                "to a different direction)."
            ),
        },
        413: {
            "model": ErrorResponse,
            "description": (
                "`transfer_content_too_large`: the body exceeded the transfer "
                "ceiling and the delivery was aborted; the partial staging "
                "file is removed and the token stays retryable."
            ),
        },
    },
)
async def transfer_upload(
    request: Request,
    x_upload_token: str = Header(default="", alias="X-Upload-Token"),
    store: TransferStore = Depends(get_transfer_service),
) -> TransferUploadResult:
    """Deliver a pending transfer's bytes against a one-time upload token.

    The raw request body streams straight to the transfer's staging file
    under an incremental byte ceiling -- no hop holds the whole file, the
    declared Content-Length is never trusted, and an oversize or interrupted
    delivery rolls the transfer back to a retryable state. The receipt
    carries the received size and digest so the sender can verify the
    delivery against the local file before issuing the completion call.
    """
    entry = store.begin_upload(x_upload_token)
    ceiling = max_transfer_bytes()
    digest = hashlib.sha256()
    received = 0
    try:
        with entry.staged_path.open("wb") as staged:
            async for chunk in request.stream():
                received += len(chunk)
                if received > ceiling:
                    raise TransferContentTooLargeError(ceiling)
                digest.update(chunk)
                staged.write(chunk)
    except (TransferContentTooLargeError, ClientDisconnect):
        store.fail_upload(entry.transfer_id)
        raise
    except Exception:
        store.fail_upload(entry.transfer_id)
        raise
    store.finish_upload(entry.transfer_id, size=received, sha256=digest.hexdigest())
    return TransferUploadResult(
        transfer_id=entry.transfer_id, size=received, sha256=digest.hexdigest()
    )


def _spool_chunks(entry: PendingTransfer) -> Iterator[bytes]:
    """Stream a spooled projection file, reclaiming the staging dir after."""
    try:
        with entry.spool_path.open("rb") as spool:
            while chunk := spool.read(_SPOOL_CHUNK_BYTES):
                yield chunk
    finally:
        entry.cleanup()


@router.get(
    "/download/{transfer_id}",
    responses={
        410: {
            "model": ErrorResponse,
            "description": (
                "`transfer_token_invalid`: the token names no redeemable "
                "pending transfer (unknown, expired, already redeemed, or "
                "scoped to a different direction)."
            ),
        },
    },
)
async def transfer_download(
    transfer_id: str,
    request: Request,
    x_download_token: str = Header(default="", alias="X-Download-Token"),
    store: TransferStore = Depends(get_transfer_service),
) -> StreamingResponse:
    """Fetch a pending transfer's bytes against a one-time download token.

    Redemption is one-time and happens before any bytes flow, so every
    failure mode arrives as a structured JSON envelope, never a truncated
    stream. Source downloads chunk straight from the vault-source store;
    projection downloads stream the spool written at mint time. The response
    carries the exact Content-Length the recipe promised.
    """
    entry = store.redeem_download(x_download_token, transfer_id=transfer_id)

    chunks: Iterator[bytes] | AsyncIterator[bytes]
    if entry.kind == "projection":
        chunks = _spool_chunks(entry)
    else:
        registry = request.app.state.vault_registry
        services = registry.get(entry.vault_id)
        if services is None:
            raise VaultNotFoundError(entry.vault_id)
        from sage.mcp_init import get_stack_config, resolve_stack_vault_source_store

        source_store = resolve_stack_vault_source_store(get_stack_config())
        storage_root = Path(services.config.vault.storage_root).expanduser().resolve()
        if not source_store.source_exists(entry.vault_id, storage_root, entry.source_path):
            raise ContentFileMissingError(entry.document_id, entry.source_path)
        chunks = source_store.iter_source(entry.vault_id, storage_root, entry.source_path)

    filename = entry.filename.replace("\\", "_").replace('"', "_")
    disposition = f'attachment; filename="{filename}"'
    if not filename.isascii():
        disposition = (
            f'attachment; filename="{filename.encode("ascii", "replace").decode()}"; '
            f"filename*=UTF-8''{quote(filename)}"
        )
    return StreamingResponse(
        chunks,
        media_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
        headers={
            "Content-Disposition": disposition,
            "Content-Length": str(entry.content_size),
        },
    )
