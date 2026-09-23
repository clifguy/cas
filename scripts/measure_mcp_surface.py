#!/usr/bin/env python3
"""Measure the MCP tool surface as each client presents it to its model.

A client forwards two things of a tool to the model, its ``description`` and
its ``inputSchema``, but not every client forwards the schema as the server
publishes it. A size that names no client measures none of them, so this
script reports each tool in two presented forms:

``raw``
    The ``tools/list`` entry exactly as published. A client that forwards the
    schema unmodified presents this.
``claude_code``
    The schema as Claude Code presents it, transcribed from the tool
    definitions its model receives: every ``title`` keyword and every
    ``anyOf`` / ``oneOf`` union is removed (the node keeps its
    ``description`` and ``default``), definitions the removed unions
    referenced are dropped (every published reference sits inside a union,
    which the test suite holds), ``additionalProperties: false`` is dropped and
    ``true`` becomes ``{}``, ``integer`` is shown as ``number``, and a
    ``$schema`` declaration is added. ``required``, ``type``, ``enum`` and
    ``default`` are kept. Claude Code was also seen omitting ``vault_id``
    from ``required``; the form keeps it, which overstates a tool by at most
    a dozen characters.

A tool's presented size is its description length plus the length of its
schema serialized compactly (sorted keys, no whitespace), so the figure moves
only when the content does.

Usage::

    python -m scripts.measure_mcp_surface                  # both forms, both surfaces
    python -m scripts.measure_mcp_surface --form raw
    python -m scripts.measure_mcp_surface --surface sage --json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Final

FORMS: Final[tuple[str, ...]] = ("raw", "claude_code")

#: The ``$schema`` declaration Claude Code adds to every presented schema.
CLAUDE_CODE_SCHEMA_URI: Final[str] = "http://json-schema.org/draft-07/schema#"

#: Keys whose value maps names to schemas, so the names are not keywords.
_SCHEMA_MAPS: Final[frozenset[str]] = frozenset({"properties", "patternProperties"})
#: Keys whose value is a single subschema or a list of them.
_SUBSCHEMAS: Final[frozenset[str]] = frozenset({"items", "not", "contains", "allOf", "prefixItems"})
_DROPPED: Final[frozenset[str]] = frozenset({"title", "anyOf", "oneOf", "$defs", "definitions"})


def _present(node: Any) -> Any:
    """One schema node in Claude Code's presented form.

    Descends only into keywords whose values are schemas, so a ``default`` or
    ``enum`` value is copied as it is whatever keys it happens to carry.
    """
    if isinstance(node, list):
        return [_present(value) for value in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _DROPPED:
            continue
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            out[key] = {name: _present(sub) for name, sub in value.items()}
        elif key == "additionalProperties":
            if value is True:
                out[key] = {}
            elif isinstance(value, dict):
                out[key] = _present(value)
        elif key == "type" and value == "integer":
            out[key] = "number"
        elif key in _SUBSCHEMAS:
            out[key] = _present(value)
        else:
            out[key] = value
    return out


def claude_code_form(schema: dict[str, Any]) -> dict[str, Any]:
    """``schema`` as Claude Code presents it to its model."""
    return {"$schema": CLAUDE_CODE_SCHEMA_URI, **_present(schema)}


def presented_schema(schema: dict[str, Any], form: str) -> dict[str, Any]:
    """``schema`` in the named presented form."""
    if form == "raw":
        return schema
    if form == "claude_code":
        return claude_code_form(schema)
    raise ValueError(f"unknown form {form!r}; expected one of {FORMS}")


def presented_size(description: str | None, schema: dict[str, Any], form: str) -> int:
    """Characters a client in ``form`` presents for one tool."""
    rendered = json.dumps(
        presented_schema(schema, form), separators=(",", ":"), sort_keys=True, ensure_ascii=False
    )
    return len(description or "") + len(rendered)


def measure(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per-tool and per-surface presented sizes, in every form, for ``catalog``."""
    report: dict[str, dict[str, Any]] = {}
    for surface, tools in catalog["surfaces"].items():
        rows = {
            tool["name"]: {
                form: presented_size(tool["description"], tool["inputSchema"], form)
                for form in FORMS
            }
            for tool in tools
        }
        totals = {form: sum(row[form] for row in rows.values()) for form in FORMS}
        report[surface] = {"tools": rows, "total": totals}
    return report


def _render_text(report: dict[str, dict[str, Any]], forms: tuple[str, ...]) -> str:
    lines: list[str] = []
    for surface, data in report.items():
        header = "  ".join(f"{form:>12}" for form in forms)
        lines.append(f"{surface}  total: " + ", ".join(f"{f}={data['total'][f]}" for f in forms))
        lines.append(f"  {'tool':<36}{header}")
        ranked = sorted(data["tools"].items(), key=lambda kv: -kv[1][forms[0]])
        for name, row in ranked:
            lines.append(f"  {name:<36}" + "  ".join(f"{row[f]:>12}" for f in forms))
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--form", choices=(*FORMS, "both"), default="both")
    parser.add_argument("--surface", help="restrict to one surface, e.g. sage or sage_maint")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)

    from scripts.dump_mcp_catalog import build_catalog

    report = measure(build_catalog())
    if args.surface:
        if args.surface not in report:
            parser.error(f"unknown surface {args.surface!r}; expected one of {sorted(report)}")
        report = {args.surface: report[args.surface]}
    forms = FORMS if args.form == "both" else (args.form,)
    if args.json:
        json.dump(report, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(_render_text(report, forms))
    return 0


if __name__ == "__main__":
    sys.exit(main())
