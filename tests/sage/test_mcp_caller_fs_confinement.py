"""Caller-filesystem confinement + transfer-recipe tests (cloud profile).

Under the cloud profile the SAGE server is a remote container that cannot see
the calling client's filesystem. These tests prove the two halves of the
caller-local byte channel at the tool surface:

- Recipes (B*): the path-bearing tools that move file bytes -- ingest with an
  absolute ``source``, ``get_document``/``read_projection`` with
  ``write_to_path`` -- return a structured transfer recipe instead of touching
  the container's own tree. The caller's environment executes the recipe
  against the token-gated transfer endpoints, and (for uploads) the same tool
  is called back with the transfer token to complete the ingest.
- Confinement: ``list_directory`` stays refused (the caller enumerates
  locally), and the inline export modes keep working (B2/B5) -- the recipe
  branch must not widen or narrow the T-series confinement perimeter.

The co-located (local) profile stays unchanged: path forms still work (L*),
and the existing ``test_mcp_profile_invariance`` suite is the broader
local-profile regression. The new axis is the deployment profile, pinned
per-test by the ``_profile`` context manager (which also pins the transfer
coordinates recipes are minted from). Each test runs once per vault-source
backend via the parameterized ``vault_source_backend`` fixture (from
conftest); store resolution stays on that backend because the vault-source
builder's env override outranks the profile, so ``_profile("cloud")`` changes
only the caller-visible behavior, not which store answers.
"""

import asyncio
import contextlib
import hashlib
import json
from pathlib import Path

import pytest

import sage.mcp_init as _mcp_init
import sage.mcp_server as _mcp
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.config import SageCoreConfig, VaultConfig
from sage.mcp_init import initialize_services
from sage.mcp_server import (
    bulk_ingest_document,
    get_document,
    ingest_document,
    list_directory,
    read_projection,
    restore_vault_source_file,
)
from sage.profiles import caller_local_filesystem_available
from sage.services.transfer import get_transfer_store, reset_transfer_store
from sage.services.vault_registry import VaultRegistryService
from tests.sage.conftest import initialize_services_for_test

_VAULT_ID = "test_vault"

# Pinned public base for recipe minting; recipes must embed it verbatim.
_BASE = "https://sage.test.example"


def _parse(result: str | dict) -> dict:
    if isinstance(result, dict):
        return result
    return json.loads(result)


@contextlib.contextmanager
def _profile(name: str, transfer_base: str | None = None):
    """Pin the active deployment profile for the duration of the block.

    Sets the module-level stack config to the named profile and restores the
    prior value on exit, so a test can drive the cloud-profile code paths
    without standing up a cloud stack. ``transfer_base``, when given, pins
    the transfer channel's public base URL so recipe minting has coordinates;
    leaving it unset exercises the not-configured refusal.
    """
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
    """Isolate the process-wide transfer store per test."""
    reset_transfer_store()
    yield
    reset_transfer_store()


@pytest.fixture
async def confined_vault(minimal_vault_config_dict, vault_source_backend):
    """One registered vault over the parameterized source binding.

    Built under the default (local) profile -- the profile is a call-time
    signal, so a test enters ``_profile("cloud")`` around the calls it wants to
    confine, not around fixture setup.
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    async with initialize_services_for_test(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
        # Wired so the maintenance surface is reachable: the source-file restore
        # reads bytes from the caller's filesystem like an ingest does, so it is
        # subject to the same confinement gate and belongs in this suite.
        registry_service=VaultRegistryService(_mcp._vaults, initialize_services),
    ) as services:
        _mcp._vaults[_VAULT_ID] = services
        try:
            yield services, config, vault_source_backend
        finally:
            # Drain the fire-and-forget pipeline tasks ingest dispatches before
            # services tear down (same discipline as the profile-invariance fixture).
            await asyncio.sleep(0.3)
            _mcp._vaults.pop(_VAULT_ID, None)


async def _ingest_local_file(tmp_path, name: str, body: str) -> tuple:
    """Write a caller-local file (outside storage_root) and ingest it under the
    active profile (local by default)."""
    inbox = tmp_path / "caller_inbox"
    inbox.mkdir(exist_ok=True)
    src = inbox / name
    src.write_text(body)
    result = _parse(await ingest_document(_VAULT_ID, str(src), "markdown"))
    return src, result


def _stage_upload(token: str, body: bytes) -> Path:
    """Deliver bytes for a minted upload the way the upload endpoint does.

    The HTTP leg itself is covered by the endpoint tests; here the store is
    driven directly so the tool-surface tests stay transport-free. Returns the
    staging directory so a caller can assert the completion leg reclaimed it.
    """
    store = get_transfer_store()
    entry = store.begin_upload(token)
    entry.staged_path.write_bytes(body)
    store.finish_upload(entry.transfer_id, size=len(body), sha256=hashlib.sha256(body).hexdigest())
    return entry.staging_dir


# ---------------------------------------------------------------------------
# CFV-pred: the profile predicate
# ---------------------------------------------------------------------------


def test_cfv_pred_predicate_maps_profile_to_visibility():
    """CFV-pred: the caller-filesystem predicate is True under the local
    profile and False under the cloud profile.

    Anti-coincidental: a predicate hardcoded to a constant fails one of the two
    legs. The recipe and confinement tests below assert behavior *under cloud*;
    this test is what ties that behavior to the real profile.
    """
    assert caller_local_filesystem_available("local") is True
    assert caller_local_filesystem_available("cloud") is False


# ---------------------------------------------------------------------------
# B*: recipes from the path-bearing byte movers (cloud profile)
# ---------------------------------------------------------------------------


async def test_b1_ingest_absolute_source_returns_upload_recipe(confined_vault, tmp_path):
    """B1: an absolute caller ``source`` path under the cloud profile returns
    a structured upload recipe -- not an error, not a container read.

    The file exists on this (co-located test) machine, so without the profile
    gate the ingest would *succeed*; the recipe is what proves the gate fires.
    Field assertions pin the executable half of the recipe (URL embeds the
    pinned base verbatim, header name, per-file token) so a refusal envelope
    or a normal ingest response cannot pass.
    """
    _services, _config, _handle = confined_vault
    src = tmp_path / "caller_inbox" / "confined.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# Confined\n\nOn the caller machine.")

    with _profile("cloud", transfer_base=_BASE):
        result = _parse(await ingest_document(_VAULT_ID, str(src), "markdown"))

    assert "error" not in result, result
    assert result["status"] == "upload_required"
    assert result["method"] == "PUT"
    assert result["token_header"] == "X-Upload-Token"
    assert result["expires_at"]
    assert len(result["uploads"]) == 1
    item = result["uploads"][0]
    assert item["source"] == str(src)
    assert item["url"] == f"{_BASE}/upload"
    assert item["token"].startswith(item["transfer_id"] + ".")
    # No document was created by the mint.
    assert "id" not in result


async def test_b1b_ingest_completion_with_transfer_token(confined_vault, tmp_path):
    """B1b: after the byte leg, calling ``ingest_document`` back with the
    recipe's token completes a normal ingest of the staged bytes.

    Anti-coincidental: the retained bytes must equal the caller file's bytes
    -- a completion that ingested an empty or wrong staged file fails on
    content, not just on status.
    """
    _services, config, handle = confined_vault
    body = b"# B1b\n\nDelivered through the transfer channel."
    src = tmp_path / "caller_inbox" / "b1b_note.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(body)

    with _profile("cloud", transfer_base=_BASE):
        recipe = _parse(await ingest_document(_VAULT_ID, str(src), "markdown"))
        token = recipe["uploads"][0]["token"]
        staging_dir = _stage_upload(token, body)
        assert staging_dir.exists()
        result = _parse(
            await ingest_document(_VAULT_ID, source_type="markdown", transfer_token=token)
        )

    assert "error" not in result, result
    assert result["source_path"] == "imports/b1b_note.md"
    retained = handle.retained_bytes(config.vault.storage_root, "imports/b1b_note.md")
    assert retained == body
    # The completion leg owns the staging directory and must reclaim it; a
    # redemption that skipped the cleanup leaves the staged bytes behind
    # without changing any part of the response.
    assert not staging_dir.exists(), "the completion leg must reclaim its staging directory"
    if handle.fake_client is not None:
        assert handle.fake_client.source_uploads == 1


async def test_b1c_completion_token_failures(confined_vault, tmp_path):
    """B1c: the completion leg's failure modes are structured and distinct --
    an unredeemable token is ``transfer_token_invalid`` (whether unknown or
    already consumed), and a valid token whose bytes never arrived is
    ``transfer_not_staged`` (the token stays valid)."""
    _services, _config, _handle = confined_vault
    body = b"# B1c\n"
    src = tmp_path / "caller_inbox" / "b1c_note.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(body)

    with _profile("cloud", transfer_base=_BASE):
        unknown = _parse(
            await ingest_document(
                _VAULT_ID, source_type="markdown", transfer_token="nope.not-a-token"
            )
        )
        assert unknown["error"] == "transfer_token_invalid", unknown

        recipe = _parse(await ingest_document(_VAULT_ID, str(src), "markdown"))
        token = recipe["uploads"][0]["token"]

        premature = _parse(
            await ingest_document(_VAULT_ID, source_type="markdown", transfer_token=token)
        )
        assert premature["error"] == "transfer_not_staged", premature

        _stage_upload(token, body)
        done = _parse(
            await ingest_document(_VAULT_ID, source_type="markdown", transfer_token=token)
        )
        assert "error" not in done, done

        replay = _parse(
            await ingest_document(_VAULT_ID, source_type="markdown", transfer_token=token)
        )
        assert replay["error"] == "transfer_token_invalid", replay


async def test_b1d_ingest_exactly_one_source(confined_vault, tmp_path):
    """B1d: supplying both a path source and a transfer token, or neither, is
    a structured validation error."""
    _services, _config, _handle = confined_vault
    src = tmp_path / "either.md"
    src.write_text("# Either\n")

    with _profile("cloud", transfer_base=_BASE):
        both = _parse(
            await ingest_document(
                _VAULT_ID,
                source=str(src),
                source_type="markdown",
                transfer_token="x.y",
            )
        )
        neither = _parse(await ingest_document(_VAULT_ID, source_type="markdown"))

    assert both["error"] == "ambiguous_ingest_source", both
    assert neither["error"] == "missing_ingest_source", neither


async def test_b1e_mint_without_public_base_url_fails_loud(confined_vault, tmp_path):
    """B1e: under the cloud profile with no ``transfer.public_base_url``
    declared, minting fails with a structured config error rather than
    emitting a recipe whose URL cannot work."""
    _services, _config, _handle = confined_vault
    src = tmp_path / "caller_inbox" / "unconfigured.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# Unconfigured\n")

    with _profile("cloud"):
        result = _parse(await ingest_document(_VAULT_ID, str(src), "markdown"))

    assert result["error"] == "transfer_endpoint_not_configured", result


async def test_b2_list_directory_refused_under_cloud(confined_vault, tmp_path):
    """B2: directory discovery is refused under the cloud profile rather than
    walking and content-hashing the container tree.

    A real file is seeded in the scanned directory: without the guard,
    ``list_directory`` would enumerate it. The refusal (no ``files`` key) proves
    the walk never happened -- the direct analogue of the ``list_directory('/')``
    disclosure probe. Discovery has no recipe: the caller enumerates its own
    filesystem locally.
    """
    _services, _config, _handle = confined_vault
    scan_dir = tmp_path / "caller_scan"
    scan_dir.mkdir()
    (scan_dir / "seed_note.md").write_text("# Seed\n\nWould be enumerated without the guard.")

    with _profile("cloud", transfer_base=_BASE):
        result = _parse(await list_directory(_VAULT_ID, str(scan_dir)))

    assert result["error"] == "caller_filesystem_unavailable", result
    assert "files" not in result  # the walk never ran


async def test_b3_get_document_write_to_path_returns_download_recipe(confined_vault, tmp_path):
    """B3: ``get_document(write_to_path=...)`` under the cloud profile returns
    a download recipe, and no file is written to the container.

    Anti-coincidental: the recipe's ``content_hash``/``content_size`` must
    equal the independently computed hash and size of the ingested source
    bytes -- a hardcoded or wrong-document promise fails here, and the skill's
    local verification downstream would inherit the same guarantee.
    """
    _services, _config, _handle = confined_vault
    body = "# B3\n\nExport me."
    src, ingest = await _ingest_local_file(tmp_path, "b3_note.md", body)
    target = tmp_path / "caller_out" / "b3_export.md"
    target.parent.mkdir()

    with _profile("cloud", transfer_base=_BASE):
        result = _parse(await get_document(_VAULT_ID, ingest["id"], write_to_path=str(target)))

    assert "error" not in result, result
    assert result["status"] == "download_required"
    assert result["method"] == "GET"
    assert result["token_header"] == "X-Download-Token"
    assert result["url"] == f"{_BASE}/download/{result['transfer_id']}"
    assert result["write_to_path"] == str(target)
    assert result["filename"] == "b3_note.md"
    raw = src.read_bytes()
    assert result["content_size"] == len(raw)
    assert result["content_hash"] == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert not target.exists()  # nothing written to the container


async def test_b4_read_projection_write_to_path_returns_download_recipe(confined_vault, tmp_path):
    """B4: ``read_projection(write_to_path=...)`` under the cloud profile
    returns a download recipe whose redemption yields the projection text, and
    no file is written to the container."""
    _services, _config, _handle = confined_vault
    _src, ingest = await _ingest_local_file(tmp_path, "b4_note.md", "# B4\n\nProjection body.")
    await asyncio.sleep(0.5)  # let the projection land
    target = tmp_path / "caller_out" / "b4_projection.md"
    target.parent.mkdir()

    with _profile("cloud", transfer_base=_BASE):
        result = _parse(await read_projection(_VAULT_ID, ingest["id"], write_to_path=str(target)))

        assert "error" not in result, result
        assert result["status"] == "download_required"
        assert result["url"] == f"{_BASE}/download/{result['transfer_id']}"
        assert not target.exists()

        # Redeem at the store seam (the HTTP leg is covered by the endpoint
        # tests): the spooled bytes are the projection text the recipe promised.
        entry = get_transfer_store().redeem_download(result["token"])
        spooled = entry.spool_path.read_bytes()
        entry.cleanup()

    assert len(spooled) == result["content_size"]
    assert "Projection body." in spooled.decode("utf-8")


async def test_b5_inline_export_still_works_under_cloud(confined_vault, tmp_path):
    """B5: the inline export modes -- ``include_content`` and inline
    ``read_projection`` -- keep returning bytes under the cloud profile. They
    remain available alongside the recipe path and must not be over-gated."""
    _services, _config, _handle = confined_vault
    body = "# B5\n\nInline export body."
    _src, ingest = await _ingest_local_file(tmp_path, "b5_note.md", body)
    await asyncio.sleep(0.5)  # let the projection land

    with _profile("cloud", transfer_base=_BASE):
        doc = _parse(await get_document(_VAULT_ID, ingest["id"], include_content=True))
        projection = _parse(await read_projection(_VAULT_ID, ingest["id"]))

    assert "error" not in doc, doc
    assert doc.get("content") is not None
    assert "error" not in projection, projection
    assert projection.get("projection_text") is not None


async def test_b6_bulk_ingest_returns_per_file_tokens_and_completes(confined_vault, tmp_path):
    """B6: ``bulk_ingest_document`` with caller-local absolute paths mints one
    upload leg per file in a single recipe; completion entries carrying the
    tokens ingest the staged bytes.

    Two files share a basename deliberately: per-entry staging must keep their
    bytes distinct (the same property the shared-staging clobber bug violated),
    so both documents land and neither is a duplicate of the other.
    """
    _services, _config, _handle = confined_vault
    alpha = b"# Same name\n\nFirst distinct body."
    beta = b"# Same name\n\nSecond distinct body."
    dir_a = tmp_path / "caller_a"
    dir_b = tmp_path / "caller_b"
    dir_a.mkdir()
    dir_b.mkdir()
    (dir_a / "same_name.md").write_bytes(alpha)
    (dir_b / "same_name.md").write_bytes(beta)

    with _profile("cloud", transfer_base=_BASE):
        recipe = _parse(
            await bulk_ingest_document(
                _VAULT_ID,
                [
                    {"file_path": str(dir_a / "same_name.md"), "source_type": "markdown"},
                    {"file_path": str(dir_b / "same_name.md"), "source_type": "markdown"},
                ],
            )
        )

        assert "error" not in recipe, recipe
        assert recipe["status"] == "upload_required"
        assert len(recipe["uploads"]) == 2
        tokens = [item["token"] for item in recipe["uploads"]]
        assert tokens[0] != tokens[1]
        assert [item["source"] for item in recipe["uploads"]] == [
            str(dir_a / "same_name.md"),
            str(dir_b / "same_name.md"),
        ]

        _stage_upload(tokens[0], alpha)
        _stage_upload(tokens[1], beta)

        result = _parse(
            await bulk_ingest_document(
                _VAULT_ID,
                [
                    {"transfer_token": tokens[0], "source_type": "markdown"},
                    {"transfer_token": tokens[1], "source_type": "markdown"},
                ],
            )
        )

    assert result.get("error_count") == 0, result
    assert result["documents_created"]["new"] == 2


# ---------------------------------------------------------------------------
# L: the local profile is unchanged (paired counterpart to B1/B2)
# ---------------------------------------------------------------------------


async def test_l_local_profile_allows_path_forms(confined_vault, tmp_path):
    """L: under the local profile the path forms still work -- neither the
    confinement guard nor the recipe branch may fire when the server shares
    the caller's machine (including the local-over-HTTP topology).

    Anti-coincidental for the predicate: a predicate that mislabels local as
    cloud would return a recipe or a refusal here, so this test fails if the
    gate over-fires in either direction.
    """
    _services, _config, _handle = confined_vault
    src = tmp_path / "caller_inbox" / "local_ok.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# Local ok\n\nCo-located.")
    scan_dir = tmp_path / "caller_scan_local"
    scan_dir.mkdir()
    (scan_dir / "local_ok.md").write_text("# scan\n")

    with _profile("local"):
        ingest = _parse(await ingest_document(_VAULT_ID, str(src), "markdown"))
        listing = _parse(await list_directory(_VAULT_ID, str(scan_dir)))

    assert "error" not in ingest, ingest
    assert "status" not in ingest or ingest.get("status") != "upload_required"
    assert ingest["source_path"] == "imports/local_ok.md"
    assert set(listing) == {"files", "warnings", "truncated"}, listing


async def test_b7_restore_source_file_recipe_and_completion(confined_vault, tmp_path):
    """B7: the source-file restore carries the same two-phase caller-local
    contract as ingest -- an absolute source under the cloud profile mints a
    recipe, and the recipe's token completes the repair.

    Without this, the restore tool's claim to apply "the same caller-local gate
    the ingest tool applies" rested on a comment. The tool is the only write
    surface besides ingest that reads bytes from the caller's filesystem, so a
    gate that silently did not fire here would read the operator's server-side
    filesystem instead.

    Anti-coincidental-pass: the drift is real before the completion leg and the
    retained bytes are asserted afterwards, so a completion that staged nothing
    (or restored the wrong file) fails on content rather than on status. The
    recipe leg asserts no repair happened, so a gate that minted a recipe *and*
    wrote would fail too.
    """
    _services, config, handle = confined_vault
    body = b"# B7\n\nThe original bytes.\n"
    src = tmp_path / "caller_inbox" / "b7_note.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(body)

    with _profile("local"):
        ingested = _parse(await ingest_document(_VAULT_ID, str(src), "markdown"))
    assert "error" not in ingested, ingested
    retained_path = ingested["source_path"]

    drifted = b"something else wrote here"
    handle.write_retained_bytes(config.vault.storage_root, retained_path, drifted)

    with _profile("cloud", transfer_base=_BASE):
        recipe = _parse(await restore_vault_source_file(_VAULT_ID, source=str(src)))

        assert recipe["status"] == "upload_required"
        assert recipe["uploads"][0]["source"] == str(src)
        assert handle.retained_bytes(config.vault.storage_root, retained_path) == drifted, (
            "minting a recipe must not itself repair anything"
        )

        token = recipe["uploads"][0]["token"]
        _stage_upload(token, body)
        result = _parse(await restore_vault_source_file(_VAULT_ID, transfer_token=token))

    assert "error" not in result, result
    assert result["status"] == "restored"
    assert result["source_path"] == retained_path
    assert handle.retained_bytes(config.vault.storage_root, retained_path) == body


async def test_b7b_restore_source_file_exactly_one_delivery_shape(confined_vault, tmp_path):
    """B7b: the restore takes exactly one of ``source`` or ``transfer_token``,
    matching ingest's contract rather than silently preferring one.

    A caller generalizing ingest's completion shape must not find this tool
    narrower, and supplying both must not resolve to whichever the
    implementation happens to check first.
    """
    _services, _config, _handle = confined_vault
    src = tmp_path / "caller_inbox" / "b7b_note.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# B7b\n")

    with _profile("local"):
        both = _parse(
            await restore_vault_source_file(_VAULT_ID, source=str(src), transfer_token="whatever")
        )
        neither = _parse(await restore_vault_source_file(_VAULT_ID))
        relative = _parse(await restore_vault_source_file(_VAULT_ID, source="relative/x.md"))

    assert both["error"] == "ambiguous_ingest_source", both
    assert neither["error"] == "missing_ingest_source", neither
    assert relative["error"] == "restore_source_not_absolute", relative


# ---------------------------------------------------------------------------
# L2/L3, B6b, B7c: the gate's contract at every tool that carries it
#
# The delivery gate is expressed once and applied by each tool that reads
# bytes from the caller's filesystem. These tests hold the applications
# honest from the tool surface, which is the only altitude at which a tool
# that failed to apply the gate is visible: a test of the gate itself would
# pass while a tool bypassed it entirely.
# ---------------------------------------------------------------------------


async def test_l2_bulk_ingest_local_profile_does_not_mint(confined_vault, tmp_path):
    """L2: under the local profile the batch tool ingests caller-local paths
    directly, minting nothing -- the local counterpart to B6.

    Anti-coincidental for the gate's direction: a gate that over-fires (mints
    when the server *can* read the caller's tree) returns a recipe here, so
    the retained bytes never appear. The assertion is on the retained bytes
    rather than the summary counts alone, so a recipe that somehow also
    reported two documents still fails.
    """
    _services, config, handle = confined_vault
    inbox = tmp_path / "caller_inbox"
    inbox.mkdir()
    one = inbox / "l2_alpha.md"
    one.write_bytes(b"# L2 alpha\n\nFirst body.")
    two = inbox / "l2_beta.md"
    two.write_bytes(b"# L2 beta\n\nSecond body.")

    with _profile("local"):
        result = _parse(
            await bulk_ingest_document(
                _VAULT_ID,
                [
                    {"file_path": str(one), "source_type": "markdown"},
                    {"file_path": str(two), "source_type": "markdown"},
                ],
            )
        )

    assert "error" not in result, result
    assert result.get("status") != "upload_required", result
    assert "uploads" not in result, result
    assert result.get("error_count") == 0, result
    assert result["documents_created"]["new"] == 2
    for src in (one, two):
        retained = handle.retained_bytes(config.vault.storage_root, f"imports/{src.name}")
        assert retained == src.read_bytes()


async def test_l3_restore_local_profile_repairs_without_minting(confined_vault, tmp_path):
    """L3: under the local profile the source-file restore repairs from the
    caller's own path, minting nothing -- the local counterpart to B7.

    The only exercise of this tool under a pinned local profile at the tool
    surface; the profile-invariance suite reaches the repair through the
    maintenance service, which sits below the gate and so cannot see it.

    Anti-coincidental: the retained copy is drifted first and asserted
    repaired afterwards, so a call that minted a recipe instead of repairing
    fails on the bytes rather than only on a status string.
    """
    _services, config, handle = confined_vault
    body = b"# L3\n\nThe original bytes.\n"
    src = tmp_path / "caller_inbox" / "l3_note.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(body)

    with _profile("local"):
        ingested = _parse(await ingest_document(_VAULT_ID, str(src), "markdown"))
        assert "error" not in ingested, ingested
        retained_path = ingested["source_path"]

        drifted = b"something else wrote here"
        handle.write_retained_bytes(config.vault.storage_root, retained_path, drifted)
        assert handle.retained_bytes(config.vault.storage_root, retained_path) == drifted

        result = _parse(await restore_vault_source_file(_VAULT_ID, source=str(src)))

    assert "error" not in result, result
    assert result.get("status") != "upload_required", result
    assert "uploads" not in result, result
    assert result["status"] == "restored"
    assert result["source_path"] == retained_path
    assert handle.retained_bytes(config.vault.storage_root, retained_path) == body


async def test_b6b_bulk_ingest_exactly_one_delivery_shape(confined_vault, tmp_path):
    """B6b: every batch entry takes exactly one of ``file_path`` or
    ``transfer_token``, and the whole batch is refused before any token is
    redeemed or any file ingested.

    The batch tool's half of the contract B1d and B7b pin for the single-file
    tools. Without it the batch tool could publish a narrower or wider
    contract than its siblings and nothing would notice.

    Anti-coincidental on both orderings the gate has to get right. The mixed
    batch pairs a *staged* entry and an *absolute path* with a malformed one,
    and redeems the staged entry afterwards. A per-entry check that refused
    only on reaching the malformed entry consumes the token on the way, so
    the completion finds nothing to redeem; a check that minted before
    validating answers ``upload_required`` off the absolute path instead of
    refusing. Either mutant fails on an assertion rather than passing on the
    error code alone.
    """
    _services, _config, _handle = confined_vault
    body = b"# B6b\n"
    # A path, deliberately not a file: nothing here reads it. The refusals
    # settle on delivery shape before any file access, minting builds a recipe
    # from the path string, and the completion's bytes arrive through staging.
    src = tmp_path / "caller_inbox" / "b6b_note.md"

    with _profile("local"):
        both = _parse(
            await bulk_ingest_document(
                _VAULT_ID,
                [
                    {
                        "file_path": str(src),
                        "transfer_token": "whatever",
                        "source_type": "markdown",
                    }
                ],
            )
        )
        neither = _parse(await bulk_ingest_document(_VAULT_ID, [{"source_type": "markdown"}]))

    assert both["error"] == "ambiguous_ingest_source", both
    assert neither["error"] == "missing_ingest_source", neither

    with _profile("cloud", transfer_base=_BASE):
        recipe = _parse(
            await bulk_ingest_document(
                _VAULT_ID, [{"file_path": str(src), "source_type": "markdown"}]
            )
        )
        token = recipe["uploads"][0]["token"]
        _stage_upload(token, body)

        # The absolute path is what makes this discriminating: it is the one
        # entry a mint would fire on, so a batch that minted before validating
        # would answer ``upload_required`` here instead of refusing.
        mixed = _parse(
            await bulk_ingest_document(
                _VAULT_ID,
                [
                    {"transfer_token": token, "source_type": "markdown"},
                    {"file_path": str(tmp_path / "b6b_absolute.md"), "source_type": "markdown"},
                    {"source_type": "markdown"},
                ],
            )
        )
        assert mixed["error"] == "missing_ingest_source", mixed
        assert mixed.get("status") != "upload_required", mixed
        assert "documents_created" not in mixed, (
            "a malformed batch must refuse before ingesting any of its entries"
        )

        # The refusal consumed nothing: the staged token is still redeemable.
        # A per-entry check would have consumed it before reaching the
        # malformed entry, and this completion would fail as
        # ``transfer_token_invalid``.
        completed = _parse(
            await bulk_ingest_document(
                _VAULT_ID, [{"transfer_token": token, "source_type": "markdown"}]
            )
        )

    assert "error" not in completed, completed
    assert completed.get("error_count") == 0, completed
    assert completed["documents_created"]["new"] == 1


async def test_b7c_restore_ambiguous_outranks_relative_refusal(confined_vault):
    """B7c: supplying both delivery shapes *and* a relative path refuses as
    ``ambiguous_ingest_source``, not ``restore_source_not_absolute``.

    The delivery contract is settled before the path's own shape is. B7b
    drives both/neither/relative as three separate calls and so cannot see
    their interaction; this pins which refusal wins when they collide, so the
    two checks cannot silently swap places.
    """
    _services, _config, _handle = confined_vault

    with _profile("local"):
        result = _parse(
            await restore_vault_source_file(
                _VAULT_ID, source="relative/x.md", transfer_token="whatever"
            )
        )

    assert result["error"] == "ambiguous_ingest_source", result


async def test_b6c_cloud_mixed_batch_mints_for_paths_and_spares_tokens(confined_vault, tmp_path):
    """B6c: a cloud batch mixing an absolute path with an already-staged token
    answers with a recipe covering only the path, and leaves the token
    redeemable.

    The mint decision is made for the whole batch before any redemption, so a
    call the gate answers with a recipe consumes nothing. Nothing else covers
    a well-formed batch that carries both delivery shapes at once.

    Anti-coincidental on three rivals. A gate that redeemed before minting
    consumes the token, so the completion leg fails ``transfer_token_invalid``
    rather than landing a document. A gate that minted for every entry rather
    than the absolute ones returns two upload legs. A gate that minted for the
    wrong entry names the staged entry's source in the recipe, which the
    per-item source assertion catches.
    """
    _services, _config, _handle = confined_vault
    staged_body = b"# B6c staged\n\nDelivered ahead of the call."
    # Both are paths, deliberately not files: the gate mints from the path
    # string without reading it, and the staged entry's bytes reach the vault
    # through the staging directory rather than from this location.
    staged_src = tmp_path / "caller_inbox" / "b6c_staged.md"
    unstaged_src = tmp_path / "caller_inbox" / "b6c_unstaged.md"

    with _profile("cloud", transfer_base=_BASE):
        first = _parse(
            await bulk_ingest_document(
                _VAULT_ID, [{"file_path": str(staged_src), "source_type": "markdown"}]
            )
        )
        token = first["uploads"][0]["token"]
        _stage_upload(token, staged_body)

        mixed = _parse(
            await bulk_ingest_document(
                _VAULT_ID,
                [
                    {"transfer_token": token, "source_type": "markdown"},
                    {"file_path": str(unstaged_src), "source_type": "markdown"},
                ],
            )
        )

        assert "error" not in mixed, mixed
        assert mixed["status"] == "upload_required", mixed
        assert len(mixed["uploads"]) == 1, mixed
        assert mixed["uploads"][0]["source"] == str(unstaged_src), mixed
        assert "documents_created" not in mixed, (
            "a batch answered with a recipe must not ingest any of its entries"
        )

        # The recipe spared the sibling token, so it still redeems.
        completed = _parse(
            await bulk_ingest_document(
                _VAULT_ID, [{"transfer_token": token, "source_type": "markdown"}]
            )
        )

    assert "error" not in completed, completed
    assert completed.get("error_count") == 0, completed
    assert completed["documents_created"]["new"] == 1
