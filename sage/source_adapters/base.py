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


TEMP_LOCATION_MARKER = "<temporary location>"


def redact_temp_base(text: str, base: Path | str, given: Path | str) -> str:
    """Return ``text`` with a scratch *directory* replaced by a fixed marker.

    For files an adapter does not create but whose location it chooses: a tool
    the adapter invokes builds its own intermediates under a directory the
    adapter selected, and names one when it fails. Those files correspond to no
    path the caller supplied, so there is nothing to respell them as -- and
    substituting the caller's file for the directory would manufacture a path
    that looks real and names a file that never existed. A marker discloses
    nothing and fabricates nothing, while the tool's account of what went wrong
    survives around it.

    Every occurrence of ``given`` is held out of the redaction rather than the
    redaction being skipped when the two overlap. The caller's own file may sit
    under the scratch directory -- bytes staged into the temp area do exactly
    that -- and a blunt replacement would then eat the leading part of the very
    spelling this exists to protect. Skipping instead would leave the tool's
    intermediates disclosed whenever that happened. Splitting on ``given``
    reaches both: the caller's path survives byte-for-byte, everything around it
    is redacted.

    The base is matched in both its literal and its resolved spelling, since a
    tool reports whichever form it was given and a temp directory commonly
    reaches one through a symlink.
    """
    if not text:
        return text
    spellings = {str(base)}
    try:
        spellings.add(str(Path(base).resolve()))
    except OSError:
        pass

    def redact(segment: str) -> str:
        for spelling in sorted(spellings, key=len, reverse=True):
            if spelling:
                segment = segment.replace(spelling, TEMP_LOCATION_MARKER)
        return segment

    given_str = str(given)
    if not given_str:
        return redact(text)
    return given_str.join(redact(part) for part in text.split(given_str))


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
