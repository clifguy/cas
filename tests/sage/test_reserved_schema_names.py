"""Vault ids never name a reserved Postgres schema; identifiers match whole.

A vault's Postgres schema is named by its vault id, so an id spelling a schema
the database or another component owns -- ``public``, ``information_schema``,
any ``pg_*`` namespace, the BFF's session schema, the migration bookkeeping
schema -- is refused on every path that provisions or drops a vault schema.
Identifier validators interpolated into SQL accept only a whole-string match,
so a trailing newline cannot ride through a ``$`` anchor.
"""

from __future__ import annotations

import inspect

import pytest

from app.backend.auth.session_store import PostgresSessionStore
from sage.api.errors import VaultConfigValidationError
from sage.maintenance.delete_vault import delete_vault
from sage.models.schemas import CreateVaultRequest
from sage.request_identity import agent_from_user_agent
from sage.services.vault_registry import VaultRegistryService
from sage.storage.postgres import cloud_bootstrap, graph_store
from sage.storage.postgres.schema import (
    drop_schema_statement,
    refuse_reserved_schema,
    schema_statements,
    validate_extension,
    validate_schema_name,
)
from tests.sage.maintenance.test_delete_vault import _common, _materialize, _SpyProvisioner

RESERVED = ["public", "information_schema", "pg_catalog", "pg_toast", "pg_x", "cas_bff"]


@pytest.mark.parametrize("name", [*RESERVED, "_cas_migration"])
def test_reserved_schema_names_are_refused(name):
    """TEST-SAGE-BH-159: each reserved name is refused by the reserved-name
    guard and by both DDL builders.
    """
    with pytest.raises(ValueError):
        refuse_reserved_schema(name)
    with pytest.raises(ValueError):
        drop_schema_statement(name)
    with pytest.raises(ValueError):
        schema_statements(name)


@pytest.mark.parametrize("name", ["cas", "test_vault", "pgx", "public_notes", "cas_bff2"])
def test_ordinary_vault_schemas_are_accepted(name):
    """TEST-SAGE-BH-160 (control): names that merely resemble a reserved one
    are accepted, so the guard matches names, not prefixes of them.
    """
    assert refuse_reserved_schema(name) == name
    assert f'"{name}"' in drop_schema_statement(name)


def test_the_bff_session_schema_is_in_the_reserved_set():
    """TEST-SAGE-BH-161: the BFF's default session schema is reserved.

    Read from the store's own signature, so a rename there that is not
    mirrored in the reserved set fails here rather than reopening the overlap.
    """
    default = inspect.signature(PostgresSessionStore.__init__).parameters["schema"].default
    with pytest.raises(ValueError):
        refuse_reserved_schema(default)


@pytest.mark.parametrize("vault_id", ["public", "information_schema", "pg_temp", "cas_bff"])
async def test_create_vault_refuses_a_reserved_id(tmp_path, vault_id):
    """TEST-SAGE-BH-162: ``create_vault`` refuses a reserved id before any
    directory, declaration or schema is created.
    """
    from sage.vault_management import bound_vault_root

    called = []

    async def fake_init(config, **kwargs):
        called.append(config.vault.id)

    svc = VaultRegistryService(registry={}, initialize_services=fake_init)
    config = VaultRegistryService.get_default_config(vault_id, "Reserved", "owner")
    root = bound_vault_root()

    with pytest.raises(VaultConfigValidationError):
        await svc.create_vault(CreateVaultRequest(config=config))

    assert called == []
    assert not (root / vault_id).exists()


async def test_delete_vault_refuses_a_reserved_id(tmp_path, minimal_vault_config_dict):
    """TEST-SAGE-BH-163: the teardown refuses a reserved id before it plans,
    drops or removes anything.
    """
    built = _materialize(tmp_path / "vaults", "victim", minimal_vault_config_dict)
    prov = _SpyProvisioner()

    with pytest.raises(ValueError):
        await delete_vault(**_common(built, vault_id="public", provisioner=prov))

    assert prov.dropped == []


@pytest.mark.parametrize(
    "check",
    [
        pytest.param(lambda v: validate_schema_name(v), id="schema"),
        pytest.param(lambda v: validate_extension(v), id="extension"),
        pytest.param(lambda v: cloud_bootstrap.validate_role_name(v), id="role"),
        pytest.param(lambda v: graph_store._validate_tier3_identifier(v, "field"), id="doc_type"),
        pytest.param(lambda v: graph_store._validate_tier3_identifier("doc", v), id="tier3_key"),
    ],
)
def test_identifier_guards_refuse_a_trailing_newline(check):
    """TEST-SAGE-BH-164: identifier guards refuse a value with a trailing
    newline and accept the same value without one.

    ``re.match`` with a ``$`` anchor accepts ``"abc\\n"``; only a whole-string
    match refuses it. The positive arm keeps a guard that refuses everything
    from passing.
    """
    check("abc")
    with pytest.raises(ValueError):
        check("abc\n")


def test_agent_name_from_user_agent_refuses_a_trailing_newline():
    """TEST-SAGE-BH-165: a product name carrying a trailing newline names no
    agent, while the same name without it does.
    """
    assert agent_from_user_agent("claude-code/1.0") == "claude-code"
    assert agent_from_user_agent("claude-code\n/1.0") is None
