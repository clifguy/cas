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

        Divergence from ``sage_ingest`` (hard-coded ``needs_review=True``):
        This tool's behavior differs from ``sage_ingest``'s in that
        every document it ingests lands with
        ``metadata_confirmed=False`` and is added to the
        metadata-review queue, regardless of caller intent. The
        ``sage_ingest`` default is the opposite (caller-authoritative
        metadata, ``metadata_confirmed=True``). This is intentional
        per CAS-ADR-021: the batch flow is a confirmation-queue
        feeder by design — callers curate filenames/metadata
        up-front, then a human (or follow-up agent) confirms each
        record via ``sage_update_metadata``. See ``sage_ingest`` for
        the contrasting caller-authoritative default.

        Divergence from ``sage_ingest`` (filename parsing always runs):
        Because ``needs_review=True`` is hard-coded (see above), the
        vault's ``FilenameParser`` always runs on every file. It may
        populate ``date``, ``project``, ``codes``, ``version``, and
        ``doc_type`` from the filename when the caller omits those
        keys from ``parsed_metadata`` — the exact fields the parser
        extracts are vault-config-defined under
        ``metadata_extraction.filename_extraction.segment_fields``
        (see ``sage_get_vault_config``). The historical claim in the
        ``files`` parameter description that omitting ``parsed_metadata``
        leaves "no other fields pre-populated" is structurally false
        for any vault that declares a filename pattern. Call
        ``sage_parse_filename`` first if you want to preview the
        parser's output before ingest. See ``sage_ingest`` for the
        contrasting default (filename parsing only runs when
        ``needs_review=True`` is opted into).

        Per-file failure isolation (CAS-ADR-029):
        The batch is NOT atomic. Per-file exceptions are caught into
        ``summary.errors[]`` as ``{filename, message}`` entries (with
        ``summary.error_count`` advancing in lockstep); the batch
        continues with the remaining files and post-ingest edge
        inference still runs across whatever did insert. Earlier or
        later items are not rolled back. Mirrors the
        ``sage_bulk_link`` / ``sage_bulk_set_lifecycle`` /
        ``sage_bulk_update_metadata`` atomicity contract.

        Predecessor auto-archive on Tier-1 supersedes inference:
        When ``infer_edges=True`` and post-ingest edge inference
        creates a Tier-1 ``supersedes`` edge via version-chain
        inference, the target document silently transitions from
        ``active`` to ``archived`` as part of edge execution — no
        explicit ``sage_set_lifecycle(action="archive")`` call is
        required and none surfaces in the response. Lifecycle
        transition failures during this phase are collected as
        warnings in ``summary.edge_warnings`` only; they do not raise
        and do not appear in ``summary.errors``.

        Tier-1 provenance-gate downgrade:
        Tier-1 ``supersedes`` adds are gated on provenance: if any
        existing edge in a candidate version chain has a non-
        ``version_chain`` rationale (e.g., a human-curated
        ``manual_review`` edge in the same chain), the entire group's
        Tier-1 adds are silently downgraded to Tier-2 (deposited in
        the staging-edge table for review via
        ``sage_list_staging_edges`` rather than landing as production
        edges; the predecessor auto-archive above does NOT fire on a
        downgraded group). The production-vs-staging outcome of a
        batch is therefore rule-dependent on the vault's prior edge
        graph, not deterministic from the input files alone.

        Per-file precondition surface inherited from ``sage_ingest``:
        Every per-file ingest runs the full ``sage_ingest``
        precondition pipeline. Failures surface as entries in
        ``summary.errors[]`` (per the per-file isolation contract
        above) and include — by inherited shape from ``sage_ingest``
        — ``adapter_not_found``, ``document_not_found``,
        ``source_file_not_found``, ``identical_content_supersede``,
        ``duplicate_content``, ``supersede_target_not_active``,
        ``tier3_unique_constraint_violation``, and
        ``tier3_schema_violation``. See ``sage_ingest`` for the
        authoritative per-file precondition surface. Mirrors the
        bulk-tool cross-reference pattern established by
        ``sage_bulk_link`` / ``sage_bulk_set_lifecycle`` /
        ``sage_bulk_update_metadata``.

        Error modes:
        - ``unknown_vault`` (400): ``vault_id`` is not a registered
          vault on this SAGE instance. Call ``sage_list_vaults`` for
          the registered set. This is a batch-boundary check (raised
          before any per-file work begins); per-file failures do not
          surface here, they accumulate in ``summary.errors[]``.
        - ``empty_file_list`` (string in response): ``files`` was
          empty. Choose at least one file or skip the call.

        Args:
            vault_id: Target vault identifier.
            files: List of file objects. Each has: ``file_path`` (str),
                ``adapter`` (str — closed ``SourceType`` vocabulary:
                ``markdown``, ``docx``, ``xlsx``, ``pdf``; the vault's
                actually-enabled subset is whatever appears under
                ``source_adapters.adapters`` in
                ``sage_get_vault_config``), and optional
                ``parsed_metadata`` (dict with ``title``, ``date``,
                ``project``, ``codes``, ``version``, ``doc_type``).
                When ``parsed_metadata`` is omitted, the stem of
                ``file_path`` is used as the title; the vault's
                ``FilenameParser`` still runs and may populate the
                remaining fields from the filename (see "Divergence
                from ``sage_ingest`` (filename parsing always runs)"
                above; call ``sage_parse_filename`` to preview).
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
