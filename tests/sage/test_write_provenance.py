"""Writes record client and agent identity beside the principal (CAS-ADR-056).

Every document write records the *client* its request came through -- the
application the validated token was issued to, named by the deployment's client
map -- and the *agent* the caller asserts made it: a write tool's ``agent``
argument, otherwise the product the request's ``User-Agent`` names. The client
is derived from the token and no argument sets it; the agent is served marked
``trust: asserted`` whatever its source. Edges record the same three at creation.

Anti-coincidental-pass discipline:

* Principal, client name, raw client id and agent name never share a string, so
  a field wired to the wrong source fails.
* ``alice`` writes through two clients with one principal, so a client derived
  from the principal, or shared across requests, fails.
* A User-Agent that spells a client name is sent alongside a token for another
  client, so a client read from headers fails.
* The header agent and the ``agent`` argument name different agents, so either
  precedence reversed, or either source dropped, fails.
* Modifications are made by a second writer, so a write that stamps ``created_*``
  or leaves ``last_modified_*`` alone fails.
* Every filter query runs with a non-matching document in the vault.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.types import Receive, Scope, Send

from sage.app import create_app
from sage.auth import AuthenticatedPrincipal, AuthMiddleware
from sage.config import SageCoreConfig, StackAuthConfig, VaultConfig
from sage.models.schemas import BulkLinkItem, BulkLinkRequest
from sage.request_identity import (
    IDENTITY_VARS,
    agent_from_user_agent,
    asserted_agent,
    client_name,
    current_agent,
    current_client,
    edge_attribution,
    request_client,
    request_header_agent,
    request_principal,
)
from tests.helpers.write_attribution import (
    VAULT,
    StubValidator,
    client,
    decode_rpc_envelope,
    install_stub,
    mcp_call,
    mcp_running,
    running_app,
    seed,
)

_OWNER = "owner-o"
_ALICE = "alice@example.org"
_BOB = "oid-b"
_SVC = "app:ci-cid"

_CLIENT_NAMES = {"bff-cid": "cas-app", "mcp-cid": "mcp-connector", "ci-cid": "ci"}

_CLAUDE_UA = "claude-code/2.1.271 (claude-desktop, agent-sdk/0.3.281)"
_CODEX_UA = "codex/1.0"
_BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"


def _delegated(username: str | None, oid: str, azp: str) -> AuthenticatedPrincipal:
    claims = {"scp": "Sage.Access", "oid": oid, "sub": f"sub-{oid}", "azp": azp}
    if username is not None:
        claims["preferred_username"] = username
    return AuthenticatedPrincipal(
        subject=f"sub-{oid}", scopes=frozenset({"Sage.Access"}), claims=claims
    )


_PRINCIPALS = {
    "alice-bff": _delegated(_ALICE, "oid-a", "bff-cid"),
    "alice-mcp": _delegated(_ALICE, "oid-a", "mcp-cid"),
    "bob-mcp": _delegated(None, _BOB, "mcp-cid"),
    "svc": AuthenticatedPrincipal(
        subject="sub-s",
        roles=frozenset({"Sage.Access"}),
        claims={"roles": ["Sage.Access"], "azp": "ci-cid", "oid": "oid-s", "sub": "sub-s"},
    ),
}
_ENABLED = SageCoreConfig(
    auth=StackAuthConfig(
        enabled=True, tenant_id="tid", audience="api://sage", client_names=_CLIENT_NAMES
    )
)


def _agent(name: str, source: str) -> dict:
    return {"name": name, "trust": "asserted", "source": source}


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture
def owned_config(minimal_vault_config_dict) -> VaultConfig:
    config = dict(minimal_vault_config_dict)
    config["vault"] = {**config["vault"], "owner": _OWNER}
    return VaultConfig.model_validate(config)


@pytest.fixture
async def auth_app(owned_config, monkeypatch) -> AsyncIterator[object]:
    install_stub(monkeypatch, _PRINCIPALS)
    async with running_app(owned_config, _ENABLED) as app:
        yield app


@pytest.fixture
async def noauth_app(owned_config) -> AsyncIterator[object]:
    async with running_app(owned_config, None) as app:
        yield app


async def _rest(app, token, ua, method: str, path: str, body: dict | None = None):
    async with client(app, token, ua) as c:
        return await c.request(method, f"/sage_vaults/{VAULT}{path}", json=body)


async def _rest_ingest(app, token, ua, source: str, **extra) -> dict:
    resp = await _rest(
        app, token, ua, "POST", "/documents", {"source": source, "source_type": "markdown", **extra}
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["document"]


async def _get(app, doc_id: str) -> dict:
    resp = await _rest(app, "svc", None, "GET", f"/documents/{doc_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _get_noauth(app, doc_id: str) -> dict:
    resp = await _rest(app, None, None, "GET", f"/documents/{doc_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _mcp_ingest(app, token, ua, source: str, **extra) -> dict:
    return await mcp_call(
        app,
        token,
        "ingest_document",
        {"vault_id": VAULT, "source": source, "source_type": "markdown", **extra},
        user_agent=ua,
    )


def _created(record: dict) -> tuple:
    # A null field is omitted from the wire, so an absent key reads as None.
    return (
        record.get("created_by"),
        record.get("created_client"),
        record.get("created_agent"),
    )


def _modified(record: dict) -> tuple:
    return (
        record.get("last_modified_by"),
        record.get("last_modified_client"),
        record.get("last_modified_agent"),
    )


# --------------------------------------------------------------------------
# Unit: client and agent derivation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ({"scp": "x", "azp": "bff-cid", "oid": "o"}, "cas-app"),
        ({"roles": ["r"], "azp": "ci-cid", "oid": "o"}, "ci"),
        ({"roles": ["r"], "appid": "legacy-cid", "oid": "o"}, "legacy-cid"),
        ({"scp": "x", "oid": "o"}, None),
    ],
    ids=["delegated-mapped", "app-mapped", "v1-appid-unmapped", "no-client-claim"],
)
def test_u1_client_name_resolution(claims: dict, expected: str | None) -> None:
    principal = AuthenticatedPrincipal(subject="s", claims=claims)
    assert client_name(principal, _CLIENT_NAMES) == expected


def test_u1_anonymous_principal_has_no_client() -> None:
    anonymous = AuthenticatedPrincipal(subject=None, anonymous=True, claims={"azp": "bff-cid"})
    assert client_name(anonymous, _CLIENT_NAMES) is None


@pytest.mark.parametrize(
    ("user_agent", "expected"),
    [
        (_CLAUDE_UA, "claude-code"),
        ("Codex/1.0 (darwin)", "codex"),
        (_BROWSER_UA, None),
        ("python-httpx/0.28.1", None),
        ("node", None),
        ("", None),
        (None, None),
        ("x" * 65 + "/1.0", None),
        ("../x", None),
    ],
    ids=[
        "claude-code",
        "mixed-case",
        "browser",
        "sdk-library",
        "bare-runtime",
        "empty",
        "absent",
        "overlong",
        "malformed",
    ],
)
def test_u2_agent_from_user_agent(user_agent: str | None, expected: str | None) -> None:
    assert agent_from_user_agent(user_agent) == expected


def test_u2_parameter_agent_wins_over_the_header_and_resets() -> None:
    binding = request_header_agent.set("claude-code")
    try:
        assert current_agent().model_dump() == _agent("claude-code", "header")
        with asserted_agent("nightly-sync"):
            assert current_agent().model_dump() == _agent("nightly-sync", "parameter")
        assert current_agent().model_dump() == _agent("claude-code", "header")
    finally:
        request_header_agent.reset(binding)
    assert current_agent() is None


def test_w1_edge_attribution_outside_a_request_is_empty() -> None:
    # Bound client and agent without a principal is not a request: nobody is attributed.
    bindings = [(request_client, request_client.set("cas-app"))]
    try:
        assert edge_attribution(_OWNER) == {
            "created_by": None,
            "created_client": None,
            "created_agent": None,
        }
    finally:
        for var, token in bindings:
            var.reset(token)


# --------------------------------------------------------------------------
# Middleware binding
# --------------------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.seen: list[tuple] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        agent = current_agent()
        self.seen.append((current_client(), agent.name if agent else None))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})


async def test_m1_middleware_binds_client_and_header_agent_per_request() -> None:
    inner = _Recorder()
    app = AuthMiddleware(inner, validator=StubValidator(_PRINCIPALS), client_names=_CLIENT_NAMES)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.get("/x", headers={"Authorization": "Bearer alice-mcp", "User-Agent": _CLAUDE_UA})
        await c.get("/x", headers={"Authorization": "Bearer alice-bff", "User-Agent": _BROWSER_UA})
    assert inner.seen == [("mcp-connector", "claude-code"), ("cas-app", None)]
    assert current_client() is None
    assert current_agent() is None


# --------------------------------------------------------------------------
# Document writes
# --------------------------------------------------------------------------


async def test_a1_mcp_ingest_records_principal_client_and_header_agent(
    auth_app, tmp_vault_dir
) -> None:
    async with mcp_running(auth_app):
        doc = await _mcp_ingest(auth_app, "alice-mcp", _CLAUDE_UA, seed(tmp_vault_dir, "a1.md"))
    stored = await _get(auth_app, doc["id"])
    expected = (_ALICE, "mcp-connector", _agent("claude-code", "header"))
    assert _created(stored) == expected
    assert _modified(stored) == expected


async def test_a2_cas_app_write_is_distinguishable_by_client(auth_app, tmp_vault_dir) -> None:
    async with mcp_running(auth_app):
        via_mcp = await _mcp_ingest(
            auth_app, "alice-mcp", _CLAUDE_UA, seed(tmp_vault_dir, "a2m.md")
        )
    via_app = await _rest_ingest(auth_app, "alice-bff", _BROWSER_UA, seed(tmp_vault_dir, "a2b.md"))
    stored_app = await _get(auth_app, via_app["id"])
    stored_mcp = await _get(auth_app, via_mcp["id"])
    assert stored_app["created_by"] == stored_mcp["created_by"] == _ALICE
    assert stored_app["created_client"] == "cas-app"
    assert stored_mcp["created_client"] == "mcp-connector"
    assert stored_app.get("created_agent") is None


async def test_a3_agent_argument_overrides_the_header(auth_app, tmp_vault_dir) -> None:
    async with mcp_running(auth_app):
        doc = await _mcp_ingest(
            auth_app, "alice-mcp", _CLAUDE_UA, seed(tmp_vault_dir, "a3.md"), agent="nightly-sync"
        )
    rest_doc = await _rest_ingest(
        auth_app, "alice-mcp", _CLAUDE_UA, seed(tmp_vault_dir, "a3r.md"), agent="nightly-sync"
    )
    for stored in (await _get(auth_app, doc["id"]), await _get(auth_app, rest_doc["id"])):
        assert stored["created_agent"] == _agent("nightly-sync", "parameter")


@pytest.mark.parametrize("agent", ["Claude Code", "", "a" * 65, "../x"])
async def test_u3_malformed_agent_is_refused(auth_app, tmp_vault_dir, agent: str) -> None:
    resp = await _rest(
        auth_app,
        "alice-mcp",
        None,
        "POST",
        "/documents",
        {"source": seed(tmp_vault_dir, "u3.md"), "source_type": "markdown", "agent": agent},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "invalid_parameter"
    async with mcp_running(auth_app):
        async with client(auth_app, "alice-mcp") as c:
            resp = await c.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "create_edges",
                        "arguments": {"vault_id": VAULT, "items": [], "agent": agent},
                    },
                },
            )
    envelope = decode_rpc_envelope(resp)
    assert "invalid_parameter" in envelope["result"]["content"][0]["text"]


def test_a4_no_write_input_can_name_a_client() -> None:
    app = create_app(stack_config=SageCoreConfig())
    offenders = []
    for mount in app.state.mcp_mounts.values():
        for tool in mount._tool_manager.list_tools():
            for name in tool.parameters.get("properties", {}):
                if "client" in name:
                    offenders.append(f"{tool.name}.{name}")
    spec = app.openapi()
    for schema_name, schema in spec["components"]["schemas"].items():
        if schema_name.endswith("Request"):
            for name in schema.get("properties", {}):
                if "client" in name:
                    offenders.append(f"{schema_name}.{name}")
    assert offenders == []


async def test_a4_client_comes_from_the_token_not_the_headers(auth_app, tmp_vault_dir) -> None:
    # The User-Agent spells another client's name; the token decides.
    doc = await _rest_ingest(auth_app, "alice-mcp", "cas-app/1.0", seed(tmp_vault_dir, "a4.md"))
    stored = await _get(auth_app, doc["id"])
    assert stored["created_client"] == "mcp-connector"
    assert stored["created_agent"] == _agent("cas-app", "header")


async def test_a4_client_in_metadata_is_not_stored(auth_app, tmp_vault_dir) -> None:
    # ``metadata`` ignores a name outside its recognized set; the token decides.
    doc = await _rest_ingest(
        auth_app,
        "alice-mcp",
        None,
        seed(tmp_vault_dir, "a4m.md"),
        metadata={"created_client": "cas-app"},
    )
    assert (await _get(auth_app, doc["id"]))["created_client"] == "mcp-connector"


async def test_a5_update_metadata_restamps_only_the_last_modification(
    auth_app, tmp_vault_dir
) -> None:
    async with mcp_running(auth_app):
        doc = await _mcp_ingest(auth_app, "alice-mcp", _CLAUDE_UA, seed(tmp_vault_dir, "a5.md"))
        await mcp_call(
            auth_app,
            "bob-mcp",
            "update_metadata",
            {"vault_id": VAULT, "items": [{"document_id": doc["id"], "title": "Patched"}]},
            user_agent=_CODEX_UA,
        )
    stored = await _get(auth_app, doc["id"])
    assert _created(stored) == (_ALICE, "mcp-connector", _agent("claude-code", "header"))
    assert _modified(stored) == (_BOB, "mcp-connector", _agent("codex", "header"))


async def test_a6_lifecycle_transition_records_the_app_client(auth_app, tmp_vault_dir) -> None:
    doc = await _rest_ingest(auth_app, "alice-mcp", _CLAUDE_UA, seed(tmp_vault_dir, "a6.md"))
    resp = await _rest(
        auth_app,
        "svc",
        None,
        "POST",
        "/lifecycles",
        {"items": [{"document_id": doc["id"], "action": "complete"}]},
    )
    assert resp.status_code == 200, resp.text
    stored = await _get(auth_app, doc["id"])
    assert _modified(stored) == (_SVC, "ci", None)
    assert stored["created_client"] == "mcp-connector"


async def test_a7_supersession_records_the_superseding_writer_everywhere(
    auth_app, tmp_vault_dir
) -> None:
    async with mcp_running(auth_app):
        first = await _mcp_ingest(auth_app, "alice-mcp", _CLAUDE_UA, seed(tmp_vault_dir, "a7.md"))
        second = await _mcp_ingest(
            auth_app,
            "bob-mcp",
            _CODEX_UA,
            seed(tmp_vault_dir, "a7-v2.md"),
            predecessor_id=first["id"],
            agent="reviser",
        )
        chain = await mcp_call(
            auth_app,
            "svc",
            "traverse",
            {"vault_id": VAULT, "start_id": second["id"], "edge_type": "supersedes"},
        )
    bob = (_BOB, "mcp-connector", _agent("reviser", "parameter"))
    assert _modified(await _get(auth_app, first["id"])) == bob
    assert _created(await _get(auth_app, second["id"])) == bob
    (node,) = chain["nodes"]
    assert _created(node["edge"]) == bob


async def test_a8_create_edges_records_the_writer_on_every_edge(auth_app, tmp_vault_dir) -> None:
    async with mcp_running(auth_app):
        a = await _mcp_ingest(auth_app, "svc", None, seed(tmp_vault_dir, "a8a.md"))
        b = await _mcp_ingest(auth_app, "svc", None, seed(tmp_vault_dir, "a8b.md"))
        c = await _mcp_ingest(auth_app, "svc", None, seed(tmp_vault_dir, "a8c.md"))
        await mcp_call(
            auth_app,
            "alice-mcp",
            "create_edges",
            {
                "vault_id": VAULT,
                "items": [
                    {
                        "source_id": a["id"],
                        "target_id": b["id"],
                        "edge_type": "depends_on",
                        "source_valid_from_version": a["id"],
                        "target_valid_from_version": b["id"],
                    },
                    {
                        "source_id": a["id"],
                        "target_id": c["id"],
                        "edge_type": "depends_on",
                        "source_valid_from_version": a["id"],
                        "target_valid_from_version": c["id"],
                    },
                ],
            },
            user_agent=_CLAUDE_UA,
        )
        traversed = await mcp_call(
            auth_app,
            "svc",
            "traverse",
            {"vault_id": VAULT, "start_id": a["id"], "edge_type": "depends_on", "depth": 1},
        )
        full = await mcp_call(
            auth_app,
            "svc",
            "search",
            {
                "vault_id": VAULT,
                "target": "edges",
                "filters": {"source_id": a["id"]},
                "response_mode": "full",
            },
        )
        light = await mcp_call(
            auth_app,
            "svc",
            "search",
            {
                "vault_id": VAULT,
                "target": "edges",
                "filters": {"source_id": a["id"]},
                "response_mode": "light",
            },
        )
    alice = (_ALICE, "mcp-connector", _agent("claude-code", "header"))
    assert len(traversed["nodes"]) == 2
    assert all(_created(node["edge"]) == alice for node in traversed["nodes"])
    assert len(full["results"]) == 2
    assert all(_created(hit) == alice for hit in full["results"])
    assert all("created_by" not in hit for hit in light["results"])


async def test_a9_staging_confirm_records_the_confirming_writer(auth_app, tmp_vault_dir) -> None:
    from datetime import datetime, timezone

    from sage.models.schemas import StagingEdge

    a = await _rest_ingest(auth_app, "svc", None, seed(tmp_vault_dir, "a9a.md"))
    b = await _rest_ingest(auth_app, "svc", None, seed(tmp_vault_dir, "a9b.md"))
    store = auth_app.state.vault_registry[VAULT].graph_store
    staged, _ = await store.insert_staging_edge(
        StagingEdge(
            id="11111111-1111-4111-8111-111111111111",
            source_id=a["id"],
            target_id=b["id"],
            edge_type="references",
            inference_evidence="test",
            confidence_tier=2,
            created_at=datetime.now(timezone.utc),
        )
    )
    async with mcp_running(auth_app):
        confirmed = await mcp_call(
            auth_app,
            "bob-mcp",
            "update_staging_edge",
            {"vault_id": VAULT, "edge_id": staged.id, "action": "confirm"},
            user_agent=_CODEX_UA,
        )
    edge = await store.get_edge(confirmed["production_edge_id"])
    assert (edge.created_by, edge.created_client, edge.created_agent.name) == (
        _BOB,
        "mcp-connector",
        "codex",
    )


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------


async def test_f1_provenance_filter_matches_exactly(auth_app, tmp_vault_dir) -> None:
    async with mcp_running(auth_app):
        a1 = await _mcp_ingest(auth_app, "alice-mcp", _CLAUDE_UA, seed(tmp_vault_dir, "f1a.md"))
        a2 = await _rest_ingest(auth_app, "alice-bff", _BROWSER_UA, seed(tmp_vault_dir, "f1b.md"))
        a5 = await _mcp_ingest(auth_app, "alice-mcp", _CLAUDE_UA, seed(tmp_vault_dir, "f1c.md"))
        await mcp_call(
            auth_app,
            "bob-mcp",
            "update_metadata",
            {"vault_id": VAULT, "items": [{"document_id": a5["id"], "title": "Bob's"}]},
        )

        async def ids(provenance: dict) -> set[str]:
            result = await mcp_call(
                auth_app,
                "svc",
                "search",
                {
                    "vault_id": VAULT,
                    "mode": "catalog",
                    "filters": {"provenance": provenance},
                    "limit": 100,
                },
            )
            return {hit["document"]["id"] for hit in result["results"]}

        assert await ids({"created_client": "cas-app"}) == {a2["id"]}
        assert await ids({"created_agent": "claude-code"}) == {a1["id"], a5["id"]}
        assert await ids({"last_modified_by": _BOB}) == {a5["id"]}
        assert await ids({"created_agent": None}) == {a2["id"]}
        assert await ids(
            {
                "created_agent": "claude-code",
                "last_modified_client": "mcp-connector",
                "last_modified_by": _ALICE,
            }
        ) == {a1["id"]}


async def test_f2_provenance_filter_refusals(auth_app) -> None:
    async with mcp_running(auth_app):
        unknown = await mcp_call(
            auth_app,
            "svc",
            "search",
            {"vault_id": VAULT, "mode": "catalog", "filters": {"provenance": {"writer": "x"}}},
        )
        on_edges = await mcp_call(
            auth_app,
            "svc",
            "search",
            {"vault_id": VAULT, "target": "edges", "filters": {"provenance": {"created_by": "x"}}},
        )
        top_level = await mcp_call(
            auth_app,
            "svc",
            "search",
            {"vault_id": VAULT, "mode": "catalog", "provenance": {"created_by": "x"}},
        )
    assert unknown["error"] == "unknown_filter_key"
    # The nested key is named, with the provenance filter's own key set.
    assert unknown["detail"]["key"] == "provenance.writer"
    assert "created_client" in unknown["detail"]["valid_keys"]
    assert "doc_type" not in unknown["detail"]["valid_keys"]
    rest = await _rest(
        auth_app,
        "svc",
        None,
        "POST",
        "/discover",
        {"mode": "catalog", "filters": {"provenance": {"writer": "x"}}},
    )
    assert rest.status_code == 400, rest.text
    assert rest.json()["code"] == "unknown_filter_key"
    assert rest.json()["detail"]["key"] == "provenance.writer"
    assert on_edges["error"] == "mode_parameter_mismatch"
    assert top_level["error"] == "misplaced_filters"


# --------------------------------------------------------------------------
# No-auth profile and isolation
# --------------------------------------------------------------------------


async def test_n1_no_auth_records_no_client_and_asserts_the_agent(
    noauth_app, tmp_vault_dir
) -> None:
    # A token carrying azp is sent anyway: the no-auth profile validates no one.
    header = await _rest_ingest(noauth_app, "alice-mcp", _CLAUDE_UA, seed(tmp_vault_dir, "n1a.md"))
    named = await _rest_ingest(
        noauth_app, None, _CLAUDE_UA, seed(tmp_vault_dir, "n1b.md"), agent="nightly-sync"
    )
    stored_header = await _get_noauth(noauth_app, header["id"])
    stored_named = await _get_noauth(noauth_app, named["id"])
    assert _created(stored_header) == (_OWNER, None, _agent("claude-code", "header"))
    assert _created(stored_named) == (_OWNER, None, _agent("nightly-sync", "parameter"))

    # A supersedes edge takes the operation's writer, here the caller's created_by.
    revised = await _rest_ingest(
        noauth_app,
        None,
        _CODEX_UA,
        seed(tmp_vault_dir, "n1a-v2.md"),
        predecessor_id=header["id"],
        created_by="carol",
    )
    async with mcp_running(noauth_app):
        chain = await mcp_call(
            noauth_app,
            None,
            "traverse",
            {"vault_id": VAULT, "start_id": revised["id"], "edge_type": "supersedes"},
        )
    (node,) = chain["nodes"]
    assert _created(node["edge"]) == ("carol", None, _agent("codex", "header"))


async def test_w1_edge_created_outside_a_request_is_unattributed(auth_app, tmp_vault_dir) -> None:
    a = await _rest_ingest(auth_app, "svc", None, seed(tmp_vault_dir, "w1a.md"))
    b = await _rest_ingest(auth_app, "svc", None, seed(tmp_vault_dir, "w1b.md"))
    service = auth_app.state.vault_registry[VAULT].graph_ops_service
    response = await service.create_edges(
        BulkLinkRequest(
            items=[
                BulkLinkItem(
                    source_id=a["id"],
                    target_id=b["id"],
                    edge_type="references",
                    source_valid_from_version=a["id"],
                    target_valid_from_version=b["id"],
                )
            ]
        )
    )
    (result,) = response.results
    edge = await auth_app.state.vault_registry[VAULT].graph_store.get_edge(result.edge.id)
    assert (edge.created_by, edge.created_client, edge.created_agent) == (None, None, None)


async def test_w1_abstraction_worker_clears_every_identity(noauth_app) -> None:
    service = noauth_app.state.vault_registry[VAULT].ingestion_service
    await service.stop_worker()
    bindings = [
        (request_principal, request_principal.set(_PRINCIPALS["alice-mcp"])),
        (request_client, request_client.set("mcp-connector")),
        (request_header_agent, request_header_agent.set("claude-code")),
    ]
    try:
        with asserted_agent("nightly-sync"):
            service._ensure_worker_running()
    finally:
        for var, token in reversed(bindings):
            var.reset(token)
    try:
        context = service._worker_task.get_context()
        assert [context.get(var) for var in IDENTITY_VARS] == [None] * len(IDENTITY_VARS)
    finally:
        await service.stop_worker()


# --------------------------------------------------------------------------
# Stored rows from before the columns existed
# --------------------------------------------------------------------------


async def test_s2_rows_without_provenance_read_back_null(auth_app, tmp_vault_dir) -> None:
    a = await _rest_ingest(auth_app, "alice-mcp", _CLAUDE_UA, seed(tmp_vault_dir, "s2a.md"))
    b = await _rest_ingest(auth_app, "alice-mcp", _CLAUDE_UA, seed(tmp_vault_dir, "s2b.md"))
    service = auth_app.state.vault_registry[VAULT]
    store = service.graph_store
    link = await _rest(
        auth_app,
        "alice-mcp",
        _CLAUDE_UA,
        "POST",
        "/edges",
        {
            "items": [
                {
                    "source_id": a["id"],
                    "target_id": b["id"],
                    "edge_type": "references",
                    "source_valid_from_version": a["id"],
                    "target_valid_from_version": b["id"],
                }
            ]
        },
    )
    assert link.status_code == 200, link.text
    # Blank the columns as a row written before they existed reads.
    async with store._pool.connection() as conn:
        await conn.execute(
            "UPDATE documents SET created_client = NULL, created_agent = NULL, "
            "last_modified_client = NULL, last_modified_agent = NULL WHERE id = %s",
            (a["id"],),
        )
        await conn.execute(
            "UPDATE edges SET created_by = NULL, created_client = NULL, created_agent = NULL "
            "WHERE source_id = %s",
            (a["id"],),
        )
    stored = await _get(auth_app, a["id"])
    assert _created(stored)[1:] == (None, None)
    assert _modified(stored)[1:] == (None, None)
    async with mcp_running(auth_app):
        traversed = await mcp_call(
            auth_app, "svc", "traverse", {"vault_id": VAULT, "start_id": a["id"], "depth": 1}
        )
    (node,) = traversed["nodes"]
    assert _created(node["edge"]) == (None, None, None)


# --------------------------------------------------------------------------
# Served descriptions
# --------------------------------------------------------------------------


def test_d1_descriptions_carry_the_trust_labels() -> None:
    app = create_app(stack_config=SageCoreConfig())
    tools = app.state.mcp_mounts["/mcp"]._tool_manager
    get_document = tools.get_tool("get_document").description
    for field in ("created_client", "created_agent", "last_modified_client", "last_modified_agent"):
        assert f"``{field}``" in get_document
    assert "server-derived" in get_document
    assert "asserted" in get_document and "never verified" in get_document
    for tool in ("ingest_document", "update_metadata", "update_lifecycles", "create_edges"):
        description = tools.get_tool(tool).parameters["properties"]["agent"]["description"]
        assert "asserted and never verified" in description, tool
    document = app.openapi()["components"]["schemas"]["Document"]["properties"]
    assert "verified" in document["created_client"]["description"]
    assert "asserted and never verified" in document["created_agent"]["description"]
