"""SAGE protocol and API tools for MCP.

Contains all tools that operate directly on the SAGE graph store and
services: protocol tools (ingest, get, update, lifecycle, link, traverse,
discover, export, refresh) and API query tools (vault stats, hash check,
staging edges, pending metadata).
"""

import json
import uuid as _uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from sage.api.errors import (
    DocumentNotFoundError,
    EdgeNotFoundError,
    SAGEError,
    StagingEdgeNotFoundError,
)
from sage.mcp_init import SAGEServices
from sage.models.schemas import (
    ChainRequest,
    DiscoverRequest,
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
    serialize: Callable[[object], str],
    error_response: Callable[[SAGEError | ValueError], str],
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
    ) -> str:
        """Ingest a source file into SAGE. Runs the three-stage pipeline:
        projection, indexing, and abstraction.

        Args:
            vault_id: Target vault identifier.
            source: Source file path relative to the vault's storage_root,
                or an absolute path to an external file. External files are
                copied verbatim into the vault's imports/ directory.
            adapter: Source format adapter (markdown, docx, pdf, email, onenote, teams_chat).
            config: Adapter-specific configuration (optional).
            created_by: Creator name. Defaults to vault owner.
            force: Allow re-ingestion of duplicate content.
        """
        try:
            v = get_vault(vault_id)
            request = IngestRequest(
                source=source,
                adapter=adapter,
                config=config,
                created_by=created_by,
                force=force,
            )
            result = await v.ingestion_service.ingest(request)
            return serialize(result.document)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_get_document(vault_id: str, document_id: str) -> str:
        """Retrieve a document record with all metadata, lifecycle state, and
        pipeline status.

        Args:
            vault_id: Target vault identifier.
            document_id: The document's unique identifier.
        """
        try:
            v = get_vault(vault_id)
            doc = await v.graph_store.get_document(document_id)
            if doc is None:
                raise DocumentNotFoundError(document_id)
            return serialize(doc)
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
    ) -> str:
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
    ) -> str:
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
    ) -> str:
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
        target_id: str,
        edge_type: str,
        notes: str | None = None,
        rationale: str | None = None,
    ) -> str:
        """Create a typed edge between two documents in the graph.

        Args:
            vault_id: Target vault identifier.
            source_id: Source document identifier.
            target_id: Target document identifier.
            edge_type: Edge type (supersedes, derived_from, covers, references,
                bundles_with, authoritative_for, depends_on, sync_target).
            notes: Free-text notes about the edge.
            rationale: Rationale for creating this edge.
        """
        try:
            v = get_vault(vault_id)
            request = LinkRequest(
                source_id=source_id,
                target_id=target_id,
                edge_type=edge_type,
                notes=notes,
                rationale=rationale,
            )
            edge = await v.graph_ops_service.link(request)
            return serialize(edge)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_unlink(vault_id: str, edge_id: str) -> str:
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
    async def sage_check_preconditions(vault_id: str, function_id: str) -> str:
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
    ) -> str:
        """Walk the document graph from a starting document.

        Args:
            vault_id: Target vault identifier.
            start_id: Starting document identifier.
            edge_type: Filter by edge type (optional).
            direction: Traversal direction (outbound, inbound, both). Default: outbound.
            depth: Maximum traversal depth (1-1000). Default: 3.
        """
        try:
            v = get_vault(vault_id)
            request = TraverseRequest(
                start_id=start_id,
                edge_type=edge_type,
                direction=direction,
                depth=depth,
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
    ) -> str:
        """Walk an edge chain to both ends from a starting document.

        Returns an ordered list of all documents in the chain with
        positional metadata (head, tail, query position, linearity).
        Designed for version history retrieval on supersedes chains
        but works with any edge type.

        Args:
            vault_id: Target vault identifier.
            document_id: Document ID to start the chain walk from.
            edge_type: Edge type to follow (e.g. "supersedes", "references").
        """
        try:
            v = get_vault(vault_id)
            request = ChainRequest(
                document_id=document_id,
                edge_type=edge_type,
            )
            response = await v.graph_ops_service.chain(request)
            return serialize(response)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_discover(
        vault_id: str,
        mode: str,
        query: str | None = None,
        scope: str = "all",
        filters: dict | None = None,
        document_id: str | None = None,
        heading_path: str | None = None,
        limit: int = 10,
        offset: int = 0,
        use_hybrid: bool = True,
        use_abstract_prefilter: bool = True,
        response_level: str = "chunks",
    ) -> str:
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
            mode: Retrieval mode (semantic, keyword, catalog, deterministic).
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
    ) -> str:
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
    async def sage_read_projection(vault_id: str, document_id: str) -> str:
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
    async def sage_refresh_views(vault_id: str) -> str:
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
    async def sage_list_vaults() -> str:
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
        return json.dumps(summaries, indent=2)

    @mcp.tool()
    async def sage_vault_stats(vault_id: str) -> str:
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
            return json.dumps(result, indent=2, default=str)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_hash_check(vault_id: str, hashes: list[str]) -> str:
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
            return json.dumps(result, indent=2)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_list_staging_edges(vault_id: str) -> str:
        """List Tier 2 suggested edges awaiting review.

        Args:
            vault_id: Target vault identifier.
        """
        try:
            v = get_vault(vault_id)
            edges = await v.graph_store.list_staging_edges()
            return json.dumps(
                [e.model_dump(mode="json") for e in edges], indent=2, default=str
            )
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_confirm_staging_edge(vault_id: str, edge_id: str) -> str:
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
    async def sage_dismiss_staging_edge(vault_id: str, edge_id: str) -> str:
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
    async def sage_pending_metadata(vault_id: str) -> str:
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
                    "document": json.loads(serialize(doc)),
                    "extracted_fields": {},
                })
            return json.dumps(items, indent=2, default=str)
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def sage_reabstract(
        vault_id: str,
        document_id: str,
    ) -> str:
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
        "sage_vault_stats": sage_vault_stats,
        "sage_hash_check": sage_hash_check,
        "sage_list_staging_edges": sage_list_staging_edges,
        "sage_confirm_staging_edge": sage_confirm_staging_edge,
        "sage_dismiss_staging_edge": sage_dismiss_staging_edge,
        "sage_pending_metadata": sage_pending_metadata,
    }
