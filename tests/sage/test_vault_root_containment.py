"""A vault's storage and brain roots stay under the bound vault root.

``create_vault`` creates both directories, and every later source read resolves
against ``storage_root``, so a caller-authored root names where the server
reads and writes. Both write paths require a root they are asked to set to
resolve strictly under the process's bound vault root. ``update_config`` holds
only roots it is asked to change to the rule, so a vault declared before it
keeps loading and accepting edits to its other sections.
"""

from __future__ import annotations

import pytest

from sage import mcp_init, vault_management
from sage.api.errors import VaultConfigValidationError
from sage.config import SageCoreConfig, VaultConfig
from sage.models.schemas import CreateVaultRequest, UpdateVaultConfigRequest
from sage.services.vault_config import VaultConfigService
from sage.services.vault_registry import VaultRegistryService
from sage.vault_source_binding import FilesystemVaultSourceStore
from tests.sage.test_vault_config_write_root_binding import (
    _FakeRegistryService,
    _FakeServices,
)


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """A published bound root distinct from the default and the literal,
    as ``test_vault_config_write_root_binding`` pins them."""
    bound, default, literal = (tmp_path / n for n in ("bound_root", "default_root", "literal"))
    for d in (bound, default, literal):
        d.mkdir()
    monkeypatch.setenv("SAGE_VAULT_ROOT", str(default))
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    monkeypatch.delenv("SAGE_TEST_VAULT_SOURCE_BACKEND", raising=False)
    monkeypatch.setattr(vault_management, "_VAULTS_ROOT", literal)
    monkeypatch.setattr(mcp_init, "_stack_config", SageCoreConfig())
    monkeypatch.setattr(mcp_init, "_vault_root", bound)
    return bound, default, literal


def _registry() -> tuple[VaultRegistryService, dict]:
    async def fake_init(config, **kwargs):
        return _FakeServices()

    registry: dict = {}
    return VaultRegistryService(registry=registry, initialize_services=fake_init), registry


@pytest.mark.parametrize("field", ["storage_root", "brain_root"])
async def test_create_vault_refuses_a_root_outside_the_bound_root(roots, tmp_path, field):
    """TEST-SAGE-BH-154: ``create_vault`` refuses a root outside the bound
    vault root before creating any directory or writing the declaration.

    The outside directory is a real, writable path, so without the check the
    call succeeds and creates it -- the absent directory and config are what
    show the refusal came first.
    """
    bound, _default, _literal = roots
    svc, registry = _registry()
    config = VaultRegistryService.get_default_config("roots_vault", "Roots", "owner")
    config["vault"]["storage_root"] = str(bound / "roots_vault" / "sources")
    config["vault"]["brain_root"] = str(bound / "roots_vault" / "brain")
    outside = tmp_path / "elsewhere" / field
    config["vault"][field] = str(outside)

    with pytest.raises(VaultConfigValidationError):
        await svc.create_vault(CreateVaultRequest(config=config))

    assert not outside.exists()
    assert not (bound / "roots_vault" / "vault_config.yaml").exists()
    assert "roots_vault" not in registry


async def test_create_vault_refuses_a_root_that_walks_out(roots):
    """TEST-SAGE-BH-155: a root spelled under the bound root that resolves
    out of it (``..``) is refused, as is the bound root itself.

    A prefix comparison on the spelling passes both; only resolution and a
    strict-descendant check refuse them.
    """
    bound, _default, _literal = roots
    svc, _registry_map = _registry()
    for spelling in (str(bound / "x" / ".." / ".." / "escaped"), str(bound)):
        config = VaultRegistryService.get_default_config("walk_vault", "Walk", "owner")
        config["vault"]["storage_root"] = spelling
        config["vault"]["brain_root"] = str(bound / "walk_vault" / "brain")
        with pytest.raises(VaultConfigValidationError):
            await svc.create_vault(CreateVaultRequest(config=config))


async def test_create_vault_accepts_roots_under_the_bound_root(roots):
    """TEST-SAGE-BH-156 (control): roots under the bound root create the vault.

    The positive arm of BH-154: a check refusing every root would pass the
    refusals and fail here.
    """
    bound, _default, _literal = roots
    svc, registry = _registry()
    config = VaultRegistryService.get_default_config("ok_vault", "OK", "owner")
    config["vault"]["storage_root"] = str(bound / "ok_vault" / "sources")
    config["vault"]["brain_root"] = str(bound / "ok_vault" / "brain")

    await svc.create_vault(CreateVaultRequest(config=config))

    assert "ok_vault" in registry
    assert (bound / "ok_vault" / "sources").is_dir()


def _config_service(bound, vault_id: str, storage_root: str) -> tuple[VaultConfigService, dict]:
    config_dict = VaultRegistryService.get_default_config(vault_id, "Original", "owner")
    config_dict["vault"]["storage_root"] = storage_root
    config_dict["vault"]["brain_root"] = str(bound / vault_id / "brain")
    FilesystemVaultSourceStore(bound).write_config(vault_id, config_dict)
    svc = VaultConfigService(
        graph_store=object(),
        content_store=object(),
        config=VaultConfig.model_validate(config_dict),
        registry_service=_FakeRegistryService(),
    )
    return svc, config_dict


async def test_update_config_refuses_moving_a_root_outside(roots, tmp_path):
    """TEST-SAGE-BH-157: ``update_config`` refuses a vault section that moves
    ``storage_root`` outside the bound root, on a dry run as well.
    """
    bound, _default, _literal = roots
    svc, config_dict = _config_service(bound, "upd_roots", str(bound / "upd_roots" / "sources"))
    section = dict(config_dict["vault"])
    section["storage_root"] = str(tmp_path / "elsewhere")

    for dry_run in (True, False):
        with pytest.raises(VaultConfigValidationError):
            await svc.update_config(
                "upd_roots", UpdateVaultConfigRequest(vault=section, dry_run=dry_run), force=False
            )


async def test_update_config_tolerates_an_unchanged_legacy_root(roots, tmp_path):
    """TEST-SAGE-BH-158 (control): a vault already declared with a root
    outside the bound root still accepts an edit that leaves that root alone.

    Refusing it would strand every vault declared before the rule; only a
    change to a root is held to it.
    """
    bound, _default, _literal = roots
    legacy = tmp_path / "legacy_sources"
    legacy.mkdir()
    svc, config_dict = _config_service(bound, "legacy_roots", str(legacy))
    section = dict(config_dict["vault"])
    section["name"] = "Renamed"

    response = await svc.update_config(
        "legacy_roots", UpdateVaultConfigRequest(vault=section, dry_run=True), force=False
    )

    assert response is not None


async def test_create_vault_refuses_a_timing_log_outside_the_bound_root(roots, tmp_path):
    """TEST-SAGE-BH-185: ``create_vault`` refuses a ``timing.log_path``
    outside the bound vault root, and accepts one under it.

    The server opens and writes that file, so it is held to the roots' rule.
    """
    bound, _default, _literal = roots
    svc, registry = _registry()

    def config_with(log_path: str) -> dict:
        config = VaultRegistryService.get_default_config("log_vault", "Log", "owner")
        config["vault"]["storage_root"] = str(bound / "log_vault" / "sources")
        config["vault"]["brain_root"] = str(bound / "log_vault" / "brain")
        config.setdefault("timing", {})["log_path"] = log_path
        return config

    with pytest.raises(VaultConfigValidationError):
        await svc.create_vault(CreateVaultRequest(config=config_with(str(tmp_path / "x.log"))))
    assert "log_vault" not in registry

    await svc.create_vault(
        CreateVaultRequest(config=config_with(str(bound / "log_vault" / "brain" / "t.log")))
    )
    assert "log_vault" in registry


@pytest.mark.parametrize("vault_id", ["my-vault", "1vault"])
async def test_create_vault_refuses_an_id_no_schema_can_carry(roots, vault_id):
    """TEST-SAGE-BH-186: an id the vault-id shape admits but a Postgres
    schema name cannot carry is refused before any directory or declaration
    is created.
    """
    bound, _default, _literal = roots
    svc, registry = _registry()
    config = VaultRegistryService.get_default_config(vault_id, "Shape", "owner")
    config["vault"]["storage_root"] = str(bound / vault_id / "sources")
    config["vault"]["brain_root"] = str(bound / vault_id / "brain")

    with pytest.raises(VaultConfigValidationError):
        await svc.create_vault(CreateVaultRequest(config=config))

    assert not (bound / vault_id).exists()
    assert vault_id not in registry


async def test_failed_create_removes_only_the_directories_it_created(roots):
    """TEST-SAGE-BH-187: when initialization fails, ``create_vault`` removes
    the root directories it created and keeps one that already existed.
    """
    bound, _default, _literal = roots
    existing = bound / "fail_vault" / "sources"
    existing.mkdir(parents=True)
    (existing / "keep.md").write_text("kept")

    async def failing_init(config, **kwargs):
        raise RuntimeError("initialization failed")

    svc = VaultRegistryService(registry={}, initialize_services=failing_init)
    config = VaultRegistryService.get_default_config("fail_vault", "Fail", "owner")
    config["vault"]["storage_root"] = str(existing)
    config["vault"]["brain_root"] = str(bound / "fail_vault" / "brain")

    with pytest.raises(RuntimeError):
        await svc.create_vault(CreateVaultRequest(config=config))

    assert (existing / "keep.md").exists()
    assert not (bound / "fail_vault" / "brain").exists()
