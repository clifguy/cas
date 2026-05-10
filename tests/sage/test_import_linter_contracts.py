"""Run import-linter contracts as a pytest test.

Contracts are configured under [tool.importlinter] in pyproject.toml.
Initial scope: enforce the boundary rule that services do not import from
routers (CAS-ADR-005 principle 5; SDLC survey §5.7).

Implementation invokes the lint-imports console script via subprocess so we
are robust against the project's library API churn between versions.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _find_lint_imports() -> Path | None:
    """Locate the lint-imports console script alongside the active python."""
    candidate = Path(sys.executable).parent / "lint-imports"
    return candidate if candidate.exists() else None


def test_import_linter_contracts_pass():
    cmd = _find_lint_imports()
    if cmd is None:
        pytest.fail(
            "lint-imports console script not found next to the active "
            f"interpreter ({sys.executable}); install the test extras with "
            '`pip install -e ".[test]"`.'
        )

    result = subprocess.run(
        [str(cmd)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "import-linter contract violation:\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
