"""Pending-transfer store for the caller-local byte channel.

Under the cloud profile the SAGE server cannot see the calling client's
filesystem, so the path-bearing tools cannot move file bytes themselves.
Instead, the authenticated call mints a short-lived, one-time,
direction-scoped transfer token here and returns a structured recipe; the
caller's environment delivers or fetches the bytes against the token-gated
transfer endpoints, and (for uploads) an authenticated completion call
redeems the token against the staged bytes. The bytes therefore ride an
ordinary HTTP body between the caller's machine and this process -- never a
tool-call payload -- while the tool contract above stays profile-invariant
(CAS-ADR-042 constraint 1).

The store is process-local by design: entries live minutes, secure only one
exchange each, and die harmlessly with the process. The deployment pins the
server to a single replica for the same reason the ingestion queue and
document locks are in-process; a horizontally scaled deployment would need to
externalize this state.

Tokens are composite (``<transfer_id>.<secret>``) so the upload endpoint can
identify the pending transfer from the token header alone, and they are never
stored at rest -- only their SHA-256 digests -- so the store's contents cannot
be replayed. Redemption compares digests in constant time.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal

from sage.api.errors import (
    TransferAlreadyStagedError,
    TransferNotStagedError,
    TransferTokenInvalidError,
)

#: Header carrying the upload token on the transfer endpoint's PUT leg.
UPLOAD_TOKEN_HEADER = "X-Upload-Token"  # noqa: S105 -- header *name*, not a credential

#: Header carrying the download token on the transfer endpoint's GET leg.
DOWNLOAD_TOKEN_HEADER = "X-Download-Token"  # noqa: S105 -- header *name*, not a credential

#: Ceiling on a single transfer body. Its own knob, separate from the
#: export-side inline-content ceiling in ``sage.services.documents``: this
#: bounds a file moved through the transfer endpoints, not a base64-inlined
#: tool response. Overridable via ``SAGE_MAX_TRANSFER_BYTES``.
DEFAULT_MAX_TRANSFER_BYTES = 100 * 1024 * 1024


def max_transfer_bytes() -> int:
    """Return the transfer byte ceiling, honoring the env override."""
    raw = os.environ.get("SAGE_MAX_TRANSFER_BYTES")
    if raw is None:
        return DEFAULT_MAX_TRANSFER_BYTES
    return int(raw)


def staging_name(filename: str | None, fallback: str) -> str:
    """Reduce a caller-supplied filename to a safe basename for temp staging.

    ``Path(...).name`` strips any directory components, so a path-shaped
    filename cannot escape the staging directory. Degenerate inputs whose
    basename is empty or a directory reference (``""``, ``"."``, ``".."``)
    fall back to the synthetic name rather than resolving to the staging
    directory itself and failing with an unstructured OS error.
    """
    name = Path(filename).name if filename else ""
    if name in ("", ".", ".."):
        return fallback
    return name


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class MintedTransfer:
    """What a mint hands back to the recipe builder: the only place the
    bearer token exists in the clear."""

    transfer_id: str
    token: str
    expires_at: datetime
    content_hash: str | None = None
    content_size: int | None = None


@dataclass
class PendingTransfer:
    """One in-flight transfer, keyed by its public transfer id.

    Holds the token digest (never the token), the direction and vault the
    token is scoped to, and the per-direction payload: uploads carry a
    staging directory the endpoint streams into; downloads carry either the
    retained source's vault-relative path or a spooled projection file.
    """

    transfer_id: str
    token_digest: str
    direction: Literal["upload", "download"]
    vault_id: str
    expires_at: datetime
    staging_dir: Path
    filename: str = ""
    #: The path the caller named when this upload was minted, from which
    #: ``filename`` is derived. Redemption substitutes the staged path for it
    #: everywhere downstream, so it is kept here for the one consumer that must
    #: not report a server-side location -- see ``IngestionService.ingest``'s
    #: ``caller_source``. Empty only on a download entry, which has no such path.
    declared_source: str = ""
    state: Literal["pending_bytes", "streaming", "bytes_staged"] = "pending_bytes"
    staged_size: int | None = None
    staged_sha256: str | None = None
    kind: Literal["", "source", "projection"] = ""
    document_id: str = ""
    source_path: str = ""
    content_hash: str = ""
    content_size: int = 0
    spool_path: Path | None = field(default=None)

    @property
    def staged_path(self) -> Path:
        """Where the upload endpoint streams this transfer's bytes."""
        return self.staging_dir / self.filename

    def cleanup(self) -> None:
        """Remove the staging directory; safe to call more than once."""
        shutil.rmtree(self.staging_dir, ignore_errors=True)


class TransferStore:
    """Process-local registry of pending transfers.

    All state transitions run under one lock; the critical sections are
    synchronous dict operations, so the store is safe to call from async
    request handlers and synchronous tool code alike. Expired entries are
    reclaimed lazily -- every public operation sweeps first -- so no
    background task is needed for a population that lives minutes.
    """

    def __init__(
        self,
        now: Callable[[], datetime] = _utcnow,
        staging_root: Path | None = None,
    ) -> None:
        self._now = now
        self._staging_root = staging_root
        self._entries: dict[str, PendingTransfer] = {}
        self._lock = threading.Lock()

    # -- minting ---------------------------------------------------------

    def mint_upload(self, vault_id: str, source: str, ttl_seconds: int) -> MintedTransfer:
        """Mint an upload token bound to one vault and one caller-named source.

        Takes the path the caller named and derives the staged basename from it,
        rather than accepting the two spellings separately. The entry needs both
        — the basename to stage under, the caller's path to name in a refusal —
        and they are the same value at different reductions, so deriving one
        from the other is what keeps them from disagreeing and makes an entry
        without a caller-facing spelling unrepresentable.
        """
        with self._lock:
            self._sweep_locked()
            minted, entry = self._new_entry_locked(
                direction="upload", vault_id=vault_id, ttl_seconds=ttl_seconds
            )
            entry.filename = staging_name(source, "transfer_source")
            entry.declared_source = source
        return minted

    def mint_download_source(
        self,
        vault_id: str,
        document_id: str,
        source_path: str,
        filename: str,
        content_hash: str,
        content_size: int,
        ttl_seconds: int,
    ) -> MintedTransfer:
        """Mint a download token for a retained source file.

        The bytes stay in the vault-source store until redemption; the entry
        records only the vault-relative path the endpoint will stream from.
        """
        with self._lock:
            self._sweep_locked()
            minted, entry = self._new_entry_locked(
                direction="download", vault_id=vault_id, ttl_seconds=ttl_seconds
            )
            entry.kind = "source"
            entry.document_id = document_id
            entry.source_path = source_path
            entry.filename = staging_name(filename, "download")
            entry.content_hash = content_hash
            entry.content_size = content_size
        minted.content_hash = content_hash
        minted.content_size = content_size
        return minted

    def mint_download_projection(
        self,
        vault_id: str,
        document_id: str,
        filename: str,
        text: str,
        ttl_seconds: int,
    ) -> MintedTransfer:
        """Mint a download token for a projection, spooled at mint time.

        The projection text is materialized into the entry's staging
        directory immediately, so redemption streams a stable byte sequence
        whose hash and size the recipe already promised.
        """
        raw = text.encode("utf-8")
        content_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
        with self._lock:
            self._sweep_locked()
            minted, entry = self._new_entry_locked(
                direction="download", vault_id=vault_id, ttl_seconds=ttl_seconds
            )
            entry.kind = "projection"
            entry.document_id = document_id
            entry.filename = staging_name(filename, "projection.md")
            entry.content_hash = content_hash
            entry.content_size = len(raw)
            spool = entry.staging_dir / entry.filename
            spool.write_bytes(raw)
            entry.spool_path = spool
        minted.content_hash = content_hash
        minted.content_size = len(raw)
        return minted

    # -- upload leg ------------------------------------------------------

    def begin_upload(self, token: str) -> PendingTransfer:
        """Validate an upload token and open the entry for byte delivery."""
        with self._lock:
            self._sweep_locked()
            entry = self._validated_locked(token, "upload")
            if entry.state != "pending_bytes":
                raise TransferAlreadyStagedError(entry.transfer_id)
            entry.state = "streaming"
            return entry

    def finish_upload(self, transfer_id: str, size: int, sha256: str) -> None:
        """Record a completed byte delivery; the entry now awaits consumption."""
        with self._lock:
            entry = self._entries.get(transfer_id)
            if entry is None or entry.state != "streaming":
                raise TransferTokenInvalidError()
            entry.state = "bytes_staged"
            entry.staged_size = size
            entry.staged_sha256 = sha256

    def fail_upload(self, transfer_id: str) -> None:
        """Roll a failed byte delivery back to a retryable state."""
        with self._lock:
            entry = self._entries.get(transfer_id)
            if entry is None:
                return
            entry.staged_path.unlink(missing_ok=True)
            entry.state = "pending_bytes"
            entry.staged_size = None
            entry.staged_sha256 = None

    def consume_upload(self, token: str, vault_id: str) -> PendingTransfer:
        """Redeem an upload token against its staged bytes and pop the entry.

        The caller owns the returned entry's staging directory and must
        ``cleanup()`` after ingesting the staged file.
        """
        with self._lock:
            self._sweep_locked()
            entry = self._validated_locked(token, "upload", vault_id)
            if entry.state != "bytes_staged":
                raise TransferNotStagedError(entry.transfer_id)
            return self._entries.pop(entry.transfer_id)

    # -- download leg ----------------------------------------------------

    def redeem_download(self, token: str, transfer_id: str | None = None) -> PendingTransfer:
        """Redeem a download token and pop the entry (one-time at redemption).

        ``transfer_id``, when given (the download URL carries it), must agree
        with the token's own id; a mismatch refuses without consuming the
        token. For projection downloads the caller must ``cleanup()`` the
        returned entry after streaming its spool file.
        """
        with self._lock:
            self._sweep_locked()
            entry = self._validated_locked(token, "download")
            if transfer_id is not None and transfer_id != entry.transfer_id:
                raise TransferTokenInvalidError()
            return self._entries.pop(entry.transfer_id)

    # -- internals -------------------------------------------------------

    def _new_entry_locked(
        self, direction: Literal["upload", "download"], vault_id: str, ttl_seconds: int
    ) -> tuple[MintedTransfer, PendingTransfer]:
        transfer_id = secrets.token_urlsafe(8)
        while transfer_id in self._entries:
            transfer_id = secrets.token_urlsafe(8)
        # Composite token: the id half routes (the upload endpoint has only
        # the header to identify the entry); the secret half authenticates.
        token = f"{transfer_id}.{secrets.token_urlsafe(32)}"
        expires_at = self._now() + timedelta(seconds=ttl_seconds)
        if self._staging_root is not None:
            self._staging_root.mkdir(parents=True, exist_ok=True)
        staging_dir = Path(
            tempfile.mkdtemp(
                prefix="sage-transfer-",
                dir=str(self._staging_root) if self._staging_root else None,
            )
        )
        entry = PendingTransfer(
            transfer_id=transfer_id,
            token_digest=_digest(token),
            direction=direction,
            vault_id=vault_id,
            expires_at=expires_at,
            staging_dir=staging_dir,
        )
        self._entries[transfer_id] = entry
        return MintedTransfer(transfer_id, token, expires_at), entry

    def _validated_locked(
        self,
        token: str,
        direction: Literal["upload", "download"],
        vault_id: str | None = None,
    ) -> PendingTransfer:
        transfer_id, _, _secret = token.partition(".")
        entry = self._entries.get(transfer_id) if _secret else None
        if (
            entry is None
            or not hmac.compare_digest(entry.token_digest, _digest(token))
            or entry.direction != direction
            or (vault_id is not None and entry.vault_id != vault_id)
        ):
            raise TransferTokenInvalidError()
        return entry

    def _sweep_locked(self) -> None:
        now = self._now()
        expired = [tid for tid, e in self._entries.items() if e.expires_at <= now]
        for tid in expired:
            entry = self._entries.pop(tid)
            shutil.rmtree(entry.staging_dir, ignore_errors=True)


# Process-wide store, resolved lazily the same way the stack config is
# (one instance per process; tests reset it around each case).
_transfer_store: TransferStore | None = None


def get_transfer_store() -> TransferStore:
    """Return the process-wide transfer store, creating it on first use."""
    global _transfer_store
    if _transfer_store is None:
        _transfer_store = TransferStore()
    return _transfer_store


def reset_transfer_store() -> None:
    """Drop the process-wide store; staged state is reclaimed by TTL sweep."""
    global _transfer_store
    _transfer_store = None


# -- recipe builders -------------------------------------------------------
#
# The tool layer calls these where it would otherwise touch a caller-local
# path it cannot reach. Each builder mints against the process-wide store
# using the stack config's transfer coordinates and returns the structured
# recipe the caller's environment executes verbatim.


def _transfer_coordinates() -> tuple[str, int]:
    """Resolve (public base URL, token TTL) from the stack config.

    Raises :class:`sage.api.errors.TransferEndpointNotConfiguredError` when
    the deployment declares no public base URL -- minting a recipe whose URL
    cannot work would strand the caller, so the gap fails loud here.
    """
    from sage.api.errors import TransferEndpointNotConfiguredError
    from sage.mcp_init import get_stack_config

    cfg = get_stack_config().transfer
    if not cfg.public_base_url:
        raise TransferEndpointNotConfiguredError()
    return cfg.public_base_url.rstrip("/"), cfg.token_ttl_seconds


def mint_upload_recipe(vault_id: str, sources: list[str]):
    """Mint one upload leg per caller-local source and build the recipe."""
    from sage.models.schemas import UploadRecipe, UploadRecipeItem

    base_url, ttl_seconds = _transfer_coordinates()
    store = get_transfer_store()
    items: list[UploadRecipeItem] = []
    expires_at = None
    for source in sources:
        minted = store.mint_upload(vault_id, source, ttl_seconds)
        expires_at = minted.expires_at
        items.append(
            UploadRecipeItem(
                source=source,
                transfer_id=minted.transfer_id,
                token=minted.token,
                url=f"{base_url}/upload",
            )
        )
    return UploadRecipe(
        expires_at=expires_at,
        max_bytes=max_transfer_bytes(),
        uploads=items,
    )


def mint_download_recipe_for_source(
    vault_id: str,
    document_id: str,
    source_path: str,
    content_hash: str,
    content_size: int,
    write_to_path: str,
):
    """Mint a download recipe for a retained source file."""
    from sage.models.schemas import DownloadRecipe

    base_url, ttl_seconds = _transfer_coordinates()
    filename = staging_name(source_path, "download")
    minted = get_transfer_store().mint_download_source(
        vault_id,
        document_id=document_id,
        source_path=source_path,
        filename=filename,
        content_hash=content_hash,
        content_size=content_size,
        ttl_seconds=ttl_seconds,
    )
    return DownloadRecipe(
        url=f"{base_url}/download/{minted.transfer_id}",
        token=minted.token,
        transfer_id=minted.transfer_id,
        expires_at=minted.expires_at,
        content_hash=content_hash,
        content_size=content_size,
        filename=filename,
        write_to_path=write_to_path,
    )


def mint_download_recipe_for_projection(
    vault_id: str,
    document_id: str,
    text: str,
    write_to_path: str,
):
    """Mint a download recipe for a projection, spooled at mint time."""
    from sage.models.schemas import DownloadRecipe

    base_url, ttl_seconds = _transfer_coordinates()
    filename = f"{document_id}.md"
    minted = get_transfer_store().mint_download_projection(
        vault_id,
        document_id=document_id,
        filename=filename,
        text=text,
        ttl_seconds=ttl_seconds,
    )
    return DownloadRecipe(
        url=f"{base_url}/download/{minted.transfer_id}",
        token=minted.token,
        transfer_id=minted.transfer_id,
        expires_at=minted.expires_at,
        content_hash=minted.content_hash,
        content_size=minted.content_size,
        filename=filename,
        write_to_path=write_to_path,
    )
