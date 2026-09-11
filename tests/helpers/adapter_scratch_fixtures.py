"""Fixture builders shared by the adapter scratch-path tests.

Two suites drive the same adapter branches from different directions: the
conformance gate in ``tests/sage/test_adapter_scratch_path_conformance.py``
asserts the property over every recorded scratch path, and the per-instance
tests in ``tests/sage/test_adapters.py`` pin each named site. Both need a
package that reaches a library's content-type check against a shadow copy,
and both need a way to fail the shadow write. Sharing the builders keeps the
two arms driving the same branch: a fixture corrected in one place cannot
leave the other exercising a different failure.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

#: Main-part content types that neither adapter's shadow rewrite converts.
#: A package typed this way enters the shadow branch and still fails its
#: library's content-type check -- the one failure on that branch naming the
#: file it was handed.
MACRO_DOCX_TYPE = "application/vnd.ms-word.template.macroEnabledTemplate.main+xml"
MACRO_PPTX_TYPE = "application/vnd.ms-powerpoint.template.macroEnabled.main+xml"


def retype_docx_main_part(built: Path, out: Path, docx_main_type: str) -> Path:
    """Copy ``built`` to ``out`` with its main part typed as a third flavor.

    The docx shadow branch is entered on the ``.dotx`` suffix and its rewrite
    swaps only the plain template type, so a package typed this way takes the
    branch and reaches python-docx's content-type check against the shadow.
    """
    with zipfile.ZipFile(built) as z_in:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z_out:
            for item in z_in.namelist():
                data = z_in.read(item)
                if item == "[Content_Types].xml":
                    data = data.replace(
                        docx_main_type.encode("utf-8"), MACRO_DOCX_TYPE.encode("utf-8")
                    )
                z_out.writestr(item, data)
    return out


def decoy_potx_package(built: Path, out: Path, pptx_main_type: str, potx_main_type: str) -> Path:
    """Copy ``built`` to ``out`` so it enters the pptx shadow branch and still fails.

    The pptx branch is entered when the template type appears anywhere in
    ``[Content_Types].xml``, and its rewrite swaps that string. Typing the main
    part as a third flavor while declaring the template type on an unrelated
    part therefore takes the branch and still fails python-pptx's content-type
    check -- against the shadow's path.
    """
    with zipfile.ZipFile(built) as z_in:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z_out:
            for item in z_in.namelist():
                data = z_in.read(item)
                if item == "[Content_Types].xml":
                    text = data.decode("utf-8")
                    text = text.replace(pptx_main_type, MACRO_PPTX_TYPE)
                    text = text.replace(
                        "</Types>",
                        f'<Override PartName="/ppt/unused.xml" '
                        f'ContentType="{potx_main_type}"/></Types>',
                    )
                    data = text.encode("utf-8")
                z_out.writestr(item, data)
    return out


def fail_zip_writes(monkeypatch: Any, module: Any) -> None:
    """Make write-mode ``zipfile.ZipFile`` raise an OSError naming its target.

    Stubs the operating-system condition, not the adapter: a full or read-only
    filesystem produces exactly this, and ``OSError.__str__`` appends the path.
    Read-mode opens are left alone, so the caller's package still opens and the
    adapter reaches the shadow write under test; failing every open would raise
    earlier, against the caller's own path, and pin nothing.
    """
    original = module.zipfile.ZipFile

    def failing(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if mode == "w":
            raise OSError(28, "No space left on device", str(file))
        return original(file, mode, *args, **kwargs)

    monkeypatch.setattr(module.zipfile, "ZipFile", failing)
