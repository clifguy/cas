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

from sage.api.errors import ContentFileMissingError
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
