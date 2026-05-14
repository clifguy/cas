"""SAGE protocol and API tools for MCP.

Contains all tools that operate directly on the SAGE graph store and
services: protocol tools (ingest, get, update, lifecycle, link, traverse,
discover, export, refresh) and API query tools (vault stats, hash check,
staging edges, pending metadata).
"""

from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from pydantic import TypeAdapter

from sage.api.errors import (
    SAGEError,
    VaultConfigValidationError,
)
from sage.mcp_init import SAGEServices
from sage.models.enums import SourceType
from sage.models.schemas import (
    ChainRequest,
    CreateVaultRequest,
    DiscoverRequest,
    EdgeIdStr,
    HashCheckRequest,
    IngestRequest,
    LinkRequest,
    RetrievalFilters,
    SetLifecycleRequest,
    TraverseRequest,
    UpdateMetadataRequest,
    UpdateVaultConfigRequest,
)
from sage.services.vault_registry import VaultRegistryService

_EDGE_ID_ADAPTER: TypeAdapter[str] = TypeAdapter(EdgeIdStr)


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
        adapter: str,
        config: dict | None = None,
        created_by: str | None = None,
        force: bool = False,
        supersedes_document_id: str | None = None,
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

        Error modes:
        - ``adapter_not_found`` (400): ``adapter`` is not an enabled
          adapter on this vault. See
          ``source_adapters.adapters`` in ``sage_get_vault_config``.
        - ``source_file_not_found`` (404): ``source`` does not resolve
          to a readable file on disk.
        - ``duplicate_content`` (409): a document with the same
          ``source_path`` and content hash already exists. Override
          with ``force=true`` to re-ingest.
        - ``supersede_target_not_active`` (409):
          ``supersedes_document_id`` was set but the predecessor is
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

        Args:
            vault_id: Target vault identifier.
            source: Source file path relative to the vault's storage_root,
                or an absolute path to an external file. External files are
                copied verbatim into the vault's imports/ directory. The
                vault's internal copy at storage_root/source_path is the
                authoritative file after ingestion; the path passed here
                is temporary and can be deleted by the caller.
            adapter: Source format adapter (markdown, docx, pdf, email, onenote, teams_chat).
            config: Adapter-specific configuration (optional).
            created_by: Creator name. Defaults to vault owner.
            force: Allow re-ingestion of duplicate content.
            supersedes_document_id: When provided, the ingested document
                supersedes this predecessor. SAGE applies the `supersede`
                lifecycle transition synchronously with record insertion:
                creates a `supersedes` edge (new -> old) and archives
                the predecessor. The predecessor must be active and its
                content hash must differ from the new file. Per
                CAS-ADR-021, the trio fields (doc_type, project,
                authority_scope) inherit from the predecessor when the
                caller omits them and the predecessor's value is
                non-None.
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
                ``{"tier3": {"<field>": <value>}}`` with exact equality
                semantics (null matches absent-or-null fields).
        """
        try:
            v = get_vault(vault_id)
            request = IngestRequest(
                source=source,
                adapter=adapter,
                config=config,
                created_by=created_by,
                force=force,
                supersedes_document_id=supersedes_document_id,
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
        adapter: str,
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
        - ``adapter_not_found`` (400): ``adapter`` is not an enabled
          adapter on this vault.

        Args:
            vault_id: Target vault identifier.
            filename: Filename to parse. The basename is preferred;
                directory components are stripped before parsing.
            adapter: Source adapter (markdown, docx, pdf, email,
                onenote, teams_chat). Must be enabled on the vault.
        """
        try:
            v = get_vault(vault_id)
            adapter_enum = SourceType(adapter)
            response = v.ingestion_service.parse_filename(filename, adapter_enum)
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
        tags: list[str] | None = None,
        doc_type: str | None = None,
        authority_scope: str | None = None,
        document_date: str | None = None,
        tier3_metadata: dict | None = None,
    ) -> dict:
        """Update mutable metadata fields on a document. Only include fields
        you want to change.

        Per CAS-ADR-021, any call to this tool sets the document's
        ``metadata_confirmed=true`` flag (the document leaves the
        metadata-review queue if it was there), even when only one
        field is updated. To inspect remaining unconfirmed documents,
        use ``sage_pending_metadata``.

        The ``doc_type`` value must be one of the values defined under
        ``document_types.doc_types`` in the vault config. The set is
        vault-config-defined; query ``sage_get_vault_config`` for the
        authoritative list. The ``tags`` argument replaces the existing
        tag set wholesale; pass the full intended list.

        ``tier3_metadata`` (T-0004) is replaced wholesale at the top
        level (no deep merge). To edit a single field, read the current
        tier3_metadata via ``sage_get_document`` first, mutate, and pass
        the full dict back.

        Error modes:
        - ``document_not_found`` (404): no document with that id.
        - ``tier3_schema_violation`` (400): ``tier3_metadata`` is set
          but the document's doc_type has no ``metadata_schema``
          declared, or the payload failed validation against the
          declared schema. Detail carries ``doc_type``, ``path``,
          ``message``, and ``instance``. When the same call also
          changes ``doc_type``, validation runs against the new
          doc_type.

        Args:
            vault_id: Target vault identifier.
            document_id: The document's unique identifier.
            title: New display title.
            version_label: Version indicator (v1, v2, draft, final, etc.).
            project: Project or workstream identifier.
            tags: Freeform tags. Replaces the existing tag list; pass
                the complete intended list, not a delta.
            doc_type: Document type. Must be defined in
                ``document_types.doc_types`` in the vault config.
            authority_scope: Authority scope identifier.
            document_date: Document calendar date (YYYY-MM-DD). Use to
                correct fallback-derived dates that misattributed across
                UTC midnight.
            tier3_metadata: Replacement Tier 3 (per-doc_type typed)
                metadata dict. Validated against the doc_type's
                ``metadata_schema`` in vault config; see T-0004 in the
                CAS architecture.
        """
        try:
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
            )
            doc = await v.metadata_service.update_metadata(
                document_id, request, v.config.vault.owner
            )
            return serialize(doc)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_set_lifecycle(
        vault_id: str,
        document_id: str,
        action: str,
        new_version_id: str | None = None,
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
        new_version_id=...)`` against the predecessor or
        ``sage_ingest(..., supersedes_document_id=<predecessor_id>)``
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
          with ``new_version_id`` whose content hash matches the
          predecessor.

        Args:
            vault_id: Target vault identifier.
            document_id: The document's unique identifier (the
                document the transition acts upon -- for supersede,
                this is the predecessor being archived).
            action: Lifecycle action name. Must appear in this
                vault's ``lifecycle.transitions`` table as a valid
                action from the document's current state. See
                ``sage_get_vault_config`` for the authoritative list.
            new_version_id: The successor document's id. Required when
                ``action="supersede"`` (a ``supersedes`` edge is created
                from ``new_version_id`` -> ``document_id``); forbidden
                for all other actions. The successor must already exist
                as a separate active document; this tool does not
                create it. For the common case where the successor has
                not yet been ingested, prefer
                ``sage_ingest(..., supersedes_document_id=<predecessor_id>)``,
                which ingests and supersedes atomically.
        """
        try:
            v = get_vault(vault_id)
            request = SetLifecycleRequest(action=action, new_version_id=new_version_id)
            response = await v.lifecycle_service.set_lifecycle(document_id, request)
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_register_user(
        vault_id: str,
        display_name: str,
        type: str,
    ) -> dict:
        """Register a new human or agent user in the vault.

        The user id returned can be used as the ``created_by`` value
        on subsequent ingest calls and is recorded as the actor on
        lifecycle and edge mutations. Registration is the canonical
        attribution source; ad-hoc strings in ``created_by`` will be
        accepted but are not linked to a registered user record.

        Args:
            vault_id: Target vault identifier.
            display_name: User display name. Stored verbatim; not
                required to be unique.
            type: User type. Must be one of ``human`` or ``agent``.
                The distinction is informational (attribution audit)
                and does not gate any tool's behavior.
        """
        try:
            from sage.models.schemas import RegisterUserRequest

            v = get_vault(vault_id)
            request = RegisterUserRequest(display_name=display_name, type=type)
            user = await v.user_service.register_user(request)
            return serialize(user)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_link(
        vault_id: str,
        source_id: str,
        target_id: str | None,
        edge_type: str,
        source_valid_from_version: str | None = None,
        target_valid_from_version: str | None = None,
        retracted_edge_id: str | None = None,
        notes: str | None = None,
        rationale: str | None = None,
    ) -> dict:
        """Create a typed edge between two documents in the graph.

        **For ``supersedes`` edges, prefer
        ``sage_set_lifecycle(action="supersede", new_version_id=...)``**
        (or ``sage_ingest(..., supersedes_document_id=...)`` when the
        successor has not yet been ingested). Those tools wire the
        edge AND archive the predecessor atomically. ``sage_link`` with
        ``edge_type="supersedes"`` creates the edge alone and does
        **not** transition the predecessor's lifecycle; reach for it
        only when stitching a missing edge into a chain whose
        lifecycle states are already correct.

        Per CAS-ADR-017, each edge type has a registry-declared
        resolution_policy that dictates which anchor fields are required
        or forbidden:

        - `none` (supersedes, retracts, merged_from): meta-edges; no
          anchor fields. `retracts` additionally takes a one-sided
          `source_valid_from_version` (anchor in the retracting chain)
          and `retracted_edge_id` (the edge being retracted) instead of
          a `target_id`.
        - `transitive_source` (derived_from): requires
          `source_valid_from_version`; no target anchor.
        - `transitive_both` (covers, references, bundles_with,
          depends_on, instantiated_from): requires both
          `source_valid_from_version` and `target_valid_from_version`.

        Anchors must lie in the supersedes lineage of their respective
        endpoint. Violations surface as 400 errors:

        - ``edge_anchor_policy_violation``: anchor field missing where
          required, present where forbidden, or pointing at a document
          not in the endpoint's supersedes lineage. Anchor values are
          ``documents.id`` strings, not version labels -- passing a
          version label (e.g. ``"v9.0"``) returns this code with a
          ``does not reference a known document`` detail.
        - ``retract_target_not_edge``: the value supplied to
          ``retracted_edge_id`` is not a known edge id.
        - ``merged_from_validation``: a ``merged_from`` edge violates
          the merge-tombstone invariants.
        - ``tbd_policy_edge``: the requested edge_type has no shipped
          resolution policy and cannot be created.
        - ``self_referential_edge``: source and target resolve to the
          same document.

        Args:
            vault_id: Target vault identifier.
            source_id: Source document identifier.
            target_id: Target document identifier. Required for all edge
                types except `retracts` (which uses `retracted_edge_id`);
                pass null for `retracts` edges.
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
                retracted. Required (and valid only) on `retracts` edges.
            notes: Free-text notes about the edge.
            rationale: Rationale for creating this edge.
        """
        try:
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
            )
            edge = await v.graph_ops_service.link(request)
            return serialize(edge)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_unlink(vault_id: str, edge_id: str) -> dict:
        """Delete a production edge from the graph.

        For staging-table edges (pre-confirmation), use
        ``sage_dismiss_staging_edge`` instead. The two tables are
        distinct and edge ids do not cross between them; an id minted
        in staging is not valid here once promoted, and vice versa.

        Error modes:
        - ``edge_not_found`` (404): no production edge with that id.

        Args:
            vault_id: Target vault identifier.
            edge_id: Production edge identifier.
        """
        try:
            edge_id = _EDGE_ID_ADAPTER.validate_python(edge_id)
            v = get_vault(vault_id)
            result = await v.graph_ops_service.unlink(edge_id)
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
            v = get_vault(vault_id)
            result = await v.graph_ops_service.check_preconditions(function_id)
            return serialize(result)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_traverse(
        vault_id: str,
        start_id: str,
        edge_type: str | None = None,
        direction: str = "outbound",
        depth: int = 3,
        debug: bool = False,
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
            start_id: Starting document identifier.
            edge_type: Filter by edge type (optional). When omitted,
                traversal returns edges of all types.
            direction: Traversal direction (outbound, inbound, both). Default: outbound.
            depth: Maximum traversal depth (1-1000). Default: 3.
            debug: When true, populate `resolution_path` on the response
                with per-event entries (`anchor_hit`, `anchor_miss`,
                `retracts_applied`, `tombstone_applied`) explaining why
                each candidate edge was surfaced or suppressed. Default:
                false (zero overhead when disabled).
        """
        try:
            v = get_vault(vault_id)
            request = TraverseRequest(
                start_id=start_id,
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
        mode: str = "semantic",
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
        response_level: str = "chunks",
    ) -> dict:
        """Search for documents using semantic, keyword, catalog, or deterministic retrieval.

        Modes:
            semantic: Vector + optional BM25 fusion. Requires query.
            keyword: BM25-only search. Requires query. Use query="*" for filter-only listing.
            catalog: Filter-only SQL enumeration. No query needed. Returns document
                metadata only (no chunks or scores). Supports pagination via limit + offset.
                Best for deterministic enumeration by tags, doc_type, or other metadata.
            deterministic: Exact heading path extraction. Requires document_id + heading_path.

        Args:
            vault_id: Target vault identifier.
            mode: Retrieval mode (semantic, keyword, catalog, deterministic). Default: semantic.
            query: Search query text (required for semantic/keyword modes).
            scope: Retrieval scope (all, authoritative, specific, filtered). Default: all.
            filters: Scope filters with optional keys: doc_type, project,
                lifecycle_status, tags, document_ids, pipeline_status,
                tier3. The ``tier3`` key takes a dict of field-name to
                expected-value pairs that match against each document's
                ``tier3_metadata`` (T-0004). Equality is exact; ``null``
                in the expected value matches documents whose stored
                field is null or absent. All pairs AND together.
                Example: ``{"doc_type": "failure_record", "tier3":
                {"severity": "high", "fix_commit": null}}``.
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
            response_level: Result detail level ("chunks" or "documents"). "documents"
                suppresses chunk_content and heading_path, returning document metadata
                and relevance scores only. Default: "chunks".
        """
        try:
            v = get_vault(vault_id)
            retrieval_filters = RetrievalFilters(**filters) if filters else None
            request = DiscoverRequest(
                mode=mode,
                query=query,
                scope=scope,
                filters=retrieval_filters,
                document_id=document_id,
                heading_path=heading_path,
                limit=limit,
                offset=offset,
                use_hybrid=use_hybrid,
                use_abstract_prefilter=use_abstract_prefilter,
                include_abstracts=include_abstracts,
                min_relevance=min_relevance,
                response_level=response_level,
            )
            response = await v.retrieval_service.discover(request)
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_export_projection(
        vault_id: str,
        document_id: str,
        output_path: str,
    ) -> dict:
        """Export a document's projection text to a Markdown file.

        Error modes:
        - ``document_not_found`` (404): no document with that id.
        - ``no_projection`` (404): the document has no stored
          projection to export.
        - ``path_traversal_denied`` (400): ``output_path`` resolves
          outside the vault's ``storage_root``. SAGE refuses to write
          projection text to non-vault locations.

        Args:
            vault_id: Target vault identifier.
            document_id: The document's unique identifier.
            output_path: Target file path (relative to storage_root or absolute,
                must resolve within storage_root).
        """
        try:
            v = get_vault(vault_id)
            response = await v.utilities_service.export_projection(document_id, output_path)
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_read_projection(vault_id: str, document_id: str) -> dict:
        """Read a document's full text into context with metadata header.

        Returns the complete projection (reconstructed from stored chunks)
        as readable text, equivalent to uploading the document. Use this
        instead of sage_discover when you need to read an entire document.

        Error modes:
        - ``document_not_found`` (404): no document with that id.
        - ``no_projection`` (404): the document exists but has no
          stored projection (e.g. ingestion failed mid-pipeline or
          the document is awaiting reabstraction). Inspect
          ``pipeline_status`` via ``sage_get_document``; if recoverable,
          ``sage_reabstract`` may restore the projection.

        Args:
            vault_id: Target vault identifier.
            document_id: The document's unique identifier.
        """
        try:
            v = get_vault(vault_id)
            response = await v.utilities_service.read_projection(document_id)
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
            v = get_vault(vault_id)
            response = await v.utilities_service.read_section(document_id, heading_path)
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
    async def sage_create_vault(
        vault_id: str | None = None,
        name: str | None = None,
        owner: str | None = None,
        config: dict | None = None,
    ) -> dict:
        """Create a new vault and register it with the running SAGE instance.

        Two modes, mutually exclusive:

        - Convenience mode: pass vault_id, name, and owner (with config=None).
          A minimal valid default config is generated, directories are
          created, vault_config.yaml is written, services are initialized,
          and the vault is registered. The full written config is echoed
          back in the response so the caller can follow up with
          sage_update_vault_config to adjust individual sections without
          a separate read.

        - Full-config mode: pass a complete config dict (with
          vault_id/name/owner all None). The dict is validated, and the
          vault is created from it.

        The new vault is registered with the running MCP server
        immediately; no restart is needed. The vault's
        ``vault_config.yaml`` is written under the vault root
        (default ``~/sage_vaults/<vault_id>/``).

        Error modes:
        - ``vault_already_exists`` (409): a vault with that
          ``vault_id`` is already registered.
        - ``vault_config_validation_error`` (400): the supplied
          config (in full-config mode) fails schema validation, or
          the convenience-mode arguments are mixed with a config dict.

        Args:
            vault_id: Unique identifier for the new vault (convenience mode).
            name: Human-readable display name (convenience mode).
            owner: Username of the vault owner (convenience mode).
            config: Full vault config dict (full-config mode only).
        """
        try:
            convenience_args_set = any(x is not None for x in (vault_id, name, owner))
            if convenience_args_set and config is not None:
                raise VaultConfigValidationError(
                    [
                        "Pass either (vault_id, name, owner) OR config, not both. "
                        "Mixing the convenience args with a full config dict is not allowed."
                    ]
                )

            if config is None:
                if vault_id is None or name is None or owner is None:
                    raise VaultConfigValidationError(
                        ["vault_id, name, and owner are all required when config is not provided"]
                    )
                config_dict = VaultRegistryService.get_default_config(vault_id, name, owner)
            else:
                config_dict = config

            summary = await vault_registry_service.create_vault(
                CreateVaultRequest(config=config_dict)
            )
            return {
                "vault_id": summary.id,
                "name": summary.name,
                "storage_root": summary.storage_root,
                "config": config_dict,
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
            services = get_vault(vault_id)
            return services.vault_config_service.get_config()
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_update_vault_config(
        vault_id: str,
        sections: dict,
        force: bool = False,
    ) -> dict:
        """Update vault configuration at the section level.

        Each key in `sections` replaces the corresponding top-level section
        of the config wholesale; sections you do not include are preserved
        unchanged. Partial-section merges are not supported -- if you pass
        `{"document_types": {"doc_types": [...]}}`, the entire
        `document_types` section is replaced by the dict you pass, so
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

        Error modes:
        - ``destructive_config_change`` (409): see above.
        - ``vault_config_validation_error`` (400): the merged config
          fails schema validation, or an unknown section name was
          passed, or the request attempts to change ``vault.id``.

        Args:
            vault_id: Target vault identifier.
            sections: Dict mapping top-level section name
                (vault, document_types, lifecycle, source_adapters,
                metadata_extraction, edge_inference, abstraction,
                access_control_defaults, retrieval_health) to the new
                section dict.
            force: When True, proceed even if the update would orphan
                existing documents. Default False.
        """
        try:
            services = get_vault(vault_id)
            valid_sections = set(UpdateVaultConfigRequest.model_fields.keys())
            for section_name in sections:
                if section_name not in valid_sections:
                    raise VaultConfigValidationError([f"Unknown config section: {section_name}"])
            body = UpdateVaultConfigRequest(**sections)
            return await services.vault_config_service.update_config(vault_id, body, force)
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
    async def sage_confirm_staging_edge(vault_id: str, edge_id: str) -> dict:
        """Confirm a staging edge: move it to the production edge table.

        The staging row is deleted and a new production edge is
        inserted with the same source, target, and edge_type. The
        returned edge carries the production ``edge_id``, which is
        distinct from the staging id passed in -- staging and production
        tables do not share an id space.

        Error modes:
        - ``staging_edge_not_found`` (404): the id is unknown
          (already confirmed, already dismissed, or never existed).

        Args:
            vault_id: Target vault identifier.
            edge_id: Staging edge identifier (from
                ``sage_list_staging_edges``).
        """
        try:
            edge_id = _EDGE_ID_ADAPTER.validate_python(edge_id)
            v = get_vault(vault_id)
            return serialize(await v.staging_edges_service.confirm_staging_edge(edge_id))
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_dismiss_staging_edge(vault_id: str, edge_id: str) -> dict:
        """Dismiss a staging edge: delete it without creating a production edge.

        The reviewer's judgment that the inferred edge is wrong. The
        staging row is removed and the underlying inference rule is
        not re-applied for the same (source, target, edge_type)
        combination during the current ingest cycle, but a future
        re-ingest that re-triggers the same inference rule will
        re-stage the candidate.

        Error modes:
        - ``staging_edge_not_found`` (404): the id is unknown.

        Args:
            vault_id: Target vault identifier.
            edge_id: Staging edge identifier (from
                ``sage_list_staging_edges``).
        """
        try:
            edge_id = _EDGE_ID_ADAPTER.validate_python(edge_id)
            v = get_vault(vault_id)
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
        """Re-run abstraction on an existing document. Reconstructs
        projection text from stored chunks, generates a new
        density-proportional semantic abstract, and writes it back
        to the document node.

        The generation uses the vault's currently-configured generic
        abstraction prompt (see ``abstraction.model`` in vault config);
        per-document or per-doc_type prompt overrides are not exposed
        here. If the new abstract is still off-topic after this call,
        the lever is a vault-config change to the abstraction prompt
        or model, not a re-issue of this tool.

        Error modes:
        - ``document_not_found`` (404): no document with that id.
        - ``no_projection`` (404): the document has no stored chunks
          to abstract from.

        Args:
            vault_id: Target vault identifier.
            document_id: Document to re-abstract.
        """
        try:
            v = get_vault(vault_id)
            result = await v.ingestion_service.reabstract(document_id)
            return serialize(result)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    return {
        "sage_ingest": sage_ingest,
        "sage_parse_filename": sage_parse_filename,
        "sage_reabstract": sage_reabstract,
        "sage_get_document": sage_get_document,
        "sage_update_metadata": sage_update_metadata,
        "sage_set_lifecycle": sage_set_lifecycle,
        "sage_register_user": sage_register_user,
        "sage_link": sage_link,
        "sage_unlink": sage_unlink,
        "sage_check_preconditions": sage_check_preconditions,
        "sage_traverse": sage_traverse,
        "sage_chain": sage_chain,
        "sage_discover": sage_discover,
        "sage_export_projection": sage_export_projection,
        "sage_read_projection": sage_read_projection,
        "sage_refresh_views": sage_refresh_views,
        "sage_list_vaults": sage_list_vaults,
        "sage_create_vault": sage_create_vault,
        "sage_get_vault_config": sage_get_vault_config,
        "sage_update_vault_config": sage_update_vault_config,
        "sage_vault_stats": sage_vault_stats,
        "sage_hash_check": sage_hash_check,
        "sage_list_staging_edges": sage_list_staging_edges,
        "sage_confirm_staging_edge": sage_confirm_staging_edge,
        "sage_dismiss_staging_edge": sage_dismiss_staging_edge,
        "sage_pending_metadata": sage_pending_metadata,
    }
