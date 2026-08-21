"""Three-stage ingestion pipeline (BH-018 through BH-026, BH-068).

Stage 1 (projection): Reads source file, produces structured text.
Stage 2 (indexing): Chunks, embeds, stores in content store.
Stage 3 (abstraction): Generates semantic abstract via LLM.

All three stages run sequentially within ingest(). The method returns
after the full pipeline completes, keeping peak memory bounded to one
document at a time (BH-026, BH-068).
"""

import asyncio
import contextlib
import logging
import shutil
import tempfile
from collections.abc import Iterator
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
    GraphStore,
)
from sage.api.errors import (
    AdapterNotFoundError,
    DocumentNotFoundError,
    DuplicateContentError,
    ExpectedHeadVersionRequiresPredecessorError,
    ForceReingestPathMismatchError,
    IdenticalContentSupersedeError,
    NoProjectionError,
    ReabstractDocumentAlreadyInFlightError,
    RecomputePipelineAlreadyInFlightError,
    SourceFileNotFoundError,
    StaleChainHeadError,
    SupersedeTargetNotActiveError,
    Tier3SchemaViolationError,
    Tier3UniqueConstraintViolation,
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
from sage.services.identifier_mention_inference import infer_identifier_mentions_for_document
from sage.services.identity import generate_document_id
from sage.services.metadata import _wire_version
from sage.source_adapters.base import ProjectionResult, SourceAdapter
from sage.storage.locks import DocumentLockManager
from sage.storage.tier3_uniqueness import Tier3UniqueViolation

logger = logging.getLogger(__name__)


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


@dataclass
class _AbstractionJob:
    """A unit of abstraction work drained by the per-vault worker.

    ``projection`` is carried in memory for ingest-time and recompute jobs
    (Stage 2-3). When ``None`` the worker derives the input at processing
    time: from the document's chunks if they exist (Stage 3 only), otherwise
    by re-projecting from source (Stage 1 then Stage 2-3) -- the startup
    recovery path.
    """

    document_id: str
    projection: ProjectionResult | None
    doc_type: str | None
    attempts: int = 0


@dataclass
class _InflightClaim:
    """A per-document reservation held while abstraction work is queued or in
    flight.

    One claim serializes the three entry points (ingest, reabstract,
    recompute) plus startup recovery so a document is never abstracted twice
    concurrently. ``pending`` counts outstanding jobs so the claim is released
    only when the last one terminates; ``start_time`` feeds the 409 raised
    when an external reabstract/recompute collides with an in-flight job.
    """

    kind: str
    start_time: datetime
    pending: int = 1


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
        graph_ops_service: object | None = None,
    ) -> None:
        self._store = graph_store
        self._locks = lock_manager
        self._content_store = content_store
        self._embedding = embedding_provider
        self._abstraction = abstraction_provider
        self._config = config
        self._adapters = source_adapters or {}
        self._lifecycle_service = lifecycle_service
        # Identifier_mention inference runs inside the per-document
        # pipeline so all ingest pathways (bulk and ingest_document) honor the
        # vault's declared rules. Optional in the constructor signature so
        # legacy call sites that don't yet pass it still construct; inference
        # silently no-ops when absent.
        self._graph_ops_service = graph_ops_service

        # Unified per-document in-flight claim for the abstraction queue. One
        # claim governs ingest-time abstraction, reabstract, recompute_pipeline,
        # and startup recovery, so the same document is never abstracted twice
        # concurrently. Checked and set synchronously at enqueue; released by
        # the worker when the last queued/in-flight job for the document
        # terminates.
        self._inflight: dict[str, _InflightClaim] = {}

        # In-memory abstraction work queue drained by a single per-vault worker.
        # The queue is created lazily on first enqueue (so it binds to the
        # running loop) and is NOT itself durable: durability comes from the
        # graph store's pipeline_status, which the worker re-derives pending
        # work from on startup (recover_incomplete_documents). The single
        # long-lived worker task is the strong reference that closes the GC
        # "task disappears mid-execution" silent-loss window.
        self._abstraction_queue: asyncio.Queue[_AbstractionJob] | None = None
        self._worker_task: asyncio.Task | None = None

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

    # ------------------------------------------------------------------
    # Persistent off-loop abstraction queue
    # ------------------------------------------------------------------

    def _try_claim(
        self, document_id: str, kind: str, *, allow_join: bool = False
    ) -> _InflightClaim | None:
        """Synchronously reserve abstraction work for a document.

        Returns ``None`` when the caller now holds (or has joined) the claim
        and may enqueue. Returns the EXISTING claim when one is already held
        and ``allow_join`` is False, so the caller can reject (the external
        reabstract/recompute 409). ``allow_join`` is for the ingest path,
        whose freshly-inserted document id effectively never collides but must
        never raise on the rare force-reingest race -- it joins the existing
        claim instead, and the serial worker keeps the two jobs from
        overlapping. Check-and-set runs in one scheduling slice (no await), so
        two callers cannot both pass.
        """
        existing = self._inflight.get(document_id)
        if existing is not None:
            if allow_join:
                existing.pending += 1
                return None
            return existing
        self._inflight[document_id] = _InflightClaim(
            kind=kind, start_time=datetime.now(timezone.utc)
        )
        return None

    def _release_claim(self, document_id: str) -> None:
        """Decrement a document's in-flight claim, dropping it when the last
        queued/in-flight job terminates."""
        claim = self._inflight.get(document_id)
        if claim is None:
            return
        claim.pending -= 1
        if claim.pending <= 0:
            del self._inflight[document_id]

    def _enqueue_abstraction_job(self, job: _AbstractionJob) -> None:
        """Put a job on the queue and ensure the single worker is draining.

        The caller is responsible for having claimed ``job.document_id`` first
        (so an external reabstract/recompute is rejected while the job pends).
        """
        queue = self._ensure_worker_running()
        queue.put_nowait(job)

    def _ensure_worker_running(self) -> "asyncio.Queue[_AbstractionJob]":
        """Lazily create the queue and start the single drain worker, bound to
        the running event loop. Idempotent; restarts the worker if a prior one
        was stopped (stop_worker) or died. Returns the queue."""
        if self._abstraction_queue is None:
            self._abstraction_queue = asyncio.Queue()
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._abstraction_worker())
        return self._abstraction_queue

    async def _abstraction_worker(self) -> None:
        """Drain the abstraction queue one job at a time.

        A single worker per vault, combined with the process-wide generation
        lock in the abstraction provider, keeps inference one-at-a-time and
        within the RAM budget. The worker is the durable strong reference to
        in-flight work; queued jobs are plain data, immune to the GC
        "task disappears mid-execution" class.
        """
        queue = self._abstraction_queue
        if queue is None:
            return
        while True:
            job = await queue.get()
            try:
                await self._process_abstraction_job(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "abstraction worker: unhandled error for document %s",
                    job.document_id,
                )
            finally:
                queue.task_done()

    async def _process_abstraction_job(self, job: _AbstractionJob) -> None:
        """Run one job with bounded retry.

        On terminal failure (after ``max_attempts``) stamp a structured
        ``pipeline_error`` rather than stranding the document. The claim is
        released when the job terminates, whether it succeeded or exhausted
        its attempts.
        """
        document_id = job.document_id
        max_attempts = self._config.abstraction.max_attempts
        last_exc: Exception | None = None
        try:
            for attempt in range(1, max_attempts + 1):
                try:
                    await self._run_job_work(job)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last_exc = exc
                    logger.warning(
                        "abstraction attempt %d/%d failed for %s: %s",
                        attempt,
                        max_attempts,
                        document_id,
                        exc,
                    )
                    if attempt < max_attempts:
                        await self._sleep_for_backoff(self._compute_backoff(attempt))
            await self._stamp_abstraction_failed(document_id, max_attempts, last_exc)
        finally:
            self._release_claim(document_id)

    async def _run_job_work(self, job: _AbstractionJob) -> None:
        """Execute a single job attempt, dispatching by available input:
        in-memory projection (Stage 2-3), existing chunks (Stage 3 only), or
        re-projection from source (Stage 1 then Stage 2-3)."""
        document_id = job.document_id
        if job.projection is not None:
            await self._execute_pipeline_stages(document_id, job.projection, job.doc_type)
        elif await self._content_store.has_chunks(document_id):
            await self._execute_abstract_from_chunks(document_id, job.doc_type)
        else:
            projection = await self._reproject_from_source(document_id)
            await self._execute_pipeline_stages(document_id, projection, job.doc_type)

    def _compute_backoff(self, attempt: int) -> float:
        """Exponential backoff with a configured cap: the delay before retry
        ``attempt`` (1-indexed) is min(base * 2**(attempt-1), max)."""
        cfg = self._config.abstraction
        return min(
            cfg.retry_backoff_base_seconds * (2 ** (attempt - 1)),
            cfg.retry_backoff_max_seconds,
        )

    async def _sleep_for_backoff(self, seconds: float) -> None:
        """Indirection over asyncio.sleep so tests can observe or skip backoff."""
        await asyncio.sleep(seconds)

    async def _stamp_abstraction_failed(
        self, document_id: str, attempts: int, exc: Exception | None
    ) -> None:
        """Record a terminal, structured failure surfaced via get_document."""
        detail = f"{type(exc).__name__}: {exc}" if exc is not None else "unknown error"
        message = f"abstraction failed after {attempts} attempts; last error: {detail}"
        async with self._locks.lock(document_id):
            await self._store.update_document(
                document_id,
                {
                    "pipeline_status": PipelineStatus.FAILED.value,
                    "pipeline_error": message,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )

    async def stop_worker(self) -> None:
        """Cancel and await the drain worker. Safe to call when no worker is
        running (idempotent). Pending queued jobs are dropped; they are
        re-derived from pipeline_status on the next recover_incomplete_documents
        call. Wired into the lifespan teardown and registry reload."""
        task = self._worker_task
        self._worker_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def recover_incomplete_documents(self) -> int:
        """Re-derive pending abstraction work from pipeline_status and enqueue it.

        Called at process startup (the standalone MCP and FastAPI lifespans)
        so a document left non-terminal by a crash or a stopped worker is
        recovered rather than stranded. Terminal states (abstraction_complete,
        abstraction_skipped, failed) are left alone: skip and failed are
        deliberate operator territory. Returns the number of documents enqueued.
        """
        non_terminal = {
            PipelineStatus.PROJECTION_COMPLETE.value,
            PipelineStatus.INDEXING_IN_PROGRESS.value,
            PipelineStatus.INDEXING_COMPLETE.value,
            PipelineStatus.ABSTRACTION_IN_PROGRESS.value,
        }
        documents = await self._store.list_all_documents()
        enqueued = 0
        for doc in documents:
            if doc.pipeline_status not in non_terminal:
                continue
            # Skip documents a live operation already claimed.
            if self._try_claim(doc.id, "recovery") is not None:
                continue
            self._enqueue_abstraction_job(
                _AbstractionJob(document_id=doc.id, projection=None, doc_type=doc.doc_type)
            )
            enqueued += 1
        return enqueued

    def _merge_adapter_config(
        self, source_type: SourceType, request_config: dict | None
    ) -> dict | None:
        """Return adapter config with vault-level base merged with per-request override.

        Looks up the vault's ``adapter_defaults`` entry for ``source_type``.
        If present, it is the base; ``request_config`` overrides on
        collision via recursive merge (request keys override vault keys;
        nested dicts merge key-by-key, e.g. ``heading_style_map``
        accumulates entries from both sources). Returns ``None`` when both
        are absent so adapters that branch on ``config is None`` keep their
        legacy fast path.

        Vault defaults are the only source of adapter parameters on the
        re-projection paths (``recompute_pipeline`` and reindex), which
        carry no per-request config to fall back on.
        """
        defaults = self._config.adapter_defaults
        vault_config: dict | None = None
        if isinstance(defaults, dict):
            entry = defaults.get(source_type.value)
            if isinstance(entry, dict):
                vault_config = entry
        if vault_config is None and request_config is None:
            return None
        if vault_config is None:
            return dict(request_config) if request_config else None
        if not request_config:
            return dict(vault_config)
        return _deep_merge_dicts(vault_config, request_config)

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
        MCP `ingest_document` tool to stay under the 60-second MCP client
        timeout on documents whose abstraction latency would otherwise
        exceed it. The supersede transition runs synchronously
        regardless of this flag (BH-129): the version chain must be
        complete when the call returns.

        Trio-field inheritance on supersede (CAS-ADR-021):
        When ``request.predecessor_id`` is set and the caller omits any
        of ``doc_type``, ``project``, or ``authority_scope`` from
        ``request.metadata``, the omitted fields silently inherit from
        the predecessor's value (when non-None). A caller who wants to
        change one of these trio fields on a supersede must pass the
        new value explicitly in ``metadata``; otherwise the
        predecessor's value carries forward without comment. No error
        is raised either way — inheritance is the documented default,
        override is the opt-in.

        Tier3 uniqueness (CAS-ADR-031):
        Doc types declaring a ``unique`` constraint in their
        ``metadata_schema`` (see
        ``document_types.doc_types[].metadata_schema`` in vault config)
        enforce per-vault uniqueness on the named tier3 field at ingest
        time. Uniqueness is checked in the same database transaction as
        the row insert, so a collision leaves the existing document
        undisturbed and raises ``Tier3UniqueConstraintViolation`` with
        ``doc_type``, ``field``, ``colliding_value``, and
        ``existing_document_id`` in the detail envelope. The check is
        independent of content-hash deduplication: ``request.force=True``
        does NOT override a tier3 uniqueness violation.

        Adapter-specific config validation:
        Each ``SourceType`` adapter declares its own required-config
        schema; ``request.config`` is adapter-specific and validated
        against that schema during projection. Per-adapter required
        keys live in vault config under ``adapter_defaults``.

        ``pipeline_status`` terminal-state outcomes (CAS-ADR-021):
        The terminal status observed by a poll of ``get_document``
        depends on vault config and runtime outcome.
        ``PipelineStatus.abstraction_complete`` is the happy path:
        projection + indexing + abstraction all succeed and
        ``metadata_confirmed=True`` is set per caller-authoritative
        semantics. ``PipelineStatus.abstraction_skipped`` is the
        deferred-abstraction branch: the vault has
        ``abstraction.enabled=False`` in its config (or the projection
        produced empty text), so Stages 1-2 ran but Stage 3 was
        bypassed. ``PipelineStatus.failed`` is the catch-all for any
        Stage-1/2/3 exception; the document persists with
        ``pipeline_error`` populated. Inspect ``abstraction.enabled``
        in vault config to know which terminal state to expect.

        Returns:
            IngestResult with the document and whether it was newly created.
            When `wait_for_pipeline` is False the document's
            pipeline_status will typically be non-terminal (indexing_in_progress
            or projection_complete); callers poll `get_document` for terminal
            state.

        Raises:
            DuplicateContentError: Same content hash exists anywhere in vault
                (BH-018, BH-066), unless force flag set.
            ForceReingestPathMismatchError: ``force=True`` and the content-hash
                match resolves to a document stored at a different
                ``source_path`` than the incoming file, without a ``document_id``
                confirming the intended target. Guards against silently
                overwriting an unrelated byte-identical document.
            AdapterNotFoundError: No adapter for requested source type.
            DocumentNotFoundError: `predecessor_id` does not exist.
            SupersedeTargetNotActiveError: predecessor is not active.
            IdenticalContentSupersedeError: new content matches predecessor.
            SourceFileNotFoundError: ``request.source`` does not resolve to
                a readable source on the vault-source store -- neither on the
                local tree nor in the backing store.
            Tier3UniqueConstraintViolation: ``tier3_metadata`` carried a
                value already in use on a doc_type with a ``unique``
                constraint (CAS-ADR-031). ``force=True`` does
                not override.
            Tier3SchemaViolationError: ``request.tier3_metadata`` failed
                validation against the resolved doc_type's
                ``metadata_schema``, or the doc_type has no
                ``metadata_schema`` declared and a non-empty payload was
                supplied.
        """
        adapter = self._adapters.get(request.source_type)
        if adapter is None:
            raise AdapterNotFoundError(request.source_type)

        # CAS-ADR-038 Primitive C: expected_head_version is bound to the
        # chain head identified by predecessor_id. Without a predecessor
        # the token has no defined meaning; reject the caller bug loudly
        # rather than silently dropping the parameter.
        if request.expected_head_version is not None and request.predecessor_id is None:
            raise ExpectedHeadVersionRequiresPredecessorError()

        # Pre-validate the supersede predecessor BEFORE running projection
        # (BH-121, BH-122, BH-124). Fail-fast keeps pipeline work behind
        # cheap validity checks. The identical-content check happens
        # post-projection, once the new file's hash is known.
        predecessor: Document | None = None
        if request.predecessor_id:
            predecessor = await self._store.get_document(request.predecessor_id)
            if predecessor is None:
                raise DocumentNotFoundError(request.predecessor_id)
            if predecessor.lifecycle_status != "active":
                raise SupersedeTargetNotActiveError(
                    request.predecessor_id,
                    predecessor.lifecycle_status,
                )

        # Resolve the source through the vault-source store, not a raw local
        # Path.exists() gate (CAS-ADR-043). Under a non-filesystem binding the
        # retained bytes live in the backing store and are never mirrored on
        # this process's local tree, so a relative source that names an
        # already-retained document must resolve through the port even when
        # nothing sits at storage_root/<source> on local disk.
        storage_root = Path(self._config.vault.storage_root).expanduser().resolve()
        source_input = Path(request.source)

        from sage.mcp_init import get_stack_config, resolve_stack_vault_source_store

        vault_source_store = resolve_stack_vault_source_store(get_stack_config())

        if source_input.is_absolute():
            # External file import: copy the caller's file into the vault,
            # retaining it on the active profile's store (BH-053 through
            # BH-057) so the copy is binding-agnostic.
            if not source_input.exists():
                raise SourceFileNotFoundError(request.source)
            source_path = source_input.resolve()
            vault_relative = vault_source_store.retain_source(
                self._config.vault.id, storage_root, source_path
            )
        else:
            # Relative source: a vault-relative path into the store. When the
            # bytes are present on the local tree, retain in place / upload as
            # usual; otherwise resolve presence through the port so a
            # backend-resident source (the post-restart cloud condition) is
            # projected rather than rejected as missing.
            source_path = storage_root / request.source
            if source_path.exists():
                vault_relative = vault_source_store.retain_source(
                    self._config.vault.id, storage_root, source_path.resolve()
                )
            elif vault_source_store.source_exists(
                self._config.vault.id, storage_root, request.source
            ):
                vault_relative = request.source
            else:
                raise SourceFileNotFoundError(request.source)

        # Stage 1: Projection (synchronous). Merge vault-level adapter config
        # with the per-request config; per-request keys override vault keys
        # on collision. The vault's adapter_defaults entry is the authority
        # for adapter behavior across all ingests; the request override is a
        # per-call escape hatch.
        merged_config = self._merge_adapter_config(request.source_type, request.config)
        with self._project_source(vault_source_store, storage_root, vault_relative) as project_path:
            projection = await adapter.project(project_path, merged_config)

        # Parse filename per vault config (CAS-ADR-015) only when the caller
        # opts in to review (CAS-ADR-021). Default ingests are caller-
        # authoritative: filename inference does not run and the adapter's
        # ProjectionResult.title remains the title fallback. When
        # needs_review is True, current filename-inference behavior is
        # preserved end-to-end.
        parsed = (
            self._parse_source_filename(source_path, request.source_type)
            if request.needs_review
            else None
        )

        # Resolve tier3_metadata with caller-precedence over adapter
        # extraction (CAS-ADR-021). The markdown adapter may surface
        # filename-derived tier3 fields (e.g. ``adr_id`` from
        # ``cas-adr-NNN_*``) via ``ProjectionResult.metadata
        # ["adapter_tier3_metadata"]``; caller-supplied
        # ``request.tier3_metadata`` wins on conflict.
        adapter_tier3 = projection.metadata.get("adapter_tier3_metadata") or None
        final_tier3 = (
            request.tier3_metadata if request.tier3_metadata is not None else adapter_tier3
        )

        # Validate the resolved tier3_metadata against the resolved
        # doc_type's metadata_schema. Done before any side effects (the
        # adapter projection runs above but is read-only on disk). Resolution
        # mirrors the precedence chain applied below by update_document
        # calls: caller > filename parse > predecessor inheritance > "misc".
        # A None validator (doc_type has no metadata_schema declared) is a
        # hard 400 per the strict no-loose-mode decision.
        if final_tier3 is not None:
            resolved_dt = self._resolve_doc_type_for_tier3(
                request=request, parsed=parsed, predecessor=predecessor
            )
            self._validate_tier3_payload(resolved_dt, final_tier3)

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
        # before crossing the typed-alias boundary. Adapters emit
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

        # Branch detection: force-reingest reuses an existing record;
        # otherwise this is a new-doc insert (with or without predecessor).
        # `existing_doc is not None` is the canonical narrowing predicate
        # for the force-reingest branch from this point forward.
        existing_doc: Document | None = None
        if hash_matches and request.force:
            match_ids = set(hash_matches.values())
            # Honor an explicit pin when it names one of the colliding
            # records; otherwise fall back to the (normally singular) hash
            # match. A vault without this bug's residue has at most one match.
            if request.document_id is not None and request.document_id in match_ids:
                existing_id = request.document_id
            else:
                existing_id = next(iter(match_ids))
            existing_doc = await self._store.get_document(existing_id)
            # Cross-document collision guard: force-reingest keys its target by
            # content hash alone, so a hash match stored at a *different* path
            # may be an unrelated document that merely shares content bytes.
            # Overwriting it would silently discard that document's identity
            # (source_path, and title when filename inference contributed it).
            # Refuse unless the caller confirmed this record via document_id.
            # Same-path re-ingest (BH-019) never trips this. An existing_doc of
            # None (rare race) falls through to the new-document branch below.
            if (
                existing_doc is not None
                and existing_doc.source_path != vault_relative
                and request.document_id != existing_doc.id
            ):
                raise ForceReingestPathMismatchError(
                    resolved_id=existing_doc.id,
                    resolved_source_path=existing_doc.source_path,
                    incoming_source_path=vault_relative,
                    content_hash=canonical_hash,
                )

        # Compute the merged metadata in memory before any insert/update
        # touches the store. Closes the partial-metadata window
        # that existed when these layers ran as separate post-insert
        # update_document calls. The baseline differs by branch: empty
        # for new-doc, the existing record's fields for force-reingest.
        baseline: dict = existing_doc.model_dump() if existing_doc is not None else {}
        adapter_tags = list(projection.metadata.get("adapter_tags") or [])
        adapter_tag_prefixes = list(projection.metadata.get("adapter_tag_prefixes") or [])
        field_updates = self._compute_metadata_field_updates(
            baseline=baseline,
            parsed=parsed,
            caller_metadata=request.metadata,
            predecessor=predecessor,
            adapter_tags=adapter_tags,
            adapter_tag_prefixes=adapter_tag_prefixes,
            needs_review=request.needs_review,
            source_modified_at=source_modified_at,
            vault_timezone=self._config.vault.timezone,
        )

        if existing_doc is not None:
            # Force re-ingestion: reuse existing record (BH-019, BH-067).
            # The pre-merged field_updates carry the full metadata into
            # this single update_document call.
            updates: dict[str, object] = {
                "pipeline_status": PipelineStatus.PROJECTION_COMPLETE.value,
                "pipeline_error": None,
                "projected_at": now.isoformat(),
                "adapter_version": projection.adapter_version,
                "updated_at": now.isoformat(),
                "semantic_abstract": None,
                "indexed_at": None,
                "source_modified_at": source_modified_at_str,
            }
            updates.update(field_updates)
            # Tier3 metadata override (caller authority on force-reingest,
            # CAS-ADR-021). Pre-validated above against the resolved
            # doc_type's schema; storage replacement is top-level.
            # Resolved tier3 may come from caller or from the adapter's
            # filename extraction; caller wins per the precedence above.
            if final_tier3 is not None:
                updates["tier3_metadata"] = final_tier3
            # Title reaffirmation (force branch): when filename parse
            # contributed, refresh title from the freshly-resolved value
            # in case the filename changed between ingestions.
            if parsed is not None and resolved_title:
                updates["title"] = resolved_title
            # Update source_path if the file moved (BH-067). A different path
            # here is reached only when the caller confirmed this record via
            # document_id; the cross-document collision guard above rejects an
            # unconfirmed different-path hash match before this point.
            if vault_relative != existing_doc.source_path:
                updates["source_path"] = vault_relative
            doc = await self._store.update_document(existing_doc.id, updates)
            is_new = False

            # Remove old content store entries for re-indexing
            await self._content_store.remove_document(existing_doc.id)

            # Force-reingest with a supersede target: apply the supersede
            # transition on the predecessor. The doc record was reused
            # (no new insert) so atomicity collapses to lifecycle._set_lifecycle's
            # own atomic primitive (BH-135).
            #
            # CAS-ADR-038 Primitive C coverage gap: when force=True drives
            # this branch, `request.expected_head_version` is currently
            # ignored. The transition runs through
            # `LifecycleService._set_lifecycle`, which acquires its own
            # per-predecessor lock; layering the version check here without
            # deadlocking would require threading the token through
            # `SetLifecycleRequest` so the check runs inside that lock.
            # The new-document branch below covers the primary supersede
            # path; force-reingest is a rare back door whose caller has
            # already opted into duplicate-content bypass, and the
            # incremental safety cost is acceptable until a caller needs
            # the contract here.
            if predecessor is not None and self._lifecycle_service is not None:
                await self._lifecycle_service._set_lifecycle(
                    predecessor.id,
                    SetLifecycleRequest(action="supersede", successor_id=doc.id),
                )
        else:
            # New document. When a predecessor is being superseded the
            # doc insert + predecessor lifecycle flip + supersedes edge
            # commit as a single database transaction (BH-136). This
            # eliminates the orphan class where a successor record exists
            # but the predecessor was never archived. Without a
            # predecessor it is a single-row insert. The pre-merged
            # field_updates carry the full metadata into the atomic
            # insert.
            created_by = request.created_by or self._config.vault.owner
            doc_id = generate_document_id(vault_relative, now.isoformat(), resolved_title)
            base = dict(
                id=doc_id,
                title=resolved_title,
                source_type=request.source_type,
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
                tier3_metadata=final_tier3,
            )
            doc = Document(**{**base, **field_updates})
            if predecessor is not None and self._lifecycle_service is not None:
                # CAS-ADR-038 Primitive C: serialize same-predecessor
                # supersedes under a per-predecessor lock so the fresh
                # re-read + expected_head_version check + atomic insert
                # form one critical section. Without the lock, two
                # parallel supersedes could each pass the pre-validation
                # and both reach insert_with_supersede_atomic on the
                # still-active predecessor, forking the supersedes chain
                # into a tree and violating CAS-ADR-023's linear-chain
                # invariant.
                async with self._locks.lock(predecessor.id):
                    # Re-read the predecessor inside the lock to catch a
                    # concurrent archive (or version bump) that happened
                    # after pre-validation. prepare_supersede consults
                    # the transition table without writing.
                    fresh_pred = await self._store.get_document(predecessor.id)
                    if fresh_pred is None:
                        raise DocumentNotFoundError(predecessor.id)
                    # Optimistic-concurrency check (Primitive C). Only
                    # fires when the caller opts in; omission preserves
                    # the pre-Primitive-C contract (with the side-benefit
                    # that the lock above still prevents silent forks).
                    # Version source is the predecessor's updated_at in
                    # canonical wire form, matching what callers see via
                    # get_document.
                    if request.expected_head_version is not None:
                        current_head_version = _wire_version(fresh_pred.updated_at)
                        if current_head_version != request.expected_head_version:
                            raise StaleChainHeadError(
                                predecessor_id=fresh_pred.id,
                                expected_head_version=request.expected_head_version,
                                current_head_id=fresh_pred.id,
                                current_head_version=current_head_version,
                            )
                    transition = self._lifecycle_service.prepare_supersede(fresh_pred, doc.id)
                    try:
                        doc, _updated_pred = await self._store.insert_with_supersede_atomic(
                            doc,
                            fresh_pred.id,
                            transition.predecessor_updates,
                            transition.edge,
                        )
                    except Tier3UniqueViolation as exc:
                        raise Tier3UniqueConstraintViolation(
                            doc_type=exc.doc_type,
                            field=exc.field,
                            colliding_value=exc.colliding_value,
                            existing_document_id=exc.existing_document_id,
                        ) from exc
                    # Sync the predecessor's new lifecycle_status to its
                    # chunks. insert_with_supersede_atomic commits the flip
                    # directly in SQL (BH-136 atomicity), bypassing
                    # LifecycleService._set_lifecycle's chunk-sync hook.
                    # pre-filter pushdown requires the chunk-level
                    # lifecycle_status column to stay aligned with the
                    # document's current state.
                    new_pred_lifecycle = transition.predecessor_updates.get("lifecycle_status")
                    if new_pred_lifecycle is not None:
                        await self._content_store.update_chunk_metadata(
                            fresh_pred.id, {"lifecycle_status": new_pred_lifecycle}
                        )
            else:
                try:
                    await self._store.insert_document(doc)
                except Tier3UniqueViolation as exc:
                    raise Tier3UniqueConstraintViolation(
                        doc_type=exc.doc_type,
                        field=exc.field,
                        colliding_value=exc.colliding_value,
                        existing_document_id=exc.existing_document_id,
                    ) from exc
            is_new = True

        # The supersede lifecycle transition was bundled into the same
        # database transaction as the new-document insert above (BH-129,
        # BH-136). The version chain is therefore complete here for both
        # the new-document and force-reingest branches.

        # Stages 2-3 (indexing, abstraction) run sync or async per the
        # caller's wait_for_pipeline choice (BH-026, BH-130). Sequential
        # execution (wait_for_pipeline=True) caps peak memory at one
        # document's embeddings and abstraction context -- the bulk ingest
        # path -- and keeps fail-fast semantics. Fire-and-forget
        # (wait_for_pipeline=False) hands Stage 2-3 to the abstraction queue:
        # a single worker drains it one job at a time with bounded retry, and
        # durability comes from the graph store's pipeline_status, re-derived
        # on startup by recover_incomplete_documents.
        if wait_for_pipeline:
            await self._run_background_pipeline(doc.id, projection, doc.doc_type)
            doc = await self._store.get_document(doc.id) or doc
        else:
            # The freshly-inserted document id effectively never collides, but
            # allow_join keeps the rare force-reingest race from raising; the
            # serial worker prevents overlap.
            self._try_claim(doc.id, "ingest", allow_join=True)
            self._enqueue_abstraction_job(
                _AbstractionJob(document_id=doc.id, projection=projection, doc_type=doc.doc_type)
            )

        return IngestResult(document=doc, is_new=is_new)

    async def _run_background_pipeline(
        self,
        document_id: str,
        projection: ProjectionResult,
        doc_type: str | None,
    ) -> None:
        """Run Stages 2-3 inline for the wait_for_pipeline=True path.

        Fail-fast: a stage failure stamps pipeline_status=failed with no
        retry. Queued work runs the same stages through the worker
        (``_process_abstraction_job``), which layers bounded retry on top of
        ``_execute_pipeline_stages``.
        """
        try:
            await self._execute_pipeline_stages(document_id, projection, doc_type)
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

    async def _execute_pipeline_stages(
        self,
        document_id: str,
        projection: ProjectionResult,
        doc_type: str | None,
    ) -> None:
        """Stage 2 (indexing) then Stage 3 (abstraction), raising on failure.

        The raising contract lets the queue worker own the terminal status: it
        retries before stamping FAILED. The inline wrapper
        (``_run_background_pipeline``) catches and stamps FAILED itself.
        Abstraction is skipped (terminal ``abstraction_skipped``) when the
        vault disables it or the projection text is empty (BH-025, BH-134).
        """
        await self._stage2_indexing(document_id, projection)

        # Run vault-declared identifier_mention inference after Stage 2
        # (chunks materialized in the content store) and before Stage 3
        # (abstraction). Placement here -- rather than after abstraction --
        # ensures inferred edges appear even when abstraction is disabled or
        # skipped on empty text.
        await self._infer_identifier_mention_edges(document_id)

        # BH-025: abstraction disabled -> abstraction_skipped.
        if not self._config.abstraction.enabled:
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
        # abstraction provider's strict-quality edge guard. Other projection
        # surfaces -- style inventory, tags, metadata -- are already persisted
        # by Stage 2.
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

    async def _infer_identifier_mention_edges(self, document_id: str) -> None:
        """Run identifier_mention inference for the just-indexed doc.

        Reads chunks from the content store (Stage 2 must have completed),
        invokes ``infer_identifier_mentions_for_document``, and lets it
        write Tier-1 ``references`` edges directly via the graph_ops
        service. Errors here are swallowed -- inference is an enrichment,
        not a precondition for terminal pipeline status.
        """
        if self._graph_ops_service is None:
            return
        edge_inference_cfg = getattr(self._config, "edge_inference", None)
        if not edge_inference_cfg:
            return
        try:
            chunks = await self._content_store.get_all_chunks(document_id)
            body_text = "\n".join(c.content for c in chunks)
            if not body_text:
                return
            await infer_identifier_mentions_for_document(
                source_doc_id=document_id,
                body_text=body_text,
                edge_inference_config=edge_inference_cfg,
                graph_store=self._store,
                graph_ops_service=self._graph_ops_service,
            )
        except Exception:
            logger.exception(
                "identifier_mention inference failed for %s; "
                "pipeline continues without inferred edges",
                document_id,
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
        # live in a standalone synthetic header chunk (F9) — not
        # inlined into chunk[0].
        doc = await self._store.get_document(document_id)
        body_chunks = self._chunk_projection(document_id, projection)

        # Stamp document-level scalars on body chunks for content-store
        # pre-filtering. doc_type, lifecycle_status, and project
        # are all stable per-document fields whose values must
        # ride along with each chunk row so the content store can
        # pre-filter at top-K time without a graph-store round trip.
        if doc and body_chunks:
            for chunk in body_chunks:
                if doc.doc_type:
                    chunk.doc_type = doc.doc_type
                chunk.lifecycle_status = doc.lifecycle_status
                chunk.project = doc.project

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
        """Rebuild the synthetic header chunk after metadata changes.

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
        # ``semantic_abstract``. Body chunks are not touched.
        await self._refresh_header_chunk(document_id)

    async def reabstract(self, document_id: str) -> dict:
        """Re-run abstraction on an existing document via the abstraction queue.

        Validates the document and its chunks synchronously, claims the
        document (raising ReabstractDocumentAlreadyInFlightError if a job is
        already queued or in flight for it), pre-marks ABSTRACTION_IN_PROGRESS,
        and enqueues a Stage-3-from-chunks job. Returns immediately; the caller
        polls get_document for the transition to ABSTRACTION_COMPLETE or FAILED.

        Args:
            document_id: ID of the document to re-abstract.

        Returns:
            Dict with status='reabstract_started' and document_id.

        Raises:
            DocumentNotFoundError: Document does not exist.
            NoProjectionError: Document has no stored projection chunks.
            ReabstractDocumentAlreadyInFlightError: Abstraction work is already
                queued or in flight for this document_id.
        """
        doc = await self._store.get_document(document_id)
        if doc is None:
            raise DocumentNotFoundError(document_id)

        if not await self._content_store.has_chunks(document_id):
            raise NoProjectionError(document_id)

        existing = self._try_claim(document_id, "reabstract")
        if existing is not None:
            raise ReabstractDocumentAlreadyInFlightError(document_id, existing.start_time)

        # Pre-mark in-progress before enqueue. If the status write fails, drop
        # the claim so future calls can proceed (otherwise the orphan claim
        # would permanently 409 every subsequent reabstract against this doc).
        try:
            async with self._locks.lock(document_id):
                await self._store.update_document(
                    document_id,
                    {
                        "pipeline_status": PipelineStatus.ABSTRACTION_IN_PROGRESS.value,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
        except Exception:
            self._release_claim(document_id)
            raise

        self._enqueue_abstraction_job(
            _AbstractionJob(document_id=document_id, projection=None, doc_type=doc.doc_type)
        )

        now = datetime.now(timezone.utc)
        logger.info("reabstract enqueued for %s at %s", document_id, now.isoformat())
        return {
            "status": "reabstract_started",
            "document_id": document_id,
            "dispatched_at": now.isoformat(),
        }

    async def _execute_abstract_from_chunks(self, document_id: str, doc_type: str | None) -> None:
        """Stage 3 from stored chunks, raising on failure.

        Reconstructs the projection text from body chunks (excluding the
        synthetic header so its title/source/tags restatement does not feed
        the abstraction prompt), regenerates the abstract, and refreshes the
        header chunk for retrieval. Used by the worker for reabstract jobs and
        for startup recovery of documents that still have chunks.
        """
        chunks = await self._content_store.get_all_chunks(document_id)
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

        # Refresh the synthetic header chunk so the new abstract is indexed.
        await self._refresh_header_chunk(document_id)

    async def recompute_pipeline(self, document_id: str) -> dict:
        """Re-run Stages 1-3 against an existing document via the abstraction queue.

        Operator-facing repair for a document stuck non-terminal with stale or
        missing chunks. Re-projects from the document's ``source_path``
        synchronously (so adapter/source errors surface in the caller's
        response envelope rather than as a FAILED stamp), claims the document,
        wipes stale chunks, and enqueues a Stage 2-3 job onto the worker.

        Per-document single-flight: a second concurrent caller against the same
        ``document_id`` raises ``RecomputePipelineAlreadyInFlightError`` rather
        than queueing.

        Args:
            document_id: ID of the document to re-run.

        Returns:
            Dict with status='recompute_pipeline_started', document_id,
            and dispatched_at.

        Raises:
            DocumentNotFoundError: Document does not exist.
            AdapterNotFoundError: No adapter registered for the document's
                source_type.
            SourceFileNotFoundError: Source file resolved from
                ``document.source_path`` does not exist on disk.
            RecomputePipelineAlreadyInFlightError: Abstraction work is already
                queued or in flight for this document_id.
        """
        doc = await self._store.get_document(document_id)
        if doc is None:
            raise DocumentNotFoundError(document_id)

        adapter = self._adapters.get(doc.source_type)
        if adapter is None:
            raise AdapterNotFoundError(doc.source_type)

        if doc.source_path is None:
            raise SourceFileNotFoundError("(none)")
        storage_root = Path(self._config.vault.storage_root).expanduser().resolve()
        from sage.mcp_init import get_stack_config, resolve_stack_vault_source_store

        vault_source_store = resolve_stack_vault_source_store(get_stack_config())

        existing = self._try_claim(document_id, "recompute")
        if existing is not None:
            raise RecomputePipelineAlreadyInFlightError(document_id, existing.start_time)

        # Stage 1 (projection) runs synchronously so adapter / source errors
        # surface in the caller's response envelope rather than as a FAILED
        # stamp on the document. Drop the claim on synchronous failure so the
        # operator can retry without restarting the service. The source is read
        # through the active vault-source binding so re-projection works under a
        # non-filesystem store after a restart (CAS-ADR-043).
        start_time = datetime.now(timezone.utc)
        try:
            merged_config = self._merge_adapter_config(doc.source_type, None)
            with self._project_source(
                vault_source_store, storage_root, doc.source_path
            ) as project_path:
                projection = await adapter.project(project_path, merged_config)
            await self._store.update_document(
                document_id,
                {
                    "pipeline_status": PipelineStatus.PROJECTION_COMPLETE.value,
                    "pipeline_error": None,
                    "projected_at": start_time.isoformat(),
                    "updated_at": start_time.isoformat(),
                },
            )
        except Exception:
            self._release_claim(document_id)
            raise

        # Wipe stale chunks so Stage 2 re-indexes from scratch. Idempotent on
        # an empty store.
        await self._content_store.remove_document(document_id)

        self._enqueue_abstraction_job(
            _AbstractionJob(document_id=document_id, projection=projection, doc_type=doc.doc_type)
        )

        logger.info("recompute_pipeline enqueued for %s at %s", document_id, start_time.isoformat())
        return {
            "status": "recompute_pipeline_started",
            "document_id": document_id,
            "dispatched_at": start_time.isoformat(),
        }

    async def _reproject_from_source(self, document_id: str) -> ProjectionResult:
        """Stage 1 for the startup-recovery path: re-project a document from
        its source file and wipe stale chunks.

        Used by the worker for recovery jobs whose document has no chunks (e.g.
        stranded at projection_complete). Raises if the document, adapter, or
        source file is missing -- the worker treats that as an attempt failure
        and ultimately stamps FAILED.
        """
        doc = await self._store.get_document(document_id)
        if doc is None:
            raise DocumentNotFoundError(document_id)
        adapter = self._adapters.get(doc.source_type)
        if adapter is None:
            raise AdapterNotFoundError(doc.source_type)
        if doc.source_path is None:
            raise SourceFileNotFoundError("(none)")
        storage_root = Path(self._config.vault.storage_root).expanduser().resolve()
        from sage.mcp_init import get_stack_config, resolve_stack_vault_source_store

        vault_source_store = resolve_stack_vault_source_store(get_stack_config())
        merged_config = self._merge_adapter_config(doc.source_type, None)
        with self._project_source(
            vault_source_store, storage_root, doc.source_path
        ) as project_path:
            projection = await adapter.project(project_path, merged_config)
        now = datetime.now(timezone.utc)
        await self._store.update_document(
            document_id,
            {
                "pipeline_status": PipelineStatus.PROJECTION_COMPLETE.value,
                "pipeline_error": None,
                "projected_at": now.isoformat(),
                "updated_at": now.isoformat(),
            },
        )
        await self._content_store.remove_document(document_id)
        return projection

    @contextlib.contextmanager
    def _project_source(self, store, storage_root: Path, source_path: str) -> Iterator[Path]:
        """Yield a local filesystem path to project a retained source from.

        A retained source the active binding keeps on the local tree (the
        filesystem binding, or a same-request local copy) is projected in place.
        Under a non-filesystem binding -- where no local copy survives a restart
        -- the bytes are pulled back through the vault-source port and staged to a
        temporary file carrying the source's original basename, so the adapter's
        filename-derived title and tier3 extraction are unchanged. Routing
        projection through the port keeps projection and chunk repair working
        under the document-store binding (CAS-ADR-043).
        """
        local = storage_root / source_path
        if local.exists():
            yield local
            return
        vault_id = self._config.vault.id
        if not store.source_exists(vault_id, storage_root, source_path):
            raise SourceFileNotFoundError(source_path)
        data = store.read_source(vault_id, storage_root, source_path)
        staging = Path(tempfile.mkdtemp(prefix="sage-project-"))
        try:
            staged = staging / Path(source_path).name
            staged.write_bytes(data)
            yield staged
        finally:
            shutil.rmtree(staging, ignore_errors=True)

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
        payload fails validation. An explicit empty dict against
        a no-schema doc_type is accepted as trivially valid — mirrors the
        carve-out in ``MetadataService._validate_tier3`` so the
        ingest-vs-update behavior stays symmetric for the
        empty-merged-against-no-schema configuration.
        """
        validator = self._config.tier3_validator(resolved_doc_type)
        if validator is None:
            if not tier3:
                return
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
                # codes accepts list[str] (per IngestRequest type) or
                # comma-separated string.
                if isinstance(value, list):
                    updates["tags"] = [str(c).strip() for c in value if str(c).strip()]
                else:
                    updates["tags"] = [c.strip() for c in str(value).split(",") if c.strip()]
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

        The endpoint previews what an ingest of the same filename would
        derive, so the adapter resolves against the same process-wide
        registry ``ingest`` resolves against. Vault configuration declares
        no adapter availability at all (CAS-ADR-046), so there is nothing
        per-vault for either path to consult. Rejecting a source type SAGE
        has no adapter for keeps the preview from handing back suggestions
        for a source that could never be ingested (CAS-ADR-021).

        Raises:
            AdapterNotFoundError: No adapter is registered for ``adapter``.
        """
        if self._adapters.get(adapter) is None:
            raise AdapterNotFoundError(adapter)
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

    @staticmethod
    def _compute_chain_inheritance(
        field_view: dict,
        predecessor: Document,
        caller_keys: set[str],
    ) -> dict:
        """Per-field gap-fill of doc_type/project/authority_scope from a
        predecessor (CAS-ADR-021). Returns updates only for fields that
        are None on `field_view`, non-None on the predecessor, and not in
        `caller_keys`. Pure function, no mutations.
        """
        updates: dict = {}
        for field in ("doc_type", "project", "authority_scope"):
            pred_value = getattr(predecessor, field, None)
            view_value = field_view.get(field)
            if pred_value is not None and view_value is None and field not in caller_keys:
                updates[field] = pred_value
        return updates

    @staticmethod
    def _compute_adapter_tag_merge(
        current_tags: list[str],
        adapter_tags: list[str],
        adapter_tag_prefixes: list[str],
    ) -> list[str]:
        """Strip current tags whose prefix is in `adapter_tag_prefixes`,
        then dedupe ``[*adapter_tags, *retained]`` preserving order via
        ``dict.fromkeys`` (BH-131, BH-132). Pure function.
        """
        if adapter_tag_prefixes:
            current_tags = [
                t for t in current_tags if not any(t.startswith(p) for p in adapter_tag_prefixes)
            ]
        return list(dict.fromkeys([*adapter_tags, *current_tags]))

    def _compute_metadata_field_updates(
        self,
        *,
        baseline: dict,
        parsed: ParsedMetadata | None,
        caller_metadata: dict[str, str | list[str]] | None,
        predecessor: Document | None,
        adapter_tags: list[str],
        adapter_tag_prefixes: list[str],
        needs_review: bool,
        source_modified_at: datetime | None,
        vault_timezone: str,
    ) -> dict:
        """Compose all metadata precedence layers in memory before any
        insert/update touches the store. Closes the partial-
        metadata window that existed when these layers ran as separate
        post-insert ``update_document`` calls.

        Layer order (lowest precedence first):
          1. filename parse
          2. caller-supplied metadata (overwrites filename parse)
          3. chain inheritance (gap-fill only, never overwrites)
          4. adapter-tag merge (composes with current tags, prefix-strip)
          5. derived defaults (metadata_confirmed, doc_type, document_date)

        Inheritance and defaults consult ``baseline`` for force-reingest's
        existing-record values so an existing field blocks the "misc"
        default and existing tags participate in the adapter-tag merge.
        """
        field_updates: dict = {}

        if parsed is not None:
            field_updates.update(self._build_metadata_updates_from_parsed(parsed))

        caller_keys: set[str] = set()
        if caller_metadata:
            field_updates.update(self._build_metadata_updates(caller_metadata))
            caller_keys = set(caller_metadata.keys())

        if predecessor is not None:
            field_view = {**baseline, **field_updates}
            inherited = self._compute_chain_inheritance(field_view, predecessor, caller_keys)
            for k, v in inherited.items():
                field_updates.setdefault(k, v)

        if adapter_tags or adapter_tag_prefixes:
            current_tags = list(field_updates.get("tags") or baseline.get("tags") or [])
            field_updates["tags"] = self._compute_adapter_tag_merge(
                current_tags, adapter_tags, adapter_tag_prefixes
            )

        field_updates["metadata_confirmed"] = not needs_review

        resolved_doc_type = field_updates.get("doc_type") or baseline.get("doc_type")
        if not resolved_doc_type:
            field_updates["doc_type"] = "misc"

        resolved_doc_date = field_updates.get("document_date") or baseline.get("document_date")
        if not resolved_doc_date and source_modified_at:
            local_tz = ZoneInfo(vault_timezone)
            field_updates["document_date"] = (
                source_modified_at.astimezone(local_tz).date().isoformat()
            )

        return field_updates

    def _chunk_projection(
        self,
        document_id: str,
        projection: ProjectionResult,
    ) -> list[Chunk]:
        """Split projection into body chunks by heading.

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
        header chunk built by ``_build_header_chunk`` (F9).
        """
        chunks: list[Chunk] = []
        for i, heading in enumerate(projection.headings):
            # Prepend the ATX heading line to chunk content so the heading
            # mark survives into the reconstructed projection text.
            # Without this, _get_projection_text emits prose-only text and
            # round-trip through re-ingestion loses the heading hierarchy.
            atx_line = ("#" * heading.level) + " " + heading.text
            chunk_content = f"{atx_line}\n\n{heading.content}" if heading.content else atx_line
            chunks.append(
                Chunk(
                    document_id=document_id,
                    heading_path=heading.path,
                    content=chunk_content,
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
        """Build the synthetic document-header chunk's content.

        Composes a single text body covering title, source filename stem,
        tags, semantic_abstract, and a case-split identifier-token line.
        The abstract line is included even when ``semantic_abstract`` is
        unset (with an empty value) so the chunk has a stable shape;
        Stage 3 refreshes the chunk once abstraction completes.

        The identifier-token line carries lowercased case-split forms of
        compound identifiers (e.g. ``PortfolioDashboard`` →
        ``portfolio dashboard``) so the BM25 leg can match natural-
        language queries against camelcased identifiers that the
        text-search tokenizer leaves intact.
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
        .

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
        """Build the standalone synthetic document-header chunk."""
        return Chunk(
            document_id=document_id,
            heading_path=SYNTHETIC_HEADER_HEADING_PATH,
            content=self._build_header_chunk_content(doc),
            chunk_index=-1,
            doc_type=doc.doc_type,
            lifecycle_status=doc.lifecycle_status,
            project=doc.project,
        )
