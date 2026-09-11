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

Documented blind spots. The detector resolves each module's ``tempfile``
imports before classifying a call, so a plain import, a module alias, and a
from-import (renamed or not) are all recognized. Arm 1's recorder follows the
same resolution -- a from-import binds the original function into the importing
module, which patching ``tempfile`` alone would not reach -- so the two arms see
the same set of sites by construction rather than by convention. What it does not reach: a
scratch file written with a hand-built path (``Path(...) / "scratch"`` under a
directory obtained some other way), and a factory reached indirectly through a
local helper or a re-export. Arm 1 still catches either for any branch a case
drives. The recorder captures the directory a ``mkdtemp`` call returns, not the
individual files written inside it; that is sufficient because a disclosed file
path contains its directory, but a message naming only a scratch *basename*
would pass. Nor does either arm see a path created by a **tool the adapter
invokes** rather than by the adapter -- the OCR intermediates ocrmypdf builds
under the base directory the pdf adapter routes it to are redacted by that
adapter directly, and no case here observes them, because every case stubs the
tool. Finally, the respelling matches a path by exact string, so a library
reporting a resolved spelling of the same file (``/var`` against
``/private/var`` on macOS) would evade it; no library in scope does today.

The gate exists because prose could not hold this invariant. Three sweeps of
this axis each closed the sites they touched and left a sibling standing, and
each sibling was found by review after the sweep had declared the axis clean.
"""

from __future__ import annotations

import ast
import importlib
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable, Final, NamedTuple

import pytest

from sage.source_adapters.base import TEMP_LOCATION_MARKER
from tests.helpers.adapter_scratch_fixtures import (
    decoy_potx_package,
    fail_zip_writes,
    retype_docx_main_part,
)

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


class _TempfileNames(NamedTuple):
    """The names a module binds that reach ``tempfile``, resolved from its imports.

    ``modules`` holds the names bound to the module itself -- ``tempfile`` for a
    plain import, plus any alias. ``direct`` maps a bare name bound by a
    from-import onto the factory it refers to, so a renamed import is followed
    to the factory rather than matched on the local spelling.
    """

    modules: frozenset[str]
    direct: dict[str, str]


def _tempfile_names(tree: ast.AST) -> _TempfileNames:
    """Resolve every local name in ``tree`` that reaches a ``tempfile`` factory.

    Import spelling is a property of the module, not of the call site, so it has
    to be resolved before any call can be classified. Matching only the literal
    ``tempfile.mkdtemp`` shape would leave ``from tempfile import mkdtemp`` and
    ``import tempfile as tf`` invisible -- a scratch site the gate is supposed to
    demand an account of, evaded by a spelling nothing else in the repo pins.
    """
    modules: set[str] = set()
    direct: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "tempfile":
                    modules.add(alias.asname or "tempfile")
        elif isinstance(node, ast.ImportFrom) and node.module == "tempfile":
            for alias in node.names:
                if alias.name in SCRATCH_FACTORIES:
                    direct[alias.asname or alias.name] = alias.name
    return _TempfileNames(frozenset(modules), direct)


def _scratch_factory(node: ast.AST, names: _TempfileNames) -> str | None:
    """The factory ``node`` calls, or None. Matched by spelling, never substring."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute):
        if (
            func.attr in SCRATCH_FACTORIES
            and isinstance(func.value, ast.Name)
            and func.value.id in names.modules
        ):
            return func.attr
        return None
    if isinstance(func, ast.Name):
        return names.direct.get(func.id)
    return None


def _find_scratch_sites() -> set[tuple[str, str]]:
    """Every (module, qualname) in the adapter package that creates a scratch path."""
    sites: set[tuple[str, str]] = set()
    for path in sorted(ADAPTER_PACKAGE.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        owners = _qualnames(tree)
        names = _tempfile_names(tree)
        for node in ast.walk(tree):
            if _scratch_factory(node, names) is not None:
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
        #: The unpatched factories, by name. A module that from-imported one
        #: holds exactly this object, bound before any patch could reach it.
        self.originals: dict[str, Any] = {}
        #: Adapter modules whose from-imported factory names were rebound, so
        #: the fixture's reach is observable rather than assumed.
        self.rebound_modules: set[str] = set()

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
        recorder.originals[name] = original

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

    # A from-import binds the original function into the adapter module at
    # import time, so patching the ``tempfile`` module afterwards does not reach
    # it. Without rebinding, Arm 1 would be blind to exactly the spellings Arm 2
    # resolves, and a case driving such a site would fail claiming its fixture
    # never reached the scratch branch -- wrong about the cause, and pointing the
    # next author at the allowlist. Rebinding here makes the two arms agree by
    # construction, as ``test_recorder_wraps_every_naming_factory`` does for the
    # factory set.
    for module_path in sorted(ADAPTER_PACKAGE.rglob("*.py")):
        names = _tempfile_names(ast.parse(module_path.read_text(encoding="utf-8")))
        if not names.direct:
            continue
        module = importlib.import_module(
            f"sage.source_adapters.{module_path.stem}"
            if module_path.stem != "__init__"
            else "sage.source_adapters"
        )
        if rebind_direct_imports(monkeypatch, module, names, recorder.wrapped):
            recorder.rebound_modules.add(module_path.relative_to(REPO_ROOT).as_posix())
    return recorder


def rebind_direct_imports(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    names: _TempfileNames,
    wrapped: frozenset[str],
) -> list[str]:
    """Point a module's from-imported factory names at the patched originals.

    Returns the local names rebound, so a caller can assert the reach rather
    than assume it. Separate from the fixture because no adapter uses a
    from-import today: walked over the live package the loop rebinds nothing,
    and a branch with no live site is scaffolding that reads as a control.
    """
    rebound: list[str] = []
    for local, factory in sorted(names.direct.items()):
        if factory in wrapped and hasattr(module, local):
            monkeypatch.setattr(module, local, getattr(tempfile, factory))
            rebound.append(local)
    return rebound


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


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
    """A real .docx package whose main part carries a third template flavor."""
    import docx

    from sage.source_adapters.docx_adapter import _DOCX_CONTENT_TYPE

    built = tmp_path / "built-for-retype.docx"
    docx.Document().save(str(built))
    return retype_docx_main_part(built, tmp_path / name, _DOCX_CONTENT_TYPE)


def _retyped_pptx(tmp_path: Path, name: str) -> Path:
    """A real .pptx package that reaches python-pptx's content-type check."""
    from pptx import Presentation

    from sage.source_adapters.pptx_adapter import _POTX_MAIN_TYPE, _PPTX_MAIN_TYPE

    built = tmp_path / "built-for-retype.pptx"
    Presentation().save(str(built))
    return decoy_potx_package(built, tmp_path / name, _PPTX_MAIN_TYPE, _POTX_MAIN_TYPE)


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
    fail_zip_writes(monkeypatch, docx_adapter)
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

    fail_zip_writes(monkeypatch, pptx_adapter)
    return await PptxAdapter().project(source), source


# Each case: the scratch site it exercises, how the failure is reached, a
# fragment of the underlying diagnosis that must survive, and which protection
# is expected to do the work. The fragment distinguishes substituting the path
# from discarding the message; ``redacted`` distinguishes a site the respelling
# protects from one the sibling redaction would cover anyway, without which a
# respell could be deleted with its own case still green. The site keys are what
# Arm 2 checks its scan against.
DRIVING_CASES: Final[dict[str, dict[str, Any]]] = {
    "pdf_post_ocr_extraction": {
        "site": ("sage/source_adapters/pdf_adapter.py", "_ocr_to_tempfile"),
        "drive": _drive_pdf_post_ocr_extraction,
        "survives": "Failed to open PDF",
        "redacted": False,
        "disposition": (
            "Respelled. Avoidance is unavailable: the OCR output exists to be "
            "re-extracted, so the extractor must be handed it."
        ),
    },
    "pdf_ocr_tool_failure": {
        "site": ("sage/source_adapters/pdf_adapter.py", "_ocr_to_tempfile"),
        "drive": _drive_pdf_ocr_tool_failure,
        "survives": "tesseract exited 1",
        "redacted": False,
        "disposition": (
            "Respelled. The tool is handed the output path by construction and "
            "names it in its own text."
        ),
    },
    "docx_template_library_failure": {
        "site": ("sage/source_adapters/docx_adapter.py", "DocxAdapter._open_document"),
        "drive": _drive_docx_template_library_failure,
        "survives": "is not a Word file",
        "redacted": False,
        "disposition": (
            "Respelled. Avoidance is unavailable: the shadow exists because the "
            "library rejects the original, so the library must be handed the shadow."
        ),
    },
    "docx_shadow_write_failure": {
        "site": ("sage/source_adapters/docx_adapter.py", "DocxAdapter._open_document"),
        "drive": _drive_docx_shadow_write_failure,
        "survives": "No space left on device",
        "redacted": False,
        "disposition": "Respelled. The write's own failure names the file being written.",
    },
    "pptx_template_library_failure": {
        "site": ("sage/source_adapters/pptx_adapter.py", "_open_presentation"),
        "drive": _drive_pptx_template_library_failure,
        "survives": "is not a PowerPoint file",
        "redacted": False,
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
        "redacted": False,
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
    if not case["redacted"]:
        # The site respells rather than redacts, so the marker must be absent.
        # Without this a site whose scratch path happens to sit under the temp
        # base is protected by the sibling redaction alone, and the leak
        # assertion above stays green with the respell deleted -- which is true
        # today of the OCR output file.
        assert TEMP_LOCATION_MARKER not in message, (
            f"Case {case_name!r} is a respelling site, but its message carries "
            f"the redaction marker, so the respell is not what protected it.\n"
            f"Message: {message}"
        )


# ---------------------------------------------------------------------------
# Arm 2: the gate tests
# ---------------------------------------------------------------------------


def _covered_sites() -> set[tuple[str, str]]:
    return {case["site"] for case in DRIVING_CASES.values()}


def test_recorder_reaches_a_from_imported_factory(
    monkeypatch: pytest.MonkeyPatch, scratch_recorder
) -> None:
    """A from-imported factory is observed by the recorder once rebound.

    A from-import binds the original function into the importing module at
    import time, so patching the ``tempfile`` module afterwards does not reach
    it. Arm 2 resolves that spelling, so without the rebinding Arm 1 would be
    blind to exactly the sites Arm 2 demands an account of, and a case driving
    one would fail claiming its fixture never reached the scratch branch.

    Driven against a synthetic module because no adapter uses a from-import
    today: over the live package the rebinding loop touches nothing, so this is
    the only thing keeping it honest. The unpatched call is asserted first as a
    positive control -- without it the test would pass against a recorder that
    observed the call for some other reason.
    """
    import types

    source = "from tempfile import mkdtemp\n"
    module = types.ModuleType("synthetic_adapter")
    # The binding a from-import makes: the *unpatched* function, captured at
    # import time. Reading ``tempfile.mkdtemp`` here would capture the fixture's
    # wrapper instead and the control below would pass for the wrong reason.
    module.mkdtemp = scratch_recorder.originals["mkdtemp"]

    before = len(scratch_recorder.paths)
    created = module.mkdtemp()
    shutil.rmtree(created, ignore_errors=True)
    assert len(scratch_recorder.paths) == before, (
        "positive control: the module-level binding must escape the fixture's "
        "patch, or this test proves nothing about the rebinding"
    )

    names = _tempfile_names(ast.parse(source))
    rebound = rebind_direct_imports(monkeypatch, module, names, scratch_recorder.wrapped)
    assert rebound == ["mkdtemp"], rebound

    created = module.mkdtemp()
    shutil.rmtree(created, ignore_errors=True)
    assert scratch_recorder.paths[-1] == created


def _modules_with_direct_imports() -> set[str]:
    """Adapter modules that from-import at least one recordable factory."""
    found: set[str] = set()
    for path in sorted(ADAPTER_PACKAGE.rglob("*.py")):
        names = _tempfile_names(ast.parse(path.read_text(encoding="utf-8")))
        if any(factory in RECORDED_FACTORIES for factory in names.direct.values()):
            found.add(path.relative_to(REPO_ROOT).as_posix())
    return found


def test_recorder_rebinds_every_from_importing_module(scratch_recorder) -> None:
    """The fixture's reach equals the from-import sites the scan resolves.

    Arm 2 recognises a from-imported factory; Arm 1 only observes one if the
    fixture rebinds it on the importing module. This pins the two together, so
    the fixture cannot be narrower than the scan.

    Vacuously green today -- no adapter uses a from-import, so both sides are
    empty -- and stated rather than hidden, following the empty allowlist above:
    it lands armed, and the first adapter to from-import a factory reds it
    unless the fixture reaches that module. The mechanism itself is pinned
    unconditionally by ``test_recorder_reaches_a_from_imported_factory``, which
    drives a synthetic module and does not depend on a live site existing.
    """
    assert scratch_recorder.rebound_modules == _modules_with_direct_imports()


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
    # Import spellings. None is live in the adapter package today -- all three
    # adapters write ``import tempfile`` -- so nothing but these cases keeps the
    # resolver's branches alive, and without them Arm 2's guarantee would rest on
    # a spelling convention no lint enforces.
    "from_import": ("from tempfile import mkdtemp\ndef f():\n    return mkdtemp()\n", True),
    "from_import_aliased": (
        "from tempfile import mkdtemp as md\ndef f():\n    return md()\n",
        True,
    ),
    "module_aliased": ("import tempfile as tf\ndef f():\n    return tf.mkdtemp()\n", True),
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
    "bare_name_without_the_import": ("def f(mkdtemp):\n    return mkdtemp()\n", False),
    "same_attr_on_another_object": (
        "import tempfile\ndef f(helper):\n    return helper.mkdtemp()\n",
        False,
    ),
    "shadowed_name_from_another_module": (
        "from shutil import rmtree as mkdtemp\ndef f():\n    return mkdtemp()\n",
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
    names = _tempfile_names(tree)
    found = any(_scratch_factory(node, names) is not None for node in ast.walk(tree))
    assert found is expected
