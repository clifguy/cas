"""Vault listing, statistics, and hash-check endpoints.

GET /sage_vaults -- list all configured vaults (BE-001, BE-002)
GET /sage_vaults/{vault_id}/stats -- vault statistics (BE-003 through BE-006)
POST /sage_vaults/{vault_id}/hash-check -- bulk hash check (BE-007 through BE-009)
"""

from pathlib import Path

from fastapi import APIRouter, Depends, Request

from sage.api.dependencies import get_graph_store, get_vault_id
from sage.api.errors import VaultNotFoundError
from sage.mcp_init import SAGEServices
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

router = APIRouter(tags=["vaults"])


@router.get("/sage_vaults", response_model=list[VaultSummary])
async def list_vaults(request: Request) -> list[VaultSummary]:
    """Return all vaults registered with the running SAGE instance."""
    registry: dict[str, SAGEServices] = request.app.state.vault_registry
    results = []
    for vault_id, services in registry.items():
        cfg = services.config
        vault = cfg.vault

        doc_types = [
            VaultDocTypeEntry(value=dt.value, label=dt.label)
            for dt in cfg.document_types.doc_types
        ]
        lifecycle_states = [
            VaultLifecycleState(
                value=s.value, label=s.label, is_terminal=s.is_terminal
            )
            for s in cfg.lifecycle.states
        ]
        adapters = []
        for src_type, adapter_cfg in cfg.source_adapters.items():
            if isinstance(adapter_cfg, dict):
                adapters.append(VaultAdapterInfo(
                    source_type=src_type,
                    enabled=adapter_cfg.get("enabled", True),
                    extensions=adapter_cfg.get("extensions", []),
                ))

        results.append(VaultSummary(
            id=vault.id,
            name=vault.name,
            description=getattr(vault, "description", None),
            storage_root=vault.storage_root,
            doc_types=doc_types,
            lifecycle_states=lifecycle_states,
            adapters=adapters,
        ))
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

    lancedb_dir = brain_root / "content_store.lance"
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
            deferred_abstract_count=deferred_count,
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
