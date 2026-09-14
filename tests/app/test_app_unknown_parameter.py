"""The CAS Application API refuses a request name the operation does not declare.

A JSON body field or query parameter ``/app/scan`` or ``/app/ingest`` does not
declare is refused with the ``unknown_parameter`` envelope the Core API and the
MCP surface return (CAS-ADR-037, CAS-ADR-052), rather than being discarded
without a word.

Each refusal row is paired with the identical request minus the unknown name,
and the pair is what makes the refusal meaningful: the control shows the
operation itself ran, so the unknown name is what the refusal is about.

Three boundaries are held alongside the refusal:

* A name nested inside a declared field -- a file entry's field, a parsed
  metadata key -- is a malformed value of that field and keeps
  ``invalid_parameter``.
* What the single-page app sends is still accepted, including the parsed
  metadata a scan returns, which the app posts back into ingest unchanged.
* The sign-in routes under ``/app/auth`` are untouched: an identity provider's
  callback carries query parameters no operation declares.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.backend.asgi import create_bff_app
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.app import _initialize_services, create_app
from sage.config import VaultConfig
from tests.app.test_app_backend import _make_vault_config_dict

VAULT_ID = "example_vault"
BOGUS_FIELD = "bogus_field_x"
BOGUS_QUERY = "bogus_q"
# The directory the single-page app probes to detect the deployment profile
# (``PROFILE_PROBE_PATH`` in ``app/src/api/ingest.ts``).
PROFILE_PROBE_PATH = "/__cas_ingest_profile_probe__"

SCAN_FIELDS = ["directory", "max_depth", "vault_id"]
INGEST_FIELDS = ["dry_run", "files", "infer_edges", "vault_id"]


@pytest.fixture
async def app_and_config(tmp_path: Path) -> AsyncIterator[tuple[FastAPI, VaultConfig]]:
    """The co-located app with one stub-backed vault."""
    config = VaultConfig.model_validate(
        _make_vault_config_dict(tmp_path, VAULT_ID, "Example Portfolio")
    )
    app = create_app(config=config)
    await _initialize_services(
        app,
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    )

    yield app, config

    await asyncio.sleep(0.3)
    for services in app.state.vault_registry.values():
        services.close_timing()
        await services.graph_store.close()


@pytest.fixture
async def client(app_and_config: tuple[FastAPI, VaultConfig]) -> AsyncIterator[AsyncClient]:
    app, _ = app_and_config
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def storage_root(app_and_config: tuple[FastAPI, VaultConfig]) -> Path:
    _, config = app_and_config
    return Path(config.vault.storage_root)


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


def _assert_code(resp: Any, status: int, code: str) -> None:
    assert resp.status_code == status, resp.text
    assert resp.json()["code"] == code


def _summary(resp: Any) -> dict:
    """The summary event of a completed ingest stream."""
    assert resp.status_code == 200, resp.text
    assert "text/event-stream" in resp.headers.get("content-type", "")
    events = [
        json.loads(line.removeprefix("data: "))
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]
    summaries = [e for e in events if e.get("event_type") == "summary"]
    assert len(summaries) == 1, events
    return summaries[0]


def _assert_one_file_ingested(resp: Any) -> None:
    summary = _summary(resp)
    assert summary["error_count"] == 0, summary
    assert summary["documents_created"]["new"] == 1, summary


def _scan_body(**extra: Any) -> dict:
    return {"vault_id": VAULT_ID, "directory": "/nonexistent/path", **extra}


def _ingest_body(**extra: Any) -> dict:
    return {"vault_id": VAULT_ID, "files": [], **extra}


# ---------------------------------------------------------------------------
# Body fields
# ---------------------------------------------------------------------------


async def test_scan_refuses_an_undeclared_body_field(client):
    refused = await client.post("/app/scan", json=_scan_body(**{BOGUS_FIELD: 1}))
    control = await client.post("/app/scan", json=_scan_body())

    _assert_unknown_parameter(
        refused, tool="list_directory", rejected=[BOGUS_FIELD], valid=SCAN_FIELDS
    )
    _assert_code(control, 400, "invalid_directory")


async def test_ingest_refuses_an_undeclared_body_field(client):
    refused = await client.post("/app/ingest", json=_ingest_body(**{BOGUS_FIELD: 1}))
    control = await client.post("/app/ingest", json=_ingest_body())

    _assert_unknown_parameter(
        refused, tool="bulk_ingest_document", rejected=[BOGUS_FIELD], valid=INGEST_FIELDS
    )
    _assert_code(control, 400, "empty_file_list")


# ---------------------------------------------------------------------------
# Query parameters
# ---------------------------------------------------------------------------


async def test_scan_refuses_an_undeclared_query_parameter(client):
    refused = await client.post("/app/scan", params={BOGUS_QUERY: "1"}, json=_scan_body())
    control = await client.post("/app/scan", json=_scan_body())

    _assert_unknown_parameter(refused, tool="list_directory", rejected=[BOGUS_QUERY], valid=[])
    _assert_code(control, 400, "invalid_directory")


async def test_ingest_refuses_an_undeclared_query_parameter(client):
    refused = await client.post("/app/ingest", params={BOGUS_QUERY: "1"}, json=_ingest_body())
    control = await client.post("/app/ingest", json=_ingest_body())

    _assert_unknown_parameter(
        refused, tool="bulk_ingest_document", rejected=[BOGUS_QUERY], valid=[]
    )
    _assert_code(control, 400, "empty_file_list")


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


async def test_undeclared_query_parameter_is_reported_before_a_body_field(client):
    resp = await client.post(
        "/app/scan", params={BOGUS_QUERY: "1"}, json=_scan_body(**{BOGUS_FIELD: 1})
    )

    _assert_unknown_parameter(resp, tool="list_directory", rejected=[BOGUS_QUERY], valid=[])


async def test_undeclared_body_field_wins_over_a_missing_field(client):
    resp = await client.post("/app/scan", json={"vault_id": VAULT_ID, BOGUS_FIELD: 1})

    _assert_unknown_parameter(
        resp, tool="list_directory", rejected=[BOGUS_FIELD], valid=SCAN_FIELDS
    )


# ---------------------------------------------------------------------------
# Nested fields keep their code
# ---------------------------------------------------------------------------


def _file_entry(path: Path, **extra: Any) -> dict:
    return {"file_path": str(path), "source_type": "markdown", **extra}


async def test_undeclared_file_entry_field_is_a_nested_invalid_parameter(client, storage_root):
    doc = storage_root / "nested_entry.md"
    doc.write_text("# Nested Entry\n\nContent.")

    control = await client.post("/app/ingest", json=_ingest_body(files=[_file_entry(doc)]))
    refused = await client.post(
        "/app/ingest", json=_ingest_body(files=[_file_entry(doc, **{BOGUS_FIELD: 1})])
    )

    _assert_code(refused, 422, "invalid_parameter")
    assert refused.json()["detail"]["parameter"] == f"files.0.{BOGUS_FIELD}"
    _assert_one_file_ingested(control)


async def test_undeclared_parsed_metadata_key_is_a_nested_invalid_parameter(client, storage_root):
    doc = storage_root / "nested_metadata.md"
    doc.write_text("# Nested Metadata\n\nContent.")
    metadata = {"title": "Nested Metadata"}

    control = await client.post(
        "/app/ingest", json=_ingest_body(files=[_file_entry(doc, parsed_metadata=metadata)])
    )
    refused = await client.post(
        "/app/ingest",
        json=_ingest_body(files=[_file_entry(doc, parsed_metadata={**metadata, BOGUS_FIELD: 1})]),
    )

    _assert_code(refused, 422, "invalid_parameter")
    assert refused.json()["detail"]["parameter"] == f"files.0.parsed_metadata.{BOGUS_FIELD}"
    _assert_one_file_ingested(control)


# ---------------------------------------------------------------------------
# What the single-page app sends
# ---------------------------------------------------------------------------


async def test_scan_result_round_trips_into_ingest(client, tmp_path):
    """The app posts a scan's parsed metadata back into ingest unchanged.

    The payload is built the way the Ingest view builds it, so a scan result
    carrying any field the ingest request refuses fails here.
    """
    scan_dir = tmp_path / "round_trip"
    scan_dir.mkdir()
    (scan_dir / "2026-03-09_EXAMPLE_PV06_Claim-Set_v7.md").write_text("# Claim Set v7")

    scan = await client.post(
        "/app/scan", json={"vault_id": VAULT_ID, "directory": str(scan_dir), "max_depth": None}
    )
    assert scan.status_code == 200, scan.text
    scanned = [f for f in scan.json()["files"] if f["source_type"] is not None]
    assert len(scanned) == 1, scan.json()

    ingest = await client.post(
        "/app/ingest",
        json={
            "vault_id": VAULT_ID,
            "files": [
                {
                    "file_path": f["file_path"],
                    "source_type": f["source_type"],
                    "parsed_metadata": f["parsed_metadata"],
                }
                for f in scanned
            ],
            "infer_edges": False,
        },
    )

    _assert_one_file_ingested(ingest)


async def test_profile_probe_is_not_refused(client):
    resp = await client.post(
        "/app/scan",
        json={"vault_id": VAULT_ID, "directory": PROFILE_PROBE_PATH, "max_depth": None},
    )

    _assert_code(resp, 400, "invalid_directory")


# ---------------------------------------------------------------------------
# Sign-in routes are untouched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "params"),
    [
        ("GET", "/app/auth/callback", {"code": "x", "state": "y", "session_state": "z"}),
        ("GET", "/app/auth/me", {}),
        ("POST", "/app/auth/logout", {}),
    ],
)
async def test_sign_in_routes_accept_undeclared_query_parameters(client, method, path, params):
    with_extra = await client.request(method, path, params={**params, BOGUS_QUERY: "1"})
    without = await client.request(method, path, params=params)

    assert with_extra.status_code == without.status_code, (with_extra.text, without.text)
    assert with_extra.content == without.content


# ---------------------------------------------------------------------------
# Standalone backend-for-frontend
# ---------------------------------------------------------------------------


_SCAN_PROBE = {"vault_id": VAULT_ID, "directory": PROFILE_PROBE_PATH, "max_depth": None}
_INGEST_DECLARED = {"vault_id": VAULT_ID, "files": []}


@pytest.mark.parametrize(
    ("path", "tool", "body", "params", "extra", "rejected", "valid"),
    [
        (
            "/app/scan",
            "list_directory",
            _SCAN_PROBE,
            {},
            {BOGUS_FIELD: 1},
            [BOGUS_FIELD],
            SCAN_FIELDS,
        ),
        ("/app/scan", "list_directory", _SCAN_PROBE, {BOGUS_QUERY: "1"}, {}, [BOGUS_QUERY], []),
        (
            "/app/ingest",
            "bulk_ingest_document",
            _INGEST_DECLARED,
            {},
            {BOGUS_FIELD: 1},
            [BOGUS_FIELD],
            INGEST_FIELDS,
        ),
    ],
    ids=["scan_body_field", "scan_query_parameter", "ingest_body_field"],
)
async def test_standalone_app_refuses_undeclared_names(
    path, tool, body, params, extra, rejected, valid
):
    """The hosted profile serves the same router, so it refuses the same names.

    The query parameter row is what shows the refusal travels with the router
    rather than with one application's inclusion of it; a body field is refused
    by the request model wherever the router is mounted. The refusal is
    reported ahead of the profile boundary, and the declared request still
    reaches that boundary, which is the signal the single-page app detects the
    hosted profile by. The refusal names the published operation id here too,
    though this application serves no specification-overlaid document.
    """
    app = create_bff_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://bff.test") as c:
        refused = await c.post(path, params=params, json={**body, **extra})
        control = await c.post(path, json=body)

    _assert_unknown_parameter(refused, tool=tool, rejected=rejected, valid=valid)
    _assert_code(control, 501, "local_profile_only")
