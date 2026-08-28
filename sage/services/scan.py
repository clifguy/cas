"""Directory scan service: pre-ingest discovery over a filesystem tree.

Walks a directory, matches files to adapters by extension, hashes each
file, parses filenames, and checks hashes against a SAGE vault to
determine new/modified/unchanged status. Side-effect free with respect
to the vault. Consumed by the MCP ``list_directory`` tool and the
in-process ``/app/scan`` delivery path.

The walk-and-hash phase is blocking filesystem work, so it runs on a
dedicated single-thread executor rather than the event loop: a scan of
an arbitrarily large tree must never freeze the server's other callers.
The scan is also scope-bound — a default depth ceiling plus file-count
and byte ceilings — and reports truncation explicitly rather than
returning a silent partial result.

This is intrinsically a co-located-filesystem capability: it stats,
reads, and hashes files on local disk. Deployments that reach SAGE
across a container boundary have no shared filesystem to walk, so the
directory-walk discovery affordance is local-profile only.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from sage.adapters.interfaces import GraphStore
from sage.config import VaultConfig
from sage.models.enums import SourceType
from sage.services.filename_parser import FilenameParser, ParsedMetadata
from sage.source_adapters.base import SourceAdapter

logger = logging.getLogger(__name__)

# Scan ceilings. The depth ceiling applies when the caller does not pass
# one; the file and byte ceilings always apply. Sized so any legitimate
# staging tree passes untouched while a misdirected scan of a huge tree
# (a home directory, a synced drive) is cut instead of hashing for
# minutes. Hitting any ceiling is reported via the ``truncated`` flag
# and a warning — never a silent partial result.
DEFAULT_MAX_DEPTH = 16
MAX_SCAN_FILES = 10_000
MAX_SCAN_BYTES = 10 * 1024**3

# Dedicated executor for the blocking walk-and-hash unit. Module-level
# (not per-call) so it is shared across scans and lives for the process,
# never leaking a thread per scan; one worker also serializes concurrent
# scans so parallel callers cannot multiply filesystem load.
_SCAN_EXECUTOR: ThreadPoolExecutor | None = None


def _get_scan_executor() -> ThreadPoolExecutor:
    global _SCAN_EXECUTOR
    if _SCAN_EXECUTOR is None:
        _SCAN_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sage-scan")
    return _SCAN_EXECUTOR


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
    sage_status: str  # "new", "modified", "unchanged", "no_adapter"

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


def _walk_directory(
    directory: Path, max_depth: int, max_files: int
) -> tuple[list[Path], list[str], bool, bool]:
    """Walk directory with depth and file-count ceilings.

    Returns ``(files, warnings, file_capped, depth_pruned)``. The walk
    stops collecting once ``max_files`` is reached (``file_capped``) and
    skips directories deeper than ``max_depth`` (``depth_pruned``); the
    caller decides how each ceiling is reported.
    """
    files: list[Path] = []
    warnings: list[str] = []
    file_capped = False
    depth_pruned = False

    def _walk(current: Path, depth: int) -> None:
        nonlocal file_capped, depth_pruned
        if depth > max_depth:
            depth_pruned = True
            return
        try:
            entries = sorted(current.iterdir())
        except PermissionError:
            warnings.append(f"Permission denied: {current}")
            return

        for entry in entries:
            if file_capped:
                return
            if entry.name.startswith(".") or entry.name.startswith("~$"):
                continue  # Skip hidden files and Word temp files
            try:
                if entry.is_file():
                    if len(files) >= max_files:
                        file_capped = True
                        return
                    files.append(entry)
                elif entry.is_dir():
                    _walk(entry, depth + 1)
            except PermissionError:
                warnings.append(f"Permission denied: {entry}")

    _walk(directory, 0)
    return files, warnings, file_capped, depth_pruned


def _scan_files_sync(
    directory: Path,
    max_depth: int,
    depth_defaulted: bool,
    extension_map: dict[str, str],
    max_files: int,
    max_bytes: int,
) -> tuple[list[tuple[Path, str, str | None, str]], list[str], bool]:
    """Walk, hash, and stat a directory tree as one blocking unit.

    Encapsulates the entire blocking filesystem phase of a scan so the
    caller can dispatch it to the scan executor in a single hop. Returns
    ``(file_infos, warnings, truncated)`` where each file info is
    ``(path, file_hash, adapter, mtime_iso)``.

    Truncation semantics: the walk stops at ``max_files``; hashing stops
    once the cumulative bytes read reach ``max_bytes``; and a walk pruned
    by the *default* depth ceiling (``depth_defaulted``) counts as
    truncation, while a caller-chosen ``max_depth`` is intentional
    pruning and does not. Every truncation appends a warning naming the
    ceiling that fired.
    """
    file_paths, warnings, file_capped, depth_pruned = _walk_directory(
        directory, max_depth, max_files
    )
    truncated = False
    if file_capped:
        truncated = True
        warnings.append(
            f"Scan truncated: file ceiling ({max_files} files) reached; "
            "narrow the directory or scan subdirectories separately"
        )
    if depth_pruned and depth_defaulted:
        truncated = True
        warnings.append(
            f"Scan truncated: default depth ceiling ({max_depth}) reached; "
            "pass max_depth to scan deeper"
        )

    file_infos: list[tuple[Path, str, str | None, str]] = []
    bytes_hashed = 0
    for index, path in enumerate(file_paths):
        if bytes_hashed >= max_bytes:
            truncated = True
            warnings.append(
                f"Scan truncated: byte ceiling ({max_bytes} bytes) reached "
                f"after hashing {index} files"
            )
            break
        adapter = _detect_adapter(path, extension_map)
        file_hash = _compute_file_hash(path)
        bytes_hashed += path.stat().st_size
        mtime = _get_mtime_iso(path)
        file_infos.append((path, file_hash, adapter, mtime))

    return file_infos, warnings, truncated


async def scan_directory(
    directory: Path,
    vault_config: VaultConfig,
    graph_store: GraphStore,
    extension_map: dict[str, str],
    max_depth: int | None = None,
    max_files: int | None = None,
    max_bytes: int | None = None,
) -> tuple[list[ScanResult], list[str], bool]:
    """Scan a directory and return file metadata with SAGE status.

    The blocking walk-and-hash phase runs on the dedicated scan executor;
    only the vault lookups and result assembly run on the event loop.

    Args:
        directory: Absolute path to scan.
        vault_config: Vault configuration (for metadata_extraction config).
        graph_store: For hash-check against existing documents.
        extension_map: File extension to source type mapping (from build_extension_map).
        max_depth: Max recursion depth (None = the default depth ceiling,
            0 = no recursion). A walk cut by the default ceiling is
            reported as truncated; a caller-chosen depth prunes silently.
        max_files: File-count ceiling (None = the module default).
        max_bytes: Cumulative hashed-bytes ceiling (None = the module default).

    Returns:
        (scan_results, warnings, truncated): Results for each file,
        warnings (permission errors and any ceiling cuts), and whether a
        ceiling truncated the scan.
    """
    doc_types_raw = [
        {"value": dt.value, "source_types": dt.source_types}
        for dt in vault_config.document_types.doc_types
        if dt.source_types is not None
    ] or None
    parser = FilenameParser(vault_config.metadata_extraction, doc_types=doc_types_raw)

    depth_defaulted = max_depth is None
    effective_depth = DEFAULT_MAX_DEPTH if depth_defaulted else max_depth
    effective_max_files = MAX_SCAN_FILES if max_files is None else max_files
    effective_max_bytes = MAX_SCAN_BYTES if max_bytes is None else max_bytes

    # Walk, hash, and stat off the event loop as a single blocking unit.
    # Every extension the process-wide registry maps is ingestable: adapter
    # availability is code-determined, not per-vault (CAS-ADR-046), so scan
    # reports exactly what an ingest of the same file would accept.
    loop = asyncio.get_running_loop()
    file_infos, warnings, truncated = await loop.run_in_executor(
        _get_scan_executor(),
        _scan_files_sync,
        directory,
        effective_depth,
        depth_defaulted,
        extension_map,
        effective_max_files,
        effective_max_bytes,
    )

    hashes_to_check = [h for _p, h, a, _m in file_infos if a is not None]

    # Bulk hash check against vault
    hash_matches = await graph_store.find_documents_by_hashes(hashes_to_check)

    # Also check by source path for "modified" detection
    # A file is "modified" if its path matches an existing doc but hash differs.
    # Bulk, like the hash check above: the lookup phase costs a fixed number of
    # graph-store round-trips, so it does not scale with the file count.
    all_source_paths = [str(p) for p, _h, a, _m in file_infos if a is not None]
    path_to_existing = await graph_store.find_documents_by_source_paths(all_source_paths)

    # Build results
    results: list[ScanResult] = []
    for path, file_hash, adapter, mtime in file_infos:
        parsed = parser.parse(path.stem, adapter=adapter)

        if adapter is None:
            status = "no_adapter"
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

    return results, warnings, truncated
