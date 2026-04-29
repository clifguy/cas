"""SAGE protocol and API tools for MCP.

Contains all tools that operate directly on the SAGE graph store and
services: protocol tools (ingest, get, update, lifecycle, link, traverse,
discover, export, refresh) and API query tools (vault stats, hash check,
staging edges, pending metadata).
"""

import uuid as _uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from sage.api.errors import (
    ContentDeliveryConflictError,
    DestructiveConfigChangeError,
    DocumentNotFoundError,
    EdgeNotFoundError,
    SAGEError,
    StagingEdgeNotFoundError,
    VaultAlreadyExistsError,
    VaultConfigValidationError,
    VaultNotFoundError,
)
from sage.api.routers.documents import attach_inline_content, deliver_to_path
from sage.mcp_init import SAGEServices, initialize_services, reload_vault_in_registry
from sage.vault_management import (
    _ALL_SECTIONS,
    _check_destructive_changes,
    _config_path_for_vault,
    _get_default_config,
    _validate_config,
    _write_config_yaml,
)
from sage.models.schemas import (
    ChainRequest,
    DiscoverRequest,
    DocumentWithContent,
    Edge,
    IngestRequest,
    LinkRequest,
    RetrievalFilters,
    SetLifecycleRequest,
    TraverseRequest,
    UpdateMetadataRequest,
)


def register_sage_tools(
    mcp: FastMCP,
    get_vault: Callable[[str], SAGEServices],
    serialize: Callable[[object], dict],
    error_response: Callable[[SAGEError | ValueError], dict],
    vaults: dict[str, SAGEServices],
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
                content hash must differ from the new file.
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
            )
            # Fire-and-forget pipeline keeps this RPC under the 60s MCP
            # client timeout (BH-130). Callers poll sage_get_document
            # for terminal pipeline_status.
            result = await v.ingestion_service.ingest(
                request, wait_for_pipeline=False
            )
            return serialize(result.document)
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
          Fails with 413 if the file exceeds the inline ceiling
          (default 100 MB; override via SAGE_MAX_INLINE_CONTENT_BYTES).
          Best for small files.
        - write_to_path=/abs/path: SAGE writes the bytes to the
          filesystem path; response carries only metadata (written_to,
          content_size, content_hash). Preferred for files that would
          exceed MCP tool-result size ceilings.

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
            if include_content and write_to_path:
                raise ContentDeliveryConflictError()

            doc = await v.graph_store.get_document(document_id)
            if doc is None:
                raise DocumentNotFoundError(document_id)

            response = DocumentWithContent(**doc.model_dump())
            storage_root = (
                Path(v.config.vault.storage_root).expanduser().resolve()
            )

            if include_content:
                attach_inline_content(response, doc, storage_root)
            elif write_to_path:
                deliver_to_path(response, doc, storage_root, write_to_path)

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
    ) -> dict:
        """Update mutable metadata fields on a document. Only include fields
        you want to change.

        Args:
            vault_id: Target vault identifier.
            document_id: The document's unique identifier.
            title: New display title.
            version_label: Version indicator (v1, v2, draft, final, etc.).
            project: Project or workstream identifier.
            tags: Freeform tags.
            doc_type: Document type (must be defined in vault config).
            authority_scope: Authority scope identifier.
            document_date: Document calendar date (YYYY-MM-DD). Use to
                correct fallback-derived dates that misattributed across
                UTC midnight.
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

        Args:
            vault_id: Target vault identifier.
            document_id: The document's unique identifier.
            action: Lifecycle action name (must be defined in vault config).
            new_version_id: Required if action is "supersede".
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

        Args:
            vault_id: Target vault identifier.
            display_name: User display name.
            type: User type (human or agent).
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
        endpoint. Violations surface as 400 errors with codes
        `edge_anchor_policy_violation`, `retract_target_not_edge`,
        `merged_from_validation`, or `tbd_policy_edge`.

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

        Args:
            vault_id: Target vault identifier.
            edge_id: Production edge identifier.
        """
        try:
            v = get_vault(vault_id)
            result = await v.graph_ops_service.unlink(edge_id)
            return serialize(result)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_check_preconditions(vault_id: str, function_id: str) -> dict:
        """Check whether all depends_on targets for a function document are
        satisfied (active or completed lifecycle, pipeline complete).

        Args:
            vault_id: Target vault identifier.
            function_id: The function document's identifier.
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

        Args:
            vault_id: Target vault identifier.
            start_id: Starting document identifier.
            edge_type: Filter by edge type (optional).
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
        but works with any edge type.

        Args:
            vault_id: Target vault identifier.
            document_id: Document ID to start the chain walk from.
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
                lifecycle_status, tags, document_ids, pipeline_status.
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
    async def sage_read_section(
        vault_id: str, document_id: str, heading_path: str
    ) -> dict:
        """Read a section of a document by heading path.

        Returns clean readable text for a heading subtree without loading
        the full document. Uses structural prefix matching: requesting
        "Technical Description" returns that heading plus all children
        (e.g. "Technical Description > Composite Claim Binding").

        Use this instead of sage_discover deterministic mode when you need
        readable text rather than search-formatted chunks.

        Args:
            vault_id: Target vault identifier.
            document_id: The document's unique identifier.
            heading_path: Heading path prefix (e.g. "Technical Description > Composite Claim Binding").
        """
        try:
            v = get_vault(vault_id)
            response = await v.utilities_service.read_section(
                document_id, heading_path
            )
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_refresh_views(vault_id: str) -> dict:
        """Regenerate browsable symlink views (by_doc_type/, by_lifecycle/)
        in the vault's storage root.

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
        summaries = []
        for vid, svc in vaults.items():
            summaries.append({
                "id": vid,
                "name": svc.config.vault.name,
                "description": getattr(svc.config.vault, "description", None),
                "storage_root": svc.config.vault.storage_root,
            })
        return {
            "vaults": summaries,
            "count": len(summaries),
        }

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
                config_dict = _get_default_config(vault_id, name, owner)
            else:
                config_dict = config

            validated = _validate_config(config_dict)
            vid = validated.vault.id

            if vid in vaults:
                raise VaultAlreadyExistsError(vid)

            config_path = _config_path_for_vault(vid)
            config_path.parent.mkdir(parents=True, exist_ok=True)
            Path(validated.vault.storage_root).expanduser().mkdir(
                parents=True, exist_ok=True
            )
            Path(validated.vault.brain_root).expanduser().mkdir(
                parents=True, exist_ok=True
            )

            _write_config_yaml(config_path, config_dict)

            services = await initialize_services(validated)
            vaults[vid] = services

            await services.user_service.bootstrap_owner()

            return {
                "vault_id": vid,
                "name": validated.vault.name,
                "storage_root": validated.vault.storage_root,
                "config": config_dict,
            }
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_get_vault_config(vault_id: str) -> dict:
        """Return the full vault configuration as a dict.

        Args:
            vault_id: Target vault identifier.
        """
        try:
            v = get_vault(vault_id)
            return v.config.model_dump()
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
        a destructive_config_change error and the affected counts are
        reported in the error detail. Pass force=True to proceed
        anyway; the warnings then appear in the success response.

        Changing `vault.id` is never permitted regardless of force -- use
        sage_create_vault to make a new vault instead.

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
            if vault_id not in vaults:
                raise VaultNotFoundError(vault_id)

            services = vaults[vault_id]
            old_config = services.config

            merged = old_config.model_dump()
            for section_name, section_value in sections.items():
                if section_name not in _ALL_SECTIONS:
                    raise VaultConfigValidationError(
                        [f"Unknown config section: {section_name}"]
                    )
                merged[section_name] = section_value

            if "vault" in sections and sections["vault"].get("id") != vault_id:
                raise VaultConfigValidationError(
                    ["vault.id cannot be changed; create a new vault instead"]
                )

            new_config = _validate_config(merged)

            warnings = await _check_destructive_changes(
                old_config, new_config, services.graph_store
            )
            if warnings and not force:
                raise DestructiveConfigChangeError(warnings)

            config_path = _config_path_for_vault(vault_id)
            _write_config_yaml(config_path, merged)

            await reload_vault_in_registry(vaults, vault_id, new_config)

            return {
                "status": "updated",
                "vault_id": vault_id,
                "warnings": warnings,
            }
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_vault_stats(vault_id: str) -> dict:
        """Vault statistics and health indicators.

        Args:
            vault_id: Target vault identifier.
        """
        try:
            v = get_vault(vault_id)
            gs = v.graph_store

            total_docs = len(await gs.list_all_documents())
            by_lifecycle = await gs.get_document_counts_by_field("lifecycle_status")
            by_doc_type = await gs.get_document_counts_by_field("doc_type")
            by_adapter = await gs.get_document_counts_by_field("source_type")
            by_pipeline = await gs.get_document_counts_by_field("pipeline_status")

            total_edges = await gs.get_total_edge_count()
            by_edge_type = await gs.get_edge_counts_by_type()

            staging_count = await gs.count_staging_edges()
            pending_meta = len(await gs.list_pending_metadata_documents())

            deferred = by_pipeline.get("abstraction_skipped", 0)
            failed = by_pipeline.get("failed", 0)
            last_ingestion = await gs.get_last_ingestion_at()

            # Storage sizes
            brain_root = Path(v.config.vault.brain_root).expanduser()
            sqlite_path = brain_root / "graph.db"
            sqlite_size = sum(
                p.stat().st_size
                for suffix in ("", "-wal", "-shm")
                if (p := sqlite_path.with_name(sqlite_path.name + suffix)).exists()
            )
            lancedb_dir = brain_root / "lancedb"
            lancedb_size = (
                sum(f.stat().st_size for f in lancedb_dir.rglob("*") if f.is_file())
                if lancedb_dir.exists()
                else 0
            )

            result = {
                "total_documents": total_docs,
                "by_lifecycle_state": by_lifecycle,
                "by_doc_type": by_doc_type,
                "by_source_adapter": by_adapter,
                "total_edges": total_edges,
                "by_edge_type": by_edge_type,
                "staging_edge_count": staging_count,
                "lancedb_size_bytes": lancedb_size,
                "sqlite_size_bytes": sqlite_size,
                "last_ingestion_at": last_ingestion,
                "health": {
                    "pending_metadata_count": pending_meta,
                    "pending_edge_count": staging_count,
                    "deferred_abstract_count": deferred if v.config.abstraction.enabled else None,
                    "failed_ingestion_count": failed,
                },
            }
            return result
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_hash_check(vault_id: str, hashes: list[str]) -> dict:
        """Bulk hash existence check against the graph store.

        Args:
            vault_id: Target vault identifier.
            hashes: List of content hash strings (e.g. "sha256:abc...").
        """
        try:
            v = get_vault(vault_id)
            matches = await v.graph_store.find_documents_by_hashes(hashes)
            result = {}
            for h in hashes:
                if h in matches:
                    result[h] = {"exists": True, "document_id": matches[h]}
                else:
                    result[h] = {"exists": False}
            return result
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_list_staging_edges(vault_id: str) -> dict:
        """List Tier 2 suggested edges awaiting review.

        Args:
            vault_id: Target vault identifier.
        """
        try:
            v = get_vault(vault_id)
            edges = await v.graph_store.list_staging_edges()
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

        Args:
            vault_id: Target vault identifier.
            edge_id: Staging edge identifier.
        """
        try:
            v = get_vault(vault_id)
            gs = v.graph_store
            staging = await gs.get_staging_edge(edge_id)
            if staging is None:
                raise StagingEdgeNotFoundError(edge_id)

            production = Edge(
                id=str(_uuid.uuid4()),
                source_id=staging.source_id,
                target_id=staging.target_id,
                edge_type=staging.edge_type,
                created_at=datetime.now(timezone.utc),
                notes=f"Confirmed from staging edge {edge_id}",
                rationale=staging.inference_evidence,
            )
            await gs.insert_edge(production)
            await gs.delete_staging_edge(edge_id)
            return serialize({
                "confirmed": True,
                "staging_edge_id": edge_id,
                "production_edge_id": production.id,
            })
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_dismiss_staging_edge(vault_id: str, edge_id: str) -> dict:
        """Dismiss a staging edge: delete it without creating a production edge.

        Args:
            vault_id: Target vault identifier.
            edge_id: Staging edge identifier.
        """
        try:
            v = get_vault(vault_id)
            gs = v.graph_store
            staging = await gs.get_staging_edge(edge_id)
            if staging is None:
                raise StagingEdgeNotFoundError(edge_id)
            await gs.delete_staging_edge(edge_id)
            return serialize({"dismissed": True, "staging_edge_id": edge_id})
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_pending_metadata(vault_id: str) -> dict:
        """List documents with unconfirmed metadata.

        Args:
            vault_id: Target vault identifier.
        """
        try:
            v = get_vault(vault_id)
            docs = await v.graph_store.list_pending_metadata_documents()
            items = []
            for doc in docs:
                items.append({
                    "document": serialize(doc),
                    "extracted_fields": {},
                })
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
        "sage_reabstract": sage_reabstract,
        "sage_get_document": sage_get_document,
        "sage_update_metadata": sage_update_metadata,
        "sage_set_lifecycle": sage_set_lifecycle,
        "sage_register_user": sage_register_user,
        "sage_link": sage_link,
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
