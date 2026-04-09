"""Pending metadata endpoint.

GET /sage_vaults/{vault_id}/pending-metadata -- documents awaiting metadata
    confirmation (BE-014, BE-015).
"""

from fastapi import APIRouter, Depends

from sage.api.dependencies import get_graph_store, get_vault_id
from sage.models.schemas import ExtractedField, PendingMetadataItem
from sage.storage.graph_store import GraphStore

router = APIRouter(tags=["pending_metadata"])


def _build_extracted_fields(doc) -> dict[str, ExtractedField]:
    """Build extracted field annotations for a document.

    Source annotations indicate how each metadata field was derived:
    - "filename": extracted from the source file path
    - "content": extracted from document content (headings, body)
    - "default": vault default or system-assigned value
    """
    fields: dict[str, ExtractedField] = {}

    # Title: derived from first heading (content) or filename
    fields["title"] = ExtractedField(
        value=doc.title,
        source="content",
        alt_value=doc.source_path.rsplit("/", 1)[-1] if "/" in doc.source_path else doc.source_path,
        alt_source="filename",
    )

    # doc_type: default unless explicitly set
    if doc.doc_type:
        fields["doc_type"] = ExtractedField(value=doc.doc_type, source="default")

    # project: if present
    if doc.project:
        fields["project"] = ExtractedField(value=doc.project, source="default")

    # tags: if present
    if doc.tags:
        fields["tags"] = ExtractedField(
            value=",".join(doc.tags), source="content"
        )

    return fields


@router.get("/pending-metadata", response_model=list[PendingMetadataItem])
async def list_pending_metadata(
    vault_id: str = Depends(get_vault_id),
    graph_store: GraphStore = Depends(get_graph_store),
) -> list[PendingMetadataItem]:
    """Return documents whose extracted metadata has not been confirmed."""
    docs = await graph_store.list_pending_metadata_documents()

    return [
        PendingMetadataItem(
            document=doc,
            extracted_fields=_build_extracted_fields(doc),
        )
        for doc in docs
    ]
