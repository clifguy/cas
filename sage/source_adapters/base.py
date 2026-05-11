"""Base class and data structures for source adapters.

A source adapter reads a native file format and produces a structured
projection (text with heading hierarchy) suitable for indexing.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


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
