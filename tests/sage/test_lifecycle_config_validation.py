"""`base_states_required` is enforced at config load, not merely declared.

`LifecycleConfig` validates two layers on construction. Unconditionally,
the ingestion pseudo-row must be well-formed: at most one `(new)` row, its
action `ingest`, its target a declared state — the invariants that make
the ingest landing state readable. When `base_states_required` is true
(the default), the base states must be declared and each base action used
by at least one transition, which is the promise the flag's description
has always made. A configuration that drops part of the base lifecycle
while claiming the base is required is rejected instead of silently
accepted.

Action presence is checked rather than exact base rows: a vault may route
a base action through extra or different states (extra supersede sources,
a non-`active` ingest landing state) without tripping the validator.
"""

import copy

import pytest
from pydantic import ValidationError

from sage.config import VaultConfig


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


def test_base_required_rejects_missing_new_row(minimal_vault_config_dict):
    """A base-required config must declare the ingestion transition."""
    mutated = _lifecycle_variant(minimal_vault_config_dict)
    mutated["lifecycle"]["transitions"] = [
        t for t in mutated["lifecycle"]["transitions"] if t["from_state"] != "(new)"
    ]
    with pytest.raises(ValidationError, match="ingest"):
        VaultConfig.model_validate(mutated)


def test_base_not_required_allows_replacement_lifecycle(minimal_vault_config_dict):
    """`base_states_required: false` admits a full replacement lifecycle."""
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
    config = VaultConfig.model_validate(mutated)
    assert config.lifecycle.base_states_required is False


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
