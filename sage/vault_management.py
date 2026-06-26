"""Shared helpers for vault-config validation and persistence.

Consumed by the vault registry and per-vault config services (under
sage/services/) so vault creation and update behavior is defined in one
place. Default-config generation lives on VaultRegistryService.
"""

import os
import tempfile
from pathlib import Path

import jsonschema
import yaml
from pydantic import ValidationError

from sage.adapters.interfaces import GraphStore
from sage.api.errors import VaultConfigValidationError
from sage.config import VaultConfig

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


def default_vault_root() -> Path:
    """Return the default vault root: ``$SAGE_VAULT_ROOT`` else ``_VAULTS_ROOT``.

    The single resolution point the filesystem vault-source binding uses when a
    caller injects no explicit root (CAS-ADR-043), mirroring the discovery root
    the transport lifespans resolve from ``--vault-root`` / ``SAGE_VAULT_ROOT`` /
    the ``~/sage_vaults`` default. The fallback is the module-level
    ``_VAULTS_ROOT`` (not a fresh literal) so the same redirect point existing
    callers and tests already use stays authoritative.
    """
    env = os.environ.get("SAGE_VAULT_ROOT")
    return Path(env).expanduser() if env else _VAULTS_ROOT


def _validate_config(config_dict: dict) -> VaultConfig:
    """Validate a config dict, raising VaultConfigValidationError on failure.

    The tier3 validator cache is built by ``VaultConfig.model_post_init``
    during ``model_validate``; a malformed ``metadata_schema`` therefore
    surfaces here at vault-create / update_config time rather than at the
    first ingest call.
    """
    try:
        return VaultConfig.model_validate(config_dict)
    except ValidationError as exc:
        errors = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise VaultConfigValidationError(errors) from exc
    except jsonschema.SchemaError as exc:
        path_str = ".".join(str(p) for p in exc.path) or "<root>"
        raise VaultConfigValidationError(
            [f"document_types.metadata_schema {path_str}: {exc.message}"]
        ) from exc


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


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via temp-file + rename so a crash
    mid-write leaves either the prior file or the new file on disk,
    never a truncated intermediate. Mirrors ``_write_config_yaml``'s
    atomicity at the byte level so yaml-rollback uses the same shape of
    operation as the original write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".yaml.tmp")
    try:
        os.write(fd, data)
        os.close(fd)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
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
