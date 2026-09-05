"""POST /sage_vaults/{vault_id}/documents -- ingest (BH-018 through BH-026).

Also exposes ``POST /documents:batch``: the hosted-profile bulk-ingest
surface that accepts uploaded file content (no shared filesystem) and
runs the three-phase batch pipeline server-side, streaming SSE progress.
"""

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from sage.api.dependencies import get_ingestion_service, get_vault_id, get_vault_services
from sage.api.errors import SAGEError
from sage.mcp_init import SAGEServices
from sage.models.schemas import (
    BatchIngestUploadMetadata,
    ErrorResponse,
    IngestRequest,
    IngestResponse,
    VaultIdStr,
)
from sage.services.batch_ingest_stream import UploadedFile, stream_uploaded_batch_ingest
from sage.services.ingestion import IngestionService

router = APIRouter(tags=["Ingestion"])


@router.post(
    "/documents",
    response_model=IngestResponse,
    responses={
        400: {
            "model": ErrorResponse,
            "description": (
                "`adapter_not_found`: no source adapter is registered for "
                "`source_type`.\n\n"
                "`vault_source_path_refused`: the vault-source store refused "
                "the destination it would have retained the source at.\n\n"
                "`expected_head_version_requires_predecessor`: "
                "`expected_head_version` was supplied without "
                "`predecessor_id`. The token is bound to the chain head "
                "identified by the predecessor (CAS-ADR-038 Primitive C)."
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
                "`force_reingest_path_mismatch`: `force: true` and the "
                "content-hash match resolves to a document stored at a "
                "different `source_path` than `source`, without a "
                "`document_id` confirming the target. Force-reingest keys the "
                "record to reuse by content hash alone, so a byte-identical "
                "file at a different path can collide with an unrelated "
                "document; pass `document_id` to confirm the intended record. "
                "Detail carries `existing_document_id`, `existing_source_path`, "
                "`new_source_path`, and `source_content_hash`.\n\n"
                "`supersede_target_not_active`: `predecessor_id` was set but "
                "the vault's lifecycle transition table does not permit "
                "`supersede` from the predecessor's current state. Detail "
                "carries `current_state` and the `allowed_states` the table "
                "does permit; a table permitting `supersede` from no state "
                "reports an empty `allowed_states` and a `required_state` of "
                "`(none)` rather than naming a state the vault does not "
                "permit. There is no one remedy, because there is no one "
                "table: read `allowed_states`, or the vault's "
                "`lifecycle.transitions` via "
                "`GET /sage_vaults/{vault_id}/config`, for the states this "
                "vault admits. A vault may permit `supersede` from "
                "`completed` or another non-`active` state, in which case the "
                "predecessor needs no walk-back at all. Where one is needed, "
                "move the predecessor to a permitted state directly from "
                "where it stands via "
                "`POST /sage_vaults/{vault_id}/lifecycles` -- `reactivate` "
                "from `completed`, where the table declares it -- rather than "
                "archiving first, which records shipped work as dropped for "
                "the length of the walk.\n\n"
                "`identical_content_supersede`: the new file's content hash "
                "matches the predecessor's; supersede chains require "
                "distinct content per step.\n\n"
                "`stale_chain_head`: `expected_head_version` was supplied "
                "and did not match the predecessor's current `updated_at` "
                "at supersede time. Detail carries the current head id and "
                "version so the caller can pivot through the chain and "
                "retry (CAS-ADR-038 Primitive C)."
            ),
        },
        422: {
            "model": ErrorResponse,
            "description": (
                "Ingestion failure. The source adapter could not produce a "
                "valid projection (unsupported format, corrupt content)."
            ),
        },
        502: {
            "model": ErrorResponse,
            "description": (
                "`vault_source_store_refused`: the vault-source store declined the "
                "operation on its merits -- quota, a permission it withdrew, a reply "
                "that opened no usable upload session. Resolve it at the store before "
                "retrying; `detail.store_status` carries the status it declined with."
            ),
        },
        503: {
            "model": ErrorResponse,
            "description": (
                "`vault_source_store_unavailable`: the vault-source store declined to "
                "serve the operation just now -- throttling, a transient backend "
                "signal, an upload session it expired. The same request may succeed "
                "on a later attempt."
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


@router.post(
    "/documents:batch",
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": (
                "SSE stream: one ``progress`` event per per-file state "
                "transition (started + completed/failed), then one final "
                "``summary`` event carrying the batch ingest counts. See "
                "sage.models.schemas.ProgressEvent and SummaryEvent."
            ),
        },
        400: {
            "model": ErrorResponse,
            "description": (
                "`empty_file_list`: no files were uploaded.\n\n"
                "`invalid_batch_metadata`: the `metadata` form field is not "
                "valid JSON for the BatchIngestUploadMetadata schema, or its "
                "`files` length does not match the number of uploaded file "
                "parts."
            ),
        },
        404: {
            "model": ErrorResponse,
            "description": "`vault_not_found`: no vault registered with that id.",
        },
    },
)
async def batch_ingest_documents(
    files: list[UploadFile] | None = File(
        default=None,
        description="Uploaded source files to ingest, in the same order as `metadata.files`.",
    ),
    metadata: str = Form(
        description=(
            "JSON-encoded BatchIngestUploadMetadata: per-file source_type and "
            "optional parsed_metadata, plus the batch infer_edges and "
            "needs_review flags."
        ),
    ),
    vault_id: VaultIdStr = Depends(get_vault_id),
    services: SAGEServices = Depends(get_vault_services),
) -> StreamingResponse:
    """Upload files and run the batch-ingest pipeline server-side, streaming SSE.

    The hosted-profile counterpart to the in-process ``/app/ingest``
    path: file content is delivered by upload (the BFF and SAGE share no
    filesystem in the cloud), staged to a temporary directory under the
    SAGE process with the original filename preserved (so the vault's
    FilenameParser sees the right stem and provenance hashes the uploaded
    bytes), then ingested through the same three-phase
    ``BatchIngestService`` the co-located profile drives -- so both
    profiles produce equivalent ingest summaries.

    Pre-stream validation is load-bearing: an unknown vault (``get_vault_id``),
    an empty upload, invalid ``metadata`` JSON, or a metadata/file-count
    mismatch all raise BEFORE the ``StreamingResponse`` is constructed, so
    the client receives an ``application/json`` error envelope rather than a
    started 200 stream. Staged files are removed in a ``finally`` once the
    stream is exhausted.
    """
    try:
        envelope = BatchIngestUploadMetadata.model_validate_json(metadata)
    except ValueError as exc:
        raise SAGEError(
            "invalid_batch_metadata",
            f"`metadata` is not valid BatchIngestUploadMetadata JSON: {exc}",
            400,
        ) from exc

    if not files:
        raise SAGEError("empty_file_list", "No files selected for ingestion", 400)
    if len(envelope.files) != len(files):
        raise SAGEError(
            "invalid_batch_metadata",
            f"metadata.files length ({len(envelope.files)}) does not match "
            f"the uploaded file count ({len(files)})",
            400,
        )

    uploads = [
        UploadedFile(
            filename=upload.filename or f"upload_{index}",
            content=await upload.read(),
            source_type=meta.source_type,
            parsed_metadata=meta.parsed_metadata,
        )
        for index, (upload, meta) in enumerate(zip(files, envelope.files))
    ]
    return StreamingResponse(
        stream_uploaded_batch_ingest(
            uploads,
            services,
            infer_edges=envelope.infer_edges,
            needs_review=envelope.needs_review,
        ),
        media_type="text/event-stream",
    )
