"""Storage-teardown discipline gate.

A vault's services own two storage resources: the graph store, and the storage
handle whose Postgres pool backs it. ``graph_store.close()`` marks the store
closed and nothing more -- the pool, its connections and its worker tasks stay
open. ``services.close_storage()`` closes both, and it is what production
shutdown and ``initialize_services_for_test`` call.

A test that tears a vault down with ``services.graph_store.close()`` therefore
leaks a pool into its event loop. The leak is silent in the ordinary case and
surfaces rarely and far from its cause: when the loop shuts down, a pool worker
can fail to finish cancelling, and the whole run hangs with every worker idle.
That is why a deterministic check is the right shape here rather than a review
prompt -- the symptom never points at the site.

The walk reports every ``<x>.graph_store.close()`` call in a tracked Python
file under ``tests/`` whose receiver is anything but ``self``. A ``self``
receiver is a fake services object implementing ``close_storage`` itself, which
is the one place closing the graph store alone is correct. A file that closes
the store deliberately and releases the pool on its own is exempted in
``KNOWN_BARE_GRAPH_STORE_CLOSES`` with its reason.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]

#: path (relative to the repository root) -> why its bare graph-store closes
#: are correct. Each such site must release the storage handle itself.
KNOWN_BARE_GRAPH_STORE_CLOSES: Final[dict[str, str]] = {
    "tests/sage/test_storage_binding.py": (
        "drives the provisioner directly: each site closes the handle's graph "
        "store and then the handle, which is the pair close_storage() wraps"
    ),
}


def _bare_graph_store_closes(tree: ast.AST) -> list[int]:
    """Lines calling ``<x>.graph_store.close()`` where ``<x>`` is not ``self``."""
    lines: list[int] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        close = node.func
        if close.attr != "close" or not isinstance(close.value, ast.Attribute):
            continue
        store = close.value
        if store.attr != "graph_store":
            continue
        if isinstance(store.value, ast.Name) and store.value.id == "self":
            continue
        lines.append(node.lineno)
    return lines


def _tracked_test_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "tests"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.endswith(".py")]


def test_detector_reports_a_services_close_and_spares_self() -> None:
    source = (
        "async def teardown(services, registry):\n"
        "    await services.graph_store.close()\n"
        "    await registry['v'].graph_store.close()\n"
        "\n"
        "class Fake:\n"
        "    async def close_storage(self):\n"
        "        await self.graph_store.close()\n"
        "\n"
        "async def ok(services):\n"
        "    await services.close_storage()\n"
    )
    assert _bare_graph_store_closes(ast.parse(source)) == [2, 3]


def test_the_walk_reaches_the_test_tree() -> None:
    files = _tracked_test_files()
    assert len(files) > 100, "the walk found almost nothing; it would pass vacuously"
    assert "tests/sage/test_storage_binding.py" in files


def test_no_test_tears_a_vault_down_through_the_graph_store_alone() -> None:
    offenders: list[str] = []
    for rel in _tracked_test_files():
        if rel in KNOWN_BARE_GRAPH_STORE_CLOSES:
            continue
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"), filename=rel)
        offenders.extend(f"{rel}:{line}" for line in _bare_graph_store_closes(tree))
    assert not offenders, (
        "these sites close a vault's graph store without its storage handle, "
        "leaking the Postgres pool; call services.close_storage() instead:\n  "
        + "\n  ".join(offenders)
    )


def test_every_exemption_is_still_needed() -> None:
    stale = []
    for rel in KNOWN_BARE_GRAPH_STORE_CLOSES:
        path = REPO_ROOT / rel
        if not path.exists() or not _bare_graph_store_closes(ast.parse(path.read_text())):
            stale.append(rel)
    assert not stale, f"exemptions no longer matching any bare close: {stale}"
