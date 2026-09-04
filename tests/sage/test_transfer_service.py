"""Unit tests for the transfer-token store behind the caller-local byte channel.

Under the cloud profile, path-bearing MCP tools mint short-lived, one-time,
direction-scoped transfer tokens and return structured recipes; the caller's
environment moves the bytes against the token-gated transfer endpoints. These
tests pin the store's contract in isolation -- no database, no HTTP, an
injected clock -- so every lifecycle rule (direction and vault scoping, expiry,
one-time redemption, retry-after-failure, digest-at-rest) is proven at the
service seam before the tool and endpoint layers build on it.

Tokens are composite (``<transfer_id>.<secret>``) because the upload endpoint
identifies the pending transfer from the token header alone; the tests
exercise the store through the composite form exactly as the endpoints do.
"""

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from sage.api.errors import (
    TransferAlreadyStagedError,
    TransferNotStagedError,
    TransferTokenInvalidError,
)
from sage.services.transfer import (
    TransferStore,
    get_transfer_store,
    reset_transfer_store,
    staging_name,
)

_VAULT = "vault_a"


class _Clock:
    """Controllable clock injected into the store."""

    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def store(clock, tmp_path) -> TransferStore:
    return TransferStore(now=clock, staging_root=tmp_path / "staging")


def _stage_bytes(store: TransferStore, minted, body: bytes) -> None:
    """Drive the PUT-side lifecycle the way the upload endpoint does."""
    entry = store.begin_upload(minted.token)
    entry.staged_path.parent.mkdir(parents=True, exist_ok=True)
    entry.staged_path.write_bytes(body)
    store.finish_upload(
        minted.transfer_id,
        size=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
    )


class TestUploadLifecycle:
    def test_mint_begin_finish_consume_round_trip(self, store):
        minted = store.mint_upload(_VAULT, "notes.md", ttl_seconds=300)
        assert minted.token.startswith(minted.transfer_id + ".")
        body = b"caller bytes"
        _stage_bytes(store, minted, body)

        entry = store.consume_upload(minted.token, _VAULT)
        assert entry.staged_path.read_bytes() == body
        assert entry.staged_size == len(body)
        assert entry.staged_sha256 == hashlib.sha256(body).hexdigest()
        # Popped at consumption: the token cannot be presented again.
        with pytest.raises(TransferTokenInvalidError):
            store.consume_upload(minted.token, _VAULT)
        entry.cleanup()
        assert not entry.staging_dir.exists()

    def test_staging_filename_is_the_minted_basename(self, store):
        minted = store.mint_upload(_VAULT, "../escape.md", ttl_seconds=300)
        entry = store.begin_upload(minted.token)
        # Path-shaped filenames cannot escape the staging directory.
        assert entry.staged_path.parent == entry.staging_dir
        assert entry.staged_path.name == "escape.md"

    @pytest.mark.parametrize(
        "mangle",
        [
            lambda t: t[:-1] + ("A" if t[-1] != "A" else "B"),  # wrong secret
            lambda t: "no-dot-token",  # malformed: no separator
            lambda t: "unknown." + t.split(".", 1)[1],  # unknown transfer id
            lambda t: "",  # empty
        ],
    )
    def test_wrong_token_refused(self, store, mangle):
        minted = store.mint_upload(_VAULT, "notes.md", ttl_seconds=300)
        with pytest.raises(TransferTokenInvalidError):
            store.begin_upload(mangle(minted.token))

    def test_direction_scoping_upload_token_cannot_download(self, store):
        minted = store.mint_upload(_VAULT, "notes.md", ttl_seconds=300)
        with pytest.raises(TransferTokenInvalidError):
            store.redeem_download(minted.token)

    def test_vault_scoping_on_consume(self, store):
        minted = store.mint_upload(_VAULT, "notes.md", ttl_seconds=300)
        _stage_bytes(store, minted, b"x")
        with pytest.raises(TransferTokenInvalidError):
            store.consume_upload(minted.token, "vault_b")

    def test_expiry_refuses_and_sweep_removes_staging(self, store, clock):
        minted = store.mint_upload(_VAULT, "notes.md", ttl_seconds=300)
        entry = store.begin_upload(minted.token)
        staging_dir = entry.staging_dir
        store.fail_upload(minted.transfer_id)
        assert staging_dir.exists()

        clock.advance(301)
        with pytest.raises(TransferTokenInvalidError):
            store.begin_upload(minted.token)
        # The refusal's sweep reclaimed the expired entry's staging dir.
        assert not staging_dir.exists()

    def test_second_put_after_staging_refused(self, store):
        minted = store.mint_upload(_VAULT, "notes.md", ttl_seconds=300)
        _stage_bytes(store, minted, b"first")
        with pytest.raises(TransferAlreadyStagedError):
            store.begin_upload(minted.token)

    def test_retry_after_failed_put(self, store):
        minted = store.mint_upload(_VAULT, "notes.md", ttl_seconds=300)
        entry = store.begin_upload(minted.token)
        entry.staged_path.write_bytes(b"partial")
        store.fail_upload(minted.transfer_id)
        # The partial file is gone and the token is retryable within TTL.
        assert not entry.staged_path.exists()
        _stage_bytes(store, minted, b"complete")
        consumed = store.consume_upload(minted.token, _VAULT)
        assert consumed.staged_path.read_bytes() == b"complete"
        consumed.cleanup()

    def test_returned_upload_redeems_again_with_its_bytes(self, store):
        """A redeemed entry handed back redeems again against the same staged
        file, so work that failed after redemption costs no second byte leg.

        Anti-coincidental-pass: the second consume is asserted to yield the
        *original* bytes, so a return that reset the entry the way
        ``fail_upload`` does -- deleting the staged file and reopening the
        entry for another PUT -- fails on the read rather than on the consume.
        ``expires_at`` is asserted unchanged so a return that refreshed the
        window (letting a repeatedly-failing call outlive the sweep) fails.
        """
        minted = store.mint_upload(_VAULT, "notes.md", ttl_seconds=300)
        _stage_bytes(store, minted, b"delivered")
        consumed = store.consume_upload(minted.token, _VAULT)
        expires_at = consumed.expires_at
        assert minted.transfer_id not in store._entries

        store.return_upload(consumed)

        assert store._entries[minted.transfer_id].expires_at == expires_at
        again = store.consume_upload(minted.token, _VAULT)
        assert again.staged_path.read_bytes() == b"delivered"
        again.cleanup()

    def test_returned_upload_still_expires_on_its_original_schedule(self, store, clock):
        """A returned token is the one that was minted, not a fresh one: once
        its original window closes the sweep reclaims it like any other.

        Anti-coincidental-pass: the paired positive above proves the return
        works at all, so a failure here isolates the clock rather than the
        mechanism.
        """
        minted = store.mint_upload(_VAULT, "notes.md", ttl_seconds=300)
        _stage_bytes(store, minted, b"delivered")
        consumed = store.consume_upload(minted.token, _VAULT)
        staging_dir = consumed.staging_dir
        store.return_upload(consumed)

        clock.advance(301)
        with pytest.raises(TransferTokenInvalidError):
            store.consume_upload(minted.token, _VAULT)
        assert not staging_dir.exists()

    def test_consume_before_staged(self, store):
        minted = store.mint_upload(_VAULT, "notes.md", ttl_seconds=300)
        with pytest.raises(TransferNotStagedError):
            store.consume_upload(minted.token, _VAULT)

    def test_token_secret_not_stored_at_rest(self, store):
        minted = store.mint_upload(_VAULT, "notes.md", ttl_seconds=300)
        secret = minted.token.split(".", 1)[1]
        entry = store._entries[minted.transfer_id]
        for value in vars(entry).values():
            assert not (isinstance(value, str) and secret in value)


class TestDownloadLifecycle:
    def test_source_mint_and_redeem_round_trip(self, store):
        minted = store.mint_download_source(
            _VAULT,
            document_id="abcd1234_doc",
            source_path="imports/notes.md",
            filename="notes.md",
            content_hash="sha256:" + hashlib.sha256(b"x").hexdigest(),
            content_size=1,
            ttl_seconds=300,
        )
        entry = store.redeem_download(minted.token, transfer_id=minted.transfer_id)
        assert entry.kind == "source"
        assert entry.vault_id == _VAULT
        assert entry.source_path == "imports/notes.md"
        # One-time: a second redemption fails.
        with pytest.raises(TransferTokenInvalidError):
            store.redeem_download(minted.token)

    def test_redeem_with_mismatched_path_id_refused(self, store):
        minted = store.mint_download_source(
            _VAULT,
            document_id="abcd1234_doc",
            source_path="imports/notes.md",
            filename="notes.md",
            content_hash="sha256:00",
            content_size=1,
            ttl_seconds=300,
        )
        # A URL transfer id that disagrees with the token's own id is refused
        # -- and the refusal must not consume the token.
        with pytest.raises(TransferTokenInvalidError):
            store.redeem_download(minted.token, transfer_id="somethingelse")
        entry = store.redeem_download(minted.token, transfer_id=minted.transfer_id)
        assert entry.kind == "source"

    def test_projection_mint_spools_text_and_reports_hash(self, store):
        text = "# Projection\n\nbody\n"
        minted = store.mint_download_projection(
            _VAULT,
            document_id="abcd1234_doc",
            filename="abcd1234_doc.md",
            text=text,
            ttl_seconds=300,
        )
        raw = text.encode("utf-8")
        assert minted.content_size == len(raw)
        assert minted.content_hash == "sha256:" + hashlib.sha256(raw).hexdigest()
        entry = store.redeem_download(minted.token)
        assert entry.kind == "projection"
        assert entry.spool_path is not None
        assert entry.spool_path.read_bytes() == raw
        entry.cleanup()
        assert not entry.staging_dir.exists()

    def test_download_token_cannot_upload(self, store):
        minted = store.mint_download_source(
            _VAULT,
            document_id="abcd1234_doc",
            source_path="imports/notes.md",
            filename="notes.md",
            content_hash="sha256:00",
            content_size=1,
            ttl_seconds=300,
        )
        with pytest.raises(TransferTokenInvalidError):
            store.begin_upload(minted.token)

    def test_expired_download_refused(self, store, clock):
        minted = store.mint_download_source(
            _VAULT,
            document_id="abcd1234_doc",
            source_path="imports/notes.md",
            filename="notes.md",
            content_hash="sha256:00",
            content_size=1,
            ttl_seconds=60,
        )
        clock.advance(61)
        with pytest.raises(TransferTokenInvalidError):
            store.redeem_download(minted.token)


class TestModuleSurface:
    def test_singleton_accessor_round_trip(self):
        reset_transfer_store()
        try:
            store = get_transfer_store()
            assert get_transfer_store() is store
        finally:
            reset_transfer_store()

    def test_staging_name_strips_directories_and_degenerates(self):
        assert staging_name("/tmp/a/b/notes.md", "fallback") == "notes.md"
        assert staging_name("..", "fallback") == "fallback"
        assert staging_name("", "fallback") == "fallback"
        assert staging_name(None, "fallback") == "fallback"
