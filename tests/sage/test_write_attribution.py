"""Write attribution follows the authenticated principal (CAS-ADR-042).

Under a profile that authenticates callers, ``created_by`` on a new document
and ``last_modified_by`` on every created, patched, superseded or
lifecycle-transitioned document record the principal whose credential made the
write -- on the REST routes and the MCP tools alike. Without authentication a
caller-supplied ``created_by`` is used and the vault owner is the default, and
lifecycle transitions stamp the vault owner.

Anti-coincidental-pass discipline:

* The vault owner, the caller-supplied values and every principal's identity
  are pairwise distinct, so neither an owner fallback nor caller pass-through
  can satisfy an assertion meant for the principal.
* Every update test creates the document as one writer and modifies it as
  another, so a write that stamps nothing leaves the first writer's value in
  place and fails.
* ``bob`` carries no ``preferred_username``: the ``oid`` fallback is exercised
  end to end, and a derivation from ``sub`` fails.
* The multi-principal tests alternate principals request by request, so an
  identity captured per application, per vault or per MCP session fails.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.types import Receive, Scope, Send

from sage.adapters.stubs import StubAbstractionProvider, StubContentStore, StubEmbeddingProvider
from sage.app import _initialize_services, create_app
from sage.auth import AuthenticatedPrincipal, AuthError, AuthMiddleware, NoAuthValidator
from sage.config import SageCoreConfig, StackAuthConfig, VaultConfig
from sage.request_identity import current_actor, principal_actor
from tests.helpers.pipeline_wait import drain_vaults

_VAULT = "test_vault"
_OWNER = "owner-o"
_ALICE = "alice@example.org"
_BOB = "oid-b"
_SVC = "app:client-1"

_PRINCIPALS = {
    "alice-tok": AuthenticatedPrincipal(
        subject="sub-a",
        scopes=frozenset({"Sage.Access"}),
        claims={"scp": "Sage.Access", "preferred_username": _ALICE, "oid": "oid-a", "sub": "sub-a"},
    ),
    "bob-tok": AuthenticatedPrincipal(
        subject="sub-b",
        scopes=frozenset({"Sage.Access"}),
        claims={"scp": "Sage.Access", "oid": _BOB, "sub": "sub-b"},
    ),
    "svc-tok": AuthenticatedPrincipal(
        subject="sub-s",
        roles=frozenset({"Sage.Access"}),
        claims={"roles": ["Sage.Access"], "azp": "client-1", "oid": "oid-s", "sub": "sub-s"},
    ),
}
_ENABLED = SageCoreConfig(
    auth=StackAuthConfig(enabled=True, tenant_id="tid", audience="api://sage")
)


class _StubValidator:
    async def validate(self, token: str | None) -> AuthenticatedPrincipal:
        if token in _PRINCIPALS:
            return _PRINCIPALS[token]
        raise AuthError(401, "invalid_token", "bad or missing token")


def _install_stub(monkeypatch) -> None:
    def fake(auth_config):
        if auth_config is None or not auth_config.enabled:
            return NoAuthValidator()
        return _StubValidator()

    monkeypatch.setattr("sage.mcp_init.build_auth_validator", fake)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def owned_config(minimal_vault_config_dict) -> VaultConfig:
    config = dict(minimal_vault_config_dict)
    config["vault"] = {**config["vault"], "owner": _OWNER}
    return VaultConfig.model_validate(config)


@asynccontextmanager
async def _app(config: VaultConfig, stack_config: SageCoreConfig | None) -> AsyncIterator[object]:
    app = create_app(config=config, stack_config=stack_config)
    await _initialize_services(
        app,
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    )
    try:
        yield app
    finally:
        try:
            await drain_vaults(app.state.vault_registry, [_VAULT])
        finally:
            registry = app.state.vault_registry
            if _VAULT in registry:
                services = registry.pop(_VAULT)
                services.close_timing()
                await services.close_storage()


@pytest.fixture
async def auth_app(owned_config, monkeypatch) -> AsyncIterator[object]:
    _install_stub(monkeypatch)
    async with _app(owned_config, _ENABLED) as app:
        yield app


@pytest.fixture
async def noauth_app(owned_config) -> AsyncIterator[object]:
    async with _app(owned_config, None) as app:
        yield app


@asynccontextmanager
async def _mcp_running(app) -> AsyncIterator[None]:
    """Run the MCP session managers for the body of one test.

    Entered in the test's own task: a session manager's task group must be
    exited in the task that entered it, which an async fixture's teardown is
    not guaranteed to be.
    """
    async with AsyncExitStack() as stack:
        for server in app.state.mcp_mounts.values():
            await stack.enter_async_context(server.session_manager.run())
        yield


def _client(app, token: str | None = None) -> AsyncClient:
    headers = {"Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=headers, timeout=30.0
    )


def _seed(tmp_vault_dir: Path, name: str, body: str | None = None) -> str:
    path = tmp_vault_dir / "sources" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body or f"# {name}\n\nBody of {name}.\n")
    return name


async def _rest_ingest(app, token: str | None, source: str, **extra) -> dict:
    async with _client(app, token) as c:
        resp = await c.post(
            f"/sage_vaults/{_VAULT}/documents",
            json={"source": source, "source_type": "markdown", **extra},
        )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def _rest_get(app, token: str | None, doc_id: str) -> dict:
    async with _client(app, token) as c:
        resp = await c.get(f"/sage_vaults/{_VAULT}/documents/{doc_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _rest_post(app, token: str | None, path: str, body: dict) -> dict:
    async with _client(app, token) as c:
        resp = await c.post(f"/sage_vaults/{_VAULT}{path}", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _decode_rpc(resp) -> dict:
    assert resp.status_code == 200, resp.text
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        data = [line[5:].strip() for line in resp.text.splitlines() if line.startswith("data:")]
        envelope = json.loads(data[-1])
    else:
        envelope = resp.json()
    assert "error" not in envelope, envelope
    return json.loads(envelope["result"]["content"][0]["text"])


async def _mcp_call(app, token: str | None, tool: str, arguments: dict) -> dict:
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    async with _client(app, token) as c:
        resp = await c.post("/mcp", json=request)
    return _decode_rpc(resp)


# --------------------------------------------------------------------------
# Unit: identity derivation and the request-scoped binding
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ({"scp": "x", "preferred_username": "u@x", "oid": "o", "sub": "s"}, "u@x"),
        ({"scp": "x", "oid": "o", "sub": "s"}, "o"),
        ({"scp": "x", "sub": "s"}, "s"),
        ({"roles": ["r"], "azp": "client-1", "oid": "o", "sub": "s"}, "app:client-1"),
        ({"roles": ["r"], "appid": "client-v1", "oid": "o", "sub": "s"}, "app:client-v1"),
    ],
    ids=["delegated-upn", "delegated-oid", "delegated-sub", "app-azp", "app-v1-appid"],
)
def test_u1_principal_actor_derivation(claims: dict, expected: str) -> None:
    principal = AuthenticatedPrincipal(subject=claims.get("sub"), claims=claims)
    assert principal_actor(principal) == expected


def test_u1_anonymous_principal_has_no_actor() -> None:
    assert principal_actor(AuthenticatedPrincipal(subject=None, anonymous=True)) is None


def test_u2_no_actor_outside_a_request() -> None:
    assert current_actor() is None


class _Recorder:
    def __init__(self) -> None:
        self.seen: list[str | None] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.seen.append(current_actor())
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})


async def test_m1_middleware_binds_the_principal_for_the_request_and_resets_it() -> None:
    inner = _Recorder()
    app = AuthMiddleware(inner, validator=_StubValidator())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.get("/x", headers={"Authorization": "Bearer alice-tok"})
        await c.get("/x", headers={"Authorization": "Bearer bob-tok"})
    assert inner.seen == [_ALICE, _BOB]
    assert current_actor() is None


async def test_m1_no_auth_binds_no_actor() -> None:
    inner = _Recorder()
    app = AuthMiddleware(inner, validator=NoAuthValidator())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.get("/x")
    assert inner.seen == [None]


# --------------------------------------------------------------------------
# REST, authenticating profile
# --------------------------------------------------------------------------


async def test_r1_ingest_records_the_principal(auth_app, tmp_vault_dir) -> None:
    body = await _rest_ingest(auth_app, "alice-tok", _seed(tmp_vault_dir, "r1.md"))
    doc = body["document"]
    assert doc["created_by"] == _ALICE
    assert doc["last_modified_by"] == _ALICE
    assert body["warnings"] == []


async def test_r2_differing_caller_created_by_is_ignored_with_a_warning(
    auth_app, tmp_vault_dir
) -> None:
    body = await _rest_ingest(
        auth_app, "alice-tok", _seed(tmp_vault_dir, "r2.md"), created_by="mallory"
    )
    assert body["document"]["created_by"] == _ALICE
    (warning,) = body["warnings"]
    assert "mallory" in warning
    assert _ALICE in warning


async def test_r3_a_second_principal_is_attributed_to_itself(auth_app, tmp_vault_dir) -> None:
    first = await _rest_ingest(auth_app, "alice-tok", _seed(tmp_vault_dir, "r3a.md"))
    second = await _rest_ingest(auth_app, "bob-tok", _seed(tmp_vault_dir, "r3b.md"))
    assert first["document"]["created_by"] == _ALICE
    assert second["document"]["created_by"] == _BOB
    assert second["document"]["last_modified_by"] == _BOB


async def test_r4_metadata_patch_stamps_the_patching_principal(auth_app, tmp_vault_dir) -> None:
    doc = (await _rest_ingest(auth_app, "alice-tok", _seed(tmp_vault_dir, "r4.md")))["document"]
    await _rest_post(
        auth_app, "bob-tok", "/metadata", {"items": [{"document_id": doc["id"], "title": "New"}]}
    )
    after = await _rest_get(auth_app, "bob-tok", doc["id"])
    assert after["last_modified_by"] == _BOB
    assert after["created_by"] == _ALICE


async def test_r5_lifecycle_transition_stamps_the_principal(auth_app, tmp_vault_dir) -> None:
    doc = (await _rest_ingest(auth_app, "alice-tok", _seed(tmp_vault_dir, "r5.md")))["document"]
    await _rest_post(
        auth_app,
        "bob-tok",
        "/lifecycles",
        {"items": [{"document_id": doc["id"], "action": "complete"}]},
    )
    after = await _rest_get(auth_app, "bob-tok", doc["id"])
    assert after["lifecycle_status"] == "completed"
    assert after["last_modified_by"] == _BOB


async def test_r6_superseding_ingest_stamps_successor_and_predecessor(
    auth_app, tmp_vault_dir
) -> None:
    pred = (await _rest_ingest(auth_app, "alice-tok", _seed(tmp_vault_dir, "r6a.md")))["document"]
    succ = (
        await _rest_ingest(
            auth_app, "bob-tok", _seed(tmp_vault_dir, "r6b.md"), predecessor_id=pred["id"]
        )
    )["document"]
    assert succ["created_by"] == _BOB
    after = await _rest_get(auth_app, "bob-tok", pred["id"])
    assert after["lifecycle_status"] == "archived"
    assert after["last_modified_by"] == _BOB


async def test_r7_supersede_action_stamps_the_predecessor(auth_app, tmp_vault_dir) -> None:
    pred = (await _rest_ingest(auth_app, "alice-tok", _seed(tmp_vault_dir, "r7a.md")))["document"]
    succ = (await _rest_ingest(auth_app, "alice-tok", _seed(tmp_vault_dir, "r7b.md")))["document"]
    await _rest_post(
        auth_app,
        "bob-tok",
        "/lifecycles",
        {"items": [{"document_id": pred["id"], "action": "supersede", "successor_id": succ["id"]}]},
    )
    after = await _rest_get(auth_app, "bob-tok", pred["id"])
    assert after["lifecycle_status"] == "archived"
    assert after["last_modified_by"] == _BOB


async def test_r8_batch_ingest_records_an_app_principal(auth_app) -> None:
    metadata = json.dumps(
        {
            "infer_edges": False,
            "files": [{"source_type": "markdown", "parsed_metadata": {"title": "Batch R8"}}],
        }
    )
    async with _client(auth_app, "svc-tok") as c:
        resp = await c.post(
            f"/sage_vaults/{_VAULT}/documents:batch",
            files=[("files", ("r8.md", b"# R8\n\nBatch body.\n", "text/markdown"))],
            data={"metadata": metadata},
        )
    assert resp.status_code == 200, resp.text
    docs = await auth_app.state.vault_registry[_VAULT].graph_store.list_all_documents()
    (doc,) = [d for d in docs if d.title == "Batch R8"]
    assert doc.created_by == _SVC
    assert doc.last_modified_by == _SVC


async def test_r9_force_reingest_stamps_the_principal(auth_app, tmp_vault_dir) -> None:
    source = _seed(tmp_vault_dir, "r9.md")
    doc = (await _rest_ingest(auth_app, "alice-tok", source))["document"]
    again = (await _rest_ingest(auth_app, "bob-tok", source, force=True))["document"]
    assert again["id"] == doc["id"]
    after = await _rest_get(auth_app, "bob-tok", doc["id"])
    assert after["last_modified_by"] == _BOB
    assert after["created_by"] == _ALICE


# --------------------------------------------------------------------------
# MCP over HTTP, authenticating profile
# --------------------------------------------------------------------------


async def test_p1_mcp_ingest_records_the_principal_and_warns_on_a_differing_caller(
    auth_app, tmp_vault_dir
) -> None:
    async with _mcp_running(auth_app):
        plain = await _mcp_call(
            auth_app,
            "alice-tok",
            "ingest_document",
            {
                "vault_id": _VAULT,
                "source": _seed(tmp_vault_dir, "p1a.md"),
                "source_type": "markdown",
            },
        )
        assert plain["created_by"] == _ALICE
        assert "warnings" not in plain

        claimed = await _mcp_call(
            auth_app,
            "alice-tok",
            "ingest_document",
            {
                "vault_id": _VAULT,
                "source": _seed(tmp_vault_dir, "p1b.md"),
                "source_type": "markdown",
                "created_by": "mallory",
            },
        )
        assert claimed["created_by"] == _ALICE
        (warning,) = claimed["warnings"]
        assert "mallory" in warning
        assert _ALICE in warning


async def test_p2_mcp_update_metadata_follows_each_requests_principal(
    auth_app, tmp_vault_dir
) -> None:
    async with _mcp_running(auth_app):
        doc = await _mcp_call(
            auth_app,
            "alice-tok",
            "ingest_document",
            {
                "vault_id": _VAULT,
                "source": _seed(tmp_vault_dir, "p2.md"),
                "source_type": "markdown",
            },
        )
        await _mcp_call(
            auth_app,
            "bob-tok",
            "update_metadata",
            {"vault_id": _VAULT, "items": [{"document_id": doc["id"], "title": "By Bob"}]},
        )
        after = await _rest_get(auth_app, "alice-tok", doc["id"])
        assert after["created_by"] == _ALICE
        assert after["last_modified_by"] == _BOB


async def test_p3_mcp_update_lifecycles_stamps_the_principal(auth_app, tmp_vault_dir) -> None:
    async with _mcp_running(auth_app):
        doc = await _mcp_call(
            auth_app,
            "alice-tok",
            "ingest_document",
            {
                "vault_id": _VAULT,
                "source": _seed(tmp_vault_dir, "p3.md"),
                "source_type": "markdown",
            },
        )
        await _mcp_call(
            auth_app,
            "bob-tok",
            "update_lifecycles",
            {"vault_id": _VAULT, "items": [{"document_id": doc["id"], "action": "complete"}]},
        )
        after = await _rest_get(auth_app, "bob-tok", doc["id"])
        assert after["last_modified_by"] == _BOB


async def test_p4_mcp_bulk_ingest_records_an_app_principal(auth_app, tmp_vault_dir) -> None:
    async with _mcp_running(auth_app):
        path = tmp_vault_dir / "sources" / "p4.md"
        path.write_text("# P4\n\nBulk body.\n")
        await _mcp_call(
            auth_app,
            "svc-tok",
            "bulk_ingest_document",
            {
                "vault_id": _VAULT,
                "files": [{"file_path": str(path), "source_type": "markdown"}],
                "infer_edges": False,
            },
        )
        docs = await auth_app.state.vault_registry[_VAULT].graph_store.list_all_documents()
        (doc,) = [d for d in docs if d.source_path.endswith("p4.md")]
        assert doc.created_by == _SVC


# --------------------------------------------------------------------------
# No-auth profile
# --------------------------------------------------------------------------


async def test_n1_no_auth_uses_the_caller_value_or_the_owner(noauth_app, tmp_vault_dir) -> None:
    claimed = await _rest_ingest(
        noauth_app, None, _seed(tmp_vault_dir, "n1a.md"), created_by="carol"
    )
    assert claimed["document"]["created_by"] == "carol"
    assert claimed["warnings"] == []
    unclaimed = await _rest_ingest(noauth_app, None, _seed(tmp_vault_dir, "n1b.md"))
    assert unclaimed["document"]["created_by"] == _OWNER


async def test_n2_no_auth_metadata_patch_stamps_the_owner_on_both_surfaces(
    noauth_app, tmp_vault_dir
) -> None:
    async with _mcp_running(noauth_app):
        via_mcp = (
            await _rest_ingest(noauth_app, None, _seed(tmp_vault_dir, "n2a.md"), created_by="carol")
        )["document"]
        via_rest = (
            await _rest_ingest(noauth_app, None, _seed(tmp_vault_dir, "n2b.md"), created_by="carol")
        )["document"]
        await _mcp_call(
            noauth_app,
            None,
            "update_metadata",
            {"vault_id": _VAULT, "items": [{"document_id": via_mcp["id"], "title": "M"}]},
        )
        await _rest_post(
            noauth_app,
            None,
            "/metadata",
            {"items": [{"document_id": via_rest["id"], "title": "R"}]},
        )
        assert (await _rest_get(noauth_app, None, via_mcp["id"]))["last_modified_by"] == _OWNER
        assert (await _rest_get(noauth_app, None, via_rest["id"]))["last_modified_by"] == _OWNER


async def test_n3_no_auth_lifecycle_transition_stamps_the_owner(noauth_app, tmp_vault_dir) -> None:
    doc = (await _rest_ingest(noauth_app, None, _seed(tmp_vault_dir, "n3.md"), created_by="carol"))[
        "document"
    ]
    assert doc["last_modified_by"] == "carol"
    await _rest_post(
        noauth_app,
        None,
        "/lifecycles",
        {"items": [{"document_id": doc["id"], "action": "complete"}]},
    )
    assert (await _rest_get(noauth_app, None, doc["id"]))["last_modified_by"] == _OWNER


async def test_n4_no_auth_force_reingest_stamps_the_caller(noauth_app, tmp_vault_dir) -> None:
    source = _seed(tmp_vault_dir, "n4.md")
    doc = (await _rest_ingest(noauth_app, None, source, created_by="carol"))["document"]
    await _rest_ingest(noauth_app, None, source, created_by="dave", force=True)
    after = await _rest_get(noauth_app, None, doc["id"])
    assert after["last_modified_by"] == "dave"
    assert after["created_by"] == "carol"


async def test_n5_no_auth_superseding_ingest_stamps_the_ingests_writer_on_the_predecessor(
    noauth_app, tmp_vault_dir
) -> None:
    pred = (
        await _rest_ingest(noauth_app, None, _seed(tmp_vault_dir, "n5a.md"), created_by="carol")
    )["document"]
    await _rest_ingest(
        noauth_app,
        None,
        _seed(tmp_vault_dir, "n5b.md"),
        created_by="dave",
        predecessor_id=pred["id"],
    )
    after = await _rest_get(noauth_app, None, pred["id"])
    assert after["lifecycle_status"] == "archived"
    assert after["last_modified_by"] == "dave"


# --------------------------------------------------------------------------
# Served descriptions
# --------------------------------------------------------------------------


def test_d1_ingest_created_by_description_states_the_principal_rule() -> None:
    app = create_app(stack_config=SageCoreConfig())
    tool = app.state.mcp_mounts["/mcp"]._tool_manager.get_tool("ingest_document")
    description = tool.parameters["properties"]["created_by"]["description"]
    assert "authenticated principal" in description
    assert "Without authentication, defaults to vault owner." in description
