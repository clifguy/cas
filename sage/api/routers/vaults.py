"""Vault listing, statistics, hash-check, and configuration endpoints.

GET  /sage_vaults                     -- list all configured vaults (BE-001, BE-002)
GET /sage_vaults/default-config -- the scaffold a new vault would be created with
GET /sage_vaults/maintenance/stack-config -- the stack-wide configuration
GET  /sage_vaults/{vault_id}/stats    -- vault statistics (BE-003 through BE-006)
POST /sage_vaults/{vault_id}/hash-check -- bulk hash check (BE-007 through BE-009)
GET  /sage_vaults/{vault_id}/config   -- read vault configuration
PUT  /sage_vaults/{vault_id}/config   -- update vault configuration (section-level)
POST /sage_vaults                     -- create a new vault with config
"""

from fastapi import APIRouter, Depends

from sage.api.dependencies import (
    get_vault_config_service,
    get_vault_id,
    get_vault_registry_service,
)
from sage.api.response_docs import boundary_400, invalid_parameter_422
from sage.api.wire_route import WireRoute
from sage.models.schemas import (
    CreateVaultRequest,
    ErrorResponse,
    HashCheckRequest,
    HashCheckResponse,
    UpdateVaultConfigRequest,
    UpdateVaultConfigResponse,
    VaultIdStr,
    VaultListResponse,
    VaultStatsResponse,
    VaultSummary,
)
from sage.services.stack_config import get_stack_config_report
from sage.services.vault_config import VaultConfigService
from sage.services.vault_registry import VaultRegistryService

router = APIRouter(route_class=WireRoute, tags=["vaults"])


@router.get(
    "/sage_vaults",
    response_model=VaultListResponse,
    responses={400: boundary_400(request=("unknown_parameter",))},
)
async def list_vaults(
    service: VaultRegistryService = Depends(get_vault_registry_service),
) -> VaultListResponse:
    """Return all vaults registered with the running SAGE instance."""
    return await service.vault_list()


# Declared ahead of the vault-scoped routes: the literal segment must be
# matched before any future /sage_vaults/{vault_id} pattern could shadow it.
@router.get(
    "/sage_vaults/default-config",
    operation_id="get_default_vault_config",
    responses={
        400: boundary_400(request=("invalid_vault_id", "unknown_parameter")),
        422: invalid_parameter_422(),
    },
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


# Declared ahead of the vault-scoped routes for the same reason: the literal
# segment is matched before a vault id could be.
@router.get(
    "/sage_vaults/maintenance/stack-config",
    operation_id="get_stack_config",
    tags=["Maintenance"],
    responses={400: boundary_400(request=("unknown_parameter",))},
)
async def get_stack_config() -> dict:
    """Return the stack-wide configuration the running process loaded."""
    return get_stack_config_report()


@router.get(
    "/sage_vaults/{vault_id}/stats",
    response_model=VaultStatsResponse,
    responses={
        400: boundary_400(path=("invalid_vault_id",), request=("unknown_parameter",)),
        404: {
            "model": ErrorResponse,
            "description": (
                "`vault_not_found`: no vault registered with that id; `detail.available_vaults` "
                "lists the registered vaults."
            ),
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
    response_model=HashCheckResponse,
    responses={
        400: boundary_400(
            path=("invalid_vault_id",), request=("invalid_sha256", "unknown_parameter")
        ),
        404: {
            "model": ErrorResponse,
            "description": (
                "`vault_not_found`: no vault registered with that id; `detail.available_vaults` "
                "lists the registered vaults."
            ),
        },
        422: invalid_parameter_422(),
    },
)
async def hash_check(
    body: HashCheckRequest,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: VaultConfigService = Depends(get_vault_config_service),
) -> HashCheckResponse:
    """Bulk hash existence check against the graph store."""
    return await service.hash_check(body)


@router.get(
    "/sage_vaults/{vault_id}/config",
    responses={
        400: boundary_400(path=("invalid_vault_id",), request=("unknown_parameter",)),
        404: {
            "model": ErrorResponse,
            "description": (
                "`vault_not_found`: no vault registered with that id; `detail.available_vaults` "
                "lists the registered vaults."
            ),
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
        400: boundary_400(
            path=("invalid_vault_id",),
            request=("unknown_parameter",),
            extra="`vault_config_validation_error`: the merged config failed "
            "schema validation, or the request attempts to change `vault.id`.",
        ),
        404: {
            "model": ErrorResponse,
            "description": (
                "`vault_not_found`: no vault registered with that id; `detail.available_vaults` "
                "lists the registered vaults."
            ),
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
        422: invalid_parameter_422(),
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
        400: boundary_400(
            request=("unknown_parameter",),
            extra=(
                "`vault_config_validation_error`: the supplied configuration "
                "failed schema validation (detail includes the list of "
                "failed constraints)."
            ),
        ),
        409: {
            "model": ErrorResponse,
            "description": (
                "`vault_already_exists`: a vault with the requested `id` is already registered."
            ),
        },
        422: invalid_parameter_422(),
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
