"""Tests for vault discovery and lifespan integration.

Covers:
  - sage.vault_discovery.discover_vault_configs
  - sage.app.create_app(vault_root=...) lifespan behavior

The lifespan tests monkeypatch ``sage.app._initialize_vault`` with a
recorder so the tests do not pull in heavy provider initialization
(NomicEmbeddingProvider, Qwen3, LanceDB). The contract under test is
discovery → load → initialize, not the per-vault service construction.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI

from sage.app import create_app
from sage.vault_discovery import discover_vault_configs

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _materialize_vault(
    root: Path, vault_id: str, base_config: dict, malformed: bool = False
) -> Path:
    """Write a vault directory under ``root`` and return its config path.

    If ``malformed`` is True, writes garbage YAML so ``load_vault_config``
    raises during the lifespan.
    """
    vault_dir = root / vault_id
    vault_dir.mkdir(parents=True, exist_ok=True)
    sources_dir = vault_dir / "sources"
    sources_dir.mkdir(exist_ok=True)
    brain_dir = vault_dir / "brain"
    brain_dir.mkdir(exist_ok=True)

    config_path = vault_dir / "vault_config.yaml"
    if malformed:
        config_path.write_text("not: valid: yaml: ::: [unclosed")
        return config_path

    cfg = copy.deepcopy(base_config)
    cfg["vault"]["id"] = vault_id
    cfg["vault"]["name"] = vault_id.replace("_", " ").title()
    cfg["vault"]["storage_root"] = str(sources_dir)
    cfg["vault"]["brain_root"] = str(brain_dir)
    config_path.write_text(yaml.safe_dump(cfg))
    return config_path


@pytest.fixture
def vault_root(tmp_path) -> Path:
    """Empty vault root directory."""
    root = tmp_path / "vault_root"
    root.mkdir()
    return root


# ---------------------------------------------------------------------------
# Surface 1: discover_vault_configs
# ---------------------------------------------------------------------------


def test_discovers_vaults_under_root(vault_root, minimal_vault_config_dict):
    """#1: Returns vault_config.yaml paths for every qualifying subdir."""
    _materialize_vault(vault_root, "pim_health", minimal_vault_config_dict)
    _materialize_vault(vault_root, "theology", minimal_vault_config_dict)

    result = discover_vault_configs(vault_root)

    assert len(result) == 2
    assert all(p.name == "vault_config.yaml" for p in result)
    parents = {p.parent.name for p in result}
    assert parents == {"pim_health", "theology"}


def test_empty_root_returns_empty_list(vault_root):
    """#2: An existing-but-empty root returns []."""
    assert discover_vault_configs(vault_root) == []


def test_missing_root_returns_empty_list(tmp_path):
    """#3: A nonexistent root returns [] without raising."""
    nonexistent = tmp_path / "does_not_exist"
    assert not nonexistent.exists()

    assert discover_vault_configs(nonexistent) == []


def test_subdirectory_without_config_is_skipped(vault_root, minimal_vault_config_dict):
    """#4: Directories that don't contain vault_config.yaml are ignored."""
    _materialize_vault(vault_root, "real_vault", minimal_vault_config_dict)
    junk = vault_root / "notes"
    junk.mkdir()
    (junk / "scratch.md").write_text("not a vault")

    result = discover_vault_configs(vault_root)

    assert len(result) == 1
    assert result[0].parent.name == "real_vault"


def test_loose_file_at_root_is_ignored(vault_root, minimal_vault_config_dict):
    """#5: Files at the root level alongside vault dirs are ignored."""
    _materialize_vault(vault_root, "real_vault", minimal_vault_config_dict)
    (vault_root / "README.md").write_text("# notes")

    result = discover_vault_configs(vault_root)

    assert len(result) == 1
    assert result[0].parent.name == "real_vault"


def test_hidden_directories_are_skipped(vault_root, minimal_vault_config_dict):
    """#6: Dot-prefixed dirs (e.g., .DS_Store) are not treated as vaults."""
    _materialize_vault(vault_root, "real_vault", minimal_vault_config_dict)

    # Materialize a hidden directory that *does* contain a vault_config.yaml
    # to prove the hidden-name skip is what excludes it (not absence of config).
    hidden = vault_root / ".DS_Store"
    hidden.mkdir()
    (hidden / "vault_config.yaml").write_text("vault: {}")

    result = discover_vault_configs(vault_root)

    assert len(result) == 1
    assert result[0].parent.name == "real_vault"


def test_discovery_order_is_deterministic(vault_root, minimal_vault_config_dict):
    """#7: Repeated calls return identical ordering."""
    for vid in ["zeta", "alpha", "mu"]:
        _materialize_vault(vault_root, vid, minimal_vault_config_dict)

    first = discover_vault_configs(vault_root)
    second = discover_vault_configs(vault_root)

    assert first == second
    # Sorted by directory name
    assert [p.parent.name for p in first] == ["alpha", "mu", "zeta"]


# ---------------------------------------------------------------------------
# Surface 2: lifespan integration
# ---------------------------------------------------------------------------


class _FakeGraphStore:
    async def close(self) -> None:  # pragma: no cover - trivial
        pass


class _FakeServices:
    """Stand-in for SAGEServices in lifespan tests."""

    def __init__(self, vault_id: str) -> None:
        self.vault_id = vault_id
        self.graph_store = _FakeGraphStore()


def _patch_initialize_vault(monkeypatch, *, fail_for: set[str] | None = None):
    """Replace sage.app._initialize_vault with a recorder.

    Returns the call-record list. If a config's vault.id is in
    ``fail_for``, the fake raises RuntimeError to simulate init failure.
    """
    calls: list[str] = []
    fail_for = fail_for or set()

    async def fake_init(app: FastAPI, config, **kwargs) -> None:
        vault_id = config.vault.id
        calls.append(vault_id)
        if vault_id in fail_for:
            raise RuntimeError(f"simulated init failure for {vault_id}")
        app.state.vault_registry[vault_id] = _FakeServices(vault_id)

    monkeypatch.setattr("sage.app._initialize_vault", fake_init)
    return calls


async def test_lifespan_populates_registry_from_discovered_vaults(
    vault_root, minimal_vault_config_dict, monkeypatch
):
    """#8: With vault_root set, the lifespan discovers and initializes each vault."""
    _materialize_vault(vault_root, "vault_a", minimal_vault_config_dict)
    _materialize_vault(vault_root, "vault_b", minimal_vault_config_dict)
    calls = _patch_initialize_vault(monkeypatch)

    app = create_app(vault_root=vault_root)
    async with app.router.lifespan_context(app):
        assert set(app.state.vault_registry.keys()) == {"vault_a", "vault_b"}

    assert set(calls) == {"vault_a", "vault_b"}


async def test_lifespan_with_empty_vault_root_starts_clean(vault_root, monkeypatch):
    """#9: Empty discovery yields an empty registry; app still starts."""
    calls = _patch_initialize_vault(monkeypatch)

    app = create_app(vault_root=vault_root)
    async with app.router.lifespan_context(app):
        assert app.state.vault_registry == {}

    assert calls == []


async def test_lifespan_isolates_malformed_vault_config(
    vault_root, minimal_vault_config_dict, monkeypatch, caplog
):
    """#10: A malformed vault_config.yaml is logged and skipped; healthy vaults load."""
    _materialize_vault(vault_root, "good_vault", minimal_vault_config_dict)
    _materialize_vault(vault_root, "broken_vault", minimal_vault_config_dict, malformed=True)
    calls = _patch_initialize_vault(monkeypatch)

    app = create_app(vault_root=vault_root)
    with caplog.at_level("ERROR"):
        async with app.router.lifespan_context(app):
            assert "good_vault" in app.state.vault_registry
            assert "broken_vault" not in app.state.vault_registry

    assert "good_vault" in calls
    assert "broken_vault" not in calls
    assert any("broken_vault" in r.message or "broken" in r.message.lower() for r in caplog.records)


async def test_lifespan_isolates_failed_service_init(
    vault_root, minimal_vault_config_dict, monkeypatch, caplog
):
    """#11: A vault whose initialization raises is logged and skipped; healthy vaults load."""
    _materialize_vault(vault_root, "good_vault", minimal_vault_config_dict)
    _materialize_vault(vault_root, "broken_vault", minimal_vault_config_dict)
    calls = _patch_initialize_vault(monkeypatch, fail_for={"broken_vault"})

    app = create_app(vault_root=vault_root)
    with caplog.at_level("ERROR"):
        async with app.router.lifespan_context(app):
            assert "good_vault" in app.state.vault_registry
            assert "broken_vault" not in app.state.vault_registry

    assert "good_vault" in calls
    assert "broken_vault" in calls  # init was attempted
    assert any("broken_vault" in r.message for r in caplog.records)


async def test_create_app_with_in_memory_config_still_works(minimal_config, monkeypatch):
    """#12: Single in-memory config injection (test path) is preserved."""
    _patch_initialize_vault(monkeypatch)

    app = create_app(config=minimal_config)
    async with app.router.lifespan_context(app):
        assert minimal_config.vault.id in app.state.vault_registry


async def test_lifespan_forwards_config_path_to_initialize_services(
    vault_root, minimal_vault_config_dict, monkeypatch
):
    """#14: T-0052/F10 regression — the vault_root= lifespan branch must forward
    each discovered config path to ``_initialize_vault`` so it lands on
    ``SAGEServices.config_path``. Without it, ``sage_reload_vault`` falls into
    the in-memory-config branch and silently no-ops on on-disk YAML edits.
    """
    cp_a = _materialize_vault(vault_root, "vault_a", minimal_vault_config_dict)
    cp_b = _materialize_vault(vault_root, "vault_b", minimal_vault_config_dict)

    captured: dict[str, dict] = {}

    async def recording_init(app: FastAPI, config, **kwargs) -> None:
        captured[config.vault.id] = kwargs
        app.state.vault_registry[config.vault.id] = _FakeServices(config.vault.id)

    monkeypatch.setattr("sage.app._initialize_vault", recording_init)

    app = create_app(vault_root=vault_root)
    async with app.router.lifespan_context(app):
        pass

    assert captured["vault_a"].get("config_path") == cp_a
    assert captured["vault_b"].get("config_path") == cp_b


async def test_lifespan_in_memory_config_branches_omit_config_path(minimal_config, monkeypatch):
    """#15: The pre-loaded ``config=`` and ``configs=`` branches intentionally
    have no on-disk config path to forward — confirm they continue to call
    ``_initialize_vault`` without a ``config_path`` kwarg, so a future refactor
    that conflates the branches would surface here.
    """
    captured: dict[str, dict] = {}

    async def recording_init(app: FastAPI, config, **kwargs) -> None:
        captured[config.vault.id] = kwargs
        app.state.vault_registry[config.vault.id] = _FakeServices(config.vault.id)

    monkeypatch.setattr("sage.app._initialize_vault", recording_init)

    app = create_app(config=minimal_config)
    async with app.router.lifespan_context(app):
        pass

    assert "config_path" not in captured[minimal_config.vault.id]


async def test_create_app_with_in_memory_configs_list_still_works(
    minimal_vault_config_dict, monkeypatch, tmp_path
):
    """#13: List-of-configs in-memory injection (test path) is preserved."""
    from sage.config import VaultConfig

    cfg1_dict = copy.deepcopy(minimal_vault_config_dict)
    cfg1_dict["vault"]["id"] = "vault_one"
    cfg1_dict["vault"]["brain_root"] = str(tmp_path / "one_brain")
    cfg1_dict["vault"]["storage_root"] = str(tmp_path / "one_src")
    cfg2_dict = copy.deepcopy(minimal_vault_config_dict)
    cfg2_dict["vault"]["id"] = "vault_two"
    cfg2_dict["vault"]["brain_root"] = str(tmp_path / "two_brain")
    cfg2_dict["vault"]["storage_root"] = str(tmp_path / "two_src")
    cfg1 = VaultConfig.model_validate(cfg1_dict)
    cfg2 = VaultConfig.model_validate(cfg2_dict)

    _patch_initialize_vault(monkeypatch)

    app = create_app(configs=[cfg1, cfg2])
    async with app.router.lifespan_context(app):
        assert set(app.state.vault_registry.keys()) == {"vault_one", "vault_two"}


async def test_f10_reload_round_trips_on_disk_yaml_edit_through_lifespan(
    vault_root, minimal_vault_config_dict, monkeypatch
):
    """#16: F10 invariant — the documented contract for ``sage_reload_vault``
    is "after returning ``reloaded: true``, the caller's next read sees the
    on-disk state." T-0052 wired ``config_path`` through the FastAPI lifespan
    so the reload tool can re-read the YAML; T-0053 added the
    ``content_store_factory`` hook so this test can drive that path end-to-end
    without LanceDB / Nomic / Qwen3 initialization.

    Flow: boot real lifespan with a stub content store; mutate
    ``vault_config.yaml`` on disk; call ``sage_reload_vault``; call
    ``sage_get_vault_config``; assert the on-disk edit is reflected.
    """
    from sage.adapters.stubs import StubContentStore
    from sage.mcp_server import sage_get_vault_config, sage_reload_vault

    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")

    vault_id = "f10_vault"
    config_path = _materialize_vault(vault_root, vault_id, minimal_vault_config_dict)

    app = create_app(
        vault_root=vault_root,
        content_store_factory=lambda _brain_root: StubContentStore(),
    )

    async with app.router.lifespan_context(app):
        # Sanity: lifespan booted via the factory, vault is registered.
        services = app.state.vault_registry[vault_id]
        assert isinstance(services.content_store, StubContentStore)
        assert services.content_store_factory is not None
        assert services.config_path == config_path

        # Mutate vault_config.yaml on disk. ``vault.name`` is a free-form
        # string field that VaultConfig.model_dump() round-trips verbatim,
        # so it serves as the F10 sentinel.
        sentinel_name = "F10 Reload Sentinel"
        raw = yaml.safe_load(config_path.read_text())
        raw["vault"]["name"] = sentinel_name
        config_path.write_text(yaml.safe_dump(raw))

        # Pre-reload guard: the in-memory config still shows the old value.
        pre_reload = await sage_get_vault_config(vault_id)
        assert pre_reload["vault"]["name"] != sentinel_name

        # Reload from disk.
        reload_result = await sage_reload_vault(vault_id)
        assert reload_result.get("reloaded") is True, reload_result

        # F10 invariant: the post-reload read reflects the on-disk edit.
        post_reload = await sage_get_vault_config(vault_id)
        assert post_reload["vault"]["name"] == sentinel_name

        # Reload must preserve the factory so the rebuilt services still
        # hold a stub content store, not a freshly-constructed LanceDB.
        rebuilt = app.state.vault_registry[vault_id]
        assert isinstance(rebuilt.content_store, StubContentStore)
        assert rebuilt.content_store_factory is not None
