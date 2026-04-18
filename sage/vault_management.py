"""Shared helpers for vault-config validation, persistence, and defaults.

Consumed by both the REST router (sage/api/routers/vaults.py) and the MCP
tools (sage/sage_api_tools.py) so vault creation and update behavior is
defined in one place.
"""

import tempfile
from pathlib import Path

import yaml
from pydantic import ValidationError

from sage.api.errors import VaultConfigValidationError
from sage.config import VaultConfig
from sage.storage.graph_store import GraphStore


_REQUIRED_SECTIONS = (
    "vault", "document_types", "lifecycle",
    "source_adapters", "metadata_extraction", "edge_inference",
)
_OPTIONAL_SECTIONS = ("abstraction", "access_control_defaults", "retrieval_health")
_ALL_SECTIONS = _REQUIRED_SECTIONS + _OPTIONAL_SECTIONS

_VAULTS_ROOT = Path("~/sage_vaults").expanduser()


def _validate_config(config_dict: dict) -> VaultConfig:
    """Validate a config dict, raising VaultConfigValidationError on failure."""
    try:
        return VaultConfig.model_validate(config_dict)
    except ValidationError as exc:
        errors = [
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
            for e in exc.errors()
        ]
        raise VaultConfigValidationError(errors) from exc


def _write_config_yaml(config_path: Path, config_dict: dict) -> None:
    """Atomically write a config dict to YAML (temp file + rename)."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(config_path.parent), suffix=".yaml.tmp"
    )
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
                warnings.append(
                    f"Removing doc_type '{dt}' would affect {n} document(s)"
                )

    old_states = {s.value for s in old_config.lifecycle.states}
    new_states = {s.value for s in new_config.lifecycle.states}
    removed_states = old_states - new_states
    if removed_states:
        counts = await graph_store.get_document_counts_by_field("lifecycle_status")
        for st in sorted(removed_states):
            n = counts.get(st, 0)
            if n > 0:
                warnings.append(
                    f"Removing lifecycle state '{st}' would affect {n} document(s)"
                )

    return warnings


def _get_default_config(vault_id: str, name: str, owner: str) -> dict:
    """Generate a minimal valid config dict for a new vault."""
    return {
        "vault": {
            "id": vault_id,
            "name": name,
            "owner": owner,
            "storage_root": str(_VAULTS_ROOT / vault_id / "sources"),
            "brain_root": str(_VAULTS_ROOT / vault_id / "brain"),
            "visibility": "personal",
        },
        "document_types": {
            "doc_types": [
                {
                    "value": "document",
                    "label": "Document",
                    "description": "General-purpose document type.",
                },
                {
                    "value": "reference",
                    "label": "Reference",
                    "description": "Reference material and supporting documents.",
                },
            ],
        },
        "lifecycle": {
            "base_states_required": True,
            "states": [
                {"value": "active", "label": "Active"},
                {"value": "completed", "label": "Completed"},
                {"value": "archived", "label": "Archived", "is_terminal": True},
            ],
            "transitions": [
                {"from_state": "(new)", "action": "ingest", "to_state": "active"},
                {
                    "from_state": "active",
                    "action": "supersede",
                    "to_state": "archived",
                    "creates_edge": "supersedes",
                },
                {"from_state": "active", "action": "complete", "to_state": "completed"},
                {"from_state": "active", "action": "archive", "to_state": "archived"},
                {"from_state": "completed", "action": "archive", "to_state": "archived"},
                {"from_state": "archived", "action": "reactivate", "to_state": "active"},
            ],
        },
        "source_adapters": {
            "adapters": [
                {"source_type": "markdown", "enabled": True},
                {"source_type": "docx", "enabled": True},
                {"source_type": "xlsx", "enabled": True},
            ],
        },
        "metadata_extraction": {
            "review_required": False,
            "filename_extraction": {
                "separator": "_",
            },
        },
        "edge_inference": {
            "tier_assignments": [
                {
                    "edge_type": "supersedes",
                    "tier": 1,
                    "inference_rules": [{"method": "version_chain"}],
                },
            ],
        },
        "abstraction": {"enabled": False},
    }


def _config_path_for_vault(vault_id: str) -> Path:
    """Return the canonical config file path for a vault."""
    return _VAULTS_ROOT / vault_id / "vault_config.yaml"
