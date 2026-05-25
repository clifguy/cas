"""Directory scan service (BE-017 through BE-021).

Walks a directory, matches files to adapters by extension, hashes each
file, parses filenames, and checks hashes against a SAGE vault to
determine new/modified/unchanged status.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from sage.config import VaultConfig
from sage.models.enums import SourceType
from sage.services.filename_parser import FilenameParser, ParsedMetadata
from sage.source_adapters.base import SourceAdapter
from sage.storage.graph_store import GraphStore

logger = logging.getLogger(__name__)


def build_extension_map(
    adapters: dict[SourceType, SourceAdapter],
) -> dict[str, str]:
    """Derive file-extension-to-source-type mapping from the adapter registry."""
    ext_map: dict[str, str] = {}
    for source_type, adapter in adapters.items():
        for ext in adapter.EXTENSIONS:
            ext_map[ext] = source_type.value
    return ext_map


@dataclass
class ScanResult:
    file_path: str
    file_hash: str
    source_modified_at: str
    source_type: str | None
    parsed_metadata: ParsedMetadata
    sage_status: str  # "new", "modified", "unchanged", "no_adapter", "adapter_disabled"

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _compute_file_hash(path: Path) -> str:
    """Compute SHA-256 hash of file content with sha256: prefix."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def _get_mtime_iso(path: Path) -> str:
    """Get file modification time as ISO 8601 string."""
    mtime = path.stat().st_mtime
    dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
    return dt.isoformat()


def _detect_adapter(path: Path, extension_map: dict[str, str]) -> str | None:
    """Detect adapter from file extension."""
    return extension_map.get(path.suffix.lower())


def _walk_directory(directory: Path, max_depth: int | None) -> tuple[list[Path], list[str]]:
    """Walk directory with optional depth limit. Returns (files, warnings)."""
    files: list[Path] = []
    warnings: list[str] = []

    def _walk(current: Path, depth: int) -> None:
        if max_depth is not None and depth > max_depth:
            return
        try:
            entries = sorted(current.iterdir())
        except PermissionError:
            warnings.append(f"Permission denied: {current}")
            return

        for entry in entries:
            if entry.name.startswith(".") or entry.name.startswith("~$"):
                continue  # Skip hidden files and Word temp files
            try:
                if entry.is_file():
                    files.append(entry)
                elif entry.is_dir():
                    _walk(entry, depth + 1)
            except PermissionError:
                warnings.append(f"Permission denied: {entry}")

    _walk(directory, 0)
    return files, warnings


async def scan_directory(
    directory: Path,
    vault_config: VaultConfig,
    graph_store: GraphStore,
    extension_map: dict[str, str],
    max_depth: int | None = None,
) -> tuple[list[ScanResult], list[str]]:
    """Scan a directory and return file metadata with SAGE status.

    Args:
        directory: Absolute path to scan.
        vault_config: Vault configuration (for metadata_extraction config).
        graph_store: For hash-check against existing documents.
        extension_map: File extension to source type mapping (from build_extension_map).
        max_depth: Max recursion depth (None = unlimited, 0 = no recursion).

    Returns:
        (scan_results, warnings): Results for each file, plus permission warnings.
    """
    doc_types_raw = [
        {"value": dt.value, "source_types": dt.source_types}
        for dt in vault_config.document_types.doc_types
        if dt.source_types is not None
    ] or None
    parser = FilenameParser(vault_config.metadata_extraction, doc_types=doc_types_raw)

    # Build set of enabled source types from vault config
    enabled_source_types: set[str] = set()
    for adapter_entry in vault_config.source_adapters.get("adapters", []):
        if adapter_entry.get("enabled", True):
            enabled_source_types.add(adapter_entry["source_type"])

    # Walk directory
    file_paths, warnings = _walk_directory(directory, max_depth)

    # Compute hashes and detect adapters
    file_infos: list[tuple[Path, str, str | None, bool, str]] = []
    hashes_to_check: list[str] = []

    for path in file_paths:
        adapter = _detect_adapter(path, extension_map)
        enabled = adapter is not None and adapter in enabled_source_types
        file_hash = _compute_file_hash(path)
        mtime = _get_mtime_iso(path)
        file_infos.append((path, file_hash, adapter, enabled, mtime))
        if enabled:
            hashes_to_check.append(file_hash)

    # Bulk hash check against vault
    hash_matches = await graph_store.find_documents_by_hashes(hashes_to_check)

    # Also check by source path for "modified" detection
    # A file is "modified" if its path matches an existing doc but hash differs
    all_source_paths = [str(p) for p, _h, a, en, _m in file_infos if en]
    path_to_existing: dict[str, str] = {}
    for sp in all_source_paths:
        docs = await graph_store.find_by_source_path(sp)
        if docs:
            path_to_existing[sp] = docs[0].source_content_hash

    # Build results
    results: list[ScanResult] = []
    for path, file_hash, adapter, enabled, mtime in file_infos:
        parsed = parser.parse(path.stem, adapter=adapter)

        if adapter is None:
            status = "no_adapter"
        elif not enabled:
            status = "adapter_disabled"
        elif file_hash in hash_matches:
            status = "unchanged"
        elif str(path) in path_to_existing:
            status = "modified"
        else:
            status = "new"

        results.append(
            ScanResult(
                file_path=str(path),
                file_hash=file_hash,
                source_modified_at=mtime,
                source_type=adapter,
                parsed_metadata=parsed,
                sage_status=status,
            )
        )

    return results, warnings
