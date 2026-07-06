"""Caller-filesystem confinement + inline-ingest byte channel tests.

Under the cloud profile the SAGE server is a remote container that cannot see
the calling client's filesystem. These tests prove the two halves of the fix:

- Confinement (B*): every path-bearing tool refuses a caller-supplied local
  path with the structured ``caller_filesystem_unavailable`` error instead of
  reading, writing, or enumerating the container's own tree.
- Inline byte channel (A*): ``ingest_document`` / ``bulk_ingest_document``
  accept the source bytes inline (``content_base64``), staged below the tool
  surface, so a remote-mount caller ingests with the same call shape.

The co-located (local) profile stays unchanged: path forms still work (L*), and
the existing ``test_mcp_profile_invariance`` suite is the broader local-profile
regression. The new axis is the deployment profile, pinned per-test by the
``_profile`` context manager. Each test runs once per vault-source backend via
the parameterized ``vault_source_backend`` fixture (from conftest); store
resolution stays on that backend because the vault-source builder's env override
outranks the profile, so ``_profile("cloud")`` changes only the caller-visible
confinement behavior, not which store answers.
"""

import asyncio
import base64
import contextlib
import json

import pytest

import sage.mcp_init as _mcp_init
import sage.mcp_server as _mcp
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.config import SageCoreConfig, VaultConfig
from sage.mcp_server import (
    bulk_ingest_document,
    get_document,
    ingest_document,
    list_directory,
    read_projection,
)
from sage.profiles import caller_local_filesystem_available
from tests.sage.conftest import initialize_services_for_test

_VAULT_ID = "test_vault"


def _parse(result: str | dict) -> dict:
    if isinstance(result, dict):
        return result
    return json.loads(result)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@contextlib.contextmanager
def _profile(name: str):
    """Pin the active deployment profile for the duration of the block.

    Sets the module-level stack config to the named profile and restores the
    prior value on exit, so a test can drive the cloud-profile code paths
    without standing up a cloud stack.
    """
    saved = _mcp_init._stack_config
    _mcp_init.set_stack_config(SageCoreConfig(profile=name))
    try:
        yield
    finally:
        _mcp_init.set_stack_config(saved)


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


# ---------------------------------------------------------------------------
# CFV-pred: the profile predicate
# ---------------------------------------------------------------------------


def test_cfv_pred_predicate_maps_profile_to_visibility():
    """CFV-pred: the caller-filesystem predicate is True under the local
    profile and False under the cloud profile.

    Anti-coincidental: a predicate hardcoded to a constant fails one of the two
    legs. The guard tests below assert refusal *under cloud*; this test is what
    ties that refusal to the real profile.
    """
    assert caller_local_filesystem_available("local") is True
    assert caller_local_filesystem_available("cloud") is False


# ---------------------------------------------------------------------------
# A*: the inline-bytes byte channel (cloud profile)
# ---------------------------------------------------------------------------


async def test_a1_inline_ingest_under_cloud(confined_vault):
    """A1: under the cloud profile, ingest a caller-local file by passing its
    bytes inline; it lands byte-identical on the active backend."""
    _services, config, handle = confined_vault
    body = b"# Cloud inline\n\nBody carried in the request."

    with _profile("cloud"):
        result = _parse(
            await ingest_document(
                _VAULT_ID,
                source_type="markdown",
                content_base64=_b64(body),
                filename="cloud_note.md",
            )
        )

    assert "error" not in result, result
    assert result["source_path"] == "imports/cloud_note.md"
    retained = handle.retained_bytes(config.vault.storage_root, "imports/cloud_note.md")
    assert retained == body  # ingested bytes are the input bytes, not a coincidence
    if handle.fake_client is not None:
        assert handle.fake_client.source_uploads == 1


async def test_a2_inline_bulk_ingest_under_cloud(confined_vault):
    """A2: under the cloud profile, bulk-ingest caller-local files by passing
    each file's bytes inline."""
    _services, config, handle = confined_vault
    alpha = b"# Alpha inline\n\nFirst."
    beta = b"# Beta inline\n\nSecond."

    with _profile("cloud"):
        result = _parse(
            await bulk_ingest_document(
                _VAULT_ID,
                [
                    {
                        "content_base64": _b64(alpha),
                        "filename": "alpha_in.md",
                        "source_type": "markdown",
                    },
                    {
                        "content_base64": _b64(beta),
                        "filename": "beta_in.md",
                        "source_type": "markdown",
                    },
                ],
            )
        )

    assert result.get("error_count") == 0, result
    assert result["documents_created"]["new"] == 2
    assert handle.retained_bytes(config.vault.storage_root, "imports/alpha_in.md") == alpha
    assert handle.retained_bytes(config.vault.storage_root, "imports/beta_in.md") == beta


async def test_a2b_inline_bulk_same_filename_entries_stage_independently(confined_vault):
    """A2b: two inline entries carrying the same filename ingest independently
    -- per-entry staging must keep their bytes distinct.

    Anti-coincidental: with a shared flat staging directory the second entry's
    write clobbers the first's bytes before the batch runs, so both descriptors
    hash identically and the batch reports a duplicate-content error instead of
    two clean documents.
    """
    _services, _config, _handle = confined_vault
    alpha = b"# Same name\n\nFirst distinct body."
    beta = b"# Same name\n\nSecond distinct body."

    with _profile("cloud"):
        result = _parse(
            await bulk_ingest_document(
                _VAULT_ID,
                [
                    {
                        "content_base64": _b64(alpha),
                        "filename": "same_name.md",
                        "source_type": "markdown",
                    },
                    {
                        "content_base64": _b64(beta),
                        "filename": "same_name.md",
                        "source_type": "markdown",
                    },
                ],
            )
        )

    assert result.get("error_count") == 0, result
    assert result["documents_created"]["new"] == 2


async def test_a1b_inline_ingest_degenerate_filename_falls_back(confined_vault):
    """A1b: a degenerate inline ``filename`` (a directory reference like
    ``".."``) falls back to the synthetic staging name and ingests cleanly,
    rather than resolving to the staging directory itself and surfacing an
    unstructured OS error."""
    _services, _config, _handle = confined_vault

    with _profile("cloud"):
        result = _parse(
            await ingest_document(
                _VAULT_ID,
                source_type="markdown",
                content_base64=_b64(b"# Degenerate\n\nName was a dot-dot."),
                filename="..",
            )
        )

    assert "error" not in result, result
    assert result["source_path"] == "imports/inline_source"


async def test_a3_inline_ingest_bound_is_a_real_threshold(confined_vault, monkeypatch):
    """A3: with the inline-ingest ceiling pinned tiny, an under-bound payload
    ingests and an over-bound payload is refused with a structured error --
    never truncated.

    Anti-coincidental: asserting both sides proves the bound is a threshold, not
    an always-error.
    """
    _services, _config, _handle = confined_vault
    monkeypatch.setenv("SAGE_MAX_INLINE_INGEST_BYTES", "32")

    under = b"# ok\n"  # 5 bytes decoded, <= 32
    over = b"# " + b"x" * 200  # > 32 bytes decoded

    with _profile("cloud"):
        ok = _parse(
            await ingest_document(
                _VAULT_ID, source_type="markdown", content_base64=_b64(under), filename="under.md"
            )
        )
        too_big = _parse(
            await ingest_document(
                _VAULT_ID, source_type="markdown", content_base64=_b64(over), filename="over.md"
            )
        )

    assert "error" not in ok, ok
    assert too_big["error"] == "inline_content_too_large", too_big


async def test_a4_inline_ingest_exactly_one_source(confined_vault, tmp_path):
    """A4: supplying both a path source and inline content, or neither, is a
    structured validation error."""
    _services, _config, _handle = confined_vault
    src = tmp_path / "either.md"
    src.write_text("# Either\n")

    with _profile("cloud"):
        both = _parse(
            await ingest_document(
                _VAULT_ID,
                source=str(src),
                source_type="markdown",
                content_base64=_b64(b"# both\n"),
            )
        )
        neither = _parse(await ingest_document(_VAULT_ID, source_type="markdown"))

    assert both["error"] == "ambiguous_ingest_source", both
    assert neither["error"] == "missing_ingest_source", neither


# ---------------------------------------------------------------------------
# B*: confinement of the path-bearing tools (cloud profile)
# ---------------------------------------------------------------------------


async def test_b1_ingest_absolute_path_refused_under_cloud(confined_vault, tmp_path):
    """B1: an absolute caller ``source`` path is refused under the cloud profile
    -- naming the inline mechanism -- rather than read from the container.

    The file exists on this (co-located test) machine, so without the guard the
    ingest would *succeed*; the refusal is what proves the guard fires.
    """
    _services, _config, _handle = confined_vault
    src = tmp_path / "caller_inbox" / "confined.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# Confined\n\nOn the caller machine.")

    with _profile("cloud"):
        result = _parse(await ingest_document(_VAULT_ID, str(src), "markdown"))

    assert result["error"] == "caller_filesystem_unavailable", result
    # Not the original symptom (a container-path miss), and not a silent success
    # reading the container tree.
    assert result["error"] != "source_file_not_found"


async def test_b2_list_directory_refused_under_cloud(confined_vault, tmp_path):
    """B2: directory discovery is refused under the cloud profile rather than
    walking and content-hashing the container tree.

    A real file is seeded in the scanned directory: without the guard,
    ``list_directory`` would enumerate it. The refusal (no ``files`` key) proves
    the walk never happened -- the direct analogue of the ``list_directory('/')``
    disclosure probe.
    """
    _services, _config, _handle = confined_vault
    scan_dir = tmp_path / "caller_scan"
    scan_dir.mkdir()
    (scan_dir / "seed_note.md").write_text("# Seed\n\nWould be enumerated without the guard.")

    with _profile("cloud"):
        result = _parse(await list_directory(_VAULT_ID, str(scan_dir)))

    assert result["error"] == "caller_filesystem_unavailable", result
    assert "files" not in result  # the walk never ran


async def test_b3_get_document_write_to_path_refused_under_cloud(confined_vault, tmp_path):
    """B3: ``get_document(write_to_path=...)`` is refused under the cloud
    profile, and no file is written to the container."""
    _services, _config, _handle = confined_vault
    _src, ingest = await _ingest_local_file(tmp_path, "b3_note.md", "# B3\n\nExport me.")
    target = tmp_path / "caller_out" / "b3_export.md"
    target.parent.mkdir()

    with _profile("cloud"):
        result = _parse(await get_document(_VAULT_ID, ingest["id"], write_to_path=str(target)))

    assert result["error"] == "caller_filesystem_unavailable", result
    assert not target.exists()  # nothing written to the container


async def test_b4_read_projection_write_to_path_refused_under_cloud(confined_vault, tmp_path):
    """B4: ``read_projection(write_to_path=...)`` is refused under the cloud
    profile, and no file is written to the container."""
    _services, _config, _handle = confined_vault
    _src, ingest = await _ingest_local_file(tmp_path, "b4_note.md", "# B4\n\nProjection body.")
    target = tmp_path / "caller_out" / "b4_projection.md"
    target.parent.mkdir()

    with _profile("cloud"):
        result = _parse(await read_projection(_VAULT_ID, ingest["id"], write_to_path=str(target)))

    assert result["error"] == "caller_filesystem_unavailable", result
    assert not target.exists()


async def test_b5_inline_export_still_works_under_cloud(confined_vault, tmp_path):
    """B5: the inline export modes -- ``include_content`` and inline
    ``read_projection`` -- keep returning bytes under the cloud profile. They
    are the sanctioned alternative to ``write_to_path`` and must not be
    over-refused by the confinement guard."""
    _services, _config, _handle = confined_vault
    body = "# B5\n\nInline export body."
    _src, ingest = await _ingest_local_file(tmp_path, "b5_note.md", body)
    await asyncio.sleep(0.5)  # let the projection land

    with _profile("cloud"):
        doc = _parse(await get_document(_VAULT_ID, ingest["id"], include_content=True))
        projection = _parse(await read_projection(_VAULT_ID, ingest["id"]))

    assert "error" not in doc, doc
    assert doc.get("content") is not None
    assert "error" not in projection, projection
    assert projection.get("projection_text") is not None


# ---------------------------------------------------------------------------
# L: the local profile is unchanged (paired counterpart to B1/B2)
# ---------------------------------------------------------------------------


async def test_l_local_profile_allows_path_forms(confined_vault, tmp_path):
    """L: under the local profile the path forms still work -- the confinement
    guard must not fire when the server shares the caller's machine (including
    the local-over-HTTP topology).

    Anti-coincidental for the predicate: a predicate that mislabels local as
    cloud would refuse here, so this test fails if confinement over-fires.
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
    assert set(listing) == {"files", "warnings"}, listing
