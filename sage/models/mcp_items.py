"""Item models of the MCP batch tools that the Core API has no counterpart for.

A Core API request model lives in ``sage.models.schemas`` beside its published
OpenAPI schema. The models here describe items only an MCP tool receives, so
they are published through that tool's input schema instead of the Core API
specification.
"""

from __future__ import annotations

from pydantic import Field

from sage.models.schemas import BatchIngestFileMetadata

__all__ = ["BulkIngestFileEntry"]


class BulkIngestFileEntry(BatchIngestFileMetadata):
    """One file entry of the ``bulk_ingest_document`` MCP tool.

    The upload's per-file metadata plus the entry's source, because the tool
    receives no file parts: each entry names its bytes by exactly one of
    ``file_path`` or ``transfer_token``. Its field names are the set the tool
    refuses any other name against (CAS-ADR-037).
    """

    file_path: str | None = Field(
        default=None,
        description=(
            "Path to the source file, read directly only when the caller's machine "
            "is the machine running the SAGE server process. Supply exactly one of "
            "`file_path` or `transfer_token`."
        ),
    )
    transfer_token: str | None = Field(
        default=None,
        description=(
            "The one-time token from a previously returned upload recipe, redeemed "
            "after the recipe's byte leg delivered this file to the upload endpoint. "
            "Supply exactly one of `file_path` or `transfer_token`."
        ),
    )
