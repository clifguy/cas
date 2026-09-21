"""Application-facing tools for MCP.

Registers the directory-scan and batch-ingest tools that support the
CAS application surface. The orchestration and scan logic live in the
SAGE substrate (``sage.services.scan``, ``sage.services.batch_ingest``);
this module only adapts the dict-shaped MCP arguments to those services.
"""

from collections.abc import Callable
from pathlib import Path, PureWindowsPath

from mcp.server.fastmcp import FastMCP
from pydantic import TypeAdapter, ValidationError

from sage._tool_annotations import READ_ONLY, WRITE_DESTRUCTIVE
from sage.api.errors import (
    InvalidParameterError,
    InvalidTypedAliasError,
    SAGEError,
    codes_and_tags_conflict_error,
    undeclared_entry_key_error,
)
from sage.mcp_init import SAGEServices, require_caller_local_filesystem
from sage.models.schemas import (
    BatchIngestParsedMetadata,
    Sha256Str,
    UploadRecipe,
    VaultIdStr,
)
from sage.services.transfer import DeliveryDeclaration, caller_local_delivery

# Module-scope TypeAdapter for Pattern 2 boundary validation. See the
# parallel adapter declarations and rationale in
# ``sage/sage_api_tools.py``.
_VAULT_ID_ADAPTER: TypeAdapter[str] = TypeAdapter(VaultIdStr)
_SHA256_ADAPTER: TypeAdapter[str] = TypeAdapter(Sha256Str)

# The names a ``bulk_ingest_document`` file entry, and the parsed metadata it
# may carry, declare. The request surface refuses any other name in the same
# places (CAS-ADR-037, CAS-ADR-052). The parsed-metadata names are those of the
# Core API's batch upload model, so the two batch surfaces close on one set.
_FILE_ENTRY_FIELDS = frozenset(
    {
        "file_path",
        "transfer_token",
        "sha256",
        "source_type",
        "parsed_metadata",
        "tier3_metadata",
    }
)
_PARSED_METADATA_FIELDS = frozenset(BatchIngestParsedMetadata.model_fields)


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
    candidates: list[tuple[int, int, str, object]] = []
    for index, entry in enumerate(files):
        locations = [(0, entry, _FILE_ENTRY_FIELDS)]
        parsed = entry.get("parsed_metadata")
        if isinstance(parsed, dict):
            locations.append((1, parsed, _PARSED_METADATA_FIELDS))
        for depth, mapping, declared in locations:
            candidates.extend(
                (index, depth, name, mapping[name]) for name in mapping if name not in declared
            )
    refusal = undeclared_entry_key_error(
        candidates,
        recognized_by_depth={0: _FILE_ENTRY_FIELDS, 1: _PARSED_METADATA_FIELDS},
    )
    if refusal is not None:
        raise refusal


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
        vault_id: str,
        files: list[dict],
        infer_edges: bool = True,
        needs_review: bool = True,
        dry_run: bool = False,
    ) -> dict:
        """Ingest multiple files with optional edge inference. Returns a
        summary when complete.

        Companion to ``list_directory``: the caller decides which scanned
        files to ingest, optionally adjusts the parsed metadata, and submits
        the curated list here. Per-file ingest applies the same precedence
        chain as ``ingest_document`` (caller-supplied metadata wins over
        filename inference). Unlike ``ingest_document``, the pipeline
        (projection, indexing, abstraction) is awaited inline for each file
        in turn, which bounds peak memory to one document at a time: the
        documents have reached a terminal ``pipeline_status`` by the time
        the summary returns, and ``abstracts_generated`` /
        ``abstracts_deferred`` report the Stage-3 outcome. No caller-side
        wait is needed. The cost is duration -- a large batch can exceed a
        client's RPC timeout.

        When ``infer_edges=True``, two-phase edge inference runs across the
        whole batch after all documents are inserted: Tier 1 edges (e.g.
        supersedes via version_chain) land as production edges; Tier 2
        candidates are deposited in the staging-edge table for review via
        ``list_staging_edges``.

        Divergence from ``ingest_document``: ``needs_review`` defaults to
        ``True`` here and ``False`` there, so a batch is a confirmation-queue
        feeder unless the caller says otherwise. The value is the caller's on
        both, and only the default differs: the batch flow exists to surface
        inferred values for review, so callers curate metadata up-front and a
        human or follow-up agent confirms each record via ``update_metadata``.
        A caller holding metadata it already trusts passes
        ``needs_review=False`` and the documents land at
        ``metadata_confirmed=True`` with no queue entry and no second call.

        While ``needs_review=True``, the vault's filename parsing runs and may
        populate ``date``, ``project``, ``codes``, ``version``, and
        ``doc_type`` from the filename when the caller omits them from
        ``parsed_metadata`` (the exact fields are vault-config-defined under
        ``metadata_extraction.filename_extraction.segment_fields``; see
        ``get_vault_config``). Call ``get_filename_metadata`` first to preview
        the parser's output.

        Per-file failure isolation: the batch is NOT atomic. Per-file
        exceptions are caught into ``summary.errors[]`` (with
        ``summary.error_count`` advancing); the batch continues and
        post-ingest edge inference still runs across whatever inserted.
        Earlier or later items are not rolled back — mirrors the
        ``create_edges`` / ``update_lifecycles`` / ``update_metadata``
        atomicity contract. Each entry names the staged ``filename``, the
        file as the caller named it (``source_path``: the ``file_path``
        sent, or the path a redeemed upload was minted from), and the
        error's ``message``; a typed SAGE error additionally carries its
        ``code`` and ``detail`` — the same envelope ``ingest_document``
        returns for that error — so a caller can branch on the code rather
        than parse prose.

        Predecessor auto-transition on Tier-1 supersedes inference: when
        ``infer_edges=True`` and inference creates a Tier-1 ``supersedes``
        edge via version chain, the target transitions as part of edge
        execution — no explicit ``update_lifecycles(action="supersede")``
        is required. Both the states the transition may be taken from and
        the state it lands in come from the vault's lifecycle table (in
        the create-vault scaffold, from ``active`` or ``completed``,
        landing in ``archived``). A target already
        holding a state a supersession lands in gets the edge and no write.
        A target in any other state is not superseded at all: no edge is
        created, ``edges_dropped`` advances, and a
        ``supersede_target_not_transitionable`` entry in
        ``summary.edge_warnings`` names the observed and permitted states
        (an absent or unreadable target refuses the edge the same way,
        under ``supersede_target_missing`` and
        ``supersede_target_read_failed``). Because the refusal is settled
        before anything is written, a chain repair whose replacement add is
        refused withholds its removals as well, under
        ``chain_repair_withheld``, rather than severing the chain it was
        repairing. Replacement adds are written before the removals they
        replace, and a replacement that fails on write
        (``edge_creation_failed``) withholds its group's removals the same
        way, so a repair never leaves the graph holding fewer supersedes
        edges than it found.

        A transition-carrying supersession commits its edge and its
        lifecycle write as one database transaction, under the same
        per-predecessor lock ``update_lifecycles`` and single-document
        supersede ingest take — a failed commit leaves neither half
        behind, and a concurrent state change refuses the edge instead of
        forking the chain. Only the single-row write that converges a
        pre-existing edge's outstanding transition can still fail with the
        edge standing, reported as ``lifecycle_transition_failed``. The
        follow-up chunk-store lifecycle sync that mirrors
        ``update_lifecycles`` is likewise best-effort: a sync failure is
        reported as a ``chunk_lifecycle_sync_failed`` entry while the
        document write stands. None of these raise.

        Tier-1 provenance-gate downgrade: Tier-1 ``supersedes`` adds are
        gated on provenance — if any existing edge in a candidate version
        chain has a non-``version_chain`` rationale (e.g. a human-curated
        edge in the same chain), the entire group's Tier-1 adds are silently
        downgraded to Tier-2 (staged for review rather than landing as
        production edges; the predecessor auto-transition above does NOT fire on
        a downgraded group). A batch's production-vs-staging outcome is
        therefore rule-dependent on the vault's prior edge graph, not
        deterministic from the input files alone.

        Per-file precondition surface: every per-file ingest runs the full
        ``ingest_document`` precondition pipeline. Failures surface as
        ``summary.errors[]`` entries carrying the code ``ingest_document``
        returns for the same failure. Each file's request carries only its
        source, source type, parsed metadata and declared ``sha256`` -- never a
        predecessor, a force re-ingest, a chain-head token or a relocation
        pointer -- so the codes an entry can carry are ``adapter_config_invalid``,
        ``adapter_not_found``, ``duplicate_content``, ``invalid_doc_type``,
        ``invalid_document_date``, ``reserved_transition``,
        ``source_digest_mismatch``, ``source_file_not_found``,
        ``source_type_unresolved``, ``source_unreadable``,
        ``tier3_schema_violation``,
        ``tier3_unique_constraint_violation``, ``vault_migration_in_flight``,
        ``vault_source_path_refused``, ``vault_source_store_refused`` and
        ``vault_source_store_unavailable``.
        Each such entry carries that error's ``code`` and ``detail``; a
        ``vault_source_path_refused`` entry's ``detail.source_path`` names
        the caller's own file, never the staging location a redeemed upload
        was written to.

        Error modes:
        - ``invalid_vault_id`` (400): the supplied vault_id is not a
          well-formed vault id.
        - ``vault_not_found`` (404): no vault is registered with that id.
          ``detail.available_vaults`` lists the registered vaults.
        - ``empty_file_list`` (string in response): ``files`` was empty.
        - ``invalid_document_date`` (per-file, in ``summary.errors[]``, not a
          call-level envelope): an entry's parsed ``date`` is not a well-formed
          calendar date. The ingest of that one file fails and the call still
          returns its summary, so a caller checking only the envelope sees a
          success.
        - ``undeclared_key`` (400): a file entry, or the ``parsed_metadata``
          it carries, names a key the tool does not declare.
          ``detail.parameter`` locates the object (``files.<n>`` or
          ``files.<n>.parsed_metadata``), ``detail.key`` names the key, and
          ``detail.recognized`` lists the names that object accepts, so the
          call is repairable on first read. With several, the one reported is
          at the lowest file index, then the lowest depth, then first in
          sorted order. A batch-boundary refusal raised before any file is
          delivered or ingested.
        - ``invalid_parameter`` (422): an entry's ``parsed_metadata`` or
          ``tier3_metadata`` is not a mapping, or the ``parsed_metadata``
          carries a value of the wrong type; or one entry's
          ``parsed_metadata`` supplies both ``codes`` and ``tags``, which set
          the same field. ``detail.parameter`` locates it
          (``files.<n>.parsed_metadata``,
          ``files.<n>.parsed_metadata.<key>``,
          ``files.<n>.parsed_metadata.<key>.<index>`` for one item of a list
          such as ``codes``, or ``files.<n>.tier3_metadata``). A
          batch-boundary refusal raised before any file is delivered or
          ingested.
        - ``invalid_sha256`` (400): a file entry's ``sha256`` is not a
          well-formed sha256 digest; the detail is keyed by its location,
          ``files.<n>.sha256``. A batch-boundary refusal raised before any
          file is delivered or ingested.
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
        instead of ``file_path``. On a dry run, each leg's file is first
        checked by the validators that read no bytes: the leg carries
        ``dry_run_validated`` naming the checks it passed, or
        ``dry_run_error`` carrying the refusal it would get, in the shape of
        a ``summary.errors[]`` entry whose ``file_index`` is the entry's
        position in ``files``. The checks that need the bytes run when the
        call is repeated with the tokens.

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
                endpoint). A recipe's tokens lapse 900 seconds after issue
                by default, and the whole exchange -- every leg's byte
                delivery plus the completion call -- must finish inside that
                window; the recipe's own ``expires_at`` is authoritative
                where a deployment has tuned the lifetime. A lapsed recipe
                cannot be resumed, and its staged bytes are gone: re-issue
                this call for a fresh one. Each entry may also carry
                ``source_type`` (str — closed ``SourceType`` vocabulary:
                ``markdown``, ``docx``, ``xlsx``, ``pptx``, ``pdf`` — the source
                types with a registered adapter; when omitted it is inferred
                from the file's extension, and an extension no registered
                adapter claims is reported for that file as
                ``source_type_unresolved``),
                and optional ``parsed_metadata`` (dict with ``title``,
                ``date``, ``project``, ``codes``, ``version``, ``doc_type``,
                ``tags``).
                When ``parsed_metadata`` is omitted, the stem of the source
                filename is used as the title and the vault's
                filename parsing still runs on the remaining fields (see
                the divergence note above; ``get_filename_metadata`` to
                preview). ``tags`` is a list carried whole, so a tag
                containing a comma stays one tag; ``codes`` sets the same
                field, so an entry supplying both is refused. An entry may
                also carry ``tier3_metadata`` (dict), the Tier-3 payload for
                that file, validated against the ``metadata_schema`` the
                vault declares for the file's resolved doc_type on the same
                terms ``ingest_document`` applies -- a payload that schema
                rejects is that file's ``tier3_schema_violation`` and the
                rest of the batch still runs. It sits beside
                ``parsed_metadata`` rather than inside it, because
                ``parsed_metadata`` carries the fields a filename parser can
                supply and Tier-3 metadata never comes from a filename. An
                entry may also carry ``sha256`` (str, bare hex
                or ``sha256:``-prefixed), the digest of that file: a file
                whose bytes have another digest is that file's
                ``source_digest_mismatch`` error, and where the call returns
                an upload recipe, that entry's token is bound to it, so the
                upload endpoint refuses any other bytes for that leg without
                spending its token or touching the other legs. Pass it again
                on the completion entry.
            infer_edges: When True (default), run two-phase edge inference
                across the batch after ingestion. When False, ingest
                documents only with no edge creation or lifecycle
                transitions.
            needs_review: When True (default), every document in the batch
                lands with ``metadata_confirmed=False`` in the
                metadata-review queue (CAS-ADR-021), where
                ``list_pending_metadata`` finds it and ``update_metadata``
                confirms it. When False, the caller's metadata is committed
                as authoritative and no queue entry is made. See the
                divergence note above for why the default is the opposite of
                ``ingest_document``'s.
            dry_run: Report what each file would do and persist nothing.
                No source is read into the vault, no projection, indexing
                or abstraction runs, no record is written, and edge
                inference does not run whatever ``infer_edges`` says. The
                summary comes back with every count at zero, a
                ``previews`` list carrying one entry per file that would
                succeed, and ``errors`` carrying one entry per file that
                would be refused -- together accounting for the batch.
                Each preview names the resolved doc_type, the content
                hash, the duplicate verdict, and the doc_type's declared
                requirement set, so a batch can be checked against a
                vault's typed-metadata rules before any of it lands.
                Files are evaluated against committed state as it stood
                at batch start, so two entries carrying identical bytes
                each report no duplicate where a real run would refuse
                the second. A ``transfer_token`` is read but not spent.
        """
        try:
            from sage.services.batch_ingest import (
                BatchIngestService,
                FileDescriptor,
                parsed_metadata_input,
            )

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
                if plan.recipe is not None:
                    if dry_run:
                        return serialize(
                            await _validate_recipe_legs(
                                v,
                                plan.recipe,
                                files,
                                parsed_metadata,
                                tier3_metadata,
                                digests,
                                needs_review=needs_review,
                            )
                        )
                    return serialize(plan.recipe)

                descriptors: list[FileDescriptor] = []
                for f, parsed, tier3, digest, delivery in zip(
                    files, parsed_metadata, tier3_metadata, digests, plan.resolved, strict=True
                ):
                    descriptors.append(
                        FileDescriptor(
                            file_path=delivery.path,
                            source_type=f.get("source_type"),
                            parsed_metadata=parsed_metadata_input(
                                None if parsed is None else parsed.model_dump(exclude_unset=True),
                                Path(delivery.path).stem,
                            ),
                            declared_source=delivery.declared_source,
                            sha256=digest,
                            tier3_metadata=tier3,
                        )
                    )

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

    async def _validate_recipe_legs(
        v: SAGEServices,
        recipe: UploadRecipe,
        files: list[dict],
        parsed_metadata: list[BatchIngestParsedMetadata | None],
        tier3_metadata: list[dict | None],
        digests: list[str | None],
        *,
        needs_review: bool,
    ) -> UploadRecipe:
        """Answer a batch dry run whose sources must be uploaded first.

        Each leg's file is checked by the validators that read no bytes, as
        the batch would build its request, and the leg carries either the
        checks it passed or the refusal it would get. Legs are minted in the
        order of the entries that need them, so they are matched to entries
        by walking both in order.
        """
        from sage.services.batch_ingest import (
            FileDescriptor,
            dry_run_error_entry,
            file_ingest_request,
            parsed_metadata_input,
        )

        legs = []
        entries = iter(enumerate(zip(files, parsed_metadata, tier3_metadata, digests, strict=True)))
        for leg in recipe.uploads:
            index, (f, parsed, tier3, digest) = next(
                e for e in entries if e[1][0].get("file_path") == leg.source
            )
            fd = FileDescriptor(
                file_path=leg.source,
                source_type=f.get("source_type"),
                parsed_metadata=parsed_metadata_input(
                    None if parsed is None else parsed.model_dump(exclude_unset=True),
                    Path(PureWindowsPath(leg.source).name).stem,
                ),
                declared_source=leg.source,
                sha256=digest,
                tier3_metadata=tier3,
            )
            try:
                ran = await v.ingestion_service.validate_without_bytes(
                    file_ingest_request(fd, needs_review=needs_review, dry_run=True)
                )
                legs.append(leg.model_copy(update={"dry_run_validated": ran}))
            except SAGEError as exc:
                legs.append(
                    leg.model_copy(update={"dry_run_error": dry_run_error_entry(index, fd, exc)})
                )
        return recipe.model_copy(update={"uploads": legs})

    return {
        "list_directory": list_directory,
        "bulk_ingest_document": bulk_ingest_document,
    }
