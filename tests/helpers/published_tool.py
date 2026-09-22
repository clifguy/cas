"""Read a tool as an MCP client receives it, not as its source declares it.

A client forwards two things of a tool to the model: the ``description`` and
the ``inputSchema``. A disclosure gate that reads the Python docstring
certifies text a caller may never be shown, and fails for the wrong reason
when a parameter's documentation moves from the docstring into the schema,
where it travels with the parameter. The gates that ask whether a caller is
told something read ``published_text`` instead.
"""

from __future__ import annotations

from functools import cache
from typing import Any

from sage import mcp_server
from sage._tool_naming import MCP_HTTP_MOUNTS


@cache
def published_tools_on(surface: str) -> dict[str, Any]:
    """Registered tools on one partitioned surface, keyed by name.

    The FastMCP ``Tool`` models rather than the raw callables, because
    ``annotations``, ``description`` and ``parameters`` live on the ``Tool``
    and not on the function it wraps.
    """
    server = mcp_server.build_partitioned_server(surface)
    return {t.name: t for t in server._tool_manager.list_tools()}  # noqa: SLF001


def published_surfaces() -> tuple[str, ...]:
    """Each partitioned surface, in mount-table order."""
    return tuple(dict.fromkeys(surface for _, surface in MCP_HTTP_MOUNTS))


def published_tools() -> dict[str, Any]:
    """Every registered tool across the partitioned surfaces, keyed by name."""
    tools: dict[str, Any] = {}
    for surface in published_surfaces():
        tools.update(published_tools_on(surface))
    return tools


def published_tool(name: str) -> Any:
    """The registered ``Tool`` named ``name``, as its surface publishes it."""
    return published_tools()[name]


def tool_name_of(fn: object) -> str:
    """The registered name of a tool function, whatever it was imported as.

    Tool functions are re-exported under legacy aliases, so a function's
    ``__name__`` need not be the name its surface registers.
    """
    for attr in ("_sage_tools", "_app_tools"):
        for name, registered in getattr(mcp_server, attr).items():
            if registered is fn:
                return name
    raise LookupError(f"{getattr(fn, '__name__', fn)!r} is not a registered MCP tool")


def parameter_descriptions(schema: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Each property ``description`` in a JSON schema, keyed by dotted path.

    Walks nested objects, array items, and ``anyOf``/``oneOf`` arms, because an
    item schema's field descriptions reach the model exactly as a top-level
    parameter's do.
    """
    found: dict[str, str] = {}
    for name, prop in (schema.get("properties") or {}).items():
        path = f"{prefix}{name}"
        if isinstance(prop.get("description"), str):
            found[path] = prop["description"]
        found.update(_nested(prop, f"{path}."))
    return found


def _nested(node: dict[str, Any], prefix: str) -> dict[str, str]:
    found: dict[str, str] = {}
    if "properties" in node:
        found.update(parameter_descriptions(node, prefix))
    if isinstance(node.get("items"), dict):
        found.update(_nested(node["items"], prefix))
    for key in ("anyOf", "oneOf"):
        for arm in node.get(key) or ():
            found.update(_nested(arm, prefix))
    return found


def published_text(name: str) -> str:
    """The tool's description followed by every parameter description.

    This is the whole of what a client shows the model about the tool, so a
    phrase a caller must be told is asserted against it. The parameter
    descriptions are rendered as a trailing ``Args:`` block, one
    ``path: text`` line each, which is the role they play: every parser that
    splits a docstring into prose, error modes and arguments reads the
    rendering exactly as it read a docstring that documented its arguments
    inline.
    """
    tool = published_tool(name)
    params = parameter_descriptions(tool.parameters)
    text = (tool.description or "").rstrip("\n")
    if not params:
        return text
    lines = "\n".join(f"    {path}: {desc}" for path, desc in params.items())
    return f"{text}\n\nArgs:\n{lines}\n"
