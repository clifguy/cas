"""Base class and data structures for source adapters.

A source adapter reads a native file format and produces a structured
projection (text with heading hierarchy) suitable for indexing.
"""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

# Filename-derived tier3 extraction for ADR sources. The cas vault names
# ADR sources ``cas-adr-NNN_<title>.<ext>`` (lowercase prefix, exactly
# three digits, required title segment) across every source type that
# ADRs may use. Source adapters call :func:`extract_adr_id_from_filename`
# from their ``project`` methods so the resulting ``adr_id`` surfaces
# through ``ProjectionResult.metadata["adapter_tier3_metadata"]`` for the
# ingestion service to validate against the resolved doc_type's
# ``metadata_schema``.
_ADR_FILENAME_RE = re.compile(r"^cas-adr-(\d{3})_")


def extract_adr_id_from_filename(stem: str) -> dict | None:
    """Return ``{"adr_id": "NNN"}`` for a ``cas-adr-NNN_*`` filename stem.

    Returns ``None`` when the stem does not match the cas vault's ADR
    filename convention. Called from source-adapter ``project`` methods
    to populate ``ProjectionResult.metadata["adapter_tier3_metadata"]``.
    """
    match = _ADR_FILENAME_RE.match(stem)
    return {"adr_id": match.group(1)} if match is not None else None


def respell_created_path(text: str, created: Path | str, given: Path | str) -> str:
    """Return ``text`` with a path the adapter created respelled as the one it was given.

    A caller-facing message must name only the file the caller supplied. An
    adapter that writes a scratch file -- an OCR output, a content-type-rewritten
    shadow copy -- and hands it to its library gets back a diagnosis naming that
    scratch file: a location the caller never sent, which no longer exists by the
    time they read it, and whose absolute form discloses the process's temp layout
    under a hosted profile.

    The substitution belongs here, on the adapter side of the projection seam.
    The seam substitutes the path it passed in, so it structurally cannot reach a
    path created inside the adapter; the adapter is the only place that knows the
    scratch path at all.

    A substitution rather than a replacement: the library's own explanation of
    what went wrong is the part worth keeping, and only the location may change.
    A message that does not name the created path is returned unchanged.
    """
    return text.replace(str(created), str(given))


@dataclass
class HeadingNode:
    """A heading in the document's structural hierarchy."""

    level: int
    text: str
    path: str  # e.g., "Section 3 > Definitions > Normalization"
    content: str  # Text content under this heading


@dataclass
class ProjectionResult:
    """Output of a source adapter's projection stage.

    The ``metadata`` dict is a free-form channel from adapter to ingestion.
    A few keys have reserved semantics:

    - ``source_modified_at`` (ISO 8601 string): file mtime, surfaced to
      the document's ``source_modified_at`` column.
    - ``adapter_tags`` (list[str]): tag strings the adapter contributes
      to ``document.tags``. Merged (union, deduplicated) with caller- and
      filename-contributed tags.
    - ``adapter_tag_prefixes`` (list[str]): namespace prefixes owned by
      the adapter. On re-ingest, existing tags matching these prefixes
      are stripped before the fresh ``adapter_tags`` are applied, so a
      stale adapter-emitted tag does not persist when the adapter would
      no longer emit it.
    - ``adapter_tier3_metadata`` (dict): tier3 fields the adapter
      contributes from source inspection (typically filename-driven).
      The ingestion service merges this below caller-supplied
      ``tier3_metadata`` (caller wins) per CAS-ADR-021. Validated
      against the resolved doc_type's ``metadata_schema`` before the
      document is persisted.
    """

    text: str  # Full structured text
    headings: list[HeadingNode]  # Heading hierarchy
    content_hash: str  # SHA-256 of source file bytes
    adapter_version: str  # Adapter version string
    title: str  # Extracted document title
    metadata: dict = field(default_factory=dict)  # Extracted metadata


class SourceAdapter(ABC):
    """Abstract base for source file adapters."""

    VERSION: str = "0.0.0"
    EXTENSIONS: list[str] = []

    @abstractmethod
    async def project(self, source_path: Path, config: dict | None = None) -> ProjectionResult:
        """Read source file and produce structured projection."""
