"""Runtime capability probes for optional subsystems.

These report whether an optional, environment-provided subsystem is actually
available in *this* running process/container -- as opposed to merely declared
in configuration. The OCR pre-pass, for example, needs both a Python package
(``ocrmypdf``, from the optional ``[ocr]`` extra) and two system binaries
(``tesseract``, ``ghostscript``); an image built without either cannot run it.

The signal is surfaced on ``/health`` so an out-of-container probe (the cloud
preflight harness runs on a CI runner, over public HTTPS) can verify the running
image ships the toolchain, rather than discovering the gap only when a scanned
document fails to ingest.

The probes are deliberately cheap and import-free: ``find_spec`` locates a module
without importing it, so a truthy result adds no resident memory (``ocrmypdf`` is
still imported lazily, only when a scanned document is actually processed), and
``which`` is a ``PATH`` lookup.
"""

from importlib.util import find_spec
from shutil import which


def ocr_capability() -> dict[str, bool]:
    """Whether the scanned-PDF OCR toolchain is available in this process.

    Returns a flat, JSON-serializable map: ``ocrmypdf`` (the Python package is
    importable), ``tesseract`` and ``ghostscript`` (the binaries are on
    ``PATH``; ghostscript resolves via the ``gs`` command). All three must be
    true for the OCR pre-pass to run.
    """
    return {
        "ocrmypdf": find_spec("ocrmypdf") is not None,
        "tesseract": which("tesseract") is not None,
        "ghostscript": which("gs") is not None,
    }
