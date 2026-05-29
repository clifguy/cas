"""Application backend tools for MCP.

Contains tools that depend on the CAS application backend (app.backend)
rather than SAGE core services directly: directory scanning and batch
ingestion.
"""

from collections.abc import Callable
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from pydantic import TypeAdapter

from sage.api.errors import SAGEError
from sage.mcp_init import SAGEServices
from sage.models.schemas import VaultIdStr

# Module-scope TypeAdapter for Pattern 2 boundary validation. See the
# parallel adapter declarations and rationale in
# ``sage/sage_api_tools.py``.
_VAULT_ID_ADAPTER: TypeAdapter[str] = TypeAdapter(VaultIdStr)


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
    async def list_directory(
        vault_id: str,
        directory: str,
        max_depth: int | None = None,
    ) -> dict:
        """Walk a directory, match files against vault adapters, hash files,
        parse filenames, and check hashes against the SAGE vault.

        Side-effect free with respect to the vault: no documents are
        created, no metadata is written. Intended as the discovery
        step before ``bulk_ingest_document``. The response is a list of
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
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
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
    async def bulk_ingest_document(
        vault_id: str,
        files: list[dict],
        infer_edges: bool = True,
    ) -> dict:
        """Ingest multiple files with optional edge inference. Returns a
        summary when complete.

        Companion to ``list_directory``: the caller decides which scanned
        files to ingest, optionally adjusts the parsed metadata, and submits
        the curated list here. Per-file ingest applies the same precedence
        chain as ``ingest_document`` (caller-supplied metadata wins over
        filename inference). Pipeline staging (projection, indexing,
        abstraction) dispatches fire-and-forget; the summary reports
        synchronous status, and ``get_document`` polls terminal
        pipeline_status if needed.

        When ``infer_edges=True``, two-phase edge inference runs across the
        whole batch after all documents are inserted: Tier 1 edges (e.g.
        supersedes via version_chain) land as production edges; Tier 2
        candidates are deposited in the staging-edge table for review via
        ``list_staging_edges``.

        Divergence from ``ingest_document``: every document this tool ingests
        lands with ``metadata_confirmed=False`` in the metadata-review queue
        regardless of caller intent (``ingest_document``'s default is the
        opposite). Because ``needs_review=True`` is hard-coded, the vault's
        ``FilenameParser`` always runs and may populate ``date``,
        ``project``, ``codes``, ``version``, and ``doc_type`` from the
        filename when the caller omits them from ``parsed_metadata`` (the
        exact fields are vault-config-defined under
        ``metadata_extraction.filename_extraction.segment_fields``; see
        ``admin_get_vault_config``). The batch flow is a confirmation-queue
        feeder by design: callers curate metadata up-front, then a human or
        follow-up agent confirms each record via ``update_metadata``. Call
        ``get_filename_metadata`` first to preview the parser's output.

        Per-file failure isolation: the batch is NOT atomic. Per-file
        exceptions are caught into ``summary.errors[]`` as
        ``{filename, message}`` (with ``summary.error_count`` advancing); the
        batch continues and post-ingest edge inference still runs across
        whatever inserted. Earlier or later items are not rolled back —
        mirrors the ``create_edges`` / ``update_lifecycles`` /
        ``update_metadata`` atomicity contract.

        Predecessor auto-archive on Tier-1 supersedes inference: when
        ``infer_edges=True`` and inference creates a Tier-1 ``supersedes``
        edge via version chain, the target silently transitions
        ``active -> archived`` as part of edge execution — no explicit
        ``update_lifecycles(action="archive")`` is required and none surfaces
        in the response. Lifecycle-transition failures during this phase are
        collected in ``summary.edge_warnings`` only; they do not raise.

        Tier-1 provenance-gate downgrade: Tier-1 ``supersedes`` adds are
        gated on provenance — if any existing edge in a candidate version
        chain has a non-``version_chain`` rationale (e.g. a human-curated
        edge in the same chain), the entire group's Tier-1 adds are silently
        downgraded to Tier-2 (staged for review rather than landing as
        production edges; the predecessor auto-archive above does NOT fire on
        a downgraded group). A batch's production-vs-staging outcome is
        therefore rule-dependent on the vault's prior edge graph, not
        deterministic from the input files alone.

        Per-file precondition surface: every per-file ingest runs the full
        ``ingest_document`` precondition pipeline. Failures surface as
        ``summary.errors[]`` entries and include — by inherited shape from
        ``ingest_document`` — ``adapter_not_found``, ``document_not_found``,
        ``source_file_not_found``, ``identical_content_supersede``,
        ``duplicate_content``, ``supersede_target_not_active``,
        ``tier3_unique_constraint_violation``, and ``tier3_schema_violation``.
        See ``ingest_document`` for the authoritative surface.

        Error modes:
        - ``unknown_vault`` (400): ``vault_id`` is not a registered vault
          (call ``admin_list_vaults`` for the set). A batch-boundary check
          raised before any per-file work; per-file failures accumulate in
          ``summary.errors[]`` instead.
        - ``empty_file_list`` (string in response): ``files`` was empty.

        Args:
            vault_id: Target vault identifier.
            files: List of file objects. Each has ``file_path`` (str),
                ``source_type`` (str — closed ``SourceType`` vocabulary:
                ``markdown``, ``docx``, ``xlsx``, ``pdf``; the vault's
                actually-enabled subset is whatever appears under
                ``source_adapters.adapters`` in ``admin_get_vault_config``),
                and optional ``parsed_metadata`` (dict with ``title``,
                ``date``, ``project``, ``codes``, ``version``, ``doc_type``).
                When ``parsed_metadata`` is omitted, the stem of
                ``file_path`` is used as the title and the vault's
                ``FilenameParser`` still runs on the remaining fields (see
                the divergence note above; ``get_filename_metadata`` to
                preview).
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

            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
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
                        source_type=f["source_type"],
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
        "list_directory": list_directory,
        "bulk_ingest_document": bulk_ingest_document,
    }
