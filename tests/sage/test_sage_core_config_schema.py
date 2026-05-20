"""Tests for the JSON Schema definition of sage_core_config.schema.json.

The stack-wide SAGE Core API configuration schema (CAS-ADR-030, T-0103).
Carries the abstraction provider and model identifier that the per-vault
schema no longer owns. Mirror discipline of test_vault_config_schema.py:
both the raw JSON Schema and the derived Pydantic model
(`StackAbstractionConfig`, Principle 8) must agree on the contract.

Test IDs follow SCH-S-NNN (Schema-Stack).
"""

import json
from pathlib import Path

import jsonschema
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_STACK_CONFIG_SCHEMA_PATH = _REPO_ROOT / "docs" / "fs" / "sage" / "sage_core_config.schema.json"


def _stack_schema() -> dict:
    return json.loads(_STACK_CONFIG_SCHEMA_PATH.read_text())


def _abstraction_schema() -> dict:
    return _stack_schema()["properties"]["abstraction"]


def test_sch_s_001_stack_unknown_provider_rejected():
    """A stack-config `abstraction.provider` value outside the enum fails
    validation against the stack schema.

    Anti-coincidental-pass: an unknown value (``"ollama"``) must fail, not
    just any string. A typo that left the field as bare ``"type": "string"``
    (no enum) would silently accept it.
    """
    schema = _abstraction_schema()
    instance = {
        "provider": "ollama",
        "model": "mlx-community/test",
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance, schema)


def test_sch_s_002_stack_provider_absent_passes_default_applied():
    """An abstraction block with `provider` absent validates against the
    schema, and `StackAbstractionConfig.model_validate` applies the
    Pydantic default of `"qwen3-mlx"`.

    Confirms the two single-source-of-truth points (JSON Schema and
    Pydantic model) agree on the default per CAS principle 1.
    """
    schema = _abstraction_schema()
    instance = {
        "model": "mlx-community/test",
        # provider deliberately absent
    }
    jsonschema.validate(instance, schema)

    from sage.config import StackAbstractionConfig

    cfg = StackAbstractionConfig.model_validate(instance)
    assert cfg.provider == "qwen3-mlx"


def test_sch_s_003_stack_additional_properties_rejected():
    """The stack `abstraction` block forbids unknown properties. A field
    that legitimately lives at vault scope (e.g., `max_abstract_tokens`)
    must NOT be accepted at stack scope.

    Anti-coincidental-pass: catches a future caller who tries to mix scopes
    by setting a vault-only knob in the stack config.
    """
    schema = _abstraction_schema()
    instance = {
        "provider": "qwen3-mlx",
        "model": "mlx-community/test",
        "max_abstract_tokens": 500,  # vault-scope field; must be rejected here
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance, schema)


def test_sch_s_004_stack_provider_field_shape():
    """Structural assertion: the schema declares `provider` as a string
    enum with the documented values and a `qwen3-mlx` default, and `model`
    as nullable string with default null.

    Catches drift in any single property — a missing `enum` makes
    SCH-S-001 incapable of detecting unknown values; a missing `default`
    makes SCH-S-002's Pydantic default test depend on the Pydantic side
    alone.
    """
    schema = _abstraction_schema()
    provider = schema["properties"]["provider"]
    assert provider["type"] == "string"
    assert provider["enum"] == ["qwen3-mlx", "stub"]
    assert provider["default"] == "qwen3-mlx"

    model = schema["properties"]["model"]
    # nullable string: declared as either ["string", "null"] or via oneOf
    assert "string" in model["type"] and "null" in model["type"]
    assert model["default"] is None

    # additionalProperties: false enforces the scope boundary (SCH-S-003).
    assert schema["additionalProperties"] is False
