"""Shared pytest fixtures for CAS test suite."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from sage.mcp_init import _timing_handlers
from tests.helpers.schema_validation import SchemaValidator
from tests.helpers.timing_leaks import (
    alive_timing_thread_idents,
    check_and_reap_timing_leaks,
)

# Default to stub providers on every pytest invocation. Stubs both the
# embedding provider (Avoids accidental ~270 MB nomic loads) and the
# abstraction provider (Prevents Qwen3 ~16GB MLX/Metal loads in tests
# alongside a running MCP server, which is the trigger profile documented in
# F-8). setdefault preserves explicit overrides, including per-test
# monkeypatch.delenv calls (see test_di_005).
os.environ.setdefault("SAGE_TEST_STUB_PROVIDERS", "1")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INVALID_FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures" / "invalid"


@pytest.fixture(scope="session")
def schema_validator() -> SchemaValidator:
    """Session-scoped SchemaValidator with pre-built registry."""
    return SchemaValidator()


@pytest.fixture(scope="session")
def project_root() -> Path:
    """Path to the CAS repository root."""
    return PROJECT_ROOT


@pytest.fixture(autouse=True)
def _isolated_vault_registry():
    """Per-test isolation of sage.mcp_server._vaults.

    Production route handlers and MCP tools both resolve vaults through the
    singleton VaultRegistryService, which is bound at module import to the
    global `_vaults` dict in sage.mcp_server. Fixtures that bypass the
    FastAPI lifespan write into that dict directly (via _initialize_services
    or by setting `_vaults[id] = services`), and not every fixture cleans
    up its entries. Without isolation, a test's view of the registry is the
    union of every prior test's leftover state. This fixture clears the
    dict before each test (autouse runs first within scope) and again on
    teardown so the next test starts clean.
    """
    import sage.mcp_server as _mcp

    _mcp._vaults.clear()
    yield
    _mcp._vaults.clear()


@pytest.fixture(autouse=True)
def _redirect_vaults_root(tmp_path_factory, monkeypatch):
    """Redirect ``_VAULTS_ROOT`` away from ``~/sage_vaults/`` for every test.

    ``sage.vault_management.config_path_for_vault`` (used by both REST
    create-vault and update-config paths) resolves the on-disk
    ``vault_config.yaml`` location from a module-level ``_VAULTS_ROOT``
    constant that points at ``~/sage_vaults`` in production. Tests that
    exercise those endpoints (e.g. tests/sage/test_vault_config_api.py)
    would otherwise write YAML into the real user vault tree and leave
    orphan vault directories behind, which then show up in
    ``list_vaults`` after the server restarts.

    ``sage.services.vault_registry._VAULTS_ROOT`` is the same constant
    re-imported for default-config path construction; patch both so a
    test using ``VaultRegistryService.get_default_config`` without overriding
    storage_root/brain_root also lands in tmp space.
    """
    from sage import vault_management
    from sage.services import vault_registry

    fake_root = tmp_path_factory.mktemp("sage_vaults_isolated")
    monkeypatch.setattr(vault_management, "_VAULTS_ROOT", fake_root)
    monkeypatch.setattr(vault_registry, "_VAULTS_ROOT", fake_root)
    yield fake_root


@pytest.fixture(autouse=True)
def _fail_on_leaked_timing_resources():
    """Fail (and reap) when a test leaks a per-vault timing handler or thread.

    Any test that builds real services with timing enabled (the default) and
    tears down without releasing them leaves the ``timing.log`` handler attached
    to the three process-global timing loggers and the ``VaultTimingThread``
    running. Neither surfaces as an "unclosed file" ``ResourceWarning`` — the
    loggers keep the handler reachable, so CPython never garbage-collects it and
    ``logging.shutdown()`` closes it cleanly only at interpreter exit — so this
    guard asserts on the observable that actually moves: a net-new entry in the
    ``_timing_handlers`` registry or a net-new live ``sage-timing-flush`` thread
    introduced by the test. Lives at the root conftest so every test tree
    (``tests/app``, ``tests/sage``, and any future tree) shares one
    garbage-collection-independent check.

    The remedy at a leaking site is to build services through
    ``initialize_services_for_test`` or to call ``services.close_timing()``
    before ``graph_store.close()`` on teardown.
    """
    handlers_before = set(_timing_handlers)
    threads_before = alive_timing_thread_idents()
    yield
    check_and_reap_timing_leaks(handlers_before, threads_before)


def load_invalid_fixture(component: str, filename: str) -> Any:
    """Load an invalid fixture YAML file.

    Args:
        component: "sage" or "root_harness"
        filename: YAML filename within the component's invalid fixtures dir
    """
    path = INVALID_FIXTURES_DIR / component / filename
    with open(path) as f:
        return yaml.safe_load(f)
