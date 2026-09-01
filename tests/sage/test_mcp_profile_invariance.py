"""MCP profile-invariance tests (MPI-001 through MPI-012).

Prove the MCP document import/export byte channel behaves identically
whether the vault's source store is bound to the local filesystem or to a
cloud document store: same call shapes, same results, caller-local paths
throughout. Specifications with per-test anti-coincidental-pass rationale
live in ``mcp_profile_invariance_tests.md``; every test here runs once per
binding via the ``vault_source_backend`` fixture.

Follows the direct-call MCP test pattern (tool functions invoked with a
pre-initialized vault registry, bypassing transport).
"""

import asyncio
import hashlib
import inspect
import json
from pathlib import Path

import pytest

import sage.mcp_server as _mcp
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.config import VaultConfig
from sage.mcp_server import (
    bulk_ingest_document,
    get_document,
    ingest_document,
    list_directory,
    read_projection,
)
from tests.sage.conftest import initialize_services_for_test

_VAULT_ID = "test_vault"


def _parse(result: str | dict) -> dict:
    if isinstance(result, dict):
        return result
    return json.loads(result)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.fixture
async def mpi_vault(minimal_vault_config_dict, vault_source_backend):
    """One registered vault running over the parameterized source binding."""
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
            # Drain the fire-and-forget pipeline tasks ingest dispatches
            # before services tear down (same discipline as the other
            # ingest-heavy MCP fixtures).
            await asyncio.sleep(0.3)
            _mcp._vaults.pop(_VAULT_ID, None)


async def _ingest_local_file(tmp_path: Path, name: str, body: str) -> tuple[Path, dict]:
    """Write a caller-local file (outside storage_root) and ingest it."""
    inbox = tmp_path / "caller_inbox"
    inbox.mkdir(exist_ok=True)
    src = inbox / name
    src.write_text(body)
    result = _parse(await ingest_document(_VAULT_ID, str(src), "markdown"))
    return src, result


try:
    import docx as _docx

    _HAS_DOCX = True
except ImportError:  # pragma: no cover -- exercised only without python-docx
    _HAS_DOCX = False

requires_docx = pytest.mark.skipif(not _HAS_DOCX, reason="python-docx not available")


def _write_office_file(tmp_path: Path, name: str, heading: str) -> Path:
    """Write a caller-local ``.docx`` (outside storage_root) and return its path.

    An Office package, not markdown, because the retaining store rewrites Office
    packages at rest: this is the format on which the delivered bytes and the
    stored bytes actually diverge.
    """
    inbox = tmp_path / "caller_inbox"
    inbox.mkdir(exist_ok=True)
    src = inbox / name
    document = _docx.Document()
    document.add_heading(heading, level=1)
    document.add_paragraph("Body of the office probe.")
    document.save(str(src))
    return src


async def test_mpi_001_ingest_caller_local_absolute_path(mpi_vault, tmp_path):
    """MPI-001: an absolute caller-local source ingests under either binding,
    retained byte-identical on the active backend."""
    _services, config, handle = mpi_vault
    src, result = await _ingest_local_file(tmp_path, "mpi_note.md", "# MPI\n\nBody one.")

    assert "error" not in result, result
    assert "id" in result
    assert result["source_path"] == "imports/mpi_note.md"

    retained = handle.retained_bytes(config.vault.storage_root, "imports/mpi_note.md")
    assert retained == src.read_bytes()
    if handle.fake_client is not None:
        assert handle.fake_client.source_uploads == 1


async def test_mpi_002_bulk_ingest_caller_local_files(mpi_vault, tmp_path):
    """MPI-002: bulk ingest of caller-local files succeeds under either
    binding with an identical summary shape."""
    _services, config, handle = mpi_vault
    inbox = tmp_path / "caller_inbox"
    inbox.mkdir()
    one = inbox / "alpha_note.md"
    one.write_text("# Alpha\n\nFirst body.")
    two = inbox / "beta_note.md"
    two.write_text("# Beta\n\nSecond body.")

    result = _parse(
        await bulk_ingest_document(
            _VAULT_ID,
            [
                {"file_path": str(one), "source_type": "markdown"},
                {"file_path": str(two), "source_type": "markdown"},
            ],
        )
    )

    assert result.get("error_count") == 0, result
    assert result["documents_created"]["new"] == 2
    for key in ("documents_created", "edges_created", "edges_staged", "errors"):
        assert key in result
    for src in (one, two):
        retained = handle.retained_bytes(config.vault.storage_root, f"imports/{src.name}")
        assert retained == src.read_bytes()


async def test_mpi_003_list_directory_discovery_and_vault_check(mpi_vault, tmp_path):
    """MPI-003: discovery walks the caller-local directory; the
    already-in-vault check resolves through the graph store, so a
    previously ingested file reports ``unchanged`` under either binding."""
    _services, _config, _handle = mpi_vault
    scan_dir = tmp_path / "caller_scan"
    scan_dir.mkdir()
    md = scan_dir / "gamma_note.md"
    md.write_text("# Gamma\n\nScan body.")
    (scan_dir / "opaque.xyz").write_text("no adapter")

    first = _parse(await list_directory(_VAULT_ID, str(scan_dir)))
    assert set(first) == {"files", "warnings", "truncated"}
    by_name = {Path(f["file_path"]).name: f for f in first["files"]}
    assert by_name["gamma_note.md"]["sage_status"] == "new"
    assert by_name["opaque.xyz"]["sage_status"] == "no_adapter"

    ingest = _parse(await ingest_document(_VAULT_ID, str(md), "markdown"))
    assert "error" not in ingest, ingest

    second = _parse(await list_directory(_VAULT_ID, str(scan_dir)))
    by_name = {Path(f["file_path"]).name: f for f in second["files"]}
    assert by_name["gamma_note.md"]["sage_status"] == "unchanged"


async def test_mpi_004_get_document_write_to_path_round_trip(mpi_vault, tmp_path):
    """MPI-004: write_to_path delivers the retained source bytes to a
    caller-local target under either binding."""
    _services, _config, _handle = mpi_vault
    src, ingest = await _ingest_local_file(tmp_path, "delta_note.md", "# Delta\n\nRound trip.")
    target = tmp_path / "caller_out" / "delta_export.md"
    target.parent.mkdir()

    result = _parse(await get_document(_VAULT_ID, ingest["id"], write_to_path=str(target)))

    assert "error" not in result, result
    original = src.read_bytes()
    assert target.read_bytes() == original
    assert result["written_to"] == str(target)
    assert result["content_size"] == len(original)
    assert result["content_hash"] == _sha256(original)
    assert result.get("content") is None
    assert result["read_meta"]["body_present"] is False


async def test_mpi_005_read_projection_spill_skips_vault_source_store(mpi_vault, tmp_path):
    """MPI-005: projection spill writes caller-locally from the content
    store; the vault-source store is never consulted."""
    _services, _config, handle = mpi_vault
    _src, ingest = await _ingest_local_file(tmp_path, "epsilon_note.md", "# Epsilon\n\nSpill body.")
    await asyncio.sleep(0.5)
    target = tmp_path / "caller_out" / "epsilon_projection.md"
    target.parent.mkdir()

    if handle.fake_client is not None:
        reads_before = handle.fake_client.source_reads
        streams_before = len(handle.fake_client.source_streams)
        stats_before = handle.fake_client.source_stats

    result = _parse(await read_projection(_VAULT_ID, ingest["id"], write_to_path=str(target)))

    assert "error" not in result, result
    assert result["written_to"] == str(target)
    assert result.get("projection_text") is None
    assert result["content_size"] > 0
    spilled = target.read_text()
    assert "Spill body." in spilled
    if handle.fake_client is not None:
        assert handle.fake_client.source_reads == reads_before
        assert len(handle.fake_client.source_streams) == streams_before
        assert handle.fake_client.source_stats == stats_before


async def test_mpi_006_export_channel_streams_above_inline_ceiling(
    mpi_vault, tmp_path, monkeypatch
):
    """MPI-006: with the inline ceiling pinned tiny, inline delivery refuses
    while write_to_path round-trips the full payload through the streaming
    read under either binding."""
    _services, _config, handle = mpi_vault
    body = "# Big\n\n" + ("payload line\n" * 5000)  # ~64 KiB
    src, ingest = await _ingest_local_file(tmp_path, "zeta_big.md", body)
    monkeypatch.setenv("SAGE_MAX_INLINE_CONTENT_BYTES", "1024")

    inline = _parse(await get_document(_VAULT_ID, ingest["id"], include_content=True))
    assert inline["error"] == "content_too_large"

    if handle.fake_client is not None:
        reads_before = handle.fake_client.source_reads

    target = tmp_path / "caller_out" / "zeta_export.md"
    target.parent.mkdir()
    spilled = _parse(await get_document(_VAULT_ID, ingest["id"], write_to_path=str(target)))

    assert "error" not in spilled, spilled
    original = src.read_bytes()
    assert len(original) > 1024
    assert target.read_bytes() == original
    assert spilled["content_hash"] == _sha256(original)
    if handle.fake_client is not None:
        assert handle.fake_client.source_streams, "delivery must use the streaming read"
        assert handle.fake_client.source_reads == reads_before, (
            "write_to_path delivery must not fall back to the buffered whole-body read"
        )


@requires_docx
async def test_mpi_009_provenance_hash_is_the_delivered_hash(mpi_vault, tmp_path):
    """MPI-009: the recorded provenance hash is the SHA-256 of the bytes the
    caller delivered, under either binding, even for a format the retaining
    store rewrites at rest.

    Anti-coincidental-pass: on the document-store leg the store must have
    actually stamped the upload (``stamped_uploads``) *and* the recorded
    as-stored hash must differ from the provenance hash -- otherwise the
    equality below is satisfied by a store that happened to retain the bytes
    verbatim and proves nothing. On the filesystem leg the two hashes must be
    equal, which is the regression guard that this binding's behavior did not
    move.
    """
    _services, _config, handle = mpi_vault
    src = _write_office_file(tmp_path, "probe_alpha.docx", "Alpha")
    delivered = _sha256(src.read_bytes())

    result = _parse(await ingest_document(_VAULT_ID, str(src), "docx"))

    assert "error" not in result, result
    doc = await _services.graph_store.get_document(result["id"])
    assert doc.source_content_hash == delivered, (
        "source_content_hash must identify the bytes the caller delivered"
    )
    if handle.fake_client is not None:
        assert handle.fake_client.stamped_uploads >= 1, (
            "the document store must have rewritten the upload; without a real "
            "divergence this test asserts nothing"
        )
        assert doc.stored_content_hash != delivered, (
            "the as-stored hash must record the rewritten copy, not the delivered bytes"
        )
        assert doc.stored_content_hash == handle.fake_client.hash_source_bytes(
            _VAULT_ID, doc.source_path
        )
    else:
        assert doc.stored_content_hash == delivered, (
            "the filesystem binding stores what it was given; the two hashes must agree"
        )


@requires_docx
async def test_mpi_010_identical_reingest_dedups_without_a_second_stored_copy(mpi_vault, tmp_path):
    """MPI-010: re-ingesting byte-identical content is rejected as a duplicate
    and leaves no additional stored copy behind.

    Anti-coincidental-pass: the duplicate error alone does not prove the second
    defect is fixed -- today's retain path uploads a disambiguated second copy
    *before* duplicate detection ever runs. The upload counter is the assertion
    with teeth: it must be unchanged across the second call. It needs its
    positive control to mean anything, though -- "unchanged" is also true of an
    implementation that never retained anything at all -- so the first ingest is
    asserted to have actually stored a copy before the counter is read.
    """
    _services, _config, handle = mpi_vault
    src = _write_office_file(tmp_path, "probe_alpha.docx", "Alpha")

    first = _parse(await ingest_document(_VAULT_ID, str(src), "docx"))
    assert "error" not in first, first

    uploads_before = handle.fake_client.source_uploads if handle.fake_client else None
    stored_before = dict(handle.fake_client.sources) if handle.fake_client else None
    if handle.fake_client is not None:
        assert uploads_before >= 1, (
            "positive control: the first ingest must have stored a copy, or "
            "'the counter did not move' is satisfied by storing nothing at all"
        )
        assert first["source_path"] in stored_before

    second = _parse(await ingest_document(_VAULT_ID, str(src), "docx"))

    assert second.get("error") == "duplicate_content", second
    assert second["detail"]["existing_document_id"] == first["id"]
    assert len(await _services.graph_store.list_all_documents()) == 1

    if handle.fake_client is not None:
        assert handle.fake_client.source_uploads == uploads_before, (
            "a byte-identical re-ingest must not write another copy to the store"
        )
        assert set(handle.fake_client.sources) == set(stored_before), (
            "no additional stored path may appear on a duplicate re-ingest"
        )


@requires_docx
async def test_mpi_011_identical_bytes_under_a_new_name_still_dedup(mpi_vault, tmp_path):
    """MPI-011: identical bytes delivered under a different filename are still
    rejected as a duplicate -- duplicate detection is hash-keyed, not
    name-keyed, under either binding.

    Anti-coincidental-pass: assert the reported hash is the *delivered* one. An
    implementation that deduped on the as-stored hash could not match here at
    all (the store mints a fresh copy per upload), so a passing assertion on the
    hash value pins which hash the rejection was keyed to.
    """
    _services, _config, _handle = mpi_vault
    src = _write_office_file(tmp_path, "probe_alpha.docx", "Alpha")
    delivered = _sha256(src.read_bytes())

    first = _parse(await ingest_document(_VAULT_ID, str(src), "docx"))
    assert "error" not in first, first

    renamed = src.parent / "probe_beta.docx"
    renamed.write_bytes(src.read_bytes())
    second = _parse(await ingest_document(_VAULT_ID, str(renamed), "docx"))

    assert second.get("error") == "duplicate_content", second
    assert second["detail"]["source_content_hash"] == delivered
    assert len(await _services.graph_store.list_all_documents()) == 1


@requires_docx
async def test_mpi_012_reuse_trusts_the_record_when_the_stored_copy_was_altered(
    mpi_vault, tmp_path
):
    """MPI-012: when the retained copy has been altered by something other than
    SAGE, re-delivering the original bytes reuses that copy's path in place
    rather than writing a second one alongside it.

    Pins a deliberate trade: the reuse decision reads the recorded provenance
    instead of re-reading the retained bytes, because confirming would cost a
    full read of the stored copy on every ingest -- a whole-file network
    round-trip for a remote store -- to defend against a mutation the binding
    contract already excludes and the source-file integrity audit already
    detects.

    Anti-coincidental-pass: the stored copy is genuinely altered first
    (asserted), so this is the branch under test and not the ordinary
    identical-copy path. The assertion is on ``source_path`` staying put and no
    new stored key appearing -- an implementation that re-read the bytes to
    confirm would disambiguate to a second path and fail here, which is exactly
    the behavior this test exists to make a visible choice rather than an
    accident.
    """
    _services, config, handle = mpi_vault
    src = _write_office_file(tmp_path, "probe_alpha.docx", "Alpha")
    first = _parse(await ingest_document(_VAULT_ID, str(src), "docx"))
    assert "error" not in first, first
    retained_path = first["source_path"]

    # Alter the retained copy out of band -- the condition the binding contract
    # excludes and the integrity audit is built to surface.
    if handle.fake_client is not None:
        before = dict(handle.fake_client.sources)
        handle.fake_client.sources[retained_path] += b"\n<!-- altered out of band -->"
        assert handle.fake_client.sources[retained_path] != before[retained_path]
        uploads_before = handle.fake_client.source_uploads
    else:
        on_disk = Path(config.vault.storage_root) / retained_path
        original = on_disk.read_bytes()
        on_disk.write_bytes(original + b"\n<!-- altered out of band -->")
        assert on_disk.read_bytes() != original

    second = _parse(await ingest_document(_VAULT_ID, str(src), "docx", force=True))

    assert "error" not in second, second
    assert second["source_path"] == retained_path, (
        "re-delivering the original bytes must reuse the recorded path in place"
    )
    if handle.fake_client is not None:
        assert handle.fake_client.source_uploads == uploads_before
        assert set(handle.fake_client.sources) == set(before)


def test_mpi_007_path_parameter_docstrings_state_server_local_contract():
    """MPI-007: every path-bearing tool documents that its path parameter
    resolves on the machine running the SAGE server process."""
    contract = "machine running the SAGE server process"
    for tool in (
        ingest_document,
        bulk_ingest_document,
        get_document,
        read_projection,
        list_directory,
    ):
        # Collapse whitespace so the check is about content, not where the
        # docstring happens to wrap.
        doc = " ".join((inspect.getdoc(tool) or "").split())
        assert contract in doc, f"{tool.__name__} docstring lacks the server-local path contract"
