"""`base_states_required` is enforced, not merely declared.

`LifecycleConfig` validates two layers on construction. Unconditionally,
the ingestion pseudo-row must be well-formed: exactly one `(new)` row, its
action `ingest`, its target a declared state, and `ingest` appearing on no
other row — the invariants that keep the ingest landing state readable and
the action non-invocable. When `base_states_required` is true (the
default), the base states must be declared and each base action used by at
least one transition, which is the promise the flag's description has
always made.

Action presence is checked rather than exact base rows: a vault may route
a base action through extra or different states (extra supersede sources,
a non-`active` ingest landing state) without tripping the validator.

Strictness is contextual: direct validation (the create/update API paths)
rejects a violating configuration, while `load_vault_config` validates
on-disk files leniently — warning and loading — so an existing vault stays
registered and reachable for repair rather than silently disappearing.
The structural subset of the invariants is also expressed in
`docs/fs/sage/lifecycle.schema.json`; the schema-parity tests here keep
the two surfaces from drifting apart.
"""

import copy
import json
import logging
from pathlib import Path

import jsonschema
import pytest
import yaml
from pydantic import ValidationError

from sage.config import VaultConfig, load_vault_config

_LIFECYCLE_SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[2] / "docs" / "fs" / "sage" / "lifecycle.schema.json"
    ).read_text()
)


def _lifecycle_variant(config_dict: dict) -> dict:
    return copy.deepcopy(config_dict)


def test_base_required_rejects_missing_base_state(minimal_vault_config_dict):
    """Dropping a base state fails validation, naming the state."""
    mutated = _lifecycle_variant(minimal_vault_config_dict)
    mutated["lifecycle"]["states"] = [
        s for s in mutated["lifecycle"]["states"] if s["value"] != "completed"
    ]
    mutated["lifecycle"]["transitions"] = [
        t
        for t in mutated["lifecycle"]["transitions"]
        if "completed" not in (t["from_state"], t["to_state"])
    ]
    with pytest.raises(ValidationError, match="completed"):
        VaultConfig.model_validate(mutated)


def test_base_required_rejects_missing_base_action(minimal_vault_config_dict):
    """Dropping every use of a base action fails validation, naming it."""
    mutated = _lifecycle_variant(minimal_vault_config_dict)
    mutated["lifecycle"]["transitions"] = [
        t for t in mutated["lifecycle"]["transitions"] if t["action"] != "reactivate"
    ]
    with pytest.raises(ValidationError, match="reactivate"):
        VaultConfig.model_validate(mutated)


@pytest.mark.parametrize("base_required", [True, False])
def test_missing_new_row_rejected_under_both_flags(minimal_vault_config_dict, base_required):
    """The ingestion transition is required regardless of the flag.

    Without it the landing state is undefined, and a substituted default
    could land documents in a state the vault never declared — so the
    requirement is unconditional, not part of the base-required layer.
    """
    mutated = _lifecycle_variant(minimal_vault_config_dict)
    mutated["lifecycle"]["base_states_required"] = base_required
    mutated["lifecycle"]["transitions"] = [
        t for t in mutated["lifecycle"]["transitions"] if t["from_state"] != "(new)"
    ]
    with pytest.raises(ValidationError, match=r"\(new\)"):
        VaultConfig.model_validate(mutated)


@pytest.mark.parametrize("base_required", [True, False])
def test_ingest_reserved_for_new_row(minimal_vault_config_dict, base_required):
    """`ingest` on any non-`(new)` row is rejected regardless of the flag.

    Such a row would make `ingest` user-invocable — enter the transition
    table and the known-action roster — which the `(new)` handling exists
    to prevent. Under `base_states_required: true` it would also satisfy
    the base-action presence check by accident, so only a dedicated rule
    excludes it.
    """
    mutated = _lifecycle_variant(minimal_vault_config_dict)
    mutated["lifecycle"]["base_states_required"] = base_required
    mutated["lifecycle"]["transitions"].append(
        {"from_state": "archived", "action": "ingest", "to_state": "active"}
    )
    with pytest.raises(ValidationError, match="reserved"):
        VaultConfig.model_validate(mutated)


def test_on_disk_config_loads_leniently_with_warning(minimal_vault_config_dict, tmp_path, caplog):
    """A violating configuration already on disk loads with a warning.

    Rejecting it would drop the vault from the registry, unreachable by
    the surfaces that could repair it. The same dict fails direct
    validation, so the leniency is scoped to the load path — the pair of
    assertions excludes an implementation that simply stopped validating.
    """
    mutated = _lifecycle_variant(minimal_vault_config_dict)
    mutated["lifecycle"]["transitions"] = [
        t for t in mutated["lifecycle"]["transitions"] if t["action"] != "supersede"
    ]
    with pytest.raises(ValidationError, match="supersede"):
        VaultConfig.model_validate(mutated)

    config_path = tmp_path / "vault_config.yaml"
    config_path.write_text(yaml.safe_dump(mutated))
    with caplog.at_level(logging.WARNING, logger="sage.config"):
        config = load_vault_config(config_path)

    assert config.vault.id == mutated["vault"]["id"]
    assert any("supersede" in record.getMessage() for record in caplog.records), (
        "the lenient load must still surface the problem"
    )


def test_replacement_lifecycle_must_declare_a_satisfying_state(minimal_vault_config_dict):
    """`base_states_required: false` admits a full replacement lifecycle — once
    it says which of its states satisfies `depends_on`.

    A replacement lifecycle inherits no engine default: none of its states
    is `active` or `completed`, so leaving the field unset everywhere
    yields the empty set. The negative half catches that; the positive
    half excludes an implementation that rejects replacement lifecycles
    wholesale.
    """
    mutated = _lifecycle_variant(minimal_vault_config_dict)
    mutated["lifecycle"] = {
        "base_states_required": False,
        "states": [
            {"value": "sentinel_state", "label": "Sentinel State", "is_terminal": True},
        ],
        "transitions": [
            {"from_state": "(new)", "action": "ingest", "to_state": "sentinel_state"},
        ],
    }
    with pytest.raises(ValidationError, match="satisfies_dependency"):
        VaultConfig.model_validate(mutated)

    mutated["lifecycle"]["states"][0]["satisfies_dependency"] = True
    config = VaultConfig.model_validate(mutated)
    assert config.lifecycle.base_states_required is False
    assert config.lifecycle.dependency_satisfying_states() == frozenset({"sentinel_state"})


@pytest.mark.parametrize("base_required", [True, False])
def test_new_row_must_land_in_declared_state(minimal_vault_config_dict, base_required):
    """The `(new)` row's target must be a declared state, whatever the flag."""
    mutated = _lifecycle_variant(minimal_vault_config_dict)
    mutated["lifecycle"]["base_states_required"] = base_required
    for transition in mutated["lifecycle"]["transitions"]:
        if transition["from_state"] == "(new)":
            transition["to_state"] = "ghost"
    with pytest.raises(ValidationError, match="ghost"):
        VaultConfig.model_validate(mutated)


def test_multiple_new_rows_rejected(minimal_vault_config_dict):
    """Two `(new)` rows are ambiguous and rejected."""
    mutated = _lifecycle_variant(minimal_vault_config_dict)
    mutated["lifecycle"]["transitions"].append(
        {"from_state": "(new)", "action": "ingest", "to_state": "archived"}
    )
    with pytest.raises(ValidationError, match=r"\(new\)"):
        VaultConfig.model_validate(mutated)


def test_new_row_action_must_be_ingest(minimal_vault_config_dict):
    """A `(new)` row carrying any action but `ingest` is rejected."""
    mutated = _lifecycle_variant(minimal_vault_config_dict)
    for transition in mutated["lifecycle"]["transitions"]:
        if transition["from_state"] == "(new)":
            transition["action"] = "file"
    with pytest.raises(ValidationError, match="ingest"):
        VaultConfig.model_validate(mutated)


def test_base_lifecycle_fixture_still_validates(minimal_config, extended_config):
    """The canonical fixtures pass both validator layers untouched.

    The blast-radius canary: a validator that over-reaches (exact base
    rows, forbidding extensions) trips here before it trips anywhere
    else in the suite.
    """
    assert minimal_config.lifecycle.base_states_required is True
    assert extended_config.lifecycle.base_states_required is True


def _schema_errors(lifecycle: dict) -> list[str]:
    validator = jsonschema.Draft202012Validator(_LIFECYCLE_SCHEMA)
    return [e.message for e in validator.iter_errors(lifecycle)]


def test_schema_expresses_the_new_row_invariants(minimal_vault_config_dict):
    """The JSON Schema carries the structural `(new)`-row invariants.

    Parity guard between the two authorities: the loader enforces the
    invariants in Pydantic, and the schema must express the structural
    subset (exactly one `(new)` row, `ingest` reserved to it) rather than
    silently accepting what the loader rejects. Each case asserts the
    schema itself fires, so a schema that dropped the constraints while
    the loader kept them turns this red.
    """
    base = _lifecycle_variant(minimal_vault_config_dict)["lifecycle"]
    assert _schema_errors(base) == []

    duplicate = copy.deepcopy(base)
    duplicate["transitions"].append(
        {"from_state": "(new)", "action": "ingest", "to_state": "archived"}
    )
    assert _schema_errors(duplicate), "two '(new)' rows must fail maxContains"

    missing = copy.deepcopy(base)
    missing["transitions"] = [t for t in missing["transitions"] if t["from_state"] != "(new)"]
    assert _schema_errors(missing), "a missing '(new)' row must fail minContains"

    stray = copy.deepcopy(base)
    stray["transitions"].append(
        {"from_state": "archived", "action": "ingest", "to_state": "active"}
    )
    assert _schema_errors(stray), "'ingest' outside the '(new)' row must fail the item conditional"

    declared = copy.deepcopy(base)
    for state in declared["states"]:
        if state["value"] == "completed":
            state["satisfies_dependency"] = False
        if state["value"] == "active":
            state["satisfies_dependency"] = True
    assert _schema_errors(declared) == [], "satisfies_dependency declarations must validate"


@pytest.mark.parametrize("endpoint", ["to_state", "from_state"])
def test_transition_endpoint_must_be_declared_state(minimal_vault_config_dict, endpoint):
    """Every transition endpoint must name a declared state, not just `(new)`'s.

    Mutates a non-`(new)` row deliberately: the `(new)` row's target was
    already checked, so a test written there would pass against a
    validator that never looks past the ingestion row. A typo on any
    other row strands documents in a state with no valid actions,
    unrecoverable through the API.
    """
    mutated = _lifecycle_variant(minimal_vault_config_dict)
    for transition in mutated["lifecycle"]["transitions"]:
        if transition["from_state"] == "active" and transition["action"] == "archive":
            transition[endpoint] = "archved"
    with pytest.raises(ValidationError, match="archved"):
        VaultConfig.model_validate(mutated)


def test_to_state_may_not_be_the_new_pseudo_state(minimal_vault_config_dict):
    """`(new)` is sanctioned as a source only, never as a target.

    A transition landing a document in the ingestion pseudo-state would
    put it in a state the table drops on construction. An implementation
    that sanctions `(new)` on both endpoints passes the declared-state
    sweep and fails here.
    """
    mutated = _lifecycle_variant(minimal_vault_config_dict)
    for transition in mutated["lifecycle"]["transitions"]:
        if transition["from_state"] == "active" and transition["action"] == "archive":
            transition["to_state"] = "(new)"
    with pytest.raises(ValidationError, match=r"\(new\)"):
        VaultConfig.model_validate(mutated)


def test_endpoint_violation_loads_leniently_with_warning(
    minimal_vault_config_dict, tmp_path, caplog
):
    """An undeclared endpoint already on disk warns rather than dropping the vault.

    Paired assertions, as with the base-action case: the same dict fails
    direct validation, so an implementation that simply stopped checking
    fails the first half and one that rejects everywhere fails the second.
    The surviving transition is asserted too: leniency means loading the
    file as written, so an implementation that warned and then quietly
    dropped or rewrote the offending row — leaving the loaded table
    different from the file the operator is about to repair — passes both
    halves and fails here.
    """
    mutated = _lifecycle_variant(minimal_vault_config_dict)
    for transition in mutated["lifecycle"]["transitions"]:
        if transition["from_state"] == "active" and transition["action"] == "archive":
            transition["to_state"] = "archved"
    with pytest.raises(ValidationError, match="archved"):
        VaultConfig.model_validate(mutated)

    config_path = tmp_path / "vault_config.yaml"
    config_path.write_text(yaml.safe_dump(mutated))
    with caplog.at_level(logging.WARNING, logger="sage.config"):
        config = load_vault_config(config_path)

    assert config.vault.id == mutated["vault"]["id"]
    assert any(t.to_state == "archved" for t in config.lifecycle.transitions), (
        "the lenient load must keep the configuration as written, not repair it"
    )
    assert any("archved" in record.getMessage() for record in caplog.records), (
        "the lenient load must still name the offending state"
    )


def test_empty_dependency_satisfying_set_rejected(minimal_vault_config_dict):
    """A lifecycle where no state satisfies `depends_on` is rejected.

    Every `depends_on` precondition would fail permanently, with nothing
    naming the configuration as the cause — so the configuration surface
    is where it is caught.
    """
    mutated = _lifecycle_variant(minimal_vault_config_dict)
    for state in mutated["lifecycle"]["states"]:
        if state["value"] in {"active", "completed"}:
            state["satisfies_dependency"] = False
    with pytest.raises(ValidationError, match="satisfies_dependency"):
        VaultConfig.model_validate(mutated)


def test_empty_satisfying_set_loads_leniently_with_warning(
    minimal_vault_config_dict, tmp_path, caplog
):
    """The same configuration on disk warns and loads.

    Same lenient-load posture the other layers take: a vault that cannot
    satisfy dependencies is still reachable by the surface that repairs it.
    """
    mutated = _lifecycle_variant(minimal_vault_config_dict)
    for state in mutated["lifecycle"]["states"]:
        if state["value"] in {"active", "completed"}:
            state["satisfies_dependency"] = False

    config_path = tmp_path / "vault_config.yaml"
    config_path.write_text(yaml.safe_dump(mutated))
    with caplog.at_level(logging.WARNING, logger="sage.config"):
        config = load_vault_config(config_path)

    assert config.lifecycle.dependency_satisfying_states() == frozenset()
    assert any("satisfies_dependency" in record.getMessage() for record in caplog.records), (
        "the lenient load must still name the cause"
    )


def test_schema_expresses_transition_endpoint_patterns(minimal_vault_config_dict):
    """The schema carries the endpoint shapes it can express, and no more.

    `to_state` is always a real state identifier and `from_state` is that
    or the `(new)` literal, both expressible as patterns. Whether an
    endpoint is *declared* is cross-array referential integrity, which
    JSON Schema cannot state — the final case pins that division so the
    two authorities are not read as diverging.
    """
    base = _lifecycle_variant(minimal_vault_config_dict)["lifecycle"]
    assert _schema_errors(base) == []

    landing_in_pseudo_state = copy.deepcopy(base)
    for transition in landing_in_pseudo_state["transitions"]:
        if transition["from_state"] == "active" and transition["action"] == "archive":
            transition["to_state"] = "(new)"
    assert _schema_errors(landing_in_pseudo_state), "'(new)' as a target must fail the pattern"

    malformed_source = copy.deepcopy(base)
    for transition in malformed_source["transitions"]:
        if transition["from_state"] == "active" and transition["action"] == "archive":
            transition["from_state"] = "Not A State"
    assert _schema_errors(malformed_source), "a malformed source identifier must fail the pattern"

    undeclared = copy.deepcopy(base)
    for transition in undeclared["transitions"]:
        if transition["from_state"] == "active" and transition["action"] == "archive":
            transition["to_state"] = "archved"
    assert _schema_errors(undeclared) == [], (
        "endpoint declaration is a loader-only invariant; the schema cannot express it"
    )
