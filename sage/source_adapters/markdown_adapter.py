"""Markdown source adapter: parses # headings and extracts structure.

Computes SHA-256 of source file bytes for content_hash.
"""

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from markdown_it import MarkdownIt
from mdit_py_plugins.front_matter import front_matter_plugin

from sage.adapters.interfaces import HEADING_PATH_SEPARATOR
from sage.source_adapters.base import (
    HeadingNode,
    ProjectionResult,
    SourceAdapter,
    extract_adr_id_from_filename,
)


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
    # 0.6.0: reports the text before the first heading as the projection's
    # preamble, so it reaches a passage; front matter is excluded.
    # 0.7.0: a heading with no text is not a heading; its line is dropped and
    # the text under it joins the section before it.
    VERSION = "0.7.0"
    EXTENSIONS = [".md", ".markdown"]

    async def project(self, source_path: Path, config: dict | None = None) -> ProjectionResult:
        raw_bytes = source_path.read_bytes()
        content_hash = hashlib.sha256(raw_bytes).hexdigest()
        text = raw_bytes.decode("utf-8")

        headings, preamble = self._parse_headings(text)
        title = self._extract_title(headings, source_path)

        source_mtime = datetime.fromtimestamp(source_path.stat().st_mtime, tz=timezone.utc)

        metadata: dict = {"source_modified_at": source_mtime.isoformat()}
        adapter_tier3 = extract_adr_id_from_filename(source_path.stem)
        if adapter_tier3 is not None:
            metadata["adapter_tier3_metadata"] = adapter_tier3

        return ProjectionResult(
            text=text,
            headings=headings,
            content_hash=content_hash,
            adapter_version=self.VERSION,
            title=title,
            metadata=metadata,
            preamble=preamble,
        )

    def _parse_headings(self, text: str) -> tuple[list[HeadingNode], str]:
        """Extract headings, and the text before the first, via CommonMark token stream.

        Code-block tokens (`fence`, `code_block`) never produce `heading_open`,
        so any `#`-shaped lines inside them are suppressed by construction.

        The text before the first heading starts below any front matter, which
        describes the document rather than belonging to its body. A document
        without headings has none: its whole text is already its one passage.

        A heading with no text (a bare ``#``, or one holding only a closing
        sequence or whitespace) is not a heading: a path built from it would be
        the empty path, which addresses text under no heading, or would carry an
        empty segment. Its line is dropped, the text under it joins the section
        before it, and a heading below it nests under the nearest named ancestor.
        """
        md = MarkdownIt("commonmark")
        md.use(front_matter_plugin)
        tokens = md.parse(text)
        lines = text.split("\n")

        # First pass: collect (level, text, start_line, end_line) for each
        # heading_open token. The next inline token carries the heading text.
        raw: list[tuple[int, str, int, int]] = []
        body_start = 0
        for i, tok in enumerate(tokens):
            if tok.type == "front_matter" and tok.map is not None:
                body_start = tok.map[1]
            if tok.type != "heading_open":
                continue
            level = int(tok.tag[1:])
            inline = tokens[i + 1] if i + 1 < len(tokens) else None
            heading_text = inline.content.strip() if inline is not None else ""
            start, end = tok.map if tok.map is not None else (0, 0)
            raw.append((level, heading_text, start, end))

        # A heading without text is dropped with its line; see the docstring.
        named = [entry for entry in raw if entry[1]]
        untitled = {line for entry in raw if not entry[1] for line in range(entry[2], entry[3])}

        headings: list[HeadingNode] = []
        stack: list[tuple[int, str]] = []
        for idx, (level, heading_text, _start, end) in enumerate(named):
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading_text))
            path = HEADING_PATH_SEPARATOR.join(h[1] for h in stack)

            next_start = named[idx + 1][2] if idx + 1 < len(named) else len(lines)
            content = _body(lines, end, next_start, untitled)

            headings.append(
                HeadingNode(
                    level=level,
                    text=heading_text,
                    path=path,
                    content=content,
                )
            )

        preamble = _body(lines, body_start, named[0][2], untitled) if named else ""
        return headings, preamble

    def _extract_title(self, headings: list[HeadingNode], source_path: Path) -> str:
        """Extract title from first H1, falling back to filename."""
        for h in headings:
            if h.level == 1:
                return h.text
        return source_path.stem


def _body(lines: list[str], start: int, stop: int, dropped: set[int]) -> str:
    """The text of ``lines[start:stop]`` without the lines in ``dropped``.

    A dropped line's following blank line goes with it when a blank line already
    precedes it, so removing a line set off by blank lines leaves one blank line
    between its neighbours rather than two.
    """
    kept: list[str] = []
    after_dropped = False
    for index in range(start, stop):
        line = lines[index]
        if index in dropped:
            after_dropped = not kept or not kept[-1].strip()
            continue
        if after_dropped and not line.strip():
            after_dropped = False
            continue
        after_dropped = False
        kept.append(line)
    return "\n".join(kept).strip()
