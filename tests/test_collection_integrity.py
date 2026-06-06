"""Test-collection integrity gate.

A ``test_*``-prefixed function is only collected by pytest when it sits at
module scope or as a method of a ``Test*`` class. When such a function ends
up **nested inside another function** — typically because a stray
module-level ``def`` at column 0 dedents out of a class body and reparents
every method written after it — pytest silently skips it. The test still
parses, still imports, and never runs: a green suite that secretly covers
nothing.

Neither CI nor a line-coverage floor detects this, because the dead code is
test code (it is the thing that would have produced the coverage). This gate
is the substrate-level backstop: a deterministic check that walks the AST of
every tracked test module and fails the build whenever a ``test_*`` function
is found nested inside another function.

The detector mirrors pytest's own collection rule (``python_functions`` in
``pyproject.toml`` → the ``test_`` prefix; ``python_classes`` → ``Test*``):
a class body resets function scope, so methods are fine; a function body does
not, so a ``test_`` def inside it is orphaned.

Allowlist convention follows ``KNOWN_VIOLATIONS`` in
``tests/sage/test_typed_alias_coverage.py`` and the allowlists in
``tests/test_public_posture.py``: empty by default, every entry carrying a
one-line rationale.

Anti-coincidental coverage: ``test_detector_flags_nested_test_functions`` and
``test_detector_ignores_class_methods_and_module_funcs`` exercise the walk
against synthetic source strings, proving the detector has teeth independent
of whatever the live tree happens to contain.
"""

import ast
import subprocess
import textwrap
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

# pytest collects functions whose name matches ``python_functions`` (the
# ``test_*`` glob in pyproject.toml). A function with this prefix nested
# inside another function is what this gate hunts for.
_TEST_FUNCTION_PREFIX: Final[str] = "test_"

# Maximum number of violations to enumerate in a single pytest.fail message.
_MAX_REPORTED: Final[int] = 30


# ---------------------------------------------------------------------------
# Allowlist
#
# path (relative to repo root) → names of nested ``test_*`` functions that
# are intentionally nested (and therefore intentionally uncollected). Empty
# by default; nesting a test function is virtually always a collection bug.
# Every entry added later requires a 1-line rationale alongside it.
# ---------------------------------------------------------------------------

ORPHANED_TEST_ALLOWLIST: Final[dict[str, list[str]]] = {}


# ---------------------------------------------------------------------------
# Tracked test-module enumeration
# ---------------------------------------------------------------------------


def _tracked_files() -> list[Path]:
    """Every file tracked by git, as absolute Path objects.

    Mirrors what the repository actually ships (vs. a filesystem walk that
    would include untracked working-tree files).
    """
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [REPO_ROOT / line for line in result.stdout.splitlines() if line]


def _tracked_test_modules() -> list[Path]:
    """Tracked ``.py`` files that pytest would collect as test modules.

    Matches ``testpaths = ["tests"]`` and ``python_files = "test_*.py"`` from
    pyproject.toml: files under ``tests/`` whose basename starts with
    ``test_``.
    """
    modules: list[Path] = []
    for path in _tracked_files():
        try:
            rel = path.relative_to(REPO_ROOT)
        except ValueError:
            continue
        if (
            rel.parts
            and rel.parts[0] == "tests"
            and path.suffix == ".py"
            and path.name.startswith(_TEST_FUNCTION_PREFIX)
        ):
            modules.append(path)
    return modules


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


def _orphaned_test_functions(tree: ast.AST) -> list[tuple[int, str, str]]:
    """Return ``(lineno, name, enclosing_function)`` for every ``test_*``
    function nested inside another function.

    A ``ClassDef`` resets the enclosing-function context (its methods are
    legitimately collectable), so only function bodies orphan a nested test.
    """
    found: list[tuple[int, str, str]] = []

    def walk(node: ast.AST, enclosing_fn: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if enclosing_fn is not None and child.name.startswith(_TEST_FUNCTION_PREFIX):
                    found.append((child.lineno, child.name, enclosing_fn))
                walk(child, child.name)
            elif isinstance(child, ast.ClassDef):
                walk(child, None)
            else:
                walk(child, enclosing_fn)

    walk(tree, None)
    return found


def _format_violations(violations: list[tuple[str, int, str]]) -> str:
    """Render a violation list as a pytest.fail-friendly message."""
    head = violations[:_MAX_REPORTED]
    body = "\n".join(f"  {path}:{line} → {detail}" for path, line, detail in head)
    overflow = len(violations) - len(head)
    tail = f"\n  ... and {overflow} more" if overflow > 0 else ""
    return (
        f"Nested (uncollected) test functions ({len(violations)} found):\n"
        f"{body}{tail}\n"
        "A test_* function defined inside another function is never collected "
        "by pytest. Move it to module scope or into a Test* class."
    )


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_no_orphaned_test_functions() -> None:
    """No tracked test module may define a ``test_*`` function nested inside
    another function, where pytest would silently never collect it.
    """
    violations: list[tuple[str, int, str]] = []
    for path in _tracked_test_modules():
        rel = str(path.relative_to(REPO_ROOT))
        try:
            tree = ast.parse(path.read_bytes(), filename=str(path))
        except SyntaxError:
            # A syntactically broken test module is a different failure mode
            # (it fails its own collection loudly); not this gate's concern.
            continue
        allowed = set(ORPHANED_TEST_ALLOWLIST.get(rel, []))
        for lineno, name, enclosing in _orphaned_test_functions(tree):
            if name in allowed:
                continue
            violations.append((rel, lineno, f"{name} (inside {enclosing})"))

    if violations:
        pytest.fail(_format_violations(violations))


# ---------------------------------------------------------------------------
# Anti-coincidental detector self-tests
# ---------------------------------------------------------------------------

# A class with a real method, a module-level test function, and two test
# functions nested inside that module-level function — the exact shape a
# dedent-out-of-class slip produces. Kept as a string so the gate's own AST
# walk over this file does not see the nested defs as real code.
_SYNTHETIC_ORPHANED_SOURCE: Final[str] = textwrap.dedent(
    """
    class TestStore:
        async def test_collected_method(self):
            assert True

    async def test_stub_module_level():
        assert True

        async def test_orphaned_ad_001(self):
            assert True

        async def test_orphaned_ad_002(self):
            assert True
    """
)

# Only legitimately-collectable test functions: class methods (sync + async)
# and module-level functions (sync + async).
_SYNTHETIC_CLEAN_SOURCE: Final[str] = textwrap.dedent(
    """
    class TestStore:
        def test_method_a(self):
            assert True

        async def test_method_b(self):
            assert True

    def test_module_func():
        assert True

    async def test_async_module_func():
        assert True
    """
)

# A test function whose nested defs are NOT test-prefixed helpers — these are
# collected fine (the helper defs are never meant to be collected).
_SYNTHETIC_NESTED_HELPERS_SOURCE: Final[str] = textwrap.dedent(
    """
    def test_with_local_helpers():
        def _build():
            return 1

        def inner():
            return 2

        assert _build() + inner() == 3
    """
)


def test_detector_flags_nested_test_functions() -> None:
    """The detector reports both nested ``test_*`` defs and attributes them to
    the function that swallowed them; it leaves the class method and the
    module-level function alone.
    """
    found = _orphaned_test_functions(ast.parse(_SYNTHETIC_ORPHANED_SOURCE))
    names = sorted(name for _, name, _ in found)
    assert names == ["test_orphaned_ad_001", "test_orphaned_ad_002"]
    assert {enclosing for _, _, enclosing in found} == {"test_stub_module_level"}


def test_detector_ignores_class_methods_and_module_funcs() -> None:
    """Methods of a ``Test*`` class and module-level test functions — sync and
    async — are collectable and must not be flagged.
    """
    assert _orphaned_test_functions(ast.parse(_SYNTHETIC_CLEAN_SOURCE)) == []


def test_detector_ignores_non_test_nested_helpers() -> None:
    """Nested defs that are not ``test_*``-prefixed are ordinary local
    helpers, not orphaned tests, and must not be flagged.
    """
    assert _orphaned_test_functions(ast.parse(_SYNTHETIC_NESTED_HELPERS_SOURCE)) == []
