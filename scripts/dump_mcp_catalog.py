#!/usr/bin/env python3
"""Render the published MCP tool catalog for both MCP surfaces.

The MCP surfaces publish their contract as a ``tools/list`` response generated
from code at startup, so unlike the two OpenAPI specifications there is no
committed document a change can be compared against. This script renders that
response for the ordinary and maintenance surfaces into
``docs/fs/sage/sage_mcp_tools.catalog.json``, which the substrate manifest lists
and which the contract-diff detector reads (CAS-ADR-008). A gate in the test
suite holds the committed file to the servers, so a change to a tool's name,
signature, or docstring cannot land without the catalog moving with it.

Each tool carries what a client reads from ``tools/list``: its name, title,
description, input schema, output schema, and annotations. Server instructions
are omitted, because they embed the running build's version and would move on
every commit.

Usage::

    python -m scripts.dump_mcp_catalog           # print the catalog
    python -m scripts.dump_mcp_catalog --write   # rewrite the committed file
    python -m scripts.dump_mcp_catalog --check   # exit 1 if the file is stale
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
CATALOG_PATH: Final[Path] = REPO_ROOT / "docs" / "fs" / "sage" / "sage_mcp_tools.catalog.json"

CATALOG_TITLE: Final[str] = "SAGE MCP Tool Catalog"
CATALOG_DESCRIPTION: Final[str] = (
    "The tools each SAGE MCP surface publishes through tools/list, generated from "
    "the servers and held to them by a test gate. The ordinary surface is mounted "
    "at /mcp and the maintenance surface at /mcp_maint. A tool's name, input "
    "schema, output schema, and surface are contract; its description, title, and "
    "annotations are published documentation."
)


def _tool_entry(tool: Any) -> dict[str, Any]:
    annotations = (
        tool.annotations.model_dump(mode="json", exclude_none=True) if tool.annotations else None
    )
    return {
        "name": tool.name,
        "title": tool.title,
        "description": tool.description,
        "inputSchema": tool.inputSchema,
        "outputSchema": tool.outputSchema,
        "annotations": annotations,
    }


def build_catalog() -> dict[str, Any]:
    """The catalog both surfaces publish, tools sorted by name."""
    from sage import mcp_server
    from sage._tool_naming import MCP_HTTP_MOUNTS

    surfaces: dict[str, list[dict[str, Any]]] = {}
    for _mount, surface in MCP_HTTP_MOUNTS:
        tools = asyncio.run(mcp_server.build_partitioned_server(surface).list_tools())
        surfaces[surface] = [_tool_entry(tool) for tool in sorted(tools, key=lambda t: t.name)]
    return {"title": CATALOG_TITLE, "description": CATALOG_DESCRIPTION, "surfaces": surfaces}


def render_catalog(catalog: dict[str, Any]) -> str:
    """The catalog's committed text: stable key order, one trailing newline."""
    return json.dumps(catalog, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="rewrite the committed catalog")
    mode.add_argument(
        "--check", action="store_true", help="exit 1 if the committed catalog is stale"
    )
    args = parser.parse_args(argv)

    rendered = render_catalog(build_catalog())
    if args.write:
        CATALOG_PATH.write_text(rendered, encoding="utf-8")
        print(f"wrote {CATALOG_PATH.relative_to(REPO_ROOT)}")
        return 0
    if args.check:
        current = CATALOG_PATH.read_text(encoding="utf-8") if CATALOG_PATH.exists() else ""
        if current != rendered:
            print(
                f"{CATALOG_PATH.relative_to(REPO_ROOT)} is stale; "
                "run python -m scripts.dump_mcp_catalog --write",
                file=sys.stderr,
            )
            return 1
        return 0
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
