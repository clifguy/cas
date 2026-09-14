"""Markdown source adapter: parses # headings and extracts structure.

Computes SHA-256 of source file bytes for content_hash.
"""

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.rules_block import StateBlock
from markdown_it.token import Token
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
    # 0.8.0: reads a document in its dialect, GFM or Pandoc Markdown, declared
    # through the ``dialect`` config key or detected; a Pandoc table is not
    # read as headings.
    VERSION = "0.8.0"
    EXTENSIONS = [".md", ".markdown"]

    async def project(self, source_path: Path, config: dict | None = None) -> ProjectionResult:
        raw_bytes = source_path.read_bytes()
        content_hash = hashlib.sha256(raw_bytes).hexdigest()
        text = raw_bytes.decode("utf-8")

        headings, preamble = self._parse_headings(text, self._tokens(text, config))
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

    def _tokens(self, text: str, config: dict | None) -> list[Token]:
        """The block token stream of ``text``, read in its dialect.

        The dialect is ``config["dialect"]`` when declared, and otherwise detected
        (see ``_detect_dialect``). GFM is CommonMark with its tables; Pandoc
        Markdown additionally has tables ruled with dash lines and a title block,
        which CommonMark would read as thematic breaks, setext headings and
        paragraphs. A dialect that is neither is refused rather than detected
        over, so a misspelling cannot pass unnoticed.
        """
        dialect = (config or {}).get("dialect")
        if dialect is not None and dialect not in DIALECTS:
            raise ValueError(
                f"unrecognized markdown dialect {dialect!r}; expected one of "
                + ", ".join(repr(name) for name in DIALECTS)
            )
        tokens = _gfm_parser().parse(text)
        if dialect is None:
            dialect = _detect_dialect(text, tokens)
        if dialect == "pandoc":
            tokens = _pandoc_parser().parse(text)
        return tokens

    def _parse_headings(self, text: str, tokens: list[Token]) -> tuple[list[HeadingNode], str]:
        """Extract headings, and the text before the first, from a block token stream.

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
        A document whose every heading is untitled has no headings, so its text is
        its one passage exactly as written, the untitled heading lines included.
        """
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


# The dialects a document can be read in.
DIALECTS = ("gfm", "pandoc")

# Pandoc table rules: a full-width dash line, and a column rule of dash runs
# separated by spaces. A column rule is never a setext underline and seldom a
# thematic break an author writes beside a table row, so it marks a Pandoc table.
_FULL_RULE = re.compile(r"-{3,}[ \t]*$")
_COLUMN_RULE = re.compile(r"-{2,}(?: +-{2,})+[ \t]*$")
_GRID_BORDER = re.compile(r"\+(?:[-=:]+\+)+[ \t]*$")
_ATX_HEADING = re.compile(r"#{1,6}(?:[ \t]|$)")
_BLOCKQUOTE_MARKERS = re.compile(r"(?: {0,3}> ?)+")
# A line that cannot be a table row or header: a heading, or a rule or setext
# underline, which pandoc reads before any table.
_NOT_A_TABLE_ROW = re.compile(r"#{1,6}(?:[ \t]|$)|[-=*_ \t]+$")

# Blocks whose lines are not markdown structure, so hold no dialect marker.
_OPAQUE_BLOCKS = frozenset({"fence", "code_block", "front_matter", "html_block"})


def _gfm_parser() -> MarkdownIt:
    """CommonMark with GFM tables, recognizing front matter."""
    return MarkdownIt("commonmark").enable("table").use(front_matter_plugin)


def _pandoc_parser() -> MarkdownIt:
    """The GFM reader with Pandoc's dash-ruled and grid tables and title block."""
    md = _gfm_parser()
    md.block.ruler.before("table", "pandoc_block", _pandoc_block)
    return md


def _detect_dialect(text: str, tokens: list[Token]) -> str:
    """``pandoc`` when ``text`` carries a marker only Pandoc Markdown writes, else ``gfm``.

    The markers are a column rule beside a table row, a grid-table border
    followed by a row, and a ``%`` title block opening the document. A marker
    nested in a container block -- a blockquote, a list item -- counts, whatever
    its indentation; none of them counts inside code, front matter or raw HTML,
    which are the parse's own blocks rather than a guess from indentation. A
    table ruled only by full-width dash lines is not a marker: to CommonMark it
    is a thematic break and a setext heading, which an author writing CommonMark
    means.
    """
    lines = [
        _BLOCKQUOTE_MARKERS.sub("", line, count=1) if line.lstrip(" ").startswith(">") else line
        for line in text.split("\n")
    ]
    opaque: set[int] = set()
    for token in tokens:
        if token.type in _OPAQUE_BLOCKS and token.map is not None:
            opaque.update(range(*token.map))

    def text_at(index: int) -> bool:
        return 0 <= index < len(lines) and index not in opaque and bool(lines[index].strip())

    def row_at(index: int) -> bool:
        return text_at(index) and not _NOT_A_TABLE_ROW.match(lines[index].lstrip(" \t"))

    if lines[0].startswith("%") and 0 not in opaque:
        return "pandoc"
    for index, line in enumerate(lines):
        stripped = line.lstrip(" \t")
        if index in opaque:
            continue
        if _COLUMN_RULE.match(stripped) and (row_at(index - 1) or row_at(index + 1)):
            return "pandoc"
        if _GRID_BORDER.match(stripped) and text_at(index + 1):
            if lines[index + 1].lstrip(" \t").startswith("|"):
                return "pandoc"
    return "gfm"


def _pandoc_block(state: StateBlock, start: int, end: int, silent: bool) -> bool:
    """Consume a Pandoc table or title block as one block that holds no heading.

    Recognized at the start of a block:

    - a ``%`` title block on the document's first line, through the next blank
      line;
    - a grid table, a border followed by a row, through the next blank line;
    - a table opening with a dash rule followed by a row -- a multiline table,
      or a simple table without a header -- through the dash rule followed by a
      blank line that closes it, or, failing one before a heading, through the
      next blank line when a dash rule precedes it;
    - a simple table with a header, a line other than a heading followed by a
      column rule, through the next blank line; pandoc reads a heading before a
      table, so a heading line is never a table header.
    """
    if state.sCount[start] - state.blkIndent >= 4:
        return False

    def line(index: int) -> str:
        return state.src[state.bMarks[index] + state.tShift[index] : state.eMarks[index]]

    def rule(index: int) -> bool:
        return bool(_FULL_RULE.match(line(index)) or _COLUMN_RULE.match(line(index)))

    def through_blank(index: int) -> int:
        while index + 1 < end and not state.isEmpty(index + 1):
            index += 1
        return index

    first = line(start)
    last: int | None = None
    if start == 0 and first.startswith("%"):
        last = through_blank(start)
    elif start + 1 < end and not state.isEmpty(start + 1):
        if _GRID_BORDER.match(first) and line(start + 1).startswith("|"):
            last = through_blank(start)
        elif rule(start):
            for index in range(start + 2, end):
                if _ATX_HEADING.match(line(index)):
                    break
                if rule(index) and (index + 1 >= end or state.isEmpty(index + 1)):
                    last = index
                    break
            if last is None:
                block = through_blank(start)
                if any(rule(index) for index in range(start + 2, block + 1)):
                    last = block
        elif _COLUMN_RULE.match(line(start + 1)) and not _ATX_HEADING.match(first):
            last = through_blank(start)
    if last is None:
        return False
    if silent:
        return True
    token = state.push("pandoc_block", "", 0)
    token.map = [start, last + 1]
    state.line = last + 1
    return True


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
