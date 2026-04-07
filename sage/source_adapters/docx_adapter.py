"""Docx source adapter: parses Word document styles, numbering, and tables.

Extracts heading hierarchy from paragraph styles, computes heading number
prefixes from Word numbering definitions, renders tables as pipe-delimited
text, and resolves cross-reference fields via cached display values.

Computes SHA-256 of raw .docx bytes for content_hash.
"""

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from lxml import etree

from sage.source_adapters.base import HeadingNode, ProjectionResult, SourceAdapter

# Word namespace
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

# Default Word heading styles mapped to heading levels
_DEFAULT_STYLE_MAP: dict[str, int] = {
    f"Heading {i}": i for i in range(1, 10)
}


class _NumberingEngine:
    """Computes rendered heading number prefixes from Word numbering definitions.

    Parses w:abstractNum definitions from the document's numbering part,
    tracks per-level counters, and formats composite prefixes using the
    lvlText template (e.g., "%1.%2" -> "1.2").
    """

    def __init__(self, numbering_part) -> None:
        # abstractNumId -> {ilvl -> {start, numFmt, lvlText}}
        self._abstract_defs: dict[int, dict[int, dict]] = {}
        # numId -> abstractNumId
        self._num_map: dict[int, int] = {}
        # (numId, ilvl) -> current counter value
        self._counters: dict[tuple[int, int], int] = {}

        if numbering_part is None:
            return

        numbering_elm = numbering_part.numbering_definitions._numbering

        for abstract in numbering_elm.findall(qn("w:abstractNum")):
            abs_id = int(abstract.get(qn("w:abstractNumId")))
            levels: dict[int, dict] = {}
            for lvl in abstract.findall(qn("w:lvl")):
                ilvl = int(lvl.get(qn("w:ilvl")))
                start_elem = lvl.find(qn("w:start"))
                fmt_elem = lvl.find(qn("w:numFmt"))
                text_elem = lvl.find(qn("w:lvlText"))
                levels[ilvl] = {
                    "start": int(start_elem.get(qn("w:val"))) if start_elem is not None else 1,
                    "numFmt": fmt_elem.get(qn("w:val")) if fmt_elem is not None else "decimal",
                    "lvlText": text_elem.get(qn("w:val")) if text_elem is not None else "",
                }
            self._abstract_defs[abs_id] = levels

        for num in numbering_elm.findall(qn("w:num")):
            num_id = int(num.get(qn("w:numId")))
            abs_ref = num.find(qn("w:abstractNumId"))
            if abs_ref is not None:
                self._num_map[num_id] = int(abs_ref.get(qn("w:val")))

    def next_number(self, num_id: int, ilvl: int) -> str:
        """Advance counter for (numId, ilvl) and return formatted prefix."""
        abs_id = self._num_map.get(num_id)
        if abs_id is None:
            return ""
        levels = self._abstract_defs.get(abs_id)
        if levels is None:
            return ""
        level_def = levels.get(ilvl)
        if level_def is None:
            return ""

        # Initialize counter if needed
        key = (num_id, ilvl)
        if key not in self._counters:
            self._counters[key] = level_def["start"]
        else:
            self._counters[key] += 1

        # Reset all child levels
        for child_ilvl in levels:
            if child_ilvl > ilvl:
                child_key = (num_id, child_ilvl)
                if child_key in self._counters:
                    del self._counters[child_key]

        # Build formatted text from lvlText template
        lvl_text = level_def["lvlText"]
        result = lvl_text
        for ref_ilvl in range(ilvl + 1):
            placeholder = f"%{ref_ilvl + 1}"
            if placeholder in result:
                ref_key = (num_id, ref_ilvl)
                counter_val = self._counters.get(ref_key, levels.get(ref_ilvl, {}).get("start", 1))
                ref_fmt = levels.get(ref_ilvl, {}).get("numFmt", "decimal")
                result = result.replace(placeholder, _format_number(counter_val, ref_fmt))

        return result


def _format_number(value: int, num_fmt: str) -> str:
    """Format a counter value according to Word's numFmt."""
    if num_fmt == "decimal":
        return str(value)
    elif num_fmt == "upperRoman":
        return _to_roman(value)
    elif num_fmt == "lowerRoman":
        return _to_roman(value).lower()
    elif num_fmt == "upperLetter":
        return _to_letter(value).upper()
    elif num_fmt == "lowerLetter":
        return _to_letter(value)
    else:
        return str(value)


def _to_roman(n: int) -> str:
    """Convert integer to Roman numeral string."""
    vals = [
        (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
        (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
        (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
    ]
    result = ""
    for val, numeral in vals:
        while n >= val:
            result += numeral
            n -= val
    return result


def _to_letter(n: int) -> str:
    """Convert integer to lowercase letter (1=a, 2=b, ..., 26=z, 27=aa)."""
    result = ""
    while n > 0:
        n -= 1
        result = chr(ord("a") + (n % 26)) + result
        n //= 26
    return result


def _extract_paragraph_text(p_elem) -> str:
    """Extract visible text from a paragraph element.

    Walks w:r/w:t elements and collects text. Skips w:instrText elements
    so that cross-reference field instructions are excluded while their
    cached display values (in w:t runs between separate/end markers) are
    included.

    Also handles w:fldSimple elements whose child runs contain display text.
    """
    parts: list[str] = []
    for child in p_elem:
        tag = etree.QName(child.tag).localname if isinstance(child.tag, str) else ""
        if tag == "r":
            # Regular run: collect w:t, skip w:instrText
            for run_child in child:
                rc_tag = etree.QName(run_child.tag).localname if isinstance(run_child.tag, str) else ""
                if rc_tag == "t" and run_child.text:
                    parts.append(run_child.text)
        elif tag == "fldSimple":
            # Simple field: collect text from child runs
            for r in child.findall(qn("w:r")):
                for t in r.findall(qn("w:t")):
                    if t.text:
                        parts.append(t.text)
    return "".join(parts)


class DocxAdapter(SourceAdapter):
    """Source adapter for Microsoft Word (.docx) documents."""

    VERSION = "0.1.0"

    async def project(
        self, source_path: Path, config: dict | None = None
    ) -> ProjectionResult:
        raw_bytes = source_path.read_bytes()
        content_hash = hashlib.sha256(raw_bytes).hexdigest()

        doc = Document(source_path)
        style_map = self._build_style_map(config)
        style_id_to_name = self._build_style_id_to_name(doc)

        # Initialize numbering engine
        numbering_part = None
        try:
            numbering_part = doc.part.numbering_part
        except Exception:
            pass
        engine = _NumberingEngine(numbering_part)

        headings: list[HeadingNode] = []
        text_parts: list[str] = []
        stack: list[tuple[int, str]] = []
        current_content_lines: list[str] = []
        current_heading_idx = -1

        # Walk document body children in order (paragraphs and tables)
        for element in doc.element.body:
            tag = etree.QName(element.tag).localname if isinstance(element.tag, str) else ""

            if tag == "p":
                para_text = _extract_paragraph_text(element)

                # Determine style name
                style_name = self._get_style_name(element, style_id_to_name)
                level = style_map.get(style_name) if style_name else None

                if level is not None:
                    # Flush content to previous heading
                    if current_heading_idx >= 0:
                        headings[current_heading_idx].content = "\n".join(
                            current_content_lines
                        ).strip()
                    current_content_lines = []

                    # Check for numbering
                    prefix = self._get_numbering_prefix(element, engine)
                    heading_text = f"{prefix} {para_text}" if prefix else para_text

                    # Update heading stack
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
                    text_parts.append(heading_text)
                else:
                    if para_text.strip():
                        current_content_lines.append(para_text)
                        text_parts.append(para_text)

            elif tag == "tbl":
                table_text = self._render_table(element)
                if table_text:
                    current_content_lines.append(table_text)
                    text_parts.append(table_text)

        # Flush final heading content
        if current_heading_idx >= 0:
            headings[current_heading_idx].content = "\n".join(
                current_content_lines
            ).strip()

        title = self._extract_title(headings, source_path)

        source_mtime = datetime.fromtimestamp(
            source_path.stat().st_mtime, tz=timezone.utc
        )

        return ProjectionResult(
            text="\n".join(text_parts),
            headings=headings,
            content_hash=content_hash,
            adapter_version=self.VERSION,
            title=title,
            metadata={"source_modified_at": source_mtime.isoformat()},
        )

    def _build_style_map(self, config: dict | None) -> dict[str, int]:
        """Build heading style map from config, merging with defaults."""
        style_map = dict(_DEFAULT_STYLE_MAP)
        if config and "heading_style_map" in config:
            style_map.update(config["heading_style_map"])
        return style_map

    def _build_style_id_to_name(self, doc: Document) -> dict[str, str]:
        """Build a mapping from style_id (XML) to style name (human)."""
        mapping: dict[str, str] = {}
        for style in doc.styles:
            if style.style_id and style.name:
                mapping[style.style_id] = style.name
        return mapping

    def _get_style_name(self, p_elem, style_id_to_name: dict[str, str]) -> str | None:
        """Get the style name for a paragraph element."""
        pPr = p_elem.find(qn("w:pPr"))
        if pPr is None:
            return None
        pStyle = pPr.find(qn("w:pStyle"))
        if pStyle is None:
            return None
        style_id = pStyle.get(qn("w:val"))
        if style_id is None:
            return None
        return style_id_to_name.get(style_id, style_id)

    def _get_numbering_prefix(self, p_elem, engine: _NumberingEngine) -> str:
        """Extract numbering prefix for a paragraph, if any."""
        pPr = p_elem.find(qn("w:pPr"))
        if pPr is None:
            return ""
        numPr = pPr.find(qn("w:numPr"))
        if numPr is None:
            return ""
        ilvl_elem = numPr.find(qn("w:ilvl"))
        numId_elem = numPr.find(qn("w:numId"))
        if ilvl_elem is None or numId_elem is None:
            return ""
        ilvl = int(ilvl_elem.get(qn("w:val")))
        num_id = int(numId_elem.get(qn("w:val")))
        if num_id == 0:
            return ""
        return engine.next_number(num_id, ilvl)

    def _render_table(self, tbl_elem) -> str:
        """Render a table element as pipe-delimited text rows."""
        rows: list[str] = []
        for tr in tbl_elem.findall(qn("w:tr")):
            cells: list[str] = []
            for tc in tr.findall(qn("w:tc")):
                # Collect text from all paragraphs in the cell
                cell_parts: list[str] = []
                for p in tc.findall(qn("w:p")):
                    cell_parts.append(_extract_paragraph_text(p))
                cells.append(" ".join(cell_parts).strip())
            rows.append("| " + " | ".join(cells) + " |")
        return "\n".join(rows)

    def _extract_title(
        self, headings: list[HeadingNode], source_path: Path
    ) -> str:
        """Extract title from first level-1 heading, fallback to filename."""
        for h in headings:
            if h.level == 1:
                return h.text
        return source_path.stem
