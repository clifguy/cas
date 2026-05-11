"""Shared pytest fixtures for CAS test suite."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.helpers.schema_validation import SchemaValidator

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


def load_invalid_fixture(component: str, filename: str) -> Any:
    """Load an invalid fixture YAML file.

    Args:
        component: "sage" or "root_harness"
        filename: YAML filename within the component's invalid fixtures dir
    """
    path = INVALID_FIXTURES_DIR / component / filename
    with open(path) as f:
        return yaml.safe_load(f)
