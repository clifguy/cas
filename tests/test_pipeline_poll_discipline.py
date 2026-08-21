"""Pipeline-poll discipline gate.

A test that waits on a document's background ingestion pipeline and then acts
on that document must wait for a *terminal* ``pipeline_status``. The
abstraction queue holds a per-document in-flight claim for the whole span from
dispatch through the terminal status write, and every operator-facing entry
point (re-abstraction, pipeline recompute) rejects a document whose claim is
held. A poll that accepts a non-terminal state therefore proceeds from exactly
the condition that makes its own next call fail, and passes only when the poll
happens to observe a terminal state first.

The failure is invisible in the ordinary case: under a fast provider the
pipeline usually reaches terminal before the poll's first read, so the test is
green almost always and red under load. That produces the signature symptom of
the same commit passing on one CI trigger and failing on another.

This gate is the substrate-level backstop. The defect was diagnosed and fixed
once at a single site, and the fix was not swept across the neighbouring tests,
so the identical race survived one function away and later propagated to two
further sites by copy. A deterministic AST check over every tracked test module
is what makes a third occurrence impossible to land silently.

Detection is limited to accept-sets written inline inside a loop body: a
membership test whose left side mentions ``pipeline_status`` and whose right
side spells the states out as string literals or ``PipelineStatus`` members.
The loop restriction is what distinguishes a poll from a one-shot assertion on
an observed status, which contends with nothing and is free to name any state.
An accept-set behind a named constant is likewise opaque to the walk, which is
intentional -- a shared terminal-only poll helper is the sanctioned way to
write one of these, and it is that helper, not each call site, that carries the
correctness argument.

Allowlist convention follows ``ORPHANED_TEST_ALLOWLIST`` in
``tests/test_collection_integrity.py`` and the allowlists in
``tests/test_public_posture.py``: empty by default, every entry carrying a
one-line rationale.

Anti-coincidental coverage: ``test_detector_flags_nonterminal_accept_set`` and
``test_detector_ignores_terminal_only_accept_set`` exercise the walk against
synthetic source strings, proving the detector has teeth independent of
whatever the live tree happens to contain.
"""

import ast
import subprocess
import textwrap
from pathlib import Path
from typing import Final

import pytest

from sage.models.enums import PipelineStatus

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

# Terminal states: no further abstraction begins from any of these, so the
# in-flight claim is released (or about to be) and the document is safe to act
# on once the claim itself is observed clear.
TERMINAL_STATES: Final[frozenset[str]] = frozenset(
    {
        PipelineStatus.ABSTRACTION_COMPLETE.value,
        PipelineStatus.ABSTRACTION_SKIPPED.value,
        PipelineStatus.FAILED.value,
    }
)

# Everything else. Each of these can still be followed by an abstraction start,
# leaving a window between the poll's observation and the caller's next call.
NON_TERMINAL_STATES: Final[frozenset[str]] = (
    frozenset(status.value for status in PipelineStatus) - TERMINAL_STATES
)

# Enum member name -> wire value, so ``PipelineStatus.INDEXING_COMPLETE`` in an
# accept-set is recognized as readily as the ``"indexing_complete"`` literal.
_MEMBER_TO_VALUE: Final[dict[str, str]] = {status.name: status.value for status in PipelineStatus}

# Maximum number of violations to enumerate in a single pytest.fail message.
_MAX_REPORTED: Final[int] = 30


# ---------------------------------------------------------------------------
# Allowlist
#
# path (relative to repo root) → line numbers where a non-terminal accept-set
# is intentional. Empty by default; a poll that acts on the polled document has
# no legitimate reason to proceed from a non-terminal state. Every entry added
# later requires a 1-line rationale alongside it.
# ---------------------------------------------------------------------------

NONTERMINAL_POLL_ALLOWLIST: Final[dict[str, list[int]]] = {}


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
            and path.name.startswith("test_")
        ):
            modules.append(path)
    return modules


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


def _accept_set_states(comparator: ast.expr) -> set[str]:
    """Return the pipeline-status values an accept-set expression spells out.

    Recognizes bare string literals and ``PipelineStatus.<MEMBER>`` attribute
    access anywhere inside the expression, so set / tuple / list / frozenset
    literals are all covered. A name that resolves elsewhere (a module-level
    constant) contributes nothing, which is what makes an indirected
    accept-set invisible to this walk.
    """
    states: set[str] = set()
    for node in ast.walk(comparator):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            states.add(node.value)
        elif isinstance(node, ast.Attribute) and node.attr in _MEMBER_TO_VALUE:
            value = getattr(node.value, "id", None)
            if value == PipelineStatus.__name__:
                states.add(_MEMBER_TO_VALUE[node.attr])
    return states


def _nonterminal_accept_sets(tree: ast.AST) -> list[tuple[int, list[str]]]:
    """Return ``(lineno, sorted non-terminal states)`` for every membership
    test on ``pipeline_status``, inside a loop body, whose inline accept-set
    names a non-terminal state.

    The left side is matched textually so every spelling the suite uses --
    ``doc["pipeline_status"]``, ``doc.get("pipeline_status")``,
    ``doc.pipeline_status`` -- is covered by one rule.

    The loop-body restriction is what separates a poll from an assertion. A
    poll is a loop by construction; a bare ``assert doc.pipeline_status in
    (...)`` on a directly-inserted document observes a state rather than
    waiting to act on it, and no claim is contended. Restricting to loop
    bodies keeps the gate's subject exactly the shape it is named for, at the
    cost of missing a poll expressed without a loop.
    """
    found: list[tuple[int, list[str]]] = []

    def walk(node: ast.AST, in_loop: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if in_loop and isinstance(child, ast.Compare):
                _collect(child)
            child_in_loop = in_loop or isinstance(child, (ast.For, ast.AsyncFor, ast.While))
            walk(child, child_in_loop)

    def _collect(node: ast.Compare) -> None:
        if not any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            return
        if "pipeline_status" not in ast.unparse(node.left):
            return
        offending: set[str] = set()
        for comparator in node.comparators:
            offending |= _accept_set_states(comparator) & NON_TERMINAL_STATES
        if offending:
            found.append((node.lineno, sorted(offending)))

    walk(tree, False)
    return found


def _format_violations(violations: list[tuple[str, int, str]]) -> str:
    """Render a violation list as a pytest.fail-friendly message."""
    head = violations[:_MAX_REPORTED]
    body = "\n".join(f"  {path}:{line} → {detail}" for path, line, detail in head)
    overflow = len(violations) - len(head)
    tail = f"\n  ... and {overflow} more" if overflow > 0 else ""
    return (
        f"Non-terminal pipeline_status accept-sets ({len(violations)} found):\n"
        f"{body}{tail}\n"
        "A poll that proceeds from a non-terminal state races the abstraction "
        "queue's in-flight claim: the next call on that document is rejected "
        "with a 409 whenever the poll observes the non-terminal state first. "
        f"Accept only terminal states ({', '.join(sorted(TERMINAL_STATES))}), "
        "and prefer a shared poll helper that also waits for the claim to "
        "clear."
    )


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_no_nonterminal_pipeline_status_accept_sets() -> None:
    """No tracked test module may poll ``pipeline_status`` against an inline
    accept-set that admits a non-terminal state.
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
        allowed = set(NONTERMINAL_POLL_ALLOWLIST.get(rel, []))
        for lineno, states in _nonterminal_accept_sets(tree):
            if lineno in allowed:
                continue
            violations.append((rel, lineno, ", ".join(states)))

    if violations:
        pytest.fail(_format_violations(violations))


# ---------------------------------------------------------------------------
# Anti-coincidental detector self-tests
# ---------------------------------------------------------------------------

# The defective shape: a bounded poll that breaks out on a state from which the
# pipeline can still advance. Kept as a string so the gate's own walk over this
# file does not see it as a real violation.
_SYNTHETIC_NONTERMINAL_SOURCE: Final[str] = textwrap.dedent(
    """
    for _ in range(200):
        doc = _parse(await get_document("v", doc_id))
        if doc.get("pipeline_status") in {
            "indexing_complete",
            "abstraction_complete",
        }:
            break
    """
)

# The enum spelling of the same defect, which a literal-only detector misses.
_SYNTHETIC_ENUM_MEMBER_SOURCE: Final[str] = textwrap.dedent(
    """
    while not done:
        if doc.pipeline_status in (
            PipelineStatus.ABSTRACTION_IN_PROGRESS,
            PipelineStatus.FAILED,
        ):
            break
    """
)

# Terminal-only accept-sets in both spellings, plus a negated membership test.
_SYNTHETIC_TERMINAL_SOURCE: Final[str] = textwrap.dedent(
    """
    for _ in range(200):
        if doc.pipeline_status in {
            PipelineStatus.ABSTRACTION_COMPLETE,
            PipelineStatus.FAILED,
        }:
            break

        if doc["pipeline_status"] not in ("abstraction_skipped", "failed"):
            keep_waiting()
    """
)

# An accept-set behind a named constant: the detector's documented blind spot.
_SYNTHETIC_INDIRECTED_SOURCE: Final[str] = textwrap.dedent(
    """
    for _ in range(200):
        if doc.get("pipeline_status") in _TERMINAL_PIPELINE_STATES:
            break
    """
)

# A membership test that has nothing to do with the pipeline but names one of
# the same string values -- must not be swept up.
_SYNTHETIC_UNRELATED_SOURCE: Final[str] = textwrap.dedent(
    """
    for event in events:
        if event.stage in {"indexing_complete", "abstraction_in_progress"}:
            record(event)
    """
)

# A one-shot assertion on a document that was inserted directly, with no
# pipeline running and no subsequent call to contend with. Observing a
# non-terminal state here is correct, and the gate must leave it alone.
_SYNTHETIC_ASSERTION_SOURCE: Final[str] = textwrap.dedent(
    """
    def test_status_after_insert():
        assert fetched.pipeline_status in (
            PipelineStatus.PROJECTION_COMPLETE,
            PipelineStatus.INDEXING_IN_PROGRESS,
        )
    """
)


def test_detector_flags_nonterminal_accept_set() -> None:
    """A poll that admits ``indexing_complete`` is reported, and only the
    non-terminal member of the accept-set is named in the finding.
    """
    found = _nonterminal_accept_sets(ast.parse(_SYNTHETIC_NONTERMINAL_SOURCE))
    assert [states for _, states in found] == [["indexing_complete"]]


def test_detector_flags_nonterminal_enum_member() -> None:
    """The enum spelling ``PipelineStatus.ABSTRACTION_IN_PROGRESS`` is caught
    as readily as the bare string literal.
    """
    found = _nonterminal_accept_sets(ast.parse(_SYNTHETIC_ENUM_MEMBER_SOURCE))
    assert [states for _, states in found] == [["abstraction_in_progress"]]


def test_detector_ignores_terminal_only_accept_set() -> None:
    """Terminal-only accept-sets -- in either spelling, and under a negated
    membership test -- are correct and must not be flagged.
    """
    assert _nonterminal_accept_sets(ast.parse(_SYNTHETIC_TERMINAL_SOURCE)) == []


def test_detector_ignores_indirected_accept_set() -> None:
    """An accept-set behind a named constant is invisible to the walk.

    This pins the documented blind spot: the shared poll helper is the
    sanctioned indirection, and it carries the correctness argument that each
    call site no longer has to restate.
    """
    assert _nonterminal_accept_sets(ast.parse(_SYNTHETIC_INDIRECTED_SOURCE)) == []


def test_detector_ignores_membership_tests_on_other_subjects() -> None:
    """A membership test on something other than ``pipeline_status`` is out of
    scope even when it names the same string values.
    """
    assert _nonterminal_accept_sets(ast.parse(_SYNTHETIC_UNRELATED_SOURCE)) == []


def test_detector_ignores_one_shot_assertions_outside_a_loop() -> None:
    """A bare assertion on an observed status is not a poll.

    Nothing is being waited for and no claim is contended, so admitting a
    non-terminal state there is correct. This pins the loop-body restriction
    so a later widening of the detector cannot quietly start reporting
    assertions as poll defects.
    """
    assert _nonterminal_accept_sets(ast.parse(_SYNTHETIC_ASSERTION_SOURCE)) == []
