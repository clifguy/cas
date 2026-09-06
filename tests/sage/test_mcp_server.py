"""MCP adapter tests.

Verifies that each MCP tool correctly translates to the underlying SAGE
service calls, returns well-formed JSON, and propagates errors as
structured error responses rather than exceptions.

Tests call the tool functions directly (bypassing MCP transport) with
a pre-initialized vault registry, matching how the existing test suite
tests services directly rather than through HTTP.
"""

import asyncio
import json
import logging
from typing import get_args

import pytest

import sage.mcp_server as _mcp
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.api.errors import SAGEError
from sage.config import VaultConfig
from sage.mcp_init import initialize_services
from sage.mcp_server import (
    chain,
    get_document,
    get_filename_metadata,
    get_vault_stats,
    ingest_document,
    list_headings,
    migrate_vault,
    read_projection,
    read_section,
    recompute_abstract,
    recompute_pipeline,
    recompute_views,
    reload_vault,
    search,
    traverse,
    update_vault_config,
    verify_preconditions,
)
from sage.mcp_server import (
    create_edges as _create_edges_bulk,
)
from sage.mcp_server import (
    delete_edge as _sage_unlink_tool,
)
from sage.mcp_server import (
    update_lifecycles as _update_lifecycles_bulk,
)
from sage.mcp_server import (
    update_metadata as _update_metadata_bulk,
)
from sage.models.enums import EdgeType as _EdgeType
from sage.models.enums import PipelineStatus
from sage.services._dry_run import DRY_RUN_SENTINEL_EDGE_ID as _DRY_RUN_SENTINEL_EDGE_ID
from tests.helpers.pipeline_wait import await_tool_idle
from tests.sage.conftest import initialize_services_for_test
from tests.sage.test_ingestion_metadata_extraction import _pim_vault_config_dict

# ---------------------------------------------------------------------------
# CAS-ADR-029 singleton-shape shims around the consolidated bulk tools.
# Post-CAS-ADR-029 v4 the MCP tools take ``items: list[dict]`` only;
# these shims preserve the existing flat singleton call sites in this
# test module by wrapping each call as a length-1 ``items`` collection
# and unwrapping the per-item result envelope back to the singleton
# shape the assertions below expect.
# ---------------------------------------------------------------------------


def _unwrap_bulk_metadata(result, dry_run=False):
    if isinstance(result, dict) and "error" in result and "results" not in result:
        return result
    if isinstance(result, dict) and result.get("results"):
        per = result["results"][0]
        if per.get("status") == "error":
            err = per.get("error") or {}
            out = {
                "error": err.get("error"),
                "message": err.get("message"),
            }
            if "detail" in err:
                out["detail"] = err["detail"]
            return out
        out = {"document": per.get("document"), "dry_run": dry_run}
        if per.get("warnings"):
            out["warnings"] = per["warnings"]
        if "changes" in per:
            out["changes"] = per["changes"]
        return out
    return result


def _unwrap_bulk_lifecycle(result, dry_run=False):
    if isinstance(result, dict) and "error" in result and "results" not in result:
        return result
    if isinstance(result, dict) and result.get("results"):
        per = result["results"][0]
        if per.get("status") == "error":
            err = per.get("error") or {}
            out = {
                "error": err.get("error"),
                "message": err.get("message"),
            }
            if "detail" in err:
                out["detail"] = err["detail"]
            return out
        out = {"document": per.get("document"), "dry_run": dry_run}
        if per.get("created_edge"):
            out["created_edge"] = per["created_edge"]
        if per.get("warnings"):
            out["warnings"] = per["warnings"]
        if "changes" in per:
            out["changes"] = per["changes"]
        return out
    return result


def _unwrap_bulk_edges(result, dry_run=False):
    if isinstance(result, dict) and "error" in result and "results" not in result:
        return result
    if isinstance(result, dict) and result.get("results"):
        per = result["results"][0]
        if per.get("status") == "error":
            err = per.get("error") or {}
            out = {
                "error": err.get("error"),
                "message": err.get("message"),
            }
            if "detail" in err:
                out["detail"] = err["detail"]
            return out
        out = {
            "edge": per.get("edge"),
            "created": per.get("created", True),
            "dry_run": dry_run,
        }
        if "existing_rationale" in per:
            out["existing_rationale"] = per["existing_rationale"]
        return out
    return result


async def update_metadata(vault_id, document_id, **kwargs):
    # dry_run is an envelope-level parameter on the bulk request, not a
    # per-item field; pop it out so it doesn't slip into the items[] entry.
    dry_run = kwargs.pop("dry_run", False)
    item = {"document_id": document_id, **kwargs}
    return _unwrap_bulk_metadata(
        await _update_metadata_bulk(vault_id=vault_id, items=[item], dry_run=dry_run),
        dry_run=dry_run,
    )


async def update_lifecycle(vault_id, document_id, action, successor_id=None, dry_run=False):
    item = {"document_id": document_id, "action": action}
    if successor_id is not None:
        item["successor_id"] = successor_id
    return _unwrap_bulk_lifecycle(
        await _update_lifecycles_bulk(vault_id=vault_id, items=[item], dry_run=dry_run),
        dry_run=dry_run,
    )


async def create_edge(vault_id, source_id, target_id, edge_type, **kwargs):
    # dry_run is an envelope-level parameter on the bulk request, not a
    # per-item field; pop it out so it doesn't slip into the items[] entry.
    dry_run = kwargs.pop("dry_run", False)
    item = {
        "source_id": source_id,
        "target_id": target_id,
        "edge_type": edge_type,
        **kwargs,
    }
    return _unwrap_bulk_edges(
        await _create_edges_bulk(vault_id=vault_id, items=[item], dry_run=dry_run),
        dry_run=dry_run,
    )


@pytest.fixture
async def vault_services(minimal_vault_config_dict, tmp_vault_dir):
    """Initialize SAGE services and register them in the MCP vault registry."""
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    async with initialize_services_for_test(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        _mcp._vaults["test_vault"] = services

        # Create a test source file
        sources = tmp_vault_dir / "sources"
        test_dir = sources / "test"
        test_dir.mkdir(parents=True, exist_ok=True)
        (test_dir / "sample.md").write_text("# Sample Document\n\nSample content.")
        (test_dir / "second.md").write_text("# Second Document\n\nDifferent content.")

        try:
            yield services
        finally:
            await asyncio.sleep(0.5)
            # A reload swaps the registry slot for a fresh bundle; the helper's
            # exit only closes the original ``services``. Release the post-swap
            # bundle's timing + graph store here so neither leaks.
            current = _mcp._vaults.pop("test_vault", None)
            if current is not None and current is not services:
                current.close_timing()
                await current.graph_store.close()


def _parse(result: str | dict) -> dict:
    """Parse a tool's result (dict or JSON string)."""
    if isinstance(result, dict):
        return result
    return json.loads(result)


async def _await_document_idle(services, vault_id, doc_id, *, attempts=400, delay=0.01):
    """Wait until a document is safe for a caller to act on, and return it.

    Thin adapter over the shared wait, reading through the tool surface so the
    poll observes what a caller of these tools would observe. The predicate --
    terminal status *and* no in-flight claim -- lives in one place for the
    whole suite; the claim is released after the terminal status write, so a
    wait keyed on the status alone hands its caller a document the next call
    rejects with a 409.
    """

    async def fetch():
        return _parse(await get_document(vault_id, doc_id))

    return await await_tool_idle(
        fetch,
        doc_id,
        service=services.ingestion_service,
        attempts=attempts,
        delay=delay,
    )


# ---------------------------------------------------------------------------
# Vault routing
# ---------------------------------------------------------------------------


async def test_unknown_vault_returns_error(vault_services):
    result = _parse(await get_document("nonexistent_vault", "deadbeef_doc"))
    assert result["error"] == "unknown_vault"
    assert "nonexistent_vault" in result["message"]


async def test_unknown_vault_lists_available(vault_services):
    result = _parse(await get_document("nonexistent_vault", "deadbeef_doc"))
    assert "test_vault" in result["message"]


# ---------------------------------------------------------------------------
# _error_response: distinguish vault-routing failures from other ValueErrors.
# Pre-fix, every non-SAGEError ValueError was labeled `unknown_vault`,
# which masked unrelated bugs (e.g., a date-parse failure deep in traverse).
# ---------------------------------------------------------------------------


def test_error_response_value_error_returns_internal_error():
    """A generic ValueError is no longer mislabeled as unknown_vault."""
    from sage.mcp_server import _error_response

    result = _error_response(ValueError("boom"))
    assert result["error"] == "internal_error"
    assert result["message"] == "boom"


def test_error_response_vault_not_found_returns_unknown_vault():
    """The unknown_vault label is reserved for actual vault-routing failures."""
    from sage.mcp_server import VaultNotFoundError, _error_response

    result = _error_response(VaultNotFoundError("Unknown vault_id: x"))
    assert result["error"] == "unknown_vault"
    assert "Unknown vault_id: x" in result["message"]


def test_error_response_malformed_document_id_returns_invalid_document_id():
    """A malformed-document_id ValidationError is relabeled from the generic
    internal_error to the structured invalid_document_id (400) shape carrying
    the offending value."""
    from pydantic import TypeAdapter, ValidationError

    from sage.mcp_server import _error_response
    from sage.models.schemas import DocumentIdStr

    try:
        TypeAdapter(DocumentIdStr).validate_python("bad")
    except ValidationError as exc:
        result = _error_response(exc)
    assert result["error"] == "invalid_document_id"
    assert result["detail"]["document_id"] == "bad"
    assert "bad" in result["message"]


def test_error_response_malformed_vault_id_returns_invalid_vault_id():
    """The structured-error relabel covers the whole typed-alias family: a
    malformed vault_id surfaces as the structured invalid_vault_id (400)
    envelope carrying the offending value -- not the generic internal_error it
    would produce if the relabel were scoped to document_id alone."""
    from pydantic import TypeAdapter, ValidationError

    from sage.mcp_server import _error_response
    from sage.models.schemas import VaultIdStr

    try:
        TypeAdapter(VaultIdStr).validate_python("not a vault id!")
    except ValidationError as exc:
        result = _error_response(exc)
    assert result["error"] == "invalid_vault_id"
    assert result["detail"]["vault_id"] == "not a vault id!"
    assert "not a vault id!" in result["message"]


def test_translate_validation_error_maps_invalid_document_id():
    """translate_validation_error rebuilds the InvalidDocumentIdError (400)
    from the PydanticCustomError ctx -- the one translation both transports
    consume."""
    from pydantic import TypeAdapter, ValidationError

    from sage.api.errors import InvalidDocumentIdError, translate_validation_error
    from sage.models.schemas import DocumentIdStr

    try:
        TypeAdapter(DocumentIdStr).validate_python("bad")
    except ValidationError as exc:
        sage_err = translate_validation_error(exc)
    assert isinstance(sage_err, InvalidDocumentIdError)
    assert sage_err.code == "invalid_document_id"
    assert sage_err.status_code == 400
    assert sage_err.detail["document_id"] == "bad"


def test_translate_validation_error_ignores_unrelated_validation():
    """A validation error outside the typed-alias family (here an int-parse
    failure) is not matched, so the caller keeps its default fall-through
    path. Guards that the family branch is not a blanket remap of every
    ValidationError -- vault_id, a family member, is asserted to map in
    test_translate_validation_error_maps_typed_alias_family."""
    from pydantic import TypeAdapter, ValidationError

    from sage.api.errors import translate_validation_error

    try:
        TypeAdapter(int).validate_python("not-an-int")
    except ValidationError as exc:
        assert translate_validation_error(exc) is None


# ---------------------------------------------------------------------------
# Unknown-parameter rejection at the FastMCP boundary (CAS-ADR-037).
# Integration test: invoking a real registered SAGE MCP tool through the
# FastMCP wire path with a misspelled kwarg returns the unknown_parameter
# envelope rather than silently dropping the kwarg or surfacing a
# misleading downstream error. Goes through _LoggingFastMCP.call_tool
# (the JSON-RPC dispatch seam), not the direct-Python tool function,
# because the strict-args rejection is a FastMCP-middleware property.
# ---------------------------------------------------------------------------


async def test_call_tool_rejects_misspelled_kwarg_on_registered_sage_tool():
    """Misspelled kwarg on a real registered SAGE tool produces the envelope.

    Control (action 1) confirms the tool itself is reachable and the
    same wire path returns success under correct invocation. Subject
    (action 2) confirms the unknown_parameter envelope shape on the
    same wire path. Two actions on the same fixture rule out
    coincidental pass: a tool that rejected every call would fail action
    1; a tool that accepted every call would fail action 2.

    list_vaults is chosen because it has no required state setup
    and exercises the JSON-RPC dispatch the same as every other tool.
    """
    from mcp.types import TextContent

    # Action 1 (control): correct invocation returns success.
    control = await _mcp.mcp.call_tool("list_vaults", {})
    assert isinstance(control, list)
    assert len(control) >= 1
    assert isinstance(control[0], TextContent)
    control_payload = json.loads(control[0].text)
    # Success payload is whatever list_vaults returns -- here we
    # just assert it's NOT the unknown_parameter envelope.
    assert control_payload.get("error") != "unknown_parameter"

    # Action 2 (subject): misspelled kwarg returns the envelope.
    subject = await _mcp.mcp.call_tool("list_vaults", {"misspelled": "x"})
    assert isinstance(subject, list)
    assert len(subject) == 1
    assert isinstance(subject[0], TextContent)
    envelope = json.loads(subject[0].text)
    assert envelope["error"] == "unknown_parameter"
    assert envelope["detail"]["tool"] == "list_vaults"
    assert envelope["detail"]["rejected_params"] == ["misspelled"]
    # valid_params reflects the tool's declared signature; we don't
    # pin the exact list to keep the test resilient to future signature
    # changes, but we do confirm the field is populated.
    assert isinstance(envelope["detail"]["valid_params"], list)


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


async def test_ingest_returns_document(vault_services):
    result = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    assert "id" in result
    assert result["source_path"] == "test/sample.md"
    assert result["source_type"] == "markdown"


async def test_ingest_duplicate_returns_error(vault_services):
    await ingest_document("test_vault", "test/sample.md", "markdown")
    await asyncio.sleep(0.1)
    result = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    assert result["error"] == "duplicate_content"


async def test_ingest_force_bypasses_duplicate(vault_services):
    await ingest_document("test_vault", "test/sample.md", "markdown")
    await asyncio.sleep(0.1)
    result = _parse(await ingest_document("test_vault", "test/sample.md", "markdown", force=True))
    assert "id" in result
    assert "error" not in result


async def test_ingest_missing_file_returns_error(vault_services):
    result = _parse(await ingest_document("test_vault", "no/such/file.md", "markdown"))
    assert result["error"] == "source_file_not_found"


# ---------------------------------------------------------------------------
# CAS-ADR-021: ingest_document accepts needs_review and metadata; new
# get_filename_metadata MCP tool returns parsed fields side-effect-free.
# ---------------------------------------------------------------------------


@pytest.fixture
async def pim_vault_services(tmp_vault_dir):
    """Initialize a EXAMPLE-style vault (with filename_extraction) for the
    get_filename_metadata test. Registered in the MCP vault registry under
    its config-declared id (test_metadata_vault).
    """
    config = VaultConfig.model_validate(_pim_vault_config_dict(tmp_vault_dir))
    async with initialize_services_for_test(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        _mcp._vaults["test_metadata_vault"] = services
        try:
            yield services
        finally:
            await asyncio.sleep(0.5)
            _mcp._vaults.pop("test_metadata_vault", None)


async def test_ad021_013_sage_ingest_accepts_metadata_and_needs_review(
    vault_services,
):
    """ingest_document threads metadata + needs_review through to the
    pipeline. Default needs_review=False commits caller-supplied
    metadata as authoritative (metadata_confirmed=True).
    """
    result = _parse(
        await ingest_document(
            "test_vault",
            "test/sample.md",
            "markdown",
            metadata={"title": "Caller Title", "doc_type": "memo"},
        )
    )
    # No error from the tool surface
    assert "error" not in result
    # Caller metadata applied to the document record
    assert result["title"] == "Caller Title"
    assert result["doc_type"] == "memo"
    # Default needs_review=False -> caller-authoritative ingest
    assert result["metadata_confirmed"] is True


async def test_ad021_014_sage_parse_filename_returns_parsed_fields(
    pim_vault_services,
):
    """get_filename_metadata returns parsed fields for a filename
    matching the vault's pattern, and creates no documents.
    """
    graph_store = pim_vault_services.graph_store

    documents_before = await graph_store.list_all_documents()
    pending_before = await graph_store.list_pending_metadata_documents()
    assert documents_before == []
    assert pending_before == []

    result = _parse(
        await get_filename_metadata(
            "test_metadata_vault",
            "2026-03-09_EXAMPLE_PV06_Claim-Set_v6.md",
            "markdown",
        )
    )

    assert result["title"] == "Claim-Set"
    assert result["project"] == "EXAMPLE"
    assert result["version_label"] == "v6.0"
    assert result["document_date"] == "2026-03-09"
    assert result["doc_type"] == "design_spec"
    assert result["codes"] == ["PV06"]

    documents_after = await graph_store.list_all_documents()
    pending_after = await graph_store.list_pending_metadata_documents()
    assert documents_after == [], "get_filename_metadata must not create document records"
    assert pending_after == [], "get_filename_metadata must not enqueue pending_metadata entries"


# ---------------------------------------------------------------------------
# Get document
# ---------------------------------------------------------------------------


async def test_get_document_returns_full_record(vault_services):
    ingest_result = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    result = _parse(await get_document("test_vault", doc_id))
    assert result["id"] == doc_id
    assert result["title"] == "Sample Document"


async def test_get_document_not_found(vault_services):
    result = _parse(await get_document("test_vault", "deadbeef_nonexistent"))
    assert result["error"] == "document_not_found"


@pytest.mark.parametrize(
    "invoke",
    [
        lambda: get_document("test_vault", document_id="not-a-doc-id"),
        lambda: read_projection("test_vault", document_id="not-a-doc-id"),
        lambda: list_headings("test_vault", document_id="not-a-doc-id"),
        lambda: read_section("test_vault", "Heading", document_id="not-a-doc-id"),
        lambda: traverse("test_vault", start_id="not-a-doc-id"),
        lambda: chain("test_vault", "supersedes", document_id="not-a-doc-id"),
    ],
    ids=["get_document", "read_projection", "list_headings", "read_section", "traverse", "chain"],
)
async def test_malformed_document_id_yields_invalid_document_id(invoke, vault_services):
    """Every read/graph tool that validates a document_id at the boundary
    rejects a malformed value with the structured invalid_document_id (400)
    envelope -- the contract is a boundary property, not a per-tool one."""
    result = _parse(await invoke())
    assert result["error"] == "invalid_document_id", f"got: {result!r}"
    assert result["detail"]["document_id"] == "not-a-doc-id", f"got: {result!r}"
    assert "not-a-doc-id" in result["message"]


async def test_read_path_three_way_error_distinctness(vault_services):
    """A malformed id, a well-formed-but-absent id, and a genuine internal
    error are three distinct, distinguishable shapes on the read path. The
    malformed case no longer collides with internal_error, and the
    well-formed-absent case still carries the id_well_formed:true
    discriminator."""
    from sage.mcp_server import _error_response

    malformed = _parse(await get_document("test_vault", document_id="not-a-doc-id"))
    absent = _parse(await get_document("test_vault", "deadbeef_nonexistent"))
    internal = _error_response(ValueError("boom"))

    assert malformed["error"] == "invalid_document_id"
    assert absent["error"] == "document_not_found"
    assert absent["detail"]["id_well_formed"] is True
    assert absent["detail"]["ever_existed"] is False
    assert internal["error"] == "internal_error"
    assert len({malformed["error"], absent["error"], internal["error"]}) == 3
    # The malformed message is caller-actionable, not a raw Pydantic regex
    # dump.
    assert "not-a-doc-id" in malformed["message"]
    assert "must match" not in malformed["message"]


# ---------------------------------------------------------------------------
# Typed-alias structured-error family.
#
# Each sibling reject-flavor validator surfaces a malformed value as its own
# invalid_<alias> (400) via the shared translate_validation_error ->
# InvalidTypedAliasError -> _error_response path, keyed off the single
# _TYPED_ALIAS_CODES frozenset. document_id keeps its own InvalidDocumentIdError
# and must not regress. The `detail` key for each code is the alias name (the
# validator is shared across fields, so it labels by type not by the specific
# field that failed).
# ---------------------------------------------------------------------------

# (external code, schemas alias name, malformed value, detail key == code suffix)
_TYPED_ALIAS_FAMILY = [
    ("invalid_vault_id", "VaultIdStr", "not a vault id!", "vault_id"),
    ("invalid_edge_id", "EdgeIdStr", "not-a-uuid", "edge_id"),
    ("invalid_sha256", "Sha256Str", "deadbeef", "sha256"),
    ("invalid_function_id", "FunctionIdStr", "not-a-fn", "function_id"),
    ("invalid_document_date", "DocumentDateStr", "2026-13-99", "document_date"),
    ("invalid_user_id", "UserIdStr", "not-a-uuid", "user_id"),
]


def _alias_validation_error(alias_name: str, bad_value: str):
    """Build a real pydantic ValidationError by running the named alias's
    TypeAdapter against a malformed value -- never a hand-faked error dict, so
    the err_type and ctx are exactly what the production validator emits."""
    from pydantic import TypeAdapter, ValidationError

    import sage.models.schemas as _schemas

    alias = getattr(_schemas, alias_name)
    try:
        TypeAdapter(alias).validate_python(bad_value)
    except ValidationError as exc:
        return exc
    raise AssertionError(f"{alias_name} unexpectedly accepted {bad_value!r}")


@pytest.mark.parametrize(
    "code,alias_name,bad_value,detail_key",
    _TYPED_ALIAS_FAMILY,
    ids=[c for c, *_ in _TYPED_ALIAS_FAMILY],
)
def test_translate_validation_error_maps_typed_alias_family(
    code, alias_name, bad_value, detail_key
):
    """Each family validator's ValidationError is rebuilt as the single
    InvalidTypedAliasError (400) carrying the offending value under its argument
    key plus an `expected` hint -- the one translation both transports consume."""
    from sage.api.errors import InvalidTypedAliasError, translate_validation_error

    sage_err = translate_validation_error(_alias_validation_error(alias_name, bad_value))
    assert isinstance(sage_err, InvalidTypedAliasError)
    assert sage_err.code == code
    assert sage_err.status_code == 400
    assert sage_err.detail[detail_key] == bad_value
    assert sage_err.detail["expected"]


@pytest.mark.parametrize(
    "code,alias_name,bad_value,detail_key",
    _TYPED_ALIAS_FAMILY,
    ids=[c for c, *_ in _TYPED_ALIAS_FAMILY],
)
def test_error_response_maps_typed_alias_family(code, alias_name, bad_value, detail_key):
    """The MCP choke point _error_response envelopes each family code (not the
    generic internal_error). This is the sole MCP-side coverage for
    invalid_user_id and invalid_sha256, whose only direct surfaces are a model
    field (no dedicated MCP tool param of their own)."""
    from sage.mcp_server import _error_response

    result = _error_response(_alias_validation_error(alias_name, bad_value))
    assert result["error"] == code
    assert result["detail"][detail_key] == bad_value
    assert bad_value in result["message"]


def test_error_response_typed_alias_family_is_scoped():
    """A ValidationError outside the family gets the general code, not a
    family one. An int-parse failure has nothing to do with a malformed
    typed-alias boundary value, so it must surface as invalid_parameter --
    _error_response must not broaden the family into a blanket remap that
    labels every ValidationError as one of the alias codes."""
    from pydantic import TypeAdapter, ValidationError

    from sage.api.errors import _TYPED_ALIAS_CODES
    from sage.mcp_server import _error_response

    try:
        TypeAdapter(int).validate_python("not-an-int")
    except ValidationError as exc:
        result = _error_response(exc)
    assert result["error"] == "invalid_parameter"
    assert result["error"] not in _TYPED_ALIAS_CODES


def test_translate_validation_error_document_id_unchanged():
    """Branch-ordering regression guard: invalid_document_id keeps its own
    InvalidDocumentIdError shape ({document_id: value}, no `expected` key) and is
    NOT swept into the generic InvalidTypedAliasError family branch. A
    misordering that put the family branch first would break document_id's
    distinct three-key ctx via **ctx."""
    from sage.api.errors import InvalidDocumentIdError, translate_validation_error

    sage_err = translate_validation_error(_alias_validation_error("DocumentIdStr", "bad"))
    assert isinstance(sage_err, InvalidDocumentIdError)
    assert sage_err.detail == {"document_id": "bad"}
    assert "expected" not in sage_err.detail


async def test_mcp_tool_malformed_edge_id_yields_invalid_edge_id(vault_services):
    """delete_edge validates edge_id at the boundary before any graph lookup;
    a malformed value surfaces the structured invalid_edge_id (400)."""
    result = _parse(await _sage_unlink_tool("test_vault", edge_id="not-a-uuid"))
    assert result["error"] == "invalid_edge_id", f"got: {result!r}"
    assert result["detail"]["edge_id"] == "not-a-uuid"


async def test_mcp_tool_malformed_function_id_yields_invalid_function_id(vault_services):
    """verify_preconditions validates function_id at the boundary before any
    lookup; a malformed value surfaces the structured invalid_function_id (400)."""
    result = _parse(await verify_preconditions("test_vault", function_id="not-a-fn"))
    assert result["error"] == "invalid_function_id", f"got: {result!r}"
    assert result["detail"]["function_id"] == "not-a-fn"


@pytest.mark.parametrize(
    "bad_date", ["2026-13-99", "2026/01/01"], ids=["impossible_calendar", "bad_shape"]
)
async def test_mcp_tool_malformed_document_date_yields_invalid_document_date(
    bad_date, vault_services
):
    """update_metadata parses items up front (BulkMetadataItem.model_validate);
    both document_date failure sub-modes (impossible calendar date, bad shape)
    surface the structured invalid_document_date (400)."""
    result = _parse(
        await _update_metadata_bulk(
            "test_vault", items=[{"document_id": "deadbeef_a", "document_date": bad_date}]
        )
    )
    assert result["error"] == "invalid_document_date", f"got: {result!r}"
    assert result["detail"]["document_date"] == bad_date


async def test_mcp_tool_malformed_sha256_yields_invalid_sha256(vault_services):
    """create_edges parses items up front (BulkLinkItem.model_validate); a
    malformed synced_from_content_hash surfaces the structured invalid_sha256
    (400). One of two caller-reachable MCP sha256 surfaces -- verify_hashes
    validates through HashCheckRequest and rejects the same way."""
    item = {
        "source_id": "deadbeef_a",
        "target_id": "deadbeef_b",
        "edge_type": "supersedes",
        "synced_from_content_hash": "deadbeef",
    }
    result = _parse(await _create_edges_bulk("test_vault", items=[item]))
    assert result["error"] == "invalid_sha256", f"got: {result!r}"
    assert result["detail"]["sha256"] == "deadbeef"


# ---------------------------------------------------------------------------
# Update metadata
# ---------------------------------------------------------------------------


async def test_update_metadata_partial(vault_services):
    """Scalar fields set verbatim; tags.add appends the patch entries to
    whatever the document already carries. Strict-superset check on tags:
    under add-only patch semantics, pre-existing adapter tags survive,
    so an exact equality would over-assert; a contains-check would
    under-assert. The post-patch set must equal pre-patch ∪ {alpha, beta}.
    """
    ingest_result = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]
    pre_patch_tags = set(_parse(await get_document("test_vault", doc_id))["tags"])

    # Sage_update_metadata now returns UpdateMetadataResponse
    # ({document, dry_run}); unwrap before asserting on document fields.
    result = _parse(
        await update_metadata(
            "test_vault",
            doc_id,
            title="Renamed Document",
            tags={"add": ["alpha", "beta"]},
            doc_type="note",
        )
    )
    assert result["dry_run"] is False
    doc = result["document"]
    assert doc["title"] == "Renamed Document"
    assert set(doc["tags"]) == pre_patch_tags | {"alpha", "beta"}
    assert doc["doc_type"] == "note"


async def test_update_metadata_sets_document_date(vault_services):
    """update_metadata accepts and persists a document_date string,
    and the value is readable via get_document. Catches the wiring
    fault where a parameter is declared on the MCP tool but not threaded
    into UpdateMetadataRequest.
    """
    ingest_result = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    # Wrapper response.
    updated = _parse(
        await update_metadata(
            "test_vault",
            doc_id,
            document_date="2026-04-28",
        )
    )
    assert updated["document"]["document_date"] == "2026-04-28"

    fetched = _parse(await get_document("test_vault", doc_id))
    assert fetched["document_date"] == "2026-04-28"


async def test_update_metadata_invalid_doc_type(vault_services):
    ingest_result = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    result = _parse(await update_metadata("test_vault", doc_id, doc_type="invalid_type"))
    assert result["error"] == "invalid_doc_type"


# ---------------------------------------------------------------------------
# Update metadata: ListFieldPatch flow through the MCP boundary (CAS-ADR-028)
# ---------------------------------------------------------------------------


async def test_update_metadata_tags_add_only(vault_services):
    ingest_result = _parse(
        await ingest_document(
            "test_vault", "test/sample.md", "markdown", metadata={"tags": "alpha,beta"}
        )
    )
    doc_id = ingest_result["id"]

    # Wrapper response.
    result = _parse(await update_metadata("test_vault", doc_id, tags={"add": ["gamma"]}))
    assert set(result["document"]["tags"]) == {"alpha", "beta", "gamma"}


async def test_update_metadata_tags_remove_only(vault_services):
    ingest_result = _parse(
        await ingest_document(
            "test_vault", "test/sample.md", "markdown", metadata={"tags": "alpha,beta"}
        )
    )
    doc_id = ingest_result["id"]

    # Wrapper response.
    result = _parse(await update_metadata("test_vault", doc_id, tags={"remove": ["alpha"]}))
    assert result["document"]["tags"] == ["beta"]


async def test_update_metadata_tags_add_and_remove(vault_services):
    ingest_result = _parse(
        await ingest_document(
            "test_vault", "test/sample.md", "markdown", metadata={"tags": "old,keep"}
        )
    )
    doc_id = ingest_result["id"]

    # Wrapper response.
    result = _parse(
        await update_metadata("test_vault", doc_id, tags={"add": ["new"], "remove": ["old"]})
    )
    assert set(result["document"]["tags"]) == {"keep", "new"}


async def test_update_metadata_tags_add_conflict(vault_services):
    """Adding a tag already present returns 400 tags_add_conflict
    carrying current_tags in the detail."""
    ingest_result = _parse(
        await ingest_document(
            "test_vault", "test/sample.md", "markdown", metadata={"tags": "alpha"}
        )
    )
    doc_id = ingest_result["id"]

    result = _parse(await update_metadata("test_vault", doc_id, tags={"add": ["alpha"]}))
    assert result["error"] == "tags_add_conflict"
    assert result["detail"]["tags"] == ["alpha"]
    assert "alpha" in result["detail"]["current_tags"]


async def test_update_metadata_tags_remove_conflict(vault_services):
    ingest_result = _parse(
        await ingest_document(
            "test_vault", "test/sample.md", "markdown", metadata={"tags": "alpha"}
        )
    )
    doc_id = ingest_result["id"]

    result = _parse(await update_metadata("test_vault", doc_id, tags={"remove": ["never_here"]}))
    assert result["error"] == "tags_remove_conflict"
    assert result["detail"]["tags"] == ["never_here"]


async def test_update_metadata_tags_legacy_form_rejected(vault_services):
    """Bare-list tags returns structured legacy_form with a worked example."""
    ingest_result = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    result = _parse(
        await update_metadata("test_vault", doc_id, tags=["a", "b"])  # type: ignore[arg-type]
    )
    assert result["error"] == "legacy_form"
    assert result["detail"]["field"] == "tags"
    assert "add" in result["detail"]["example"]


# ---------------------------------------------------------------------------
# update_metadata: commutative concurrency on list-valued metadata fields
# (CAS-ADR-038 Primitive A)
# ---------------------------------------------------------------------------


async def test_update_metadata_tags_parallel_distinct_adds_both_land(vault_services):
    """Two parallel `update_metadata` calls each adding a distinct value to
    `tags` on the same document both land. The Primitive A contract:
    callers never read-modify-write a list, so commutative adds are
    set-based and order-independent. Repeated 25 times with distinct
    values per iteration to surface interleaving races.
    """
    ingest_result = _parse(
        await ingest_document("test_vault", "test/sample.md", "markdown", metadata={"tags": "seed"})
    )
    doc_id = ingest_result["id"]

    expected: set[str] = {"seed"}
    for i in range(25):
        x = f"x{i}"
        y = f"y{i}"
        results = await asyncio.gather(
            update_metadata("test_vault", doc_id, tags={"add": [x]}),
            update_metadata("test_vault", doc_id, tags={"add": [y]}),
        )
        for r in results:
            parsed = _parse(r)
            assert "error" not in parsed, f"iteration {i}: unexpected error: {parsed!r}"

        expected.update({x, y})
        after = _parse(await get_document("test_vault", doc_id))
        assert set(after["tags"]) == expected, (
            f"iteration {i}: both parallel adds must land; expected {expected!r}, "
            f"got {after['tags']!r}"
        )
        # Set-based: no duplicates within the iteration's accumulated state.
        assert len(after["tags"]) == len(set(after["tags"])), (
            f"iteration {i}: final tags must contain no duplicates; got {after['tags']!r}"
        )


async def test_update_metadata_tags_parallel_same_add_one_wins_one_conflicts(
    vault_services,
):
    """Two parallel adds of the same value: exactly one succeeds, the
    other returns 400 `tags_add_conflict` carrying `current_tags` with
    the value present once. Set-based semantics: the value lands at
    most once.
    """
    ingest_result = _parse(
        await ingest_document("test_vault", "test/sample.md", "markdown", metadata={"tags": "seed"})
    )
    doc_id = ingest_result["id"]

    results = await asyncio.gather(
        update_metadata("test_vault", doc_id, tags={"add": ["dup"]}),
        update_metadata("test_vault", doc_id, tags={"add": ["dup"]}),
    )
    parsed = [_parse(r) for r in results]
    successes = [p for p in parsed if "error" not in p]
    failures = [p for p in parsed if p.get("error") == "tags_add_conflict"]
    assert len(successes) == 1, f"expected exactly one success; got {parsed!r}"
    assert len(failures) == 1, f"expected exactly one tags_add_conflict; got {parsed!r}"
    assert failures[0]["detail"]["tags"] == ["dup"]
    assert "dup" in failures[0]["detail"]["current_tags"]

    after = _parse(await get_document("test_vault", doc_id))
    assert after["tags"].count("dup") == 1, (
        f"set-based semantics: 'dup' must appear exactly once; got {after['tags']!r}"
    )


async def test_update_metadata_tags_parallel_add_and_remove_disjoint_both_land(
    vault_services,
):
    """Parallel add of one value and remove of another (disjoint) both
    succeed regardless of interleaving. Final state is the union of the
    two operations applied to the seed."""
    ingest_result = _parse(
        await ingest_document(
            "test_vault",
            "test/sample.md",
            "markdown",
            metadata={"tags": "seed,drop"},
        )
    )
    doc_id = ingest_result["id"]

    results = await asyncio.gather(
        update_metadata("test_vault", doc_id, tags={"add": ["new"]}),
        update_metadata("test_vault", doc_id, tags={"remove": ["drop"]}),
    )
    for r in results:
        parsed = _parse(r)
        assert "error" not in parsed, f"unexpected error: {parsed!r}"

    after = _parse(await get_document("test_vault", doc_id))
    assert set(after["tags"]) == {"seed", "new"}, (
        f"disjoint add+remove must compose: expected {{seed, new}}, got {after['tags']!r}"
    )


async def test_update_metadata_tier3_legacy_form_rejected(vault_services):
    """Bare-dict tier3_metadata returns structured legacy_form."""
    ingest_result = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    result = _parse(
        await update_metadata("test_vault", doc_id, tier3_metadata={"some_key": "value"})
    )
    assert result["error"] == "legacy_form"
    assert result["detail"]["field"] == "tier3_metadata"


async def test_update_metadata_empty_tags_patch_rejected(vault_services):
    """tags={} is degenerate; Pydantic returns a validation error."""
    ingest_result = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    result = _parse(await update_metadata("test_vault", doc_id, tags={}))
    # The empty dict has no recognized op keys -- routed through Pydantic
    # validation; the MCP error envelope carries an error code or message.
    assert "error" in result


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_set_lifecycle_archive(vault_services):
    ingest_result = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    result = _parse(await update_lifecycle("test_vault", doc_id, "archive"))
    assert result["document"]["lifecycle_status"] == "archived"


async def test_set_lifecycle_invalid_transition(vault_services):
    ingest_result = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    result = _parse(await update_lifecycle("test_vault", doc_id, "reactivate"))
    assert result["error"] == "invalid_lifecycle_transition"
    assert "valid_actions" in result["detail"]


async def test_set_lifecycle_unknown_action(vault_services):
    ingest_result = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    result = _parse(await update_lifecycle("test_vault", doc_id, "explode"))
    assert result["error"] == "invalid_action"


# Dry_run rollout closes the asymmetry with the bulk variant.
# The service layer (LifecycleService._set_lifecycle) and the
# SetLifecycleRequest/Response schemas already carry dry_run; these
# tests pin the MCP wrapper plumbing.


@pytest.mark.filterwarnings("error:Pydantic serializer warnings:UserWarning")
async def test_set_lifecycle_dry_run_archive_returns_dry_run_true_and_leaves_state(
    vault_services,
):
    """T1: dry_run=True returns dry_run=True and the would-be
    archived state, but the persisted document is still active.

    Paired with test_set_lifecycle_real_run_archive_... (positive
    control): together they catch both directions of wrapper bugs (drop
    dry_run vs. hardcode dry_run=True)."""
    ingest_result = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    response = _parse(await update_lifecycle("test_vault", doc_id, "archive", dry_run=True))

    assert response["dry_run"] is True
    assert response["document"]["lifecycle_status"] == "archived"  # would-be

    persisted = _parse(await get_document("test_vault", doc_id))
    assert persisted["lifecycle_status"] == "active", (
        "dry_run=True must not persist the lifecycle transition; "
        "the wrapper is dropping dry_run on the floor if this fails."
    )


async def test_set_lifecycle_real_run_archive_returns_dry_run_false_and_changes_state(
    vault_services,
):
    """T2: positive control for T1. Without dry_run, the wrapper
    must persist the transition and echo dry_run=False."""
    ingest_result = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    response = _parse(await update_lifecycle("test_vault", doc_id, "archive"))

    assert response["dry_run"] is False
    persisted = _parse(await get_document("test_vault", doc_id))
    assert persisted["lifecycle_status"] == "archived"


@pytest.mark.filterwarnings("error:Pydantic serializer warnings:UserWarning")
async def test_set_lifecycle_dry_run_supersede_returns_sentinel_edge_and_persists_nothing(
    vault_services,
):
    """T3: dry-run supersede populates created_edge with the
    nil-UUID sentinel id, leaves the predecessor active, and persists
    no supersedes edge.

    Anti-coincidental-pass: the traverse zero-edge assertion guards
    against a wrapper that echoes dry_run=True in the envelope but
    actually persists the supersede. The sentinel id is asserted against
    the imported constant, not a literal."""
    doc_a = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await ingest_document("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)

    response = _parse(
        await update_lifecycle(
            "test_vault",
            doc_a["id"],
            "supersede",
            successor_id=doc_b["id"],
            dry_run=True,
        )
    )

    assert response["dry_run"] is True
    assert response["created_edge"] is not None
    assert response["created_edge"]["id"] == _DRY_RUN_SENTINEL_EDGE_ID
    assert response["created_edge"]["source_id"] == doc_b["id"]
    assert response["created_edge"]["target_id"] == doc_a["id"]

    persisted = _parse(await get_document("test_vault", doc_a["id"]))
    assert persisted["lifecycle_status"] == "active"

    traversal = _parse(
        await traverse(
            "test_vault",
            doc_b["id"],
            edge_type="supersedes",
            direction="outbound",
        )
    )
    # traverse returns {start_id, nodes: [...]} where nodes is the
    # set of reachable documents (zero on dry-run since no edge exists).
    assert traversal["nodes"] == [], (
        "dry_run=True supersede must not persist a supersedes edge; "
        f"traverse from {doc_b['id']} returned {traversal['nodes']!r}."
    )


async def test_set_lifecycle_dry_run_invalid_action_error_envelope_matches_real_run(
    vault_services,
):
    """T4: same-validator paired check. invalid_action error
    envelope must be identical whether dry_run is set or not — confirms
    dry_run does not skip or alter validators."""
    ingest_result = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    real = _parse(await update_lifecycle("test_vault", doc_id, "explode"))
    dry = _parse(await update_lifecycle("test_vault", doc_id, "explode", dry_run=True))

    assert real["error"] == "invalid_action"
    assert dry["error"] == "invalid_action"
    # Full envelope equality is stricter than a detail-only check — any
    # divergence (extra field, different message) on the dry-run path
    # fails the test. invalid_action carries no detail payload in this
    # vault, but the envelope-equality guard would catch a future change
    # that started populating one only on the real path.
    assert real == dry


# ---------------------------------------------------------------------------
# Graph operations: link
# ---------------------------------------------------------------------------


async def test_link_creates_edge(vault_services):
    doc_a = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await ingest_document("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)

    result = _parse(
        await create_edge(
            "test_vault",
            doc_a["id"],
            doc_b["id"],
            "supersedes",
            rationale="test link",
        )
    )
    # Sage_link now returns LinkResponse-shaped {edge, created,
    # existing_rationale, dry_run}; unwrap edge for field assertions.
    assert result["dry_run"] is False
    edge = result["edge"]
    assert edge["source_id"] == doc_a["id"]
    assert edge["target_id"] == doc_b["id"]
    assert edge["edge_type"] == "supersedes"
    assert "id" in edge


async def test_link_self_referential_error(vault_services):
    doc = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.3)

    result = _parse(await create_edge("test_vault", doc["id"], doc["id"], "references"))
    assert result["error"] == "self_referential_edge"


async def test_sage_link_explicit_rationale_kind(vault_services):
    """T7. create_edge accepts an optional ``rationale_kind`` argument
    and persists it verbatim on the edge — even when the rationale text
    would otherwise derive to a different kind. Tests with a non-default
    discriminator (``version_chain``) so a system that ignored
    rationale_kind and always returned ``manual`` would fail.
    """
    doc_a = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await ingest_document("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)

    result = _parse(
        await create_edge(
            "test_vault",
            doc_a["id"],
            doc_b["id"],
            "supersedes",
            rationale="caller knows this is from version_chain inference",
            rationale_kind="version_chain",
        )
    )
    assert result.get("error") is None, result
    # Wrapper response.
    assert result["edge"]["rationale_kind"] == "version_chain"


async def test_sage_link_derives_rationale_kind_from_prefix(vault_services):
    """T7. create_edge derives rationale_kind from the rationale text
    prefix when the caller omits the explicit argument. A
    ``[version_chain]`` prefix yields ``rationale_kind=version_chain``.
    """
    doc_a = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await ingest_document("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)

    result = _parse(
        await create_edge(
            "test_vault",
            doc_a["id"],
            doc_b["id"],
            "supersedes",
            rationale="[version_chain] v2 supersedes v1",
        )
    )
    assert result.get("error") is None, result
    # Wrapper response.
    assert result["edge"]["rationale_kind"] == "version_chain"


async def test_sage_link_defaults_to_manual(vault_services):
    """T7. create_edge defaults rationale_kind to ``manual`` when neither
    an explicit kind nor a recognized rationale prefix is supplied.
    """
    doc_a = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await ingest_document("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)

    result = _parse(
        await create_edge(
            "test_vault",
            doc_a["id"],
            doc_b["id"],
            "supersedes",
            rationale="just a freeform note",
        )
    )
    assert result.get("error") is None, result
    # Wrapper response.
    assert result["edge"]["rationale_kind"] == "manual"


async def test_link_idempotent_returns_created_flag(vault_services):
    """Re-calling create_edge with the same natural-key triple
    returns ``created=False`` and preserves the original rationale."""
    doc_a = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await ingest_document("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)

    first = _parse(
        await create_edge(
            "test_vault",
            doc_a["id"],
            doc_b["id"],
            "supersedes",
            rationale="original rationale",
        )
    )
    # Wrapper-level fields (created, existing_rationale, dry_run)
    # remain at the top level; edge fields live under result["edge"].
    assert first["created"] is True
    assert first.get("existing_rationale") is None
    original_edge_id = first["edge"]["id"]

    second = _parse(
        await create_edge(
            "test_vault",
            doc_a["id"],
            doc_b["id"],
            "supersedes",
            rationale="DIFFERENT rationale on second call",
        )
    )
    assert second["created"] is False
    assert second["edge"]["id"] == original_edge_id
    # The pre-existing rationale is surfaced so callers can detect drift.
    assert second["existing_rationale"] == "original rationale"


# ---------------------------------------------------------------------------
# Graph operations: check_preconditions
# ---------------------------------------------------------------------------


async def test_check_preconditions_no_deps(vault_services):
    doc = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.3)

    result = _parse(await verify_preconditions("test_vault", doc["id"]))
    assert result["function_id"] == doc["id"]
    assert result["satisfied"] is True
    assert result["checks"] == []


# ---------------------------------------------------------------------------
# Graph operations: traverse
# ---------------------------------------------------------------------------


async def test_traverse_returns_nodes(vault_services):
    doc_a = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await ingest_document("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)
    await create_edge("test_vault", doc_a["id"], doc_b["id"], "supersedes")

    result = _parse(await traverse("test_vault", doc_a["id"]))
    assert result["start_id"] == doc_a["id"]
    assert len(result["nodes"]) == 1
    assert result["nodes"][0]["document"]["id"] == doc_b["id"]


async def test_traverse_no_edges(vault_services):
    doc = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.3)

    result = _parse(await traverse("test_vault", doc["id"]))
    assert result["start_id"] == doc["id"]
    assert result["nodes"] == []


# ---------------------------------------------------------------------------
# Graph operations: anchor-bearing link and retracts (CAS-ADR-017, Chunk 8)
# ---------------------------------------------------------------------------


async def test_link_transitive_both_requires_anchors(vault_services):
    """covers is transitive_both; omitting anchors via MCP surfaces a 400."""
    doc_a = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await ingest_document("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)

    # No anchor fields -> edge_anchor_policy_violation
    result = _parse(await create_edge("test_vault", doc_a["id"], doc_b["id"], "covers"))
    assert result["error"] == "edge_anchor_policy_violation"
    assert result["detail"]["resolution_policy"] == "transitive_both"

    # Same call with anchors populated -> 201 persistence
    result = _parse(
        await create_edge(
            "test_vault",
            doc_a["id"],
            doc_b["id"],
            "covers",
            source_valid_from_version=doc_a["id"],
            target_valid_from_version=doc_b["id"],
        )
    )
    # Wrapper response.
    edge = result["edge"]
    assert edge["edge_type"] == "covers"
    assert edge["resolution_policy"] == "transitive_both"
    assert edge["source_valid_from_version"] == doc_a["id"]
    assert edge["target_valid_from_version"] == doc_b["id"]


async def test_link_retracts_round_trip(vault_services):
    """Retracts a covers edge through the MCP wrapper and verifies the
    retracts edge round-trips with null target and the correct
    retracted_edge_id."""
    doc_a = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await ingest_document("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)

    covers = _parse(
        await create_edge(
            "test_vault",
            doc_a["id"],
            doc_b["id"],
            "covers",
            source_valid_from_version=doc_a["id"],
            target_valid_from_version=doc_b["id"],
        )
    )
    # Wrapper response.
    assert "id" in covers["edge"]
    covers_edge_id = covers["edge"]["id"]

    # Bad retracted_edge_id -> retract_target_not_edge
    # Use a valid-shape UUID that doesn't exist in the store; the runtime
    # check inside graph_ops then surfaces retract_target_not_edge. A
    # malformed-shape value would short-circuit at LinkRequest validation
    # and yield a generic ValidationError instead.
    import uuid as _uuid

    bad = _parse(
        await create_edge(
            "test_vault",
            doc_a["id"],
            None,
            "retracts",
            source_valid_from_version=doc_a["id"],
            retracted_edge_id=str(_uuid.uuid4()),
        )
    )
    assert bad["error"] == "retract_target_not_edge"

    retract = _parse(
        await create_edge(
            "test_vault",
            doc_a["id"],
            None,
            "retracts",
            source_valid_from_version=doc_a["id"],
            retracted_edge_id=covers_edge_id,
        )
    )
    # Wrapper response.
    retract_edge = retract["edge"]
    assert retract_edge["edge_type"] == "retracts"
    assert retract_edge["resolution_policy"] == "none"
    assert retract_edge["retracted_edge_id"] == covers_edge_id
    assert retract_edge.get("target_id") is None


# ---------------------------------------------------------------------------
# Graph operations: traverse with debug=True (CAS-ADR-017, Chunk 8)
# ---------------------------------------------------------------------------


async def test_traverse_debug_populates_resolution_path(vault_services):
    doc_a = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await ingest_document("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)
    await create_edge(
        "test_vault",
        doc_a["id"],
        doc_b["id"],
        "covers",
        source_valid_from_version=doc_a["id"],
        target_valid_from_version=doc_b["id"],
    )

    off = _parse(await traverse("test_vault", doc_a["id"], edge_type="covers"))
    # debug defaults to False -> resolution_path is absent (exclude_none)
    assert off.get("resolution_path") is None

    on = _parse(await traverse("test_vault", doc_a["id"], edge_type="covers", debug=True))
    path = on.get("resolution_path") or []
    assert any(e["event_type"] == "anchor_hit" for e in path)


# ---------------------------------------------------------------------------
# Retrieval: discover
# ---------------------------------------------------------------------------


async def test_discover_semantic(vault_services):
    await ingest_document("test_vault", "test/sample.md", "markdown")
    await asyncio.sleep(0.5)

    result = _parse(await search("test_vault", "semantic", query="sample content"))
    assert result["mode"] == "semantic"
    assert isinstance(result["results"], list)


async def test_discover_catalog_sort_by_title_through_mcp_wrapper(vault_services):
    """Sort_by / sort_order on the MCP wrapper must reach DiscoverRequest.

    Ingest two documents with distinct titles, then verify that asc and desc
    sort_order values produce reversed orderings. Catches the wrapper silently
    dropping either parameter on the floor.
    """
    await ingest_document("test_vault", "test/sample.md", "markdown")
    await ingest_document("test_vault", "test/second.md", "markdown")
    await asyncio.sleep(0.3)

    asc_result = _parse(
        await search(
            "test_vault",
            mode="catalog",
            sort_by="title",
            sort_order="asc",
            limit=10,
        )
    )
    desc_result = _parse(
        await search(
            "test_vault",
            mode="catalog",
            sort_by="title",
            sort_order="desc",
            limit=10,
        )
    )

    titles_asc = [hit["document"]["title"] for hit in asc_result["results"]]
    titles_desc = [hit["document"]["title"] for hit in desc_result["results"]]

    # Precondition guard: both seeded docs surfaced so the ordering
    # assertions below are non-trivial.
    assert len(titles_asc) == 2
    assert "Sample Document" in titles_asc
    assert "Second Document" in titles_asc

    # Proves sort_by="title" reached DiscoverRequest. Default catalog
    # ordering is lifecycle_status then document_date desc; on two
    # freshly-ingested same-status docs it does not deterministically
    # alphabetize.
    assert titles_asc == sorted(titles_asc)

    # Proves sort_order reached DiscoverRequest. If sort_order were dropped,
    # both calls would reduce to the same request and this would fail.
    assert titles_asc == list(reversed(titles_desc))


async def test_discover_deterministic(vault_services):
    doc = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.5)

    result = _parse(
        await search(
            "test_vault",
            "deterministic",
            document_id=doc["id"],
            heading_path="Sample Document",
        )
    )
    assert result["mode"] == "deterministic"
    assert len(result["results"]) > 0


async def test_discover_semantic_missing_query(vault_services):
    result = _parse(await search("test_vault", "semantic"))
    assert result["error"] == "missing_query"


# ---------------------------------------------------------------------------
# Retrieval: discover — ADR-028 error envelope on parameter validation
# ---------------------------------------------------------------------------


async def test_discover_invalid_mode(vault_services):
    """Unknown mode value returns typed invalid_mode envelope, not internal_error."""
    result = _parse(await search("test_vault", mode="bogus"))
    assert result["error"] == "invalid_mode"
    assert result["detail"]["mode"] == "bogus"
    assert set(result["detail"]["valid_modes"]) == {
        "semantic",
        "keyword",
        "catalog",
        "deterministic",
    }
    assert "bogus" in result["message"]


async def test_discover_unknown_filter_key(vault_services):
    """Unknown filter key (AC: a) returns unknown_filter_key envelope
    rather than silently dropping the key."""
    result = _parse(await search("test_vault", mode="catalog", filters={"tickett_id": "T-0001"}))
    assert result["error"] == "unknown_filter_key"
    assert result["detail"]["key"] == "tickett_id"
    valid_keys = set(result["detail"]["valid_keys"])
    assert {
        "doc_type",
        "project",
        "lifecycle_status",
        "tags",
        "document_ids",
        "pipeline_status",
        "source_type",
        "tier3_metadata",
    } <= valid_keys
    # A worked example helps the caller self-correct without a probe round-trip.
    assert "tier3_metadata" in result["detail"]["example"]


async def test_discover_invalid_filter_shape(vault_services):
    """Wrong type for a known filter key (AC: b) returns
    invalid_filter_shape envelope with the offending field named."""
    result = _parse(await search("test_vault", mode="catalog", filters={"tags": 42}))
    assert result["error"] == "invalid_filter_shape"
    assert result["detail"]["field"] == "tags"
    assert "list" in result["detail"]["expected_type"]
    assert "int" in result["detail"]["received_type"]


async def test_discover_malformed_document_ids_filter_rejected(vault_services):
    """A malformed entry in the document_ids filter is refused, not emptied.

    The filter resolves by SQL equality against stored ids, which are minted
    against the id grammar, so a malformed entry matched zero rows and the
    caller got a successful empty result indistinguishable from a well-formed
    id with no matches. The filter is passed at its correct nested level, so
    the misplaced_filters guard is not what produces this envelope.
    """
    result = _parse(
        await search("test_vault", mode="catalog", filters={"document_ids": ["not-a-doc-id"]})
    )
    assert result["error"] == "invalid_document_id", f"got: {result!r}"
    assert result["detail"]["document_id"] == "not-a-doc-id", f"got: {result!r}"
    assert "not-a-doc-id" in result["message"], f"got: {result!r}"


async def test_discover_well_formed_absent_document_id_still_returns_empty(vault_services):
    """A well-formed id that matches nothing stays a successful empty result.

    The discriminator for the test above: the boundary rejects malformed
    *syntax*, and a genuine zero-match is a data condition, not a client
    error. Without this control, a validator that refused every id would
    satisfy the rejection test.

    Anti-coincidental-pass: the vault is seeded first, and the unfiltered
    count is asserted alongside the filtered one. Against an empty vault the
    filtered emptiness is worth nothing -- it holds whether the constraint is
    applied or dropped on the floor. The pair (one document present, zero
    returned under the filter) is reachable only by applying it.
    """
    await ingest_document("test_vault", "test/sample.md", "markdown")

    result = _parse(
        await search("test_vault", mode="catalog", filters={"document_ids": ["deadbeef_absent"]})
    )
    assert "error" not in result, f"a well-formed absent id must not error; got: {result!r}"
    assert result["total_available"] == 0, f"got: {result!r}"

    unfiltered = _parse(await search("test_vault", mode="catalog", limit=50))
    assert unfiltered["total_available"] == 1, (
        f"the seed must be visible unfiltered, or the filtered emptiness above "
        f"proves nothing; got: {unfiltered!r}"
    )


async def test_discover_accepts_source_type_filter_key(vault_services):
    """source_type is an accepted document filter key.

    Positive control on the pre-change behavior: this exact call used to
    return ``unknown_filter_key``. Asserting the absence of that error is
    the whole point, so the assertion names it explicitly.
    """
    result = _parse(await search("test_vault", mode="catalog", filters={"source_type": "markdown"}))
    assert result.get("error") != "unknown_filter_key"
    assert "results" in result


async def test_discover_invalid_source_type_value_rejected(vault_services):
    """An out-of-vocabulary source_type is refused, not silently emptied.

    The motivating case: "dotx" is not a SAGE source type at all, so the
    caller gets the closed vocabulary back rather than an empty success
    they cannot distinguish from a genuine zero-match.
    """
    result = _parse(await search("test_vault", mode="catalog", filters={"source_type": "dotx"}))
    assert result["error"] == "invalid_filter_value"
    assert result["detail"]["field"] == "source_type"
    assert result["detail"]["value"] == "dotx"
    valid = set(result["detail"]["valid_values"])
    assert valid == {
        "markdown",
        "docx",
        "pdf",
        "email",
        "onenote",
        "teams_chat",
        "xlsx",
        "pptx",
    }


async def test_discover_invalid_edge_type_value_rejected(vault_services):
    """The same envelope covers edge_type, which previously fell through.

    Anti-coincidental: a translator branch scoped to source_type alone
    passes the test above and fails this one. Before the change this
    input produced an untranslated validation error rather than a typed
    envelope, so the assertion on the error code is the discriminator.
    """
    result = _parse(
        await search(
            "test_vault",
            mode="catalog",
            target="edges",
            filters={"edge_type": "refrences"},
        )
    )
    assert result["error"] == "invalid_filter_value"
    assert result["detail"]["field"] == "edge_type"
    assert result["detail"]["value"] == "refrences"
    assert "references" in result["detail"]["valid_values"]


async def test_discover_mode_parameter_mismatch_catalog_with_heading_path(vault_services):
    """Catalog mode with heading_path (AC: c) returns
    mode_parameter_mismatch envelope. heading_path is deterministic-only."""
    result = _parse(await search("test_vault", mode="catalog", heading_path="Section 1"))
    assert result["error"] == "mode_parameter_mismatch"
    assert result["detail"]["mode"] == "catalog"
    assert result["detail"]["forbidden_param"] == "heading_path"
    assert "deterministic" in result["detail"]["allowed_modes"]


async def test_discover_mode_parameter_mismatch_deterministic_with_query(vault_services):
    """Deterministic mode with query set: deterministic does not search."""
    doc = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.5)
    result = _parse(
        await search(
            "test_vault",
            mode="deterministic",
            document_id=doc["id"],
            heading_path="Sample Document",
            query="ignored",
        )
    )
    assert result["error"] == "mode_parameter_mismatch"
    assert result["detail"]["mode"] == "deterministic"
    assert result["detail"]["forbidden_param"] == "query"


async def test_discover_semantic_missing_query_still_typed(vault_services):
    """Regression guard: the existing service-layer missing_query envelope
    must not be folded into mode_parameter_mismatch."""
    result = _parse(await search("test_vault", "semantic"))
    assert result["error"] == "missing_query"


async def test_discover_semantic_happy_path_unchanged(vault_services):
    """Regression guard: success-path response shape is preserved."""
    await ingest_document("test_vault", "test/sample.md", "markdown")
    await asyncio.sleep(0.5)
    result = _parse(await search("test_vault", "semantic", query="sample content"))
    assert result["mode"] == "semantic"
    assert isinstance(result["results"], list)


# ---------------------------------------------------------------------------
# Utilities: read_projection
# ---------------------------------------------------------------------------


async def test_read_projection(vault_services):
    doc = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.5)

    result = _parse(await read_projection("test_vault", doc["id"]))
    assert result["document_id"] == doc["id"]
    assert "projection_text" in result
    assert len(result["projection_text"]) > 0
    assert "title" in result
    # write_to_path was not requested, so delivery fields stay null
    assert result.get("written_to") is None
    assert result.get("content_size") is None


async def test_read_projection_not_found(vault_services):
    result = _parse(await read_projection("test_vault", "deadbeef_nonexistent"))
    assert result["error"] == "document_not_found"


async def test_read_projection_write_to_path_writes_file_and_returns_metadata(
    vault_services, tmp_path
):
    """Sage_read_projection(write_to_path=...) writes the projection
    text bytes to the absolute path and returns metadata only (no inline
    text). Replaces the pre-audit sage_export_projection MCP tool.
    """
    doc = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.5)
    target = tmp_path / "out.md"

    result = _parse(await read_projection("test_vault", doc["id"], write_to_path=str(target)))

    assert result["document_id"] == doc["id"]
    assert result["written_to"] == str(target)
    assert result["content_size"] > 0
    # write-mode response must not double-ship the text inline
    assert result.get("projection_text") is None
    # Anti-coincidental-pass: the file must actually exist and have
    # non-empty contents matching the reported size.
    assert target.exists()
    written = target.read_bytes()
    assert len(written) == result["content_size"]
    assert len(written) > 0


async def test_read_projection_write_to_path_existing_target_errors(vault_services, tmp_path):
    doc = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.5)
    target = tmp_path / "existing.md"
    target.write_text("pre-existing")

    result = _parse(await read_projection("test_vault", doc["id"], write_to_path=str(target)))

    assert result["error"] == "write_path_exists"
    # File must not have been clobbered.
    assert target.read_text() == "pre-existing"


async def test_read_projection_write_to_path_relative_errors(vault_services):
    doc = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.5)

    result = _parse(await read_projection("test_vault", doc["id"], write_to_path="relative.md"))

    assert result["error"] == "write_path_invalid"


async def test_read_projection_delivery_inline(vault_services):
    """delivery=inline forces the projection body inline and reports body_length."""
    doc = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.5)

    result = _parse(await read_projection("test_vault", doc["id"], delivery="inline"))

    assert result["document_id"] == doc["id"]
    assert len(result["projection_text"]) > 0
    assert result.get("written_to") is None
    assert result["read_meta"]["body_present"] is True
    assert result["read_meta"]["body_length"] == len(result["projection_text"])


async def test_read_projection_delivery_spill(vault_services, tmp_path):
    """delivery=spill writes the projection to disk and returns metadata only."""
    doc = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.5)
    target = tmp_path / "delivery_out.md"

    result = _parse(
        await read_projection("test_vault", doc["id"], write_to_path=str(target), delivery="spill")
    )

    assert result["written_to"] == str(target)
    assert result["content_size"] > 0
    assert result.get("projection_text") is None
    assert target.exists()
    assert len(target.read_bytes()) == result["content_size"]


async def test_read_projection_delivery_spill_without_path_errors(vault_services):
    """delivery=spill without a write_to_path target is refused with a structured error."""
    doc = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.5)

    result = _parse(await read_projection("test_vault", doc["id"], delivery="spill"))

    assert result["error"] == "delivery_conflict"


def test_read_projection_publishes_delivery_param():
    """``delivery`` is a published, optional property with the inline/spill/auto
    enum and an ``auto`` default — not a server-only kwarg.

    A server-only kwarg would be absent from the published schema and stripped
    by an additionalProperties:false client before dispatch, so this asserts the
    enum, the default, and the strict-args invariant the published shape relies on.
    """
    tool = _mcp.mcp._tool_manager.get_tool("read_projection")
    schema = tool.parameters
    props = schema.get("properties", {})
    assert "delivery" in props, "read_projection must publish delivery"
    delivery_schema = props["delivery"]
    assert delivery_schema.get("default") == "auto"
    # The enum may be expressed inline or via $ref/anyOf; flatten to the
    # literal values regardless of nesting.
    enum_values = delivery_schema.get("enum")
    if enum_values is None:
        for branch in delivery_schema.get("anyOf", []):
            if "enum" in branch:
                enum_values = branch["enum"]
                break
    assert enum_values is not None, f"delivery must publish an enum: {delivery_schema!r}"
    assert set(enum_values) == {"inline", "spill", "auto"}
    assert "delivery" not in schema.get("required", [])
    assert schema.get("additionalProperties") is False


# ---------------------------------------------------------------------------
# Utilities: refresh_views
# ---------------------------------------------------------------------------


async def test_refresh_views(vault_services):
    await ingest_document("test_vault", "test/sample.md", "markdown")

    result = _parse(await recompute_views("test_vault"))
    assert result["vault_id"] == "test_vault"
    assert isinstance(result["views_generated"], int)
    assert result["views_generated"] >= 1


# ---------------------------------------------------------------------------
# Vault reload
# ---------------------------------------------------------------------------


async def test_reload_vault_reinitializes_services(vault_services):
    """Reload replaces services with a fresh instance and returns stats."""
    # Ingest a document so we can verify data survives reload
    await ingest_document("test_vault", "test/sample.md", "markdown")
    await asyncio.sleep(0.3)

    old_services = _mcp._vaults["test_vault"]
    result = _parse(await reload_vault("test_vault"))

    assert result["vault_id"] == "test_vault"
    assert result["reloaded"] is True
    assert result["document_count"] >= 1
    # Services instance should be replaced
    assert _mcp._vaults["test_vault"] is not old_services


async def test_reload_vault_count_comes_from_the_store_total(vault_services, monkeypatch):
    """``document_count`` is the store's COUNT(*), not a list length.

    Pins the producer rather than the value. The fake store's
    ``list_all_documents()`` raises, so a count taken by materializing every
    document record cannot quietly agree with the sentinel;
    ``get_total_document_count()`` returns a value no default or fixture
    could supply.

    What discriminates is the sentinel comparison, not the raise reaching the
    caller. A rival reading the length *outside* the tool's degrade guard
    propagates the ``AssertionError``; one reading it *inside* is caught
    there -- ``except Exception`` takes ``AssertionError`` too -- logged, and
    degraded to ``None``. Both go red, the second on the value rather than on
    the exception, which is why the assertion is on 4242 and not on the raise.
    """
    import sage.sage_api_tools as _sage_tools_module

    class _CountOnlyGraphStore:
        async def get_total_document_count(self) -> int:
            return 4242

        async def list_all_documents(self):
            raise AssertionError(
                "list_all_documents() must not be called for reload_vault's document_count"
            )

    class _FakeServices:
        graph_store = _CountOnlyGraphStore()

    async def fake_reload(*args, **kwargs):
        return _FakeServices()

    # The tool closure resolves ``reload_vault_in_registry`` through its
    # defining module's globals, so the patch must land there rather than on
    # sage.mcp_server, which only re-exports the closure.
    monkeypatch.setattr(_sage_tools_module, "reload_vault_in_registry", fake_reload)

    result = _parse(await reload_vault("test_vault"))

    assert result["vault_id"] == "test_vault"
    assert result["reloaded"] is True
    assert result["document_count"] == 4242


async def test_reload_vault_count_spans_every_lifecycle_state(vault_services, tmp_vault_dir):
    """The count covers every lifecycle state, not just the current ones.

    The vault holds exactly one document in each of its three states, so
    excluding *any* single state reports 2 rather than 3. Populating one state
    per document is what makes that true: with only an active and an archived
    document, an implementation that dropped ``completed`` rows would agree
    with the correct one and pass, because no fixture would carry the input
    that separates them.

    The literal is asserted alongside parity with ``list_all_documents()`` so
    that two paths broken the same way cannot agree their way to a pass.
    """
    (tmp_vault_dir / "sources" / "test" / "third.md").write_text("# Third Document\n\nMore.")

    active = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    to_archive = _parse(await ingest_document("test_vault", "test/second.md", "markdown"))
    to_complete = _parse(await ingest_document("test_vault", "test/third.md", "markdown"))
    for doc in (active, to_archive, to_complete):
        await _await_document_idle(vault_services, "test_vault", doc["id"])

    archived = _parse(await update_lifecycle("test_vault", to_archive["id"], "archive"))
    completed = _parse(await update_lifecycle("test_vault", to_complete["id"], "complete"))
    assert archived["document"]["lifecycle_status"] == "archived"
    assert completed["document"]["lifecycle_status"] == "completed"

    result = _parse(await reload_vault("test_vault"))

    live = await _mcp._vaults["test_vault"].graph_store.list_all_documents()
    assert result["document_count"] == 3
    assert result["document_count"] == len(live)
    # One document per state, so no single-state exclusion can still total 3.
    assert {d.lifecycle_status for d in live} == {"active", "archived", "completed"}


async def test_reload_vault_reports_success_when_the_count_read_fails(
    vault_services, monkeypatch, caplog
):
    """A failed count must not turn a completed reload into a reported failure.

    By the time the count is read the new services are already installed, so
    the reload has happened. Reporting an error here would tell the caller to
    retry an operation that already succeeded -- tearing down and rebuilding
    services that are correct. The count is decoration on a success, so it
    degrades to null and the reload is still reported.

    The raised type is a bare ``RuntimeError``, standing in for the driver
    errors the store raises untranslated on this path: ``COUNT(*)`` goes
    through ``_fetch_scalar`` with no ``StorageQueryError`` translation, so
    what escapes is neither a ``SAGEError`` nor a ``ValueError`` and no
    existing handler in the tool body sees it.
    """
    import sage.sage_api_tools as _sage_tools_module

    class _FailingCountGraphStore:
        async def get_total_document_count(self) -> int:
            raise RuntimeError("simulated driver failure on the count read")

    class _FakeServices:
        graph_store = _FailingCountGraphStore()

    async def fake_reload(*args, **kwargs):
        return _FakeServices()

    monkeypatch.setattr(_sage_tools_module, "reload_vault_in_registry", fake_reload)

    with caplog.at_level(logging.ERROR):
        result = _parse(await reload_vault("test_vault"))

    assert "error" not in result
    assert result["vault_id"] == "test_vault"
    assert result["reloaded"] is True
    # None, not 0: an unknown count and an empty vault are different facts,
    # and 0 would be read as the latter.
    assert result["document_count"] is None
    # The failure is not swallowed -- an operator can still see it.
    assert "simulated driver failure on the count read" in caplog.text


async def test_reload_vault_closes_old_graph_store(vault_services):
    """The old graph store is closed after reload, and the closed store
    enforces the CAS-ADR-036 barrier: post-close dispatch raises rather
    than silently serving through the released connection pool.
    """
    old_graph_store = vault_services.graph_store
    # Positive control: the store dispatches before the reload, so the
    # post-reload raise below is attributable to the close, not to a store
    # that never worked.
    assert isinstance(await old_graph_store.list_all_documents(), list)

    await reload_vault("test_vault")

    # Barrier semantics: dispatch through the closed store raises rather
    # than silently degrading.
    with pytest.raises(RuntimeError, match="closed"):
        await old_graph_store.list_all_documents()

    # The fresh store installed by the reload serves reads.
    fresh_graph_store = _mcp._vaults["test_vault"].graph_store
    assert fresh_graph_store is not old_graph_store
    assert isinstance(await fresh_graph_store.list_all_documents(), list)


async def test_reload_vault_settles_dropped_abstraction_work(vault_services, tmp_vault_dir):
    """Reloading through the MCP tool settles the work the predecessor's worker
    was carrying, rather than discarding it non-terminal along with that worker.

    Read back through the successor's own store, which the reload builds fresh
    over the same database, so the assertion is on what was durably written and
    not on a handle the teardown happened to leave open.
    """
    from tests.sage.test_abstraction_queue import _GatedAbstractionProvider, _seed_indexed_doc

    ingestion = vault_services.ingestion_service
    doc_id = await _seed_indexed_doc(ingestion, tmp_vault_dir, "samples/mr1.md")
    gated = _GatedAbstractionProvider()
    ingestion._abstraction = gated
    await ingestion.reabstract(doc_id)
    await asyncio.wait_for(gated.entered.wait(), timeout=2.0)
    assert (
        await vault_services.graph_store.get_document(doc_id)
    ).pipeline_status == PipelineStatus.ABSTRACTION_IN_PROGRESS

    try:
        await reload_vault("test_vault")
    finally:
        gated.gate.set()

    fresh = _mcp._vaults["test_vault"].graph_store
    doc = await fresh.get_document(doc_id)
    assert doc.pipeline_status == PipelineStatus.ABSTRACTION_INTERRUPTED
    assert doc.pipeline_error


async def test_reload_vault_unknown_vault_returns_error(vault_services):
    """Reload on a nonexistent vault returns structured error."""
    result = _parse(await reload_vault("nonexistent_vault"))
    assert result["error"] == "unknown_vault"
    assert "nonexistent_vault" in result["message"]


async def test_reload_vault_sees_external_changes(vault_services):
    """After external DB changes and reload, fresh services see current state.

    Simulates the core use case: data modified outside the MCP process,
    then reload picks up the new state.
    """
    # Ingest through the current services
    await ingest_document("test_vault", "test/sample.md", "markdown")
    await asyncio.sleep(0.3)

    # Verify document is visible
    stats_before = _parse(await get_vault_stats("test_vault"))
    assert stats_before["total_documents"] == 1

    # Simulate external modification: insert a document through a SECOND
    # graph store over the same vault schema — a separate connection pool,
    # as another OS process would hold — bypassing the in-process services.
    # This mimics what happens when the FastAPI server or another process
    # writes to the database.
    import os
    from datetime import datetime, timezone

    from sage.models.schemas import Document
    from sage.storage.postgres.graph_store import PostgresGraphStore
    from sage.storage.postgres.pool import pool_from_conninfo

    now = datetime.now(timezone.utc)
    external_doc = Document(
        id="deadbeef_external",
        title="Externally Added",
        source_type="markdown",
        source_path="external/doc.md",
        lifecycle_status="active",
        source_content_hash="sha256:" + "e" * 64,
        adapter_version="1.0",
        created_by="test",
        created_at=now,
        last_modified_by="test",
        updated_at=now,
        pipeline_status="abstraction_complete",
    )
    external_pool = pool_from_conninfo(
        os.environ["SAGE_TEST_PG_DSN"], search_path="test_vault,public"
    )
    await external_pool.open()
    try:
        external_store = PostgresGraphStore(external_pool)
        await external_store.insert_document(external_doc)
        await external_store.close()
    finally:
        await external_pool.close()

    # Reload vault to pick up external changes
    await reload_vault("test_vault")
    await asyncio.sleep(0.1)

    # Fresh services should see both documents
    stats_after = _parse(await get_vault_stats("test_vault"))
    assert stats_after["total_documents"] == 2


async def test_reload_vault_failure_keeps_old_services_in_registry(vault_services, monkeypatch):
    """AC2: a failed reload leaves _vaults pointing at functional old services.

    Trap (anti-coincidental): a literal try/restore that re-installs the (closed)
    old reference would pass the identity check but fail the "graph store still
    open" assertion. Both checks must hold.
    """
    import sage.mcp_init as _mcp_init

    old = _mcp._vaults["test_vault"]

    async def failing_initialize_services(*args, **kwargs):
        raise SAGEError(
            code="schema_migration_required",
            message="simulated failure for T-0183 atomicity test",
            status_code=409,
        )

    # Patch both call sites so the test exercises the failure path whether
    # reload_vault still has the inline initialize_services call (pre-refactor)
    # or delegates to reload_vault_in_registry which uses sage.mcp_init's binding
    # (post-refactor).
    monkeypatch.setattr(_mcp_init, "initialize_services", failing_initialize_services)
    monkeypatch.setattr(_mcp, "initialize_services", failing_initialize_services)

    result = _parse(await reload_vault("test_vault"))

    # (a) Error envelope returned, not an exception
    assert result.get("error") == "schema_migration_required"
    assert "simulated failure" in result["message"]

    # (b) Registry slot still points at the SAME object (identity check)
    assert _mcp._vaults["test_vault"] is old

    # (c) The old services are still FUNCTIONAL — the graph store was not
    # closed. Behavioural assertion per TEST-SAGE-BH-137: the CAS-ADR-036
    # close barrier makes every post-close dispatch raise (see
    # test_reload_vault_closes_old_graph_store above), so a successful
    # list_all_documents() is the contrapositive of close() having run. A
    # literal try/restore around a close-old-first ordering would re-install
    # the closed reference — passing the identity check — and fail here.
    live_docs = await old.graph_store.list_all_documents()
    assert isinstance(live_docs, list)


async def test_reload_vault_failure_releases_partially_allocated_resources(
    vault_services, monkeypatch
):
    """AC2 + Risk: a failed reload must not leak background threads.

    `_build_vault_timers` calls `flusher.start()` before initialize_services
    returns. If initialize_services raises after that point without
    transactional cleanup, the new vault's timing thread runs forever.

    This test asserts that the failed reload does not increase the count of
    live `sage-timing-flush` threads.
    """
    import threading

    def _count_timing_threads() -> int:
        return sum(1 for t in threading.enumerate() if t.name.startswith("sage-timing-flush"))

    pre_count = _count_timing_threads()

    # Patch UserService.bootstrap_owner to raise inside initialize_services.
    # That method runs AFTER timing thread + graph store + content store have
    # been constructed, so this exercises the late-stage cleanup path.
    from sage.services.user_service import UserService

    async def raising_bootstrap(self):
        raise SAGEError(
            code="schema_migration_required",
            message="simulated late-stage failure for T-0183 cleanup test",
            status_code=409,
        )

    monkeypatch.setattr(UserService, "bootstrap_owner", raising_bootstrap)

    result = _parse(await reload_vault("test_vault"))

    # (a) Error envelope returned
    assert result.get("error") == "schema_migration_required"

    # (b) No new timing threads leaked from the failed partial initialization.
    # Brief grace period for thread.join() inside cleanup to complete.
    await asyncio.sleep(0.1)
    post_count = _count_timing_threads()
    assert post_count <= pre_count, (
        f"Timing thread leaked on failed reload: pre={pre_count}, post={post_count}"
    )


async def test_reload_vault_stops_old_timing_thread(vault_services, monkeypatch):
    """AC3 (reconciliation): MCP reload path now stops the old vault's
    timing thread on success (parity with the FastAPI path via
    reload_vault_in_registry).

    Trap (anti-coincidental): the current inline MCP code skips
    timing_thread.stop() entirely; only the registry version stops it. After
    delegation, both paths must stop the thread. The assert_called_once_with
    is the trap.
    """
    from unittest.mock import MagicMock

    # The fixture's services may or may not have a real timing_thread (depends
    # on TimingConfig defaults). Install a fake we can observe regardless.
    fake_thread = MagicMock()
    fake_thread.stop = MagicMock()
    # Stop the real flusher before swapping in the observable fake. Otherwise
    # the fake's no-op stop() leaves the real thread running, and close_timing
    # on the old bundle (which now sees the fake) never stops it — an orphaned
    # VaultTimingThread the root timing guard would flag.
    _real_thread = _mcp._vaults["test_vault"].timing_thread
    if _real_thread is not None:
        _real_thread.stop(timeout=1.0)
    _mcp._vaults["test_vault"].timing_thread = fake_thread

    result = _parse(await reload_vault("test_vault"))
    assert result["reloaded"] is True

    fake_thread.stop.assert_called_once_with(timeout=1.0)


async def test_reload_vault_preserves_content_store_factory_across_two_reloads(
    minimal_vault_config_dict, tmp_vault_dir
):
    """AC3 (reconciliation): content_store_factory survives across
    multiple successive reloads.

    Trap (anti-coincidental): a single-reload test would pass against the
    pre-refactor inline code (which already carries factory forward). The
    second reload is the trap — it verifies the factory survives the
    delegation path twice in a row (i.e., the new code reads factory from old
    on every reload, not just once).
    """

    def my_factory(_brain_root):
        return StubContentStore()

    config = VaultConfig.model_validate(minimal_vault_config_dict)
    async with initialize_services_for_test(
        config,
        content_store_factory=my_factory,
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        _mcp._vaults["factory_vault"] = services
        try:
            assert _mcp._vaults["factory_vault"].content_store_factory is my_factory

            # First reload
            result1 = _parse(await reload_vault("factory_vault"))
            assert result1["reloaded"] is True
            assert _mcp._vaults["factory_vault"].content_store_factory is my_factory

            # Second reload — the real anti-coincidental check
            result2 = _parse(await reload_vault("factory_vault"))
            assert result2["reloaded"] is True
            assert _mcp._vaults["factory_vault"].content_store_factory is my_factory
            assert isinstance(_mcp._vaults["factory_vault"].content_store, StubContentStore)
        finally:
            # Post-reload registry slot may be a fresh bundle; close it
            # before the helper exits. The helper only closes the
            # original ``services`` (idempotent if reload already did).
            current = _mcp._vaults.get("factory_vault")
            if current is not None and current is not services:
                current.close_timing()
                await current.graph_store.close()
            _mcp._vaults.pop("factory_vault", None)


async def test_reload_vault_picks_up_yaml_edits(minimal_vault_config_dict, tmp_vault_dir, tmp_path):
    """Reload re-reads vault_config.yaml from disk and reflects edits.

    Documents the contract: when a vault was loaded from a YAML file and
    the file is later edited, reload_vault must pick up the new
    values, not silently reuse the in-memory config.
    """
    import yaml as _yaml

    from sage.config import load_vault_config

    config_path = tmp_path / "vault_config.yaml"
    initial_config_dict = _copy_dict(minimal_vault_config_dict)
    initial_config_dict["abstraction"] = {"enabled": True}
    config_path.write_text(_yaml.safe_dump(initial_config_dict))

    config = load_vault_config(config_path)
    async with initialize_services_for_test(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
        config_path=config_path,
    ) as services:
        _mcp._vaults["yaml_reload_vault"] = services
        try:
            # Sanity check: starting state matches what we wrote
            assert _mcp._vaults["yaml_reload_vault"].config.abstraction.enabled is True

            # Edit the YAML on disk
            edited = _copy_dict(initial_config_dict)
            edited["abstraction"]["enabled"] = False
            config_path.write_text(_yaml.safe_dump(edited))

            # Reload
            result = _parse(await reload_vault("yaml_reload_vault"))
            assert result["reloaded"] is True

            # In-memory config now reflects the edit
            assert _mcp._vaults["yaml_reload_vault"].config.abstraction.enabled is False, (
                "reload_vault did not re-read the YAML from disk"
            )
        finally:
            # Post-reload registry slot may be a fresh bundle; close it
            # before the helper exits. The helper only closes the
            # original ``services`` (idempotent if reload already did).
            current = _mcp._vaults.get("yaml_reload_vault")
            if current is not None and current is not services:
                current.close_timing()
                await current.graph_store.close()
            _mcp._vaults.pop("yaml_reload_vault", None)


def _copy_dict(d: dict) -> dict:
    import copy as _copy

    return _copy.deepcopy(d)


# ---------------------------------------------------------------------------
# Outer-sequence atomicity at the MCP envelope surface: verifies the
# restructured service methods (yaml-write+reload rollback;
# build-new-first migration) are wired through the MCP envelope,
# mirroring the inner-reload reload-failure surface tests above.
# ---------------------------------------------------------------------------


@pytest.fixture
async def vault_services_with_registry(minimal_vault_config_dict, tmp_vault_dir, monkeypatch):
    """Parallel to ``vault_services`` but wires a real ``VaultRegistryService``
    into the services bundle so calls that need ``_registry_service`` -- such
    as ``update_vault_config`` and ``migrate_vault`` -- can
    reach the registry-reload code path. Also installs stub providers via
    ``SAGE_TEST_STUB_PROVIDERS=1`` and a ``content_store_factory`` so reload
    paths don't try to build LanceDB.
    """
    from sage.services.vault_registry import VaultRegistryService

    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    registry_service = VaultRegistryService(_mcp._vaults, initialize_services)
    async with initialize_services_for_test(
        config,
        registry_service=registry_service,
        content_store_factory=lambda _brain: StubContentStore(),
    ) as services:
        _mcp._vaults["test_vault"] = services
        try:
            yield services
        finally:
            await asyncio.sleep(0.1)
            # Re-read the registry at teardown -- a successful migrate or
            # reload swaps the slot, and the local ``services`` binding
            # becomes stale. If the slot was swapped, the post-swap bundle
            # needs an explicit close here; the helper's exit only closes
            # the original ``services.graph_store`` (idempotent if reload
            # already closed it).
            current = _mcp._vaults.get("test_vault")
            if current is not None and current is not services:
                current.close_timing()
                await current.graph_store.close()
            _mcp._vaults.pop("test_vault", None)


async def test_sage_update_vault_config_atomicity_via_mcp_surface(
    vault_services_with_registry, monkeypatch, tmp_path, tmp_vault_dir
):
    """C1: an MCP ``update_vault_config`` call that fails at the
    inner reload step rolls back the on-disk yaml and leaves the registry
    slot identity unchanged.

    Trap (anti-coincidental): the registry-preservation half of this
    assertion is already guaranteed by the inner-reload build-new-first
    contract. The trap that *only* the outer-sequence rollback satisfies
    is the yaml-rollback half -- a write-first, reload-second
    implementation persists the new yaml on disk even when the reload
    raises.
    """
    import yaml as _yaml

    import sage.mcp_init as _mcp_init

    # Isolate yaml writes to a tmp dir; otherwise the MCP path touches
    # ``~/sage_vaults/test_vault/vault_config.yaml`` on the live host.
    monkeypatch.setattr("sage.vault_management._VAULTS_ROOT", tmp_path / "sage_vaults")

    # First, seed the on-disk yaml with a known state via a successful
    # MCP call. After this call the registry slot is freshly swapped by
    # the reload step, so ``old`` below captures the post-seed services.
    seed_result = _parse(
        await update_vault_config(
            vault_id="test_vault",
            vault={
                "id": "test_vault",
                "name": "MCP Pre Failure",
                "owner": "testuser",
                "storage_root": str(tmp_vault_dir / "sources"),
                "brain_root": str(tmp_vault_dir / "brain"),
                "visibility": "personal",
            },
        )
    )
    assert "error" not in seed_result, seed_result

    config_path = tmp_path / "sage_vaults" / "test_vault" / "vault_config.yaml"
    pre_call_dict = _yaml.safe_load(config_path.read_text())
    assert pre_call_dict["vault"]["name"] == "MCP Pre Failure"

    old = _mcp._vaults["test_vault"]

    async def failing_initialize_services(*args, **kwargs):
        raise SAGEError(
            code="schema_migration_required",
            message="simulated reload failure for outer-sequence atomicity MCP test",
            status_code=409,
        )

    monkeypatch.setattr(_mcp_init, "initialize_services", failing_initialize_services)
    monkeypatch.setattr(_mcp, "initialize_services", failing_initialize_services)

    result = _parse(
        await update_vault_config(
            vault_id="test_vault",
            vault={
                "id": "test_vault",
                "name": "MCP Should Not Persist",
                "owner": "testuser",
                "storage_root": str(tmp_vault_dir / "sources"),
                "brain_root": str(tmp_vault_dir / "brain"),
                "visibility": "personal",
            },
        )
    )

    assert result.get("error") == "schema_migration_required"
    assert "simulated reload failure" in result["message"]

    # Registry slot identity unchanged (inner-reload build-new-first
    # contract).
    assert _mcp._vaults["test_vault"] is old

    # Yaml rolled back (outer-sequence atomicity at the MCP surface).
    post_call_dict = _yaml.safe_load(config_path.read_text())
    assert post_call_dict == pre_call_dict, (
        "MCP-path yaml-rollback failed: on-disk yaml carries the failed call's body. "
        f"Expected name={pre_call_dict['vault']['name']!r}, "
        f"got name={post_call_dict['vault']['name']!r}."
    )


async def test_sage_maint_migrate_vault_atomicity_via_mcp_surface(
    vault_services_with_registry, monkeypatch
):
    """C2: an MCP ``migrate_vault`` call that fails mid-operation returns a
    structured error envelope and leaves the vault's registered services
    untouched and live.

    On the Postgres backend the schema step is a deliberate no-op (the
    schema is provisioned externally), so the fallible stage that remains is
    the tier3-uniqueness scan; the failure is injected there. Trap
    (anti-coincidental): a migrate path that tears down or swaps the vault's
    services before its fallible stage would leave the registry slot
    re-pointed or its graph_store closed — and a closed store's CAS-ADR-036
    dispatch barrier would raise on the behavioural co-assertion below.
    """
    from sage.services.maintenance import MaintenanceService

    async def failing_tier3_scan(self):
        raise SAGEError(
            code="schema_migration_required",
            message="simulated tier3-scan failure for MCP atomicity test",
            status_code=409,
        )

    monkeypatch.setattr(MaintenanceService, "_activate_tier3_uniqueness", failing_tier3_scan)

    old = _mcp._vaults["test_vault"]

    result = _parse(await migrate_vault(vault_id="test_vault"))

    assert result.get("error") == "schema_migration_required"
    assert "simulated tier3-scan failure" in result["message"]

    # Registry slot identity unchanged and graph_store still live.
    # Behavioural co-assertion per TEST-SAGE-BH-137 confirms the CAS-ADR-036
    # dispatch barrier did not engage.
    assert _mcp._vaults["test_vault"] is old
    live_docs = await old.graph_store.list_all_documents()
    assert isinstance(live_docs, list)


# ---------------------------------------------------------------------------
# Reabstract
# ---------------------------------------------------------------------------


async def test_reabstract_returns_started_status(vault_services):
    """BH-122: recompute_abstract should return a JSON response with
    status='reabstract_started' and the document_id, not the full
    document (fire-and-forget pattern)."""
    ingest_result = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    # ingest_document dispatches Stages 2-3 in the background (BH-130).
    # Wait for the ingest's own abstraction to finish and release its claim,
    # so reabstract has a projection to work with and is not rejected as a
    # concurrent job.
    await _await_document_idle(vault_services, "test_vault", doc_id)

    result = _parse(await recompute_abstract("test_vault", doc_id))
    assert "error" not in result
    assert result["status"] == "reabstract_started"
    assert result["document_id"] == doc_id


async def test_reabstract_unknown_vault(vault_services):
    """recompute_abstract should return an error for unknown vault_id."""
    result = _parse(await recompute_abstract("nonexistent_vault", "deadbeef_doc"))
    assert result["error"] == "unknown_vault"


async def test_reabstract_document_not_found(vault_services):
    """recompute_abstract should return document_not_found for unknown doc."""
    result = _parse(await recompute_abstract("test_vault", "deadbeef_nonexistent"))
    assert result["error"] == "document_not_found"


async def test_sage_reabstract_mcp_tool_returns_409_on_concurrent_call(vault_services):
    """A second recompute_abstract call against the same document_id while the
    first is mid-flight must return the structured 409 error envelope
    (no exception propagated past the MCP boundary).
    """
    from datetime import datetime

    ingest_result = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    # The claim is shared across job kinds, so the ingest's own abstraction
    # would reject the *first* call below and the gated provider would never
    # engage. Wait for the document to go idle before contending for it.
    await _await_document_idle(vault_services, "test_vault", doc_id)

    entered = asyncio.Event()
    gate = asyncio.Event()

    async def gated_abstract(text: str, max_tokens: int, doc_type: str | None) -> str:
        entered.set()
        await gate.wait()
        return "gated abstract"

    vault_services.ingestion_service._abstraction.generate_abstract = gated_abstract

    first = _parse(await recompute_abstract("test_vault", doc_id))
    assert first.get("status") == "reabstract_started"

    await asyncio.wait_for(entered.wait(), timeout=2.0)

    try:
        second = _parse(await recompute_abstract("test_vault", doc_id))
        assert second["error"] == "reabstract_document_already_in_flight"
        assert second["detail"]["document_id"] == doc_id
        # detail["start_time"] is an ISO 8601 string; just confirm it parses.
        datetime.fromisoformat(second["detail"]["start_time"])
    finally:
        gate.set()
        await asyncio.sleep(0.3)


# ---------------------------------------------------------------------------
# Recompute pipeline (operator-driven Stage 1-3 re-run)
# ---------------------------------------------------------------------------


async def test_recompute_pipeline_tool_returns_started_status(vault_services):
    """recompute_pipeline MCP tool returns the fire-and-forget envelope with
    status='recompute_pipeline_started' and the dispatched document_id.
    """
    ingest_result = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    # Wait for the initial ingest to commit chunks and release its claim so
    # recompute_pipeline has the steady-state "re-run from terminal" path; the
    # stuck-recovery path is exercised at the service layer
    # (test_ingestion.py B1).
    await _await_document_idle(vault_services, "test_vault", doc_id)

    result = _parse(await recompute_pipeline("test_vault", doc_id))
    assert "error" not in result
    assert result["status"] == "recompute_pipeline_started"
    assert result["document_id"] == doc_id


async def test_recompute_pipeline_tool_unknown_vault_returns_envelope(vault_services):
    """Unknown vault_id must surface as the unknown_vault envelope."""
    result = _parse(await recompute_pipeline("nonexistent_vault", "deadbeef_doc"))
    assert result["error"] == "unknown_vault"


async def test_recompute_pipeline_tool_unknown_document_returns_envelope(vault_services):
    """Unknown document_id (valid vault) must surface as the document_not_found
    envelope -- not a propagated exception.
    """
    result = _parse(await recompute_pipeline("test_vault", "deadbeef_nonexistent"))
    assert result["error"] == "document_not_found"


async def test_recompute_pipeline_tool_invalid_document_id_returns_envelope(vault_services):
    """An empty document_id fails typed-alias validation at the tool
    boundary and surfaces as the structured ``invalid_document_id`` (400)
    envelope -- the convention catches the pydantic ValidationError as
    ``ValueError`` and funnels it through ``_error_response``, which now
    maps the malformed-document_id case to the caller-actionable code
    (carrying the offending value) rather than the generic
    ``internal_error``. The fix is surface-wide: it applies to every tool
    that validates a document_id at the boundary, not only the read tools.
    """
    result = _parse(await recompute_pipeline("test_vault", ""))
    assert "error" in result
    assert result["error"] == "invalid_document_id"
    assert result["detail"]["document_id"] == ""


async def test_recompute_pipeline_tool_concurrent_returns_409(vault_services):
    """A second concurrent recompute_pipeline against the same document_id
    while the first is mid-flight must return the structured 409 envelope
    (no exception propagated past the MCP boundary).
    """
    from datetime import datetime

    ingest_result = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    # The claim is shared across job kinds, so the ingest's own abstraction
    # would reject the *first* call below and the gated embed would never
    # engage. Wait for the document to go idle before contending for it.
    await _await_document_idle(vault_services, "test_vault", doc_id)

    # Gate embed so the first recompute_pipeline call holds its
    # reservation while the second attempts entry.
    entered = asyncio.Event()
    gate = asyncio.Event()
    original_embed = vault_services.ingestion_service._embedding.embed

    async def gated_embed(texts):
        entered.set()
        await gate.wait()
        return await original_embed(texts)

    vault_services.ingestion_service._embedding.embed = gated_embed

    first = _parse(await recompute_pipeline("test_vault", doc_id))
    assert first["status"] == "recompute_pipeline_started"

    await asyncio.wait_for(entered.wait(), timeout=2.0)
    assert not gate.is_set()

    try:
        second = _parse(await recompute_pipeline("test_vault", doc_id))
        assert second["error"] == "recompute_pipeline_already_in_flight"
        assert second["detail"]["document_id"] == doc_id
        datetime.fromisoformat(second["detail"]["start_time"])
    finally:
        gate.set()
        await asyncio.sleep(0.3)


# ---------------------------------------------------------------------------
# First-class edge enumeration via search(target="edges")
# ---------------------------------------------------------------------------


async def test_sage_discover_edges_happy_path(vault_services):
    """28. End-to-end happy path via the MCP tool: target=edges + mode=catalog
    returns a serialized envelope with target field, results array, and
    total_available.
    """
    doc_a = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await ingest_document("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)
    # supersedes has resolution_policy=none so no anchor version
    # requirements; using it keeps the fixture small while still
    # exercising the edge enumeration path.
    link_result = _parse(
        await create_edge(
            "test_vault",
            doc_a["id"],
            doc_b["id"],
            "supersedes",
            rationale="t0157 fixture",
        )
    )

    # Sage_link returns wrapper; edge fields live under "edge".
    actual_source = link_result["edge"]["source_id"]
    actual_target = link_result["edge"]["target_id"]

    result = _parse(
        await search(
            "test_vault",
            mode="catalog",
            target="edges",
            filters={"source_id": actual_source},
            response_mode="full",
        )
    )
    assert result.get("target") == "edges"
    assert result.get("mode") == "catalog"
    assert result["total_available"] >= 1, (
        f"expected at least 1 edge from {actual_source}, got 0. result={result}"
    )
    assert isinstance(result["results"], list)
    hit = result["results"][0]
    for key in ("edge_id", "source_id", "target_id", "edge_type", "rationale"):
        assert key in hit, f"full envelope missing {key}: {hit.keys()}"
    assert hit["source_id"] == actual_source
    assert hit["target_id"] == actual_target
    assert hit["edge_type"] == "supersedes"


async def test_sage_discover_edges_light_round_trips_through_serializer(vault_services):
    """29. Light mode round-trips through serialize(): the envelope keys
    on the wire match what the model produced.
    """
    doc_a = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await ingest_document("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)
    link = _parse(await create_edge("test_vault", doc_a["id"], doc_b["id"], "supersedes"))

    result = _parse(
        await search(
            "test_vault",
            mode="catalog",
            target="edges",
            # Sage_link returns wrapper; source_id is under "edge".
            filters={"source_id": link["edge"]["source_id"]},
            response_mode="light",
        )
    )
    hit = result["results"][0]
    # serialize() uses exclude_none=True so light rows on the wire should
    # carry exactly the identity columns. The dict is JSON, not a Pydantic
    # model, so we check key presence directly.
    assert set(hit.keys()) == {"edge_id", "source_id", "target_id", "edge_type"}


async def test_sage_discover_edges_target_edges_with_semantic_returns_error(vault_services):
    """28b. target=edges combined with a non-catalog mode is rejected via
    the typed mode_parameter_mismatch error envelope.
    """
    result = _parse(
        await search(
            "test_vault",
            mode="semantic",
            target="edges",
            query="anything",
        )
    )
    assert result["error"] == "mode_parameter_mismatch", result


def test_sage_discover_docstring_carries_edge_example():
    """30. search docstring documents the target="edges" dispatch
    with a worked example. This is a guard test that fails closed when
    the cross-tool documentation contract breaks (e.g., a later edit
    drops the example).
    """
    doc = search.__doc__
    assert doc is not None
    assert 'target="edges"' in doc, (
        "search docstring must carry a worked example for the target='edges' dispatch"
    )


def test_sage_unlink_docstring_points_at_edge_discovery():
    """31. delete_edge docstring references search(target="edges")
    as the canonical path to discover edge_id. Guard test.
    """
    doc = _sage_unlink_tool.__doc__
    assert doc is not None
    assert 'target="edges"' in doc, (
        "delete_edge docstring must point at search(target='edges') "
        "as the canonical edge_id discovery path"
    )


def test_retracts_edge_type_docstring_points_at_edge_discovery():
    """32. EdgeType class docstring documents the discovery path for
    edge_id when minting a retracts edge. Guard test.
    """
    doc = _EdgeType.__doc__
    assert doc is not None
    assert 'target="edges"' in doc, (
        "EdgeType class docstring must point at search(target='edges') "
        "for retracts edge_id discovery"
    )


# ---------------------------------------------------------------------------
# Facet aggregation via search(target="facets")
# ---------------------------------------------------------------------------


async def test_sage_discover_facets_happy_path(vault_services):
    """End-to-end happy path via the MCP tool: target=facets + mode=catalog
    returns a serialized envelope with one facet row per field.

    The wire-shape assertion (each row carries exactly {"field",
    "values", "total_distinct"}) also pins that serialize()'s
    exclude_none=True drops only None -- a zero-count field must keep
    its empty values object AND its zero total_distinct, so the shape
    survives an int-or-None modeling regression that would drop the
    zero case.
    """
    await ingest_document("test_vault", "test/sample.md", "markdown")
    await ingest_document("test_vault", "test/second.md", "markdown")
    await asyncio.sleep(0.3)

    result = _parse(await search("test_vault", mode="catalog", target="facets"))

    assert result.get("target") == "facets"
    assert result.get("mode") == "catalog"
    assert result["total_available"] == 2
    rows = result["results"]
    assert [r["field"] for r in rows] == [
        "doc_type",
        "lifecycle_status",
        "source_type",
        "pipeline_status",
        "tags",
    ]
    for r in rows:
        assert set(r.keys()) == {"field", "values", "total_distinct"}, (
            f"facet rows on the wire must carry exactly "
            f"field+values+total_distinct, got {set(r.keys())}"
        )
    by_field = {r["field"]: r for r in rows}
    assert by_field["source_type"]["values"] == {"markdown": 2}
    assert by_field["source_type"]["total_distinct"] == 1
    assert by_field["lifecycle_status"]["values"] == {"active": 2}
    # No tags were ingested: the zero-count field keeps an explicit
    # empty object and a zero total on the wire rather than being
    # dropped.
    assert by_field["tags"]["values"] == {}
    assert by_field["tags"]["total_distinct"] == 0


async def test_sage_discover_facet_params_roundtrip(vault_services):
    """facet_fields and facet_value_limit reach the retrieval service
    through the MCP signature.

    The plumbing trap: publishing the parameters on the tool signature
    without forwarding them into DiscoverRequest would silently ignore
    them -- every lower layer passes while the tool no-ops. Each
    parameter needs its own discriminating observable: the row list
    catches an unforwarded facet_fields (five rows instead of one), and
    the two-tag fixture catches an unforwarded facet_value_limit (the
    default cap of 50 would return both tags instead of one).
    """
    await ingest_document(
        "test_vault", "test/sample.md", "markdown", metadata={"tags": ["za", "zb"]}
    )
    await ingest_document("test_vault", "test/second.md", "markdown", metadata={"tags": ["za"]})
    await asyncio.sleep(0.3)

    result = _parse(
        await search(
            "test_vault",
            mode="catalog",
            target="facets",
            facet_fields=["tags"],
            facet_value_limit=1,
        )
    )

    rows = result["results"]
    assert [r["field"] for r in rows] == ["tags"], (
        "facet_fields must select rows -- all five rows means the "
        "parameter never reached the service"
    )
    assert rows[0]["values"] == {"za": 2}, (
        "facet_value_limit=1 must cap the row to the top value -- both "
        "tags present means the parameter never reached the service"
    )
    assert rows[0]["total_distinct"] == 2


async def test_sage_discover_facets_with_semantic_returns_error(vault_services):
    """target=facets combined with a non-catalog mode is rejected via
    the typed mode_parameter_mismatch error envelope.
    """
    result = _parse(
        await search(
            "test_vault",
            mode="semantic",
            target="facets",
            query="anything",
        )
    )
    assert result["error"] == "mode_parameter_mismatch", result


def test_sage_discover_docstring_carries_facet_example():
    """search docstring documents the target="facets" dispatch with a
    worked example. Guard test that fails closed when the documentation
    contract breaks.
    """
    doc = search.__doc__
    assert doc is not None
    assert 'target="facets"' in doc, (
        "search docstring must carry a worked example for the target='facets' dispatch"
    )


# ---------------------------------------------------------------------------
# Document_id alias on traverse + docstring clarification on create_edge
# ---------------------------------------------------------------------------
#
# MCP tools should converge on `document_id` as the canonical
# parameter name for "the document being operated on". traverse historically
# uses `start_id`; this section verifies that `document_id` is accepted as an
# alias (both forms work, exactly one must be supplied). create_edge keeps its
# semantic `source_id`/`target_id` distinction; the docstring is clarified to
# state both are `documents.id` values.


async def test_traverse_accepts_document_id_alias(vault_services):
    """T1. traverse accepts `document_id` as a keyword alias for
    `start_id`. Happy path: alias resolves to the same traversal result
    as the canonical name.
    """
    doc_a = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await ingest_document("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)
    await create_edge("test_vault", doc_a["id"], doc_b["id"], "supersedes")

    result = _parse(await traverse(vault_id="test_vault", document_id=doc_a["id"]))
    # Response shape unchanged: `start_id` remains the response key.
    assert result["start_id"] == doc_a["id"]
    assert len(result["nodes"]) == 1
    # Asserting the *correct* neighbor (doc_b) defeats any coincidental pass
    # where the alias was dropped and the function defaulted to some other id.
    assert result["nodes"][0]["document"]["id"] == doc_b["id"]


async def test_traverse_accepts_start_id_kwarg(vault_services):
    """T2. traverse continues to accept `start_id` as a keyword
    argument after the alias is added. Back-compat guard.
    """
    doc_a = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await ingest_document("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)
    await create_edge("test_vault", doc_a["id"], doc_b["id"], "supersedes")

    result = _parse(await traverse(vault_id="test_vault", start_id=doc_a["id"]))
    assert result["start_id"] == doc_a["id"]
    assert len(result["nodes"]) == 1
    assert result["nodes"][0]["document"]["id"] == doc_b["id"]


async def test_traverse_accepts_start_id_positional(vault_services):
    """T3. traverse continues to accept `start_id` positionally
    after the alias is added. Back-compat guard for the form used by
    the vast majority of existing tests.
    """
    doc_a = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await ingest_document("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)
    await create_edge("test_vault", doc_a["id"], doc_b["id"], "supersedes")

    result = _parse(await traverse("test_vault", doc_a["id"]))
    assert result["start_id"] == doc_a["id"]
    assert len(result["nodes"]) == 1
    assert result["nodes"][0]["document"]["id"] == doc_b["id"]


async def test_traverse_rejects_both_kwargs(vault_services):
    """T4. traverse rejects supplying both `start_id` and
    `document_id` (even with equal values). Strict ambiguity rule:
    exactly one must be supplied.
    """
    doc = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.3)

    result = _parse(
        await traverse(
            vault_id="test_vault",
            start_id=doc["id"],
            document_id=doc["id"],
        )
    )
    assert result["error"] == "ambiguous_document_identifier"
    # Detail must name both parameter names verbatim, so callers can
    # see which fields are in conflict.
    assert "start_id" in result["detail"]["supplied"]
    assert "document_id" in result["detail"]["supplied"]


async def test_traverse_rejects_missing_identifier(vault_services):
    """T5. traverse rejects neither `start_id` nor `document_id`
    being supplied. Specific code (not a downstream `document_not_found`
    or generic ValidationError) confirms the validation branch fired.
    """
    result = _parse(await traverse(vault_id="test_vault"))
    assert result["error"] == "missing_document_identifier"
    # Detail must enumerate the accepted parameter names so the caller
    # learns the alias without trial-and-error.
    assert "start_id" in result["detail"]["accepted"]
    assert "document_id" in result["detail"]["accepted"]


async def test_traverse_rejects_positional_plus_alias_kwarg(vault_services):
    """T6. traverse rejects positional `start_id` plus keyword
    `document_id`. Mixing the two forms of the same logical argument
    is treated as the both-supplied case, not as silent precedence.
    """
    doc = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.3)

    result = _parse(await traverse("test_vault", doc["id"], document_id=doc["id"]))
    assert result["error"] == "ambiguous_document_identifier"
    assert "start_id" in result["detail"]["supplied"]
    assert "document_id" in result["detail"]["supplied"]


def test_traverse_docstring_documents_alias():
    """T7. traverse docstring documents the `document_id` alias
    inline on the `start_id` Args entry (not just in prose elsewhere).
    Guard test: ensures the ticket's docstring requirement lands at
    the parameter site where an MCP caller browsing the schema will
    see it.
    """
    import re
    import textwrap

    doc = traverse.__doc__
    assert doc is not None
    dedented = textwrap.dedent(doc)
    # Anchor the match to the start_id: line of the Args section: the
    # word document_id must appear on the same line where start_id is
    # being described. A loose `"document_id" in doc` would pass
    # coincidentally if document_id appeared in unrelated prose.
    assert re.search(r"start_id:[^\n]*document_id", dedented), (
        "traverse docstring must document `document_id` as an alias "
        "inline on the start_id Args entry"
    )


# ---------------------------------------------------------------------------
# doc_id alias on the document_id read tools
# ---------------------------------------------------------------------------
#
# get_document / read_projection / read_section / list_headings accept
# ``doc_id`` as a tolerance alias for ``document_id``. Both forms are
# published as optional, default-null properties (so a doc_id-only call
# survives the additionalProperties:false client coercion and reaches the
# server); the server resolves exactly one. Mirrors the start_id/document_id
# alias on traverse (test_t0155_* above).

# (tool_fn, extra-required-kwargs) for the document-id read tools.
# ``read_section`` also needs ``heading_path`` and ``chain`` needs
# ``edge_type``; the others take only the id.
_DOC_ID_ALIAS_READ_TOOLS = [
    pytest.param(get_document, {}, id="get_document"),
    pytest.param(read_projection, {}, id="read_projection"),
    pytest.param(read_section, {"heading_path": "Sample Document"}, id="read_section"),
    pytest.param(list_headings, {}, id="list_headings"),
    pytest.param(chain, {"edge_type": "supersedes"}, id="chain"),
]


@pytest.mark.parametrize(
    "tool_name",
    ["get_document", "read_projection", "read_section", "list_headings", "chain"],
)
def test_publishes_doc_id_as_optional_property(tool_name):
    """A1. Both ``document_id`` and ``doc_id`` are published as optional
    (default-null) properties, with ``additionalProperties: false``. This
    is the precise published-schema shape under which a doc_id-only call
    survives the Cowork client's client-side coercion (doc_id not stripped,
    document_id not missing-required) and reaches the server. A server-only
    alias would leave doc_id out of the published schema and fail this.
    """
    tool = _mcp.mcp._tool_manager.get_tool(tool_name)
    schema = tool.parameters
    props = schema.get("properties", {})
    assert "document_id" in props, f"{tool_name} must publish document_id"
    assert "doc_id" in props, f"{tool_name} must publish doc_id"
    required = schema.get("required", [])
    # Both optional: neither is required, so a call supplying only the
    # other form passes client-side validation.
    assert "document_id" not in required, f"{tool_name} document_id must be optional"
    assert "doc_id" not in required, f"{tool_name} doc_id must be optional"
    # The additionalProperties:false strict-args substrate invariant the
    # alias relies on to survive client-side coercion.
    assert schema.get("additionalProperties") is False


async def test_get_document_accepts_doc_id_alias(vault_services):
    """B1. get_document resolves ``doc_id`` to the same record as
    ``document_id``. The correct-id assertion (not merely "no error")
    defeats a coincidental pass where the alias was accepted by the
    signature but never resolved.
    """
    doc = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    result = _parse(await get_document(vault_id="test_vault", doc_id=doc["id"]))
    assert result["id"] == doc["id"]
    assert result["title"] == "Sample Document"


async def test_read_projection_accepts_doc_id_alias(vault_services):
    """B1. read_projection resolves ``doc_id`` to the correct document's
    projection.
    """
    doc = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.5)
    result = _parse(await read_projection(vault_id="test_vault", doc_id=doc["id"]))
    assert result["document_id"] == doc["id"]
    assert len(result["projection_text"]) > 0


async def test_read_section_accepts_doc_id_alias(vault_services):
    """B1. read_section resolves ``doc_id`` to the correct document's
    section (heading_path supplied alongside the alias).
    """
    doc = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.5)
    result = _parse(
        await read_section(vault_id="test_vault", doc_id=doc["id"], heading_path="Sample Document")
    )
    assert result["document_id"] == doc["id"]
    assert result["heading_path"] == "Sample Document"


async def test_list_headings_accepts_doc_id_alias(vault_services):
    """B1. list_headings resolves ``doc_id`` to the correct document's
    heading list.
    """
    doc = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.5)
    result = _parse(await list_headings(vault_id="test_vault", doc_id=doc["id"]))
    assert result["document_id"] == doc["id"]
    assert "Sample Document" in result["headings"]


async def test_chain_accepts_doc_id_alias(vault_services):
    """B1. chain resolves ``doc_id`` to the correct document. A freshly
    ingested doc with no supersedes edges is its own single-entry chain, so
    head_id echoes the resolved id — a coincidental pass that dropped the
    alias would surface missing_document_identifier instead.
    """
    doc = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    result = _parse(await chain(vault_id="test_vault", doc_id=doc["id"], edge_type="supersedes"))
    assert result["head_id"] == doc["id"]


async def test_chain_accepts_document_id_keyword(vault_services):
    """B2. chain still accepts the canonical ``document_id`` keyword after
    the alias and the signature reorder (edge_type moved ahead of the
    optional id params). Back-compat guard.
    """
    doc = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    result = _parse(
        await chain(vault_id="test_vault", document_id=doc["id"], edge_type="supersedes")
    )
    assert result["head_id"] == doc["id"]


@pytest.mark.parametrize(
    "tool_fn,extra,echo_key",
    [
        pytest.param(get_document, {}, "id", id="get_document"),
        pytest.param(read_projection, {}, "document_id", id="read_projection"),
        pytest.param(
            read_section,
            {"heading_path": "Sample Document"},
            "document_id",
            id="read_section",
        ),
        pytest.param(list_headings, {}, "document_id", id="list_headings"),
    ],
)
async def test_accepts_document_id_keyword(vault_services, tool_fn, extra, echo_key):
    """B2. The canonical ``document_id`` keyword form still resolves to the
    correct record after the alias is added. Back-compat guard.
    """
    doc = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.5)
    result = _parse(await tool_fn(vault_id="test_vault", document_id=doc["id"], **extra))
    assert result[echo_key] == doc["id"]


@pytest.mark.parametrize(
    "tool_fn,echo_key",
    [
        pytest.param(get_document, "id", id="get_document"),
        pytest.param(read_projection, "document_id", id="read_projection"),
        pytest.param(list_headings, "document_id", id="list_headings"),
    ],
)
async def test_positional_document_id_still_binds(vault_services, tool_fn, echo_key):
    """B2-positional. The ``(vault_id, document_id)`` positional form used by
    existing callers still binds after ``document_id`` became optional.
    Guards against an accidental read_section-style reorder on these three
    tools. (read_section is intentionally excluded: it reorders heading_path
    ahead of the optional id params, so its positional contract changes.)
    """
    doc = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.5)
    result = _parse(await tool_fn("test_vault", doc["id"]))
    assert result[echo_key] == doc["id"]


@pytest.mark.parametrize("tool_fn,extra", _DOC_ID_ALIAS_READ_TOOLS)
async def test_rejects_both_document_id_and_doc_id_when_equal(vault_services, tool_fn, extra):
    """B3. Supplying both ``document_id`` and ``doc_id`` — even with equal
    values — is rejected. Strict ambiguity rule: exactly one must be
    supplied.
    """
    doc = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    result = _parse(
        await tool_fn(vault_id="test_vault", document_id=doc["id"], doc_id=doc["id"], **extra)
    )
    assert result["error"] == "ambiguous_document_identifier"
    assert "document_id" in result["detail"]["supplied"]
    assert "doc_id" in result["detail"]["supplied"]


@pytest.mark.parametrize("tool_fn,extra", _DOC_ID_ALIAS_READ_TOOLS)
async def test_rejects_both_document_id_and_doc_id_when_unequal(vault_services, tool_fn, extra):
    """B4. Supplying two *different* ids via the canonical name and the
    alias is rejected. Exercises the conflict branch distinctly from the
    both-equal case.
    """
    doc_a = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await ingest_document("test_vault", "test/second.md", "markdown"))
    result = _parse(
        await tool_fn(vault_id="test_vault", document_id=doc_a["id"], doc_id=doc_b["id"], **extra)
    )
    assert result["error"] == "ambiguous_document_identifier"
    assert "document_id" in result["detail"]["supplied"]
    assert "doc_id" in result["detail"]["supplied"]


@pytest.mark.parametrize("tool_fn,extra", _DOC_ID_ALIAS_READ_TOOLS)
async def test_rejects_neither_document_id_nor_doc_id(vault_services, tool_fn, extra):
    """B5. Supplying neither ``document_id`` nor ``doc_id`` yields the
    structured ``missing_document_identifier`` code (not a downstream
    ``document_not_found`` or a Python TypeError), confirming the
    resolution branch fired before any service call.
    """
    result = _parse(await tool_fn(vault_id="test_vault", **extra))
    assert result["error"] == "missing_document_identifier"
    assert "document_id" in result["detail"]["accepted"]
    assert "doc_id" in result["detail"]["accepted"]


@pytest.mark.parametrize(
    "tool_fn",
    [get_document, read_projection, read_section, list_headings, chain],
    ids=["get_document", "read_projection", "read_section", "list_headings", "chain"],
)
def test_docstring_documents_doc_id_alias(tool_fn):
    """C1. Each tool's docstring documents the ``doc_id`` alias inline on the
    ``document_id`` Args entry (where an MCP caller browsing the schema sees
    it), not in unrelated prose. Anchoring to the ``document_id:`` line
    defeats a loose ``"doc_id" in doc`` coincidental pass.
    """
    import re
    import textwrap

    doc = tool_fn.__doc__
    assert doc is not None
    dedented = textwrap.dedent(doc)
    assert re.search(r"document_id:[^\n]*doc_id", dedented), (
        f"{tool_fn.__name__} docstring must document `doc_id` as an alias "
        "inline on the document_id Args entry"
    )


def test_link_per_item_id_fields_use_document_id_alias():
    """Per CAS-ADR-029 v4 the ``create_edges`` tool takes
    ``items: list[dict]``; the per-item ``source_id``, ``target_id``,
    and anchor fields enforce the document-id shape via the
    ``DocumentIdStr`` typed alias on ``BulkLinkItem`` rather than via
    a docstring clarification on the (retired) singleton tool
    signature. The shape clarification moves with the field.

    Anti-coincidental-pass: identity equality against the
    ``Annotated[str, AfterValidator(...)]`` form exposed in the
    class's ``__annotations__`` dict (Pydantic's ``model_fields``
    strips the ``Annotated`` wrapper to the bare type, which would
    not distinguish ``DocumentIdStr`` from bare ``str``). Replacing
    the alias with ``str`` fails the test even if the description
    still mentions "documents.id".
    """
    from sage.models.schemas import BulkLinkItem, DocumentIdStr

    annotations = BulkLinkItem.__annotations__
    # ``source_id`` is non-nullable; the alias must appear directly.
    assert annotations["source_id"] is DocumentIdStr, (
        f"BulkLinkItem.source_id annotation is {annotations['source_id']!r}; "
        "expected DocumentIdStr. Without the alias at the per-item schema, "
        "callers can pass any string and the SQL-lookup hazard "
        "(CAS-ADR-019) re-opens."
    )
    # ``target_id`` and the anchor fields are nullable per the
    # edge_type policy bucket (CAS-ADR-017); the alias appears as
    # ``DocumentIdStr | None``.
    for field_name in (
        "target_id",
        "source_valid_from_version",
        "target_valid_from_version",
    ):
        ann = annotations[field_name]
        assert DocumentIdStr in get_args(ann), (
            f"BulkLinkItem.{field_name} annotation is {ann!r}; expected DocumentIdStr | None."
        )


def test_set_lifecycle_per_item_successor_id_uses_document_id_alias():
    """Per CAS-ADR-029 v4 the ``update_lifecycles`` tool takes
    ``items: list[dict]``; the per-item ``successor_id`` enforces the
    document-id shape via ``DocumentIdStr`` on ``BulkLifecycleItem``.
    Parallel-pattern guard with
    test_link_per_item_id_fields_use_document_id_alias.
    """
    from sage.models.schemas import BulkLifecycleItem, DocumentIdStr

    ann = BulkLifecycleItem.__annotations__["successor_id"]
    assert DocumentIdStr in get_args(ann), (
        f"BulkLifecycleItem.successor_id annotation is {ann!r}; expected DocumentIdStr | None."
    )


# ---------------------------------------------------------------------------
# Server-level operational tools — registration-surface conformance
# ---------------------------------------------------------------------------


def test_reload_vault_and_get_stack_config_in_sage_tools_registry():
    """``reload_vault`` and ``get_stack_config`` are registered
    through ``register_sage_tools`` and the module-level re-exports point
    at the same callables.

    Both are substrate-maintenance operations, placed on the maintenance
    surface by the surface-assignment table (CAS-ADR-029) rather than by
    anything in their names. The two tools have no HTTP counterpart by design and are
    operationally MCP-only; they nonetheless ride the canonical
    registration path so the conformance gates and the ``_sage_tools``
    registry view cover them on the same terms as every other tool.

    Anti-coincidental: identity (``is``) check against the module-level
    re-export rules out bare-key stubs and cross-wired keys; equality
    would tolerate distinct wrappers.
    """
    import sage.mcp_server as _mcp_server

    assert "reload_vault" in _mcp_server._sage_tools, (
        "reload_vault must be registered through register_sage_tools; "
        "an @mcp.tool() definition at module scope in sage/mcp_server.py "
        "would bypass the conformance registry view and is not the "
        "supported registration site."
    )
    assert "get_stack_config" in _mcp_server._sage_tools, (
        "get_stack_config must be registered through register_sage_tools; "
        "an @mcp.tool() definition at module scope in sage/mcp_server.py "
        "would bypass the conformance registry view and is not the "
        "supported registration site."
    )
    assert _mcp_server._sage_tools["reload_vault"] is _mcp_server.reload_vault, (
        "_sage_tools['reload_vault'] must be the same callable as the "
        "sage.mcp_server.reload_vault re-export."
    )
    assert _mcp_server._sage_tools["get_stack_config"] is _mcp_server.get_stack_config, (
        "_sage_tools['get_stack_config'] must be the same callable as the "
        "sage.mcp_server.get_stack_config re-export."
    )


def test_no_get_stack_config_shadow_alias_in_mcp_server():
    """``sage.mcp_server`` does not bind ``_get_stack_config``.

    The alias name would only exist as a workaround for a module-level
    ``async def get_stack_config(...)`` tool function shadowing a same-named
    import from ``sage.mcp_init``. Because the tool function is registered
    inside ``register_sage_tools`` in ``sage.sage_api_tools`` rather than at
    module scope in ``sage.mcp_server``, the shadow does not exist and the
    alias must not be re-introduced.
    """
    import sage.mcp_server as _mcp_server

    assert not hasattr(_mcp_server, "_get_stack_config"), (
        "sage.mcp_server binds `_get_stack_config`; this name should only "
        "exist as a workaround for a module-level tool function shadowing "
        "the sage.mcp_init.get_stack_config import. Remove the alias and "
        "ensure the tool function lives inside register_sage_tools, where "
        "the qualified `sage.mcp_init.get_stack_config()` call resolves "
        "without a shadow."
    )
