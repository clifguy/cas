"""The three hot paths route their source-byte work through the vault-source
store, not direct filesystem access (CAS-ADR-043).

Each test injects a fake store by monkeypatching
``sage.mcp_init.resolve_stack_vault_source_store`` and has that fake return a
sentinel the real filesystem binding could never produce. A test passes only if
the service actually delegates to the store; were the service still constructing
``storage_root / source_path`` itself, the real file's bytes (or a real path)
would surface and the sentinel assertion would fail. Each fake subclasses the
real filesystem binding so any method it does not override behaves normally.

Test IDs follow VSBB-NNN (Vault-Source Binding Bytes), routing slice.
"""

import base64
import shutil
from pathlib import Path

import pytest

from sage.api.errors import ContentFileMissingError, SourceFileNotFoundError
from sage.models.enums import SourceType
from sage.models.schemas import IngestRequest
from sage.services.documents import DocumentsService
from sage.services.maintenance import MaintenanceService
from sage.vault_source_binding import FilesystemVaultSourceStore

_UNUSED_ROOT = Path("/unused/vault_root")


def _patch_store(monkeypatch, store) -> None:
    """Make the lazily-resolved stack vault-source store be ``store``."""
    monkeypatch.setattr(
        "sage.mcp_init.resolve_stack_vault_source_store",
        lambda *args, **kwargs: store,
    )


async def _ingest_internal(ingestion_service, tmp_vault_dir, rel: str, body: str):
    """Seed an internal source file and ingest it; return the document."""
    full = tmp_vault_dir / "sources" / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body)
    result = await ingestion_service.ingest(
        IngestRequest(source=rel, source_type=SourceType.MARKDOWN)
    )
    return result.document


# --------------------------------------------------------------------------- #
# VSBB-013: ingest retention routes through retain_source
# --------------------------------------------------------------------------- #


class _SentinelRetainStore(FilesystemVaultSourceStore):
    SENTINEL = "imports/SENTINEL.md"

    def retain_source(self, vault_id, storage_root, source_path):
        # Materialize the retained file so the post-retain projection can read
        # it, but return a sentinel relative path the real binding never would.
        dest = storage_root / "imports" / "SENTINEL.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, dest)
        return self.SENTINEL


async def test_vsbb_013_ingest_uses_port_retain(
    ingestion_service, tmp_vault_dir, tmp_path, monkeypatch
):
    """The ingested document's ``source_path`` is the value ``retain_source``
    returned, proving retention routes through the port. Anti-coincidental: the
    real binding would return ``imports/external.md``, not the sentinel."""
    _patch_store(monkeypatch, _SentinelRetainStore(_UNUSED_ROOT))

    external = tmp_path / "external.md"
    external.write_text("# External\n\nBody.")

    result = await ingestion_service.ingest(
        IngestRequest(source=str(external), source_type=SourceType.MARKDOWN)
    )

    assert result.document.source_path == _SentinelRetainStore.SENTINEL


# --------------------------------------------------------------------------- #
# VSBB-014 / VSBB-015: delivery routes through the port
# --------------------------------------------------------------------------- #


class _SentinelReadStore(FilesystemVaultSourceStore):
    SENTINEL_BYTES = b"SENTINEL-BYTES"

    def source_exists(self, vault_id, storage_root, source_path):
        return True

    def source_size(self, vault_id, storage_root, source_path):
        return len(self.SENTINEL_BYTES)

    def read_source(self, vault_id, storage_root, source_path):
        return self.SENTINEL_BYTES


class _MissingStore(FilesystemVaultSourceStore):
    def source_exists(self, vault_id, storage_root, source_path):
        return False


async def test_vsbb_014_delivery_reads_via_port(
    ingestion_service, graph_store, minimal_config, tmp_vault_dir, monkeypatch
):
    """Inline delivery returns the bytes ``read_source`` produced, not the real
    file's. Anti-coincidental: the real file holds ``# Real…``, so a direct read
    would not base64-encode the sentinel bytes."""
    doc = await _ingest_internal(
        ingestion_service, tmp_vault_dir, "reports/real.md", "# Real\n\nX."
    )

    _patch_store(monkeypatch, _SentinelReadStore(_UNUSED_ROOT))
    documents = DocumentsService(graph_store, minimal_config)

    response = await documents.get_document_with_content(
        doc.id, include_content=True, write_to_path=None
    )

    assert response.content == base64.b64encode(_SentinelReadStore.SENTINEL_BYTES).decode("ascii")


async def test_vsbb_015_delivery_missing_via_port(
    ingestion_service, graph_store, minimal_config, tmp_vault_dir, monkeypatch
):
    """When the store reports the source absent, delivery raises
    ``ContentFileMissingError`` even though the real file is present.
    Anti-coincidental: a direct ``Path.exists()`` on the present file would not
    raise."""
    doc = await _ingest_internal(
        ingestion_service, tmp_vault_dir, "reports/present.md", "# Present\n\nX."
    )
    # The real file is deliberately present.
    assert (tmp_vault_dir / "sources" / doc.source_path).exists()

    _patch_store(monkeypatch, _MissingStore(_UNUSED_ROOT))
    documents = DocumentsService(graph_store, minimal_config)

    with pytest.raises(ContentFileMissingError):
        await documents.get_document_with_content(doc.id, include_content=True, write_to_path=None)


# --------------------------------------------------------------------------- #
# VSBB-024: streaming content delivery routes through the port
# --------------------------------------------------------------------------- #


class _SentinelStreamStore(FilesystemVaultSourceStore):
    SENTINEL_CHUNKS = [b"SENTINEL-", b"CHUNKS"]

    def source_exists(self, vault_id, storage_root, source_path):
        return True

    def source_size(self, vault_id, storage_root, source_path):
        return sum(len(c) for c in self.SENTINEL_CHUNKS)

    def iter_source(self, vault_id, storage_root, source_path):
        yield from self.SENTINEL_CHUNKS

    def read_source(self, vault_id, storage_root, source_path):
        raise AssertionError("buffered read on the streaming path")


async def test_vsbb_024_content_delivery_via_port(
    ingestion_service, graph_store, minimal_config, tmp_vault_dir, monkeypatch
):
    """Streaming content delivery yields exactly the chunks ``iter_source``
    produced, and never touches the buffered ``read_source``. Anti-coincidental:
    the real file holds ``# Real…`` (a direct read would surface it), and the
    sentinel store's ``read_source`` raises, so any whole-bytes fallback fails
    loudly."""
    doc = await _ingest_internal(
        ingestion_service, tmp_vault_dir, "reports/stream.md", "# Real\n\nX."
    )

    _patch_store(monkeypatch, _SentinelStreamStore(_UNUSED_ROOT))
    documents = DocumentsService(graph_store, minimal_config)

    delivery = await documents.get_document_content(doc.id)

    assert list(delivery.chunks) == _SentinelStreamStore.SENTINEL_CHUNKS
    assert delivery.size == sum(len(c) for c in _SentinelStreamStore.SENTINEL_CHUNKS)
    assert delivery.filename == "stream.md"
    assert delivery.media_type == "text/markdown"


# --------------------------------------------------------------------------- #
# VSBB-016 / VSBB-017: integrity audit routes through the port
# --------------------------------------------------------------------------- #


class _SentinelHashStore(FilesystemVaultSourceStore):
    SENTINEL_HASH = "sha256:" + "0" * 64

    def source_exists(self, vault_id, storage_root, source_path):
        return True

    def hash_source(self, vault_id, storage_root, source_path):
        return self.SENTINEL_HASH


def _maintenance(vault_id, tmp_vault_dir, graph_store, config, content_store):
    return MaintenanceService(
        vault_id=vault_id,
        db_path=tmp_vault_dir / "brain" / "graph.db",
        graph_store=graph_store,
        config=config,
        registry_service=None,
        content_store=content_store,
    )


async def test_vsbb_016_audit_hash_via_port(
    ingestion_service, graph_store, minimal_config, tmp_vault_dir, stub_content_store, monkeypatch
):
    """``verify_vault_source_files(check_hashes=True)`` reports a hash mismatch
    carrying the value ``hash_source`` returned. Anti-coincidental: hashing the
    real (matching) file would yield no entry."""
    await _ingest_internal(ingestion_service, tmp_vault_dir, "reports/h.md", "# H\n\nX.")

    _patch_store(monkeypatch, _SentinelHashStore(_UNUSED_ROOT))
    maintenance = _maintenance(
        minimal_config.vault.id, tmp_vault_dir, graph_store, minimal_config, stub_content_store
    )

    report = await maintenance.verify_vault_source_files(check_hashes=True)

    assert report.summary["hash_mismatch"] == 1
    entry = next(e for e in report.entries if e.integrity_status == "hash_mismatch")
    assert entry.observed_content_hash == _SentinelHashStore.SENTINEL_HASH


async def test_vsbb_017_audit_missing_via_port(
    ingestion_service, graph_store, minimal_config, tmp_vault_dir, stub_content_store, monkeypatch
):
    """When the store reports a source absent, the audit classifies it
    ``missing`` even though the real file exists. Anti-coincidental: a direct
    ``Path.exists()`` on the present file would report it healthy."""
    doc = await _ingest_internal(ingestion_service, tmp_vault_dir, "reports/m.md", "# M\n\nX.")
    assert (tmp_vault_dir / "sources" / doc.source_path).exists()

    _patch_store(monkeypatch, _MissingStore(_UNUSED_ROOT))
    maintenance = _maintenance(
        minimal_config.vault.id, tmp_vault_dir, graph_store, minimal_config, stub_content_store
    )

    report = await maintenance.verify_vault_source_files(check_hashes=False)

    assert report.summary["missing"] == 1
    assert any(e.integrity_status == "missing" for e in report.entries)


# --------------------------------------------------------------------------- #
# VSBB-018 / 019 / 020 / 021: projection & repair route through the port
# --------------------------------------------------------------------------- #


class _SentinelProjectStore(FilesystemVaultSourceStore):
    """Reports every source present and yields sentinel markdown the local file
    could never produce, so a projection reflecting the sentinel proves the read
    routed through ``read_source``. ``retain_source`` deliberately materializes no
    local copy, forcing the post-retain projection down the port path."""

    SENTINEL_TITLE = "PROJECT-SENTINEL"
    SENTINEL_BYTES = b"# PROJECT-SENTINEL\n\nRouted through the port.\n"
    RETAINED = "imports/sentinel.md"

    def retain_source(self, vault_id, storage_root, source_path):
        return self.RETAINED

    def source_exists(self, vault_id, storage_root, source_path):
        return True

    def read_source(self, vault_id, storage_root, source_path):
        return self.SENTINEL_BYTES


class _RaisingReadStore(FilesystemVaultSourceStore):
    """``read_source`` raises: proves the local-copy fast path never calls the
    port when a local source file is present."""

    def source_exists(self, vault_id, storage_root, source_path):
        return True

    def read_source(self, vault_id, storage_root, source_path):
        raise AssertionError("read_source must not be called when a local copy exists")


async def test_vsbb_018_ingest_projection_via_port(
    ingestion_service, tmp_vault_dir, tmp_path, monkeypatch
):
    """The ingest-time projection reads through ``read_source`` when no local copy
    exists: the document's title is the sentinel markdown's heading, not the
    external file's. Anti-coincidental: the store materializes no local file, so a
    direct ``adapter.project(storage_root / vault_relative)`` would hit a missing
    path and fail."""
    _patch_store(monkeypatch, _SentinelProjectStore(_UNUSED_ROOT))
    external = tmp_path / "external.md"
    external.write_text("# External Heading\n\nlocal body")

    result = await ingestion_service.ingest(
        IngestRequest(source=str(external), source_type=SourceType.MARKDOWN)
    )

    assert result.document.title == _SentinelProjectStore.SENTINEL_TITLE


async def test_vsbb_019_recompute_projection_via_port(
    ingestion_service, tmp_vault_dir, monkeypatch
):
    """``recompute_pipeline`` re-projects through the port after the local source
    copy is gone (the post-restart cloud condition). Anti-coincidental: with the
    local file deleted, the pre-port code raised ``SourceFileNotFoundError``;
    routing through ``read_source`` lets the re-projection succeed."""
    doc = await _ingest_internal(ingestion_service, tmp_vault_dir, "reports/r.md", "# Real\n\nX.")
    (tmp_vault_dir / "sources" / doc.source_path).unlink()

    _patch_store(monkeypatch, _SentinelProjectStore(_UNUSED_ROOT))
    result = await ingestion_service.recompute_pipeline(doc.id)

    assert result["status"] == "recompute_pipeline_started"


async def test_vsbb_020_reproject_from_source_via_port(
    ingestion_service, tmp_vault_dir, monkeypatch
):
    """``_reproject_from_source`` re-projects through the port after the local
    copy is gone, returning a projection built from the sentinel bytes.
    Anti-coincidental: the returned projection's title is the sentinel heading,
    which only ``read_source`` could supply once the local file is deleted."""
    doc = await _ingest_internal(ingestion_service, tmp_vault_dir, "reports/p.md", "# Real\n\nX.")
    (tmp_vault_dir / "sources" / doc.source_path).unlink()

    _patch_store(monkeypatch, _SentinelProjectStore(_UNUSED_ROOT))
    projection = await ingestion_service._reproject_from_source(doc.id)

    assert projection.title == _SentinelProjectStore.SENTINEL_TITLE


async def test_vsbb_021_local_copy_short_circuits_the_port(
    ingestion_service, tmp_vault_dir, monkeypatch
):
    """When a local source copy is present, projection reads it directly and never
    calls the port. Anti-coincidental: the store's ``read_source`` raises, so a
    re-projection that always staged through the port would surface that error;
    success proves the ``exists()`` short-circuit holds (and the filesystem
    binding's direct read is not regressed)."""
    doc = await _ingest_internal(
        ingestion_service, tmp_vault_dir, "reports/keep.md", "# Keep\n\nX."
    )
    assert (tmp_vault_dir / "sources" / doc.source_path).exists()

    _patch_store(monkeypatch, _RaisingReadStore(_UNUSED_ROOT))
    projection = await ingestion_service._reproject_from_source(doc.id)

    assert projection.title == "Keep"


# --------------------------------------------------------------------------- #
# VSBB-022 / VSBB-023: ingest resolves a relative source through the port
# --------------------------------------------------------------------------- #


class _BackendOnlyStore(FilesystemVaultSourceStore):
    """A relative source present in the backing store with no local mirror --
    the post-restart document-store-binding condition (CAS-ADR-043). Reports the
    source present, yields sentinel bytes, and fails loudly if the service tries
    to re-retain (re-upload) an already-retained relative source."""

    SENTINEL_TITLE = "BACKEND-ONLY-SENTINEL"
    SENTINEL_BYTES = b"# BACKEND-ONLY-SENTINEL\n\nFetched from the backend.\n"

    def source_exists(self, vault_id, storage_root, source_path):
        return True

    def read_source(self, vault_id, storage_root, source_path):
        return self.SENTINEL_BYTES

    def retain_source(self, vault_id, storage_root, source_path):
        raise AssertionError("retain_source must not run for an already-retained relative source")


class _AbsentBackendStore(FilesystemVaultSourceStore):
    """Reports every relative source absent from the store."""

    def source_exists(self, vault_id, storage_root, source_path):
        return False


async def test_vsbb_022_ingest_relative_backend_source_resolves(
    ingestion_service, tmp_vault_dir, monkeypatch
):
    """A relative ``source`` present only in the backing store resolves through
    the port instead of 404'ing on a raw local ``Path.exists()`` gate
    (CAS-ADR-043). The document's title is the sentinel backend markdown's
    heading, proving the bytes were read via ``read_source``; its
    ``source_path`` is the relative input recorded verbatim. Anti-coincidental:
    with no local mirror the pre-fix code raised ``SourceFileNotFoundError`` at
    the local-disk gate, and the fake's ``retain_source`` raises if the service
    mistreats the relative source as an external file to re-upload."""
    _patch_store(monkeypatch, _BackendOnlyStore(_UNUSED_ROOT))

    result = await ingestion_service.ingest(
        IngestRequest(source="imports/backend_only.md", source_type=SourceType.MARKDOWN)
    )

    assert result.document.title == _BackendOnlyStore.SENTINEL_TITLE
    assert result.document.source_path == "imports/backend_only.md"


async def test_vsbb_023_ingest_relative_absent_source_still_raises(
    ingestion_service, tmp_vault_dir, monkeypatch
):
    """Relocating the existence gate behind the port does not remove it: a
    relative source absent from both the local tree and the store still raises
    ``SourceFileNotFoundError``. Anti-coincidental: were the gate dropped, the
    call would fall through to projection and surface a different failure (or
    none)."""
    _patch_store(monkeypatch, _AbsentBackendStore(_UNUSED_ROOT))

    with pytest.raises(SourceFileNotFoundError):
        await ingestion_service.ingest(
            IngestRequest(source="imports/nope.md", source_type=SourceType.MARKDOWN)
        )
