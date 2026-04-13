"""Markdown source adapter: parses # headings and extracts structure.

Computes SHA-256 of source file bytes for content_hash.
"""

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from sage.source_adapters.base import HeadingNode, ProjectionResult, SourceAdapter


class MarkdownAdapter(SourceAdapter):
    VERSION = "0.1.0"
    EXTENSIONS = [".md", ".markdown"]

    async def project(
        self, source_path: Path, config: dict | None = None
    ) -> ProjectionResult:
        raw_bytes = source_path.read_bytes()
        content_hash = hashlib.sha256(raw_bytes).hexdigest()
        text = raw_bytes.decode("utf-8")

        headings = self._parse_headings(text)
        title = self._extract_title(headings, source_path)

        source_mtime = datetime.fromtimestamp(
            source_path.stat().st_mtime, tz=timezone.utc
        )

        return ProjectionResult(
            text=text,
            headings=headings,
            content_hash=content_hash,
            adapter_version=self.VERSION,
            title=title,
            metadata={"source_modified_at": source_mtime.isoformat()},
        )

    def _parse_headings(self, text: str) -> list[HeadingNode]:
        """Parse ATX-style headings (# through ######) into a hierarchy."""
        headings: list[HeadingNode] = []
        lines = text.split("\n")
        heading_pattern = re.compile(r"^(#{1,6})\s+(.+)$")

        # Track heading stack for path construction
        stack: list[tuple[int, str]] = []
        current_content_lines: list[str] = []
        current_heading_idx = -1

        for line in lines:
            match = heading_pattern.match(line)
            if match:
                # Flush content to previous heading
                if current_heading_idx >= 0:
                    headings[current_heading_idx].content = "\n".join(
                        current_content_lines
                    ).strip()
                current_content_lines = []

                level = len(match.group(1))
                heading_text = match.group(2).strip()

                # Update stack: pop deeper or equal levels
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, heading_text))

                path = " > ".join(h[1] for h in stack)

                headings.append(
                    HeadingNode(
                        level=level,
                        text=heading_text,
                        path=path,
                        content="",
                    )
                )
                current_heading_idx = len(headings) - 1
            else:
                current_content_lines.append(line)

        # Flush final heading content
        if current_heading_idx >= 0:
            headings[current_heading_idx].content = "\n".join(
                current_content_lines
            ).strip()

        return headings

    def _extract_title(
        self, headings: list[HeadingNode], source_path: Path
    ) -> str:
        """Extract title from first H1, falling back to filename."""
        for h in headings:
            if h.level == 1:
                return h.text
        return source_path.stem
