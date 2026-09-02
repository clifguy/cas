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

import pytest
from httpx import ASGITransport, AsyncClient

import sage.mcp_init as _mcp_init
import sage.mcp_server as _mcp
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.app import _initialize_services, create_app
from sage.config import SageCoreConfig, StackAuthConfig, VaultConfig
from sage.mcp_server import get_document, ingest_document, read_projection
from sage.services.transfer import get_transfer_store, reset_transfer_store

_VAULT_ID = "test_vault"
_BASE = "https://sage.test.example"


def _parse(result: str | dict) -> dict:
    if isinstance(result, dict):
        return result
    return json.loads(result)


@contextlib.contextmanager
def _profile(name: str, transfer_base: str | None = _BASE):
    """Pin profile + transfer coordinates, mirroring the confinement suite."""
    saved = _mcp_init._stack_config
    kwargs: dict = {"profile": name}
    if transfer_base is not None:
        kwargs["transfer"] = {"public_base_url": transfer_base}
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
    own temp tree. A refusal reported from there hands a hosted caller a path it
    never named and cannot act on -- the one shape of this detail that is worse
    than useless, since the caller's own file is what it would have to fix.

    Anti-coincidental-pass: the staged file keeps the caller's basename, so an
    ``endswith`` or basename assertion passes against the defect. Equality
    against the caller's absolute path is the only form that fails without the
    declared source carried on the transfer entry -- and the fixture asserts the
    two paths differ before relying on that.
    """
    body = b"# refused upload\n"
    # Dangling, so retention reaches its write exit and refuses the link there.
    imports = tmp_vault_dir / "sources" / "imports"
    imports.mkdir(parents=True, exist_ok=True)
    (imports / "refused_upload.md").symlink_to(tmp_path / "nowhere.md")

    supplied = str(tmp_path / "caller_inbox" / "refused_upload.md")

    with _profile("cloud"):
        item = await _mint_upload(tmp_path, "refused_upload.md", body)
        resp = await client.put("/upload", content=body, headers={"X-Upload-Token": item["token"]})
        assert resp.status_code == 201, resp.text

        # Read before redemption consumes the entry. This is the control the
        # assertion below rests on: the two paths must actually differ, or
        # reporting either one would satisfy it and the test would discriminate
        # nothing.
        staged = str(get_transfer_store()._entries[item["transfer_id"]].staged_path)
        assert staged != supplied

        done = _parse(
            await ingest_document(_VAULT_ID, source_type="markdown", transfer_token=item["token"])
        )

    assert done["error"] == "vault_source_path_refused", done
    assert done["detail"] == {"source_path": supplied}
    assert done["detail"]["source_path"] != staged


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


async def test_download_round_trip_projection(client, tmp_path):
    """Mint via ``read_projection(write_to_path=...)`` -> GET -> the streamed
    bytes are the projection text the recipe promised."""
    ingested = await _ingest_locally(tmp_path, "dl_proj.md", "# DP\n\nProjection body here.")
    await asyncio.sleep(0.5)  # let the projection land

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
