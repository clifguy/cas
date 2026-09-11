"""Gate on the gate: prove the test-surface comparison has teeth.

``scripts/check_test_surface.py`` exists to catch a change that shrinks the
regression surface without producing a failing signal. A check like that is
easy to write in a way that never fires, and nothing about the live tree would
reveal it -- a comparison that reports nothing looks exactly like a repository
with no contraction in it. So every claim it makes is exercised here against
synthetic trees built for the purpose, with a control alongside each one
naming the weaker implementation that the claim rules out.

The two shapes the check was built for get a pair of tests each:

* A method that dedents out of its class body and stops being collected.
  The control is that the same edit also *adds* tests, so the collected count
  does not move -- an implementation comparing counts passes every other test
  in this file and fails only that one.
* A class newly gated behind an opt-in environment variable, which stays
  collectable and stops running. The control is that the collected sets are
  identical across the pair, so the only place the signal can come from is the
  skip-mark evaluation. A third test flips the variable on and requires the
  report to come back clean, which pins that evaluation from the other side:
  delete it and the first two fail, invert it and the third does.

Synthetic sources are kept as strings and written into ``tmp_path`` at run
time, the same device ``tests/test_collection_integrity.py`` uses: a tracked
file carrying a deliberately-dedented test class would be collected by this
suite and read as a real defect by that gate.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Final

import pytest

from scripts.check_test_surface import (
    KNOWN_TEST_REMOVALS,
    MIN_BASE_ACTIVE,
    Surface,
    _run_compare,
    classify,
    is_declared,
    leaf_key,
)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
CHECK_SCRIPT: Final[Path] = REPO_ROOT / "scripts" / "check_test_surface.py"

# The opt-in variable the synthetic gated class keys on. Named for this file so
# a real run of the suite can never happen to have it set.
FIXTURE_OPTIN: Final[str] = "SAGE_TEST_SURFACE_FIXTURE_OPTIN"


# ---------------------------------------------------------------------------
# Synthetic trees
#
# Each constant is one module's source. A "before" and an "after" constant
# together describe a change, and the tests below measure both and compare.
# ---------------------------------------------------------------------------

# Four methods in a class, all collected, all running.
_DEDENT_BEFORE: Final[str] = textwrap.dedent(
    """
    class TestStore:
        def test_a(self):
            assert True

        def test_b(self):
            assert True

        def test_c(self):
            assert True

        def test_d(self):
            assert True
    """
)

# The same class with a module-level def at column 0 after the second method.
# ``test_c`` and ``test_d`` now parse as nested functions and are never
# collected. Two module-level functions are added in the same edit, so the
# collected count is unchanged: four before, four after.
_DEDENT_AFTER: Final[str] = textwrap.dedent(
    """
    class TestStore:
        def test_a(self):
            assert True

        def test_b(self):
            assert True

    def test_stub_one():
        assert True

        def test_c(self):
            assert True

        def test_d(self):
            assert True

    def test_stub_two():
        assert True
    """
)

# Three tests in a class with no gate on it.
_GATED_BEFORE: Final[str] = textwrap.dedent(
    """
    class TestProvider:
        def test_one(self):
            assert True

        def test_two(self):
            assert True

        def test_three(self):
            assert True
    """
)

# The same three tests behind an opt-in gate. Still collected, identically;
# they run only where the variable is set.
_GATED_AFTER: Final[str] = textwrap.dedent(
    f"""
    import os

    import pytest

    _OPT_IN = os.environ.get({FIXTURE_OPTIN!r}) == "1"


    @pytest.mark.skipif(not _OPT_IN, reason="opt-in tier")
    class TestProvider:
        def test_one(self):
            assert True

        def test_two(self):
            assert True

        def test_three(self):
            assert True
    """
)

_MOVE_BEFORE_HOME: Final[str] = textwrap.dedent(
    """
    class TestThing:
        def test_travels(self):
            assert True

        def test_stays(self):
            assert True
    """
)

_MOVE_AFTER_HOME: Final[str] = textwrap.dedent(
    """
    class TestThing:
        def test_stays(self):
            assert True
    """
)

_MOVE_AFTER_DESTINATION: Final[str] = textwrap.dedent(
    """
    class TestThing:
        def test_travels(self):
            assert True
    """
)

# A second module carrying the same class and method name as the first. Method
# names repeat across parallel modules in any suite of size -- 77 leaf keys are
# shared across modules in this one -- so this is the ordinary case, not a
# contrived one, and it is what makes the relocation escape a population
# comparison rather than a presence check.
_NAMESAKE_TWIN: Final[str] = textwrap.dedent(
    """
    class TestThing:
        def test_travels(self):
            assert True
    """
)

# The same two tests, one of them renamed in place. Indistinguishable from a
# deletion plus an addition, and reported as a loss -- the control on the
# relocation escape.
_RENAME_AFTER: Final[str] = textwrap.dedent(
    """
    class TestThing:
        def test_travels_far(self):
            assert True

        def test_stays(self):
            assert True
    """
)

# Collected, never active: the base side of the vacuity-floor test.
_ALL_GATED: Final[str] = textwrap.dedent(
    """
    import pytest


    @pytest.mark.skipif(True, reason="never runs")
    class TestNothing:
        def test_one(self):
            assert True
    """
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tree(root: Path, name: str, modules: dict[str, str]) -> Path:
    """Materialize ``modules`` (filename -> source) as a directory under ``root``."""
    path = root / name
    path.mkdir(parents=True)
    for filename, source in modules.items():
        (path / filename).write_text(source, encoding="utf-8")
    return path


def _measure(tree: Path, env_extra: dict[str, str] | None = None) -> Surface:
    """Run the check's emit mode over ``tree`` and return the measurement.

    Invoked by absolute path from inside the tree rather than as
    ``-m scripts.check_test_surface`` from the repository root, because node
    ids are written relative to whatever pytest resolves as its root. Running
    from the repository would both apply this project's pytest settings to the
    synthetic tree and stamp each id with the tree's own directory name, so the
    two halves of a pair would share no ids at all. In real use the same rule
    applies: each side is measured from its own checkout root, so both describe
    the same repository-relative paths.
    """
    env = dict(os.environ)
    env.pop(FIXTURE_OPTIN, None)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.update(env_extra or {})
    out = tree.parent / f"{tree.name}.json"
    result = subprocess.run(
        [sys.executable, str(CHECK_SCRIPT), "--emit", "--target", ".", "--out", str(out)],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"emit failed:\n{result.stdout}\n{result.stderr}"
    return json.loads(out.read_text(encoding="utf-8"))


def _filler(count: int) -> list[str]:
    return [f"tests/pad/test_pad.py::test_pad_{index:05d}" for index in range(count)]


def _pad(surface: Surface, count: int = MIN_BASE_ACTIVE + 100, *, active: bool = True) -> Surface:
    """Add ``count`` filler ids to a measurement's collected set, and its active
    set unless ``active`` is false.

    The vacuity floor rejects a base side that measured almost nothing, which
    every synthetic tree here does. Padding both sides of a comparison with the
    same ids clears the floor without moving any category, so the exit-code
    tests exercise the real entry point rather than a relaxed copy of it.

    ``active=False`` pads collection alone, producing the shape the floor is
    actually about: a measurement that collected plenty and would run almost
    none of it.
    """
    filler = _filler(count)
    return {
        **surface,
        "collected": sorted([*surface["collected"], *filler]),
        "active": sorted([*surface["active"], *filler]) if active else sorted(surface["active"]),
    }


def _compare_via_cli(tmp_path: Path, base: Surface, head: Surface) -> int:
    """Run the comparison entry point over two measurements, padded to clear the floor."""
    base_path = tmp_path / "base.json"
    head_path = tmp_path / "head.json"
    base_path.write_text(json.dumps(_pad(base)), encoding="utf-8")
    head_path.write_text(json.dumps(_pad(head)), encoding="utf-8")
    return _run_compare(str(base_path), str(head_path))


# ---------------------------------------------------------------------------
# A test that stops being collected
# ---------------------------------------------------------------------------


def test_dedented_class_body_reports_the_lost_methods(tmp_path: Path) -> None:
    """The two methods reparented by a stray module-level def are named.

    They are reported as lost and NOT as deactivated: nothing collects them any
    more, so the remedy is to restore the class body rather than to remove a
    gate. An implementation that copied ``lost`` into ``deactivated`` would
    satisfy every other test here, so the empty assertion is load-bearing.
    """
    before = _measure(_tree(tmp_path, "before", {"test_sample.py": _DEDENT_BEFORE}))
    after = _measure(_tree(tmp_path, "after", {"test_sample.py": _DEDENT_AFTER}))

    verdict = classify(before, after, removals={})

    assert verdict["lost"] == [
        "test_sample.py::TestStore::test_c",
        "test_sample.py::TestStore::test_d",
    ]
    assert verdict["deactivated"] == []
    assert verdict["moved"] == []


def test_a_same_count_change_still_reports_the_loss(tmp_path: Path) -> None:
    """The edit adds as many tests as it disables, so the count does not move.

    An implementation comparing counts rather than sets reports nothing here.
    """
    before = _measure(_tree(tmp_path, "before", {"test_sample.py": _DEDENT_BEFORE}))
    after = _measure(_tree(tmp_path, "after", {"test_sample.py": _DEDENT_AFTER}))

    assert len(before["active"]) == len(after["active"]), (
        "the fixture pair must hold the active count flat for this control to bite"
    )
    assert classify(before, after, removals={})["lost"]


# ---------------------------------------------------------------------------
# A test that stops being selected
# ---------------------------------------------------------------------------


def test_a_newly_gated_class_reports_as_deactivated(tmp_path: Path) -> None:
    """A class newly behind an unset opt-in is reported, and reported as gated."""
    before = _measure(_tree(tmp_path, "before", {"test_sample.py": _GATED_BEFORE}))
    after = _measure(_tree(tmp_path, "after", {"test_sample.py": _GATED_AFTER}))

    verdict = classify(before, after, removals={})

    expected = [
        "test_sample.py::TestProvider::test_one",
        "test_sample.py::TestProvider::test_three",
        "test_sample.py::TestProvider::test_two",
    ]
    assert verdict["lost"] == expected
    assert verdict["deactivated"] == expected


def test_a_newly_gated_class_is_invisible_to_collection_alone(tmp_path: Path) -> None:
    """Collection cannot see the gate, so the signal must come from skip evaluation.

    This is the control on the test above: with the collected sets provably
    identical, an implementation that only counts or compares collection has
    nothing to report.
    """
    before = _measure(_tree(tmp_path, "before", {"test_sample.py": _GATED_BEFORE}))
    after = _measure(_tree(tmp_path, "after", {"test_sample.py": _GATED_AFTER}))

    assert before["collected"] == after["collected"]
    assert before["active"] != after["active"]


def test_the_gate_clears_when_the_environment_supplies_the_opt_in(tmp_path: Path) -> None:
    """Measured where the opt-in is set, the same pair shows no contraction.

    The measurement is a property of the tree *and* the environment, not of the
    tree alone. An implementation that inverted or ignored the skip evaluation
    would report a loss here.
    """
    opted_in = {FIXTURE_OPTIN: "1"}
    before = _measure(_tree(tmp_path, "before", {"test_sample.py": _GATED_BEFORE}), opted_in)
    after = _measure(_tree(tmp_path, "after", {"test_sample.py": _GATED_AFTER}), opted_in)

    verdict = classify(before, after, removals={})

    assert verdict["lost"] == []
    assert verdict["deactivated"] == []


# ---------------------------------------------------------------------------
# Relocation, and its control
# ---------------------------------------------------------------------------


def test_a_moved_test_is_not_reported_as_lost(tmp_path: Path) -> None:
    """A test that changed modules still runs, and is reported as moved."""
    before = _measure(_tree(tmp_path, "before", {"test_home.py": _MOVE_BEFORE_HOME}))
    after = _measure(
        _tree(
            tmp_path,
            "after",
            {
                "test_home.py": _MOVE_AFTER_HOME,
                "test_elsewhere.py": _MOVE_AFTER_DESTINATION,
            },
        )
    )

    verdict = classify(before, after, removals={})

    assert verdict["lost"] == []
    assert verdict["moved"] == ["test_home.py::TestThing::test_travels"]

    exit_code = _compare_via_cli(tmp_path, before, after)
    assert exit_code == 0


def test_a_deletion_beside_a_namesake_is_reported_as_lost(tmp_path: Path) -> None:
    """A module can not hide its deletion behind a namesake in another module.

    Two modules carry the same ``Class::method``; one is deleted. The leaf key
    still exists in the head active set, so an escape asking only whether the
    name survives reports a move and exits clean -- which, measured against the
    live tree, would let a whole module's worth of tests disappear unseen. The
    population comparison sees the count drop from two to one.
    """
    before = _measure(
        _tree(
            tmp_path,
            "before",
            {"test_home.py": _MOVE_AFTER_DESTINATION, "test_twin.py": _NAMESAKE_TWIN},
        )
    )
    after = _measure(_tree(tmp_path, "after", {"test_twin.py": _NAMESAKE_TWIN}))

    assert leaf_key("test_home.py::TestThing::test_travels") in {
        leaf_key(node_id) for node_id in after["active"]
    }, "the surviving namesake must keep the leaf key present, or this control proves nothing"

    verdict = classify(before, after, removals={})

    assert verdict["lost"] == ["test_home.py::TestThing::test_travels"]
    assert verdict["moved"] == []

    exit_code = _compare_via_cli(tmp_path, before, after)
    assert exit_code == 1


def test_a_renamed_test_is_reported_as_lost(tmp_path: Path) -> None:
    """Renaming in place is a loss, not a move.

    Without this control, an implementation that reported nothing at all would
    satisfy the relocation test above.
    """
    before = _measure(_tree(tmp_path, "before", {"test_home.py": _MOVE_BEFORE_HOME}))
    after = _measure(_tree(tmp_path, "after", {"test_home.py": _RENAME_AFTER}))

    verdict = classify(before, after, removals={})

    assert verdict["lost"] == ["test_home.py::TestThing::test_travels"]
    assert verdict["deactivated"] == []
    assert verdict["moved"] == []

    exit_code = _compare_via_cli(tmp_path, before, after)
    assert exit_code == 1


# ---------------------------------------------------------------------------
# Declared removals
# ---------------------------------------------------------------------------


def test_a_declared_removal_clears_and_a_near_miss_does_not(tmp_path: Path) -> None:
    """An exact entry clears the removal; a near miss on either side does not.

    Both near misses matter and they rule out different mistakes. An entry one
    character too long is the ordinary typo. An entry one character too short
    is the one that would be accepted by matching on substring or on a bare
    prefix, which is how a shortened entry ends up quietly covering tests
    nobody declared.
    """
    before = _measure(_tree(tmp_path, "before", {"test_home.py": _MOVE_BEFORE_HOME}))
    after = _measure(_tree(tmp_path, "after", {"test_home.py": _RENAME_AFTER}))
    node_id = "test_home.py::TestThing::test_travels"

    cleared = classify(before, after, removals={node_id: "renamed for clarity"})
    assert cleared["lost"] == []
    assert cleared["declared"] == [node_id]

    for near_miss in (node_id + "x", node_id[:-1]):
        verdict = classify(before, after, removals={near_miss: "not this one"})
        assert verdict["lost"] == [node_id], f"{near_miss!r} must not clear {node_id!r}"
        assert verdict["declared"] == []


def test_a_prefix_entry_covers_a_deleted_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One module-prefix entry declares every test in a file that went away.

    The deleted module's method name also survives in ``test_home.py``, so the
    relocation escape does not clear this id and the declaration has to. The
    entry is installed in the real allowlist for the exit-code arm, because
    that path reads the module-level table rather than a passed-in one -- and
    with a passed-in one it would report exit 0 whether the declaration worked
    or not.
    """
    entry = "test_gone.py"
    reason = "the behaviour it covered was removed"
    before = _measure(
        _tree(
            tmp_path,
            "before",
            {"test_home.py": _MOVE_BEFORE_HOME, "test_gone.py": _MOVE_AFTER_DESTINATION},
        )
    )
    after = _measure(_tree(tmp_path, "after", {"test_home.py": _MOVE_BEFORE_HOME}))

    assert classify(before, after, removals={})["lost"] == [
        "test_gone.py::TestThing::test_travels"
    ], "without the declaration this must be a loss, or the arm below proves nothing"

    verdict = classify(before, after, removals={entry: reason})
    assert verdict["lost"] == []
    assert verdict["declared"] == ["test_gone.py::TestThing::test_travels"]

    monkeypatch.setitem(KNOWN_TEST_REMOVALS, entry, reason)
    assert _compare_via_cli(tmp_path, before, after) == 0


def test_prefix_matching_requires_a_boundary() -> None:
    """A prefix entry matches only at a module or class boundary."""
    node_id = "tests/sage/test_adapters.py::TestStore::test_case"

    assert is_declared(node_id, {"tests/sage/test_adapters.py": "gone"})
    assert is_declared(node_id, {"tests/sage/test_adapters.py::TestStore::": "gone"})
    assert not is_declared(node_id, {"tests/sage/test_ad": "gone"})
    assert not is_declared(node_id, {"tests/sage/test_adapters.py::TestStore::test_c": "gone"})


# ---------------------------------------------------------------------------
# Vacuity
# ---------------------------------------------------------------------------


def test_a_base_measurement_that_would_run_nothing_fails(tmp_path: Path) -> None:
    """A base side that would run almost nothing is a broken comparison.

    The floor reads the *active* set, and this fixture is built so that matters:
    collection is padded well past the floor while the active set stays empty,
    which is what a misconfigured environment produces. A floor reading the
    collected set instead would let this through and then report every test in
    the repository as newly lost.
    """
    before = _measure(_tree(tmp_path, "before", {"test_sample.py": _ALL_GATED}))
    after = _measure(_tree(tmp_path, "after", {"test_sample.py": _ALL_GATED}))
    before = _pad(before, active=False)

    assert len(before["collected"]) > MIN_BASE_ACTIVE, (
        "collection must clear the floor, so only the active set can trip it"
    )
    assert before["active"] == []

    base_path = tmp_path / "base.json"
    head_path = tmp_path / "head.json"
    base_path.write_text(json.dumps(before), encoding="utf-8")
    head_path.write_text(json.dumps(after), encoding="utf-8")

    assert _run_compare(str(base_path), str(head_path)) == 1


# ---------------------------------------------------------------------------
# Unit-level detail and standing hygiene
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("node_id", "expected"),
    [
        ("tests/sage/test_x.py::TestThing::test_case", "TestThing::test_case"),
        ("tests/sage/test_x.py::test_case[a-b]", "test_case[a-b]"),
        ("tests/a.py/test_x.py::test_case", "test_case"),
        ("test_case", "test_case"),
    ],
)
def test_leaf_key_drops_everything_up_to_the_module(node_id: str, expected: str) -> None:
    """The relocation escape keys on the part of a node id a move preserves."""
    assert leaf_key(node_id) == expected


def test_every_declared_removal_carries_a_reason() -> None:
    """A declared removal without a stated reason is an unreviewable waiver."""
    missing = [entry for entry, reason in KNOWN_TEST_REMOVALS.items() if not reason.strip()]
    assert missing == [], f"declared removals with no reason: {missing}"


def test_the_skip_evaluation_helper_is_available() -> None:
    """The mark evaluation this check depends on must still be importable.

    It lives on a private pytest module. Without it the check silently
    degrades to comparing collection, which cannot see a test that stopped
    running in this environment while remaining collectable -- one of the two
    shapes it exists to catch. A pytest upgrade that moves it should fail here,
    pointedly, rather than leave the gate half-blind.
    """
    from _pytest.skipping import evaluate_skip_marks

    assert callable(evaluate_skip_marks)
