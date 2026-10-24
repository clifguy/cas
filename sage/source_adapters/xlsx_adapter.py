"""Full sparse authored-cell XLSX projection, independent of inference budgets.

Retained bytes own visual/non-cell objects. No formulas are evaluated, external
links followed or macros run. preview_rows is accepted but never truncates the
canonical projection; max_sheets is an explicit limited-coverage request.
"""

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from sage.source_adapters.base import (
    HeadingNode,
    ProjectedPassage,
    ProjectionResult,
    SourceAdapter,
    SourceReadError,
    discovery_configuration,
    optional_positive_int,
    positive_int,
)
from sage.source_adapters.structure import check_projection_work
from sage.source_adapters.xlsx_cells import cells

_DEFAULT_SHEET_NAMES = {"Sheet", "Sheet1"}
_DEFAULT_PREVIEW_ROWS = 5
# Also bounds expansion of a tiny shared-string table referenced by many cells.
# This is canonical output admission, independent of inference allowances.
_MAX_PROJECTION_CHARACTERS = 16 * 1024 * 1024


class XlsxAdapter(SourceAdapter):
    NATIVE_FACTS = True
    VERSION = "0.4.0"
    EXTENSIONS = [".xlsx"]

    def check_config(self, config: dict | None) -> None:
        super().check_config(config)
        positive_int(config, "preview_rows", _DEFAULT_PREVIEW_ROWS)
        optional_positive_int(config, "max_sheets")

    def effective_config(self, config: dict | None) -> dict:
        return {
            **super().effective_config(config),
            "preview_rows": positive_int(config, "preview_rows", _DEFAULT_PREVIEW_ROWS),
            "max_sheets": optional_positive_int(config, "max_sheets"),
        }

    async def project(self, source_path: Path, config: dict | None = None) -> ProjectionResult:
        self.check_config(config)
        max_sheets = optional_positive_int(config, "max_sheets")
        check_projection_work(source_path, discovery_configuration(config)[1])
        output_characters = 0

        def admit_output(characters: int) -> None:
            nonlocal output_characters
            if characters > _MAX_PROJECTION_CHARACTERS - output_characters:
                raise SourceReadError(
                    f"Workbook {source_path} exceeds canonical projection character limit"
                )
            output_characters += characters

        metadata: dict = {}
        by_sheet: dict[str, list] = {}
        for cell in cells(source_path, metadata):
            # Admit the expanded authored value before building any repeated
            # display strings, including shared-string aliases and cache text.
            admit_output(
                len(cell.authored)
                + len(cell.cache or "")
                + len(cell.address)
                + len(cell.kind)
                + len(str(dict(cell.formula_attributes)))
                + 64
            )
            by_sheet.setdefault(cell.sheet, []).append(cell)
        names = metadata["sheet_names"]
        selected = names[:max_sheets] if max_sheets else names
        headings: list[HeadingNode] = []
        passages: list[ProjectedPassage] = []
        caches: list[dict] = []
        text_parts: list[str] = []
        dimensions = {}
        for section, sheet in enumerate(selected):
            first_passage = len(passages)
            sheet_cells = by_sheet.get(sheet, [])
            dimensions[sheet] = {
                "rows": max((c.row for c in sheet_cells), default=0),
                "columns": max((c.column for c in sheet_cells), default=0),
            }
            rendered = []
            orientation = (
                f"# {sheet}\n\n"
                f"[sheet state: {metadata['sheet_states'][sheet]}; "
                f"epoch: {metadata['epoch']}; "
                f"merged ranges: {metadata['merges'][sheet]}; "
                f"hidden rows: {metadata['hidden_rows'][sheet]}; "
                f"hidden columns: {metadata['hidden_columns'][sheet]}]\n"
            )
            admit_output(len(orientation) + 2)
            for cell in sheet_cells:
                # Text stays verbatim, including leading/trailing and multiline
                # whitespace. Orientation is separated from authored evidence.
                display = f"{cell.address} [{cell.kind}]\n{cell.authored}\n"
                if cell.formula_attributes:
                    display += f"[formula attributes: {dict(cell.formula_attributes)}]\n"
                if cell.cache is not None:
                    display += f"[unverified formula cache: {cell.cache}]\n"
                    caches.append(
                        {
                            "sheet": sheet,
                            "address": cell.address,
                            "value": cell.cache,
                            "verified": False,
                        }
                    )
                rendered.append(display)
                prefix = orientation if len(rendered) == 1 else ""
                passages.append(ProjectedPassage(sheet, prefix + display, cell.authored, section))
            if not rendered:
                passages.append(ProjectedPassage(sheet, f"# {sheet}\n\n", "", section))
            headings.append(HeadingNode(1, sheet, sheet, "".join(rendered)))
            text_parts.append("".join(p.content for p in passages[first_passage:]))
        limited = len(selected) < len(names)
        metadata.update(
            {
                "source_modified_at": datetime.fromtimestamp(
                    source_path.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
                "total_sheets": len(names),
                "dimensions": dimensions,
                "formula_caches": caches,
            }
        )
        first = names[0] if names else None
        return ProjectionResult(
            text="\n\n".join(text_parts),
            headings=headings,
            content_hash=hashlib.sha256(source_path.read_bytes()).hexdigest(),
            adapter_version=self.VERSION,
            title=first if first and first not in _DEFAULT_SHEET_NAMES else source_path.stem,
            metadata=metadata,
            projection_complete=not limited,
            projection_limitations=("max_sheets",) if limited else (),
            passages=passages,
        )
