"""The vault configuration carries no editor model (CAS-ADR-044, CAS-ADR-056).

Admission to SAGE is a single group gate and write identity is the
request-derived principal, client and asserted agent. The vault member roles
(``vault.members``) and the per-document editor default
(``access_control_defaults``) described a registry-backed write-control model
that nothing enforced; they are retired from the schema, the model, the
contracts and the ROOT Harness agent-registration surface.

A configuration read back from storage that still carries a retired key loads
with the key ignored and a warning naming it; a request carrying one is
refused, so a caller cannot believe it configured something that is dropped.

Anti-coincidental-pass discipline:

* Each absence check carries a positive control on a field that must still be
  present, so a check reading the wrong model, schema path or document passes
  nothing.
* The stored-path tests assert the retired key is in the file before loading,
  so a fixture that never held it cannot pass them.
* The refusal tests validate the same dict without the key as a control, and
  require the error to name the key, so a refusal for any other reason fails.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from sage.api.errors import VaultConfigValidationError
from sage.app import create_app
from sage.config import SageCoreConfig, VaultConfig, VaultIdentity, load_vault_config
from sage.models.schemas import UpdateVaultConfigRequest
from sage.vault_management import _validate_config

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FS = _REPO_ROOT / "docs" / "fs"
_VAULT_CONFIG_SCHEMA = _FS / "sage" / "vault_config.schema.json"
_CORE_SPEC = _FS / "sage" / "sage_core_api.openapi.yaml"
_MCP_CATALOG = _FS / "sage" / "sage_mcp_tools.catalog.json"
_ORCHESTRATION_SPEC = _FS / "root_harness" / "orchestration_api.openapi.yaml"
_AGENT_SCHEMA = _FS / "root_harness" / "agent.schema.json"

_ACCESS_DEFAULTS = {"new_documents_restricted": False}
_MEMBERS = [{"user_id": "user1", "role": "editor"}]


def _with_retired(config: dict, key: str) -> dict:
    """Return a copy of ``config`` carrying the retired ``key`` (dotted path)."""
    stale = copy.deepcopy(config)
    if key == "access_control_defaults":
        stale["access_control_defaults"] = dict(_ACCESS_DEFAULTS)
    elif key == "vault.members":
        stale["vault"]["members"] = copy.deepcopy(_MEMBERS)
    elif key == "source_adapters":
        stale["source_adapters"] = {"adapters": [{"source_type": "markdown", "enabled": False}]}
    else:
        raise AssertionError(key)
    return stale


def _carries(config: dict, key: str) -> bool:
    head, _, tail = key.partition(".")
    if not tail:
        return head in config
    return tail in config.get(head, {})


_EDITOR_MODEL_KEYS = ("access_control_defaults", "vault.members")


# ---------------------------------------------------------------------------
# Model and schema
# ---------------------------------------------------------------------------


def test_vault_config_declares_no_member_roles_or_access_defaults() -> None:
    assert "access_control_defaults" not in VaultConfig.model_fields
    assert "members" not in VaultIdentity.model_fields
    # Positive controls: the lookups read the real models.
    assert "adapter_defaults" in VaultConfig.model_fields
    assert "timezone" in VaultIdentity.model_fields


def test_schema_declares_no_member_roles_or_access_defaults() -> None:
    schema = json.loads(_VAULT_CONFIG_SCHEMA.read_text())
    vault_properties = schema["properties"]["vault"]["properties"]
    assert "access_control_defaults" not in schema["properties"]
    assert "members" not in vault_properties
    # Positive control: the path addresses the vault identity block.
    assert "timezone" in vault_properties


# ---------------------------------------------------------------------------
# A stored configuration still carrying a retired key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "keys",
    [("access_control_defaults",), ("vault.members",), _EDITOR_MODEL_KEYS],
    ids=["access_control_defaults", "vault.members", "both"],
)
def test_stored_config_with_retired_keys_loads(
    minimal_vault_config_dict: dict, tmp_path: Path, keys: tuple[str, ...]
) -> None:
    stale = minimal_vault_config_dict
    for key in keys:
        stale = _with_retired(stale, key)
    path = tmp_path / "vault_config.yaml"
    path.write_text(yaml.dump(stale, sort_keys=False))
    on_disk = yaml.safe_load(path.read_text())
    assert all(_carries(on_disk, key) for key in keys)

    config = load_vault_config(path)

    assert config.vault.id == minimal_vault_config_dict["vault"]["id"]
    dumped = config.model_dump()
    assert not any(_carries(dumped, key) for key in _EDITOR_MODEL_KEYS)


def test_stored_retired_keys_log_one_warning_each(
    minimal_vault_config_dict: dict, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clean = tmp_path / "clean.yaml"
    clean.write_text(yaml.dump(minimal_vault_config_dict, sort_keys=False))
    with caplog.at_level(logging.WARNING, logger="sage.config"):
        load_vault_config(clean)
    assert [r for r in caplog.records if "is retired and ignored" in r.getMessage()] == []

    stale = minimal_vault_config_dict
    for key in _EDITOR_MODEL_KEYS:
        stale = _with_retired(stale, key)
    path = tmp_path / "stale.yaml"
    path.write_text(yaml.dump(stale, sort_keys=False))

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="sage.config"):
        load_vault_config(path)

    messages = [
        r.getMessage() for r in caplog.records if "is retired and ignored" in r.getMessage()
    ]
    vault_id = minimal_vault_config_dict["vault"]["id"]
    for key in _EDITOR_MODEL_KEYS:
        naming = [m for m in messages if f"'{key}'" in m]
        assert len(naming) == 1, (key, messages)
        assert vault_id in naming[0]


# ---------------------------------------------------------------------------
# A request carrying a retired key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", [*_EDITOR_MODEL_KEYS, "source_adapters"])
def test_request_carrying_retired_key_is_refused(minimal_vault_config_dict: dict, key: str) -> None:
    # Control: the same configuration without the key is a valid request.
    _validate_config(copy.deepcopy(minimal_vault_config_dict))

    with pytest.raises(VaultConfigValidationError) as excinfo:
        _validate_config(_with_retired(minimal_vault_config_dict, key))

    # The refusal is one message, naming the section first: a model-level
    # error carries no location to prefix, and no framework preamble.
    (message,) = excinfo.value.detail["errors"]
    assert message.startswith(f"'{key}' is retired: "), message


def test_update_request_rejects_access_control_defaults() -> None:
    with pytest.raises(ValidationError):
        UpdateVaultConfigRequest.model_validate({"access_control_defaults": {}})
    # Control: a declared section is accepted.
    UpdateVaultConfigRequest.model_validate({"adapter_defaults": {}})


# ---------------------------------------------------------------------------
# Published contracts
# ---------------------------------------------------------------------------


def _contract_text(which: str) -> str:
    if which == "served":
        return json.dumps(create_app(stack_config=SageCoreConfig()).openapi())
    if which == "published":
        return _CORE_SPEC.read_text()
    return _MCP_CATALOG.read_text()


@pytest.mark.parametrize("which", ["served", "published", "catalog"])
def test_contracts_declare_no_editor_model(which: str) -> None:
    text = _contract_text(which)
    # Positive control: the document describes the vault-config surface.
    assert "adapter_defaults" in text
    assert "access_control_defaults" not in text
    assert "new_documents_restricted" not in text


@pytest.mark.parametrize("path", [_ORCHESTRATION_SPEC, _AGENT_SCHEMA], ids=lambda p: p.name)
def test_root_harness_specs_describe_no_sage_user(path: Path) -> None:
    text = path.read_text()
    # Positive control: the file is the agent-registration surface.
    assert "agent_id" in text
    lowered = text.lower()
    assert "sage user" not in lowered
    assert "dual_write_failure" not in lowered
    assert "editor-based" not in lowered


def test_register_agent_declares_no_dual_write_response() -> None:
    spec = yaml.safe_load(_ORCHESTRATION_SPEC.read_text())
    operation = spec["paths"]["/agents"]["post"]
    assert operation["operationId"] == "register_agent"
    responses = operation["responses"]
    assert {"201", "400", "409"} <= set(responses)
    assert "500" not in responses


def test_backfill_script_loads_a_stored_config_with_retired_keys(
    minimal_vault_config_dict: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository script reading a stored config tolerates it as the server does."""
    from scripts import backfill_references_mentions as script

    stale = minimal_vault_config_dict
    for key in _EDITOR_MODEL_KEYS:
        stale = _with_retired(stale, key)
    path = tmp_path / "vault_config.yaml"
    path.write_text(yaml.dump(stale, sort_keys=False))
    monkeypatch.setattr(script, "config_path_for_vault", lambda vault_id: path)

    config = script._load_vault_config(stale["vault"]["id"])

    assert config.vault.id == stale["vault"]["id"]
