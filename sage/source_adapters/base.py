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
    """Output of a source adapter's projection stage."""

    text: str  # Full structured text
    headings: list[HeadingNode]  # Heading hierarchy
    content_hash: str  # SHA-256 of source file bytes
    adapter_version: str  # Adapter version string
    title: str  # Extracted document title
    metadata: dict = field(default_factory=dict)  # Extracted metadata


class SourceAdapter(ABC):
    """Abstract base for source file adapters."""

    VERSION: str = "0.0.0"

    @abstractmethod
    async def project(
        self, source_path: Path, config: dict | None = None
    ) -> ProjectionResult:
        """Read source file and produce structured projection."""
