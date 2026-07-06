"""Backend-agnostic vault resolution for the out-of-band purge tooling.

``open_vault_stores`` must resolve a vault through ``discover()`` /
``load_config()`` — the binding-agnostic half of the vault-source port — never
through the filesystem-only ``config_locator``, which the document-store
binding returns ``None`` for by design (CAS-ADR-043). These tests pin that
with trap doubles: a ``config_locator`` that raises proves resolution never
touches it, and a sibling whose ``load_config`` raises proves the discovery
prefilter loads only a candidate that may match.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from sage.maintenance._internal import open_vault_stores
from sage.vault_source_binding import DiscoveredVault


def _config(vault_id: str) -> SimpleNamespace:
    return SimpleNamespace(vault=SimpleNamespace(id=vault_id, brain_root="/tmp/brain"))


class _SourceStoreDouble:
    """Vault-source double: pinned discovery, per-id configs, trapped locator."""

    def __init__(self, discovered, configs, *, raising_ids=()):
        self._discovered = list(discovered)
        self._configs = dict(configs)
        self._raising_ids = set(raising_ids)
        self.loaded_ids: list[str | None] = []

    def discover(self):
        return list(self._discovered)

    def load_config(self, discovered):
        key = discovered.vault_id
        self.loaded_ids.append(key)
        if key in self._raising_ids:
            raise RuntimeError(f"simulated malformed vault {key!r}")
        return self._configs[key]

    def config_locator(self, vault_id):
        raise AssertionError(
            "config_locator must never be called: it is the filesystem-only idiom "
            "the document-store binding returns None for"
        )


class _StubSink:
    async def append(self, record):  # pragma: no cover - never appended here
        raise AssertionError("resolution must not append audit records")


class _ProvisionerDouble:
    def __init__(self):
        self.sink = _StubSink()
        self.opened: list[str] = []
        self.handle = SimpleNamespace(graph_store=object(), content_store=object())

    def purge_audit_sink(self, vault_id):
        return self.sink

    async def open_vault_storage(self, vault_id, brain_root, *, need_graph, need_content):
        self.opened.append(vault_id)
        assert need_graph and need_content
        return self.handle


async def test_resolves_under_pathless_document_store_binding():
    """The shipped-defect gap: a pathless ``DiscoveredVault`` (config_path=None)
    resolves via ``discover()``/``load_config()``; ``config_locator`` — which
    raises on this double — is never consulted."""
    store = _SourceStoreDouble(
        [DiscoveredVault(config_path=None, vault_id="smoke")],
        {"smoke": _config("smoke")},
    )
    prov = _ProvisionerDouble()

    opened = await open_vault_stores("smoke", source_store=store, provisioner=prov)

    assert opened is not None
    graph_store, content_store, audit_sink, handle = opened
    assert graph_store is prov.handle.graph_store
    assert content_store is prov.handle.content_store
    assert audit_sink is prov.sink
    assert handle is prov.handle
    assert prov.opened == ["smoke"]


async def test_resolves_under_filesystem_binding():
    """Regression guard: a path-bearing ``DiscoveredVault`` resolves through the
    same ``discover()``/``load_config()`` path (the filesystem binding populates
    ``vault_id`` from the directory name)."""
    store = _SourceStoreDouble(
        [
            DiscoveredVault(
                config_path=Path("/tmp/vaults/smoke/vault_config.yaml"), vault_id="smoke"
            )
        ],
        {"smoke": _config("smoke")},
    )
    prov = _ProvisionerDouble()

    opened = await open_vault_stores("smoke", source_store=store, provisioner=prov)

    assert opened is not None
    assert opened[2] is prov.sink


async def test_malformed_sibling_vault_cannot_break_resolution():
    """The discovery prefilter loads only a candidate whose discovered id may
    match, so a sibling vault whose ``load_config`` raises never touches the
    target's resolution."""
    store = _SourceStoreDouble(
        [
            DiscoveredVault(config_path=None, vault_id="broken"),
            DiscoveredVault(config_path=None, vault_id="smoke"),
        ],
        {"smoke": _config("smoke")},
        raising_ids={"broken"},
    )
    prov = _ProvisionerDouble()

    opened = await open_vault_stores("smoke", source_store=store, provisioner=prov)

    assert opened is not None
    assert store.loaded_ids == ["smoke"]


async def test_returns_none_when_no_discovered_vault_matches():
    """No match returns ``None`` (preserving the CLIs' exit-2 path) and never
    opens storage — a resolver that opened stores before confirming a match
    would waste a connection."""
    store = _SourceStoreDouble(
        [DiscoveredVault(config_path=None, vault_id="other")],
        {"other": _config("other")},
    )
    prov = _ProvisionerDouble()

    opened = await open_vault_stores("missing", source_store=store, provisioner=prov)

    assert opened is None
    assert prov.opened == []


async def test_loaded_config_id_confirms_the_match():
    """A discovered id that matches but a loaded config whose own id differs is
    not a match — the config's id is authoritative."""
    store = _SourceStoreDouble(
        [DiscoveredVault(config_path=None, vault_id="smoke")],
        {"smoke": _config("renamed")},
    )
    prov = _ProvisionerDouble()

    assert await open_vault_stores("smoke", source_store=store, provisioner=prov) is None
    assert prov.opened == []


async def test_default_path_resolves_from_the_stack_singleton(monkeypatch):
    """With no injected bindings, resolution goes through the stack config and
    the profile resolvers — the local CLI path, unchanged."""
    store = _SourceStoreDouble(
        [DiscoveredVault(config_path=None, vault_id="smoke")],
        {"smoke": _config("smoke")},
    )
    prov = _ProvisionerDouble()
    sentinel_cfg = object()

    monkeypatch.setattr("sage.mcp_init.get_stack_config", lambda: sentinel_cfg)
    monkeypatch.setattr(
        "sage.mcp_init.resolve_stack_vault_source_store",
        lambda cfg: store if cfg is sentinel_cfg else pytest.fail("wrong stack config"),
    )
    monkeypatch.setattr(
        "sage.mcp_init.resolve_stack_storage_provisioner",
        lambda cfg: prov if cfg is sentinel_cfg else pytest.fail("wrong stack config"),
    )

    opened = await open_vault_stores("smoke")

    assert opened is not None
    assert opened[2] is prov.sink
