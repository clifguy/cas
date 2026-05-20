"""Tests for the JSON Schema definition of vault_config.schema.json.

Focused on the ``abstraction`` block discriminator added by T-0099. The
``abstraction.provider`` field is an enum-constrained string with a schema
default of ``"qwen3-mlx"``. Both the raw JSON Schema and the derived
Pydantic model (Principle 8 in CLAUDE.md) must agree on the contract.
"""

import json
from pathlib import Path

import jsonschema
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VAULT_CONFIG_SCHEMA_PATH = _REPO_ROOT / "docs" / "fs" / "sage" / "vault_config.schema.json"


def _abstraction_schema() -> dict:
    """Return the inline ``abstraction`` sub-schema from vault_config.schema.json.

    The block is defined inline (not behind a ``$ref``), so validation
    against just this sub-tree does not need a registry of sibling
    schemas. Keeps the test independent of $ref resolution machinery.
    """
    full = json.loads(_VAULT_CONFIG_SCHEMA_PATH.read_text())
    return full["properties"]["abstraction"]


def test_sch_001_unknown_provider_rejected():
    """An ``abstraction.provider`` value outside the enum fails validation.

    Anti-coincidental-pass: assert that an unknown value (``"ollama"``)
    fails, not just that any value fails. A typo that left the field as
    bare ``"type": "string"`` (no enum) would silently accept this.
    """
    schema = _abstraction_schema()
    instance = {
        "enabled": True,
        "model": "mlx-community/test",
        "provider": "ollama",
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance, schema)


def test_sch_002_provider_absent_passes_and_pydantic_applies_default():
    """An abstraction block with ``provider`` absent validates against the
    schema, and ``AbstractionConfig.model_validate`` applies the
    Pydantic-side default of ``"qwen3-mlx"``.

    Confirms the two single-source-of-truth points (JSON Schema and
    Pydantic model) agree on the default per CAS principle 1.
    """
    schema = _abstraction_schema()
    instance = {
        "enabled": True,
        "model": "mlx-community/test",
        # provider deliberately absent
    }
    # Should validate without error.
    jsonschema.validate(instance, schema)

    from sage.config import AbstractionConfig

    cfg = AbstractionConfig.model_validate(instance)
    assert cfg.provider == "qwen3-mlx"


def test_sch_provider_field_shape():
    """Structural check: the schema declares ``provider`` as a string enum
    with the documented values and a ``qwen3-mlx`` default.

    Catches drift in any single property — a missing ``enum`` makes
    SCH-001 incapable of detecting unknown values; a missing ``default``
    makes SCH-002's Pydantic default test depend on the Pydantic side
    alone.
    """
    schema = _abstraction_schema()
    provider = schema["properties"]["provider"]
    assert provider["type"] == "string"
    assert provider["enum"] == ["qwen3-mlx", "stub"]
    assert provider["default"] == "qwen3-mlx"
