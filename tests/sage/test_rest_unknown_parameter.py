"""The Core API refuses a request name the operation does not declare.

A JSON body field or a query parameter an operation does not declare is
refused with the ``unknown_parameter`` envelope the MCP surface returns for an
unknown argument (CAS-ADR-037), so a caller sees one refusal shape whichever
surface it reached (CAS-ADR-052). Before this rule both kinds of name were
discarded without a word, and the call ran as though they had never been sent.

Each refusal row is paired with the identical request minus the unknown name.
The pair is what makes a refusal meaningful: a row whose base request were
itself refused for some other reason would still reach a 400, and only the
paired control shows that the unknown name is what the refusal is about.

The refusal reaches only names at the top of the body or the query string.
A name nested inside a declared field keeps the code it had -- an unknown
filter key is still ``unknown_filter_key`` -- and a field typed as an open
mapping keeps accepting any key.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from sage.models import schemas

VAULT_ID = "test_vault"
VS = f"/sage_vaults/{VAULT_ID}"
# Well-formed ids naming nothing: a refusal must not depend on a record existing.
DOC_ID = "00000000_absent_document"
EDGE_ID = "00000000-0000-0000-0000-000000000000"
BOGUS_FIELD = "bogus_field_x"
BOGUS_QUERY = "bogus_q"


@pytest.fixture
async def client(app_with_one_vault) -> AsyncIterator[AsyncClient]:
    """An ASGI client over the Core API app with one stub-backed vault."""
    transport = ASGITransport(app=app_with_one_vault)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _assert_unknown_parameter(
    resp: Any, *, tool: str, rejected: list[str], valid: list[str]
) -> None:
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "unknown_parameter", body
    assert set(body["detail"]) == {"tool", "rejected_params", "valid_params"}
    assert body["detail"]["tool"] == tool
    assert body["detail"]["rejected_params"] == rejected
    assert body["detail"]["valid_params"] == valid


def _code(resp: Any) -> str | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    return body.get("code") if isinstance(body, dict) else None


def _fields(model: type) -> list[str]:
    return sorted(model.model_fields)


# ---------------------------------------------------------------------------
# Body fields
# ---------------------------------------------------------------------------

# (operationId, method, path, body model, a body the model accepts). The
# operationId is the published one, and several differ from the handler name
# (``search`` is served by ``discover``, ``ingest_document`` by ``ingest``), so
# a refusal naming the handler would fail these rows.
BODY_ROWS: list[tuple[str, str, str, type, dict[str, Any]]] = [
    (
        "create_vault",
        "POST",
        "/sage_vaults",
        schemas.CreateVaultRequest,
        {"config": {}},
    ),
    (
        "update_vault_config",
        "PUT",
        f"{VS}/config",
        schemas.UpdateVaultConfigRequest,
        {"abstraction": {"enabled": False}, "dry_run": True},
    ),
    (
        "verify_hashes",
        "POST",
        f"{VS}/hash-check",
        schemas.HashCheckRequest,
        {"hashes": ["sha256:" + "0" * 64]},
    ),
    (
        "ingest_document",
        "POST",
        f"{VS}/documents",
        schemas.IngestRequest,
        {"source": "absent/nothing.md", "source_type": "markdown"},
    ),
    (
        "update_lifecycles",
        "POST",
        f"{VS}/lifecycles",
        schemas.BulkLifecycleRequest,
        {"items": [{"document_id": DOC_ID, "action": "archive"}], "dry_run": True},
    ),
    (
        "update_metadata",
        "POST",
        f"{VS}/metadata",
        schemas.BulkMetadataRequest,
        {"items": [{"document_id": DOC_ID, "title": "t"}], "dry_run": True},
    ),
    (
        "register_user",
        "POST",
        f"{VS}/users",
        schemas.RegisterUserRequest,
        {"display_name": "Someone", "user_type": "human"},
    ),
    (
        "create_edges",
        "POST",
        f"{VS}/edges",
        schemas.BulkLinkRequest,
        {
            "items": [{"source_id": DOC_ID, "edge_type": "references", "target_id": DOC_ID}],
            "dry_run": True,
        },
    ),
    (
        "traverse",
        "POST",
        f"{VS}/traverse",
        schemas.TraverseRequest,
        {"start_id": DOC_ID},
    ),
    (
        "search",
        "POST",
        f"{VS}/discover",
        schemas.DiscoverRequest,
        {"mode": "catalog", "limit": 1},
    ),
    (
        "export_projection",
        "POST",
        f"{VS}/documents/{DOC_ID}/export",
        schemas.ExportProjectionRequest,
        {"output_path": "/nonexistent-dir/out.md"},
    ),
    (
        "get_filename_metadata",
        "POST",
        f"{VS}/parse-filename",
        schemas.ParseFilenameRequest,
        {"filename": "note.md", "source_type": "markdown"},
    ),
    (
        "verify_vault_source_files",
        "POST",
        f"{VS}/maintenance/verify-source-files",
        schemas.SourceFileIntegrityRequest,
        {"check_hashes": False},
    ),
    (
        "restore_vault_source_file",
        "POST",
        f"{VS}/maintenance/restore-source-file",
        schemas.SourceFileRestoreRequest,
        {"source": "/nonexistent-dir/restore.md", "document_id": DOC_ID},
    ),
]

# Rows whose declared request would change state outside the test database
# when accepted. Their control is the model accepting the body in process.
_NO_HTTP_CONTROL = {"create_vault"}


@pytest.mark.parametrize(
    ("tool", "method", "path", "model", "body"),
    BODY_ROWS,
    ids=[row[0] for row in BODY_ROWS],
)
async def test_unknown_body_field_refused(client, tool, method, path, model, body):
    """An undeclared top-level body field is refused, naming the valid fields."""
    resp = await client.request(method, path, json={**body, BOGUS_FIELD: 1})

    _assert_unknown_parameter(resp, tool=tool, rejected=[BOGUS_FIELD], valid=_fields(model))


@pytest.mark.parametrize(
    ("tool", "method", "path", "model", "body"),
    BODY_ROWS,
    ids=[row[0] for row in BODY_ROWS],
)
async def test_declared_body_not_refused(client, tool, method, path, model, body):
    """The same body without the unknown field is not refused as unknown.

    The model accepting the body in process is asserted for every row, so a
    base body that were itself malformed cannot hide behind a refusal that
    happens to carry the expected code.
    """
    model.model_validate(body)
    if tool in _NO_HTTP_CONTROL:
        return

    resp = await client.request(method, path, json=body)

    assert _code(resp) != "unknown_parameter", resp.text


# ---------------------------------------------------------------------------
# Query parameters
# ---------------------------------------------------------------------------

# (operationId, method, path, declared query already present, json body or
# None, declared query parameter names).
QUERY_ROWS: list[tuple[str, str, str, dict[str, str], dict[str, Any] | None, list[str]]] = [
    ("list_vaults", "GET", "/sage_vaults", {}, None, []),
    (
        "get_document",
        "GET",
        f"{VS}/documents/{DOC_ID}",
        {"include_content": "false"},
        None,
        ["include_content", "write_to_path"],
    ),
    ("delete_edge", "DELETE", f"{VS}/edges/{EDGE_ID}", {"dry_run": "true"}, None, ["dry_run"]),
    ("list_headings", "GET", f"{VS}/documents/{DOC_ID}/headings", {}, None, []),
    ("list_staging_edges", "GET", f"{VS}/staging-edges", {}, None, []),
    (
        "list_pending_metadata",
        "GET",
        f"{VS}/pending-metadata",
        {},
        None,
        ["limit", "offset", "response_mode"],
    ),
    ("batch_ingest_documents", "POST", f"{VS}/documents:batch", {}, None, []),
    (
        "update_vault_config",
        "PUT",
        f"{VS}/config",
        {"force": "false"},
        {"abstraction": {"enabled": False}, "dry_run": True},
        ["force"],
    ),
    ("traverse", "POST", f"{VS}/traverse", {}, {"start_id": DOC_ID}, []),
    ("transfer_upload", "PUT", "/upload", {}, None, []),
    ("verify_preconditions", "GET", f"{VS}/preconditions/{DOC_ID}", {}, None, []),
    ("get_vault_stats", "GET", f"{VS}/stats", {}, None, []),
    ("get_stack_config", "GET", "/sage_vaults/maintenance/stack-config", {}, None, []),
]

# Rows whose declared request succeeds against an empty vault, so the control
# can assert success rather than only the absence of the refusal.
_SUCCEEDS_WHEN_DECLARED = {
    "list_vaults",
    "list_staging_edges",
    "list_pending_metadata",
    "get_stack_config",
}


@pytest.mark.parametrize(
    ("tool", "method", "path", "params", "body", "declared"),
    QUERY_ROWS,
    ids=[row[0] for row in QUERY_ROWS],
)
async def test_unknown_query_parameter_refused(client, tool, method, path, params, body, declared):
    """An undeclared query parameter is refused, naming the declared ones."""
    resp = await client.request(method, path, params={**params, BOGUS_QUERY: "1"}, json=body)

    _assert_unknown_parameter(resp, tool=tool, rejected=[BOGUS_QUERY], valid=sorted(declared))


@pytest.mark.parametrize(
    ("tool", "method", "path", "params", "body", "declared"),
    QUERY_ROWS,
    ids=[row[0] for row in QUERY_ROWS],
)
async def test_declared_query_not_refused(client, tool, method, path, params, body, declared):
    """The same request without the unknown parameter is not refused as unknown."""
    resp = await client.request(method, path, params=params, json=body)

    assert _code(resp) != "unknown_parameter", resp.text
    if tool in _SUCCEEDS_WHEN_DECLARED:
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Scope and ordering
# ---------------------------------------------------------------------------


async def test_unknown_filter_key_keeps_its_code(client):
    """A name nested under ``filters`` is still an unknown filter key."""
    resp = await client.post(f"{VS}/discover", json={"query": "x", "filters": {"tickett_id": "1"}})

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "unknown_filter_key"


async def test_unknown_item_field_is_not_an_unknown_parameter(client):
    """A name nested inside a batch item is refused, but not as a parameter.

    The operation's parameters are the top-level names; an item's fields are
    not among them, so naming them as the valid parameter set would be false.
    The item's own field set is what the caller needs, and the refusal carries
    it under a code of its own so the two answers stay distinguishable.
    """
    from sage.models.schemas import BulkLifecycleItem

    resp = await client.post(
        f"{VS}/lifecycles",
        json={"items": [{"document_id": DOC_ID, "action": "archive", BOGUS_FIELD: 1}]},
    )

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "undeclared_key"
    assert body["detail"]["parameter"] == "items.0"
    assert body["detail"]["key"] == BOGUS_FIELD
    assert body["detail"]["recognized"] == sorted(schemas.canonical_fields(BulkLifecycleItem))
    assert body["detail"]["aliases"] == {"doc_id": "document_id"}


async def test_open_mapping_fields_accept_any_key(client):
    """A field typed as an open mapping keeps accepting keys it does not name.

    Each request is asserted to get past validation entirely, not merely to
    avoid ``unknown_parameter``: a mapping wrongly closed to extra keys would
    refuse them as a nested ``invalid_parameter``, which a weaker assertion
    would let through. The ingest names a source that does not exist, so
    reaching that lookup is the evidence the body was accepted.
    """
    ingest = await client.post(
        f"{VS}/documents",
        json={
            "source": "absent/nothing.md",
            "source_type": "markdown",
            "metadata": {"title": "t", "an_undeclared_key": "x"},
        },
    )
    config = await client.put(
        f"{VS}/config",
        json={"abstraction": {"enabled": False, "an_undeclared_key": 1}, "dry_run": True},
    )

    assert ingest.status_code == 404, ingest.text
    assert _code(ingest) == "source_file_not_found", ingest.text
    assert config.status_code == 200, config.text


# Operations that declare no request body: (operationId, method, path).
BODYLESS_ROWS: list[tuple[str, str, str]] = [
    ("open_document", "POST", f"{VS}/documents/{DOC_ID}/open"),
    ("recompute_abstract", "POST", f"{VS}/documents/{DOC_ID}/reabstract"),
    ("recompute_pipeline", "POST", f"{VS}/documents/{DOC_ID}/recompute-pipeline"),
    ("reload_vault", "POST", f"{VS}/maintenance/reload"),
    ("recompute_views", "POST", f"{VS}/refresh-views"),
]


@pytest.mark.parametrize(
    ("tool", "method", "path"), BODYLESS_ROWS, ids=[r[0] for r in BODYLESS_ROWS]
)
async def test_json_field_on_bodyless_operation_refused(client, tool, method, path):
    """A JSON field sent to an operation that declares no body is refused.

    Such an operation's parameters are its path and query, so a field in a
    JSON body names nothing it accepts, and the valid body names are none.
    """
    resp = await client.request(method, path, json={BOGUS_FIELD: 1})

    _assert_unknown_parameter(resp, tool=tool, rejected=[BOGUS_FIELD], valid=[])


@pytest.mark.parametrize(
    ("tool", "method", "path"), BODYLESS_ROWS, ids=[r[0] for r in BODYLESS_ROWS]
)
async def test_empty_object_body_on_bodyless_operation_accepted(client, tool, method, path):
    """An empty JSON object, as the application sends, names no field."""
    resp = await client.request(method, path, json={})

    assert _code(resp) != "unknown_parameter", resp.text


async def test_raw_upload_body_is_not_read_as_parameters(client):
    """A body the published contract declares as raw bytes is content, not fields.

    The upload operation binds no body parameter -- it streams the request --
    but its published request body is a byte stream, so bytes labelled and
    shaped as a JSON object are still not parameters. The label is JSON on
    purpose: it is the published declaration, not the content type, that
    keeps this request from being refused. What is asserted is the absence of
    the refusal; that the stream is also left unread follows from the
    exemption returning first, and is not observed here.
    """
    resp = await client.put(
        "/upload",
        content=b'{"bogus_field_x": 1}',
        headers={"Content-Type": "application/json"},
    )

    assert _code(resp) != "unknown_parameter", resp.text


async def test_query_refusal_reported_before_body_refusal(client):
    """A request with both kinds of unknown name reports the query parameter."""
    resp = await client.post(
        f"{VS}/traverse",
        params={BOGUS_QUERY: "1"},
        json={"start_id": DOC_ID, BOGUS_FIELD: 1},
    )

    _assert_unknown_parameter(resp, tool="traverse", rejected=[BOGUS_QUERY], valid=[])


async def test_several_unknown_fields_named_in_sorted_order(client):
    """Every unknown field is named, in sorted order rather than the order sent."""
    resp = await client.post(f"{VS}/traverse", json={"start_id": DOC_ID, "zz": 1, "aa": 2})

    _assert_unknown_parameter(
        resp, tool="traverse", rejected=["aa", "zz"], valid=_fields(schemas.TraverseRequest)
    )
