"""Write attribution follows the authenticated principal.

Under a profile that authenticates callers, ``created_by`` on a new document
and ``last_modified_by`` on every created, patched, superseded or
lifecycle-transitioned document record the principal whose credential made the
write -- on the REST routes and the MCP tools alike. A human principal is
keyed by its tenant and object id, which survive a rename; its display name is
recorded beside the key in ``created_by_name`` and ``last_modified_by_name`` as
a snapshot at write time. Without authentication a caller-supplied
``created_by`` is used and the vault owner is the default, lifecycle
transitions stamp the vault owner, and no display name is recorded.

Anti-coincidental-pass discipline:

* The vault owner, the caller-supplied values and every principal's identity
  are pairwise distinct, so neither an owner fallback nor caller pass-through
  can satisfy an assertion meant for the principal.
* Every update test creates the document as one writer and modifies it as
  another, so a write that stamps nothing leaves the first writer's value in
  place and fails.
* ``alice`` carries both a ``preferred_username`` and an ``oid``, so a key
  derived from the mutable name fails; ``alice-renamed`` shares her ``oid``
  under another name, so a key derived from any mutable claim splits her
  history and fails.
* ``bob`` carries no display-name claim, so a snapshot copied from the key
  fails, and a key derived from ``sub`` fails.
* The multi-principal tests alternate principals request by request, so an
  identity captured per application, per vault or per MCP session fails.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.types import Receive, Scope, Send

from sage.app import create_app
from sage.auth import AuthenticatedPrincipal, AuthMiddleware, NoAuthValidator
from sage.config import SageCoreConfig, StackAuthConfig, VaultConfig
from sage.request_identity import (
    current_actor,
    principal_actor,
    principal_display_name,
    request_principal,
)
from tests.helpers.write_attribution import (
    VAULT as _VAULT,
)
from tests.helpers.write_attribution import (
    StubValidator,
    install_stub,
    mcp_running,
    running_app,
    seed,
)
from tests.helpers.write_attribution import (
    client as _client,
)
from tests.helpers.write_attribution import (
    mcp_call as _mcp_call,
)

_OWNER = "owner-o"
_TID = "tid-1"
_ALICE = f"{_TID}:oid-a"
_ALICE_NAME = "alice@example.org"
_ALICE_NEW_NAME = "alice.new@example.org"
_BOB = f"{_TID}:oid-b"
_SVC = "app:client-1"

_PRINCIPALS = {
    "alice-tok": AuthenticatedPrincipal(
        subject="sub-a",
        scopes=frozenset({"Sage.Access"}),
        claims={
            "scp": "Sage.Access",
            "tid": _TID,
            "preferred_username": _ALICE_NAME,
            "oid": "oid-a",
            "sub": "sub-a",
        },
    ),
    "alice-renamed-tok": AuthenticatedPrincipal(
        subject="sub-a2",
        scopes=frozenset({"Sage.Access"}),
        claims={
            "scp": "Sage.Access",
            "tid": _TID,
            "preferred_username": _ALICE_NEW_NAME,
            "oid": "oid-a",
            "sub": "sub-a2",
        },
    ),
    "bob-tok": AuthenticatedPrincipal(
        subject="sub-b",
        scopes=frozenset({"Sage.Access"}),
        claims={"scp": "Sage.Access", "tid": _TID, "oid": "oid-b", "sub": "sub-b"},
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


def _StubValidator() -> StubValidator:  # noqa: N802 -- stands in for the former class
    return StubValidator(_PRINCIPALS)


def _install_stub(monkeypatch) -> None:
    install_stub(monkeypatch, _PRINCIPALS)


_app = running_app
_mcp_running = mcp_running
_seed = seed


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def owned_config(minimal_vault_config_dict) -> VaultConfig:
    config = dict(minimal_vault_config_dict)
    config["vault"] = {**config["vault"], "owner": _OWNER}
    return VaultConfig.model_validate(config)


@pytest.fixture
async def auth_app(owned_config, monkeypatch) -> AsyncIterator[object]:
    _install_stub(monkeypatch)
    async with _app(owned_config, _ENABLED) as app:
        yield app


@pytest.fixture
async def noauth_app(owned_config) -> AsyncIterator[object]:
    async with _app(owned_config, None) as app:
        yield app


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


# --------------------------------------------------------------------------
# Unit: identity derivation and the request-scoped binding
# --------------------------------------------------------------------------


_ISS = "https://login.example/tid-1/v2.0"


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        (
            {"scp": "x", "tid": "t", "iss": _ISS, "preferred_username": "u@x", "oid": "o"},
            "t:o",
        ),
        ({"scp": "x", "tid": "t", "iss": _ISS, "oid": "o", "sub": "s"}, "t:o"),
        ({"scp": "x", "tid": "t", "iss": _ISS, "preferred_username": "u@x", "sub": "s"}, "t:s"),
        ({"scp": "x", "iss": _ISS, "preferred_username": "u@x", "oid": "o"}, f"{_ISS}#o"),
        ({"scp": "x", "iss": _ISS, "sub": "s"}, f"{_ISS}#s"),
        ({"roles": ["r"], "tid": "t", "azp": "client-1", "oid": "o"}, "app:client-1"),
        ({"roles": ["r"], "tid": "t", "appid": "client-v1", "oid": "o"}, "app:client-v1"),
        ({"roles": ["r"], "sub": "s"}, "s"),
    ],
    ids=[
        "delegated-tenant-oid-not-upn",
        "delegated-tenant-oid-not-sub",
        "delegated-tenant-sub-without-oid",
        "delegated-issuer-oid-without-tid",
        "delegated-issuer-sub-without-tid-or-oid",
        "app-azp",
        "app-v1-appid",
        "no-client-falls-back-to-subject",
    ],
)
def test_u1_principal_actor_derivation(claims: dict, expected: str) -> None:
    principal = AuthenticatedPrincipal(subject=claims.get("sub"), claims=claims)
    assert principal_actor(principal) == expected


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ({"scp": "x", "tid": "t", "preferred_username": "u@x", "name": "U X", "oid": "o"}, "u@x"),
        ({"scp": "x", "tid": "t", "name": "U X", "oid": "o"}, "U X"),
        ({"scp": "x", "tid": "t", "oid": "o"}, None),
        ({"roles": ["r"], "azp": "c", "preferred_username": "u@x", "name": "U X"}, None),
    ],
    ids=["delegated-upn", "delegated-name", "delegated-unnamed", "app-only-unnamed"],
)
def test_u1b_principal_display_name(claims: dict, expected: str | None) -> None:
    principal = AuthenticatedPrincipal(subject=claims.get("sub"), claims=claims)
    assert principal_display_name(principal) == expected


def test_u1b_anonymous_principal_has_no_display_name() -> None:
    assert principal_display_name(AuthenticatedPrincipal(subject=None, anonymous=True)) is None


def test_u1c_a_rename_keeps_the_key_and_changes_the_snapshot() -> None:
    before, after = _PRINCIPALS["alice-tok"], _PRINCIPALS["alice-renamed-tok"]
    assert principal_actor(before) == principal_actor(after) == _ALICE
    assert (principal_display_name(before), principal_display_name(after)) == (
        _ALICE_NAME,
        _ALICE_NEW_NAME,
    )


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
    assert doc["created_by_name"] == _ALICE_NAME
    assert doc["last_modified_by_name"] == _ALICE_NAME
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
    # An absent field is omitted from the wire.
    assert second["document"].get("created_by_name") is None
    assert second["document"].get("last_modified_by_name") is None


async def test_r4_metadata_patch_stamps_the_patching_principal(auth_app, tmp_vault_dir) -> None:
    doc = (await _rest_ingest(auth_app, "alice-tok", _seed(tmp_vault_dir, "r4.md")))["document"]
    await _rest_post(
        auth_app, "bob-tok", "/metadata", {"items": [{"document_id": doc["id"], "title": "New"}]}
    )
    after = await _rest_get(auth_app, "bob-tok", doc["id"])
    assert after["last_modified_by"] == _BOB
    assert after["created_by"] == _ALICE
    assert after["created_by_name"] == _ALICE_NAME
    assert after.get("last_modified_by_name") is None


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
    assert doc.created_by_name is None
    assert doc.last_modified_by_name is None


async def test_r9_force_reingest_stamps_the_principal(auth_app, tmp_vault_dir) -> None:
    source = _seed(tmp_vault_dir, "r9.md")
    doc = (await _rest_ingest(auth_app, "alice-tok", source))["document"]
    again = (await _rest_ingest(auth_app, "bob-tok", source, force=True))["document"]
    assert again["id"] == doc["id"]
    after = await _rest_get(auth_app, "bob-tok", doc["id"])
    assert after["last_modified_by"] == _BOB
    assert after["created_by"] == _ALICE


async def test_r10_a_rename_does_not_split_the_principals_history(auth_app, tmp_vault_dir) -> None:
    doc = (await _rest_ingest(auth_app, "alice-tok", _seed(tmp_vault_dir, "r10.md")))["document"]
    other = (await _rest_ingest(auth_app, "bob-tok", _seed(tmp_vault_dir, "r10b.md")))["document"]
    await _rest_post(
        auth_app,
        "alice-renamed-tok",
        "/metadata",
        {"items": [{"document_id": doc["id"], "title": "Renamed"}]},
    )
    after = await _rest_get(auth_app, "bob-tok", doc["id"])
    assert after["created_by"] == after["last_modified_by"] == _ALICE
    assert after["created_by_name"] == _ALICE_NAME
    assert after["last_modified_by_name"] == _ALICE_NEW_NAME

    async def found(provenance: dict) -> set[str]:
        body = await _rest_post(
            auth_app,
            "bob-tok",
            "/discover",
            {"mode": "catalog", "limit": 100, "filters": {"provenance": provenance}},
        )
        return {hit["document"]["id"] for hit in body["results"]}

    # Positive control: bob's document is excluded by the key, not by the filter failing.
    assert await found({"created_by": _BOB}) == {other["id"]}
    assert await found({"created_by": _ALICE}) == {doc["id"]}
    assert await found({"last_modified_by": _ALICE}) == {doc["id"]}
    assert await found({"created_by": _ALICE_NAME}) == set()


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
        assert plain["created_by_name"] == _ALICE_NAME
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
    assert unclaimed["document"]["last_modified_by"] == _OWNER
    # Neither the caller's value nor the owner is a display name.
    for record in (claimed["document"], unclaimed["document"]):
        assert record.get("created_by_name") is None
        assert record.get("last_modified_by_name") is None


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
        for doc_id in (via_mcp["id"], via_rest["id"]):
            after = await _rest_get(noauth_app, None, doc_id)
            assert after["last_modified_by"] == _OWNER
            assert after.get("last_modified_by_name") is None


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


async def test_n6_no_auth_force_reingest_with_predecessor_stamps_the_ingests_writer(
    noauth_app, tmp_vault_dir
) -> None:
    pred = (
        await _rest_ingest(noauth_app, None, _seed(tmp_vault_dir, "n6a.md"), created_by="carol")
    )["document"]
    source = _seed(tmp_vault_dir, "n6b.md")
    await _rest_ingest(noauth_app, None, source, created_by="carol")
    await _rest_ingest(
        noauth_app, None, source, created_by="dave", force=True, predecessor_id=pred["id"]
    )
    after = await _rest_get(noauth_app, None, pred["id"])
    assert after["lifecycle_status"] == "archived"
    assert after["last_modified_by"] == "dave"


async def test_abstraction_worker_does_not_inherit_the_request_principal(noauth_app) -> None:
    # The per-vault worker is started lazily from inside whichever request first
    # enqueues work, and outlives it; it must not carry that request's identity.
    service = noauth_app.state.vault_registry[_VAULT].ingestion_service
    await service.stop_worker()
    binding = request_principal.set(_PRINCIPALS["alice-tok"])
    try:
        assert current_actor() == _ALICE
        service._ensure_worker_running()
    finally:
        request_principal.reset(binding)
    try:
        assert service._worker_task.get_context().get(request_principal) is None
    finally:
        await service.stop_worker()


# --------------------------------------------------------------------------
# Served descriptions
# --------------------------------------------------------------------------


def test_d1_ingest_created_by_description_states_the_principal_rule() -> None:
    app = create_app(stack_config=SageCoreConfig())
    tool = app.state.mcp_mounts["/mcp"]._tool_manager.get_tool("ingest_document")
    description = tool.parameters["properties"]["created_by"]["description"]
    assert "authenticated principal" in description
    assert "Without authentication, defaults to vault owner." in description


def test_d1_created_by_descriptions_state_the_stable_key() -> None:
    app = create_app(stack_config=SageCoreConfig())
    document = app.openapi()["components"]["schemas"]["Document"]["properties"]
    created_by = document["created_by"]["description"]
    assert "tenant" in created_by and "object id" in created_by
    assert "preferred_username" not in created_by
    for field in ("created_by_name", "last_modified_by_name"):
        description = document[field]["description"]
        assert "snapshot" in description and "never a key" in description, field
    get_document = app.state.mcp_mounts["/mcp"]._tool_manager.get_tool("get_document")
    for field in ("created_by_name", "last_modified_by_name"):
        assert f"``{field}``" in get_document.description
