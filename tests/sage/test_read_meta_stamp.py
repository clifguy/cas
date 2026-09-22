"""Read responses carry the answering build and a vault-config fingerprint.

Pins the stamp CAS-ADR-055 adds to the read-response carrier CAS-ADR-039
established: every read-path response names the server build that answered
it and a fingerprint of the vault configuration the answer was computed
under, so a caller comparing stamps across calls learns that the server or
its rules changed without re-reading the configuration.
"""

import asyncio
import copy
import inspect
import re

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from sage import build_info
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.app import _initialize_services, create_app
from sage.config import VaultConfig
from sage.models import schemas

_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")


@pytest.fixture
async def app(minimal_vault_config_dict, tmp_vault_dir):
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    await _initialize_services(
        app,
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    )
    test_dir = tmp_vault_dir / "sources" / "test"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "sample.md").write_text("# Sample Document\n\nSample content.")
    yield app
    await asyncio.sleep(0.5)
    for services in app.state.vault_registry.values():
        services.close_timing()
        await services.graph_store.close()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _ingest(client) -> str:
    resp = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample.md", "source_type": "markdown"},
    )
    assert resp.status_code == 201, resp.text
    await asyncio.sleep(0.5)
    return resp.json()["document"]["id"]


def _live_fingerprint(app) -> str:
    return app.state.vault_registry["test_vault"].config.fingerprint()


# ---------------------------------------------------------------------------
# The fingerprint itself
# ---------------------------------------------------------------------------


def test_fingerprint_is_deterministic_and_default_insensitive(minimal_vault_config_dict):
    """One effective configuration has one fingerprint, however it was spelled."""
    base = VaultConfig.model_validate(minimal_vault_config_dict)
    again = VaultConfig.model_validate(copy.deepcopy(minimal_vault_config_dict))

    explicit_default = copy.deepcopy(minimal_vault_config_dict)
    explicit_default["adapter_defaults"] = {}
    assert "adapter_defaults" not in minimal_vault_config_dict
    stated = VaultConfig.model_validate(explicit_default)

    reordered = VaultConfig.model_validate(dict(reversed(list(minimal_vault_config_dict.items()))))

    fingerprints = {c.fingerprint() for c in (base, again, stated, reordered)}
    assert len(fingerprints) == 1
    assert _FINGERPRINT.match(fingerprints.pop())


def _add_transition(d: dict) -> None:
    d["lifecycle"]["transitions"].append(
        {"from_state": "completed", "action": "reactivate", "to_state": "active"}
    )


def _set_satisfies_dependency(d: dict) -> None:
    d["lifecycle"]["states"][0]["satisfies_dependency"] = False


def _add_doc_type(d: dict) -> None:
    d["document_types"]["doc_types"].append({"value": "report", "label": "Report"})


def _edit_abstraction(d: dict) -> None:
    # A section no read consults today: pins whole-config coverage, so a
    # fingerprint over a curated read-section subset fails here.
    d["abstraction"]["retry_backoff_base_seconds"] = 1.5


@pytest.mark.parametrize(
    "edit",
    [_add_transition, _set_satisfies_dependency, _add_doc_type, _edit_abstraction],
    ids=["lifecycle-transition", "dependency-satisfying-rule", "document-type", "abstraction"],
)
def test_fingerprint_changes_on_read_rule_edit(minimal_vault_config_dict, edit):
    """An edit to any configured rule moves the fingerprint, read-consulted or not."""
    baseline = VaultConfig.model_validate(minimal_vault_config_dict).fingerprint()
    edited = copy.deepcopy(minimal_vault_config_dict)
    edit(edited)
    assert VaultConfig.model_validate(edited).fingerprint() != baseline


# ---------------------------------------------------------------------------
# Every read-path response carries the stamp
# ---------------------------------------------------------------------------


def _read_models() -> set[str]:
    """Every response model that carries ``read_meta``, bar the error envelope."""
    found = set()
    for name, obj in inspect.getmembers(schemas, inspect.isclass):
        if (
            issubclass(obj, BaseModel)
            and obj.__module__ == schemas.__name__
            and "read_meta" in obj.model_fields
            and name != "ErrorResponse"
        ):
            found.add(name)
    return found


_DISCOVER = "/sage_vaults/test_vault/discover"

# model name -> list of (case id, method, path template, json body)
_READ_CALLS: dict[str, list[tuple[str, str, str, dict | None]]] = {
    "DocumentWithContent": [
        ("get_document", "GET", "/sage_vaults/test_vault/documents/{doc}", None),
    ],
    "ReadProjectionResponse": [
        ("read_projection", "GET", "/sage_vaults/test_vault/documents/{doc}/projection", None),
    ],
    "ReadSectionResponse": [
        (
            "read_section",
            "GET",
            "/sage_vaults/test_vault/documents/{doc}/section/Sample Document",
            None,
        ),
    ],
    "DiscoverResponse": [
        ("semantic", "POST", _DISCOVER, {"mode": "semantic", "query": "sample"}),
        ("keyword", "POST", _DISCOVER, {"mode": "keyword", "query": "sample"}),
        ("catalog", "POST", _DISCOVER, {"mode": "catalog"}),
        (
            "catalog-edges",
            "POST",
            _DISCOVER,
            {"mode": "catalog", "target": "edges"},
        ),
        (
            "catalog-facets",
            "POST",
            _DISCOVER,
            {"mode": "catalog", "target": "facets"},
        ),
        (
            "deterministic",
            "POST",
            _DISCOVER,
            {"mode": "deterministic", "document_id": "{doc}", "heading_path": "Sample Document"},
        ),
    ],
}


def test_every_read_model_has_a_stamp_case():
    """A read model added later cannot ship without a case below."""
    assert _read_models() == set(_READ_CALLS)


_CASES = [
    pytest.param(method, path, body, id=f"{model}:{case}")
    for model, calls in _READ_CALLS.items()
    for case, method, path, body in calls
]


def _fill(value, doc_id: str):
    if isinstance(value, str):
        return value.replace("{doc}", doc_id)
    if isinstance(value, dict):
        return {k: _fill(v, doc_id) for k, v in value.items()}
    return value


@pytest.mark.parametrize(("method", "path", "body"), _CASES)
async def test_every_read_response_is_stamped(app, client, method, path, body):
    doc_id = await _ingest(client)
    resp = await client.request(method, _fill(path, doc_id), json=_fill(body, doc_id))
    assert resp.status_code == 200, resp.text
    read_meta = resp.json()["read_meta"]
    assert read_meta["server_build"] == build_info.VERSION_WITH_BUILD
    assert read_meta["vault_config_fingerprint"] == _live_fingerprint(app)
    assert _FINGERPRINT.match(read_meta["vault_config_fingerprint"])


async def test_error_envelope_carries_build_and_omits_fingerprint(client):
    """An answer not computed under a vault's rules names no fingerprint."""
    resp = await client.get("/sage_vaults/no_such_vault/documents/abc")
    assert resp.status_code == 404
    read_meta = resp.json()["read_meta"]
    assert read_meta["success"] is False
    assert read_meta["server_build"] == build_info.VERSION_WITH_BUILD
    assert "vault_config_fingerprint" not in read_meta


# ---------------------------------------------------------------------------
# The stamp follows the configuration the answer was computed under
# ---------------------------------------------------------------------------


async def _read_fingerprint(client, doc_id: str) -> str:
    resp = await client.get(f"/sage_vaults/test_vault/documents/{doc_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()["read_meta"]["vault_config_fingerprint"]


async def test_noop_config_write_leaves_fingerprint_unchanged(app, client):
    doc_id = await _ingest(client)
    before = await _read_fingerprint(client, doc_id)
    config_before = app.state.vault_registry["test_vault"].config

    current = (await client.get("/sage_vaults/test_vault/config")).json()
    rewrite = await client.put(
        "/sage_vaults/test_vault/config", json={"lifecycle": current["lifecycle"]}
    )
    assert rewrite.status_code == 200, rewrite.text
    # The write really reloaded: the vault now answers from a new config object.
    assert app.state.vault_registry["test_vault"].config is not config_before
    assert await _read_fingerprint(client, doc_id) == before

    # Positive control: a real rule edit through the same path moves it.
    lifecycle = copy.deepcopy(current["lifecycle"])
    lifecycle["transitions"].append(
        {"from_state": "completed", "action": "reactivate", "to_state": "active"}
    )
    edit = await client.put("/sage_vaults/test_vault/config", json={"lifecycle": lifecycle})
    assert edit.status_code == 200, edit.text
    assert await _read_fingerprint(client, doc_id) != before


# ---------------------------------------------------------------------------
# The build matches the one advertised at the handshake
# ---------------------------------------------------------------------------


async def test_build_identity_matches_startup_handshake(app, client, tool_payload):
    doc_id = await _ingest(client)
    server = app.state.mcp_mounts["/mcp"]
    opts = server._mcp_server.create_initialization_options()

    payload = tool_payload(
        await server.call_tool("get_document", {"vault_id": "test_vault", "document_id": doc_id})
    )

    assert "error" not in payload, payload
    assert payload["read_meta"]["server_build"] == opts.server_version
    assert payload["read_meta"]["server_build"] in (opts.instructions or "")
    assert payload["read_meta"]["vault_config_fingerprint"] == _live_fingerprint(app)


# ---------------------------------------------------------------------------
# The byte budget measures the stamped response
# ---------------------------------------------------------------------------


async def test_search_budget_measures_stamped_response(app, client, monkeypatch):
    """Budget sizing sees the stamp, so a response fitted to the budget still fits."""
    from sage.services import retrieval

    await _ingest(client)
    measured: list[str | None] = []
    original = retrieval._serialized_response_bytes

    def spy(response):
        measured.append(response.read_meta.vault_config_fingerprint)
        return original(response)

    monkeypatch.setattr(retrieval, "_serialized_response_bytes", spy)
    monkeypatch.setattr(retrieval, "_resolve_mcp_inline_budget_bytes", lambda: 1)

    resp = await client.post("/sage_vaults/test_vault/discover", json={"mode": "catalog"})

    assert resp.status_code == 200, resp.text
    assert resp.json().get("hints"), "the budget policy did not engage"
    assert measured, "the budget policy measured nothing"
    assert all(fp == _live_fingerprint(app) for fp in measured), measured
