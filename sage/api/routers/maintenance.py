"""Maintenance/admin router (CAS-ADR-029).

Pilot operation: POST /sage_vaults/{vault_id}/admin/migrate. The first
operation on the SAGE Core API maintenance surface; subsequent
``sage_admin_*`` operations are added here with the same three-layer
shape.
"""

from fastapi import APIRouter, Depends

from sage.api.dependencies import get_maintenance_service, get_vault_id
from sage.models.schemas import ErrorResponse, MigrationReport, VaultIdStr
from sage.services.maintenance import MaintenanceService

router = APIRouter(tags=["Maintenance"])


@router.post(
    "/admin/migrate",
    response_model=MigrationReport,
    responses={
        404: {
            "model": ErrorResponse,
            "description": "`vault_not_found`: no vault registered with that id.",
        },
    },
)
async def admin_migrate_vault(
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: MaintenanceService = Depends(get_maintenance_service),
) -> MigrationReport:
    return await service.migrate_vault()
