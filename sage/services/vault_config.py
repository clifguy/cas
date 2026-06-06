"""Per-vault administration: stats, hash-check, config get/update.

Owns the work behind the four vault-scoped administration routes:
- GET /sage_vaults/{vault_id}/stats -- dashboard aggregation.
- POST /sage_vaults/{vault_id}/hash-check -- bulk hash existence check.
- GET /sage_vaults/{vault_id}/config -- read full config as JSON.
- PUT /sage_vaults/{vault_id}/config -- update sections (with destructive
                                              -change detection and reload).

Update orchestration: validation, destructive-change detection, and YAML
write live here; the registry-mutation step (close old services, init new,
swap in the registry dict) is delegated to VaultRegistryService.reload so
registry state stays encapsulated in the registry service.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from sage.adapters.interfaces import ContentStore
from sage.api.errors import (
    DestructiveConfigChangeError,
    VaultConfigValidationError,
)
from sage.config import VaultConfig
from sage.models.schemas import (
    HashCheckMatch,
    HashCheckRequest,
    HealthIndicators,
    UpdateVaultConfigRequest,
    UpdateVaultConfigResponse,
    VaultConfigPreview,
    VaultStatsResponse,
)
from sage.services.maintenance_log import read_last_optimize_summary
from sage.storage.graph_store import GraphStore
from sage.vault_management import (
    _ALL_SECTIONS,
    _atomic_write_bytes,
    _check_destructive_changes,
    _validate_config,
    _write_config_yaml,
    config_path_for_vault,
)

if TYPE_CHECKING:
    from sage.services.vault_registry import VaultRegistryService

logger = logging.getLogger(__name__)


class VaultConfigService:
    def __init__(
        self,
        graph_store: GraphStore,
        content_store: ContentStore,
        config: VaultConfig,
        registry_service: "VaultRegistryService | None",
    ) -> None:
        self._store = graph_store
        self._content_store = content_store
        self._config = config
        self._registry_service = registry_service

    async def get_stats(self) -> VaultStatsResponse:
        """Return Dashboard statistics for the vault."""
        config = self._config

        total_documents = await self._store.get_total_document_count()
        by_lifecycle = await self._store.get_document_counts_by_field("lifecycle_status")
        by_doc_type = await self._store.get_document_counts_by_field("doc_type")
        by_source_type = await self._store.get_document_counts_by_field("source_type")
        total_edges = await self._store.get_total_edge_count()
        by_edge_type = await self._store.get_edge_counts_by_type()
        staging_count = await self._store.count_staging_edges()
        last_ingestion = await self._store.get_last_ingestion_at()

        failed_count = await self._store.count_documents_by_pipeline_status("failed")
        deferred_count = await self._store.count_documents_by_pipeline_status("abstraction_skipped")
        pending_metadata_docs = await self._store.list_pending_metadata_documents()
        pending_metadata_count = len(pending_metadata_docs)

        brain_root = Path(config.vault.brain_root).expanduser()
        sqlite_path = brain_root / "graph.db"
        sqlite_size = sqlite_path.stat().st_size if sqlite_path.exists() else 0

        lancedb_dir = brain_root / "lancedb"
        lancedb_size = 0
        if lancedb_dir.exists():
            for f in lancedb_dir.rglob("*"):
                if f.is_file():
                    lancedb_size += f.stat().st_size

        lancedb_chunk_count = await self._content_store.count_chunks()
        lancedb_version_count = await self._content_store.count_retained_versions()
        lancedb_small_fragment_count = await self._content_store.count_small_fragments()

        vault_dir = config_path_for_vault(config.vault.id).parent
        last_optimize = read_last_optimize_summary(vault_dir)

        return VaultStatsResponse(
            total_documents=total_documents,
            by_lifecycle_status=by_lifecycle,
            by_doc_type=by_doc_type,
            by_source_type=by_source_type,
            total_edges=total_edges,
            by_edge_type=by_edge_type,
            staging_edge_count=staging_count,
            lancedb_size_bytes=lancedb_size,
            lancedb_chunk_count=lancedb_chunk_count,
            lancedb_version_count=lancedb_version_count,
            lancedb_small_fragment_count=lancedb_small_fragment_count,
            sqlite_size_bytes=sqlite_size,
            last_ingestion_at=last_ingestion,
            last_optimize=last_optimize,
            health=HealthIndicators(
                pending_metadata_count=pending_metadata_count,
                pending_edge_count=staging_count,
                deferred_abstract_count=deferred_count if config.abstraction.enabled else None,
                failed_ingestion_count=failed_count,
            ),
        )

    async def hash_check(self, body: HashCheckRequest) -> dict[str, HashCheckMatch]:
        """Bulk hash existence check against the graph store.

        Returns a dict keyed by each input hash, carrying a
        ``HashCheckMatch`` envelope with ``exists`` and (when matched)
        the ``document_id`` of the storing document.

        Empty-list short-circuit:
        ``body.hashes == []`` short-circuits to an empty result dict
        without consulting the graph store (the early return below).
        The empty response is indistinguishable from "every queried
        hash is unknown" without inspecting the request; callers that
        branch on result emptiness should also branch on input
        emptiness.

        Malformed hashes:
        This service does NOT validate the format of input hash
        strings. The ``verify_hash`` MCP wrapper constructs
        ``HashCheckRequest`` via ``model_construct`` to round-trip
        the bare-hex form ``ingest_document`` emits, so malformed entries
        (truncated hex, non-hex characters, wrong length) reach
        ``find_documents_by_hashes`` as-is, miss every row, and
        surface in the result with ``exists=False`` indistinguishable
        from well-formed-but-unknown hashes. Callers that need a
        "valid format" precondition must enforce it before calling.
        """
        if not body.hashes:
            return {}

        matches = await self._store.find_documents_by_hashes(body.hashes)

        result: dict[str, HashCheckMatch] = {}
        for h in body.hashes:
            if h in matches:
                result[h] = HashCheckMatch(exists=True, document_id=matches[h])
            else:
                result[h] = HashCheckMatch(exists=False)
        return result

    def get_config(self) -> dict:
        """Return the full vault configuration as a plain dict."""
        return self._config.model_dump()

    async def update_config(
        self,
        vault_id: str,
        body: UpdateVaultConfigRequest,
        force: bool,
    ) -> UpdateVaultConfigResponse:
        """Update vault configuration at the section level.

        Each provided top-level section replaces the current section
        wholesale; omitted sections are preserved.

        If the merged config removes a doc_type or lifecycle state that
        still has documents attached, raises DestructiveConfigChangeError
        unless ``force`` is True.

        When ``body.dry_run`` is True, runs the merge, schema
        validation, vault.id-change check, and destructive-change
        detection, but skips ``_write_config_yaml`` and
        ``_registry_service.reload``. Dry-run NEVER raises
        ``DestructiveConfigChangeError``; warnings are always returned
        in the response body so the caller can decide whether to
        retry with ``force=True``. ``force`` is a no-op on dry-run.
        """
        if self._registry_service is None:
            raise RuntimeError(
                "VaultConfigService.update_config requires a registry service "
                "back-reference; this service was constructed in a context "
                "(e.g. a test fixture) that did not wire one."
            )

        old_config = self._config

        merged = old_config.model_dump()
        # Exclude `dry_run` from the body dict — it's a request
        # control field, not a config section. (`_ALL_SECTIONS` filtering
        # below would skip it anyway, but excluding here keeps `merged`
        # clean for the section-diff computation.)
        body_dict = body.model_dump(exclude_none=True, exclude={"dry_run"})
        for section in _ALL_SECTIONS:
            if section in body_dict:
                merged[section] = body_dict[section]

        if "vault" in body_dict and body_dict["vault"].get("id") != vault_id:
            raise VaultConfigValidationError(
                ["vault.id cannot be changed; create a new vault instead"]
            )

        new_config = _validate_config(merged)

        warnings = await _check_destructive_changes(old_config, new_config, self._store)

        # Dry-run path — compute the section-level diff and
        # return the preview without writing yaml or reloading the
        # registry. dry-run never raises DestructiveConfigChangeError;
        # warnings (if any) appear in the response body so the caller
        # can decide whether to follow up with a real-run + force.
        if body.dry_run:
            old_dump = old_config.model_dump()
            changed_sections = [
                section
                for section in _ALL_SECTIONS
                if section in body_dict and merged[section] != old_dump.get(section)
            ]
            return UpdateVaultConfigResponse(
                status="previewed",
                vault_id=vault_id,
                warnings=warnings,
                dry_run=True,
                preview=VaultConfigPreview(changed_sections=changed_sections),
            )

        if warnings and not force:
            raise DestructiveConfigChangeError(warnings)

        config_path = config_path_for_vault(vault_id)
        # Snapshot the pre-write bytes so a reload failure below can
        # restore the on-disk yaml byte-for-byte. Snapshotting bytes
        # (rather than re-serializing the model) preserves the shape of
        # the original yaml even when caller body sections differ in
        # field-completeness from a model_dump round-trip.
        old_yaml_bytes = config_path.read_bytes() if config_path.exists() else None

        _write_config_yaml(config_path, merged)

        try:
            await self._registry_service.reload(vault_id, new_config)
        except BaseException:
            # Roll back the yaml so on-disk state continues to match the
            # in-memory registry, which still holds the old services per
            # reload_vault_in_registry's build-new-first guarantee. The
            # original exception is the one that propagates -- rollback
            # failures are logged and swallowed so they cannot mask the
            # cause.
            try:
                if old_yaml_bytes is not None:
                    _atomic_write_bytes(config_path, old_yaml_bytes)
                else:
                    config_path.unlink(missing_ok=True)
            except BaseException:
                logger.exception(
                    "rollback of vault_config.yaml failed after reload "
                    "failure for vault_id=%s; on-disk yaml may diverge "
                    "from in-memory state",
                    vault_id,
                )
            raise

        return UpdateVaultConfigResponse(
            status="updated",
            vault_id=vault_id,
            warnings=warnings,
            dry_run=False,
            preview=None,
        )
