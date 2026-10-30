"""A declared tier3 ``unique_keys`` constraint is enforced or visibly refused.

Coverage for CAS-ADR-031 activation:

* Declaring a ``unique_keys`` entry through ``update_vault_config`` or
  ``create_vault`` builds its index in the same operation, so a duplicate
  ingest is refused without a separate ``migrate_vault`` call.
* A declaration whose portfolio already collides is refused by the config
  write (``tier3_unique_activation_blocked``), on the dry run as on the real
  run, and nothing is persisted.
* Loading or reloading a vault activates its clean declarations, however the
  configuration arrived.
* A declaration whose index is absent refuses every write it governs with
  ``tier3_unique_constraint_inactive``; writes it does not govern proceed.
* Withdrawing a declaration drops its index.
* The ingest dry run reports the active subset beside the declared one.
* A tier3 update that collides reports ``tier3_unique_constraint_violation``.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from sage.app import _initialize_services, create_app
from sage.config import VaultConfig
from sage.services.vault_registry import VaultRegistryService
from sage.vault_management import bound_vault_root
from tests.helpers.pipeline_wait import drain_vaults

VAULT = "test_vault"

TICKET_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ticket_id": {"type": "string", "pattern": r"^T-\d{4}$"},
        "ticket_priority": {"type": "string", "enum": ["high", "medium", "low"]},
    },
}


def _doc_types(*, unique: bool) -> dict:
    ticket: dict = {"value": "ticket", "label": "Ticket", "metadata_schema": TICKET_SCHEMA}
    if unique:
        ticket["unique_keys"] = ["ticket_id"]
    return {
        "doc_types": [
            {"value": "note", "label": "Note"},
            {"value": "memo", "label": "Memo"},
            # Shares the ticket schema without declaring it unique, so a
            # record can carry ticket_id before being retyped to ticket.
            {"value": "draft", "label": "Draft", "metadata_schema": TICKET_SCHEMA},
            ticket,
        ]
    }


@pytest.fixture
def base_config_dict(minimal_vault_config_dict) -> dict:
    cfg = copy.deepcopy(minimal_vault_config_dict)
    cfg["document_types"] = _doc_types(unique=False)
    return cfg


@pytest.fixture
async def app(base_config_dict):
    config = VaultConfig.model_validate(base_config_dict)
    app = create_app(config=config)
    await _initialize_services(app, config)
    yield app
    try:
        await drain_vaults(app.state.vault_registry)
    finally:
        for services in app.state.vault_registry.values():
            services.close_timing()
            await services.close_storage()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _seed(tmp_vault_dir: Path, name: str, body: str | None = None) -> str:
    full = tmp_vault_dir / "sources" / name
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body or f"# {name}\n\nBody of {name}.")
    return name


async def _ingest(
    client,
    tmp_vault_dir: Path,
    name: str,
    ticket_id: str | None,
    *,
    predecessor_id: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    doc_type: str | None = None,
    vault: str = VAULT,
    seed_root: Path | None = None,
):
    _seed(seed_root or tmp_vault_dir, name)
    payload: dict = {
        "source": name,
        "source_type": "markdown",
        "metadata": {"doc_type": doc_type or ("ticket" if ticket_id is not None else "note")},
    }
    if force:
        payload["force"] = True
    if ticket_id is not None:
        payload["tier3_metadata"] = {"ticket_id": ticket_id, "ticket_priority": "medium"}
    if predecessor_id is not None:
        payload["predecessor_id"] = predecessor_id
    if dry_run:
        payload["dry_run"] = True
    return await client.post(f"/sage_vaults/{vault}/documents", json=payload)


async def _ingest_ok(client, tmp_vault_dir, name, ticket_id, **kwargs) -> str:
    resp = await _ingest(client, tmp_vault_dir, name, ticket_id, **kwargs)
    assert resp.status_code == 201, resp.text
    return resp.json()["document"]["id"]


async def _put_doc_types(client, doc_types: dict, *, dry_run: bool = False):
    body: dict = {"document_types": doc_types}
    if dry_run:
        body["dry_run"] = True
    return await client.put(f"/sage_vaults/{VAULT}/config", json=body)


async def _index_exists(app, field: str = "ticket_id", vault: str = VAULT) -> bool:
    services = app.state.vault_registry[vault]
    return await services.graph_store.tier3_unique_index_exists("ticket", field)


def _declared_unique_keys(config_body: dict) -> list | None:
    ticket = next(
        dt for dt in config_body["document_types"]["doc_types"] if dt["value"] == "ticket"
    )
    return ticket.get("unique_keys")


async def _reload_with(app, config_dict: dict) -> None:
    """Load a declaration that did not pass through the config write tools."""
    registry: VaultRegistryService = app.state.vault_registry_service
    await registry.reload(VAULT, VaultConfig.model_validate(config_dict))


# ---------------------------------------------------------------------------
# Activation on config writes
# ---------------------------------------------------------------------------


async def test_update_config_declaration_enforces_without_migrate(app, client, tmp_vault_dir):
    """Adding ``unique_keys`` through the config write makes the very next
    duplicate ingest fail, with no ``migrate_vault`` call between."""
    resp = await _put_doc_types(client, _doc_types(unique=True))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {"doc_type": "ticket", "field": "ticket_id"} in body["tier3_uniqueness_activations"]
    assert body["tier3_uniqueness_collisions"] == []
    assert await _index_exists(app)

    await _ingest_ok(client, tmp_vault_dir, "a.md", "T-0001")
    second = await _ingest(client, tmp_vault_dir, "b.md", "T-0001")
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "tier3_unique_constraint_violation"


async def test_without_declaration_duplicates_land(app, client, tmp_vault_dir):
    """Control: with no declaration the same pair of ingests both land."""
    await _ingest_ok(client, tmp_vault_dir, "a.md", "T-0001")
    await _ingest_ok(client, tmp_vault_dir, "b.md", "T-0001")
    assert not await _index_exists(app)


async def test_create_vault_declaration_enforces_without_migrate(app, client):
    config = VaultRegistryService.get_default_config("uniq_vault", "Uniq Vault", "testuser")
    root = bound_vault_root()
    sources = root / "uniq_vault" / "sources"
    config["vault"]["storage_root"] = str(sources)
    config["vault"]["brain_root"] = str(root / "uniq_vault" / "brain")
    config["document_types"]["doc_types"].append(
        {
            "value": "ticket",
            "label": "Ticket",
            "metadata_schema": TICKET_SCHEMA,
            "unique_keys": ["ticket_id"],
        }
    )
    resp = await client.post("/sage_vaults", json={"config": config})
    assert resp.status_code == 201, resp.text
    assert await _index_exists(app, vault="uniq_vault")

    first = await _ingest(
        client, sources, "a.md", "T-0001", vault="uniq_vault", seed_root=sources.parent
    )
    assert first.status_code == 201, first.text
    second = await _ingest(
        client, sources, "b.md", "T-0001", vault="uniq_vault", seed_root=sources.parent
    )
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "tier3_unique_constraint_violation"


async def test_update_config_refused_when_portfolio_collides(app, client, tmp_vault_dir):
    a = await _ingest_ok(client, tmp_vault_dir, "a.md", "T-0001")
    b = await _ingest_ok(client, tmp_vault_dir, "b.md", "T-0001")

    for dry_run in (True, False):
        resp = await _put_doc_types(client, _doc_types(unique=True), dry_run=dry_run)
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["code"] == "tier3_unique_activation_blocked"
        (collision,) = body["detail"]["collisions"]
        assert collision["doc_type"] == "ticket"
        assert collision["field"] == "ticket_id"
        assert collision["value"] == "T-0001"
        assert collision["document_ids"] == sorted([a, b])

    current = (await client.get(f"/sage_vaults/{VAULT}/config")).json()
    assert _declared_unique_keys(current) is None
    assert not await _index_exists(app)


async def test_archived_chain_head_still_collides(app, client, tmp_vault_dir):
    a = await _ingest_ok(client, tmp_vault_dir, "a.md", "T-0001")
    resp = await client.post(
        f"/sage_vaults/{VAULT}/lifecycles",
        json={"items": [{"document_id": a, "action": "archive"}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"][0]["status"] == "success", resp.text
    await _ingest_ok(client, tmp_vault_dir, "b.md", "T-0001")

    resp = await _put_doc_types(client, _doc_types(unique=True))
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "tier3_unique_activation_blocked"


async def test_superseded_predecessor_does_not_collide(app, client, tmp_vault_dir):
    a = await _ingest_ok(client, tmp_vault_dir, "a.md", "T-0001")
    await _ingest_ok(client, tmp_vault_dir, "a2.md", "T-0001", predecessor_id=a)

    resp = await _put_doc_types(client, _doc_types(unique=True))
    assert resp.status_code == 200, resp.text
    assert await _index_exists(app)


async def test_withdrawn_declaration_drops_its_index(app, client, tmp_vault_dir):
    assert (await _put_doc_types(client, _doc_types(unique=True))).status_code == 200
    assert await _index_exists(app)

    resp = await _put_doc_types(client, _doc_types(unique=False))
    assert resp.status_code == 200, resp.text
    assert not await _index_exists(app)
    await _ingest_ok(client, tmp_vault_dir, "a.md", "T-0001")
    await _ingest_ok(client, tmp_vault_dir, "b.md", "T-0001")


# ---------------------------------------------------------------------------
# Activation at load
# ---------------------------------------------------------------------------


async def test_reload_activates_clean_declaration(app, client, base_config_dict, tmp_vault_dir):
    await _ingest_ok(client, tmp_vault_dir, "a.md", "T-0001")
    declared = copy.deepcopy(base_config_dict)
    declared["document_types"] = _doc_types(unique=True)

    await _reload_with(app, declared)

    assert await _index_exists(app)
    dup = await _ingest(client, tmp_vault_dir, "b.md", "T-0001")
    assert dup.status_code == 409, dup.text
    assert dup.json()["code"] == "tier3_unique_constraint_violation"


# ---------------------------------------------------------------------------
# Writes governed by an inactive declaration are refused
# ---------------------------------------------------------------------------


@pytest.fixture
async def inactive_vault(app, client, base_config_dict, tmp_vault_dir):
    """A vault whose ticket_id declaration arrived at load over a colliding
    portfolio, so its index could not be built."""
    a = await _ingest_ok(client, tmp_vault_dir, "a.md", "T-0001")
    b = await _ingest_ok(client, tmp_vault_dir, "b.md", "T-0001")
    declared = copy.deepcopy(base_config_dict)
    declared["document_types"] = _doc_types(unique=True)
    await _reload_with(app, declared)
    assert not await _index_exists(app)
    return {"a": a, "b": b, "config": declared}


def _assert_inactive(resp) -> None:
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["code"] == "tier3_unique_constraint_inactive", body
    assert body["detail"] == {"doc_type": "ticket", "field": "ticket_id"}


async def test_inactive_declaration_refuses_ingest(inactive_vault, client, tmp_vault_dir):
    _assert_inactive(await _ingest(client, tmp_vault_dir, "c.md", "T-0002"))


async def test_inactive_declaration_refuses_supersession(inactive_vault, client, tmp_vault_dir):
    _assert_inactive(
        await _ingest(client, tmp_vault_dir, "a2.md", "T-0001", predecessor_id=inactive_vault["a"])
    )


async def test_inactive_declaration_refuses_ingest_dry_run(inactive_vault, client, tmp_vault_dir):
    _assert_inactive(await _ingest(client, tmp_vault_dir, "c.md", "T-0002", dry_run=True))


async def test_inactive_declaration_refuses_unique_field_update(inactive_vault, client):
    resp = await client.post(
        f"/sage_vaults/{VAULT}/metadata",
        json={
            "items": [
                {
                    "document_id": inactive_vault["a"],
                    "tier3_metadata": {"set": {"ticket_id": "T-0009"}},
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    (result,) = resp.json()["results"]
    assert result["status"] == "error", result
    assert result["error"]["error"] == "tier3_unique_constraint_inactive"
    assert result["error"]["detail"] == {"doc_type": "ticket", "field": "ticket_id"}


async def test_inactive_declaration_leaves_ungoverned_writes_alone(
    inactive_vault, client, tmp_vault_dir
):
    """Control: a write that neither creates a row of the doc_type nor
    touches the declared field is not governed by it."""
    await _ingest_ok(client, tmp_vault_dir, "n.md", None)
    resp = await client.post(
        f"/sage_vaults/{VAULT}/metadata",
        json={
            "items": [
                {
                    "document_id": inactive_vault["a"],
                    "tier3_metadata": {"set": {"ticket_priority": "high"}},
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"][0]["status"] == "success", resp.text


async def test_inactive_pair_does_not_block_unrelated_config_edit(inactive_vault, client):
    doc_types = _doc_types(unique=True)
    doc_types["doc_types"].append({"value": "report", "label": "Report"})
    resp = await _put_doc_types(client, doc_types)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    (collision,) = body["tier3_uniqueness_collisions"]
    assert collision["field"] == "ticket_id"
    assert collision["document_ids"] == sorted([inactive_vault["a"], inactive_vault["b"]])


# ---------------------------------------------------------------------------
# Dry run reports the active subset
# ---------------------------------------------------------------------------


async def test_dry_run_reports_active_unique_fields(app, client, tmp_vault_dir):
    assert (await _put_doc_types(client, _doc_types(unique=True))).status_code == 200
    resp = await _ingest(client, tmp_vault_dir, "a.md", "T-0001", dry_run=True)
    assert resp.status_code == 200, resp.text
    requirements = resp.json()["requirements"]
    assert requirements["unique_tier3_fields"] == ["ticket_id"]
    assert requirements["active_unique_tier3_fields"] == ["ticket_id"]


async def test_dry_run_reports_inactive_field_as_not_active(inactive_vault, client, tmp_vault_dir):
    """A preview whose payload does not carry the declared field is not
    refused, and reports the declared field as inactive."""
    _seed(tmp_vault_dir, "p.md")
    resp = await client.post(
        f"/sage_vaults/{VAULT}/documents",
        json={
            "source": "p.md",
            "source_type": "markdown",
            "metadata": {"doc_type": "ticket"},
            "tier3_metadata": {"ticket_priority": "low"},
            "dry_run": True,
        },
    )
    assert resp.status_code == 200, resp.text
    requirements = resp.json()["requirements"]
    assert requirements["unique_tier3_fields"] == ["ticket_id"]
    assert requirements["active_unique_tier3_fields"] == []


# ---------------------------------------------------------------------------
# A colliding tier3 update reports the typed violation
# ---------------------------------------------------------------------------


async def test_colliding_update_reports_typed_violation(app, client, tmp_vault_dir):
    assert (await _put_doc_types(client, _doc_types(unique=True))).status_code == 200
    a = await _ingest_ok(client, tmp_vault_dir, "a.md", "T-0001")
    b = await _ingest_ok(client, tmp_vault_dir, "b.md", "T-0002")

    resp = await client.post(
        f"/sage_vaults/{VAULT}/metadata",
        json={"items": [{"document_id": b, "tier3_metadata": {"set": {"ticket_id": "T-0001"}}}]},
    )
    assert resp.status_code == 200, resp.text
    (result,) = resp.json()["results"]
    assert result["status"] == "error", result
    assert result["error"]["error"] == "tier3_unique_constraint_violation"
    detail = result["error"]["detail"]
    assert detail["field"] == "ticket_id"
    assert detail["colliding_value"] == "T-0001"
    assert detail["existing_document_id"] == a


async def test_create_vault_refused_over_colliding_surviving_storage(app, client):
    """An id re-created over storage that survived an eviction can already
    hold colliding rows; declaring the key over them is refused and the
    vault is not registered."""
    config = VaultRegistryService.get_default_config("again_vault", "Again", "testuser")
    root = bound_vault_root()
    sources = root / "again_vault" / "sources"
    config["vault"]["storage_root"] = str(sources)
    config["vault"]["brain_root"] = str(root / "again_vault" / "brain")
    config["document_types"]["doc_types"].append(
        {"value": "ticket", "label": "Ticket", "metadata_schema": TICKET_SCHEMA}
    )
    assert (await client.post("/sage_vaults", json={"config": config})).status_code == 201
    for name in ("a.md", "b.md"):
        resp = await _ingest(
            client, sources, name, "T-0001", vault="again_vault", seed_root=sources.parent
        )
        assert resp.status_code == 201, resp.text
    await drain_vaults({"again_vault": app.state.vault_registry["again_vault"]})
    evicted = app.state.vault_registry.pop("again_vault")
    evicted.close_timing()
    await evicted.close_storage()

    config["document_types"]["doc_types"][-1]["unique_keys"] = ["ticket_id"]
    # A clean declaration beside the colliding one: its index is built during
    # the load and must not survive the refusal.
    config["document_types"]["doc_types"].append(
        {
            "value": "spec",
            "label": "Spec",
            "metadata_schema": TICKET_SCHEMA,
            "unique_keys": ["ticket_id"],
        }
    )
    resp = await client.post("/sage_vaults", json={"config": config})
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["code"] == "tier3_unique_activation_blocked"
    (collision,) = body["detail"]["collisions"]
    assert collision["value"] == "T-0001"
    assert "again_vault" not in app.state.vault_registry
    survivor = await app.state.vault_registry[VAULT].graph_store._fetch_rows(
        "SELECT indexname FROM pg_indexes WHERE indexname = %s",
        ("idx_tier3_unique_spec_ticket_id",),
    )
    assert survivor == []


# ---------------------------------------------------------------------------
# The remaining governed write paths
# ---------------------------------------------------------------------------


async def test_inactive_declaration_refuses_force_reingest(inactive_vault, client, tmp_vault_dir):
    _assert_inactive(await _ingest(client, tmp_vault_dir, "a.md", "T-0001", force=True))


async def test_inactive_declaration_refuses_force_reingest_dry_run(
    inactive_vault, client, tmp_vault_dir
):
    _assert_inactive(
        await _ingest(client, tmp_vault_dir, "a.md", "T-0001", force=True, dry_run=True)
    )


async def test_inactive_declaration_refuses_upload_preview(inactive_vault, app):
    """The checks run before an upload's bytes arrive refuse it too."""
    from sage.api.errors import Tier3UniqueConstraintInactiveError
    from sage.models.enums import SourceType
    from sage.models.schemas import IngestRequest

    ingestion = app.state.vault_registry[VAULT].ingestion_service
    with pytest.raises(Tier3UniqueConstraintInactiveError) as excinfo:
        await ingestion.validate_without_bytes(
            IngestRequest(
                source="/nowhere/upload.md",
                source_type=SourceType.MARKDOWN,
                metadata={"doc_type": "ticket"},
                tier3_metadata={"ticket_id": "T-0003"},
                dry_run=True,
            )
        )
    assert excinfo.value.detail == {"doc_type": "ticket", "field": "ticket_id"}


async def test_inactive_declaration_refuses_retype_carrying_the_field(
    inactive_vault, client, tmp_vault_dir
):
    """Moving a record that already carries the field into the declaring
    doc_type is governed by the declaration, with no tier3 patch at all."""
    draft = await _ingest_ok(client, tmp_vault_dir, "d.md", "T-0007", doc_type="draft")
    resp = await client.post(
        f"/sage_vaults/{VAULT}/metadata",
        json={"items": [{"document_id": draft, "doc_type": "ticket"}]},
    )
    assert resp.status_code == 200, resp.text
    (result,) = resp.json()["results"]
    assert result["status"] == "error", result
    assert result["error"]["error"] == "tier3_unique_constraint_inactive"


async def test_colliding_force_reingest_reports_typed_violation(app, client, tmp_vault_dir):
    assert (await _put_doc_types(client, _doc_types(unique=True))).status_code == 200
    a = await _ingest_ok(client, tmp_vault_dir, "a.md", "T-0001")
    await _ingest_ok(client, tmp_vault_dir, "b.md", "T-0002")

    resp = await _ingest(client, tmp_vault_dir, "b.md", "T-0001", force=True)
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["code"] == "tier3_unique_constraint_violation"
    assert body["detail"]["existing_document_id"] == a


# ---------------------------------------------------------------------------
# Nothing is left behind when a config write fails
# ---------------------------------------------------------------------------


async def test_failed_config_persist_leaves_no_index(app, client, monkeypatch):
    from sage.mcp_init import get_stack_config, get_vault_root, resolve_stack_vault_source_store

    store = resolve_stack_vault_source_store(get_stack_config(), vault_root=get_vault_root())

    def refuse(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(type(store), "write_config", refuse)
    with pytest.raises(OSError):
        await _put_doc_types(client, _doc_types(unique=True))
    assert not await _index_exists(app)


async def test_failed_reload_leaves_no_index(app, client, monkeypatch):
    registry = app.state.vault_registry_service

    async def refuse(*_args, **_kwargs):
        raise RuntimeError("reload failed")

    monkeypatch.setattr(registry, "reload", refuse)
    with pytest.raises(RuntimeError):
        await _put_doc_types(client, _doc_types(unique=True))
    assert not await _index_exists(app)
    current = (await client.get(f"/sage_vaults/{VAULT}/config")).json()
    assert _declared_unique_keys(current) is None


async def test_index_build_refuses_a_collision_the_scan_missed(
    app, client, tmp_vault_dir, monkeypatch
):
    """The build is the authority: a duplicate the pre-write scan did not
    see -- one written between the scan and the build -- still refuses the
    write and leaves no index."""
    import sage.services.vault_config as vault_config_module

    await _ingest_ok(client, tmp_vault_dir, "a.md", "T-0001")
    await _ingest_ok(client, tmp_vault_dir, "b.md", "T-0001")

    async def blind_scan(*_args, **_kwargs):
        return []

    monkeypatch.setattr(vault_config_module, "scan_unique_collisions", blind_scan)
    resp = await _put_doc_types(client, _doc_types(unique=True))
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "tier3_unique_activation_blocked"
    assert not await _index_exists(app)
    current = (await client.get(f"/sage_vaults/{VAULT}/config")).json()
    assert _declared_unique_keys(current) is None


# ---------------------------------------------------------------------------
# Vault load survives an activation it cannot complete
# ---------------------------------------------------------------------------


async def test_load_survives_an_index_build_error(app, client, base_config_dict, monkeypatch):
    """A build failure that is not a collision -- a lock wait that timed
    out, a dropped connection -- leaves the pair unenforced and the vault
    serving, rather than dropping the vault from the registry."""
    from psycopg import errors as pg_errors

    from sage.storage.postgres.graph_store import PostgresGraphStore

    async def fail(*_args, **_kwargs):
        raise pg_errors.LockNotAvailable("lock timeout")

    monkeypatch.setattr(PostgresGraphStore, "ensure_tier3_unique_index", fail)
    declared = copy.deepcopy(base_config_dict)
    declared["document_types"] = _doc_types(unique=True)
    await _reload_with(app, declared)

    assert VAULT in app.state.vault_registry
    assert (await client.get(f"/sage_vaults/{VAULT}/config")).status_code == 200
    assert not await _index_exists(app)


async def test_reload_drops_an_index_no_longer_declared(
    app, client, base_config_dict, tmp_vault_dir
):
    """A declaration withdrawn outside the config write -- an edited file
    reloaded, say -- releases its index at load."""
    assert (await _put_doc_types(client, _doc_types(unique=True))).status_code == 200
    assert await _index_exists(app)

    await _reload_with(app, base_config_dict)

    assert not await _index_exists(app)
    await _ingest_ok(client, tmp_vault_dir, "a.md", "T-0001")
    await _ingest_ok(client, tmp_vault_dir, "b.md", "T-0001")


# ---------------------------------------------------------------------------
# Index names as the database stores them
# ---------------------------------------------------------------------------


def _doc_types_with_awkward_names() -> dict:
    """A mixed-case field, which Postgres folds, and a doc_type whose index
    name runs past the 63-byte identifier limit, which it truncates."""
    doc_types = _doc_types(unique=True)
    doc_types["doc_types"] += [
        {
            "value": "case",
            "label": "Case",
            "metadata_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"CaseRef": {"type": "string"}},
            },
            "unique_keys": ["CaseRef"],
        },
        {
            "value": "a_doc_type_with_a_name_long_enough_to_be_truncated",
            "label": "Long",
            "metadata_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"reference_number": {"type": "string"}},
            },
            "unique_keys": ["reference_number"],
        },
    ]
    return doc_types


async def test_reload_keeps_indexes_whose_stored_names_are_folded(app, client, base_config_dict):
    """The store lowercases and truncates index names; a declaration whose
    stored name differs from the one derived from it -- one that predates
    the write-time length check, say -- keeps its index across loads."""
    declared = copy.deepcopy(base_config_dict)
    declared["document_types"] = _doc_types_with_awkward_names()
    await _reload_with(app, declared)
    store = app.state.vault_registry[VAULT].graph_store
    long_type = "a_doc_type_with_a_name_long_enough_to_be_truncated"
    assert await store.tier3_unique_index_exists("case", "CaseRef")
    assert await store.tier3_unique_index_exists(long_type, "reference_number")

    await _reload_with(app, declared)

    store = app.state.vault_registry[VAULT].graph_store
    assert await store.tier3_unique_index_exists("case", "CaseRef")
    assert await store.tier3_unique_index_exists(long_type, "reference_number")
    assert await store.tier3_unique_index_exists("ticket", "ticket_id")


async def test_config_write_refuses_an_index_name_the_store_cannot_hold(app, client):
    resp = await _put_doc_types(client, _doc_types_with_awkward_names())
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "vault_config_validation_error"
    assert any("longer than 63 bytes" in e for e in body["detail"]["errors"]), body


async def test_config_write_refuses_entries_folding_to_one_index(app, client):
    doc_types = _doc_types(unique=True)
    doc_types["doc_types"].append(
        {
            "value": "case",
            "label": "Case",
            "metadata_schema": {
                "type": "object",
                "properties": {"CaseRef": {"type": "string"}, "caseref": {"type": "string"}},
            },
            "unique_keys": ["CaseRef", "caseref"],
        }
    )
    resp = await _put_doc_types(client, doc_types)
    assert resp.status_code == 400, resp.text
    assert any("shares its index name" in e for e in resp.json()["detail"]["errors"])


async def test_config_write_accepts_a_mixed_case_field(app, client):
    doc_types = _doc_types(unique=True)
    doc_types["doc_types"].append(_doc_types_with_awkward_names()["doc_types"][-2])
    resp = await _put_doc_types(client, doc_types)
    assert resp.status_code == 200, resp.text
    store = app.state.vault_registry[VAULT].graph_store
    assert await store.tier3_unique_index_exists("case", "CaseRef")


# ---------------------------------------------------------------------------
# Activation reported is activation observed
# ---------------------------------------------------------------------------


async def test_partial_activation_failure_leaves_no_index(app, client, monkeypatch):
    """When a later pair of a multi-pair declaration fails to build, the
    earlier pair's freshly built index does not outlive the refused write."""
    from psycopg import errors as pg_errors

    from sage.storage.postgres.graph_store import PostgresGraphStore

    real_build = PostgresGraphStore.ensure_tier3_unique_index

    async def fail_on_case(self, doc_type, field):
        if doc_type == "case":
            raise pg_errors.LockNotAvailable("lock timeout")
        await real_build(self, doc_type, field)

    monkeypatch.setattr(PostgresGraphStore, "ensure_tier3_unique_index", fail_on_case)
    doc_types = _doc_types(unique=True)
    doc_types["doc_types"].append(_doc_types_with_awkward_names()["doc_types"][-2])
    with pytest.raises(pg_errors.LockNotAvailable):
        await _put_doc_types(client, doc_types)
    assert not await _index_exists(app)


async def test_reported_activations_are_observed(app, client, base_config_dict, monkeypatch):
    """A declaration whose build failed at the reload is not reported active."""
    from psycopg import errors as pg_errors

    from sage.storage.postgres.graph_store import PostgresGraphStore

    declared = copy.deepcopy(base_config_dict)
    declared["document_types"] = _doc_types(unique=True)

    async def fail(*_args, **_kwargs):
        raise pg_errors.LockNotAvailable("lock timeout")

    monkeypatch.setattr(PostgresGraphStore, "ensure_tier3_unique_index", fail)
    await _reload_with(app, declared)

    doc_types = _doc_types(unique=True)
    doc_types["doc_types"].append({"value": "report", "label": "Report"})
    # The pair is already declared, so this write does not build it, and the
    # reload it triggers fails to build it again: it is neither active nor a
    # collision, and is not reported active.
    resp = await _put_doc_types(client, doc_types)
    assert resp.status_code == 200, resp.text
    assert resp.json()["tier3_uniqueness_activations"] == []
    assert resp.json()["tier3_uniqueness_collisions"] == []
    assert not await _index_exists(app)


def _new_vault_config(vault_id: str) -> tuple[dict, Path]:
    config = VaultRegistryService.get_default_config(vault_id, vault_id, "testuser")
    root = bound_vault_root()
    config["vault"]["storage_root"] = str(root / vault_id / "sources")
    config["vault"]["brain_root"] = str(root / vault_id / "brain")
    config["document_types"]["doc_types"].append(
        {
            "value": "ticket",
            "label": "Ticket",
            "metadata_schema": TICKET_SCHEMA,
            "unique_keys": ["ticket_id"],
        }
    )
    return config, root / vault_id


async def test_create_vault_retries_a_build_that_failed_at_load(app, client, monkeypatch):
    """A new vault is registered only with its declarations enforced: a
    transient build failure during its load is retried before it returns."""
    from psycopg import errors as pg_errors

    from sage.storage.postgres.graph_store import PostgresGraphStore

    real_build = PostgresGraphStore.ensure_tier3_unique_index
    calls = {"n": 0}

    async def fail_once(self, doc_type, field):
        calls["n"] += 1
        if calls["n"] == 1:
            raise pg_errors.LockNotAvailable("lock timeout")
        await real_build(self, doc_type, field)

    monkeypatch.setattr(PostgresGraphStore, "ensure_tier3_unique_index", fail_once)
    config, _ = _new_vault_config("retry_vault")
    resp = await client.post("/sage_vaults", json={"config": config})
    assert resp.status_code == 201, resp.text
    assert calls["n"] == 2
    assert await _index_exists(app, vault="retry_vault")


async def test_create_vault_refused_when_a_build_cannot_complete(app, client, monkeypatch):
    from psycopg import errors as pg_errors

    from sage.storage.postgres.graph_store import PostgresGraphStore

    async def fail(*_args, **_kwargs):
        raise pg_errors.LockNotAvailable("lock timeout")

    monkeypatch.setattr(PostgresGraphStore, "ensure_tier3_unique_index", fail)
    config, _ = _new_vault_config("stuck_vault")
    with pytest.raises(pg_errors.LockNotAvailable):
        await client.post("/sage_vaults", json={"config": config})
    assert "stuck_vault" not in app.state.vault_registry
