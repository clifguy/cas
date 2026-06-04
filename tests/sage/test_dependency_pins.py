"""Reproducibility guards anchored on the committed ``uv.lock`` lockfile.

``uv.lock`` is the single source of truth for the fully-resolved dependency
set; ``pyproject.toml`` carries only abstract compatibility ranges (floors,
plus ceilings where a real incompatibility is known). An unbounded ``>=``
floor lets a fresh resolve float to a newer release, which can let a change
pass locally and fail CI (or leave the running server exercising a different
build than ships). The lockfile freezes one resolution that every
environment installs identically; these guards assert that the lock is in
place, that the active environment matches it for the native-stack packages
most prone to that skew, and that the torch CPU-index wiring stays wired.

CI additionally runs ``uv lock --check`` (lock-vs-pyproject freshness) and
``uv sync --locked`` (install exactly from the lock); these pytest guards
are the fast local signal that the active environment matches the lock.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest
from packaging.requirements import Requirement

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
UV_LOCK_PATH = REPO_ROOT / "uv.lock"
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Native-stack packages whose installed build must match the lock exactly.
# Each carries a compiled native extension where a version float silently
# changes behaviour (the local/CI skew this guard exists to catch); ``torch``
# is included because its CPU build is index-routed and especially skew-prone.
LOCK_TRACKED = ("lancedb", "pyarrow", "torch")


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))


def _locked_versions() -> dict[str, set[str]]:
    """Parse ``uv.lock`` into ``{canonical_name: {version, ...}}``.

    A package can appear under more than one ``[[package]]`` entry when it
    resolves to platform-specific builds -- ``torch`` resolves to the PyPI
    wheel on macOS and the ``+cpu`` index wheel on Linux, so it has two
    entries differentiated by ``resolution-markers``. Collect every resolved
    version per name so the env-match check holds on whichever platform runs.

    The root project carries a VCS-derived dynamic version, which uv omits
    from the lock entirely (the ``[[package]]`` block has no ``version``
    field); such version-less entries have no pinned version to track and are
    skipped.
    """
    data = tomllib.loads(UV_LOCK_PATH.read_text(encoding="utf-8"))
    versions: dict[str, set[str]] = {}
    for pkg in data["package"]:
        pkg_version = pkg.get("version")
        if pkg_version is None:
            continue
        versions.setdefault(pkg["name"].lower(), set()).add(pkg_version)
    return versions


def _project_dependency_names() -> set[str]:
    deps = _pyproject()["project"]["dependencies"]
    return {Requirement(raw).name.lower() for raw in deps}


def test_uv_lock_present_and_parseable() -> None:
    """The committed lockfile exists at the repo root and carries packages.

    Regression guard: the lock is the source of truth for the resolved
    dependency set. Its absence or corruption must fail loudly rather than
    silently fall back to a floating resolve.
    """
    assert UV_LOCK_PATH.is_file(), (
        f"uv.lock not found at {UV_LOCK_PATH}; generate it with `uv lock` and "
        "commit it -- it is the source of truth for resolved dependencies."
    )
    assert _locked_versions(), "uv.lock parsed but declares no [[package]] entries."


@pytest.fixture(scope="module")
def locked() -> dict[str, set[str]]:
    return _locked_versions()


@pytest.mark.parametrize("name", LOCK_TRACKED)
def test_installed_matches_lock(name: str, locked: dict[str, set[str]]) -> None:
    """The installed build of a native-stack package matches the lock.

    Reproducibility guard: fails fast when the active environment (local
    venv or CI runner) has drifted from ``uv.lock`` -- the local/CI skew
    this whole mechanism exists to prevent. The installed version must be
    one of the versions the lock pins for this package (more than one when
    the package has platform-specific builds, e.g. torch).
    """
    assert name in locked, f"{name!r} is not pinned in uv.lock; expected it in the resolved set."
    installed = version(name)
    assert installed in locked[name], (
        f"installed {name} {installed} is not among the uv.lock pins "
        f"{sorted(locked[name])}; run `uv sync --locked` to reconcile the "
        "environment with the lockfile."
    )


def test_torch_is_direct_dependency() -> None:
    """``torch`` is declared in ``[project.dependencies]``.

    Mechanism guard: ``[tool.uv.sources]`` only routes *direct* dependencies.
    If ``torch`` reverts to transitive-only (pulled in by another package)
    the CPU-index routing silently stops applying and Linux installs float to
    the CUDA build. Keep torch direct so the source pin stays in force.
    """
    assert "torch" in _project_dependency_names(), (
        "torch must be a direct dependency so [tool.uv.sources] can route it "
        "through the pytorch-cpu index on Linux."
    )


def test_pytorch_cpu_index_wired() -> None:
    """The torch CPU index and source routing are present in pyproject.

    Mechanism guard against accidental removal of the
    ``[[tool.uv.index]] name = "pytorch-cpu"`` / ``[tool.uv.sources].torch``
    wiring that pins the reproducible CPU build of torch.
    """
    uv_cfg = _pyproject().get("tool", {}).get("uv", {})
    indexes = uv_cfg.get("index", [])
    assert any(idx.get("name") == "pytorch-cpu" for idx in indexes), (
        "expected a [[tool.uv.index]] entry named 'pytorch-cpu'."
    )
    torch_sources = uv_cfg.get("sources", {}).get("torch", [])
    assert any(src.get("index") == "pytorch-cpu" for src in torch_sources), (
        "expected [tool.uv.sources].torch to route to the 'pytorch-cpu' index."
    )


def test_ci_installs_from_lockfile() -> None:
    """CI installs from the lockfile and does not pip-install the project.

    Regression guard: a floating ``pip install -e`` in CI reopens the
    local/CI skew. CI must install via ``uv sync --locked`` (which fails on a
    stale lock) instead.
    """
    ci_text = CI_WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "uv sync --locked" in ci_text, (
        "CI must install dependencies via `uv sync --locked` (lock-pinned)."
    )
    assert "pip install -e" not in ci_text, (
        "CI must not `pip install -e` the project -- that bypasses uv.lock."
    )
