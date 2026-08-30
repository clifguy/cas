"""The depends_on satisfaction set is derived from the vault's lifecycle config.

`GraphOpsService.check_preconditions` gates dependency preconditions on the
set of lifecycle states that satisfy a `depends_on` edge. That set must come
from the vault's declared states — each state's `satisfies_dependency`
setting, with an engine default for states that leave it unset — rather than
from a literal restated in code. A vault that declares a domain state as
dependency-satisfying gets it honoured; a vault that excludes a base state
gets the exclusion.

These tests pin three properties:

- the resolution rule: `satisfies_dependency=True` opts a state in, `False`
  opts it out, unset defers to the engine default (`active` and `completed`);
- `check_preconditions` enforces the derived set end to end, and its
  `required` payload reports the derived set in every branch;
- the derived `required` string never goes empty, even for a vault whose
  states all opt out.
"""

import copy
import hashlib
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from sage.config import VaultConfig
from sage.models.enums import EdgeType, PipelineStatus, SourceType
from sage.models.schemas import Document, Edge
from sage.services.graph_ops import GraphOpsService


def _id(name: str) -> str:
    return f"{hashlib.sha256(name.encode()).hexdigest()[:8]}_{name}"


def _eid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_OID, f"sage-test-edge:{name}"))


def _sha(name: str) -> str:
    return "sha256:" + hashlib.sha256(f"sage-test-hash:{name}".encode()).hexdigest()


def _make_doc(doc_id: str, lifecycle_status: str = "active") -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=doc_id,
        title=f"Test {doc_id}",
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


def _config_with_satisfies(config_dict: dict, **flags: bool) -> VaultConfig:
    """Return a config with `satisfies_dependency` set on named states.

    Keyword name = state value, keyword value = the flag. States not named
    keep the field unset, exercising the engine-default path alongside the
    explicit ones.
    """
    mutated = copy.deepcopy(config_dict)
    for state in mutated["lifecycle"]["states"]:
        if state["value"] in flags:
            state["satisfies_dependency"] = flags[state["value"]]
    return VaultConfig.model_validate(mutated)


async def _seed_dependency(graph_store, dep_status: str) -> str:
    """Insert a function doc depending on one doc in `dep_status`."""
    function_doc = _make_doc(_id("doc_function"))
    dep_doc = _make_doc(_id("doc_dep"), lifecycle_status=dep_status)
    await graph_store.insert_document(function_doc)
    await graph_store.insert_document(dep_doc)
    await graph_store.insert_edge(
        Edge(
            id=_eid("edge_dep"),
            source_id=function_doc.id,
            target_id=dep_doc.id,
            edge_type=EdgeType.DEPENDS_ON,
            created_at=datetime.now(timezone.utc),
        )
    )
    return function_doc.id


def test_dependency_satisfying_states_default_resolution(minimal_config, extended_config):
    """Unset `satisfies_dependency` resolves to the engine default.

    Asserted on both the base config and the extended one (which declares
    `filed` with the field unset): an implementation that returned every
    declared state would pass the base half and fail the extended half,
    so the pair excludes the rival "all declared states" resolution.
    """
    assert minimal_config.lifecycle.dependency_satisfying_states() == frozenset(
        {"active", "completed"}
    )
    assert extended_config.lifecycle.dependency_satisfying_states() == frozenset(
        {"active", "completed"}
    )


def test_domain_state_opts_in(extended_vault_config_dict):
    """A domain state declaring `satisfies_dependency: true` joins the set.

    `filed` cannot come from any engine default, so this fails against an
    implementation that ignores the declaration.
    """
    config = _config_with_satisfies(extended_vault_config_dict, filed=True)
    assert config.lifecycle.dependency_satisfying_states() == frozenset(
        {"active", "completed", "filed"}
    )


def test_base_state_opts_out(minimal_vault_config_dict):
    """A base state declaring `satisfies_dependency: false` leaves the set.

    The engine default keeps `completed`; only an implementation that
    honours the explicit `false` can drop it.
    """
    config = _config_with_satisfies(minimal_vault_config_dict, completed=False)
    assert config.lifecycle.dependency_satisfying_states() == frozenset({"active"})


async def test_check_preconditions_honors_opt_in_end_to_end(
    graph_store, extended_vault_config_dict
):
    """A dependency resting in an opted-in domain state satisfies.

    The mirror image of BH-036 under a divergent config: same `filed`
    state, opposite outcome, because this vault declares it satisfying.
    A leftover literal set reads as `satisfied is False` here.
    """
    config = _config_with_satisfies(extended_vault_config_dict, filed=True)
    service = GraphOpsService(graph_store, config)
    function_id = await _seed_dependency(graph_store, "filed")

    result = await service.check_preconditions(function_id)
    assert result.satisfied is True
    assert result.checks[0].satisfied is True
    assert result.checks[0].required == "active or completed or filed"


async def test_check_preconditions_honors_opt_out_end_to_end(
    graph_store, minimal_vault_config_dict
):
    """A dependency resting in an opted-out base state does not satisfy.

    `completed` satisfies under the engine default, so only the config
    declaration can produce this rejection.
    """
    config = _config_with_satisfies(minimal_vault_config_dict, completed=False)
    service = GraphOpsService(graph_store, config)
    function_id = await _seed_dependency(graph_store, "completed")

    result = await service.check_preconditions(function_id)
    assert result.satisfied is False
    assert result.checks[0].actual == "completed"
    assert result.checks[0].required == "active"


async def test_required_string_derived_in_all_three_branches(extended_vault_config_dict):
    """Every `required` payload reports the derived set, whatever the branch.

    Exercises the not-found, pipeline-failed, and lifecycle branches in one
    call over a stub store (the only clean route to a dangling `depends_on`
    edge: the real store's foreign keys forbid one). A three-state string in
    all three branches proves the derivation is shared rather than restated
    per branch — a literal can only ever produce two states.
    """
    config = _config_with_satisfies(extended_vault_config_dict, filed=True)

    function_id = _id("doc_function")
    failed_dep = _make_doc(_id("dep_failed"))
    failed_dep.pipeline_status = PipelineStatus.FAILED
    archived_dep = _make_doc(_id("dep_archived"), lifecycle_status="archived")
    docs = {
        function_id: _make_doc(function_id),
        failed_dep.id: failed_dep,
        archived_dep.id: archived_dep,
    }
    edges = [
        SimpleNamespace(target_id=_id("dep_missing")),
        SimpleNamespace(target_id=failed_dep.id),
        SimpleNamespace(target_id=archived_dep.id),
    ]

    async def get_document(doc_id):
        return docs.get(doc_id)

    async def get_edges_by_source(source_id, edge_type):
        return edges

    store = SimpleNamespace(get_document=get_document, get_edges_by_source=get_edges_by_source)
    service = GraphOpsService(store, config)

    result = await service.check_preconditions(function_id)
    assert result.satisfied is False
    assert [c.required for c in result.checks] == ["active or completed or filed"] * 3
    assert [c.actual for c in result.checks] == [
        "not found",
        "failed (pipeline_incomplete)",
        "archived",
    ]


async def test_required_string_empty_set_guard(graph_store, minimal_vault_config_dict):
    """A vault whose states all opt out still reports a readable `required`.

    Pathological but expressible; the API must not emit an empty string.
    """
    config = _config_with_satisfies(minimal_vault_config_dict, active=False, completed=False)
    service = GraphOpsService(graph_store, config)
    function_id = await _seed_dependency(graph_store, "active")

    result = await service.check_preconditions(function_id)
    assert result.satisfied is False
    assert result.checks[0].required == "(no state satisfies dependencies)"
