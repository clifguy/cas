"""The caller-local byte transfer over the Core API's JSON request surface.

Whether the server can read a caller's filesystem is a property of where the
two sit, which callers of both protocols share, so the two-phase transfer is a
capability of the operation rather than of the MCP tool that fronts it
(CAS-ADR-052). These tests drive ``POST /documents`` and
``POST /maintenance/restore-source-file`` over ASGI through the whole exchange:
an absolute caller path that the server cannot reach mints an upload recipe,
the bytes cross the upload leg, and the same operation redeems the token.

The not-co-located arm is the deployment profile, pinned around each call by
``_profile`` rather than around fixture setup: the profile is a call-time
signal. The delivery-shape refusals are compared whole against the literals
the MCP suite pins, so the two surfaces cannot drift apart in wording.
"""

import asyncio
import contextlib
import hashlib
import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import sage.mcp_init as _mcp_init
import sage.mcp_server as _mcp
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.api.errors import SourceDigestMismatchError
from sage.app import _initialize_services, create_app
from sage.config import SageCoreConfig, VaultConfig
from sage.mcp_server import ingest_document, restore_vault_source_file
from sage.models.schemas import IngestRequest
from sage.services.transfer import get_transfer_store, reset_transfer_store
from tests.sage.test_mcp_caller_fs_confinement import (
    _AMBIGUOUS_NAMING_SOURCE,
    _MISSING_NAMING_SOURCE,
)

_VAULT_ID = "test_vault"
_BASE = "https://sage.test.example"
_INGEST = f"/sage_vaults/{_VAULT_ID}/documents"
_RESTORE = f"/sage_vaults/{_VAULT_ID}/maintenance/restore-source-file"


@contextlib.contextmanager
def _profile(name: str, transfer_base: str | None = _BASE):
    """Pin the deployment profile and transfer coordinates for the block."""
    saved = _mcp_init._stack_config
    kwargs: dict = {"profile": name}
    if transfer_base is not None:
        kwargs["transfer"] = {"public_base_url": transfer_base}
    _mcp_init.set_stack_config(SageCoreConfig(**kwargs))
    try:
        yield
    finally:
        _mcp_init.set_stack_config(saved)


def _parse(result: str | dict) -> dict:
    return result if isinstance(result, dict) else json.loads(result)


@pytest.fixture(autouse=True)
def _fresh_transfer_store():
    reset_transfer_store()
    yield
    reset_transfer_store()


@pytest.fixture
async def vault(minimal_vault_config_dict):
    """One initialized vault, shared between the HTTP app and the MCP tools."""
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
        yield application, Path(config.vault.storage_root)
    finally:
        await asyncio.sleep(0.3)
        for services in application.state.vault_registry.values():
            services.close_timing()
            await services.graph_store.close()
        _mcp._vaults.pop(_VAULT_ID, None)


@pytest.fixture
async def client(vault):
    application, _root = vault
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as c:
        yield c


def _caller_file(tmp_path: Path, name: str, body: bytes) -> Path:
    src = tmp_path / "caller_inbox" / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(body)
    return src


async def _deliver(client: AsyncClient, item: dict, body: bytes) -> Path:
    """Send the bytes over the upload leg; return the staging directory."""
    staging_dir = get_transfer_store()._entries[item["transfer_id"]].staging_dir
    resp = await client.put("/upload", content=body, headers={"X-Upload-Token": item["token"]})
    assert resp.status_code == 201, resp.text
    return staging_dir


def _assert_recipe_for(body: dict, source: str) -> dict:
    assert body.get("status") == "upload_required", body
    assert body["method"] == "PUT"
    assert body["token_header"] == "X-Upload-Token"
    assert body["expires_at"]
    assert len(body["uploads"]) == 1, body
    (item,) = body["uploads"]
    assert item["source"] == source
    assert item["url"] == f"{_BASE}/upload"
    assert item["token"].startswith(item["transfer_id"] + ".")
    return item


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


async def test_rest_ingest_unreachable_absolute_source_mints_a_recipe(client, tmp_path):
    """An absolute caller path the server cannot reach earns a recipe.

    Anti-coincidental: before the gate sat beneath the route, this call read
    the path against the server's own tree and refused ``source_file_not_found``.
    ``id`` is absent, so a route that minted and also ingested fails.
    """
    src = _caller_file(tmp_path, "r1_note.md", b"# R1\n")

    with _profile("cloud"):
        resp = await client.post(_INGEST, json={"source": str(src), "source_type": "markdown"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    _assert_recipe_for(body, str(src))
    assert "id" not in body
    assert "document" not in body


async def test_rest_dry_run_validates_before_the_recipe(client):
    """A dry run over HTTP refuses a bad doc_type without a byte leg, and its
    valid twin returns the recipe naming the validators that ran."""
    source = "/caller/machine/inbox/r_dry.md"
    with _profile("cloud"):
        refused = await client.post(
            _INGEST,
            json={
                "source": source,
                "source_type": "markdown",
                "dry_run": True,
                "metadata": {"doc_type": "not_a_type"},
            },
        )
        valid = await client.post(
            _INGEST,
            json={
                "source": source,
                "source_type": "markdown",
                "dry_run": True,
                "metadata": {"doc_type": "note"},
            },
        )

    assert refused.json()["code"] == "invalid_doc_type", refused.text
    body = valid.json()
    item = _assert_recipe_for(body, source)
    assert "doc_type" in item["dry_run_validated"]


async def test_rest_ingest_redeems_the_transfer_token(client, tmp_path):
    """Mint, deliver over the upload leg, complete with the token alone.

    The content hash is computed here rather than read from any response, and
    the staging directory's removal shows the completion spent the token rather
    than reading the bytes and leaving them behind. The caller's file is gone
    before the completion, so a completion that read the path the caller named
    -- which this test host could reach, where a deployed server could not --
    instead of the staged bytes fails rather than passing on a shared disk.
    """
    body = b"# R2\n\nDelivered over the upload leg.\n"
    src = _caller_file(tmp_path, "r2_note.md", body)

    with _profile("cloud"):
        minted = await client.post(_INGEST, json={"source": str(src), "source_type": "markdown"})
        item = _assert_recipe_for(minted.json(), str(src))
        src.unlink()
        staging_dir = await _deliver(client, item, body)
        done = await client.post(
            _INGEST, json={"transfer_token": item["token"], "source_type": "markdown"}
        )

    assert done.status_code == 201, done.text
    document = done.json()["document"]
    assert document["source_path"] == "imports/r2_note.md"
    assert document["source_content_hash"] == "sha256:" + hashlib.sha256(body).hexdigest()
    assert not staging_dir.exists()


@pytest.mark.parametrize(
    ("shape", "code", "message"),
    [
        ("both", "ambiguous_ingest_source", _AMBIGUOUS_NAMING_SOURCE),
        ("neither", "missing_ingest_source", _MISSING_NAMING_SOURCE),
    ],
    ids=["ambiguous", "missing"],
)
async def test_rest_ingest_delivery_shape_refusals(client, tmp_path, shape, code, message):
    """Exactly one of ``source`` and ``transfer_token``; the message whole."""
    payload: dict = {"source_type": "markdown"}
    if shape == "both":
        payload |= {"source": str(tmp_path / "r3.md"), "transfer_token": "whatever"}

    with _profile("cloud"):
        resp = await client.post(_INGEST, json=payload)

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == code
    assert resp.json()["message"] == message


async def test_rest_ingest_near_miss_offers_transfer_token(client, tmp_path):
    """A misspelled completion argument is refused naming the declared one.

    The refusal itself predates this capability; what this pins is that
    ``transfer_token`` is among the names the operation declares, which the
    envelope derives from the bound request model.
    """
    payload = {
        "source": str(tmp_path / "r5.md"),
        "source_type": "markdown",
        "transfer_tokens": "whatever",
    }

    resp = await client.post(_INGEST, json=payload)

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert resp.json()["code"] == "unknown_parameter"
    assert detail["rejected_params"] == ["transfer_tokens"]
    assert "transfer_token" in detail["valid_params"]


async def test_rest_ingest_co_located_absolute_source_ingests_directly(client, tmp_path):
    """Where the server shares the caller's machine, the path is simply read.

    The control for the recipe tests: a gate that minted whatever the profile
    said would pass them and fail here. The transfer coordinates are configured,
    as on the recipe arm, so a gate that minted wherever a recipe *could* be
    minted fails here too.
    """
    body = b"# R9\n"
    src = _caller_file(tmp_path, "r9_note.md", body)

    with _profile("local"):
        resp = await client.post(_INGEST, json={"source": str(src), "source_type": "markdown"})

    assert resp.status_code == 201, resp.text
    assert resp.json()["document"]["source_content_hash"] == (
        "sha256:" + hashlib.sha256(body).hexdigest()
    )


# ---------------------------------------------------------------------------
# Declared digest: the same verdict wherever the bytes are read
# ---------------------------------------------------------------------------


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


async def test_colocated_ingest_refuses_digest_mismatch_before_retention(vault, client, tmp_path):
    """Where the server reads the caller's path itself, a declared digest the
    bytes do not have is refused before anything is retained -- with the code
    the upload leg uses, and identically on both surfaces.

    Anti-coincidental-pass: the retained-file assertion is paired with the
    matching-digest control below, which asserts the same path *does* appear
    on success, so it cannot pass merely because retention lands elsewhere. A
    check placed after retention would produce the right code and fail on the
    retained file. The surfaces are compared against each other, not against a
    literal, so wording drift on one fails here.
    """
    _application, root = vault
    body = b"# Declared digest mismatch\n"
    src = _caller_file(tmp_path, "mismatch_note.md", body)
    declared = _digest(b"not these bytes")

    with _profile("local"):
        rest = await client.post(
            _INGEST, json={"source": str(src), "source_type": "markdown", "sha256": declared}
        )
        mcp = _parse(
            await ingest_document(
                _VAULT_ID, source=str(src), source_type="markdown", sha256=declared
            )
        )

    assert rest.status_code == 400, rest.text
    assert rest.json()["code"] == "source_digest_mismatch"
    assert rest.json()["detail"]["declared_sha256"] == declared
    assert rest.json()["detail"]["delivered_sha256"] == _digest(body)
    assert mcp["error"] == rest.json()["code"]
    assert mcp["message"] == rest.json()["message"]
    assert not (root / "imports" / "mismatch_note.md").exists()


async def test_colocated_ingest_with_matching_digest_ingests(vault, client, tmp_path):
    """The positive control: the right digest, in bare spelling, ingests normally."""
    _application, root = vault
    body = b"# Declared digest match\n"
    src = _caller_file(tmp_path, "match_note.md", body)

    with _profile("local"):
        resp = await client.post(
            _INGEST,
            json={
                "source": str(src),
                "source_type": "markdown",
                "sha256": hashlib.sha256(body).hexdigest(),
            },
        )

    assert resp.status_code == 201, resp.text
    assert resp.json()["document"]["source_content_hash"] == _digest(body)
    assert (root / "imports" / "match_note.md").exists()


async def test_dry_run_reaches_same_digest_verdict(client, tmp_path):
    """A preview refuses the mismatch the real run would refuse.

    Anti-coincidental-pass: the preview path hashes the source on its own
    branch, so a check wired only into the real run returns a preview here.
    """
    body = b"# Previewed mismatch\n"
    src = _caller_file(tmp_path, "preview_note.md", body)

    with _profile("local"):
        resp = await client.post(
            _INGEST,
            json={
                "source": str(src),
                "source_type": "markdown",
                "sha256": _digest(b"other"),
                "dry_run": True,
            },
        )

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "source_digest_mismatch"


async def test_redeemed_token_completion_rechecks_declared_digest(client, tmp_path):
    """A completion call declaring a digest its staged bytes lack is refused,
    and the token goes back unspent.

    Anti-coincidental-pass: the bytes staged against the right binding, so the
    upload leg's check has already passed; only a check on the completion arm
    can refuse. The follow-up completion with the right digest must succeed on
    the same token, so a refusal that spent it fails.
    """
    body = b"# Redeemed with a declared digest\n"
    src = tmp_path / "caller_inbox" / "redeemed_note.md"

    with _profile("cloud"):
        minted = await client.post(
            _INGEST,
            json={"source": str(src), "source_type": "markdown", "sha256": _digest(body)},
        )
        item = _assert_recipe_for(minted.json(), str(src))
        await _deliver(client, item, body)

        refused = await client.post(
            _INGEST,
            json={
                "transfer_token": item["token"],
                "source_type": "markdown",
                "sha256": _digest(b"a different file"),
            },
        )
        done = await client.post(
            _INGEST,
            json={
                "transfer_token": item["token"],
                "source_type": "markdown",
                "sha256": _digest(body),
            },
        )

    assert refused.status_code == 400, refused.text
    assert refused.json()["code"] == "source_digest_mismatch"
    assert done.status_code == 201, done.text
    assert done.json()["document"]["source_content_hash"] == _digest(body)


@pytest.mark.parametrize("operation", ["ingest", "restore"])
async def test_malformed_sha256_is_refused_before_anything_is_minted(client, tmp_path, operation):
    """A digest that is not a well-formed sha256 is refused at the boundary on
    both surfaces, with no recipe minted.

    Anti-coincidental-pass: the call is otherwise one that mints a recipe --
    an absolute source the cloud profile cannot reach -- so a boundary that
    let the malformed digest through would answer ``upload_required`` rather
    than refuse; and the store is checked empty, so a refusal raised after
    minting fails too. The malformed value is hex-free, so a validator that
    only canonicalized spelling without checking shape would pass it.
    """
    src = tmp_path / "caller_inbox" / "malformed_digest.md"
    arguments = {"source": str(src), "sha256": "nothex"}

    with _profile("cloud"):
        if operation == "ingest":
            rest = await client.post(_INGEST, json={**arguments, "source_type": "markdown"})
            mcp = _parse(await ingest_document(_VAULT_ID, source_type="markdown", **arguments))
        else:
            rest = await client.post(_RESTORE, json=arguments)
            mcp = _parse(await restore_vault_source_file(_VAULT_ID, **arguments))

    assert rest.status_code == 400, rest.text
    assert rest.json()["code"] == "invalid_sha256"
    assert mcp["error"] == "invalid_sha256", mcp
    assert get_transfer_store()._entries == {}


async def test_rest_ingest_near_miss_offers_sha256(client, tmp_path):
    """``sha256`` is a declared field: a near-miss spelling is refused naming it."""
    payload = {
        "source": str(tmp_path / "r5b.md"),
        "source_type": "markdown",
        "sha_256": "whatever",
    }

    resp = await client.post(_INGEST, json=payload)

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "unknown_parameter"
    assert resp.json()["detail"]["rejected_params"] == ["sha_256"]
    assert "sha256" in resp.json()["detail"]["valid_params"]


async def test_rest_ingest_mints_for_a_windows_absolute_source(client):
    """A path absolute on the caller's platform is the caller's, whatever this one's.

    Read with this process's own semantics the spelling is relative and would
    resolve against the vault's store rather than minting.
    """
    spelling = r"C:\originals\r11_note.md"

    with _profile("cloud"):
        resp = await client.post(_INGEST, json={"source": spelling, "source_type": "markdown"})

    assert resp.status_code == 200, resp.text
    _assert_recipe_for(resp.json(), spelling)


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


async def _ingest_then_drift(client, root: Path, tmp_path: Path, name: str, body: bytes):
    """Ingest a caller file locally, then overwrite its retained copy."""
    src = _caller_file(tmp_path, name, body)
    with _profile("local", transfer_base=None):
        resp = await client.post(_INGEST, json={"source": str(src), "source_type": "markdown"})
    assert resp.status_code == 201, resp.text
    retained = root / resp.json()["document"]["source_path"]
    retained.write_bytes(b"something else wrote here")
    return src, retained


async def test_rest_restore_unreachable_absolute_source_mints_a_recipe(vault, client, tmp_path):
    """The restore mints on the same terms as the ingest, and repairs nothing yet."""
    _app, root = vault
    src, retained = await _ingest_then_drift(client, root, tmp_path, "r6_note.md", b"# R6\n")

    with _profile("cloud"):
        resp = await client.post(_RESTORE, json={"source": str(src)})

    assert resp.status_code == 200, resp.text
    _assert_recipe_for(resp.json(), str(src))
    assert retained.read_bytes() == b"something else wrote here"


async def test_rest_restore_redeems_the_transfer_token(vault, client, tmp_path):
    """The token completes the repair: the retained copy holds the originals.

    The caller's file is gone before the completion, so a repair that read the
    path the caller named rather than the staged bytes fails here.
    """
    _app, root = vault
    body = b"# R7\n\nThe original bytes.\n"
    src, retained = await _ingest_then_drift(client, root, tmp_path, "r7_note.md", body)

    with _profile("cloud"):
        minted = await client.post(_RESTORE, json={"source": str(src)})
        item = _assert_recipe_for(minted.json(), str(src))
        src.unlink()
        staging_dir = await _deliver(client, item, body)
        done = await client.post(_RESTORE, json={"transfer_token": item["token"]})

    assert done.status_code == 200, done.text
    assert done.json()["status"] == "restored"
    assert retained.read_bytes() == body
    assert not staging_dir.exists()


async def test_restore_digest_bound_upload_refuses_other_bytes(vault, client, tmp_path):
    """A restore's token binds to the declared digest exactly as an ingest's does.

    Anti-coincidental-pass: the wrong bytes are refused on the upload leg with
    the entry left retryable, and the *same* token then carries the right bytes
    through to a completed repair -- so a refusal that spent the token, or a
    restore mint that ignored the declaration, fails. The retained copy is read
    back at the end, so a repair that wrote anything but the declared bytes
    fails on content.
    """
    _app, root = vault
    body = b"# R11\n\nThe declared original.\n"
    src, retained = await _ingest_then_drift(client, root, tmp_path, "r11_note.md", body)

    with _profile("cloud"):
        minted = await client.post(_RESTORE, json={"source": str(src), "sha256": _digest(body)})
        item = _assert_recipe_for(minted.json(), str(src))
        assert item["sha256"] == _digest(body)

        refused = await client.put(
            "/upload", content=b"not the original", headers={"X-Upload-Token": item["token"]}
        )
        assert refused.status_code == 400, refused.text
        assert refused.json()["code"] == "source_digest_mismatch"
        assert get_transfer_store()._entries[item["transfer_id"]].state == "pending_bytes"

        await _deliver(client, item, body)
        done = await client.post(
            _RESTORE, json={"transfer_token": item["token"], "sha256": _digest(body)}
        )

    assert done.status_code == 200, done.text
    assert done.json()["status"] == "restored"
    assert retained.read_bytes() == body


async def test_colocated_restore_refuses_digest_mismatch_before_writing(vault, client, tmp_path):
    """Where the server reads the caller's path, a declared digest the bytes lack
    is refused before the retained copy is touched, identically on both surfaces.

    Anti-coincidental-pass: the retained copy is drifted, so a restore that
    ignored the declaration would repair it; the assertion that the drift is
    still there is what fails. The matching-digest control below repairs the
    same shape, so the drift surviving is owed to the refusal.
    """
    _app, root = vault
    body = b"# R12\n"
    src, retained = await _ingest_then_drift(client, root, tmp_path, "r12_note.md", body)
    declared = _digest(b"some other file")

    with _profile("local", transfer_base=None):
        rest = await client.post(_RESTORE, json={"source": str(src), "sha256": declared})
        mcp = _parse(await restore_vault_source_file(_VAULT_ID, source=str(src), sha256=declared))

    assert rest.status_code == 400, rest.text
    assert rest.json()["code"] == "source_digest_mismatch"
    assert rest.json()["detail"]["declared_sha256"] == declared
    assert rest.json()["detail"]["delivered_sha256"] == _digest(body)
    assert mcp["error"] == rest.json()["code"]
    assert mcp["message"] == rest.json()["message"]
    assert retained.read_bytes() == b"something else wrote here"


async def test_colocated_restore_with_matching_digest_repairs(vault, client, tmp_path):
    """The control: the right digest, in bare spelling, repairs as before."""
    _app, root = vault
    body = b"# R13\n"
    src, retained = await _ingest_then_drift(client, root, tmp_path, "r13_note.md", body)

    with _profile("local", transfer_base=None):
        resp = await client.post(
            _RESTORE, json={"source": str(src), "sha256": hashlib.sha256(body).hexdigest()}
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "restored"
    assert retained.read_bytes() == body


@pytest.mark.parametrize(
    ("shape", "code", "message"),
    [
        ("both", "ambiguous_ingest_source", _AMBIGUOUS_NAMING_SOURCE),
        ("neither", "missing_ingest_source", _MISSING_NAMING_SOURCE),
    ],
    ids=["ambiguous", "missing"],
)
async def test_rest_restore_delivery_shape_refusals(client, tmp_path, shape, code, message):
    payload: dict = {}
    if shape == "both":
        payload = {"source": str(tmp_path / "r8.md"), "transfer_token": "whatever"}

    with _profile("cloud"):
        resp = await client.post(_RESTORE, json=payload)

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == code
    assert resp.json()["message"] == message


async def test_rest_restore_ambiguous_outranks_relative_refusal(client):
    """The delivery shape is settled before the path's own shape is."""
    with _profile("local", transfer_base=None):
        resp = await client.post(
            _RESTORE, json={"source": "relative/x.md", "transfer_token": "whatever"}
        )

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "ambiguous_ingest_source"


async def test_rest_restore_mints_for_a_windows_absolute_source(client):
    spelling = r"C:\originals\drifted.pdf"

    with _profile("cloud"):
        resp = await client.post(_RESTORE, json={"source": spelling})

    assert resp.status_code == 200, resp.text
    _assert_recipe_for(resp.json(), spelling)


def test_declared_digest_with_no_digest_to_hold_it_to_is_refused(vault):
    """A declaration that cannot be checked is refused rather than admitted.

    Resident bytes no document records reach the guard with no digest. The
    positive arm is the same guard on the same request with a digest that
    matches, so the refusal is owed to the missing digest and not to the
    declaration alone.
    """
    application, _root = vault
    service = application.state.vault_registry[_VAULT_ID].ingestion_service
    declared = _digest(b"resident bytes")
    request = IngestRequest(source="resident.md", source_type="markdown", sha256=declared)

    service._validate_declared_digest(request, declared, "resident.md")
    with pytest.raises(SourceDigestMismatchError) as caught:
        service._validate_declared_digest(request, None, "resident.md")

    assert caught.value.detail == {
        "source": "resident.md",
        "declared_sha256": declared,
        "delivered_sha256": None,
    }


async def test_resolved_path_ingest_refuses_an_undelivered_request(vault):
    """The entry that applies no gate will not run on a delivery it cannot settle.

    It ingests a path already resolved, so a request still carrying a
    ``transfer_token``, or carrying no ``source``, would otherwise have its
    token ignored or fail on the missing path with no word about why.

    Each arm carries only the shape its guard refuses and matches that guard's
    own wording, so neither arm can pass on the other guard's refusal.
    """
    application, _root = vault
    service = application.state.vault_registry[_VAULT_ID].ingestion_service

    with pytest.raises(ValueError, match="carrying transfer_token"):
        await service.ingest(
            IngestRequest(source="/x/a.md", transfer_token="t", source_type="markdown")
        )
    with pytest.raises(ValueError, match="naming no source"):
        await service.ingest(IngestRequest(source_type="markdown"))


# ---------------------------------------------------------------------------
# Both surfaces, one contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("operation", ["ingest", "restore"])
@pytest.mark.parametrize("shape", ["both", "neither"])
async def test_delivery_shape_refusal_is_identical_on_both_surfaces(
    vault, client, tmp_path, operation, shape
):
    """The same malformed call is refused with the same code and message.

    Compared surface against surface rather than each against a literal, so a
    change to one surface's wording fails here even if the literal moved too.
    """
    arguments: dict = {}
    if shape == "both":
        arguments = {"source": str(tmp_path / "r10.md"), "transfer_token": "whatever"}

    with _profile("cloud"):
        if operation == "ingest":
            mcp = _parse(await ingest_document(_VAULT_ID, source_type="markdown", **arguments))
            rest = await client.post(_INGEST, json={"source_type": "markdown", **arguments})
        else:
            mcp = _parse(await restore_vault_source_file(_VAULT_ID, **arguments))
            rest = await client.post(_RESTORE, json=arguments)

    assert rest.status_code == 400, rest.text
    assert rest.json()["code"] == mcp["error"]
    assert rest.json()["message"] == mcp["message"]
