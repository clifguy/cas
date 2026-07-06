"""Shared helpers for the in-request inline-ingest byte channel.

The MCP ``ingest_document`` and ``bulk_ingest_document`` tools accept a source
file's bytes inline (base64) so a remote-mount caller -- whose local file the
SAGE server cannot see -- can still ingest with the same call shape as a
co-located caller. Decoding and the size bound live here so both tools enforce
one ceiling and one error surface (CAS-ADR-042 constraint 1: the caller-visible
surface stays profile-invariant; the per-profile byte transport below it is a
binding detail). Each tool owns its own temp staging, which differs by shape
(single file vs. batch).
"""

from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path

from sage.api.errors import InlineContentTooLargeError, InvalidInlineContentError

#: Ceiling on the in-request inline-ingest byte channel (``content_base64``).
#: Its own knob, separate from the export-side inline-content ceiling in
#: ``sage.services.documents``: this bounds an upload carried in the request,
#: not a base64-inlined download. Overridable via ``SAGE_MAX_INLINE_INGEST_BYTES``.
DEFAULT_MAX_INLINE_INGEST_BYTES = 100 * 1024 * 1024


def max_inline_ingest_bytes() -> int:
    """Return the inline-ingest byte ceiling, honoring the env override."""
    raw = os.environ.get("SAGE_MAX_INLINE_INGEST_BYTES")
    if raw is None:
        return DEFAULT_MAX_INLINE_INGEST_BYTES
    return int(raw)


def staging_name(filename: str | None, fallback: str) -> str:
    """Reduce a caller-supplied filename to a safe basename for temp staging.

    ``Path(...).name`` strips any directory components, so a path-shaped
    filename cannot escape the staging directory. Degenerate inputs whose
    basename is empty or a directory reference (``""``, ``"."``, ``".."``)
    fall back to the synthetic name rather than resolving to the staging
    directory itself and failing with an unstructured OS error.
    """
    name = Path(filename).name if filename else ""
    if name in ("", ".", ".."):
        return fallback
    return name


def decode_inline_content(content_base64: str) -> bytes:
    """Decode and bound-check inline ingest bytes before any staging.

    The ceiling is enforced on the decoded size *before* the bytes are written
    anywhere, so an oversize payload is refused without touching disk. A
    malformed base64 payload surfaces as a structured 400 rather than a generic
    error. Raises :class:`sage.api.errors.InvalidInlineContentError` or
    :class:`sage.api.errors.InlineContentTooLargeError`.
    """
    try:
        raw = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidInlineContentError(str(exc)) from exc
    ceiling = max_inline_ingest_bytes()
    if len(raw) > ceiling:
        raise InlineContentTooLargeError(len(raw), ceiling)
    return raw
