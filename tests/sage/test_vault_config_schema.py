"""Tests for the JSON Schema definition of vault_config.schema.json.

Post-T-0103 / CAS-ADR-030: the per-vault `abstraction` block no longer
carries `provider` or `model` (those moved to stack scope; see
test_sage_core_config_schema.py). The block still carries `enabled` and
the density-proportional token-budget fields, and is still
`additionalProperties: false` so any vault YAML that retains the moved
fields fails loudly at startup.
"""

import json
from pathlib import Path

import jsonschema
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VAULT_CONFIG_SCHEMA_PATH = _REPO_ROOT / "docs" / "fs" / "sage" / "vault_config.schema.json"


def _abstraction_schema() -> dict:
    """Return the inline `abstraction` sub-schema from vault_config.schema.json."""
    full = json.loads(_VAULT_CONFIG_SCHEMA_PATH.read_text())
    return full["properties"]["abstraction"]


def test_sch_001_provider_field_removed_from_vault_schema():
    """The vault `abstraction` block must not declare `provider` or `model`
    (CAS-ADR-030 moved both to stack scope). Anti-coincidence: if the bump
    is forgotten or partial, this test fails.
    """
    schema = _abstraction_schema()
    properties = schema.get("properties", {})
    assert "provider" not in properties
    assert "model" not in properties


def test_sch_002_unknown_field_rejected_at_vault_scope():
    """`additionalProperties: false` enforces the new boundary: any vault
    YAML still carrying `provider` or `model` fails validation loudly. The
    schema strictness IS the migration safety net per ADR-030.
    """
    schema = _abstraction_schema()
    assert schema["additionalProperties"] is False

    instance = {
        "enabled": True,
        "provider": "qwen3-mlx",  # legacy field; must be rejected
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance, schema)


def test_sch_003_vault_abstraction_block_keeps_vault_scope_fields():
    """Sanity check: the fields that legitimately live per-vault remain
    declared and validate as expected. Guards against accidental over-
    aggressive deletion that would remove vault-scope knobs.
    """
    schema = _abstraction_schema()
    properties = schema.get("properties", {})
    assert "enabled" in properties
    assert "max_abstract_tokens" in properties
    assert "base_abstract_tokens" in properties
    assert "tokens_per_word" in properties

    instance = {
        "enabled": True,
        "max_abstract_tokens": 500,
        "base_abstract_tokens": 150,
        "tokens_per_word": 0.02,
    }
    jsonschema.validate(instance, schema)
