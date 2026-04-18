"""Vault listing, statistics, hash-check, and configuration endpoints.

GET  /sage_vaults                     -- list all configured vaults (BE-001, BE-002)
GET  /sage_vaults/{vault_id}/stats    -- vault statistics (BE-003 through BE-006)
POST /sage_vaults/{vault_id}/hash-check -- bulk hash check (BE-007 through BE-009)
GET  /sage_vaults/{vault_id}/config   -- read vault configuration
PUT  /sage_vaults/{vault_id}/config   -- update vault configuration (section-level)
POST /sage_vaults                     -- create a new vault with config
"""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from sage.api.dependencies import get_graph_store, get_vault_id
from sage.api.errors import (
    DestructiveConfigChangeError,
    VaultAlreadyExistsError,
    VaultConfigValidationError,
    VaultNotFoundError,
)
from sage.config import VaultConfig
from sage.mcp_init import SAGEServices, reload_vault_in_registry
from sage.models.schemas import (
    HashCheckMatch,
    HashCheckRequest,
    HealthIndicators,
    VaultAdapterInfo,
    VaultDocTypeEntry,
    VaultLifecycleState,
    VaultStatsResponse,
    VaultSummary,
)
from sage.storage.graph_store import GraphStore
from sage.vault_management import (
    _ALL_SECTIONS,
    _check_destructive_changes,
    _config_path_for_vault,
    _validate_config,
    _write_config_yaml,
)

router = APIRouter(tags=["vaults"])


def _build_vault_summary(
    config: VaultConfig,
    services: SAGEServices,
    projects: list[str] | None = None,
) -> VaultSummary:
    """Build a VaultSummary from a config and services instance."""
    vault = config.vault
    doc_types = [
        VaultDocTypeEntry(value=dt.value, label=dt.label)
        for dt in config.document_types.doc_types
    ]
    lifecycle_states = [
        VaultLifecycleState(
            value=s.value, label=s.label, is_terminal=s.is_terminal
        )
        for s in config.lifecycle.states
    ]
    adapters = [
        VaultAdapterInfo(
            source_type=source_type.value,
            enabled=True,
            extensions=adapter.EXTENSIONS,
        )
        for source_type, adapter in services.ingestion_service.registered_adapters.items()
    ]
    return VaultSummary(
        id=vault.id,
        name=vault.name,
        description=getattr(vault, "description", None),
        storage_root=vault.storage_root,
        doc_types=doc_types,
        lifecycle_states=lifecycle_states,
        adapters=adapters,
        projects=projects or [],
    )


# ---------------------------------------------------------------------------
# Request models for config endpoints
# ---------------------------------------------------------------------------

class UpdateVaultConfigRequest(BaseModel):
    """Section-level config update.  Only provided sections are replaced."""
    vault: dict | None = None
    document_types: dict | None = None
    lifecycle: dict | None = None
    source_adapters: dict | None = None
    metadata_extraction: dict | None = None
    edge_inference: dict | None = None
    abstraction: dict | None = None
    access_control_defaults: dict | None = None
    retrieval_health: dict | None = None


class CreateVaultRequest(BaseModel):
    """Full config dict for new vault creation."""
    config: dict


# ---------------------------------------------------------------------------
# Existing endpoints
# ---------------------------------------------------------------------------

@router.get("/sage_vaults", response_model=list[VaultSummary])
async def list_vaults(request: Request) -> list[VaultSummary]:
    """Return all vaults registered with the running SAGE instance."""
    registry: dict[str, SAGEServices] = request.app.state.vault_registry
    results = []
    for vault_id, services in registry.items():
        cfg = services.config
        project_counts = await services.graph_store.get_document_counts_by_field("project")
        projects = sorted(project_counts.keys())
        results.append(_build_vault_summary(cfg, services, projects))
    return results


@router.get(
    "/sage_vaults/{vault_id}/stats",
    response_model=VaultStatsResponse,
)
async def vault_stats(
    request: Request,
    vault_id: str = Depends(get_vault_id),
    graph_store: GraphStore = Depends(get_graph_store),
) -> VaultStatsResponse:
    """Return all Dashboard statistics for a vault."""
    registry: dict[str, SAGEServices] = request.app.state.vault_registry
    services = registry[vault_id]
    config = services.config

    # Aggregate counts
    total_documents = await graph_store.get_total_document_count()
    by_lifecycle = await graph_store.get_document_counts_by_field("lifecycle_status")
    by_doc_type = await graph_store.get_document_counts_by_field("doc_type")
    by_source_adapter = await graph_store.get_document_counts_by_field("source_type")
    total_edges = await graph_store.get_total_edge_count()
    by_edge_type = await graph_store.get_edge_counts_by_type()
    staging_count = await graph_store.count_staging_edges()
    last_ingestion = await graph_store.get_last_ingestion_at()

    # Health indicators
    failed_count = await graph_store.count_documents_by_pipeline_status("failed")
    deferred_count = await graph_store.count_documents_by_pipeline_status(
        "abstraction_skipped"
    )
    pending_metadata_docs = await graph_store.list_pending_metadata_documents()
    pending_metadata_count = len(pending_metadata_docs)

    # Storage sizes
    brain_root = Path(config.vault.brain_root).expanduser()
    sqlite_path = brain_root / "graph.db"
    sqlite_size = sqlite_path.stat().st_size if sqlite_path.exists() else 0

    lancedb_dir = brain_root / "lancedb"
    lancedb_size = 0
    if lancedb_dir.exists():
        for f in lancedb_dir.rglob("*"):
            if f.is_file():
                lancedb_size += f.stat().st_size

    return VaultStatsResponse(
        total_documents=total_documents,
        by_lifecycle_state=by_lifecycle,
        by_doc_type=by_doc_type,
        by_source_adapter=by_source_adapter,
        total_edges=total_edges,
        by_edge_type=by_edge_type,
        staging_edge_count=staging_count,
        lancedb_size_bytes=lancedb_size,
        sqlite_size_bytes=sqlite_size,
        last_ingestion_at=last_ingestion,
        health=HealthIndicators(
            pending_metadata_count=pending_metadata_count,
            pending_edge_count=staging_count,
            deferred_abstract_count=deferred_count if config.abstraction.enabled else None,
            failed_ingestion_count=failed_count,
        ),
    )


@router.post(
    "/sage_vaults/{vault_id}/hash-check",
    response_model=dict[str, HashCheckMatch],
)
async def hash_check(
    body: HashCheckRequest,
    vault_id: str = Depends(get_vault_id),
    graph_store: GraphStore = Depends(get_graph_store),
) -> dict[str, HashCheckMatch]:
    """Bulk hash existence check against the graph store."""
    if not body.hashes:
        return {}

    matches = await graph_store.find_documents_by_hashes(body.hashes)

    result: dict[str, HashCheckMatch] = {}
    for h in body.hashes:
        if h in matches:
            result[h] = HashCheckMatch(exists=True, document_id=matches[h])
        else:
            result[h] = HashCheckMatch(exists=False)
    return result


# ---------------------------------------------------------------------------
# Config endpoints
# ---------------------------------------------------------------------------

@router.get("/sage_vaults/{vault_id}/config")
async def get_vault_config(
    request: Request,
    vault_id: str = Depends(get_vault_id),
) -> dict:
    """Return the full vault configuration as JSON."""
    registry: dict[str, SAGEServices] = request.app.state.vault_registry
    services = registry[vault_id]
    return services.config.model_dump()


@router.put("/sage_vaults/{vault_id}/config")
async def update_vault_config(
    body: UpdateVaultConfigRequest,
    request: Request,
    vault_id: str = Depends(get_vault_id),
    graph_store: GraphStore = Depends(get_graph_store),
    force: bool = False,
) -> JSONResponse:
    """Update vault configuration at the section level.

    Each provided top-level section replaces the current section wholesale;
    omitted sections are preserved. Partial-section merges are not supported.

    If the merged config removes a doc_type or lifecycle state that still
    has documents attached, the request is rejected with 409 and a
    destructive_config_change error unless the caller passes
    ?force=true. With force=true, the update proceeds and the warnings
    are returned in the response.
    """
    registry: dict[str, SAGEServices] = request.app.state.vault_registry
    services = registry[vault_id]
    old_config = services.config

    merged = old_config.model_dump()
    body_dict = body.model_dump(exclude_none=True)
    for section in _ALL_SECTIONS:
        if section in body_dict:
            merged[section] = body_dict[section]

    if "vault" in body_dict and body_dict["vault"].get("id") != vault_id:
        raise VaultConfigValidationError(
            ["vault.id cannot be changed; create a new vault instead"]
        )

    new_config = _validate_config(merged)

    warnings = await _check_destructive_changes(old_config, new_config, graph_store)
    if warnings and not force:
        raise DestructiveConfigChangeError(warnings)

    config_path = _config_path_for_vault(vault_id)
    _write_config_yaml(config_path, merged)

    await reload_vault_in_registry(registry, vault_id, new_config)

    return JSONResponse(
        content={
            "status": "updated",
            "vault_id": vault_id,
            "warnings": warnings,
        }
    )


@router.post("/sage_vaults", status_code=201, response_model=VaultSummary)
async def create_vault(
    body: CreateVaultRequest,
    request: Request,
) -> VaultSummary:
    """Create a new vault from a full config dict.

    Creates the vault directory, writes vault_config.yaml, initializes
    services, and registers the vault in the running instance.
    """
    registry: dict[str, SAGEServices] = request.app.state.vault_registry

    config = _validate_config(body.config)
    vault_id = config.vault.id

    if vault_id in registry:
        raise VaultAlreadyExistsError(vault_id)

    config_path = _config_path_for_vault(vault_id)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    Path(config.vault.storage_root).expanduser().mkdir(parents=True, exist_ok=True)
    Path(config.vault.brain_root).expanduser().mkdir(parents=True, exist_ok=True)

    _write_config_yaml(config_path, body.config)

    from sage.mcp_init import initialize_services
    services = await initialize_services(config)
    registry[vault_id] = services

    await services.user_service.bootstrap_owner()

    return _build_vault_summary(config, services)
