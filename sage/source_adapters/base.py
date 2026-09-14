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


class SourceReadError(ValueError):
    """A source the adapter cannot read: malformed, truncated, encrypted, or not
    in the format the adapter projects.

    The caller's to correct, and so reported to it as such, where any other
    failure an adapter raises -- a missing processing dependency, a defect --
    stays a server fault. Raised with a single message naming the source, so the
    projection seam can respell its path. Adapters sit below the API layer and may
    not import its error hierarchy, so the translation to a public error happens at
    the service. A ``ValueError`` subclass, so nothing catching that type changes.
    """


class AdapterConfigError(ValueError):
    """A config value the adapter cannot use, refused before the source is read.

    Distinct from the ``ValueError`` an adapter raises for a source it cannot
    read: this one is the caller's to correct, and it carries the offending key
    and value so the request surfaces can report them without parsing the
    message. Adapters sit below the API layer and may not import its error
    hierarchy, so the translation to a public error happens at the service.
    """

    def __init__(self, key: str, value: object, expected: str) -> None:
        super().__init__(f"adapter config {key}={value!r}: {expected}")
        self.key = key
        self.value = value
        self.expected = expected


def _is_positive_int(value: object) -> bool:
    # ``bool`` is an ``int`` subclass; ``True`` is not a count.
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def positive_int(config: dict | None, key: str, default: int) -> int:
    """``config[key]`` when it is an integer of at least 1, ``default`` when absent.

    Raises:
        AdapterConfigError: the key is present with any other value.
    """
    if not config or key not in config:
        return default
    value = config[key]
    if not _is_positive_int(value):
        raise AdapterConfigError(key, value, "expected an integer of at least 1")
    return value


def optional_positive_int(config: dict | None, key: str) -> int | None:
    """``config[key]`` when it is an integer of at least 1 or null, ``None`` when absent.

    Raises:
        AdapterConfigError: the key is present with any other value.
    """
    if not config or config.get(key) is None:
        return None
    value = config[key]
    if not _is_positive_int(value):
        raise AdapterConfigError(key, value, "expected an integer of at least 1, or null")
    return value


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

    ``preamble`` is the authored text that precedes the first heading and so
    belongs to no ``HeadingNode``. It is empty when the document opens at a
    heading, and when the document has no headings at all, since ``text`` is
    then the document's one passage.
    """

    text: str  # Full structured text
    headings: list[HeadingNode]  # Heading hierarchy
    content_hash: str  # SHA-256 of source file bytes
    adapter_version: str  # Adapter version string
    title: str  # Extracted document title
    metadata: dict = field(default_factory=dict)  # Extracted metadata
    preamble: str = ""  # Text before the first heading


class SourceAdapter(ABC):
    """Abstract base for source file adapters."""

    VERSION: str = "0.0.0"
    EXTENSIONS: list[str] = []

    def check_config(self, config: dict | None) -> None:
        """Refuse a config value this adapter cannot use, without reading any source.

        Lets a caller refuse a request before acting on its source -- an ingest
        retains the bytes before projecting them. ``project`` refuses the same
        values, so an adapter overriding one keeps the other in step; an adapter
        whose config needs no checking leaves both alone.

        Raises:
            AdapterConfigError: a value in ``config`` the adapter cannot use.
        """

    @abstractmethod
    async def project(self, source_path: Path, config: dict | None = None) -> ProjectionResult:
        """Read source file and produce structured projection."""
