"""Frontend router-package gate.

The CAS SPA's router dependency carries a security floor that nothing else in
the tree enforces. GHSA-qwww-vcr4-c8h2 (RSC-mode CSRF bypass) covers
``react-router`` ``>= 7.12.0, < 8.3.0``; 8.3.0 is the first patched release, so
no 7.x version clears it. React Router 8 also collapsed the DOM package into
the core one -- there is no published ``react-router-dom`` 8.x, and every
version of it that exists depends on a ``react-router`` inside the vulnerable
range. Reintroducing ``react-router-dom``, or floating ``react-router`` back
below 8.3.0, silently reopens the advisory.

Neither hazard is caught by the frontend toolchain. ``tsc -b`` and Vitest only
prove that whatever specifiers the source names can be resolved: they are
equally happy with a vulnerable tree, and equally happy with a manifest edit
that adds the DOM package back. ``npm audit`` would notice, but it is not a CI
gate and it needs the registry.

This gate reads the tracked manifest, lockfile, and source tree only (no
registry, no ``node_modules``) and asserts the four sites stay consistent, in
the structural-gate style of ``tests/infra/test_frontend_node_version.py``.
Each check proves it actually located its site (present key / non-empty package
map / non-empty file walk) first, so a parser that silently matches nothing
cannot pass vacuously.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

APP_PACKAGE_JSON: Final[Path] = REPO_ROOT / "app" / "package.json"
APP_PACKAGE_LOCK: Final[Path] = REPO_ROOT / "app" / "package-lock.json"
APP_SRC: Final[Path] = REPO_ROOT / "app" / "src"

# The advisory this floor exists to hold closed, named so a future reader can
# look up why the floor is where it is rather than treating it as a stale pin.
ROUTER_ADVISORY: Final[str] = "GHSA-qwww-vcr4-c8h2"

# First ``react-router`` release outside the advisory's vulnerable range. Raise
# this only to move forward; lowering it reopens the advisory.
MIN_REACT_ROUTER: Final[tuple[int, int]] = (8, 3)

# The package React Router 8 removed. Kept as a constant because three separate
# checks below look for it in three different shapes.
DOM_PACKAGE: Final[str] = "react-router-dom"

# A module specifier is always quoted, so matching the quoted literal finds
# every import form -- ``from '...'``, ``vi.mock('...')``, ``import('...')`` --
# without also flagging incidental prose mentions in a comment.
DOM_SPECIFIER_RE: Final[re.Pattern[str]] = re.compile(rf"['\"]{re.escape(DOM_PACKAGE)}['\"]")

_SOURCE_SUFFIXES: Final[tuple[str, ...]] = (".ts", ".tsx")


def _version_floor(spec: str) -> tuple[int, int]:
    """Return the ``(major, minor)`` floor of an npm semver range.

    Accepts the caret/tilde/exact forms the manifest uses (``'^8.3.0'``,
    ``'~8.3.0'``, ``'8.3.0'``). A spec whose leading component is not an
    integer raises ``ValueError`` -- a range we cannot resolve to a floor must
    fail loudly rather than resolve to a permissive default.
    """
    parts = str(spec).strip().lstrip("^~=v").split(".")
    return int(parts[0]), int(parts[1])


def _app_dependencies() -> tuple[dict[str, str], dict[str, str]]:
    """The SPA manifest's runtime and dev dependency maps."""
    package = json.loads(APP_PACKAGE_JSON.read_text(encoding="utf-8"))
    return package.get("dependencies") or {}, package.get("devDependencies") or {}


def _source_files() -> list[Path]:
    """Every TypeScript source file under the SPA's ``src`` tree."""
    return sorted(p for p in APP_SRC.rglob("*") if p.suffix in _SOURCE_SUFFIXES)


def test_react_router_floor_clears_the_rsc_csrf_advisory() -> None:
    """The ``react-router`` floor sits at or above the first patched release.

    Asserts the dependency is present before reading its floor, so a manifest
    that dropped the package entirely fails here instead of passing over a
    missing key.
    """
    deps, _ = _app_dependencies()
    assert "react-router" in deps, f"react-router not found in dependencies of {APP_PACKAGE_JSON}"
    floor = _version_floor(deps["react-router"])
    assert floor >= MIN_REACT_ROUTER, (
        f"react-router floor must be >= {MIN_REACT_ROUTER[0]}.{MIN_REACT_ROUTER[1]} to stay "
        f"clear of {ROUTER_ADVISORY}; got {deps['react-router']!r}."
    )


def test_react_router_dom_is_absent_from_the_manifest() -> None:
    """Neither dependency map declares the removed DOM package.

    Asserts the runtime map is non-empty first, so a manifest this gate failed
    to parse cannot pass by presenting two empty maps.
    """
    deps, dev_deps = _app_dependencies()
    assert deps, f"no dependencies parsed from {APP_PACKAGE_JSON}"
    declared_in = [
        name
        for name, mapping in (("dependencies", deps), ("devDependencies", dev_deps))
        if DOM_PACKAGE in mapping
    ]
    assert not declared_in, (
        f"{DOM_PACKAGE} is declared in {declared_in} of {APP_PACKAGE_JSON}; it has no 8.x "
        f"release and every published version resolves a react-router inside {ROUTER_ADVISORY}."
    )


def test_react_router_dom_is_absent_from_the_lockfile() -> None:
    """No lockfile entry resolves the removed DOM package.

    Covers what the manifest check cannot see: a transitive dependency pulling
    the package back into the installed tree.
    """
    lock = json.loads(APP_PACKAGE_LOCK.read_text(encoding="utf-8"))
    packages = lock.get("packages") or {}
    assert packages, f"no packages map parsed from {APP_PACKAGE_LOCK}"
    resolved = [key for key in packages if key.split("node_modules/")[-1] == DOM_PACKAGE]
    assert not resolved, (
        f"{APP_PACKAGE_LOCK} still resolves {DOM_PACKAGE} at {resolved}; "
        f"regenerate the lockfile after removing it from the manifest."
    )


def test_no_source_file_imports_react_router_dom() -> None:
    """No SPA source file names the removed DOM package as a module specifier.

    Asserts the walk found files first, so a mistyped source root fails loudly
    instead of reporting a clean scan over an empty set.
    """
    sources = _source_files()
    assert sources, f"no {'/'.join(_SOURCE_SUFFIXES)} files found under {APP_SRC}"
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in sources
        if DOM_SPECIFIER_RE.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        f"{len(offenders)} source file(s) still import {DOM_PACKAGE}: {offenders}. "
        f"React Router 8 serves every one of these APIs from 'react-router'."
    )
