"""Shared helpers for vault-config validation and persistence.

Consumed by the vault registry and per-vault config services (under
sage/services/) so vault creation and update behavior is defined in one
place. Default-config generation lives on VaultRegistryService.
"""

import tempfile
from pathlib import Path

import yaml
from pydantic import ValidationError

from sage.api.errors import VaultConfigValidationError
from sage.config import VaultConfig
from sage.storage.graph_store import GraphStore

_REQUIRED_SECTIONS = (
    "vault",
    "document_types",
    "lifecycle",
    "source_adapters",
    "metadata_extraction",
    "edge_inference",
)
_OPTIONAL_SECTIONS = ("abstraction", "access_control_defaults", "retrieval_health")
_ALL_SECTIONS = _REQUIRED_SECTIONS + _OPTIONAL_SECTIONS

_VAULTS_ROOT = Path("~/sage_vaults").expanduser()


def _validate_config(config_dict: dict) -> VaultConfig:
    """Validate a config dict, raising VaultConfigValidationError on failure."""
    try:
        return VaultConfig.model_validate(config_dict)
    except ValidationError as exc:
        errors = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise VaultConfigValidationError(errors) from exc


def _write_config_yaml(config_path: Path, config_dict: dict) -> None:
    """Atomically write a config dict to YAML (temp file + rename)."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(config_path.parent), suffix=".yaml.tmp")
    try:
        with open(fd, "w") as f:
            yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)
        Path(tmp_path).replace(config_path)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise


async def _check_destructive_changes(
    old_config: VaultConfig,
    new_config: VaultConfig,
    graph_store: GraphStore,
) -> list[str]:
    """Return warnings for removed doc_types/lifecycle states that have documents."""
    warnings: list[str] = []

    old_doc_types = {dt.value for dt in old_config.document_types.doc_types}
    new_doc_types = {dt.value for dt in new_config.document_types.doc_types}
    removed_doc_types = old_doc_types - new_doc_types
    if removed_doc_types:
        counts = await graph_store.get_document_counts_by_field("doc_type")
        for dt in sorted(removed_doc_types):
            n = counts.get(dt, 0)
            if n > 0:
                warnings.append(f"Removing doc_type '{dt}' would affect {n} document(s)")

    old_states = {s.value for s in old_config.lifecycle.states}
    new_states = {s.value for s in new_config.lifecycle.states}
    removed_states = old_states - new_states
    if removed_states:
        counts = await graph_store.get_document_counts_by_field("lifecycle_status")
        for st in sorted(removed_states):
            n = counts.get(st, 0)
            if n > 0:
                warnings.append(f"Removing lifecycle state '{st}' would affect {n} document(s)")

    return warnings


def config_path_for_vault(vault_id: str) -> Path:
    """Return the canonical config file path for a vault."""
    return _VAULTS_ROOT / vault_id / "vault_config.yaml"
