#!/usr/bin/env python3
"""Classify the difference between two snapshots of the published contract.

CAS-ADR-008 makes every change a patch unless it crosses a minor boundary, and
names a mechanical comparison of the published contract as the primary detector
of the two boundaries a comparison can see:

- **capability** -- a caller can do something it could not do before: a new
  operation or tool, parameter, accepted value, or response field;
- **caller-adaptation** -- a caller must change what it does: a removal, a
  rename, a new required input, a narrowed schema, a changed default.

The published contract is three artifacts: the SAGE Core API and CAS Application
API OpenAPI specifications, and the MCP tool catalog covering both MCP surfaces.
This module compares a pair of snapshots of each and reports every observable
difference as a finding carrying one of those categories. It reads documents
only; resolving which revisions to compare, and what to do with the findings,
belongs to its callers.

**What it cannot see is deliberately left unseen.** Descriptive text --
descriptions, summaries, titles, examples, vendor extensions -- never produces a
finding, and neither do the contract version or the order of keys and set-like
lists. Semantic changes behind an unchanged shape, and every operator-facing
change, are outside any comparison of documents and remain the author's
judgment.

**Where direction is ambiguous it reports the adaptation.** A component schema
may be read by a request and a response at once, so this module does not work
out which side a change reaches: an added property is a capability and a newly
required one an adaptation, wherever they sit. Both categories are minor, so
the classification a finding forces is right either way, and the owner can
downgrade a finding that is not one through the change record's override.

Usage::

    python -m scripts.contract_diff OLD_SPEC.yaml NEW_SPEC.yaml
    python -m scripts.contract_diff --mcp OLD_CATALOG.json NEW_CATALOG.json

Exit status is 0 when the snapshots differ in no contract-visible way, 1 when a
finding is reported.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

CAPABILITY: Final[str] = "capability"
CALLER_ADAPTATION: Final[str] = "caller-adaptation"

# Keywords that describe a schema or operation without constraining it.
_IGNORED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "description",
        "summary",
        "title",
        "example",
        "examples",
        "externalDocs",
        "$comment",
        "$schema",
        "$id",
        "deprecated",
        # Folded into the type set by ``_type_set``.
        "nullable",
    }
)

# Bounds whose lower value admits less, and whose higher value admits less.
_UPPER_BOUNDS: Final[frozenset[str]] = frozenset(
    {"maxLength", "maxItems", "maxProperties", "maximum", "exclusiveMaximum"}
)
_LOWER_BOUNDS: Final[frozenset[str]] = frozenset(
    {"minLength", "minItems", "minProperties", "minimum", "exclusiveMinimum"}
)

# Keywords whose presence constrains and whose absence does not.
_PRESENCE_CONSTRAINTS: Final[frozenset[str]] = frozenset(
    {"pattern", "format", "multipleOf", "uniqueItems", "contentMediaType", "const"}
)

# Keywords whose value is a set: order carries no meaning.
_SET_VALUED: Final[frozenset[str]] = frozenset({"enum", "required", "type"})

_HANDLED_SCHEMA_KEYS: Final[frozenset[str]] = (
    frozenset(
        {
            "$ref",
            "type",
            "enum",
            "properties",
            "required",
            "additionalProperties",
            "items",
            "prefixItems",
            "anyOf",
            "oneOf",
            "allOf",
            "default",
            "$defs",
            "definitions",
        }
    )
    | _UPPER_BOUNDS
    | _LOWER_BOUNDS
    | _PRESENCE_CONSTRAINTS
)

_HTTP_METHODS: Final[tuple[str, ...]] = (
    "get",
    "put",
    "post",
    "delete",
    "patch",
    "options",
    "head",
    "trace",
)


@dataclass(frozen=True)
class Finding:
    """One contract-visible difference and the minor category it falls in."""

    surface: str
    pointer: str
    kind: str
    category: str

    def render(self) -> str:
        return f"{self.category}: {self.kind} at {self.surface}:{self.pointer}"


@dataclass(frozen=True)
class Contract:
    """A snapshot of the published contract; an absent artifact is ``None``."""

    sage_core_api: dict[str, Any] | None
    cas_app_api: dict[str, Any] | None
    mcp_catalog: dict[str, Any] | None


# ---------------------------------------------------------------------------
# Canonical comparison
# ---------------------------------------------------------------------------


def _is_ignored(key: str) -> bool:
    return key in _IGNORED_KEYS or key.startswith("x-")


def _strip(value: Any, key: str | None = None) -> Any:
    """The value with descriptive keys removed and set-like lists sorted."""
    if isinstance(value, dict):
        stripped = {k: _strip(v, k) for k, v in value.items() if not _is_ignored(k)}
        types = _type_set(value)
        if types is not None:
            stripped["type"] = sorted(types)
        return stripped
    if isinstance(value, list):
        items = [_strip(v) for v in value]
        if key in _SET_VALUED:
            return sorted(items, key=_canon)
        return items
    return value


def _canon(value: Any) -> str:
    return json.dumps(_strip(value), sort_keys=True, ensure_ascii=False)


def _type_set(schema: dict[str, Any]) -> frozenset[str] | None:
    """The types a schema admits, or ``None`` when it does not constrain type.

    The OpenAPI 3.0 ``nullable: true`` is read as admitting ``null``, so moving a
    declaration to the 3.1 ``type: [..., "null"]`` spelling is not a change.
    """
    if "type" not in schema:
        return None
    declared = schema["type"]
    types = set(declared if isinstance(declared, list) else [declared])
    if schema.get("nullable") is True:
        types.add("null")
    return frozenset(types)


# ---------------------------------------------------------------------------
# JSON Schema
# ---------------------------------------------------------------------------


class _Collector:
    def __init__(self, surface: str) -> None:
        self.surface = surface
        self.findings: list[Finding] = []

    def add(self, pointer: str, kind: str, category: str) -> None:
        self.findings.append(Finding(self.surface, pointer, kind, category))


def _diff_schema(old: Any, new: Any, pointer: str, out: _Collector) -> None:
    if _canon(old) == _canon(new):
        return
    if not isinstance(old, dict) or not isinstance(new, dict):
        out.add(pointer, "schema-changed", CALLER_ADAPTATION)
        return
    if old.get("$ref") != new.get("$ref"):
        out.add(pointer, "ref-retargeted", CALLER_ADAPTATION)
        return

    _diff_type(old, new, pointer, out)
    _diff_enum(old, new, pointer, out)
    _diff_properties(old, new, pointer, out)
    _diff_required(old, new, pointer, out)
    _diff_additional_properties(old, new, pointer, out)
    _diff_bounds(old, new, pointer, out)
    _diff_presence_constraints(old, new, pointer, out)

    if _canon(old.get("default")) != _canon(new.get("default")) or (
        ("default" in old) != ("default" in new)
    ):
        out.add(f"{pointer}/default", "default-changed", CALLER_ADAPTATION)

    for key in ("items",):
        _diff_optional_subschema(old, new, key, pointer, out)
    _diff_positional(old.get("prefixItems"), new.get("prefixItems"), f"{pointer}/prefixItems", out)
    for key in ("anyOf", "oneOf", "allOf"):
        _diff_branches(old.get(key), new.get(key), key, pointer, out)
    for key in ("$defs", "definitions"):
        _diff_named_schemas(old.get(key) or {}, new.get(key) or {}, f"{pointer}/{key}", out)

    for key in sorted((set(old) | set(new)) - _HANDLED_SCHEMA_KEYS):
        if _is_ignored(key):
            continue
        if _canon(old.get(key)) != _canon(new.get(key)) or ((key in old) != (key in new)):
            out.add(f"{pointer}/{key}", "keyword-changed", CALLER_ADAPTATION)


def _diff_type(old: dict[str, Any], new: dict[str, Any], pointer: str, out: _Collector) -> None:
    old_types, new_types = _type_set(old), _type_set(new)
    if old_types == new_types:
        return
    at = f"{pointer}/type"
    if old_types is None:
        out.add(at, "type-narrowed", CALLER_ADAPTATION)
    elif new_types is None or new_types > old_types:
        out.add(at, "type-widened", CAPABILITY)
    else:
        out.add(at, "type-narrowed", CALLER_ADAPTATION)


def _diff_enum(old: dict[str, Any], new: dict[str, Any], pointer: str, out: _Collector) -> None:
    if "enum" not in old and "enum" not in new:
        return
    at = f"{pointer}/enum"
    if "enum" not in old:
        out.add(at, "constraint-tightened", CALLER_ADAPTATION)
        return
    if "enum" not in new:
        out.add(at, "constraint-loosened", CAPABILITY)
        return
    old_values = {_canon(v) for v in old["enum"]}
    new_values = {_canon(v) for v in new["enum"]}
    if new_values - old_values:
        out.add(at, "enum-value-added", CAPABILITY)
    if old_values - new_values:
        out.add(at, "enum-value-removed", CALLER_ADAPTATION)


def _diff_properties(
    old: dict[str, Any], new: dict[str, Any], pointer: str, out: _Collector
) -> None:
    old_props = old.get("properties") or {}
    new_props = new.get("properties") or {}
    for name in sorted(set(new_props) - set(old_props)):
        out.add(f"{pointer}/properties/{name}", "property-added", CAPABILITY)
    for name in sorted(set(old_props) - set(new_props)):
        out.add(f"{pointer}/properties/{name}", "property-removed", CALLER_ADAPTATION)
    for name in sorted(set(old_props) & set(new_props)):
        _diff_schema(old_props[name], new_props[name], f"{pointer}/properties/{name}", out)


def _diff_required(old: dict[str, Any], new: dict[str, Any], pointer: str, out: _Collector) -> None:
    old_required = set(old.get("required") or [])
    new_required = set(new.get("required") or [])
    for name in sorted(new_required - old_required):
        out.add(f"{pointer}/required/{name}", "required-added", CALLER_ADAPTATION)
    for name in sorted(old_required - new_required):
        out.add(f"{pointer}/required/{name}", "required-removed", CAPABILITY)


def _diff_additional_properties(
    old: dict[str, Any], new: dict[str, Any], pointer: str, out: _Collector
) -> None:
    old_ap = old.get("additionalProperties", True)
    new_ap = new.get("additionalProperties", True)
    if _canon(old_ap) == _canon(new_ap):
        return
    at = f"{pointer}/additionalProperties"
    if isinstance(old_ap, dict) and isinstance(new_ap, dict):
        _diff_schema(old_ap, new_ap, at, out)
    elif old_ap is False or new_ap is True:
        out.add(at, "constraint-loosened", CAPABILITY)
    else:
        out.add(at, "constraint-tightened", CALLER_ADAPTATION)


def _diff_bounds(old: dict[str, Any], new: dict[str, Any], pointer: str, out: _Collector) -> None:
    for key in sorted(_UPPER_BOUNDS | _LOWER_BOUNDS):
        if key not in old and key not in new:
            continue
        at = f"{pointer}/{key}"
        if key not in old:
            out.add(at, "constraint-tightened", CALLER_ADAPTATION)
        elif key not in new:
            out.add(at, "constraint-loosened", CAPABILITY)
        elif old[key] != new[key]:
            numeric = all(
                isinstance(v, (int, float)) and not isinstance(v, bool)
                for v in (old[key], new[key])
            )
            if not numeric:
                out.add(at, "constraint-tightened", CALLER_ADAPTATION)
            elif (key in _UPPER_BOUNDS) == (new[key] < old[key]):
                out.add(at, "constraint-tightened", CALLER_ADAPTATION)
            else:
                out.add(at, "constraint-loosened", CAPABILITY)


def _diff_presence_constraints(
    old: dict[str, Any], new: dict[str, Any], pointer: str, out: _Collector
) -> None:
    for key in sorted(_PRESENCE_CONSTRAINTS):
        old_on = key in old and old[key] is not False
        new_on = key in new and new[key] is not False
        at = f"{pointer}/{key}"
        if new_on and (not old_on or _canon(old[key]) != _canon(new[key])):
            out.add(at, "constraint-tightened", CALLER_ADAPTATION)
        elif old_on and not new_on:
            out.add(at, "constraint-loosened", CAPABILITY)


def _diff_optional_subschema(
    old: dict[str, Any], new: dict[str, Any], key: str, pointer: str, out: _Collector
) -> None:
    at = f"{pointer}/{key}"
    if key in old and key in new:
        _diff_schema(old[key], new[key], at, out)
    elif key in new:
        out.add(at, "constraint-tightened", CALLER_ADAPTATION)
    elif key in old:
        out.add(at, "constraint-loosened", CAPABILITY)


def _diff_positional(old: Any, new: Any, pointer: str, out: _Collector) -> None:
    if old is None and new is None:
        return
    if not isinstance(old, list) or not isinstance(new, list) or len(old) != len(new):
        if _canon(old) != _canon(new):
            out.add(pointer, "keyword-changed", CALLER_ADAPTATION)
        return
    for index, (old_item, new_item) in enumerate(zip(old, new, strict=True)):
        _diff_schema(old_item, new_item, f"{pointer}/{index}", out)


def _diff_branches(old: Any, new: Any, key: str, pointer: str, out: _Collector) -> None:
    """Compare composition branches as a multiset.

    Branches identical on both sides are matched first, so reordering or
    inserting a branch does not misreport every later one as changed; the
    unmatched remainders are then paired in order and compared.
    """
    old_list = old if isinstance(old, list) else []
    new_list = new if isinstance(new, list) else []
    if not old_list and not new_list:
        return
    unmatched_new = list(range(len(new_list)))
    unmatched_old: list[int] = []
    for i, branch in enumerate(old_list):
        match = next((j for j in unmatched_new if _canon(new_list[j]) == _canon(branch)), None)
        if match is None:
            unmatched_old.append(i)
        else:
            unmatched_new.remove(match)
    for i, j in zip(unmatched_old, unmatched_new, strict=False):
        _diff_schema(old_list[i], new_list[j], f"{pointer}/{key}/{j}", out)
    widening = key != "allOf"
    for j in unmatched_new[len(unmatched_old) :]:
        category = CAPABILITY if widening else CALLER_ADAPTATION
        out.add(f"{pointer}/{key}/{j}", "branch-added", category)
    for i in unmatched_old[len(unmatched_new) :]:
        category = CALLER_ADAPTATION if widening else CAPABILITY
        out.add(f"{pointer}/{key}/{i}", "branch-removed", category)


def _diff_named_schemas(
    old: dict[str, Any], new: dict[str, Any], pointer: str, out: _Collector
) -> None:
    for name in sorted(set(old) - set(new)):
        out.add(f"{pointer}/{name}", "component-removed", CALLER_ADAPTATION)
    for name in sorted(set(old) & set(new)):
        _diff_schema(old[name], new[name], f"{pointer}/{name}", out)


# ---------------------------------------------------------------------------
# OpenAPI
# ---------------------------------------------------------------------------


def _resolve(spec: dict[str, Any], node: Any, section: str) -> Any:
    """A local ``#/components/<section>/<name>`` reference, followed once."""
    if not isinstance(node, dict) or "$ref" not in node:
        return node
    prefix = f"#/components/{section}/"
    ref = node["$ref"]
    if not isinstance(ref, str) or not ref.startswith(prefix):
        return node
    return ((spec.get("components") or {}).get(section) or {}).get(ref[len(prefix) :], node)


def _parameters(
    spec: dict[str, Any], path_item: dict[str, Any], operation: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """An operation's parameters keyed by location and name, path level first."""
    merged: dict[str, dict[str, Any]] = {}
    for raw in [*(path_item.get("parameters") or []), *(operation.get("parameters") or [])]:
        param = _resolve(spec, raw, "parameters")
        if isinstance(param, dict) and "name" in param:
            merged[f"{param.get('in', '')}:{param['name']}"] = param
    return merged


def _diff_parameter(
    old: dict[str, Any], new: dict[str, Any], pointer: str, out: _Collector
) -> None:
    old_required = bool(old.get("required"))
    new_required = bool(new.get("required"))
    if new_required and not old_required:
        out.add(pointer, "parameter-made-required", CALLER_ADAPTATION)
    elif old_required and not new_required:
        out.add(pointer, "parameter-made-optional", CAPABILITY)
    _diff_schema(old.get("schema") or {}, new.get("schema") or {}, f"{pointer}/schema", out)
    for key in sorted((set(old) | set(new)) - {"name", "in", "required", "schema"}):
        if _is_ignored(key):
            continue
        if _canon(old.get(key)) != _canon(new.get(key)):
            out.add(f"{pointer}/{key}", "keyword-changed", CALLER_ADAPTATION)


def _diff_content(old: Any, new: Any, pointer: str, out: _Collector) -> None:
    old_content = old if isinstance(old, dict) else {}
    new_content = new if isinstance(new, dict) else {}
    for media in sorted(set(new_content) - set(old_content)):
        out.add(f"{pointer}/content/{media}", "media-type-added", CAPABILITY)
    for media in sorted(set(old_content) - set(new_content)):
        out.add(f"{pointer}/content/{media}", "media-type-removed", CALLER_ADAPTATION)
    for media in sorted(set(old_content) & set(new_content)):
        _diff_schema(
            (old_content[media] or {}).get("schema") or {},
            (new_content[media] or {}).get("schema") or {},
            f"{pointer}/content/{media}/schema",
            out,
        )


def _diff_request_body(
    old_spec: dict[str, Any],
    new_spec: dict[str, Any],
    old_op: dict[str, Any],
    new_op: dict[str, Any],
    pointer: str,
    out: _Collector,
) -> None:
    old_body = _resolve(old_spec, old_op.get("requestBody"), "requestBodies")
    new_body = _resolve(new_spec, new_op.get("requestBody"), "requestBodies")
    at = f"{pointer}/requestBody"
    if old_body is None and new_body is None:
        return
    if old_body is None:
        required = bool(new_body.get("required"))
        out.add(
            at,
            "request-body-added",
            CALLER_ADAPTATION if required else CAPABILITY,
        )
        return
    if new_body is None:
        out.add(at, "request-body-removed", CALLER_ADAPTATION)
        return
    if new_body.get("required") and not old_body.get("required"):
        out.add(at, "request-body-made-required", CALLER_ADAPTATION)
    elif old_body.get("required") and not new_body.get("required"):
        out.add(at, "request-body-made-optional", CAPABILITY)
    _diff_content(old_body.get("content"), new_body.get("content"), at, out)


def _diff_responses(
    old_spec: dict[str, Any],
    new_spec: dict[str, Any],
    old_op: dict[str, Any],
    new_op: dict[str, Any],
    pointer: str,
    out: _Collector,
) -> None:
    old_responses = old_op.get("responses") or {}
    new_responses = new_op.get("responses") or {}
    for status in sorted(set(map(str, new_responses)) - set(map(str, old_responses))):
        out.add(f"{pointer}/responses/{status}", "response-added", CAPABILITY)
    for status in sorted(set(map(str, old_responses)) - set(map(str, new_responses))):
        out.add(f"{pointer}/responses/{status}", "response-removed", CALLER_ADAPTATION)
    old_by_status = {str(k): v for k, v in old_responses.items()}
    new_by_status = {str(k): v for k, v in new_responses.items()}
    for status in sorted(set(old_by_status) & set(new_by_status)):
        at = f"{pointer}/responses/{status}"
        old_raw, new_raw = old_by_status[status], new_by_status[status]
        if isinstance(old_raw, dict) and isinstance(new_raw, dict):
            if old_raw.get("$ref") != new_raw.get("$ref"):
                out.add(at, "ref-retargeted", CALLER_ADAPTATION)
                continue
        old_response = _resolve(old_spec, old_raw, "responses") or {}
        new_response = _resolve(new_spec, new_raw, "responses") or {}
        _diff_content(old_response.get("content"), new_response.get("content"), at, out)


def _operations(spec: dict[str, Any]) -> dict[tuple[str, str], tuple[dict, dict]]:
    found: dict[tuple[str, str], tuple[dict, dict]] = {}
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method in _HTTP_METHODS:
            if isinstance(item.get(method), dict):
                found[(path, method)] = (item, item[method])
    return found


def diff_openapi(old: dict[str, Any], new: dict[str, Any], *, surface: str) -> list[Finding]:
    """Every contract-visible difference between two OpenAPI documents."""
    out = _Collector(surface)
    old_ops, new_ops = _operations(old), _operations(new)

    for path, method in sorted(set(new_ops) - set(old_ops)):
        out.add(f"paths/{path}/{method}", "operation-added", CAPABILITY)
    for path, method in sorted(set(old_ops) - set(new_ops)):
        out.add(f"paths/{path}/{method}", "operation-removed", CALLER_ADAPTATION)

    for key in sorted(set(old_ops) & set(new_ops)):
        path, method = key
        pointer = f"paths/{path}/{method}"
        (old_item, old_op), (new_item, new_op) = old_ops[key], new_ops[key]

        old_params = _parameters(old, old_item, old_op)
        new_params = _parameters(new, new_item, new_op)
        for name in sorted(set(new_params) - set(old_params)):
            required = bool(new_params[name].get("required"))
            out.add(
                f"{pointer}/parameters/{name}",
                "required-parameter-added" if required else "parameter-added",
                CALLER_ADAPTATION if required else CAPABILITY,
            )
        for name in sorted(set(old_params) - set(new_params)):
            out.add(f"{pointer}/parameters/{name}", "parameter-removed", CALLER_ADAPTATION)
        for name in sorted(set(old_params) & set(new_params)):
            _diff_parameter(old_params[name], new_params[name], f"{pointer}/parameters/{name}", out)

        _diff_request_body(old, new, old_op, new_op, pointer, out)
        _diff_responses(old, new, old_op, new_op, pointer, out)

        if old_op.get("operationId") != new_op.get("operationId"):
            out.add(f"{pointer}/operationId", "operation-id-changed", CALLER_ADAPTATION)
        if _canon(old_op.get("security")) != _canon(new_op.get("security")):
            out.add(f"{pointer}/security", "security-changed", CALLER_ADAPTATION)

    old_components = old.get("components") or {}
    new_components = new.get("components") or {}
    _diff_named_schemas(
        old_components.get("schemas") or {},
        new_components.get("schemas") or {},
        "components/schemas",
        out,
    )
    for section in ("parameters", "requestBodies", "responses"):
        old_section = old_components.get(section) or {}
        new_section = new_components.get(section) or {}
        for name in sorted(set(old_section) - set(new_section)):
            out.add(f"components/{section}/{name}", "component-removed", CALLER_ADAPTATION)
    if _canon(old_components.get("securitySchemes")) != _canon(
        new_components.get("securitySchemes")
    ) or _canon(old.get("security")) != _canon(new.get("security")):
        out.add("security", "security-changed", CALLER_ADAPTATION)

    return out.findings


# ---------------------------------------------------------------------------
# MCP tool catalog
# ---------------------------------------------------------------------------


def _tools(catalog: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (surface, tool["name"]): tool
        for surface, tools in (catalog.get("surfaces") or {}).items()
        for tool in tools
    }


def diff_mcp_catalog(old: dict[str, Any], new: dict[str, Any]) -> list[Finding]:
    """Every contract-visible difference between two MCP tool catalogs.

    A tool's name, input schema, output schema, and the surface that lists it
    are contract; its description, title, and annotations are not.
    """
    out = _Collector("mcp")
    old_tools, new_tools = _tools(old), _tools(new)
    added = set(new_tools) - set(old_tools)
    removed = set(old_tools) - set(new_tools)

    moved_to: set[tuple[str, str]] = set()
    for surface, name in sorted(removed):
        destination = next(
            (key for key in sorted(added) if key[1] == name and key not in moved_to), None
        )
        if destination is None:
            out.add(f"{surface}/{name}", "tool-removed", CALLER_ADAPTATION)
        else:
            moved_to.add(destination)
            out.add(f"{surface}/{name}", "tool-moved", CALLER_ADAPTATION)
    for surface, name in sorted(added - moved_to):
        out.add(f"{surface}/{name}", "tool-added", CAPABILITY)

    for key in sorted(set(old_tools) & set(new_tools)):
        surface, name = key
        pointer = f"{surface}/{name}"
        old_tool, new_tool = old_tools[key], new_tools[key]
        _diff_schema(
            old_tool.get("inputSchema") or {},
            new_tool.get("inputSchema") or {},
            f"{pointer}/inputSchema",
            out,
        )
        old_output, new_output = old_tool.get("outputSchema"), new_tool.get("outputSchema")
        if old_output is None and new_output is not None:
            out.add(f"{pointer}/outputSchema", "output-schema-added", CAPABILITY)
        elif old_output is not None and new_output is None:
            out.add(f"{pointer}/outputSchema", "output-schema-removed", CALLER_ADAPTATION)
        elif old_output is not None:
            _diff_schema(old_output, new_output, f"{pointer}/outputSchema", out)

    return out.findings


# ---------------------------------------------------------------------------
# Whole contract
# ---------------------------------------------------------------------------


def diff_contracts(old: Contract, new: Contract) -> tuple[list[Finding], list[str]]:
    """Findings across all three artifacts, and notes on what was not compared.

    An artifact absent from the older snapshot has no baseline and produces a
    note rather than findings: the change that starts publishing an artifact
    declares what already existed. One absent from the newer snapshot is a
    removal of the whole surface.
    """
    findings: list[Finding] = []
    notes: list[str] = []
    pairs = (
        ("sage_core_api", old.sage_core_api, new.sage_core_api),
        ("cas_app_api", old.cas_app_api, new.cas_app_api),
        ("mcp", old.mcp_catalog, new.mcp_catalog),
    )
    for label, old_doc, new_doc in pairs:
        if old_doc is None and new_doc is None:
            continue
        if old_doc is None:
            notes.append(f"{label}: no baseline at the older revision; not compared")
            continue
        if new_doc is None:
            findings.append(Finding(label, "", "artifact-removed", CALLER_ADAPTATION))
            continue
        if label == "mcp":
            findings.extend(diff_mcp_catalog(old_doc, new_doc))
        else:
            findings.extend(diff_openapi(old_doc, new_doc, surface=label))
    return findings, notes


def _load(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("old", type=Path, help="the older snapshot")
    parser.add_argument("new", type=Path, help="the newer snapshot")
    parser.add_argument(
        "--mcp", action="store_true", help="compare MCP tool catalogs rather than OpenAPI documents"
    )
    args = parser.parse_args(argv)

    old, new = _load(args.old), _load(args.new)
    findings = diff_mcp_catalog(old, new) if args.mcp else diff_openapi(old, new, surface="openapi")
    for finding in findings:
        print(finding.render())
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
