"""Three-stage ingestion pipeline (BH-018 through BH-026, BH-068).

Stage 1 (projection): Reads source file, produces structured text.
Stage 2 (indexing): Chunks, embeds, stores in content store.
Stage 3 (abstraction): Generates semantic abstract via LLM.

All three stages run sequentially within ingest(). The method returns
after the full pipeline completes, keeping peak memory bounded to one
document at a time (BH-026, BH-068).
"""

import asyncio
import ctypes
import ctypes.util
import hashlib
import logging
import os
import shutil
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import jsonschema

from sage.adapters.abstraction_utils import compute_max_tokens, trim_to_sentence_boundary
from sage.adapters.interfaces import (
    SYNTHETIC_HEADER_HEADING_PATH,
    AbstractionProvider,
    Chunk,
    ContentStore,
    EmbeddingProvider,
)
from sage.api.errors import (
    AdapterNotFoundError,
    DocumentNotFoundError,
    DuplicateContentError,
    IdenticalContentSupersedeError,
    NoProjectionError,
    SourceFileNotFoundError,
    SupersedeTargetNotActiveError,
    Tier3SchemaViolationError,
)
from sage.config import VaultConfig
from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import (
    Document,
    IngestRequest,
    ParseFilenameResponse,
    SetLifecycleRequest,
)
from sage.services.filename_parser import FilenameParser, ParsedMetadata
from sage.services.identity import generate_document_id
from sage.source_adapters.base import ProjectionResult, SourceAdapter
from sage.storage.graph_store import GraphStore
from sage.storage.locks import DocumentLockManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# UI-layer metadata normalization (CAS-ADR-016)
# ---------------------------------------------------------------------------
#
# Agents often flag their working temp files invisible on macOS (BSD
# UF_HIDDEN chflag, or com.apple.FinderInfo invisible bit). When SAGE
# copies such a file into the vault via shutil.copy2, the BSD chflag
# propagates to the canonical copy, hiding it from Finder. The invisible
# bit encodes source-artifact semantics ("this is scratch"), not
# canonical-artifact semantics -- the vault is the state substrate and
# its files must remain user-auditable.
#
# Empirical behavior on macOS + CPython 3.12/3.14:
#   * shutil.copy2 DOES propagate UF_HIDDEN (via os.chflags in copystat).
#   * shutil.copy2 does NOT propagate com.apple.FinderInfo xattr on macOS
#     (Python stdlib has no xattr API there; _copyxattr is a no-op).
#
# Clearing the xattr is therefore defensive: guards against future Python
# versions that add macOS xattr support, alternative copy mechanisms, or
# filesystem operations that propagate FinderInfo.
#
# macOS lacks a Python stdlib xattr API, so we call libc's getxattr /
# setxattr / removexattr via ctypes. No third-party dependency.


_XATTR_NOFOLLOW = 0x0001
_FINDER_INFO_NAME = b"com.apple.FinderInfo"
_FINDER_INFO_LEN = 32
_FINDER_INVISIBLE_MASK = 0x40  # bit 0x40 in byte 8 of FinderInfo


def _macos_libc() -> ctypes.CDLL | None:
    """Load libc on macOS and declare signatures for xattr functions.

    Returns None on non-macOS platforms so callers can treat absence as
    "nothing to sanitize."
    """
    if sys.platform != "darwin":
        return None
    lib_path = ctypes.util.find_library("c")
    if lib_path is None:
        return None
    libc = ctypes.CDLL(lib_path, use_errno=True)

    # ssize_t getxattr(const char *path, const char *name,
    #                  void *value, size_t size,
    #                  u_int32_t position, int options);
    libc.getxattr.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    libc.getxattr.restype = ctypes.c_ssize_t

    # int setxattr(const char *path, const char *name,
    #              void *value, size_t size,
    #              u_int32_t position, int options);
    libc.setxattr.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    libc.setxattr.restype = ctypes.c_int

    # int removexattr(const char *path, const char *name, int options);
    libc.removexattr.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_int,
    ]
    libc.removexattr.restype = ctypes.c_int

    return libc


# Cached at module load. None on non-macOS.
_LIBC = _macos_libc()


def _read_finder_info(path: Path) -> bytes | None:
    """Return com.apple.FinderInfo payload, or None if absent / unavailable."""
    if _LIBC is None:
        return None
    buf = (ctypes.c_ubyte * _FINDER_INFO_LEN)()
    rc = _LIBC.getxattr(
        str(path).encode("utf-8"),
        _FINDER_INFO_NAME,
        buf,
        _FINDER_INFO_LEN,
        0,
        _XATTR_NOFOLLOW,
    )
    if rc < 0:
        return None
    return bytes(buf)[:rc]


def _write_finder_info(path: Path, data: bytes) -> bool:
    """Write com.apple.FinderInfo; returns True on success."""
    if _LIBC is None:
        return False
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    rc = _LIBC.setxattr(
        str(path).encode("utf-8"),
        _FINDER_INFO_NAME,
        buf,
        len(data),
        0,
        _XATTR_NOFOLLOW,
    )
    return rc == 0


def _remove_finder_info(path: Path) -> bool:
    """Remove com.apple.FinderInfo; returns True on success or absent."""
    if _LIBC is None:
        return False
    rc = _LIBC.removexattr(
        str(path).encode("utf-8"),
        _FINDER_INFO_NAME,
        _XATTR_NOFOLLOW,
    )
    return rc == 0


def _strip_ui_invisibility(path: Path) -> None:
    """Clear macOS UI-invisibility markers from a file.

    On macOS: clears the BSD UF_HIDDEN chflag and clears bit 0x40 in
    byte 8 of com.apple.FinderInfo (kIsInvisible). Preserves all other
    bytes of the xattr (type/creator codes, color labels, stationery
    flag, etc.).

    On non-macOS platforms: no-op.

    Errors are swallowed: UI-layer sanitization is best-effort and must
    not fail an ingest. Logged at debug level for diagnosis.
    """
    if sys.platform != "darwin":
        return

    # 1. BSD UF_HIDDEN chflag.
    try:
        st = os.lstat(str(path))
        flags = getattr(st, "st_flags", 0)
        if flags & stat.UF_HIDDEN:
            os.chflags(str(path), flags & ~stat.UF_HIDDEN)
    except (OSError, AttributeError) as exc:
        logger.debug("UF_HIDDEN sanitization failed for %s: %s", path, exc)

    # 2. com.apple.FinderInfo invisible bit.
    try:
        info = _read_finder_info(path)
        if info is None or len(info) < 9:
            return
        if not (info[8] & _FINDER_INVISIBLE_MASK):
            return  # bit not set; nothing to do
        new_info = bytearray(info)
        new_info[8] &= ~_FINDER_INVISIBLE_MASK
        # Pad / truncate to canonical 32 bytes for Finder compatibility.
        if len(new_info) < _FINDER_INFO_LEN:
            new_info.extend(b"\x00" * (_FINDER_INFO_LEN - len(new_info)))
        elif len(new_info) > _FINDER_INFO_LEN:
            new_info = new_info[:_FINDER_INFO_LEN]
        # If every byte is zero after clearing, remove the xattr entirely.
        if all(b == 0 for b in new_info):
            _remove_finder_info(path)
        else:
            _write_finder_info(path, bytes(new_info))
    except Exception as exc:  # noqa: BLE001 -- best-effort sanitization
        logger.debug("FinderInfo sanitization failed for %s: %s", path, exc)


@dataclass
class IngestResult:
    """Result of an ingestion operation."""

    document: Document
    is_new: bool


def _deep_merge_dicts(base: dict, override: dict) -> dict:
    """Recursively merge override into a copy of base.

    Override values replace base values at the same key, except when both
    sides hold dicts at the same key — those merge recursively.
    """
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


class IngestionService:
    def __init__(
        self,
        graph_store: GraphStore,
        lock_manager: DocumentLockManager,
        content_store: ContentStore,
        embedding_provider: EmbeddingProvider,
        abstraction_provider: AbstractionProvider,
        config: VaultConfig,
        source_adapters: dict[SourceType, SourceAdapter] | None = None,
        lifecycle_service: object | None = None,
    ) -> None:
        self._store = graph_store
        self._locks = lock_manager
        self._content_store = content_store
        self._embedding = embedding_provider
        self._abstraction = abstraction_provider
        self._config = config
        self._adapters = source_adapters or {}
        self._lifecycle_service = lifecycle_service

        # Build the vault's FilenameParser once per service instance (CAS-ADR-015).
        # Only active when the vault's metadata_extraction block declares a
        # filename_extraction.pattern; otherwise filename parsing is skipped
        # and the adapter's ProjectionResult.title is preserved (ME-004).
        self._filename_parser: FilenameParser | None = None
        me = getattr(config, "metadata_extraction", None) or {}
        fe = (me or {}).get("filename_extraction") or {}
        if fe.get("pattern"):
            doc_types_raw = [
                {
                    "value": dt.value,
                    "label": dt.label,
                    "source_types": dt.source_types,
                }
                for dt in config.document_types.doc_types
            ]
            self._filename_parser = FilenameParser(me, doc_types=doc_types_raw)

    @property
    def registered_adapters(self) -> dict[SourceType, SourceAdapter]:
        """Return the runtime adapter registry."""
        return dict(self._adapters)

    def _merge_adapter_config(
        self, source_type: SourceType, request_config: dict | None
    ) -> dict | None:
        """Return adapter config with vault-level base merged with per-request override.

        Looks up the vault's ``source_adapters.adapters[]`` entry whose
        ``source_type`` matches ``source_type``. If found, its ``config``
        dict is the base; ``request_config`` overrides on collision via
        recursive merge (request keys override vault keys; nested dicts
        merge key-by-key, e.g. ``heading_style_map`` accumulates entries
        from both sources). Returns ``None`` when both are absent so
        adapters that branch on ``config is None`` keep their legacy
        fast path.
        """
        adapters_block = (
            self._config.source_adapters.get("adapters")
            if isinstance(self._config.source_adapters, dict)
            else None
        )
        vault_config: dict | None = None
        if adapters_block:
            for entry in adapters_block:
                if entry.get("source_type") == source_type.value:
                    vault_config = entry.get("config")
                    break
        if vault_config is None and request_config is None:
            return None
        if vault_config is None:
            return dict(request_config) if request_config else None
        if not request_config:
            return dict(vault_config)
        return _deep_merge_dicts(vault_config, request_config)

    def _ensure_vault_local(self, source_path: Path, storage_root: Path) -> str:
        """Return a vault-relative path to the source file, copying it
        into ``{storage_root}/imports/`` if it lives outside the vault.

        Internal files (already under *storage_root*) are returned as-is
        with a normalized relative path.  External files are copied
        verbatim; on filename collision a content-hash suffix is appended.

        Returns:
            Vault-relative path string (e.g. ``patents/doc.md`` or
            ``imports/doc_a1b2c3d4.md``).
        """
        try:
            relative = source_path.relative_to(storage_root)
            return str(relative)
        except ValueError:
            pass  # external file -- fall through to import

        imports_dir = storage_root / "imports"
        imports_dir.mkdir(exist_ok=True)

        dest = imports_dir / source_path.name
        if dest.exists():
            content_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()[:8]
            existing_hash = hashlib.sha256(dest.read_bytes()).hexdigest()[:8]
            if content_hash == existing_hash:
                # Identical content already imported -- reuse existing path
                return str(dest.relative_to(storage_root))
            # Different content: disambiguate with 8-char content hash
            stem = source_path.stem
            suffix = source_path.suffix
            dest = imports_dir / f"{stem}_{content_hash}{suffix}"

        shutil.copy2(source_path, dest)
        # Strip UI-layer invisibility markers that shutil.copy2 may have
        # propagated from an agent's temp source (CAS-ADR-016).
        _strip_ui_invisibility(dest)
        return str(dest.relative_to(storage_root))

    async def ingest(
        self,
        request: IngestRequest,
        wait_for_pipeline: bool = True,
    ) -> IngestResult:
        """Execute Stage 1 synchronously and run Stages 2-3 sync or async.

        When `wait_for_pipeline` is True (default), Stages 2-3 (embedding,
        abstraction) are awaited inline; memory use is bounded to one
        document at a time across successive calls (BH-068). This is the
        mode the batch ingest service uses to preserve memory discipline
        during bulk runs.

        When `wait_for_pipeline` is False, Stages 2-3 dispatch via
        `asyncio.create_task` and the call returns after projection,
        record insertion, metadata application, and the supersede
        lifecycle transition (if requested) have committed. Used by the
        MCP `sage_ingest` tool to stay under the 60-second MCP client
        timeout on documents whose abstraction latency would otherwise
        exceed it. The supersede transition runs synchronously
        regardless of this flag (BH-129): the version chain must be
        complete when the call returns.

        Returns:
            IngestResult with the document and whether it was newly created.
            When `wait_for_pipeline` is False the document's
            pipeline_status will typically be non-terminal (indexing_in_progress
            or projection_complete); callers poll `get_document` for terminal
            state.

        Raises:
            DuplicateContentError: Same content hash exists anywhere in vault
                (BH-018, BH-066), unless force flag set.
            AdapterNotFoundError: No adapter for requested source type.
            DocumentNotFoundError: `supersedes_document_id` does not exist.
            SupersedeTargetNotActiveError: predecessor is not active.
            IdenticalContentSupersedeError: new content matches predecessor.
        """
        adapter = self._adapters.get(request.adapter)
        if adapter is None:
            raise AdapterNotFoundError(request.adapter)

        # Pre-validate the supersede predecessor BEFORE running projection
        # (BH-121, BH-122, BH-124). Fail-fast keeps pipeline work behind
        # cheap validity checks. The identical-content check happens
        # post-projection, once the new file's hash is known.
        predecessor: Document | None = None
        if request.supersedes_document_id:
            predecessor = await self._store.get_document(request.supersedes_document_id)
            if predecessor is None:
                raise DocumentNotFoundError(request.supersedes_document_id)
            if predecessor.lifecycle_status != "active":
                raise SupersedeTargetNotActiveError(
                    request.supersedes_document_id,
                    predecessor.lifecycle_status,
                )

        # Resolve source path: relative to storage_root, or absolute external
        storage_root = Path(self._config.vault.storage_root).expanduser().resolve()
        source_input = Path(request.source)

        if source_input.is_absolute():
            source_path = source_input
        else:
            source_path = storage_root / request.source

        if not source_path.exists():
            raise SourceFileNotFoundError(request.source)

        # Import external files into the vault (BH-053 through BH-057)
        source_path = source_path.resolve()
        vault_relative = self._ensure_vault_local(source_path, storage_root)

        # Stage 1: Projection (synchronous). Merge vault-level adapter config
        # with the per-request config; per-request keys override vault keys
        # on collision. The vault's source_adapters[].config is the authority
        # for adapter behavior across all ingests; the request override is a
        # per-call escape hatch.
        merged_config = self._merge_adapter_config(request.adapter, request.config)
        projection = await adapter.project(storage_root / vault_relative, merged_config)

        # Parse filename per vault config (CAS-ADR-015) only when the caller
        # opts in to review (CAS-ADR-021). Default ingests are caller-
        # authoritative: filename inference does not run and the adapter's
        # ProjectionResult.title remains the title fallback. When
        # needs_review is True, current filename-inference behavior is
        # preserved end-to-end.
        parsed = (
            self._parse_source_filename(source_path, request.adapter)
            if request.needs_review
            else None
        )

        # Validate tier3_metadata against the resolved doc_type's
        # metadata_schema (T-0004). Done before any side effects (the
        # adapter projection runs above but is read-only on disk). Resolution
        # mirrors the precedence chain applied below by update_document
        # calls: caller > filename parse > predecessor inheritance > "misc".
        # A None validator (doc_type has no metadata_schema declared) is a
        # hard 400 per the strict no-loose-mode decision.
        if request.tier3_metadata is not None:
            resolved_dt = self._resolve_doc_type_for_tier3(
                request=request, parsed=parsed, predecessor=predecessor
            )
            self._validate_tier3_payload(resolved_dt, request.tier3_metadata)

        # Resolve title with precedence: caller > filename parse > adapter.
        # The adapter's ProjectionResult.title is a content-extraction
        # candidate and loses to filename-parsed title when the vault has
        # declared a filename convention (ME-003). When no filename pattern
        # is configured, the adapter title is preserved (ME-004).
        caller_title = (request.metadata or {}).get("title") if request.metadata else None
        resolved_title = (
            caller_title or (parsed.title if parsed and parsed.title else None) or projection.title
        )

        # Canonicalize the adapter-computed content hash to the
        # Document.source_content_hash shape (`sha256:` + 64 lowercase hex)
        # before crossing the typed-alias boundary (T-0026). Adapters emit
        # raw hex from hashlib.sha256(...).hexdigest(); the canonical-form
        # validator requires the explicit `sha256:` algorithm prefix.
        # Idempotent: an already-prefixed hash passes through unchanged.
        raw_hash = projection.content_hash
        canonical_hash = raw_hash if raw_hash.startswith("sha256:") else f"sha256:{raw_hash}"

        # Supersede identical-content check (BH-123). Fires before the
        # generic duplicate-detection path so the caller gets a distinct
        # error code signalling "no-op edit" rather than "already ingested
        # somewhere in the vault". Only applies when a supersede target
        # was provided and its hash matches the new file's hash.
        if predecessor is not None and (predecessor.source_content_hash == canonical_hash):
            raise IdenticalContentSupersedeError(predecessor.id, canonical_hash)

        # Duplicate detection (BH-018, BH-019, BH-066, BH-067)
        hash_matches = await self._store.find_documents_by_hashes([canonical_hash])

        now = datetime.now(timezone.utc)

        source_modified_at_str = projection.metadata.get("source_modified_at")
        source_modified_at = (
            datetime.fromisoformat(source_modified_at_str) if source_modified_at_str else None
        )

        if hash_matches and not request.force:
            # Hash-only check: identical content already in vault (BH-066)
            existing_id = next(iter(hash_matches.values()))
            raise DuplicateContentError(existing_id, canonical_hash)
        elif hash_matches and request.force:
            # Force re-ingestion: reuse existing record (BH-019, BH-067)
            existing_id = next(iter(hash_matches.values()))
            updates: dict[str, object] = {
                "pipeline_status": PipelineStatus.PROJECTION_COMPLETE.value,
                "pipeline_error": None,
                "projected_at": now.isoformat(),
                "adapter_version": projection.adapter_version,
                "updated_at": now.isoformat(),
                "semantic_abstract": None,
                "indexed_at": None,
                "source_modified_at": source_modified_at_str,
                "document_date": None,
            }
            # Apply tier3_metadata if supplied. Pre-validated above against
            # the resolved doc_type's schema. On force re-ingest the doc_type
            # on the existing record IS the authority; storage replacement
            # is top-level per T-0004 (no deep merge).
            if request.tier3_metadata is not None:
                updates["tier3_metadata"] = request.tier3_metadata
            # Update source_path if the file moved (BH-067)
            if vault_relative != (await self._store.get_document(existing_id)).source_path:
                updates["source_path"] = vault_relative
            doc = await self._store.update_document(existing_id, updates)
            is_new = False

            # Remove old content store entries for re-indexing
            await self._content_store.remove_document(existing_id)

            # Force-reingest with a supersede target: apply the supersede
            # transition on the predecessor. The doc record was reused
            # (no new insert) so atomicity collapses to lifecycle.set_lifecycle's
            # own atomic primitive (BH-135).
            if predecessor is not None and self._lifecycle_service is not None:
                await self._lifecycle_service.set_lifecycle(
                    predecessor.id,
                    SetLifecycleRequest(action="supersede", new_version_id=doc.id),
                )
        else:
            # New document. When a predecessor is being superseded the
            # doc insert + predecessor lifecycle flip + supersedes edge
            # commit as a single SQLite transaction (BH-136). This
            # eliminates the orphan class where a successor record exists
            # but the predecessor was never archived. Without a
            # predecessor it is a single-row insert.
            created_by = request.created_by or self._config.vault.owner
            doc_id = generate_document_id(vault_relative, now.isoformat(), resolved_title)
            doc = Document(
                id=doc_id,
                title=resolved_title,
                source_type=request.adapter,
                source_path=vault_relative,
                lifecycle_status="active",
                source_content_hash=canonical_hash,
                adapter_version=projection.adapter_version,
                created_by=created_by,
                created_at=now,
                last_modified_by=created_by,
                updated_at=now,
                projected_at=now,
                source_modified_at=source_modified_at,
                pipeline_status=PipelineStatus.PROJECTION_COMPLETE,
                tier3_metadata=request.tier3_metadata,
            )
            if predecessor is not None and self._lifecycle_service is not None:
                # Re-read the predecessor inside the (about-to-commit)
                # window to catch a concurrent archive that happened
                # after pre-validation. prepare_supersede consults the
                # transition table without writing.
                fresh_pred = await self._store.get_document(predecessor.id)
                if fresh_pred is None:
                    raise DocumentNotFoundError(predecessor.id)
                transition = self._lifecycle_service.prepare_supersede(fresh_pred, doc.id)
                doc, _updated_pred = await self._store.insert_with_supersede_atomic(
                    doc,
                    fresh_pred.id,
                    transition.predecessor_updates,
                    transition.edge,
                )
            else:
                await self._store.insert_document(doc)
            is_new = True

        # Apply filename-parsed metadata (CAS-ADR-015). Precedence layer:
        # filename parse < caller metadata. Title was already resolved above.
        if parsed is not None:
            parsed_updates = self._build_metadata_updates_from_parsed(parsed)
            # Force branch: reaffirm the resolved title in case the filename
            # changed between ingestions.
            if not is_new and resolved_title:
                parsed_updates["title"] = resolved_title
            if parsed_updates:
                doc = await self._store.update_document(doc.id, parsed_updates)

        # Apply caller-supplied metadata (highest precedence before manual
        # confirmation).
        if request.metadata:
            metadata_updates = self._build_metadata_updates(request.metadata)
            if metadata_updates:
                doc = await self._store.update_document(doc.id, metadata_updates)

        # Chain inheritance for type-shaped trio fields (CAS-ADR-021). When
        # the new document supersedes a predecessor, copy doc_type, project,
        # and authority_scope down the chain on a per-field basis: each
        # field inherits only when the predecessor has a non-None value AND
        # the caller did not supply the field AND the field is unset on the
        # new document. Caller-supplied values continue to win per-field;
        # this step only fills gaps. Version-specific fields (title,
        # version_label, document_date) and tags are explicitly excluded.
        if predecessor is not None:
            caller_keys = set((request.metadata or {}).keys())
            inheritance_updates: dict = {}
            for field in ("doc_type", "project", "authority_scope"):
                pred_value = getattr(predecessor, field, None)
                doc_value = getattr(doc, field, None)
                if pred_value is not None and doc_value is None and field not in caller_keys:
                    inheritance_updates[field] = pred_value
            if inheritance_updates:
                doc = await self._store.update_document(doc.id, inheritance_updates)

        # Merge adapter-emitted tags into document.tags (BH-131, BH-132).
        # The adapter declares owned namespace prefixes so force re-ingest
        # can strip stale adapter-owned tags before applying the fresh set,
        # without disturbing caller- or filename-contributed tags in other
        # namespaces.
        adapter_tags = list(projection.metadata.get("adapter_tags") or [])
        adapter_tag_prefixes = list(projection.metadata.get("adapter_tag_prefixes") or [])
        if adapter_tags or adapter_tag_prefixes:
            current_tags = list(doc.tags or [])
            if adapter_tag_prefixes:
                current_tags = [
                    t
                    for t in current_tags
                    if not any(t.startswith(p) for p in adapter_tag_prefixes)
                ]
            merged = list(dict.fromkeys([*adapter_tags, *current_tags]))
            if merged != (doc.tags or []):
                doc = await self._store.update_document(doc.id, {"tags": merged})

        # Set metadata_confirmed per the caller's needs_review flag
        # (CAS-ADR-021). Caller-authoritative ingests (needs_review=False,
        # the default) commit confirmed; review-queue ingests
        # (needs_review=True) leave metadata unconfirmed until
        # update_metadata is called.
        confirm = not request.needs_review
        if doc.metadata_confirmed != confirm:
            doc = await self._store.update_document(doc.id, {"metadata_confirmed": confirm})

        # Ensure every document has a doc_type for content-store pre-filtering.
        # Defaults to "misc" only when neither filename parse, caller metadata,
        # nor existing record provides one (ME-005, ME-006).
        if not doc.doc_type:
            doc = await self._store.update_document(doc.id, {"doc_type": "misc"})

        # Fallback: derive document_date from source_modified_at (BH-063).
        # Convert to the vault owner's local zone before taking the
        # calendar date so a late-evening local mtime that crosses UTC
        # midnight still attributes to the local work date.
        if not doc.document_date and source_modified_at:
            local_tz = ZoneInfo(self._config.vault.timezone)
            local_date = source_modified_at.astimezone(local_tz).date()
            doc = await self._store.update_document(
                doc.id,
                {
                    "document_date": local_date.isoformat(),
                },
            )

        # The supersede lifecycle transition was bundled into the same
        # SQLite transaction as the new-document insert above (BH-129,
        # BH-136). The version chain is therefore complete here for both
        # the new-document and force-reingest branches.

        # Stages 2-3 (indexing, abstraction) run sync or async per the
        # caller's wait_for_pipeline choice (BH-026, BH-130). Sequential
        # execution (wait_for_pipeline=True) caps peak memory at one
        # document's embeddings and abstraction context -- the bulk
        # ingest path. Fire-and-forget (wait_for_pipeline=False) returns
        # immediately so MCP clients avoid the 60s RPC timeout on long
        # abstractions; the task survives client disconnection because
        # asyncio.create_task detaches it from the request-handling
        # task.
        if wait_for_pipeline:
            await self._run_background_pipeline(doc.id, projection, doc.doc_type)
            doc = await self._store.get_document(doc.id) or doc
        else:
            asyncio.create_task(self._run_background_pipeline(doc.id, projection, doc.doc_type))

        return IngestResult(document=doc, is_new=is_new)

    async def _run_background_pipeline(
        self,
        document_id: str,
        projection: ProjectionResult,
        doc_type: str | None,
    ) -> None:
        """Run Stages 2-3 as a background task. Updates pipeline_status
        in the graph store as it progresses. Catches all exceptions and
        sets pipeline_status to failed with pipeline_error.
        """
        try:
            await self._stage2_indexing(document_id, projection)

            # Check abstraction config
            if not self._config.abstraction.enabled:
                # BH-025: abstraction_skipped
                async with self._locks.lock(document_id):
                    await self._store.update_document(
                        document_id,
                        {
                            "pipeline_status": PipelineStatus.ABSTRACTION_SKIPPED.value,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                return

            # BH-134: empty projection text (e.g., Word template with no body)
            # transitions to abstraction_skipped rather than crashing the
            # abstraction provider's strict-quality edge guard. Other
            # projection surfaces -- style inventory, tags, metadata --
            # are already persisted by Stage 2.
            if not projection.text.strip():
                async with self._locks.lock(document_id):
                    await self._store.update_document(
                        document_id,
                        {
                            "pipeline_status": PipelineStatus.ABSTRACTION_SKIPPED.value,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                return

            await self._stage3_abstraction(document_id, projection, doc_type)
        except Exception as exc:
            logger.exception("Pipeline failed for document %s", document_id)
            async with self._locks.lock(document_id):
                await self._store.update_document(
                    document_id,
                    {
                        "pipeline_status": PipelineStatus.FAILED.value,
                        "pipeline_error": str(exc),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                )

    async def _stage2_indexing(self, document_id: str, projection: ProjectionResult) -> None:
        """Stage 2: Chunk projection, embed, store in content store.
        Sets indexed_at on completion (BH-008).
        """
        async with self._locks.lock(document_id):
            await self._store.update_document(
                document_id,
                {
                    "pipeline_status": PipelineStatus.INDEXING_IN_PROGRESS.value,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )

        # Build body chunks from the projection. Document-identity signals
        # live in a standalone synthetic header chunk (T-0038, F9) — not
        # inlined into chunk[0].
        doc = await self._store.get_document(document_id)
        body_chunks = self._chunk_projection(document_id, projection)

        # Stamp doc_type on body chunks for content-store pre-filtering
        if doc and doc.doc_type and body_chunks:
            for chunk in body_chunks:
                chunk.doc_type = doc.doc_type

        # Prepend the synthetic header chunk. ``semantic_abstract`` is
        # still ``None`` at this point in the pipeline; Stage 3 will
        # rebuild this chunk once abstraction completes.
        chunks: list[Chunk] = []
        if doc is not None:
            chunks.append(self._build_header_chunk(document_id, doc))
        chunks.extend(body_chunks)

        # Embed all chunks. Combine heading_path with content so that
        # semantic search can reach chunks via heading-text-only queries
        # (the equivalent of Word's Find on a heading). Stored chunk.content
        # is unchanged — heading_path travels with the embedder input only.
        if chunks:
            texts = [
                f"{c.heading_path}\n\n{c.content}" if c.heading_path else c.content for c in chunks
            ]
            embeddings = await self._embedding.embed(texts)
            for chunk, embedding in zip(chunks, embeddings):
                chunk.embedding = embedding

        # Store in content store
        await self._content_store.index_chunks(document_id, chunks)

        # Mark indexing complete (BH-008)
        now = datetime.now(timezone.utc)
        async with self._locks.lock(document_id):
            await self._store.update_document(
                document_id,
                {
                    "pipeline_status": PipelineStatus.INDEXING_COMPLETE.value,
                    "indexed_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                },
            )

    async def _refresh_header_chunk(self, document_id: str) -> None:
        """Rebuild the synthetic header chunk after metadata changes (T-0038).

        Loads the current document, rebuilds the header chunk content
        (now potentially with ``semantic_abstract``), re-embeds, and
        writes via the content store's targeted replace method. Body
        chunks are not touched.

        Silently no-ops when the document is missing — the caller is
        expected to have already validated existence (Stage 3 and
        reabstract both do).
        """
        doc = await self._store.get_document(document_id)
        if doc is None:
            return

        chunk = self._build_header_chunk(document_id, doc)
        # Embed with heading_path prepended, matching the body-chunk
        # embed convention in _stage2_indexing so similarity scores are
        # comparable across the synthetic and body chunks.
        text_for_embedding = (
            f"{chunk.heading_path}\n\n{chunk.content}" if chunk.heading_path else chunk.content
        )
        [embedding] = await self._embedding.embed([text_for_embedding])
        chunk.embedding = embedding
        await self._content_store.replace_synthetic_header_chunk(document_id, chunk)

    async def _generate_abstract_text(self, text: str, doc_type: str | None) -> str:
        """Generate a semantic abstract from document text.

        Shared core for both initial ingestion (stage 3) and reabstract.
        Computes a density-proportional token budget, invokes the
        abstraction provider, and trims the result to the last complete
        sentence boundary.

        Args:
            text: Full projection text of the document.
            doc_type: The document's type, threaded into the abstraction
                prompt so the model can pick appropriate descriptive
                verbs and skip metadata the agent already sees.

        Returns:
            Trimmed abstract string.
        """
        word_count = len(text.split())
        max_tokens = compute_max_tokens(word_count, self._config.abstraction)
        raw_abstract = await self._abstraction.generate_abstract(text, max_tokens, doc_type)
        return trim_to_sentence_boundary(raw_abstract)

    async def _stage3_abstraction(
        self,
        document_id: str,
        projection: ProjectionResult,
        doc_type: str | None,
    ) -> None:
        """Stage 3: Generate semantic abstract via LLM (BH-024, BH-025)."""
        async with self._locks.lock(document_id):
            await self._store.update_document(
                document_id,
                {
                    "pipeline_status": PipelineStatus.ABSTRACTION_IN_PROGRESS.value,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )

        abstract = await self._generate_abstract_text(projection.text, doc_type)

        now = datetime.now(timezone.utc)
        async with self._locks.lock(document_id):
            await self._store.update_document(
                document_id,
                {
                    "pipeline_status": PipelineStatus.ABSTRACTION_COMPLETE.value,
                    "semantic_abstract": abstract,
                    "updated_at": now.isoformat(),
                },
            )

        # Refresh the synthetic header chunk so retrieval sees the new
        # ``semantic_abstract`` (T-0038). Body chunks are not touched.
        await self._refresh_header_chunk(document_id)

    async def reabstract(self, document_id: str) -> dict:
        """Re-run abstraction on an existing document (fire-and-forget).

        Validates that the document and its projection chunks exist,
        sets pipeline_status to ABSTRACTION_IN_PROGRESS, then spawns
        the actual abstraction work in a background asyncio.Task.
        Returns immediately with a status dict.

        The caller can poll sage_get_document to observe when
        pipeline_status transitions to ABSTRACTION_COMPLETE (success)
        or FAILED (error).

        Args:
            document_id: ID of the document to re-abstract.

        Returns:
            Dict with status='reabstract_started' and document_id.

        Raises:
            DocumentNotFoundError: Document does not exist.
            NoProjectionError: Document has no stored projection chunks.
        """
        doc = await self._store.get_document(document_id)
        if doc is None:
            raise DocumentNotFoundError(document_id)

        if not await self._content_store.has_chunks(document_id):
            raise NoProjectionError(document_id)

        # Mark in-progress before dispatching background work
        async with self._locks.lock(document_id):
            await self._store.update_document(
                document_id,
                {
                    "pipeline_status": PipelineStatus.ABSTRACTION_IN_PROGRESS.value,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )

        asyncio.create_task(self._reabstract_background(document_id, doc.doc_type))

        now = datetime.now(timezone.utc)
        logger.info("reabstract dispatched for %s at %s", document_id, now.isoformat())
        return {
            "status": "reabstract_started",
            "document_id": document_id,
            "dispatched_at": now.isoformat(),
        }

    async def _reabstract_background(self, document_id: str, doc_type: str | None) -> None:
        """Background worker for reabstract. Loads chunks, generates
        abstract, and updates the document. Sets pipeline_status to
        FAILED on error.

        The initial sleep yields control so the MCP tool response can
        flush through the SSE transport before heavy work begins.
        Without it, the synchronous MLX model load and inference
        block the event loop and prevent the "reabstract_started"
        response from reaching the client.
        """
        await asyncio.sleep(0.1)
        try:
            chunks = await self._content_store.get_all_chunks(document_id)
            # Exclude the synthetic header chunk (T-0038) from the
            # reconstituted projection text so its title/source/tags
            # restatement does not feed back into the abstraction prompt.
            body_chunks = [c for c in chunks if c.heading_path != SYNTHETIC_HEADER_HEADING_PATH]
            projection_text = "\n\n".join(chunk.content for chunk in body_chunks)
            abstract = await self._generate_abstract_text(projection_text, doc_type)
            now = datetime.now(timezone.utc)
            async with self._locks.lock(document_id):
                await self._store.update_document(
                    document_id,
                    {
                        "semantic_abstract": abstract,
                        "pipeline_status": PipelineStatus.ABSTRACTION_COMPLETE.value,
                        "updated_at": now.isoformat(),
                    },
                )

            # Refresh the synthetic header chunk so the new abstract is
            # indexed for retrieval (T-0038).
            await self._refresh_header_chunk(document_id)
        except Exception:
            logger.exception("Background reabstract failed for document %s", document_id)
            async with self._locks.lock(document_id):
                await self._store.update_document(
                    document_id,
                    {
                        "pipeline_status": PipelineStatus.FAILED.value,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                )

    @staticmethod
    def _resolve_doc_type_for_tier3(
        request: IngestRequest,
        parsed: ParsedMetadata | None,
        predecessor: Document | None,
    ) -> str:
        """Pre-resolve the final doc_type for tier3_metadata validation.

        Mirrors the precedence chain applied below by the post-insert
        update_document calls: caller > filename parse > predecessor
        inheritance > "misc". This keeps validation strictly upstream of
        the insert so a tier3_schema_violation never commits a row.
        """
        caller_meta = request.metadata or {}
        caller_dt = caller_meta.get("doc_type")
        if isinstance(caller_dt, str) and caller_dt:
            return caller_dt
        if parsed is not None and parsed.doc_type:
            return parsed.doc_type
        if predecessor is not None and predecessor.doc_type:
            return predecessor.doc_type
        return "misc"

    def _validate_tier3_payload(
        self,
        resolved_doc_type: str,
        tier3: dict,
    ) -> None:
        """Validate a tier3_metadata payload against the resolved doc_type's
        metadata_schema. Raises ``Tier3SchemaViolationError`` when the
        doc_type has no schema declared (strict no-loose-mode) or when the
        payload fails validation.
        """
        validator = self._config.tier3_validator(resolved_doc_type)
        if validator is None:
            raise Tier3SchemaViolationError(
                doc_type=resolved_doc_type,
                path="",
                message=(
                    f"doc_type '{resolved_doc_type}' has no metadata_schema "
                    "declared in vault config"
                ),
                instance=tier3,
            )
        try:
            validator.validate(tier3)
        except jsonschema.ValidationError as exc:
            raise Tier3SchemaViolationError(
                doc_type=resolved_doc_type,
                path=exc.json_path,
                message=exc.message,
                instance=tier3,
            ) from exc

    @staticmethod
    def _build_metadata_updates(metadata: dict[str, str | list[str]]) -> dict:
        """Convert caller-supplied metadata dict to document field updates.

        Known fields are mapped to document columns. Unknown fields are
        stored but have no schema-enforced semantics.
        """
        KNOWN_FIELDS = {
            "title",
            "version_label",
            "project",
            "doc_type",
            "authority_scope",
            "document_date",
        }
        updates: dict = {}
        for key, value in metadata.items():
            if value is None:
                continue
            if key in KNOWN_FIELDS:
                updates[key] = value
            elif key == "date":
                # Filename-parsed date -> document_date (BH-062)
                updates["document_date"] = value
            elif key == "codes":
                # codes stored as comma-separated string -> tags list
                updates["tags"] = [c.strip() for c in value.split(",") if c.strip()]
            elif key == "tags":
                if isinstance(value, list):
                    updates["tags"] = [str(t).strip() for t in value if str(t).strip()]
                else:
                    updates["tags"] = [t.strip() for t in str(value).split(",") if t.strip()]
        return updates

    def _parse_source_filename(
        self, source_path: Path, adapter: SourceType
    ) -> ParsedMetadata | None:
        """Parse the source file's stem using the vault's FilenameParser.

        Returns None when the vault has no filename_extraction pattern
        configured, in which case filename parsing is skipped entirely
        and the adapter's ProjectionResult.title is authoritative (ME-004).

        CAS-ADR-015: metadata extraction is a SAGE-level capability. Any
        ingestion entry point gets uniform behavior without re-implementing
        the parse on the caller side.
        """
        if self._filename_parser is None:
            return None
        adapter_value = adapter.value if isinstance(adapter, SourceType) else str(adapter)
        return self._filename_parser.parse(source_path.stem, adapter=adapter_value)

    def parse_filename(self, filename: str, adapter: SourceType | str) -> ParseFilenameResponse:
        """Side-effect-free filename parse for the parse-filename endpoint.

        Wraps the per-vault FilenameParser without performing an ingest.
        Returns a ParseFilenameResponse whose fields are all null when
        the vault has no filename_extraction.pattern configured. When a
        pattern is configured, fields the parser could not extract are
        null and the codes field is an empty list rather than null.
        """
        if self._filename_parser is None:
            return ParseFilenameResponse()
        adapter_value = adapter.value if isinstance(adapter, SourceType) else str(adapter)
        stem = Path(filename).stem
        parsed = self._filename_parser.parse(stem, adapter=adapter_value)
        return ParseFilenameResponse(
            title=parsed.title,
            project=parsed.project,
            version_label=parsed.version,
            document_date=parsed.date,
            doc_type=parsed.doc_type,
            codes=list(parsed.codes),
        )

    @staticmethod
    def _build_metadata_updates_from_parsed(parsed: ParsedMetadata) -> dict:
        """Translate a ParsedMetadata instance into document field updates.

        Title is handled separately by the caller (resolved_title composes
        caller metadata, parse result, and adapter title in priority order).
        This method maps only the non-title fields.
        """
        updates: dict = {}
        if parsed.date is not None:
            updates["document_date"] = parsed.date
        if parsed.project is not None:
            updates["project"] = parsed.project
        if parsed.version is not None:
            updates["version_label"] = parsed.version
        if parsed.codes:
            updates["tags"] = list(parsed.codes)
        if parsed.doc_type is not None:
            updates["doc_type"] = parsed.doc_type
        return updates

    def _chunk_projection(
        self,
        document_id: str,
        projection: ProjectionResult,
    ) -> list[Chunk]:
        """Split projection into body chunks by heading (T-0038).

        Emits one chunk per heading regardless of whether the heading has
        immediate body content. A heading whose next paragraph is another
        heading at the same or higher level (e.g. a USPTO Section like
        "DETAILED DESCRIPTION" immediately followed by another section
        marker) ends up with empty body content — but its heading_path
        must still enter the FTS index so that searches for the heading
        text find the document, matching the behavior of Word's Find on
        a heading paragraph.

        Body chunks carry projected content only. Document-identity
        signals (title, source filename, tags, semantic_abstract,
        case-split identifier tokens) live in the standalone synthetic
        header chunk built by ``_build_header_chunk`` (T-0038, F9).
        """
        chunks: list[Chunk] = []
        for i, heading in enumerate(projection.headings):
            chunks.append(
                Chunk(
                    document_id=document_id,
                    heading_path=heading.path,
                    content=heading.content,  # may be empty
                    chunk_index=i,
                )
            )

        # If no headings, create a single chunk from the full text
        if not chunks and projection.text.strip():
            chunks.append(
                Chunk(
                    document_id=document_id,
                    heading_path="",
                    content=projection.text,
                    chunk_index=0,
                )
            )

        return chunks

    @staticmethod
    def _build_header_chunk_content(doc: Document) -> str:
        """Build the synthetic document-header chunk's content (T-0038).

        Composes a single text body covering title, source filename stem,
        tags, semantic_abstract, and a case-split identifier-token line.
        The abstract line is included even when ``semantic_abstract`` is
        unset (with an empty value) so the chunk has a stable shape;
        Stage 3 refreshes the chunk once abstraction completes.

        The identifier-token line carries lowercased case-split forms of
        compound identifiers (e.g. ``PortfolioDashboard`` →
        ``portfolio dashboard``) so the BM25 leg can match natural-
        language queries against camelcased identifiers that the
        LanceDB ``simple`` tokenizer leaves intact.
        """
        title = doc.title or ""
        stem = ""
        if doc.source_path:
            filename = doc.source_path.rsplit("/", 1)[-1]
            stem = filename.rsplit(".", 1)[0] if "." in filename else filename
        tags_line = ", ".join(doc.tags) if doc.tags else ""
        abstract = doc.semantic_abstract or ""
        identifier_tokens = IngestionService._case_split_identifiers(title, stem, *(doc.tags or []))

        return (
            f"Title: {title}\n"
            f"Source: {stem}\n"
            f"Tags: {tags_line}\n"
            f"Abstract: {abstract}\n\n"
            f"Identifier tokens: {identifier_tokens}\n"
        )

    @staticmethod
    def _case_split_identifiers(*sources: str) -> str:
        """Return a deduplicated, lowercased space-separated string of
        case-split identifier tokens drawn from the given source strings
        (T-0038).

        For each whitespace/punctuation-bounded token in the sources:
        - All-alpha CamelCase compounds with at least two title-cased
          parts (``PortfolioDashboard``, ``NanoBanana``) are split into
          their constituent words and lowercased.
        - Underscored compounds are also split on the underscore.
        - Other tokens (``XLSX``, ``PV07``, single words) are not
          split; they appear lowercased.

        Output preserves first-seen order and is deduplicated. Returns
        an empty string when no sources contribute any tokens.
        """
        import re

        camel_split = re.compile(r"[A-Z][a-z]+|[A-Z]+(?=[A-Z]|$)|[a-z]+|[0-9]+")
        seen: dict[str, None] = {}
        for source in sources:
            if not source:
                continue
            # Split on whitespace and underscore first.
            for raw in re.split(r"[\s_]+", source):
                if not raw:
                    continue
                # Try CamelCase split when there are 2+ uppercase boundaries
                # in an otherwise alphabetic token.
                if raw.isalpha() and sum(1 for ch in raw if ch.isupper()) >= 2:
                    parts = camel_split.findall(raw)
                    if len(parts) >= 2:
                        for part in parts:
                            key = part.lower()
                            if key:
                                seen.setdefault(key, None)
                        continue
                key = raw.lower()
                if key:
                    seen.setdefault(key, None)
        return " ".join(seen.keys())

    def _build_header_chunk(self, document_id: str, doc: Document) -> Chunk:
        """Build the standalone synthetic document-header chunk (T-0038)."""
        return Chunk(
            document_id=document_id,
            heading_path=SYNTHETIC_HEADER_HEADING_PATH,
            content=self._build_header_chunk_content(doc),
            chunk_index=-1,
            doc_type=doc.doc_type,
        )
