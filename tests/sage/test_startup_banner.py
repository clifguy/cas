"""Tests for the SAGE startup banner: the pure renderer in
``sage.startup_banner`` and its emission from the ``create_app`` lifespan.

Renderer tests (BANNER-001..010) are pure — they pass every datum in and
assert on the returned text, so they never construct an app. Emission tests
(BANNER-011..012) drive the real ``create_app`` lifespan with stubbed
providers and a fake ``_initialize_vault`` so no model or content store
loads, and assert the banner reaches the ``sage.app`` logger exactly once
carrying live startup data.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path

import pytest
import yaml

import sage.mcp_server as mcp_server
from sage import build_info
from sage.app import create_app
from sage.startup_banner import render_startup_banner

# ---------------------------------------------------------------------------
# Pure renderer — render_startup_banner
# ---------------------------------------------------------------------------

_BASE_KW = {
    "build_identity": "cc019b8",
    "api_version": "0.1.0",
    "python_version": "3.14.0",
    "pid": 4242,
    "vault_root": Path("/home/u/sage_vaults"),
    "loaded_vault_ids": ["cas", "test"],
    "skipped_vaults": [],
    "mcp_mounts": ["/mcp", "/mcp_admin"],
}


def _render(**overrides: object) -> str:
    kw = {**_BASE_KW, **overrides}
    return render_startup_banner(**kw)  # type: ignore[arg-type]


def test_banner_001_build_identity_verbatim() -> None:
    """The build identity (including a ``-dirty`` suffix) appears verbatim."""
    text = _render(build_identity="cc019b8-dirty")
    assert "cc019b8-dirty" in text


def test_banner_002_process_facts_present() -> None:
    """API version, Python version, and PID all appear in the banner."""
    text = _render(api_version="0.1.0", python_version="3.14.0", pid=4242)
    assert "0.1.0" in text
    assert "3.14.0" in text
    assert "4242" in text


def test_banner_003_vault_root_shown_when_given() -> None:
    """A concrete vault root path is rendered."""
    text = _render(vault_root=Path("/x/sage_vaults"))
    assert "/x/sage_vaults" in text


def test_banner_004_vault_root_none_is_graceful() -> None:
    """A ``None`` vault root renders an explicit sentinel, never the literal None."""
    text = _render(vault_root=None)
    assert "vault root: (none)" in text


def test_banner_005_loaded_inventory() -> None:
    """Every loaded vault id and the count appear."""
    text = _render(loaded_vault_ids=["cas", "test"])
    assert "cas" in text
    assert "test" in text
    assert "vaults loaded (2)" in text


def test_banner_006_empty_registry_is_graceful() -> None:
    """An empty registry renders a zero count, not a crash."""
    text = _render(loaded_vault_ids=[])
    assert "vaults loaded (0)" in text


def test_banner_007_skipped_inventory() -> None:
    """A skipped vault's path, reason, and the count all appear."""
    text = _render(skipped_vaults=[("/r/broken", "ConfigError: bad yaml")])
    assert "/r/broken" in text
    assert "ConfigError: bad yaml" in text
    assert "vaults skipped (1)" in text


def test_banner_008_no_skips_renders_cleanly() -> None:
    """No skipped vaults renders a zero count and an explicit 'none'."""
    text = _render(skipped_vaults=[])
    assert "vaults skipped (0)" in text
    assert "none" in text


def test_banner_009_mcp_mounts_listed() -> None:
    """Every mounted MCP surface path appears."""
    text = _render(mcp_mounts=["/mcp", "/mcp_admin"])
    assert "/mcp" in text
    assert "/mcp_admin" in text


def test_banner_010_unknown_identity_hint() -> None:
    """An 'unknown' build identity carries an explanatory degradation hint."""
    text = _render(build_identity=build_info.UNKNOWN)
    assert "unknown" in text
    assert "unavailable" in text


# ---------------------------------------------------------------------------
# Emission — driving the create_app lifespan
# ---------------------------------------------------------------------------

_BANNER_MARKER = "SAGE Core API ready"


class _FakeGraphStore:
    async def close(self) -> None:
        pass


class _FakeIngestionService:
    async def recover_incomplete_documents(self) -> int:
        return 0

    async def stop_worker(self) -> None:
        pass


class _FakeServices:
    """Minimal stand-in registered by the patched ``_initialize_vault``.

    Provides exactly the hooks the lifespan touches: the recovery call, and
    the three teardown calls (stop_worker, close_timing, graph_store.close).
    """

    def __init__(self, config: object) -> None:
        self.config = config
        self.graph_store = _FakeGraphStore()
        self.ingestion_service = _FakeIngestionService()

    def close_timing(self) -> None:
        pass


def _materialize_vault(
    root: Path, vault_id: str, base_config: dict, malformed: bool = False
) -> Path:
    """Write a vault directory under ``root`` and return its config path."""
    vault_dir = root / vault_id
    vault_dir.mkdir(parents=True, exist_ok=True)
    (vault_dir / "sources").mkdir(exist_ok=True)
    (vault_dir / "brain").mkdir(exist_ok=True)

    config_path = vault_dir / "vault_config.yaml"
    if malformed:
        config_path.write_text("not: valid: yaml: ::: [unclosed")
        return config_path

    cfg = copy.deepcopy(base_config)
    cfg["vault"]["id"] = vault_id
    cfg["vault"]["name"] = vault_id.replace("_", " ").title()
    cfg["vault"]["storage_root"] = str(vault_dir / "sources")
    cfg["vault"]["brain_root"] = str(vault_dir / "brain")
    config_path.write_text(yaml.safe_dump(cfg))
    return config_path


@pytest.fixture
def isolate_vaults():
    """Save/clear/restore the shared ``mcp_server._vaults`` registry.

    The lifespan aliases this module-level dict onto ``app.state.vault_registry``
    and clears it on teardown, so tests must isolate it to avoid cross-talk.
    """
    saved = dict(mcp_server._vaults)
    mcp_server._vaults.clear()
    yield
    mcp_server._vaults.clear()
    mcp_server._vaults.update(saved)


def _stub_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch out the real abstraction-provider build and per-vault init.

    ``build_stack_abstraction_provider`` is imported inside the lifespan from
    ``sage.mcp_init`` at call time, so it must be patched on that module.
    ``_initialize_vault`` is a module-level function in ``sage.app`` called by
    bare name; the fake registers a ``_FakeServices`` so no real provider,
    content store, or graph store is constructed.
    """

    async def _fake_init_vault(app, config, **_kwargs):  # noqa: ANN001, ANN202
        app.state.vault_registry[config.vault.id] = _FakeServices(config)

    monkeypatch.setattr("sage.mcp_init.build_stack_abstraction_provider", lambda _cfg: object())
    monkeypatch.setattr("sage.app._initialize_vault", _fake_init_vault)


def _banner_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "sage.app" and _BANNER_MARKER in r.getMessage()]


async def test_banner_011_emitted_once_with_live_data(
    isolate_vaults, monkeypatch, minimal_config, caplog
) -> None:
    """The lifespan emits the banner exactly once, carrying the live build
    identity and the loaded vault id."""
    _stub_providers(monkeypatch)
    app = create_app(configs=[minimal_config])

    with caplog.at_level(logging.INFO, logger="sage.app"):
        async with app.router.lifespan_context(app):
            pass

    records = _banner_records(caplog)
    assert len(records) == 1, "banner must be emitted exactly once at end of startup"
    msg = records[0].getMessage()
    assert build_info.BUILD_IDENTITY in msg
    assert "test_vault" in msg  # minimal_config's vault id


async def test_banner_012_skipped_vault_reaches_banner(
    isolate_vaults, monkeypatch, minimal_vault_config_dict, tmp_path, caplog
) -> None:
    """A vault that fails to load is collected and reported in the banner's
    skipped inventory alongside the successfully-loaded vault."""
    root = tmp_path / "vault_root"
    root.mkdir()
    _materialize_vault(root, "good_vault", minimal_vault_config_dict)
    _materialize_vault(root, "broken_vault", minimal_vault_config_dict, malformed=True)

    _stub_providers(monkeypatch)
    app = create_app(vault_root=root)

    with caplog.at_level(logging.INFO, logger="sage.app"):
        async with app.router.lifespan_context(app):
            pass

    records = _banner_records(caplog)
    assert len(records) == 1
    msg = records[0].getMessage()
    assert "vaults loaded (1)" in msg
    assert "good_vault" in msg
    assert "vaults skipped (1)" in msg
    assert "broken_vault" in msg
