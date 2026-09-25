"""MCP tool modules take nothing from storage or adapters, even re-exported.

CAS-ADR-052 places every capability beneath both request surfaces, so the
MCP tool modules reach storage and adapters only through the service layer.
The import-linter contract for these modules is module-granular: it sees
``from sage.mcp_init import X`` as an import of ``sage.mcp_init`` whatever
``X`` is, so a storage or adapter name re-exported by an intermediate module
passes it. This gate resolves each imported name to the module that defines
it and refuses any defined under a forbidden package.

It covers names bound by ``from ... import`` statements at any depth,
function-local imports included. It does not cover attributes reached at
runtime through an imported object, such as a service handle's fields, and
it attributes a value of a builtin type (a number or string constant) to the
module it was imported from, so a re-exported constant is not traced.
"""

import ast
import importlib
import importlib.util
import inspect
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

TOOL_MODULES = ("sage/sage_api_tools.py", "sage/app_tools.py")
FORBIDDEN_PACKAGES = ("sage.storage", "sage.adapters", "sage.source_adapters")


def _defining_module(obj: object, imported_from: str) -> str:
    """Name the module that defines ``obj``, as imported from ``imported_from``.

    Classes and functions carry the module that defined them. A module is its
    own definition. Any other value is attributed to the module that defines
    its type, unless that type is a builtin, in which case the value is
    attributed to the module it was imported from.
    """
    if inspect.ismodule(obj):
        return obj.__name__
    if inspect.isclass(obj) or inspect.isroutine(obj):
        return obj.__module__
    type_module = type(obj).__module__
    return imported_from if type_module == "builtins" else type_module


def _is_forbidden(module_name: str) -> bool:
    return any(module_name == p or module_name.startswith(p + ".") for p in FORBIDDEN_PACKAGES)


def _imported_names(source_path: Path, package: str) -> list[tuple[int, str, str]]:
    """Every ``(line, name, defining module)`` bound by a ``from sage... import``.

    Relative imports are resolved against ``package``, the tool module's package.
    """
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(ast.parse(source_path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.ImportFrom):
            continue
        target = importlib.util.resolve_name("." * node.level + (node.module or ""), package)
        if target != "sage" and not target.startswith("sage."):
            continue
        module = importlib.import_module(target)
        for alias in node.names:
            try:
                obj = getattr(module, alias.name)
            except AttributeError:
                obj = importlib.import_module(f"{target}.{alias.name}")
            found.append((node.lineno, alias.name, _defining_module(obj, target)))
    return found


@pytest.mark.parametrize("tool_module", TOOL_MODULES)
def test_tool_module_imports_no_storage_or_adapter_name(tool_module: str) -> None:
    package = tool_module.removesuffix(".py").replace("/", ".").rpartition(".")[0]
    names = _imported_names(REPO_ROOT / tool_module, package)
    assert names, f"{tool_module}: no sage imports found; the walk examined nothing"
    violations = [
        f"{tool_module}:{line} imports {name!r}, defined in {origin}"
        for line, name, origin in names
        if _is_forbidden(origin)
    ]
    assert not violations, "\n".join(violations)
