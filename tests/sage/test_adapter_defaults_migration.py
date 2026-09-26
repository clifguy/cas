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

from sage.config import STORED_CONFIG_CONTEXT, VaultConfig, load_vault_config

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
    """A stored config still carrying the retired section loads with it dropped.

    Asserting the attribute is *absent* is the load-bearing half: a bare
    "validation succeeded" assertion would also pass under
    ``extra="allow"``, which would keep the stale section reachable and
    invite a consumer to grow back. A request declaring the section is
    refused instead; ``test_editor_model_retired`` covers that path.
    """
    minimal_vault_config_dict["source_adapters"] = _LEGACY_SECTION

    config = VaultConfig.model_validate(minimal_vault_config_dict, context=STORED_CONFIG_CONTEXT)

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


def test_adapter_defaults_rejects_an_unrecognized_markdown_dialect(minimal_vault_config_dict):
    """A markdown dialect the adapter does not read is rejected when written.

    The adapter refuses it at projection, so accepting it here would leave every
    later markdown ingest and re-projection of the vault failing, far from the
    write that caused it.
    """
    minimal_vault_config_dict["adapter_defaults"] = {"markdown": {"dialect": "pandc"}}

    with pytest.raises(ValidationError) as exc:
        VaultConfig.model_validate(minimal_vault_config_dict)

    message = str(exc.value)
    assert "adapter_defaults.markdown.dialect" in message
    assert "gfm" in message and "pandoc" in message


@pytest.mark.parametrize("dialect", ["gfm", "pandoc"])
def test_adapter_defaults_accepts_each_markdown_dialect(minimal_vault_config_dict, dialect):
    """Each dialect the adapter reads is accepted.

    Anti-coincidental partner to the rejection above: a check that refused every
    dialect would satisfy it.
    """
    minimal_vault_config_dict["adapter_defaults"] = {"markdown": {"dialect": dialect}}

    config = VaultConfig.model_validate(minimal_vault_config_dict)

    assert config.adapter_defaults["markdown"]["dialect"] == dialect


def test_schema_markdown_dialect_enum_matches_the_adapter():
    """The schema's dialect values are the ones the adapter reads."""
    from sage.source_adapters.markdown_adapter import DIALECTS

    schema = json.loads(_VAULT_CONFIG_SCHEMA_PATH.read_text())
    per_adapter = schema["properties"]["adapter_defaults"]["additionalProperties"]

    assert per_adapter["properties"]["dialect"]["enum"] == list(DIALECTS)


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


#: A value each adapter refuses, one per parameter an adapter reads. Pydantic
#: types ``adapter_defaults`` as a mapping of mappings, so none of these is
#: refused by field typing: a refusal can only come from the adapter's check.
_REFUSED_PARAMETERS = [
    ("pdf", "max_pages", 0),
    ("pptx", "max_slides", "3"),
    ("xlsx", "preview_rows", True),
    ("xlsx", "max_sheets", -1),
    ("docx", "heading_style_map", {"Custom Section": 10}),
]


@pytest.mark.parametrize(("source", "key", "value"), _REFUSED_PARAMETERS)
def test_adapter_defaults_refuses_a_parameter_the_adapter_cannot_use(
    minimal_vault_config_dict, source, key, value
):
    """A parameter value its adapter would refuse is refused when written.

    The adapter refuses the same value at projection, so accepting it here would
    leave every later ingest and re-projection of that format failing, far from
    the write that caused it -- the ground the dialect check already stands on.
    """
    minimal_vault_config_dict["adapter_defaults"] = {source: {key: value}}

    with pytest.raises(ValidationError) as exc:
        VaultConfig.model_validate(minimal_vault_config_dict)

    assert f"adapter_defaults.{source}.{key}" in str(exc.value)


@pytest.mark.parametrize(
    ("source", "key", "value"),
    [
        ("pdf", "max_pages", 5),
        ("pptx", "max_slides", 1),
        ("xlsx", "preview_rows", 10),
        ("xlsx", "max_sheets", None),
        ("docx", "heading_style_map", {"Custom Section": 2}),
    ],
)
def test_adapter_defaults_accepts_a_parameter_the_adapter_can_use(
    minimal_vault_config_dict, source, key, value
):
    """Each parameter accepts a value its adapter reads.

    Anti-coincidental partner to the refusal above: a check refusing every value
    of a key would satisfy it.
    """
    minimal_vault_config_dict["adapter_defaults"] = {source: {key: value}}

    config = VaultConfig.model_validate(minimal_vault_config_dict)

    assert config.adapter_defaults[source][key] == value


#: One stored value per kind of problem the section can hold: a parameter its
#: adapter refuses, a markdown dialect none reads, a key naming no source type,
#: and a parameter block that is not a mapping.
_STORED_PROBLEMS = [
    pytest.param({"pdf": {"max_pages": 0}}, "adapter_defaults.pdf.max_pages", id="parameter"),
    pytest.param(
        {"markdown": {"dialect": "pandc"}}, "adapter_defaults.markdown.dialect", id="dialect"
    ),
    pytest.param({"docs": {}}, "adapter_defaults.docs", id="key"),
    pytest.param({"docx": "not-a-mapping"}, "adapter_defaults.docx", id="shape"),
]


@pytest.mark.parametrize(("defaults", "path"), _STORED_PROBLEMS)
def test_stored_adapter_defaults_problem_loads_with_a_warning(
    minimal_vault_config_dict, tmp_path, caplog, defaults, path
):
    """A stored configuration holding a refused value loads, and says so.

    A stored configuration is a fact, not a request: refusing it would drop the
    vault from discovery, unreachable by the surfaces that could repair it
    (CAS-ADR-047). The value is kept as stored, and the warning names the path
    to repair: for a refused parameter or dialect the projection that reads it
    still refuses it by name, while an unknown key or a non-mapping entry
    configures nothing and the warning is its only trace.
    """
    minimal_vault_config_dict["adapter_defaults"] = defaults
    stored = _write_config(tmp_path / "vault_config.yaml", minimal_vault_config_dict)

    with caplog.at_level(logging.WARNING, logger="sage.config"):
        config = load_vault_config(stored)

    assert config.adapter_defaults == defaults
    warnings = [r.getMessage() for r in caplog.records if "loaded leniently" in r.getMessage()]
    assert len(warnings) == 1
    assert path in warnings[0]


def test_validating_a_configuration_imports_an_adapter_only_for_its_entries(
    minimal_vault_config_dict, tmp_path
):
    """Reading a configuration pulls in the adapters only when an entry needs one.

    The registry imports every adapter's parsing dependencies, which a
    configuration with no ``adapter_defaults`` entry never uses. Run in a fresh
    interpreter that imports only ``sage.config``, so an import made by an
    earlier test or another module cannot satisfy either half; the second half
    is the control that the probed module is the one a recognised entry loads,
    so the first cannot pass for a module never loaded.
    """
    import subprocess
    import sys

    assert "adapter_defaults" not in minimal_vault_config_dict  # precondition
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(minimal_vault_config_dict))
    probe = (
        "import json, sys\n"
        "from sage.config import VaultConfig\n"
        f"config = json.loads(open({str(config_file)!r}).read())\n"
        "VaultConfig.model_validate(config)\n"
        "before = 'sage.source_adapters.pdf_adapter' in sys.modules\n"
        "config['adapter_defaults'] = {'pdf': {'max_pages': 5}}\n"
        "VaultConfig.model_validate(config)\n"
        "after = 'sage.source_adapters.pdf_adapter' in sys.modules\n"
        "print(before, after)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert result.stdout.split() == ["False", "True"]
