"""XLSX source adapter: extracts structural digest from Excel workbooks.

Produces a lightweight projection with sheet names as headings, column
headers, dimensions, and configurable preview rows rendered as
pipe-delimited tables. Designed for discovery, not full-content search;
agents retrieve the source .xlsx file for programmatic access.

Computes SHA-256 of raw .xlsx bytes for content_hash.
"""

import hashlib
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from sage.source_adapters.base import (
    HeadingNode,
    ProjectionResult,
    SourceAdapter,
    SourceReadError,
    check_zip_package,
    optional_positive_int,
    positive_int,
)

# Default sheet names that signal "the user never renamed it"
_DEFAULT_SHEET_NAMES = {"Sheet", "Sheet1"}

_DEFAULT_PREVIEW_ROWS = 5

# Fixed bounds on reading a sheet. A sheet's declared dimension is not trusted
# to size its rows: rows are read lazily, at most this many columns wide, and
# counted up to a ceiling past which the count is reported as a lower bound.
_MAX_COLUMNS = 256
_MAX_ROWS_SCANNED = 100_000


def _scan_sheet(ws, keep: int) -> tuple[list[tuple], int, int, bool]:
    """Read a read-only sheet lazily within the fixed bounds.

    Returns the first ``keep`` rows, the number of rows up to the last one
    holding a value, the number of columns up to the last one holding a value,
    and whether the scan stopped at the row ceiling. Both bounds are passed to
    the reader explicitly, so neither a row's width nor the number of rows read
    comes from the sheet's declared dimension.
    """
    kept: list[tuple] = []
    num_rows = 0
    num_cols = 0
    scanned = 0
    # The reader yields one shared tuple for every row absent from the sheet;
    # recognizing it by identity keeps a long gap from costing a scan per row.
    gap: tuple | None = None
    for scanned, row in enumerate(
        ws.iter_rows(max_row=_MAX_ROWS_SCANNED, max_col=_MAX_COLUMNS, values_only=True),
        start=1,
    ):
        if len(kept) < keep:
            kept.append(row)
        if row is gap:
            continue
        last = next((i for i in range(len(row) - 1, -1, -1) if row[i] is not None), -1)
        if last < 0:
            gap = row
        if last >= 0:
            num_rows = scanned
            num_cols = max(num_cols, last + 1)
    return kept[:num_rows], num_rows, num_cols, scanned >= _MAX_ROWS_SCANNED


class XlsxAdapter(SourceAdapter):
    # 0.2.0: chunks indexed with heading-context (heading_path embedded with
    # content, plus FTS index on heading_path).
    # 0.3.0: chunker emits one chunk per heading regardless of body content
    # (Word-Find equivalence for empty-content parents).
    VERSION = "0.3.0"
    EXTENSIONS = [".xlsx"]

    def check_config(self, config: dict | None) -> None:
        positive_int(config, "preview_rows", _DEFAULT_PREVIEW_ROWS)
        optional_positive_int(config, "max_sheets")

    async def project(self, source_path: Path, config: dict | None = None) -> ProjectionResult:
        preview_rows = positive_int(config, "preview_rows", _DEFAULT_PREVIEW_ROWS)
        max_sheets = optional_positive_int(config, "max_sheets")

        raw_bytes = source_path.read_bytes()
        content_hash = hashlib.sha256(raw_bytes).hexdigest()

        check_zip_package(source_path)
        try:
            wb = load_workbook(source_path, read_only=True, data_only=True)
        except (zipfile.BadZipFile, KeyError, InvalidFileException) as exc:
            raise SourceReadError(f"Failed to open workbook {source_path}: {exc}") from exc
        try:
            sheet_names_all = wb.sheetnames
            sheets_to_process = sheet_names_all[:max_sheets] if max_sheets else sheet_names_all

            headings: list[HeadingNode] = []
            text_parts: list[str] = []
            dimensions_meta: dict[str, dict[str, int]] = {}

            for sheet_name in sheets_to_process:
                ws = wb[sheet_name]
                rows, num_rows, num_cols, truncated = _scan_sheet(ws, 1 + preview_rows)

                dimensions_meta[sheet_name] = {
                    "rows": num_rows,
                    "columns": num_cols,
                }

                content_lines: list[str] = []
                row_count = f"at least {num_rows}" if truncated else f"{num_rows}"
                content_lines.append(f"{row_count} rows x {num_cols} columns")
                content_lines.append("")

                if rows:
                    # First row as header
                    header_row = rows[0][:num_cols]
                    header_cells = [str(c) if c is not None else "" for c in header_row]
                    content_lines.append("| " + " | ".join(header_cells) + " |")
                    content_lines.append("| " + " | ".join("---" for _ in header_cells) + " |")

                    # Preview data rows
                    data_rows = rows[1 : 1 + preview_rows]
                    for row in data_rows:
                        cells = [str(c) if c is not None else "" for c in row[:num_cols]]
                        content_lines.append("| " + " | ".join(cells) + " |")

                    omitted = num_rows - 1 - len(data_rows)
                    if omitted > 0:
                        content_lines.append(f"... ({omitted} more data rows)")

                content = "\n".join(content_lines)

                headings.append(
                    HeadingNode(
                        level=1,
                        text=sheet_name,
                        path=sheet_name,
                        content=content,
                    )
                )

                text_parts.append(f"# {sheet_name}\n\n{content}")

            full_text = "\n\n".join(text_parts)

            # Title: first sheet name, unless it's a default name
            first_sheet = sheet_names_all[0] if sheet_names_all else None
            if first_sheet and first_sheet not in _DEFAULT_SHEET_NAMES:
                title = first_sheet
            else:
                title = source_path.stem

            source_mtime = datetime.fromtimestamp(source_path.stat().st_mtime, tz=timezone.utc)

            metadata = {
                "source_modified_at": source_mtime.isoformat(),
                "sheet_names": list(sheet_names_all),
                "total_sheets": len(sheet_names_all),
                "dimensions": dimensions_meta,
            }

        finally:
            wb.close()

        return ProjectionResult(
            text=full_text,
            headings=headings,
            content_hash=content_hash,
            adapter_version=self.VERSION,
            title=title,
            metadata=metadata,
        )
