"""Conformance gate: typed-alias coverage at three boundary site classes.

Cites the CAS Typed-Alias Boundary Conventions steering document (cas
SAGE vault, doc_type=steering_document). A typed alias is a
``pydantic.Annotated[str, AfterValidator(...)]`` declared in
``sage/models/schemas.py``. Three site classes are governed:

1. **Pydantic ``BaseModel`` fields** in modules listed in
   ``_SCOPED_MODULES``. Every shape-bearing field must carry the
   alias or be pinned in ``KNOWN_VIOLATIONS``. (T-0028.)
2. **FastAPI route parameters** in the routers listed in
   ``_FASTAPI_ROUTER_MODULES``. Pattern 1: the alias goes directly on
   the parameter annotation. ``KNOWN_FASTAPI_VIOLATIONS`` is the
   allowlist. (T-0035.)
3. **FastMCP tool entry points** registered on the FastMCP server.
   Pattern 2: signature stays bare ``str``; a module-scope
   ``TypeAdapter[<Alias>]`` is constructed and the tool body calls
   ``<adapter>.validate_python(<param>)`` against each shape-bearing
   parameter. ``pydantic_core.ValidationError`` extends ``ValueError``
   so the existing ``except (SAGEError, ValueError)`` block routes
   shape failures through the SAGE error envelope.
   ``KNOWN_FASTMCP_VIOLATIONS`` is the allowlist. (T-0035.)

Allowlist contract — uniform across all three site classes:

- Adding a new bare-``str`` shape-bearing field or parameter without
  an allowlist entry fails the suite (new-drift).
- Removing an allowlist entry without remediating fails the suite.
- Typing a previously-allowlisted site without removing the entry
  fails the suite (stale-allowlist).
- Every allowlist entry must correspond to a real shape-bearing site
  (phantom-entry check).

Scope — F4 scan, still-parallel site classes:

- ``root_harness/`` does not exist yet; future BaseModels, routes, or
  tool entry points there are expected to follow the same convention
  and join the appropriate scope tuple.
- No CLI argparse handlers exist in this codebase today. If added,
  their argparse ``type=`` callables should validate against the
  typed alias (e.g., ``type=lambda v: VaultIdStr_ADAPTER.validate_python(v)``)
  and a fourth site-class walker should join this file.

Scope — not yet typeable (no alias exists):

- ISO timestamp strings (e.g. ``ScanResultResponse.source_modified_at``)
  — names do not match the current ``*_date`` suffix rule and no
  ``IsoTimestampStr`` alias exists.
- Filesystem paths (e.g. ``VaultIdentity.storage_root``,
  ``VaultIdentity.brain_root``, ``RetrievalHealthConfig.assertions_file``)
  — no path alias exists. The gate does not flag these today.

Drain plan: T-0026 typed the 22 currently-allowlisted BaseModel fields
whose aliases already existed; T-0027 introduced ``UserIdStr`` /
``VaultIdStr`` / ``FunctionIdStr`` and typed the remaining 5; T-0028
extended the gate to ``app.backend.router`` and ``sage.config`` and
typed the four shape-bearing fields the extension surfaced; T-0035
extended the gate to FastAPI route parameters and FastMCP tool entry
points and typed every shape-bearing parameter at those boundaries.
All three ``KNOWN_*_VIOLATIONS`` dicts are now empty.
"""

from __future__ import annotations

import ast
import inspect
import typing
from collections.abc import Callable
from types import ModuleType, UnionType
from typing import Final

import pytest
from fastapi.params import Depends as DependsParam
from pydantic import AfterValidator, BaseModel

from app.backend import models as models_mod
from app.backend import router as router_mod
from sage import config as config_mod

# Importing ``sage.mcp_server`` runs the module-level ``register_sage_tools``
# / ``register_app_tools`` calls, which decorate every tool function on the
# module-level ``mcp`` instance. After import, the registered tools are
# discoverable via ``mcp._tool_manager._tools``.
from sage import mcp_server as fastmcp_server_mod

# FastAPI router modules under the gate's scope. Each module exposes a
# module-level ``router: APIRouter`` whose registered route endpoints
# are walked for shape-bearing parameters.
from sage.api import dependencies as fastapi_deps_mod
from sage.api.routers import documents as fastapi_documents_mod
from sage.api.routers import filename_parser as fastapi_filename_parser_mod
from sage.api.routers import graph_ops as fastapi_graph_ops_mod
from sage.api.routers import ingestion as fastapi_ingestion_mod
from sage.api.routers import lifecycle as fastapi_lifecycle_mod
from sage.api.routers import metadata as fastapi_metadata_mod
from sage.api.routers import pending_metadata as fastapi_pending_metadata_mod
from sage.api.routers import retrieval as fastapi_retrieval_mod
from sage.api.routers import staging_edges as fastapi_staging_edges_mod
from sage.api.routers import users as fastapi_users_mod
from sage.api.routers import utilities as fastapi_utilities_mod
from sage.api.routers import vaults as fastapi_vaults_mod
from sage.models import schemas as schemas_mod
from sage.models.schemas import (
    DocumentDateStr,
    DocumentIdStr,
    EdgeIdStr,
    FunctionIdStr,
    Sha256Str,
    UserIdStr,
    VaultIdStr,
)

# Modules whose ``BaseModel`` subclasses are governed by this gate.
# Order is irrelevant; discovery dedupes by class identity.
_SCOPED_MODULES: Final[tuple] = (schemas_mod, router_mod, models_mod, config_mod)

# ---------------------------------------------------------------------------
# Shape registry
#
# Specific-name keys (no leading ``*``) win over ``*_suffix`` patterns
# at lookup time.
# ---------------------------------------------------------------------------

SHAPE_REGISTRY: Final[dict[str, type]] = {
    "id": DocumentIdStr,  # exact match — Model.id default
    "edge_id": EdgeIdStr,  # exact match — wins over *_id
    "vault_id": VaultIdStr,  # exact match — wins over *_id
    "function_id": FunctionIdStr,  # exact match — wins over *_id
    "*_id": DocumentIdStr,
    "*_hash": Sha256Str,
    "*_date": DocumentDateStr,
}


# Validators that count as typed-alias coverage. A field whose Pydantic
# metadata contains an ``AfterValidator`` whose ``.func`` is one of these
# is considered shape-validated, regardless of which specific alias the
# registry would have chosen. This relaxation lets ``Edge.id: EdgeIdStr``
# pass the ``id`` registry entry (which defaults to ``DocumentIdStr``)
# without a false positive.
_TYPED_VALIDATORS: Final[frozenset] = frozenset(
    {
        schemas_mod._validate_document_id,
        schemas_mod._validate_edge_id,
        schemas_mod._validate_function_id,
        schemas_mod._validate_sha256,
        schemas_mod._validate_document_date,
        schemas_mod._validate_user_id,
        schemas_mod._validate_vault_id,
    }
)


# ---------------------------------------------------------------------------
# KNOWN_VIOLATIONS
#
# Keyed by (ClassName, field_name). Each value is a one-line reason; the
# leading T-NNNN points at the remediation ticket where applicable.
# ---------------------------------------------------------------------------

KNOWN_VIOLATIONS: Final[dict[tuple[str, str], str]] = {}


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------


def _discover_basemodels() -> list[type[BaseModel]]:
    """Every ``BaseModel`` subclass declared in any module in ``_SCOPED_MODULES``.

    A class is attributed to a module only if its ``__module__`` matches
    the module's import name, so re-exports do not double-count.
    """
    out: list[type[BaseModel]] = []
    for mod in _SCOPED_MODULES:
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if obj is BaseModel:
                continue
            if not issubclass(obj, BaseModel):
                continue
            if obj.__module__ != mod.__name__:
                continue
            out.append(obj)
    return out


def _expected_alias(field_name: str) -> type | None:
    """Lookup the expected alias for ``field_name``.

    Exact-name keys win; ``*_suffix`` patterns match only when no exact
    entry exists.
    """
    if field_name in SHAPE_REGISTRY:
        return SHAPE_REGISTRY[field_name]
    for pattern, expected in SHAPE_REGISTRY.items():
        if pattern.startswith("*_") and field_name.endswith(pattern[1:]):
            return expected
    return None


def _walk_annotation(annotation) -> tuple[bool, bool]:
    """Walk an annotation tree; return ``(has_str_arm, has_typed_validator)``.

    Pydantic v2 stores typed aliases in two places depending on whether
    the field is optional:

    - Required ``DocumentIdStr``: ``field_info.annotation == str`` and the
      ``AfterValidator`` lives in ``field_info.metadata``.
    - ``DocumentIdStr | None``: the Union arm is the ``Annotated[str, ...]``
      itself; ``field_info.metadata`` is empty.

    This walker covers the second case (and is composed with a direct
    metadata check elsewhere for the first). It also reports whether a
    ``str`` arm exists at all, so callers can exclude e.g.
    ``datetime | None`` fields whose names happen to match a registry
    suffix.
    """
    has_str = False
    has_typed = False
    stack = [annotation]
    while stack:
        node = stack.pop()
        if hasattr(node, "__metadata__"):
            for m in node.__metadata__:
                if isinstance(m, AfterValidator) and m.func in _TYPED_VALIDATORS:
                    has_typed = True
            stack.append(node.__origin__)
            continue
        origin = typing.get_origin(node)
        if origin is typing.Union or origin is UnionType:
            stack.extend(typing.get_args(node))
            continue
        if node is str:
            has_str = True
    return has_str, has_typed


def _field_has_typed_alias(cls: type[BaseModel], name: str) -> bool:
    """True iff the field carries a typed-alias validator anywhere in its annotation."""
    field_info = cls.model_fields[name]
    for m in field_info.metadata:
        if isinstance(m, AfterValidator) and m.func in _TYPED_VALIDATORS:
            return True
    _, has_typed = _walk_annotation(field_info.annotation)
    return has_typed


def _alias_display_name(alias) -> str:
    """Human-readable name for a typed alias (Annotated has no ``__name__``)."""
    if alias is DocumentIdStr:
        return "DocumentIdStr"
    if alias is EdgeIdStr:
        return "EdgeIdStr"
    if alias is Sha256Str:
        return "Sha256Str"
    if alias is DocumentDateStr:
        return "DocumentDateStr"
    if alias is UserIdStr:
        return "UserIdStr"
    if alias is VaultIdStr:
        return "VaultIdStr"
    if alias is FunctionIdStr:
        return "FunctionIdStr"
    return str(alias)


def _shape_bearing_fields() -> list[tuple[type[BaseModel], str, type]]:
    """Yield (cls, field_name, expected_alias) for every own-field whose
    name matches the shape registry *and* whose annotation carries a
    ``str`` arm. Inherited fields are not re-walked; each class accounts
    only for fields declared in its own body. Non-``str`` fields whose
    names happen to match a suffix (e.g. ``DocumentSummary.document_date:
    datetime | None``) are filtered out — bare ``datetime`` is acceptable
    per the convention.
    """
    rows: list[tuple[type[BaseModel], str, type]] = []
    for cls in _discover_basemodels():
        own_annotations = inspect.get_annotations(cls)
        for field_name in own_annotations:
            if field_name not in cls.model_fields:
                # ClassVar / Pydantic-skipped annotation
                continue
            expected = _expected_alias(field_name)
            if expected is None:
                continue
            has_str, _ = _walk_annotation(cls.model_fields[field_name].annotation)
            if not has_str:
                continue
            rows.append((cls, field_name, expected))
    return rows


# ---------------------------------------------------------------------------
# FastAPI route-parameter gate (Pattern 1: alias on the parameter annotation)
# ---------------------------------------------------------------------------

# Modules whose ``APIRouter.routes`` are walked. ``fastapi_deps_mod`` has no
# router but exposes the dependency-provider functions whose signatures the
# router endpoints reuse via ``Depends(...)``; its module-level functions are
# walked separately below.
_FASTAPI_ROUTER_MODULES: Final[tuple] = (
    fastapi_documents_mod,
    fastapi_filename_parser_mod,
    fastapi_graph_ops_mod,
    fastapi_ingestion_mod,
    fastapi_lifecycle_mod,
    fastapi_metadata_mod,
    fastapi_pending_metadata_mod,
    fastapi_retrieval_mod,
    fastapi_staging_edges_mod,
    fastapi_users_mod,
    fastapi_utilities_mod,
    fastapi_vaults_mod,
    router_mod,  # app.backend.router
)

# Dependency-provider module: every async function whose ``__module__`` is
# ``sage.api.dependencies`` is treated as a boundary site for any
# shape-bearing parameter it declares (the ``vault_id`` it accepts via
# ``Path(...)`` or ``Depends(get_vault_id)``).
_FASTAPI_DEPENDENCY_MODULES: Final[tuple] = (fastapi_deps_mod,)


# Keyed by (qualified_callable_name, param_name). One-line reason values.
KNOWN_FASTAPI_VIOLATIONS: Final[dict[tuple[str, str], str]] = {}


def _qualified_callable_name(fn: Callable) -> str:
    """Return ``<module>.<qualname>`` for a callable (handles closures)."""
    return f"{fn.__module__}.{fn.__qualname__}"


def _depends_target(default: object) -> Callable | None:
    """If ``default`` is a FastAPI ``Depends(...)`` value, return its target.

    FastAPI's ``Depends`` factory returns an instance of ``Depends`` (the
    class re-exported as ``fastapi.params.Depends``) whose ``.dependency``
    attribute is the wrapped callable. Returns ``None`` for non-Depends
    defaults.
    """
    if isinstance(default, DependsParam):
        return default.dependency
    return None


def _param_annotation_has_typed_alias(annotation, expected_alias: type) -> bool:
    """Pattern 1 check: parameter annotation carries the expected alias.

    Reuses ``_walk_annotation`` to traverse the annotation tree for an
    ``AfterValidator`` whose ``func`` is in ``_TYPED_VALIDATORS``. The
    ``expected_alias`` is currently only used for the failure message;
    the gate is satisfied by any typed-alias validator on a shape-bearing
    parameter, mirroring the BaseModel-side relaxation (an ``Edge.id:
    EdgeIdStr`` passes the ``id`` registry entry which would otherwise
    default to ``DocumentIdStr``).
    """
    _, has_typed = _walk_annotation(annotation)
    return has_typed


def _fastapi_route_endpoints() -> list[Callable]:
    """Every registered route endpoint across all FastAPI routers in scope.

    Deduplicates by function identity in case the same endpoint is
    mounted on multiple routes (e.g., GET and HEAD on the same path).
    """
    seen: dict[int, Callable] = {}
    for mod in _FASTAPI_ROUTER_MODULES:
        for route in getattr(mod.router, "routes", []):
            endpoint = getattr(route, "endpoint", None)
            if endpoint is None:
                continue
            seen[id(endpoint)] = endpoint
    return list(seen.values())


def _fastapi_dependency_providers() -> list[Callable]:
    """Every module-level async function declared in dependency modules.

    Filters by ``__module__`` to avoid catching re-exports (e.g.,
    ``Depends`` itself if imported into the module).
    """
    out: list[Callable] = []
    for mod in _FASTAPI_DEPENDENCY_MODULES:
        for name, obj in inspect.getmembers(mod, inspect.iscoroutinefunction):
            if obj.__module__ != mod.__name__:
                continue
            if name.startswith("_"):
                continue
            out.append(obj)
    return out


def _discover_fastapi_route_params() -> list[tuple[Callable, str, type]]:
    """Yield ``(handler, param_name, expected_alias)`` for every shape-bearing
    parameter on every FastAPI route endpoint and dependency provider.

    A parameter is shape-bearing iff its name matches the ``SHAPE_REGISTRY``
    (via ``_expected_alias``) and its annotation has a ``str`` arm.
    Parameters whose default is ``Depends(get_vault_id)`` are still
    walked — Pattern 1 requires the alias on the local annotation, not
    only on the dependency target.
    """
    rows: list[tuple[Callable, str, type]] = []
    callables = _fastapi_route_endpoints() + _fastapi_dependency_providers()
    for handler in callables:
        try:
            sig = inspect.signature(handler)
        except (TypeError, ValueError):
            continue
        for param_name, param in sig.parameters.items():
            expected = _expected_alias(param_name)
            if expected is None:
                continue
            annotation = param.annotation
            if annotation is inspect.Parameter.empty:
                continue
            has_str, _ = _walk_annotation(annotation)
            if not has_str:
                continue
            rows.append((handler, param_name, expected))
    return rows


# ---------------------------------------------------------------------------
# FastMCP tool-parameter gate (Pattern 2: TypeAdapter in body)
# ---------------------------------------------------------------------------


def _registered_fastmcp_tools() -> list[Callable]:
    """Every tool function registered on the module-level ``mcp`` instance.

    Importing ``sage.mcp_server`` is the side-effect that registers
    everything; by the time this function is called, the registry is
    populated.
    """
    mcp = fastmcp_server_mod.mcp
    tools = mcp._tool_manager._tools  # noqa: SLF001 -- FastMCP exposes no public API
    return [t.fn for t in tools.values()]


# Keyed by (qualified_callable_name, param_name). One-line reason values.
KNOWN_FASTMCP_VIOLATIONS: Final[dict[tuple[str, str], str]] = {}


# Cache: ``module → {adapter_name → alias_class}`` computed via AST.
# Keyed by module identity so re-parsing happens at most once per module.
_TYPEADAPTER_BINDINGS_CACHE: dict[int, dict[str, type]] = {}

# Cache: ``function id → list[(adapter_name, param_name)]`` extracted via
# AST walk of the function body. The list captures every call of the
# form ``<adapter_name>.validate_python(<param_name>)`` where the arg
# is a bare ``Name`` node (not an expression, attribute, or constant).
_VALIDATE_PYTHON_CALLS_CACHE: dict[int, list[tuple[str, str]]] = {}


def _module_typeadapter_bindings(module: ModuleType) -> dict[str, type]:
    """Map ``adapter_name → alias_class`` from module-scope assignments.

    Walks the module's AST looking for top-level statements of the form::

        <NAME>: TypeAdapter[...] = TypeAdapter(<AliasName>)
        <NAME> = TypeAdapter(<AliasName>)

    where ``<AliasName>`` resolves to one of the typed aliases. Only
    module-scope bindings count — adapters constructed inside function
    bodies are not discoverable here.
    """
    cached = _TYPEADAPTER_BINDINGS_CACHE.get(id(module))
    if cached is not None:
        return cached

    bindings: dict[str, type] = {}
    try:
        src = inspect.getsource(module)
    except (OSError, TypeError):
        _TYPEADAPTER_BINDINGS_CACHE[id(module)] = bindings
        return bindings

    tree = ast.parse(src)
    for node in tree.body:
        # Capture both ``x = TypeAdapter(...)`` (ast.Assign) and
        # ``x: TypeAdapter[...] = TypeAdapter(...)`` (ast.AnnAssign).
        targets: list[ast.Name] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    targets.append(t)
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets.append(node.target)
            value = node.value

        if value is None:
            continue
        if not isinstance(value, ast.Call):
            continue
        if not (isinstance(value.func, ast.Name) and value.func.id == "TypeAdapter"):
            continue
        if not value.args:
            continue
        arg = value.args[0]
        if not isinstance(arg, ast.Name):
            continue
        alias_cls = getattr(module, arg.id, None)
        if alias_cls is None:
            continue
        for t in targets:
            bindings[t.id] = alias_cls

    _TYPEADAPTER_BINDINGS_CACHE[id(module)] = bindings
    return bindings


def _validate_python_calls_in_function(fn: Callable) -> list[tuple[str, str]]:
    """Return ``[(adapter_name, param_name), ...]`` from the function's body.

    Walks the AST of ``fn`` looking for calls of the form::

        <adapter_name>.validate_python(<param_name>)

    Only matches when the argument is a bare ``Name`` node — the
    parameter is being validated by name, not through an expression.
    Closures and nested functions inside ``fn`` are included so a
    plumbed registration helper does not hide the validation.
    """
    cached = _VALIDATE_PYTHON_CALLS_CACHE.get(id(fn))
    if cached is not None:
        return cached

    out: list[tuple[str, str]] = []
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):
        _VALIDATE_PYTHON_CALLS_CACHE[id(fn)] = out
        return out

    # ``getsource`` of a nested ``def`` returns the def text indented
    # relative to its enclosing scope; ``ast.parse`` rejects leading
    # whitespace. Use ``textwrap.dedent``.
    import textwrap

    src = textwrap.dedent(src)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "validate_python"):
            continue
        if not isinstance(func.value, ast.Name):
            continue
        adapter_name = func.value.id
        if not node.args:
            continue
        arg = node.args[0]
        if not isinstance(arg, ast.Name):
            continue
        out.append((adapter_name, arg.id))

    _VALIDATE_PYTHON_CALLS_CACHE[id(fn)] = out
    return out


def _alias_carries_typed_validator(alias_cls: type) -> bool:
    """True iff ``alias_cls`` is an ``Annotated[str, AfterValidator(<known>)]``
    whose validator function is in ``_TYPED_VALIDATORS``.

    Mirrors the BaseModel gate's relaxation: any typed alias counts as
    coverage for a shape-bearing site, not only the registry-chosen one.
    This lets ``_EDGE_ID_ADAPTER.validate_python(retracted_edge_id)`` pass
    the ``*_id`` registry entry (which defaults to ``DocumentIdStr``)
    without a false positive — semantically the field is an edge id and
    the validator is the appropriate one.
    """
    if not hasattr(alias_cls, "__metadata__"):
        return False
    for m in alias_cls.__metadata__:
        if isinstance(m, AfterValidator) and m.func in _TYPED_VALIDATORS:
            return True
    return False


def _tool_has_typeadapter_validation(
    tool_fn: Callable, param_name: str, expected_alias: type
) -> bool:
    """Pattern 2 check: ``_<X>_ADAPTER.validate_python(<param_name>)`` in body.

    True iff:

    1. The tool function's enclosing module declares a module-scope
       ``TypeAdapter[...] = TypeAdapter(<Alias>)`` binding where
       ``<Alias>`` is in ``_TYPED_VALIDATORS`` (any typed alias counts).
    2. The tool function's body contains a call
       ``<that_name>.validate_python(<param_name>)``.

    The ``expected_alias`` argument is used only for the failure-message
    hint; coverage is satisfied by any typed-alias validator on the
    parameter (parallels the BaseModel-side relaxation).
    """
    module = inspect.getmodule(tool_fn)
    if module is None:
        return False
    bindings = _module_typeadapter_bindings(module)
    calls = _validate_python_calls_in_function(tool_fn)
    for adapter_name, validated_arg in calls:
        if validated_arg != param_name:
            continue
        alias_cls = bindings.get(adapter_name)
        if alias_cls is None:
            continue
        if _alias_carries_typed_validator(alias_cls):
            return True
    return False


def _discover_fastmcp_tool_params() -> list[tuple[Callable, str, type]]:
    """Yield ``(tool_fn, param_name, expected_alias)`` for every shape-bearing
    parameter on every FastMCP tool registered on the module-level ``mcp``
    instance.
    """
    rows: list[tuple[Callable, str, type]] = []
    for tool_fn in _registered_fastmcp_tools():
        try:
            sig = inspect.signature(tool_fn)
        except (TypeError, ValueError):
            continue
        for param_name, param in sig.parameters.items():
            expected = _expected_alias(param_name)
            if expected is None:
                continue
            annotation = param.annotation
            if annotation is inspect.Parameter.empty:
                continue
            has_str, _ = _walk_annotation(annotation)
            if not has_str:
                continue
            rows.append((tool_fn, param_name, expected))
    return rows


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_discovered_basemodels_are_nonempty():
    """Sanity: discovery surfaces the expected schemas across every scoped module."""
    classes = _discover_basemodels()
    names = {cls.__name__ for cls in classes}
    # sage.models.schemas
    assert "Document" in names, "Discovery missed core Document model"
    assert "Edge" in names, "Discovery missed core Edge model"
    assert "LinkRequest" in names, "Discovery missed canonical LinkRequest model"
    # app.backend.models (scan-chain models -- T-0043)
    assert "ScanRequest" in names, "Discovery missed app.backend.models ScanRequest"
    assert "ScanResultResponse" in names, "Discovery missed app.backend.models ScanResultResponse"
    # sage.config
    assert "VaultIdentity" in names, "Discovery missed sage.config VaultIdentity"
    assert "VaultConfig" in names, "Discovery missed sage.config VaultConfig"
    assert len(classes) >= 30, (
        f"Discovery surfaced only {len(classes)} models; expected at least 30. "
        "Did discovery filtering regress?"
    )


@pytest.mark.parametrize(
    "cls,field_name,expected_alias",
    [
        pytest.param(cls, field, expected, id=f"{cls.__name__}.{field}")
        for cls, field, expected in _shape_bearing_fields()
    ],
)
def test_typed_alias_coverage(cls: type[BaseModel], field_name: str, expected_alias: type) -> None:
    """Every shape-bearing field is typed or pinned in KNOWN_VIOLATIONS."""
    key = (cls.__name__, field_name)
    has_alias = _field_has_typed_alias(cls, field_name)
    allowlisted = key in KNOWN_VIOLATIONS

    if has_alias and allowlisted:
        pytest.fail(
            f"{cls.__name__}.{field_name} is typed (carries an AfterValidator "
            f"from sage.models.schemas) AND is allowlisted in KNOWN_VIOLATIONS. "
            f"Remove the stale entry ({KNOWN_VIOLATIONS[key]!r})."
        )
    if has_alias:
        return
    if allowlisted:
        return

    expected_name = _alias_display_name(expected_alias)
    pytest.fail(
        f"{cls.__name__}.{field_name} is shape-bearing (registry expects "
        f"{expected_name}) but is bare `str`. Either annotate it with "
        f'{expected_name} (preferred) or add ("{cls.__name__}", '
        f'"{field_name}") to KNOWN_VIOLATIONS with a comment '
        "explaining why."
    )


def test_known_violations_reference_real_fields():
    """Every KNOWN_VIOLATIONS entry must correspond to a real shape-bearing field."""
    shape_bearing = {(cls.__name__, name) for cls, name, _ in _shape_bearing_fields()}
    stale = sorted(set(KNOWN_VIOLATIONS) - shape_bearing)
    assert not stale, (
        f"KNOWN_VIOLATIONS contains entries that do not correspond to any "
        f"shape-bearing field in sage.models.schemas: {stale}. Did the field "
        "get renamed or removed? Remove the stale entry."
    )


# ---------------------------------------------------------------------------
# FastAPI route-parameter tests
# ---------------------------------------------------------------------------


def test_discovered_fastapi_callables_are_nonempty():
    """Sanity: discovery surfaces route handlers and dependency providers."""
    endpoints = _fastapi_route_endpoints()
    providers = _fastapi_dependency_providers()
    endpoint_names = {fn.__name__ for fn in endpoints}
    provider_names = {fn.__name__ for fn in providers}
    # Spot-check a few well-known endpoints exist in the registry.
    assert "get_document" in endpoint_names, "Missing documents router endpoint"
    assert "check_preconditions" in endpoint_names, "Missing graph_ops endpoint"
    assert "set_lifecycle" in endpoint_names, "Missing lifecycle endpoint"
    # Spot-check dependency providers.
    assert "get_vault_id" in provider_names, "Missing get_vault_id provider"
    assert "get_documents_service" in provider_names, "Missing get_documents_service"
    # Sanity on total counts so a regression in discovery surfaces here.
    assert len(endpoints) >= 25, (
        f"Discovery surfaced only {len(endpoints)} route endpoints; expected at least 25."
    )
    assert len(providers) >= 10, (
        f"Discovery surfaced only {len(providers)} dependency providers; expected at least 10."
    )


@pytest.mark.parametrize(
    "handler,param_name,expected_alias",
    [
        pytest.param(
            handler,
            param_name,
            expected,
            id=f"{_qualified_callable_name(handler)}.{param_name}",
        )
        for handler, param_name, expected in _discover_fastapi_route_params()
    ],
)
def test_fastapi_route_param_coverage(
    handler: Callable, param_name: str, expected_alias: type
) -> None:
    """Every shape-bearing FastAPI parameter is typed or pinned in KNOWN_FASTAPI_VIOLATIONS."""
    qualified = _qualified_callable_name(handler)
    key = (qualified, param_name)
    sig = inspect.signature(handler)
    annotation = sig.parameters[param_name].annotation
    has_alias = _param_annotation_has_typed_alias(annotation, expected_alias)
    allowlisted = key in KNOWN_FASTAPI_VIOLATIONS

    if has_alias and allowlisted:
        pytest.fail(
            f"{qualified}({param_name}) carries a typed-alias validator AND is "
            f"allowlisted in KNOWN_FASTAPI_VIOLATIONS. Remove the stale entry "
            f"({KNOWN_FASTAPI_VIOLATIONS[key]!r})."
        )
    if has_alias:
        return
    if allowlisted:
        return

    expected_name = _alias_display_name(expected_alias)
    pytest.fail(
        f"{qualified}({param_name}) is shape-bearing (registry expects "
        f"{expected_name}) but is bare `str`. Either annotate it with "
        f"{expected_name} (Pattern 1, preferred for FastAPI) or add "
        f'("{qualified}", "{param_name}") to KNOWN_FASTAPI_VIOLATIONS '
        "with a comment explaining why."
    )


def test_known_fastapi_violations_reference_real_params():
    """Every KNOWN_FASTAPI_VIOLATIONS entry must correspond to a real param."""
    shape_bearing = {
        (_qualified_callable_name(h), n) for h, n, _ in _discover_fastapi_route_params()
    }
    stale = sorted(set(KNOWN_FASTAPI_VIOLATIONS) - shape_bearing)
    assert not stale, (
        f"KNOWN_FASTAPI_VIOLATIONS contains entries that do not correspond to any "
        f"shape-bearing FastAPI parameter: {stale}. Did the handler get renamed "
        "or the parameter removed? Remove the stale entry."
    )


# ---------------------------------------------------------------------------
# FastMCP tool-parameter tests
# ---------------------------------------------------------------------------


def test_discovered_fastmcp_tools_are_nonempty():
    """Sanity: tool discovery finds registered tools on the mcp instance."""
    tools = _registered_fastmcp_tools()
    names = {fn.__name__ for fn in tools}
    assert "sage_ingest" in names, "Missing sage_ingest tool registration"
    assert "sage_get_document" in names, "Missing sage_get_document tool registration"
    assert "sage_unlink" in names, "Missing sage_unlink tool registration (Pattern 2 precedent)"
    assert "app_scan_directory" in names, "Missing app_scan_directory tool registration"
    assert len(tools) >= 25, f"Discovery surfaced only {len(tools)} tools; expected at least 25."


@pytest.mark.parametrize(
    "tool_fn,param_name,expected_alias",
    [
        pytest.param(
            tool_fn,
            param_name,
            expected,
            id=f"{tool_fn.__module__}.{tool_fn.__name__}.{param_name}",
        )
        for tool_fn, param_name, expected in _discover_fastmcp_tool_params()
    ],
)
def test_fastmcp_tool_param_coverage(
    tool_fn: Callable, param_name: str, expected_alias: type
) -> None:
    """Every shape-bearing FastMCP tool parameter is wrapped with the TypeAdapter
    matching its expected alias, or pinned in KNOWN_FASTMCP_VIOLATIONS.

    Pattern 2 detection: a module-scope ``_<ALIAS>_ADAPTER: TypeAdapter[str]
    = TypeAdapter(<ExpectedAlias>)`` exists, and the function body calls
    ``_<ALIAS>_ADAPTER.validate_python(<param_name>)``.
    """
    qualified = f"{tool_fn.__module__}.{tool_fn.__name__}"
    key = (qualified, param_name)
    has_validation = _tool_has_typeadapter_validation(tool_fn, param_name, expected_alias)
    allowlisted = key in KNOWN_FASTMCP_VIOLATIONS

    if has_validation and allowlisted:
        pytest.fail(
            f"{qualified}({param_name}) is validated via a module-scope TypeAdapter "
            f"AND is allowlisted in KNOWN_FASTMCP_VIOLATIONS. Remove the stale entry "
            f"({KNOWN_FASTMCP_VIOLATIONS[key]!r})."
        )
    if has_validation:
        return
    if allowlisted:
        return

    expected_name = _alias_display_name(expected_alias)
    adapter_hint = f"_{expected_name.replace('Str', '').upper()}_ADAPTER"
    pytest.fail(
        f"{qualified}({param_name}) is shape-bearing (registry expects "
        f"{expected_name}) but has no module-scope TypeAdapter validation in "
        f"the function body. Pattern 2: declare "
        f"`{adapter_hint}: TypeAdapter[str] = TypeAdapter({expected_name})` at "
        f"module scope and call `{adapter_hint}.validate_python({param_name})` "
        f"inside the tool body (before any use), or add "
        f'("{qualified}", "{param_name}") to KNOWN_FASTMCP_VIOLATIONS '
        "with a comment explaining why."
    )


def test_known_fastmcp_violations_reference_real_params():
    """Every KNOWN_FASTMCP_VIOLATIONS entry must correspond to a real param."""
    shape_bearing = {
        (f"{fn.__module__}.{fn.__name__}", n) for fn, n, _ in _discover_fastmcp_tool_params()
    }
    stale = sorted(set(KNOWN_FASTMCP_VIOLATIONS) - shape_bearing)
    assert not stale, (
        f"KNOWN_FASTMCP_VIOLATIONS contains entries that do not correspond to any "
        f"shape-bearing FastMCP tool parameter: {stale}. Did the tool get renamed "
        "or the parameter removed? Remove the stale entry."
    )


# ---------------------------------------------------------------------------
# Boundary-validation construction tests
#
# DiscoverRequest.document_id is the one request-side field T-0026 typed.
# Property coverage in test_alias_invariants.py locks the validator; this
# test confirms the alias is wired through at the model-construction
# boundary so that bad caller input is rejected before reaching the
# service layer (per the Typed-Alias Boundary Conventions).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_value",
    [
        "not-a-doc-id",  # no underscore, contains hyphens
        "DEADBEEF_uppercase_prefix",  # uppercase hex
        "deadbeef-no-underscore",  # hyphen instead of underscore
        "1234abcd_",  # empty slug
        "12345678_Trailing-Slash",  # slug has uppercase + hyphen
        "",  # empty string
    ],
)
def test_discover_request_document_id_rejects_non_canonical(bad_value: str) -> None:
    """Non-canonical document_id values must be rejected at request construction."""
    from pydantic import ValidationError

    from sage.models.schemas import DiscoverRequest

    with pytest.raises(ValidationError):
        DiscoverRequest(document_id=bad_value)


# ---------------------------------------------------------------------------
# Negative-control tests for the new boundary-validation surfaces
#
# These prove the typed aliases actually fire at the boundary, not just at
# the gate. The gate confirms the alias is wired; these confirm the wiring
# rejects bad input.
# ---------------------------------------------------------------------------


def test_fastapi_handler_rejects_non_canonical_vault_id() -> None:
    """A malformed vault_id in a route path returns HTTP 422 from the alias's validator.

    Uses the existing FastAPI TestClient infrastructure. The error must
    come from ``_validate_vault_id`` (shape rejection), not from
    ``VaultNotFoundError`` (registry miss). The distinction is observable
    by status code: 422 means request-binding validation rejected the
    input; 404 means binding passed and the registry then rejected.
    """
    from fastapi.testclient import TestClient

    from sage.app import create_app

    app = create_app()
    with TestClient(app) as client:
        # ``VAULT-WITH-UPPER`` is shape-invalid (uppercase + hyphens).
        # Any handler under /sage_vaults/{vault_id}/... works as a probe;
        # use a documents endpoint that exists in the registry.
        resp = client.get("/sage_vaults/VAULT-WITH-UPPER/documents/14405c6d_x")
    assert resp.status_code == 422, (
        f"Expected 422 from vault_id alias validator, got {resp.status_code}: {resp.text[:300]}"
    )


def test_fastmcp_tool_rejects_non_canonical_document_id() -> None:
    """A malformed document_id passed to a FastMCP tool returns the SAGE error envelope.

    Pattern 2 routes ``pydantic_core.ValidationError`` (a ``ValueError``
    subclass) through the existing ``except (SAGEError, ValueError)`` block,
    which calls ``error_response(...)``. The response is the standard SAGE
    error dict shape, not a raised exception and not a FastMCP default error.
    """
    import asyncio

    from sage.mcp_server import sage_get_document

    # ``not-an-id`` is shape-invalid for DocumentIdStr (no underscore-slug form).
    result = asyncio.run(sage_get_document(vault_id="cas", document_id="not-an-id"))
    assert isinstance(result, dict), f"Expected dict envelope, got {type(result).__name__}"
    assert "error" in result, f"Expected SAGE error envelope with 'error' key, got: {result!r}"
    # The validator's ValueError surfaces through error_response as
    # ``internal_error`` (the fall-through bucket for non-SAGEError ValueErrors).
    # What matters is the envelope shape, not the specific error code.
    assert "message" in result, f"Expected 'message' in error envelope, got: {result!r}"
