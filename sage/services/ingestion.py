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

from sage.adapters.abstraction_utils import compute_max_tokens, trim_to_sentence_boundary
from sage.adapters.interfaces import AbstractionProvider, ContentStore, Chunk, EmbeddingProvider
from sage.api.errors import (
    AdapterNotFoundError,
    DocumentNotFoundError,
    DuplicateContentError,
    IdenticalContentSupersedeError,
    NoProjectionError,
    SourceFileNotFoundError,
    SupersedeTargetNotActiveError,
)
from sage.config import VaultConfig
from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import Document, IngestRequest, SetLifecycleRequest
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
        ctypes.c_char_p, ctypes.c_char_p,
        ctypes.c_void_p, ctypes.c_size_t,
        ctypes.c_uint32, ctypes.c_int,
    ]
    libc.getxattr.restype = ctypes.c_ssize_t

    # int setxattr(const char *path, const char *name,
    #              void *value, size_t size,
    #              u_int32_t position, int options);
    libc.setxattr.argtypes = [
        ctypes.c_char_p, ctypes.c_char_p,
        ctypes.c_void_p, ctypes.c_size_t,
        ctypes.c_uint32, ctypes.c_int,
    ]
    libc.setxattr.restype = ctypes.c_int

    # int removexattr(const char *path, const char *name, int options);
    libc.removexattr.argtypes = [
        ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int,
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
        buf, _FINDER_INFO_LEN,
        0, _XATTR_NOFOLLOW,
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
        buf, len(data),
        0, _XATTR_NOFOLLOW,
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

        # review_required controls metadata_confirmed at ingest (ME-008).
        # When the vault's naming conventions are declared trustworthy
        # (review_required=false), ingested documents are marked confirmed.
        # When review is required, documents await interactive confirmation
        # via update_metadata.
        self._review_required: bool = bool(me.get("review_required", False))

    @property
    def registered_adapters(self) -> dict[SourceType, SourceAdapter]:
        """Return the runtime adapter registry."""
        return dict(self._adapters)

    def _ensure_vault_local(
        self, source_path: Path, storage_root: Path
    ) -> str:
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
        self, request: IngestRequest, wait_for_pipeline: bool = True,
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
            predecessor = await self._store.get_document(
                request.supersedes_document_id
            )
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

        # Stage 1: Projection (synchronous)
        projection = await adapter.project(
            storage_root / vault_relative, request.config
        )

        # Parse filename per vault config (CAS-ADR-015). Active only when the
        # vault declares a filename_extraction.pattern; None otherwise.
        parsed = self._parse_source_filename(source_path, request.adapter)

        # Resolve title with precedence: caller > filename parse > adapter.
        # The adapter's ProjectionResult.title is a content-extraction
        # candidate and loses to filename-parsed title when the vault has
        # declared a filename convention (ME-003). When no filename pattern
        # is configured, the adapter title is preserved (ME-004).
        caller_title = (
            (request.metadata or {}).get("title") if request.metadata else None
        )
        resolved_title = (
            caller_title
            or (parsed.title if parsed and parsed.title else None)
            or projection.title
        )

        # Supersede identical-content check (BH-123). Fires before the
        # generic duplicate-detection path so the caller gets a distinct
        # error code signalling "no-op edit" rather than "already ingested
        # somewhere in the vault". Only applies when a supersede target
        # was provided and its hash matches the new file's hash.
        if predecessor is not None and (
            predecessor.source_content_hash == projection.content_hash
        ):
            raise IdenticalContentSupersedeError(
                predecessor.id, projection.content_hash
            )

        # Duplicate detection (BH-018, BH-019, BH-066, BH-067)
        hash_matches = await self._store.find_documents_by_hashes(
            [projection.content_hash]
        )

        now = datetime.now(timezone.utc)

        source_modified_at_str = projection.metadata.get("source_modified_at")
        source_modified_at = (
            datetime.fromisoformat(source_modified_at_str)
            if source_modified_at_str
            else None
        )

        if hash_matches and not request.force:
            # Hash-only check: identical content already in vault (BH-066)
            existing_id = next(iter(hash_matches.values()))
            raise DuplicateContentError(existing_id, projection.content_hash)
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
            # Update source_path if the file moved (BH-067)
            if vault_relative != (await self._store.get_document(existing_id)).source_path:
                updates["source_path"] = vault_relative
            doc = await self._store.update_document(existing_id, updates)
            is_new = False

            # Remove old content store entries for re-indexing
            await self._content_store.remove_document(existing_id)
        else:
            # New document
            created_by = request.created_by or self._config.vault.owner
            doc_id = generate_document_id(
                vault_relative, now.isoformat(), resolved_title
            )
            doc = Document(
                id=doc_id,
                title=resolved_title,
                source_type=request.adapter,
                source_path=vault_relative,
                lifecycle_status="active",
                source_content_hash=projection.content_hash,
                adapter_version=projection.adapter_version,
                created_by=created_by,
                created_at=now,
                last_modified_by=created_by,
                updated_at=now,
                projected_at=now,
                source_modified_at=source_modified_at,
                pipeline_status=PipelineStatus.PROJECTION_COMPLETE,
            )
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

        # Merge adapter-emitted tags into document.tags (BH-131, BH-132).
        # The adapter declares owned namespace prefixes so force re-ingest
        # can strip stale adapter-owned tags before applying the fresh set,
        # without disturbing caller- or filename-contributed tags in other
        # namespaces.
        adapter_tags = list(projection.metadata.get("adapter_tags") or [])
        adapter_tag_prefixes = list(
            projection.metadata.get("adapter_tag_prefixes") or []
        )
        if adapter_tags or adapter_tag_prefixes:
            current_tags = list(doc.tags or [])
            if adapter_tag_prefixes:
                current_tags = [
                    t for t in current_tags
                    if not any(t.startswith(p) for p in adapter_tag_prefixes)
                ]
            merged = list(dict.fromkeys([*adapter_tags, *current_tags]))
            if merged != (doc.tags or []):
                doc = await self._store.update_document(doc.id, {"tags": merged})

        # Set metadata_confirmed per vault's review_required flag (ME-008).
        # A vault that trusts its naming conventions (review_required=false)
        # confirms metadata at ingest; a vault that requires review leaves
        # metadata unconfirmed until update_metadata is called.
        confirm = not self._review_required
        if doc.metadata_confirmed != confirm:
            doc = await self._store.update_document(
                doc.id, {"metadata_confirmed": confirm}
            )

        # Ensure every document has a doc_type for content-store pre-filtering.
        # Defaults to "misc" only when neither filename parse, caller metadata,
        # nor existing record provides one (ME-005, ME-006).
        if not doc.doc_type:
            doc = await self._store.update_document(doc.id, {"doc_type": "misc"})

        # Fallback: derive document_date from source_modified_at (BH-063)
        if not doc.document_date and source_modified_at:
            doc = await self._store.update_document(doc.id, {
                "document_date": source_modified_at.date().isoformat(),
            })

        # Apply the supersede lifecycle transition on the predecessor
        # BEFORE dispatching Stages 2-3 (BH-120, BH-129). Creates the
        # supersedes edge (new -> old) and sets the predecessor's
        # lifecycle_status to "archived". Running this synchronously
        # with record insertion guarantees the version chain is complete
        # when ingest() returns, regardless of whether the caller
        # chooses sync or async pipeline dispatch. Uses the standard
        # state machine via LifecycleService to honor vault-declared
        # transitions.
        if predecessor is not None and self._lifecycle_service is not None:
            await self._lifecycle_service.set_lifecycle(
                predecessor.id,
                SetLifecycleRequest(action="supersede", new_version_id=doc.id),
            )

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
            await self._run_background_pipeline(doc.id, projection)
            doc = await self._store.get_document(doc.id) or doc
        else:
            asyncio.create_task(self._run_background_pipeline(doc.id, projection))

        return IngestResult(document=doc, is_new=is_new)

    async def _run_background_pipeline(
        self, document_id: str, projection: ProjectionResult
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
                    await self._store.update_document(document_id, {
                        "pipeline_status": PipelineStatus.ABSTRACTION_SKIPPED.value,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    })
                return

            # BH-134: empty projection text (e.g., Word template with no body)
            # transitions to abstraction_skipped rather than crashing the
            # abstraction provider's strict-quality edge guard. Other
            # projection surfaces -- style inventory, tags, metadata --
            # are already persisted by Stage 2.
            if not projection.text.strip():
                async with self._locks.lock(document_id):
                    await self._store.update_document(document_id, {
                        "pipeline_status": PipelineStatus.ABSTRACTION_SKIPPED.value,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    })
                return

            await self._stage3_abstraction(document_id, projection)
        except Exception as exc:
            logger.exception(
                "Pipeline failed for document %s", document_id
            )
            async with self._locks.lock(document_id):
                await self._store.update_document(document_id, {
                    "pipeline_status": PipelineStatus.FAILED.value,
                    "pipeline_error": str(exc),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })

    async def _stage2_indexing(
        self, document_id: str, projection: ProjectionResult
    ) -> None:
        """Stage 2: Chunk projection, embed, store in content store.
        Sets indexed_at on completion (BH-008).
        """
        async with self._locks.lock(document_id):
            await self._store.update_document(document_id, {
                "pipeline_status": PipelineStatus.INDEXING_IN_PROGRESS.value,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

        # Build search preamble from document metadata (BH-058)
        doc = await self._store.get_document(document_id)
        preamble = self._build_search_preamble(doc) if doc else ""

        # Chunk the projection by heading
        chunks = self._chunk_projection(document_id, projection, preamble)

        # Stamp doc_type on chunks for content-store pre-filtering
        if doc and doc.doc_type and chunks:
            for chunk in chunks:
                chunk.doc_type = doc.doc_type

        # Embed all chunks
        if chunks:
            texts = [c.content for c in chunks]
            embeddings = await self._embedding.embed(texts)
            for chunk, embedding in zip(chunks, embeddings):
                chunk.embedding = embedding

        # Store in content store
        await self._content_store.index_chunks(document_id, chunks)

        # Mark indexing complete (BH-008)
        now = datetime.now(timezone.utc)
        async with self._locks.lock(document_id):
            await self._store.update_document(document_id, {
                "pipeline_status": PipelineStatus.INDEXING_COMPLETE.value,
                "indexed_at": now.isoformat(),
                "updated_at": now.isoformat(),
            })

    async def _generate_abstract_text(self, text: str) -> str:
        """Generate a semantic abstract from document text.

        Shared core for both initial ingestion (stage 3) and reabstract.
        Computes a density-proportional token budget, invokes the
        abstraction provider, and trims the result to the last complete
        sentence boundary.

        Args:
            text: Full projection text of the document.

        Returns:
            Trimmed abstract string.
        """
        word_count = len(text.split())
        max_tokens = compute_max_tokens(word_count, self._config.abstraction)
        raw_abstract = await self._abstraction.generate_abstract(
            text, max_tokens
        )
        return trim_to_sentence_boundary(raw_abstract)

    async def _stage3_abstraction(
        self, document_id: str, projection: ProjectionResult
    ) -> None:
        """Stage 3: Generate semantic abstract via LLM (BH-024, BH-025)."""
        async with self._locks.lock(document_id):
            await self._store.update_document(document_id, {
                "pipeline_status": PipelineStatus.ABSTRACTION_IN_PROGRESS.value,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

        abstract = await self._generate_abstract_text(projection.text)

        now = datetime.now(timezone.utc)
        async with self._locks.lock(document_id):
            await self._store.update_document(document_id, {
                "pipeline_status": PipelineStatus.ABSTRACTION_COMPLETE.value,
                "semantic_abstract": abstract,
                "updated_at": now.isoformat(),
            })

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
            await self._store.update_document(document_id, {
                "pipeline_status": PipelineStatus.ABSTRACTION_IN_PROGRESS.value,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

        asyncio.create_task(
            self._reabstract_background(document_id)
        )

        now = datetime.now(timezone.utc)
        logger.info(
            "reabstract dispatched for %s at %s", document_id, now.isoformat()
        )
        return {
            "status": "reabstract_started",
            "document_id": document_id,
            "dispatched_at": now.isoformat(),
        }

    async def _reabstract_background(self, document_id: str) -> None:
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
            projection_text = "\n\n".join(
                chunk.content for chunk in chunks
            )
            abstract = await self._generate_abstract_text(projection_text)
            now = datetime.now(timezone.utc)
            async with self._locks.lock(document_id):
                await self._store.update_document(document_id, {
                    "semantic_abstract": abstract,
                    "pipeline_status": PipelineStatus.ABSTRACTION_COMPLETE.value,
                    "updated_at": now.isoformat(),
                })
        except Exception:
            logger.exception(
                "Background reabstract failed for document %s", document_id
            )
            async with self._locks.lock(document_id):
                await self._store.update_document(document_id, {
                    "pipeline_status": PipelineStatus.FAILED.value,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })

    @staticmethod
    def _build_metadata_updates(metadata: dict[str, str]) -> dict:
        """Convert caller-supplied metadata dict to document field updates.

        Known fields are mapped to document columns. Unknown fields are
        stored but have no schema-enforced semantics.
        """
        KNOWN_FIELDS = {
            "title", "version_label", "project", "doc_type",
            "authority_scope", "document_date",
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
        adapter_value = (
            adapter.value if isinstance(adapter, SourceType) else str(adapter)
        )
        return self._filename_parser.parse(source_path.stem, adapter=adapter_value)

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
        self, document_id: str, projection: ProjectionResult,
        search_preamble: str = "",
    ) -> list[Chunk]:
        """Split projection into chunks by heading.

        Prepends a search preamble to the first chunk so that document
        identity signals (title, source filename, tags) are indexed in
        both BM25 and vector search (BH-058).
        """
        chunks: list[Chunk] = []
        for i, heading in enumerate(projection.headings):
            if heading.content.strip():
                chunks.append(
                    Chunk(
                        document_id=document_id,
                        heading_path=heading.path,
                        content=heading.content,
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

        # Prepend search preamble to the first chunk for indexing (BH-058)
        if chunks and search_preamble:
            chunks[0].content = search_preamble + chunks[0].content

        return chunks

    @staticmethod
    def _build_search_preamble(doc: Document) -> str:
        """Build a search preamble from document metadata (BH-058).

        Includes title, source filename, and tags so they are indexed
        for BM25 keyword search and vector similarity.
        """
        parts: list[str] = []
        if doc.title:
            parts.append(f"Title: {doc.title}")
        # Source filename contains codes like PV07, REF, etc.
        if doc.source_path:
            filename = doc.source_path.rsplit("/", 1)[-1]
            # Strip extension for cleaner indexing
            stem = filename.rsplit(".", 1)[0] if "." in filename else filename
            parts.append(f"Source: {stem}")
        if doc.tags:
            parts.append(f"Tags: {', '.join(doc.tags)}")
        if not parts:
            return ""
        return "\n".join(parts) + "\n\n"
