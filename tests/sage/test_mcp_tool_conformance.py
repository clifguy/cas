"""Architectural-conformance tests for the MCP tool surface.

Gates the structural alignment between the MCP tool surface
(``sage/sage_api_tools.py``, ``sage/app_tools.py``,
``sage/mcp_server.py``) and the OpenAPI substrate (``docs/fs/sage/``,
``docs/fs/cas_app_api.openapi.yaml``, ``docs/fs/root_harness/``).

Mirrors ``test_router_conformance.py``: a small ``ToolSurface`` tuple
declares each surface; per-element parametrized tests assert that the
MCP surface and the OpenAPI surface agree on names, operation coverage,
and per-argument shapes. An allowlist drains as drift is remediated.

Conformance interpretation: schema-subset ( planning
session). Each MCP tool argument must match a parameter or
requestBody field of its OpenAPI counterpart by name and compatible
type. Tools may expose a strict subset of OpenAPI inputs. The MCP
transport's JSON-string-as-carrier convention is encoded in the type
table below: a Python ``str`` argument may stand in for an OpenAPI
``object`` or ``array`` field (e.g. ``search(filters: str)``
where the spec declares ``filters`` as an object). The tolerance is
asymmetric and scoped: ``int`` cannot stand in for ``object``, etc.

The check is bi-directional: every MCP tool must map to an OpenAPI
operation (allowlisted in ``DIVERGENT_TOOLS`` otherwise), and every
OpenAPI operation must have an MCP tool (allowlisted in
``HTTP_ONLY_OPERATIONS`` otherwise). MCP-side argument gaps that
predate the gate are pinned in ``KNOWN_ARG_DRIFT`` until reconciled.
All three allowlists fail when stale.

The ROOT Harness Orchestration spec exists today but no MCP tools
yet implement its operations (/); the entire spec is
allowlisted at the operation level until those tools land.
"""

from __future__ import annotations

import functools
import inspect
import types
import typing
from pathlib import Path
from typing import Any, Callable, NamedTuple

import pytest
import yaml

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
# expects every operation to be listed in HTTP_ONLY_OPERATIONS until
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

# (surface_name, tool_name) -> justification for MCP tools that
# legitimately have no HTTP counterpart.
DIVERGENT_TOOLS: dict[tuple[str, str], str] = {
    (
        "sage_core",
        "recompute_pipeline",
    ): (
        "Operator-only ingestion-pipeline repair for documents stuck at "
        "pipeline_status=projection_complete with no chunks; no HTTP route by "
        "design -- the recovery surface lives on the MCP transport alongside "
        "recompute_abstract."
    ),
    (
        "sage_core",
        "update_staging_edge",
    ): (
        "MCP-only consolidation of confirm_staging_edge + dismiss_staging_edge "
        "per the SAGE MCP Tool Surface enumeration discipline (CAS-ADR-035). "
        "REST exposes the two operations separately; MCP collapses them via the "
        "action parameter."
    ),
    (
        "sage_core",
        "admin_reload_vault",
    ): (
        "MCP-only operational tool: closes a vault's services and "
        "re-initializes them after on-disk vault_config.yaml edits or "
        "external DB writes. No HTTP counterpart by design — the FastAPI "
        "vault-config PUT endpoint owns the reload path on the REST side."
    ),
    (
        "sage_core",
        "admin_get_stack_config",
    ): (
        "MCP-only introspection of the SAGE-stack-wide config singleton "
        "(CAS-ADR-030). No HTTP counterpart by design; the stack config is "
        "process-scoped and only the MCP transport carries the agent-facing "
        "read."
    ),
}

# (surface_name, tool_name) -> set of MCP argument names that
# legitimately do not appear in the OpenAPI operation. Each entry
# must be either remediated (by adding the field to the spec or
# removing it from the tool) or replaced with a justification for
# permanent divergence. The test fails on stale entries (drift
# remediated but allowlist not pruned).
KNOWN_ARG_DRIFT: dict[tuple[str, str], frozenset[str]] = {
    # ``document_id`` is an MCP-only alias for ``start_id`` on the
    # ``traverse`` tool, added to unify document-ID parameter naming
    # across MCP tools after a field report. The HTTP API surface is
    # explicitly out of scope (HTTP callers see the OpenAPI schema and
    # don't suffer the same field-name guessing cost). Permanent
    # divergence by design, not pending remediation.
    ("sage_core", "traverse"): frozenset({"document_id"}),
    # ``write_to_path`` was added to the ``read_projection`` MCP tool
    # as the consolidated home for the write-to-disk delivery mode
    # that previously lived on a separate export_projection tool. The
    # REST surface keeps ``export_projection`` as its own discrete
    # endpoint (storage_root-relative semantics); the MCP-side
    # ``write_to_path`` is an absolute-path mode mirroring
    # ``get_document``. ``delivery`` (inline | spill | auto) pins the
    # inline-vs-spill shape and is meaningful only alongside the
    # MCP-only ``write_to_path``; the REST projection endpoint has no
    # write-to-disk target to spill to, so the selector is MCP-only by
    # construction. ``doc_id`` is the MCP-only inbound alias for
    # ``document_id`` (see the read-tool cluster below). All three are
    # permanent divergences by design.
    ("sage_core", "read_projection"): frozenset({"write_to_path", "doc_id", "delivery"}),
    # ``doc_id`` is an MCP-only inbound alias for ``document_id`` on the
    # document-id read tools, mirroring the ``traverse`` alias above.
    # External agents (notably Cowork over /mcp) supply the ``doc_id``
    # shorthand; the published schema must accept it so the client's
    # additionalProperties:false coercion does not strip it before
    # dispatch. The HTTP API surface is out of scope (HTTP callers read
    # the OpenAPI schema and don't guess field names). Permanent
    # divergence by design, not pending remediation.
    ("sage_core", "get_document"): frozenset({"doc_id"}),
    ("sage_core", "read_section"): frozenset({"doc_id"}),
    ("sage_core", "list_headings"): frozenset({"doc_id"}),
    ("sage_core", "chain"): frozenset({"doc_id"}),
}


# (surface_name, operation_id) -> justification for OpenAPI
# operations that legitimately have no MCP tool. Drains as
# operations are exposed via MCP.
HTTP_ONLY_OPERATIONS: dict[tuple[str, str], str] = {
    ("sage_core", "eval_retrieval"): "HTTP-only retrieval evaluation harness.",
    (
        "sage_core",
        "register_user",
    ): (
        "REST-only per the SAGE MCP Tool Surface enumeration discipline "
        "(CAS-ADR-035): user registration is a CAS App account-creation concern. "
        "Agents pass ad-hoc `created_by` strings per CAS-ADR-021 and do not need "
        "an MCP path."
    ),
    (
        "sage_core",
        "confirm_staging_edge",
    ): (
        "REST keeps the operation as a discrete endpoint; MCP collapses "
        "confirm+dismiss into update_staging_edge(action=...) per the SAGE "
        "MCP Tool Surface enumeration discipline (CAS-ADR-035)."
    ),
    (
        "sage_core",
        "dismiss_staging_edge",
    ): (
        "REST keeps the operation as a discrete endpoint; MCP collapses "
        "confirm+dismiss into update_staging_edge(action=...) per the SAGE "
        "MCP Tool Surface enumeration discipline (CAS-ADR-035)."
    ),
    (
        "sage_core",
        "export_projection",
    ): (
        "REST keeps export_projection (storage_root-relative write semantics); "
        "MCP folds the write-to-disk capability into "
        "read_projection(write_to_path=...) (absolute-path semantics, mirroring "
        "get_document) per the SAGE MCP Tool Surface enumeration discipline "
        "(CAS-ADR-035)."
    ),
    (
        "sage_core",
        "get_editors",
    ): "Editor-model write control; forward-declared per SAGE Architecture Ref §4.3/§6.3.",
    (
        "sage_core",
        "set_editors",
    ): "Editor-model write control; forward-declared per SAGE Architecture Ref §4.3/§6.3.",
    ("sage_core", "open_document"): "HTTP-only UI affordance.",
    (
        "sage_core",
        "get_document_download_url",
    ): (
        "HTTP-only browser-delivery affordance: mints a short-lived "
        "pre-authenticated URL the browser fetches directly from the backing "
        "store. Agents read source bytes via get_document/read_projection and "
        "have no need for a browser download URL, so there is no MCP tool."
    ),
    (
        "sage_core",
        "batch_ingest_documents",
    ): (
        "Multipart upload + SSE batch-ingest endpoint for the hosted profile "
        "(content delivered by upload across the BFF/SAGE container boundary). "
        "Has no stdio MCP counterpart: the path-based bulk_ingest_document MCP "
        "tool serves the co-located local-filesystem case, and multipart file "
        "upload has no MCP-transport equivalent (CAS-ADR-042)."
    ),
    # Backend-for-frontend interactive sign-in: browser-facing redirect and
    # cookie flows that have no agent-facing MCP surface by design.
    (
        "cas_app",
        "begin_login",
    ): "Browser-interactive OIDC sign-in entry (redirect + cookie); no MCP tool by design.",
    (
        "cas_app",
        "get_session",
    ): "Cookie-scoped session-state read for the SPA; no MCP tool by design.",
    (
        "cas_app",
        "end_session",
    ): "Cookie-scoped sign-out; no MCP tool by design.",
    # ROOT Harness Orchestration API: no MCP surface yet. Each
    # operation must drain individually once the orchestrator MCP
    # tools land (/).
    (
        "root_harness",
        "trigger_workflow",
    ): "ROOT Harness MCP surface not yet implemented; see T-0015/T-0016.",
    (
        "root_harness",
        "get_status",
    ): "ROOT Harness MCP surface not yet implemented; see T-0015/T-0016.",
    (
        "root_harness",
        "approve",
    ): "ROOT Harness MCP surface not yet implemented; see T-0015/T-0016.",
    (
        "root_harness",
        "list_pending",
    ): "ROOT Harness MCP surface not yet implemented; see T-0015/T-0016.",
    (
        "root_harness",
        "subscribe_events",
    ): "ROOT Harness MCP surface not yet implemented; see T-0015/T-0016.",
    (
        "root_harness",
        "register_agent",
    ): "ROOT Harness MCP surface not yet implemented; see T-0015/T-0016.",
    (
        "root_harness",
        "get_agent",
    ): "ROOT Harness MCP surface not yet implemented; see T-0015/T-0016.",
    (
        "root_harness",
        "get_agent_history",
    ): "ROOT Harness MCP surface not yet implemented; see T-0015/T-0016.",
    (
        "root_harness",
        "get_pipeline_status",
    ): "ROOT Harness MCP surface not yet implemented; see T-0015/T-0016.",
}


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


def _types_compatible(py_types: frozenset[type], openapi_type: str | None) -> bool:
    """Return whether any Python type in the set may stand in for the OpenAPI type."""
    if openapi_type is None:
        return True
    if not py_types:
        return True
    for t in py_types:
        if openapi_type in _TYPE_COMPAT.get(t, frozenset()):
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
    """Each registered MCP tool maps to an OpenAPI operation or is allowlisted."""
    surface = _SURFACES_BY_NAME[surface_name]
    spec = _load_spec(surface.spec_path)
    expected_op_id = _resolve_expected_operation_id(surface, tool_name)
    op = _find_operation(spec, expected_op_id)
    divergent = (surface_name, tool_name) in DIVERGENT_TOOLS

    if op is None and divergent:
        return  # allowlisted; legitimate divergence
    if op is None and not divergent:
        pytest.fail(
            f"MCP tool {tool_name!r} on surface {surface_name!r} has no OpenAPI "
            f"operation (expected operationId {expected_op_id!r}). Either add "
            "the operation to the spec, file an OPERATION_RENAMES override, or "
            "add (surface, tool) to DIVERGENT_TOOLS with a justification."
        )
    if op is not None and divergent:
        pytest.fail(
            f"MCP tool {tool_name!r} on surface {surface_name!r} is allowlisted "
            f"in DIVERGENT_TOOLS but OpenAPI now has operationId "
            f"{expected_op_id!r}. Remove the stale allowlist entry."
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
    """Each OpenAPI operation has an MCP tool or is allowlisted."""
    surface = _SURFACES_BY_NAME[surface_name]
    registry = _surface_registry(surface)
    expected_tool = _resolve_expected_tool_name(surface, operation_id)
    present = expected_tool in registry
    http_only = (surface_name, operation_id) in HTTP_ONLY_OPERATIONS

    if not present and http_only:
        return  # allowlisted; legitimate HTTP-only operation
    if not present and not http_only:
        pytest.fail(
            f"OpenAPI operationId {operation_id!r} on surface {surface_name!r} "
            f"has no MCP tool (expected tool name {expected_tool!r}). Either "
            "add the MCP tool, add an OPERATION_RENAMES override if the tool "
            "exists under a different name, or add (surface, operation_id) to "
            "HTTP_ONLY_OPERATIONS with a justification."
        )
    if present and http_only:
        pytest.fail(
            f"OpenAPI operationId {operation_id!r} on surface {surface_name!r} "
            f"is allowlisted in HTTP_ONLY_OPERATIONS but MCP tool "
            f"{expected_tool!r} is now registered. Remove the stale entry."
        )


def _mapped_tool_pairs() -> list[tuple[str, str]]:
    """Tool pairs that resolve to an OpenAPI operation (excludes DIVERGENT_TOOLS)."""
    pairs: list[tuple[str, str]] = []
    for surface_name, tool_name in _tool_id_pairs():
        if (surface_name, tool_name) in DIVERGENT_TOOLS:
            continue
        pairs.append((surface_name, tool_name))
    return pairs


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
    spec = _load_spec(surface.spec_path)
    registry = _surface_registry(surface)
    op_id = _resolve_expected_operation_id(surface, tool_name)
    op = _find_operation(spec, op_id)
    assert op is not None, (
        f"Internal: expected operation {op_id!r} to exist (covered by "
        "test_mcp_tool_has_openapi_counterpart)."
    )

    tool_fn = registry[tool_name]
    sig = inspect.signature(tool_fn)
    openapi_fields = _operation_parameters(spec, op)
    allowed = KNOWN_ARG_DRIFT.get((surface_name, tool_name), frozenset())

    actual_missing: set[str] = set()
    type_violations: list[str] = []

    for param_name, param in sig.parameters.items():
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        if param_name not in openapi_fields:
            actual_missing.add(param_name)
            continue
        openapi_type, _ = openapi_fields[param_name]
        py_types, _ = _python_types_and_optional(param.annotation)
        if not _types_compatible(py_types, openapi_type):
            py_repr = sorted(t.__name__ for t in py_types) or [repr(param.annotation)]
            type_violations.append(f"{param_name}: python={py_repr} openapi={openapi_type!r}")

    new_drift = actual_missing - allowed
    assert not new_drift, (
        f"MCP tool {tool_name!r} (operationId {op_id!r}) exposes argument(s) "
        f"{sorted(new_drift)!r} that do not appear in the OpenAPI operation's "
        "parameters or requestBody schema. Either rename the MCP argument to "
        "match the spec, add the field to the spec, or pin the new gap in "
        "KNOWN_ARG_DRIFT with a remediation-ticket reference."
    )

    stale_allowlist = allowed - actual_missing
    assert not stale_allowlist, (
        f"MCP tool {tool_name!r} (operationId {op_id!r}) is allowlisted in "
        f"KNOWN_ARG_DRIFT for argument(s) {sorted(stale_allowlist)!r} but no "
        "longer exhibits the gap. Remove the stale entry (or delete the whole "
        "key if it was the only one)."
    )

    assert not type_violations, (
        f"MCP tool {tool_name!r} (operationId {op_id!r}) has argument(s) with "
        f"types incompatible with the OpenAPI schema: {type_violations!r}. "
        "Adjust either side so the types line up (consult the type-compat "
        "table in the test module docstring)."
    )


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
