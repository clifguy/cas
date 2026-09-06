"""Conformance gate: typed-alias coverage at three boundary site classes.

Cites the CAS Typed-Alias Boundary Conventions steering document (cas
SAGE vault, doc_type=steering_document). A typed alias is a
``pydantic.Annotated[str, AfterValidator(...)]`` declared in
``sage/models/schemas.py``. Three site classes are governed:

1. **Pydantic ``BaseModel`` fields** in modules listed in
   ``_SCOPED_MODULES``. Every shape-bearing field must carry the
   alias or be pinned in ``KNOWN_VIOLATIONS``.
2. **FastAPI route parameters** in the routers listed in
   ``_FASTAPI_ROUTER_MODULES``. Pattern 1: the alias goes directly on
   the parameter annotation. ``KNOWN_FASTAPI_VIOLATIONS`` is the
   allowlist.
3. **FastMCP tool entry points** registered on the FastMCP server.
   Pattern 2: signature stays bare ``str``; a module-scope
   ``TypeAdapter[<Alias>]`` is constructed and the tool body calls
   ``<adapter>.validate_python(<param>)`` against each shape-bearing
   parameter. ``pydantic_core.ValidationError`` extends ``ValueError``
   so the existing ``except (SAGEError, ValueError)`` block routes
   shape failures through the SAGE error envelope.
   ``KNOWN_FASTMCP_VIOLATIONS`` is the allowlist.

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

Drain plan: typed the 22 currently-allowlisted BaseModel fields
whose aliases already existed; introduced ``UserIdStr`` /
``VaultIdStr`` / ``FunctionIdStr`` and typed the remaining 5;
extended the gate to ``app.backend.router`` and ``sage.config`` and
typed the four shape-bearing fields the extension surfaced;
extended the gate to FastAPI route parameters and FastMCP tool entry
points and typed every shape-bearing parameter at those boundaries.
A later change added ``synced_from_content_hash`` to ``Edge`` /
``LinkRequest`` and the edge-creation MCP tool as ``str | None`` with
hash-format validation deferred; it retyped those three sites to
``Sha256Str``-shaped validation and cleared the three allowlist
entries. The MCP side rides Pattern 3 rather than Pattern 2: the bulk
edge-creation tool takes an ``items`` list and validates each entry
through ``BulkLinkItem.model_validate``, whose fields carry the alias.
Widened the registry to the plural forms of its suffix patterns and
taught the annotation walk to descend into sequence element types,
which surfaced the collection-shaped sites the ``endswith`` rules had
never enumerated.

What remains allowlisted is exceptions rather than debt:
``KNOWN_FASTAPI_VIOLATIONS`` is empty; ``KNOWN_VIOLATIONS`` holds
foreign identifiers that are not SAGE ids; ``KNOWN_FASTMCP_VIOLATIONS``
holds published-but-unconsumed tripwire parameters. Every entry carries
its justification inline.

Collection sites carry the alias on the element type -- ``list[<Alias>]``
on a ``BaseModel`` field, and a module-scope
``TypeAdapter(list[<Alias>])`` for the Pattern 2 form. The container
itself is never aliased.
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
#
# Suffix patterns match by ``endswith``, so a plural name is a distinct
# pattern rather than a form the singular already covers: ``document_ids``
# does not end in ``_id``, nor ``hashes`` in ``_hash``. Each suffix therefore
# carries its plural, and a collection site is expected to apply the alias to
# its *element* type. The plural of a bare stem is an exact key, as ``hashes``
# is below.
# ---------------------------------------------------------------------------

SHAPE_REGISTRY: Final[dict[str, type]] = {
    "id": DocumentIdStr,  # exact match — Model.id default
    "edge_id": EdgeIdStr,  # exact match — wins over *_id
    "vault_id": VaultIdStr,  # exact match — wins over *_id
    "function_id": FunctionIdStr,  # exact match — wins over *_id
    "hashes": Sha256Str,  # exact match — the bare-stem plural
    "user_ids": UserIdStr,  # exact match — wins over *_ids
    "*_id": DocumentIdStr,
    "*_hash": Sha256Str,
    "*_date": DocumentDateStr,
    "*_ids": DocumentIdStr,
    # Load-bearing despite having no *collection* site: ``check_hashes`` is a
    # boolean flag that matches this pattern and is excluded by the str-arm
    # filter, which is the overshoot boundary a test pins. Removing this entry
    # reds that test.
    "*_hashes": Sha256Str,
    # Unlike ``*_hashes`` above, nothing matches ``*_dates`` today — not even
    # a filtered-out non-``str`` site. It is declared so the plural rule covers
    # the whole suffix set rather than the subset that happened to have an
    # escapee when the gap was found.
    "*_dates": DocumentDateStr,
}


# Container origins the annotation walk and the adapter-binding walk descend
# into. A sequence holds its shape on the element type; a mapping does not,
# and descending into one would treat a ``dict[str, ...]`` key as a
# shape-bearing arm.
_SEQUENCE_ORIGINS: Final[frozenset] = frozenset({list, set, tuple, frozenset})
_SEQUENCE_NAMES: Final[frozenset[str]] = frozenset({"list", "set", "tuple", "frozenset"})


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

KNOWN_VIOLATIONS: Final[dict[tuple[str, str], str]] = {
    # The Entra directory tenant identifier (an external identity-provider
    # GUID) is not a SAGE document id; the DocumentIdStr alias would impose
    # SAGE's id grammar on a foreign identifier. Plain str is correct.
    ("StackAuthConfig", "tenant_id"): "identity-provider tenant id, not a SAGE document id",
    # The SharePoint site and document-library identifiers (CAS-ADR-043) are
    # opaque Microsoft Graph identifiers, not SAGE document ids; the
    # DocumentIdStr alias would impose SAGE's id grammar on a foreign
    # identifier. Plain str is correct.
    ("StackDocumentStoreConfig", "site_id"): "Microsoft Graph site id, not a SAGE document id",
    ("StackDocumentStoreConfig", "drive_id"): "Microsoft Graph drive id, not a SAGE document id",
    # The transfer channel's public identifier is an opaque server-minted
    # handle round-tripped verbatim between a recipe and the transfer
    # endpoints; it is not a SAGE document id, and the DocumentIdStr alias
    # would impose SAGE's id grammar on it. Plain str is correct.
    ("UploadRecipeItem", "transfer_id"): "opaque transfer handle, not a SAGE document id",
    ("DownloadRecipe", "transfer_id"): "opaque transfer handle, not a SAGE document id",
    ("TransferUploadResult", "transfer_id"): "opaque transfer handle, not a SAGE document id",
}


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
    - ``DocumentIdStr | None``: the Union arm is the ``Annotated[str,...]``
      itself; ``field_info.metadata`` is empty.

    This walker covers the second case (and is composed with a direct
    metadata check elsewhere for the first). It also reports whether a
    ``str`` arm exists at all, so callers can exclude e.g.
    ``datetime | None`` fields whose names happen to match a registry
    suffix.

    A sequence carries its shape on the element type -- ``list[DocumentIdStr]``,
    never ``DocumentIdStr`` on the container -- so the walk descends into
    sequence arguments. Descent is deliberately limited to sequence
    containers: a mapping's ``str`` key is not a shape-bearing arm, and
    descending into every generic would report a ``str`` arm for any
    ``dict[str, ...]`` field whose name matched a registry pattern.
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
        if origin in _SEQUENCE_ORIGINS:
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
    names happen to match a suffix are filtered out — bare ``datetime``
    is acceptable per the convention.
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
KNOWN_FASTMCP_VIOLATIONS: Final[dict[tuple[str, str], str]] = {
    (
        "sage.sage_api_tools.ingest_document",
        "document_date",
    ): (
        "Tripwire parameter, not a functional argument: ingest_document "
        "publishes the recognized metadata keys at the top level only so a "
        "wrong-level spelling reaches the misplaced_metadata guard instead of "
        "being stripped by a client's published-schema coercion. The value is "
        "never consumed, so shape validation would be actively harmful -- a "
        "caller who misplaced a well-formed date would get a date-format "
        "complaint instead of the message naming the nested shape."
    ),
    (
        "sage.sage_api_tools.search",
        "source_id",
    ): (
        "Tripwire parameter, not a functional argument: search publishes the "
        "RetrievalFilters keys at the top level only so a wrong-level spelling "
        "reaches the misplaced_filters guard instead of being stripped by a "
        "client's published-schema coercion. The value is never consumed, so "
        "shape validation would be actively harmful -- a caller who misplaced "
        "a well-formed document id would get an id-grammar complaint instead "
        "of the message naming the nested shape."
    ),
    (
        "sage.sage_api_tools.search",
        "target_id",
    ): (
        "Tripwire parameter, not a functional argument: the target_id half of "
        "the same edge-filter pair as search.source_id above, published and "
        "left unvalidated for the same reason."
    ),
    (
        "sage.sage_api_tools.search",
        "document_ids",
    ): (
        "Tripwire parameter, not a functional argument: the collection-shaped "
        "member of the same published-filter-key set as search.source_id "
        "above, left unvalidated for the same reason. The functional site is "
        "RetrievalFilters.document_ids, which carries the alias on its element "
        "type and rejects a malformed entry at the nested level the caller "
        "should have used."
    ),
}


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
        <NAME>: TypeAdapter[...] = TypeAdapter(list[<AliasName>])

    where ``<AliasName>`` resolves to one of the typed aliases. Only
    module-scope bindings count — adapters constructed inside function
    bodies are not discoverable here.

    The third form is the collection shape. A sequence adapter binds to its
    *element* alias: the shape contract belongs to the element, and the
    binding is what lets the Pattern 2 detector credit a
    ``validate_python(<collection_param>)`` call.
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
        # ``TypeAdapter(list[Alias])`` parses as a Subscript, not a Name.
        # Unwrap one sequence layer to the element alias.
        if (
            isinstance(arg, ast.Subscript)
            and isinstance(arg.value, ast.Name)
            and arg.value.id in _SEQUENCE_NAMES
        ):
            arg = arg.slice
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
    """Return ``[(adapter_name, param_name),...]`` from the function's body.

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
    # app.backend.models (scan-chain models --)
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
    # Post-CAS-ADR-029 (CAS-ADR-029 v4): the lifecycle router endpoint is
    # the consolidated plural-noun handler ``update_lifecycles``.
    assert "update_lifecycles" in endpoint_names, "Missing lifecycle endpoint"
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
    assert "ingest_document" in names, "Missing ingest_document tool registration"
    assert "get_document" in names, "Missing get_document tool registration"
    assert "delete_edge" in names, "Missing delete_edge tool registration (Pattern 2 precedent)"
    assert "list_directory" in names, "Missing list_directory tool registration"
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
# DiscoverRequest.document_id is the one request-side field typed.
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
    """A malformed vault_id in a route path returns the structured
    invalid_vault_id (400) from the alias's validator.

    Uses the existing FastAPI TestClient infrastructure. The error must
    come from ``_validate_vault_id`` (shape rejection), not from
    ``VaultNotFoundError`` (registry miss). The distinction is observable
    by status code: 400 (``invalid_vault_id``) means request-binding
    validation rejected the input -- the typed-alias family translates the
    binding rejection into a typed SAGE 400 instead of FastAPI's native 422;
    404 would mean binding passed and the registry then rejected.
    """
    from fastapi.testclient import TestClient

    from sage.app import create_app

    app = create_app()
    with TestClient(app) as client:
        # ``VAULT-WITH-UPPER`` is shape-invalid (uppercase + hyphens).
        # Any handler under /sage_vaults/{vault_id}/... works as a probe;
        # use a documents endpoint that exists in the registry.
        resp = client.get("/sage_vaults/VAULT-WITH-UPPER/documents/14405c6d_x")
    assert resp.status_code == 400, (
        f"Expected 400 invalid_vault_id from the vault_id alias validator, got "
        f"{resp.status_code}: {resp.text[:300]}"
    )
    assert resp.json()["code"] == "invalid_vault_id", resp.text


def test_fastmcp_tool_rejects_non_canonical_document_id() -> None:
    """A malformed document_id passed to a FastMCP tool returns the SAGE error envelope.

    Pattern 2 routes ``pydantic_core.ValidationError`` (a ``ValueError``
    subclass) through the existing ``except (SAGEError, ValueError)`` block,
    which calls ``error_response(...)``. The response is the standard SAGE
    error dict shape, not a raised exception and not a FastMCP default error.
    """
    import asyncio

    from sage.mcp_server import get_document

    # ``not-an-id`` is shape-invalid for DocumentIdStr (no underscore-slug form).
    result = asyncio.run(get_document(vault_id="cas", document_id="not-an-id"))
    assert isinstance(result, dict), f"Expected dict envelope, got {type(result).__name__}"
    assert "error" in result, f"Expected SAGE error envelope with 'error' key, got: {result!r}"
    # A malformed document_id is rejected at the boundary as the structured
    # ``invalid_document_id`` (400) envelope -- not the generic
    # ``internal_error`` -- so the caller gets a caller-actionable code plus
    # the offending value, never a raw Pydantic dump.
    assert result["error"] == "invalid_document_id", f"got: {result!r}"
    assert result["detail"]["document_id"] == "not-an-id", f"got: {result!r}"
    assert "message" in result, f"Expected 'message' in error envelope, got: {result!r}"


# ---------------------------------------------------------------------------
# Leaf-layer structured-error contract for the typed-alias family.
#
# Each sibling reject validator must raise PydanticCustomError with its own
# external code and a uniform ctx {argument, value, expected}, so the
# request-boundary translator can rebuild the invalid_<alias> (400) envelope on
# both transports without parsing a raw validator message (mirrors
# _validate_document_id). The `argument` is the alias name -- the validator is
# shared across fields, so it labels by type, not by the field that failed.
# ---------------------------------------------------------------------------

_TYPED_ALIAS_FAMILY_CONTRACT = [
    ("invalid_vault_id", VaultIdStr, "not a vault id!", "vault_id"),
    ("invalid_edge_id", EdgeIdStr, "not-a-uuid", "edge_id"),
    ("invalid_sha256", Sha256Str, "deadbeef", "sha256"),
    ("invalid_function_id", FunctionIdStr, "not-a-fn", "function_id"),
    ("invalid_document_date", DocumentDateStr, "2026-13-99", "document_date"),
    ("invalid_user_id", UserIdStr, "not-a-uuid", "user_id"),
]


@pytest.mark.parametrize(
    "code,alias,bad_value,argument",
    _TYPED_ALIAS_FAMILY_CONTRACT,
    ids=[c for c, *_ in _TYPED_ALIAS_FAMILY_CONTRACT],
)
def test_typed_alias_validator_raises_structured_custom_error(
    code: str, alias: type, bad_value: str, argument: str
) -> None:
    """Each sibling reject validator raises a PydanticCustomError whose external
    type is its invalid_<alias> code and whose ctx carries the uniform
    {argument, value, expected} triple -- the leaf-layer half of the contract
    that translate_validation_error consumes."""
    from pydantic import TypeAdapter, ValidationError

    with pytest.raises(ValidationError) as excinfo:
        TypeAdapter(alias).validate_python(bad_value)
    err = excinfo.value.errors()[0]
    assert err["type"] == code, err
    ctx = err["ctx"]
    assert ctx["argument"] == argument, ctx
    assert ctx["value"] == bad_value, ctx
    assert ctx["expected"], ctx


# ---------------------------------------------------------------------------
# Collection-shaped sites
#
# The suffix patterns match by ``endswith``, so a plural name such as
# ``document_ids`` or ``hashes`` matched nothing and its site escaped all
# three gates. Two mechanisms had to move together: the registry has to name
# the plural forms, and the annotation walker has to descend into a
# collection's element type -- a ``list[str]`` field reports no ``str`` arm
# under a walker that stops at the container, so a registry edit alone leaves
# the discovery sets unchanged. These tests pin both, and pin the boundary
# that keeps the widening from overshooting onto boolean flags.
# ---------------------------------------------------------------------------


def test_walk_annotation_descends_into_collection_element_types():
    """A collection carries its shape on the element type, not the container.

    Both edges of the descent rule are pinned. ``list`` is the only sequence
    with a live site today, so the other three in ``_SEQUENCE_ORIGINS`` would
    otherwise be unfalsifiable breadth -- narrowing the set to ``{list}`` alone
    would leave the suite green while the declared rule quietly shrank.
    """
    assert _walk_annotation(list[str] | None) == (True, False)
    assert _walk_annotation(list[DocumentIdStr]) == (True, True)
    # Inclusion breadth: every declared sequence origin descends, not just the
    # one that happens to have a site.
    assert _walk_annotation(tuple[DocumentIdStr, ...]) == (True, True)
    assert _walk_annotation(set[DocumentIdStr]) == (True, True)
    assert _walk_annotation(frozenset[DocumentIdStr]) == (True, True)
    # Exclusion boundary: descent is scoped to sequence containers. A mapping's
    # ``str`` key is not a shape-bearing arm, and treating it as one would flag
    # every ``dict[str, ...]`` field whose name matches a plural pattern.
    assert _walk_annotation(dict[str, int]) == (False, False)
    assert _walk_annotation(dict[str, DocumentIdStr]) == (False, False)
    # The overshoot boundary: a boolean flag has no ``str`` arm at any depth.
    assert _walk_annotation(bool) == (False, False)


def test_registry_enumerates_plural_collection_sites():
    """The widened registry surfaces every collection-shaped site.

    Without this the widening is unobservable: a registry edit that matched
    nothing would leave every other test in this file green, because each one
    is parametrized off the discovery sets it would have emptied.
    """
    model_sites = {(cls.__name__, name) for cls, name, _ in _shape_bearing_fields()}
    for site in (
        ("RetrievalFilters", "document_ids"),
        ("HashCheckRequest", "hashes"),
        ("SetEditorsRequest", "user_ids"),
        ("Tier3UniquenessCollision", "document_ids"),
    ):
        assert site in model_sites, f"{site} escaped the BaseModel gate; got {sorted(model_sites)}"

    tool_sites = {
        (f"{fn.__module__}.{fn.__name__}", name) for fn, name, _ in _discover_fastmcp_tool_params()
    }
    for site in (
        ("sage.sage_api_tools.verify_hash", "hashes"),
        ("sage.sage_api_tools.search", "document_ids"),
    ):
        assert site in tool_sites, f"{site} escaped the FastMCP gate"


def test_boolean_flag_ending_in_a_shape_word_is_not_enumerated():
    """A boolean flag whose name ends in a shape word stays out of the gate.

    Asserted through the mechanism rather than by absence alone. Absence on
    its own would also hold for a registry that matched nothing, which is the
    failure this widening exists to fix. The name *does* match a registry
    pattern; the ``str``-arm filter is what excludes it.

    Anti-coincidental-pass: excludes a registry narrowed back to its singular
    patterns (the first assertion goes red) and a walker that reports a
    ``str`` arm for ``bool`` (the second does). It does **not** exclude a
    walker widened to descend into every generic rather than sequences only:
    ``bool`` has no origin, so that overshoot leaves this flag untouched and
    every assertion here still passes. The guard for that rival is the
    ``dict[str, int]`` case in
    ``test_walk_annotation_descends_into_collection_element_types``, which is
    where a mapping's ``str`` key would start counting as a shape-bearing arm.
    """
    assert _expected_alias("check_hashes") is not None, (
        "expected the widened registry to match the name, with the str-arm "
        "filter -- not the name rule -- doing the exclusion"
    )
    assert _walk_annotation(bool) == (False, False)

    tool_sites = {
        (f"{fn.__module__}.{fn.__name__}", name) for fn, name, _ in _discover_fastmcp_tool_params()
    }
    assert ("sage.sage_api_tools.verify_vault_source_files", "check_hashes") not in tool_sites


def test_module_typeadapter_bindings_resolve_collection_adapters():
    """A collection adapter is discoverable as a Pattern 2 binding.

    ``TypeAdapter(list[Sha256Str])`` is an ``ast.Subscript``, not the
    ``ast.Name`` the scalar form produces. Left unresolved, the Pattern 2
    detector cannot see the adapter and the tool fails the gate despite
    validating its parameter correctly.
    """
    from sage import sage_api_tools

    bindings = _module_typeadapter_bindings(sage_api_tools)
    assert bindings.get("_SHA256_LIST_ADAPTER") is Sha256Str, sorted(bindings)
    # The scalar bindings are unaffected.
    assert bindings.get("_DOCUMENT_ID_ADAPTER") is DocumentIdStr, sorted(bindings)


def test_module_typeadapter_bindings_unwrap_only_declared_sequences(tmp_path) -> None:
    """Only a subscript whose container is a declared sequence unwraps.

    Exercised against a synthesized module because no production module
    declares a non-sequence adapter subscript, which is exactly what leaves
    the container check unfalsifiable in place. Without it the rule degrades
    to "unwrap any single-argument subscript", and the gate would credit an
    adapter whose element position it has not reasoned about as though it
    validated the element.
    """
    import importlib.util

    probe = tmp_path / "adapter_binding_probe.py"
    probe.write_text(
        "from typing import Optional\n\n"
        "from pydantic import TypeAdapter\n\n"
        "from sage.models.schemas import DocumentIdStr\n\n"
        "SEQ_ADAPTER: TypeAdapter[list[str]] = TypeAdapter(list[DocumentIdStr])\n"
        "NON_SEQ_ADAPTER: TypeAdapter[str] = TypeAdapter(Optional[DocumentIdStr])\n"
    )
    spec = importlib.util.spec_from_file_location("adapter_binding_probe", probe)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    bindings = _module_typeadapter_bindings(module)
    assert bindings.get("SEQ_ADAPTER") is DocumentIdStr, sorted(bindings)
    assert "NON_SEQ_ADAPTER" not in bindings, (
        f"a non-sequence subscript must not unwrap to its argument; got {sorted(bindings)}"
    )
