"""Application-facing tools for MCP.

Registers the directory-scan and batch-ingest tools that support the
CAS application surface. The orchestration and scan logic live in the
SAGE substrate (``sage.services.scan``, ``sage.services.batch_ingest``);
this module only adapts the dict-shaped MCP arguments to those services.
"""

from collections.abc import Callable
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from pydantic import TypeAdapter

import sage.mcp_init
from sage.api.errors import (
    AmbiguousIngestSourceError,
    MissingIngestSourceError,
    SAGEError,
)
from sage.mcp_init import SAGEServices, require_caller_local_filesystem
from sage.models.schemas import VaultIdStr
from sage.services.transfer import PendingTransfer, get_transfer_store, mint_upload_recipe

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
        """Scan a filesystem directory for pre-ingest discovery: walk the
        path, match files against vault adapters, hash files, parse
        filenames, and check the hashes against the SAGE vault.

        To enumerate documents already in a vault (not files on disk), this
        is the wrong tool — use ``search(mode="catalog",
        response_mode="light")`` instead, the canonical vault-document
        enumerator. ``list_directory`` only inspects the filesystem and
        requires a ``directory`` argument.

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
        - ``caller_filesystem_unavailable`` (501): under the cloud profile the
          server cannot see the caller's filesystem, so directory discovery is
          refused rather than walking the container's own tree. Enumerate the
          directory in the caller's environment instead, then ingest each file
          by its absolute path -- the upload recipe the ingest tools return
          carries the rest of the exchange.

        Args:
            vault_id: Target vault identifier.
            directory: Absolute path to the directory to scan, resolved
                on the machine running the SAGE server process; the
                already-in-vault check goes through the vault's stores, so
                results are identical whether those are local or
                cloud-hosted. Single and double quote wrappers are
                stripped, so paths round-tripped from shell pasting are
                accepted.
            max_depth: Max recursion depth (null = unlimited,
                0 = scan only the named directory with no descent).
        """
        from sage.services.scan import build_extension_map, scan_directory

        try:
            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            # Directory discovery walks a path on the *caller's* machine. Under
            # the cloud profile the server cannot see it, so refuse rather than
            # walking and content-hashing the container's own tree; the caller
            # enumerates locally and ingests each file's bytes inline instead.
            require_caller_local_filesystem(
                "list_directory",
                "enumerate the directory in the caller's environment and "
                "ingest each file by its absolute path via ingest_document",
            )
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
        - ``ambiguous_ingest_source`` / ``missing_ingest_source`` (400): a
          file entry set both ``file_path`` and ``transfer_token``, or
          neither; each entry needs exactly one.
        - ``transfer_token_invalid`` (410) / ``transfer_not_staged`` (409):
          an entry's ``transfer_token`` was unredeemable, or its bytes have
          not been delivered to the upload endpoint yet. These are
          batch-boundary refusals raised before any per-file work, not
          per-file ``summary.errors[]`` entries.
        - ``transfer_endpoint_not_configured`` (500): the batch needs the
          transfer channel but the deployment declares no public transfer
          endpoint, so no recipe can be minted.

        When the server cannot read the caller's filesystem and any entry
        names an absolute ``file_path``, the call returns one upload recipe
        (``status: upload_required``) covering those entries instead of
        ingesting: deliver each file to its own URL with its own token, then
        repeat the call with each such entry carrying ``transfer_token``
        instead of ``file_path``.

        Args:
            vault_id: Target vault identifier.
            files: List of file objects. Each carries a source by exactly one
                delivery shape: ``file_path`` (str, a path to the source
                file, read directly only when the caller's machine is the
                machine running the SAGE server process; the retained copy
                lands on the vault's configured source store) or
                ``transfer_token`` (str, the one-time token from a
                previously returned upload recipe, redeemed after the
                recipe's byte leg delivered that file to the upload
                endpoint). Each entry also carries
                ``source_type`` (str — closed ``SourceType`` vocabulary:
                ``markdown``, ``docx``, ``xlsx``, ``pdf``; the vault's
                actually-enabled subset is whatever appears under
                ``source_adapters.adapters`` in ``admin_get_vault_config``),
                and optional ``parsed_metadata`` (dict with ``title``,
                ``date``, ``project``, ``codes``, ``version``, ``doc_type``).
                When ``parsed_metadata`` is omitted, the stem of the source
                filename is used as the title and the vault's
                ``FilenameParser`` still runs on the remaining fields (see
                the divergence note above; ``get_filename_metadata`` to
                preview).
            infer_edges: When True (default), run two-phase edge inference
                across the batch after ingestion. When False, ingest
                documents only with no edge creation or lifecycle
                transitions.
        """
        try:
            from sage.services.batch_ingest import (
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

            # Each entry arrives by exactly one delivery shape: a
            # ``file_path``, or a ``transfer_token`` redeeming bytes the
            # caller's environment already delivered to the upload endpoint.
            # Validated batch-wide before any redemption so a malformed batch
            # consumes no tokens.
            for f in files:
                if f.get("file_path") is not None and f.get("transfer_token") is not None:
                    raise AmbiguousIngestSourceError()
                if f.get("file_path") is None and f.get("transfer_token") is None:
                    raise MissingIngestSourceError()

            # Absolute paths name files on the caller's machine. When the
            # server cannot see them, answer with one upload recipe covering
            # every such entry rather than reading the container's own tree;
            # the caller's environment delivers each file and repeats the
            # call with per-entry transfer tokens. Relative paths are
            # vault-store references the pipeline resolves (CAS-ADR-043).
            if not sage.mcp_init.caller_local_filesystem_reachable():
                absolute_sources = [
                    f["file_path"]
                    for f in files
                    if f.get("file_path") is not None and Path(f["file_path"]).is_absolute()
                ]
                if absolute_sources:
                    return serialize(mint_upload_recipe(vault_id, absolute_sources))

            # Redeemed entries own per-token staging directories, removed
            # once the batch run has read them (the run is fully awaited, so
            # cleanup after it is safe).
            consumed: list[PendingTransfer] = []
            try:
                descriptors: list[FileDescriptor] = []
                for f in files:
                    transfer_token = f.get("transfer_token")
                    if transfer_token is not None:
                        entry = get_transfer_store().consume_upload(transfer_token, vault_id)
                        consumed.append(entry)
                        resolved_path = str(entry.staged_path)
                    else:
                        resolved_path = f["file_path"]

                    pm = f.get("parsed_metadata")
                    parsed = None
                    if pm:
                        parsed = ParsedMetadataInput(
                            title=pm.get("title", Path(resolved_path).stem),
                            date=pm.get("date"),
                            project=pm.get("project"),
                            codes=pm.get("codes", []),
                            version=pm.get("version"),
                            doc_type=pm.get("doc_type"),
                        )
                    descriptors.append(
                        FileDescriptor(
                            file_path=resolved_path,
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
            finally:
                for entry in consumed:
                    entry.cleanup()
        except (SAGEError, ValueError) as e:
            return error_response(e)

    return {
        "list_directory": list_directory,
        "bulk_ingest_document": bulk_ingest_document,
    }
