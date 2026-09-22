"""Architectural-conformance tests for the MCP tool surface.

Gates the structural alignment between the MCP tool surface
(``sage/sage_api_tools.py``, ``sage/app_tools.py``,
``sage/mcp_server.py``) and the OpenAPI substrate (``docs/fs/sage/``,
``docs/fs/cas_app_api.openapi.yaml``, ``docs/fs/root_harness/``).

Mirrors ``test_router_conformance.py``: a small ``ToolSurface`` tuple
declares each surface; per-element parametrized tests assert that the
MCP surface and the OpenAPI surface agree on names, operation coverage,
and per-argument shapes. A divergence pending remediation drains from its
register when remediated; a permanent category does not.

Conformance interpretation: schema-subset. Each MCP tool argument
must match a parameter or
requestBody field of its OpenAPI counterpart by name and compatible
type. Tools may expose a strict subset of OpenAPI inputs. The MCP
transport's JSON-string-as-carrier convention is encoded in the type
table below: a Python ``str`` argument may stand in for an OpenAPI
``object`` or ``array`` field (e.g. ``search(filters: str)``
where the spec declares ``filters`` as an object). The tolerance is
asymmetric and scoped: ``int`` cannot stand in for ``object``, etc.

The check is bi-directional at both levels. Every MCP tool must map to an
OpenAPI operation and every OpenAPI operation to an MCP tool; every
argument of a mapped tool must appear on its operation and every
parameter or request-body field of the operation on its tool. A gap in
any of the four directions is admitted only by an entry in the matching
register in ``surface_divergences.py`` -- ``MCP_ONLY_TOOLS``,
``REST_ONLY_OPERATIONS``, ``MCP_ONLY_ARGUMENTS``, ``REST_ONLY_ARGUMENTS``
-- and every register fails on a stale entry: one whose divergence has
closed, or one naming a tool, operation, or mapped pair that no longer exists.

Under CAS-ADR-052 a divergence between the surfaces is admissible only
in a closed set of categories, and doubt resolves to parity. Each
register entry therefore names a ``DivergenceCategory``; an entry
justified in prose alone fails here. The register's home is the
*Surface divergences* section of the *SAGE MCP Tool Surface* steering
document, which ``surface_divergences.py`` transcribes. Entries pending
remediation are printed at the end of any run that exercises this module.

Refusal parity -- an undeclared argument refused on the same terms by
both surfaces -- is gated by ``test_rest_request_strictness_conformance.py``.

The ROOT Harness Orchestration spec exists today but no MCP tools yet
implement its operations; the entire spec is registered as pending
remediation at the operation level until those tools land.
"""

from __future__ import annotations

import functools
import inspect
import re
import types
import typing
from pathlib import Path
from typing import Any, Callable, Final, NamedTuple

import pytest
import yaml

from tests.helpers.published_tool import published_tools, published_tools_on
from tests.sage.surface_divergences import (
    MCP_ONLY_ARGUMENTS,
    MCP_ONLY_TOOLS,
    REGISTERS,
    REST_ONLY_ARGUMENTS,
    REST_ONLY_OPERATIONS,
    Divergence,
    DivergenceCategory,
    pending_remediation_lines,
    uncategorized,
)

# ---------------------------------------------------------------------------
# Paths to OpenAPI specs (reused from test_openapi_conformance.py)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
SAGE_CORE_SPEC_PATH = _REPO_ROOT / "docs" / "fs" / "sage" / "sage_core_api.openapi.yaml"
CAS_APP_SPEC_PATH = _REPO_ROOT / "docs" / "fs" / "cas_app_api.openapi.yaml"
ROOT_HARNESS_SPEC_PATH = (
    _REPO_ROOT / "docs" / "fs" / "root_harness" / "orchestration_api.openapi.yaml"
)


# ---------------------------------------------------------------------------
# Tool-surface configuration
# ---------------------------------------------------------------------------


class ToolSurface(NamedTuple):
    name: str
    spec_path: Path
    tool_registry_attr: str | None
    tool_prefix: str
    operation_prefix: str


# Each surface pairs an OpenAPI spec with the module attribute on
# ``sage.mcp_server`` that holds the registered MCP tool dict.
# ``tool_registry_attr=None`` means "no MCP surface exists for this
# spec yet"; the gate runs the operation-coverage direction only and
# expects every operation to be registered in REST_ONLY_OPERATIONS until
# the tools land. ``operation_prefix`` and ``tool_prefix`` are both
# empty for the SAGE surfaces post the verb-convention rename: MCP
# tool names match OpenAPI operationIds directly (per CAS-ADR-033).
TOOL_SURFACES: tuple[ToolSurface, ...] = (
    ToolSurface(
        name="sage_core",
        spec_path=SAGE_CORE_SPEC_PATH,
        tool_registry_attr="_sage_tools",
        tool_prefix="",
        operation_prefix="",
    ),
    ToolSurface(
        name="cas_app",
        spec_path=CAS_APP_SPEC_PATH,
        tool_registry_attr="_app_tools",
        tool_prefix="",
        operation_prefix="",
    ),
    ToolSurface(
        name="root_harness",
        spec_path=ROOT_HARNESS_SPEC_PATH,
        tool_registry_attr=None,
        tool_prefix="root_",
        operation_prefix="",
    ),
)

_SURFACES_BY_NAME: dict[str, ToolSurface] = {s.name: s for s in TOOL_SURFACES}


# ---------------------------------------------------------------------------
# Allowlists
# ---------------------------------------------------------------------------

# (surface_name, tool_name) -> operation_id when the tool name does
# not equal "<tool_prefix>" + operation_id. After the verb-convention
# rename (CAS-ADR-033), MCP tool names equal OpenAPI operationIds for
# the SAGE surfaces; this allowlist is empty.
OPERATION_RENAMES: dict[tuple[str, str], str] = {}

# The divergence registers -- MCP-only tools, REST-only operations, and the
# arguments present on one surface only -- live in ``surface_divergences.py``,
# each entry carrying its CAS-ADR-052 category.


# ---------------------------------------------------------------------------
# Spec loading and helpers
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=None)
def _load_spec(path: Path) -> dict[str, Any]:
    with path.open() as f:
        return yaml.safe_load(f)


def _resolve_ref(spec: dict[str, Any], ref: str) -> dict[str, Any]:
    """Resolve a local ``#/components/...`` reference to its target node."""
    assert ref.startswith("#/"), f"non-local $ref not supported: {ref}"
    node: Any = spec
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _all_operation_ids(spec: dict[str, Any]) -> set[str]:
    """Return every ``operationId`` declared in the spec's paths."""
    ids: set[str] = set()
    for path_item in spec.get("paths", {}).values():
        for method, op in path_item.items():
            if method.lower() in {"get", "post", "put", "patch", "delete"} and isinstance(op, dict):
                op_id = op.get("operationId")
                if op_id:
                    ids.add(op_id)
    return ids


def _find_operation(spec: dict[str, Any], operation_id: str) -> dict[str, Any] | None:
    """Return the operation node with the given operationId, or None."""
    for path_item in spec.get("paths", {}).values():
        for method, op in path_item.items():
            if method.lower() in {"get", "post", "put", "patch", "delete"} and isinstance(op, dict):
                if op.get("operationId") == operation_id:
                    return op
    return None


def _operation_parameters(
    spec: dict[str, Any], op: dict[str, Any]
) -> dict[str, tuple[str | None, bool]]:
    """Return ``{name: (openapi_type, required)}`` for an operation.

    Combines path/query/header parameters and requestBody schema
    properties. ``openapi_type`` is None if the parameter's schema
    cannot be resolved to a single primitive type (e.g. oneOf without
    a uniform ``type``); callers treat None as "type-compat check is
    a no-op for this field, name-match still applies".
    """
    fields: dict[str, tuple[str | None, bool]] = {}

    for param in op.get("parameters", []):
        if "$ref" in param:
            param = _resolve_ref(spec, param["$ref"])
        name = param.get("name")
        if not name:
            continue
        required = bool(param.get("required", False))
        schema = param.get("schema", {})
        fields[name] = (_schema_type(spec, schema), required)

    body = op.get("requestBody")
    if body:
        if "$ref" in body:
            body = _resolve_ref(spec, body["$ref"])
        content = body.get("content", {})
        json_content = content.get("application/json")
        if json_content:
            schema = json_content.get("schema", {})
            if "$ref" in schema:
                schema = _resolve_ref(spec, schema["$ref"])
            required_fields = set(schema.get("required", []))
            for prop_name, prop_schema in (schema.get("properties") or {}).items():
                fields[prop_name] = (
                    _schema_type(spec, prop_schema),
                    prop_name in required_fields,
                )

    return fields


def _schema_type(spec: dict[str, Any], schema: dict[str, Any]) -> str | None:
    """Extract a single OpenAPI primitive type from a property schema.

    Follows ``$ref`` once. Returns the ``type`` of the resolved node.
    For schemas that combine types via ``oneOf``/``anyOf``/``allOf``
    without a single ``type`` field, returns None (caller treats as
    "skip type check").
    """
    if "$ref" in schema:
        schema = _resolve_ref(spec, schema["$ref"])
    return schema.get("type")


# ---------------------------------------------------------------------------
# Python -> OpenAPI type mapping
# ---------------------------------------------------------------------------

# Python concrete type -> set of OpenAPI types it may stand in for.
# ``str`` is asymmetrically tolerant: complex JSON args are
# transported as JSON-encoded strings over MCP, so a Python ``str``
# parameter may match an OpenAPI ``object`` or ``array`` field.
_TYPE_COMPAT: dict[type, frozenset[str]] = {
    str: frozenset({"string", "object", "array"}),
    int: frozenset({"integer"}),
    float: frozenset({"number"}),
    bool: frozenset({"boolean"}),
    list: frozenset({"array"}),
    dict: frozenset({"object"}),
}


def _python_types_and_optional(annotation: Any) -> tuple[frozenset[type], bool]:
    """Return ``(concrete_types, is_optional)`` for a Python annotation.

    Strips ``Optional[X]`` / ``X | None`` and reports whether ``None``
    was present. Returns the empty set for annotations the test can't
    reduce to a concrete type (e.g. unbound TypeVars); callers treat
    an empty set as "skip type check, name-match still applies".
    """
    optional = False
    # A published description wraps the type in ``Annotated``; compare the
    # type it wraps, or every described parameter skips the type check.
    if typing.get_origin(annotation) is typing.Annotated:
        annotation = typing.get_args(annotation)[0]
    origin = typing.get_origin(annotation)

    if origin is typing.Union or origin is types.UnionType:
        members = [m for m in typing.get_args(annotation) if m is not type(None)]
        optional = type(None) in typing.get_args(annotation)
        types_acc: set[type] = set()
        for m in members:
            sub_types, sub_optional = _python_types_and_optional(m)
            types_acc |= sub_types
            optional = optional or sub_optional
        return frozenset(types_acc), optional

    if origin is list:
        return frozenset({list}), False
    if origin is dict:
        return frozenset({dict}), False
    if annotation in _TYPE_COMPAT:
        return frozenset({annotation}), False
    return frozenset(), False


def _declared_types(openapi_type: str | list | None) -> frozenset[str]:
    """The non-null types a ``type`` keyword declares.

    OpenAPI 3.1 expresses nullability as a type array -- ``[string,
    "null"]`` -- rather than with 3.0's ``nullable`` keyword, so this
    keyword is a bare string on some properties and a list on others.
    Nullability is not what this gate compares, so ``"null"`` is dropped
    and the remaining members are what a Python annotation must stand in
    for.
    """
    if openapi_type is None:
        return frozenset()
    if isinstance(openapi_type, str):
        return frozenset({openapi_type})
    return frozenset(t for t in openapi_type if t != "null")


def _types_compatible(py_types: frozenset[type], openapi_type: str | list | None) -> bool:
    """Return whether any Python type in the set may stand in for the OpenAPI type."""
    declared = _declared_types(openapi_type)
    if not declared:
        return True
    if not py_types:
        return True
    for t in py_types:
        if declared & _TYPE_COMPAT.get(t, frozenset()):
            return True
    return False


# ---------------------------------------------------------------------------
# MCP registry access
# ---------------------------------------------------------------------------


def _surface_registry(surface: ToolSurface) -> dict[str, Callable[..., Any]]:
    """Return the registered MCP tool dict for the surface, or empty."""
    if surface.tool_registry_attr is None:
        return {}
    from sage import mcp_server

    return getattr(mcp_server, surface.tool_registry_attr)


def _resolve_expected_operation_id(surface: ToolSurface, tool_name: str) -> str:
    """Map an MCP tool name to its expected operationId.

    Strips the surface's ``tool_prefix`` and prepends its
    ``operation_prefix``. For sage_core: ``ingest_document`` -> ``ingest``.
    For cas_app: ``list_directory`` -> ``list_directory`` (the
    operation_prefix matches the tool_prefix).
    """
    override = OPERATION_RENAMES.get((surface.name, tool_name))
    if override is not None:
        return override
    assert tool_name.startswith(surface.tool_prefix), (
        f"Tool {tool_name!r} on surface {surface.name!r} does not start with "
        f"the surface's prefix {surface.tool_prefix!r}."
    )
    stem = tool_name[len(surface.tool_prefix) :]
    return f"{surface.operation_prefix}{stem}"


def _resolve_expected_tool_name(surface: ToolSurface, operation_id: str) -> str:
    """Map an OpenAPI operationId to its expected MCP tool name."""
    for (surf, tool), op_id in OPERATION_RENAMES.items():
        if surf == surface.name and op_id == operation_id:
            return tool
    assert operation_id.startswith(surface.operation_prefix), (
        f"operationId {operation_id!r} on surface {surface.name!r} does not "
        f"start with the surface's operation prefix "
        f"{surface.operation_prefix!r}."
    )
    stem = operation_id[len(surface.operation_prefix) :]
    return f"{surface.tool_prefix}{stem}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surface_name", sorted(s.name for s in TOOL_SURFACES))
def test_tool_registry_matches_surface_prefix(surface_name: str):
    """Every registered tool's name must start with its surface's prefix."""
    surface = _SURFACES_BY_NAME[surface_name]
    registry = _surface_registry(surface)
    offenders = [name for name in registry if not name.startswith(surface.tool_prefix)]
    assert not offenders, (
        f"Surface {surface_name!r} registry contains tool(s) {offenders!r} "
        f"that do not start with prefix {surface.tool_prefix!r}. Either fix "
        "the tool name or move it to the correct surface."
    )


def test_no_tool_appears_in_two_surfaces():
    """A tool name must belong to exactly one surface registry."""
    seen: dict[str, str] = {}
    collisions: list[tuple[str, str, str]] = []
    for surface in TOOL_SURFACES:
        for tool_name in _surface_registry(surface):
            if tool_name in seen:
                collisions.append((tool_name, seen[tool_name], surface.name))
            else:
                seen[tool_name] = surface.name
    assert not collisions, (
        f"Tool name(s) appear in multiple surface registries: {collisions!r}. "
        "Each tool must belong to exactly one surface."
    )


def _tool_id_pairs() -> list[tuple[str, str]]:
    """``(surface_name, tool_name)`` pairs for every registered MCP tool."""
    pairs: list[tuple[str, str]] = []
    for surface in TOOL_SURFACES:
        for tool_name in sorted(_surface_registry(surface)):
            pairs.append((surface.name, tool_name))
    return pairs


@pytest.mark.parametrize(
    ("surface_name", "tool_name"),
    _tool_id_pairs(),
    ids=[f"{s}-{t}" for s, t in _tool_id_pairs()],
)
def test_mcp_tool_has_openapi_counterpart(surface_name: str, tool_name: str):
    """Each registered MCP tool maps to an OpenAPI operation or is registered as divergent."""
    surface = _SURFACES_BY_NAME[surface_name]
    spec = _load_spec(surface.spec_path)
    expected_op_id = _resolve_expected_operation_id(surface, tool_name)
    op = _find_operation(spec, expected_op_id)
    divergent = (surface_name, tool_name) in MCP_ONLY_TOOLS

    if op is None and divergent:
        return  # registered divergence; its category is gated separately
    if op is None and not divergent:
        pytest.fail(
            f"MCP tool {tool_name!r} on surface {surface_name!r} has no OpenAPI "
            f"operation (expected operationId {expected_op_id!r}). Either add "
            "the operation to the spec, file an OPERATION_RENAMES override, or "
            "register (surface, tool) in MCP_ONLY_TOOLS as a Divergence naming "
            "its CAS-ADR-052 category."
        )
    if op is not None and divergent:
        pytest.fail(
            f"MCP tool {tool_name!r} on surface {surface_name!r} is registered "
            f"in MCP_ONLY_TOOLS but OpenAPI now has operationId "
            f"{expected_op_id!r}. Remove the stale register entry."
        )


def _operation_id_pairs() -> list[tuple[str, str]]:
    """``(surface_name, operation_id)`` pairs for every operation in every spec."""
    pairs: list[tuple[str, str]] = []
    for surface in TOOL_SURFACES:
        spec = _load_spec(surface.spec_path)
        for op_id in sorted(_all_operation_ids(spec)):
            pairs.append((surface.name, op_id))
    return pairs


@pytest.mark.parametrize(
    ("surface_name", "operation_id"),
    _operation_id_pairs(),
    ids=[f"{s}-{o}" for s, o in _operation_id_pairs()],
)
def test_openapi_operation_has_mcp_tool(surface_name: str, operation_id: str):
    """Each OpenAPI operation has an MCP tool or is registered as divergent."""
    surface = _SURFACES_BY_NAME[surface_name]
    registry = _surface_registry(surface)
    expected_tool = _resolve_expected_tool_name(surface, operation_id)
    present = expected_tool in registry
    http_only = (surface_name, operation_id) in REST_ONLY_OPERATIONS

    if not present and http_only:
        return  # registered divergence; its category is gated separately
    if not present and not http_only:
        pytest.fail(
            f"OpenAPI operationId {operation_id!r} on surface {surface_name!r} "
            f"has no MCP tool (expected tool name {expected_tool!r}). Either "
            "add the MCP tool, add an OPERATION_RENAMES override if the tool "
            "exists under a different name, or register (surface, operation_id) "
            "in REST_ONLY_OPERATIONS as a Divergence naming its CAS-ADR-052 "
            "category."
        )
    if present and http_only:
        pytest.fail(
            f"OpenAPI operationId {operation_id!r} on surface {surface_name!r} "
            f"is registered in REST_ONLY_OPERATIONS but MCP tool "
            f"{expected_tool!r} is now registered. Remove the stale entry."
        )


def _mapped_tool_pairs() -> list[tuple[str, str]]:
    """Tool pairs that resolve to an OpenAPI operation (excludes MCP_ONLY_TOOLS)."""
    pairs: list[tuple[str, str]] = []
    for surface_name, tool_name in _tool_id_pairs():
        if (surface_name, tool_name) in MCP_ONLY_TOOLS:
            continue
        pairs.append((surface_name, tool_name))
    return pairs


def _registered_arguments(
    register: dict[tuple[str, str, str], Divergence], surface_name: str, tool_name: str
) -> set[str]:
    """The arguments a per-argument register admits for one tool."""
    return {arg for (surf, tool, arg) in register if (surf, tool) == (surface_name, tool_name)}


def _argument_gaps(
    present: set[str], declared: set[str], allowed: set[str]
) -> tuple[set[str], set[str]]:
    """Compare one surface's arguments against the other's, net of a register.

    ``present`` are the names on the surface being checked and ``declared``
    those on its counterpart. Returns ``(new, stale)``: names present but
    neither declared nor registered, and registered names that no longer
    diverge.
    """
    missing = present - declared
    return missing - allowed, allowed - missing


def _mapped_operation(surface_name: str, tool_name: str) -> tuple[str, dict[str, Any]]:
    surface = _SURFACES_BY_NAME[surface_name]
    op_id = _resolve_expected_operation_id(surface, tool_name)
    op = _find_operation(_load_spec(surface.spec_path), op_id)
    assert op is not None, (
        f"Internal: expected operation {op_id!r} to exist (covered by "
        "test_mcp_tool_has_openapi_counterpart)."
    )
    return op_id, op


def _tool_arguments(surface_name: str, tool_name: str) -> dict[str, inspect.Parameter]:
    tool_fn = _surface_registry(_SURFACES_BY_NAME[surface_name])[tool_name]
    return {
        name: param
        for name, param in inspect.signature(tool_fn).parameters.items()
        if param.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    }


@pytest.mark.parametrize(
    ("surface_name", "tool_name"),
    _mapped_tool_pairs(),
    ids=[f"{s}-{t}" for s, t in _mapped_tool_pairs()],
)
def test_mcp_tool_args_conform_to_openapi(surface_name: str, tool_name: str):
    """Schema-subset check: every MCP tool arg matches an OpenAPI param or field.

    For each MCP tool argument: the name must appear in the union of
    OpenAPI parameters and requestBody schema properties; the Python
    type must be compatible with the OpenAPI type (subject to the
    JSON-string-as-carrier tolerance for ``str``).
    """
    surface = _SURFACES_BY_NAME[surface_name]
    op_id, op = _mapped_operation(surface_name, tool_name)
    openapi_fields = _operation_parameters(_load_spec(surface.spec_path), op)
    arguments = _tool_arguments(surface_name, tool_name)

    new_drift, stale_register = _argument_gaps(
        present=set(arguments),
        declared=set(openapi_fields),
        allowed=_registered_arguments(MCP_ONLY_ARGUMENTS, surface_name, tool_name),
    )
    assert not new_drift, (
        f"MCP tool {tool_name!r} (operationId {op_id!r}) exposes argument(s) "
        f"{sorted(new_drift)!r} that do not appear in the OpenAPI operation's "
        "parameters or requestBody schema. Either rename the MCP argument to "
        "match the spec, add the field to the spec, or register "
        "(surface, tool, argument) in MCP_ONLY_ARGUMENTS as a Divergence naming "
        "its CAS-ADR-052 category."
    )
    assert not stale_register, (
        f"MCP tool {tool_name!r} (operationId {op_id!r}) is registered in "
        f"MCP_ONLY_ARGUMENTS for argument(s) {sorted(stale_register)!r} but no "
        "longer exhibits the gap. Remove the stale entries."
    )

    type_violations: list[str] = []
    for param_name, param in arguments.items():
        if param_name not in openapi_fields:
            continue
        openapi_type, _ = openapi_fields[param_name]
        py_types, _ = _python_types_and_optional(param.annotation)
        if not _types_compatible(py_types, openapi_type):
            py_repr = sorted(t.__name__ for t in py_types) or [repr(param.annotation)]
            type_violations.append(f"{param_name}: python={py_repr} openapi={openapi_type!r}")

    assert not type_violations, (
        f"MCP tool {tool_name!r} (operationId {op_id!r}) has argument(s) with "
        f"types incompatible with the OpenAPI schema: {type_violations!r}. "
        "Adjust either side so the types line up (consult the type-compat "
        "table in the test module docstring)."
    )


# ---------------------------------------------------------------------------
# Divergence categories (CAS-ADR-052)
# ---------------------------------------------------------------------------


def test_uncategorized_reports_each_malformed_entry():
    """Each way an entry can fail to name an admissible category is reported.

    The raw-string arm matters most: ``DivergenceCategory`` is a ``StrEnum``,
    so a bare ``"translation artifact"`` compares equal to its member and a
    membership test by equality would admit it.
    """
    register = {
        ("s", "bare_string"): "Justified in prose alone.",
        ("s", "raw_category"): Divergence("translation artifact", "x"),
        ("s", "no_category"): Divergence(None, "x"),
        ("s", "empty_basis"): Divergence(DivergenceCategory.DELIVERY_FORM, ""),
        ("s", "well_formed"): Divergence(DivergenceCategory.DELIVERY_FORM, "x"),
    }

    # A second register, so a walk that stops after the first is caught.
    later = {("s", "later_bare_string"): "Justified in prose alone."}

    reported = uncategorized((("SYNTHETIC", register), ("LATER", later)))

    assert len(reported) == 5, reported
    for key in ("bare_string", "raw_category", "no_category", "empty_basis", "later_bare_string"):
        assert sum(f"'{key}'" in line for line in reported) == 1, (key, reported)
    assert not any("well_formed" in line for line in reported)


def test_pending_remediation_lines_report_only_pending_entries():
    register = {
        ("s", "permanent_tool"): Divergence(DivergenceCategory.OPERATION_FACTORING, "factored"),
        ("s", "pending_tool"): Divergence(
            DivergenceCategory.PENDING_REMEDIATION, "add the REST operation"
        ),
    }

    lines = pending_remediation_lines((("SYNTHETIC", register),))

    assert len(lines) == 1, lines
    assert "pending_tool" in lines[0]
    assert "add the REST operation" in lines[0]
    assert "SYNTHETIC" in lines[0]


def test_argument_gaps_reports_new_and_stale():
    new, stale = _argument_gaps(present={"a", "b"}, declared={"a"}, allowed={"b", "c"})
    assert (new, stale) == (set(), {"c"})

    new, stale = _argument_gaps(present={"a", "b"}, declared={"a"}, allowed=set())
    assert (new, stale) == ({"b"}, set())


def test_every_divergence_names_an_admissible_category():
    # The walk covers exactly the registers the gate consults; a register
    # left out of REGISTERS would escape the category check unnoticed.
    assert list(REGISTERS) == [
        ("MCP_ONLY_TOOLS", MCP_ONLY_TOOLS),
        ("REST_ONLY_OPERATIONS", REST_ONLY_OPERATIONS),
        ("MCP_ONLY_ARGUMENTS", MCP_ONLY_ARGUMENTS),
        ("REST_ONLY_ARGUMENTS", REST_ONLY_ARGUMENTS),
    ]
    assert all(register is globals()[name] for name, register in REGISTERS)
    assert uncategorized(REGISTERS) == []

    sizes = {name: len(register) for name, register in REGISTERS}
    assert all(size > 0 for name, size in sizes.items() if name != "REST_ONLY_ARGUMENTS"), sizes
    assert sum(sizes.values()) >= 50, sizes


def test_divergence_bases_cite_no_tickets():
    """A basis states a protocol reason or the closing work, never a ticket id."""
    ticket = re.compile(r"\bT-\d{4}\b")
    citing = [
        f"{name}{key!r}"
        for name, register in REGISTERS
        for key, entry in register.items()
        if ticket.search(repr(key)) or ticket.search(entry.basis)
    ]
    assert citing == []


def test_pending_remediation_lines_list_exactly_the_pending_entries():
    pending = {
        (name, key)
        for name, register in REGISTERS
        for key, entry in register.items()
        if entry.category is DivergenceCategory.PENDING_REMEDIATION
    }
    permanent = {
        (name, key)
        for name, register in REGISTERS
        for key, entry in register.items()
        if entry.category is not DivergenceCategory.PENDING_REMEDIATION
    }

    lines = pending_remediation_lines(REGISTERS)

    # Population pin: recategorizing or remediating an entry is a deliberate
    # edit here as well as in the register.
    assert len(pending) == 11
    assert len(lines) == len(pending)
    for name, key in pending:
        entry = dict(REGISTERS)[name][key]
        assert sum(f"{name} {'/'.join(key)}: {entry.basis}" == line for line in lines) == 1
    for name, key in permanent:
        assert not any(line.startswith(f"{name} {'/'.join(key)}:") for line in lines)


@pytest.mark.parametrize(
    ("surface_name", "tool_name"),
    _mapped_tool_pairs(),
    ids=[f"{s}-{t}" for s, t in _mapped_tool_pairs()],
)
def test_rest_operation_args_appear_on_mcp_tool(surface_name: str, tool_name: str):
    """Every parameter or request-body field of a mapped operation is on its tool."""
    surface = _SURFACES_BY_NAME[surface_name]
    op_id, op = _mapped_operation(surface_name, tool_name)

    new_gap, stale_register = _argument_gaps(
        present=set(_operation_parameters(_load_spec(surface.spec_path), op)),
        declared=set(_tool_arguments(surface_name, tool_name)),
        allowed=_registered_arguments(REST_ONLY_ARGUMENTS, surface_name, tool_name),
    )
    assert not new_gap, (
        f"OpenAPI operation {op_id!r} accepts argument(s) {sorted(new_gap)!r} that "
        f"MCP tool {tool_name!r} does not. Add them to the tool, or register "
        "(surface, tool, argument) in REST_ONLY_ARGUMENTS as a Divergence naming "
        "its CAS-ADR-052 category."
    )
    assert not stale_register, (
        f"MCP tool {tool_name!r} (operationId {op_id!r}) is registered in "
        f"REST_ONLY_ARGUMENTS for argument(s) {sorted(stale_register)!r} but no "
        "longer exhibits the gap. Remove the stale entries."
    )


def test_rest_argument_walk_is_not_empty():
    """The REST-to-MCP direction would pass vacuously on empty field sets."""
    fields = {
        (surface_name, tool_name): set(
            _operation_parameters(
                _load_spec(_SURFACES_BY_NAME[surface_name].spec_path),
                _mapped_operation(surface_name, tool_name)[1],
            )
        )
        for surface_name, tool_name in _mapped_tool_pairs()
    }

    assert sum(len(names) for names in fields.values()) >= 100
    assert "filters" in fields[("sage_core", "search")]
    assert "transfer_token" in fields[("sage_core", "ingest_document")]

    unwalked = [
        f"{surface_name}/{tool_name}: {reason}"
        for surface_name, tool_name in _mapped_tool_pairs()
        for reason in _unwalked_request_body(
            _load_spec(_SURFACES_BY_NAME[surface_name].spec_path),
            _mapped_operation(surface_name, tool_name)[1],
        )
    ]
    assert unwalked == [], (
        "The argument walk reads only top-level properties of an application/json "
        "request body; these bodies would contribute fields it cannot see."
    )


def _unwalked_request_body(spec: dict[str, Any], op: dict[str, Any]) -> list[str]:
    """Ways an operation's request body escapes ``_operation_parameters``.

    The walk reads the top-level ``properties`` of an ``application/json`` body.
    A body in another media type contributes no fields, and a composed schema
    (``oneOf``/``anyOf``/``allOf``) hides its members' fields whether or not
    top-level properties sit beside it, so the REST-to-MCP direction would pass
    over those fields vacuously.
    """
    body = op.get("requestBody")
    if not body:
        return []
    if "$ref" in body:
        body = _resolve_ref(spec, body["$ref"])
    reasons: list[str] = []
    media_types = set(body.get("content", {}))
    if media_types != {"application/json"}:
        reasons.append(f"media types {sorted(media_types)!r}")
    schema = body.get("content", {}).get("application/json", {}).get("schema", {})
    if "$ref" in schema:
        schema = _resolve_ref(spec, schema["$ref"])
    composed = sorted({"oneOf", "anyOf", "allOf"} & set(schema))
    if composed:
        reasons.append(f"JSON body schema composes with {composed!r}")
    elif "application/json" in media_types and not schema.get("properties"):
        reasons.append("JSON body schema has no top-level properties")
    return reasons


def test_unwalked_request_body_flags_what_the_walk_cannot_read():
    spec: dict[str, Any] = {}
    json_body = {"content": {"application/json": {"schema": {"properties": {"a": {}}}}}}
    composed = {"content": {"application/json": {"schema": {"oneOf": [{}, {}]}}}}
    composed_beside_properties = {
        "content": {"application/json": {"schema": {"allOf": [{}], "properties": {"a": {}}}}}
    }
    multipart = {"content": {"multipart/form-data": {"schema": {"properties": {"a": {}}}}}}

    assert _unwalked_request_body(spec, {}) == []
    assert _unwalked_request_body(spec, {"requestBody": json_body}) == []
    assert len(_unwalked_request_body(spec, {"requestBody": composed})) == 1
    assert len(_unwalked_request_body(spec, {"requestBody": composed_beside_properties})) == 1
    assert len(_unwalked_request_body(spec, {"requestBody": multipart})) == 1


def test_every_register_entry_names_a_live_subject():
    """A register row outliving its subject on both surfaces is stale too.

    The per-element tests consult a register only when their walk over the live
    surfaces reaches its key, so a row for a deleted tool, a removed operation,
    or an argument on a tool that is no longer mapped would never be read.
    """
    registered_tools = {
        (surface.name, tool) for surface in TOOL_SURFACES for tool in _surface_registry(surface)
    }
    operation_ids = {
        (surface.name, op_id)
        for surface in TOOL_SURFACES
        for op_id in _all_operation_ids(_load_spec(surface.spec_path))
    }
    mapped = set(_mapped_tool_pairs())

    orphaned = sorted(
        [f"MCP_ONLY_TOOLS{key!r}" for key in MCP_ONLY_TOOLS if key not in registered_tools]
        + [
            f"REST_ONLY_OPERATIONS{key!r}"
            for key in REST_ONLY_OPERATIONS
            if key not in operation_ids
        ]
        + [
            f"{name}{key!r}"
            for name, register in (
                ("MCP_ONLY_ARGUMENTS", MCP_ONLY_ARGUMENTS),
                ("REST_ONLY_ARGUMENTS", REST_ONLY_ARGUMENTS),
            )
            for key in register
            if key[:2] not in mapped
        ]
    )
    assert orphaned == [], "register rows naming a subject that exists on neither surface"


def test_pending_summary_hook_is_keyed_to_this_module():
    """A rename of this module would otherwise silence the summary with nothing red."""
    from tests.conftest import _SURFACE_CONFORMANCE_GATE

    assert _SURFACE_CONFORMANCE_GATE == f"{Path(__file__).relative_to(_REPO_ROOT).as_posix()}::"


def test_surface_gate_reported_predicate():
    from tests.conftest import _SURFACE_CONFORMANCE_GATE, _surface_gate_reported

    class _Report:
        def __init__(self, nodeid: str) -> None:
            self.nodeid = nodeid

    gate_report = _Report(f"{_SURFACE_CONFORMANCE_GATE}test_example")
    other_report = _Report("tests/sage/test_other.py::test_example")

    assert _surface_gate_reported({"passed": [other_report, gate_report]}) is True
    assert _surface_gate_reported({"passed": [other_report], "": [object()]}) is False


# ---------------------------------------------------------------------------
# List-valued metadata field discipline (CAS-ADR-038 Primitive A)
# ---------------------------------------------------------------------------
#
# Every list-valued metadata field exposed through `update_metadata` (and
# its equivalents) must accept the `{add, remove}` ops-object form, not a
# bare list. This is the surface-level enforcement of CAS-ADR-038's
# binding-scope clause: callers never read-modify-write a list, so two
# parallel adds of distinct values are commutative by construction.
#
# Two gates:
#   - D1 scans the relevant Pydantic models and asserts no bare `list[...]`
#     field slips back in.
#   - D2 asserts every list-valued field discovered is registered for
#     dispatch in `MetadataService.LIST_VALUED_METADATA_FIELDS`. The
#     registry is the runtime side of the same contract; without it,
#     adding a `ListFieldPatch` field to a request model would be a
#     silent no-op at the service layer.
#
# `KNOWN_BARE_LIST_FIELDS` is a forensic-only carveout. It is empty by
# convention; non-empty entries must carry a justification comment.

KNOWN_BARE_LIST_FIELDS: frozenset[tuple[str, str]] = frozenset()


def _iter_list_typed_fields(model_cls) -> list[tuple[str, Any]]:
    """Yield (field_name, annotation) for every field on ``model_cls``
    whose declared annotation resolves to `list[...]` or
    `Optional[list[...]]` (including `list[...] | None`).

    The `ListFieldPatch` patch type is NOT list-typed at the Pydantic
    level — it's a sub-model whose ``add`` / ``remove`` fields hold the
    lists. This helper deliberately surfaces only the wholesale-list
    shape that the gate forbids.
    """
    hits: list[tuple[str, Any]] = []
    for field_name, field_info in model_cls.model_fields.items():
        annotation = field_info.annotation
        if _annotation_is_bare_list(annotation):
            hits.append((field_name, annotation))
    return hits


def _annotation_is_bare_list(annotation: Any) -> bool:
    """True iff the annotation is `list[...]` or a union that contains
    `list[...]` alongside only `None` / `NoneType`."""
    origin = typing.get_origin(annotation)
    if origin is list:
        return True
    if origin in (typing.Union, types.UnionType):
        args = typing.get_args(annotation)
        non_none = [a for a in args if a is not type(None)]
        return len(non_none) == 1 and typing.get_origin(non_none[0]) is list
    return False


def _patch_request_models() -> dict[str, type]:
    """Return the request models the gate scans.

    Centralized so adding a new mutation surface (a future bulk-edge
    request, etc.) is a one-line registry addition rather than a
    test-by-test sprawl.
    """
    from sage.models.schemas import (
        BulkMetadataItem,
        UpdateMetadataRequest,
    )

    return {
        "UpdateMetadataRequest": UpdateMetadataRequest,
        "BulkMetadataItem": BulkMetadataItem,
    }


def test_list_valued_metadata_request_fields_use_ops_object_patch():
    """No field on a metadata-mutation request model may be declared as a
    bare `list[...]`. List-valued fields go through `ListFieldPatch` so
    the ops-object commutativity contract is uniform across the surface.

    Forensic-only carveouts live in ``KNOWN_BARE_LIST_FIELDS``; the gate
    fails when a model carries an unallowlisted bare-list field AND when
    the allowlist is stale (an entry that no longer corresponds to a
    real bare-list field).
    """
    found: set[tuple[str, str]] = set()
    for model_name, model_cls in _patch_request_models().items():
        for field_name, _annotation in _iter_list_typed_fields(model_cls):
            found.add((model_name, field_name))

    unallowed = found - KNOWN_BARE_LIST_FIELDS
    assert not unallowed, (
        f"Metadata-mutation request model(s) carry bare-list field(s): "
        f"{sorted(unallowed)!r}. Use ListFieldPatch instead so parallel "
        "callers can mutate the list without read-modify-write. If this "
        "field genuinely cannot be expressed under the ops-object contract, "
        "add it to KNOWN_BARE_LIST_FIELDS with an inline justification."
    )

    stale_allowlist = KNOWN_BARE_LIST_FIELDS - found
    assert not stale_allowlist, (
        f"KNOWN_BARE_LIST_FIELDS is stale: entries {sorted(stale_allowlist)!r} "
        "no longer correspond to a real bare-list field on a tracked model. "
        "Remove them."
    )


def test_list_valued_metadata_fields_registered_for_dispatch():
    """Every `ListFieldPatch`-typed field on a tracked request model must
    appear in `MetadataService.LIST_VALUED_METADATA_FIELDS`. Catches the
    silent-noop class: a `ListFieldPatch` field added to a model but
    never wired into the service-layer dispatch loop.
    """
    from sage.models.schemas import ListFieldPatch
    from sage.services.metadata import MetadataService

    patch_field_names: set[str] = set()
    for model_cls in _patch_request_models().values():
        for field_name, field_info in model_cls.model_fields.items():
            if _annotation_resolves_to(field_info.annotation, ListFieldPatch):
                patch_field_names.add(field_name)

    registry = MetadataService.LIST_VALUED_METADATA_FIELDS
    missing = patch_field_names - set(registry)
    assert not missing, (
        f"ListFieldPatch field(s) {sorted(missing)!r} present in a metadata-"
        "mutation request model but not registered in "
        "MetadataService.LIST_VALUED_METADATA_FIELDS. Register the field so "
        "the service-layer dispatcher picks it up; otherwise the patch is "
        "validated by Pydantic but silently ignored at apply time."
    )


def test_list_valued_metadata_field_names_match_across_layers():
    """``sage.models.legacy_form.LIST_VALUED_METADATA_FIELD_NAMES`` must equal
    ``MetadataService.LIST_VALUED_METADATA_FIELDS.keys()``.

    Two registries exist by necessity: ``MetadataService`` carries the
    dispatcher's descriptor registry (with per-field accessors that read
    a ``Document``), and ``sage.models.legacy_form`` carries the name-only
    set used by the request-body legacy-form guard. The accessor field
    keeps the descriptor registry in the service layer; the leaf-layer
    contract keeps the guard's set in ``sage.models``. This gate catches
    silent drift between them: a new list-valued metadata field added to
    one registry but not the other would either fail to dispatch or fail
    to reject its bare-list legacy shape with the structured envelope.
    """
    from sage.models.legacy_form import LIST_VALUED_METADATA_FIELD_NAMES
    from sage.services.metadata import MetadataService

    descriptor_names = frozenset(MetadataService.LIST_VALUED_METADATA_FIELDS.keys())
    assert descriptor_names == LIST_VALUED_METADATA_FIELD_NAMES, (
        "List-valued metadata field names diverged between the two registries:\n"
        f"  MetadataService.LIST_VALUED_METADATA_FIELDS: {sorted(descriptor_names)!r}\n"
        f"  sage.models.legacy_form.LIST_VALUED_METADATA_FIELD_NAMES: "
        f"{sorted(LIST_VALUED_METADATA_FIELD_NAMES)!r}\n"
        "Add the field to both, or remove it from both."
    )


def _annotation_resolves_to(annotation: Any, target: type) -> bool:
    """True iff the annotation is exactly ``target`` or ``Optional[target]``
    (including ``target | None``)."""
    if annotation is target:
        return True
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = typing.get_args(annotation)
        non_none = [a for a in args if a is not type(None)]
        return len(non_none) == 1 and non_none[0] is target
    return False


# ---------------------------------------------------------------------------
# Tool annotation declaration (MCP `ToolAnnotations`)
# ---------------------------------------------------------------------------
#
# Every tool advertised on either Streamable HTTP mount must carry an
# explicit `ToolAnnotations` object so a client reading `tools/list` can
# tell a pure read from a write from a destructive operation. The two
# mounts and their partition are anchored to CAS-ADR-034.
#
# Declaration vocabulary. Every `ToolAnnotations` field defaults to None
# and is dropped from the wire, at which point the MCP specification
# applies its own client-side defaults -- `destructiveHint` true and
# `openWorldHint` true. Declaring `readOnlyHint` alone would therefore
# leave additive writers such as `create_edges` reading as destructive
# and every vault-scoped tool reading as open-world. So:
#
#   - `readOnlyHint` is declared on all tools.
#   - `destructiveHint` is declared on every tool that mutates state and
#     left unset on read-only tools, where the specification says it
#     carries no meaning.
#   - `openWorldHint` is False everywhere: SAGE operates on a closed
#     vault domain, and `list_directory` -- the one tool that reaches
#     outside the vault -- reads a caller-local filesystem, which is
#     still a closed, deterministic domain.
#   - `idempotentHint` is deliberately left unset on every tool. Its
#     specification default (false) is already the conservative reading,
#     so omission claims nothing that must later be defended.
#
# `EXPECTED_ANNOTATIONS` is a hand-maintained oracle transcribed from
# each tool's implementation, NOT derived from the registered
# annotations. Deriving it would make the split test a tautology that
# passes for any classification, including an inverted one. (Unlike
# `SERVER_ASSIGNMENT` in `sage/_tool_naming.py`, which registration now
# consumes, this table has no production reader; its independence is
# what makes the annotation gates a check rather than an echo.)
#
# Two gates:
#   - A1 asserts exhaustiveness in both directions. A newly registered
#     tool fails until it is classified here; an entry for a tool that
#     no longer exists fails as stale.
#   - A2 asserts the declared hints equal this table, per tool.
#
# Classification rule: a tool is read-only only if no code path mutates
# vault state. Writing a cached or derived artifact counts as a write.

#: tool name -> (readOnlyHint, destructiveHint, openWorldHint).
#: `destructiveHint` is None exactly for read-only tools.
EXPECTED_ANNOTATIONS: dict[str, tuple[bool, bool | None, bool]] = {
    # -- Read-only ---------------------------------------------------
    "search": (True, None, False),
    "traverse": (True, None, False),
    "chain": (True, None, False),
    "read_section": (True, None, False),
    "list_headings": (True, None, False),
    "list_staging_edges": (True, None, False),
    "list_pending_metadata": (True, None, False),
    "verify_hashes": (True, None, False),
    "verify_preconditions": (True, None, False),
    "get_filename_metadata": (True, None, False),
    # `write_to_path` spills bytes to a caller-named path, but that is
    # caller-directed delivery -- an output channel, not a state change.
    # No code path in either tool mutates vault state, so both stay in
    # the CAS-ADR-034 read spine.
    "get_document": (True, None, False),
    "read_projection": (True, None, False),
    # Reads the caller-local filesystem; side-effect free w.r.t. the vault.
    "list_directory": (True, None, False),
    "list_vaults": (True, None, False),
    "get_vault_config": (True, None, False),
    "get_vault_stats": (True, None, False),
    "get_stack_config": (True, None, False),
    "verify_vault_drift": (True, None, False),
    "verify_vault_source_files": (True, None, False),
    # Runs discover queries and reports a verdict; writes nothing.
    "verify_vault_retrieval": (True, None, False),
    # Builds a scaffold for a vault that need not exist; creates nothing.
    "get_default_vault_config": (True, None, False),
    # -- Write, additive only ----------------------------------------
    # Idempotent on the natural key; inserts only.
    "create_edges": (False, False, False),
    # Creates a new vault; disturbs nothing that already exists.
    "create_vault": (False, False, False),
    # Installs partial UNIQUE indexes when the scan is clean. Idempotent,
    # no data loss.
    "migrate_vault": (False, False, False),
    # Backfills documents left in `abstraction_skipped`; fills gaps
    # rather than overwriting existing abstracts.
    "recompute_deferred_vault_abstracts": (False, False, False),
    # VACUUM (FULL, ANALYZE) rewrites the relation but preserves every
    # row; the maintenance-log entry is an append.
    "optimize_vault_content_store": (False, False, False),
    # -- Write, may be destructive -----------------------------------
    # `force=true` overwrites a record keyed by content hash, and
    # `predecessor_id` applies the supersede transition to the predecessor.
    "ingest_document": (False, True, False),
    # `ingest_document` semantics per item, plus the Tier-1 supersedes
    # auto-transition.
    "bulk_ingest_document": (False, True, False),
    # In-place lifecycle transitions, e.g. active -> archived.
    "update_lifecycles": (False, True, False),
    # Set-or-omit semantics overwrite scalar fields in place.
    "update_metadata": (False, True, False),
    # Deletes a production edge.
    "delete_edge": (False, True, False),
    # The `dismiss` action deletes the staging edge.
    "update_staging_edge": (False, True, False),
    # Overwrites an existing semantic abstract in place.
    "recompute_abstract": (False, True, False),
    # Rewrites projection, chunks, and abstract in place.
    "recompute_pipeline": (False, True, False),
    # Drops the symlink trees with rmtree and rebuilds; non-atomic, with
    # no rollback.
    "recompute_views": (False, True, False),
    # Whole-section replace; `force=true` can orphan documents.
    "update_vault_config": (False, True, False),
    # Tears down and replaces live service objects, disrupting any
    # in-flight work against the vault.
    "reload_vault": (False, True, False),
    # Overwrites a retained source file in place. The copy it replaces is
    # the drifted one an operator asked to have repaired, but the bytes it
    # writes over are unrecoverable, so this is not additive.
    "restore_vault_source_file": (False, True, False),
    # Writes into the vault's storage root and overwrites a file already at
    # the target, whatever it held.
    "export_projection": (False, True, False),
}


#: Registered tool objects on a freshly built partitioned server, keyed by
#: tool name. One enumeration serves this module and the disclosure gates:
#: two roster walks reconciled against the same pin can still answer
#: differently while both pass it.
_registered_tools = published_tools_on


def _all_registered_tools() -> dict[str, Any]:
    """Every tool across both partitioned surfaces, keyed by tool name."""
    return published_tools()


def test_registered_tool_enumeration_is_nonempty():
    """The annotation gates enumerate the whole live roster.

    Anti-vacuity control. If ``_registered_tools`` returned an empty (or
    merely partial) mapping, every assertion in the two annotation gates
    below would pass over nothing at all and the gate would be silently
    disarmed. Reconciling against the hand-maintained roster pin in
    ``tests/sage/mcp_surface_pin.py`` -- a literal registration does not
    read, naming every tool on either surface -- makes both the empty and
    the partial case fail loudly without echoing the table registration
    consumes.
    """
    from tests.sage.mcp_surface_pin import EXPECTED_SURFACE

    names = set(_all_registered_tools())
    assert names == set(EXPECTED_SURFACE), (
        "annotation gates do not enumerate the full tool roster: "
        f"missing {sorted(set(EXPECTED_SURFACE) - names)}, "
        f"extra {sorted(names - set(EXPECTED_SURFACE))}"
    )


def test_every_registered_tool_declares_annotations():
    """No tool is advertised with a null ``annotations`` object."""
    unannotated = sorted(
        name for name, tool in _all_registered_tools().items() if tool.annotations is None
    )
    assert not unannotated, (
        f"tool(s) registered without ToolAnnotations: {unannotated}. "
        "Pass `annotations=` to the `@mcp.tool()` decorator, choosing the "
        "constant from `sage._tool_annotations` that matches the tool's "
        "read/write behaviour."
    )


def test_annotation_table_is_exhaustive_over_live_roster():
    """``EXPECTED_ANNOTATIONS`` covers exactly the registered roster."""
    registered = set(_all_registered_tools())
    tabled = set(EXPECTED_ANNOTATIONS)

    unclassified = registered - tabled
    assert not unclassified, (
        f"tool(s) registered but not classified in EXPECTED_ANNOTATIONS: "
        f"{sorted(unclassified)}. Read each tool's implementation, then add "
        "the expected (readOnlyHint, destructiveHint, openWorldHint) triple "
        "with a comment justifying the classification."
    )

    stale = tabled - registered
    assert not stale, (
        f"EXPECTED_ANNOTATIONS classifies tool(s) that are no longer "
        f"registered: {sorted(stale)}. Remove the stale entr(ies)."
    )


@pytest.mark.parametrize(
    "tool_name", sorted(EXPECTED_ANNOTATIONS), ids=sorted(EXPECTED_ANNOTATIONS)
)
def test_declared_annotations_match_expected_split(tool_name):
    """Each tool's declared hints equal the hand-maintained oracle.

    ``destructiveHint`` is compared with ``is`` against the expected
    value so that an unset hint (None) and an explicitly-declared False
    stay distinguishable: ``==`` would let a read-only tool wrongly
    carrying ``destructiveHint=False`` pass against an expected None.
    """
    tool = _all_registered_tools()[tool_name]
    expected_read_only, expected_destructive, expected_open_world = EXPECTED_ANNOTATIONS[tool_name]
    ann = tool.annotations

    assert ann is not None, f"{tool_name!r} declares no ToolAnnotations"
    assert ann.readOnlyHint is expected_read_only, (
        f"{tool_name!r} declares readOnlyHint={ann.readOnlyHint!r}, expected "
        f"{expected_read_only!r}. Either the declaration or the "
        "EXPECTED_ANNOTATIONS entry is wrong -- resolve against the tool's "
        "implementation, not its name."
    )
    assert ann.destructiveHint is expected_destructive, (
        f"{tool_name!r} declares destructiveHint={ann.destructiveHint!r}, "
        f"expected {expected_destructive!r}. Read-only tools must leave it "
        "unset (None); writers must declare it explicitly, since the MCP "
        "specification defaults an omitted destructiveHint to true."
    )
    assert ann.openWorldHint is expected_open_world, (
        f"{tool_name!r} declares openWorldHint={ann.openWorldHint!r}, "
        f"expected {expected_open_world!r}."
    )
    assert ann.idempotentHint is None, (
        f"{tool_name!r} declares idempotentHint={ann.idempotentHint!r}; the "
        "SAGE surface leaves it unset on every tool."
    )


# ---------------------------------------------------------------------------
# Reachability: what a divergence's category actually claims
# ---------------------------------------------------------------------------
#
# The category gate holds every entry to naming one of CAS-ADR-052's
# admissible categories. That buys legibility: a wrong claim is visible in
# review rather than persuasive in prose. It does not read the claim.
#
# Two of the categories make a claim a gate *can* read. Delivery form admits a
# request or response form only one protocol can carry "where the capability it
# delivers is reachable on the other surface"; operation factoring admits a
# split "the whole capability reachable on both". Each asserts a counterpart
# exists. Nothing checked that the named counterpart was real, and nothing
# checked that it carried the options the diverging surface offers -- so a
# capability gap could sit beneath a correctly-categorized row indefinitely,
# which is how a caller-settable flag on the batch upload route went years
# without one on the tool the row named as its counterpart. These gates read
# the claim.

#: Categories whose definition asserts the capability is reachable on the
#: other surface, and which therefore must name where.
_REACHABILITY_CLAIMING: Final[frozenset[DivergenceCategory]] = frozenset(
    {DivergenceCategory.DELIVERY_FORM, DivergenceCategory.OPERATION_FACTORING}
)

#: The registers whose entries name a whole tool or operation. The argument
#: registers are excluded: an argument's counterpart is the operation that
#: carries it, which its own entry already names.
_OPERATION_REGISTERS: Final[tuple[tuple[str, dict[tuple[str, str], Divergence]], ...]] = (
    ("MCP_ONLY_TOOLS", MCP_ONLY_TOOLS),
    ("REST_ONLY_OPERATIONS", REST_ONLY_OPERATIONS),
)


def _reachability_entries() -> list[tuple[str, tuple[str, str], Divergence]]:
    return [
        (register_name, key, divergence)
        for register_name, register in _OPERATION_REGISTERS
        for key, divergence in register.items()
        if divergence.category in _REACHABILITY_CLAIMING
    ]


def _every_registered_tool_name() -> set[str]:
    """Every MCP tool name across every surface that has a registry.

    The reached_by name of a REST-only operation is a tool, and which surface
    registers it is a table lookup rather than anything the name carries, so
    the lookup spans them all.
    """
    return {name for surface in TOOL_SURFACES for name in _surface_registry(surface)}


def _counterpart_names(register_name: str, surface_name: str) -> set[str]:
    """Every name the other surface offers, for an entry in this register."""
    if register_name == "REST_ONLY_OPERATIONS":
        return _every_registered_tool_name()
    return _all_operation_ids(_load_spec(_SURFACES_BY_NAME[surface_name].spec_path))


def unreachable_options(options: set[str], arguments: set[str]) -> list[str]:
    """The options a counterpart does not offer, sorted.

    The whole of the reachability comparison, in one place so the gate below
    and its positive control exercise the same code. A control that recomputed
    this relation would test the data the gate reads rather than the gate.
    """
    return sorted(options - arguments)


def _counterpart_arguments(tool_name: str) -> set[str]:
    """The argument names of a tool, wherever it is registered."""
    for surface in TOOL_SURFACES:
        registry = _surface_registry(surface)
        if tool_name in registry:
            return set(_tool_arguments(surface.name, tool_name))
    raise AssertionError(f"no surface registers a tool named {tool_name!r}")


def test_reachability_claiming_divergences_name_a_counterpart():
    """A category asserting the capability is reachable must say where.

    The claim is the category's own: delivery form and operation factoring
    both assert a counterpart exists. An entry making that claim and naming
    nothing is unfalsifiable, which is the state every row was in.
    """
    silent = [
        f"{register_name} {'/'.join(key)}: category {divergence.category!r} asserts the "
        "capability is reachable on the other surface but names nothing that carries "
        "it. Add reached_by, or recategorize."
        for register_name, key, divergence in _reachability_entries()
        if not divergence.reached_by
    ]
    assert not silent, "\n".join(silent)


def test_only_reachability_claiming_divergences_name_a_counterpart():
    """The other three categories assert no counterpart, so they name none.

    Negative control for the gate above: without it, `reached_by` could be
    added to every entry and the requirement would stop distinguishing the
    categories that make the claim from the ones that do not.
    """
    overreaching = [
        f"{register_name} {'/'.join(key)}: category {divergence.category!r} asserts no "
        f"counterpart, but names {list(divergence.reached_by)}. A translation artifact, "
        "a single-audience operation and a pending remediation each assert the "
        "opposite of reachability."
        for register_name, register in _OPERATION_REGISTERS
        for key, divergence in register.items()
        if divergence.category not in _REACHABILITY_CLAIMING and divergence.reached_by
    ]
    assert not overreaching, "\n".join(overreaching)


@pytest.mark.parametrize(
    ("register_name", "key"),
    [(r, k) for r, k, _ in _reachability_entries()],
    ids=[f"{r}-{'-'.join(k)}" for r, k, _ in _reachability_entries()],
)
def test_named_counterpart_exists_on_the_other_surface(register_name: str, key: tuple[str, str]):
    """The counterpart a divergence names is really offered over there.

    A name that resolves to nothing is the same unfalsifiable claim the gate
    above rejects, one step later: it reads as a reachable capability and
    carries none. Remediating a divergence by building its counterpart
    elsewhere, or renaming that counterpart, leaves the row behind; this is
    what notices.
    """
    surface_name = key[0]
    divergence = dict(_OPERATION_REGISTERS)[register_name][key]
    available = _counterpart_names(register_name, surface_name)

    missing = sorted(set(divergence.reached_by) - available)
    assert not missing, (
        f"{register_name} {'/'.join(key)} names {missing} as carrying its capability "
        "on the other surface, and the other surface offers no such tool or "
        "operation. Either the name is stale or the counterpart was never built."
    )


@pytest.mark.parametrize(
    ("register_name", "key"),
    [(r, k) for r, k, d in _reachability_entries() if d.options_schema],
    ids=[f"{r}-{'-'.join(k)}" for r, k, d in _reachability_entries() if d.options_schema],
)
def test_delivery_form_options_reach_the_named_counterpart(
    register_name: str, key: tuple[str, str]
):
    """Every option the diverging surface offers is offered by its counterpart.

    This is what "the capability is reachable on the other surface" asserts,
    read rather than taken. A delivery form that encodes its options in a
    transport field the specification types opaquely -- a JSON envelope in a
    multipart form field -- puts them beyond every structural reader, which is
    why the row names the component instead. Options the form itself carries
    are declared in `carried_by_form` and excluded; there is no general
    exemption list, and an option that stops reaching the counterpart fails
    here rather than waiting for a caller to want it.
    """
    surface_name, operation_id = key
    divergence = dict(_OPERATION_REGISTERS)[register_name][key]
    spec = _load_spec(_SURFACES_BY_NAME[surface_name].spec_path)

    component = spec["components"]["schemas"].get(divergence.options_schema)
    assert component is not None, (
        f"{register_name} {'/'.join(key)} names options_schema "
        f"{divergence.options_schema!r}, which {surface_name} does not declare."
    )
    options = set(component.get("properties", {})) - set(divergence.carried_by_form)
    assert options, (
        f"{divergence.options_schema!r} contributes no options to compare, so this "
        "gate would pass over nothing. Either the component is the wrong one or "
        "carried_by_form excludes all of it."
    )

    for counterpart in divergence.reached_by:
        unreachable = unreachable_options(options, _counterpart_arguments(counterpart))
        assert not unreachable, (
            f"{operation_id} offers {unreachable}, which its named counterpart "
            f"{counterpart!r} does not, so the capability this row records as "
            "merely differently *delivered* is not reachable there. Add the "
            "argument to the tool, or recategorize the row as pending remediation."
        )


def test_the_options_reachability_gate_fires_on_a_dropped_option():
    """Positive control: the gate above fails when an option stops reaching.

    The probe drops one real option from the counterpart's argument set and
    feeds it to ``unreachable_options`` -- the same function the gate calls,
    not a second copy of its set arithmetic. That distinction is the point: a
    control that recomputes the comparison itself is a control on the *data*
    the gate reads, and stays green against an inverted subset, a
    ``carried_by_form`` that excluded everything, or a ``reached_by`` loop that
    never iterated. Reading the live pair through the gate's own function also
    makes an empty option set, or arguments read as "everything", fail here.
    """
    spec = _load_spec(_SURFACES_BY_NAME["sage_core"].spec_path)
    options = set(spec["components"]["schemas"]["BatchIngestUploadMetadata"]["properties"])
    arguments = _counterpart_arguments("bulk_ingest_document")
    assert options, "precondition: the component contributes options to compare"
    assert not unreachable_options(options, arguments), "precondition: the live pair is clean"

    assert unreachable_options(options, arguments - {"needs_review"}) == ["needs_review"], (
        "dropping needs_review from the counterpart's arguments must surface it as "
        "unreachable; if it does not, the gate's own comparison is not separating "
        "an option the counterpart offers from one it does not."
    )


def test_python_types_see_through_a_published_description():
    """A described parameter is type-checked like an undescribed one.

    ``Annotated`` carries the parameter's published description. Reduced to
    the empty set, it would read as "cannot tell" and skip the type
    comparison for every described parameter, silently.
    """
    from typing import Annotated

    from pydantic import Field

    described = Annotated[int | None, Field(description="A described parameter.")]
    assert _python_types_and_optional(described) == (frozenset({int}), True)
    assert _python_types_and_optional(described) == _python_types_and_optional(int | None)
