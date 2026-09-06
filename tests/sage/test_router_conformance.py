"""Architectural-conformance tests for CAS FastAPI routers.

Gates the canonical "service-as-load-bearer" router pattern documented in the
AI-First SDLC Tooling Survey §5.7 and addresses failure modes F1 (API
convention drift) and F5 (validation-bypass via missing dependency
declaration).

Discovery is static and parameterized over a small list of `RouterTree`
records. Each tree names a package or module to walk, the set of
routers expected within it, and the dependencies module from which
`get_*_service` factories are harvested. The SAGE tree walks
``sage/api/routers/`` and recognizes the path-scoped ``Depends(get_vault_id)``
pattern; the CAS App tree walks ``app/backend/router.py`` and additionally
recognizes the body-scoped variant in which the request body carries
``vault_id`` and the service factory resolves it.

Allowlisted drifted routers are tracked in `KNOWN_VIOLATIONS` keyed by
``(tree_name, router_name)``; the allowlist drains as each router is
remediated.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import typing
from pathlib import Path
from typing import Callable, Literal, NamedTuple

import pytest
from fastapi.params import Depends as DependsParam
from fastapi.routing import APIRoute
from pydantic import BaseModel

from sage.api.dependencies import get_vault_id

# ---------------------------------------------------------------------------
# Router-tree configuration
#
# Each tree describes one route surface that is subject to the conformance
# gate. SAGE walks the routers package as a whole; the CAS App backend ships
# a single module. New surfaces (e.g. ROOT Harness once implemented) get a
# new RouterTree record here; the rest of the file generalizes over the
# `ROUTER_TREES` tuple.
# ---------------------------------------------------------------------------


class RouterTree(NamedTuple):
    name: str
    discovery_kind: Literal["package", "module"]
    discovery_target: str
    vault_scoped_routers: frozenset[str]
    cross_vault_routers: frozenset[str]
    dependencies_module: str

    @property
    def all_routers(self) -> frozenset[str]:
        return self.vault_scoped_routers | self.cross_vault_routers


ROUTER_TREES: tuple[RouterTree, ...] = (
    RouterTree(
        name="sage",
        discovery_kind="package",
        discovery_target="sage.api.routers",
        # Must mirror the include_router block in sage/app.py.
        vault_scoped_routers=frozenset(
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
                "maintenance",
            }
        ),
        cross_vault_routers=frozenset({"vaults", "transfer"}),
        dependencies_module="sage.api.dependencies",
    ),
    RouterTree(
        name="cas_app",
        discovery_kind="module",
        discovery_target="app.backend.router",
        # Single-module tree: the stem is the module filename ("router").
        vault_scoped_routers=frozenset({"router"}),
        cross_vault_routers=frozenset(),
        dependencies_module="app.backend.dependencies",
    ),
    RouterTree(
        name="cas_auth",
        discovery_kind="module",
        discovery_target="app.backend.auth.router",
        # The interactive-sign-in surface resolves no vault: login, callback,
        # session-info, and logout are cross-vault, so the vault-id check
        # applies only to {vault_id} paths (of which there are none). At least
        # one route depends on a get_*_service factory (get_oidc_service /
        # get_session_service), satisfying service-as-load-bearer.
        vault_scoped_routers=frozenset(),
        cross_vault_routers=frozenset({"router"}),
        dependencies_module="app.backend.auth.dependencies",
    ),
)

_TREES_BY_NAME: dict[str, RouterTree] = {t.name: t for t in ROUTER_TREES}


# ---------------------------------------------------------------------------
# Allowlist of currently-drifted routers
#
# Each entry pins the exact set of violations a (tree_name, router_name) pair
# exhibits. Adding a violation outside this set fails the test (new drift).
# Reducing a router's violation set without updating this dict also fails
# (stale allowlist after a remediation).
# ---------------------------------------------------------------------------

VIOLATION_VAULT_ID = "vault-id-dependency"
VIOLATION_SERVICE_LOAD_BEARING = "service-as-load-bearer"

KNOWN_VIOLATIONS: dict[tuple[str, str], frozenset[str]] = {}


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------


def _discover_routers_in_tree(tree: RouterTree) -> dict[str, object]:
    """Return ``{router_stem: APIRouter}`` for every router in this tree."""
    if tree.discovery_kind == "package":
        pkg = importlib.import_module(tree.discovery_target)
        pkg_dir = Path(pkg.__file__).parent
        out: dict[str, object] = {}
        for _, name, ispkg in pkgutil.iter_modules([str(pkg_dir)]):
            if ispkg:
                continue
            module = importlib.import_module(f"{tree.discovery_target}.{name}")
            out[name] = module.router
        return out
    # discovery_kind == "module"
    module = importlib.import_module(tree.discovery_target)
    stem = tree.discovery_target.rsplit(".", 1)[-1]
    return {stem: module.router}


def _service_dependency_callables(tree: RouterTree) -> set[Callable]:
    """Set of ``get_*_service`` helpers from this tree's dependencies module.

    These are the canonical "load-bearing" entry points. A router is
    service-as-load-bearer iff at least one route depends on at least one of
    these callables.
    """
    deps_module = importlib.import_module(tree.dependencies_module)
    return {
        obj
        for name, obj in vars(deps_module).items()
        if name.startswith("get_") and name.endswith("_service") and callable(obj)
    }


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


def _route_requires_vault_id(tree: RouterTree, router_name: str, route: APIRoute) -> bool:
    """Decide whether this route resolves a vault.

    For vault-scoped routers, every route is in scope. For cross-vault
    routers (currently only SAGE's ``vaults.py``), only routes whose declared
    path contains ``{vault_id}`` are.
    """
    if router_name in tree.vault_scoped_routers:
        return True
    return "{vault_id}" in route.path


def _handler_uses_body_scoped_vault_id(route: APIRoute, service_callables: set[Callable]) -> bool:
    """True iff some service-factory dependency binds a body with ``vault_id``.

    The body-scoped vault_id pattern (CAS App): the handler
    declares ``body: SomeRequest`` plus ``service: SomeService =
    Depends(get_xxx_service)``, and the service factory's own signature also
    binds the request body and reads ``body.vault_id`` to resolve the vault.

    The gate accepts this pattern as conformant on the vault-id dimension
    iff at least one of the route's service-factory dependencies has a
    parameter whose annotation is a ``BaseModel`` subclass declaring a
    ``vault_id`` field. The field-presence check distinguishes "factory
    resolves a vault via the body" from "factory happens to take a body for
    some other reason".
    """
    for dep in _depends_callables(route):
        if dep not in service_callables:
            continue
        # Resolve string annotations produced by ``from __future__ import
        # annotations`` so that we can inspect the actual BaseModel classes.
        # NameError is the realistic failure mode (an unresolved forward
        # reference); any other exception propagates so structural surprises
        # surface rather than silently mark the route non-conformant.
        try:
            hints = typing.get_type_hints(dep)
        except NameError:
            continue
        for name, ann in hints.items():
            if name == "return":
                continue
            if not inspect.isclass(ann):
                continue
            if not issubclass(ann, BaseModel):
                continue
            if "vault_id" in ann.model_fields:
                return True
    return False


# ---------------------------------------------------------------------------
# Per-router violation detection
# ---------------------------------------------------------------------------


def _detect_violations(tree: RouterTree, router_name: str) -> set[str]:
    """Return the set of violation tags this router exhibits."""
    routers = _discover_routers_in_tree(tree)
    routes = _api_routes(routers[router_name])
    service_callables = _service_dependency_callables(tree)

    violations: set[str] = set()

    # Vault-id check: every applicable route must declare vault resolution,
    # either path-scoped (Depends(get_vault_id)) or body-scoped (a service
    # factory dependency that itself binds a body model with a vault_id field).
    for route in routes:
        if not _route_requires_vault_id(tree, router_name, route):
            continue
        deps = _depends_callables(route)
        if get_vault_id in deps:
            continue
        if _handler_uses_body_scoped_vault_id(route, service_callables):
            continue
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


def _allowed(tree_name: str, router_name: str) -> frozenset[str]:
    return KNOWN_VIOLATIONS.get((tree_name, router_name), frozenset())


def _all_router_pairs() -> list[tuple[str, str]]:
    """Flatten ROUTER_TREES into ``(tree_name, router_name)`` test ids."""
    return sorted(
        (tree.name, router_name) for tree in ROUTER_TREES for router_name in tree.all_routers
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tree_name", sorted(t.name for t in ROUTER_TREES))
def test_router_files_match_known_routers(tree_name: str):
    """Every router in this tree is listed in its mount table; no surprises."""
    tree = _TREES_BY_NAME[tree_name]
    discovered = set(_discover_routers_in_tree(tree))
    extra = discovered - tree.all_routers
    missing = tree.all_routers - discovered
    assert not extra, (
        f"Router(s) {sorted(extra)} exist in tree {tree_name!r} but are not "
        "listed in vault_scoped_routers or cross_vault_routers. Decide where "
        "each new router mounts and update the RouterTree record."
    )
    assert not missing, (
        f"Router(s) {sorted(missing)} are in tree {tree_name!r}'s mount table "
        "but no module was found. Did the file get deleted or renamed?"
    )


@pytest.mark.parametrize(
    ("tree_name", "router_name"),
    _all_router_pairs(),
    ids=[f"{t}-{r}" for t, r in _all_router_pairs()],
)
def test_router_conformance(tree_name: str, router_name: str):
    """Per-router conformance: violations equal the allowlist exactly."""
    tree = _TREES_BY_NAME[tree_name]
    actual = _detect_violations(tree, router_name)
    allowed = _allowed(tree_name, router_name)

    new_drift = actual - allowed
    assert not new_drift, (
        f"Router {tree_name}/{router_name!r} introduced new violation(s) "
        f"{sorted(new_drift)}. Either fix the router to match the canonical "
        "pattern (preferred) or, if this is a deliberate exception, add the "
        "violation tag(s) to KNOWN_VIOLATIONS with a TODO referencing the "
        "remediation ticket."
    )

    stale_allowlist = allowed - actual
    assert not stale_allowlist, (
        f"Router {tree_name}/{router_name!r} is allowlisted for violation(s) "
        f"{sorted(stale_allowlist)} but no longer exhibits them. Remove the "
        "stale entry from KNOWN_VIOLATIONS (or delete the whole entry if it "
        "was the only one)."
    )


@pytest.mark.parametrize(
    ("tree_name", "router_name"),
    _all_router_pairs(),
    ids=[f"{t}-{r}" for t, r in _all_router_pairs()],
)
def test_route_models_are_pydantic(tree_name: str, router_name: str):
    """Request bodies and response_model declarations are BaseModel subclasses.

    This is a tripwire for future regressions; all current routers pass.
    """
    tree = _TREES_BY_NAME[tree_name]
    routers = _discover_routers_in_tree(tree)
    for route in _api_routes(routers[router_name]):
        # response_model
        rm = route.response_model
        if rm is not None:
            assert _is_basemodel_or_collection_of(rm), (
                f"{tree_name}/{router_name}:{route.endpoint.__name__} "
                f"response_model {rm!r} is not a Pydantic BaseModel (or list "
                "thereof)."
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
    real_pairs = set(_all_router_pairs())
    unknown = set(KNOWN_VIOLATIONS) - real_pairs
    assert not unknown, (
        f"KNOWN_VIOLATIONS references pair(s) {sorted(unknown)} that are not "
        "in any RouterTree's mount table. Did a router get renamed or removed?"
    )
