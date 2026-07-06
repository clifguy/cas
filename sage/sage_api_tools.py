"""SAGE protocol and API tools for MCP.

Contains all tools that operate directly on the SAGE graph store and
services: protocol tools (ingest, get, update, lifecycle, link, traverse,
discover, export, refresh) and API query tools (vault stats, hash check,
staging edges, pending metadata).
"""

from collections.abc import Callable
from typing import Literal

from mcp.server.fastmcp import FastMCP
from pydantic import TypeAdapter, ValidationError

# Qualified module import so the ``get_stack_config`` MCP tool below can call
# ``sage.mcp_init.get_stack_config()`` from inside an inner function that
# shares its name. Resolving the implementation via the module attribute
# (rather than a ``from sage.mcp_init import get_stack_config`` binding)
# sidesteps the LEGB shadow that would otherwise make the inner tool function
# recurse to itself.
import sage.mcp_init  # noqa: I001 -- module import keeps the qualified call site alias-free
from sage.api.errors import (
    AmbiguousDocumentIdentifierError,
    LegacyFormError,
    MissingDocumentIdentifierError,
    SAGEError,
    translate_validation_error,
)
from sage.mcp_init import SAGEServices, reload_vault_in_registry
from sage.models.enums import RetrievalMode, SourceType
from sage.models.legacy_form import detect_legacy_form
from sage.models.schemas import (
    BulkLifecycleItem,
    BulkLifecycleRequest,
    BulkLinkItem,
    BulkLinkRequest,
    BulkMetadataItem,
    BulkMetadataRequest,
    ChainRequest,
    CreateVaultRequest,
    DiscoverRequest,
    DocumentDateStr,
    DocumentIdStr,
    EdgeIdStr,
    FunctionIdStr,
    HashCheckRequest,
    IngestRequest,
    Sha256Str,
    TraverseRequest,
    UpdateVaultConfigRequest,
    VaultIdStr,
)
from sage.services.vault_registry import VaultRegistryService

# Module-scope TypeAdapters for Pattern 2 boundary validation on FastMCP tool
# parameters. Each adapter is constructed once at import time and reused per
# tool call. ``validate_python`` raises ``pydantic_core.ValidationError`` (a
# ``ValueError`` subclass) on shape failures; every tool's
# ``except (SAGEError, ValueError)`` block catches it and routes through
# ``error_response(...)`` so MCP callers see a uniform error envelope.
_VAULT_ID_ADAPTER: TypeAdapter[str] = TypeAdapter(VaultIdStr)
_DOCUMENT_ID_ADAPTER: TypeAdapter[str] = TypeAdapter(DocumentIdStr)
_EDGE_ID_ADAPTER: TypeAdapter[str] = TypeAdapter(EdgeIdStr)
_FUNCTION_ID_ADAPTER: TypeAdapter[str] = TypeAdapter(FunctionIdStr)
_DOCUMENT_DATE_ADAPTER: TypeAdapter[str | None] = TypeAdapter(DocumentDateStr)
_SHA256_ADAPTER: TypeAdapter[str] = TypeAdapter(Sha256Str)


def _check_legacy_patch_form(field: str, value: object) -> None:
    """Raise ``LegacyFormError`` when ``value`` is the pre-patch shape for ``field``.

    Thin wrapper around ``detect_legacy_form`` that translates the
    pure-data detection result into the public-facing exception. The
    MCP tool surface uses this directly; the FastAPI surface goes
    through ``PydanticCustomError(type='legacy_form')`` raised inside
    request-model validators, which ``translate_validation_error``
    converts to ``LegacyFormError`` for the wire.
    """
    details = detect_legacy_form(field, value)
    if details is not None:
        raise LegacyFormError(
            field=details.field,
            received_type=details.received_type,
            example=details.example,
        )


def register_sage_tools(
    mcp: FastMCP,
    get_vault: Callable[[str], SAGEServices],
    serialize: Callable[[object], dict],
    error_response: Callable[[SAGEError | ValueError], dict],
    get_vaults: Callable[[], dict[str, SAGEServices]],
    get_vault_registry_service: Callable[[], VaultRegistryService],
) -> dict[str, Callable]:
    """Register all SAGE protocol and API tools on the MCP server.

    Returns a dict mapping tool function names to the actual functions,
    for re-export from mcp_server.

    ``get_vaults`` and ``get_vault_registry_service`` are call-time getters
    rather than instance arguments because the registration site
    (``sage.mcp_server``) is exposed to ``importlib.reload`` by
    ``tests/sage/test_cleanup_refactor.py``. After a reload, the module
    rebinds ``_vaults`` and ``_vault_registry_service`` to the original
    instances so that other modules keep working; resolving the values via
    getters at call time picks up the rebound originals, whereas capturing
    the instances at registration time would freeze the closures on the
    orphan reload-time objects.
    """

    # -------------------------------------------------------------------
    # SAGE protocol tools
    # -------------------------------------------------------------------

    @mcp.tool()
    async def ingest_document(
        vault_id: str,
        source: str,
        source_type: str,
        config: dict | None = None,
        created_by: str | None = None,
        force: bool = False,
        predecessor_id: str | None = None,
        expected_head_version: str | None = None,
        needs_review: bool = False,
        metadata: dict | None = None,
        tier3_metadata: dict | None = None,
        document_id: str | None = None,
    ) -> dict:
        """Ingest a source file into SAGE, running the projection ->
        indexing -> abstraction pipeline.

        Stages 2-3 (indexing, abstraction) dispatch as a background task;
        the call returns in seconds with ``pipeline_status`` typically
        non-terminal (projection_complete or indexing_in_progress), keeping
        the RPC under the 60-second MCP client timeout. Poll
        ``get_document`` for the terminal status: ``abstraction_complete``
        (the happy path), ``abstraction_skipped`` (the vault sets
        ``abstraction.enabled=false`` or the projection is empty, so Stage 3
        is bypassed), or ``failed`` (any Stage exception; ``pipeline_error``
        is populated). A requested supersede transition runs synchronously,
        so the version chain is complete on return.

        Metadata is caller-authoritative. Pass prepared values via
        ``metadata`` and leave ``needs_review=false``; the document commits
        with ``metadata_confirmed=true``. Set ``needs_review=true`` to defer
        to the review queue: filename inference fills fields the caller
        omitted (the field set is vault-config-defined under
        ``metadata_extraction.filename_extraction``; see
        ``admin_get_vault_config``) and the document is held with
        ``metadata_confirmed=false`` until confirmed via ``update_metadata``.

        Trio-field inheritance on supersede: when ``predecessor_id`` is set
        and the caller omits ``doc_type``, ``project``, or
        ``authority_scope`` from ``metadata``, each omitted field inherits
        the predecessor's non-None value silently. Pass the field
        explicitly to override.

        Tier3 uniqueness: a doc_type declaring a ``unique`` constraint in
        its ``metadata_schema`` (see
        ``document_types.doc_types[].metadata_schema`` in
        ``admin_get_vault_config``) enforces per-vault uniqueness on the
        named tier3 field at ingest time, checked in the same transaction
        as the row insert so the existing document is never disturbed. In
        the ``cas`` vault, ``ticket.ticket_id`` is the live example.

        Error modes:
        - ``adapter_not_found`` (400): ``source_type`` is not an enabled
          adapter (see ``source_adapters.adapters`` in
          ``admin_get_vault_config``).
        - ``source_file_not_found`` (404): ``source`` does not resolve to a
          readable file.
        - ``duplicate_content`` (409): a document with the same
          ``source_path`` and content hash exists. Override with
          ``force=true``.
        - ``force_reingest_path_mismatch`` (409): ``force=true`` and the
          content-hash match resolves to a document stored at a different
          ``source_path`` than ``source``, without a ``document_id``
          confirming the target. Force-reingest keys the record to reuse by
          content hash alone, so a byte-identical file at a different path can
          collide with an unrelated document; the guard refuses rather than
          overwrite its identity. Detail carries ``existing_document_id``,
          ``existing_source_path``, ``new_source_path``, and
          ``source_content_hash``. Pass ``document_id`` to confirm the intended
          record (for example, a document whose file legitimately moved).
        - ``supersede_target_not_active`` (409): ``predecessor_id`` was set
          but the predecessor is not ``active``. Run the archive ->
          reactivate dance via ``update_lifecycles`` before retrying.
        - ``identical_content_supersede`` (409): the new file's content
          hash matches the predecessor's; chains require distinct content
          per step.
        - ``stale_chain_head`` (409): ``expected_head_version`` did not
          match the predecessor's current ``updated_at`` at supersede time.
          Detail carries ``predecessor_id``, ``expected_head_version``,
          ``current_head_id``, and ``current_head_version`` so the caller
          can pivot to the current head and retry.
        - ``expected_head_version_requires_predecessor`` (400):
          ``expected_head_version`` supplied without ``predecessor_id`` (the
          token is bound to the predecessor's chain head).
        - ``tier3_schema_violation`` (400): ``tier3_metadata`` is set but
          the resolved doc_type has no ``metadata_schema``, or the payload
          failed validation. Detail carries ``doc_type``, ``path`` (JSON
          Pointer; empty when the doc_type has no schema), ``message``, and
          ``instance``.
        - ``tier3_unique_constraint_violation`` (409): ``tier3_metadata``
          supplied a value already in use on a ``unique`` tier3 field.
          Detail carries ``doc_type``, ``field``, ``colliding_value``, and
          ``existing_document_id``. ``force=true`` does NOT override this --
          uniqueness is independent of content-hash deduplication.

        Args:
            vault_id: Target vault identifier.
            source: Source file path relative to the vault's storage_root,
                or an absolute path to an external file (copied verbatim
                into the vault's imports/ directory). An absolute path is
                read from the filesystem of the machine running the SAGE
                server process; the retained copy lands on the vault's
                configured source store (CAS-ADR-043) and is authoritative
                after ingest, wherever that store lives -- the path passed
                here is temporary. Behavior is identical whether the
                vault's stores are local or cloud-hosted.
            source_type: Source artifact format (markdown, docx, pdf, email,
                onenote, teams_chat). Selects the source adapter.
            config: Adapter-specific configuration (optional). Not a
                SAGE-wide shape; inspect ``source_adapters.adapters[].config``
                in ``admin_get_vault_config`` for the per-adapter shape.
                Deep-merged over the vault's adapter-config defaults; unknown
                keys are rejected by the adapter.
            created_by: Creator name. Defaults to vault owner.
            force: Allow re-ingestion of duplicate content. The record to
                reuse is resolved by content hash alone, not by ``source``
                path; a byte-identical file at a different path collides with
                whatever document already carries that hash. When that
                document sits at a different ``source_path``, the ingest is
                rejected with ``force_reingest_path_mismatch`` unless
                ``document_id`` names the intended target. Same-path
                re-ingestion is unaffected.
            predecessor_id: When set, the ingested document supersedes this
                predecessor: SAGE creates a ``supersedes`` edge (new -> old)
                and archives the predecessor synchronously. The predecessor
                must be active with a content hash differing from the new
                file. Trio fields inherit from it when omitted (see above).
            expected_head_version: Optimistic-concurrency token on the chain
                head identified by ``predecessor_id``. Verified against the
                predecessor's current ``updated_at`` under the
                per-predecessor lock at supersede time; mismatch rejects with
                ``stale_chain_head``. Pass the value observed on a prior
                ``get_document`` read (ISO 8601 with ``Z`` suffix). Omit for
                last-writer-wins. Requires ``predecessor_id``.
            needs_review: When true, the document enters the metadata-review
                queue (metadata_confirmed=false) and filename inference fills
                omitted fields. Default false: inference is skipped and
                caller metadata commits authoritatively. Use
                ``get_filename_metadata`` for suggestions without queuing.
            metadata: Caller-supplied metadata fields, authoritative per
                field over filename parse, chain inheritance, and vault
                defaults. Recognized keys: title, version_label, project,
                doc_type, authority_scope, document_date, tags. Tags accept a
                list or a comma-separated string (whitespace trimmed, empty
                fragments dropped).
            tier3_metadata: Per-doc_type typed payload, validated against the
                doc_type's ``metadata_schema`` (see ``admin_get_vault_config``).
                When the doc_type declares no schema and this is non-null,
                ingest fails with ``tier3_schema_violation``. Stored verbatim
                once validated; queryable via ``search`` filters as
                ``{"tier3_metadata": {"<field>": <value>}}`` (exact equality;
                null matches absent-or-null fields).
            document_id: Pins the force-reingest target. Consulted only when
                ``force=true`` and a content-hash collision exists; ignored
                otherwise. Names the record to re-ingest into so a
                different-path hash match is treated as a deliberate reuse
                (for example, a moved file) rather than the unrelated-document
                collision that ``force_reingest_path_mismatch`` guards against.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            if predecessor_id is not None:
                predecessor_id = _DOCUMENT_ID_ADAPTER.validate_python(predecessor_id)
            if document_id is not None:
                document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            v = get_vault(vault_id)
            request = IngestRequest(
                source=source,
                source_type=source_type,
                config=config,
                created_by=created_by,
                force=force,
                predecessor_id=predecessor_id,
                expected_head_version=expected_head_version,
                needs_review=needs_review,
                metadata=metadata,
                tier3_metadata=tier3_metadata,
                document_id=document_id,
            )
            # Fire-and-forget pipeline keeps this RPC under the 60s MCP
            # client timeout (BH-130). Callers poll get_document
            # for terminal pipeline_status.
            result = await v.ingestion_service.ingest(request, wait_for_pipeline=False)
            return serialize(result.document)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def get_filename_metadata(
        vault_id: str,
        filename: str,
        source_type: str,
    ) -> dict:
        """Parse a filename's basename through the vault's FilenameParser and
        return the extracted metadata. Side-effect free: no document is
        created and vault state is unchanged.

        This is the companion to ``ingest_document``'s caller-authoritative
        metadata flow: call it first for filename-derived suggestions, decide
        which fields to keep, then call ``ingest_document`` with
        ``metadata=...``. Fields the parser could not extract come back null;
        when the vault has no ``filename_extraction.pattern`` configured, all
        fields are null.

        Which fields the parser extracts is vault-config-defined; see
        ``metadata_extraction.filename_extraction.segment_fields`` in
        ``admin_get_vault_config``. In the ``cas`` vault the pattern is
        ``{date}_{project}_{code}_{title}_{version}``, so the parser returns
        ``doc_date``, ``project``, ``doc_code``, ``title``, and ``version``.

        Error modes:
        - ``adapter_not_found`` (400): ``source_type`` is not an enabled
          adapter on this vault.

        Args:
            vault_id: Target vault identifier.
            filename: Filename to parse; the basename is used (directory
                components are stripped).
            source_type: Source artifact format (markdown, docx, pdf, email,
                onenote, teams_chat). Must be enabled on the vault.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            source_type_enum = SourceType(source_type)
            response = v.ingestion_service.parse_filename(filename, source_type_enum)
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def get_document(
        vault_id: str,
        document_id: str | None = None,
        include_content: bool = False,
        write_to_path: str | None = None,
        doc_id: str | None = None,
    ) -> dict:
        """Retrieve a document record with all metadata, lifecycle state, and
        pipeline status. Optional delivery of the vault-local source file
        bytes supports the agentic read-modify-reingest round-trip.

        Two mutually-exclusive delivery modes:
        - include_content=true: inline base64 bytes in the response.
          Fails with 413 ``content_too_large`` if the file exceeds the
          inline ceiling (default 100 MB; override via
          SAGE_MAX_INLINE_CONTENT_BYTES). Best for small files.
        - write_to_path=/abs/path: SAGE writes the bytes to the
          filesystem path; response carries only metadata (written_to,
          content_size, content_hash). Preferred for files that would
          exceed MCP tool-result size ceilings.

        Error modes:
        - ``invalid_document_id`` (400): the supplied document_id is not a
          well-formed id; rejected at the boundary before any lookup.
        - ``document_not_found`` (404): no document with that id.
        - ``content_file_missing`` (404): only when bytes are
          requested (either delivery mode); the document record
          exists but the vault-local file is absent.
        - ``content_too_large`` (413): include_content=true but the
          file exceeds the inline ceiling. Use ``write_to_path`` instead.
        - ``content_delivery_conflict`` (400): both ``include_content``
          and ``write_to_path`` were set; choose one.
        - ``write_path_exists`` (409): ``write_to_path`` target
          already exists.
        - ``write_path_invalid`` (400): ``write_to_path`` parent is
          missing or not writable, or the path is not absolute.

        Args:
            vault_id: Target vault identifier.
            document_id: The document's unique identifier. Alias: ``doc_id``.
                Supply exactly one of ``document_id`` or ``doc_id``.
            doc_id: Alias for ``document_id``; supply exactly one.
            include_content: When true, add `content` (base64) and
                `content_size` to the response. Default: false.
            write_to_path: Absolute filesystem path, resolved on the
                machine running the SAGE server process. When set, SAGE
                streams the retained source bytes there (from the vault's
                configured source store, unbounded by the inline-content
                ceiling) and populates `written_to`, `content_size`, and
                `content_hash` in the response. The target must not exist;
                its parent must exist and be writable. Mutually exclusive
                with `include_content`.
        """
        try:
            # Validate each id-bearing parameter by its literal name (so the
            # typed-alias gate in tests/sage/test_typed_alias_coverage.py sees
            # a _DOCUMENT_ID_ADAPTER.validate_python(<param>) call for each),
            # then resolve the alias to one value, then raise the
            # ambiguous/missing errors before any service call.
            if document_id is not None:
                document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            if doc_id is not None:
                doc_id = _DOCUMENT_ID_ADAPTER.validate_python(doc_id)
            if document_id is not None and doc_id is not None:
                raise AmbiguousDocumentIdentifierError(
                    tool="get_document", canonical="document_id", alias="doc_id"
                )
            if document_id is None and doc_id is None:
                raise MissingDocumentIdentifierError(
                    tool="get_document", accepted=["document_id", "doc_id"]
                )
            resolved_document_id = document_id if document_id is not None else doc_id
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            response = await v.documents_service.get_document_with_content(
                resolved_document_id, include_content, write_to_path
            )
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def update_lifecycles(
        vault_id: str,
        items: list[dict],
        response_mode: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Apply one or more lifecycle state transitions to documents.

        Accepts ``items`` as a list of N>=1 per-item transition requests;
        ``items=[{...}]`` is the single-transition form. This is the sole
        MCP entry point for lifecycle transitions.

        Each item carries ``document_id``, ``action``, and optional
        ``successor_id``. Items are processed in order, each holding the
        per-document lock and a per-item database transaction.

        The ``action`` vocabulary is vault-config-defined, not a fixed
        SAGE-wide set. Read ``lifecycle.transitions`` in the vault config
        (via ``admin_get_vault_config``) for the authoritative
        (from_state, action, to_state, creates_edge) tuples. The ``cas``
        vault uses ``ingest``, ``supersede``, ``complete``, ``archive``,
        ``reactivate``.

        **``supersede`` is the canonical atomic form for replacing one
        document with another:** it transitions the predecessor
        (``active -> archived``) AND creates the ``supersedes`` edge
        (new -> old) in one operation. The two-step alternative —
        ``create_edges`` with ``edge_type="supersedes"`` then
        ``update_lifecycles`` with ``action="archive"`` — ends in the same
        state but is needed only to patch an already-archived predecessor
        whose edge is missing (``create_edges`` does NOT auto-transition
        the predecessor's lifecycle).

        Each item is validated for the full lifecycle precondition surface
        (vault-config action vocabulary, ``invalid_lifecycle_transition``
        from the current state, the ``supersede`` chain-head and
        identical-content guards, ``pipeline_incomplete`` on ``complete``).

        **The batch is NOT atomic.** A per-item error surfaces in that
        item's error envelope without rolling back other items; the tool
        returns a success envelope whenever at least one item is processed,
        so inspect each ``BulkLifecycleItemResult.status`` and the
        aggregate ``success_count`` / ``error_count``. An error envelope is
        returned only when up-front validation rejects the call (invalid
        ``vault_id``, malformed ``items``, unknown vault, or invalid
        ``response_mode``). Empty ``items`` is valid: empty ``results``,
        zero counts.

        Args:
            vault_id: Target vault identifier.
            items: List of per-item transition requests, each conforming to
                the ``BulkLifecycleItem`` shape: ``{document_id?: str,
                doc_id?: str, action: str, successor_id: str | None}``.
                Supply exactly one of ``document_id`` or ``doc_id`` per
                item; ``doc_id`` is a back-compatible alias (neither or
                both is a per-item error). Shape validation runs up front;
                one malformed item rejects the whole batch before any
                per-item work.
            response_mode: Per-item payload depth. ``"full"`` returns each
                success item's complete ``document`` body (including the
                potentially large ``semantic_abstract``); ``"light"`` strips
                the ``document`` field to identity + status + warnings +
                error so the response stays inside the MCP inline budget
                (default 24 KiB; override via
                ``SAGE_MCP_INLINE_BUDGET_BYTES``). Failure entries always
                carry the full error envelope. When unset, defaults to
                ``"light"`` for ``len(items) > 5``, else ``"full"``
                (threshold ``LIGHT_DEFAULT_THRESHOLD = 5`` in
                ``sage.services.lifecycle``). Invalid values surface as
                ``internal_error`` before any per-item work.
            dry_run: When True, every item runs as a dry-run: validators
                execute, the would-be post-state projection is computed, and
                each result carries a ``changes`` block of field-level
                deltas (kept under ``response_mode=light``). No persistence;
                envelope-level only. **Limitation:** each item is evaluated
                against committed state at batch start, so sequential
                dependencies (item N supersedes a doc, item N+1 mutates it)
                are not reflected — dry-run such items separately. Default
                False.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            # Up-front shape validation across the whole batch: rejecting
            # the request here (rather than per-item inside the service
            # loop) guarantees that a malformed item produces an error
            # envelope without committing any partial state. The
            # ``response_mode`` ValueError from Pydantic enum validation
            # rides this same up-front rejection path.
            validated_items = [BulkLifecycleItem.model_validate(it) for it in items]
            v = get_vault(vault_id)
            request = BulkLifecycleRequest(
                items=validated_items,
                response_mode=response_mode,
                dry_run=dry_run,
            )
            response = await v.lifecycle_service.bulk_set_lifecycle(request)
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def create_edges(
        vault_id: str,
        items: list[dict],
        response_mode: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Create one or more typed edges between documents in the graph.

        Accepts ``items`` as a list of N>=1 per-item edge specs;
        ``items=[{...}]`` is the single-edge form. This is the sole MCP
        entry point for edge creation.

        Each item carries ``source_id``, ``target_id``, ``edge_type``,
        anchor fields, ``retracted_edge_id``, ``rationale``,
        ``rationale_kind``, ``notes``, and ``synced_from_*`` fields.
        Dispatch is idempotent: a duplicate natural-key triple
        (``source_id``, ``target_id``, ``edge_type``) returns the existing
        edge with ``created=false`` rather than raising. Items are
        processed in order, each under the process-wide link lock and a
        per-item database transaction.

        **For ``supersedes`` edges, prefer ``update_lifecycles`` with
        ``action="supersede"``** (or ``ingest_document(..., predecessor_id=...)``
        when the successor is not yet ingested): those wire the edge AND
        archive the predecessor atomically. ``create_edges`` with an
        ``edge_type="supersedes"`` item creates the edge alone and does
        **not** transition the predecessor's lifecycle — use it only to
        stitch a missing edge into a chain whose lifecycle states are
        already correct.

        **Per-item anchor fields by edge_type policy bucket.** Each edge
        type has a registry-declared ``resolution_policy`` dictating which
        anchor fields the item must carry:

        - ``none`` (supersedes, retracts, merged_from): meta-edges, no
          anchor fields. ``retracts`` instead takes a one-sided
          ``source_valid_from_version`` and ``retracted_edge_id`` (no
          ``target_id``).
        - ``transitive_source`` (derived_from): requires
          ``source_valid_from_version`` (anchors the edge in the source
          chain); no target anchor. For whole-document derivations set
          ``source_valid_from_version`` equal to ``source_id``.
        - ``transitive_both`` (covers, references, bundles_with,
          depends_on, instantiated_from): requires both
          ``source_valid_from_version`` and ``target_valid_from_version``.

        Canonical ``derived_from`` item (kwarg form shown; pass it as an
        ``items`` dict)::

            edge_type="derived_from", source_id="<deliverable_id>",
            target_id="<template_id>",
            source_valid_from_version="<deliverable_id>"

        **``merged_from`` chain-head precondition.** Both endpoints must be
        chain heads — neither ``source_id`` nor ``target_id`` may have an
        outbound ``supersedes`` edge — or the per-item
        ``merged_from_validation`` envelope is returned. When the source is
        mid-chain and content reuse is what's wanted, use ``derived_from``
        instead: its ``source_valid_from_version`` anchor captures the
        chain-visibility semantics ``merged_from`` lacks.

        **The batch is NOT atomic.** A per-item error surfaces in that
        item's error envelope without rolling back other items; the tool
        returns a success envelope whenever at least one item is processed,
        so inspect each ``BulkLinkItemResult.status`` and the aggregate
        ``success_count`` / ``error_count``. An error envelope is returned
        only when up-front validation rejects the call: ``invalid_vault_id``
        (malformed ``vault_id``), ``invalid_sha256`` (a per-item
        ``synced_from_content_hash`` is not a well-formed hash), another
        malformed ``items`` shape, ``unknown_vault``, or invalid
        ``response_mode``. Empty ``items`` is valid: empty ``results``,
        zero counts.

        Per-item error modes (inside the response envelope):
        ``self_referential_edge`` (400), ``document_not_found`` (404),
        ``tbd_policy_edge`` (400), ``edge_anchor_policy_violation`` (400),
        ``retract_target_not_edge`` (400), ``merged_from_validation`` (400),
        ``synced_from_inapplicable_edge_type`` (400),
        ``synced_from_version_not_in_source_chain`` (404).

        On ``dry_run=True`` no edges persist: each ``edge.id`` carries the
        nil-UUID sentinel (or the existing id on a natural-key hit with
        ``created=false``) and the envelope echoes ``dry_run=True``.

        Args:
            vault_id: Target vault identifier.
            items: List of per-item link requests, each conforming to the
                ``BulkLinkItem`` shape: ``{source_id, target_id?,
                edge_type, source_valid_from_version?,
                target_valid_from_version?, retracted_edge_id?, notes?,
                rationale?, rationale_kind?, synced_from_version?,
                synced_from_content_hash?}``. Shape validation runs up
                front; one malformed item rejects the whole batch before
                any per-item work.
            response_mode: Per-item payload depth. ``"full"`` returns each
                success item's complete ``edge`` body; ``"light"`` strips
                it to ``source_id`` / ``target_id`` / ``edge_type`` /
                ``status`` / ``created`` / ``existing_rationale`` /
                ``error`` to stay inside the MCP inline budget (``created``
                and ``existing_rationale`` are kept as the only natural-key
                idempotency signals). Failure entries always carry the full
                error envelope. When unset, defaults to ``"light"`` for
                ``len(items) > 5``, else ``"full"`` (threshold
                ``LIGHT_DEFAULT_THRESHOLD = 5`` in
                ``sage.services.graph_ops``). Invalid values surface as
                ``internal_error`` before any per-item work.
            dry_run: When True, every item runs as a dry-run: validators
                execute, the would-be edge projection is computed, and each
                ``edge.id`` carries the sentinel (or the existing id on a
                natural-key hit). No persistence; envelope-level only.
                **Limitation:** each item is evaluated against committed
                state at batch start, so no item's would-be effects are
                visible to later items. Default False.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            # Up-front shape validation across the whole batch: rejecting
            # the request here (rather than per-item inside the service
            # loop) guarantees that a malformed item produces an error
            # envelope without committing any partial state. The
            # ``response_mode`` ValueError from Pydantic enum validation
            # rides this same up-front rejection path.
            validated_items = [BulkLinkItem.model_validate(it) for it in items]
            v = get_vault(vault_id)
            request = BulkLinkRequest(
                items=validated_items,
                response_mode=response_mode,
                dry_run=dry_run,
            )
            response = await v.graph_ops_service.create_edges(request)
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def update_metadata(
        vault_id: str,
        items: list[dict],
        response_mode: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Patch mutable metadata fields on one or more documents.

        Accepts ``items`` as a list of N>=1 per-item patch requests;
        ``items=[{...}]`` is the single-document form. This is the sole MCP
        entry point for metadata patching.

        Each item carries ``document_id`` plus any subset of the patchable
        fields (``title``, ``version_label``, ``project``, ``tags``,
        ``doc_type``, ``authority_scope``, ``document_date``,
        ``tier3_metadata``, ``expected_version``). Items are processed in
        order, each holding the per-document lock and a per-item database
        transaction.

        Scalars (``title``, ``version_label``, ``project``, ``doc_type``,
        ``authority_scope``, ``document_date``) use set-or-omit semantics:
        pass to set, omit to leave unchanged. List-valued fields (today:
        ``tags``) take a ``ListFieldPatch`` ops-object (``{add, remove}``);
        ``tier3_metadata`` takes a ``Tier3Patch`` ops-object
        (``{set, unset}``). Bare-list / bare-dict forms are rejected (see
        ``legacy_form`` below). The ops-object shape is the
        concurrency-safety contract: parallel adds of distinct values to
        the same list-valued field commute.

        Each successful per-item patch sets ``metadata_confirmed=true`` on
        the target (it leaves the metadata-review queue if it was there).
        The ``doc_type`` value must be one declared under
        ``document_types.doc_types`` in the vault config; query
        ``admin_get_vault_config`` for the authoritative list.

        Empty-patch confirmation-flip: an item carrying only
        ``document_id`` (no field-patch keys) is a **pure-confirmation
        flip**, not a no-op — it flips ``metadata_confirmed`` to True,
        advances ``updated_at``, and stamps ``last_modified_by``. Including
        the item IS the confirmation signal.

        **The batch is NOT atomic.** A per-item error surfaces in that
        item's error envelope without rolling back other items; the tool
        returns a success envelope whenever at least one item is processed,
        so inspect each ``BulkMetadataItemResult.status`` and the aggregate
        ``success_count`` / ``error_count``. An error envelope is returned
        only when up-front validation rejects the call (invalid
        ``vault_id``, malformed ``items``, per-item ``legacy_form`` shape,
        unknown vault, or invalid ``response_mode``). Empty ``items`` is
        valid: empty ``results``, zero counts.

        List-valued field patch shape (per-item ``tags``)::

            {"add": ["x",...], "remove": ["y",...]}

        At least one key required and non-empty; ``add`` values must NOT be
        present on the field, ``remove`` values MUST be present (strict
        conflict).

        Tier3 patch shape (per-item ``tier3_metadata``)::

            {"set": {"key": "value",...}, "unset": ["other_key",...]}

        The merged result is validated against the resolved doc_type's
        ``metadata_schema``.

        Per-item error modes (inside the response envelope):
        ``document_not_found`` (404), ``invalid_doc_type`` (400),
        ``{field}_add_conflict`` / ``{field}_remove_conflict`` (400, e.g.
        ``tags_add_conflict``), ``tag_patch_overlap`` (400),
        ``tier3_unset_conflict`` / ``tier3_patch_overlap`` / ``patch_empty``
        (400), ``tier3_schema_violation`` (400),
        ``tier3_doc_type_change_stale_keys`` (400), and ``stale_read`` (409,
        when a per-item ``expected_version`` does not match the target's
        current version).

        Batch-level error modes (the tool's error envelope): ``legacy_form``
        (a per-item ``tags`` is a bare list or ``tier3_metadata`` a bare
        key/value dict; detail names the ops-object shape),
        ``invalid_vault_id`` (400, malformed ``vault_id``),
        ``invalid_document_date`` (400, a per-item ``document_date`` is not a
        YYYY-MM-DD calendar date), ``unknown_vault``, and ``internal_error``
        (a malformed ``items`` shape or invalid ``response_mode``).

        Args:
            vault_id: Target vault identifier.
            items: List of per-item patch requests, each conforming to the
                ``BulkMetadataItem`` shape: ``{document_id?: str, doc_id?:
                str, title?: str, version_label?: str, project?: str,
                tags?: ListFieldPatch, doc_type?: str, authority_scope?:
                str, document_date?: str, tier3_metadata?: Tier3Patch,
                expected_version?: str}``. Supply exactly one of
                ``document_id`` or ``doc_id`` per item; ``doc_id`` is a
                back-compatible alias (neither or both is a per-item
                error). Shape validation runs up front; one malformed item
                rejects the whole batch before any per-item work.
            response_mode: Per-item payload depth. ``"full"`` returns each
                success item's complete ``document`` body (including the
                potentially large ``semantic_abstract``); ``"light"`` strips
                the ``document`` field to identity + status + warnings +
                error so the response stays inside the MCP inline budget
                (default 24 KiB; override via
                ``SAGE_MCP_INLINE_BUDGET_BYTES``). Failure entries always
                carry the full error envelope. When unset, defaults to
                ``"light"`` for ``len(items) > 5``, else ``"full"``
                (threshold ``LIGHT_DEFAULT_THRESHOLD = 5`` in
                ``sage.services.metadata``). Invalid values surface as
                ``internal_error`` before any per-item work.
            dry_run: When True, every item runs as a dry-run: validators
                execute, the would-be post-state projection is computed, and
                each result carries a ``changes`` block of field-level
                deltas (kept under ``response_mode=light``). No persistence;
                envelope-level only. **Limitation:** each item is evaluated
                against committed state at batch start, so sequential
                dependencies (item N adds tag X, item N+1 adds the same tag)
                are not reflected — dry-run such items separately. Default
                False.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            # Up-front per-item legacy-form check BEFORE Pydantic
            # validation: without this, Pydantic would raise a generic
            # ValueError for a bare-list `tags` or bare-dict
            # `tier3_metadata` value and the `legacy_form` error code
            # (with the worked-example detail per CAS-ADR-028) would be
            # lost to the caller.
            for item in items:
                if isinstance(item, dict):
                    _check_legacy_patch_form("tags", item.get("tags"))
                    _check_legacy_patch_form("tier3_metadata", item.get("tier3_metadata"))
            # Up-front shape validation across the whole batch: rejecting
            # the request here (rather than per-item inside the service
            # loop) guarantees that a malformed item produces an error
            # envelope without committing any partial state. The
            # ``response_mode`` ValueError from Pydantic enum validation
            # rides this same up-front rejection path.
            validated_items = [BulkMetadataItem.model_validate(it) for it in items]
            v = get_vault(vault_id)
            request = BulkMetadataRequest(
                items=validated_items,
                response_mode=response_mode,
                dry_run=dry_run,
            )
            response = await v.metadata_service.bulk_update_metadata(request, v.config.vault.owner)
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def delete_edge(vault_id: str, edge_id: str, dry_run: bool = False) -> dict:
        """Delete a production edge from the graph.

        For staging-table edges (pre-confirmation), use
        ``update_staging_edge(action="dismiss")`` instead — the two tables
        are distinct and edge ids do not cross between them.

        Discovering ``edge_id``: use ``search`` with ``target="edges"`` to
        enumerate production edges by ``source_id`` / ``target_id`` /
        ``edge_type``, e.g. ``search(vault_id=..., mode="catalog",
        target="edges", filters={"source_id": "...", "edge_type": "..."})``.
        The returned ``edge_id`` is the value to pass here.

        Error modes:
        - ``invalid_vault_id`` (400): ``vault_id`` failed typed-alias
          validation at the boundary.
        - ``invalid_edge_id`` (400): ``edge_id`` is not a well-formed UUID.
        - ``edge_not_found`` (404): no production edge with that id (raised
          on dry-run too).

        Args:
            vault_id: Target vault identifier.
            edge_id: Production edge identifier.
            dry_run: When True, confirm the edge exists and preview the
                would-be deletion without persisting; the response carries
                ``deleted=false``, ``dry_run=true``, and the edge in
                ``preview_edge`` (the change surface — there is no separate
                ``changes`` block). Default False.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            edge_id = _EDGE_ID_ADAPTER.validate_python(edge_id)
            v = get_vault(vault_id)
            result = await v.graph_ops_service.unlink(edge_id, dry_run=dry_run)
            return serialize(result)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def verify_preconditions(vault_id: str, function_id: str) -> dict:
        """Check whether all depends_on targets for a function document are
        satisfied (active or completed lifecycle, pipeline complete).

        Iterates the document's outbound ``depends_on`` edges; for each
        target, verifies lifecycle in (active, completed) and
        pipeline_status terminal. Returns ``satisfied`` boolean plus a
        per-edge breakdown of failing reasons (e.g. predecessor still
        in projection, target archived) so the caller can act on the
        gap rather than re-querying each dependency.

        Error modes:
        - ``invalid_vault_id`` (400): ``vault_id`` failed typed-alias
          validation at the boundary.
        - ``invalid_function_id`` (400): ``function_id`` is not a well-formed
          document id.
        - ``document_not_found`` (404): no document with ``function_id``.

        Args:
            vault_id: Target vault identifier.
            function_id: The function document's identifier. Despite
                the name, this works on any document with outbound
                ``depends_on`` edges, not only documents typed as
                "function".
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            function_id = _FUNCTION_ID_ADAPTER.validate_python(function_id)
            v = get_vault(vault_id)
            result = await v.graph_ops_service.check_preconditions(function_id)
            return serialize(result)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def traverse(
        vault_id: str,
        start_id: str | None = None,
        edge_type: str | None = None,
        direction: str = "outbound",
        depth: int = 3,
        debug: bool = False,
        document_id: str | None = None,
    ) -> dict:
        """Walk the document graph from a starting document.

        Traversal honors chain-scoped edge resolution: anchor fields
        determine which edges are visible from the query version's lineage;
        `retracts` edges can suppress downstream edges; `merged_from`
        tombstones suppress predecessor-chain edges downstream of the
        termination point.

        **Outbound dedup on ``transitive_both`` edges.** For
        ``transitive_both`` edge types (``covers``, ``references``,
        ``bundles_with``, ``depends_on``, ``instantiated_from``), outbound
        traversal deduplicates by target document: at most one
        representative edge per (source-chain, target) pair is returned,
        chain-scoped resolution selecting the winner from the query
        position's lineage. Distinct edges from different chain members
        pointing at the same target are masked. To enumerate every edge into
        a target chain, traverse **inbound** from the target instead.
        ``edge_counts.{edge_type}`` reflects total visible edges from the
        query position including masked ones; if ``edge_counts >
        len(nodes)`` the result has masked siblings. ``supersedes`` is
        point-to-point and exempt; the rule applies only to the five
        ``transitive_both`` types.

        Args:
            vault_id: Target vault identifier.
            start_id: Starting document identifier. Alias: ``document_id``.
                Supply exactly one of ``start_id`` or ``document_id``. The
                response key remains ``start_id`` regardless of which input
                form was used.
            edge_type: Filter by edge type (optional). When omitted,
                traversal returns edges of all types.
            direction: Traversal direction (outbound, inbound, both). Default: outbound.
            depth: Maximum traversal depth (1-1000). Default: 3.
            debug: When true, populate `resolution_path` on the response
                with per-event entries (`anchor_hit`, `anchor_miss`,
                `retracts_applied`, `tombstone_applied`) explaining why
                each candidate edge was surfaced or suppressed. Default:
                false (zero overhead when disabled).
            document_id: Alias for ``start_id``. Either parameter
                is accepted; supply exactly one. Supplying both — even with
                equal values — returns ``ambiguous_document_identifier``.
        """
        try:
            # Validate each id-bearing parameter by its literal
            # name (so the typed-alias conformance gate in
            # tests/sage/test_typed_alias_coverage.py sees a
            # _DOCUMENT_ID_ADAPTER.validate_python(<param>) call for
            # each), then resolve the alias to a single value, then
            # surface the tool-specific ambiguous/missing errors before
            # any service call so callers don't see a downstream
            # document_not_found for an empty input.
            if start_id is not None:
                start_id = _DOCUMENT_ID_ADAPTER.validate_python(start_id)
            if document_id is not None:
                document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            if start_id is not None and document_id is not None:
                raise AmbiguousDocumentIdentifierError(
                    tool="traverse",
                    canonical="start_id",
                    alias="document_id",
                )
            if start_id is None and document_id is None:
                raise MissingDocumentIdentifierError(
                    tool="traverse",
                    accepted=["start_id", "document_id"],
                )
            resolved_start_id = start_id if start_id is not None else document_id
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            request = TraverseRequest(
                start_id=resolved_start_id,
                edge_type=edge_type,
                direction=direction,
                depth=depth,
                debug=debug,
            )
            response = await v.graph_ops_service.traverse(request)
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def chain(
        vault_id: str,
        edge_type: str,
        document_id: str | None = None,
        doc_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict:
        """Walk an edge chain to both ends from a starting document.

        Returns an ordered list of all documents in the chain with
        positional metadata (head, tail, query position, linearity).
        Designed for version history retrieval on supersedes chains
        but works with any edge type. A document with no edges of the
        requested type returns a single-entry chain (the document
        itself as both head and tail).

        ``linearity`` in the response reports whether the chain is
        strictly linear, branched (multiple successors), or forks
        somewhere in the lineage -- relevant when chasing supersedes
        chains that have not yet been merged.

        Error modes:
        - ``invalid_document_id`` (400): the supplied document_id is not a
          well-formed id; rejected at the boundary before any lookup.
        - ``document_not_found`` (404): no document with that id.

        Args:
            vault_id: Target vault identifier.
            edge_type: Edge type to follow (e.g. "supersedes", "references").
            document_id: Document ID to start the chain walk from. Alias: ``doc_id``.
                Supply exactly one of ``document_id`` or ``doc_id``. The result
                is symmetric: any chain member returns the full ordered chain
                with that member's position indicated.
            doc_id: Alias for ``document_id``; supply exactly one.
            limit: Maximum chain entries to return. Default: all.
                Use with offset to page through long version chains.
            offset: Skip this many entries from the start (oldest). Default: 0.
        """
        try:
            # See get_document: validate each id param by literal name for the
            # typed-alias gate, then resolve the alias and surface the
            # ambiguous/missing errors before any service call.
            if document_id is not None:
                document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            if doc_id is not None:
                doc_id = _DOCUMENT_ID_ADAPTER.validate_python(doc_id)
            if document_id is not None and doc_id is not None:
                raise AmbiguousDocumentIdentifierError(
                    tool="chain", canonical="document_id", alias="doc_id"
                )
            if document_id is None and doc_id is None:
                raise MissingDocumentIdentifierError(
                    tool="chain", accepted=["document_id", "doc_id"]
                )
            resolved_document_id = document_id if document_id is not None else doc_id
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            request = ChainRequest(
                document_id=resolved_document_id,
                edge_type=edge_type,
                limit=limit,
                offset=offset,
            )
            response = await v.graph_ops_service.chain(request)
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def search(
        vault_id: str,
        mode: RetrievalMode = RetrievalMode.SEMANTIC,
        query: str | None = None,
        scope: str = "all",
        filters: dict | None = None,
        document_id: str | None = None,
        heading_path: str | None = None,
        limit: int = 10,
        offset: int = 0,
        use_hybrid: bool = True,
        use_abstract_prefilter: bool = True,
        include_abstracts: bool = False,
        min_relevance: float | None = None,
        target: str = "documents",
        response_mode: str | None = None,
        sort_by: str | None = None,
        sort_order: str | None = None,
    ) -> dict:
        """Search for documents or edges; semantic, keyword, catalog, or deterministic retrieval.

        Modes:
            semantic: Vector + optional BM25 fusion. Requires query.
            keyword: BM25-only search. Requires query. Use query="*" for filter-only listing.
            catalog: Filter-only SQL enumeration -- the canonical way to
                enumerate documents already in a vault. No query needed. Returns
                document metadata only (no chunks or scores). Supports pagination
                via limit + offset. Best for deterministic enumeration by tags,
                doc_type, or other metadata.
            deterministic: Exact heading path extraction. Requires document_id + heading_path.

        Edge enumeration:
            When ``target="edges"`` (only valid with ``mode="catalog"``),
            results are edge rows rather than document rows. Filter by any
            subset of ``{"source_id":..., "target_id":..., "edge_type":...}``;
            an empty filter returns all edges in the vault, paginated. Each
            row carries the edge id (required for ``delete_edge`` and the
            ``retracts`` edge_type), endpoints, edge_type, anchor versions,
            rationale, and retraction state (``retracted_at`` plus the id
            of the disclaiming retracts edge, when applicable). Use
            ``response_mode="light"`` to strip to identity columns; ``full``
            to carry the complete envelope. Default obeys a threshold rule:
            ``light`` when more than 5 results would be returned, otherwise
            ``full``.

            Example::

                search(
                    vault_id="cas",
                    mode="catalog",
                    target="edges",
                    filters={"source_id": "<doc_id>", "edge_type": "references"},
                    response_mode="full")

        Response-mode semantics across targets:
            ``response_mode`` is the canonical payload-depth selector for both
            targets. Behavior matrix:

            - ``target="documents", mode="catalog"``: ``light`` returns a
              stripped ``DocumentSummaryLight`` carrying only id, title,
              doc_type, lifecycle_status, and tier3_metadata; ``full``
              returns the complete ``DocumentSummary``. The edge-side
              >5-results default-to-light rule does NOT apply --
              document-target defaults remain full-equivalent unless
              ``response_mode="light"`` is passed explicitly.
            - ``target="documents", mode="semantic"`` or ``"keyword"``:
              ``light`` suppresses ``chunk_content`` but preserves the full
              ``DocumentSummary``; ``full`` includes ``chunk_content``.
            - ``target="documents", mode="deterministic"``:
              ``response_mode`` is ignored. Deterministic always returns
              chunk content.
            - ``target="edges"``: see the *Edge enumeration* section above.

        Args:
            vault_id: Target vault identifier.
            mode: Retrieval mode (semantic, keyword, catalog, deterministic). Default: semantic.
            query: Search query text (required for semantic/keyword modes).
            scope: Retrieval scope (all, authoritative, specific, filtered). Default: all.
            filters: Scope filters. Document-target keys: doc_type, project,
                lifecycle_status, tags, document_ids, pipeline_status,
                tier3_metadata. Edge-target keys (only when
                ``target="edges"``): source_id, target_id, edge_type.
                The ``tier3_metadata`` key takes a dict of field-name to
                expected-value pairs that match against each document's
                ``tier3_metadata``. Equality is exact; ``null``
                in the expected value matches documents whose stored field
                is null or absent. All pairs AND together. Mixing
                document-only and edge-only keys is rejected via
                ``mode_parameter_mismatch``.
                Example (documents): ``{"doc_type": "failure_record",
                "tier3_metadata": {"severity": "high", "fix_commit": null}}``.
                Example (edges): ``{"source_id": "...", "edge_type":
                "references"}``.
            document_id: Target document (required for deterministic mode).
            heading_path: Heading path prefix (required for deterministic mode).
            limit: Maximum results (1-100). Default: 10.
            offset: Skip this many results before returning (catalog mode pagination). Default: 0.
            use_hybrid: Use hybrid RRF fusion of vector + BM25 in semantic mode. Default: true.
            use_abstract_prefilter: Boost documents whose semantic abstract matches the
                query (two-pass retrieval). Applies to semantic and keyword modes. Default: true.
            include_abstracts: Include semantic_abstract in results. Default: false.
                Abstracts are large and rarely needed in search result lists. Set true
                when you need document summaries for disambiguation or orientation.
            min_relevance: Minimum relevance score to include in results. Default: None
                (no filtering). For semantic mode (cosine similarity), scores range 0-1;
                reasonable thresholds are 0.3-0.5. Does not apply to catalog or
                deterministic modes which have no relevance scores.
            target: Result row type. "documents" (default) preserves the
                historical surface; "edges" enumerates production edges
                via filter on ``source_id`` / ``target_id`` /
                ``edge_type`` and is valid only with ``mode="catalog"``.
                See the *Edge enumeration* section above.
            response_mode: Canonical payload-depth selector. See the
                *Response-mode semantics across targets* section above for
                the full matrix. "light" returns the stripped shape
                (DocumentSummaryLight for catalog+documents, identity
                columns for edges, chunk_content-suppressed for
                semantic/keyword); "full" returns the complete envelope.
                When unset, edges apply the >5-results default-threshold
                rule; documents preserve full-equivalent behavior.
            sort_by: Sort key for catalog mode results. One of:
                "title", "doc_type", "document_date",
                "lifecycle_status". Ignored by semantic, keyword, and
                deterministic modes. Default: unset -- catalog falls
                back to active-lifecycle-first then ``document_date``
                descending.
            sort_order: Sort direction for catalog mode results. One
                of: "asc", "desc". Ignored by semantic, keyword, and
                deterministic modes. Default: unset -- ascending when
                ``sort_by`` is specified.

        Catalog budget hint:
            Catalog responses include a ``hints`` field carrying
            ``recommended_limit`` when the serialized result would
            exceed the Claude Code MCP inline ceiling. When present,
            re-page with ``limit=recommended_limit`` to keep the
            response inline and avoid the disk/jq fallback. The
            budget defaults to 24 KiB and is configurable per process
            via ``SAGE_MCP_INLINE_BUDGET_BYTES``.

        Error modes:
        - ``invalid_mode`` (400): ``mode`` is not one of ``semantic``,
          ``keyword``, ``catalog``, ``deterministic``. Detail carries
          the offending ``mode`` and ``valid_modes``.
        - ``unknown_filter_key`` (400): a key in ``filters`` is not a
          declared field on ``RetrievalFilters``. Detail carries the
          offending ``key``, ``valid_keys``, and a worked ``example``.
        - ``invalid_filter_shape`` (400): a value in ``filters`` has the
          wrong type for its field (e.g., ``{"tags": 42}`` where
          ``list[str]`` was expected). Detail carries ``field``,
          ``expected_type``, ``received_type``.
        - ``mode_parameter_mismatch`` (400): a parameter is set that is
          not valid for the chosen mode (e.g., ``heading_path`` outside
          deterministic mode, ``query`` in deterministic mode). Detail
          carries ``mode``, ``forbidden_param``, ``allowed_modes``.
        - ``missing_query`` / ``missing_document_id`` / ``missing_heading_path``
          (400): a parameter required for the chosen mode is absent (the
          inverse case of ``mode_parameter_mismatch``).
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            if document_id is not None:
                document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            v = get_vault(vault_id)
            # Pass the raw dict so DiscoverRequest performs the
            # nested RetrievalFilters validation. This keeps the
            # ValidationError loc prefixed with ``("filters",...)``, which
            # the translator in sage.api.errors needs to map into typed
            # ``unknown_filter_key`` / ``invalid_filter_shape`` envelopes.
            request = DiscoverRequest(
                mode=mode,
                query=query,
                scope=scope,
                filters=filters,
                document_id=document_id,
                heading_path=heading_path,
                limit=limit,
                offset=offset,
                target=target,
                response_mode=response_mode,
                use_hybrid=use_hybrid,
                use_abstract_prefilter=use_abstract_prefilter,
                include_abstracts=include_abstracts,
                min_relevance=min_relevance,
                sort_by=sort_by,
                sort_order=sort_order,
            )
            response = await v.retrieval_service.discover(request)
            return serialize(response)
        except SAGEError as e:
            return error_response(e)
        except ValidationError as e:
            sage_err = translate_validation_error(e)
            if sage_err is not None:
                return error_response(sage_err)
            return error_response(e)
        except ValueError as e:
            return error_response(e)

    @mcp.tool()
    async def read_projection(
        vault_id: str,
        document_id: str | None = None,
        write_to_path: str | None = None,
        doc_id: str | None = None,
        delivery: Literal["inline", "spill", "auto"] = "auto",
    ) -> dict:
        """Read a document's full text into context with metadata header.

        Two delivery modes:
        - inline (default): returns the complete projection (reconstructed
          from stored chunks) inline as ``projection_text``, equivalent to
          uploading the document. Use this instead of ``search``
          when you need the whole document.
        - ``write_to_path=/abs/path``: SAGE writes the projection text
          to the given absolute path. The response carries ``written_to``
          and ``content_size``; ``projection_text`` is null. Preferred
          for large projections that would exceed the MCP tool-result
          inline budget. Mirrors ``get_document(write_to_path=...)``.

        ``delivery`` pins which shape you get instead of leaving it implicit
        in whether ``write_to_path`` was supplied:
        - ``auto`` (default): spill to disk when ``write_to_path`` is given,
          inline otherwise — the prior behavior.
        - ``inline``: force the inline body. Supplying ``write_to_path``
          alongside it is contradictory and returns ``delivery_conflict``.
        - ``spill``: force write-to-disk delivery; it requires
          ``write_to_path`` and returns ``delivery_conflict`` without one.
        Decide up front with ``read_meta.body_length`` (the inline body
        size on a prior or auto read) before forcing ``inline`` on a large
        document.

        Error modes:
        - ``invalid_document_id`` (400): the supplied document_id is not a
          well-formed id; rejected at the boundary before any lookup.
        - ``document_not_found`` (404): no document with that id.
        - ``no_projection`` (404): the document exists but has no
          stored projection (e.g. ingestion failed mid-pipeline or
          the document is awaiting reabstraction). Inspect
          ``pipeline_status`` via ``get_document``; if recoverable,
          ``recompute_abstract`` may restore the projection.
        - ``delivery_conflict`` (400): ``delivery`` contradicts
          ``write_to_path`` (``inline`` with a path, or ``spill``
          without one).
        - ``write_path_exists`` (409): ``write_to_path`` target already
          exists.
        - ``write_path_invalid`` (400): ``write_to_path`` is not
          absolute, or its parent directory is missing / not writable.

        Args:
            vault_id: Target vault identifier.
            document_id: The document's unique identifier. Alias: ``doc_id``.
                Supply exactly one of ``document_id`` or ``doc_id``.
            doc_id: Alias for ``document_id``; supply exactly one.
            write_to_path: Absolute filesystem path, resolved on the
                machine running the SAGE server process. When set, SAGE
                writes the projection text to this path and returns
                metadata only. The target must not exist; its parent
                must exist and be writable.
            delivery: Inline-vs-spill selector (``inline | spill | auto``).
                ``auto`` keeps the write_to_path-driven default.
        """
        try:
            # See get_document: validate each id param by literal name for the
            # typed-alias gate, then resolve the alias and surface the
            # ambiguous/missing errors before any service call.
            if document_id is not None:
                document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            if doc_id is not None:
                doc_id = _DOCUMENT_ID_ADAPTER.validate_python(doc_id)
            if document_id is not None and doc_id is not None:
                raise AmbiguousDocumentIdentifierError(
                    tool="read_projection", canonical="document_id", alias="doc_id"
                )
            if document_id is None and doc_id is None:
                raise MissingDocumentIdentifierError(
                    tool="read_projection", accepted=["document_id", "doc_id"]
                )
            resolved_document_id = document_id if document_id is not None else doc_id
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            response = await v.utilities_service.read_projection(
                resolved_document_id, write_to_path=write_to_path, delivery=delivery
            )
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def read_section(
        vault_id: str,
        heading_path: str,
        document_id: str | None = None,
        doc_id: str | None = None,
    ) -> dict:
        """Read a section of a document by heading path.

        Returns clean readable text for a heading subtree without loading
        the full document. Uses structural prefix matching from the
        document root: requesting "Technical Description" returns that
        heading plus all children (e.g.
        "Technical Description > Composite Claim Binding"). Bare-text
        queries that match the *tail* of a stored path (e.g. "CLAIMS"
        against a stored "CLAIMS -- Remove Before Filing") will not match
        — when this happens, the heading_not_found error includes a
        ``candidate_matches`` field listing stored paths that contain the
        query as a substring, so you can retry with the exact path.

        For free-text "find this section by name regardless of path
        position," prefer search semantic or keyword mode — both
        index heading_path text alongside content.

        Args:
            vault_id: Target vault identifier.
            document_id: The document's unique identifier. Alias: ``doc_id``.
                Supply exactly one of ``document_id`` or ``doc_id``.
            doc_id: Alias for ``document_id``; supply exactly one.
            heading_path: Heading path prefix
                (e.g. "Technical Description > Composite Claim Binding").
        """
        try:
            # See get_document: validate each id param by literal name for the
            # typed-alias gate, then resolve the alias and surface the
            # ambiguous/missing errors before any service call.
            if document_id is not None:
                document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            if doc_id is not None:
                doc_id = _DOCUMENT_ID_ADAPTER.validate_python(doc_id)
            if document_id is not None and doc_id is not None:
                raise AmbiguousDocumentIdentifierError(
                    tool="read_section", canonical="document_id", alias="doc_id"
                )
            if document_id is None and doc_id is None:
                raise MissingDocumentIdentifierError(
                    tool="read_section", accepted=["document_id", "doc_id"]
                )
            resolved_document_id = document_id if document_id is not None else doc_id
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            response = await v.utilities_service.read_section(resolved_document_id, heading_path)
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def list_headings(
        vault_id: str,
        document_id: str | None = None,
        doc_id: str | None = None,
    ) -> dict:
        """List all heading paths for a document in document order.

        Returns the structural table of contents (heading paths only) without
        reading body content. Use this to verify a document's structure or
        pick a heading path before calling read_section.

        Replaces the antipattern of calling read_section with a
        deliberately wrong heading path to harvest ``available_headings``
        from the resulting ``heading_not_found`` error response. The
        synthetic header chunk is excluded, so the returned paths
        are exactly those a caller may pass to read_section.

        Args:
            vault_id: Target vault identifier.
            document_id: The document's unique identifier. Alias: ``doc_id``.
                Supply exactly one of ``document_id`` or ``doc_id``.
            doc_id: Alias for ``document_id``; supply exactly one.
        """
        try:
            # See get_document: validate each id param by literal name for the
            # typed-alias gate, then resolve the alias and surface the
            # ambiguous/missing errors before any service call.
            if document_id is not None:
                document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            if doc_id is not None:
                doc_id = _DOCUMENT_ID_ADAPTER.validate_python(doc_id)
            if document_id is not None and doc_id is not None:
                raise AmbiguousDocumentIdentifierError(
                    tool="list_headings", canonical="document_id", alias="doc_id"
                )
            if document_id is None and doc_id is None:
                raise MissingDocumentIdentifierError(
                    tool="list_headings", accepted=["document_id", "doc_id"]
                )
            resolved_document_id = document_id if document_id is not None else doc_id
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            response = await v.utilities_service.list_headings(resolved_document_id)
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(name="admin_recompute_views")
    async def recompute_views(vault_id: str) -> dict:
        """Regenerate browsable symlink views (by_doc_type/, by_lifecycle/)
        in the vault's storage root.

        Drops and recreates the symlink trees under the vault's
        ``storage_root``. Useful after bulk metadata updates that moved
        documents between doc_type or lifecycle buckets. The views are for
        human file-browser navigation; no SAGE tool consumes them.

        ``doc_type=None`` exclusion: documents whose ``doc_type`` is null are
        silently omitted from ``by_doc_type/`` (no ``<null>/`` bucket). The
        sibling ``by_lifecycle/`` never drops, since every document has a
        non-null ``lifecycle_status``. A document present in the graph but
        absent from ``by_doc_type/`` is the signal that its ``doc_type`` is
        unset — patch via ``update_metadata`` and re-call.

        Wipe-then-rebuild is NOT atomic: ``{storage_root}/views/`` is removed
        in full and then rebuilt from the current document list. A
        mid-rebuild failure (permission denial, missing symlink target)
        leaves ``views/`` partially regenerated with no rollback; recovery is
        a re-call once the cause is addressed. On an empty (or fully
        filtered-out) vault the wipe runs and ``views/`` is not recreated;
        the response carries ``views_generated=0`` — indistinguishable from
        "every document filtered out", so check ``admin_get_vault_stats`` if
        the distinction matters.

        Error modes:
        - ``invalid_vault_id`` (400): ``vault_id`` failed typed-alias
          validation at the boundary.
        - ``unknown_vault`` (404): ``vault_id`` is not a registered vault.
          Detail enumerates the available vaults.

        Args:
            vault_id: Target vault identifier.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            response = await v.utilities_service.refresh_views()
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    # -------------------------------------------------------------------
    # SAGE API tools for CAS Application (MCP-001 through MCP-014)
    # -------------------------------------------------------------------

    @mcp.tool(name="admin_list_vaults")
    async def list_vaults() -> dict:
        """Enumerate all configured vaults. No vault_id parameter -- operates
        across all registered vaults.
        """
        try:
            summaries = await get_vault_registry_service().list_vaults()
            return {
                "vaults": [
                    {
                        "id": s.id,
                        "name": s.name,
                        "description": s.description,
                        "storage_root": s.storage_root,
                    }
                    for s in summaries
                ],
                "count": len(summaries),
            }
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(name="admin_create_vault")
    async def create_vault(config: dict) -> dict:
        """Create a new vault and register it with the running SAGE instance.

        Pass a complete vault config dict. It is validated against the vault
        config schema, directories are created, ``vault_config.yaml`` is
        written under the vault root (default ``~/sage_vaults/<vault_id>/``),
        services are initialized, and the vault is registered immediately
        (no restart). The full written config is echoed back so the caller
        can follow up with ``admin_update_vault_config`` without a separate
        read.

        Config dict structure: the ``config`` parameter is opaque at the MCP
        boundary (typed ``dict``); its shape lives in
        ``docs/fs/sage/vault_config.schema.json``. Six top-level sections are
        required (``vault``, ``document_types``, ``lifecycle``,
        ``source_adapters``, ``metadata_extraction``, ``edge_inference``) and
        three optional (``abstraction``, ``access_control_defaults``,
        ``retrieval_health``). A minimal default is available from
        ``VaultRegistryService.get_default_config(vault_id, name, owner)``.

        The new vault inherits the running process's stack-wide
        abstraction-provider singleton (built once at startup); the vault
        config's ``abstraction`` section governs only enable/disable and
        per-vault parameters, not provider identity. A different provider
        requires a stack-config edit and process restart.

        Creation is not atomic: it runs five sequential steps (config
        directory, yaml write, service init, registry insertion, owner
        bootstrap) with no cross-step rollback. A mid-sequence failure can
        leave ``~/sage_vaults/{vault_id}/`` present with a partial yaml while
        the registry has no entry; recovery is to remove the directory and
        re-call. The final step bootstraps the owner user (required for
        subsequent access-controlled operations); the response carries only
        the ``VaultSummary`` plus the echoed config, with no field signaling
        the owner insert.

        A doc_type's ``metadata_schema`` is compiled into a JSON Schema
        validator at create time, not first ingest, so a malformed schema
        (non-Draft 2020-12, unresolvable ``$ref``) surfaces here as
        ``vault_config_validation_error`` rather than on the first ingest
        that would exercise it.

        Error modes:
        - ``vault_already_exists`` (409): a vault with that ``vault_id`` is
          already registered.
        - ``vault_config_validation_error`` (400): the config fails schema
          validation — missing/malformed top-level sections, or a malformed
          ``document_types.doc_types[].metadata_schema``.

        Args:
            config: Full vault config dict, validating against
                ``docs/fs/sage/vault_config.schema.json`` (six required
                top-level sections plus three optional).
        """
        try:
            summary = await get_vault_registry_service().create_vault(
                CreateVaultRequest(config=config)
            )
            return {
                "vault_id": summary.id,
                "name": summary.name,
                "storage_root": summary.storage_root,
                "config": config,
            }
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(name="admin_get_vault_config")
    async def get_vault_config(vault_id: str) -> dict:
        """Return the full vault configuration as a dict.

        This is the authoritative source for vault-config-defined
        vocabulary that other tools depend on. Read this when you need:

        - The valid ``action`` vocabulary for ``update_lifecycles``
          (under ``lifecycle.transitions``; each entry includes
          ``from_state``, ``action``, ``to_state``, ``creates_edge``).
        - The valid ``doc_type`` values for ``update_metadata``
          or for filtering ``search`` (under
          ``document_types.doc_types``).
        - The enabled source adapters for ``ingest_document`` / ``get_filename_metadata``
          (under ``source_adapters.adapters``).
        - The filename-parsing pattern and segment fields used by
          ``get_filename_metadata`` (under
          ``metadata_extraction.filename_extraction``).
        - The edge inference tier assignments and inference rules
          relevant to ``list_staging_edges`` (under
          ``edge_inference.tier_assignments``).

        The returned dict is the live in-memory config; on-disk edits
        to ``vault_config.yaml`` are not picked up until
        ``admin_reload_vault`` is called.

        Args:
            vault_id: Target vault identifier.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            services = get_vault(vault_id)
            return services.vault_config_service.get_config()
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(name="admin_update_vault_config")
    async def update_vault_config(
        vault_id: str,
        vault: dict | None = None,
        document_types: dict | None = None,
        lifecycle: dict | None = None,
        source_adapters: dict | None = None,
        metadata_extraction: dict | None = None,
        edge_inference: dict | None = None,
        abstraction: dict | None = None,
        access_control_defaults: dict | None = None,
        retrieval_health: dict | None = None,
        force: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Update vault configuration at the section level.

        Each non-null section argument replaces the corresponding top-level
        config section wholesale; sections left None are preserved.
        Partial-section merges are not supported — passing
        ``document_types={"doc_types": [...]}`` replaces the entire
        ``document_types`` section, so include every key you want to keep.

        If the merged config would remove a doc_type or lifecycle state that
        still has documents attached, the update is rejected with
        ``destructive_config_change`` and the affected counts in the detail.
        Pass ``force=True`` to proceed anyway; the warnings then appear in
        the success response. Changing ``vault.id`` is never permitted
        regardless of force — use ``admin_create_vault`` for a new vault.

        The update writes to disk and updates the running config in place;
        subsequent calls see the new vocabulary immediately. The
        write-then-reload sequence is atomic: the reload builds new services
        before tearing down the old, and if any step raises (schema
        migration required, duplicate edges, abstraction-provider build
        failure) the yaml is rolled back to its pre-call bytes and the
        previous config keeps serving. ``admin_reload_vault`` is needed only
        when an external process edited the yaml.

        Error modes:
        - ``invalid_vault_id`` (400): ``vault_id`` failed typed-alias
          validation at the boundary.
        - ``destructive_config_change`` (409): see above.
        - ``vault_config_validation_error`` (400): the merged config fails
          schema validation, or the request attempts to change ``vault.id``.

        Dry-run: ``dry_run=true`` validates the merged config and previews
        which sections would change without writing yaml or reloading. The
        response carries ``status="previewed"``, ``dry_run=true``,
        ``warnings`` (dry-run never raises ``destructive_config_change``),
        and ``preview.changed_sections``. ``force`` is a no-op on dry-run.

        Args:
            vault_id: Target vault identifier.
            vault: Replacement for the vault identity section.
            document_types: Replacement for the document_types section.
            lifecycle: Replacement for the lifecycle section.
            source_adapters: Replacement for the source_adapters section.
            metadata_extraction: Replacement for the metadata_extraction section.
            edge_inference: Replacement for the edge_inference section.
            abstraction: Replacement for the abstraction section.
            access_control_defaults: Replacement for the access_control_defaults section.
            retrieval_health: Replacement for the retrieval_health section.
            force: When True, proceed even if the update would orphan
                existing documents. Default False.
            dry_run: When True, preview the change
                (``preview.changed_sections``) without persisting; never
                raises destructive_config_change. Default False.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            services = get_vault(vault_id)
            body = UpdateVaultConfigRequest(
                vault=vault,
                document_types=document_types,
                lifecycle=lifecycle,
                source_adapters=source_adapters,
                metadata_extraction=metadata_extraction,
                edge_inference=edge_inference,
                abstraction=abstraction,
                access_control_defaults=access_control_defaults,
                retrieval_health=retrieval_health,
                dry_run=dry_run,
            )
            return serialize(
                await services.vault_config_service.update_config(vault_id, body, force)
            )
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(name="admin_get_vault_stats")
    async def get_vault_stats(vault_id: str) -> dict:
        """Vault statistics and health indicators.

        Returns aggregate counts and health summaries for the vault,
        including total document count, counts per lifecycle state,
        counts per doc_type, counts per pipeline_status, and any
        registered retrieval-health checks (if
        ``retrieval_health`` is configured in vault_config). Inexpensive;
        safe to poll.

        Args:
            vault_id: Target vault identifier.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            services = get_vault(vault_id)
            stats = await services.vault_config_service.get_stats()
            return serialize(stats)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(name="verify_hashes")
    async def verify_hash(vault_id: str, hashes: list[str]) -> dict:
        """Bulk hash existence check against the graph store.

        For each input hash, returns whether an existing document in the
        vault carries that content hash and, if so, the matching document's
        id and source_path. Used by the scan-and-batch-ingest flow to
        identify already-ingested files without re-hashing on the SAGE side.
        The response is a dict keyed by input hash; missing hashes are simply
        absent (see *Malformed hashes* for why absent-vs-present is
        load-bearing).

        Hash format: the canonical form is the prefixed ``sha256:<hex>``. The
        MCP transport also accepts bare hex (the form ``ingest_document``
        emits) without rewriting, so ingest results round-trip directly.
        Output records carry the prefixed form.

        Empty-list short-circuit: ``hashes=[]`` returns an empty result dict
        without consulting the graph store — indistinguishable from "every
        queried hash is unknown", so callers that branch on result emptiness
        should also branch on input emptiness.

        Malformed hashes: hash strings bypass validation so the bare-hex form
        can round-trip. Malformed inputs (truncated, non-hex, wrong length)
        are NOT rejected — they reach the lookup as-is, miss every row, and
        surface as ``exists=False`` entries indistinguishable from
        well-formed-but-unknown hashes. Callers relying on "valid format
        implies in-store" must pre-validate input shape themselves.

        Args:
            vault_id: Target vault identifier.
            hashes: List of content hash strings (``sha256:<hex>`` or bare
                hex). An empty list short-circuits; malformed entries
                silently surface as ``exists=False`` (see above).
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            services = get_vault(vault_id)
            # Skip Sha256Str validation: the MCP transport historically
            # accepts bare-hex hashes (the form ingest_document emits in its
            # response) in addition to the prefixed form the REST request
            # schema requires. Normalizing the two storage formats is a
            # separate concern from.
            body = HashCheckRequest.model_construct(hashes=hashes)
            matches = await services.vault_config_service.hash_check(body)
            return {h: m.model_dump(exclude_none=True) for h, m in matches.items()}
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def list_staging_edges(vault_id: str) -> dict:
        """List Tier 2 suggested edges awaiting review.

        SAGE's edge-inference subsystem runs edges through tiers
        defined in ``edge_inference.tier_assignments``:

        - **Tier 1** (e.g. ``supersedes`` via version_chain, ``sync_target``
          via re_ingestion): high-confidence inferences. Created
          directly as production edges; do not appear here.
        - **Tier 2** (e.g. ``references`` via content_reference): inferred
          edges that require human review. Land in the staging-edge
          table; surfaced by this tool until confirmed or dismissed.
        - **Tier 3**: agent-supplied edges (``derived_from``,
          ``depends_on``); not inferred, so do not pass through staging.

        Each staging edge carries the source/target ids, edge_type,
        and the inference rule + evidence that produced it. The result
        list groups by source document
        (``staging_review_grouping=by_source_document``) so a reviewer
        can sweep all candidate edges from one document together.

        Args:
            vault_id: Target vault identifier.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            edges = await v.staging_edges_service.list_staging_edges()
            items = [e.model_dump(mode="json") for e in edges]
            return {
                "items": items,
                "count": len(items),
                "vault_id": vault_id,
                "status": "awaiting_review" if items else "no_staging_edges",
            }
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def update_staging_edge(vault_id: str, edge_id: str, action: str) -> dict:
        """Confirm or dismiss a staging edge.

        Dispatches by ``action``:

        - ``action="confirm"``: promote the staging edge to production. The
          staging row is deleted and a new production edge is inserted with
          the same source, target, and edge_type. The returned envelope
          carries the production ``edge_id``, distinct from the staging id
          passed in — staging and production tables do not share an id space.
        - ``action="dismiss"``: delete the staging edge without creating a
          production edge. The inference rule is not re-applied for the same
          (source, target, edge_type) during the current ingest cycle, but a
          future re-ingest that re-triggers it will re-stage the candidate.

        Confirm idempotency on natural-key collision: on ``confirm``, if the
        staging edge's natural-key triple ``(source_id, target_id,
        edge_type)`` already exists in production — e.g. a parallel
        ``create_edges`` or an earlier auto-inference already created it —
        confirm silently returns the existing production edge's id rather
        than raising, and the staging row is consumed either way. A caller
        cannot distinguish "I created it" from "I just consumed my staging
        row"; both surface as a successful confirm with a populated
        ``production_edge_id``.

        Insert-then-delete atomicity gap: confirm sequences insert then
        delete-staging without a single wrapping transaction. If the delete
        fails after the insert succeeds, the staging row persists alongside
        the new production edge until a subsequent confirm consumes the
        orphan (itself a silent-idempotent no-op per the rule above). Treat
        confirm as "at-least-once" for the production-edge insert and rely on
        the natural-key UNIQUE constraint plus idempotency to absorb retries.

        Error modes:
        - ``invalid_vault_id`` (400): ``vault_id`` failed typed-alias
          validation.
        - ``invalid_edge_id`` (400): ``edge_id`` failed typed-alias
          validation.
        - ``staging_edge_not_found`` (404): the id is unknown (already
          confirmed, already dismissed, or never existed).
        - ``invalid_action`` (400): ``action`` is not ``"confirm"`` or
          ``"dismiss"``.

        Args:
            vault_id: Target vault identifier.
            edge_id: Staging edge identifier (from ``list_staging_edges``).
            action: One of ``"confirm"`` or ``"dismiss"``. On ``"confirm"``,
                natural-key-collision behavior and the insert/delete
                atomicity gap are governed by the paragraphs above.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            edge_id = _EDGE_ID_ADAPTER.validate_python(edge_id)
            if action not in ("confirm", "dismiss"):
                raise ValueError(
                    f"invalid_action: action must be 'confirm' or 'dismiss', got {action!r}"
                )
            v = get_vault(vault_id)
            if action == "confirm":
                return serialize(await v.staging_edges_service.confirm_staging_edge(edge_id))
            return serialize(await v.staging_edges_service.dismiss_staging_edge(edge_id))
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def list_pending_metadata(vault_id: str) -> dict:
        """List documents with unconfirmed metadata.

        A document is "pending" when its ``metadata_confirmed`` flag is
        false. This typically arises from ``ingest_document(needs_review=true)``:
        the caller deferred metadata to filename inference, which populated
        omitted fields and held the document for review. The pending state is
        cleared on any ``update_metadata`` call against the document (even a
        single-field update).

        For the default ``ingest_document`` path (``needs_review=false``),
        documents land with ``metadata_confirmed=true`` and never appear
        here.

        Args:
            vault_id: Target vault identifier.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            docs = await v.graph_store.list_pending_metadata_documents()
            items = []
            for doc in docs:
                items.append(
                    {
                        "document": serialize(doc),
                        "extracted_fields": {},
                    }
                )
            return {
                "items": items,
                "count": len(items),
                "vault_id": vault_id,
                "status": "pending_review" if items else "no_pending_metadata",
            }
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def recompute_abstract(
        vault_id: str,
        document_id: str,
    ) -> dict:
        """Re-run abstraction on an existing document (fire-and-forget).
        Reconstructs projection text from stored chunks and dispatches a new
        semantic abstract as a background task; the abstract is written to
        the document node by that task, not by this call.

        Generation uses the SAGE stack's configured abstraction provider and
        model (``abstraction`` in ``sage/config.yaml``; the model identifier
        is stack-wide, not per-vault). If the new abstract is still
        off-topic, the lever is a stack-config change, not a re-issue of this
        tool.

        Fire-and-forget: this call validates the document, flips
        ``pipeline_status=abstraction_in_progress``, dispatches the
        abstraction work as a background task, and returns immediately with::

            {"status": "reabstract_started",
             "document_id": "<id>",
             "dispatched_at": "<iso8601 timestamp>"}

        The background task generates and persists ``semantic_abstract`` and
        flips ``pipeline_status`` to ``abstraction_complete`` (success) or
        ``failed`` (error). To observe terminal state, poll ``get_document``
        until ``pipeline_status`` is no longer ``abstraction_in_progress``.
        A caller that assumes this tool returns the new abstract in place
        will observe stale state.

        Per-document single-flight lock: a concurrent call against the same
        ``document_id`` while a reabstract is in-flight returns a structured
        409 (``reabstract_document_already_in_flight``) rather than
        dispatching a parallel task; the reservation releases when the task
        reaches terminal state. Calls against different document_ids run in
        parallel.

        Process-crash recovery: a process-level kill (SIGKILL, OOM) during a
        background reabstract leaves the document stuck at
        ``abstraction_in_progress`` with no terminal stamp. After restart,
        enumerate stuck docs via ``search(mode="catalog",
        filters={"pipeline_status": "abstraction_in_progress"})`` and
        re-issue ``recompute_abstract`` against each.

        Error modes (raised synchronously in this call's response;
        background-task failures are NOT surfaced here — they manifest as
        ``pipeline_status=failed`` with ``pipeline_error`` populated,
        observable via ``get_document``):
        - ``invalid_vault_id`` (400): ``vault_id`` failed typed-alias
          validation at the boundary.
        - ``invalid_document_id`` (400): ``document_id`` failed typed-alias
          validation at the boundary.
        - ``document_not_found`` (404): no document with that id.
        - ``no_projection`` (404): the document has no stored chunks to
          abstract from.
        - ``reabstract_document_already_in_flight`` (409): a reabstract is
          already running on this ``document_id``. ``detail`` carries
          ``document_id`` and the in-flight call's ISO 8601 ``start_time``.

        Args:
            vault_id: Target vault identifier.
            document_id: Document to re-abstract.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            v = get_vault(vault_id)
            result = await v.ingestion_service.reabstract(document_id)
            return serialize(result)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def recompute_pipeline(
        vault_id: str,
        document_id: str,
    ) -> dict:
        """Re-run the full ingestion pipeline against an existing document
        (fire-and-forget). Operator repair for documents stuck at
        ``pipeline_status=projection_complete`` with no chunks — the
        silent-loss state when a Stage 2 background dispatch is
        garbage-collected or its host process dies mid-execution.

        Stage 1 (projection) re-runs synchronously from
        ``document.source_path`` so adapter / source-file errors surface in
        this call's response rather than as a ``pipeline_status=failed``
        stamp. Stages 2-3 (indexing, abstraction) then dispatch as a
        background task whose strong reference is held until terminal,
        closing the garbage-collection window.

        Fire-and-forget: this call returns immediately after Stage 1 +
        dispatch with::

            {"status": "recompute_pipeline_started",
             "document_id": "<id>",
             "dispatched_at": "<iso8601 timestamp>"}

        The background task re-indexes the chunks, regenerates the abstract,
        and flips ``pipeline_status`` to ``abstraction_complete`` /
        ``abstraction_skipped`` (success) or ``failed`` (Stage 2/3 error). To
        observe terminal state, poll ``get_document`` until
        ``pipeline_status`` is no longer ``indexing_in_progress`` or
        ``abstraction_in_progress``.

        Per-document single-flight lock: a concurrent call against the same
        ``document_id`` while a recompute is in-flight returns a structured
        409 (``recompute_pipeline_already_in_flight``) rather than
        dispatching a parallel task. Calls against different document_ids run
        in parallel.

        Process-crash recovery: after a process-level kill (SIGKILL, OOM)
        interrupted a prior recompute or ingest mid-Stage-2, enumerate stuck
        docs via ``search(mode="catalog", filters={"pipeline_status":
        "projection_complete"})`` and re-issue ``recompute_pipeline`` against
        each.

        Error modes (raised synchronously in this call's response;
        background-task failures are NOT surfaced here — they manifest as
        ``pipeline_status=failed`` with ``pipeline_error`` populated,
        observable via ``get_document``):
        - ``internal_error`` (500): ``vault_id`` or ``document_id`` failed
          typed-alias validation at the boundary.
        - ``unknown_vault``: ``vault_id`` is not a registered vault.
        - ``document_not_found`` (404): no document with that id.
        - ``adapter_not_found`` (400): no source adapter for the document's
          ``source_type``.
        - ``source_file_not_found`` (404): the document's ``source_path`` no
          longer resolves to a readable file.
        - ``recompute_pipeline_already_in_flight`` (409): a recompute is
          already running on this ``document_id``. ``detail`` carries
          ``document_id`` and the in-flight call's ISO 8601 ``start_time``.

        Args:
            vault_id: Target vault identifier.
            document_id: Document to re-run the pipeline against.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            v = get_vault(vault_id)
            result = await v.ingestion_service.recompute_pipeline(document_id)
            return serialize(result)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    # -------------------------------------------------------------------
    # SAGE admin / maintenance API tools (CAS-ADR-029)
    #
    # Family-shared preconditions for every admin-family tool below:
    #
    # 1. ``vault_id`` is validated through the ``VaultIdStr`` typed alias
    # (``_VAULT_ID_ADAPTER.validate_python``) before any vault lookup.
    # Inputs that violate the typed-alias shape raise a structured
    # ``ValueError`` rather than reaching the registry. See the CAS
    # Typed-Alias Boundary Conventions for the shared validation
    # contract.
    #
    # 2. The targeted vault must have been initialized with a
    # ``registry_service``; otherwise ``v.maintenance_service`` is
    # ``None`` and the tool raises ``RuntimeError``. This is primarily
    # a test-fixture concern (production vault construction wires
    # ``registry_service`` by default), but agents and integration
    # tests that build vaults directly without the registry will hit
    # this error rather than a silent no-op. The maintenance/admin
    # API surface is governed by CAS-ADR-029.
    #
    # These two preconditions apply to every admin-family tool below;
    # the per-tool docstrings surface only the caller-facing error codes
    # they produce (e.g. ``invalid_vault_id``, ``vault_not_found``).
    # -------------------------------------------------------------------

    @mcp.tool(name="admin_migrate_vault")
    async def migrate_vault(vault_id: str) -> dict:
        """Run the schema-migration surface's tier3-uniqueness scan for a vault.

        The durable store provisions its schema externally, so there is no
        pending schema or backfill work for this tool to apply: it always
        returns an empty no-op report (``columns_added`` and
        ``backfills_applied`` both empty). Idempotent for the same reason —
        a re-call returns the same empty shape with no error.

        tier3 uniqueness activation: every ``unique_keys`` declaration in
        vault config is scanned. Clean declarations get partial UNIQUE
        indexes installed; declarations whose existing data violates the
        constraint are recorded in ``tier3_uniqueness_collisions``, the index
        is not activated (see ``Tier3UniqueIndexBlockedError`` below), and any
        previously-clean index is preserved (no implicit DROP). Activated
        declarations are listed in ``tier3_uniqueness_activations``.
        **Callers must inspect both fields** on every call, no-op or not.
        Query ``admin_get_vault_config`` for the ``unique_keys`` declarations.

        Error modes:
        - ``vault_not_found`` (404): no vault registered with that id.
        - ``Tier3UniqueIndexBlockedError``: a ``unique_keys`` declaration's
          existing data violates the constraint, so its partial UNIQUE index
          cannot be installed; the collisions are captured in
          ``tier3_uniqueness_collisions`` and not auto-resolved.

        Args:
            vault_id: Target vault identifier.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            if v.maintenance_service is None:
                raise RuntimeError(
                    f"Vault {vault_id!r} was initialized without a "
                    "registry_service; maintenance_service is unavailable."
                )
            report = await v.maintenance_service.migrate_vault()
            return serialize(report)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(name="admin_verify_vault_drift")
    async def verify_vault_drift(vault_id: str) -> dict:
        """Audit active sync_target / derived_from edges for drift.

        Walks every active provenance-bearing edge in the vault and compares
        its recorded ``synced_from_*`` fields against the current head of the
        source's supersedes chain. Returns a DriftReport whose ``entries``
        enumerate edges needing operator attention; current edges are absent.
        Hash is the authoritative comparator; ``synced_from_version`` is a
        display key.

        ``StalenessBasis`` buckets — each ``DriftEntry`` carries a
        ``staleness_basis`` classifying why the edge surfaced:

        - ``content_drift``: the recorded ``synced_from_content_hash``
          differs from the current chain-head hash. The "stale, act now"
          signal — re-sync the dependent artifact.
        - ``chain_advanced_no_content_change``: the chain advanced past the
          recorded version but the head's content hash still matches.
          Informational — the pointer is behind but the bytes are equivalent.
        - ``recorded_null``: the edge predates the provenance columns
          (neither ``synced_from_version`` nor ``synced_from_content_hash``
          recorded). Informational — back-filling is optional cleanup.
        - ``chain_nonlinear``: the source's supersedes chain forks (more than
          one head). Data-quality flag, not a drift signal; reconcile the
          chain first. ``current_head_*`` is null; ``competing_head_count``
          is populated.

        Error modes:
        - ``vault_not_found`` (404): no vault registered with that id.
        - ``chain_nonlinear`` (reported as ``DriftEntry`` rows, not an
          envelope error, per the bucket above, so one forked chain does not
          mask drift on other edges).
        - Graph-store query failures (500): unexpected storage errors while
          walking edges or resolving chain heads — infrastructure
          conditions, retrying is appropriate.

        Args:
            vault_id: Target vault identifier.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            if v.maintenance_service is None:
                raise RuntimeError(
                    f"Vault {vault_id!r} was initialized without a "
                    "registry_service; maintenance_service is unavailable."
                )
            report = await v.maintenance_service.detect_drift()
            return serialize(report)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(name="admin_verify_vault_source_files")
    async def verify_vault_source_files(vault_id: str, check_hashes: bool = False) -> dict:
        """Audit that every document's backing source file is present.

        Walks every document in the vault and checks that its
        ``source_path`` resolves to an existing file under the vault
        storage root. Returns a SourceFileIntegrityReport whose
        ``entries`` enumerate documents whose source file is missing;
        documents with an intact source file are absent. Read-only —
        mutates nothing.

        When ``check_hashes`` is true, each present file's SHA-256 is
        recomputed and compared against the recorded
        ``source_content_hash``; a divergent file surfaces as a
        ``hash_mismatch`` entry (a full file read per document). Default
        false performs an existence check only.

        Note: this audits the vault-local source files (the ``imports/``
        copies that ``get_document`` delivers), distinct from the content
        store that ``admin_optimize_vault_content_store`` reclaims.

        Error modes:
        - ``vault_not_found`` (404): no vault registered with that id.

        Args:
            vault_id: Target vault identifier.
            check_hashes: Recompute and compare on-disk hashes when true;
                existence check only when false (default).
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            if v.maintenance_service is None:
                raise RuntimeError(
                    f"Vault {vault_id!r} was initialized without a "
                    "registry_service; maintenance_service is unavailable."
                )
            report = await v.maintenance_service.verify_vault_source_files(
                check_hashes=check_hashes
            )
            return serialize(report)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(name="admin_recompute_deferred_vault_abstracts")
    async def recompute_deferred_vault_abstracts(vault_id: str, include_pdf: bool = False) -> dict:
        """Backfill semantic abstracts for documents whose pipeline_status is abstraction_skipped.

        Enumerates documents in the named vault at
        ``pipeline_status=abstraction_skipped``, dispatches a reabstract per
        document, and polls until each reaches terminal status
        (``abstraction_complete`` or ``failed``). Returns a ReabstractReport
        with per-document outcomes and aggregate counts.

        Reuses the in-process AbstractionProvider this MCP server loaded at
        startup; does NOT spin up a second Qwen3 instance. The standalone
        ``scripts/reabstract_deferred.py`` remains the operator fallback for
        cron-style workflows where no MCP server is running.

        Single-flight per vault: a concurrent call returns a structured
        ``reabstract_already_in_flight`` (409) whose detail carries the
        in-flight operation's ``start_time``.

        Long-running: an N-document pass takes roughly N times the
        per-document abstraction wall-clock (seconds to tens of seconds each
        against Qwen3-30B MLX, sub-second against the test stub). The tool
        returns a single ReabstractReport once the pass completes; allocate a
        generous client-side timeout. (The HTTP route streams per-document
        SSE progress; the MCP contract is report-and-return.)

        Error modes:
        - ``vault_not_found`` (404): no vault registered with that id.
        - ``reabstract_already_in_flight`` (409): a reabstract is already
          running on this vault.
        - ``RuntimeError``: the vault's ``MaintenanceService`` was
          constructed without an ``ingestion_service`` dependency
          (test-fixture concern; production wiring supplies it).

        Args:
            vault_id: Target vault identifier.
            include_pdf: When False (default), source_type=pdf documents are
                skipped (scanned PDFs typically have no extractable text).
                When True, PDFs are included in the worklist.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            if v.maintenance_service is None:
                raise RuntimeError(
                    f"Vault {vault_id!r} was initialized without a "
                    "registry_service; maintenance_service is unavailable."
                )
            report = await v.maintenance_service.reabstract_deferred(include_pdf=include_pdf)
            return serialize(report)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(name="admin_optimize_vault_content_store")
    async def optimize_vault_content_store(vault_id: str, cleanup_older_than_days: int = 7) -> dict:
        """Reclaim bloat in the per-vault content store.

        Wraps a ``VACUUM (FULL, ANALYZE)`` against the vault's chunks table:
        removes dead tuples, returns free space to the OS, and shrinks the
        relation. Postgres MVCC writes a new row version on every update or
        delete rather than reclaiming space in place, so disk usage on
        actively-churned vaults grows until this is called.

        Runs on its own autocommit connection (VACUUM cannot run inside a
        transaction block) and holds no lock that blocks concurrent reads or
        writes; a first run against a highly-churned vault may still take
        minutes and exceed an MCP client timeout, with subsequent runs
        settling to seconds.

        Returns an OptimizeContentStoreReport with pre/post observations
        (relation byte size, retained version count, fragment counts) — the
        caller-visible evidence of reclamation, since the underlying
        operation itself returns nothing. ``cleanup_older_than_days`` has no
        Postgres analog (VACUUM reclaims every eligible dead tuple
        regardless of age) and is accepted only for the port contract; it is
        echoed for audit-log alignment.

        Error modes:
        - ``vault_not_found`` (404): no vault registered with that id.
        - ``ValueError``: ``cleanup_older_than_days`` is negative.

        Args:
            vault_id: Target vault identifier.
            cleanup_older_than_days: Accepted for the port contract; has no
                effect on the Postgres binding (see above).
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)
            if v.maintenance_service is None:
                raise RuntimeError(
                    f"Vault {vault_id!r} was initialized without a "
                    "registry_service; maintenance_service is unavailable."
                )
            report = await v.maintenance_service.optimize_content_store(
                cleanup_older_than_days=cleanup_older_than_days
            )
            return serialize(report)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    # -------------------------------------------------------------------
    # Server-level operational tools (no HTTP counterpart by design)
    # -------------------------------------------------------------------

    @mcp.tool(name="admin_reload_vault")
    async def reload_vault(vault_id: str) -> dict:
        """Reload a vault by closing its current services and reinitializing.

        When the vault was loaded from a YAML file (the production path), the
        file is re-read from disk so on-disk edits to vault_config.yaml take
        effect. Vaults initialized from an in-memory ``VaultConfig`` (e.g. in
        tests) reuse the existing config.

        Use this after editing vault_config.yaml on disk, or when external
        database changes (the FastAPI server, another MCP client, direct DB
        writes) have left this MCP session with stale data.

        Scope is per-vault, NOT stack-wide: only the target vault's
        ``vault_config.yaml`` is re-read. The stack-wide config
        (``sage/config.yaml``, governing the abstraction-provider singleton)
        is captured at process startup and requires a process restart to
        change; verify it via ``admin_get_stack_config`` if you suspect
        drift.

        Reload is atomic with respect to the registry slot: new services are
        built first and the old ones are torn down only on success. If
        construction raises (schema migration, duplicate edges,
        abstraction-provider build failure), the slot keeps pointing at the
        still-functional old services and an error envelope is returned; the
        caller can retry after addressing the cause.

        Error modes (the registry slot is preserved on every failure path;
        the old services stay installed and the caller can retry):
        - ``invalid_vault_id`` (400): ``vault_id`` failed typed-alias
          validation at the boundary.
        - ``unknown_vault`` (404): ``vault_id`` is not a registered vault.
          Detail enumerates the available vaults.
        - ``schema_migration_required`` (409): the vault's ``graph.db`` has
          pending migrations or backfills, so the new graph store cannot
          ``initialize(migrate=False)``. Run ``admin_migrate_vault`` first.
        - ``duplicate_edges_present`` (409): the ``edges`` or
          ``staging_edges`` table has duplicate rows on the natural-key
          triple ``(source_id, target_id, edge_type)``, so UNIQUE index
          creation fails. Dedupe the offending table before retrying.
        - Abstraction-provider build failure: reload builds the provider
          from the current in-memory stack config; e.g.
          ``provider="local-mlx"`` with ``model=None`` raises ``ValueError``.
          Verify via ``admin_get_stack_config`` if you suspect drift.

        Args:
            vault_id: Target vault identifier.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            # ``get_vault`` raises ``VaultNotFoundError`` (a ``ValueError``
            # subclass) when ``vault_id`` is not registered; the
            # ``except (SAGEError, ValueError)`` block below routes that
            # through ``error_response`` as the ``unknown_vault`` envelope.
            old_services = get_vault(vault_id)
            config_path = old_services.config_path
            if config_path is not None:
                # CAS-ADR-043: re-read the declaration through the active
                # profile's vault-source store rather than the filesystem
                # directly, so a non-filesystem binding re-reads from its store.
                from sage.mcp_init import get_stack_config, resolve_stack_vault_source_store
                from sage.vault_source_binding import DiscoveredVault

                store = resolve_stack_vault_source_store(get_stack_config())
                config = store.load_config(DiscoveredVault(config_path=config_path))
            else:
                config = old_services.config

            # Delegate to the registry-aware reload. ``reload_vault_in_registry``
            # builds new services first; only on success does it stop the old
            # timing thread, close the old graph store, and install the new
            # services in the registry. On failure the exception propagates
            # here with the registry untouched, so the live ``_vaults`` dict
            # continues to point at the still-functional old services and the
            # caller can retry. The dict and registry service are resolved
            # via call-time getters; see ``register_sage_tools``' docstring
            # for the rationale (interaction with ``importlib.reload``-based
            # tests that rebind the module-level state).
            new_services = await reload_vault_in_registry(
                get_vaults(),
                vault_id,
                config,
                config_path=config_path,
                registry_service=get_vault_registry_service(),
            )
        except (SAGEError, ValueError) as e:
            return error_response(e)

        # Return confirmation with basic stats
        total_docs = len(await new_services.graph_store.list_all_documents())
        return {
            "vault_id": vault_id,
            "reloaded": True,
            "document_count": total_docs,
        }

    @mcp.tool(name="admin_get_stack_config")
    async def get_stack_config() -> dict:
        """Return the SAGE-stack-wide configuration.

        Stack-wide config governs resources whose enforcement spans the whole
        SAGE process (e.g., the abstraction provider singleton). Per-vault
        knobs live in `admin_get_vault_config`.

        Today the response carries:
          - `profile`: the active deployment-profile marker (e.g. `"local"`),
            the stack-scope selection that co-binds the adapter ports.
          - `abstraction`: with `provider` (dispatch key — `"local-mlx"`,
            `"anthropic"`, or `"stub"`) and `model` (the identifier passed to
            the provider's factory; string, or null when the stack is
            stub-only).

        The shape is forward-compatible: new top-level sections can be added
        without changing the contract of existing callers.
        """
        # Qualified call resolves via the ``sage.mcp_init`` module attribute,
        # bypassing the LEGB lookup that would otherwise rebind to the
        # enclosing ``register_sage_tools`` scope (where this inner function
        # is itself named ``get_stack_config``).
        cfg = sage.mcp_init.get_stack_config()
        return cfg.model_dump(mode="json")

    return {
        "ingest_document": ingest_document,
        "get_filename_metadata": get_filename_metadata,
        "recompute_abstract": recompute_abstract,
        "recompute_pipeline": recompute_pipeline,
        "get_document": get_document,
        "update_metadata": update_metadata,
        "update_lifecycles": update_lifecycles,
        "create_edges": create_edges,
        "delete_edge": delete_edge,
        "verify_preconditions": verify_preconditions,
        "traverse": traverse,
        "chain": chain,
        "search": search,
        "read_projection": read_projection,
        "read_section": read_section,
        "list_headings": list_headings,
        "admin_recompute_views": recompute_views,
        "admin_list_vaults": list_vaults,
        "admin_create_vault": create_vault,
        "admin_get_vault_config": get_vault_config,
        "admin_update_vault_config": update_vault_config,
        "admin_get_vault_stats": get_vault_stats,
        "verify_hashes": verify_hash,
        "list_staging_edges": list_staging_edges,
        "update_staging_edge": update_staging_edge,
        "list_pending_metadata": list_pending_metadata,
        "admin_migrate_vault": migrate_vault,
        "admin_verify_vault_drift": verify_vault_drift,
        "admin_verify_vault_source_files": verify_vault_source_files,
        "admin_recompute_deferred_vault_abstracts": recompute_deferred_vault_abstracts,
        "admin_optimize_vault_content_store": optimize_vault_content_store,
        "admin_reload_vault": reload_vault,
        "admin_get_stack_config": get_stack_config,
    }
