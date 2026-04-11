"""SAGE MCP server -- thin adapter translating MCP tool calls to Core API.

Supports multiple vaults loaded at startup. Each tool takes vault_id as
its first parameter to select the target vault.

Usage:
    python -m sage.mcp_server <config1.yaml> [config2.yaml ...]
"""

import json
import sys
import uuid as _uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from sage.api.errors import (
    DocumentNotFoundError,
    SAGEError,
    StagingEdgeNotFoundError,
)
from sage.config import load_vault_config
from sage.mcp_init import SAGEServices, initialize_services
from sage.models.schemas import (
    DiscoverRequest,
    Edge,
    IngestRequest,
    LinkRequest,
    RetrievalFilters,
    SetLifecycleRequest,
    StagingEdge,
    TraverseRequest,
    UpdateMetadataRequest,
)

# ---------------------------------------------------------------------------
# Vault registry
# ---------------------------------------------------------------------------

_vaults: dict[str, SAGEServices] = {}


def _get_vault(vault_id: str) -> SAGEServices:
    """Look up services for a vault. Raises ValueError if unknown."""
    if vault_id not in _vaults:
        available = ", ".join(sorted(_vaults.keys())) or "(none)"
        raise ValueError(
            f"Unknown vault_id: {vault_id}. Available vaults: {available}"
        )
    return _vaults[vault_id]


def _serialize(obj: object) -> str:
    """Serialize a Pydantic model or dict to JSON string for MCP response."""
    if hasattr(obj, "model_dump"):
        return json.dumps(obj.model_dump(mode="json", exclude_none=True), indent=2)
    return json.dumps(obj, indent=2, default=str)


def _error_response(exc: SAGEError | ValueError) -> str:
    """Format a SAGE or vault-routing error as a JSON string for MCP response."""
    if isinstance(exc, SAGEError):
        payload: dict = {"error": exc.code, "message": exc.message}
        if exc.detail:
            payload["detail"] = exc.detail
    else:
        payload = {"error": "unknown_vault", "message": str(exc)}
    return json.dumps(payload, indent=2)


# ---------------------------------------------------------------------------
# Lifespan: load vault configs and initialize services
# ---------------------------------------------------------------------------

_config_paths: list[Path] = []


@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[None]:
    for config_path in _config_paths:
        config = load_vault_config(config_path)
        services = await initialize_services(config)
        _vaults[config.vault.id] = services

    yield

    for services in _vaults.values():
        await services.graph_store.close()
    _vaults.clear()


mcp = FastMCP("SAGE", lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


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
        v = _get_vault(vault_id)
        request = IngestRequest(
            source=source,
            adapter=adapter,
            config=config,
            created_by=created_by,
            force=force,
        )
        result = await v.ingestion_service.ingest(request)
        return _serialize(result.document)
    except (SAGEError, ValueError) as e:
        return _error_response(e)


@mcp.tool()
async def sage_get_document(vault_id: str, document_id: str) -> str:
    """Retrieve a document record with all metadata, lifecycle state, and
    pipeline status.

    Args:
        vault_id: Target vault identifier.
        document_id: The document's unique identifier.
    """
    try:
        v = _get_vault(vault_id)
        doc = await v.graph_store.get_document(document_id)
        if doc is None:
            raise DocumentNotFoundError(document_id)
        return _serialize(doc)
    except (SAGEError, ValueError) as e:
        return _error_response(e)


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
        v = _get_vault(vault_id)
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
        return _serialize(doc)
    except (SAGEError, ValueError) as e:
        return _error_response(e)


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
        v = _get_vault(vault_id)
        request = SetLifecycleRequest(action=action, new_version_id=new_version_id)
        response = await v.lifecycle_service.set_lifecycle(document_id, request)
        return _serialize(response)
    except (SAGEError, ValueError) as e:
        return _error_response(e)


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

        v = _get_vault(vault_id)
        request = RegisterUserRequest(display_name=display_name, type=type)
        user = await v.user_service.register_user(request)
        return _serialize(user)
    except (SAGEError, ValueError) as e:
        return _error_response(e)


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
        v = _get_vault(vault_id)
        request = LinkRequest(
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_type,
            notes=notes,
            rationale=rationale,
        )
        edge = await v.graph_ops_service.link(request)
        return _serialize(edge)
    except (SAGEError, ValueError) as e:
        return _error_response(e)


@mcp.tool()
async def sage_check_preconditions(vault_id: str, function_id: str) -> str:
    """Check whether all depends_on targets for a function document are
    satisfied (active or completed lifecycle, pipeline complete).

    Args:
        vault_id: Target vault identifier.
        function_id: The function document's identifier.
    """
    try:
        v = _get_vault(vault_id)
        result = await v.graph_ops_service.check_preconditions(function_id)
        return _serialize(result)
    except (SAGEError, ValueError) as e:
        return _error_response(e)


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
        depth: Maximum traversal depth (1-50). Default: 3.
    """
    try:
        v = _get_vault(vault_id)
        request = TraverseRequest(
            start_id=start_id,
            edge_type=edge_type,
            direction=direction,
            depth=depth,
        )
        response = await v.graph_ops_service.traverse(request)
        return _serialize(response)
    except (SAGEError, ValueError) as e:
        return _error_response(e)


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
    use_hybrid: bool = False,
) -> str:
    """Search for documents using semantic or deterministic retrieval.

    Args:
        vault_id: Target vault identifier.
        mode: Retrieval mode (semantic, deterministic).
        query: Search query text (required for semantic mode).
        scope: Retrieval scope (all, authoritative, specific, filtered). Default: all.
        filters: Scope filters with optional keys: doc_type, project,
            lifecycle_status, tags, document_ids.
        document_id: Target document (required for deterministic mode).
        heading_path: Heading path prefix (required for deterministic mode).
        limit: Maximum results (1-100). Default: 10.
        use_hybrid: Use hybrid RRF fusion of vector + BM25. Default: false.
    """
    try:
        v = _get_vault(vault_id)
        retrieval_filters = RetrievalFilters(**filters) if filters else None
        request = DiscoverRequest(
            mode=mode,
            query=query,
            scope=scope,
            filters=retrieval_filters,
            document_id=document_id,
            heading_path=heading_path,
            limit=limit,
            use_hybrid=use_hybrid,
        )
        response = await v.retrieval_service.discover(request)
        return _serialize(response)
    except (SAGEError, ValueError) as e:
        return _error_response(e)


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
        v = _get_vault(vault_id)
        response = await v.utilities_service.export_projection(document_id, output_path)
        return _serialize(response)
    except (SAGEError, ValueError) as e:
        return _error_response(e)


@mcp.tool()
async def sage_refresh_views(vault_id: str) -> str:
    """Regenerate browsable symlink views (by_doc_type/, by_lifecycle/)
    in the vault's storage root.

    Args:
        vault_id: Target vault identifier.
    """
    try:
        v = _get_vault(vault_id)
        response = await v.utilities_service.refresh_views()
        return _serialize(response)
    except (SAGEError, ValueError) as e:
        return _error_response(e)


# ---------------------------------------------------------------------------
# SAGE API tools for CAS Application (MCP-001 through MCP-014)
# ---------------------------------------------------------------------------


@mcp.tool()
async def sage_list_vaults() -> str:
    """Enumerate all configured vaults. No vault_id parameter -- operates
    across all registered vaults.
    """
    summaries = []
    for vid, svc in _vaults.items():
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
        v = _get_vault(vault_id)
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

        result = {
            "total_documents": total_docs,
            "by_lifecycle_state": by_lifecycle,
            "by_doc_type": by_doc_type,
            "by_source_adapter": by_adapter,
            "total_edges": total_edges,
            "by_edge_type": by_edge_type,
            "staging_edge_count": staging_count,
            "lancedb_size_bytes": 0,
            "sqlite_size_bytes": 0,
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
        return _error_response(e)


@mcp.tool()
async def sage_hash_check(vault_id: str, hashes: list[str]) -> str:
    """Bulk hash existence check against the graph store.

    Args:
        vault_id: Target vault identifier.
        hashes: List of content hash strings (e.g. "sha256:abc...").
    """
    try:
        v = _get_vault(vault_id)
        matches = await v.graph_store.find_documents_by_hashes(hashes)
        result = {}
        for h in hashes:
            if h in matches:
                result[h] = {"exists": True, "document_id": matches[h]}
            else:
                result[h] = {"exists": False}
        return json.dumps(result, indent=2)
    except (SAGEError, ValueError) as e:
        return _error_response(e)


@mcp.tool()
async def sage_list_staging_edges(vault_id: str) -> str:
    """List Tier 2 suggested edges awaiting review.

    Args:
        vault_id: Target vault identifier.
    """
    try:
        v = _get_vault(vault_id)
        edges = await v.graph_store.list_staging_edges()
        return json.dumps(
            [e.model_dump(mode="json") for e in edges], indent=2, default=str
        )
    except (SAGEError, ValueError) as e:
        return _error_response(e)


@mcp.tool()
async def sage_confirm_staging_edge(vault_id: str, edge_id: str) -> str:
    """Confirm a staging edge: move it to the production edge table.

    Args:
        vault_id: Target vault identifier.
        edge_id: Staging edge identifier.
    """
    try:
        v = _get_vault(vault_id)
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
        return _serialize({
            "confirmed": True,
            "staging_edge_id": edge_id,
            "production_edge_id": production.id,
        })
    except (SAGEError, ValueError) as e:
        return _error_response(e)


@mcp.tool()
async def sage_dismiss_staging_edge(vault_id: str, edge_id: str) -> str:
    """Dismiss a staging edge: delete it without creating a production edge.

    Args:
        vault_id: Target vault identifier.
        edge_id: Staging edge identifier.
    """
    try:
        v = _get_vault(vault_id)
        gs = v.graph_store
        staging = await gs.get_staging_edge(edge_id)
        if staging is None:
            raise StagingEdgeNotFoundError(edge_id)
        await gs.delete_staging_edge(edge_id)
        return _serialize({"dismissed": True, "staging_edge_id": edge_id})
    except (SAGEError, ValueError) as e:
        return _error_response(e)


@mcp.tool()
async def sage_pending_metadata(vault_id: str) -> str:
    """List documents with unconfirmed metadata.

    Args:
        vault_id: Target vault identifier.
    """
    try:
        v = _get_vault(vault_id)
        docs = await v.graph_store.list_pending_metadata_documents()
        items = []
        for doc in docs:
            items.append({
                "document": json.loads(_serialize(doc)),
                "extracted_fields": {},
            })
        return json.dumps(items, indent=2, default=str)
    except (SAGEError, ValueError) as e:
        return _error_response(e)


# ---------------------------------------------------------------------------
# Application backend tools (MCP-015 through MCP-022)
# ---------------------------------------------------------------------------


@mcp.tool()
async def app_scan_directory(
    vault_id: str,
    directory: str,
    max_depth: int | None = None,
) -> str:
    """Walk a directory, match files against vault adapters, hash files,
    parse filenames, and check hashes against the SAGE vault.

    Args:
        vault_id: Target vault identifier.
        directory: Absolute path to the directory to scan.
        max_depth: Max recursion depth (null = unlimited, 0 = no recursion).
    """
    from app.backend.scan import scan_directory

    try:
        v = _get_vault(vault_id)
        d = Path(directory)
        if not d.is_dir():
            return json.dumps({
                "error": "invalid_directory",
                "message": "Directory not found or not readable",
            }, indent=2)

        results, warnings = await scan_directory(
            directory=d,
            vault_config=v.config,
            graph_store=v.graph_store,
            max_depth=max_depth,
        )
        files = []
        for r in results:
            files.append(r.to_dict())
        return json.dumps({"files": files, "warnings": warnings}, indent=2, default=str)
    except (SAGEError, ValueError) as e:
        return _error_response(e)


@mcp.tool()
async def app_batch_ingest(
    vault_id: str,
    files: list[dict],
    infer_edges: bool = True,
) -> str:
    """Ingest multiple files with optional edge inference. Returns a
    summary when complete.

    Args:
        vault_id: Target vault identifier.
        files: List of file objects. Each has: file_path (str), adapter (str),
            and optional parsed_metadata (dict with title, date, project,
            codes, version, doc_type).
        infer_edges: When True (default), run two-phase edge inference.
            When False, ingest documents only with no edge creation or
            lifecycle transitions.
    """
    try:
        from app.backend.ingest_service import (
            BatchIngestService,
            FileDescriptor,
            ParsedMetadataInput,
        )

        v = _get_vault(vault_id)

        if not files:
            return json.dumps({
                "error": "empty_file_list",
                "message": "No files selected for ingestion",
            }, indent=2)

        # Convert raw dicts to FileDescriptors
        descriptors: list[FileDescriptor] = []
        for f in files:
            pm = f.get("parsed_metadata")
            parsed = None
            if pm:
                parsed = ParsedMetadataInput(
                    title=pm.get("title", Path(f["file_path"]).stem),
                    date=pm.get("date"),
                    project=pm.get("project"),
                    codes=pm.get("codes", []),
                    version=pm.get("version"),
                    doc_type=pm.get("doc_type"),
                )
            descriptors.append(FileDescriptor(
                file_path=f["file_path"],
                adapter=f["adapter"],
                parsed_metadata=parsed,
            ))

        svc = BatchIngestService()
        result = await svc.run(
            files=descriptors,
            vault_services=v,
            infer_edges=infer_edges,
        )
        return json.dumps(result.to_dict(), indent=2, default=str)
    except (SAGEError, ValueError) as e:
        return _error_response(e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m sage.mcp_server <config1.yaml> [config2.yaml ...]")
        sys.exit(1)

    for arg in sys.argv[1:]:
        path = Path(arg)
        if not path.exists():
            print(f"Config file not found: {path}")
            sys.exit(1)
        _config_paths.append(path)

    mcp.run(transport="stdio")
