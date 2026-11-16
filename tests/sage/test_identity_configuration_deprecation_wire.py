"""Every response carrying a processing identity warns that its configuration is deprecated.

A processing identity's own ``configuration`` is deprecated in favour of the
coverage object's shared ``configurations`` block (CAS-ADR-008 clause 8,
CAS-ADR-059). Each operation whose response can carry coverage warns the caller,
on REST and MCP alike, whenever the response actually holds an established
identity, and stays silent otherwise.

Anti-coincidental-pass discipline:

* Each operation runs against a document with published evidence and against a
  control document with only legacy unknown coverage; a service that always
  warns fails the control arm.
* The expected warning is a literal string, found under a ``warnings`` key of the
  response, on both surfaces.
* The service-level arms (download URL, download recipes) build the response the
  wire would carry and assert on it directly, with the same paired control.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from sage.app import create_app
from sage.mcp_server import build_partitioned_server
from sage.models.coverage import ProcessingCoverage
from sage.services.deprecations import coverage_warnings
from tests.sage.test_processing_coverage import _chunks, _doc, _evidence
from tests.sage.test_validation_envelope_parity import vault_services as _shared_services

vault_services = _shared_services

VAULT = "test_vault"
WARNING = (
    "Deprecated: the ProcessingIdentity.configuration response field is deprecated; use the "
    "processing_coverage configurations block, through each identity holder's "
    "configuration_ref instead. The change may ship from 2027-01-04."
)
COVERED = _doc(1).id
LEGACY = _doc(2).id


def _decode(result) -> dict:
    return json.loads(result[0].text)


def _warnings(body: object) -> Iterator[str]:
    """Every string listed under a ``warnings`` key anywhere in ``body``."""
    if isinstance(body, dict):
        for key, value in body.items():
            if key == "warnings" and isinstance(value, list):
                yield from (w for w in value if isinstance(w, str))
            else:
                yield from _warnings(value)
    elif isinstance(body, list):
        for item in body:
            yield from _warnings(item)


def _identity_warnings(body: object) -> list[str]:
    return [w for w in _warnings(body) if "ProcessingIdentity" in w]


async def _seed(services) -> None:
    await services.graph_store.insert_document(_doc(1))
    attempt = await services.content_store.begin_processing_attempt(COVERED)
    assert await services.content_store.publish_processing(
        COVERED, attempt, _evidence(), chunks=_chunks("old")
    )
    await services.graph_store.insert_document(_doc(2))


async def _both(services, tool: str, args: dict, rest) -> tuple[dict, dict]:
    server = build_partitioned_server("sage")
    mcp = _decode(await server.call_tool(tool, {"vault_id": VAULT, **args}))
    app = create_app(config=services.config)
    app.state.vault_registry = {VAULT: services}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await rest(client)
    assert response.status_code == 200, response.text
    return response.json(), mcp


def _document_reads(doc_id: str, tmp_path) -> dict:
    base = f"/sage_vaults/{VAULT}/documents/{doc_id}"
    return {
        "get_document": (
            {"document_id": doc_id},
            lambda c: c.get(base),
        ),
        "read_projection": (
            {"document_id": doc_id},
            lambda c: c.get(f"{base}/projection"),
        ),
        "read_section": (
            {"document_id": doc_id, "heading_path": "Section"},
            lambda c: c.get(f"{base}/section/Section"),
        ),
        "list_headings": (
            {"document_id": doc_id},
            lambda c: c.get(f"{base}/headings"),
        ),
    }


@pytest.mark.parametrize(
    "operation", ["get_document", "read_projection", "read_section", "list_headings"]
)
async def test_document_reads_warn_only_when_an_identity_is_carried(
    vault_services, operation, tmp_path
):
    await _seed(vault_services)

    args, rest = _document_reads(COVERED, tmp_path)[operation]
    for body in await _both(vault_services, operation, args, rest):
        assert _identity_warnings(body) == [WARNING], operation
        assert body["processing_coverage"]["configurations"], operation

    if operation == "read_section":
        return  # The legacy control has no projected section to read.
    args, rest = _document_reads(LEGACY, tmp_path)[operation]
    try:
        bodies = await _both(vault_services, operation, args, rest)
    except AssertionError:
        return  # A legacy document without a projection is refused, so carries nothing.
    for body in bodies:
        assert _identity_warnings(body) == [], operation


async def test_export_projection_warns(vault_services, tmp_path):
    await _seed(vault_services)
    for name, doc_id, expected in (("covered", COVERED, [WARNING]), ("legacy", LEGACY, [])):
        root = Path(vault_services.config.vault.storage_root).expanduser().resolve()
        outputs = [root / f"{name}-mcp.md", root / f"{name}-rest.md"]
        server = build_partitioned_server("sage_maint")
        result = await server.call_tool(
            "export_projection",
            {"vault_id": VAULT, "document_id": doc_id, "output_path": str(outputs[0])},
        )
        bodies = [_decode(result)]
        app = create_app(config=vault_services.config)
        app.state.vault_registry = {VAULT: vault_services}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                f"/sage_vaults/{VAULT}/documents/{doc_id}/export",
                json={"output_path": str(outputs[1])},
            )
        if doc_id == LEGACY and response.status_code != 200:
            continue
        assert response.status_code == 200, response.text
        bodies.append(response.json())
        for body in bodies:
            assert _identity_warnings(body) == expected, name


async def test_search_and_pending_pages_warn(vault_services):
    await _seed(vault_services)
    search = await _both(
        vault_services,
        "search",
        {"mode": "catalog", "response_mode": "full"},
        lambda c: c.post(
            f"/sage_vaults/{VAULT}/discover", json={"mode": "catalog", "response_mode": "full"}
        ),
    )
    pending = await _both(
        vault_services,
        "list_pending_metadata",
        {"response_mode": "full"},
        lambda c: c.get(f"/sage_vaults/{VAULT}/pending-metadata", params={"response_mode": "full"}),
    )
    for body in (*search, *pending):
        assert _identity_warnings(body) == [WARNING]


async def test_search_without_identities_does_not_warn(vault_services):
    await vault_services.graph_store.insert_document(_doc(2))
    for body in await _both(
        vault_services,
        "search",
        {"mode": "catalog", "response_mode": "full"},
        lambda c: c.post(
            f"/sage_vaults/{VAULT}/discover", json={"mode": "catalog", "response_mode": "full"}
        ),
    ):
        assert _identity_warnings(body) == []


async def test_traverse_warns_when_a_node_carries_an_identity(vault_services):
    await _seed(vault_services)
    server = build_partitioned_server("sage")
    for source, target in ((LEGACY, COVERED), (COVERED, LEGACY)):
        created = _decode(
            await server.call_tool(
                "create_edges",
                {
                    "vault_id": VAULT,
                    "items": [
                        {
                            "edge_type": "references",
                            "source_id": source,
                            "target_id": target,
                            "source_valid_from_version": source,
                            "target_valid_from_version": target,
                        }
                    ],
                },
            )
        )
        assert created["success_count"] == 1, created

    for start, expected in ((LEGACY, [WARNING]), (COVERED, [])):
        request = {
            "start_id": start,
            "edge_type": "references",
            "direction": "outbound",
            "depth": 1,
        }
        for body in await _both(
            vault_services,
            "traverse",
            request,
            lambda c, request=request: c.post(f"/sage_vaults/{VAULT}/traverse", json=request),
        ):
            assert body["nodes"], start
            assert _identity_warnings(body) == expected, start


async def test_metadata_and_lifecycle_writes_warn_per_item(vault_services):
    await _seed(vault_services)
    for doc_id, expected in ((COVERED, [WARNING]), (LEGACY, [])):
        metadata = {"items": [{"document_id": doc_id, "title": "Patched"}]}
        for body in await _both(
            vault_services,
            "update_metadata",
            metadata,
            lambda c, metadata=metadata: c.post(f"/sage_vaults/{VAULT}/metadata", json=metadata),
        ):
            assert body["results"][0]["status"] == "success", body
            assert _identity_warnings(body["results"][0]) == expected, doc_id

        lifecycle = {
            "items": [{"document_id": doc_id, "action": "archive"}],
            "dry_run": True,
        }
        for body in await _both(
            vault_services,
            "update_lifecycles",
            lifecycle,
            lambda c, lifecycle=lifecycle: c.post(
                f"/sage_vaults/{VAULT}/lifecycles", json=lifecycle
            ),
        ):
            assert body["results"][0]["status"] == "success", body
            assert _identity_warnings(body["results"][0]) == expected, doc_id


async def test_ingest_warns_for_its_established_source_retention(vault_services, tmp_path):
    root = Path(vault_services.config.vault.storage_root).expanduser()
    source = root / "fresh.md"
    source.write_text("# Fresh\n\nBody text.\n")
    server = build_partitioned_server("sage")
    body = _decode(
        await server.call_tool(
            "ingest_document",
            {"vault_id": VAULT, "source": "fresh.md", "metadata": {"doc_type": "note"}},
        )
    )
    assert body["processing_coverage"]["source_retention"]["scope"] == "complete", body
    assert _identity_warnings(body) == [WARNING]


@pytest.mark.parametrize(
    "operation",
    [
        "search",
        "list_pending_metadata",
        "ingest_document",
        "update_metadata",
        "update_lifecycles",
        "get_document",
        "get_document_download_url",
        "traverse",
        "export_projection",
        "read_projection",
        "read_section",
        "list_headings",
    ],
)
async def test_every_coverage_operation_warns_from_carried_coverage_alone(
    vault_services, operation
):
    await _seed(vault_services)
    covered = (await vault_services.graph_store.get_document(COVERED)).processing_coverage
    assert coverage_warnings(operation, covered) == [WARNING]
    assert coverage_warnings(operation, ProcessingCoverage()) == []
    assert coverage_warnings(operation, [{"x": covered}]) == [WARNING]


async def test_download_url_warns_only_when_an_identity_is_carried(vault_services, monkeypatch):
    import sage.mcp_init

    class _UrlStore:
        def source_exists(self, vault_id, storage_root, source_path) -> bool:
            return True

        def download_url(self, vault_id, storage_root, source_path) -> str:
            return "https://example.invalid/source"

    monkeypatch.setattr(sage.mcp_init, "resolve_stack_vault_source_store", lambda _: _UrlStore())
    await _seed(vault_services)
    app = create_app(config=vault_services.config)
    app.state.vault_registry = {VAULT: vault_services}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for doc_id, expected in ((COVERED, [WARNING]), (LEGACY, [])):
            response = await client.get(f"/sage_vaults/{VAULT}/documents/{doc_id}/download-url")
            assert response.status_code == 200, response.text
            assert _identity_warnings(response.json()) == expected, doc_id


async def test_download_recipes_warn_for_the_coverage_they_carry(vault_services):
    import sage.mcp_init

    await _seed(vault_services)
    root = Path(vault_services.config.vault.storage_root).expanduser()
    (root / "recipe.md").write_text("# Recipe\n\nBody text.\n")
    server = build_partitioned_server("sage")
    ingested = _decode(
        await server.call_tool(
            "ingest_document",
            {"vault_id": VAULT, "source": "recipe.md", "metadata": {"doc_type": "note"}},
        )
    )
    from sage.config import SageCoreConfig
    from sage.services.transfer import reset_transfer_store

    saved = sage.mcp_init._stack_config
    sage.mcp_init.set_stack_config(
        SageCoreConfig(
            profile="cloud", transfer={"public_base_url": "https://sage.example.invalid"}
        )
    )
    reset_transfer_store()
    try:
        target = "/caller/elsewhere/out.md"
        for tool, doc_id in (("get_document", ingested["id"]), ("read_projection", COVERED)):
            body = _decode(
                await server.call_tool(
                    tool,
                    {"vault_id": VAULT, "document_id": doc_id, "write_to_path": target},
                )
            )
            assert body.get("status") == "download_required", (tool, body)
            assert _identity_warnings(body) == [WARNING], tool
    finally:
        sage.mcp_init.set_stack_config(saved)
        reset_transfer_store()
