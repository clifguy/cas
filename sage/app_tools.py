"""Application backend tools for MCP.

Contains tools that depend on the CAS application backend (app.backend)
rather than SAGE core services directly: directory scanning and batch
ingestion.
"""

import json
from collections.abc import Callable
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from sage.api.errors import SAGEError
from sage.mcp_init import SAGEServices


def register_app_tools(
    mcp: FastMCP,
    get_vault: Callable[[str], SAGEServices],
    serialize: Callable[[object], str],
    error_response: Callable[[SAGEError | ValueError], str],
) -> dict[str, Callable]:
    """Register application backend tools on the MCP server.

    Returns a dict mapping tool function names to the actual functions,
    for re-export from mcp_server.
    """

    # -------------------------------------------------------------------
    # Application backend tools (MCP-015 through MCP-022)
    # -------------------------------------------------------------------

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
            v = get_vault(vault_id)
            d = Path(directory.strip("'\""))
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
            return error_response(e)

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

            v = get_vault(vault_id)

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
            return error_response(e)

    return {
        "app_scan_directory": app_scan_directory,
        "app_batch_ingest": app_batch_ingest,
    }
