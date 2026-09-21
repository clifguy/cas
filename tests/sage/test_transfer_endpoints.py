"""HTTP tests for the transfer endpoints: the byte legs of the caller-local channel.

``PUT /upload`` and ``GET /download/{transfer_id}`` carry the bytes a recipe
promised, gated by the recipe's one-time token as the sole credential. These
tests drive the full exchange over ASGI -- MCP mint, HTTP byte leg, MCP
completion -- plus the endpoint-only failure modes (bad token, replay,
mid-stream ceiling, client disconnect rollback) and the auth exemption that
lets curl present only the transfer token while sibling routes still demand a
bearer.
"""

import asyncio
import contextlib
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import sage.mcp_init as _mcp_init
import sage.mcp_server as _mcp
import sage.services.transfer as _transfer
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.app import _initialize_services, create_app
from sage.config import SageCoreConfig, StackAuthConfig, VaultConfig
from sage.config import StackTransferConfig as _StackTransferConfig
from sage.mcp_server import bulk_ingest_document, get_document, ingest_document, read_projection
from sage.services.transfer import TransferStore, get_transfer_store, reset_transfer_store
from sage.vault_source_binding import FilesystemVaultSourceStore
from tests.helpers.pipeline_wait import await_pipeline_idle
from tests.helpers.store_refusal import STORE_BODY, store_refusal

_VAULT_ID = "test_vault"
_BASE = "https://sage.test.example"

#: The window a recipe minted under the default stack config carries.
#: Derived rather than written down, so retuning the lifetime moves the
#: clock advance with it instead of leaving the test overshooting by an
#: accident that would still pass.
_TTL = _StackTransferConfig.model_fields["token_ttl_seconds"].default


class _Clock:
    """Controllable clock, mirroring the store suite's own."""

    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _parse(result: str | dict) -> dict:
    if isinstance(result, dict):
        return result
    return json.loads(result)


@contextlib.contextmanager
def _profile(name: str, transfer_base: str | None = _BASE, **transfer: object):
    """Pin profile + transfer coordinates, mirroring the confinement suite.

    Keyword arguments beyond the base URL are further ``transfer`` settings.
    """
    saved = _mcp_init._stack_config
    kwargs: dict = {"profile": name}
    if transfer_base is not None:
        kwargs["transfer"] = {"public_base_url": transfer_base, **transfer}
    _mcp_init.set_stack_config(SageCoreConfig(**kwargs))
    try:
        yield
    finally:
        _mcp_init.set_stack_config(saved)


@pytest.fixture(autouse=True)
def _fresh_transfer_store():
    reset_transfer_store()
    yield
    reset_transfer_store()


@pytest.fixture
async def app(minimal_vault_config_dict):
    """FastAPI app with one initialized vault (registry shared with MCP)."""
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    application = create_app(config=config)
    await _initialize_services(
        application,
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    )
    try:
        yield application
    finally:
        await asyncio.sleep(0.3)
        for services in application.state.vault_registry.values():
            services.close_timing()
            await services.graph_store.close()
        _mcp._vaults.pop(_VAULT_ID, None)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _mint_upload(tmp_path, name: str, body: bytes) -> dict:
    """Mint an upload recipe for a caller-local file under the cloud profile."""
    src = tmp_path / "caller_inbox" / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(body)
    recipe = _parse(await ingest_document(_VAULT_ID, str(src), "markdown"))
    assert recipe.get("status") == "upload_required", recipe
    return recipe["uploads"][0]


async def _ingest_locally(tmp_path, name: str, body: str) -> dict:
    inbox = tmp_path / "caller_inbox"
    inbox.mkdir(exist_ok=True)
    src = inbox / name
    src.write_text(body)
    with _profile("local", transfer_base=None):
        result = _parse(await ingest_document(_VAULT_ID, str(src), "markdown"))
    assert "error" not in result, result
    return result


# ---------------------------------------------------------------------------
# Round trips
# ---------------------------------------------------------------------------


async def test_upload_round_trip(client, tmp_path):
    """Mint -> chunked PUT -> receipt -> completion: the retained bytes equal
    the original caller file.

    Anti-coincidental: the receipt's size/sha256 are checked against the
    independently computed values, and the retained source is read back and
    compared byte-for-byte -- a handler that staged the wrong or empty body
    fails on content, not just status.
    """
    body = b"# Upload round trip\n\n" + b"payload line\n" * 500

    with _profile("cloud"):
        item = await _mint_upload(tmp_path, "round_trip.md", body)

        async def _chunks():
            for i in range(0, len(body), 1024):
                yield body[i : i + 1024]

        resp = await client.put(
            "/upload", content=_chunks(), headers={"X-Upload-Token": item["token"]}
        )
        assert resp.status_code == 201, resp.text
        receipt = resp.json()
        assert receipt["transfer_id"] == item["transfer_id"]
        assert receipt["size"] == len(body)
        assert receipt["sha256"] == hashlib.sha256(body).hexdigest()

        done = _parse(
            await ingest_document(_VAULT_ID, source_type="markdown", transfer_token=item["token"])
        )

    assert "error" not in done, done
    assert done["source_path"] == "imports/round_trip.md"
    assert done["source_content_hash"] == "sha256:" + hashlib.sha256(body).hexdigest()


async def test_vsbb_047_refused_upload_reports_the_callers_path_not_the_staging_one(
    client, tmp_path, tmp_vault_dir
):
    """A retention refusal on the completion leg names the file the caller sent,
    not the server-side staging path the token redeemed to.

    The two-phase completion substitutes the staged path for the caller's, so
    everything downstream of the redemption sees a location inside the server's
    own temp tree. See ``IngestionService.ingest``'s ``caller_source`` for why
    reporting that back is the one shape of this detail worse than useless.

    Anti-coincidental-pass: three rivals.

    Reporting the staged path is the defect, and the staged file keeps the
    caller's basename, so an ``endswith`` or basename assertion passes against
    it. Equality is the only form that fails, and the fixture asserts the two
    paths actually differ before resting on that -- otherwise reporting either
    one would satisfy the assertion.

    Reporting the entry's ``filename`` -- the sanitized basename -- fails the
    same equality against an absolute path.

    Third, any implementation that round-trips the declared source through
    ``Path``, which is why this mints from a path carrying a ``/./`` segment
    rather than from the ``_mint_upload`` helper's clean one. On a clean
    absolute path that round-trip is the identity, so a fixture using one
    excludes nothing; the dotted spelling is what separates them. This is the
    transfer leg's form of the rival VSBB-045 pins at the service level.

    The expected value is the recipe's own echoed ``source`` rather than a
    re-typed path, making this a round-trip claim: the string the caller handed
    to the mint is the string the refusal names.
    """
    body = b"# refused upload\n"
    # Dangling, so retention reaches its write exit and refuses the link there.
    imports = tmp_vault_dir / "sources" / "imports"
    imports.mkdir(parents=True, exist_ok=True)
    (imports / "refused_upload.md").symlink_to(tmp_path / "nowhere.md")

    # No file is created at this path, and none is needed: under the cloud
    # profile the mint decides on the path's shape alone and never stats it --
    # the bytes arrive on the upload leg below. Creating one here would be setup
    # no assertion depends on.
    #
    # Built by concatenation: ``Path`` would collapse the ``/./`` on
    # construction, and that segment is the whole point of the fixture.
    dotted = f"{tmp_path}/caller_inbox/./refused_upload.md"

    with _profile("cloud"):
        recipe = _parse(await ingest_document(_VAULT_ID, dotted, "markdown"))
        assert recipe.get("status") == "upload_required", recipe
        item = recipe["uploads"][0]
        resp = await client.put("/upload", content=body, headers={"X-Upload-Token": item["token"]})
        assert resp.status_code == 201, resp.text

        supplied = item["source"]
        assert supplied == dotted, "the recipe echoes the caller's spelling verbatim"
        assert supplied != str(Path(supplied)), "the fixture must survive a Path round-trip"
        # Read before redemption consumes the entry.
        staged = str(get_transfer_store()._entries[item["transfer_id"]].staged_path)
        assert staged != supplied

        done = _parse(
            await ingest_document(_VAULT_ID, source_type="markdown", transfer_token=item["token"])
        )

    assert done["error"] == "vault_source_path_refused", done
    assert done["detail"] == {"source_path": supplied}


async def test_vsbb_052_refused_bulk_upload_entry_carries_code_and_the_callers_path(
    client, tmp_path, tmp_vault_dir
):
    """A retention refusal on the bulk completion leg reaches the caller as a
    typed per-file entry -- its code, and a detail naming the file the caller
    sent rather than the staging path the token redeemed to.

    The bulk leg redeems and substitutes the staged path exactly as the
    singleton leg does (VSBB-047), but its failures are collected into
    ``summary.errors`` instead of raised, and that collection kept only the
    rendered message: no code to branch on, and no ``detail`` for the caller's
    path to travel in.

    Anti-coincidental-pass: the same three rivals VSBB-047 pins, on the entry
    instead of the envelope. Equality against the recipe's echoed ``source``
    excludes the staged path (same basename) and the sanitized ``filename``;
    the ``/./`` segment excludes a ``Path`` round-trip; and the staged path is
    read and asserted different before redemption consumes the entry, so
    reporting either spelling could not satisfy the assertion by coincidence.
    Against a message-only collection this fails on the missing ``code`` key.
    The entry also carries the file's position in the batch, the field the
    token leg shares with the upload leg's summary.
    """
    body = b"# refused bulk upload\n"
    imports = tmp_vault_dir / "sources" / "imports"
    imports.mkdir(parents=True, exist_ok=True)
    (imports / "refused_bulk.md").symlink_to(tmp_path / "nowhere.md")

    dotted = f"{tmp_path}/caller_inbox/./refused_bulk.md"

    with _profile("cloud"):
        recipe = _parse(
            await bulk_ingest_document(
                _VAULT_ID, [{"file_path": dotted, "source_type": "markdown"}]
            )
        )
        assert recipe.get("status") == "upload_required", recipe
        item = recipe["uploads"][0]
        resp = await client.put("/upload", content=body, headers={"X-Upload-Token": item["token"]})
        assert resp.status_code == 201, resp.text

        supplied = item["source"]
        assert supplied == dotted, "the recipe echoes the caller's spelling verbatim"
        staged = str(get_transfer_store()._entries[item["transfer_id"]].staged_path)
        assert staged != supplied

        summary = _parse(
            await bulk_ingest_document(
                _VAULT_ID,
                [{"transfer_token": item["token"], "source_type": "markdown"}],
            )
        )

    assert summary["error_count"] == 1, summary
    entry = summary["errors"][0]
    assert entry["code"] == "vault_source_path_refused"
    assert entry["detail"] == {"source_path": supplied}
    assert entry["source_path"] == supplied
    assert entry["filename"] == "refused_bulk.md"
    assert entry["file_index"] == 0


async def test_download_round_trip_source(client, tmp_path):
    """Mint via ``get_document(write_to_path=...)`` -> GET -> the streamed
    bytes equal the ingested source and match the recipe's promised hash."""
    body = "# Download source\n\n" + "line\n" * 200
    ingested = await _ingest_locally(tmp_path, "dl_source.md", body)

    with _profile("cloud"):
        recipe = _parse(
            await get_document(_VAULT_ID, ingested["id"], write_to_path=str(tmp_path / "out.md"))
        )
        assert recipe.get("status") == "download_required", recipe

        resp = await client.get(
            f"/download/{recipe['transfer_id']}",
            headers={"X-Download-Token": recipe["token"]},
        )

    assert resp.status_code == 200, resp.text
    fetched = resp.content
    assert fetched == body.encode("utf-8")
    assert len(fetched) == recipe["content_size"]
    assert "sha256:" + hashlib.sha256(fetched).hexdigest() == recipe["content_hash"]
    assert resp.headers["content-length"] == str(recipe["content_size"])


async def test_download_round_trip_projection(app, client, tmp_path):
    """Mint via ``read_projection(write_to_path=...)`` -> GET -> the streamed
    bytes are the projection text the recipe promised."""
    ingested = await _ingest_locally(tmp_path, "dl_proj.md", "# DP\n\nProjection body here.")
    services = app.state.vault_registry[_VAULT_ID]
    await await_pipeline_idle(
        services.graph_store, ingested["id"], service=services.ingestion_service
    )

    with _profile("cloud"):
        recipe = _parse(
            await read_projection(
                _VAULT_ID, ingested["id"], write_to_path=str(tmp_path / "proj_out.md")
            )
        )
        assert recipe.get("status") == "download_required", recipe

        resp = await client.get(
            f"/download/{recipe['transfer_id']}",
            headers={"X-Download-Token": recipe["token"]},
        )

    assert resp.status_code == 200, resp.text
    assert "Projection body here." in resp.text
    assert len(resp.content) == recipe["content_size"]
    assert "sha256:" + hashlib.sha256(resp.content).hexdigest() == recipe["content_hash"]


async def test_transfer_not_bounded_by_inline_ceiling(client, tmp_path, monkeypatch):
    """With the inline-content ceiling pinned far below the payload size, the
    transfer channel still round-trips the bytes -- neither leg is bounded by
    the tool-response inline budget."""
    monkeypatch.setenv("SAGE_MAX_INLINE_CONTENT_BYTES", "64")
    body = b"# Big\n\n" + b"x" * 10_000  # far above the pinned inline ceiling

    with _profile("cloud"):
        item = await _mint_upload(tmp_path, "big_note.md", body)
        put = await client.put("/upload", content=body, headers={"X-Upload-Token": item["token"]})
        assert put.status_code == 201, put.text
        done = _parse(
            await ingest_document(_VAULT_ID, source_type="markdown", transfer_token=item["token"])
        )
        assert "error" not in done, done

        recipe = _parse(
            await get_document(_VAULT_ID, done["id"], write_to_path=str(tmp_path / "big_out.md"))
        )
        resp = await client.get(
            f"/download/{recipe['transfer_id']}",
            headers={"X-Download-Token": recipe["token"]},
        )

    assert resp.status_code == 200
    assert resp.content == body


# ---------------------------------------------------------------------------
# Endpoint failure modes
# ---------------------------------------------------------------------------


async def test_upload_ceiling_aborts_mid_stream(client, tmp_path, monkeypatch):
    """A body exceeding the transfer ceiling is aborted with 413, the partial
    staging file is deleted, and the token stays retryable.

    Anti-coincidental: the follow-up under-ceiling PUT on the *same token*
    must succeed -- proving the abort rolled the entry back rather than
    consuming or corrupting it -- and the deleted-partial assertion catches a
    handler that only checks size after buffering the whole body to disk.
    """
    monkeypatch.setenv("SAGE_MAX_TRANSFER_BYTES", "64")

    with _profile("cloud"):
        item = await _mint_upload(tmp_path, "capped.md", b"placeholder")
        big = b"y" * 200
        resp = await client.put("/upload", content=big, headers={"X-Upload-Token": item["token"]})
        assert resp.status_code == 413, resp.text
        assert resp.json()["code"] == "transfer_content_too_large"

        from sage.services.transfer import get_transfer_store

        entry = get_transfer_store()._entries[item["transfer_id"]]
        assert not entry.staged_path.exists()  # partial removed
        assert entry.state == "pending_bytes"  # retryable

        small = b"# ok\n"
        retry = await client.put(
            "/upload", content=small, headers={"X-Upload-Token": item["token"]}
        )
        assert retry.status_code == 201, retry.text
        assert retry.json()["size"] == len(small)


# ---------------------------------------------------------------------------
# Digest-bound upload tokens
# ---------------------------------------------------------------------------


def _sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


async def _mint_bound_upload(tmp_path, name: str, sha256: str) -> dict:
    """Mint a single-leg recipe whose token is bound to ``sha256``.

    Under the cloud profile the mint never reads the caller's path, so none
    is written: the digest is the only thing tying the token to a file.
    """
    src = tmp_path / "caller_inbox" / name
    recipe = _parse(
        await ingest_document(_VAULT_ID, source=str(src), source_type="markdown", sha256=sha256)
    )
    assert recipe.get("status") == "upload_required", recipe
    return recipe["uploads"][0]


async def test_digest_bound_upload_refuses_other_bytes(client, tmp_path):
    """Bytes whose digest differs from the bound one are refused, not staged,
    and the token is not spent.

    Anti-coincidental-pass: the refusal is identified by its code rather than
    its status alone, so an ``unknown_parameter`` 400 cannot stand in for it.
    The rollback is proven three ways -- no staged file, the entry back at
    ``pending_bytes`` with no recorded digest, and the *same* token then
    accepting the right bytes and completing the ingest -- so a handler that
    refused after staging, or that spent the token on refusal, fails. The
    bound digest is searched for in the whole response body, so a refusal
    that told the presenter which bytes would pass fails too.
    """
    right = b"# Bound\n\nThe exact caller file.\n"
    wrong = b"# Bound\n\nSomebody else's bytes.\n"

    with _profile("cloud"):
        item = await _mint_bound_upload(tmp_path, "bound.md", _sha256_hex(right))

        refused = await client.put(
            "/upload", content=wrong, headers={"X-Upload-Token": item["token"]}
        )
        assert refused.status_code == 400, refused.text
        assert refused.json()["code"] == "source_digest_mismatch"
        assert _sha256_hex(right) not in refused.text

        entry = get_transfer_store()._entries[item["transfer_id"]]
        assert not entry.staged_path.exists()
        assert entry.state == "pending_bytes"
        assert entry.staged_sha256 is None

        accepted = await client.put(
            "/upload", content=right, headers={"X-Upload-Token": item["token"]}
        )
        assert accepted.status_code == 201, accepted.text

        done = _parse(
            await ingest_document(
                _VAULT_ID,
                source_type="markdown",
                transfer_token=item["token"],
                sha256=_sha256_hex(right),
            )
        )

    assert "error" not in done, done
    assert done["source_content_hash"] == "sha256:" + _sha256_hex(right)


async def test_digest_bound_upload_accepts_matching_bytes(client, tmp_path):
    """The right bytes stage on the first attempt, and the recipe echoes the
    bound digest in canonical form.

    Anti-coincidental-pass: the digest is supplied bare, so a recipe echoing
    the caller's spelling verbatim fails the canonical-form equality.
    """
    body = b"# Matching\n"

    with _profile("cloud"):
        item = await _mint_bound_upload(tmp_path, "matching.md", _sha256_hex(body))
        assert item["sha256"] == "sha256:" + _sha256_hex(body)

        resp = await client.put("/upload", content=body, headers={"X-Upload-Token": item["token"]})
        assert resp.status_code == 201, resp.text

        done = _parse(
            await ingest_document(_VAULT_ID, source_type="markdown", transfer_token=item["token"])
        )

    assert "error" not in done, done


async def test_unbound_upload_accepts_any_bytes_and_recipe_omits_digest(client, tmp_path):
    """The paired control: a token minted without a digest behaves as before.

    Paired with the refusal above, so neither an always-on check (which reds
    here) nor an always-off one (which reds there) passes both.
    """
    with _profile("cloud"):
        item = await _mint_upload(tmp_path, "unbound.md", b"# minted against these bytes\n")
        assert item.get("sha256") is None

        resp = await client.put(
            "/upload",
            content=b"# but any bytes are admitted\n",
            headers={"X-Upload-Token": item["token"]},
        )

    assert resp.status_code == 201, resp.text


async def test_bulk_digest_refusal_is_per_leg(client, tmp_path):
    """A refused leg of a bulk recipe leaves its siblings staged and completable.

    Anti-coincidental-pass: leg B is delivered *after* leg A is refused and is
    completed without A, so a refusal that failed or rolled back the whole
    recipe fails on B. A is then delivered correctly on its original token and
    completed, so a refusal that spent A's token fails there.
    """
    body_a = b"# Leg A\n"
    body_b = b"# Leg B\n"
    # Named, never written: the cloud-profile mint does not read the caller's path.
    inbox = tmp_path / "caller_inbox"

    with _profile("cloud"):
        recipe = _parse(
            await bulk_ingest_document(
                _VAULT_ID,
                [
                    {
                        "file_path": str(inbox / "leg_a.md"),
                        "source_type": "markdown",
                        "sha256": _sha256_hex(body_a),
                    },
                    {
                        "file_path": str(inbox / "leg_b.md"),
                        "source_type": "markdown",
                        "sha256": _sha256_hex(body_b),
                    },
                ],
            )
        )
        assert recipe.get("status") == "upload_required", recipe
        leg_a, leg_b = recipe["uploads"]
        assert leg_a["sha256"] == "sha256:" + _sha256_hex(body_a)
        assert leg_b["sha256"] == "sha256:" + _sha256_hex(body_b)

        refused = await client.put(
            "/upload", content=body_b, headers={"X-Upload-Token": leg_a["token"]}
        )
        assert refused.status_code == 400, refused.text
        assert refused.json()["code"] == "source_digest_mismatch"
        assert get_transfer_store()._entries[leg_a["transfer_id"]].state == "pending_bytes"

        staged_b = await client.put(
            "/upload", content=body_b, headers={"X-Upload-Token": leg_b["token"]}
        )
        assert staged_b.status_code == 201, staged_b.text

        summary_b = _parse(
            await bulk_ingest_document(
                _VAULT_ID,
                [{"transfer_token": leg_b["token"], "source_type": "markdown"}],
            )
        )
        assert summary_b.get("error_count") == 0, summary_b
        assert summary_b["documents_created"]["new"] == 1

        staged_a = await client.put(
            "/upload", content=body_a, headers={"X-Upload-Token": leg_a["token"]}
        )
        assert staged_a.status_code == 201, staged_a.text
        summary_a = _parse(
            await bulk_ingest_document(
                _VAULT_ID,
                [{"transfer_token": leg_a["token"], "source_type": "markdown"}],
            )
        )

    assert summary_a.get("error_count") == 0, summary_a
    assert summary_a["documents_created"]["new"] == 1


# ---------------------------------------------------------------------------
# Refusal limit
# ---------------------------------------------------------------------------

#: How many refused deliveries a token minted under the default stack config
#: survives. Derived rather than written down, as ``_TTL`` is.
_REFUSAL_LIMIT = _StackTransferConfig.model_fields["max_refused_deliveries"].default


async def _put(client, token: str, body: bytes):
    return await client.put("/upload", content=body, headers={"X-Upload-Token": token})


async def test_repeated_ceiling_aborts_exhaust_the_token(client, tmp_path, monkeypatch):
    """Oversize deliveries are refused up to the limit; the one that reaches
    it reclaims the transfer with a typed 410.

    Anti-coincidental-pass: the refusals before the last are asserted to be
    the ordinary 413, so a limit that tripped early fails; the last is
    asserted by code, so an ordinary 413 on it (no limit at all) fails; and a
    well-formed delivery afterwards must be refused as an unknown token, so a
    limit that answered 410 but left the entry live fails.
    """
    monkeypatch.setenv("SAGE_MAX_TRANSFER_BYTES", "64")

    with _profile("cloud"):
        item = await _mint_upload(tmp_path, "abused.md", b"placeholder")
        staging_dir = get_transfer_store()._entries[item["transfer_id"]].staging_dir
        big = b"y" * 200

        for _ in range(_REFUSAL_LIMIT - 1):
            refused = await _put(client, item["token"], big)
            assert refused.status_code == 413, refused.text
            assert refused.json()["code"] == "transfer_content_too_large"

        exhausted = await _put(client, item["token"], big)
        assert exhausted.status_code == 410, exhausted.text
        assert exhausted.json()["code"] == "transfer_refusal_limit_reached"
        assert exhausted.json()["detail"] == {
            "transfer_id": item["transfer_id"],
            "max_refused_deliveries": _REFUSAL_LIMIT,
        }

        after = await _put(client, item["token"], b"# ok\n")
        assert after.status_code == 410, after.text
        assert after.json()["code"] == "transfer_token_invalid"

    assert item["transfer_id"] not in get_transfer_store()._entries
    assert not staging_dir.exists()


async def test_repeated_digest_mismatches_exhaust_the_token(client, tmp_path):
    """Wrong-digest deliveries count toward the same limit, and once it is
    reached even the right bytes are refused.

    Anti-coincidental-pass: the final delivery carries the *bound* bytes, so
    a reclaim that only blocked further mismatches fails; the bound digest is
    searched for in every response, so a limit refusal that disclosed it
    fails.
    """
    right = b"# Bound\n\nThe exact caller file.\n"
    wrong = b"# Bound\n\nSomebody else's bytes.\n"
    bound = _sha256_hex(right)

    with _profile("cloud"):
        item = await _mint_bound_upload(tmp_path, "bound.md", bound)

        responses = [await _put(client, item["token"], wrong) for _ in range(_REFUSAL_LIMIT)]
        after = await _put(client, item["token"], right)

    assert [r.status_code for r in responses] == [400] * (_REFUSAL_LIMIT - 1) + [410]
    assert responses[-1].json()["code"] == "transfer_refusal_limit_reached"
    assert after.status_code == 410, after.text
    assert after.json()["code"] == "transfer_token_invalid"
    for resp in [*responses, after]:
        assert bound not in resp.text


async def test_token_survives_refusals_below_the_limit(client, tmp_path, monkeypatch):
    """The paired control: one refusal short of the limit, of either kind,
    and the same token still stages and completes the right file.

    Anti-coincidental-pass: a limit that tripped a delivery early fails
    here while the exhaustion tests above still pass. Each refusal is pinned
    by code, so an oversize body that was refused on its digest instead --
    the ceiling unset -- fails too. A limit counted per refusal kind passes
    here; the mixed-kind test below is what excludes it.
    """
    monkeypatch.setenv("SAGE_MAX_TRANSFER_BYTES", "64")
    right = b"# Kept\n"
    kinds = [
        (b"y" * 200, "transfer_content_too_large"),
        (b"# not the bound file\n", "source_digest_mismatch"),
    ]

    with _profile("cloud"):
        item = await _mint_bound_upload(tmp_path, "kept.md", _sha256_hex(right))
        for n in range(_REFUSAL_LIMIT - 1):
            body, code = kinds[n % 2]
            refused = await _put(client, item["token"], body)
            assert refused.json()["code"] == code, refused.text

        accepted = await _put(client, item["token"], right)
        assert accepted.status_code == 201, accepted.text
        done = _parse(
            await ingest_document(_VAULT_ID, source_type="markdown", transfer_token=item["token"])
        )

    assert "error" not in done, done


async def test_refusals_of_different_kinds_share_one_limit(client, tmp_path, monkeypatch):
    """Oversize bodies and wrong digests count against one limit, not one each.

    Anti-coincidental-pass: the two kinds alternate, so no kind alone reaches
    the limit by the last delivery. A store keeping a count per kind answers
    that delivery with its own 413 or 400 rather than the 410, where the
    single-kind exhaustion tests above cannot tell the two apart.
    """
    monkeypatch.setenv("SAGE_MAX_TRANSFER_BYTES", "64")
    right = b"# Mixed\n"
    kinds = [b"y" * 200, b"# not the bound file\n"]

    with _profile("cloud"):
        item = await _mint_bound_upload(tmp_path, "mixed.md", _sha256_hex(right))
        responses = [await _put(client, item["token"], kinds[n % 2]) for n in range(_REFUSAL_LIMIT)]

    assert _REFUSAL_LIMIT >= 3, "needs a limit no single kind reaches when alternating"
    assert [r.json()["code"] for r in responses[:-1]] == [
        ("transfer_content_too_large", "source_digest_mismatch")[n % 2]
        for n in range(_REFUSAL_LIMIT - 1)
    ]
    assert responses[-1].status_code == 410, responses[-1].text
    assert responses[-1].json()["code"] == "transfer_refusal_limit_reached"


async def _put_then_disconnect(app, token: str) -> None:
    """Send one chunk of an upload body, then drop the connection.

    Driven below the HTTP client, which cannot disconnect mid-body: the ASGI
    ``receive`` yields a partial body and then ``http.disconnect``, as a
    server does when the peer goes away.
    """
    messages = iter(
        [
            {"type": "http.request", "body": b"partial", "more_body": True},
            {"type": "http.disconnect"},
        ]
    )

    async def receive():
        return next(messages, {"type": "http.disconnect"})

    async def send(_message):
        return None

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "PUT",
        "scheme": "http",
        "path": "/upload",
        "raw_path": b"/upload",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test"), (b"x-upload-token", token.encode())],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }
    with contextlib.suppress(Exception):
        await app(scope, receive, send)


async def test_client_disconnect_counts_toward_the_limit(app, client, tmp_path):
    """A delivery the presenter abandons mid-body is a refusal like any other.

    Otherwise the limit is bypassed by streaming to just under the ceiling
    and dropping the connection, over and over.

    Anti-coincidental-pass: the first disconnect is asserted to leave the
    token live, so a disconnect that was simply fatal fails; after the limit,
    a well-formed delivery must be refused as an unknown token, which a
    disconnect routed to the uncounted rollback never produces.
    """
    with _profile("cloud"):
        item = await _mint_upload(tmp_path, "dropped.md", b"placeholder")

        await _put_then_disconnect(app, item["token"])
        entry = get_transfer_store()._entries[item["transfer_id"]]
        assert entry.state == "pending_bytes"
        assert entry.refused_deliveries == 1

        for _ in range(_REFUSAL_LIMIT - 1):
            await _put_then_disconnect(app, item["token"])

        after = await _put(client, item["token"], b"# ok\n")

    assert after.status_code == 410, after.text
    assert after.json()["code"] == "transfer_token_invalid"


async def test_server_fault_does_not_count_toward_the_limit(client, tmp_path, monkeypatch):
    """A delivery that fails on the server's side costs the token nothing.

    Anti-coincidental-pass: more faults than the limit are driven before the
    good delivery, so a handler that counted every failure would have
    reclaimed the transfer and the final delivery would be refused.
    """

    def _fault(self, transfer_id, sha256):
        raise RuntimeError("simulated staging fault")

    with _profile("cloud"):
        item = await _mint_upload(tmp_path, "faulty.md", b"placeholder")

        with monkeypatch.context() as patched:
            patched.setattr(TransferStore, "check_bound_digest", _fault)
            for _ in range(_REFUSAL_LIMIT + 1):
                with contextlib.suppress(RuntimeError):
                    faulted = await _put(client, item["token"], b"# body\n")
                    assert faulted.status_code == 500

        accepted = await _put(client, item["token"], b"# body\n")

    assert accepted.status_code == 201, accepted.text


async def test_configured_refusal_limit_is_honoured(client, tmp_path, monkeypatch):
    """The limit is the deployment's configured value, echoed in the refusal.

    Anti-coincidental-pass: the configured limit differs from the default, so
    a handler with the default hard-coded fails on the second delivery's
    status and on the echoed figure.
    """
    monkeypatch.setenv("SAGE_MAX_TRANSFER_BYTES", "64")
    configured = 2
    assert configured != _REFUSAL_LIMIT

    with _profile("cloud", max_refused_deliveries=configured):
        item = await _mint_upload(tmp_path, "tight.md", b"placeholder")
        first = await _put(client, item["token"], b"y" * 200)
        second = await _put(client, item["token"], b"y" * 200)

    assert first.status_code == 413, first.text
    assert second.status_code == 410, second.text
    assert second.json()["code"] == "transfer_refusal_limit_reached"
    assert second.json()["detail"]["max_refused_deliveries"] == configured


async def test_upload_token_failures(client, tmp_path):
    """Missing/wrong token -> 410; a second PUT after success -> 409."""
    with _profile("cloud"):
        no_header = await client.put("/upload", content=b"x")
        assert no_header.status_code == 410
        assert no_header.json()["code"] == "transfer_token_invalid"

        wrong = await client.put("/upload", content=b"x", headers={"X-Upload-Token": "bogus.token"})
        assert wrong.status_code == 410

        item = await _mint_upload(tmp_path, "once.md", b"# once\n")
        first = await client.put(
            "/upload", content=b"# once\n", headers={"X-Upload-Token": item["token"]}
        )
        assert first.status_code == 201
        second = await client.put(
            "/upload", content=b"# again\n", headers={"X-Upload-Token": item["token"]}
        )
        assert second.status_code == 409
        assert second.json()["code"] == "transfer_token_already_used"


async def test_expired_upload_token_is_refused_at_the_endpoint(client, tmp_path, monkeypatch):
    """A lapsed token reaches the byte leg as the documented 410.

    The store's own suite proves the sweep's boundary; this proves the
    refusal survives the transport a caller actually meets, where the token
    is the sole credential and no bearer is in play.

    Anti-coincidental-pass: the token is delivered against successfully
    *before* the clock moves, so a 410 afterwards cannot be a malformed
    header or an unroutable id -- the same token worked a moment earlier.
    The error code is asserted alongside the status because a 410 alone is
    reachable from a missing header, which the sibling above already covers.
    """
    clock = _Clock()
    monkeypatch.setattr(
        _transfer, "_transfer_store", TransferStore(now=clock, staging_root=tmp_path / "staging")
    )

    with _profile("cloud"):
        item = await _mint_upload(tmp_path, "lapses.md", b"# lapses\n")

        live = await client.put(
            "/upload", content=b"# lapses\n", headers={"X-Upload-Token": item["token"]}
        )
        assert live.status_code == 201, live.text

        lapsed = await _mint_upload(tmp_path, "lapses2.md", b"# two\n")
        clock.advance(_TTL + 1)
        after = await client.put(
            "/upload", content=b"# two\n", headers={"X-Upload-Token": lapsed["token"]}
        )
        assert after.status_code == 410, after.text
        assert after.json()["code"] == "transfer_token_invalid"


async def test_minted_window_is_the_configured_lifetime(client, tmp_path, monkeypatch):
    """A minted recipe's window is the lifetime the stack config declares.

    This is the link that makes the figure the tool descriptions state a true
    statement to a caller. Its siblings pin the two ends and not the middle:
    the disclosure gate proves the docstrings agree with the config, and the
    expiry tests prove a token is dead past whatever window it was given.
    Neither reaches the one place the configured value becomes a minted
    window, so a mint honouring some other lifetime satisfies both while the
    caller is told 900 seconds and gets something else.

    Anti-coincidental-pass: the assertion is an equality against the injected
    clock plus the configured lifetime, not a bound. A bound admits exactly
    the defect -- halving the window still expires "after the mint" -- and
    halving it was the probe that found this gap, with 164 tests across four
    suites staying green.
    """
    clock = _Clock()
    monkeypatch.setattr(
        _transfer, "_transfer_store", TransferStore(now=clock, staging_root=tmp_path / "staging")
    )

    src = tmp_path / "caller_inbox" / "window.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"# window\n")

    with _profile("cloud"):
        recipe = _parse(await ingest_document(_VAULT_ID, str(src), "markdown"))

    assert recipe.get("status") == "upload_required", recipe
    minted_at = clock.now
    assert datetime.fromisoformat(recipe["expires_at"]) == minted_at + timedelta(seconds=_TTL)


async def test_download_token_failures(client, tmp_path):
    """Wrong token, mismatched URL id, and replay after redemption -> 410."""
    ingested = await _ingest_locally(tmp_path, "dl_fail.md", "# DF\n\nBody.")

    with _profile("cloud"):
        recipe = _parse(
            await get_document(_VAULT_ID, ingested["id"], write_to_path=str(tmp_path / "df_out.md"))
        )
        tid = recipe["transfer_id"]

        wrong = await client.get(f"/download/{tid}", headers={"X-Download-Token": "bogus.token"})
        assert wrong.status_code == 410

        # URL id disagreeing with the token's own id refuses without consuming.
        mismatch = await client.get(
            "/download/someotherid", headers={"X-Download-Token": recipe["token"]}
        )
        assert mismatch.status_code == 410

        ok = await client.get(f"/download/{tid}", headers={"X-Download-Token": recipe["token"]})
        assert ok.status_code == 200

        replay = await client.get(f"/download/{tid}", headers={"X-Download-Token": recipe["token"]})
        assert replay.status_code == 410


# ---------------------------------------------------------------------------
# Auth exemption
# ---------------------------------------------------------------------------


async def test_transfer_paths_exempt_from_bearer_auth(monkeypatch):
    """With auth enabled, the transfer endpoints answer bearer-less requests
    with their own token errors (410), while a sibling route still 401s.

    The sibling 401 is the positive control: if the fixture's auth were not
    actually enforcing, that assertion fails, so the exemption cannot pass
    vacuously.
    """
    from sage.auth import AuthenticatedPrincipal, AuthError, NoAuthValidator

    class _StubValidator:
        async def validate(self, token):
            if token == "good-token":
                return AuthenticatedPrincipal(subject="user-1", scopes=frozenset({"Sage.Access"}))
            raise AuthError(401, "invalid_token", "bad or missing token")

    def fake(auth_config):
        if auth_config is None or not auth_config.enabled:
            return NoAuthValidator()
        return _StubValidator()

    monkeypatch.setattr("sage.mcp_init.build_auth_validator", fake)

    enabled = SageCoreConfig(
        auth=StackAuthConfig(enabled=True, tenant_id="tid", audience="api://sage")
    )
    application = create_app(stack_config=enabled)
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        sibling = await c.get("/sage_vaults")
        assert sibling.status_code == 401  # positive control: auth is on

        upload = await c.put("/upload", content=b"x", headers={"X-Upload-Token": "bogus.token"})
        assert upload.status_code == 410  # token error, not a bearer challenge
        assert "www-authenticate" not in upload.headers

        download = await c.get("/download/xyz", headers={"X-Download-Token": "bogus.token"})
        assert download.status_code == 410
        assert "www-authenticate" not in download.headers


# ---------------------------------------------------------------------------
# The download leg's source reads reach the vault-source store
# ---------------------------------------------------------------------------


class _RefusingSourceStore(FilesystemVaultSourceStore):
    """A binding whose presence check is declined by the store behind it."""

    def __init__(self, root, refusal: Exception) -> None:
        super().__init__(root)
        self._refusal = refusal

    def source_exists(self, vault_id, storage_root, source_path):
        raise self._refusal


@pytest.mark.parametrize(
    ("retryable", "status", "code"),
    [
        (False, 502, "vault_source_store_refused"),
        (True, 503, "vault_source_store_unavailable"),
    ],
)
async def test_download_source_store_refusal_is_typed(
    client, tmp_path, monkeypatch, retryable, status, code
):
    """A store that declines the download leg's read reaches the caller as the
    typed refusal rather than a bare 500.

    The download leg is the third caller-facing surface reading a retained
    source, and its token is the sole credential -- so an untyped failure here
    costs the caller a spent token *and* tells them nothing about whether
    re-issuing the originating call is worth doing. The status is what carries
    that: 503 says try again, 502 says fix it at the store first.

    Anti-coincidental-pass: the two arms refuse with the same status and differ
    only in the flag the binding set, so a translation deriving transience from
    the status satisfies at most one. The `code` assertion also excludes the
    route's existing `content_file_missing`, which is what a binding reporting
    a genuinely absent source returns -- a different fact from one that
    declined to answer.
    """
    body = "# Refused download\n\n" + "line\n" * 50
    ingested = await _ingest_locally(tmp_path, "dl_refused.md", body)

    with _profile("cloud"):
        recipe = _parse(
            await get_document(_VAULT_ID, ingested["id"], write_to_path=str(tmp_path / "o.md"))
        )
        assert recipe.get("status") == "download_required", recipe

        # Wrapped as the resolver wraps it (CAS-ADR-043): the translation lives
        # on the binding, so a fake installed raw would reach the route untyped
        # and prove only that this test bypassed the mechanism under test.
        from sage.services.vault_source_errors import wrap_vault_source_store

        monkeypatch.setattr(
            "sage.mcp_init.resolve_stack_vault_source_store",
            lambda *a, **k: wrap_vault_source_store(
                _RefusingSourceStore(
                    tmp_path,
                    store_refusal(
                        409,
                        retryable=retryable,
                        operation="stat source",
                        target=recipe["transfer_id"],
                    ),
                )
            ),
        )
        resp = await client.get(
            f"/download/{recipe['transfer_id']}",
            headers={"X-Download-Token": recipe["token"]},
        )

    assert resp.status_code == status, resp.text
    payload = resp.json()
    assert payload["code"] == code
    assert payload["code"] != "content_file_missing"
    assert payload["detail"]["operation"] == "stat source"
    assert payload["detail"]["store_status"] == 409
    assert STORE_BODY not in json.dumps(payload)
