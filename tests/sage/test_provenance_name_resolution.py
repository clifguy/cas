"""A principal filters by display name and enumerates through facets (CAS-ADR-056).

A ``created_by`` or ``last_modified_by`` filter value matches its key exactly,
and also every key whose *latest* display-name snapshot in the vault equals it
case-insensitively. The filter always executes on keys; the resolution is
reported in ``hints.provenance_resolution``, a name matching several keys
matches all of them, and a value that is neither a key nor any latest name
names the nearest display names. The six provenance fields are opt-in facet
fields, and a principal facet row labels each key with its latest name.

Anti-coincidental-pass discipline:

* Alice writes under her former name and is then renamed on a write to another
  document, so matching the stored snapshot, rather than the latest name,
  returns only the renamed write and fails; matching any historical name
  returns her documents for the former name and fails.
* The ambiguity principal's name differs from alice's latest only in case, so
  first-match-wins or case-sensitive resolution fails.
* Every resolving query runs with a non-matching document in the vault.
* A client name never equals a display name, so resolution applied to the
  client fields shows as a resolution hint.
* The facet label test slices to a document written under the former name, so
  labels read from the slice or from the stored snapshot fail.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest

from sage.auth import AuthenticatedPrincipal
from sage.config import SageCoreConfig, StackAuthConfig, VaultConfig
from sage.models.enums import EdgeType, PipelineStatus, SourceType
from sage.models.schemas import AssertedAgent, Document, Edge
from tests.helpers.write_attribution import (
    VAULT,
    client,
    decode_rpc_envelope,
    install_stub,
    mcp_call,
    mcp_running,
    running_app,
    seed,
)

_OWNER = "owner-o"
_TID = "tid-1"
_ALICE = f"{_TID}:oid-a"
_ALICE2 = f"{_TID}:oid-a2"
_MALLORY = f"{_TID}:oid-m"
_BOB = f"{_TID}:oid-b"
_SVC = "app:ci-cid"
_ALICE_OLD = "alice@example.org"
_ALICE_NEW = "alice.new@example.org"
_ALICE2_NAME = "Alice.New@example.org"

_CLIENT_NAMES = {"bff-cid": "cas-app", "mcp-cid": "mcp-connector", "ci-cid": "ci"}
_CLAUDE_UA = "claude-code/2.1.271"

_PROVENANCE_FIELDS = (
    "created_by",
    "last_modified_by",
    "created_client",
    "last_modified_client",
    "created_agent",
    "last_modified_agent",
)
# A literal, not the implementation's constant: a drifted constant must not
# re-shape the expectation it is tested by.
_DEFAULT_FACET_FIELDS = ["doc_type", "lifecycle_status", "source_type", "pipeline_status", "tags"]


def _delegated(username: str | None, oid: str) -> AuthenticatedPrincipal:
    claims = {"scp": "Sage.Access", "tid": _TID, "oid": oid, "sub": f"sub-{oid}", "azp": "mcp-cid"}
    if username is not None:
        claims["preferred_username"] = username
    return AuthenticatedPrincipal(
        subject=f"sub-{oid}", scopes=frozenset({"Sage.Access"}), claims=claims
    )


_PRINCIPALS = {
    "alice-old": _delegated(_ALICE_OLD, "oid-a"),
    "alice-new": _delegated(_ALICE_NEW, "oid-a"),
    "alice2": _delegated(_ALICE2_NAME, "oid-a2"),
    "bob": _delegated(None, "oid-b"),
    # A display name that spells another principal's key.
    "mallory": _delegated(_BOB, "oid-m"),
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


async def _ingest(app, token: str, tmp_vault_dir, name: str) -> str:
    source = seed(tmp_vault_dir, name)
    async with client(app, token, _CLAUDE_UA) as c:
        resp = await c.post(
            f"/sage_vaults/{VAULT}/documents", json={"source": source, "source_type": "markdown"}
        )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["document"]["id"]


async def _retitle(app, token: str, doc_id: str) -> None:
    async with client(app, token, _CLAUDE_UA) as c:
        resp = await c.post(
            f"/sage_vaults/{VAULT}/metadata",
            json={"items": [{"document_id": doc_id, "title": f"Retitled {uuid.uuid4().hex[:6]}"}]},
        )
    assert resp.status_code == 200, resp.text


async def _search(app, surface: str, body: dict) -> dict:
    if surface == "rest":
        async with client(app, "svc") as c:
            resp = await c.post(f"/sage_vaults/{VAULT}/discover", json=body)
        assert resp.status_code == 200, resp.text
        return resp.json()
    async with mcp_running(app):
        return await mcp_call(app, "svc", "search", {"vault_id": VAULT, **body})


def _ids(response: dict) -> set[str]:
    return {hit["document"]["id"] for hit in response["results"]}


def _resolution(response: dict) -> dict | None:
    return (response.get("hints") or {}).get("provenance_resolution")


async def _renamed_alice(app, tmp_vault_dir) -> dict[str, str]:
    """Alice writes under her former name, bob writes, then alice is renamed elsewhere."""
    old = await _ingest(app, "alice-old", tmp_vault_dir, "old.md")
    other = await _ingest(app, "bob", tmp_vault_dir, "bob.md")
    renamed = await _ingest(app, "alice-new", tmp_vault_dir, "new.md")
    return {"old": old, "other": other, "renamed": renamed}


_SURFACES = pytest.mark.parametrize("surface", ["rest", "mcp"])


# --------------------------------------------------------------------------
# Store: latest display name per key, key tuples, provenance facets
# --------------------------------------------------------------------------


def _doc(doc_id: str, at: datetime, **fields) -> Document:
    base = {
        "id": doc_id,
        "title": f"Doc {doc_id}",
        "source_type": SourceType.MARKDOWN,
        "source_path": f"test/{doc_id}.md",
        "lifecycle_status": "active",
        "source_content_hash": "sha256:" + uuid.uuid5(uuid.NAMESPACE_OID, doc_id).hex * 2,
        "adapter_version": "0.1.0",
        "created_by": "testuser",
        "created_at": at,
        "last_modified_by": "testuser",
        "updated_at": at,
        "projected_at": at,
        "pipeline_status": PipelineStatus.ABSTRACTION_COMPLETE,
    }
    return Document(**{**base, **fields})


_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _at(minutes: int) -> datetime:
    return _T0 + timedelta(minutes=minutes)


async def test_t1_latest_principal_names_takes_the_newest_named_write(graph_store) -> None:
    await graph_store.insert_document(
        _doc(
            "0000abcd_d_a",
            _at(1),
            created_by=_ALICE,
            created_by_name="name-1",
            last_modified_by=_ALICE,
            last_modified_by_name="name-1",
        )
    )
    await graph_store.insert_document(
        _doc(
            "0000abcd_d_b",
            _at(2),
            created_by=_BOB,
            last_modified_by=_ALICE,
            last_modified_by_name="name-2",
            updated_at=_at(3),
        )
    )
    await graph_store.insert_document(
        _doc("0000abcd_d_s", _at(4), created_by=_SVC, last_modified_by=_SVC)
    )
    # An edge is a write too: its later snapshot wins over both documents.
    await graph_store.insert_edge(
        Edge(
            id=str(uuid.uuid4()),
            source_id="0000abcd_d_a",
            target_id="0000abcd_d_b",
            edge_type=EdgeType.REFERENCES,
            created_at=_at(5),
            created_by=_ALICE,
            created_by_name="name-3",
        )
    )

    names = await graph_store.latest_principal_names()

    assert names[_ALICE] == "name-3"
    assert names[_BOB] is None
    assert names[_SVC] is None


async def test_t2_an_unnamed_later_write_does_not_erase_a_name(graph_store) -> None:
    await graph_store.insert_document(
        _doc(
            "0000abcd_d_1",
            _at(1),
            created_by=_ALICE,
            created_by_name=_ALICE_OLD,
            last_modified_by=_ALICE,
            last_modified_by_name=_ALICE_OLD,
        )
    )
    await graph_store.insert_document(
        _doc("0000abcd_d_2", _at(9), created_by=_ALICE, last_modified_by=_ALICE)
    )

    assert (await graph_store.latest_principal_names())[_ALICE] == _ALICE_OLD


async def test_t3_the_provenance_predicate_accepts_a_key_tuple(graph_store) -> None:
    for doc_id, key in (("0000abcd_d_a", _ALICE), ("0000abcd_d_b", _BOB), ("0000abcd_d_s", _SVC)):
        await graph_store.insert_document(
            _doc(doc_id, _at(1), created_by=key, last_modified_by=key)
        )

    async def found(value) -> set[str]:
        docs, _ = await graph_store.query_documents(
            filters={"provenance": {"created_by": value}}, limit=100
        )
        return {d.id for d in docs}

    assert await found((_ALICE, _BOB)) == {"0000abcd_d_a", "0000abcd_d_b"}
    assert await found(_SVC) == {"0000abcd_d_s"}
    assert await found(("no-such-key",)) == set()


async def test_t4_provenance_facet_fields_count_keys_clients_and_agent_names(graph_store) -> None:
    agent = AssertedAgent(name="claude-code", source="parameter")
    await graph_store.insert_document(
        _doc(
            "0000abcd_d_1",
            _at(1),
            created_by=_ALICE,
            last_modified_by=_BOB,
            created_client="cas-app",
            last_modified_client="ci",
            created_agent=agent,
        )
    )
    await graph_store.insert_document(
        _doc(
            "0000abcd_d_2",
            _at(2),
            created_by=_ALICE,
            last_modified_by=_ALICE,
            created_client="cas-app",
            created_agent=agent,
        )
    )
    await graph_store.insert_document(
        _doc("0000abcd_d_3", _at(3), created_by=_BOB, last_modified_by=_BOB, created_client="ci")
    )

    facets, total = await graph_store.query_document_facets(fields=list(_PROVENANCE_FIELDS))

    assert total == 3
    assert facets["created_by"].values == {_ALICE: 2, _BOB: 1}
    assert facets["last_modified_by"].values == {_BOB: 2, _ALICE: 1}
    assert facets["created_client"].values == {"cas-app": 2, "ci": 1}
    assert facets["last_modified_client"].values == {"ci": 1}
    # Keyed by the agent's name, not its stored JSON; a null agent is not counted.
    assert facets["created_agent"].values == {"claude-code": 2}
    assert facets["last_modified_agent"].values == {}
    assert facets["last_modified_agent"].total_distinct == 0

    capped, _ = await graph_store.query_document_facets(fields=["created_by"], value_limit=1)
    assert capped["created_by"].values == {_ALICE: 2}
    assert capped["created_by"].total_distinct == 2


# --------------------------------------------------------------------------
# Service: name resolution on both surfaces
# --------------------------------------------------------------------------


@_SURFACES
@pytest.mark.parametrize(
    "search_mode",
    [
        {"mode": "catalog"},
        {"mode": "keyword", "query": "Body"},
        {"mode": "semantic", "query": "Body"},
    ],
    ids=["catalog", "keyword", "semantic"],
)
async def test_t5_a_latest_name_resolves_to_the_key_and_finds_former_name_writes(
    auth_app, tmp_vault_dir, surface, search_mode
) -> None:
    docs = await _renamed_alice(auth_app, tmp_vault_dir)

    body = {
        **search_mode,
        "limit": 100,
        "filters": {"provenance": {"created_by": "ALICE.new@example.org"}},
    }
    response = await _search(auth_app, surface, body)

    assert _ids(response) == {docs["old"], docs["renamed"]}
    resolution = _resolution(response)["created_by"]
    assert resolution["value"] == "ALICE.new@example.org"
    assert resolution["keys"] == [_ALICE]
    assert "ambiguous" not in resolution


@_SURFACES
async def test_t6_a_former_name_does_not_resolve(auth_app, tmp_vault_dir, surface) -> None:
    await _renamed_alice(auth_app, tmp_vault_dir)

    body = {"mode": "catalog", "filters": {"provenance": {"created_by": _ALICE_OLD}}}
    response = await _search(auth_app, surface, body)

    assert _ids(response) == set()
    resolution = _resolution(response)["created_by"]
    assert resolution["keys"] == []
    assert _ALICE_NEW in resolution["nearest_names"]


@_SURFACES
async def test_t7_an_ambiguous_name_matches_every_key(auth_app, tmp_vault_dir, surface) -> None:
    docs = await _renamed_alice(auth_app, tmp_vault_dir)
    twin = await _ingest(auth_app, "alice2", tmp_vault_dir, "twin.md")

    body = {"mode": "catalog", "limit": 100, "filters": {"provenance": {"created_by": _ALICE_NEW}}}
    response = await _search(auth_app, surface, body)

    assert _ids(response) == {docs["old"], docs["renamed"], twin}
    resolution = _resolution(response)["created_by"]
    assert resolution["keys"] == sorted([_ALICE, _ALICE2])
    assert resolution["ambiguous"] is True
    (warning,) = [w for w in response["hints"]["warnings"] if _ALICE_NEW in w]
    assert "Filter by one key" in warning


@pytest.mark.parametrize(
    "body",
    [
        {"mode": "catalog", "limit": 100},
        {"mode": "keyword", "query": "Body", "limit": 100},
        {"mode": "semantic", "query": "Body", "limit": 100},
        {"target": "facets"},
    ],
    ids=["catalog", "keyword", "semantic", "facets"],
)
async def test_t8_a_key_filters_exactly_and_reports_no_resolution(
    auth_app, tmp_vault_dir, body
) -> None:
    docs = await _renamed_alice(auth_app, tmp_vault_dir)

    response = await _search(
        auth_app, "rest", {**body, "filters": {"provenance": {"created_by": _ALICE}}}
    )

    assert _resolution(response) is None
    if body.get("target") == "facets":
        assert response["total_available"] == 2
    else:
        assert _ids(response) == {docs["old"], docs["renamed"]}


async def test_t8b_a_value_that_is_a_key_and_a_name_matches_both(auth_app, tmp_vault_dir) -> None:
    docs = await _renamed_alice(auth_app, tmp_vault_dir)
    spoof = await _ingest(auth_app, "mallory", tmp_vault_dir, "spoof.md")

    response = await _search(
        auth_app,
        "rest",
        {"mode": "catalog", "limit": 100, "filters": {"provenance": {"created_by": _BOB}}},
    )

    # The key is never displaced by a name: bob's own write is still found.
    assert _ids(response) == {docs["other"], spoof}
    resolution = _resolution(response)["created_by"]
    assert resolution["keys"] == sorted([_BOB, _MALLORY])
    assert resolution["ambiguous"] is True
    # The caller already filtered by one key, so the advisory explains the
    # widening rather than advising a narrowing it has already done.
    (warning,) = [w for w in response["hints"]["warnings"] if _BOB in w]
    assert _MALLORY in warning
    assert "Filter by one key" not in warning


@_SURFACES
async def test_t9_an_unknown_value_names_the_nearest_display_names(
    auth_app, tmp_vault_dir, surface
) -> None:
    await _renamed_alice(auth_app, tmp_vault_dir)

    body = {"mode": "catalog", "filters": {"provenance": {"created_by": "alice.nwe@example.org"}}}
    response = await _search(auth_app, surface, body)

    assert _ids(response) == set()
    resolution = _resolution(response)["created_by"]
    assert resolution["keys"] == []
    assert 1 <= len(resolution["nearest_names"]) <= 3
    assert resolution["nearest_names"][0] == _ALICE_NEW
    assert any("alice.nwe@example.org" in w for w in response["hints"]["warnings"])


async def test_t10_last_modified_by_resolves_and_client_fields_never_do(
    auth_app, tmp_vault_dir
) -> None:
    docs = await _renamed_alice(auth_app, tmp_vault_dir)
    await _retitle(auth_app, "alice-new", docs["other"])

    modified = await _search(
        auth_app,
        "rest",
        {
            "mode": "catalog",
            "limit": 100,
            "filters": {"provenance": {"last_modified_by": _ALICE_NEW}},
        },
    )
    assert _ids(modified) == {docs["old"], docs["renamed"], docs["other"]}
    assert _resolution(modified)["last_modified_by"]["keys"] == [_ALICE]

    by_client = await _search(
        auth_app,
        "rest",
        {"mode": "catalog", "filters": {"provenance": {"created_client": _ALICE_NEW}}},
    )
    assert _ids(by_client) == set()
    assert _resolution(by_client) is None


@_SURFACES
async def test_t11_a_name_narrows_the_facet_slice(auth_app, tmp_vault_dir, surface) -> None:
    await _renamed_alice(auth_app, tmp_vault_dir)

    body = {"target": "facets", "filters": {"provenance": {"created_by": _ALICE_NEW}}}
    response = await _search(auth_app, surface, body)

    assert response["total_available"] == 2
    assert _resolution(response)["created_by"]["keys"] == [_ALICE]


# --------------------------------------------------------------------------
# Facets: provenance fields, labels, opt-in
# --------------------------------------------------------------------------


@_SURFACES
async def test_t12_principal_facet_rows_label_each_key_with_its_latest_name(
    auth_app, tmp_vault_dir, surface
) -> None:
    docs = await _renamed_alice(auth_app, tmp_vault_dir)
    svc = await _ingest(auth_app, "svc", tmp_vault_dir, "svc.md")

    # Sliced to the documents written under alice's former name and by
    # principals with no name, so the latest name exists only outside it.
    body = {
        "target": "facets",
        "facet_fields": ["created_by", "last_modified_by", "created_agent"],
        "filters": {"document_ids": [docs["old"], docs["other"], svc]},
    }
    response = await _search(auth_app, surface, body)

    rows = {row["field"]: row for row in response["results"]}
    assert list(rows) == ["created_by", "last_modified_by", "created_agent"]
    assert rows["created_by"]["values"] == {_ALICE: 1, _BOB: 1, _SVC: 1}
    assert rows["created_by"]["labels"] == {_ALICE: _ALICE_NEW, _BOB: _BOB, _SVC: _SVC}
    assert rows["last_modified_by"]["labels"][_ALICE] == _ALICE_NEW
    assert rows["created_agent"]["values"] == {"claude-code": 3}
    assert "labels" not in rows["created_agent"]


@_SURFACES
async def test_t13_the_default_facet_set_is_unchanged(auth_app, tmp_vault_dir, surface) -> None:
    await _renamed_alice(auth_app, tmp_vault_dir)

    response = await _search(auth_app, surface, {"target": "facets"})

    assert [row["field"] for row in response["results"]] == _DEFAULT_FACET_FIELDS
    assert all(set(row) == {"field", "values", "total_distinct"} for row in response["results"])


async def test_t14_the_search_description_states_both_paths(auth_app) -> None:
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    async with mcp_running(auth_app), client(auth_app, "svc") as c:
        resp = await c.post("/mcp", json=request)

    tools = {t["name"]: t for t in decode_rpc_envelope(resp)["result"]["tools"]}
    description = tools["search"]["description"]
    assert "display name" in description
    assert "facet_fields" in description
    assert "created_by" in description
    assert "last_modified_by" in description
