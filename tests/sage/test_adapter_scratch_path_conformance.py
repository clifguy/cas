"""Conformance gate: an adapter never names a path it created for itself.

A caller-facing failure message must name only the file the caller supplied.
The projection seam in ``sage/services/ingestion.py`` substitutes the path it
passed *into* the adapter, so it structurally cannot reach a path the adapter
created after that -- an OCR output, a content-type-rewritten shadow copy, any
scratch file. Where an adapter names one of those, the process's temp layout
reaches the caller and the seam is powerless to stop it.

The gate has two arms, and each covers what the other cannot.

**Arm 1, the property.** A recorder wraps every ``tempfile`` creation function
and captures the paths an adapter makes during one call. Each case drives an
adapter to a failure *on its scratch branch* and asserts that no recorded path
appears in the message. This tests the property rather than enumerating known
raise sites, so a new raise added inside an existing scratch branch is covered
without anyone remembering to list it.

**Arm 2, the reach.** Arm 1 can only cover a branch someone wrote a case for.
An ``ast`` walk collects every ``(module, qualname)`` in the adapter package
that calls a ``tempfile`` creation function, and requires each to be either
driven by a case or allowlisted. A new adapter, or a new scratch path in an
existing one, reds this until it is covered.

Allowlist contract, following ``test_typed_alias_coverage.py``:

- A scratch site neither driven nor allowlisted fails the suite (new-drift).
- An allowlist entry naming a site that no longer exists fails the suite
  (phantom-entry), so removing a scratch path cannot leave the allowlist stale.

``KNOWN_UNREACHABLE_SCRATCH_SITES`` ships empty. Every live scratch site today
is driven by a case, and an entry added later must state why the created path
provably cannot reach a caller-facing message.

Documented blind spots. The detector recognizes creation through the
``tempfile`` module by attribute spelling. A scratch file written with a
hand-built path (``Path(...) / "scratch"`` under a directory obtained some other
way) is invisible to Arm 2, though Arm 1 still catches it for any branch a case
drives. And the recorder captures the directory a ``mkdtemp`` call returns, not
the individual files written inside it; that is sufficient because a disclosed
file path contains its directory, but a message naming only a scratch
*basename* would pass.

The gate exists because prose could not hold this invariant. Three sweeps of
this axis each closed the sites they touched and left a sibling standing, and
each sibling was found by review after the sweep had declared the axis clean.
"""

from __future__ import annotations

import ast
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable, Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
ADAPTER_PACKAGE: Final[Path] = REPO_ROOT / "sage" / "source_adapters"

# ``tempfile`` members that bring a new filesystem path into existence. Reads of
# the module's configuration (``gettempdir``, ``tempdir``) are deliberately
# absent: they name a location the adapter did not create.
SCRATCH_FACTORIES: Final[frozenset[str]] = frozenset(
    {
        "mkdtemp",
        "mkstemp",
        "NamedTemporaryFile",
        "TemporaryFile",
        "TemporaryDirectory",
        "SpooledTemporaryFile",
    }
)

# The subset whose result carries a filesystem name, which the recorder wraps.
# ``TemporaryFile`` and ``SpooledTemporaryFile`` are deliberately outside it:
# neither yields a path, so neither can be named in a message. They stay in the
# detector's set anyway, so Arm 2 still demands an account of a site that uses
# one -- conservative detection, with the reason for the narrower recording
# pinned by ``test_recorder_wraps_every_naming_factory``.
RECORDED_FACTORIES: Final[frozenset[str]] = frozenset(
    {"mkdtemp", "mkstemp", "NamedTemporaryFile", "TemporaryDirectory"}
)

UNNAMED_FACTORIES: Final[frozenset[str]] = frozenset({"TemporaryFile", "SpooledTemporaryFile"})

# (module path relative to the repo root, qualname) -> why the created path can
# never reach a caller-facing message. Empty: every live site is driven by a
# case in DRIVING_CASES below. An entry added here must carry that argument,
# not merely assert it.
KNOWN_UNREACHABLE_SCRATCH_SITES: Final[dict[tuple[str, str], str]] = {}


# ---------------------------------------------------------------------------
# Arm 2: the static scan
# ---------------------------------------------------------------------------


def _qualnames(tree: ast.AST) -> dict[int, str]:
    """Map each node's id to the dotted name of its nearest enclosing def.

    Depth-first, assigning on the way down, so a method's body is owned by
    ``Class.method`` rather than by ``Class``.
    """
    owners: dict[int, str] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}.{child.name}" if prefix else child.name
                owners[id(child)] = prefix or "<module>"
                walk(child, name)
            else:
                owners[id(child)] = prefix or "<module>"
                walk(child, prefix)

    walk(tree, "")
    return owners


def _is_scratch_call(node: ast.AST) -> bool:
    """True for ``tempfile.<factory>(...)``, matched by spelling, never substring."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in SCRATCH_FACTORIES
        and isinstance(func.value, ast.Name)
        and func.value.id == "tempfile"
    )


def _find_scratch_sites() -> set[tuple[str, str]]:
    """Every (module, qualname) in the adapter package that creates a scratch path."""
    sites: set[tuple[str, str]] = set()
    for path in sorted(ADAPTER_PACKAGE.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        owners = _qualnames(tree)
        for node in ast.walk(tree):
            if _is_scratch_call(node):
                sites.add((rel, owners.get(id(node), "<module>")))
    return sites


def _format_sites(sites: list[tuple[str, str]]) -> str:
    return "\n".join(f"  {module} ({qualname})" for module, qualname in sites)


# ---------------------------------------------------------------------------
# Arm 1: the recorder
# ---------------------------------------------------------------------------


class ScratchRecorder:
    """Records every path the ``tempfile`` module hands out while active."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.wrapped: frozenset[str] = frozenset()

    def leaked_in(self, message: str) -> list[str]:
        return [p for p in self.paths if p in message]


@pytest.fixture
def scratch_recorder(monkeypatch: pytest.MonkeyPatch) -> ScratchRecorder:
    """Wrap the ``tempfile`` factories so created paths are observable.

    Delegates to the real implementation in every case: the adapter's scratch
    files must actually exist, or the branch under test would fail for the wrong
    reason and the case would pin nothing.
    """
    recorder = ScratchRecorder()

    def wrap(name: str, extract: Callable[[Any], str]) -> None:
        original = getattr(tempfile, name)

        def recording(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            recorder.paths.append(extract(result))
            return result

        monkeypatch.setattr(tempfile, name, recording)

    extractors: dict[str, Callable[[Any], str]] = {
        "mkdtemp": lambda r: str(r),
        "mkstemp": lambda r: str(r[1]),
        "NamedTemporaryFile": lambda r: str(r.name),
        "TemporaryDirectory": lambda r: str(r.name),
    }
    recorder.wrapped = frozenset(extractors)
    for factory, extract in extractors.items():
        wrap(factory, extract)
    return recorder


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

_MACRO_DOCX_TYPE: Final[str] = "application/vnd.ms-word.template.macroEnabledTemplate.main+xml"
_MACRO_PPTX_TYPE: Final[str] = "application/vnd.ms-powerpoint.template.macroEnabled.main+xml"


def _blank_page_pdf(path: Path) -> Path:
    """A one-page PDF with no text layer, which the adapter treats as scanned."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=letter)
    c.setTitle("")
    c.showPage()
    c.save()
    return path


def _retyped_docx(tmp_path: Path, name: str) -> Path:
    """A real .docx package whose main part carries a third template flavor.

    The adapter enters its shadow branch on the ``.dotx`` suffix, its rewrite
    swaps only the plain template type, so python-docx reaches its content-type
    check against the shadow -- the one library failure here that names the file
    it was handed.
    """
    import docx

    from sage.source_adapters.docx_adapter import _DOCX_CONTENT_TYPE

    built = tmp_path / "built-for-retype.docx"
    docx.Document().save(str(built))
    out = tmp_path / name
    with zipfile.ZipFile(built) as z_in:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z_out:
            for item in z_in.namelist():
                data = z_in.read(item)
                if item == "[Content_Types].xml":
                    data = data.replace(
                        _DOCX_CONTENT_TYPE.encode("utf-8"), _MACRO_DOCX_TYPE.encode("utf-8")
                    )
                z_out.writestr(item, data)
    return out


def _retyped_pptx(tmp_path: Path, name: str) -> Path:
    """A real .pptx package that reaches python-pptx's content-type check.

    The adapter enters its shadow branch when the template content type appears
    anywhere in ``[Content_Types].xml``, and its rewrite swaps that string. A
    package whose *main* part is a third flavor, with the plain template type
    declared on an unrelated part, therefore takes the branch and still fails
    the library's check -- against the shadow's path.
    """
    from pptx import Presentation

    from sage.source_adapters.pptx_adapter import _POTX_MAIN_TYPE, _PPTX_MAIN_TYPE

    built = tmp_path / "built-for-retype.pptx"
    Presentation().save(str(built))
    out = tmp_path / name
    with zipfile.ZipFile(built) as z_in:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z_out:
            for item in z_in.namelist():
                data = z_in.read(item)
                if item == "[Content_Types].xml":
                    text = data.decode("utf-8")
                    text = text.replace(_PPTX_MAIN_TYPE, _MACRO_PPTX_TYPE)
                    text = text.replace(
                        "</Types>",
                        f'<Override PartName="/ppt/unused.xml" '
                        f'ContentType="{_POTX_MAIN_TYPE}"/></Types>',
                    )
                    data = text.encode("utf-8")
                z_out.writestr(item, data)
    return out


def _fail_zip_writes(monkeypatch: pytest.MonkeyPatch, module: Any) -> None:
    """Make write-mode ``zipfile.ZipFile`` raise an OSError naming its target.

    Stubs the operating-system condition, not the adapter: a full or read-only
    filesystem produces exactly this, and ``OSError.__str__`` appends the path.
    Read-mode opens are left alone, so the source package still opens and the
    adapter reaches the shadow write it is being tested on.
    """
    original = module.zipfile.ZipFile

    def failing(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if mode == "w":
            raise OSError(28, "No space left on device", str(file))
        return original(file, mode, *args, **kwargs)

    monkeypatch.setattr(module.zipfile, "ZipFile", failing)


# ---------------------------------------------------------------------------
# Arm 1: the driving cases
# ---------------------------------------------------------------------------


async def _drive_pdf_post_ocr_extraction(tmp_path, monkeypatch):
    """The post-OCR re-extraction fails against the OCR output tempfile."""
    import sys
    import types

    from sage.source_adapters.pdf_adapter import PdfAdapter

    source = _blank_page_pdf(tmp_path / "scan.pdf")

    fake = types.ModuleType("ocrmypdf")

    def ocr(input_file, output_file, **kwargs):
        # A successful OCR run that emits an unreadable PDF. The real extractor
        # then raises against the tempfile, which is the branch under test; a
        # stubbed extractor would prove nothing about the real raise sites.
        Path(output_file).write_bytes(b"%PDF-1.4 truncated and unreadable")

    fake.ocr = ocr
    monkeypatch.setitem(sys.modules, "ocrmypdf", fake)
    return await PdfAdapter().project(source), source


async def _drive_pdf_ocr_tool_failure(tmp_path, monkeypatch):
    """The OCR tool itself fails, naming the output file it was handed."""
    import sys
    import types

    from sage.source_adapters.pdf_adapter import PdfAdapter

    source = _blank_page_pdf(tmp_path / "scan.pdf")

    fake = types.ModuleType("ocrmypdf")

    def ocr(input_file, output_file, **kwargs):
        raise RuntimeError(f"tesseract exited 1 while writing {output_file}")

    fake.ocr = ocr
    monkeypatch.setitem(sys.modules, "ocrmypdf", fake)
    return await PdfAdapter().project(source), source


async def _drive_docx_template_library_failure(tmp_path, monkeypatch):
    """python-docx rejects the shadow on its content-type check."""
    from sage.source_adapters.docx_adapter import DocxAdapter

    source = _retyped_docx(tmp_path, "macro_template.dotx")
    return await DocxAdapter().project(source), source


async def _drive_docx_shadow_write_failure(tmp_path, monkeypatch):
    """Writing the shadow copy fails, and the OSError names the shadow."""
    import docx as _docx

    from sage.source_adapters import docx_adapter
    from sage.source_adapters.docx_adapter import DocxAdapter

    built = tmp_path / "template.dotx"
    _docx.Document().save(str(built))
    _fail_zip_writes(monkeypatch, docx_adapter)
    return await DocxAdapter().project(built), built


async def _drive_pptx_template_library_failure(tmp_path, monkeypatch):
    """python-pptx rejects the shadow on its content-type check."""
    from sage.source_adapters.pptx_adapter import PptxAdapter

    source = _retyped_pptx(tmp_path, "decoy_template.pptx")
    return await PptxAdapter().project(source), source


async def _drive_pptx_shadow_write_failure(tmp_path, monkeypatch):
    """Writing the shadow copy fails, and the OSError names the shadow."""
    from pptx import Presentation

    from sage.source_adapters import pptx_adapter
    from sage.source_adapters.pptx_adapter import (
        _POTX_MAIN_TYPE,
        _PPTX_MAIN_TYPE,
        PptxAdapter,
    )

    built = tmp_path / "built.pptx"
    Presentation().save(str(built))
    source = tmp_path / "template.potx"
    with zipfile.ZipFile(built) as z_in:
        with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as z_out:
            for item in z_in.namelist():
                data = z_in.read(item)
                if item == "[Content_Types].xml":
                    data = data.replace(
                        _PPTX_MAIN_TYPE.encode("utf-8"), _POTX_MAIN_TYPE.encode("utf-8")
                    )
                z_out.writestr(item, data)

    _fail_zip_writes(monkeypatch, pptx_adapter)
    return await PptxAdapter().project(source), source


# Each case: the scratch site it exercises, how the failure is reached, and a
# fragment of the underlying diagnosis that must survive the respelling. The
# fragment is what distinguishes substituting the path from discarding the
# message, and the site keys are what Arm 2 checks its scan against.
DRIVING_CASES: Final[dict[str, dict[str, Any]]] = {
    "pdf_post_ocr_extraction": {
        "site": ("sage/source_adapters/pdf_adapter.py", "_ocr_to_tempfile"),
        "drive": _drive_pdf_post_ocr_extraction,
        "survives": "Failed to open PDF",
        "disposition": (
            "Respelled. Avoidance is unavailable: the OCR output exists to be "
            "re-extracted, so the extractor must be handed it."
        ),
    },
    "pdf_ocr_tool_failure": {
        "site": ("sage/source_adapters/pdf_adapter.py", "_ocr_to_tempfile"),
        "drive": _drive_pdf_ocr_tool_failure,
        "survives": "tesseract exited 1",
        "disposition": (
            "Respelled. The tool is handed the output path by construction and "
            "names it in its own text."
        ),
    },
    "docx_template_library_failure": {
        "site": ("sage/source_adapters/docx_adapter.py", "DocxAdapter._open_document"),
        "drive": _drive_docx_template_library_failure,
        "survives": "is not a Word file",
        "disposition": (
            "Respelled. Avoidance is unavailable: the shadow exists because the "
            "library rejects the original, so the library must be handed the shadow."
        ),
    },
    "docx_shadow_write_failure": {
        "site": ("sage/source_adapters/docx_adapter.py", "DocxAdapter._open_document"),
        "drive": _drive_docx_shadow_write_failure,
        "survives": "No space left on device",
        "disposition": "Respelled. The write's own failure names the file being written.",
    },
    "pptx_template_library_failure": {
        "site": ("sage/source_adapters/pptx_adapter.py", "_open_presentation"),
        "drive": _drive_pptx_template_library_failure,
        "survives": "is not a PowerPoint file",
        "disposition": (
            "Respelled. Gating the branch on the package's content type rather "
            "than its suffix narrows what can reach the library, but does not "
            "keep the shadow out of its hands."
        ),
    },
    "pptx_shadow_write_failure": {
        "site": ("sage/source_adapters/pptx_adapter.py", "_open_presentation"),
        "drive": _drive_pptx_shadow_write_failure,
        "survives": "No space left on device",
        "disposition": "Respelled. The write's own failure names the file being written.",
    },
}


@pytest.mark.parametrize("case_name", sorted(DRIVING_CASES))
async def test_adapter_failure_names_no_path_it_created(
    case_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scratch_recorder
) -> None:
    """An adapter driven to a scratch-branch failure names only the caller's file.

    Anti-coincidental-pass: three assertions, and the second is the one that
    makes the others mean anything. A case whose input never reaches the scratch
    branch creates no scratch path, so the leak assertion is satisfied by any
    implementation -- including one that interpolates the scratch path verbatim.
    Requiring the recorder to have captured at least one path turns that silent
    pass into a red. The surviving-fragment assertion fails an implementation
    that drops the underlying diagnosis instead of substituting the path, since
    only the location may change.
    """
    case = DRIVING_CASES[case_name]

    with pytest.raises(Exception) as excinfo:
        await case["drive"](tmp_path, monkeypatch)
    message = str(excinfo.value)

    assert scratch_recorder.paths, (
        f"Case {case_name!r} raised without creating any scratch path, so it "
        "cannot discriminate. Fix the fixture so it reaches the adapter's "
        "scratch branch."
    )
    leaked = scratch_recorder.leaked_in(message)
    assert not leaked, (
        f"Case {case_name!r}: the message names a path the adapter created "
        f"rather than the one it was handed: {leaked}\n"
        f"Message: {message}\n"
        "Respell it with sage.source_adapters.base.respell_created_path."
    )
    assert str(tmp_path) in message, message
    assert case["survives"] in message, (
        f"Case {case_name!r}: the underlying diagnosis "
        f"{case['survives']!r} did not survive. Substitute the path rather than "
        f"replacing the message.\nMessage: {message}"
    )


# ---------------------------------------------------------------------------
# Arm 2: the gate tests
# ---------------------------------------------------------------------------


def _covered_sites() -> set[tuple[str, str]]:
    return {case["site"] for case in DRIVING_CASES.values()}


def test_recorder_wraps_every_naming_factory(scratch_recorder) -> None:
    """The recorder observes every detected factory that yields a path.

    Without this the recorder's wrapper set is inert scaffolding: a factory it
    forgets to wrap creates paths the leak assertion never sees, and every case
    stays green while the property goes unchecked. Asserting the set against the
    detector's own is what makes the two agree by construction rather than by
    someone remembering to update both. The excluded pair is asserted by name,
    so widening the detector without deciding which side a new factory belongs
    on reds here rather than silently joining the unrecorded set.
    """
    assert scratch_recorder.wrapped == RECORDED_FACTORIES
    assert RECORDED_FACTORIES | UNNAMED_FACTORIES == SCRATCH_FACTORIES
    assert not RECORDED_FACTORIES & UNNAMED_FACTORIES


def test_no_undriven_scratch_site() -> None:
    """New-drift: every scratch site is driven by a case or allowlisted.

    Arm 1 proves the property only for branches a case reaches. This is what
    stops a new scratch path -- a new adapter, or a second one in an existing
    adapter -- from landing unrespelled with the gate still green.
    """
    undriven = sorted(
        _find_scratch_sites() - _covered_sites() - set(KNOWN_UNREACHABLE_SCRATCH_SITES)
    )
    assert not undriven, (
        "These sites create a scratch path that no case drives to failure, so "
        "nothing checks whether the path can reach a caller:\n"
        f"{_format_sites(undriven)}\n"
        "Add a case to DRIVING_CASES, or add a KNOWN_UNREACHABLE_SCRATCH_SITES "
        "entry arguing why the created path cannot reach a caller-facing message."
    )


def test_no_stale_allowlist_entry() -> None:
    """Phantom-entry: an allowlist entry must name a site that still exists.

    Vacuously green while the allowlist is empty, following
    ``test_source_delimitation_coverage.py``: it lands armed so the first entry
    arrives with its liveness check already in place.
    """
    stale = sorted(set(KNOWN_UNREACHABLE_SCRATCH_SITES) - _find_scratch_sites())
    assert not stale, (
        "These allowlist entries name no remaining scratch site; drain them:\n"
        f"{_format_sites(stale)}"
    )


def test_no_stale_driving_case() -> None:
    """A case must name a scratch site the scan still finds.

    The control on the scan itself. Every case key is a live site today, so
    narrowing the walk -- a dropped factory name, a wrong package root, a
    qualname regression -- orphans a case and reds this, rather than quietly
    emptying the set difference in the new-drift test above.
    """
    stale = sorted(_covered_sites() - _find_scratch_sites())
    assert not stale, (
        "These DRIVING_CASES site keys match no scratch site the scan found. "
        "Either the site moved and the key needs updating, or the scan's reach "
        f"regressed:\n{_format_sites(stale)}"
    )


def test_scan_reaches_the_known_scratch_sites() -> None:
    """Anti-vacuity: the walk finds the three sites known to exist.

    Named individually rather than counted, so the test also fails when the scan
    reaches only some of them. Without this, an empty walk would leave every
    assertion above passing over nothing.
    """
    sites = _find_scratch_sites()
    for expected in (
        ("sage/source_adapters/pdf_adapter.py", "_ocr_to_tempfile"),
        ("sage/source_adapters/docx_adapter.py", "DocxAdapter._open_document"),
        ("sage/source_adapters/pptx_adapter.py", "_open_presentation"),
    ):
        assert expected in sites, f"scan did not reach {expected}; found {sorted(sites)}"


def test_scan_covers_every_registered_adapter() -> None:
    """Every adapter the process registers lives in a module the scan walks.

    An adapter registered from outside the walked package would carry scratch
    paths this gate never examines, and nothing else in the suite would say so.
    """
    from sage.mcp_init import build_source_adapter_registry

    walked = {path.relative_to(REPO_ROOT).as_posix() for path in ADAPTER_PACKAGE.rglob("*.py")}
    registered = {
        type(adapter).__module__.replace(".", "/") + ".py"
        for adapter in build_source_adapter_registry().values()
    }
    assert registered <= walked, (
        f"Registered adapters outside the scanned package: {sorted(registered - walked)}"
    )


_DETECTOR_CASES: Final[dict[str, tuple[str, bool]]] = {
    "mkdtemp": ("import tempfile\ndef f():\n    return tempfile.mkdtemp()\n", True),
    "mkstemp": ("import tempfile\ndef f():\n    return tempfile.mkstemp()\n", True),
    "named_temporary_file": (
        "import tempfile\ndef f():\n    return tempfile.NamedTemporaryFile()\n",
        True,
    ),
    "temporary_file": ("import tempfile\ndef f():\n    return tempfile.TemporaryFile()\n", True),
    "temporary_directory": (
        "import tempfile\ndef f():\n    return tempfile.TemporaryDirectory()\n",
        True,
    ),
    "spooled_temporary_file": (
        "import tempfile\ndef f():\n    return tempfile.SpooledTemporaryFile()\n",
        True,
    ),
    # Near misses. The first two are live in the tree: the pdf adapter reads and
    # reassigns the module's configuration to route OCR intermediates, and
    # neither call creates anything.
    "gettempdir_is_a_read": (
        "import tempfile\ndef f():\n    return tempfile.gettempdir()\n",
        False,
    ),
    "tempdir_assignment_creates_nothing": (
        "import tempfile\ndef f():\n    tempfile.tempdir = '/x'\n",
        False,
    ),
    "bare_name_is_not_the_module": ("def f(mkdtemp):\n    return mkdtemp()\n", False),
    "same_attr_on_another_object": (
        "def f(helper):\n    return helper.mkdtemp()\n",
        False,
    ),
}


@pytest.mark.parametrize(
    ("source", "expected"),
    [pytest.param(src, ok, id=name) for name, (src, ok) in _DETECTOR_CASES.items()],
)
def test_detector_matches_exactly_the_creation_spellings(source: str, expected: bool) -> None:
    """The detector accepts each creating spelling and rejects each near miss.

    A direct control on the classifier. A factory name with no live site in the
    tree is invisible to the site-based tests above and could be deleted with
    the gate still green; and a false positive on a configuration read would
    make the gate unusable, which is the other way a gate dies.
    """
    tree = ast.parse(source)
    found = any(_is_scratch_call(node) for node in ast.walk(tree))
    assert found is expected
