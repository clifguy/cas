"""Reproducibility guards for the pinned native-stack dependencies.

``lancedb`` and ``pyarrow`` are held to exact (``==``) pins in
``pyproject.toml`` so that local development, CI, and the deployed server
all resolve the same build. An unbounded ``>=`` floor lets a fresh resolve
float to a newer release, which can let a change pass locally and fail CI
(or leave the running server exercising a different build than ships).
These guards turn that skew into a red test instead of a silent divergence:

- the declared dependency must be a single exact pin, and
- the version installed in the active environment must satisfy that pin.

The set is deliberately narrow. The remaining dependencies keep
compatibility-range floors pending a project-wide lockfile; this guard
asserts nothing about them.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest
from packaging.requirements import Requirement

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

# Dependencies held to an exact pin for cross-environment reproducibility.
EXACT_PINNED = ("lancedb", "pyarrow")


def _project_dependencies() -> dict[str, Requirement]:
    """Parse ``[project].dependencies`` into ``{canonical_name: Requirement}``."""
    data = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    parsed: dict[str, Requirement] = {}
    for raw in data["project"]["dependencies"]:
        req = Requirement(raw)
        parsed[req.name.lower()] = req
    return parsed


@pytest.fixture(scope="module")
def dependencies() -> dict[str, Requirement]:
    return _project_dependencies()


@pytest.mark.parametrize("name", EXACT_PINNED)
def test_dependency_is_exact_pinned(name: str, dependencies: dict[str, Requirement]) -> None:
    """The dependency is declared as a single exact (``==``) pin.

    Regression guard against reverting to an unbounded ``>=`` floor -- the
    pattern that allowed local/CI version skew.
    """
    assert name in dependencies, (
        f"{name!r} is not declared in [project].dependencies; the pin guard "
        "cannot find it (renamed or removed?)."
    )
    clauses = list(dependencies[name].specifier)
    assert len(clauses) == 1 and clauses[0].operator == "==", (
        f"{name} must be an exact pin (== X.Y.Z), got "
        f"{str(dependencies[name].specifier)!r}. An unbounded or range "
        "spec reopens local/CI version skew."
    )


@pytest.mark.parametrize("name", EXACT_PINNED)
def test_installed_version_matches_pin(name: str, dependencies: dict[str, Requirement]) -> None:
    """The installed version satisfies the declared pin.

    Reproducibility guard: fails fast when the active environment (local
    venv or CI runner) has drifted from the pin in pyproject.toml.
    """
    assert name in dependencies, f"{name!r} is not declared in [project].dependencies."
    specifier = dependencies[name].specifier
    installed = version(name)
    assert specifier.contains(installed, prereleases=True), (
        f"installed {name} {installed} does not satisfy the pin "
        f"{str(specifier)!r}; reconcile the environment (reinstall) so it "
        "matches the declared pin."
    )
