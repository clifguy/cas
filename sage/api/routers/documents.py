"""GET /sage_vaults/{vault_id}/documents/{document_id} -- get_document (BH-022).

Supports the agentic read-modify-reingest round-trip via two
mutually-exclusive content-delivery modes:
- `include_content=true` returns base64 bytes inline (BH-116 through BH-119).
- `write_to_path=<path>` writes bytes to disk and returns metadata
  (BH-125 through BH-128). Preferred for files that would otherwise
  exceed MCP tool-result size ceilings.
"""

from fastapi import APIRouter, Depends, Query

from sage.api.dependencies import get_documents_service, get_vault_id
from sage.models.schemas import DocumentWithContent
from sage.services.documents import DocumentsService

router = APIRouter(tags=["Document Metadata"])


@router.post("/documents/{document_id}/open")
async def open_document(
    document_id: str,
    vault_id: str = Depends(get_vault_id),
    service: DocumentsService = Depends(get_documents_service),
) -> dict:
    return await service.open_document_locally(document_id)


@router.get("/documents/{document_id}", response_model=DocumentWithContent)
async def get_document(
    document_id: str,
    include_content: bool = Query(default=False),
    write_to_path: str | None = Query(default=None),
    vault_id: str = Depends(get_vault_id),
    service: DocumentsService = Depends(get_documents_service),
) -> DocumentWithContent:
    return await service.get_document_with_content(document_id, include_content, write_to_path)
