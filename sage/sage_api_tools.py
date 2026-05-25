"""SAGE protocol and API tools for MCP.

Contains all tools that operate directly on the SAGE graph store and
services: protocol tools (ingest, get, update, lifecycle, link, traverse,
discover, export, refresh) and API query tools (vault stats, hash check,
staging edges, pending metadata).
"""

from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from pydantic import TypeAdapter, ValidationError

from sage.api.errors import (
    AmbiguousDocumentIdentifierError,
    LegacyFormError,
    MissingDocumentIdentifierError,
    SAGEError,
    translate_validation_error,
)
from sage.mcp_init import SAGEServices
from sage.models.enums import EdgeType, RationaleKind, RetrievalMode, SourceType
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
    LinkRequest,
    LinkResponse,
    SetLifecycleRequest,
    Sha256Str,
    TraverseRequest,
    UpdateMetadataRequest,
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
    """Raise LegacyFormError when a caller passes the pre-patch shape.

    Catches the two common pre-patch shapes that Pydantic would otherwise
    reject with a generic validation error:
      - tags=["a", "b"]: bare list, the old replacement form.
      - tier3_metadata={"ticket_priority": "high"}: bare dict with no
        recognized patch verb. A dict that contains only the keys
        ``set`` and/or ``unset`` is a valid Tier3Patch; anything else
        is the legacy "this is the new state" form.
    """
    if value is None:
        return
    if field == "tags":
        if isinstance(value, list):
            raise LegacyFormError(
                field="tags",
                received_type="list",
                example='{"add": [...]} or {"remove": [...]}',
            )
    elif field == "tier3_metadata":
        if isinstance(value, dict) and value and not (set(value) <= {"set", "unset"}):
            raise LegacyFormError(
                field="tier3_metadata",
                received_type="dict (bare key/value pairs)",
                example='{"set": {"key": "value"}} or {"unset": ["key"]}',
            )


def register_sage_tools(
    mcp: FastMCP,
    get_vault: Callable[[str], SAGEServices],
    serialize: Callable[[object], dict],
    error_response: Callable[[SAGEError | ValueError], dict],
    vaults: dict[str, SAGEServices],
    vault_registry_service: VaultRegistryService,
) -> dict[str, Callable]:
    """Register all SAGE protocol and API tools on the MCP server.

    Returns a dict mapping tool function names to the actual functions,
    for re-export from mcp_server.
    """

    # -------------------------------------------------------------------
    # SAGE protocol tools
    # -------------------------------------------------------------------

    @mcp.tool()
    async def sage_ingest(
        vault_id: str,
        source: str,
        source_type: str,
        config: dict | None = None,
        created_by: str | None = None,
        force: bool = False,
        predecessor_id: str | None = None,
        needs_review: bool = False,
        metadata: dict | None = None,
        tier3_metadata: dict | None = None,
    ) -> dict:
        """Ingest a source file into SAGE. Runs the three-stage pipeline:
        projection, indexing, and abstraction.

        This tool dispatches Stages 2-3 (indexing, abstraction) as a
        background task and returns in seconds with `pipeline_status`
        typically non-terminal (projection_complete or
        indexing_in_progress). Poll `sage_get_document` to observe
        terminal status (abstraction_complete, abstraction_skipped, or
        failed). The fire-and-forget dispatch keeps the RPC under the
        60-second MCP client timeout for documents whose abstraction
        latency would otherwise exceed it. The supersede lifecycle
        transition (if requested) runs synchronously with record
        insertion, so the version chain is complete when this tool
        returns.

        Per CAS-ADR-021, callers are authoritative for metadata. Pass
        prepared values via `metadata` and leave `needs_review` at the
        default false; the document is committed with caller-supplied
        values authoritative and metadata_confirmed=true. Set
        needs_review=true to defer to the metadata-review queue:
        filename inference runs, parsed values populate fields the
        caller did not supply (the specific fields are vault-config-
        defined; see ``metadata_extraction.filename_extraction`` in
        ``sage_get_vault_config``), and the document is held with
        metadata_confirmed=false until a reviewer confirms via
        sage_update_metadata.

        Trio-field inheritance on supersede (CAS-ADR-021): when
        ``predecessor_id`` is set and the caller omits any of
        ``doc_type``, ``project``, or ``authority_scope`` from
        ``metadata``, the omitted fields inherit from the predecessor's
        value (when non-None). A caller who *wants* to change one of
        these trio fields on a supersede must pass the new value
        explicitly in ``metadata``; otherwise the predecessor's value
        carries forward silently. No error is raised either way --
        inheritance is the documented default, override is the
        opt-in.

        Tier3 uniqueness (CAS-ADR-031, T-0115): doc_types declaring a
        ``unique`` constraint in their ``metadata_schema`` (see
        ``document_types.doc_types[].metadata_schema`` in
        ``sage_get_vault_config``) enforce per-vault uniqueness on the
        named tier3 field at ingest time. In the ``cas`` vault,
        ``ticket.ticket_id`` is the live example: re-ingesting a
        document with a ticket_id already in use raises
        ``tier3_unique_constraint_violation``. Uniqueness is checked
        in the same SQLite transaction as the row insert, so the
        existing document is never disturbed.

        ``pipeline_status`` outcomes (per CAS-ADR-021): the terminal
        status observed by a poll of ``sage_get_document`` depends on
        vault config and runtime outcome. ``abstraction_complete`` is
        the caller-authoritative happy path: projection + indexing +
        abstraction all succeed, ``metadata_confirmed=true``.
        ``abstraction_skipped`` is the deferred-abstraction branch:
        the vault has ``abstraction.enabled=false`` in its config (or
        the projection produced empty text); Stages 1-2 ran but Stage
        3 was bypassed. ``failed`` is the catch-all for any
        Stage-1/2/3 exception; the document persists with
        ``pipeline_error`` populated. Inspect
        ``abstraction.enabled`` in ``sage_get_vault_config`` to know
        which terminal state to expect on a given vault.

        Error modes:
        - ``adapter_not_found`` (400): ``source_type`` is not an enabled
          adapter on this vault. See
          ``source_adapters.adapters`` in ``sage_get_vault_config``.
        - ``source_file_not_found`` (404): ``source`` does not resolve
          to a readable file on disk.
        - ``duplicate_content`` (409): a document with the same
          ``source_path`` and content hash already exists. Override
          with ``force=true`` to re-ingest.
        - ``supersede_target_not_active`` (409):
          ``predecessor_id`` was set but the predecessor is
          not in ``active``. For completed, filed, or otherwise
          non-active predecessors, run the archive -> reactivate dance
          via ``sage_set_lifecycle`` before retrying. See the
          ``sage_set_lifecycle`` docstring for the full pattern.
        - ``identical_content_supersede`` (409): the new file's content
          hash matches the predecessor's; supersede chains require
          distinct content per step.
        - ``tier3_schema_violation`` (400): ``tier3_metadata`` is set
          but the resolved doc_type has no ``metadata_schema`` declared
          in vault config, or the payload failed validation against the
          declared schema. Detail carries ``doc_type``, ``path`` (JSON
          Pointer to the offending field; empty when the doc_type has
          no schema), ``message``, and ``instance`` (the payload that
          failed).
        - ``tier3_unique_constraint_violation`` (409): the resolved
          doc_type declares a ``unique`` constraint on a tier3 field
          (see ``document_types.doc_types[].metadata_schema`` in
          ``sage_get_vault_config``) and ``tier3_metadata`` supplied a
          value already in use by another document. Detail carries
          ``doc_type``, ``field``, ``colliding_value``, and
          ``existing_document_id``. ``force=true`` does NOT override
          this -- uniqueness is independent of content-hash
          deduplication (CAS-ADR-031, T-0115).

        Args:
            vault_id: Target vault identifier.
            source: Source file path relative to the vault's storage_root,
                or an absolute path to an external file. External files are
                copied verbatim into the vault's imports/ directory. The
                vault's internal copy at storage_root/source_path is the
                authoritative file after ingestion; the path passed here
                is temporary and can be deleted by the caller.
            source_type: Source artifact format (markdown, docx, pdf,
                email, onenote, teams_chat). Determines which source
                adapter processes the artifact.
            config: Adapter-specific configuration (optional). Each
                adapter declares its own required-config schema; this
                payload is **not** a SAGE-wide shape. Inspect
                ``source_adapters.adapters[].config`` in
                ``sage_get_vault_config`` for the per-adapter
                required-config shape on the target vault. Caller-
                supplied keys are deep-merged over the vault's
                adapter-config defaults at ingest time; unknown keys
                are rejected by the adapter when it validates the
                merged payload.
            created_by: Creator name. Defaults to vault owner.
            force: Allow re-ingestion of duplicate content.
            predecessor_id: When provided, the ingested document
                supersedes this predecessor. SAGE applies the `supersede`
                lifecycle transition synchronously with record insertion:
                creates a `supersedes` edge (new -> old) and archives
                the predecessor. The predecessor must be active and its
                content hash must differ from the new file. Per
                CAS-ADR-021, the trio fields (doc_type, project,
                authority_scope) inherit from the predecessor when the
                caller omits them and the predecessor's value is
                non-None; to override any trio field on a supersede,
                pass the new value explicitly in ``metadata``. See
                "Trio-field inheritance on supersede" above.
            needs_review: When true, the document enters the
                metadata-review queue (metadata_confirmed=false) and
                filename inference fills in fields the caller did not
                supply. Default false: filename inference is skipped
                and caller metadata is committed authoritatively. Use
                sage_parse_filename ahead of ingest if you want
                filename-based suggestions without entering the review
                queue.
            metadata: Caller-supplied metadata fields applied to the
                document at ingest. Per the CAS-ADR-021 precedence
                chain, caller values win over filename parse, chain
                inheritance, and vault defaults on a per-field basis.
                Recognized keys are the mutable document fields
                (title, version_label, project, doc_type,
                authority_scope, document_date, tags). Tags may be
                supplied as a list of strings or as a comma-separated
                string; whitespace is trimmed and empty fragments are
                dropped in the string form.
            tier3_metadata: Per-doc_type typed metadata payload (T-0004).
                Validated against the JSON Schema fragment declared
                under ``document_types.doc_types[].metadata_schema`` for
                the resolved doc_type (see ``sage_get_vault_config``).
                When the doc_type has no metadata_schema declared and
                this argument is non-null, ingest fails with 400
                ``tier3_schema_violation``. Stored verbatim once
                validated; queryable via ``sage_discover`` filters as
                ``{"tier3_metadata": {"<field>": <value>}}`` with exact
                equality semantics (null matches absent-or-null fields).
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            if predecessor_id is not None:
                predecessor_id = _DOCUMENT_ID_ADAPTER.validate_python(predecessor_id)
            v = get_vault(vault_id)
            request = IngestRequest(
                source=source,
                source_type=source_type,
                config=config,
                created_by=created_by,
                force=force,
                predecessor_id=predecessor_id,
                needs_review=needs_review,
                metadata=metadata,
                tier3_metadata=tier3_metadata,
            )
            # Fire-and-forget pipeline keeps this RPC under the 60s MCP
            # client timeout (BH-130). Callers poll sage_get_document
            # for terminal pipeline_status.
            result = await v.ingestion_service.ingest(request, wait_for_pipeline=False)
            return serialize(result.document)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_parse_filename(
        vault_id: str,
        filename: str,
        source_type: str,
    ) -> dict:
        """Parse a filename's basename through the vault's
        FilenameParser and return the extracted metadata. Side-effect
        free: no document is created and vault state is unchanged.

        Per CAS-ADR-021, this is the agent-facing companion to
        sage_ingest's caller-authoritative metadata flow. Call this
        first to obtain filename-derived suggestions, decide which
        fields to keep, then call sage_ingest with metadata=...
        carrying the resolved values. Fields the parser could not
        extract come back null. When the vault has no
        filename_extraction.pattern configured, all fields are null.

        Which fields the parser extracts is vault-config-defined; see
        ``metadata_extraction.filename_extraction.segment_fields`` in
        ``sage_get_vault_config`` for the active mapping. In the
        ``cas`` vault, the configured pattern is
        ``{date}_{project}_{code}_{title}_{version}``, so the parser
        returns ``doc_date``, ``project``, ``doc_code``, ``title``,
        and ``version``. Other vaults may configure a different
        pattern and emit a different field set.

        Error modes:
        - ``adapter_not_found`` (400): ``source_type`` is not an
          enabled adapter on this vault.

        Args:
            vault_id: Target vault identifier.
            filename: Filename to parse. The basename is preferred;
                directory components are stripped before parsing.
            source_type: Source artifact format (markdown, docx, pdf,
                email, onenote, teams_chat). Must be enabled on the
                vault.
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
    async def sage_get_document(
        vault_id: str,
        document_id: str,
        include_content: bool = False,
        write_to_path: str | None = None,
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
            document_id: The document's unique identifier.
            include_content: When true, add `content` (base64) and
                `content_size` to the response. Default: false.
            write_to_path: Absolute filesystem path. When set, SAGE
                writes bytes there and populates `written_to`,
                `content_size`, and `content_hash` in the response.
                The target must not exist; its parent must exist and
                be writable. Mutually exclusive with `include_content`.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            v = get_vault(vault_id)
            response = await v.documents_service.get_document_with_content(
                document_id, include_content, write_to_path
            )
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_update_metadata(
        vault_id: str,
        document_id: str,
        title: str | None = None,
        version_label: str | None = None,
        project: str | None = None,
        tags: dict | None = None,
        doc_type: str | None = None,
        authority_scope: str | None = None,
        document_date: str | None = None,
        tier3_metadata: dict | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Patch mutable metadata fields on a document.

        Scalars (``title``, ``version_label``, ``project``, ``doc_type``,
        ``authority_scope``, ``document_date``) use set-or-omit
        semantics: pass to set, omit to leave unchanged. The collection
        fields ``tags`` and ``tier3_metadata`` take ops-object patches;
        the bare-list / bare-dict forms are no longer accepted (see
        ``legacy_form`` below).

        Per CAS-ADR-021, any successful call sets ``metadata_confirmed=true``
        on the document (it leaves the metadata-review queue if it was
        there). The ``doc_type`` value must be one of the values defined
        under ``document_types.doc_types`` in the vault config; query
        ``sage_get_vault_config`` for the authoritative list.

        Empty-call confirmation-flip semantics (CAS-ADR-021):
        A call carrying only ``vault_id``, ``document_id``, and (implicit)
        ``modified_by`` -- with every field-patch parameter (``title``,
        ``version_label``, ``project``, ``tags``, ``doc_type``,
        ``authority_scope``, ``document_date``, ``tier3_metadata``) omitted
        or None -- is a **pure-confirmation flip**, not a no-op. It
        succeeds and: flips ``metadata_confirmed`` to True (the document
        leaves the metadata-review queue), advances ``updated_at``, and
        stamps ``last_modified_by``. This is intentional under
        CAS-ADR-021's caller-authoritative semantics: the caller's
        decision to invoke this tool IS the confirmation signal,
        independent of whether any field-patch parameter accompanies it.

        Compound-risk warning (FastMCP silent-drop interaction):
        FastMCP's ``ArgModelBase`` silently drops unknown JSON-RPC kwargs
        at the MCP framework boundary (see
        ``.venv/lib/python3.14/site-packages/mcp/server/fastmcp/utilities/func_metadata.py``).
        This compounds with the empty-call confirmation-flip behavior: a
        caller who misspells a kwarg name (e.g., types ``patch={...}``
        instead of the declared ``tier3_metadata={...}``) reduces their
        call to the all-None code path and silently triggers the
        confirmation flip with no semantic edit. The MCP envelope
        returns success; nothing in the response signals that the
        intended edit was dropped. **If your response indicates success
        but the document state did not change as expected, check for a
        misspelled parameter name -- unknown kwargs are silently dropped
        at the MCP framework boundary, which can reduce your call to
        the empty-call confirmation flip.**

        See CAS-ADR-028 for the ingest-vs-update shape asymmetry
        rationale: ``sage_ingest`` still takes ``tags`` as a list and
        ``tier3_metadata`` as a dict (creation supplies full state);
        ``sage_update_metadata`` patches existing state.

        Tags patch shape (``tags``)::

            {"add": ["x", ...], "remove": ["y", ...]}

        At least one key required and non-empty. ``add`` keys must NOT
        be present; ``remove`` keys MUST be present (strict conflict).
        ``add`` and ``remove`` must be disjoint and individually
        deduplicated. Worked examples:

        - Add a single tag: ``tags={"add": ["urgent"]}``
        - Remove one, add another: ``tags={"add": ["new"], "remove": ["old"]}``

        Tier3 patch shape (``tier3_metadata``)::

            {"set": {"key": "value", ...}, "unset": ["other_key", ...]}

        At least one key required and non-empty. ``set`` overwrites
        existing keys without error (the verb is literal: assert this
        value). ``unset`` keys must currently be present (strict
        conflict). ``set`` and ``unset`` must be disjoint. The merged
        result is validated against the resolved doc_type's
        ``metadata_schema``. Worked examples:

        - Change ticket priority: ``tier3_metadata={"set": {"ticket_priority": "high"}}``
        - Drop a key: ``tier3_metadata={"unset": ["stale_field"]}``

        Error modes:
        - ``document_not_found`` (404): no document with that id.
        - ``invalid_doc_type`` (400): ``doc_type`` not in vault config.
        - ``tag_add_conflict`` (400): one or more ``tags.add`` entries
          are already present. Detail carries ``document_id``, ``tags``
          (the conflicting subset), and ``current_tags``.
        - ``tag_remove_conflict`` (400): one or more ``tags.remove``
          entries are absent. Detail mirrors ``tag_add_conflict``.
        - ``tag_patch_overlap`` (400): ``add`` and ``remove`` share
          entries, or one list contains duplicates.
        - ``tier3_unset_conflict`` (400): one or more ``unset`` keys
          are absent. Detail carries ``document_id``, ``doc_type``,
          ``keys``, and ``current_tier3_keys``.
        - ``tier3_patch_overlap`` (400): ``set`` and ``unset`` share keys.
        - ``patch_empty`` (400): a patch object was supplied but carries
          no actionable operation (e.g., ``tags={}``).
        - ``tier3_schema_violation`` (400): the merged tier3 dict failed
          validation against the doc_type's metadata_schema, or the
          doc_type has no metadata_schema declared.
        - ``tier3_doc_type_change_stale_keys`` (400): the call changes
          ``doc_type`` AND supplies a ``tier3_metadata`` patch, and the
          merged tier3 dict carries keys that are not in the new
          doc_type's metadata_schema properties. Detail carries
          ``previous_doc_type``, ``new_doc_type``, and ``stale_keys`` —
          the exact list the caller must add to ``unset``. A new
          doc_type with no ``metadata_schema`` allows no keys, so every
          merged key is stale.
        - ``legacy_form`` (400): caller passed the deprecated bare-list
          form for ``tags`` or bare-dict form for ``tier3_metadata``.
          Detail names the new ops-object shape.

        Strict-conflict errors are planning bugs, not retry conditions;
        the caller should adjust its model of the document state rather
        than blindly re-issue.

        Dry-run mode (T-0152, T-0163):
        Set ``dry_run=true`` to validate the patch and compute the
        would-be projection of the post-patch state without persisting.
        The response is wrapped: ``{"document": <would-be post-patch
        document>, "dry_run": true, "changes": [...]}``. No
        ``updated_at`` advance, no ``metadata_confirmed`` flip, no
        chunk-store sync. Same validators run in the same order, so a
        dry-run that returns success means the real call will succeed
        modulo race conditions on shared state. The per-document lock
        is still acquired so the preview is consistent with concurrent
        mutations.

        ``changes`` (T-0163) enumerates the field-level deltas the
        patch would persist on a real run as a list of
        ``{path, before, after}`` entries (``FieldChange`` shape).
        Scalar field changes use the bare field name as ``path``;
        tier3 changes enumerate per-key with dotted paths (e.g.,
        ``tier3_metadata.severity``); tags carry the full ordered
        before/after lists in ``before``/``after``. Entries are
        sorted by ``path`` for determinism. Populated only on dry-run;
        ``changes=null`` on real-run responses and on dry-runs that
        touch no caller-supplied fields.

        Worked example: ``sage_update_metadata(vault_id="v",
        document_id="d", tier3_metadata={"set": {"severity": "high"}},
        dry_run=True)`` returns the would-be document with the patched
        tier3 dict plus ``changes=[{path: "tier3_metadata.severity",
        before: <old>, after: "high"}]``; storage is byte-identical
        to pre-call.

        Args:
            vault_id: Target vault identifier.
            document_id: The document's unique identifier.
            title: New display title.
            version_label: Version indicator (v1, v2, draft, final, etc.).
            project: Project or workstream identifier.
            tags: Tags patch object ``{add?, remove?}``. See above.
            doc_type: Document type. Must be defined in
                ``document_types.doc_types`` in the vault config.
            authority_scope: Authority scope identifier.
            document_date: Document calendar date (YYYY-MM-DD).
            tier3_metadata: Tier-3 patch object ``{set?, unset?}``.
                See above.
            dry_run: T-0152 / T-0163. When True, run all validators
                and compute the would-be projection of the post-patch
                state, but do NOT persist. The response carries a
                ``changes`` block enumerating field-level deltas (see
                "Dry-run mode" above). Default False.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            if document_date is not None:
                document_date = _DOCUMENT_DATE_ADAPTER.validate_python(document_date)
            _check_legacy_patch_form("tags", tags)
            _check_legacy_patch_form("tier3_metadata", tier3_metadata)
            v = get_vault(vault_id)
            request = UpdateMetadataRequest(
                title=title,
                version_label=version_label,
                project=project,
                tags=tags,
                doc_type=doc_type,
                authority_scope=authority_scope,
                document_date=document_date,
                tier3_metadata=tier3_metadata,
                dry_run=dry_run,
            )
            response = await v.metadata_service.update_metadata(
                document_id, request, v.config.vault.owner
            )
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_set_lifecycle(
        vault_id: str,
        document_id: str,
        action: str,
        successor_id: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Execute a lifecycle state transition on a document.

        The action vocabulary is **vault-config-defined**, not a fixed
        SAGE-wide set. Call ``sage_get_vault_config`` and read the
        ``lifecycle.transitions`` array for the authoritative list of
        (from_state, action, to_state, creates_edge) tuples in the
        target vault. Two examples seen in practice:

        - ``cas`` vault: ``ingest``, ``supersede``, ``complete``,
          ``archive``, ``reactivate``.
        - ``pim_health`` vault: ``ingest``, ``supersede``, ``complete``,
          ``archive``, ``reactivate``, ``file``.

        Neither vault defines ``activate``; the action for
        ``archived -> active`` is ``reactivate``.

        **The ``supersede`` action is the canonical atomic form for
        replacing one document with another.** It both transitions the
        predecessor (``active -> archived``) and creates the
        ``supersedes`` edge (new -> old) in a single operation. The
        alternative two-step pattern -- ``sage_link(edge_type="supersedes")``
        followed by ``sage_set_lifecycle(action="archive")`` -- ends
        in the same state but is required only when patching up an
        already-archived predecessor whose supersedes edge is missing.
        ``sage_link`` with ``edge_type="supersedes"`` does **not**
        auto-transition the predecessor's lifecycle.

        ``supersede`` is defined only as a transition out of ``active``.
        To supersede a predecessor in ``completed``, ``filed``, or any
        other non-active state, run the archive -> reactivate dance
        first, then either call ``sage_set_lifecycle(action="supersede",
        successor_id=...)`` against the predecessor or
        ``sage_ingest(..., predecessor_id=<predecessor_id>)``
        which applies the same atomic transition synchronously with
        record insertion. A direct call against a non-active
        predecessor returns ``supersede_target_not_active``.

        ``creates_edge`` in the vault config reveals which actions
        wire edges atomically. In all currently-shipped vault configs,
        only ``supersede`` does; other actions are state-only.

        Error modes:
        - ``invalid_action`` (400): the ``action`` string is not in any
          transition table for this vault.
        - ``invalid_lifecycle_transition`` (409): action is known but
          not valid from the document's current lifecycle state. The
          error detail enumerates the valid actions from the current
          state.
        - ``supersede_target_not_active`` (409): ``action="supersede"``
          was requested against a document not in ``active``.
        - ``pipeline_incomplete`` (409): emitted by some transitions
          (e.g. ``complete``) when the document's
          ``pipeline_status`` is not yet terminal.
        - ``identical_content_supersede`` (409): ``action="supersede"``
          with ``successor_id`` whose content hash matches the
          predecessor.

        Dry-run mode (T-0152, T-0163):
        Set ``dry_run=true`` to validate the request and compute the
        would-be projection of the post-transition state without
        persisting. The response is the same ``SetLifecycleResponse``
        envelope as a real run, augmented with ``dry_run: true`` at
        the top level and a ``changes`` block enumerating field-level
        deltas. No ``updated_at`` advance, no ``lifecycle_status``
        flip on the persisted document, no chunk-store sync. Same
        validators run in the same order, so a dry-run that returns
        success means the real call will succeed modulo race
        conditions on shared state. For ``action="supersede"``, the
        would-be edge surfaces in ``created_edge`` populated with the
        nil-UUID sentinel id ``00000000-0000-0000-0000-000000000000``
        so a caller that mistakes it for a real edge id fails loudly
        on lookup; no ``supersedes`` edge is persisted. The
        per-document lock is still acquired so the preview is
        consistent with concurrent mutations.

        ``changes`` (T-0163) carries a single ``FieldChange`` entry
        for ``lifecycle_status`` when the action changes state
        (skipped on no-op transitions). The would-be ``supersedes``
        edge stays in ``created_edge`` and is NOT duplicated in
        ``changes`` — edge mutations are a separate concept from
        document field-level deltas. Populated only on dry-run;
        ``changes=null`` on real-run responses.

        Worked example: ``sage_set_lifecycle(vault_id="v",
        document_id="d", action="archive", dry_run=True)`` returns
        the would-be document with ``lifecycle_status="archived"``
        plus ``changes=[{path: "lifecycle_status", before: "active",
        after: "archived"}]``; storage is byte-identical to pre-call.

        Args:
            vault_id: Target vault identifier.
            document_id: The document's unique identifier (the
                document the transition acts upon -- for supersede,
                this is the predecessor being archived).
            action: Lifecycle action name. Must appear in this
                vault's ``lifecycle.transitions`` table as a valid
                action from the document's current state. See
                ``sage_get_vault_config`` for the authoritative list.
            successor_id: The successor document's id (a ``documents.id``
                value — the same shape as ``document_id`` on other tools;
                T-0155). The ``document_id``/``successor_id`` pair is a
                semantic distinction, not a naming inconsistency; both
                endpoints carry document ids. Required when
                ``action="supersede"`` (a ``supersedes`` edge is created
                from ``successor_id`` -> ``document_id``); forbidden
                for all other actions. The successor must already exist
                as a separate active document; this tool does not
                create it. For the common case where the successor has
                not yet been ingested, prefer
                ``sage_ingest(..., predecessor_id=<predecessor_id>)``,
                which ingests and supersedes atomically.
            dry_run: T-0152 / T-0163. When True, run all validators
                and compute the would-be projection of the
                post-transition state, but do NOT persist. The
                response carries a ``changes`` block with a single
                ``lifecycle_status`` entry when the action changes
                state (see "Dry-run mode" above). For ``supersede``,
                the would-be edge surfaces in ``created_edge`` with
                the nil-UUID sentinel id
                ``00000000-0000-0000-0000-000000000000`` (not
                duplicated in ``changes``). Default False.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            if successor_id is not None:
                successor_id = _DOCUMENT_ID_ADAPTER.validate_python(successor_id)
            v = get_vault(vault_id)
            request = SetLifecycleRequest(
                action=action,
                successor_id=successor_id,
                dry_run=dry_run,
            )
            response = await v.lifecycle_service.set_lifecycle(document_id, request)
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_bulk_set_lifecycle(
        vault_id: str,
        items: list[dict],
        response_mode: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Apply lifecycle transitions to many documents in one call.

        First ``sage_bulk_*`` operation per CAS-ADR-029. Each item carries
        ``document_id``, ``action``, and optional ``successor_id`` with
        the same semantics as ``sage_set_lifecycle``; items are processed
        in order, each holding the per-document lock and a per-item
        SQLite transaction.

        Each per-item entry in ``items`` is validated using the full
        ``sage_set_lifecycle`` precondition surface — see that tool's
        docstring for the inherited rules (vault-config-defined action
        vocabulary, ``invalid_lifecycle_transition`` from the current
        state, the ``supersede`` chain-head and identical-content guards,
        ``pipeline_incomplete`` on ``complete``, etc.). Item-level errors
        do NOT roll back earlier or later items (CAS-ADR-029).

        **The batch is NOT atomic.** A per-item SAGEError surfaces in the
        response's per-item error envelope but does not roll back earlier-
        or-later successful items. The tool returns a successful response
        envelope (not an error envelope) when at least one item is processed,
        even if some items failed; callers must inspect each
        ``BulkLifecycleItemResult.status`` and the aggregate
        ``success_count`` / ``error_count`` fields. The tool returns an
        error envelope only when up-front validation rejects the call
        (invalid ``vault_id`` shape, malformed ``items`` shape, unknown
        vault, or invalid ``response_mode`` value).

        Empty ``items`` is valid: the response carries an empty
        ``results`` array and all counts are zero. Callers building bulk
        operations programmatically may pass ``items=[]`` without
        special-casing the call site.

        Performance: a bulk call is observably faster than N sequential
        ``sage_set_lifecycle`` calls because MCP framing overhead and
        inter-call asyncio scheduling are eliminated; the per-document
        lock and per-item SQLite transaction are unchanged.

        Args:
            vault_id: Target vault identifier.
            items: List of per-item transition requests. Each item must
                conform to the ``BulkLifecycleItem`` shape:
                ``{document_id: str, action: str, successor_id: str | None}``.
                Shape validation runs up front; a single malformed item
                rejects the entire batch before any per-item work
                executes.
            response_mode: Per-item payload depth (T-0153). ``"full"``
                returns each success item's complete ``document`` body
                (including the potentially-large ``semantic_abstract``);
                ``"light"`` strips the per-item ``document`` field
                entirely, returning only identity + status + warnings +
                error so the response stays inside the MCP inline-output
                budget (default 24 KiB; configurable per process via
                ``SAGE_MCP_INLINE_BUDGET_BYTES``). Failure entries carry the full structured error
                envelope regardless of mode. When unset, ``response_mode``
                defaults to ``"light"`` when ``len(items) > 5``, otherwise
                ``"full"``. The threshold is fixed at the constant
                ``LIGHT_DEFAULT_THRESHOLD = 5`` (defined in
                ``sage.services.lifecycle``); pass ``response_mode``
                explicitly to override. Invalid values surface as an
                ``internal_error`` envelope before any per-item work runs.
            dry_run: T-0152 / T-0163. When True, every item runs as
                a dry-run — validators execute, the would-be
                projection of the post-state is computed, and each
                per-item result carries a ``changes`` block
                enumerating field-level deltas (preserved under
                ``response_mode=light``). No persistence occurs.
                Envelope-level only; per-item override is not
                supported. **Limitation:** each item's dry-run is
                evaluated against the committed state at batch start;
                no item's would-be effects are visible to subsequent
                items. For full preview accuracy under sequential
                dependencies (e.g., item N supersedes a document and
                item N+1 tries to mutate it), dry-run each item
                separately. Default False.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            # Up-front shape validation across the whole batch: rejecting
            # the request here (rather than per-item inside the service
            # loop) guarantees that a malformed item produces an error
            # envelope without committing any partial state. The
            # ``response_mode`` ValueError from Pydantic enum validation
            # rides this same up-front rejection path (T-0153).
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
    async def sage_bulk_link(
        vault_id: str,
        items: list[dict],
        response_mode: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Create many edges in one call (T-0165, CAS-ADR-029).

        Third ``sage_bulk_*`` operation. Each item carries the same
        fields as a single ``sage_link`` call (``source_id``,
        ``target_id``, ``edge_type``, anchor fields, ``retracted_edge_id``,
        ``rationale``, ``rationale_kind``, ``notes``, ``synced_from_*``)
        and is dispatched through the idempotent variant: a duplicate
        natural-key triple (``source_id``, ``target_id``, ``edge_type``)
        returns the existing edge with ``created=false`` rather than
        raising (T-0079). Items are processed in order, each under the
        process-wide ``_link_lock`` and a per-item SQLite transaction.

        Each per-item entry in ``items`` is validated using the full
        ``sage_link`` precondition surface — see that tool's docstring
        for the inherited rules (document existence, edge-type
        registry-declared anchor policy per CAS-ADR-017, ``merged_from``
        chain-head invariant, ``retracted_edge_id`` shape, natural-key
        idempotency per T-0079, etc.). Item-level errors do NOT roll
        back earlier or later items (CAS-ADR-029).

        **The batch is NOT atomic.** A per-item SAGEError surfaces in the
        response's per-item error envelope but does not roll back earlier-
        or-later successful items. The tool returns a successful response
        envelope (not an error envelope) when at least one item is
        processed, even if some items failed; callers must inspect each
        ``BulkLinkItemResult.status`` and the aggregate ``success_count``
        / ``error_count`` fields. The tool returns an error envelope only
        when up-front validation rejects the call (invalid ``vault_id``
        shape, malformed ``items`` shape, unknown vault, or invalid
        ``response_mode`` value).

        Empty ``items`` is valid: the response carries an empty
        ``results`` array and all counts are zero. Callers building bulk
        operations programmatically may pass ``items=[]`` without
        special-casing the call site.

        Performance: a bulk call is observably faster than N sequential
        ``sage_link`` calls because MCP framing overhead and inter-call
        asyncio scheduling are eliminated; the process-wide ``_link_lock``
        and per-item SQLite transaction are unchanged.

        Per-item error modes (surfaced inside the response envelope, same
        codes as ``sage_link``): ``self_referential_edge`` (400),
        ``document_not_found`` (404), ``tbd_policy_edge`` (400),
        ``edge_anchor_policy_violation`` (400),
        ``retract_target_not_edge`` (400), ``merged_from_validation``
        (400), ``synced_from_inapplicable_edge_type`` (400),
        ``synced_from_version_not_in_source_chain`` (404). See
        ``sage_link`` for detail-envelope shape.

        Worked example. To create three references edges and a fourth
        depends_on edge in one call::

            sage_bulk_link(
                vault_id="cas",
                items=[
                    {"source_id": "a1b2c3d4_doc_a",
                     "target_id": "e5f6a7b8_doc_b",
                     "edge_type": "references",
                     "source_valid_from_version": "a1b2c3d4_doc_a",
                     "target_valid_from_version": "e5f6a7b8_doc_b",
                     "rationale": "doc A cites doc B"},
                    ...
                ],
            )

        On a dry-run (``dry_run=True``), every per-item ``edge.id``
        carries the nil-UUID sentinel ``00000000-0000-0000-0000-000000000000``
        on the create path, or the existing edge's real id on a natural-
        key hit (with ``created=false``). The envelope echoes
        ``dry_run=True`` and no edges are persisted.

        Args:
            vault_id: Target vault identifier.
            items: List of per-item link requests. Each item must
                conform to the ``BulkLinkItem`` shape:
                ``{source_id: str, target_id: str | null, edge_type: str,
                source_valid_from_version: str | null,
                target_valid_from_version: str | null,
                retracted_edge_id: str | null, notes: str | null,
                rationale: str | null, rationale_kind: str | null,
                synced_from_version: str | null,
                synced_from_content_hash: str | null}``. Shape
                validation runs up front; a single malformed item
                rejects the entire batch before any per-item work
                executes.
            response_mode: Per-item payload depth (T-0153 / T-0158).
                ``"full"`` returns each success item's complete ``edge``
                body; ``"light"`` strips the per-item ``edge`` field
                entirely, returning only ``source_id`` / ``target_id`` /
                ``edge_type`` / ``status`` / ``created`` /
                ``existing_rationale`` / ``error`` so the response stays
                inside the MCP inline-output budget. Failure entries
                carry the full structured error envelope regardless of
                mode. ``created`` and ``existing_rationale`` are
                preserved under light because they are the only signals
                callers have for the natural-key idempotency outcome.
                When unset, ``response_mode`` defaults to ``"light"``
                when ``len(items) > 5``, otherwise ``"full"``. The
                threshold is fixed at the constant
                ``LIGHT_DEFAULT_THRESHOLD = 5`` (defined in
                ``sage.services.graph_ops``); pass ``response_mode``
                explicitly to override. Invalid values surface as an
                ``internal_error`` envelope before any per-item work
                runs.
            dry_run: T-0152 / T-0163. When True, every item runs as a
                dry-run — validators execute (including the T-0079
                natural-key pre-check), the would-be projection of the
                edge is computed, and each per-item ``edge.id`` carries
                the sentinel (or the existing edge id on a natural-key
                hit). No persistence occurs. Envelope-level only;
                per-item override is not supported. **Limitation:** each
                item's dry-run is evaluated against the committed state
                at batch start; no item's would-be effects are visible
                to subsequent items. Default False.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            # Up-front shape validation across the whole batch: rejecting
            # the request here (rather than per-item inside the service
            # loop) guarantees that a malformed item produces an error
            # envelope without committing any partial state. The
            # ``response_mode`` ValueError from Pydantic enum validation
            # rides this same up-front rejection path (T-0153).
            validated_items = [BulkLinkItem.model_validate(it) for it in items]
            v = get_vault(vault_id)
            request = BulkLinkRequest(
                items=validated_items,
                response_mode=response_mode,
                dry_run=dry_run,
            )
            response = await v.graph_ops_service.bulk_link(request)
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_bulk_update_metadata(
        vault_id: str,
        items: list[dict],
        response_mode: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Apply metadata patches to many documents in one call.

        Second ``sage_bulk_*`` operation per CAS-ADR-029. Each item carries
        ``document_id`` plus any subset of the single-item
        ``sage_update_metadata`` fields (``title``, ``version_label``,
        ``project``, ``tags``, ``doc_type``, ``authority_scope``,
        ``document_date``, ``tier3_metadata``) with the same semantics as
        ``sage_update_metadata``; items are processed in order, each
        holding the per-document lock and a per-item SQLite transaction.

        Each per-item entry in ``items`` is validated using the full
        ``sage_update_metadata`` precondition surface — see that tool's
        docstring for the inherited rules (document existence, tag and
        tier3 patch grammar per CAS-ADR-028, doc_type validation,
        tier3 schema enforcement against the resolved doc_type,
        ``metadata_confirmed=true`` side-effect per CAS-ADR-021, etc.).
        Item-level errors do NOT roll back earlier or later items
        (CAS-ADR-029).

        **The batch is NOT atomic.** A per-item SAGEError surfaces in the
        response's per-item error envelope but does not roll back earlier-
        or-later successful items. The tool returns a successful response
        envelope (not an error envelope) when at least one item is
        processed, even if some items failed; callers must inspect each
        ``BulkMetadataItemResult.status`` and the aggregate
        ``success_count`` / ``error_count`` fields. The tool returns an
        error envelope only when up-front validation rejects the call
        (invalid ``vault_id`` shape, malformed ``items`` shape, per-item
        ``legacy_form`` shape, unknown vault, or invalid
        ``response_mode`` value).

        Per CAS-ADR-021, every successful per-item patch sets
        ``metadata_confirmed=true`` on the target document (the document
        leaves the metadata-review queue if it was there).

        Empty ``items`` is valid: the response carries an empty
        ``results`` array and all counts are zero. Callers building bulk
        operations programmatically may pass ``items=[]`` without
        special-casing the call site.

        Tags patch shape (per item ``tags``)::

            {"add": ["x", ...], "remove": ["y", ...]}

        At least one key required and non-empty. ``add`` keys must NOT be
        present; ``remove`` keys MUST be present (strict conflict). See
        ``sage_update_metadata`` for the full grammar (CAS-ADR-028).

        Tier3 patch shape (per item ``tier3_metadata``)::

            {"set": {"key": "value", ...}, "unset": ["other_key", ...]}

        Same grammar as ``sage_update_metadata``. The merged result is
        validated against the resolved doc_type's ``metadata_schema``.

        Per-item error modes (surfaced inside the response envelope):
        ``document_not_found`` (404), ``invalid_doc_type`` (400),
        ``tag_add_conflict`` / ``tag_remove_conflict`` /
        ``tag_patch_overlap`` (400), ``tier3_unset_conflict`` /
        ``tier3_patch_overlap`` / ``patch_empty`` (400),
        ``tier3_schema_violation`` (400), and
        ``tier3_doc_type_change_stale_keys`` (400). See
        ``sage_update_metadata`` for detail-envelope shape.

        Batch-level error modes (surfaced as the tool's error envelope):
        ``legacy_form`` (a per-item ``tags`` is a bare list or per-item
        ``tier3_metadata`` is a bare key/value dict; detail names the new
        ops-object shape), ``unknown_vault``, and ``internal_error``
        (malformed ``vault_id`` / ``items`` shape, or invalid
        ``response_mode``).

        Performance: a bulk call is observably faster than N sequential
        ``sage_update_metadata`` calls because MCP framing overhead and
        inter-call asyncio scheduling are eliminated; the per-document
        lock and per-item SQLite transaction are unchanged.

        Args:
            vault_id: Target vault identifier.
            items: List of per-item patch requests. Each item must
                conform to the ``BulkMetadataItem`` shape:
                ``{document_id: str, title?: str, version_label?: str,
                project?: str, tags?: TagsPatch, doc_type?: str,
                authority_scope?: str, document_date?: str,
                tier3_metadata?: Tier3Patch}``. Shape validation runs up
                front; a single malformed item rejects the entire batch
                before any per-item work executes.
            response_mode: Per-item payload depth (T-0153). ``"full"``
                returns each success item's complete ``document`` body
                (including the potentially-large ``semantic_abstract``);
                ``"light"`` strips the per-item ``document`` field
                entirely, returning only identity + status + warnings +
                error so the response stays inside the MCP inline-output
                budget (default 24 KiB; configurable per process via
                ``SAGE_MCP_INLINE_BUDGET_BYTES``). Failure entries carry the full structured error
                envelope regardless of mode. When unset, ``response_mode``
                defaults to ``"light"`` when ``len(items) > 5``, otherwise
                ``"full"``. The threshold is fixed at the constant
                ``LIGHT_DEFAULT_THRESHOLD = 5`` (defined in
                ``sage.services.metadata``); pass ``response_mode``
                explicitly to override. Invalid values surface as an
                ``internal_error`` envelope before any per-item work runs.
            dry_run: T-0152 / T-0163. When True, every item runs as
                a dry-run — validators execute, the would-be
                projection of the post-state is computed, and each
                per-item result carries a ``changes`` block
                enumerating field-level deltas (preserved under
                ``response_mode=light``). No persistence occurs.
                Envelope-level only; per-item override is not
                supported. **Limitation:** each item's dry-run is
                evaluated against the committed state at batch start;
                no item's would-be effects are visible to subsequent
                items. For full preview accuracy under sequential
                dependencies (e.g., item N adds tag X and item N+1
                tries to add the same tag), dry-run each item
                separately. Default False.
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
            # rides this same up-front rejection path (T-0153).
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
    async def sage_link(
        vault_id: str,
        source_id: str,
        target_id: str | None,
        edge_type: EdgeType,
        source_valid_from_version: str | None = None,
        target_valid_from_version: str | None = None,
        retracted_edge_id: str | None = None,
        notes: str | None = None,
        rationale: str | None = None,
        rationale_kind: RationaleKind | None = None,
        synced_from_version: str | None = None,
        synced_from_content_hash: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Create a typed edge between two documents in the graph.

        **For ``supersedes`` edges, prefer
        ``sage_set_lifecycle(action="supersede", successor_id=...)``**
        (or ``sage_ingest(..., predecessor_id=...)`` when the
        successor has not yet been ingested). Those tools wire the
        edge AND archive the predecessor atomically. ``sage_link`` with
        ``edge_type="supersedes"`` creates the edge alone and does
        **not** transition the predecessor's lifecycle; reach for it
        only when stitching a missing edge into a chain whose
        lifecycle states are already correct.

        Per CAS-ADR-017, each edge type has a registry-declared
        ``resolution_policy`` (one of: ``none``, ``transitive_source``,
        ``transitive_both``, ``TBD``) that dictates which anchor fields
        are required or forbidden. The policy is **not** a caller-supplied
        parameter — it is fixed per edge_type in the edge registry —
        but understanding it is necessary to know which anchor fields
        the call must carry:

        - `none` (supersedes, retracts, merged_from): meta-edges; no
          anchor fields. `retracts` additionally takes a one-sided
          `source_valid_from_version` (anchor in the retracting chain)
          and `retracted_edge_id` (the edge being retracted) instead of
          a `target_id`.
        - `transitive_source` (derived_from): requires
          `source_valid_from_version`; no target anchor. The anchor
          marks which version of the source chain this derivation is
          valid from, for chain-scoped traversal visibility per
          CAS-ADR-017. For whole-document derivations the convention is
          to set ``source_valid_from_version`` equal to ``source_id``
          itself — the edge is "valid from the source as it exists
          right now."
        - `transitive_both` (covers, references, bundles_with,
          depends_on, instantiated_from): requires both
          `source_valid_from_version` and `target_valid_from_version`.

        Canonical example — agent-asserted ``derived_from`` (e.g., a
        deliverable that traces to its template)::

            sage_link(
                vault_id="cas",
                source_id="<deliverable_id>",
                target_id="<template_id>",
                edge_type="derived_from",
                source_valid_from_version="<deliverable_id>",
                rationale="Template authored by ...",
            )

        ``merged_from`` chain-head precondition: **both** endpoints
        must be chain heads — i.e., neither ``source_id`` nor
        ``target_id`` may have an outbound ``supersedes`` edge.
        Absorption into a stale predecessor (source-side rule) and
        merging into an already-superseded node (target-side rule,
        symmetric) are both incoherent; the merge endpoints should be
        the currently authoritative heads, not nodes that have already
        been superseded. Attempting ``merged_from`` from a mid-chain
        source OR into a mid-chain target returns
        ``merged_from_validation``. When the source is mid-chain and a
        content-reuse edge is what's actually wanted, use
        ``derived_from`` instead (its anchor field
        ``source_valid_from_version`` captures the chain-visibility
        semantics that ``merged_from`` lacks).

        Document existence: both ``source_id`` and (when set)
        ``target_id`` must reference documents that currently exist in
        the vault. A missing endpoint raises ``document_not_found``.
        Self-referential edges (``source_id == target_id``) are
        rejected with ``self_referential_edge``; no edge_type allows a
        node to point at itself.

        ``retracts`` field-presence rules (closed-form): a ``retracts``
        edge requires ``source_valid_from_version`` (the anchor in the
        retracting chain), forbids ``target_valid_from_version`` (which
        must be null), and requires ``retracted_edge_id`` to reference
        an existing edge in the same vault. Violations surface as
        ``edge_anchor_policy_violation`` (anchor required/forbidden) or
        ``retract_target_not_edge`` (``retracted_edge_id`` does not
        name a known edge).

        ``synced_from_*`` field applicability (closed list): the
        ``synced_from_version`` and ``synced_from_content_hash``
        parameters are accepted **only** on ``edge_type="derived_from"``
        and ``edge_type="sync_target"``. Any other ``edge_type`` with
        either field set raises
        ``synced_from_inapplicable_edge_type``. The fields are not
        merely ignored on inapplicable types — they are a structural
        error.

        ``synced_from_version`` chain-membership (T-0111): when set,
        the value must be a member of the **target document's**
        ``supersedes`` chain (i.e., the target itself or any
        predecessor of the target reachable by walking outbound
        ``supersedes`` edges from the target). Out-of-chain values
        raise ``synced_from_version_not_in_source_chain``. The check
        runs only when ``synced_from_version`` is non-null and the
        edge_type permits the field per the closed-list rule above.

        TBD-policy edge types (CAS-ADR-017): two values appear in the
        ``EdgeType`` enum but are reserved-and-not-implemented:
        ``authoritative_for`` and ``sync_target``. Both have
        ``resolution_policy=TBD`` in the edge registry; every
        ``sage_link`` call carrying either type raises
        ``tbd_policy_edge`` unconditionally. Callers should not select
        these values until a future ADR retires the TBD policy. (The
        ``synced_from_*`` field-applicability rule above lists
        ``sync_target`` as a legitimate carrier of those fields for
        forward compatibility; this does not unblock ``sync_target``
        link creation today.)

        Anchors must lie in the supersedes lineage of their respective
        endpoint. Violations surface as 400 errors:

        - ``edge_anchor_policy_violation``: anchor field missing where
          required, present where forbidden, or pointing at a document
          not in the endpoint's supersedes lineage. Anchor values are
          ``documents.id`` strings, not version labels -- passing a
          version label (e.g. ``"v9.0"``) returns this code with a
          ``does not reference a known document`` detail. Also raised
          for ``retracts`` edges when ``source_valid_from_version`` is
          missing or ``target_valid_from_version`` is set.
        - ``document_not_found``: ``source_id`` or (when set)
          ``target_id`` does not reference an existing document in
          this vault.
        - ``self_referential_edge``: ``source_id`` and ``target_id``
          resolve to the same document. No edge_type permits a node
          to point at itself.
        - ``retract_target_not_edge``: the value supplied to
          ``retracted_edge_id`` is not a known edge id in this vault.
        - ``merged_from_validation``: a ``merged_from`` edge violates
          the merge-tombstone invariants -- either ``source_id`` or
          ``target_id`` is mid-chain (has an outbound ``supersedes``
          edge).
        - ``synced_from_inapplicable_edge_type``:
          ``synced_from_version`` or ``synced_from_content_hash`` was
          set on an ``edge_type`` other than ``derived_from`` or
          ``sync_target``.
        - ``synced_from_version_not_in_source_chain`` (T-0111):
          ``synced_from_version`` was set but the named document is
          not a member of the target's ``supersedes`` chain.
        - ``tbd_policy_edge``: the requested edge_type has
          ``resolution_policy=TBD`` and cannot be created. Currently
          ``authoritative_for`` and ``sync_target`` (CAS-ADR-017).

        Idempotency (T-0079): the edges table carries a UNIQUE
        constraint on ``(source_id, target_id, edge_type)``. Re-calling
        ``sage_link`` with the same triple does NOT error; it returns
        the pre-existing edge with ``created=false`` and a populated
        ``existing_rationale``. The caller's ``rationale``/``notes`` on
        the duplicate call are discarded -- the first-write rationale
        is preserved as canonical provenance. To intentionally replace
        an edge, ``sage_unlink`` it first.

        Dry-run mode (T-0152, T-0163):
        Set ``dry_run=true`` to validate the request and compute the
        would-be projection of the edge without persisting. The
        response shape is identical to a real-run response
        (``{edge, created, existing_rationale, dry_run}``);
        ``dry_run=true`` is echoed and the would-be ``edge.id`` is
        the nil-UUID sentinel ``00000000-0000-0000-0000-000000000000``.
        The T-0079 natural-key pre-check runs in dry-run too, so a
        preview on a (source, target, edge_type) that already has an
        edge returns ``created=false`` with the existing edge id and
        rationale — same shape as the real-run no-op path.

        Note: link is an edge mutation, not a document field
        mutation, so the change surface is the existing ``edge``
        field (with the nil-UUID sentinel) rather than a separate
        ``changes`` block (T-0163). ``LinkResponse`` does not carry
        a ``changes`` field.

        Worked example: ``sage_link(vault_id="v", source_id="a",
        target_id="b", edge_type="references",
        source_valid_from_version="a", target_valid_from_version="b",
        dry_run=True)`` returns the would-be edge; no edge row is
        inserted.

        Args:
            vault_id: Target vault identifier.
            source_id: Source document identifier (a ``documents.id``
                value — the same shape as ``document_id`` on other
                tools; T-0155). The ``source_id``/``target_id`` pair is a
                semantic distinction, not a naming inconsistency; both
                endpoints carry document ids.
            target_id: Target document identifier (a ``documents.id``
                value — the same shape as ``document_id`` on other
                tools; T-0155). Required for all edge types except
                ``retracts`` (which uses ``retracted_edge_id``); pass
                null for ``retracts`` edges.
            edge_type: Edge type (supersedes, derived_from, covers, references,
                bundles_with, depends_on, instantiated_from, retracts,
                merged_from).
            source_valid_from_version: Document ID of the source-chain
                version that anchors this edge in the supersedes
                lineage (a `documents.id`, not a version label string).
                Required for `transitive_source`, `transitive_both`,
                and `retracts` edges; forbidden on policy-`none`
                meta-edges.
            target_valid_from_version: Document ID of the target-chain
                version that anchors this edge in the supersedes
                lineage (a `documents.id`, not a version label string).
                Required only for `transitive_both` edges.
            retracted_edge_id: Edge-id of the edge instance being
                retracted. Required (and valid only) on `retracts`
                edges; must reference an existing edge in this vault
                (``retract_target_not_edge`` otherwise).
            notes: Free-text notes about the edge.
            rationale: Rationale for creating this edge.
            rationale_kind: Optional explicit provenance discriminator
                (CAS-ADR-019 / T-0080). Accepts one of
                ``version_chain``, ``references_mention``,
                ``filename_code_match``, ``manual``. When omitted, the
                value is derived from the rationale text prefix and
                falls back to ``manual``.
            synced_from_version: Source-chain version (document id) the
                content was copied or derived from at the moment this
                edge is asserted (T-0110). Accepted **only** on
                ``edge_type="derived_from"`` and
                ``edge_type="sync_target"``; any other edge_type with
                this field set raises
                ``synced_from_inapplicable_edge_type``. When set, the
                value must be a member of the target's ``supersedes``
                chain (T-0111) — out-of-chain values raise
                ``synced_from_version_not_in_source_chain``.
                Semantically meaningful on ``sync_target`` (Tier 1,
                auto-populated at re-ingestion when the Tier-1
                inference subsystem ships) and ``derived_from`` (Tier
                3, agent-supplied). Distinct from
                ``source_valid_from_version`` (CAS-ADR-017 chain
                visibility). Unset = explicit null; never inferred
                from chain anchors.
            synced_from_content_hash: Source document's
                ``source_content_hash`` captured at edge assertion
                (T-0110). Accepted **only** on
                ``edge_type="derived_from"`` and
                ``edge_type="sync_target"`` (same closed list as
                ``synced_from_version``;
                ``synced_from_inapplicable_edge_type`` otherwise).
                Optional companion to ``synced_from_version``;
                recommended on derivations because version labels are
                reused and can drift from content (in-place edits).
                Unset = explicit null.
            dry_run: T-0152. When True, validate the request and
                compute the would-be projection of the edge without
                persisting. No separate ``changes`` block (T-0163);
                the would-be edge is the change surface. Default False.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            source_id = _DOCUMENT_ID_ADAPTER.validate_python(source_id)
            if target_id is not None:
                target_id = _DOCUMENT_ID_ADAPTER.validate_python(target_id)
            if retracted_edge_id is not None:
                retracted_edge_id = _EDGE_ID_ADAPTER.validate_python(retracted_edge_id)
            if synced_from_version is not None:
                synced_from_version = _DOCUMENT_ID_ADAPTER.validate_python(synced_from_version)
            if synced_from_content_hash is not None:
                synced_from_content_hash = _SHA256_ADAPTER.validate_python(synced_from_content_hash)
            v = get_vault(vault_id)
            request = LinkRequest(
                source_id=source_id,
                target_id=target_id,
                edge_type=edge_type,
                source_valid_from_version=source_valid_from_version,
                target_valid_from_version=target_valid_from_version,
                retracted_edge_id=retracted_edge_id,
                notes=notes,
                rationale=rationale,
                rationale_kind=rationale_kind,
                synced_from_version=synced_from_version,
                synced_from_content_hash=synced_from_content_hash,
                dry_run=dry_run,
            )
            # T-0079: link_idempotent returns (edge, created). On a
            # duplicate natural-key triple the existing edge is
            # returned with created=False and the caller's rationale
            # is discarded. T-0152: wrap in LinkResponse so the
            # dry_run echo and the existing_rationale field have a
            # typed home.
            edge, created = await v.graph_ops_service.link_idempotent(request)
            response = LinkResponse(
                edge=edge,
                created=created,
                existing_rationale=edge.rationale if not created else None,
                dry_run=dry_run,
            )
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_unlink(vault_id: str, edge_id: str, dry_run: bool = False) -> dict:
        """Delete a production edge from the graph.

        For staging-table edges (pre-confirmation), use
        ``sage_update_staging_edge(action="dismiss")`` instead. The two
        tables are distinct and edge ids do not cross between them; an
        id minted in staging is not valid here once promoted, and vice
        versa.

        Discovering ``edge_id`` (T-0157): use ``sage_discover`` with
        ``target="edges"`` to enumerate production edges by
        ``source_id`` / ``target_id`` / ``edge_type``. Example:
        ``sage_discover(vault_id=..., mode="catalog", target="edges",
        filters={"source_id": "...", "edge_type": "..."})``. The
        returned ``edge_id`` is the value to pass here.

        Error modes:
        - ``edge_not_found`` (404): no production edge with that id.

        Dry-run mode (T-0152, T-0163):
        Set ``dry_run=true`` to confirm the edge exists and preview
        the would-be projection of what would be deleted without
        persisting. The response carries ``deleted=false``,
        ``dry_run=true``, and ``preview_edge`` populated with the
        would-be-deleted edge. A missing edge_id raises the same
        ``edge_not_found`` error as a real-run.

        Note: unlink is an edge mutation, not a document field
        mutation, so the change surface is the existing
        ``preview_edge`` field rather than a separate ``changes``
        block (T-0163). ``UnlinkResponse`` does not carry a
        ``changes`` field.

        Args:
            vault_id: Target vault identifier.
            edge_id: Production edge identifier.
            dry_run: T-0152. When True, preview the would-be
                projection of the deletion without persisting; the
                edge surfaces in ``preview_edge``. No separate
                ``changes`` block (T-0163); ``preview_edge`` is the
                change surface. Default False.
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
    async def sage_check_preconditions(vault_id: str, function_id: str) -> dict:
        """Check whether all depends_on targets for a function document are
        satisfied (active or completed lifecycle, pipeline complete).

        Iterates the document's outbound ``depends_on`` edges; for each
        target, verifies lifecycle in (active, completed) and
        pipeline_status terminal. Returns ``satisfied`` boolean plus a
        per-edge breakdown of failing reasons (e.g. predecessor still
        in projection, target archived) so the caller can act on the
        gap rather than re-querying each dependency.

        Error modes:
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
    async def sage_traverse(
        vault_id: str,
        start_id: str | None = None,
        edge_type: str | None = None,
        direction: str = "outbound",
        depth: int = 3,
        debug: bool = False,
        document_id: str | None = None,
    ) -> dict:
        """Walk the document graph from a starting document.

        Traversal honors chain-scoped edge resolution per CAS-ADR-017:
        anchor fields determine which edges are visible from the query
        version's lineage; `retracts` edges can suppress downstream
        edges; `merged_from` tombstones suppress predecessor-chain edges
        downstream of the termination point.

        **Outbound dedup on ``transitive_both`` edges.** For
        ``transitive_both`` edge types (``covers``, ``references``,
        ``bundles_with``, ``depends_on``, ``instantiated_from``),
        outbound traversal deduplicates by target document: at most
        one representative edge per (source-chain, target) pair is
        returned, with chain-scoped resolution selecting the winner
        from the query position's lineage. Distinct edges sourced
        from different chain members but pointing at the same target
        are masked. For comprehensive enumeration of every edge into
        a target chain, traverse **inbound** from the target instead.
        The ``edge_counts.{edge_type}`` field in the response reflects
        the total visible edges from the query position, including
        those masked by dedup; if ``edge_counts > len(nodes)``, the
        result has masked siblings. ``supersedes`` is point-to-point
        and not subject to this dedup; the rule applies only to the
        five ``transitive_both`` edge types.

        Args:
            vault_id: Target vault identifier.
            start_id: Starting document identifier. Alias: ``document_id`` (T-0155).
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
            document_id: Alias for ``start_id`` (T-0155). Either parameter
                is accepted; supply exactly one. Supplying both — even with
                equal values — returns ``ambiguous_document_identifier``.
        """
        try:
            # T-0155: validate each id-bearing parameter by its literal
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
                    tool="sage_traverse",
                    canonical="start_id",
                    alias="document_id",
                )
            if start_id is None and document_id is None:
                raise MissingDocumentIdentifierError(
                    tool="sage_traverse",
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
    async def sage_chain(
        vault_id: str,
        document_id: str,
        edge_type: str,
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
        - ``document_not_found`` (404): no document with that id.

        Args:
            vault_id: Target vault identifier.
            document_id: Document ID to start the chain walk from.
                The result is symmetric: any chain member returns the
                full ordered chain with that member's position
                indicated.
            edge_type: Edge type to follow (e.g. "supersedes", "references").
            limit: Maximum chain entries to return. Default: all.
                Use with offset to page through long version chains.
            offset: Skip this many entries from the start (oldest). Default: 0.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            v = get_vault(vault_id)
            request = ChainRequest(
                document_id=document_id,
                edge_type=edge_type,
                limit=limit,
                offset=offset,
            )
            response = await v.graph_ops_service.chain(request)
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_discover(
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
            catalog: Filter-only SQL enumeration. No query needed. Returns document
                metadata only (no chunks or scores). Supports pagination via limit + offset.
                Best for deterministic enumeration by tags, doc_type, or other metadata.
            deterministic: Exact heading path extraction. Requires document_id + heading_path.

        Edge enumeration (T-0157):
            When ``target="edges"`` (only valid with ``mode="catalog"``),
            results are edge rows rather than document rows. Filter by any
            subset of ``{"source_id": ..., "target_id": ..., "edge_type": ...}``;
            an empty filter returns all edges in the vault, paginated. Each
            row carries the edge id (required for ``sage_unlink`` and the
            ``retracts`` edge_type), endpoints, edge_type, anchor versions,
            rationale, and retraction state (``retracted_at`` plus the id
            of the disclaiming retracts edge, when applicable). Use
            ``response_mode="light"`` to strip to identity columns; ``full``
            to carry the complete envelope. Default obeys a threshold rule:
            ``light`` when more than 5 results would be returned, otherwise
            ``full``.

            Example::

                sage_discover(
                    vault_id="cas",
                    mode="catalog",
                    target="edges",
                    filters={"source_id": "<doc_id>", "edge_type": "references"},
                    response_mode="full",
                )

        Response-mode semantics across targets (T-0158):
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
                ``tier3_metadata`` (T-0004). Equality is exact; ``null``
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
                See the *Edge enumeration* section above. (T-0157)
            response_mode: Canonical payload-depth selector across SAGE
                surfaces (T-0157, T-0158, T-0153). See the
                *Response-mode semantics across targets* section above
                for the full behavior matrix. "light" returns the
                stripped shape (DocumentSummaryLight for
                catalog+documents, identity columns for edges,
                chunk_content-suppressed for semantic/keyword); "full"
                returns the complete envelope. When unset, edges apply
                the >5-results default-threshold rule; documents
                preserve full-equivalent behavior unconditionally.
            sort_by: Sort key for catalog mode results. One of:
                "title", "doc_type", "document_date",
                "lifecycle_status". Ignored by semantic, keyword, and
                deterministic modes. Default: unset -- catalog falls
                back to active-lifecycle-first then ``document_date``
                descending. (T-0174)
            sort_order: Sort direction for catalog mode results. One
                of: "asc", "desc". Ignored by semantic, keyword, and
                deterministic modes. Default: unset -- ascending when
                ``sort_by`` is specified. (T-0174)

        Catalog budget hint (T-0091):
            Catalog responses include a ``hints`` field carrying
            ``recommended_limit`` when the serialized result would
            exceed the Claude Code MCP inline ceiling. When present,
            re-page with ``limit=recommended_limit`` to keep the
            response inline and avoid the disk/jq fallback. The
            budget defaults to 24 KiB and is configurable per process
            via ``SAGE_MCP_INLINE_BUDGET_BYTES``.

        Error modes (T-0092):
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
            # T-0092: pass the raw dict so DiscoverRequest performs the
            # nested RetrievalFilters validation. This keeps the
            # ValidationError loc prefixed with ``("filters", ...)``, which
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
    async def sage_read_projection(
        vault_id: str,
        document_id: str,
        write_to_path: str | None = None,
    ) -> dict:
        """Read a document's full text into context with metadata header.

        Two delivery modes:
        - default: returns the complete projection (reconstructed from
          stored chunks) inline as ``projection_text``, equivalent to
          uploading the document. Use this instead of ``sage_discover``
          when you need the whole document.
        - ``write_to_path=/abs/path``: SAGE writes the projection text
          to the given absolute path. The response carries ``written_to``
          and ``content_size``; ``projection_text`` is null. Preferred
          for large projections that would exceed the MCP tool-result
          inline budget. Mirrors ``sage_get_document(write_to_path=...)``.

        Replaces the pre-audit ``sage_export_projection`` MCP tool, which
        was folded into this write_to_path mode per the *SAGE MCP Tool
        Surface* steering doc v3 audit. The REST surface retains the
        original ``export_projection`` endpoint (storage_root-relative
        semantics).

        Error modes:
        - ``document_not_found`` (404): no document with that id.
        - ``no_projection`` (404): the document exists but has no
          stored projection (e.g. ingestion failed mid-pipeline or
          the document is awaiting reabstraction). Inspect
          ``pipeline_status`` via ``sage_get_document``; if recoverable,
          ``sage_reabstract`` may restore the projection.
        - ``write_path_exists`` (409): ``write_to_path`` target already
          exists.
        - ``write_path_invalid`` (400): ``write_to_path`` is not
          absolute, or its parent directory is missing / not writable.

        Args:
            vault_id: Target vault identifier.
            document_id: The document's unique identifier.
            write_to_path: Absolute filesystem path. When set, SAGE
                writes the projection text to this path and returns
                metadata only. The target must not exist; its parent
                must exist and be writable.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            v = get_vault(vault_id)
            response = await v.utilities_service.read_projection(
                document_id, write_to_path=write_to_path
            )
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_read_section(vault_id: str, document_id: str, heading_path: str) -> dict:
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
        position," prefer sage_discover semantic or keyword mode — both
        index heading_path text alongside content.

        Args:
            vault_id: Target vault identifier.
            document_id: The document's unique identifier.
            heading_path: Heading path prefix
                (e.g. "Technical Description > Composite Claim Binding").
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            v = get_vault(vault_id)
            response = await v.utilities_service.read_section(document_id, heading_path)
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_list_headings(vault_id: str, document_id: str) -> dict:
        """List all heading paths for a document in document order.

        Returns the structural table of contents (heading paths only) without
        reading body content. Use this to verify a document's structure or
        pick a heading path before calling sage_read_section.

        Replaces the antipattern of calling sage_read_section with a
        deliberately wrong heading path to harvest ``available_headings``
        from the resulting ``heading_not_found`` error response. The
        synthetic header chunk (T-0038) is excluded, so the returned paths
        are exactly those a caller may pass to sage_read_section.

        Args:
            vault_id: Target vault identifier.
            document_id: The document's unique identifier.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            v = get_vault(vault_id)
            response = await v.utilities_service.list_headings(document_id)
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_refresh_views(vault_id: str) -> dict:
        """Regenerate browsable symlink views (by_doc_type/, by_lifecycle/)
        in the vault's storage root.

        Drops and recreates the symlink trees under the vault's
        ``storage_root``. Useful after bulk metadata updates that
        moved documents between doc_type or lifecycle buckets. The
        symlink views are for human Finder/file-browser navigation;
        no SAGE tool consumes them.

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

    @mcp.tool()
    async def sage_list_vaults() -> dict:
        """Enumerate all configured vaults. No vault_id parameter -- operates
        across all registered vaults.
        """
        try:
            summaries = await vault_registry_service.list_vaults()
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

    @mcp.tool()
    async def sage_create_vault(config: dict) -> dict:
        """Create a new vault and register it with the running SAGE instance.

        Pass a complete vault config dict. The dict is validated against the
        vault config schema, directories are created, ``vault_config.yaml``
        is written under the vault root (default
        ``~/sage_vaults/<vault_id>/``), services are initialized, and the
        vault is registered with the running MCP server immediately
        (no restart needed). The full written config is echoed back in
        the response so the caller can follow up with
        sage_update_vault_config to adjust individual sections without
        a separate read.

        A minimal default config (suitable for most one-off vaults) can be
        produced with
        ``VaultRegistryService.get_default_config(vault_id, name, owner)``
        from ``sage.services.vault_registry``; callers that want a tailored
        vault should construct the dict directly against the vault config
        schema.

        Config dict structure:
        The ``config`` parameter is opaque at the MCP boundary (the
        signature accepts ``dict``); its required shape lives in
        ``docs/fs/sage/vault_config.schema.json``. Six top-level
        sections are required -- ``vault``, ``document_types``,
        ``lifecycle``, ``source_adapters``, ``metadata_extraction``,
        ``edge_inference`` -- and three are optional --
        ``abstraction``, ``access_control_defaults``,
        ``retrieval_health``. Missing required sections (or malformed
        sub-section payloads) surface as ``vault_config_validation_error``
        at validation time. Construct against the schema directly, or
        start from ``VaultRegistryService.get_default_config`` and
        edit the returned dict.

        Stack abstraction provider dependency (CAS-ADR-030):
        The new vault silently inherits the running SAGE process's
        stack-wide abstraction provider singleton, built once at
        process startup from ``sage/config.yaml``. There is no
        per-vault provider override on this tool; the ``abstraction``
        section in the vault config governs only enable/disable and
        per-vault parameters, not provider identity. Callers that
        want a different provider for a new vault must edit the
        stack config and restart the SAGE process before calling
        ``sage_create_vault``; verify the in-memory stack config via
        ``sage_get_stack_config`` if you suspect drift.

        Partial-failure non-atomicity:
        Vault creation runs five sequential steps -- (1) config
        directory creation, (2) ``vault_config.yaml`` write, (3) service
        initialization (graph store, content store, abstraction
        provider wiring), (4) registry insertion, (5) owner bootstrap
        via ``UserService.bootstrap_owner`` -- with no rollback across
        step boundaries. A failure mid-sequence (disk error during step
        2, schema-migration error during step 3, provider build failure
        during step 3, etc.) leaves the filesystem and the in-memory
        registry in an intermediate state: ``~/sage_vaults/{vault_id}/``
        may exist with a partial ``vault_config.yaml`` while the
        registry has no entry for the vault. Recovery: manually remove
        ``~/sage_vaults/{vault_id}/`` and call ``sage_create_vault``
        again.

        ``bootstrap_owner`` side effect:
        Step 5 of the create sequence creates the owner user (per the
        vault config's ``access_control_defaults.owner`` value or
        equivalent) via ``UserService.bootstrap_owner``. This is a
        silent state mutation -- the response carries only the
        ``VaultSummary`` projection (id, name, storage_root, plus the
        echoed config dict), with no field indicating that the owner
        row was inserted. The owner row is required for subsequent
        access-controlled operations on the new vault.

        Eager tier3 validator cache build:
        ``VaultConfig.model_post_init`` builds a JSON Schema validator
        for every doc_type that declares a ``metadata_schema`` (per
        CAS-ADR-031) at config construction time, not at first ingest.
        A malformed ``document_types.doc_types[].metadata_schema``
        payload (e.g., a non-Draft 2020-12 schema, an unresolvable
        ``$ref``) surfaces at create time as part of
        ``vault_config_validation_error`` rather than deferred to the
        first ``sage_ingest`` call that would have used the validator.
        This is intentional: catching schema authoring errors at vault
        create time is cheaper than catching them on the first ingest
        whose ``tier3_metadata`` happens to exercise the offending
        doc_type.

        Error modes:
        - ``vault_already_exists`` (409): a vault with that
          ``vault_id`` is already registered.
        - ``vault_config_validation_error`` (400): the supplied
          config fails schema validation. Covers missing or malformed
          top-level sections (see "Config dict structure" above) and
          malformed ``document_types.doc_types[].metadata_schema``
          payloads caught by the eager tier3 validator cache build
          (see "Eager tier3 validator cache build" above).

        Args:
            config: Full vault config dict. Must validate against the
                vault config schema at
                ``docs/fs/sage/vault_config.schema.json`` -- six
                required top-level sections plus three optional. See
                "Config dict structure" above for the section list and
                "Eager tier3 validator cache build" for the
                doc_type-schema validation that runs at create time.
        """
        try:
            summary = await vault_registry_service.create_vault(CreateVaultRequest(config=config))
            return {
                "vault_id": summary.id,
                "name": summary.name,
                "storage_root": summary.storage_root,
                "config": config,
            }
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_get_vault_config(vault_id: str) -> dict:
        """Return the full vault configuration as a dict.

        This is the authoritative source for vault-config-defined
        vocabulary that other tools depend on. Read this when you need:

        - The valid ``action`` vocabulary for ``sage_set_lifecycle``
          (under ``lifecycle.transitions``; each entry includes
          ``from_state``, ``action``, ``to_state``, ``creates_edge``).
        - The valid ``doc_type`` values for ``sage_update_metadata``
          or for filtering ``sage_discover`` (under
          ``document_types.doc_types``).
        - The enabled source adapters for ``sage_ingest`` / ``sage_parse_filename``
          (under ``source_adapters.adapters``).
        - The filename-parsing pattern and segment fields used by
          ``sage_parse_filename`` (under
          ``metadata_extraction.filename_extraction``).
        - The edge inference tier assignments and inference rules
          relevant to ``sage_list_staging_edges`` (under
          ``edge_inference.tier_assignments``).

        The returned dict is the live in-memory config; on-disk edits
        to ``vault_config.yaml`` are not picked up until
        ``sage_reload_vault`` is called.

        Args:
            vault_id: Target vault identifier.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            services = get_vault(vault_id)
            return services.vault_config_service.get_config()
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_update_vault_config(
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
        section of the config wholesale; sections left as None are
        preserved unchanged. Partial-section merges are not supported --
        if you pass ``document_types={"doc_types": [...]}``, the entire
        ``document_types`` section is replaced by the dict you pass, so
        include every key of that section you want to keep.

        If the merged config would remove a doc_type or lifecycle state
        that still has documents attached, the update is rejected with
        a ``destructive_config_change`` error and the affected counts
        are reported in the error detail. Pass force=True to proceed
        anyway; the warnings then appear in the success response.

        Changing `vault.id` is never permitted regardless of force -- use
        sage_create_vault to make a new vault instead.

        The update writes to disk and updates the running config in
        place; subsequent tool calls see the new vocabulary
        immediately. Re-load via ``sage_reload_vault`` is only needed
        if external processes edited ``vault_config.yaml`` outside
        this MCP server.

        Reload-atomicity gap on the outer write+reload sequence:
        A real-run update walks ``_write_config_yaml(...)`` first and
        then ``_registry_service.reload(...)``. The reload step itself
        is atomic with respect to the registry slot per T-0183 (the
        new services are built first; the old services remain
        installed if construction fails — see ``sage_reload_vault``
        for the inner-reload guarantees and the recovery recipe).
        **The outer yaml-write + reload sequence is NOT atomic**: if
        the reload step fails (schema-migration required, duplicate
        edges, abstraction-provider build failure, etc.), the on-disk
        ``vault_config.yaml`` has already been overwritten with the
        new merged config while the in-memory registry continues to
        serve the **old** config. Subsequent tool calls see the old
        vocabulary; a manual ``sage_reload_vault`` after addressing
        the underlying failure is required to reconcile disk and
        memory. The reload-atomicity sibling row for
        ``sage_reload_vault`` itself is documented in the T-0180
        audit; T-0183 closed the inner step's atomicity, not the
        outer sequence.

        Compound-risk warning (FastMCP silent-drop interaction):
        FastMCP's ``ArgModelBase`` silently drops unknown JSON-RPC
        kwargs at the MCP framework boundary (see T-0186 and
        ``.venv/lib/python3.14/site-packages/mcp/server/fastmcp/utilities/func_metadata.py``).
        This compounds with the all-None real-run code path: when
        every section parameter is omitted or None (or every section
        kwarg is misspelled — e.g., ``doctypes={...}`` instead of
        ``document_types={...}``), and ``dry_run=False``, the tool
        still revalidates the current in-memory config, writes a
        byte-identical ``vault_config.yaml`` to disk, and triggers a
        full ``_registry_service.reload``. The MCP envelope returns
        ``status="updated"`` with empty warnings; nothing in the
        response signals that no intended section edit reached the
        config. **If your response indicates success but the vault
        config did not change as expected, check for a misspelled
        section kwarg name -- unknown kwargs are silently dropped at
        the MCP framework boundary, which can reduce your call to
        the all-None no-op-with-reload path.**

        Error modes:
        - ``invalid_vault_id`` (400): ``vault_id`` failed
          ``VaultIdStr`` typed-alias validation at the boundary (per
          CAS Typed-Alias Boundary Conventions). The alias enforces a
          non-empty slug-shaped identifier; malformed inputs are
          caught here rather than at a downstream lookup.
        - ``destructive_config_change`` (409): see above.
        - ``vault_config_validation_error`` (400): the merged config
          fails schema validation, or the request attempts to change
          ``vault.id``.

        Test-fixture concern (``_registry_service`` back-reference):
        ``VaultConfigService`` requires a registry-service
        back-reference to perform the post-write reload. Production
        wiring supplies one automatically; a service constructed in
        a context that does NOT wire one (e.g., a unit-test fixture
        that bypasses the registry) raises ``RuntimeError`` on the
        first real-run call to this tool. Dry-run mode also raises
        if the back-reference is absent (the guard runs before the
        dry-run branch). Tests exercising this tool against an
        ad-hoc service must supply a registry service or stub.

        Dry-run mode (T-0152, T-0163):
        Set ``dry_run=true`` to validate the merged config and preview
        the would-be projection of which sections would change,
        without writing yaml or reloading the registry. The response
        carries ``status="previewed"``, ``dry_run=true``, ``warnings``
        (always populated when present — dry-run NEVER raises
        ``destructive_config_change``), and a
        ``preview.changed_sections`` list naming the top-level
        sections that would change. ``force`` is a no-op on dry-run.

        Note: vault-config updates are a config mutation, not a
        document field mutation, so the change surface is the
        existing ``preview.changed_sections`` list rather than a
        separate ``changes`` block (T-0163).
        ``UpdateVaultConfigResponse`` does not carry a ``changes``
        field.

        Worked example: ``sage_update_vault_config(vault_id="v",
        document_types={"doc_types": [...]}, dry_run=True)`` returns
        the destructive-change warnings (if any) so the caller can
        decide whether to follow up with ``force=True`` on a real run.

        Args:
            vault_id: Target vault identifier. Validated against
                ``VaultIdStr`` (typed-alias boundary check; see
                ``invalid_vault_id`` above) before the registered-vault
                lookup.
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
            dry_run: T-0152. When True, preview the would-be
                projection of the change without persisting; never
                raises destructive_config_change. The change surface
                is the existing ``preview.changed_sections`` field;
                no separate ``changes`` block (T-0163). Default False.
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

    @mcp.tool()
    async def sage_vault_stats(vault_id: str) -> dict:
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

    @mcp.tool()
    async def sage_hash_check(vault_id: str, hashes: list[str]) -> dict:
        """Bulk hash existence check against the graph store.

        For each input hash, returns whether an existing document in
        the vault carries that content hash and, if so, the matching
        document's id and source_path. Used by the scan-and-batch-
        ingest flow to identify already-ingested files without
        re-hashing on the SAGE side. The response is a dict keyed by
        input hash; missing hashes are simply absent from the result.

        Hash format: the canonical request form is the prefixed
        ``sha256:<hex>``, matching ``ContentHashStr``. The MCP
        transport additionally accepts bare hex strings (the form
        ``sage_ingest`` emits in its response payload) without
        rewriting them, so callers can round-trip ingest results
        directly. Output document records carry the prefixed form.

        Args:
            vault_id: Target vault identifier.
            hashes: List of content hash strings. Accepts both
                ``sha256:<hex>`` and bare hex for each entry.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            services = get_vault(vault_id)
            # Skip Sha256Str validation: the MCP transport historically
            # accepts bare-hex hashes (the form sage_ingest emits in its
            # response) in addition to the prefixed form the REST request
            # schema requires. Normalizing the two storage formats is a
            # separate concern from T-0009.
            body = HashCheckRequest.model_construct(hashes=hashes)
            matches = await services.vault_config_service.hash_check(body)
            return {h: m.model_dump(exclude_none=True) for h, m in matches.items()}
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_list_staging_edges(vault_id: str) -> dict:
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
    async def sage_update_staging_edge(vault_id: str, edge_id: str, action: str) -> dict:
        """Confirm or dismiss a staging edge.

        Dispatches by ``action``:

        - ``action="confirm"``: promote the staging edge to production.
          The staging row is deleted and a new production edge is
          inserted with the same source, target, and edge_type. The
          returned envelope carries the production ``edge_id``, which is
          distinct from the staging id passed in -- staging and production
          tables do not share an id space.
        - ``action="dismiss"``: delete the staging edge without creating
          a production edge. The reviewer's judgment that the inferred
          edge is wrong. The underlying inference rule is not re-applied
          for the same (source, target, edge_type) combination during
          the current ingest cycle, but a future re-ingest that re-triggers
          the same inference rule will re-stage the candidate.

        Replaces the pre-audit ``sage_confirm_staging_edge`` and
        ``sage_dismiss_staging_edge`` MCP tools, which were collapsed into
        this single parameter-dispatched form per the *SAGE MCP Tool
        Surface* steering doc v3 audit.

        ``vault_id`` typed-alias validation:
        ``vault_id`` is validated through the ``VaultIdStr`` typed alias
        before any service-layer dispatch. Empty strings, whitespace-only
        strings, and other shape violations surface as
        ``invalid_vault_id`` before the registry lookup runs.

        ``edge_id`` typed-alias validation:
        ``edge_id`` is validated through the ``EdgeIdStr`` typed alias
        before any service-layer dispatch. Shape violations surface as
        ``invalid_edge_id`` before the staging-row lookup runs.

        Confirm idempotency on natural-key collision (T-0079):
        On ``action="confirm"``, if the staging edge's natural-key triple
        ``(source_id, target_id, edge_type)`` already exists in the
        production edges table -- for example, because a parallel
        ``sage_link`` call or an earlier auto-inference path already
        created the production edge -- confirm silently returns the
        existing production edge's id rather than raising
        ``IntegrityError``. The staging row is consumed in either case
        (this is the ``on_conflict="noop"`` insert path in
        ``StagingEdgesService.confirm_staging_edge``; see
        ``sage/services/staging_edges.py``). A caller observing the
        response cannot distinguish "I caused the production edge to be
        created" from "someone else already created it; I just consumed
        my staging row" -- both surface as a successful confirm with a
        populated ``production_edge_id``.

        Insert-then-delete atomicity gap:
        The confirm path sequences ``insert_edge`` followed by
        ``delete_staging_edge`` without wrapping the pair in a single
        transaction. If the delete fails after the insert succeeds, the
        staging row persists alongside the new production edge; the
        natural-key triple then exists in both tables until a subsequent
        confirm consumes the orphaned staging row (which is itself a
        T-0079 silent-idempotent no-op per the rule above). Callers
        building provenance over staging-edge promotion should treat
        confirm as "at-least-once" for the production-edge insert and
        rely on the natural-key UNIQUE constraint plus T-0079 idempotency
        to absorb retries.

        Error modes:
        - ``invalid_vault_id`` (400): ``vault_id`` failed
          ``VaultIdStr`` typed-alias validation.
        - ``invalid_edge_id`` (400): ``edge_id`` failed
          ``EdgeIdStr`` typed-alias validation.
        - ``staging_edge_not_found`` (404): the id is unknown
          (already confirmed, already dismissed, or never existed).
        - ``invalid_action`` (400): ``action`` is not one of
          ``"confirm"`` or ``"dismiss"``.

        Args:
            vault_id: Target vault identifier. Validated through
                ``VaultIdStr`` (see the ``vault_id`` typed-alias
                validation paragraph above).
            edge_id: Staging edge identifier (from
                ``sage_list_staging_edges``). Validated through
                ``EdgeIdStr`` (see the ``edge_id`` typed-alias
                validation paragraph above).
            action: One of ``"confirm"`` or ``"dismiss"``. On
                ``"confirm"``, behavior on a natural-key collision is
                governed by the T-0079 confirm-idempotency paragraph
                above, and the insert/delete pair is not atomic per the
                atomicity-gap paragraph above.
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
    async def sage_pending_metadata(vault_id: str) -> dict:
        """List documents with unconfirmed metadata.

        A document is "pending" when its ``metadata_confirmed`` flag
        is false. Per CAS-ADR-021, this typically arises from
        ``sage_ingest(needs_review=true)``: the caller deferred metadata
        to filename inference, which populated fields the caller did
        not supply and held the document for review. The pending
        state is cleared on any ``sage_update_metadata`` call against
        the document (even a single-field update).

        For the default ``sage_ingest`` path (``needs_review=false``),
        documents land with ``metadata_confirmed=true`` and never
        appear here.

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
    async def sage_reabstract(
        vault_id: str,
        document_id: str,
    ) -> dict:
        """Re-run abstraction on an existing document (fire-and-forget).
        Reconstructs projection text from stored chunks and dispatches
        a new density-proportional semantic abstract as a background
        task; the abstract is written to the document node by that
        background task, not by this call.

        The generation uses the SAGE stack's currently-configured
        abstraction provider and model (see ``abstraction`` in
        ``sage/config.yaml``; per CAS-ADR-030 the model identifier is
        stack-wide, not per-vault). Per-document or per-doc_type prompt
        overrides are not exposed here. If the new abstract is still
        off-topic after this call, the lever is a stack-config change
        to the abstraction model (or the prompt template), not a
        re-issue of this tool.

        Fire-and-forget semantics (caller-expectation-mismatch class):
        This tool does NOT return when the new abstract is persisted.
        It validates the document, flips
        ``pipeline_status=abstraction_in_progress``, dispatches the
        abstraction work in an ``asyncio.create_task`` background task,
        and returns immediately with::

            {"status": "reabstract_started",
             "document_id": "<id>",
             "dispatched_at": "<iso8601 timestamp>"}

        The background task is what generates the abstract, persists
        ``semantic_abstract``, and flips ``pipeline_status`` to
        ``abstraction_complete`` (success) or ``failed`` (error). To
        observe terminal state, poll ``sage_get_document`` and read
        ``pipeline_status``; the call is complete when that field is
        no longer ``abstraction_in_progress``. Any caller that assumes
        ``sage_reabstract`` returns a document with the new abstract
        in place will observe stale state.

        No per-document single-flight lock:
        Repeated ``sage_reabstract`` calls against the same
        ``document_id`` while a prior reabstract is still in-flight
        dispatch additional parallel background tasks; the later
        background writer wins via ``_locks.lock(document_id)`` on the
        final document update, but earlier tasks' completed work is
        silently overwritten. Callers receive no contention signal in
        the ``reabstract_started`` response. Debounce repeated calls
        client-side (e.g., wait for ``pipeline_status`` to leave
        ``abstraction_in_progress`` before re-issuing). The structural
        fix (per-document single-flight lock at dispatch time) is
        T-0202; this docstring discloses the gap as-is.

        Process-crash stuck-state recovery:
        The background task's exception handler catches Python-level
        ``Exception`` and stamps ``pipeline_status=failed``, but a
        process-level kill (``SIGKILL``, OOM kill, kernel panic) during
        background reabstract leaves the document stuck in
        ``pipeline_status=abstraction_in_progress`` with no terminal
        stamp. Recovery after such a crash: after process restart,
        enumerate documents with
        ``sage_discover(mode="catalog", filters={"pipeline_status":
        "abstraction_in_progress"})`` and re-issue ``sage_reabstract``
        against each stuck id.

        Error modes:
        - ``invalid_vault_id`` (400): ``vault_id`` failed ``VaultIdStr``
          typed-alias validation at the boundary (per CAS Typed-Alias
          Boundary Conventions). The alias enforces a non-empty
          slug-shaped identifier; malformed inputs are caught here
          rather than at a downstream lookup.
        - ``invalid_document_id`` (400): ``document_id`` failed
          ``DocumentIdStr`` typed-alias validation at the boundary
          (per CAS Typed-Alias Boundary Conventions).
        - ``document_not_found`` (404): no document with that id.
        - ``no_projection`` (404): the document has no stored chunks
          to abstract from.

        Note: error modes above are raised synchronously and reported
        in the call's response envelope. Background-task failures
        (abstraction provider error, content-store read failure, etc.)
        are NOT surfaced in this tool's response; they manifest as
        ``pipeline_status=failed`` and a populated ``pipeline_error``
        field on the document, observable via ``sage_get_document``.

        Args:
            vault_id: Target vault identifier. See ``invalid_vault_id``
                in Error modes.
            document_id: Document to re-abstract. See
                ``invalid_document_id`` in Error modes; the fire-and-
                forget dispatch and polling recipe are described in
                "Fire-and-forget semantics" above.
        """
        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            document_id = _DOCUMENT_ID_ADAPTER.validate_python(document_id)
            v = get_vault(vault_id)
            result = await v.ingestion_service.reabstract(document_id)
            return serialize(result)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    # -------------------------------------------------------------------
    # SAGE admin / maintenance API tools (CAS-ADR-029)
    #
    # Family-shared preconditions for every ``sage_admin_*`` tool below:
    #
    # 1. ``vault_id`` is validated through the ``VaultIdStr`` typed alias
    #    (``_VAULT_ID_ADAPTER.validate_python``) before any vault lookup.
    #    Inputs that violate the typed-alias shape raise a structured
    #    ``ValueError`` rather than reaching the registry. See the CAS
    #    Typed-Alias Boundary Conventions for the shared validation
    #    contract.
    #
    # 2. The targeted vault must have been initialized with a
    #    ``registry_service``; otherwise ``v.maintenance_service`` is
    #    ``None`` and the tool raises ``RuntimeError``. This is primarily
    #    a test-fixture concern (production vault construction wires
    #    ``registry_service`` by default), but agents and integration
    #    tests that build vaults directly without the registry will hit
    #    this error rather than a silent no-op. The maintenance/admin
    #    API surface is governed by CAS-ADR-029.
    #
    # Per-tool docstrings cross-reference this block rather than
    # repeating these two rules inline.
    # -------------------------------------------------------------------

    @mcp.tool()
    async def sage_admin_migrate_vault(vault_id: str) -> dict:
        """Apply pending schema migrations to a single vault in the running session.

        Pilot of the maintenance/admin API surface (CAS-ADR-029). Wraps
        the GraphStore.initialize(migrate=True) codepath: detects pending
        ALTER TABLE migrations and BACKFILL_PLAN entries, applies them
        if any are pending, then reloads the vault in this MCP process's
        registry so subsequent operations observe the new schema.

        Common preconditions:
        See the ``sage_admin_*`` family preconditions block above for
        shared rules (``vault_id`` typed-alias validation,
        ``maintenance_service`` wiring requirement).

        Idempotent: a re-call against an already-migrated vault returns
        a MigrationReport with empty ``columns_added`` and
        ``backfills_applied`` lists and no error; the registry reload is
        skipped on the no-op path.

        Cross-process staleness caveat (F-10, CAS-ADR-029): the
        operation closes and reopens this MCP process's view of the
        vault, but is not bulletproof if other MCP server processes
        hold imports of the same vault directory. Cross-process
        staleness requires the caller to restart any other open MCP
        sessions to observe the new schema.

        Migration is NOT atomic -- partial-failure window:
        On the migrate-needed path the flow is close-old-graph-store,
        then construct a fresh GraphStore over the same db_path solely
        to run ``initialize(migrate=True)``, close that fresh store,
        then ask the registry to reload the vault. If
        ``fresh.initialize(migrate=True)`` raises (or the subsequent
        reload raises -- see the inherited reload-atomicity row below)
        after ``self._graph_store.close()`` has already returned, the
        in-memory state is corrupted: the registry holds services
        whose graph_store is closed, and every subsequent tool call
        against the vault hits the closed-graph-store error path.
        Recovery options: (a) re-issue the migration after fixing the
        underlying cause (which itself races the same window), or
        (b) a process restart. This mirrors the ``sage_reload_vault``
        atomicity hazard documented under T-0180; the structural fix
        for the outer pre-reload migration sequence is tracked as
        T-0201.

        T-0115 tier3 uniqueness activation (CAS-ADR-031):
        After the schema-migration step settles, every ``unique_keys``
        declaration in vault config is scanned. Clean declarations get
        partial UNIQUE indexes installed under
        ``(doc_type, json_extract(tier3_metadata, '$.<field>'))``;
        declarations whose existing data violates the constraint are
        recorded in MigrationReport's ``tier3_uniqueness_collisions``
        list, the substrate refuses to activate the index (see
        ``Tier3UniqueIndexBlockedError`` in the Error modes block
        below), and any previously-clean partial index for that
        declaration is preserved (no implicit DROP). Successfully
        activated declarations are listed in
        ``tier3_uniqueness_activations``. **Callers must inspect both
        fields** -- a MigrationReport with empty ``columns_added`` and
        ``backfills_applied`` may still carry tier3 activations or
        collisions from this scan. The ``unique_keys`` vocabulary
        lives in vault config; query ``sage_get_vault_config`` for the
        authoritative declarations.

        Inherited post-migration reload-atomicity:
        After the migration step the call invokes
        ``_registry_service.reload(...)``. Per T-0183 the reload itself
        is now atomic (the registry restores the prior services on
        failure), but T-0183 closes only the inner reload step; the
        outer migration-then-reload sequence remains non-atomic per
        the row above. See ``sage_reload_vault`` for the in-place
        reload atomicity disclosures; see T-0201 for the open
        structural fix that extends T-0183 to wrap the outer sequence.

        Error modes:
        - ``vault_not_found`` (404): no vault registered with that id.
        - ``schema_migration_required`` (409): raised when a downstream
          operation detects pending migrations without ``--migrate``;
          this tool's purpose is precisely to clear that state, but
          chained operations triggered post-migration may surface it
          if a second migration is queued behind the first.
        - MIGRATION_PLAN errors (500): individual ALTER TABLE
          statements in ``MIGRATION_PLAN`` may fail at
          ``fresh.initialize(migrate=True)``; the error surface depends
          on the offending DDL (SQLite ``OperationalError`` rewrapped
          as the SAGEError envelope). Compounds with the migration-
          atomicity gap above.
        - BACKFILL_PLAN errors (500): individual ``BACKFILL_PLAN``
          entries may fail at the post-schema-update backfill step;
          same compound behavior as MIGRATION_PLAN errors.
        - ``Tier3UniqueIndexBlockedError`` (RuntimeError): a tier3
          ``unique_keys`` declaration's underlying data violates the
          requested constraint, so the partial UNIQUE index cannot be
          installed. The collision report is captured in the returned
          MigrationReport's ``tier3_uniqueness_collisions`` field per
          the T-0115 row above; the substrate does not auto-resolve.

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

    @mcp.tool()
    async def sage_admin_detect_drift(vault_id: str) -> dict:
        """Audit active sync_target / derived_from edges for drift (T-0111).

        Walks every active provenance-bearing edge in the vault and
        compares its recorded ``synced_from_*`` fields against the
        current head of the source's supersedes chain. Returns a
        DriftReport whose ``entries`` enumerate edges that need
        operator attention; current edges are absent from the report.

        Hash is the authoritative comparator; ``synced_from_version``
        is a display key.

        Replaces the manual hand-walk phase of the verbatim-sync and
        terminology-remediation workflows.

        Common preconditions (CAS-ADR-029):
        See the ``sage_admin_*`` family preconditions block above for
        shared rules (``vault_id`` typed-alias validation,
        ``maintenance_service`` wiring requirement).

        ``StalenessBasis`` bucket semantics (T-0111):
        Each ``DriftEntry`` carries a ``staleness_basis`` field
        classifying why the edge surfaced. Callers interpret a
        DriftReport against these four buckets without leaving this
        docstring:

        - ``content_drift``: the recorded ``synced_from_content_hash``
          differs from the current chain-head hash. Load-bearing
          "stale, act now" signal — re-sync the dependent artifact.
        - ``chain_advanced_no_content_change``: the chain has advanced
          past the recorded version, but the head's content hash
          still matches what was recorded. Informational — the
          provenance pointer is behind but the bytes are equivalent.
        - ``recorded_null``: the edge predates the T-0110 provenance
          columns (neither ``synced_from_version`` nor
          ``synced_from_content_hash`` is recorded). Informational —
          back-filling the provenance is optional cleanup, not a
          drift signal.
        - ``chain_nonlinear``: the source's supersedes chain forks
          (more than one head). Data-quality flag, not a drift
          signal; the chain must be reconciled before drift can be
          assessed against it. ``current_head_*`` fields are null on
          these entries; ``competing_head_count`` is populated.

        Error modes:
        - ``vault_not_found`` (404): no vault registered with the
          given ``vault_id`` in this MCP process.
        - ``chain_nonlinear`` (surfaced as ``DriftEntry`` rows, not
          an envelope error): chain forks are reported in-band per
          the bucket above rather than as a top-level failure, so
          one forked chain does not mask drift on other edges in the
          same vault.
        - Graph-store query failures (500): unexpected SQLite errors
          while walking provenance edges or resolving chain heads
          propagate as opaque server errors. These are infrastructure
          conditions, not caller bugs; retrying is appropriate.

        Args:
            vault_id: Target vault identifier. See the
                ``sage_admin_*`` family preconditions block above.
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

    @mcp.tool()
    async def sage_admin_reabstract_deferred_vault(
        vault_id: str, include_pdf: bool = False
    ) -> dict:
        """Backfill semantic abstracts for documents whose pipeline_status is abstraction_skipped.

        Graduation of the standalone scripts/reabstract_deferred.py
        script to the maintenance API surface (T-0089, CAS-ADR-029).
        Enumerates documents in the named vault whose pipeline_status is
        ``abstraction_skipped``, dispatches IngestionService.reabstract
        per document, and polls until each reaches terminal status
        (``abstraction_complete`` or ``failed``). Returns a
        ReabstractReport with per-document outcomes and aggregate counts.

        Common preconditions:
        See the ``sage_admin_*`` family preconditions block above for
        shared rules (``vault_id`` typed-alias validation,
        ``maintenance_service`` wiring requirement).

        Reuses the in-process AbstractionProvider that this MCP server
        loaded at startup; does NOT spin up a second Qwen3 instance
        (F-8 cautionary tale). The standalone script remains as the
        operator fallback for cron-style workflows where no MCP server
        is running.

        Ingestion-service wiring requirement (F-8 guard):
        ``MaintenanceService.reabstract_deferred_events`` raises
        ``RuntimeError`` synchronously (before generator iteration
        starts) when its ``MaintenanceService`` was constructed without
        an ``ingestion_service`` dependency. This is a defensive guard
        against the F-8 dual-AbstractionProvider hazard: the operator
        fallback path (``scripts/reabstract_deferred.py``) runs in a
        separate OS process and is allowed to self-initialize a
        provider; this in-process path is not. Production startup
        wires ``ingestion_service`` in via ``initialize_services``, so
        the gate fires almost exclusively against test fixtures that
        construct ``MaintenanceService`` directly without supplying an
        ingestion dependency. The guard distinguishes "this fixture is
        missing wiring" from the dual-provider hazard the F-8 rule
        prohibits.

        Single-flight per vault: a concurrent call returns a structured
        ``reabstract_already_in_flight`` error (409) whose detail
        carries the ``start_time`` of the in-flight operation. The
        operation is non-blocking on the rejection path -- a queued
        long-running second caller would mask client-side coordination
        bugs.

        Long-running: an N-document pass takes roughly N times the
        per-document abstraction wall-clock (seconds to tens of seconds
        each against Qwen3-30B MLX, sub-second against the test stub).
        The MCP tool returns a single ReabstractReport dict once the
        pass completes; allocate a generous client-side timeout.

        T-0134: the HTTP route now streams per-document SSE progress
        events, but the MCP-layer contract is unchanged. Under the
        hood, ``MaintenanceService.reabstract_deferred`` consumes the
        streaming generator and re-shapes the final summary event as a
        ``ReabstractReport``, so the dict this tool returns is
        structurally identical to the pre-streaming response shape.
        Callers that want per-document observability should subscribe
        to the HTTP route's SSE stream directly; the MCP tool exists
        for the report-and-return access pattern.

        Framework boundary -- ``include_pdf`` silent-drop compound risk:
        If your response indicates success but PDF documents were not
        processed as expected, check for a misspelled ``include_pdf``
        parameter name (e.g. ``includePdf``, ``includepdfs``, or
        ``pdf=True``). Unknown kwargs are silently dropped at the
        FastMCP framework boundary, which means the tool runs with
        ``include_pdf=False`` -- its default -- and PDFs are silently
        skipped despite caller intent to include them. The diagnostic
        signal is a successful ReabstractReport whose ``pdf_skipped``
        count matches the vault's PDF count even though the caller
        believed they had opted PDFs in. See T-0186 (framework-level
        FastMCP ``extra=forbid`` finding) and the T-0159 v2 cross-
        cutting compound-risk note for the underlying mechanism.

        Error modes:
        - ``vault_not_found`` (404): no vault registered with that id.
        - ``reabstract_already_in_flight`` (409): a reabstract is
          already running on this vault.
        - ``RuntimeError`` (ingestion-service guard): the vault's
          ``MaintenanceService`` was constructed without an
          ``ingestion_service`` dependency. See the Ingestion-service
          wiring requirement row above; test-fixture concern primarily.

        Args:
            vault_id: Target vault identifier.
            include_pdf: When False (default), source_type=pdf documents
                are skipped (scanned PDFs typically have no extractable
                text). When True, PDFs are included in the worklist.
                Note the FastMCP silent-drop compound risk documented
                above: a typo in this parameter name is dropped at the
                framework boundary and falls back to the False default.
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

    return {
        "sage_ingest": sage_ingest,
        "sage_parse_filename": sage_parse_filename,
        "sage_reabstract": sage_reabstract,
        "sage_get_document": sage_get_document,
        "sage_update_metadata": sage_update_metadata,
        "sage_set_lifecycle": sage_set_lifecycle,
        "sage_bulk_set_lifecycle": sage_bulk_set_lifecycle,
        "sage_bulk_link": sage_bulk_link,
        "sage_bulk_update_metadata": sage_bulk_update_metadata,
        "sage_link": sage_link,
        "sage_unlink": sage_unlink,
        "sage_check_preconditions": sage_check_preconditions,
        "sage_traverse": sage_traverse,
        "sage_chain": sage_chain,
        "sage_discover": sage_discover,
        "sage_read_projection": sage_read_projection,
        "sage_read_section": sage_read_section,
        "sage_list_headings": sage_list_headings,
        "sage_refresh_views": sage_refresh_views,
        "sage_list_vaults": sage_list_vaults,
        "sage_create_vault": sage_create_vault,
        "sage_get_vault_config": sage_get_vault_config,
        "sage_update_vault_config": sage_update_vault_config,
        "sage_vault_stats": sage_vault_stats,
        "sage_hash_check": sage_hash_check,
        "sage_list_staging_edges": sage_list_staging_edges,
        "sage_update_staging_edge": sage_update_staging_edge,
        "sage_pending_metadata": sage_pending_metadata,
        "sage_admin_migrate_vault": sage_admin_migrate_vault,
        "sage_admin_detect_drift": sage_admin_detect_drift,
        "sage_admin_reabstract_deferred_vault": sage_admin_reabstract_deferred_vault,
    }
