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

import contextlib
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import sage.mcp_init as _mcp_init
import sage.services.transfer as _transfer
from sage.api.errors import (
    AmbiguousIngestSourceError,
    MissingIngestSourceError,
    TransferAlreadyStagedError,
    TransferNotStagedError,
    TransferTokenInvalidError,
)
from sage.config import SageCoreConfig
from sage.services.caller_paths import caller_basename
from sage.services.transfer import (
    DeliveryDeclaration,
    TransferStore,
    caller_local_delivery,
    get_transfer_store,
    mint_upload_recipe,
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


class _SteppingClock(_Clock):
    """A clock that advances on every read.

    Where a caller mints several tokens inside one call, the test cannot
    interject between them to move an ordinary clock, so the legs all land on
    the same instant and any difference between them is unobservable. Stepping
    on read gives each leg a distinct window without the test having to know
    how many times a mint reads the clock.
    """

    def __init__(self, step_seconds: float = 1.0) -> None:
        super().__init__()
        self._step = step_seconds

    def __call__(self) -> datetime:
        reading = self.now
        self.advance(self._step)
        return reading


@contextlib.contextmanager
def _profile(name: str):
    """Pin the stack config to a profile that can mint, and restore it after."""
    saved = _mcp_init._stack_config
    _mcp_init.set_stack_config(
        SageCoreConfig(profile=name, transfer={"public_base_url": "https://sage.test.example"})
    )
    try:
        yield
    finally:
        _mcp_init.set_stack_config(saved)


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

    def test_token_redeemable_one_second_inside_its_window(self, store, clock):
        """A token one second short of its expiry instant still redeems.

        Anti-coincidental-pass: paired with the exact-instant arm below.
        Neither arm alone can tell which side of the sweep's comparison
        moved -- a store that expired everything, or nothing, would satisfy
        one of them. Widening the sweep to reclaim ahead of ``expires_at``
        reds this arm and leaves its partner green.
        """
        minted = store.mint_upload(_VAULT, "notes.md", ttl_seconds=300)

        clock.advance(299)

        entry = store.begin_upload(minted.token)
        assert entry.transfer_id == minted.transfer_id

    def test_token_refused_exactly_at_its_expiry_instant(self, store, clock):
        """``expires_at`` is the first instant a token no longer redeems.

        Anti-coincidental-pass: the partner arm above proves the token was
        live a second earlier, so this arm isolates the boundary rather than
        the mechanism. Relaxing the sweep's ``<=`` to ``<`` reds this arm
        alone.
        """
        minted = store.mint_upload(_VAULT, "notes.md", ttl_seconds=300)
        staging_dir = store._entries[minted.transfer_id].staging_dir

        clock.advance(300)

        with pytest.raises(TransferTokenInvalidError):
            store.begin_upload(minted.token)
        assert not staging_dir.exists()

    def test_expired_upload_is_not_resumable_on_the_byte_leg(self, store, clock):
        """An expired token cannot be resumed: the sweep took its bytes.

        Delivered bytes are as unrecoverable as undelivered ones once the
        window closes, which is why the remedy is re-issuing the originating
        call rather than redeeming the lapsed token again.

        Anti-coincidental-pass: the bytes are staged before the clock moves,
        so a store that merely lost the lookup while keeping the staging
        directory fails on the directory assertion rather than satisfying a
        bare ``pytest.raises``.

        The completion leg is a separate test rather than a second arm here,
        and the separation is load-bearing: the sweep reclaims the whole
        entry table, so whichever verb runs first pops the other's entry too,
        and the second arm would then raise on absence whatever its own
        method does. Two tests means two stores, so each verb has to sweep
        for itself.
        """
        minted = store.mint_upload(_VAULT, "notes.md", ttl_seconds=300)
        _stage_bytes(store, minted, b"delivered")
        staging_dir = store._entries[minted.transfer_id].staging_dir
        assert staging_dir.exists()

        clock.advance(301)

        with pytest.raises(TransferTokenInvalidError):
            store.begin_upload(minted.token)
        assert minted.transfer_id not in store._entries
        assert not staging_dir.exists()

    def test_expired_upload_is_not_resumable_on_the_completion_leg(self, store, clock):
        """The completion verb reclaims an expired entry on its own.

        Paired with the byte-leg test above; see its docstring for why the
        two cannot share a store. Removing ``consume_upload``'s sweep reds
        this test and leaves its partner green.
        """
        minted = store.mint_upload(_VAULT, "notes.md", ttl_seconds=300)
        _stage_bytes(store, minted, b"delivered")
        staging_dir = store._entries[minted.transfer_id].staging_dir
        assert staging_dir.exists()

        clock.advance(301)

        with pytest.raises(TransferTokenInvalidError):
            store.consume_upload(minted.token, _VAULT)
        assert minted.transfer_id not in store._entries
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

    def test_caller_basename_reduces_a_windows_spelling_without_breaking_posix(self):
        """The basename half of the caller-vs-server platform question.

        ``staging_name`` above keeps this platform's separators, which is
        right for the server-owned names it now serves. A caller-authored
        path needs the other reading: ``PurePosixPath`` finds no separator in
        a drive-letter or UNC spelling and hands back the whole path, which
        becomes the staged and then the retained name.

        The last two cases are the reason the Windows reading is applied
        conditionally rather than always. A backslash is a legal character in
        a POSIX filename, so reducing under both flavours unconditionally
        would truncate ``we\\ird.md`` to ``ird.md`` -- correct-looking, and
        wrong. The condition is "absolute under Windows and not under POSIX",
        which no POSIX-absolute path satisfies.
        """
        assert caller_basename(r"C:\docs\note.md", "fallback") == "note.md"
        assert caller_basename(r"\\fileserver\share\note.md", "fallback") == "note.md"
        assert caller_basename("/tmp/a/b/notes.md", "fallback") == "notes.md"
        assert caller_basename("docs/notes.md", "fallback") == "notes.md"
        assert caller_basename("..", "fallback") == "fallback"
        assert caller_basename("", "fallback") == "fallback"
        assert caller_basename(None, "fallback") == "fallback"
        # A literal backslash in a POSIX filename survives intact.
        assert caller_basename(r"/home/x/we\ird.md", "fallback") == r"we\ird.md"
        assert caller_basename(r"we\ird.md", "fallback") == r"we\ird.md"
        # The `and not posix.is_absolute()` clause earns its keep only here.
        # A path absolute under *both* flavours with a backslash in its tail
        # is the one shape the two readings disagree on: dropping the clause
        # reduces this Windows-style to "b.md". Every case above passes with
        # the clause removed, so without this one it is untested.
        assert caller_basename(r"//host/share/a\b.md", "fallback") == r"a\b.md"


class TestGateReportsItsOwnAnswer:
    """The gate publishes the absoluteness it decided minting on.

    Consumers downstream of the gate need the same answer -- a restore has no
    vault-relative reading of "the bytes to repair", so it refuses a source
    the gate judged relative. Re-deriving it at each consumer is how the
    predicate came to differ between sites in the first place, so the plan
    carries the decision and a consumer reads it.

    The profile is pinned rather than the reachability predicate patched, so
    each arm is reached the way a deployment reaches it.
    """

    @pytest.fixture(autouse=True)
    def _fresh_store(self):
        reset_transfer_store()
        yield
        reset_transfer_store()

    def test_caller_absolute_reads_either_flavour_when_unreachable(self):
        """Where the caller's environment writes, a foreign spelling is absolute.

        A relative source resolves rather than minting, which is what makes
        the plan observable at all: a caller-absolute source returns a recipe
        and carries no resolved deliveries. Both halves are asserted, because
        the pair is what says the flag agrees with the minting decision rather
        than merely existing.

        Discriminating only alongside its sibling below, and the two must not
        be collapsed. A gate that hardcoded the flag to ``False`` passes this
        test whole: the relative source expects ``False`` anyway, and minting
        reads the predicate directly rather than the flag, so the recipe half
        is unaffected. Only the reachable-arm test, which expects ``True`` for
        a posix-absolute source, reds against it. Verified by mutation rather
        than reasoned.
        """
        with _profile("cloud"):
            with caller_local_delivery(_VAULT, [DeliveryDeclaration(source="docs/a.md")]) as plan:
                assert plan.recipe is None
                (resolved,) = plan.resolved
                assert resolved.caller_absolute is False

            for spelling in (r"C:\docs\a.md", r"\\host\share\a.md", "/home/x/a.md"):
                with caller_local_delivery(_VAULT, [DeliveryDeclaration(source=spelling)]) as plan:
                    assert plan.recipe is not None, f"{spelling} must mint"

    def test_caller_absolute_uses_this_platform_when_reachable(self):
        """Where this process writes, its own semantics govern.

        The asymmetry the shared predicate turns on: co-located, the caller
        *is* this machine, so a drive-letter spelling is a relative name here
        and must report as one. Reading either flavour on this arm would let a
        foreign spelling through to a consumer that resolves it locally.
        """
        with _profile("local"):
            with caller_local_delivery(
                _VAULT,
                [
                    DeliveryDeclaration(source="/tmp/a.md"),
                    DeliveryDeclaration(source=r"C:\docs\a.md"),
                    DeliveryDeclaration(source="docs/a.md"),
                ],
            ) as plan:
                assert plan.recipe is None
                posix, windows, relative = plan.resolved

        assert posix.caller_absolute is True
        assert windows.caller_absolute is False
        assert relative.caller_absolute is False


class TestMultiLegRecipeWindow:
    """The window a multi-leg upload recipe reports covers every leg.

    Each leg is minted on its own clock reading, so a batch carries a spread
    of windows while the recipe carries one instant. The recipe's promise is
    that every leg is still live at that instant, which only the earliest
    leg's expiry can keep -- a later one lapses the earlier legs before the
    caller has been told to stop.
    """

    @pytest.fixture(autouse=True)
    def _fresh_store(self):
        reset_transfer_store()
        yield
        reset_transfer_store()

    @staticmethod
    def _install(monkeypatch, tmp_path) -> TransferStore:
        """Put a stepping-clock store where the minting path will find it."""
        store = TransferStore(now=_SteppingClock(), staging_root=tmp_path / "staging")
        monkeypatch.setattr(_transfer, "_transfer_store", store)
        return store

    def test_multi_leg_recipe_reports_the_earliest_leg_expiry(self, monkeypatch, tmp_path):
        """No leg lapses before the instant the recipe names.

        Anti-coincidental-pass: the distinctness assertion is what makes the
        rest discriminating. On a clock that does not move between legs every
        leg shares one expiry, so the earliest, the latest and the last-minted
        are the same value and a recipe reporting the last leg's window passes
        whole -- which is exactly the defect. Asserting the three windows
        differ turns a stalled clock into a red rather than a silent
        disarming.
        """
        store = self._install(monkeypatch, tmp_path)

        with _profile("cloud"):
            recipe = mint_upload_recipe(_VAULT, ["a.md", "b.md", "c.md"])

        leg_expiries = [store._entries[item.transfer_id].expires_at for item in recipe.uploads]
        assert len(set(leg_expiries)) == 3, (
            f"the clock did not advance between legs ({leg_expiries!r}); without "
            "distinct windows this case cannot tell the earliest from the latest"
        )
        assert recipe.expires_at == min(leg_expiries)
        assert all(recipe.expires_at <= expiry for expiry in leg_expiries)

    def test_single_leg_recipe_reports_its_own_leg_expiry(self, monkeypatch, tmp_path):
        """A one-leg batch still reports that leg's window.

        Not discriminating on its own -- over a single leg the earliest, the
        latest and the last-minted coincide -- and it is not claimed to be.
        It is the control for the overwhelmingly common shape, so a fix that
        reaches for the wrong leg on a batch of one reds here.
        """
        store = self._install(monkeypatch, tmp_path)

        with _profile("cloud"):
            recipe = mint_upload_recipe(_VAULT, ["solo.md"])

        (item,) = recipe.uploads
        assert recipe.expires_at == store._entries[item.transfer_id].expires_at


class TestGateRefusalsNameTheCallersSpelling:
    """The gate's delivery-shape refusals name the spelling it was handed.

    Several tools reach the gate and spell the path parameter differently, and
    the gate raises before it has resolved anything that could tell them
    apart. The caller supplies its own spelling; each refusal must carry it,
    under an unchanged code and status. Both refusals are driven, because
    forwarding the spelling to only one leaves the other still naming a
    parameter the caller never had.

    No profile is pinned and no store is reset: the refusal precedes both the
    reachability decision and any redemption, so neither is in its causal path.
    """

    @pytest.mark.parametrize(
        ("declaration", "error", "code", "message"),
        [
            (
                DeliveryDeclaration(source="/tmp/a.md", transfer_token="whatever"),
                AmbiguousIngestSourceError,
                "ambiguous_ingest_source",
                "Supply exactly one of `file_path` (a source file path) or "
                "`transfer_token` (redeeming an already-delivered upload); "
                "both were provided.",
            ),
            (
                DeliveryDeclaration(),
                MissingIngestSourceError,
                "missing_ingest_source",
                "Supply exactly one of `file_path` (a source file path) or "
                "`transfer_token` (redeeming an already-delivered upload); "
                "neither was provided.",
            ),
        ],
        ids=["ambiguous", "missing"],
    )
    def test_refusals_name_the_supplied_spelling(self, declaration, error, code, message):
        with pytest.raises(error) as raised:
            with caller_local_delivery(_VAULT, [declaration], source_parameter="file_path"):
                pytest.fail("the gate must refuse before yielding a plan")

        assert raised.value.code == code
        assert raised.value.status_code == 400
        assert raised.value.message == message


class TestPreviewDoesNotSpendTheToken:
    """``consume=False`` reads a redeemed entry's bytes and hands it back.

    The gate already returns every redeemed token on a *failing* block, so a
    caller repeats the byte leg only when the bytes themselves were the
    problem. A preview is the other case that reads without doing the work:
    it succeeds, and the real call it previews is still to come. Spending
    the token there would charge a second byte leg for asking a question.
    """

    @pytest.fixture(autouse=True)
    def _fresh_store(self):
        reset_transfer_store()
        yield
        reset_transfer_store()

    def test_preview_leaves_the_token_redeemable(self, tmp_path, monkeypatch):
        """The same token redeems again, against the same staged bytes."""
        store = TransferStore(now=_Clock(), staging_root=tmp_path / "staging")
        monkeypatch.setattr(_transfer, "_transfer_store", store)
        minted = store.mint_upload(_VAULT, "notes.md", ttl_seconds=300)
        _stage_bytes(store, minted, b"delivered once")

        with _profile("local"):
            with caller_local_delivery(
                _VAULT,
                [DeliveryDeclaration(transfer_token=minted.token)],
                consume=False,
            ) as plan:
                (resolved,) = plan.resolved
                assert Path(resolved.path).read_bytes() == b"delivered once"

            # The bytes are still there and the token still answers for them.
            with caller_local_delivery(
                _VAULT,
                [DeliveryDeclaration(transfer_token=minted.token)],
            ) as plan:
                (again,) = plan.resolved
                assert Path(again.path).read_bytes() == b"delivered once"

    def test_a_real_run_still_spends_the_token(self, tmp_path, monkeypatch):
        """Negative control. A fix that simply never consumed would pass the
        test above and fail here, so the pair pins ``consume`` as a switch
        rather than a removal."""
        store = TransferStore(now=_Clock(), staging_root=tmp_path / "staging")
        monkeypatch.setattr(_transfer, "_transfer_store", store)
        minted = store.mint_upload(_VAULT, "notes.md", ttl_seconds=300)
        _stage_bytes(store, minted, b"delivered once")

        with _profile("local"):
            with caller_local_delivery(
                _VAULT,
                [DeliveryDeclaration(transfer_token=minted.token)],
            ) as plan:
                assert plan.resolved

            with pytest.raises(TransferTokenInvalidError):
                with caller_local_delivery(
                    _VAULT,
                    [DeliveryDeclaration(transfer_token=minted.token)],
                ):
                    pass
