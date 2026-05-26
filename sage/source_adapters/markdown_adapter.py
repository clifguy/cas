"""Markdown source adapter: parses # headings and extracts structure.

Computes SHA-256 of source file bytes for content_hash.
"""

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from markdown_it import MarkdownIt
from mdit_py_plugins.front_matter import front_matter_plugin

from sage.source_adapters.base import HeadingNode, ProjectionResult, SourceAdapter


class MarkdownAdapter(SourceAdapter):
    # 0.2.0: chunks indexed with heading-context (heading_path embedded with
    # content, plus FTS index on heading_path).
    # 0.3.0: chunker emits one chunk per heading regardless of body content
    # (Word-Find equivalence for empty-content parents).
    # 0.4.0: CommonMark-compliant heading extraction via markdown-it-py.
    # Suppresses ATX heading-shaped lines inside fenced and indented code
    # blocks.
    # 0.5.0: enables mdit-py-plugins front_matter_plugin so YAML
    # frontmatter is recognized as a block-level construct rather than
    # binding its closing `---` to the preceding YAML body as a setext
    # H2 underline.
    VERSION = "0.5.0"
    EXTENSIONS = [".md", ".markdown"]

    async def project(self, source_path: Path, config: dict | None = None) -> ProjectionResult:
        raw_bytes = source_path.read_bytes()
        content_hash = hashlib.sha256(raw_bytes).hexdigest()
        text = raw_bytes.decode("utf-8")

        headings = self._parse_headings(text)
        title = self._extract_title(headings, source_path)

        source_mtime = datetime.fromtimestamp(source_path.stat().st_mtime, tz=timezone.utc)

        return ProjectionResult(
            text=text,
            headings=headings,
            content_hash=content_hash,
            adapter_version=self.VERSION,
            title=title,
            metadata={"source_modified_at": source_mtime.isoformat()},
        )

    def _parse_headings(self, text: str) -> list[HeadingNode]:
        """Extract headings via CommonMark token stream.

        Code-block tokens (`fence`, `code_block`) never produce `heading_open`,
        so any `#`-shaped lines inside them are suppressed by construction.
        """
        md = MarkdownIt("commonmark")
        md.use(front_matter_plugin)
        tokens = md.parse(text)
        lines = text.split("\n")

        # First pass: collect (level, text, start_line, end_line) for each
        # heading_open token. The next inline token carries the heading text.
        raw: list[tuple[int, str, int, int]] = []
        for i, tok in enumerate(tokens):
            if tok.type != "heading_open":
                continue
            level = int(tok.tag[1:])
            inline = tokens[i + 1] if i + 1 < len(tokens) else None
            heading_text = inline.content.strip() if inline is not None else ""
            start, end = tok.map if tok.map is not None else (0, 0)
            raw.append((level, heading_text, start, end))

        headings: list[HeadingNode] = []
        stack: list[tuple[int, str]] = []
        for idx, (level, heading_text, _start, end) in enumerate(raw):
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading_text))
            path = " > ".join(h[1] for h in stack)

            next_start = raw[idx + 1][2] if idx + 1 < len(raw) else len(lines)
            content = "\n".join(lines[end:next_start]).strip()

            headings.append(
                HeadingNode(
                    level=level,
                    text=heading_text,
                    path=path,
                    content=content,
                )
            )

        return headings

    def _extract_title(self, headings: list[HeadingNode], source_path: Path) -> str:
        """Extract title from first H1, falling back to filename."""
        for h in headings:
            if h.level == 1:
                return h.text
        return source_path.stem
