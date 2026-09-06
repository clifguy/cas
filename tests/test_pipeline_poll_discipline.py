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

A terminal status is necessary but not sufficient, so this module carries a
second detector for the other half of the same defect. The worker releases the
in-flight claim in its ``finally``, which runs *after* the terminal status
write -- on the completion path it refreshes the document's synthetic header
chunk in between, so the window spans a content-store write rather than a
scheduling hairline. A poll that observes the terminal status inside that
window proceeds to a call the guard still rejects. The claim-arm walk
therefore reports any function that polls ``pipeline_status`` in an inline
loop and then reaches an entry point that rejects a held claim -- by calling
the service method or the tool that wraps it, or by posting to the route that
wraps it, since a test contending over HTTP names the route rather than the
function. Its blind spot is the same one by the same design: a wait expressed
through a shared claim-aware helper is invisible, because that helper is where
the argument belongs.

Both blind spots presume the helper carries the argument, and a helper that
does not is invisible to both arms while being exactly the defect. A third
detector closes that from the other side: any function polling
``pipeline_status`` in an inline loop must consult the claim registry as well,
or delegate to the shared helper in ``tests/helpers/pipeline_wait.py`` that
does. The first two arms may then keep their indirection blind spot, because
the indirection is checked rather than assumed -- and "which modules define
their own poll" stops being an enumeration somebody has to remember to repeat.

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

# Entry points that reject a document whose abstraction claim is held: the
# service methods and the tool names that wrap them. A poll placed to gate one
# of these has to wait for the claim to clear, not merely for a terminal
# status. Matched on the bare attribute / name, so ``service.reabstract(...)``
# and a directly-imported ``recompute_abstract(...)`` are both recognized.
CONTENDING_CALLS: Final[frozenset[str]] = frozenset(
    {
        "reabstract",
        "recompute_pipeline",
        "recompute_abstract",
    }
)

# The same guarded work reached over HTTP. A test that contends through the API
# surface names the route, not the function, so the name walk alone cannot see
# it. Only routes that actually exist are listed; a new contending route has to
# be added here, and the walk stays blind to it until it is.
CONTENDING_ROUTE_SEGMENTS: Final[tuple[str, ...]] = ("/reabstract",)

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
# Claim-arm allowlist
#
# path (relative to repo root) → line numbers of the *first* inline
# pipeline_status poll in a function that later contends for the claim, where
# that shape is nonetheless correct. Empty by default; the sanctioned way to
# write one of these is a shared poll helper that waits on the claim as well as
# the status. Every entry added later requires a 1-line rationale alongside it.
# ---------------------------------------------------------------------------

CLAIM_ARM_POLL_ALLOWLIST: Final[dict[str, list[int]]] = {}


# ---------------------------------------------------------------------------
# Status-only helper allowlist
#
# path (relative to repo root) → names of the functions whose inline
# pipeline_status loop consults no claim state, where that is nonetheless
# correct. Empty by default; a wait that does not check the claim is not a wait
# a caller can act on, and the sanctioned form is to delegate to
# tests/helpers/pipeline_wait.py. Every entry added later requires a 1-line
# rationale alongside it.
#
# Keyed by function name rather than by line number, unlike the two allowlists
# above. A line number is a coordinate that any edit or reformat above it
# invalidates -- the failure is loud rather than silent, since a staled anchor
# reds this gate, but it is still a red that says nothing about the code it
# names. The function name is stable under formatting and is what a reader has
# to go and look at.
# ---------------------------------------------------------------------------

STATUS_ONLY_POLL_ALLOWLIST: Final[dict[str, list[str]]] = {
    # Waits for a document to *enter* indexing_in_progress, to show the stage
    # observably re-ran; it follows its own recompute_pipeline call and gates
    # nothing, and the settle-wait on the next line is delegated.
    "tests/sage/test_ingestion.py": ["test_recompute_pipeline_idempotent_on_terminal_document"],
}


# Evidence, in a polling function's own body, that the wait consults the claim
# as well as the status: the registry itself, or one of the shared helper's
# entry points, which check it unconditionally. Matched as identifiers by
# ``_consults_claim`` rather than as substrings of the unparsed function -- a
# false exemption here is silent, and is exactly the shape this arm exists to
# report.
CLAIM_AWARE_MARKERS: Final[tuple[str, ...]] = (
    "_inflight",
    "await_pipeline_idle",
    "await_tool_idle",
)


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


def _called_name(func: ast.expr) -> str | None:
    """The bare name a call resolves to.

    ``service.reabstract(...)`` and ``reabstract(...)`` both yield
    ``"reabstract"``; anything more elaborate yields None rather than a guess.
    """
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _contended_target(call: ast.Call) -> str | None:
    """What a call contends for, or None if it contends for nothing.

    Two spellings reach the same guarded work: calling the service method or
    the tool that wraps it, and posting to the route that wraps it. The second
    carries the route as a string -- plain or f-string -- inside the call, so
    the literal is what identifies it.
    """
    name = _called_name(call.func)
    if name in CONTENDING_CALLS:
        return name
    for node in ast.walk(call):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        for segment in CONTENDING_ROUTE_SEGMENTS:
            if node.value.endswith(segment):
                return segment
    return None


def _poll_then_contend(tree: ast.AST) -> list[tuple[int, str, int]]:
    """Return ``(poll lineno, contending call name, call lineno)`` for every
    function that polls ``pipeline_status`` in an inline loop and then calls an
    entry point that rejects a held claim.

    Order is the whole rule. A contending call placed *before* the poll is
    correct and common -- the call dispatches background work and the poll
    drains it, contending with nothing. Only a poll positioned to gate a later
    call is making the promise this walk checks, and a terminal status alone
    does not keep it.

    The poll is anchored at the first inline loop in the function, which is the
    line an allowlist entry names and the line a reader has to edit.
    """
    findings: list[tuple[int, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        loops = [
            inner
            for inner in ast.walk(node)
            if isinstance(inner, (ast.For, ast.AsyncFor, ast.While))
            and "pipeline_status" in ast.unparse(inner)
        ]
        if not loops:
            continue
        anchor = min(loop.lineno for loop in loops)
        last_poll_line = max(loop.end_lineno or loop.lineno for loop in loops)
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call) or inner.lineno <= last_poll_line:
                continue
            contended = _contended_target(inner)
            if contended is not None:
                findings.append((anchor, contended, inner.lineno))
    return findings


def _format_claim_arm_violations(violations: list[tuple[str, int, str, int]]) -> str:
    """Render a claim-arm violation list as a pytest.fail-friendly message."""
    head = violations[:_MAX_REPORTED]
    body = "\n".join(
        f"  {path}:{poll_line} → gates {call} at line {call_line}"
        for path, poll_line, call, call_line in head
    )
    overflow = len(violations) - len(head)
    tail = f"\n  ... and {overflow} more" if overflow > 0 else ""
    return (
        f"Inline pipeline_status polls gating a claim-contending call "
        f"({len(violations)} found):\n{body}{tail}\n"
        "The abstraction queue releases a document's in-flight claim after the "
        "terminal status write, not with it, so a poll that waits only for the "
        "status can return while the claim is still held and the call it gates "
        "is rejected. Wait on both through a shared claim-aware poll helper."
    )


def _sleeps(loop: ast.AST) -> bool:
    """Whether a loop yields between iterations.

    A wait sleeps; a loop that enumerates or asserts does not. Matched on the
    bare call name, so ``asyncio.sleep``, ``time.sleep`` and a directly
    imported ``sleep`` all count.
    """
    return any(
        isinstance(node, ast.Call) and _called_name(node.func) == "sleep" for node in ast.walk(loop)
    )


def _status_only_poll_helpers(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(poll lineno, function name)`` for every function that polls
    ``pipeline_status`` in an inline loop without consulting the claim.

    Each loop is attributed to its *innermost* enclosing function, so a helper
    nested inside a test is reported once, under its own name, rather than
    twice under both. A module-level loop belongs to no function and is out of
    scope: this walk's subject is the reusable wait, and the two arms above
    already cover a poll written inline at its call site.

    Delegation is the sanctioned form and is invisible here by construction: a
    function that calls the shared helper has no loop of its own, so there is
    nothing for this walk to anchor on.

    A loop qualifies only if it *sleeps*. That is what separates a wait from
    the far more common loop that merely mentions ``pipeline_status`` while
    seeding documents, asserting over a result set, or walking source in
    another gate. Those yield to nothing and race nothing, and reporting them
    would bury the findings that matter.
    """
    findings: list[tuple[int, ast.FunctionDef | ast.AsyncFunctionDef]] = []

    def visit(node: ast.AST, func: ast.FunctionDef | ast.AsyncFunctionDef | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child)
                continue
            if (
                func is not None
                and isinstance(child, (ast.For, ast.AsyncFor, ast.While))
                and "pipeline_status" in ast.unparse(child)
                and _sleeps(child)
            ):
                findings.append((child.lineno, func))
            visit(child, func)

    visit(tree, None)

    # One entry per function -- keyed by the function object, not its name, so
    # two same-named functions in one module are judged separately. The first
    # loop is the line a reader has to go and look at.
    seen: set[int] = set()
    kept: list[tuple[int, str]] = []
    for lineno, func in sorted(findings, key=lambda entry: entry[0]):
        if id(func) in seen or _consults_claim(func):
            continue
        seen.add(id(func))
        kept.append((lineno, func.name))
    return kept


def _consults_claim(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether the function's own body consults the in-flight claim.

    Matched on identifiers -- a name or an attribute, which between them cover
    the function half of a call -- rather than on the unparsed text of the
    function. Text matching read a marker out of two places it does not belong:

    * a **docstring**, which is prose *about* the wait rather than a check the
      wait performs -- and "deliberately does not consult ``_inflight``" is a
      natural sentence for exactly the helper this walk exists to report;
    * a **nested definition**, whose body runs in its own scope and is
      attributed to that definition by the walk above, so a nested delegating
      helper would exempt the enclosing function's own status-only loop.

    Both were reachable and neither was hypothetical. Note which change closes
    which: the nested-definition case is excluded *here*, by the descent
    stopping at a nested scope, while the docstring case is excluded by the
    match being over identifiers at all -- a docstring is a string constant,
    which is neither a name nor an attribute and has no children to descend
    into. There is deliberately no separate docstring skip: one would pin
    nothing, and would leave a later reader believing prose is excluded by a
    guard rather than by the shape of the match. A scan widened to read string
    content would reopen the case, and should reopen it visibly.

    Scope is the whole body rather than the polling loop's own predicate, so a
    marker anywhere in the function exempts every loop in it -- including one
    appearing only in a timeout diagnostic. That is a known limit rather than
    an oversight: narrowing to the predicate has to keep admitting the
    sanctioned shape, which binds the registry to a local name *before* its
    loop and tests that name inside it.
    """

    def scan(node: ast.AST) -> bool:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return False
        if isinstance(node, ast.Attribute) and node.attr in CLAIM_AWARE_MARKERS:
            return True
        if isinstance(node, ast.Name) and node.id in CLAIM_AWARE_MARKERS:
            return True
        return any(scan(child) for child in ast.iter_child_nodes(node))

    return any(scan(stmt) for stmt in func.body)


def _format_helper_violations(violations: list[tuple[str, int, str]]) -> str:
    """Render a status-only-helper violation list as a pytest.fail message."""
    head = violations[:_MAX_REPORTED]
    body = "\n".join(f"  {path}:{line} → in {name}()" for path, line, name in head)
    overflow = len(violations) - len(head)
    tail = f"\n  ... and {overflow} more" if overflow > 0 else ""
    return (
        f"Pipeline polls that never check the in-flight claim "
        f"({len(violations)} found):\n{body}{tail}\n"
        "A terminal pipeline_status is necessary but not sufficient: the "
        "abstraction queue releases the claim after the terminal status write, "
        "so a wait keyed on status alone can hand its caller a document the "
        "next call rejects. Delegate to await_pipeline_idle / await_tool_idle "
        "in tests/helpers/pipeline_wait.py, which check both."
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


def test_no_poll_then_contend_without_claim_arm() -> None:
    """No tracked test module may gate a claim-contending call on an inline
    ``pipeline_status`` poll.
    """
    violations: list[tuple[str, int, str, int]] = []
    for path in _tracked_test_modules():
        rel = str(path.relative_to(REPO_ROOT))
        try:
            tree = ast.parse(path.read_bytes(), filename=str(path))
        except SyntaxError:
            # A syntactically broken test module fails its own collection
            # loudly; not this gate's concern.
            continue
        allowed = set(CLAIM_ARM_POLL_ALLOWLIST.get(rel, []))
        for poll_line, call, call_line in _poll_then_contend(tree):
            if poll_line in allowed:
                continue
            violations.append((rel, poll_line, call, call_line))

    if violations:
        pytest.fail(_format_claim_arm_violations(violations))


def test_no_status_only_pipeline_poll_helpers() -> None:
    """No tracked test module may define a ``pipeline_status`` poll that does
    not also wait for the in-flight claim to clear.
    """
    violations: list[tuple[str, int, str]] = []
    for path in _tracked_test_modules():
        rel = str(path.relative_to(REPO_ROOT))
        try:
            tree = ast.parse(path.read_bytes(), filename=str(path))
        except SyntaxError:
            # A syntactically broken test module fails its own collection
            # loudly; not this gate's concern.
            continue
        allowed = set(STATUS_ONLY_POLL_ALLOWLIST.get(rel, []))
        for lineno, name in _status_only_poll_helpers(tree):
            if name in allowed:
                continue
            violations.append((rel, lineno, name))

    if violations:
        pytest.fail(_format_helper_violations(violations))


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


# ---------------------------------------------------------------------------
# Claim-arm detector self-tests
#
# Held as source strings for the same reason as the status-arm synthetics: the
# live walk over this file must not mistake a fixture for a real finding.
# ---------------------------------------------------------------------------

# The defect: a poll that waits only for the terminal status, then issues the
# call the still-held claim rejects.
_SYNTHETIC_POLL_THEN_CONTEND_SOURCE: Final[str] = textwrap.dedent(
    """
    async def test_second_reabstract_is_accepted():
        for _ in range(40):
            doc = await graph_store.get_document(doc_id)
            if doc.pipeline_status == PipelineStatus.ABSTRACTION_COMPLETE:
                break
            await asyncio.sleep(0.05)
        second = await ingestion_service.reabstract(doc_id)
        assert second["status"] == "reabstract_started"
    """
)

# The inverse arrangement: the call dispatches background work and the poll
# drains it. Nothing is gated and no claim is contended.
_SYNTHETIC_CONTEND_BEFORE_POLL_SOURCE: Final[str] = textwrap.dedent(
    """
    async def test_background_job_drains():
        response = await ingestion_service.recompute_pipeline(doc_id)
        assert response["status"] == "recompute_pipeline_started"
        for _ in range(40):
            doc = await graph_store.get_document(doc_id)
            if doc.pipeline_status == PipelineStatus.ABSTRACTION_COMPLETE:
                break
            await asyncio.sleep(0.05)
    """
)

# A poll that observes the pipeline and asserts on the result. Terminal-only
# and contending with nothing.
_SYNTHETIC_POLL_WITHOUT_CONTEND_SOURCE: Final[str] = textwrap.dedent(
    """
    async def test_pipeline_reaches_terminal():
        for _ in range(40):
            doc = await graph_store.get_document(doc_id)
            if doc.pipeline_status == PipelineStatus.ABSTRACTION_COMPLETE:
                break
            await asyncio.sleep(0.05)
        assert doc.semantic_abstract
    """
)

# The sanctioned form: the wait is delegated to a helper that takes the service
# and so can require the claim clear as well as the status.
_SYNTHETIC_HELPER_MEDIATED_SOURCE: Final[str] = textwrap.dedent(
    """
    async def test_second_reabstract_is_accepted_via_helper():
        await _await_pipeline_terminal(graph_store, doc_id, service=ingestion_service)
        second = await ingestion_service.reabstract(doc_id)
        assert second["status"] == "reabstract_started"
    """
)


# The API spelling of the same defect: the contending call is a route, so the
# name walk alone would miss it.
_SYNTHETIC_POLL_THEN_ROUTE_CONTEND_SOURCE: Final[str] = textwrap.dedent(
    """
    async def test_second_reabstract_over_http_is_accepted():
        for _ in range(40):
            doc = await services.graph_store.get_document(doc_id)
            if doc.pipeline_status == PipelineStatus.ABSTRACTION_COMPLETE:
                break
            await asyncio.sleep(0.05)
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents/{doc_id}/reabstract"
        )
        assert resp.status_code == 200
    """
)


def test_claim_arm_detector_flags_poll_then_contend() -> None:
    """An inline poll followed by a contending call is reported, naming the
    poll's own line and the call it gates.
    """
    found = _poll_then_contend(ast.parse(_SYNTHETIC_POLL_THEN_CONTEND_SOURCE))
    assert [(call, call_line) for _, call, call_line in found] == [("reabstract", 8)]


def test_claim_arm_detector_ignores_contend_before_poll() -> None:
    """A contending call that precedes the poll is correct and must be left
    alone.

    This is the negative control for the ordering rule: a detector that merely
    looked for both shapes in one function would report this, and every test
    that drains a job it dispatched would go red.
    """
    assert _poll_then_contend(ast.parse(_SYNTHETIC_CONTEND_BEFORE_POLL_SOURCE)) == []


def test_claim_arm_detector_ignores_poll_with_no_contend() -> None:
    """A poll that gates nothing contends with nothing."""
    assert _poll_then_contend(ast.parse(_SYNTHETIC_POLL_WITHOUT_CONTEND_SOURCE)) == []


def test_claim_arm_detector_ignores_helper_mediated_wait() -> None:
    """A wait expressed through a shared claim-aware helper is invisible.

    This pins the documented blind spot rather than leaving it to omission:
    the helper is the sanctioned indirection, and it carries the correctness
    argument each call site no longer has to restate.
    """
    assert _poll_then_contend(ast.parse(_SYNTHETIC_HELPER_MEDIATED_SOURCE)) == []


def test_claim_arm_detector_flags_poll_then_route_contend() -> None:
    """Contention issued over HTTP is caught as readily as a direct call.

    The route is the only thing naming the guarded work at such a site, so a
    walk that matched function names alone would let the whole API-mediated
    half of the suite reintroduce the defect.
    """
    found = _poll_then_contend(ast.parse(_SYNTHETIC_POLL_THEN_ROUTE_CONTEND_SOURCE))
    assert [target for _, target, _ in found] == ["/reabstract"]


# ---------------------------------------------------------------------------
# Status-only-helper detector self-tests
#
# Source strings again, for the same reason: the live walk over this file must
# not read a fixture as a real finding.
# ---------------------------------------------------------------------------

# The defect this arm exists for: a reusable wait that returns on the status
# alone, so every one of its call sites inherits the race.
_SYNTHETIC_STATUS_ONLY_HELPER_SOURCE: Final[str] = textwrap.dedent(
    """
    async def _await_terminal(graph_store, doc_id, *, attempts=500):
        for _ in range(attempts):
            doc = await graph_store.get_document(doc_id)
            if doc is not None and doc.pipeline_status in _TERMINAL_STATES:
                return doc.pipeline_status
            await asyncio.sleep(0.01)
        raise AssertionError("timed out")
    """
)

# The same wait with the claim arm present. Both conditions, so a caller can
# act on what it returns.
_SYNTHETIC_CLAIM_AWARE_HELPER_SOURCE: Final[str] = textwrap.dedent(
    """
    async def _await_idle(graph_store, doc_id, *, service, attempts=400):
        for _ in range(attempts):
            doc = await graph_store.get_document(doc_id)
            if doc.pipeline_status in _TERMINAL_STATES and doc_id not in service._inflight:
                return doc
            await asyncio.sleep(0.01)
        raise AssertionError("timed out")
    """
)

# The sanctioned form: no loop at all, because the wait is delegated.
_SYNTHETIC_DELEGATING_WAIT_SOURCE: Final[str] = textwrap.dedent(
    """
    async def test_reabstract_after_settle(graph_store, ingestion_service, doc_id):
        await await_pipeline_idle(graph_store, doc_id, service=ingestion_service)
        result = await ingestion_service.reabstract(doc_id)
        assert result["status"] == "reabstract_started"
    """
)

# A helper nested inside a test, which is how one of these hid from an
# enumeration that only looked at module level.
_SYNTHETIC_NESTED_HELPER_SOURCE: Final[str] = textwrap.dedent(
    """
    async def test_end_to_end(services):
        async def _await_terminal_pipeline(doc_id):
            for _ in range(200):
                doc = await services.graph_store.get_document(doc_id)
                if doc.pipeline_status in TERMINAL_PIPELINE_STATUSES:
                    return doc
                await asyncio.sleep(0.05)
            raise AssertionError("timed out")

        await _await_terminal_pipeline("doc1")
    """
)

# A polling loop on something else entirely. Out of scope.
_SYNTHETIC_UNRELATED_POLL_SOURCE: Final[str] = textwrap.dedent(
    """
    async def _await_health(client):
        for _ in range(30):
            resp = await client.get("/healthz")
            if resp.status_code == 200:
                return resp
            await asyncio.sleep(0.1)
        raise AssertionError("timed out")
    """
)


def test_helper_detector_flags_status_only_helper() -> None:
    """A reusable wait that checks the status and nothing else is reported,
    named, and anchored at its loop.
    """
    found = _status_only_poll_helpers(ast.parse(_SYNTHETIC_STATUS_ONLY_HELPER_SOURCE))
    assert [name for _, name in found] == ["_await_terminal"]


def test_helper_detector_ignores_claim_aware_helper() -> None:
    """A wait that also requires the claim clear is the correct shape and must
    not be reported -- otherwise the gate would flag its own remedy.
    """
    assert _status_only_poll_helpers(ast.parse(_SYNTHETIC_CLAIM_AWARE_HELPER_SOURCE)) == []


def test_helper_detector_ignores_delegating_wait() -> None:
    """A caller that delegates the wait has no loop to anchor on.

    This is the negative control for the whole arm: if delegation were
    reported, migrating to the shared helper would leave the gate red and the
    only way out would be an allowlist entry per call site.
    """
    assert _status_only_poll_helpers(ast.parse(_SYNTHETIC_DELEGATING_WAIT_SOURCE)) == []


def test_helper_detector_flags_nested_helper_under_its_own_name() -> None:
    """A helper defined inside a test is reported once, under its own name.

    Attribution to the innermost enclosing function is what keeps a nested
    definition from being reported twice, and what makes the finding name the
    function a reader has to edit rather than the test that happens to hold it.
    """
    found = _status_only_poll_helpers(ast.parse(_SYNTHETIC_NESTED_HELPER_SOURCE))
    assert [name for _, name in found] == ["_await_terminal_pipeline"]


def test_helper_detector_ignores_polls_on_other_subjects() -> None:
    """A bounded poll that has nothing to do with the pipeline is out of scope
    even though it is structurally identical.
    """
    assert _status_only_poll_helpers(ast.parse(_SYNTHETIC_UNRELATED_POLL_SOURCE)) == []


# A loop that enumerates documents and reads pipeline_status off each. It
# yields to nothing, so it waits for nothing and races nothing.
_SYNTHETIC_NON_SLEEPING_LOOP_SOURCE: Final[str] = textwrap.dedent(
    """
    async def _seed_mixed_vault(graph_store, specs):
        for name, status in specs:
            doc = _document(name, pipeline_status=status)
            await graph_store.create_document(doc)
    """
)


def test_helper_detector_ignores_loops_that_never_sleep() -> None:
    """A loop that does not yield is not a wait.

    This pins the rule that keeps the arm's findings legible: seeding loops,
    assertion loops over a result set, and the AST walks in this very module
    all mention ``pipeline_status`` inside a loop while waiting for nothing.
    Reporting them would bury the handful of real waits among dozens that can
    never race anything.
    """
    assert _status_only_poll_helpers(ast.parse(_SYNTHETIC_NON_SLEEPING_LOOP_SOURCE)) == []


# A module holding both shapes at once: a status-only helper beside a function
# that delegates. Every synthetic above holds one function, so none of them can
# tell per-function exemption from module-wide exemption.
_SYNTHETIC_MIXED_MODULE_SOURCE: Final[str] = textwrap.dedent(
    """
    async def _await_terminal(graph_store, doc_id):
        for _ in range(400):
            doc = await graph_store.get_document(doc_id)
            if doc.pipeline_status in _TERMINAL_STATES:
                return doc
            await asyncio.sleep(0.01)
        raise AssertionError("timed out")

    async def test_uses_the_shared_helper(graph_store, ingestion_service, doc_id):
        await await_pipeline_idle(graph_store, doc_id, service=ingestion_service)
        assert True
    """
)

# A status-only helper that *mentions* the claim registry in its docstring.
_SYNTHETIC_DOCSTRING_MARKER_SOURCE: Final[str] = textwrap.dedent(
    """
    async def _await_terminal(graph_store, doc_id):
        \"\"\"Status-only wait; deliberately does not consult _inflight.\"\"\"
        for _ in range(400):
            doc = await graph_store.get_document(doc_id)
            if doc.pipeline_status in _TERMINAL_STATES:
                return doc
            await asyncio.sleep(0.01)
        raise AssertionError("timed out")
    """
)

# A status-only loop in the outer function, with a nested def that delegates.
_SYNTHETIC_NESTED_MARKER_SOURCE: Final[str] = textwrap.dedent(
    """
    async def test_outer(graph_store, ingestion_service, doc_id):
        for _ in range(400):
            doc = await graph_store.get_document(doc_id)
            if doc.pipeline_status in _TERMINAL_STATES:
                break
            await asyncio.sleep(0.01)

        async def _later(other_id):
            await await_pipeline_idle(graph_store, other_id, service=ingestion_service)
    """
)


def test_helper_detector_exempts_per_function_not_per_module() -> None:
    """A delegating function does not exempt its status-only neighbour.

    Per-function scoping is the whole value of this arm: after the migration
    every module that polls also contains a marker somewhere, so an
    implementation that exempted module-wide would leave the live gate green
    while blind in exactly the modules it guards. Every other synthetic here
    holds a single function and cannot tell the two apart.
    """
    found = _status_only_poll_helpers(ast.parse(_SYNTHETIC_MIXED_MODULE_SOURCE))
    assert [name for _, name in found] == ["_await_terminal"]


def test_helper_detector_ignores_a_marker_in_a_docstring() -> None:
    """Prose about the claim is not a check on it.

    "deliberately does not consult ``_inflight``" is a natural sentence for
    precisely the helper this arm exists to report, so reading the docstring as
    evidence of a claim arm exempts the defect on the strength of admitting it.
    """
    found = _status_only_poll_helpers(ast.parse(_SYNTHETIC_DOCSTRING_MARKER_SOURCE))
    assert [name for _, name in found] == ["_await_terminal"]


def test_helper_detector_ignores_a_marker_in_a_nested_def() -> None:
    """A nested definition's body does not exempt its enclosing function.

    The nested def runs in its own scope and is attributed to itself by the
    walk, so a marker inside it says nothing about the enclosing function's own
    loop.
    """
    found = _status_only_poll_helpers(ast.parse(_SYNTHETIC_NESTED_MARKER_SOURCE))
    assert [name for _, name in found] == ["test_outer"]


def test_status_only_allowlist_has_no_stale_entries() -> None:
    """Every allowlist entry names a function the walk still reports.

    An entry whose function was since migrated, renamed, or deleted lingers
    silently: the gate stays green while the allowlist documents an exemption
    that no longer applies, and the next reader inherits a waiver for something
    that was fixed. ``KNOWN_VIOLATIONS`` in
    ``tests/sage/test_router_conformance.py`` carries the same assertion for
    the same reason.
    """
    stale: list[str] = []
    for rel, names in STATUS_ONLY_POLL_ALLOWLIST.items():
        path = REPO_ROOT / rel
        reported = (
            {name for _, name in _status_only_poll_helpers(ast.parse(path.read_bytes()))}
            if path.exists()
            else set()
        )
        stale.extend(f"{rel}: {name}" for name in names if name not in reported)

    assert not stale, (
        "STATUS_ONLY_POLL_ALLOWLIST entries that the walk no longer reports "
        f"({len(stale)}): {', '.join(stale)}. Drop each one — the poll it "
        "exempted is gone, so the entry now waives nothing."
    )
