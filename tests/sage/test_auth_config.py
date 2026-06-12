"""Stack auth-config tests (CAS-ADR-042).

The OAuth resource-server binding is configured by an optional ``auth`` block
on the stack config. These guard that the block is absent by default (so the
on-box deployment runs with no auth), that it round-trips and rejects unknown
keys, and that the JSON Schema and the Pydantic model agree on its shape
(architectural principle 8).
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from sage.config import SageCoreConfig, StackAuthConfig
from sage.mcp_init import load_stack_config_or_default

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "fs" / "sage" / "sage_core_config.schema.json"
)


def _schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def test_a1_auth_absent_by_default() -> None:
    """A bare config and the committed config.yaml both carry no auth block.

    Regression guard for AC4: the local deployment is unaffected and runs
    without auth. A default that materialized an enabled block would break
    this.
    """
    assert SageCoreConfig().auth is None
    assert load_stack_config_or_default().auth is None


def test_a2_enabled_block_round_trips_and_forbids_unknown_keys() -> None:
    """An enabled auth block validates; an unknown sub-key is rejected.

    The model carries ``extra='forbid'`` so a typo'd field fails loudly at
    config load rather than being silently ignored.
    """
    cfg = SageCoreConfig.model_validate(
        {"auth": {"enabled": True, "tenant_id": "tid", "audience": "api://sage"}}
    )
    assert isinstance(cfg.auth, StackAuthConfig)
    assert cfg.auth.enabled is True
    assert cfg.auth.audience == "api://sage"
    # Defaults preserved.
    assert cfg.auth.required_scopes == ["Sage.Access"]
    assert cfg.auth.required_roles == ["Sage.Reader"]

    with pytest.raises(ValidationError):
        SageCoreConfig.model_validate({"auth": {"enabled": True, "bogus": 1}})


def test_a3_schema_matches_model_on_auth_block() -> None:
    """The JSON Schema admits the enabled example and rejects an unknown key.

    Cross-checks the schema against the same payloads as A2 so the schema and
    the Pydantic model cannot drift apart (principle 8); ``additionalProperties:
    false`` on the auth object is what rejects the unknown key.
    """
    schema = _schema()
    jsonschema.validate(
        {"auth": {"enabled": True, "tenant_id": "tid", "audience": "api://sage"}},
        schema,
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"auth": {"bogus": 1}}, schema)
