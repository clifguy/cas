"""PDF source adapter.

Extracts text from PDFs that carry a real text layer via `pdfplumber`.
Reads the document outline and `/Info` dictionary via `pypdf` (its API
for those is more direct than pdfplumber's).

PDFs with no usable text layer (image-only "scanned" PDFs) are
detected via the scanned-text threshold and run through an inline
`ocrmypdf` OCR pre-pass; the OCR'd output is then re-extracted on the
same code path as a native-text PDF. The OCR dependency lives in the
optional `[ocr]` extra (`pip install -e ".[ocr]"`) and requires the
`tesseract` and `ghostscript` system binaries.

Computes SHA-256 of raw PDF bytes for content_hash (the source PDF is
the document's identity, not the OCR derivative).
"""

import contextlib
import hashlib
import io
import logging
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pdfplumber
import pypdf

from sage.source_adapters.base import HeadingNode, ProjectionResult, SourceAdapter

_DEFAULT_MAX_PAGES = 1000
_OUTLINE_MAX_DEPTH = 10
_TITLE_MAX_LINE_CHARS = 120


_CID_PATTERN = re.compile(r"\(cid:(\d+)\)")


def _decode_safe_cid(text: str) -> str:
    """Decode ``(cid:N)`` sequences to ``chr(N)`` for printable ASCII (32-126).

    pdfminer.six emits CID glyph indices for Type 1 fonts (Helvetica)
    without a ``/ToUnicode`` CMap when ``ocrmypdf`` has been imported in
    the same process (a global side-effect on pdfminer's font-resolution
    path; see T-0104). For the Helvetica WinAnsiEncoding subset, CID == ASCII
    code, so we can recover Unicode for printable codes. Non-printable
    and >127 CIDs survive intact rather than guess at the right codepoint.
    """

    def _replace(match: re.Match[str]) -> str:
        n = int(match.group(1))
        if 32 <= n <= 126:
            return chr(n)
        return match.group(0)

    return _CID_PATTERN.sub(_replace, text)


@contextlib.contextmanager
def _suppress_pdf_noise():
    """Suppress stderr + pdfminer/pypdf logger noise during PDF parsing.

    Real-world PDFs frequently emit "Ignoring wrong pointing object N M
    (offset 0)" warnings from pypdf and CropBox/MediaBox warnings from
    pdfminer that are non-actionable for content extraction. We scope
    suppression to the parsing call so other tooling remains verbose.
    """
    pdfminer_logger = logging.getLogger("pdfminer")
    pypdf_logger = logging.getLogger("pypdf")
    original_pdfminer_level = pdfminer_logger.level
    original_pypdf_level = pypdf_logger.level
    pdfminer_logger.setLevel(logging.ERROR)
    pypdf_logger.setLevel(logging.ERROR)

    saved_stderr = sys.stderr
    sys.stderr = io.StringIO()
    try:
        yield
    finally:
        sys.stderr = saved_stderr
        pdfminer_logger.setLevel(original_pdfminer_level)
        pypdf_logger.setLevel(original_pypdf_level)


def _flatten_outline(
    outline: list, reader: pypdf.PdfReader, max_depth: int
) -> list[tuple[int, str, int]]:
    """Flatten pypdf's nested outline into ordered (level, text, page_index) tuples.

    pypdf exposes the outline as a nested list: a Destination object is an
    entry, and a list immediately following an entry contains its children.
    Entries deeper than max_depth are dropped (their underlying page text
    remains accessible via the nearest ancestor's page range, by
    construction).
    """
    entries: list[tuple[int, str, int]] = []

    def _walk(items: list, level: int) -> None:
        for item in items:
            if isinstance(item, list):
                _walk(item, level + 1)
                continue
            if level > max_depth:
                continue
            title = getattr(item, "title", None) or str(item)
            try:
                page_idx = reader.get_destination_page_number(item)
            except Exception:
                page_idx = None
            if page_idx is None:
                continue
            entries.append((level, str(title).strip(), int(page_idx)))

    _walk(outline, 1)
    return entries


def _build_outline_headings(
    entries: list[tuple[int, str, int]],
    page_texts: list[str],
    pages_extracted: int,
) -> list[HeadingNode]:
    """Construct HeadingNodes from outline entries with page-range content.

    Each entry's content spans from its start_page to the start_page of
    the next entry in document order (or pages_extracted, whichever
    comes first). Entries whose start_page is beyond pages_extracted are
    dropped.
    """
    in_range = [(lvl, text, sp) for (lvl, text, sp) in entries if sp < pages_extracted]
    headings: list[HeadingNode] = []
    ancestor_stack: list[tuple[int, str]] = []

    for i, (level, text, start_page) in enumerate(in_range):
        while ancestor_stack and ancestor_stack[-1][0] >= level:
            ancestor_stack.pop()
        path_parts = [a[1] for a in ancestor_stack] + [text]
        path = " > ".join(path_parts)

        if i + 1 < len(in_range):
            end_page = min(in_range[i + 1][2], pages_extracted)
        else:
            end_page = pages_extracted

        content = "\n\n".join(
            page_texts[p].strip()
            for p in range(start_page, end_page)
            if 0 <= p < len(page_texts) and page_texts[p].strip()
        )
        headings.append(HeadingNode(level=level, text=text, path=path, content=content))
        ancestor_stack.append((level, text))

    return headings


def _resolve_title(
    info_title: str | None,
    outline_entries: list[tuple[int, str, int]],
    page_texts: list[str],
    source_path: Path,
) -> str:
    """Resolve title via 4-step priority chain.

    Priority: /Info /Title -> first outline entry -> first body line
    <=120 chars -> filename stem.
    """
    if info_title:
        return info_title
    if outline_entries:
        return outline_entries[0][1]
    for page_text in page_texts:
        if not page_text:
            continue
        for line in page_text.split("\n"):
            stripped = line.strip()
            if stripped and len(stripped) <= _TITLE_MAX_LINE_CHARS:
                return stripped
    return source_path.stem


def _extract_info_title(reader: pypdf.PdfReader) -> str | None:
    """Read /Info /Title if present and non-empty."""
    try:
        info = reader.metadata
        if info is None:
            return None
        title = info.get("/Title")
        if title is None:
            return None
        title_str = str(title).strip()
        return title_str or None
    except Exception:
        return None


def _extract_from_path(
    path: Path, max_pages: int
) -> tuple[list[str], list[tuple[int, str, int]], str | None, int, int]:
    """Run the pypdf + pdfplumber extraction pipeline against a PDF on disk.

    Returns (page_texts, outline_entries, info_title, actual_page_count,
    pages_extracted). Shared by the native-text path and the post-OCR
    re-extraction path.
    """
    with _suppress_pdf_noise():
        try:
            reader = pypdf.PdfReader(str(path), strict=False)
        except Exception as e:
            raise ValueError(f"Failed to open PDF {path}: {e}") from e

        if reader.is_encrypted:
            raise ValueError(f"PDF is encrypted and cannot be projected: {path}")

        try:
            actual_page_count = len(reader.pages)
        except Exception as e:
            raise ValueError(f"Failed to read pages from PDF {path}: {e}") from e

        info_title = _extract_info_title(reader)

        try:
            raw_outline = reader.outline
        except Exception:
            raw_outline = []
        outline_entries = _flatten_outline(raw_outline or [], reader, _OUTLINE_MAX_DEPTH)

        pages_extracted = min(actual_page_count, max_pages)

        page_texts: list[str] = []
        if pages_extracted > 0:
            try:
                with pdfplumber.open(str(path)) as pdf:
                    for i in range(pages_extracted):
                        try:
                            pt = pdf.pages[i].extract_text() or ""
                        except Exception:
                            pt = ""
                        page_texts.append(_decode_safe_cid(pt))
            except Exception as e:
                raise ValueError(f"Failed to extract text from PDF {path}: {e}") from e

    return page_texts, outline_entries, info_title, actual_page_count, pages_extracted


def _ocr_to_tempfile(source_path: Path) -> Path:
    """OCR ``source_path`` to a new tempfile and return the tempfile path.

    Lazy-imports ``ocrmypdf``. ImportError surfaces as a ValueError that
    names the [ocr] extra and the required Homebrew binaries. Any
    ocrmypdf runtime error unlinks the tempfile and re-raises as
    ValueError. The caller is responsible for unlinking the returned
    tempfile on success.
    """
    try:
        import ocrmypdf
    except ImportError as e:
        raise ValueError(
            f"Scanned PDF detected at {source_path}; OCR requires the [ocr] extra "
            f'(pip install -e ".[ocr]") and the tesseract + ghostscript system '
            f"binaries (brew install tesseract ghostscript)."
        ) from e

    out = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    out.close()
    try:
        ocrmypdf.ocr(
            str(source_path),
            out.name,
            language="eng",
            progress_bar=False,
            quiet=True,
        )
    except Exception as e:
        Path(out.name).unlink(missing_ok=True)
        raise ValueError(f"OCR failed for {source_path}: {e}") from e
    return Path(out.name)


class PdfAdapter(SourceAdapter):
    # 0.2.0: chunks indexed with heading-context (heading_path embedded with
    # content, plus FTS index on heading_path).
    # 0.3.0: chunker emits one chunk per heading regardless of body content
    # (Word-Find equivalence for empty-content parents).
    # 0.4.0: scanned PDFs run through inline ocrmypdf pre-pass; tag set
    # changes (pdf:scanned removed; pdf:ocr_applied / pdf:ocr_no_text added).
    # 0.5.0: post-extraction CID safe-decode for printable-ASCII glyphs
    # (T-0104; mitigates pdfminer.six side-effect after ocrmypdf import).
    VERSION = "0.5.0"
    EXTENSIONS = [".pdf"]

    async def project(self, source_path: Path, config: dict | None = None) -> ProjectionResult:
        config = config or {}
        max_pages = config.get("max_pages", _DEFAULT_MAX_PAGES)

        raw_bytes = source_path.read_bytes()
        content_hash = hashlib.sha256(raw_bytes).hexdigest()
        source_mtime = datetime.fromtimestamp(source_path.stat().st_mtime, tz=timezone.utc)

        (
            page_texts,
            outline_entries,
            info_title,
            actual_page_count,
            pages_extracted,
        ) = _extract_from_path(source_path, max_pages)

        truncated = pages_extracted < actual_page_count
        total_chars = sum(len(p.strip()) for p in page_texts)
        is_scanned = pages_extracted > 0 and total_chars == 0

        adapter_tags: list[str] = []
        full_text: str
        headings: list[HeadingNode]

        if is_scanned:
            ocr_path = _ocr_to_tempfile(source_path)
            try:
                (
                    page_texts,
                    outline_entries,
                    info_title,
                    _ocr_page_count,
                    _ocr_pages_extracted,
                ) = _extract_from_path(ocr_path, max_pages)
            finally:
                ocr_path.unlink(missing_ok=True)

            total_chars = sum(len(p.strip()) for p in page_texts)
            if total_chars > 0:
                adapter_tags.append("pdf:ocr_applied")
                full_text = "\n\n".join(p.strip() for p in page_texts if p.strip())
                if outline_entries:
                    headings = _build_outline_headings(outline_entries, page_texts, pages_extracted)
                    adapter_tags.append("pdf:has_outline")
                else:
                    title_for_heading = _resolve_title(
                        info_title, outline_entries, page_texts, source_path
                    )
                    headings = [
                        HeadingNode(
                            level=1,
                            text=title_for_heading,
                            path=title_for_heading,
                            content=full_text,
                        )
                    ]
            else:
                adapter_tags.append("pdf:ocr_no_text")
                full_text = ""
                headings = []
        elif pages_extracted == 0:
            full_text = ""
            headings = []
        else:
            full_text = "\n\n".join(p.strip() for p in page_texts if p.strip())
            if outline_entries:
                headings = _build_outline_headings(outline_entries, page_texts, pages_extracted)
                adapter_tags.append("pdf:has_outline")
            else:
                title_for_heading = _resolve_title(
                    info_title, outline_entries, page_texts, source_path
                )
                headings = [
                    HeadingNode(
                        level=1,
                        text=title_for_heading,
                        path=title_for_heading,
                        content=full_text,
                    )
                ]

        if truncated:
            adapter_tags.append("pdf:truncated")

        title = _resolve_title(info_title, outline_entries, page_texts, source_path)

        metadata: dict = {
            "source_modified_at": source_mtime.isoformat(),
            "page_count": actual_page_count,
            "pages_extracted": pages_extracted,
            "has_outline": bool(outline_entries),
        }
        if adapter_tags:
            metadata["adapter_tags"] = adapter_tags
            metadata["adapter_tag_prefixes"] = ["pdf:"]

        return ProjectionResult(
            text=full_text,
            headings=headings,
            content_hash=content_hash,
            adapter_version=self.VERSION,
            title=title,
            metadata=metadata,
        )
