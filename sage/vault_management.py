"""Shared helpers for vault-config validation and persistence.

Consumed by the vault registry and per-vault config services (under
sage/services/) so vault creation and update behavior is defined in one
place. Default-config generation lives on VaultRegistryService.
"""

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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
    "metadata_extraction",
    "edge_inference",
)
_OPTIONAL_SECTIONS = (
    "adapter_defaults",
    "abstraction",
    "retrieval_health",
)
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

    This is the codebase's only read of ``_VAULTS_ROOT``, and deliberately so:
    every other root consumer resolves through :func:`bound_vault_root` or
    through this function, which keeps the constant a single redirect point
    rather than a location several modules can reach independently. A caller
    that needs the default root asks here; one that needs the root the process
    is actually bound to asks :func:`bound_vault_root`. Reading the constant
    directly is neither, and reintroduces the divergence between a served path
    and the process's own binding that CAS-ADR-043 exists to prevent.
    """
    env = os.environ.get("SAGE_VAULT_ROOT")
    return Path(env).expanduser() if env else _VAULTS_ROOT


def bound_vault_root() -> Path:
    """Return the vault root this process is bound to.

    Resolution order: the root the active transport lifespan published, else
    ``default_vault_root()`` (``$SAGE_VAULT_ROOT``, else the module-level
    ``_VAULTS_ROOT``). The lifespan-published root is the authority whenever one
    exists, because it is what discovery and the vault-source binding already
    resolved from ``--vault-root`` / ``SAGE_VAULT_ROOT`` / the default
    (CAS-ADR-043); falling back to the default chain covers the callers that run
    without a lifespan at all -- repo scripts, in-process mounts, and
    injected-config paths.

    The import is function-local because ``sage.mcp_init`` sits above this
    module: resolving it at call time keeps the dependency one-directional at
    import time.
    """
    from sage.mcp_init import get_vault_root

    return get_vault_root() or default_vault_root()


def config_refusal(exc: Exception) -> VaultConfigValidationError:
    """Translate a configuration parse or validation failure into a refusal.

    One translator for every caller that turns a declaration into a
    ``VaultConfig``, whether it validates a dict in hand or reads the
    declaration back through a vault-source store. Each accepted cause names
    what a caller has to correct: a field and its message, the schema path
    inside ``metadata_schema``, or the YAML parse position. Anything else is
    re-raised by the caller rather than described as a configuration error.
    """
    if isinstance(exc, ValidationError):
        return VaultConfigValidationError([_describe_error(e) for e in exc.errors()])
    if isinstance(exc, jsonschema.SchemaError):
        path_str = ".".join(str(p) for p in exc.path) or "<root>"
        return VaultConfigValidationError(
            [f"document_types.metadata_schema {path_str}: {exc.message}"]
        )
    if isinstance(exc, yaml.YAMLError):
        return VaultConfigValidationError([f"declaration is not valid YAML: {exc}"])
    raise TypeError(f"not a configuration failure: {type(exc).__name__}")


def _describe_error(error: Mapping[str, Any]) -> str:
    """Render one Pydantic error as ``<location>: <message>``.

    A model-level error has no location, so it renders as its message alone,
    and a ``ValueError`` raised there as the raised text without Pydantic's
    ``Value error,`` preamble: the text already names what it refuses.
    """
    location = ".".join(str(p) for p in error["loc"])
    if location:
        return f"{location}: {error['msg']}"
    raised = error.get("ctx", {}).get("error")
    if error["type"] == "value_error" and raised is not None:
        return str(raised)
    return error["msg"]


#: The failures :func:`config_refusal` translates. A caller catches exactly
#: these and lets anything else propagate.
CONFIG_FAILURES: tuple[type[Exception], ...] = (
    ValidationError,
    jsonschema.SchemaError,
    yaml.YAMLError,
)


def _validate_config(config_dict: dict) -> VaultConfig:
    """Validate a config dict, raising VaultConfigValidationError on failure.

    The tier3 validator cache is built by ``VaultConfig.model_post_init``
    during ``model_validate``; a malformed ``metadata_schema`` therefore
    surfaces here at vault-create / update_config time rather than at the
    first ingest call. A retired section is refused here rather than
    warned about: this path validates a request, not a stored configuration.
    """
    try:
        return VaultConfig.model_validate(config_dict)
    except (ValidationError, jsonschema.SchemaError) as exc:
        raise config_refusal(exc) from exc


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
    """Return warnings for removed doc_types or states, and narrowed state scopes,
    that would affect existing documents."""
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

    # A state newly scoped, or scoped more narrowly, strands the documents
    # already in it whose doc_type the new scope excludes: no transition may
    # move them, and nothing names the configuration as the cause
    # (CAS-ADR-054). The scan runs only when some kept state's scope narrowed.
    narrowed: dict[str, set[str]] = {}
    for state in new_config.lifecycle.states:
        if state.value not in old_states or state.doc_types is None:
            continue
        old_scope = old_config.lifecycle.state_scope(state.value)
        if old_scope is None or not set(old_scope) <= set(state.doc_types):
            narrowed[state.value] = set(state.doc_types)
    if narrowed:
        stranded: dict[str, int] = {}
        for doc in await graph_store.list_all_documents():
            scope = narrowed.get(doc.lifecycle_status)
            if scope is not None and doc.doc_type not in scope:
                stranded[doc.lifecycle_status] = stranded.get(doc.lifecycle_status, 0) + 1
        for st in sorted(stranded):
            warnings.append(
                f"Scoping lifecycle state '{st}' to doc_type(s) "
                f"{', '.join(sorted(narrowed[st]))} would strand {stranded[st]} document(s) "
                "of other doc_types in it"
            )

    return warnings


def config_path_for_vault(vault_id: str) -> Path:
    """Return the canonical config file path for a vault, under the root this
    process is bound to.

    Resolves through :func:`bound_vault_root`, so the path tracks the same root
    authority discovery and the vault-source binding use rather than a fixed
    location. Callers that want the vault *directory* rather than the config
    file take ``.parent`` of this result and inherit the same resolution -- the
    maintenance audit log's writer and reader both do, and must agree.
    """
    return bound_vault_root() / vault_id / "vault_config.yaml"
