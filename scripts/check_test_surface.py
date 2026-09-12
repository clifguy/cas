#!/usr/bin/env python3
"""Compare the set of tests that actually run, before and after a change.

A change can shrink the regression surface without producing a single failing
signal. The suite stays green precisely because the lost tests no longer run,
and a line-coverage floor does not observe them either: the lines they
exercised are usually still touched by something else. Two shapes have been
observed in this repository, and they fail differently.

**A test stops being collected.** A module-level ``def`` at column 0 lands
inside a class body; every method written after it dedents out of the class,
reparents into a nested function, and pytest silently stops collecting it. The
file still parses and still imports. ``tests/test_collection_integrity.py`` is
the deterministic gate against that specific mechanism, walking the AST for a
``test_*`` function nested inside another function. It does not generalize: it
cannot see a deleted module, a class renamed off the ``Test*`` prefix, a
shrunken ``parametrize`` set, or the second shape below.

**A test stops being selected.** A class gains an opt-in ``skipif`` gate. The
tests remain perfectly collectable -- a local run with the opt-in set collects
and runs them normally -- but they stop running in the environment that had
been running them. Comparing collected counts is blind to this, because the
count does not move.

The measurement that unifies both is the set of tests that would *actually
execute in a given environment*: the first shape removes ids from collection,
the second moves ids from active to skipped, and both drop out of that set.
This check emits that set for one tree and compares two of them.

Three properties keep it usable rather than merely correct:

* **Sets, not counts.** A change that adds one test while silently losing
  another leaves the count flat. The observed instance of the first shape did
  exactly that -- it added two stub functions in the same edit that disabled
  fourteen methods.
* **A relocated test is not a lost test.** Before reporting a loss, the leaf
  key (everything after the last ``.py::``) is counted across the new active
  set. Without this escape, every file rename reads as a mass contraction,
  which is how a gate like this gets switched off in a week. It has to be a
  count rather than a presence check: method names repeat across parallel
  modules, so a rule asking only whether the name survives somewhere lets a
  deleted module hide behind its namesakes.
* **An empty measurement fails.** A base side that collected nothing is a
  broken comparison, not a clean one, and is reported as such rather than
  passing.

Deliberate removals are declared in ``KNOWN_TEST_REMOVALS`` below, which puts
them in the diff where a reviewer sees them.

The comparison is only as good as the environment it runs in: a check run
where the opt-in variables differ from the suite whose coverage is at stake
measures the wrong thing, which is the second shape recurring one level up.
Both sides must be emitted under the same environment as that suite.

Usage::

    python -m scripts.check_test_surface --emit --out head.json
    python -m scripts.check_test_surface --emit --target tests/sage --out part.json
    python -m scripts.check_test_surface --compare base.json head.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Final, TypedDict

# Evaluating a ``skipif`` mark without running the test is what separates this
# check from a collected-count comparison, and pytest exposes it only on a
# private module. The dependency is deliberate and pinned by the lockfile, so
# an upgrade that moves it is always a deliberate act; the accompanying test
# named for this import turns that move into a pointed failure rather than a
# silent degradation to collection-only.
try:
    from _pytest.skipping import evaluate_skip_marks
except ImportError as exc:  # pragma: no cover - exercised by the named guard test
    raise ImportError(
        "cannot import evaluate_skip_marks from _pytest.skipping; without it this "
        "check degrades to a collected-count comparison, which cannot see a test "
        "that stopped running in this environment while remaining collectable"
    ) from exc

# Minimum active tests the base side must carry for the comparison to mean
# anything. A base that measured nothing reports no contraction, which is the
# worst shape of false green: the gate goes quiet exactly when it broke. The
# floor is an order of magnitude below the observed size of this suite (~7,000
# collected), so it catches a failed or misdirected measurement without
# tracking the suite's real growth.
MIN_BASE_ACTIVE: Final[int] = 500

# Maximum number of entries to enumerate in a single report section.
_MAX_REPORTED: Final[int] = 30


class Surface(TypedDict):
    """One revision's measurement, as it crosses the boundary between the two
    steps that produce and consume it.

    Written as JSON by ``--emit`` and read back by ``--compare``, in the
    general case from a different checkout, so this is a file format rather
    than an in-process structure and is named accordingly.
    """

    target: str
    exit_code: int
    collected: list[str]
    active: list[str]


# ---------------------------------------------------------------------------
# Declared removals
#
# node id (exact) or prefix ending in ``::`` or ``.py`` -> one-line reason for
# the tests no longer running. Empty by default: a shrinking regression
# surface is almost always accidental, and the few deliberate cases should be
# visible in the diff that causes them.
#
# Entries go inert on their own. Once the removal lands on the default branch
# it is absent from the base side too, so the entry never matches again;
# pruning a landed entry is housekeeping, not correctness.
#
# Reasons describe what changed in the product, not which piece of work
# changed it -- this is a durable code surface, where CAS-ADR-NNN is the only
# sanctioned anchor.
# ---------------------------------------------------------------------------

KNOWN_TEST_REMOVALS: Final[dict[str, str]] = {
    "tests/sage/test_graph_store_seam.py::test_stub_hash_lookup_signature_matches_port": (
        "single-method stub signature check absorbed by the stub arm of the graph-store "
        "signature gate, which covers every port method"
    ),
    **{
        f"tests/sage/test_retrieval.py::"
        f"test_semantic_and_keyword_responses_are_not_degraded[{mode}]": (
            "pinned scored modes carrying no budget element; replaced by "
            "test_scored_responses_reach_a_budget_outcome_but_never_the_catalog_degrade, "
            "which requires one now that scored responses are excerpted over budget"
        )
        for mode in ("semantic", "keyword")
    },
}


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


class _SurfacePlugin:
    """Record every collected item and whether it would actually run.

    ``pytest_collection_modifyitems`` is the last hook that sees the whole
    collected set, and it runs under ``--collect-only``, so the walk costs one
    collection pass rather than a suite run. ``evaluate_skip_marks`` returns a
    ``Skip`` for an item a ``skip`` or ``skipif`` mark takes out of the run,
    and ``None`` for one that survives to execution.

    Skips raised at run time -- from inside a fixture or a test body -- are
    invisible here, because nothing has run. That is a known limit: this
    measures the statically determined active set, which is where both
    observed contractions lived.
    """

    def __init__(self) -> None:
        self.collected: list[str] = []
        self.active: list[str] = []

    def pytest_collection_modifyitems(self, items: list[Any]) -> None:
        for item in items:
            self.collected.append(item.nodeid)
            if evaluate_skip_marks(item) is None:
                self.active.append(item.nodeid)


def measure(target: str) -> Surface:
    """Collect ``target`` and return its collected and active node id sets.

    ``-n 0`` keeps collection on one process: the parallel default would both
    distribute the walk and, through the worker-sizing hook, open a database
    connection this measurement has no use for. ``-p no:cacheprovider`` keeps
    the run from writing a cache directory into the tree being measured, which
    matters when that tree is a throwaway checkout of another revision.
    """
    import pytest

    plugin = _SurfacePlugin()
    outcome = pytest.main(
        ["--collect-only", "-q", "-p", "no:cacheprovider", "-n", "0", target],
        plugins=[plugin],
    )
    return {
        "target": target,
        "exit_code": int(outcome),
        "collected": sorted(plugin.collected),
        "active": sorted(plugin.active),
    }


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def leaf_key(node_id: str) -> str:
    """The part of a node id that survives moving the test to another module.

    ``tests/sage/test_x.py::TestThing::test_case[a]`` yields
    ``TestThing::test_case[a]``. A node id with no module separator is its own
    leaf key.
    """
    marker = ".py::"
    index = node_id.rfind(marker)
    return node_id if index == -1 else node_id[index + len(marker) :]


def is_declared(node_id: str, removals: dict[str, str]) -> bool:
    """True when ``node_id`` is covered by a declared-removal entry.

    An entry matches exactly, or as a prefix when it ends in ``::`` (a class or
    module boundary) or ``.py`` (a whole module). Bare substring matching is
    deliberately not offered: it would let a short entry silently cover tests
    nobody meant to waive.
    """
    if node_id in removals:
        return True
    return any(
        node_id.startswith(entry)
        for entry in removals
        if entry.endswith("::") or entry.endswith(".py")
    )


def classify(
    base: Surface,
    head: Surface,
    removals: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    """Partition the base-to-head change in the active set.

    ``lost`` is the only failing category. ``deactivated`` is the subset of it
    that head still collects -- the test is still there and still parses, it
    just no longer runs -- because that distinction points at a different fix
    from a deletion.

    The relocation escape compares leaf-key *populations*, not membership.
    Method names repeat heavily across parallel modules in a suite of any size,
    so asking only whether the leaf key still exists somewhere cannot separate
    "this test moved" from "a namesake in another module survived and this one
    is gone" -- and under that weaker rule, deleting a whole module whose names
    recur elsewhere reports every one of its tests as relocated and exits
    clean. Requiring the head's count for a leaf key to hold at the base's
    keeps a true move (the count is unchanged) and reports a deletion beside a
    namesake (the count drops).
    """
    entries = KNOWN_TEST_REMOVALS if removals is None else removals
    base_active = set(base["active"])
    head_active = set(head["active"])
    head_collected = set(head["collected"])
    base_leaves = Counter(leaf_key(node_id) for node_id in base_active)
    head_leaves = Counter(leaf_key(node_id) for node_id in head_active)

    lost: list[str] = []
    moved: list[str] = []
    declared: list[str] = []
    for node_id in sorted(base_active - head_active):
        leaf = leaf_key(node_id)
        if is_declared(node_id, entries):
            declared.append(node_id)
        elif head_leaves[leaf] >= base_leaves[leaf]:
            moved.append(node_id)
        else:
            lost.append(node_id)

    return {
        "lost": lost,
        "deactivated": [node_id for node_id in lost if node_id in head_collected],
        "moved": moved,
        "declared": declared,
        "added": sorted(head_active - base_active),
    }


def _section(title: str, entries: list[str]) -> str:
    head = entries[:_MAX_REPORTED]
    body = "\n".join(f"  {entry}" for entry in head)
    overflow = len(entries) - len(head)
    tail = f"\n  ... and {overflow} more" if overflow > 0 else ""
    return f"{title} ({len(entries)}):\n{body}{tail}"


def format_report(
    base: Surface,
    head: Surface,
    verdict: dict[str, list[str]],
) -> str:
    """Render the comparison, failing sections first."""
    lines = [
        f"tests active: {len(base['active'])} before, {len(head['active'])} after "
        f"({len(base['collected'])} and {len(head['collected'])} collected)",
    ]
    if verdict["lost"]:
        lines.append("")
        lines.append(_section("Tests that no longer run", verdict["lost"]))
        if verdict["deactivated"]:
            lines.append("")
            lines.append(
                _section(
                    "  of which are still collected, so they were gated rather than removed",
                    verdict["deactivated"],
                )
            )
        lines.append("")
        lines.append(
            "Each of these ran before this change and does not run now. If that is "
            "deliberate, add the node id (or a '::' / '.py' prefix) to "
            "KNOWN_TEST_REMOVALS in scripts/check_test_surface.py with a one-line "
            "reason, so the removal is visible in the diff."
        )
    for title, key in (
        ("Tests moved to another module", "moved"),
        ("Declared removals", "declared"),
    ):
        if verdict[key]:
            lines.append("")
            lines.append(_section(title, verdict[key]))
    if verdict["added"]:
        lines.append("")
        lines.append(f"newly running: {len(verdict['added'])}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load(path: str) -> Surface:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _run_compare(base_path: str, head_path: str) -> int:
    base = _load(base_path)
    head = _load(head_path)

    if len(base["active"]) < MIN_BASE_ACTIVE:
        print(
            f"the earlier measurement carries {len(base['active'])} active tests, "
            f"below the floor of {MIN_BASE_ACTIVE}; refusing to report a comparison "
            "against a measurement that appears to have examined nothing",
            file=sys.stderr,
        )
        return 1

    verdict = classify(base, head)
    print(format_report(base, head, verdict))
    return 1 if verdict["lost"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare the set of tests that actually run, before and after a change."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--emit",
        action="store_true",
        help="measure this tree and write the collected and active node id sets.",
    )
    mode.add_argument(
        "--compare",
        nargs=2,
        metavar=("EARLIER", "LATER"),
        help="compare two measurements and fail if any test stopped running.",
    )
    parser.add_argument(
        "--target",
        default="tests",
        help="path to collect when emitting (default: tests).",
    )
    parser.add_argument(
        "--out",
        help="write the measurement here instead of standard output.",
    )
    args = parser.parse_args(argv)

    if args.compare:
        return _run_compare(*args.compare)

    surface = measure(args.target)
    if surface["exit_code"] not in (0, 5):
        print(
            f"collection of {args.target!r} failed with pytest exit code "
            f"{surface['exit_code']}; the measurement is unusable",
            file=sys.stderr,
        )
        return 1

    payload = json.dumps(surface, indent=2)
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
        print(
            f"{len(surface['active'])} of {len(surface['collected'])} collected tests "
            f"would run; wrote {args.out}"
        )
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
