"""POST /sage_vaults/{vault_id}/parse-filename -- side-effect-free
filename parser endpoint (CAS-ADR-021).

Wraps the vault's FilenameParser through IngestionService.parse_filename
to return parsed metadata without creating a document or mutating vault
state. Intended for callers that want filename-based suggestions to
populate a UI before submitting an ingest with caller-authoritative
metadata.
"""

from fastapi import APIRouter, Depends

from sage.api.dependencies import get_ingestion_service, get_vault_id
from sage.models.schemas import ParseFilenameRequest, ParseFilenameResponse, VaultIdStr
from sage.services.ingestion import IngestionService

router = APIRouter(tags=["Utilities"])


@router.post("/parse-filename", response_model=ParseFilenameResponse)
async def parse_filename(
    request: ParseFilenameRequest,
    vault_id: VaultIdStr = Depends(get_vault_id),
    ingestion_service: IngestionService = Depends(get_ingestion_service),
) -> ParseFilenameResponse:
    return ingestion_service.parse_filename(request.filename, request.adapter)
