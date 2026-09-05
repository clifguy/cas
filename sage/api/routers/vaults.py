"""Vault listing, statistics, hash-check, and configuration endpoints.

GET /sage_vaults -- list all configured vaults (BE-001, BE-002)
GET /sage_vaults/default-config -- the scaffold a new vault would be created with
GET /sage_vaults/{vault_id}/stats -- vault statistics (BE-003 through BE-006)
POST /sage_vaults/{vault_id}/hash-check -- bulk hash check (BE-007 through BE-009)
GET /sage_vaults/{vault_id}/config -- read vault configuration
PUT /sage_vaults/{vault_id}/config -- update vault configuration (section-level)
POST /sage_vaults -- create a new vault with config
"""

from fastapi import APIRouter, Depends

from sage.api.dependencies import (
    get_vault_config_service,
    get_vault_id,
    get_vault_registry_service,
)
from sage.models.schemas import (
    CreateVaultRequest,
    ErrorResponse,
    HashCheckMatch,
    HashCheckRequest,
    UpdateVaultConfigRequest,
    UpdateVaultConfigResponse,
    VaultIdStr,
    VaultStatsResponse,
    VaultSummary,
)
from sage.services.vault_config import VaultConfigService
from sage.services.vault_registry import VaultRegistryService

router = APIRouter(tags=["vaults"])


@router.get("/sage_vaults", response_model=list[VaultSummary])
async def list_vaults(
    service: VaultRegistryService = Depends(get_vault_registry_service),
) -> list[VaultSummary]:
    """Return all vaults registered with the running SAGE instance."""
    return await service.list_vaults()


# Declared ahead of the vault-scoped routes: the literal segment must be
# matched before any future /sage_vaults/{vault_id} pattern could shadow it.
@router.get(
    "/sage_vaults/default-config",
    operation_id="get_default_vault_config",
    summary="Return the default configuration a new vault would be created with.",
)
async def get_default_vault_config(
    vault_id: VaultIdStr,
    service: VaultRegistryService = Depends(get_vault_registry_service),
) -> dict:
    """Return the default vault configuration scaffold for a vault id.

    The scaffold precedes the vault: no vault with this id need exist, and
    none is created. The id is required because it shapes the storage and
    brain roots, which the server derives rather than anything a caller can
    compute. The display name and owner come back empty for the caller to
    fill before posting the result to the create-vault endpoint.
    """
    return service.get_default_config(vault_id)


@router.get(
    "/sage_vaults/{vault_id}/stats",
    response_model=VaultStatsResponse,
    responses={
        404: {
            "model": ErrorResponse,
            "description": "Vault not found.",
        },
    },
)
async def vault_stats(
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: VaultConfigService = Depends(get_vault_config_service),
) -> VaultStatsResponse:
    """Return all Dashboard statistics for a vault."""
    return await service.get_stats()


@router.post(
    "/sage_vaults/{vault_id}/hash-check",
    response_model=dict[str, HashCheckMatch],
    responses={
        404: {
            "model": ErrorResponse,
            "description": "Vault not found.",
        },
    },
)
async def hash_check(
    body: HashCheckRequest,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: VaultConfigService = Depends(get_vault_config_service),
) -> dict[str, HashCheckMatch]:
    """Bulk hash existence check against the graph store."""
    return await service.hash_check(body)


@router.get(
    "/sage_vaults/{vault_id}/config",
    responses={
        404: {
            "model": ErrorResponse,
            "description": "Vault not found.",
        },
    },
)
async def get_vault_config(
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: VaultConfigService = Depends(get_vault_config_service),
) -> dict:
    """Return the full vault configuration as JSON."""
    return service.get_config()


@router.put(
    "/sage_vaults/{vault_id}/config",
    response_model=UpdateVaultConfigResponse,
    responses={
        400: {
            "model": ErrorResponse,
            "description": (
                "`vault_config_validation_error`: the merged config failed "
                "schema validation, an unknown section name was passed, or "
                "the request attempts to change `vault.id`."
            ),
        },
        404: {
            "model": ErrorResponse,
            "description": "Vault not found.",
        },
        409: {
            "model": ErrorResponse,
            "description": (
                "`destructive_config_change`: the update would orphan "
                "documents (removed `doc_type` or lifecycle state still has "
                "documents attached). Pass `force=true` to proceed; the "
                "warnings then appear in the success response."
            ),
        },
    },
)
async def update_vault_config(
    body: UpdateVaultConfigRequest,
    vault_id: VaultIdStr = Depends(get_vault_id),
    force: bool = False,
    service: VaultConfigService = Depends(get_vault_config_service),
) -> UpdateVaultConfigResponse:
    """Update vault configuration at the section level.

    Each provided top-level section replaces the current section wholesale;
    omitted sections are preserved. Partial-section merges are not supported.

    If the merged config removes a doc_type or lifecycle state that still
    has documents attached, the request is rejected with 409 and a
    destructive_config_change error unless the caller passes
    ?force=true. With force=true, the update proceeds and the warnings
    are returned in the response.
    """
    return await service.update_config(vault_id, body, force)


@router.post(
    "/sage_vaults",
    status_code=201,
    response_model=VaultSummary,
    responses={
        400: {
            "model": ErrorResponse,
            "description": (
                "`vault_config_validation_error`: the supplied configuration "
                "failed schema validation (detail includes the list of "
                "failed constraints)."
            ),
        },
        409: {
            "model": ErrorResponse,
            "description": (
                "`vault_already_exists`: a vault with the requested `id` is already registered."
            ),
        },
    },
)
async def create_vault(
    body: CreateVaultRequest,
    service: VaultRegistryService = Depends(get_vault_registry_service),
) -> VaultSummary:
    """Create a new vault from a full config dict.

    Creates the vault directory, writes vault_config.yaml, initializes
    services, and registers the vault in the running instance.
    """
    return await service.create_vault(body)
