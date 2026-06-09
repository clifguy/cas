"""Service-layer load-bearer for the /app/scan endpoint.

Owns directory validation, extension-map construction, scan
orchestration, and ScanResponse construction. The /app/scan route
handler in app.backend.router reduces to a three-line F1 dispatch
(service lookup, service construction, service call).
"""

from __future__ import annotations

from pathlib import Path

from app.backend.exceptions import InvalidDirectoryError
from app.backend.models import (
    ParsedMetadata,
    ScanRequest,
    ScanResponse,
    ScanResultResponse,
)
from app.backend.scan import ScanResult, build_extension_map, scan_directory
from sage.adapters.interfaces import GraphStore
from sage.config import VaultConfig
from sage.services.ingestion import IngestionService


def _scan_result_to_response(sr: ScanResult) -> ScanResultResponse:
    """Map the internal ScanResult dataclass to the API response model."""
    pm = sr.parsed_metadata
    return ScanResultResponse(
        file_path=sr.file_path,
        file_hash=sr.file_hash,
        source_modified_at=sr.source_modified_at,
        source_type=sr.source_type,
        parsed_metadata=ParsedMetadata(
            title=pm.title,
            date=pm.date,
            project=pm.project,
            codes=pm.codes,
            version=pm.version,
            doc_type=pm.doc_type,
        ),
        sage_status=sr.sage_status,
    )


class ScanService:
    """Service-layer load-bearer for /app/scan."""

    def __init__(
        self,
        vault_config: VaultConfig,
        graph_store: GraphStore,
        ingestion_service: IngestionService,
    ) -> None:
        self.vault_config = vault_config
        self.graph_store = graph_store
        self.ingestion_service = ingestion_service

    async def scan(self, body: ScanRequest) -> ScanResponse:
        directory = Path(body.directory.strip("'\""))
        if not directory.is_dir():
            raise InvalidDirectoryError(str(directory))
        ext_map = build_extension_map(self.ingestion_service.registered_adapters)
        results, warnings = await scan_directory(
            directory=directory,
            vault_config=self.vault_config,
            graph_store=self.graph_store,
            extension_map=ext_map,
            max_depth=body.max_depth,
        )
        return ScanResponse(
            files=[_scan_result_to_response(r) for r in results],
            warnings=warnings,
        )
