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
    string enum of `local` and `cloud`, defaulting to `local`
    (deployment-profile marker, CAS-ADR-042).

    Catches drift in any single property — a missing `enum` makes SCH-S-006
    incapable of detecting an unknown profile value; a missing `default` makes
    SCH-S-007's Pydantic default test depend on the Pydantic side alone.
    """
    schema = _stack_schema()
    profile = schema["properties"]["profile"]
    assert profile["type"] == "string"
    assert profile["enum"] == ["local", "cloud"]
    assert profile["default"] == "local"


def test_sch_s_006_unknown_profile_value_rejected():
    """Both registered profiles (`local`, `cloud`) validate against the schema
    *and* SageCoreConfig; a value outside the enum is rejected by both gates.

    Anti-coincidental-pass: the positive controls must pass so the failure is
    attributable to the enum and not to some unrelated constraint. A never-
    registered value (`hybrid`) stands in for the unknown-profile case now that
    `cloud` is valid; a typo leaving `profile` a bare `"type": "string"` (no
    enum) would silently accept it.
    """
    from pydantic import ValidationError

    from sage.config import SageCoreConfig

    schema = _stack_schema()

    for good in ("local", "cloud"):
        jsonschema.validate({"profile": good}, schema)
        SageCoreConfig.model_validate({"profile": good})

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"profile": "hybrid"}, schema)
    with pytest.raises(ValidationError):
        SageCoreConfig.model_validate({"profile": "hybrid"})


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
    value with `jsonschema.ValidationError`; a `cloud` config loads and exposes
    `profile == "cloud"`.

    Anti-coincidental-pass: assert specifically `jsonschema.ValidationError`.
    If the loader skipped `jsonschema.validate` and relied on Pydantic alone,
    an unknown profile would surface as a different error type (a Pydantic
    `ValidationError`), so this asserts the schema gate runs at startup — the
    same path the running deployment takes. A never-registered value (`hybrid`)
    is the unknown-profile case now that `cloud` is a valid enum member.
    """
    from sage.config import load_sage_core_config

    bad = tmp_path / "bad_config.yaml"
    bad.write_text("profile: hybrid\nabstraction:\n  provider: stub\n  model: null\n")
    with pytest.raises(jsonschema.ValidationError):
        load_sage_core_config(bad)

    good = tmp_path / "good_config.yaml"
    good.write_text("profile: cloud\nabstraction:\n  provider: stub\n  model: null\n")
    cfg = load_sage_core_config(good)
    assert cfg.profile == "cloud"


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


# ---------------------------------------------------------------------------
# Storage-backend selector (CAS-ADR-042). SCH-S-013..015.
# ---------------------------------------------------------------------------


def test_sch_s_013_storage_backend_field_shape():
    """Structural assertion: the schema declares a top-level `storage_backend`
    as a string enum with the sole value `postgres`, defaulting to `postgres`.

    Catches drift in any single property — a missing `enum` makes SCH-S-014
    incapable of detecting a retired backend value; a missing `default` makes
    SCH-S-015's Pydantic default test depend on the Pydantic side alone.
    """
    schema = _stack_schema()
    backend = schema["properties"]["storage_backend"]
    assert backend["type"] == "string"
    assert backend["enum"] == ["postgres"]
    assert backend["default"] == "postgres"


def test_sch_s_014_unknown_storage_backend_rejected():
    """A stack config whose `storage_backend` is outside the enum fails
    validation against the JSON Schema *and* against SageCoreConfig; the
    sole in-enum value passes both gates.

    Anti-coincidental-pass: the positive control must pass so the failure is
    attributable to the enum. `embedded` is asserted to fail specifically --
    the retired fallback binding must not silently re-validate now that the
    storage port has a single binding.
    """
    from pydantic import ValidationError

    from sage.config import SageCoreConfig

    schema = _stack_schema()

    jsonschema.validate({"storage_backend": "postgres"}, schema)
    SageCoreConfig.model_validate({"storage_backend": "postgres"})

    for bad in ("embedded", "lancedb", "sqlite"):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"storage_backend": bad}, schema)
        with pytest.raises(ValidationError):
            SageCoreConfig.model_validate({"storage_backend": bad})


def test_sch_s_015_storage_backend_absent_passes_default_applied():
    """An instance with `storage_backend` absent validates against the schema,
    and `SageCoreConfig.model_validate` applies the Pydantic default of
    `"postgres"`.

    Confirms the two single-source-of-truth points (JSON Schema and Pydantic
    model) agree on the default per CAS principle 1: the local profile binds
    Postgres, the storage port's sole binding.
    """
    schema = _stack_schema()
    instance: dict = {}  # storage_backend deliberately absent
    jsonschema.validate(instance, schema)
    assert schema["properties"]["storage_backend"]["default"] == "postgres"

    from sage.config import SageCoreConfig

    cfg = SageCoreConfig.model_validate(instance)
    assert cfg.storage_backend == "postgres"


# ---------------------------------------------------------------------------
# Vault-source-store selector (CAS-ADR-043). SCH-S-016..018.
# ---------------------------------------------------------------------------


def test_sch_s_016_vault_source_backend_field_shape():
    """Structural assertion: the schema declares a top-level
    `vault_source_backend` as a string enum of `filesystem` and
    `document_store`, defaulting to `filesystem`.

    Catches drift in any single property — a missing `enum` makes SCH-S-017
    incapable of detecting an unknown value; a missing `default` makes
    SCH-S-018's Pydantic default test depend on the Pydantic side alone.
    """
    schema = _stack_schema()
    backend = schema["properties"]["vault_source_backend"]
    assert backend["type"] == "string"
    assert backend["enum"] == ["filesystem", "document_store"]
    assert backend["default"] == "filesystem"


def test_sch_s_017_unknown_vault_source_backend_rejected():
    """A stack config whose `vault_source_backend` is outside the enum fails
    validation against the JSON Schema *and* against SageCoreConfig; both
    in-enum values pass both gates.

    Anti-coincidental-pass: the positive controls must pass so the failure is
    attributable to the enum. `sharepoint` is asserted to fail specifically —
    the concrete cloud binding is selected as one coherent binding
    (`document_store`), not by naming a particular document-store product, so a
    product-name value passing would mean the selector leaked an implementation
    detail into the contract.
    """
    from pydantic import ValidationError

    from sage.config import SageCoreConfig

    schema = _stack_schema()

    for good in ("filesystem", "document_store"):
        jsonschema.validate({"vault_source_backend": good}, schema)
        SageCoreConfig.model_validate({"vault_source_backend": good})

    for bad in ("sharepoint", "s3"):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"vault_source_backend": bad}, schema)
        with pytest.raises(ValidationError):
            SageCoreConfig.model_validate({"vault_source_backend": bad})


def test_sch_s_018_vault_source_backend_absent_passes_default_applied():
    """An instance with `vault_source_backend` absent validates against the
    schema, and `SageCoreConfig.model_validate` applies the Pydantic default of
    `"filesystem"`.

    Confirms the two single-source-of-truth points (JSON Schema and Pydantic
    model) agree on the default per CAS principle 1: the vault-source store
    binds the local filesystem unless the config selects the document store.
    """
    schema = _stack_schema()
    instance: dict = {}  # vault_source_backend deliberately absent
    jsonschema.validate(instance, schema)
    assert schema["properties"]["vault_source_backend"]["default"] == "filesystem"

    from sage.config import SageCoreConfig

    cfg = SageCoreConfig.model_validate(instance)
    assert cfg.vault_source_backend == "filesystem"


def test_sch_s_019_document_store_block_shape():
    """The schema declares a top-level `document_store` object with
    `additionalProperties: false` and the four coordinate properties the
    document-store binding reads.

    Catches drift in the block shape — a missing `additionalProperties: false`
    would make SCH-S-020 incapable of detecting a stray/typo'd key, and a
    dropped property would silently strip a coordinate the binding needs.
    """
    schema = _stack_schema()
    block = schema["properties"]["document_store"]
    assert block["type"] == "object"
    assert block["additionalProperties"] is False
    assert set(block["properties"]) == {"site_id", "drive_id", "root_path", "graph_scope"}


def test_sch_s_020_document_store_unknown_property_rejected():
    """A `document_store` block carrying an unknown property fails validation
    against the JSON Schema *and* against SageCoreConfig; a block of only known
    properties passes both gates.

    Anti-coincidental-pass: the positive control (a fully-specified known block)
    must pass so the failure is attributable to the unknown key, not to an
    unrelated shape error. `additionalProperties: false` on the schema and
    `extra="forbid"` on the model are the two single-source-of-truth points that
    must agree.
    """
    from pydantic import ValidationError

    from sage.config import SageCoreConfig

    schema = _stack_schema()

    good = {
        "document_store": {
            "site_id": "contoso.sharepoint.com,site-guid,web-guid",
            "drive_id": "b!drive-id",
            "root_path": "vaults",
            "graph_scope": "https://graph.microsoft.com/.default",
        }
    }
    jsonschema.validate(good, schema)
    SageCoreConfig.model_validate(good)

    bad = {"document_store": {"site_id": "s", "library_name": "Documents"}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)
    with pytest.raises(ValidationError):
        SageCoreConfig.model_validate(bad)


def test_sch_s_021_document_store_absent_passes_defaults_applied():
    """An instance with `document_store` absent validates against the schema,
    and `SageCoreConfig.model_validate` applies the Pydantic defaults
    (`root_path="vaults"`, the Graph `.default` scope, null site/drive ids).

    Confirms the JSON Schema and the Pydantic model agree on the block's
    defaults per CAS principle 1, so an instance that omits the block (every
    filesystem-backed deployment) is valid and the document-store coordinates
    default rather than fail.
    """
    schema = _stack_schema()
    instance: dict = {}  # document_store deliberately absent
    jsonschema.validate(instance, schema)

    block = schema["properties"]["document_store"]["properties"]
    assert block["root_path"]["default"] == "vaults"
    assert block["graph_scope"]["default"] == "https://graph.microsoft.com/.default"
    assert block["site_id"]["default"] is None
    assert block["drive_id"]["default"] is None

    from sage.config import SageCoreConfig

    cfg = SageCoreConfig.model_validate(instance)
    assert cfg.document_store.root_path == "vaults"
    assert cfg.document_store.graph_scope == "https://graph.microsoft.com/.default"
    assert cfg.document_store.site_id is None
    assert cfg.document_store.drive_id is None


def test_sch_s_022_transfer_block_shape():
    """The schema declares a top-level `transfer` object with
    `additionalProperties: false` and the two properties the transfer-recipe
    minting path reads: `public_base_url` (nullable string, default null) and
    `token_ttl_seconds` (integer, default 300).

    Catches drift in the block shape -- a missing `additionalProperties: false`
    would make SCH-S-023 incapable of detecting a stray/typo'd key, and a
    dropped property would silently strip a coordinate minting needs.
    """
    schema = _stack_schema()
    block = schema["properties"]["transfer"]
    assert block["type"] == "object"
    assert block["additionalProperties"] is False
    assert set(block["properties"]) == {"public_base_url", "token_ttl_seconds"}
    assert block["properties"]["public_base_url"]["default"] is None
    assert block["properties"]["token_ttl_seconds"]["default"] == 300


def test_sch_s_023_transfer_unknown_property_rejected():
    """A `transfer` block carrying an unknown property fails validation against
    the JSON Schema *and* against SageCoreConfig; a block of only known
    properties passes both gates.

    Anti-coincidental-pass: the positive control (a fully-populated known
    block) must pass so the failure is attributable to the unknown key, not to
    a malformed block; both gates must refuse so the schema and the model
    cannot drift apart on strictness.
    """
    from pydantic import ValidationError

    from sage.config import SageCoreConfig

    schema = _stack_schema()

    good = {
        "transfer": {
            "public_base_url": "https://sage.example.org",
            "token_ttl_seconds": 120,
        }
    }
    jsonschema.validate(good, schema)
    cfg = SageCoreConfig.model_validate(good)
    assert cfg.transfer.public_base_url == "https://sage.example.org"
    assert cfg.transfer.token_ttl_seconds == 120

    bad = {"transfer": {"public_base_url": "https://x", "ttl": 5}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)
    with pytest.raises(ValidationError):
        SageCoreConfig.model_validate(bad)


def test_sch_s_024_transfer_absent_passes_defaults_applied():
    """An instance with `transfer` absent validates against the schema, and
    `SageCoreConfig.model_validate` applies the Pydantic defaults (no public
    base URL, 300-second token TTL).

    Confirms the JSON Schema and the Pydantic model agree on the block's
    defaults per CAS principle 1: a deployment that never mints recipes (every
    co-located deployment) is valid without the block, and one that does mint
    fails loud at mint time -- not at load time -- when the URL is absent.
    """
    schema = _stack_schema()
    instance: dict = {}  # transfer deliberately absent
    jsonschema.validate(instance, schema)

    from sage.config import SageCoreConfig

    cfg = SageCoreConfig.model_validate(instance)
    assert cfg.transfer.public_base_url is None
    assert cfg.transfer.token_ttl_seconds == 300
