"""POST /sage_vaults/{vault_id}/documents -- ingest (BH-018 through BH-026)."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from sage.api.dependencies import get_ingestion_service, get_vault_id
from sage.models.schemas import IngestRequest, IngestResponse
from sage.services.ingestion import IngestionService

router = APIRouter(tags=["Ingestion"])


@router.post("/documents", response_model=IngestResponse)
async def ingest(
    request: IngestRequest,
    vault_id: str = Depends(get_vault_id),
    ingestion_service: IngestionService = Depends(get_ingestion_service),
) -> JSONResponse:
    doc, http_status = await ingestion_service.ingest(request, vault_id)
    response = IngestResponse(
        document=doc,
        pipeline_status=doc.pipeline_status,
    )
    return JSONResponse(
        status_code=http_status,
        content=response.model_dump(mode="json"),
    )
