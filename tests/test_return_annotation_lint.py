"""Exercise the return-annotation gate through the repository's Ruff configuration."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_CASES = [
    ("def public(){}:\n    return 1\n", " -> int", "ANN201"),
    ("def _private(){}:\n    return 1\n", " -> int", "ANN202"),
    ("class Example:\n    def __str__(self){}:\n        return 'example'\n", " -> str", "ANN204"),
    (
        "class Example:\n    @staticmethod\n    def build(){}:\n        return 1\n",
        " -> int",
        "ANN205",
    ),
    (
        "class Example:\n    @classmethod\n    def build(cls){}:\n        return 1\n",
        " -> int",
        "ANN206",
    ),
    (
        "def outer() -> None:\n    def nested(){}:\n        return 1\n    nested()\n",
        " -> int",
        "ANN202",
    ),
    ("async def public(){}:\n    return 1\n", " -> int", "ANN201"),
    (
        "from collections.abc import Iterator\n\n\ndef values(){}:\n    yield 1\n",
        " -> Iterator[int]",
        "ANN201",
    ),
    (
        "from collections.abc import AsyncIterator\n\n\nasync def values(){}:\n    yield 1\n",
        " -> AsyncIterator[int]",
        "ANN201",
    ),
]


def _lint(source: str, filename: str) -> tuple[int, set[str]]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--config",
            str(_ROOT / "pyproject.toml"),
            "--output-format",
            "json",
            "--stdin-filename",
            str(_ROOT / filename),
            "-",
        ],
        input=source,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in (0, 1), result.stderr
    diagnostics = json.loads(result.stdout)
    return result.returncode, {item["code"] for item in diagnostics}


@pytest.mark.parametrize("template,annotation,rule", _CASES)
@pytest.mark.parametrize("filename", ["sage/lint_probe.py", "sage/services/lint_probe.py"])
def test_return_annotations_required(template, annotation, rule, filename):
    # The unannotated arm must fail with the signature's own rule, and adding
    # its annotation must clear that diagnostic under the same configuration.
    source = template.format("")
    # Generator imports are only consumed once annotated; the baseline F401
    # remains enabled, so remove the import in the unannotated arm.
    if source.startswith("from collections.abc import"):
        source = source.split("\n\n", 1)[1]
    assert _lint(source, filename) == (1, {rule})
    assert _lint(template.format(annotation), filename) == (0, set())


@pytest.mark.parametrize(
    "filename", ["tests/lint_probe.py", "scripts/lint_probe.py", "app/probe.py"]
)
def test_return_annotation_scope_excludes_other_trees(filename):
    assert _lint("def public():\n    return 1\n", filename) == (0, set())


def test_return_gate_allows_any_and_unannotated_parameters():
    source = "from typing import Any\n\n\ndef public(value) -> Any:\n    return value\n"
    assert _lint(source, "sage/lint_probe.py") == (0, set())
