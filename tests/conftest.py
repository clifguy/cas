"""Shared pytest fixtures for CAS test suite."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.helpers.schema_validation import SchemaValidator


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PIM_HEALTH_DIR = PROJECT_ROOT / "domains" / "pim_health"
INVALID_FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures" / "invalid"


@pytest.fixture(scope="session")
def schema_validator() -> SchemaValidator:
    """Session-scoped SchemaValidator with pre-built registry."""
    return SchemaValidator()


@pytest.fixture(scope="session")
def project_root() -> Path:
    """Path to the CAS repository root."""
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def pim_health_dir() -> Path:
    """Path to the PIM Health domain config directory."""
    return PIM_HEALTH_DIR


def _load_yaml(path: Path) -> Any:
    """Load a YAML file and return its contents."""
    with open(path) as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="session")
def pim_health_config() -> dict[str, Any]:
    """Load PIM Health sage_vault_config.yaml (full composed config)."""
    return _load_yaml(PIM_HEALTH_DIR / "sage_vault_config.yaml")


@pytest.fixture(scope="session")
def pim_health_pipeline() -> dict[str, Any]:
    """Load PIM Health pipeline.yaml."""
    return _load_yaml(PIM_HEALTH_DIR / "pipeline.yaml")


@pytest.fixture(scope="session")
def pim_health_agents() -> dict[str, Any]:
    """Load PIM Health agents.yaml."""
    return _load_yaml(PIM_HEALTH_DIR / "agents.yaml")


@pytest.fixture(scope="session")
def pim_health_policies() -> dict[str, Any]:
    """Load PIM Health policies.yaml."""
    return _load_yaml(PIM_HEALTH_DIR / "policies.yaml")


@pytest.fixture(scope="session")
def pim_health_workflows() -> dict[str, Any]:
    """Load PIM Health workflows.yaml."""
    return _load_yaml(PIM_HEALTH_DIR / "workflows.yaml")


def load_invalid_fixture(component: str, filename: str) -> Any:
    """Load an invalid fixture YAML file.

    Args:
        component: "sage" or "root_harness"
        filename: YAML filename within the component's invalid fixtures dir
    """
    path = INVALID_FIXTURES_DIR / component / filename
    return _load_yaml(path)
