"""Sparse, guarded OOXML cells shared by projection and native discovery.

Dimensions describe an envelope, never an iteration bound. The pinned openpyxl
cell decoder handles shared strings, shared formula translation and Excel epochs;
raw numeric lexical tokens are retained alongside decoded date/time values.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile

from defusedxml.ElementTree import iterparse
from openpyxl.utils import get_column_letter
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.worksheet._reader import WorkSheetParser

from sage.source_adapters.base import SourceReadError

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


@dataclass(frozen=True)
class WorkbookCell:
    sheet: str
    address: str
    row: int
    column: int
    kind: str
    value: object
    authored: str
    cache: str | None
    formula_attributes: tuple[tuple[str, str], ...] = ()


def cells(path: Path, metadata: dict | None = None) -> Iterator[WorkbookCell]:
    """Decode stored cells only, never evaluating formulas or following links.

    Caller must run check_projection_work first. Keeping that admission outside
    the iterator ensures native facts and canonical projection share one guard.
    Explicit blank cells survive; absent cells and merged placeholders do not
    become authored values.
    """
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(path, read_only=True, data_only=False, keep_links=False)
    except (BadZipFile, InvalidFileException, KeyError, ValueError) as exc:
        raise SourceReadError(f"Cannot open workbook {path}: {exc}") from exc
    if metadata is not None:
        metadata.update(
            sheet_names=workbook.sheetnames,
            sheet_states={},
            merges={},
            hidden_rows={},
            hidden_columns={},
            epoch=workbook.epoch.isoformat(),
        )
    try:
        for sheet in workbook:
            if metadata is not None:
                metadata["sheet_states"][sheet.title] = sheet.sheet_state
                for key in ("merges", "hidden_rows", "hidden_columns"):
                    metadata[key][sheet.title] = []
            decoder = WorkSheetParser(
                None,
                sheet._shared_strings,
                data_only=False,
                epoch=workbook.epoch,
                date_formats=workbook._date_formats,
                timedelta_formats=workbook._timedelta_formats,
            )
            with sheet._get_source() as source:
                for _, element in iterparse(source, events=("end",), forbid_dtd=True):
                    if metadata is not None:
                        if element.tag == _NS + "mergeCell":
                            metadata["merges"][sheet.title].append(element.get("ref"))
                        elif element.tag == _NS + "col" and element.get("hidden") == "1":
                            metadata["hidden_columns"][sheet.title].append(
                                (element.get("min"), element.get("max"))
                            )
                        elif element.tag == _NS + "row" and element.get("hidden") == "1":
                            metadata["hidden_rows"][sheet.title].append(element.get("r"))
                    if element.tag != _NS + "row":
                        continue
                    decoder.row_counter = int(element.get("r", decoder.row_counter + 1))
                    decoder.col_counter = 0
                    for node in element:
                        if node.tag != _NS + "c":
                            continue
                        raw = node.findtext(_NS + "v")
                        formula = node.find(_NS + "f")
                        decoded = decoder.parse_cell(node)
                        value = decoded["value"]
                        kind = decoded["data_type"]
                        if formula is not None:
                            # Array/data-table formula objects are not their repr:
                            # expose the authored expression and range from OOXML.
                            value = (
                                value
                                if isinstance(value, str)
                                else "=" + formula.text
                                if formula.text is not None
                                else ""
                            )
                            kind, authored = "formula", str(value)
                        elif kind == "d":
                            kind = "date/time" if hasattr(value, "isoformat") else "duration"
                            authored = (
                                value.isoformat() if hasattr(value, "isoformat") else str(value)
                            )
                        elif value is None:
                            kind = (
                                "string" if node.get("t") in {"s", "str", "inlineStr"} else "blank"
                            )
                            authored = ""
                        elif kind == "b":
                            kind, authored = "boolean", "true" if value else "false"
                        elif kind == "n":
                            kind, authored = "number", raw or str(value)
                        elif kind == "e":
                            kind, authored = "error", str(value)
                        else:
                            kind, authored = "string", str(value)
                        row, column = decoded["row"], decoded["column"]
                        yield WorkbookCell(
                            sheet.title,
                            f"{get_column_letter(column)}{row}",
                            row,
                            column,
                            kind,
                            value,
                            authored,
                            raw if formula is not None else None,
                            tuple(formula.attrib.items()) if formula is not None else (),
                        )
                    element.clear()
    except (KeyError, ValueError, TypeError, IndexError) as exc:
        raise SourceReadError(f"Cannot decode workbook {path}: {exc}") from exc
    finally:
        workbook.close()
