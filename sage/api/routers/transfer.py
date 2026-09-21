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
    SourceDigestMismatchError,
    TransferContentTooLargeError,
    VaultNotFoundError,
)
from sage.api.response_docs import boundary_400
from sage.api.wire_route import WireRoute
from sage.models.schemas import ErrorResponse, TransferUploadResult
from sage.services.transfer import (
    PendingTransfer,
    TransferStore,
    max_transfer_bytes,
)

router = APIRouter(route_class=WireRoute, tags=["Transfer"])

_SPOOL_CHUNK_BYTES = 65536


@router.put(
    "/upload",
    status_code=201,
    responses={
        400: boundary_400(
            request=("unknown_parameter",),
            extra=(
                "`source_digest_mismatch`: the token was minted against a declared "
                "`sha256` and the delivered bytes have a different digest. Nothing "
                "is staged and the token stays retryable until its refusal limit is "
                "reached. The detail names the transfer and the delivered digest, "
                "never the bound one."
            ),
        ),
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
                "to a different direction). "
                "`transfer_refusal_limit_reached`: this delivery was refused, "
                "and it was the token's last: the token has now been refused "
                "`transfer.max_refused_deliveries` deliveries (default 3), so "
                "the transfer is reclaimed and a fresh recipe must be minted by "
                "re-issuing the originating call. A body over the ceiling, "
                "bytes not matching a bound digest, and a body abandoned "
                "mid-stream each count as a refusal."
            ),
        },
        413: {
            "model": ErrorResponse,
            "description": (
                "`transfer_content_too_large`: the body exceeded the transfer "
                "ceiling and the delivery was aborted; the partial staging "
                "file is removed and the token stays retryable until its "
                "refusal limit is reached."
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
    delivery rolls the transfer back to a retryable state. A token minted
    against a digest admits only bytes with that digest; any others are
    refused the same way, staging nothing and spending nothing. Refusals are
    bounded per token: the one that reaches the configured limit reclaims
    the transfer, so a disclosed token cannot be used to stream up to the
    ceiling for as long as it lives. A failure on the server's side is not
    the presenter's refusal and does not count. The receipt
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
        # Only once the whole body is in can its digest be known, so a bound
        # token's refusal lands after the bytes reached staging -- inside this
        # block, so the rollback below removes them before anything records
        # the delivery.
        store.check_bound_digest(entry.transfer_id, digest.hexdigest())
    except TransferContentTooLargeError, SourceDigestMismatchError, ClientDisconnect:
        store.refuse_upload(entry.transfer_id)
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
        400: boundary_400(request=("unknown_parameter",)),
        410: {
            "model": ErrorResponse,
            "description": (
                "`transfer_token_invalid`: the token names no redeemable "
                "pending transfer (unknown, expired, already redeemed, or "
                "scoped to a different direction)."
            ),
        },
        502: {
            "model": ErrorResponse,
            "description": (
                "`vault_source_store_refused`: the vault-source store declined the "
                "operation on its merits -- quota, a permission it withdrew, a reply "
                "that could not be used. Resolve it at the store before retrying; "
                "`detail.store_status` carries the status it declined with."
            ),
        },
        503: {
            "model": ErrorResponse,
            "description": (
                "`vault_source_store_unavailable`: the vault-source store declined to "
                "serve the operation just now -- throttling, or a transient backend "
                "signal. The same request may succeed on a later attempt."
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

    Redemption is one-time and happens before any bytes flow, as does the
    source's presence check, so those failures arrive as a structured JSON
    envelope. A store refusal raised once the bytes are already flowing ends
    the body short of the promised Content-Length instead. Source downloads
    chunk straight from the vault-source store; projection downloads stream
    the spool written at mint time, and reach no store at all.

    A refusal spends the token, which the contract already answers for: a
    failed fetch is retried by re-issuing the originating call. There is
    nothing to hand back the way a refused *upload* returns its token -- that
    one protects bytes the caller already paid to send, where a download
    token is re-minted for free and, on this leg, spools nothing.
    """
    entry = store.redeem_download(x_download_token, transfer_id=transfer_id)

    chunks: Iterator[bytes] | AsyncIterator[bytes]
    if entry.kind == "projection":
        chunks = _spool_chunks(entry)
    else:
        registry = request.app.state.vault_registry
        services = registry.get(entry.vault_id)
        if services is None:
            raise VaultNotFoundError(entry.vault_id, available_vaults=registry)
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
