"""Architectural boundary enforcement for the sage.maintenance package.

The ``sage.maintenance`` package is operator-only by construction: document
removal is absent from the SAGE request surface by the No-Delete Invariant
(CAS-ADR-029), so no module on that surface -- ``sage.mcp_server``,
``sage.sage_api_tools``, or anything under ``sage.api`` -- may import any
``sage.maintenance`` module, directly or transitively.

This test parses every ``.py`` file under ``sage/`` with ``ast``, builds a
module-to-imports graph, and runs BFS from each request-surface module. If any
``sage.maintenance.*`` node is reachable, the test fails with the shortest import
chain that reaches it.
"""

from __future__ import annotations

import ast
from collections import deque
from collections.abc import Iterable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SAGE_ROOT = REPO_ROOT / "sage"
FORBIDDEN_PREFIX = "sage.maintenance"
ARCHITECTURE_ANCHOR = "SAGE No-Delete Invariant (CAS-ADR-029)"


def _module_metadata(path: Path) -> tuple[str, str]:
    """Return (module_name, package_name) for a .py file under REPO_ROOT.

    sage/api/__init__.py -> ("sage.api", "sage.api")
    sage/api/errors.py   -> ("sage.api.errors", "sage.api")
    sage/mcp_server.py   -> ("sage.mcp_server", "sage")
    """
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
        module_name = ".".join(parts)
        package_name = module_name
    else:
        module_name = ".".join(parts)
        package_name = ".".join(parts[:-1])
    return module_name, package_name


def _resolve_relative(current_package: str, level: int, module: str | None) -> str | None:
    parts = current_package.split(".") if current_package else []
    drop = level - 1
    if drop > 0:
        if drop > len(parts):
            return None
        parts = parts[:-drop]
    if not parts:
        return None
    base = ".".join(parts)
    return f"{base}.{module}" if module else base


def _collect_imports(tree: ast.AST, current_package: str) -> set[str]:
    """Collect sage-rooted import targets from a parsed source file.

    For each ``import X`` or ``from X import Y``, records the resolved target
    module name. For from-imports also records ``target.alias`` since the alias
    may itself name a submodule. Targets outside the ``sage`` package are dropped
    -- they cannot reach sage.maintenance.
    """
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sage" or alias.name.startswith("sage."):
                    out.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                target = _resolve_relative(current_package, node.level, node.module)
                if target is None:
                    continue
            else:
                target = node.module
                if target is None:
                    continue
            if not (target == "sage" or target.startswith("sage.")):
                continue
            out.add(target)
            for alias in node.names:
                if alias.name == "*":
                    continue
                out.add(f"{target}.{alias.name}")
    return out


def _build_import_graph() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for path in sorted(SAGE_ROOT.rglob("*.py")):
        module_name, package_name = _module_metadata(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        graph.setdefault(module_name, set()).update(_collect_imports(tree, package_name))
    return graph


def _find_forbidden_chain(
    graph: dict[str, set[str]], starts: str | Iterable[str]
) -> list[str] | None:
    """BFS from ``starts``; return the shortest chain ending at a forbidden node."""
    seeds = [starts] if isinstance(starts, str) else list(starts)
    visited: set[str] = set(seeds)
    queue: deque[tuple[str, list[str]]] = deque((s, [s]) for s in seeds)
    while queue:
        node, path = queue.popleft()
        for child in graph.get(node, set()):
            if child in visited:
                continue
            if child == FORBIDDEN_PREFIX or child.startswith(FORBIDDEN_PREFIX + "."):
                return path + [child]
            visited.add(child)
            queue.append((child, path + [child]))
    return None


def _surface_modules(graph: dict[str, set[str]], surface: str) -> set[str]:
    """Return every module name in ``graph`` that is or lives under ``surface``."""
    return {m for m in graph if m == surface or m.startswith(surface + ".")}


@pytest.fixture(scope="module")
def import_graph() -> dict[str, set[str]]:
    return _build_import_graph()


def _assert_no_breach(graph: dict[str, set[str]], surface: str, seeds: Iterable[str]) -> None:
    chain = _find_forbidden_chain(graph, seeds)
    if chain is None:
        return
    raise AssertionError(
        f"Architectural boundary breach ({ARCHITECTURE_ANCHOR}): "
        f"{surface} transitively imports {FORBIDDEN_PREFIX}. "
        f"Import chain: {' -> '.join(chain)}"
    )


def test_mcp_server_does_not_reach_maintenance(import_graph: dict[str, set[str]]) -> None:
    _assert_no_breach(import_graph, "sage.mcp_server", ["sage.mcp_server"])


def test_sage_api_tools_does_not_reach_maintenance(import_graph: dict[str, set[str]]) -> None:
    assert "sage.sage_api_tools" in import_graph, "sage.sage_api_tools not discovered"
    _assert_no_breach(import_graph, "sage.sage_api_tools", ["sage.sage_api_tools"])


def test_api_does_not_reach_maintenance(import_graph: dict[str, set[str]]) -> None:
    seeds = _surface_modules(import_graph, "sage.api")
    assert seeds, "sage.api package not discovered by the graph builder"
    _assert_no_breach(import_graph, "sage.api", seeds)


def test_chain_finder_detects_synthetic_breach() -> None:
    synthetic = {
        "sage.mcp_server": {"sage.helper"},
        "sage.helper": {"sage.maintenance.purge_document"},
    }
    assert _find_forbidden_chain(synthetic, "sage.mcp_server") == [
        "sage.mcp_server",
        "sage.helper",
        "sage.maintenance.purge_document",
    ]


def test_chain_finder_returns_none_when_no_breach() -> None:
    synthetic = {"sage.mcp_server": {"sage.config"}, "sage.config": set()}
    assert _find_forbidden_chain(synthetic, "sage.mcp_server") is None


def test_graph_builder_collects_known_edge(import_graph: dict[str, set[str]]) -> None:
    """Anti-coincidental-pass guard.

    sage.mcp_server is known to import from sage.mcp_init -- a structural
    dependency (the server cannot register services or resolve the deployment
    profile without it). If the graph builder ever silently returned empty edge
    sets, the breach tests would pass vacuously; this assertion fails loudly in
    that case.
    """
    assert "sage.mcp_init" in import_graph.get("sage.mcp_server", set())
