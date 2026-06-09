"""Tests for the JSON Schema definition of sage_core_config.schema.json.

The stack-wide SAGE Core API configuration schema (CAS-ADR-030).
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
    validation against the stack schema; the in-enum values pass.

    Anti-coincidental-pass: an unknown value (``"ollama"``) must fail, not
    just any string. A typo that left the field as bare ``"type": "string"``
    (no enum) would silently accept it. The retired ``"qwen3-mlx"`` value is
    asserted to fail too, proving the rename to ``"local-mlx"`` took (not just
    that ``"anthropic"`` was added alongside the old key).
    """
    schema = _abstraction_schema()

    # Positive controls: every documented enum value validates.
    for good in ("local-mlx", "anthropic", "stub"):
        jsonschema.validate({"provider": good, "model": "x"}, schema)

    # Unknown value and the retired pre-rename key both fail.
    for bad in ("ollama", "qwen3-mlx"):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"provider": bad, "model": "mlx-community/test"}, schema)


def test_sch_s_002_stack_provider_absent_passes_default_applied():
    """An abstraction block with `provider` absent validates against the
    schema, and `StackAbstractionConfig.model_validate` applies the
    Pydantic default of `"local-mlx"`.

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
    assert cfg.provider == "local-mlx"


def test_sch_s_003_stack_additional_properties_rejected():
    """The stack `abstraction` block forbids unknown properties. A field
    that legitimately lives at vault scope (e.g., `max_abstract_tokens`)
    must NOT be accepted at stack scope.

    Anti-coincidental-pass: catches a future caller who tries to mix scopes
    by setting a vault-only knob in the stack config.
    """
    schema = _abstraction_schema()
    instance = {
        "provider": "local-mlx",
        "model": "mlx-community/test",
        "max_abstract_tokens": 500,  # vault-scope field; must be rejected here
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance, schema)


def test_sch_s_004_stack_provider_field_shape():
    """Structural assertion: the schema declares `provider` as a string
    enum with the documented values and a `local-mlx` default, and `model`
    as nullable string with default null.

    Catches drift in any single property — a missing `enum` makes
    SCH-S-001 incapable of detecting unknown values; a missing `default`
    makes SCH-S-002's Pydantic default test depend on the Pydantic side
    alone.
    """
    schema = _abstraction_schema()
    provider = schema["properties"]["provider"]
    assert provider["type"] == "string"
    assert provider["enum"] == ["local-mlx", "anthropic", "stub"]
    assert provider["default"] == "local-mlx"

    model = schema["properties"]["model"]
    # nullable string: declared as either ["string", "null"] or via oneOf
    assert "string" in model["type"] and "null" in model["type"]
    assert model["default"] is None

    # additionalProperties: false enforces the scope boundary (SCH-S-003).
    assert schema["additionalProperties"] is False


def test_sch_s_005_profile_field_shape():
    """Structural assertion: the schema declares a top-level `profile` as a
    string enum whose sole value is `local`, defaulting to `local`
    (deployment-profile marker, CAS-ADR-042).

    Catches drift in any single property — a missing `enum` makes SCH-S-006
    incapable of detecting an unknown profile value; a missing `default` makes
    SCH-S-007's Pydantic default test depend on the Pydantic side alone.
    """
    schema = _stack_schema()
    profile = schema["properties"]["profile"]
    assert profile["type"] == "string"
    assert profile["enum"] == ["local"]
    assert profile["default"] == "local"


def test_sch_s_006_unknown_profile_value_rejected():
    """A stack config whose top-level `profile` is outside the enum fails
    validation against the stack schema; the in-range `local` validates.

    Anti-coincidental-pass: the positive control (`profile: local`) must pass,
    so the failure is attributable to the enum and not to some unrelated
    constraint. A typo leaving `profile` a bare `"type": "string"` (no enum)
    would silently accept `cloud`.
    """
    schema = _stack_schema()

    # Positive control: the only value the resolver assembles today validates.
    jsonschema.validate({"profile": "local"}, schema)

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"profile": "cloud"}, schema)


def test_sch_s_007_profile_absent_passes_default_applied():
    """An instance with `profile` absent validates against the schema, and
    `SageCoreConfig.model_validate` applies the Pydantic default of `"local"`.

    Confirms the two single-source-of-truth points (JSON Schema and Pydantic
    model) agree on the default per CAS principle 1.
    """
    schema = _stack_schema()
    instance: dict = {}  # profile deliberately absent
    jsonschema.validate(instance, schema)

    from sage.config import SageCoreConfig

    cfg = SageCoreConfig.model_validate(instance)
    assert cfg.profile == "local"


def test_sch_s_008_loader_fails_loud_on_unknown_profile(tmp_path):
    """`load_sage_core_config` (the startup loader) rejects an unknown profile
    value with `jsonschema.ValidationError`; a `local` config loads and exposes
    `profile == "local"`.

    Anti-coincidental-pass: assert specifically `jsonschema.ValidationError`.
    If the loader skipped `jsonschema.validate` and relied on Pydantic alone,
    an unknown profile would surface as a different error type (a Pydantic
    `ValidationError`), so this asserts the schema gate runs at startup — the
    same path the running deployment takes.
    """
    from sage.config import load_sage_core_config

    bad = tmp_path / "bad_config.yaml"
    bad.write_text("profile: cloud\nabstraction:\n  provider: stub\n  model: null\n")
    with pytest.raises(jsonschema.ValidationError):
        load_sage_core_config(bad)

    good = tmp_path / "good_config.yaml"
    good.write_text("profile: local\nabstraction:\n  provider: stub\n  model: null\n")
    cfg = load_sage_core_config(good)
    assert cfg.profile == "local"


# ---------------------------------------------------------------------------
# Postgres storage-engine connection block (CAS-ADR-042). SCH-S-009..012.
# ---------------------------------------------------------------------------


def _postgres_schema() -> dict:
    return _stack_schema()["properties"]["postgres"]


def test_sch_s_009_postgres_additional_properties_rejected():
    """The stack `postgres` block forbids unknown properties at both gates.

    Anti-coincidental-pass: the positive controls (a known field) must validate,
    while an unknown key is rejected by the JSON Schema *and* by
    StackPostgresConfig (model_config extra='forbid'). If additionalProperties
    were relaxed, or the model defaulted to extra='ignore', a typo'd connection
    field would pass silently.
    """
    from pydantic import ValidationError

    from sage.config import StackPostgresConfig

    schema = _postgres_schema()
    jsonschema.validate({"database": "sage"}, schema)  # positive control
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"bogus": "x"}, schema)

    StackPostgresConfig.model_validate({"database": "sage"})  # positive control
    with pytest.raises(ValidationError):
        StackPostgresConfig.model_validate({"bogus": "x"})


def test_sch_s_010_postgres_absent_passes_defaults_applied():
    """An instance with `postgres` absent validates against the schema, and
    SageCoreConfig applies the documented socket-default connection parameters.
    """
    jsonschema.validate({}, _stack_schema())

    from sage.config import SageCoreConfig

    pg = SageCoreConfig.model_validate({}).postgres
    assert pg.host is None and pg.user is None and pg.sslmode is None
    assert pg.port == 5432 and pg.database == "sage"
    assert pg.min_pool_size == 1 and pg.max_pool_size == 10
    assert pg.extensions == ["vector", "pgstattuple"]


def test_sch_s_011_postgres_full_block_agrees_schema_and_model():
    """A fully-specified `postgres` block validates against the JSON Schema and
    round-trips through StackPostgresConfig with identical values (Principle 8).
    """
    block = {
        "host": "db.example",
        "port": 6432,
        "database": "sage_cloud",
        "user": "svc",
        "sslmode": "require",
        "min_pool_size": 2,
        "max_pool_size": 20,
        "extensions": ["vector", "pgstattuple", "pg_repack"],
    }
    jsonschema.validate({"postgres": block}, _stack_schema())

    from sage.config import StackPostgresConfig

    pg = StackPostgresConfig.model_validate(block)
    assert pg.model_dump() == block


def test_sch_s_012_postgres_config_carries_no_secret():
    """StackPostgresConfig has no password/DSN field: a credential cannot be
    sourced from configuration -- the pool reads it from the environment.
    """
    from sage.config import StackPostgresConfig

    fields = set(StackPostgresConfig.model_fields)
    assert "password" not in fields
    assert "dsn" not in fields
