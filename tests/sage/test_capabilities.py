"""Runtime OCR-capability probe tests.

``sage.capabilities.ocr_capability`` reports whether this process/container can
run the scanned-PDF OCR pre-pass: the ``ocrmypdf`` module is importable and the
``tesseract`` + ``ghostscript`` binaries are on ``PATH``. The cloud preflight
reads this over ``/health`` because that harness runs outside the container.

The probe must be *computed*, not hardcoded: a container built without the OCR
toolchain must report the relevant key ``False``. These tests drive each probe
input independently so a helper that returned a constant all-true dict would
fail the false-path cases.
"""

import sage.capabilities as capabilities
from sage.capabilities import ocr_capability


def test_ocr_capability_all_present(monkeypatch) -> None:
    # ocrmypdf importable + both binaries on PATH -> every key True.
    monkeypatch.setattr(capabilities, "find_spec", lambda name: object())
    monkeypatch.setattr(capabilities, "which", lambda name: f"/usr/bin/{name}")
    assert ocr_capability() == {
        "ocrmypdf": True,
        "tesseract": True,
        "ghostscript": True,
    }


def test_ocr_capability_ocrmypdf_absent(monkeypatch) -> None:
    # find_spec("ocrmypdf") -> None means the [ocr] extra is not installed.
    monkeypatch.setattr(capabilities, "find_spec", lambda name: None)
    monkeypatch.setattr(capabilities, "which", lambda name: f"/usr/bin/{name}")
    caps = ocr_capability()
    assert caps["ocrmypdf"] is False
    assert caps["tesseract"] is True
    assert caps["ghostscript"] is True


def test_ocr_capability_tesseract_absent(monkeypatch) -> None:
    monkeypatch.setattr(capabilities, "find_spec", lambda name: object())
    monkeypatch.setattr(
        capabilities, "which", lambda name: None if name == "tesseract" else f"/usr/bin/{name}"
    )
    caps = ocr_capability()
    assert caps["tesseract"] is False
    assert caps["ocrmypdf"] is True
    assert caps["ghostscript"] is True


def test_ocr_capability_ghostscript_absent(monkeypatch) -> None:
    # ghostscript resolves via the `gs` binary name.
    monkeypatch.setattr(capabilities, "find_spec", lambda name: object())
    monkeypatch.setattr(
        capabilities, "which", lambda name: None if name == "gs" else f"/usr/bin/{name}"
    )
    caps = ocr_capability()
    assert caps["ghostscript"] is False
    assert caps["ocrmypdf"] is True
    assert caps["tesseract"] is True


def test_ocr_capability_values_are_bools() -> None:
    # Real environment: values must be plain bools (JSON-serializable on /health),
    # never the truthy ModuleSpec / path string the probes return internally.
    caps = ocr_capability()
    assert set(caps) == {"ocrmypdf", "tesseract", "ghostscript"}
    assert all(isinstance(v, bool) for v in caps.values())
