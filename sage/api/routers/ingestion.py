"""POST /sage_vaults/{vault_id}/documents -- ingest (BH-018 through BH-026)."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from sage.api.dependencies import get_ingestion_service, get_vault_id
from sage.models.schemas import ErrorResponse, IngestRequest, IngestResponse, VaultIdStr
from sage.services.ingestion import IngestionService

router = APIRouter(tags=["Ingestion"])


@router.post(
    "/documents",
    response_model=IngestResponse,
    responses={
        400: {
            "model": ErrorResponse,
            "description": (
                "`adapter_not_found`: `source_type` is not an enabled "
                "adapter on this vault. See `source_adapters.adapters` in "
                "the vault config."
            ),
        },
        404: {
            "model": ErrorResponse,
            "description": (
                "`source_file_not_found`: `source` does not resolve to a "
                "readable file on disk; or vault not found."
            ),
        },
        409: {
            "model": ErrorResponse,
            "description": (
                "`duplicate_content`: a document with the same `source_path` "
                "and content hash already exists. Use `force: true` to "
                "bypass detection and re-run the pipeline.\n\n"
                "`supersede_target_not_active`: `predecessor_id` was "
                "set but the predecessor is not in `active`. For completed, "
                "filed, or otherwise non-active predecessors, run the "
                "archive → reactivate dance via "
                "`POST /documents/{id}/lifecycle` before retrying.\n\n"
                "`identical_content_supersede`: the new file's content hash "
                "matches the predecessor's; supersede chains require "
                "distinct content per step."
            ),
        },
        422: {
            "model": ErrorResponse,
            "description": (
                "Ingestion failure. The source adapter could not produce a "
                "valid projection (unsupported format, corrupt content)."
            ),
        },
    },
)
async def ingest(
    request: IngestRequest,
    vault_id: VaultIdStr = Depends(get_vault_id),
    ingestion_service: IngestionService = Depends(get_ingestion_service),
) -> JSONResponse:
    result = await ingestion_service.ingest(request)
    response = IngestResponse(
        document=result.document,
        pipeline_status=result.document.pipeline_status,
    )
    return JSONResponse(
        status_code=201 if result.is_new else 200,
        content=response.model_dump(mode="json"),
    )
