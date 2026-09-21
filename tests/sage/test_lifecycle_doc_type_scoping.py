"""Lifecycle states and transitions may be scoped to declared doc_types.

A state or transition carrying `doc_types` applies only to documents of
those types; one without the key applies vault-wide, which is every
configuration written before the key existed (CAS-ADR-054). The fixture
scopes a `blocked` state and its two transitions to `work_item`, leaving
`control_exception` in the same vault on the base lifecycle alone.

Every scoping assertion is paired with a control over the identical
configuration minus the `doc_types` keys, which admits the action on every
type -- so a refusal that came from anywhere but the key cannot pass.

Two structural invariants keep the scoped vocabulary coherent, both under
the strict-on-write, lenient-on-load split of CAS-ADR-047:

- a transition's scope lies within the scope of each state it touches, so
  no transition moves a document into a state its type cannot hold;
- the ingestion row is unscoped and lands in an unscoped state, so every
  declared doc_type can be ingested.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from sage import mcp_server
from sage.adapters.stubs import StubContentStore
from sage.api.errors import (
    InvalidActionError,
    InvalidLifecycleTransitionError,
    LifecycleStateNotApplicableError,
    SupersedeTargetNotActiveError,
    VaultConfigValidationError,
)
from sage.app import _initialize_services, create_app
from sage.config import (
    VaultConfig,
    build_transition_table,
    document_scope,
    load_vault_config,
)
from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import (
    BulkMetadataRequest,
    Document,
    IngestRequest,
    SetLifecycleRequest,
)
from sage.services.ingestion import IngestionService
from sage.services.lifecycle import LifecycleService
from sage.services.metadata import MetadataService
from sage.services.vault_registry import VaultRegistryService
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from sage.vault_management import _validate_config
from tests.sage.test_lifecycle import _id, _sha

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_CAS_VAULT_CONFIG = REPO_ROOT / "tests" / "fixtures" / "ci_cas_vault_config.yaml"

WORK_ITEM = "work_item"
CONTROL_EXCEPTION = "control_exception"


# ---------------------------------------------------------------------------
# Fixture configuration
# ---------------------------------------------------------------------------


def _scoped(config_dict: dict) -> dict:
    """The minimal config plus a `blocked` state scoped to `work_item`.

    The base table's `archived -> reactivate` row keeps `reactivate` a known
    action on every doc_type, so one attempted from `active` is refused for
    its state rather than as unknown.
    """
    mutated = copy.deepcopy(config_dict)
    mutated["document_types"]["doc_types"] += [
        {"value": WORK_ITEM, "label": "Work item"},
        {"value": CONTROL_EXCEPTION, "label": "Control exception"},
    ]
    lifecycle = mutated["lifecycle"]
    lifecycle["states"].append({"value": "blocked", "label": "Blocked", "doc_types": [WORK_ITEM]})
    lifecycle["transitions"] += [
        {
            "from_state": "active",
            "action": "block",
            "to_state": "blocked",
            "doc_types": [WORK_ITEM],
        },
        {
            "from_state": "blocked",
            "action": "unblock",
            "to_state": "active",
            "doc_types": [WORK_ITEM],
        },
    ]
    return mutated


def _unscoped(config_dict: dict) -> dict:
    """The same configuration with every `doc_types` key removed: the control."""
    stripped = copy.deepcopy(config_dict)
    for entry in stripped["lifecycle"]["states"] + stripped["lifecycle"]["transitions"]:
        entry.pop("doc_types", None)
    return stripped


def _transition(config_dict: dict, action: str) -> dict:
    return next(t for t in config_dict["lifecycle"]["transitions"] if t["action"] == action)


def _state(config_dict: dict, value: str) -> dict:
    return next(s for s in config_dict["lifecycle"]["states"] if s["value"] == value)


def _load_from_disk(config_dict: dict, tmp_path: Path) -> VaultConfig:
    path = tmp_path / "vault_config.yaml"
    path.write_text(yaml.safe_dump(config_dict))
    return load_vault_config(path)


def _doc(name: str, doc_type: str | None, lifecycle_status: str = "active") -> Document:
    now = datetime.now(timezone.utc)
    doc_id = _id(name)
    return Document(
        id=doc_id,
        title=f"Test {name}",
        doc_type=doc_type,
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{doc_id}.md",
        lifecycle_status=lifecycle_status,
        source_content_hash=_sha(doc_id),
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
    )


# ---------------------------------------------------------------------------
# C1-C5: configuration validation
# ---------------------------------------------------------------------------


def test_scoped_configuration_validates_strictly(minimal_vault_config_dict):
    """C1: a coherent scoped configuration passes the strict write path."""
    config = VaultConfig.model_validate(_scoped(minimal_vault_config_dict))

    blocked = next(s for s in config.lifecycle.states if s.value == "blocked")
    assert blocked.doc_types == [WORK_ITEM]
    block = next(t for t in config.lifecycle.transitions if t.action == "block")
    assert block.doc_types == [WORK_ITEM]
    active = next(s for s in config.lifecycle.states if s.value == "active")
    assert active.doc_types is None, "an entry without the key stays vault-wide"


@pytest.mark.parametrize("entry", ["state", "transition"])
def test_empty_scope_is_refused(minimal_vault_config_dict, entry):
    """An empty list would scope an entry to no doc_type at all; omission means all."""
    mutated = _scoped(minimal_vault_config_dict)
    target = _state(mutated, "blocked") if entry == "state" else _transition(mutated, "block")
    target["doc_types"] = []

    with pytest.raises(ValidationError, match="doc_types"):
        VaultConfig.model_validate(mutated)


def _ghost_state(config_dict: dict) -> dict:
    mutated = _scoped(config_dict)
    _state(mutated, "blocked")["doc_types"] = [WORK_ITEM, "ghost"]
    return mutated


def _ghost_transition(config_dict: dict) -> dict:
    mutated = _scoped(config_dict)
    _transition(mutated, "complete")["doc_types"] = ["ghost"]
    return mutated


@pytest.mark.parametrize(
    ("variant", "names"),
    [(_ghost_state, "blocked"), (_ghost_transition, "active -> complete -> completed")],
    ids=["state", "transition"],
)
def test_undeclared_doc_type_is_refused_on_write(minimal_vault_config_dict, variant, names):
    """C2: a `doc_types` entry naming no declared doc_type fails strict validation.

    The fixture is otherwise coherent -- the transition's only scope entry
    is the undeclared one, so the subset rule has nothing to object to --
    and the message must name both the stray type and the entry carrying it.
    """
    mutated = variant(minimal_vault_config_dict)

    with pytest.raises(ValidationError) as exc:
        VaultConfig.model_validate(mutated)
    message = str(exc.value)
    assert "ghost" in message
    assert "not a declared doc_type" in message
    assert names in message

    with pytest.raises(VaultConfigValidationError):
        _validate_config(mutated)


@pytest.mark.parametrize("variant", [_ghost_state, _ghost_transition], ids=["state", "transition"])
def test_undeclared_doc_type_loads_leniently_from_disk(
    minimal_vault_config_dict, variant, tmp_path, caplog
):
    """C3: the same configuration on disk loads, warning, with the entry intact."""
    mutated = variant(minimal_vault_config_dict)

    with caplog.at_level(logging.WARNING, logger="sage.config"):
        config = _load_from_disk(mutated, tmp_path)

    assert config.vault.id == mutated["vault"]["id"]
    scoped = [
        entry.doc_types
        for entry in [*config.lifecycle.states, *config.lifecycle.transitions]
        if entry.doc_types and "ghost" in entry.doc_types
    ]
    assert scoped, "the lenient load keeps the declaration as written"
    assert any(
        "ghost" in r.getMessage() and "not a declared doc_type" in r.getMessage()
        for r in caplog.records
    ), "the lenient load must still surface the problem"


def _unscoped_transition_into_scoped_state(config_dict: dict) -> dict:
    mutated = _scoped(config_dict)
    del _transition(mutated, "block")["doc_types"]
    return mutated


def _wider_transition_than_state(config_dict: dict) -> dict:
    mutated = _scoped(config_dict)
    _transition(mutated, "block")["doc_types"] = [WORK_ITEM, CONTROL_EXCEPTION]
    return mutated


def _unscoped_transition_out_of_scoped_state(config_dict: dict) -> dict:
    mutated = _scoped(config_dict)
    del _transition(mutated, "unblock")["doc_types"]
    return mutated


@pytest.mark.parametrize(
    ("variant", "where"),
    [
        (_unscoped_transition_into_scoped_state, "active -> block -> blocked"),
        (_wider_transition_than_state, "active -> block -> blocked"),
        (_unscoped_transition_out_of_scoped_state, "blocked -> unblock -> active"),
    ],
    ids=["unscoped", "wider", "leaving"],
)
def test_transition_scope_must_lie_within_its_states(
    minimal_vault_config_dict, variant, where, tmp_path, caplog
):
    """C4: a transition touching a state for types that state does not carry fails.

    Every name in the fixture is declared, so the undeclared-type rule
    cannot be what fires; the message must be the subset rule's. The
    `leaving` case has the scoped state as the source, so a rule that
    checked only the state a transition lands in would pass the other two
    and fail this one.
    """
    mutated = variant(minimal_vault_config_dict)

    with pytest.raises(ValidationError) as exc:
        VaultConfig.model_validate(mutated)
    message = str(exc.value)
    assert where in message
    assert "outside the scope of the state 'blocked'" in message

    with caplog.at_level(logging.WARNING, logger="sage.config"):
        config = _load_from_disk(mutated, tmp_path)
    assert config.vault.id == mutated["vault"]["id"]
    assert any("outside the scope" in r.getMessage() for r in caplog.records)


def test_ingest_landing_state_may_not_be_scoped(minimal_vault_config_dict):
    """C5: a scoped landing state would leave other doc_types unable to ingest."""
    mutated = _scoped(minimal_vault_config_dict)
    _state(mutated, "active")["doc_types"] = [WORK_ITEM]
    # Keep every other transition touching `active` inside its new scope,
    # so the landing rule is the only one the fixture can trip.
    for transition in mutated["lifecycle"]["transitions"]:
        if "active" in (transition["from_state"], transition["to_state"]) and (
            transition["from_state"] != "(new)"
        ):
            transition["doc_types"] = [WORK_ITEM]

    with pytest.raises(ValidationError) as exc:
        VaultConfig.model_validate(mutated)
    message = str(exc.value)
    assert "ingest landing state 'active'" in message
    assert "outside the scope" not in message, (
        "the landing rule must speak for the ingestion row, not the generic subset rule"
    )


def test_ingestion_row_may_not_declare_doc_types(minimal_vault_config_dict):
    """C5: the '(new)' row is the single ingest row for every doc_type."""
    mutated = _scoped(minimal_vault_config_dict)
    _transition(mutated, "ingest")["doc_types"] = [WORK_ITEM]

    with pytest.raises(ValidationError, match="'\\(new\\)' ingestion transition may not declare"):
        VaultConfig.model_validate(mutated)


# ---------------------------------------------------------------------------
# T1-T5: the transition table
# ---------------------------------------------------------------------------


def test_table_resolves_actions_per_doc_type(minimal_vault_config_dict):
    """T1: the scoped action is valid for `work_item` alone."""
    table = build_transition_table(VaultConfig.model_validate(_scoped(minimal_vault_config_dict)))

    assert "block" in table.get_valid_actions("active", doc_type=WORK_ITEM)
    assert "block" not in table.get_valid_actions("active", doc_type=CONTROL_EXCEPTION)
    assert table.validate_transition("active", "block", doc_type=WORK_ITEM) == ("blocked", None)
    assert table.validate_transition("active", "block", doc_type=CONTROL_EXCEPTION) is None


def test_table_without_doc_types_admits_the_action_on_every_type(minimal_vault_config_dict):
    """T2: the paired control -- the same table minus the key admits both types."""
    table = build_transition_table(
        VaultConfig.model_validate(_unscoped(_scoped(minimal_vault_config_dict)))
    )

    for doc_type in (WORK_ITEM, CONTROL_EXCEPTION):
        assert "block" in table.get_valid_actions("active", doc_type=doc_type)
        assert table.validate_transition("active", "block", doc_type=doc_type) == (
            "blocked",
            None,
        )


#: The default scaffold's table as it resolved before scoping existed,
#: including the `completed -> supersede -> archived` row a revisable vault
#: declares.
_SCAFFOLD_GOLDEN_ACTIONS = {
    "active": ["supersede", "complete", "archive", "relocate"],
    "completed": ["supersede", "archive", "reactivate", "relocate"],
    "archived": ["reactivate"],
    "relocated": [],
}
_SCAFFOLD_GOLDEN_KNOWN = ["archive", "complete", "reactivate", "relocate", "supersede"]


def test_unscoped_configuration_resolves_exactly_as_before():
    """T3: a configuration with no `doc_types` anywhere is unchanged.

    Every per-type answer equals the vault-wide one, and both equal a
    literal golden, so a per-type path that silently matched a broken
    vault-wide path cannot pass.
    """
    scaffold = VaultRegistryService.get_default_config("golden")
    config = VaultConfig.model_validate(scaffold)
    table = build_transition_table(config)

    for doc_type in [None, *sorted(config.valid_doc_type_values())]:
        for state, actions in _SCAFFOLD_GOLDEN_ACTIONS.items():
            assert table.get_valid_actions(state, doc_type=doc_type) == actions
        assert table.known_actions(doc_type=doc_type) == _SCAFFOLD_GOLDEN_KNOWN
        assert table.states_allowing("supersede", doc_type=doc_type) == ["active", "completed"]
        assert table.landing_states("supersede", doc_type=doc_type) == {"archived"}
        assert table.validate_transition("completed", "supersede", doc_type=doc_type) == (
            "archived",
            "supersedes",
        )


def test_unscoped_ci_vault_fixture_resolves_identically_per_type():
    """T3: the committed cas-vault fixture resolves the same for every type."""
    config = VaultConfig.model_validate(yaml.safe_load(CI_CAS_VAULT_CONFIG.read_text()))
    table = build_transition_table(config)
    states = [s.value for s in config.lifecycle.states]

    for doc_type in sorted(config.valid_doc_type_values()):
        for state in states:
            assert table.get_valid_actions(state, doc_type=doc_type) == table.get_valid_actions(
                state
            )
        assert table.known_actions(doc_type=doc_type) == table.known_actions()
    assert table.get_valid_actions("active") == ["supersede", "complete", "archive", "relocate"]


def test_roster_is_per_doc_type(minimal_vault_config_dict):
    """T4: the known-action roster narrows to the type; the bare roster does not."""
    table = build_transition_table(VaultConfig.model_validate(_scoped(minimal_vault_config_dict)))

    assert table.is_known_action("block", doc_type=WORK_ITEM)
    assert not table.is_known_action("block", doc_type=CONTROL_EXCEPTION)
    assert "block" not in table.known_actions(doc_type=CONTROL_EXCEPTION)
    assert "complete" in table.known_actions(doc_type=CONTROL_EXCEPTION)
    assert "block" in table.known_actions()
    assert table.is_known_action("block")


def test_vault_wide_queries_report_the_union(minimal_vault_config_dict):
    """T5: with no doc_type, the inverse queries answer for the whole vault."""
    table = build_transition_table(VaultConfig.model_validate(_scoped(minimal_vault_config_dict)))

    assert table.states_allowing("block") == ["active"]
    assert table.landing_states("block") == {"blocked"}
    assert table.states_allowing("block", doc_type=CONTROL_EXCEPTION) == []
    assert table.landing_states("block", doc_type=CONTROL_EXCEPTION) == set()


# ---------------------------------------------------------------------------
# S1-S3, S5: the lifecycle and ingestion services
# ---------------------------------------------------------------------------


def _lifecycle(config_dict, graph_store, lock_manager, content_store) -> LifecycleService:
    return LifecycleService(
        graph_store, lock_manager, VaultConfig.model_validate(config_dict), content_store
    )


async def test_scoped_action_transitions_a_document_of_the_scoped_type(
    graph_store, lock_manager, stub_content_store, minimal_vault_config_dict
):
    """S1: `block` moves a `work_item` into `blocked`."""
    service = _lifecycle(
        _scoped(minimal_vault_config_dict), graph_store, lock_manager, stub_content_store
    )
    doc = _doc("wi_s1", WORK_ITEM)
    await graph_store.insert_document(doc)

    await service._set_lifecycle(doc.id, SetLifecycleRequest(action="block"))

    assert (await graph_store.get_document(doc.id)).lifecycle_status == "blocked"


@pytest.mark.parametrize("scoped", [True, False], ids=["scoped", "control"])
async def test_scoped_action_is_an_invalid_action_for_other_types(
    graph_store, lock_manager, stub_content_store, minimal_vault_config_dict, scoped
):
    """S2: the same action on a `control_exception` is `invalid_action`.

    The control arm runs the identical configuration minus the keys, where
    the action succeeds -- so the refusal is the key's doing alone. The
    code is `invalid_action`, not a wrong-state refusal: scoping only the
    rendering of `valid_actions` would not produce it.
    """
    config_dict = _scoped(minimal_vault_config_dict)
    if not scoped:
        config_dict = _unscoped(config_dict)
    service = _lifecycle(config_dict, graph_store, lock_manager, stub_content_store)
    doc = _doc("ce_s2", CONTROL_EXCEPTION)
    await graph_store.insert_document(doc)

    if not scoped:
        await service._set_lifecycle(doc.id, SetLifecycleRequest(action="block"))
        assert (await graph_store.get_document(doc.id)).lifecycle_status == "blocked"
        return

    with pytest.raises(InvalidActionError) as exc:
        await service._set_lifecycle(doc.id, SetLifecycleRequest(action="block"))
    known = exc.value.detail["known_actions"]
    assert "block" not in known
    assert "complete" in known
    assert (await graph_store.get_document(doc.id)).lifecycle_status == "active"


@pytest.mark.parametrize(
    ("doc_type", "expect_block"),
    [(CONTROL_EXCEPTION, False), (WORK_ITEM, True)],
)
async def test_valid_actions_render_only_the_documents_type(
    graph_store,
    lock_manager,
    stub_content_store,
    minimal_vault_config_dict,
    doc_type,
    expect_block,
):
    """S3: a wrong-state refusal lists the actions the document's type carries."""
    service = _lifecycle(
        _scoped(minimal_vault_config_dict), graph_store, lock_manager, stub_content_store
    )
    doc = _doc(f"s3_{doc_type}", doc_type)
    await graph_store.insert_document(doc)

    with pytest.raises(InvalidLifecycleTransitionError) as exc:
        await service._set_lifecycle(doc.id, SetLifecycleRequest(action="reactivate"))
    valid = exc.value.detail["valid_actions"]
    assert ("block" in valid) is expect_block
    assert "complete" in valid


def _seed_file(tmp_vault_dir: Path, relative: str, content: str) -> None:
    full = tmp_vault_dir / "sources" / relative
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)


def _supersede_scoped(config_dict: dict) -> dict:
    """`supersede` from `active` for `work_item` only; from `completed` for all."""
    mutated = _scoped(config_dict)
    _transition(mutated, "supersede")["doc_types"] = [WORK_ITEM]
    mutated["lifecycle"]["transitions"].append(
        {
            "from_state": "completed",
            "action": "supersede",
            "to_state": "archived",
            "creates_edge": "supersedes",
        }
    )
    return mutated


@pytest.mark.parametrize(
    ("doc_type", "refused"),
    [(CONTROL_EXCEPTION, True), (WORK_ITEM, False)],
)
async def test_supersession_is_scoped_by_the_predecessors_type(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_vault_config_dict,
    doc_type,
    refused,
):
    """S5: the supersede row consulted is the predecessor's own.

    Both arms ingest a successor of an active predecessor under one
    configuration; only the predecessor's doc_type differs, so the refusal
    cannot be any other supersede failure. Each successor declares the
    *other* doc_type, so a gate that consulted the successor's rows instead
    would refuse the `work_item` arm and admit the `control_exception` one.
    """
    config = VaultConfig.model_validate(_supersede_scoped(minimal_vault_config_dict))
    lifecycle = LifecycleService(graph_store, lock_manager, config, stub_content_store)
    ingestion = IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=stub_abstraction_provider,
        config=config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
        lifecycle_service=lifecycle,
    )
    _seed_file(tmp_vault_dir, f"sp_{doc_type}_v1.md", "# V1\n\nOriginal.")
    _seed_file(tmp_vault_dir, f"sp_{doc_type}_v2.md", "# V2\n\nRevised.")
    v1 = await ingestion.ingest(
        IngestRequest(
            source=f"sp_{doc_type}_v1.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": doc_type},
        )
    )
    other_type = WORK_ITEM if doc_type == CONTROL_EXCEPTION else CONTROL_EXCEPTION
    successor = IngestRequest(
        source=f"sp_{doc_type}_v2.md",
        source_type=SourceType.MARKDOWN,
        predecessor_id=v1.document.id,
        metadata={"doc_type": other_type},
    )

    if refused:
        with pytest.raises(SupersedeTargetNotActiveError):
            await ingestion.ingest(successor)
        assert (await graph_store.get_document(v1.document.id)).lifecycle_status == "active"
    else:
        await ingestion.ingest(successor)
        assert (await graph_store.get_document(v1.document.id)).lifecycle_status == "archived"


# ---------------------------------------------------------------------------
# S4, S6: the request surfaces (CAS-ADR-052)
# ---------------------------------------------------------------------------


@pytest.fixture
def _mcp_registry():
    saved = dict(mcp_server._vaults)
    mcp_server._vaults.clear()
    try:
        yield
    finally:
        mcp_server._vaults.clear()
        mcp_server._vaults.update(saved)


@pytest.fixture
async def scoped_app(minimal_vault_config_dict, monkeypatch, _mcp_registry):
    """The scoped vault booted behind both request surfaces."""
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = VaultConfig.model_validate(_scoped(minimal_vault_config_dict))
    app = create_app(config=config)
    await _initialize_services(app, config, content_store_factory=lambda _brain: StubContentStore())
    services = app.state.vault_registry[config.vault.id]
    mcp_server._vaults[config.vault.id] = services
    yield app, config.vault.id, services
    await asyncio.sleep(0.1)
    services.close_timing()
    await services.graph_store.close()


async def _rest(app, path: str, body: dict) -> dict:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(path, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _item(response: dict) -> dict:
    assert "error" not in response, response
    (item,) = response["results"]
    return item


async def test_lifecycle_scoping_is_identical_across_surfaces(scoped_app):
    """S4: REST and MCP agree on both the scoped success and the refusal."""
    app, vault_id, services = scoped_app
    outcomes = {}
    for surface in ("rest", "mcp"):
        wi = _doc(f"s4_wi_{surface}", WORK_ITEM)
        ce = _doc(f"s4_ce_{surface}", CONTROL_EXCEPTION)
        for doc in (wi, ce):
            await services.graph_store.insert_document(doc)
        results = []
        for doc in (wi, ce):
            items = [{"document_id": doc.id, "action": "block"}]
            if surface == "rest":
                response = await _rest(app, f"/sage_vaults/{vault_id}/lifecycles", {"items": items})
            else:
                response = await mcp_server.update_lifecycles(vault_id=vault_id, items=items)
            results.append(_item(response))
        outcomes[surface] = results

    for surface, (wi_item, ce_item) in outcomes.items():
        assert wi_item["status"] == "success", surface
        assert ce_item["status"] == "error", surface
        assert ce_item["error"]["error"] == "invalid_action", surface
        assert "block" not in ce_item["error"]["detail"]["known_actions"], surface
    assert outcomes["rest"][1]["error"]["detail"] == outcomes["mcp"][1]["error"]["detail"]


@pytest.mark.parametrize("surface", ["rest", "mcp"])
@pytest.mark.parametrize(
    ("state", "refused"),
    [("blocked", True), ("active", False)],
)
async def test_retype_into_a_type_the_state_excludes_is_refused(
    scoped_app, surface, state, refused
):
    """S6: a doc_type change may not strand a document in a state its new type lacks.

    The `active` arm retypes the same `work_item` successfully, so the
    refusal is the scoped state's doing, not a blanket refusal of retypes.
    """
    app, vault_id, services = scoped_app
    doc = _doc(f"s6_{surface}_{state}", WORK_ITEM, lifecycle_status=state)
    await services.graph_store.insert_document(doc)
    items = [{"document_id": doc.id, "doc_type": CONTROL_EXCEPTION}]

    if surface == "rest":
        response = await _rest(app, f"/sage_vaults/{vault_id}/metadata", {"items": items})
    else:
        response = await mcp_server.update_metadata(vault_id=vault_id, items=items)
    item = _item(response)
    stored = await services.graph_store.get_document(doc.id)

    if refused:
        assert item["status"] == "error"
        assert item["error"]["error"] == "lifecycle_state_not_applicable"
        assert item["error"]["detail"] == {
            "current_state": "blocked",
            "doc_type": CONTROL_EXCEPTION,
            "state_doc_types": [WORK_ITEM],
        }
        assert stored.doc_type == WORK_ITEM
    else:
        assert item["status"] == "success"
        assert stored.doc_type == CONTROL_EXCEPTION


async def test_retype_between_types_the_state_admits_is_allowed(
    graph_store, lock_manager, stub_content_store, minimal_vault_config_dict
):
    """S6: the guard reads the state's scope, not merely that it has one.

    `blocked` here admits both types, so retyping a blocked `work_item` to
    `control_exception` keeps the document in a state its new type holds. A
    guard refusing every retype out of a scoped state would refuse this.
    """
    config_dict = _scoped(minimal_vault_config_dict)
    _state(config_dict, "blocked")["doc_types"] = [WORK_ITEM, CONTROL_EXCEPTION]
    service = MetadataService(
        graph_store, lock_manager, VaultConfig.model_validate(config_dict), stub_content_store
    )
    doc = _doc("s6_wide", WORK_ITEM, lifecycle_status="blocked")
    await graph_store.insert_document(doc)

    response = await service.bulk_update_metadata(
        BulkMetadataRequest(items=[{"document_id": doc.id, "doc_type": CONTROL_EXCEPTION}]),
        "testuser",
    )

    assert response.results[0].status == "success", response.results[0].error
    assert (await graph_store.get_document(doc.id)).doc_type == CONTROL_EXCEPTION


@pytest.mark.parametrize("entry", ["state", "transition"])
def test_duplicate_scope_entry_is_refused(minimal_vault_config_dict, entry):
    """A repeated doc_type is refused, as the schema's `uniqueItems` refuses it."""
    mutated = _scoped(minimal_vault_config_dict)
    target = _state(mutated, "blocked") if entry == "state" else _transition(mutated, "block")
    target["doc_types"] = [WORK_ITEM, WORK_ITEM]

    with pytest.raises(ValidationError, match="duplicate"):
        VaultConfig.model_validate(mutated)


def test_untyped_document_matches_only_unscoped_rows(minimal_vault_config_dict):
    """A document without a doc_type is not the vault as a whole.

    `None` asks for the vault-wide union and so admits the scoped `block`
    row; the key a typeless document resolves to must admit only the rows
    that apply to every doc_type.
    """
    table = build_transition_table(VaultConfig.model_validate(_scoped(minimal_vault_config_dict)))
    untyped = document_scope(None)

    assert table.validate_transition("active", "block", None) == ("blocked", None)
    assert table.validate_transition("active", "block", untyped) is None
    assert "block" not in table.get_valid_actions("active", untyped)
    assert "complete" in table.get_valid_actions("active", untyped)
    assert document_scope(WORK_ITEM) == WORK_ITEM


async def test_untyped_document_cannot_take_a_scoped_action(
    graph_store, lock_manager, stub_content_store, minimal_vault_config_dict
):
    """The service resolves a typeless document against unscoped rows only."""
    service = _lifecycle(
        _scoped(minimal_vault_config_dict), graph_store, lock_manager, stub_content_store
    )
    doc = _doc("untyped", None)
    await graph_store.insert_document(doc)

    with pytest.raises(InvalidActionError):
        await service._set_lifecycle(doc.id, SetLifecycleRequest(action="block"))
    await service._set_lifecycle(doc.id, SetLifecycleRequest(action="complete"))
    assert (await graph_store.get_document(doc.id)).lifecycle_status == "completed"


@pytest.mark.parametrize(
    ("state", "new_type", "refused"),
    [
        ("blocked", CONTROL_EXCEPTION, True),
        ("active", CONTROL_EXCEPTION, False),
        ("blocked", WORK_ITEM, False),
    ],
    ids=["retype-out-of-scope", "retype-from-unscoped", "no-retype-in-scope"],
)
async def test_force_reingest_may_not_retype_out_of_the_states_scope(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_vault_config_dict,
    state,
    new_type,
    refused,
):
    """S6: a force re-ingest carrying a new doc_type is held to the same rule.

    Re-ingestion reuses the existing record and writes the resolved doc_type
    through, so it is a retype. The `active` arm retypes the same document
    successfully, so the refusal is the scoped state's doing; the
    `no-retype-in-scope` arm re-ingests the blocked document keeping its
    doc_type, so the refusal is the retype's doing, not the re-ingest's.
    """
    config = VaultConfig.model_validate(_scoped(minimal_vault_config_dict))
    lifecycle = LifecycleService(graph_store, lock_manager, config, stub_content_store)
    ingestion = IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=stub_abstraction_provider,
        config=config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
        lifecycle_service=lifecycle,
    )
    source = f"fr_{state}_{new_type}.md"
    _seed_file(tmp_vault_dir, source, "# Force\n\nBody.")
    first = await ingestion.ingest(
        IngestRequest(
            source=source, source_type=SourceType.MARKDOWN, metadata={"doc_type": WORK_ITEM}
        )
    )
    if state == "blocked":
        await lifecycle._set_lifecycle(first.document.id, SetLifecycleRequest(action="block"))
    retype = IngestRequest(
        source=source,
        source_type=SourceType.MARKDOWN,
        force=True,
        metadata={"doc_type": new_type},
    )

    if refused:
        with pytest.raises(LifecycleStateNotApplicableError) as exc:
            await ingestion.ingest(retype)
        assert exc.value.detail == {
            "current_state": "blocked",
            "doc_type": CONTROL_EXCEPTION,
            "state_doc_types": [WORK_ITEM],
        }
        assert (await graph_store.get_document(first.document.id)).doc_type == WORK_ITEM
    else:
        await ingestion.ingest(retype)
        stored = await graph_store.get_document(first.document.id)
        assert (stored.doc_type, stored.lifecycle_status) == (new_type, state)
