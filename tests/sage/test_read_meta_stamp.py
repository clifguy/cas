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
from pydantic import BaseModel, ValidationError

from sage import build_info
from sage._tool_naming import SERVER_ASSIGNMENT
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.app import _initialize_services, create_app
from sage.config import VaultConfig
from sage.models import schemas
from tests.helpers.pipeline_wait import await_tool_idle

_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")


@pytest.fixture
async def app(minimal_vault_config_dict, tmp_vault_dir):
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    # One store behind a factory, so projections survive a config reload.
    content_store = StubContentStore()
    await _initialize_services(
        app,
        config,
        content_store_factory=lambda _brain_root: content_store,
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    )
    test_dir = tmp_vault_dir / "sources" / "test"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "sample.md").write_text("# Sample Document\n\nSample content.")
    (test_dir / "dependency.md").write_text("# Dependency\n\nDependency content.")
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


async def _ingest(app, client, source: str = "test/sample.md") -> str:
    """Ingest a source and wait until every read path can serve it."""
    resp = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": source, "source_type": "markdown"},
    )
    assert resp.status_code == 201, resp.text
    doc_id = resp.json()["document"]["id"]

    async def fetch():
        return (await client.get(f"/sage_vaults/test_vault/documents/{doc_id}")).json()

    await await_tool_idle(
        fetch, doc_id, service=app.state.vault_registry["test_vault"].ingestion_service
    )
    return doc_id


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
    "PreconditionResult": [
        ("verify_preconditions", "GET", "/sage_vaults/test_vault/preconditions/{doc}", None),
    ],
    "TraverseResponse": [
        ("traverse", "POST", "/sage_vaults/test_vault/traverse", {"start_id": "{doc}"}),
    ],
    "ChainResponse": [
        ("chain", "POST", "/sage_vaults/test_vault/chain", {"document_id": "{doc}"}),
    ],
    "ListHeadingsResponse": [
        ("list_headings", "GET", "/sage_vaults/test_vault/documents/{doc}/headings", None),
    ],
    "PendingMetadataPage": [
        ("list_pending_metadata", "GET", "/sage_vaults/test_vault/pending-metadata", None),
    ],
    "ParseFilenameResponse": [
        (
            "get_filename_metadata",
            "POST",
            "/sage_vaults/test_vault/parse-filename",
            {"filename": "sample.md", "source_type": "markdown"},
        ),
    ],
    "StagingEdgeListResponse": [
        ("list_staging_edges", "GET", "/sage_vaults/test_vault/staging-edges", None),
    ],
    "HashCheckResponse": [
        (
            "verify_hashes",
            "POST",
            "/sage_vaults/test_vault/hash-check",
            {"hashes": ["sha256:" + "0" * 64]},
        ),
        # The empty-input short-circuit answers without the store, and is
        # stamped all the same.
        ("verify_hashes-empty", "POST", "/sage_vaults/test_vault/hash-check", {"hashes": []}),
    ],
}


# Read models answered without reference to any one vault's configuration.
# They carry the build and omit the fingerprint (CAS-ADR-055).
_BUILD_ONLY_CALLS: dict[str, list[tuple[str, str, str, dict | None]]] = {
    "VaultListResponse": [("list_vaults", "GET", "/sage_vaults", None)],
}


def test_every_read_model_has_a_stamp_case():
    """A read model added later cannot ship without a case below."""
    assert _read_models() == set(_READ_CALLS) | set(_BUILD_ONLY_CALLS)


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
    doc_id = await _ingest(app, client)
    resp = await client.request(method, _fill(path, doc_id), json=_fill(body, doc_id))
    assert resp.status_code == 200, resp.text
    read_meta = resp.json()["read_meta"]
    assert read_meta["server_build"] == build_info.VERSION_WITH_BUILD
    assert read_meta["vault_config_fingerprint"] == _live_fingerprint(app)
    assert _FINGERPRINT.match(read_meta["vault_config_fingerprint"])


_BUILD_ONLY_CASES = [
    pytest.param(method, path, body, id=f"{model}:{case}")
    for model, calls in _BUILD_ONLY_CALLS.items()
    for case, method, path, body in calls
]


@pytest.mark.parametrize(("method", "path", "body"), _BUILD_ONLY_CASES)
async def test_build_only_read_response_omits_fingerprint(app, client, method, path, body):
    """A read spanning vaults names the build and no one vault's configuration."""
    resp = await client.request(method, path, json=body)
    assert resp.status_code == 200, resp.text
    read_meta = resp.json()["read_meta"]
    assert read_meta["success"] is True
    assert read_meta["server_build"] == build_info.VERSION_WITH_BUILD
    assert "vault_config_fingerprint" not in read_meta


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


async def _read_fingerprints(client, doc_id: str) -> dict[str, str]:
    """The fingerprint every read path reports, keyed by its stamp case."""
    seen = {}
    for model, calls in _READ_CALLS.items():
        for case, method, path, body in calls:
            resp = await client.request(method, _fill(path, doc_id), json=_fill(body, doc_id))
            assert resp.status_code == 200, resp.text
            seen[f"{model}:{case}"] = resp.json()["read_meta"]["vault_config_fingerprint"]
    return seen


async def test_noop_config_write_leaves_fingerprint_unchanged(app, client):
    """Every read path follows the configuration through a reload, not only one."""
    doc_id = await _ingest(app, client)
    before = _live_fingerprint(app)
    assert set((await _read_fingerprints(client, doc_id)).values()) == {before}
    config_before = app.state.vault_registry["test_vault"].config

    current = (await client.get("/sage_vaults/test_vault/config")).json()
    rewrite = await client.put(
        "/sage_vaults/test_vault/config", json={"lifecycle": current["lifecycle"]}
    )
    assert rewrite.status_code == 200, rewrite.text
    # The write really reloaded: the vault now answers from a new config object.
    assert app.state.vault_registry["test_vault"].config is not config_before
    assert set((await _read_fingerprints(client, doc_id)).values()) == {before}

    # Positive control: a real rule edit through the same path moves it on every read path.
    lifecycle = copy.deepcopy(current["lifecycle"])
    lifecycle["transitions"].append(
        {"from_state": "completed", "action": "reactivate", "to_state": "active"}
    )
    edit = await client.put("/sage_vaults/test_vault/config", json={"lifecycle": lifecycle})
    assert edit.status_code == 200, edit.text
    after = _live_fingerprint(app)
    assert after != before
    assert set((await _read_fingerprints(client, doc_id)).values()) == {after}


async def _link_depends_on(client, source_id: str, target_id: str) -> None:
    resp = await client.post(
        "/sage_vaults/test_vault/edges",
        json={
            "items": [
                {
                    "source_id": source_id,
                    "target_id": target_id,
                    "edge_type": "depends_on",
                    "source_valid_from_version": source_id,
                    "target_valid_from_version": target_id,
                }
            ]
        },
    )
    assert resp.json()["success_count"] == 1, resp.text


async def test_preconditions_fingerprint_tracks_satisfies_dependency(app, client):
    """The rule behind ``required`` moves the fingerprint; a no-op write does not."""
    doc_id = await _ingest(app, client)
    dep_id = await _ingest(app, client, "test/dependency.md")
    await _link_depends_on(client, doc_id, dep_id)

    async def check() -> dict:
        resp = await client.get(f"/sage_vaults/test_vault/preconditions/{doc_id}")
        assert resp.status_code == 200, resp.text
        return resp.json()

    before = await check()
    assert before["satisfied"] is True
    assert [c["target_id"] for c in before["checks"]] == [dep_id]
    assert before["read_meta"]["vault_config_fingerprint"] == _live_fingerprint(app)
    config_before = app.state.vault_registry["test_vault"].config

    current = (await client.get("/sage_vaults/test_vault/config")).json()
    rewrite = await client.put(
        "/sage_vaults/test_vault/config", json={"lifecycle": current["lifecycle"]}
    )
    assert rewrite.status_code == 200, rewrite.text
    assert app.state.vault_registry["test_vault"].config is not config_before
    assert await check() == before

    # Opt ``active`` out of satisfying a dependency: the same call now means
    # something else, and the stamp says so.
    lifecycle = copy.deepcopy(current["lifecycle"])
    (active,) = [s for s in lifecycle["states"] if s["value"] == "active"]
    active["satisfies_dependency"] = False
    edit = await client.put("/sage_vaults/test_vault/config", json={"lifecycle": lifecycle})
    assert edit.status_code == 200, edit.text

    after = await check()
    assert after["satisfied"] is False
    assert after["read_meta"]["vault_config_fingerprint"] == _live_fingerprint(app)
    assert (
        after["read_meta"]["vault_config_fingerprint"]
        != before["read_meta"]["vault_config_fingerprint"]
    )


# ---------------------------------------------------------------------------
# Every read tool on the ordinary MCP surface carries the stamp
# ---------------------------------------------------------------------------

# Ordinary read tools answered without reference to any one vault's
# configuration, with the reason. They carry the build only.
_MCP_NOT_VAULT_SCOPED: dict[str, str] = {
    "list_vaults": "enumerates every registered vault rather than reading one",
}

# tool name -> arguments; "{doc}" and "{dir}" are filled per test.
_MCP_READ_CALLS: dict[str, dict] = {
    "list_vaults": {},
    "search": {"mode": "keyword", "query": "sample"},
    "get_document": {"document_id": "{doc}"},
    "read_section": {"document_id": "{doc}", "heading_path": "Sample Document"},
    "read_projection": {"document_id": "{doc}"},
    "list_headings": {"document_id": "{doc}"},
    "traverse": {"start_id": "{doc}"},
    "chain": {"document_id": "{doc}"},
    "list_staging_edges": {},
    "verify_preconditions": {"document_id": "{doc}"},
    "verify_hashes": {"hashes": ["sha256:" + "0" * 64]},
    "list_pending_metadata": {},
    "get_filename_metadata": {"filename": "sample.md", "source_type": "markdown"},
    "list_directory": {"directory": "{dir}"},
}


async def _ordinary_read_tools(server) -> set[str]:
    """Read tools the ordinary surface registers, per the assignment table."""
    return {
        tool.name
        for tool in await server.list_tools()
        if SERVER_ASSIGNMENT.get(tool.name) == "sage"
        and tool.annotations is not None
        and tool.annotations.readOnlyHint is True
    }


async def test_every_ordinary_read_tool_has_a_stamp_case(app):
    """A read tool added to the ordinary surface cannot ship without a case."""
    server = app.state.mcp_mounts["/mcp"]
    assert await _ordinary_read_tools(server) == set(_MCP_READ_CALLS)
    assert set(_MCP_NOT_VAULT_SCOPED) <= set(_MCP_READ_CALLS)


@pytest.mark.parametrize("tool", sorted(_MCP_READ_CALLS))
async def test_every_ordinary_read_tool_is_stamped(app, client, tool_payload, tmp_path, tool):
    doc_id = await _ingest(app, client)
    args = {
        k: (v.replace("{doc}", doc_id).replace("{dir}", str(tmp_path)) if isinstance(v, str) else v)
        for k, v in _MCP_READ_CALLS[tool].items()
    }
    if tool not in _MCP_NOT_VAULT_SCOPED:
        args["vault_id"] = "test_vault"
    server = app.state.mcp_mounts["/mcp"]

    payload = tool_payload(await server.call_tool(tool, args))

    # An error envelope carries the build too, so a refusal must not pass here.
    assert "error" not in payload, payload
    read_meta = payload["read_meta"]
    assert read_meta["success"] is True
    assert read_meta["server_build"] == build_info.VERSION_WITH_BUILD
    if tool in _MCP_NOT_VAULT_SCOPED:
        assert "vault_config_fingerprint" not in read_meta
    else:
        assert read_meta["vault_config_fingerprint"] == _live_fingerprint(app)


# ---------------------------------------------------------------------------
# The build matches the one advertised at the handshake
# ---------------------------------------------------------------------------


async def test_build_identity_matches_startup_handshake(app, client, tool_payload):
    doc_id = await _ingest(app, client)
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

    await _ingest(app, client)
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
    # The fitted response delivered is the stamped one that was measured.
    assert resp.json()["read_meta"]["vault_config_fingerprint"] == _live_fingerprint(app)


def test_stamped_enforces_the_fingerprint_shape():
    """The stamp goes through validation, so a malformed fingerprint is refused."""
    meta = schemas.ReadMeta(success=True, body_present=False)
    with pytest.raises(ValidationError):
        meta.stamped("garbage")
    good = "sha256:" + "a" * 64
    assert meta.stamped(good).vault_config_fingerprint == good
