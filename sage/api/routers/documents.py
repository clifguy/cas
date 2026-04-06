"""GET /sage_vaults/{vault_id}/documents/{document_id} -- get_document (BH-022)."""

from fastapi import APIRouter, Depends

from sage.api.dependencies import get_graph_store, get_vault_id
from sage.api.errors import DocumentNotFoundError
from sage.models.schemas import Document
from sage.storage.graph_store import GraphStore

router = APIRouter(tags=["Document Metadata"])


@router.get("/documents/{document_id}", response_model=Document)
async def get_document(
    document_id: str,
    vault_id: str = Depends(get_vault_id),
    graph_store: GraphStore = Depends(get_graph_store),
) -> Document:
    doc = await graph_store.get_document(document_id)
    if doc is None:
        raise DocumentNotFoundError(document_id)
    return doc
