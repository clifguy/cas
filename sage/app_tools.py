"""Application backend tools for MCP.

Contains tools that depend on the CAS application backend (app.backend)
rather than SAGE core services directly: directory scanning and batch
ingestion.
"""

from collections.abc import Callable
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from sage.api.errors import SAGEError
from sage.mcp_init import SAGEServices


def register_app_tools(
    mcp: FastMCP,
    get_vault: Callable[[str], SAGEServices],
    serialize: Callable[[object], dict],
    error_response: Callable[[SAGEError | ValueError], dict],
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
    ) -> dict:
        """Walk a directory, match files against vault adapters, hash files,
        parse filenames, and check hashes against the SAGE vault.

        Side-effect free with respect to the vault: no documents are
        created, no metadata is written. Intended as the discovery
        step before ``app_batch_ingest``. The response is a list of
        per-file objects with hash, matched adapter, parsed filename
        metadata, and an ``existing_document_id`` indicator when the
        hash already exists in the vault (so the caller can skip or
        decide to re-ingest with ``force=true``). Warnings list files
        with no matching adapter or filenames that did not parse.

        Error modes:
        - ``invalid_directory`` (string in response, not a SAGE error):
          ``directory`` does not exist or is not readable.

        Args:
            vault_id: Target vault identifier.
            directory: Absolute path to the directory to scan. Single
                and double quote wrappers are stripped, so paths
                round-tripped from shell pasting are accepted.
            max_depth: Max recursion depth (null = unlimited,
                0 = scan only the named directory with no descent).
        """
        from app.backend.scan import build_extension_map, scan_directory

        try:
            v = get_vault(vault_id)
            d = Path(directory.strip("'\""))
            if not d.is_dir():
                return {
                    "error": "invalid_directory",
                    "message": "Directory not found or not readable",
                }

            ext_map = build_extension_map(v.ingestion_service.registered_adapters)
            results, warnings = await scan_directory(
                directory=d,
                vault_config=v.config,
                graph_store=v.graph_store,
                extension_map=ext_map,
                max_depth=max_depth,
            )
            files = []
            for r in results:
                files.append(r.to_dict())
            return {"files": files, "warnings": warnings}
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool()
    async def app_batch_ingest(
        vault_id: str,
        files: list[dict],
        infer_edges: bool = True,
    ) -> dict:
        """Ingest multiple files with optional edge inference. Returns a
        summary when complete.

        Companion to ``app_scan_directory``: the caller decides which
        scanned files to ingest, optionally adjusts the parsed
        metadata, and submits the curated list here. Per-file ingest
        applies the same precedence chain as ``sage_ingest`` (per
        CAS-ADR-021): caller-supplied metadata wins over filename
        inference. Pipeline staging (projection, indexing,
        abstraction) is dispatched fire-and-forget; the response
        summary reports synchronous status, and ``sage_get_document``
        is used to poll terminal pipeline_status if needed.

        When ``infer_edges=True``, the two-phase edge inference runs
        across the entire batch after all documents are inserted:
        Tier 1 edges (e.g. supersedes via version_chain) land as
        production edges; Tier 2 candidates are deposited in the
        staging-edge table for review via
        ``sage_list_staging_edges``.

        Error modes:
        - ``empty_file_list`` (string in response): ``files`` was
          empty. Choose at least one file or skip the call.

        Args:
            vault_id: Target vault identifier.
            files: List of file objects. Each has: ``file_path`` (str),
                ``adapter`` (str), and optional ``parsed_metadata``
                (dict with ``title``, ``date``, ``project``, ``codes``,
                ``version``, ``doc_type``). When ``parsed_metadata``
                is omitted, the stem of ``file_path`` is used as the
                title and no other fields are pre-populated.
            infer_edges: When True (default), run two-phase edge inference
                across the batch after ingestion. When False, ingest
                documents only with no edge creation or lifecycle
                transitions.
        """
        try:
            from app.backend.ingest_service import (
                BatchIngestService,
                FileDescriptor,
                ParsedMetadataInput,
            )

            v = get_vault(vault_id)

            if not files:
                return {
                    "error": "empty_file_list",
                    "message": "No files selected for ingestion",
                }

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
                descriptors.append(
                    FileDescriptor(
                        file_path=f["file_path"],
                        adapter=f["adapter"],
                        parsed_metadata=parsed,
                    )
                )

            svc = BatchIngestService()
            result = await svc.run(
                files=descriptors,
                vault_services=v,
                infer_edges=infer_edges,
            )
            return result.to_dict()
        except (SAGEError, ValueError) as e:
            return error_response(e)

    return {
        "app_scan_directory": app_scan_directory,
        "app_batch_ingest": app_batch_ingest,
    }
