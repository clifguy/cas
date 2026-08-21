"""Vault config declares adapter parameters, never adapter availability.

Adapter availability is a process-wide capability fixed by the installed
adapter implementations, so vault configuration carries no adapter
declaration: no entry list, no enablement flag (CAS-ADR-046). What the
retired section did carry that was live -- the per-adapter projection
parameters that supply the vault-level base under a per-request ``config``
-- moves to the ``adapter_defaults`` object, keyed by source type.

The migration posture is tolerance, not rejection: vault configurations
live outside the repository and are cleaned operationally after the code
change deploys, so a loader that rejected the stale section would make a
vault silently unavailable during the transition. These tests pin the
tolerance, the warning that makes the stale section visible, and the fact
that the stale section is genuinely inert rather than quietly still read.
"""

import json
import logging
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from sage.config import VaultConfig, load_vault_config

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VAULT_CONFIG_SCHEMA_PATH = _REPO_ROOT / "docs" / "fs" / "sage" / "vault_config.schema.json"

#: The shape a pre-migration vault config carries: entries with an
#: enablement flag, a dead ``file_extensions`` key, and the live parameter
#: block whose contents are what ``adapter_defaults`` now holds.
_LEGACY_SECTION = {
    "adapters": [
        {
            "source_type": "docx",
            "enabled": True,
            "config": {
                "file_extensions": [".docx"],
                "heading_style_map": {"Custom Section": 1},
            },
        },
        {"source_type": "markdown", "enabled": False},
    ]
}


def _write_config(path: Path, config_dict: dict) -> Path:
    path.write_text(yaml.dump(config_dict, sort_keys=False))
    return path


def test_vault_config_declares_adapter_defaults_not_source_adapters():
    """The model carries ``adapter_defaults`` and no ``source_adapters``.

    Both halves matter: the absence alone would also hold for a model that
    had lost the parameter surface entirely, which is the regression the
    relocation exists to avoid.
    """
    fields = VaultConfig.model_fields
    assert "source_adapters" not in fields
    assert "adapter_defaults" in fields


def test_adapter_defaults_is_optional_and_defaults_empty(minimal_vault_config_dict):
    """A config that declares no adapter parameters validates, yielding {}."""
    minimal_vault_config_dict.pop("adapter_defaults", None)
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    assert config.adapter_defaults == {}


def test_legacy_source_adapters_section_is_ignored_not_rejected(minimal_vault_config_dict):
    """A config still carrying the retired section loads with it dropped.

    Asserting the attribute is *absent* is the load-bearing half: a bare
    "validation succeeded" assertion would also pass under
    ``extra="allow"``, which would keep the stale section reachable and
    invite a consumer to grow back.
    """
    minimal_vault_config_dict["source_adapters"] = _LEGACY_SECTION

    config = VaultConfig.model_validate(minimal_vault_config_dict)

    assert getattr(config, "source_adapters", None) is None
    assert config.adapter_defaults == {}


def test_legacy_source_adapters_section_logs_a_migration_warning(
    minimal_vault_config_dict, tmp_path, caplog
):
    """Loading a stale config warns once; loading a clean one stays silent.

    The clean-config control is what gives the test teeth: a warning
    emitted unconditionally on every load would satisfy the positive
    assertion while telling an operator nothing about which vault needs
    migrating.
    """
    clean_path = _write_config(tmp_path / "clean.yaml", minimal_vault_config_dict)

    stale_dict = dict(minimal_vault_config_dict)
    stale_dict["source_adapters"] = _LEGACY_SECTION
    stale_path = _write_config(tmp_path / "stale.yaml", stale_dict)

    with caplog.at_level(logging.WARNING, logger="sage.config"):
        load_vault_config(clean_path)
    assert [r for r in caplog.records if "source_adapters" in r.getMessage()] == []

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="sage.config"):
        load_vault_config(stale_path)

    matching = [r for r in caplog.records if "source_adapters" in r.getMessage()]
    assert len(matching) == 1
    message = matching[0].getMessage()
    assert minimal_vault_config_dict["vault"]["id"] in message
    assert "adapter_defaults" in message


def test_adapter_defaults_rejects_a_key_that_names_no_source_type(minimal_vault_config_dict):
    """A mistyped source type is a validation error, not a silent no-op.

    The section is read by source-type lookup, so an unrecognized key is
    never consulted. Without the validator the config would load, the
    parameters would apply to nothing, and the vault would project at
    adapter defaults with no error, no warning, and no way to see why.
    """
    minimal_vault_config_dict["adapter_defaults"] = {
        "docs": {"heading_style_map": {"Custom Section": 1}},  # typo for "docx"
    }

    with pytest.raises(ValidationError) as exc:
        VaultConfig.model_validate(minimal_vault_config_dict)

    assert "adapter_defaults.docs" in str(exc.value)


def test_adapter_defaults_rejects_a_non_mapping_parameter_block(minimal_vault_config_dict):
    """A recognized source type whose value is not a mapping is rejected.

    The merge treats a non-mapping entry as absent, so this would be the
    same silent no-op as an unrecognized key, arriving by a different
    route. Pinning both halves keeps the validator from being narrowed to
    the key check alone.
    """
    minimal_vault_config_dict["adapter_defaults"] = {"docx": "not-a-mapping"}

    with pytest.raises(ValidationError) as exc:
        VaultConfig.model_validate(minimal_vault_config_dict)

    assert "adapter_defaults.docx" in str(exc.value)


def test_adapter_defaults_accepts_every_registered_source_type(minimal_vault_config_dict):
    """The validator admits the real vocabulary it is written against.

    Anti-coincidental partner to the two rejection tests: a validator that
    rejected everything would satisfy both of them while making the section
    unusable. Driving the accepted set from ``SourceType`` itself also means
    a newly added source type cannot silently become unconfigurable.
    """
    from sage.models.enums import SourceType

    minimal_vault_config_dict["adapter_defaults"] = {
        source_type.value: {} for source_type in SourceType
    }

    config = VaultConfig.model_validate(minimal_vault_config_dict)

    assert set(config.adapter_defaults) == {st.value for st in SourceType}


def test_schema_declares_adapter_defaults_and_drops_source_adapters():
    """The formal substrate matches the model: no ``source_adapters``
    anywhere, ``adapter_defaults`` present and optional.
    """
    schema = json.loads(_VAULT_CONFIG_SCHEMA_PATH.read_text())

    assert "source_adapters" not in schema["properties"]
    assert "source_adapters" not in schema["required"]
    assert "adapter_defaults" in schema["properties"]
    assert "adapter_defaults" not in schema["required"]


def test_schema_adapter_defaults_declares_no_enablement():
    """No per-adapter object in the schema may declare enablement.

    The retired section's `enabled` flag is the specific claim CAS-ADR-046
    removes; a schema that re-declared it would reopen the drift class the
    decision closed structurally.
    """
    schema = json.loads(_VAULT_CONFIG_SCHEMA_PATH.read_text())
    per_adapter = schema["properties"]["adapter_defaults"]["additionalProperties"]

    assert "enabled" not in per_adapter.get("properties", {})
    assert "file_extensions" not in per_adapter.get("properties", {})


def test_retired_source_adapters_schema_file_is_gone():
    """``source_adapters.schema.json`` is retired from the substrate and
    from the manifest inventory that enumerates it.
    """
    assert not (_REPO_ROOT / "docs" / "fs" / "sage" / "source_adapters.schema.json").exists()

    manifest = json.loads((_REPO_ROOT / "docs" / "fs" / "manifest.json").read_text())
    paths = [entry["path"] for entry in manifest["schemas"]]
    assert "sage/source_adapters.schema.json" not in paths
