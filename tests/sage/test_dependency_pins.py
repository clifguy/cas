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
from typing import Final

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
UV_LOCK_PATH = REPO_ROOT / "uv.lock"
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Native-stack packages whose installed build must match the lock exactly.
# Each carries a compiled native extension where a version float silently
# changes behaviour (the local/CI skew this guard exists to catch); ``torch``
# is included because its CPU build is index-routed and especially skew-prone.
# The Postgres serving stack (binary libpq driver, its async pool, and the
# pgvector type adapter) joined when the local profile's durable-storage
# binding flipped to the Postgres adapters; ``lancedb``/``pyarrow`` dropped
# when the embedded fallback binding was retired.
LOCK_TRACKED = ("torch", "psycopg", "psycopg-pool", "pgvector")

# Minimum resolved versions carrying a published fix for a security advisory
# remediated in this repository, keyed by package with the advisory id that
# motivated the floor. ``pyproject.toml`` declares no floor for these -- they
# are transitive, or their direct floor is deliberately permissive -- so the
# lockfile is the only place the remediation is recorded, and a later
# ``uv lock --upgrade`` or a revert could otherwise drop back below a fixed
# version with nothing to catch it. This is a *minimum* guard and deliberately
# distinct from ``LOCK_TRACKED`` above, which asserts exact installed-matches-lock
# equality for the native stack: a floor must keep holding as versions move
# forward, so conflating the two would make every future bump require a
# ``uv sync`` before the suite could pass.
SECURITY_FLOORS: Final[dict[str, tuple[str, str]]] = {
    "aiohttp": ("3.14.3", "GHSA-cq5v-8q36-5273"),
    "cryptography": ("50.0.0", "GHSA-g6cj-pr64-35w5"),
}


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


def _direct_dependency_specifier(name: str) -> SpecifierSet | None:
    """Return the version specifier for a direct ``[project.dependencies]``
    entry, or ``None`` if the package is not declared directly."""
    for raw in _pyproject()["project"]["dependencies"]:
        req = Requirement(raw)
        if req.name.lower() == name:
            return req.specifier
    return None


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


def test_security_floor_packages_are_locked(locked: dict[str, set[str]]) -> None:
    """Every package carrying a security floor is present in the lock.

    Vacuity guard for ``test_security_floor_holds``: if a package leaves the
    resolution entirely -- e.g. ``aiohttp`` disappears when the
    ``azure-core[aio]`` extra is dropped -- that test's parametrization has
    nothing left to compare and would report a pass. Assert presence
    separately so a genuine removal fails loudly as "no longer locked",
    distinct from "locked but below the floor".
    """
    missing = sorted(name for name in SECURITY_FLOORS if name not in locked)
    assert not missing, (
        f"security-floor packages absent from uv.lock: {missing}. If a package "
        "legitimately left the dependency tree, drop its SECURITY_FLOORS entry "
        "deliberately -- do not let the floor check pass vacuously."
    )


@pytest.mark.parametrize(
    ("name", "floor", "advisory"),
    [(name, floor, advisory) for name, (floor, advisory) in SECURITY_FLOORS.items()],
    ids=list(SECURITY_FLOORS),
)
def test_security_floor_holds(
    name: str, floor: str, advisory: str, locked: dict[str, set[str]]
) -> None:
    """Every resolved version of a remediated package is at or above its floor.

    Regression guard: these floors are not expressible in ``pyproject.toml``
    (the packages are transitive, or carry a deliberately permissive direct
    floor), so without this check a resolve that walks one of them backwards
    silently reintroduces a known-vulnerable version.

    Checks *every* version the lock resolves for the package, not just one.
    ``_locked_versions`` returns a set because a package can appear under
    several ``[[package]]`` blocks differentiated by ``resolution-markers`` --
    ``torch`` resolves to a PyPI wheel and an index-routed ``+cpu`` wheel, and
    a package reached as its dependency inherits the same split. Comparing a
    single element would let one platform's resolution sit below the floor
    undetected.

    Comparison is by ``packaging.version.Version``, not string ordering:
    ``"3.9.0" >= "3.14.3"`` is True as strings, which would be a false pass on
    a real downgrade.
    """
    assert Version("3.9.0") < Version("3.14.3"), (
        "sanity check on the comparison semantics in force: this ordering is "
        "True under string comparison and must be False under version "
        "comparison, so its failure means the guard below is not doing what "
        "it claims."
    )
    assert name in locked, f"{name!r} is not pinned in uv.lock."
    below = sorted(v for v in locked[name] if Version(v) < Version(floor))
    assert not below, (
        f"uv.lock resolves {name} at {below}, below the {floor} security floor "
        f"({advisory}). Raise it with `uv lock --upgrade-package {name}`; do not "
        "lower the floor."
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


def test_transformers_upper_bound_pinned() -> None:
    """``transformers`` is a direct dependency carrying a ``< 5.12`` ceiling.

    Forward-looking guard: transformers 5.12 removes
    ``get_extended_attention_mask``, which nomic-embed-text's remote modeling
    code still calls at load time -- crossing it would be a startup
    ``AttributeError``. transformers is otherwise only transitive (via
    sentence-transformers / mlx-lm), so the bound must be declared directly to
    apply. The ceiling is asserted by what it admits/excludes rather than a
    literal string, so a reformat that keeps the bound still passes; a future
    edit that drops or loosens it past 5.12.0 fails here.
    """
    spec = _direct_dependency_specifier("transformers")
    assert spec is not None, (
        "transformers must be a direct [project.dependencies] entry to carry "
        "the < 5.12 upper bound (a transitive-only dep cannot pin it)."
    )
    assert spec.contains("5.11.0"), (
        f"transformers specifier {spec} must admit 5.11.x (the bound is an "
        "upper ceiling, not an exact pin)."
    )
    assert not spec.contains("5.12.0"), (
        f"transformers specifier {spec} must exclude 5.12.0 -- the release that "
        "removes get_extended_attention_mask."
    )
