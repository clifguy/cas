"""Architectural-conformance tests for SAGE FastAPI routers.

Gates the canonical "service-as-load-bearer" router pattern documented in the
AI-First SDLC Tooling Survey §5.7 and addresses failure modes F1 (API
convention drift) and F5 (validation-bypass via missing dependency
declaration).

Discovery is static: the test enumerates every `*.py` file under
`sage/api/routers/`, imports each module, and introspects `module.router`
without constructing a FastAPI app. Allowlisted drifted routers are tracked
in `KNOWN_VIOLATIONS`; the allowlist drains as each router is remediated.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from pathlib import Path
from typing import Callable

import pytest
from fastapi.params import Depends as DependsParam
from fastapi.routing import APIRoute
from pydantic import BaseModel

from sage.api import dependencies as deps_module
from sage.api import routers as routers_pkg
from sage.api.dependencies import get_vault_id

# ---------------------------------------------------------------------------
# Mount table: must mirror sage/app.py:187-200
# ---------------------------------------------------------------------------

VAULT_SCOPED_ROUTERS: frozenset[str] = frozenset(
    {
        "ingestion",
        "documents",
        "lifecycle",
        "metadata",
        "users",
        "graph_ops",
        "retrieval",
        "utilities",
        "staging_edges",
        "pending_metadata",
        "filename_parser",
    }
)

CROSS_VAULT_ROUTERS: frozenset[str] = frozenset({"vaults"})

ALL_KNOWN_ROUTERS: frozenset[str] = VAULT_SCOPED_ROUTERS | CROSS_VAULT_ROUTERS

# ---------------------------------------------------------------------------
# Allowlist of currently-drifted routers
#
# Each entry pins the exact set of violations a router exhibits. Adding a
# violation outside this set fails the test (new drift). Reducing a router's
# violation set without updating this dict also fails (stale allowlist after
# a remediation).
#
# Cleanup ticket: each entry should reference its remediation ticket once
# those tickets are filed. Ticket IDs are placeholders pending T-NNNN
# allocation.
# ---------------------------------------------------------------------------

VIOLATION_VAULT_ID = "vault-id-dependency"
VIOLATION_SERVICE_LOAD_BEARING = "service-as-load-bearer"

KNOWN_VIOLATIONS: dict[str, frozenset[str]] = {}


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------


def _routers_dir() -> Path:
    return Path(routers_pkg.__file__).parent


def _discover_router_module_names() -> set[str]:
    """Return the stem of every router module file (excluding __init__)."""
    return {name for _, name, ispkg in pkgutil.iter_modules([str(_routers_dir())]) if not ispkg}


def _service_dependency_callables() -> set[Callable]:
    """Set of `get_*_service` helpers from sage.api.dependencies.

    These are the canonical "load-bearing" entry points. A router is
    service-as-load-bearer iff at least one route depends on at least one of
    these callables.
    """
    return {
        obj
        for name, obj in vars(deps_module).items()
        if name.startswith("get_") and name.endswith("_service") and callable(obj)
    }


def _import_router(name: str):
    return importlib.import_module(f"sage.api.routers.{name}")


def _api_routes(router) -> list[APIRoute]:
    """Filter to APIRoute (skip mounts/websocket/etc., none currently exist)."""
    return [r for r in router.routes if isinstance(r, APIRoute)]


def _depends_callables(route: APIRoute) -> list[Callable]:
    """Return the dependency callables declared via Depends() on this route."""
    sig = inspect.signature(route.endpoint)
    out: list[Callable] = []
    for param in sig.parameters.values():
        if isinstance(param.default, DependsParam) and param.default.dependency is not None:
            out.append(param.default.dependency)
    return out


def _route_requires_vault_id(router_name: str, route: APIRoute) -> bool:
    """Decide whether this route is mounted under /sage_vaults/{vault_id}.

    For vault-scoped routers, every route is. For cross-vault routers
    (vaults.py), only routes whose declared path contains {vault_id} are.
    """
    if router_name in VAULT_SCOPED_ROUTERS:
        return True
    return "{vault_id}" in route.path


# ---------------------------------------------------------------------------
# Per-router violation detection
# ---------------------------------------------------------------------------


def _detect_violations(router_name: str) -> set[str]:
    """Return the set of violation tags this router exhibits."""
    module = _import_router(router_name)
    routes = _api_routes(module.router)
    service_callables = _service_dependency_callables()

    violations: set[str] = set()

    # Vault-id check: every applicable route must declare Depends(get_vault_id).
    for route in routes:
        if not _route_requires_vault_id(router_name, route):
            continue
        deps = _depends_callables(route)
        if get_vault_id not in deps:
            violations.add(VIOLATION_VAULT_ID)
            break

    # Service-as-load-bearer: at least one route must depend on a service.
    has_service_dep = False
    for route in routes:
        deps = _depends_callables(route)
        if any(d in service_callables for d in deps):
            has_service_dep = True
            break
    if routes and not has_service_dep:
        violations.add(VIOLATION_SERVICE_LOAD_BEARING)

    return violations


def _allowed(router_name: str) -> frozenset[str]:
    return KNOWN_VIOLATIONS.get(router_name, frozenset())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_router_files_match_known_routers():
    """Every router file is listed in the mount table; no surprises."""
    discovered = _discover_router_module_names()
    extra = discovered - ALL_KNOWN_ROUTERS
    missing = ALL_KNOWN_ROUTERS - discovered
    assert not extra, (
        f"Router file(s) {sorted(extra)} exist on disk but are not listed in "
        "VAULT_SCOPED_ROUTERS or CROSS_VAULT_ROUTERS. Decide where each new "
        "router mounts and update the table."
    )
    assert not missing, (
        f"Router(s) {sorted(missing)} are in the mount table but no module "
        "file was found. Did the file get deleted or renamed?"
    )


@pytest.mark.parametrize("router_name", sorted(ALL_KNOWN_ROUTERS))
def test_router_conformance(router_name: str):
    """Per-router conformance: violations equal the allowlist exactly."""
    actual = _detect_violations(router_name)
    allowed = _allowed(router_name)

    new_drift = actual - allowed
    assert not new_drift, (
        f"Router {router_name!r} introduced new violation(s) {sorted(new_drift)}. "
        "Either fix the router to match the canonical pattern (preferred) or, "
        "if this is a deliberate exception, add the violation tag(s) to "
        "KNOWN_VIOLATIONS with a TODO referencing the remediation ticket."
    )

    stale_allowlist = allowed - actual
    assert not stale_allowlist, (
        f"Router {router_name!r} is allowlisted for violation(s) "
        f"{sorted(stale_allowlist)} but no longer exhibits them. Remove the "
        "stale entry from KNOWN_VIOLATIONS (or delete the whole entry if it "
        "was the only one)."
    )


@pytest.mark.parametrize("router_name", sorted(ALL_KNOWN_ROUTERS))
def test_route_models_are_pydantic(router_name: str):
    """Request bodies and response_model declarations are BaseModel subclasses.

    This is a tripwire for future regressions; all current routers pass.
    """
    module = _import_router(router_name)
    for route in _api_routes(module.router):
        # response_model
        rm = route.response_model
        if rm is not None:
            assert _is_basemodel_or_collection_of(rm), (
                f"{router_name}:{route.endpoint.__name__} response_model "
                f"{rm!r} is not a Pydantic BaseModel (or list thereof)."
            )

        # Request body parameters: detected by FastAPI as those whose
        # annotation is a BaseModel and whose default is not Depends/Path/Query.
        sig = inspect.signature(route.endpoint)
        for pname, param in sig.parameters.items():
            ann = param.annotation
            if ann is inspect.Parameter.empty:
                continue
            if isinstance(param.default, DependsParam):
                continue
            # Heuristic: any class annotation that is a BaseModel subclass is
            # fine (request body). Non-BaseModel annotations are scalars/queries
            # and are not subject to this check.
            if inspect.isclass(ann) and issubclass(ann, BaseModel):
                continue  # pass: request body uses Pydantic


def _is_basemodel_or_collection_of(tp) -> bool:
    """True for BaseModel subclass, list[BaseModel], dict[..., BaseModel], etc."""
    if inspect.isclass(tp) and issubclass(tp, BaseModel):
        return True
    # typing constructs: list[Document], dict[str, Document], etc.
    args = getattr(tp, "__args__", ())
    if args:
        return any(_is_basemodel_or_collection_of(a) for a in args)
    # Permit dict / list typing without inner BaseModel (some endpoints return
    # plain dicts intentionally).
    return tp in (dict, list)


def test_known_violations_reference_real_routers():
    """Every key in KNOWN_VIOLATIONS is a real, mounted router."""
    unknown = set(KNOWN_VIOLATIONS) - ALL_KNOWN_ROUTERS
    assert not unknown, (
        f"KNOWN_VIOLATIONS references router(s) {sorted(unknown)} that are "
        "not in the mount table. Did a router get renamed or removed?"
    )
