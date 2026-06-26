"""Schema validation for the committed cloud test-vault seed config.

The cloud profile bootstraps its first vault by seeding a ``vault_config.yaml``
directly into the SharePoint document library (CAS-ADR-043); a deployed SAGE
discovers it at startup and loads it with ``VaultConfig.model_validate``. This
suite guards the committed seed so a config SAGE would reject can never be
uploaded -- a defect that would otherwise surface only at runtime, in the
cloud, as a discovery failure.
"""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from sage.config import VaultConfig

SEED_CONFIG_PATH = (
    Path(__file__).resolve().parents[2] / "deploy" / "test-vault" / "vault_config.yaml"
)

# Top-level sections VaultConfig declares without a default (sage/config.py).
# Each must be present for a config to validate; the optional sections
# (abstraction, access_control_defaults, retrieval_health, timing) carry
# defaults and are intentionally excluded.
REQUIRED_SECTIONS = (
    "vault",
    "document_types",
    "lifecycle",
    "source_adapters",
    "metadata_extraction",
    "edge_inference",
)


def _load_seed_dict() -> dict:
    return yaml.safe_load(SEED_CONFIG_PATH.read_text())


def test_seed_config_is_schema_valid() -> None:
    """The committed seed validates through the exact call SAGE discovery uses."""
    cfg = VaultConfig.model_validate(_load_seed_dict())
    assert cfg.vault.id == "test"


@pytest.mark.parametrize("section", REQUIRED_SECTIONS)
def test_seed_config_rejects_missing_required_section(section: str) -> None:
    """Anti-coincidental guard: dropping any required section must fail validation.

    Proves the schema-valid assertion above is genuine -- a config missing a
    required section (which SAGE would refuse to load) does not slip through as
    a coincidental YAML-parses pass.
    """
    data = _load_seed_dict()
    del data[section]
    with pytest.raises(ValidationError):
        VaultConfig.model_validate(data)
