"""Application-facing tools for MCP.

Registers the directory-scan and batch-ingest tools that support the
CAS application surface. The orchestration and scan logic live in the
SAGE substrate (``sage.services.scan``, ``sage.services.batch_ingest``);
this module only adapts the dict-shaped MCP arguments to those services.
"""

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field, TypeAdapter, ValidationError

from sage._mcp_item_schema import published_item_list
from sage._mcp_param import VaultIdParam, model_param
from sage._tool_annotations import READ_ONLY, WRITE_DESTRUCTIVE
from sage.api.errors import (
    InvalidParameterError,
    InvalidTypedAliasError,
    SAGEError,
    codes_and_tags_conflict_error,
    undeclared_entry_key_error,
    undeclared_entry_keys,
)
from sage.mcp_init import SAGEServices, require_caller_local_filesystem
from sage.models.mcp_items import BulkIngestFileEntry
from sage.models.schemas import (
    BatchIngestParsedMetadata,
    BatchIngestUploadMetadata,
    Sha256Str,
    VaultIdStr,
)
from sage.services.caller_paths import caller_basename
from sage.services.transfer import DeliveryDeclaration, caller_local_delivery

if TYPE_CHECKING:
    from sage.services.batch_ingest import FileDescriptor

# Module-scope TypeAdapter for Pattern 2 boundary validation. See the
# parallel adapter declarations and rationale in
# ``sage/sage_api_tools.py``.
_VAULT_ID_ADAPTER: TypeAdapter[str] = TypeAdapter(VaultIdStr)
_SHA256_ADAPTER: TypeAdapter[str] = TypeAdapter(Sha256Str)

# The names a ``bulk_ingest_document`` file entry, and the parsed metadata it
# may carry, declare. The request surface refuses any other name in the same
# places (CAS-ADR-037, CAS-ADR-052). Both sets are read from the models the
# tool publishes as the entry's shape, and the parsed-metadata names are those
# of the Core API's batch upload model, so the two batch surfaces close on one set.
_FILE_ENTRY_FIELDS = frozenset(BulkIngestFileEntry.model_fields)
_FILE_ENTRIES = published_item_list(
    BulkIngestFileEntry,
    description=(
        "The files to ingest, one entry each. Each carries a source by exactly "
        "one delivery shape: ``file_path``, read directly only when the "
        "caller's machine is the machine running the SAGE server process (the "
        "retained copy lands on the vault's configured source store), or "
        "``transfer_token``, the one-time token from a previously returned "
        "upload recipe, redeemed after the recipe's byte leg delivered that "
        "file to the upload endpoint. A recipe's tokens lapse 900 seconds "
        "after issue by default, and the whole exchange -- every leg's byte "
        "delivery plus the completion call -- must finish inside that window; "
        "the recipe's own ``expires_at`` is authoritative where a deployment "
        "has tuned the lifetime. A leg's token is also reclaimed once 3 "
        "refused deliveries have been made against it by default -- a body "
        "over the ceiling, bytes not matching its bound digest, or a body "
        "abandoned mid-stream; earlier refusals leave it retryable. A lapsed "
        "recipe cannot be resumed, and its staged bytes are gone: re-issue "
        "this call for a fresh one. When ``parsed_metadata`` is omitted, the "
        "stem of the source filename is used as the title and the vault's "
        "filename parsing still runs on the remaining fields. An entry's "
        "``sha256`` binds that leg of an upload recipe, so the upload endpoint "
        "refuses any other bytes for that leg without spending its token "
        "(short of its refusal limit) or touching the other legs; pass it "
        "again on the completion entry."
    ),
)
_PARSED_METADATA_FIELDS = frozenset(BatchIngestParsedMetadata.model_fields)

_INFER_EDGES = model_param(
    bool,
    BatchIngestUploadMetadata,
    "infer_edges",
    mcp=(
        "Tier 1 edges land as production edges; Tier 2 candidates are deposited "
        "in the staging-edge table for review via ``list_staging_edges``. When inference creates a "
        "Tier-1 ``supersedes`` edge, the target transitions as part of edge "
        'execution -- no explicit ``update_lifecycles(action="supersede")`` '
        "is required. Both the states the transition may be taken from and the "
        "state it lands in come from the vault's lifecycle table (in the "
        "create-vault scaffold, from ``active`` or ``completed``, landing in "
        "``archived``). A target already holding a state a supersession lands "
        "in gets the edge and no write. A target in any other state is not "
        "superseded at all: no edge is created, ``edges_dropped`` advances, and "
        "a ``supersede_target_not_transitionable`` entry in "
        "``summary.edge_warnings`` names the observed and permitted states (an "
        "absent or unreadable target refuses the edge the same way, under "
        "``supersede_target_missing`` and ``supersede_target_read_failed``). "
        "Because the refusal is settled before anything is written, a chain "
        "repair whose replacement add is refused withholds its removals as "
        "well, under ``chain_repair_withheld``, rather than severing the chain "
        "it was repairing. Replacement adds are written before the removals "
        "they replace, and a replacement that fails on write "
        "(``edge_creation_failed``) withholds its group's removals the same "
        "way, so a repair never leaves the graph holding fewer supersedes "
        "edges than it found. A transition-carrying supersession commits its "
        "edge and its lifecycle write as one database transaction, under the "
        "same per-predecessor lock ``update_lifecycles`` and single-document "
        "supersede ingest take. Only the single-row write that converges a "
        "pre-existing edge's outstanding transition can still fail with the "
        "edge standing, reported as ``lifecycle_transition_failed``; a "
        "chunk-store lifecycle sync failure is reported as a "
        "``chunk_lifecycle_sync_failed`` entry while the document write "
        "stands. None of these raise. Tier-1 ``supersedes`` adds are gated on "
        "provenance: if any existing edge in a candidate version chain has a "
        "non-``version_chain`` rationale, the entire group's Tier-1 adds are "
        "downgraded to Tier-2 (staged for review; the predecessor "
        "auto-transition does NOT fire on a downgraded group). A batch's "
        "production-vs-staging outcome is therefore rule-dependent on the "
        "vault's prior edge graph, not deterministic from the input files "
        "alone."
    ),
)

_NEEDS_REVIEW = model_param(
    bool,
    BatchIngestUploadMetadata,
    "needs_review",
    mcp=(
        "``list_pending_metadata`` finds a queued document and "
        "``update_metadata`` confirms it. The default is ``True`` here and "
        "``False`` on ``ingest_document``, so a batch is a confirmation-queue "
        "feeder unless the caller says otherwise. The value is the caller's on "
        "both, and only the default differs: the batch flow exists to surface "
        "inferred values for review, so callers curate metadata up-front and a "
        "human or follow-up agent confirms each record via ``update_metadata``. "
        "A caller holding metadata it already "
        "trusts passes ``needs_review=False`` and the documents land at "
        "``metadata_confirmed=True`` with no queue entry and no second call. "
        "While ``needs_review=True``, the vault's filename parsing runs and may "
        "populate ``date``, ``project``, ``codes``, ``version``, and "
        "``doc_type`` from the filename when the caller omits them from "
        "``parsed_metadata`` (the exact fields are vault-config-defined under "
        "``metadata_extraction.filename_extraction.segment_fields``; see "
        "``get_vault_config``). Call ``get_filename_metadata`` first to "
        "preview the parser's output."
    ),
)

# Authored here rather than taken from ``BatchIngestUploadMetadata.dry_run``:
# that text describes the multipart upload channel, whose bytes are staged per
# request, while this tool also reads ``file_path`` entries in place.
_DRY_RUN = Field(
    description=(
        "Report what each file would do and persist nothing. No source is read "
        "into the vault, no projection, indexing or abstraction runs, no record "
        "is written, and edge inference does not run whatever ``infer_edges`` "
        "says. The summary comes back with every count at zero, a ``previews`` "
        "list carrying one entry per file that would succeed, and ``errors`` "
        "carrying one entry per file that would be refused -- together "
        "accounting for the batch. Each preview names the resolved doc_type, "
        "the content hash, the duplicate verdict, and the doc_type's declared "
        "requirement set. Files are evaluated against committed state as it "
        "stood at batch start, so two entries carrying identical bytes each "
        "report no duplicate where a real run would refuse the second. A "
        "``transfer_token`` is read but not spent. Where the call returns an "
        "upload recipe instead, each leg's file is first checked by the "
        "validators that read no bytes: the leg carries ``dry_run_validated`` "
        "naming the checks it passed, or ``dry_run_error`` carrying the refusal "
        "it would get, in the shape of a ``summary.errors[]`` entry whose "
        "``file_index`` is the entry's position in ``files``. Default false."
    ),
)


def _refuse_undeclared_entry_fields(files: list[dict]) -> None:
    """Refuse a name a file entry or its parsed metadata does not declare.

    The entries arrive as plain mappings, so a misspelled or misplaced name
    would otherwise be read past without a word. The refusal is the
    ``undeclared_key`` envelope the request surface gives the same name,
    located the same way and chosen among several by the same rule, and it is
    raised before anything in the batch is delivered or ingested. It carries
    the names the entry does declare, so the call is repairable on first read;
    that set is this tool's own at the entry level, where an upload's entries
    name no path because their bytes arrive as file parts.
    """
    declared = {0: _FILE_ENTRY_FIELDS, 1: _PARSED_METADATA_FIELDS}
    refusal = undeclared_entry_key_error(
        undeclared_entry_keys(files, declared), recognized_by_depth=declared
    )
    if refusal is not None:
        raise refusal


def _file_descriptor(
    entry: dict,
    parsed: BatchIngestParsedMetadata | None,
    tier3: dict | None,
    digest: str | None,
    path: str,
    declared_source: str | None,
) -> "FileDescriptor":
    """The batch descriptor for one entry, read from ``path``.

    Shared by the ingest and by the dry run answered with a recipe, which
    names the file by the caller's own path rather than a staged one. The
    fallback title is the path's basename under the caller's separator, which
    is the name a staged upload carries.
    """
    from sage.services.batch_ingest import FileDescriptor, parsed_metadata_input

    return FileDescriptor(
        file_path=path,
        source_type=entry.get("source_type"),
        parsed_metadata=parsed_metadata_input(
            None if parsed is None else parsed.model_dump(exclude_unset=True),
            Path(caller_basename(path, "upload")).stem,
        ),
        declared_source=declared_source,
        sha256=digest,
        tier3_metadata=tier3,
    )


def _parsed_metadata_of(files: list[dict]) -> list[BatchIngestParsedMetadata | None]:
    """Validate each entry's parsed metadata and return it as the shared model.

    Checked for the whole batch before anything is delivered or ingested, after
    the undeclared-name refusal. A value that is not a mapping, or a field of
    the wrong type, refuses the call as ``invalid_parameter`` located at
    ``files.<n>.parsed_metadata``, ``files.<n>.parsed_metadata.<field>``, or,
    for one item of a list field, ``files.<n>.parsed_metadata.<field>.<index>``.
    An absent or empty mapping supplies nothing and is returned as ``None``.
    """
    validated: list[BatchIngestParsedMetadata | None] = []
    for index, entry in enumerate(files):
        raw = entry.get("parsed_metadata")
        prefix = f"files.{index}.parsed_metadata"
        if raw is None or raw == {}:
            validated.append(None)
            continue
        if not isinstance(raw, dict):
            raise InvalidParameterError(
                parameter=prefix, value=raw, constraint="Input should be a valid dictionary"
            )
        try:
            validated.append(BatchIngestParsedMetadata.model_validate(raw))
        except ValidationError as exc:
            err = exc.errors()[0]
            loc = ".".join(str(segment) for segment in err.get("loc") or ())
            raise InvalidParameterError(
                parameter=f"{prefix}.{loc}" if loc else prefix,
                value=err.get("input"),
                constraint=str(err.get("msg", "Invalid value")),
            ) from exc
    return validated


def _tier3_metadata_of(files: list[dict]) -> list[dict | None]:
    """Validate each entry's Tier-3 metadata and return it.

    Checked for the whole batch before anything is delivered or ingested, after
    the undeclared-name refusal. A value that is not a mapping is a defect in
    the call rather than in one file, so it refuses the call as
    ``invalid_parameter`` located at ``files.<n>.tier3_metadata``. Whether the
    payload *satisfies* the doc_type's declared schema is a question about that
    file's content, answered per file during ingestion as
    ``tier3_schema_violation``, not here.

    An absent key supplies nothing and is returned as ``None``. An explicit
    empty mapping is **not** the same thing and is carried through as ``{}``,
    because the ingest it reaches distinguishes them: a payload that is not
    ``None`` overrides whatever tier-3 metadata the adapter extracted and is
    then validated, so ``{}`` suppresses an adapter-supplied payload and is
    refused by any schema declaring a required field. Folding it to ``None``
    here would give one batch surface a different document, or a different
    outcome, for an entry the sibling surfaces carry unchanged. The sibling
    normalization in ``_parsed_metadata_of`` is not a precedent: there every
    field defaults, so an empty mapping and an absent one genuinely coincide.
    """
    validated: list[dict | None] = []
    for index, entry in enumerate(files):
        raw = entry.get("tier3_metadata")
        if raw is None:
            validated.append(None)
            continue
        if not isinstance(raw, dict):
            raise InvalidParameterError(
                parameter=f"files.{index}.tier3_metadata",
                value=raw,
                constraint="Input should be a valid dictionary",
            )
        validated.append(raw)
    return validated


def _refuse_codes_and_tags_together(
    parsed_metadata: list[BatchIngestParsedMetadata | None],
) -> None:
    """Refuse an entry whose parsed metadata supplies both codes and tags.

    The two set the same field, so an entry supplying both leaves the outcome
    to walk order. Which entry is reported, and where, is
    ``codes_and_tags_conflict_error``'s -- the rule the Core API batch upload
    reports through too, which states where the two surfaces agree on the
    location and where their refusal precedence differs. Raised before anything
    in the batch is delivered or ingested, after each entry's own names and
    types have been settled.
    """
    refusal = codes_and_tags_conflict_error(
        (index, pm.tags)
        for index, pm in enumerate(parsed_metadata)
        if pm is not None and pm.codes and pm.tags
    )
    if refusal is not None:
        raise refusal


def _declared_digests(files: list[dict]) -> list[str | None]:
    """Validate each entry's declared ``sha256`` and return them canonicalized.

    Checked for the whole batch before anything is minted, delivered or
    ingested, as the undeclared-name refusal is, and located the same way: a
    malformed digest is a defect in the call rather than in one file, so it
    refuses the call rather than becoming one entry's error.
    """
    digests: list[str | None] = []
    for index, entry in enumerate(files):
        raw = entry.get("sha256")
        if raw is None:
            digests.append(None)
            continue
        try:
            digests.append(_SHA256_ADAPTER.validate_python(raw))
        except ValidationError as exc:
            ctx = exc.errors()[0].get("ctx") or {}
            raise InvalidTypedAliasError(
                code="invalid_sha256",
                argument=f"files.{index}.sha256",
                value=raw,
                expected=str(ctx.get("expected", "a sha256 digest")),
            ) from exc
    return digests


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

    @mcp.tool(annotations=READ_ONLY)
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
        metadata, and a ``sage_status`` telling whether the vault
        already holds the file (``new`` / ``modified`` / ``unchanged`` /
        ``no_adapter``). Warnings list unreadable entries and any scan
        ceiling that fired.

        The scan is scope-bound: server-side ceilings on file count and
        hashed bytes always apply, and omitting ``max_depth`` applies a
        default depth ceiling. A scan cut by any ceiling returns
        ``truncated: true`` plus a warning naming the ceiling — narrow
        the directory or scan subdirectories separately.

        Error modes:
        - ``invalid_vault_id`` (400): the supplied vault_id is not a
          well-formed vault id.
        - ``vault_not_found`` (404): no vault is registered with that id.
          ``detail.available_vaults`` lists the registered vaults.
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
            max_depth: Max recursion depth (null = the server's default
                depth ceiling, 0 = scan only the named directory with no
                descent). A walk cut by the default ceiling is reported
                as truncated; an explicit depth prunes silently.
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
            results, warnings, truncated = await scan_directory(
                directory=d,
                vault_config=v.config,
                graph_store=v.graph_store,
                extension_map=ext_map,
                max_depth=max_depth,
            )
            files = []
            for r in results:
                files.append(r.to_dict())
            return {"files": files, "warnings": warnings, "truncated": truncated}
        except (SAGEError, ValueError) as e:
            return error_response(e)

    @mcp.tool(annotations=WRITE_DESTRUCTIVE)
    async def bulk_ingest_document(
        vault_id: VaultIdParam,
        files: _FILE_ENTRIES,
        infer_edges: _INFER_EDGES = True,
        needs_review: _NEEDS_REVIEW = True,
        dry_run: Annotated[bool, _DRY_RUN] = False,
    ) -> dict:
        """Ingest multiple files with optional edge inference. Returns a
        summary when complete.

        Companion to ``list_directory``: the caller submits the scanned files
        it chose. Per-file ingest
        applies the same precedence chain as ``ingest_document``. Unlike
        ``ingest_document``, the pipeline is awaited inline for each file in
        turn: the documents have reached a terminal ``pipeline_status`` by
        the time the summary returns, so no caller-side wait is needed. The
        cost is duration -- a large batch can exceed a client's RPC timeout.

        ``needs_review`` defaults to ``True`` here, unlike ``ingest_document``.

        The batch is NOT atomic: a per-file failure lands in
        ``summary.errors[]`` carrying its ``code`` and ``detail``, and the
        batch continues.

        When the server cannot read the caller's filesystem and an entry
        names an absolute ``file_path``, the call returns an upload recipe
        (``status: upload_required``): deliver each file to its URL, then
        repeat the call with ``transfer_token`` in place of ``file_path``.

        Per-file precondition surface: every per-file ingest runs the full
        ``ingest_document`` precondition pipeline, so a ``summary.errors[]``
        entry can carry ``adapter_config_invalid``, ``adapter_not_found``,
        ``duplicate_content``, ``invalid_doc_type``,
        ``invalid_document_date``, ``reserved_transition``,
        ``source_digest_mismatch``, ``source_file_not_found``,
        ``source_type_unresolved``, ``source_unreadable``,
        ``tier3_schema_violation``, ``tier3_unique_constraint_violation``,
        ``vault_migration_in_flight``, ``vault_source_path_refused``,
        ``vault_source_store_refused`` and ``vault_source_store_unavailable``.

        Error modes:
        - ``invalid_vault_id`` (400)
        - ``vault_not_found`` (404)
        - ``empty_file_list`` (string in response)
        - ``invalid_document_date``: per file, in ``summary.errors[]``
        - 400: ``undeclared_key``, ``invalid_sha256``, ``ambiguous_ingest_source``,
          ``missing_ingest_source``
        - ``invalid_parameter`` (422)
        - ``transfer_token_invalid`` (410) / ``transfer_not_staged`` (409)
        - ``transfer_endpoint_not_configured`` (500)
        """
        try:
            from sage.services.batch_ingest import BatchIngestService

            vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
            v = get_vault(vault_id)

            if not files:
                return {
                    "error": "empty_file_list",
                    "message": "No files selected for ingestion",
                }

            _refuse_undeclared_entry_fields(files)
            parsed_metadata = _parsed_metadata_of(files)
            _refuse_codes_and_tags_together(parsed_metadata)
            tier3_metadata = _tier3_metadata_of(files)
            digests = _declared_digests(files)

            # Each entry arrives by exactly one delivery shape: a
            # ``file_path``, or a ``transfer_token`` redeeming bytes the
            # caller's environment already delivered to the upload endpoint.
            # The same gate the single-file tools apply, so the batch surface
            # cannot publish a narrower or wider contract than its siblings.
            # It validates the whole batch before redeeming anything, so a
            # malformed batch consumes no token; it answers an unreachable
            # caller path with one recipe covering every such entry; and it
            # reclaims the per-token staging directories once this block
            # exits *successfully*, which the fully-awaited batch run makes
            # safe. A batch that fails as a whole returns every token it
            # redeemed, bytes intact, so recovering from it costs no second
            # upload -- a per-file failure is collected into the summary and
            # does not reach that path.
            with caller_local_delivery(
                vault_id,
                [
                    DeliveryDeclaration(
                        source=f.get("file_path"),
                        transfer_token=f.get("transfer_token"),
                        sha256=digest,
                    )
                    for f, digest in zip(files, digests, strict=True)
                ],
                # A preview reads the staged bytes without spending the
                # tokens, so the real batch it previews still has them.
                consume=not dry_run,
                # The entries spell the path ``file_path``, so a malformed
                # entry's refusal names that rather than ``source``.
                source_parameter="file_path",
            ) as plan:
                entries = list(zip(files, parsed_metadata, tier3_metadata, digests, strict=True))
                if plan.recipe is not None:
                    if not dry_run:
                        return serialize(plan.recipe)
                    # Legs are minted in the order of the entries that need
                    # them, so each is matched to its entry by walking both in
                    # order; the leg's source is the entry's path verbatim.
                    remaining = iter(enumerate(entries))
                    legs: list[tuple[int, FileDescriptor]] = []
                    for leg in plan.recipe.uploads:
                        index, entry = next(
                            (i, e) for i, e in remaining if e[0].get("file_path") == leg.source
                        )
                        legs.append((index, _file_descriptor(*entry, leg.source, leg.source)))
                    return serialize(
                        await BatchIngestService().validate_recipe_legs(
                            plan.recipe, legs, vault_services=v, needs_review=needs_review
                        )
                    )

                descriptors = [
                    _file_descriptor(*entry, delivery.path, delivery.declared_source)
                    for entry, delivery in zip(entries, plan.resolved, strict=True)
                ]

                svc = BatchIngestService()
                result = await svc.run(
                    files=descriptors,
                    vault_services=v,
                    infer_edges=infer_edges,
                    needs_review=needs_review,
                    dry_run=dry_run,
                )
                return result.to_dict()
        except (SAGEError, ValueError) as e:
            return error_response(e)

    return {
        "list_directory": list_directory,
        "bulk_ingest_document": bulk_ingest_document,
    }
