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

All three arms above share one premise: that a wait is written as a *loop*.
Each asks what the loop reads and whether it reads enough. None of them asks
whether a loop is there at all, so the crudest form of the defect -- ingest,
sleep a fixed interval, act -- passes every one of them untouched. That blind
spot was not theoretical. The sweep that built this module ran over a test
module carrying dozens of bare sleeps, converted the polls it could see, and
left the sleeps standing; the class then recurred in that same file. A fourth
detector closes it from the remaining side: a function that ingests a document
and then awaits a fixed sleep outside any loop is reported, because a sleep
reads nothing and so cannot be waiting on anything. Its exemption is the
mirror of the others' -- a wait expressed through the shared helper has no
sleep of its own to anchor on.

That fourth arm reads the ingest through one hop of the call graph as well as
through the scope chain, because a module that ingests via its own helper puts
the ingestion in no scope enclosing the sleep -- walking outward never reaches
it, and the class survived twice in exactly that shape. The hop is one call
deep and confined to the module being scanned; ``_module_ingest_helpers``
states why the bound sits where it does.

All four arms so far need a sleep to anchor on, and so all four are blind to
the wait that was never written. A fifth closes that where it does the most
damage: a *fixture* that ingests a document and yields without waiting hands
one still in flight to every test that requests it, and none of those tests
can repair it -- by the time any of them runs, it has been racing since before
its first line. Two fixtures stood in exactly that shape while carrying an
allowlist entry for their teardown drain, so the fourth arm reported them and
the report said nothing about the gap that mattered; they were found by
somebody reading the code. The exemption is the others' again, widened to the
module's own wait adapter as well as the shared helper's entry points.

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


# ---------------------------------------------------------------------------
# Bare-sleep allowlist
#
# path (relative to repo root) -> names of the functions that ingest a document
# and then await a fixed sleep outside any loop, where the sleep waits for
# something other than that document becoming actionable. Empty by default; a
# wait on the ingestion pipeline has to poll the thing it is waiting for, and
# the sanctioned form is to delegate to tests/helpers/pipeline_wait.py. Every
# entry requires a one-line rationale naming what the sleep actually waits for.
#
# Keyed by function name rather than line number, for the reason
# STATUS_ONLY_POLL_ALLOWLIST gives: a line number is a coordinate any edit
# above it invalidates, while the name is what a reader has to go and look at.
# ---------------------------------------------------------------------------

BARE_SLEEP_WAIT_ALLOWLIST: Final[dict[str, list[str]]] = {
    # Settles the post-reload registry swap, not a document: the row this test
    # reloads to see is written straight to the store already terminal, so it
    # never enters the pipeline and there is nothing to poll for.
    "tests/sage/test_mcp_server.py": ["test_reload_vault_sees_external_changes"],
    # Fixture teardown drains: they let background work unwind before the
    # registry slot is dropped, after the tests using the documents have run.
    "tests/sage/test_search_misplaced_filters.py": ["vault_services"],
    "tests/sage/test_storage_query_error_envelope.py": ["vault_services"],
}


# ---------------------------------------------------------------------------
# Unwaited-fixture allowlist
#
# path (relative to repo root) -> names of the fixtures that ingest a document
# and then yield without waiting for it, where handing a moving document to
# every test using the fixture is nonetheless correct. Empty by default, and
# the bar for an entry is high: a fixture's tests cannot each decide to wait,
# because the fixture is where the document was put in flight.
#
# Keyed by function name, for the reason the two allowlists above give. Every
# entry requires a one-line rationale naming why the tests can act on a
# document that has not settled.
# ---------------------------------------------------------------------------

UNWAITED_FIXTURE_YIELD_ALLOWLIST: Final[dict[str, list[str]]] = {}


# The ingestion entry points. A function that calls one of these has put a
# document into the background pipeline, so a fixed sleep after that call is
# standing in for a wait on it. Matched on the bare name by ``_called_name``,
# so the tool and a directly-imported service method both count.
INGEST_CALLS: Final[frozenset[str]] = frozenset(
    {
        "ingest_document",
        "bulk_ingest_document",
    }
)


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


def _module_ingest_helpers(tree: ast.AST) -> frozenset[str]:
    """Names of the module's own top-level functions that ingest.

    A module that ingests through a per-module helper -- write a caller-local
    file, ingest it, hand the result back -- puts the ingestion outside every
    scope enclosing the call site, where the lexical walk cannot reach it.
    Resolving the call is the only way to see it, and this is the set of names
    worth resolving.

    Two bounds, both deliberate and both pinned by their own tests:

    * **The module's own top-level definitions**, not a name set shared across
      the tree. Helper names repeat, and a name that ingests in one module may
      do something else entirely in another -- seeding the graph store
      directly, say, which puts nothing in the pipeline. Reading each module's
      definitions answers for that module rather than guessing from a name.
    * **The helper's own body**, so resolution stops one call deep. A helper
      that reaches an ingest only through a second helper is not in this set,
      at the cost of missing a two-hop chain. Following calls to arbitrary
      depth would make the walk's own reach hard to state, and one hop covers
      the shape that recurs: a module with a single ingest helper its tests
      call.

    Only module-level *names* enter the set, though each name is judged on its
    whole body, nested definitions included -- the scoping ``_first_ingest_line``
    already uses, and for the same reason: a call that reaches an ingest
    through a nested helper has ingested. A nested ``def``'s own name stays
    out, because no sibling can call it: admitting it would let an unrelated
    call to a same-named function elsewhere qualify a sleep that waits for
    nothing.

    The match is on the call, not on its effect. A helper that calls an
    ingestion entry point which declines to ingest -- minting a transfer
    recipe under the cloud profile rather than taking the document -- counts
    here regardless, because no static walk can tell the two apart.
    """
    return frozenset(
        node.name
        for node in ast.iter_child_nodes(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(inner, ast.Call) and _called_name(inner.func) in INGEST_CALLS
            for inner in ast.walk(node)
        )
    )


def _first_ingest_line(
    func: ast.FunctionDef | ast.AsyncFunctionDef, helpers: frozenset[str]
) -> int | None:
    """The line of the function's earliest ingestion call, or None if it makes none.

    Scoped to the whole body, nested definitions included: a document put into
    the pipeline by a nested helper is in the pipeline just the same, and the
    enclosing function is where the wait for it has to be written.

    ``helpers`` names the module's own ingesting functions, from
    ``_module_ingest_helpers``. A call to one of them counts as an ingestion at
    the line of the *call*, which is where the document enters the pipeline as
    far as this function is concerned -- and is the line a reader comparing it
    against a following sleep has to look at.
    """
    lines = [
        node.lineno
        for node in ast.walk(func)
        if isinstance(node, ast.Call) and _called_name(node.func) in (INGEST_CALLS | helpers)
    ]
    return min(lines) if lines else None


def _bare_sleep_waits(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(sleep lineno, function name)`` for every fixed sleep standing in
    for a wait on a document the same function ingested.

    The three arms above all require an inline *loop*, because each is about a
    poll that reads the wrong thing. This arm is about the absence of a poll:
    a bare ``await asyncio.sleep(0.5)`` reads nothing at all, so it races the
    pipeline outright and is invisible to every loop-shaped walk. That blind
    spot is not hypothetical -- the sweep that built this module left dozens of
    bare sleeps standing in the very file it swept, and the class recurred.

    Three conditions, each narrowing toward the shape that actually races:

    * **Outside a loop.** A sleeping loop is a poll, and belongs to the arms
      above; flagging it here would report the sanctioned shape twice.
    * **After an ingestion call in the sleeping function, in any function
      enclosing it, or in a module-local helper one of those calls.** A sleep
      in a scope that ingests nothing waits for something else -- a worker
      unwinding, a thread joining, a registry slot swapping -- and this walk
      has no opinion about it. The qualifier reads the enclosing chain rather
      than the innermost body alone, because a test that ingests and then
      sleeps inside a nested helper is the same race written one scope down;
      reading only the innermost body drops it. It then resolves calls to the
      module's own ingesting helpers, from ``_module_ingest_helpers``, because
      a *sibling* helper is in no enclosing scope at all: walking outward
      never reaches it, however far it goes. That resolution is one call deep
      and confined to the module, which is a known limit rather than an
      oversight -- the bounds and the reasons for them are stated there.
    * **Attributed to the innermost enclosing function**, so a nested helper is
      judged under its own name rather than its caller's. Attribution and
      qualification therefore look in opposite directions, which is deliberate:
      the name a finding carries is the one a reader has to go and open, while
      the ingest that makes the sleep a race can live further out.

    Delegation is invisible by construction, as in the arm above: a function
    that waits through the shared helper has no bare sleep for this walk to
    anchor on. A function that does both keeps its sleep and needs an
    allowlist entry saying what the sleep is really for.
    """
    helpers = _module_ingest_helpers(tree)
    findings: list[
        tuple[int, ast.FunctionDef | ast.AsyncFunctionDef, ast.FunctionDef | ast.AsyncFunctionDef]
    ] = []

    def visit(
        node: ast.AST,
        func: ast.FunctionDef | ast.AsyncFunctionDef | None,
        outermost: ast.FunctionDef | ast.AsyncFunctionDef | None,
        in_loop: bool,
    ) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child, outermost or child, False)
                continue
            nested = in_loop or isinstance(child, (ast.For, ast.AsyncFor, ast.While))
            if (
                func is not None
                and outermost is not None
                and not nested
                and isinstance(child, ast.Call)
                and _called_name(child.func) == "sleep"
            ):
                findings.append((child.lineno, func, outermost))
            visit(child, func, outermost, nested)

    visit(tree, None, None, False)

    kept: list[tuple[int, str]] = []
    for lineno, func, outermost in sorted(findings, key=lambda entry: entry[0]):
        # Qualify against the outermost enclosing function, whose subtree walk
        # covers every ingest in the chain including the sleeping scope's own,
        # plus the calls that reach one of the module's own ingest helpers.
        ingest_line = _first_ingest_line(outermost, helpers)
        if ingest_line is None:
            continue
        # The textual ordering test applies only where the sleep runs where it
        # is written. A nested definition does not: its body runs when the
        # helper is called, which is necessarily after the enclosing function
        # reached the call, wherever the ``def`` happens to sit. Comparing a
        # nested sleeper's line against the ingest asks about definition order
        # and answers as though it were execution order, dropping the helper
        # defined above the ingest and called below it.
        if func is outermost and lineno <= ingest_line:
            continue
        kept.append((lineno, func.name))
    return kept


def _format_bare_sleep_violations(violations: list[tuple[str, int, str]]) -> str:
    """Render a bare-sleep violation list as a pytest.fail message."""
    head = violations[:_MAX_REPORTED]
    body = "\n".join(f"  {path}:{line} → in {name}()" for path, line, name in head)
    overflow = len(violations) - len(head)
    tail = f"\n  ... and {overflow} more" if overflow > 0 else ""
    return (
        f"Fixed sleeps standing in for a wait on an ingested document "
        f"({len(violations)} found):\n{body}{tail}\n"
        "A fixed sleep reads nothing, so it does not wait for the pipeline -- "
        "it guesses how long the pipeline takes, and passes only while the "
        "guess holds. Under load it does not, and the test fails on the state "
        "its own next call rejects. Delegate to await_pipeline_idle / "
        "await_tool_idle in tests/helpers/pipeline_wait.py, which poll the "
        "terminal status and the in-flight claim together."
    )


def _module_wait_helpers(tree: ast.AST) -> frozenset[str]:
    """Names of the module's own top-level functions that wait on the claim.

    The mirror of ``_module_ingest_helpers``, and bounded the same way: the
    module's own top-level definitions, each judged on its whole body. A
    module that waits through a thin local adapter -- one that reads through
    the tool surface, say, and hands the predicate to the shared helper -- has
    waited, and a walk that only recognized the shared helper's own names
    would report every one of those call sites.
    """
    return frozenset(
        node.name
        for node in ast.iter_child_nodes(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _consults_claim(node)
    )


def _first_pipeline_wait_line(
    func: ast.FunctionDef | ast.AsyncFunctionDef, wait_helpers: frozenset[str]
) -> int | None:
    """The line of the function's earliest wait on the pipeline, or None if it makes none.

    A wait is either a consultation of the claim in the function's own body --
    the shared helper's entry points or the registry, matched exactly as
    ``_consults_claim`` matches them -- or a call to one of the module's own
    waiting helpers, from ``_module_wait_helpers``.

    ``_consults_claim``'s two exclusions are reproduced rather than relaxed,
    because both bite here for the same reasons they bite there. The match is
    on identifiers, so a docstring saying the fixture deliberately does not
    wait is a string constant and cannot match. The descent stops at a nested
    scope, so a nested helper's wait does not exempt the body that encloses
    it.

    A *line* rather than a boolean, because for the arm below the position is
    the whole question: a fixture that waits only after handing control on has
    not waited for its tests, and a walk that reads the body as an unordered
    set cannot tell the two apart.
    """

    def scan(node: ast.AST) -> list[int]:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return []
        if isinstance(node, ast.Attribute) and node.attr in CLAIM_AWARE_MARKERS:
            return [node.lineno]
        if isinstance(node, ast.Name) and node.id in CLAIM_AWARE_MARKERS:
            return [node.lineno]
        found = (
            [node.lineno]
            if isinstance(node, ast.Call) and _called_name(node.func) in wait_helpers
            else []
        )
        for child in ast.iter_child_nodes(node):
            found.extend(scan(child))
        return found

    lines = [line for stmt in func.body for line in scan(stmt)]
    return min(lines) if lines else None


def _is_fixture(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether a function carries a pytest fixture decorator.

    Matched on the decorator's bare name, so ``@pytest.fixture``,
    ``@pytest.fixture(scope=...)`` and a directly imported ``@fixture`` all
    count.
    """
    return any(
        _called_name(decorator.func if isinstance(decorator, ast.Call) else decorator) == "fixture"
        for decorator in func.decorator_list
    )


def _unwaited_fixture_yields(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(yield lineno, fixture name)`` for every fixture that ingests a
    document and then yields without waiting for it.

    The four arms above all need a *sleep* to anchor on: each asks what a wait
    reads, and the last asks whether a wait that reads nothing is standing in
    for one. None of them asks whether a wait was written at all, so a fixture
    that ingests and yields straight through passes every one untouched -- and
    that is the worst form of the race rather than the mildest. A fixture is
    setup for every test that requests it, so an unsettled document is handed
    to all of them at once, and none of those tests can fix it: by the time
    one runs, the document has been in flight since before its first line.

    That shape was not theoretical either. Two fixtures seeded documents and
    yielded with no wait; both carried an allowlist entry for the *teardown*
    drain, which is a different sleep and correctly exempt, so the arm above
    reported them and the report said nothing about the gap that mattered.
    They were found by somebody reading the code.

    Three conditions:

    * **A fixture**, by its decorator. A plain helper that ingests and returns
      is out of scope: its caller is a single function, which is where the
      wait can be written and where the arms above already look.
    * **Yielding after an ingestion** -- reached directly, through the
      enclosing chain, or through one module-local helper, exactly as
      ``_first_ingest_line`` resolves it for the arm above. A fixture whose
      only ingest follows its ``yield`` is doing teardown work, and has no
      document in flight at the moment it hands control on.
    * **Not waiting before that yield**, by ``_first_pipeline_wait_line``.
      Delegation is the sanctioned form and is invisible here by
      construction, whether the fixture calls the shared helper or the
      module's own adapter for it -- but only a wait that *precedes* the
      handoff exempts one. Both conditions are positional against the same
      line, and deliberately so: a wait in a ``finally`` runs after every
      test has already had the document, and reading the body as an unordered
      set would exempt a fixture on the strength of a wait its tests never
      benefited from. That is not a hypothetical spelling. Both attested
      instances carry a teardown drain, so the natural next cleanup -- retire
      the drain's allowlist entry by making it a real wait -- writes exactly
      that fixture, and a position-blind walk would go quiet on it while
      still printing "wait before the yield".

    The walk has no opinion about *what* the tests then do with the document.
    It cannot: they are other functions, often in other files by the time a
    fixture is shared. A fixture that ingests owes its tests a settled
    document whether or not this walk can prove one of them reads a field the
    pipeline writes.

    Two bounds worth stating, both measured empty on the tree today. The walk
    sees only the modules ``_tracked_test_modules`` enumerates, whose basename
    must begin with ``test_`` -- so a fixture defined in a ``conftest.py`` is
    invisible to it, and that is where a widely shared fixture would most
    naturally live. And it anchors on a ``yield``, so a fixture that ingests
    and *returns* hands its tests an unsettled document just the same while
    going unreported. Neither is closed here; both are named so a later
    reader does not mistake an unexercised limit for coverage.
    """
    ingest_helpers = _module_ingest_helpers(tree)
    wait_helpers = _module_wait_helpers(tree)

    findings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not _is_fixture(node):
            continue
        ingest_line = _first_ingest_line(node, ingest_helpers)
        if ingest_line is None:
            continue
        yields = [
            inner.lineno
            for inner in ast.walk(node)
            if isinstance(inner, (ast.Yield, ast.YieldFrom)) and inner.lineno > ingest_line
        ]
        if not yields:
            continue
        handoff = min(yields)
        wait_line = _first_pipeline_wait_line(node, wait_helpers)
        if wait_line is not None and wait_line < handoff:
            continue
        findings.append((handoff, node.name))
    return sorted(findings)


def _format_unwaited_fixture_violations(violations: list[tuple[str, int, str]]) -> str:
    """Render an unwaited-fixture violation list as a pytest.fail message."""
    head = violations[:_MAX_REPORTED]
    body = "\n".join(f"  {path}:{line} → in {name}()" for path, line, name in head)
    overflow = len(violations) - len(head)
    tail = f"\n  ... and {overflow} more" if overflow > 0 else ""
    return (
        f"Fixtures that ingest a document and yield without waiting for it "
        f"({len(violations)} found):\n{body}{tail}\n"
        "Ingestion dispatches the pipeline in the background, so a fixture "
        "that yields straight from the ingest hands a document still in "
        "flight to every test that requests it -- and no test can repair "
        "that, because it was already racing before its first line ran. Wait "
        "before the yield, via await_pipeline_idle / await_tool_idle in "
        "tests/helpers/pipeline_wait.py."
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


def test_no_bare_sleep_waits_on_ingested_documents() -> None:
    """No tracked test module may wait on an ingested document with a fixed sleep."""
    violations: list[tuple[str, int, str]] = []
    for path in _tracked_test_modules():
        rel = str(path.relative_to(REPO_ROOT))
        try:
            tree = ast.parse(path.read_bytes(), filename=str(path))
        except SyntaxError:
            # A syntactically broken test module fails its own collection
            # loudly; not this gate's concern.
            continue
        allowed = set(BARE_SLEEP_WAIT_ALLOWLIST.get(rel, []))
        for lineno, name in _bare_sleep_waits(tree):
            if name in allowed:
                continue
            violations.append((rel, lineno, name))

    if violations:
        pytest.fail(_format_bare_sleep_violations(violations))


def test_no_fixture_yields_an_unsettled_document() -> None:
    """No tracked test module may seed a document in a fixture and yield unwaited."""
    violations: list[tuple[str, int, str]] = []
    for path in _tracked_test_modules():
        rel = str(path.relative_to(REPO_ROOT))
        try:
            tree = ast.parse(path.read_bytes(), filename=str(path))
        except SyntaxError:
            # A syntactically broken test module fails its own collection
            # loudly; not this gate's concern.
            continue
        allowed = set(UNWAITED_FIXTURE_YIELD_ALLOWLIST.get(rel, []))
        for lineno, name in _unwaited_fixture_yields(tree):
            if name in allowed:
                continue
            violations.append((rel, lineno, name))

    if violations:
        pytest.fail(_format_unwaited_fixture_violations(violations))


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


# ---------------------------------------------------------------------------
# Bare-sleep arm: anti-coincidental detector self-tests
# ---------------------------------------------------------------------------

# The defective shape this arm exists for: ingest, guess how long the pipeline
# takes, then act on the document. Kept as a string so the gate's own walk over
# this file does not see it as a real violation.
_SYNTHETIC_BARE_SLEEP_SOURCE: Final[str] = textwrap.dedent(
    """
    async def test_reads_a_projection(vault_services):
        doc = _parse(await ingest_document("v", "test/sample.md", "markdown"))
        await asyncio.sleep(0.5)
        return _parse(await read_projection("v", doc["id"]))
    """
)

# The same wait written as a poll. It belongs to the three arms above, which
# judge what the loop reads; reporting it here as well would flag the
# sanctioned shape under a rule it already satisfies.
_SYNTHETIC_LOOPED_SLEEP_SOURCE: Final[str] = textwrap.dedent(
    """
    async def test_polls_instead(vault_services):
        doc = _parse(await ingest_document("v", "test/sample.md", "markdown"))
        for _ in range(400):
            fetched = _parse(await get_document("v", doc["id"]))
            if fetched["pipeline_status"] in {"abstraction_complete", "failed"}:
                break
            await asyncio.sleep(0.01)
    """
)

# A fixed sleep in a function that ingests nothing. It waits for something this
# walk has no opinion about -- a worker unwinding, a thread joining, a registry
# slot swapping at teardown -- and reporting it would bury the real findings.
_SYNTHETIC_SLEEP_WITHOUT_INGEST_SOURCE: Final[str] = textwrap.dedent(
    """
    async def vault_services(tmp_vault_dir):
        async with initialize_services_for_test(config) as services:
            try:
                yield services
            finally:
                await asyncio.sleep(0.5)
                _mcp._vaults.pop("test_vault", None)
    """
)

# The sanctioned form. A function that waits through the shared helper has no
# bare sleep to anchor on, so it is invisible here by construction rather than
# by an exemption somebody has to maintain.
_SYNTHETIC_DELEGATED_WAIT_SOURCE: Final[str] = textwrap.dedent(
    """
    async def test_reads_a_projection(vault_services):
        doc = _parse(await ingest_document("v", "test/sample.md", "markdown"))
        await _await_document_idle(vault_services, "v", doc["id"])
        return _parse(await read_projection("v", doc["id"]))
    """
)

# A sleep that precedes the ingestion it is read against. Ordering is what
# makes a sleep a *wait* for the pipeline: one that runs before anything was
# ingested cannot be waiting on the document, whatever else it is doing.
_SYNTHETIC_SLEEP_BEFORE_INGEST_SOURCE: Final[str] = textwrap.dedent(
    """
    async def test_settles_the_clock_first(vault_services):
        await asyncio.sleep(0.5)
        return _parse(await ingest_document("v", "test/sample.md", "markdown"))
    """
)


def test_bare_sleep_detector_flags_ingest_then_sleep() -> None:
    """The arm has teeth: the defective shape is reported under its own name."""
    assert _bare_sleep_waits(ast.parse(_SYNTHETIC_BARE_SLEEP_SOURCE)) == [
        (4, "test_reads_a_projection")
    ]


def test_bare_sleep_detector_ignores_a_sleeping_poll() -> None:
    """A sleep inside a loop is a poll, and is the other three arms' subject.

    This is the boundary between the arms. Without it a correctly written
    bounded poll would be reported by this walk as well, and the sanctioned
    shape would have nowhere left to stand.
    """
    assert _bare_sleep_waits(ast.parse(_SYNTHETIC_LOOPED_SLEEP_SOURCE)) == []


def test_bare_sleep_detector_ignores_a_sleep_without_an_ingest() -> None:
    """A fixed sleep in a function that ingests nothing waits for something else.

    Fixture teardown drains, thread-join grace periods and gate-release
    unwinds all take this shape, and none of them races the ingestion
    pipeline. The ingest call is what makes a sleep this walk's business.
    """
    assert _bare_sleep_waits(ast.parse(_SYNTHETIC_SLEEP_WITHOUT_INGEST_SOURCE)) == []


def test_bare_sleep_detector_ignores_a_delegated_wait() -> None:
    """The sanctioned form is invisible because it has no sleep of its own."""
    assert _bare_sleep_waits(ast.parse(_SYNTHETIC_DELEGATED_WAIT_SOURCE)) == []


def test_bare_sleep_detector_ignores_a_sleep_before_the_ingest() -> None:
    """A sleep the ingestion follows is not a wait for that ingestion.

    Pins the ordering condition rather than leaving it to the coincidence that
    most tests ingest on their first line. A walk keyed on mere co-occurrence
    would report a setup delay as though it were a pipeline race.
    """
    assert _bare_sleep_waits(ast.parse(_SYNTHETIC_SLEEP_BEFORE_INGEST_SOURCE)) == []


def test_bare_sleep_allowlist_has_no_stale_entries() -> None:
    """Every bare-sleep allowlist entry names a function the walk still reports.

    Carried for the reason ``test_status_only_allowlist_has_no_stale_entries``
    gives: an entry whose function was since swept, renamed or deleted lingers
    silently, and the next reader inherits a waiver for something already
    fixed. This arm's entries are the likeliest to go stale, because each one
    names a sleep somebody may well convert later.
    """
    stale: list[str] = []
    for rel, names in BARE_SLEEP_WAIT_ALLOWLIST.items():
        path = REPO_ROOT / rel
        reported = (
            {name for _, name in _bare_sleep_waits(ast.parse(path.read_bytes()))}
            if path.exists()
            else set()
        )
        stale.extend(f"{rel}: {name}" for name in names if name not in reported)

    assert not stale, (
        "BARE_SLEEP_WAIT_ALLOWLIST entries that the walk no longer reports "
        f"({len(stale)}): {', '.join(stale)}. Drop each one — the sleep it "
        "exempted is gone, so the entry now waives nothing."
    )


# A helper nested inside a function that ingests, sleeping in its own body. The
# outer function ingests; the inner one is where the sleep lives. Attribution
# decides which name the finding — and therefore any allowlist entry — carries.
_SYNTHETIC_NESTED_SLEEP_SOURCE: Final[str] = textwrap.dedent(
    """
    async def test_outer(vault_services):
        doc = _parse(await ingest_document("v", "test/sample.md", "markdown"))

        async def _settle():
            await asyncio.sleep(0.5)

        await _settle()
        return doc
    """
)


def test_bare_sleep_detector_attributes_a_nested_sleep_to_the_inner_function() -> None:
    """A nested helper is judged under its own name, not its caller's.

    The status-only arm pins this property for its own walk; without the same
    pin here, an implementation attributing every sleep to the *outermost*
    enclosing function passes all five synthetics above. That rival is not
    academic: the allowlist is keyed by function name, so under it an entry
    naming an outer test would silently exempt sleeps in every helper nested
    inside it.

    The inner function ingests nothing itself, so this also pins that the
    ingest qualifier reaches the enclosing scope's call rather than requiring
    the ingest and the sleep to sit in the same body.
    """
    assert _bare_sleep_waits(ast.parse(_SYNTHETIC_NESTED_SLEEP_SOURCE)) == [(6, "_settle")]


# The same nested race with the helper *defined* above the ingest and called
# below it. Definition order and execution order come apart here, and only the
# second is what makes the sleep a race.
_SYNTHETIC_NESTED_SLEEP_DEFINED_FIRST_SOURCE: Final[str] = textwrap.dedent(
    """
    async def test_outer(vault_services):
        async def _settle():
            await asyncio.sleep(0.5)

        doc = _parse(await ingest_document("v", "test/sample.md", "markdown"))
        await _settle()
        return doc
    """
)


def test_bare_sleep_detector_flags_a_nested_sleeper_defined_before_the_ingest() -> None:
    """Definition order is not execution order for a nested helper.

    The ordering condition exists so a sleep that *precedes* the ingest is not
    read as a wait for it. That reasoning holds only where the sleep runs where
    it is written, which a nested ``def`` does not: its body runs when the
    helper is called. Comparing its line against the ingest asks about
    definition order and answers as though it were execution order, so the
    helper hoisted above the ingest and called below it -- an ordinary way to
    write one -- would be dropped.

    The companion to ``_ignores_a_sleep_before_the_ingest``, which pins the
    condition this one bounds: at the top level the textual test is still the
    right one, and that test keeps it honest.
    """
    assert _bare_sleep_waits(ast.parse(_SYNTHETIC_NESTED_SLEEP_DEFINED_FIRST_SOURCE)) == [
        (4, "_settle")
    ]


# The same race with the ingest moved out to a *sibling* helper -- the shape a
# module grows once more than one of its tests needs a caller-local file. The
# ingest is now in no scope enclosing the sleep, so only a walk that resolves
# the call sees it.
_SYNTHETIC_HELPER_INGEST_SOURCE: Final[str] = textwrap.dedent(
    """
    async def _ingest_local_file(tmp_path, name, body):
        src = tmp_path / name
        src.write_text(body)
        return _parse(await ingest_document("v", str(src), "markdown"))


    async def test_reads_a_projection(vault_services, tmp_path):
        doc = await _ingest_local_file(tmp_path, "sample.md", "# S")
        await asyncio.sleep(0.5)
        return _parse(await read_projection("v", doc["id"]))
    """
)


def test_bare_sleep_detector_flags_an_ingest_through_a_module_helper() -> None:
    """A sibling helper's ingest qualifies the sleep that follows the call.

    The enclosing-chain walk cannot reach this on its own: a module-level
    helper encloses nothing, so no amount of walking outward from the sleep
    arrives at the ingest. The finding is attributed to the *test*, whose body
    holds the sleep, rather than to the helper that ingests -- the two live in
    different functions here, which is what makes the pairing worth asserting.
    """
    assert _bare_sleep_waits(ast.parse(_SYNTHETIC_HELPER_INGEST_SOURCE)) == [
        (10, "test_reads_a_projection")
    ]


# The same shape with one more hop: the helper the test calls does not ingest
# itself, it calls a second helper that does. This is the far side of the
# resolution bound.
_SYNTHETIC_INDIRECT_HELPER_INGEST_SOURCE: Final[str] = textwrap.dedent(
    """
    async def _ingests(tmp_path):
        return _parse(await ingest_document("v", "test/sample.md", "markdown"))


    async def _outer(tmp_path):
        return await _ingests(tmp_path)


    async def test_reads_a_projection(vault_services, tmp_path):
        doc = await _outer(tmp_path)
        await asyncio.sleep(0.5)
        return _parse(await read_projection("v", doc["id"]))
    """
)


def test_bare_sleep_detector_ignores_an_ingest_two_helpers_away() -> None:
    """Call resolution stops one hop out, and the limit is asserted here.

    The companion to ``_flags_an_ingest_through_a_module_helper``: that one
    fails and this one passes under a walk that resolves nothing, so only a
    walk resolving exactly one hop passes both. Without the pair, "one level"
    would be a claim in a docstring rather than a property of the code.

    Following calls to arbitrary depth is not the goal. The reach of a walk
    that does is hard to state and harder to predict from a call site, and one
    hop covers the shape this arm exists for -- a module with its own ingest
    helper. A two-hop chain is missed, and that is the cost.
    """
    assert _bare_sleep_waits(ast.parse(_SYNTHETIC_INDIRECT_HELPER_INGEST_SOURCE)) == []


# A call to an ingest helper the module does not define -- imported, or simply
# named the same as one elsewhere in the tree. The other side of the bound: the
# resolution reads definitions, not names.
_SYNTHETIC_FOREIGN_HELPER_CALL_SOURCE: Final[str] = textwrap.dedent(
    """
    async def test_reads_a_projection(vault_services, tmp_path):
        doc = await _ingest_local_file(tmp_path, "sample.md", "# S")
        await asyncio.sleep(0.5)
        return _parse(await read_projection("v", doc["id"]))
    """
)


def test_bare_sleep_detector_ignores_a_call_the_module_does_not_define() -> None:
    """An unresolvable call is not an ingest, whatever the name suggests.

    ``_ingest_local_file`` is a real ingesting helper in more than one module
    of this tree, which is the point: a walk keyed on a set of names collected
    across the tree reports this source, and a walk keyed on the scanned
    module's own definitions does not. Helper names repeat, and the same name
    seeds the graph store directly in one module while ingesting in another --
    so a shared name set would waive nothing and report the wrong thing.
    """
    assert _bare_sleep_waits(ast.parse(_SYNTHETIC_FOREIGN_HELPER_CALL_SOURCE)) == []


# Four definitions for the resolution set to sort: one module-level function
# that ingests directly, one synchronous one that ingests, one that does not
# ingest at all, and one that ingests only through a ``def`` nested in its own
# body. The inert one is ``async`` and the second ingesting one is not, so
# neither membership nor absence can be explained by the kind of ``def``.
_SYNTHETIC_HELPER_RESOLUTION_SOURCE: Final[str] = textwrap.dedent(
    """
    async def _ingests(tmp_path):
        return _parse(await ingest_document("v", "test/sample.md", "markdown"))


    def _ingests_sync(client):
        return client.call(ingest_document("v", "test/sample.md", "markdown"))


    async def _inert(payload):
        return payload["id"]


    async def _nests_its_own(vault_services):
        async def _seed():
            return _parse(await ingest_document("v", "test/sample.md", "markdown"))

        return await _seed()
    """
)


def test_module_ingest_helpers_reads_module_level_names_and_whole_bodies() -> None:
    """The set holds every ingesting top-level name, and only top-level names.

    Four properties, one synthetic. ``_inert`` pins that the scan reads each
    definition's body rather than admitting every module-level function.
    ``_nests_its_own`` pins that a body is read whole, nested definitions
    included -- calling it ingests, so a sleep after that call is a race, and
    the scoping matches ``_first_ingest_line``'s. ``_seed`` pins the other
    half: its enclosing function is in the set but its own name is not, so a
    walk from the module root -- which would collect every ``FunctionDef``
    anywhere in the file -- fails here. That rival is the likely one, and
    under it a call to any same-named function in an unrelated module would
    qualify a sleep waiting for nothing.

    ``_ingests_sync`` closes a rival the other three admit: a scan that reads
    only ``AsyncFunctionDef``. Every helper the live tree ingests through is
    ``async``, so such a scan is green on all of them and on the absence of an
    ``async`` ``_inert``, while silently dropping a synchronous ingest helper.
    Carrying one ingesting plain ``def`` and one inert ``async def`` makes both
    membership and absence attributable to the body rather than to the kind of
    definition.
    """
    assert _module_ingest_helpers(ast.parse(_SYNTHETIC_HELPER_RESOLUTION_SOURCE)) == {
        "_ingests",
        "_ingests_sync",
        "_nests_its_own",
    }


# The defective shape the fifth arm exists for: seed a document, hand it to
# every test that asks for the fixture, wait for nothing. The teardown drain is
# carried too, because the real instances of this had one -- and its presence
# is what let the arm above report the fixture while saying nothing about the
# gap at the yield.
_SYNTHETIC_UNWAITED_FIXTURE_SOURCE: Final[str] = textwrap.dedent(
    """
    @pytest.fixture
    async def vault_services(config, tmp_vault_dir):
        async with initialize_services_for_test(config) as services:
            _mcp_vaults["test_vault"] = services
            await ingest_document("test_vault", "test/sample.md", "markdown")

            try:
                yield services
            finally:
                await asyncio.sleep(0.5)
                _mcp_vaults.pop("test_vault", None)
    """
)


def test_unwaited_fixture_detector_flags_an_ingest_then_yield() -> None:
    """The arm has teeth: a fixture that seeds and yields unwaited is reported.

    The finding is anchored at the ``yield`` rather than at the ingest, which
    is the line a reader has to edit -- the wait goes immediately above it.
    """
    assert _unwaited_fixture_yields(ast.parse(_SYNTHETIC_UNWAITED_FIXTURE_SOURCE)) == [
        (9, "vault_services")
    ]


# The sanctioned form: the same fixture waiting before it yields. The teardown
# drain is carried for shape parity with the unwaited synthetic above, so the
# two differ in one thing only. It pins nothing by itself -- this fixture is
# exempt by its real wait whether or not the drain is there -- and the rival
# that reads a sleep as a wait is excluded above, where its presence does make
# the difference.
_SYNTHETIC_WAITED_FIXTURE_SOURCE: Final[str] = textwrap.dedent(
    """
    @pytest.fixture
    async def vault_services(config, tmp_vault_dir):
        async with initialize_services_for_test(config) as services:
            _mcp_vaults["test_vault"] = services
            doc = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
            await await_pipeline_idle(
                services.graph_store, doc["id"], service=services.ingestion_service
            )

            try:
                yield services
            finally:
                await asyncio.sleep(0.5)
                _mcp_vaults.pop("test_vault", None)
    """
)


def test_unwaited_fixture_detector_ignores_a_delegated_wait() -> None:
    """The sanctioned form is invisible because the wait is there to find."""
    assert _unwaited_fixture_yields(ast.parse(_SYNTHETIC_WAITED_FIXTURE_SOURCE)) == []


# The same real wait, moved to the teardown side. Every identifier the walk
# looks for is present in the body; the only thing wrong with it is where it
# runs. This is the shape the arm's own remedy path produces -- retire the
# drain's allowlist entry by turning it into a wait -- so it is the rival most
# likely to be written, not the most contrived.
_SYNTHETIC_TEARDOWN_WAIT_FIXTURE_SOURCE: Final[str] = textwrap.dedent(
    """
    @pytest.fixture
    async def vault_services(config):
        async with initialize_services_for_test(config) as services:
            doc = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))

            try:
                yield services
            finally:
                await await_pipeline_idle(
                    services.graph_store, doc["id"], service=services.ingestion_service
                )
    """
)


def test_unwaited_fixture_detector_flags_a_wait_that_runs_only_in_teardown() -> None:
    """A wait after the handoff is not a wait the fixture's tests received.

    The condition is positional, and this is the test that makes it so. A walk
    reading the body as an unordered set finds ``await_pipeline_idle`` and
    every marker it looks for, and exempts this fixture -- while each test
    using it got its document while the pipeline was still moving, which is
    the entire defect. Position is the only thing separating this source from
    ``_SYNTHETIC_WAITED_FIXTURE_SOURCE``, so the pair isolates it.

    Worth pinning rather than documenting as a limit, because the arm's own
    failure message points a reader here: both attested fixtures carry an
    allowlisted teardown drain, and the obvious way to retire those entries is
    to make the drain a real wait. Under the position-blind reading that edit
    silently disarms the arm for that fixture while the message still says to
    wait before the yield.
    """
    assert _unwaited_fixture_yields(ast.parse(_SYNTHETIC_TEARDOWN_WAIT_FIXTURE_SOURCE)) == [
        (8, "vault_services")
    ]


# The same wait written through the module's own adapter, which is how a module
# with many wait sites actually spells it. Both the ingest and the wait are one
# call away from the fixture here.
_SYNTHETIC_ADAPTED_FIXTURE_SOURCE: Final[str] = textwrap.dedent(
    """
    def _parse(result):
        return result if isinstance(result, dict) else json.loads(result)


    async def _ingest_local_file(tmp_path, name, body):
        return _parse(await ingest_document("v", str(tmp_path / name), "markdown"))


    async def _await_document_idle(services, doc_id):
        async def fetch():
            return _parse(await get_document("v", doc_id))

        return await await_tool_idle(fetch, doc_id, service=services.ingestion_service)


    @pytest.fixture
    async def seeded_vault(services, tmp_path):
        doc = await _ingest_local_file(tmp_path, "sample.md", "# S")
        await _await_document_idle(services, doc["id"])
        yield services


    @pytest.fixture
    async def unsettled_vault(services, tmp_path):
        doc = _parse(await _ingest_local_file(tmp_path, "other.md", "# O"))
        yield services
    """
)


def test_unwaited_fixture_detector_resolves_a_wait_through_a_module_adapter() -> None:
    """A wait reached through the module's own adapter still counts as a wait.

    Both hops matter here and they pull opposite ways: the ingest resolves
    through ``_ingest_local_file``, which is what puts a fixture in scope at
    all, and the wait resolves through ``_await_document_idle``, which takes
    it back out. A walk that resolved only the first reports every fixture in
    a module that spells its waits this way -- which is the common spelling,
    so the false-positive rate would be most of them.

    The two fixtures share a module and differ only in the second hop, which
    is what makes the discrimination checkable. A rival that exempts a fixture
    for calling *any* module-local helper is green on ``seeded_vault`` alone
    -- and that rival is the false-negative form of the same mistake, since a
    fixture calling only the ingest helper is precisely the shape this arm
    exists to report. Asserting the pair excludes it; asserting the exempted
    one by itself does not.

    ``unsettled_vault`` calls ``_parse`` as well as the ingest helper, which
    closes the nearer rival: *exempt on a call to any module-local helper that
    is not an ingest helper*. Without that call the reported fixture calls
    nothing but the ingest helper, so that rival is green on the pair too --
    and on the live tree it would exempt both real fixtures, since both spell
    their ingest through ``_parse``. The exemption has to be attributable to
    the adapter consulting the claim and to nothing else about the shape of
    the call.
    """
    assert _unwaited_fixture_yields(ast.parse(_SYNTHETIC_ADAPTED_FIXTURE_SOURCE)) == [
        (27, "unsettled_vault")
    ]


# A fixture whose only ingestion happens after the yield, on the teardown side.
# Nothing is in flight at the moment control is handed on.
_SYNTHETIC_TEARDOWN_INGEST_FIXTURE_SOURCE: Final[str] = textwrap.dedent(
    """
    @pytest.fixture
    async def vault_services(config):
        async with initialize_services_for_test(config) as services:
            yield services
            await ingest_document("test_vault", "test/teardown.md", "markdown")
    """
)


def test_unwaited_fixture_detector_ignores_an_ingest_after_the_yield() -> None:
    """An ingestion the yield precedes is not a document the tests received.

    The ordering condition, and the same one the arm above carries: without
    it the walk keys on mere co-occurrence and reports a fixture that hands
    its tests nothing at all. The remedy it would print -- wait before the
    yield -- would name a document that does not yet exist.
    """
    assert _unwaited_fixture_yields(ast.parse(_SYNTHETIC_TEARDOWN_INGEST_FIXTURE_SOURCE)) == []


# An ingesting context manager: it ingests and yields, exactly as the fixture
# does, and carries no fixture decorator. The whole of the difference is the
# decorator, which is what makes it the control for that condition.
_SYNTHETIC_INGESTING_NON_FIXTURE_SOURCE: Final[str] = textwrap.dedent(
    """
    @contextlib.asynccontextmanager
    async def seeded_document(config):
        doc = _parse(await ingest_document("test_vault", "test/sample.md", "markdown"))
        yield doc
    """
)


def test_unwaited_fixture_detector_ignores_a_plain_ingesting_helper() -> None:
    """Only fixtures are in scope, and the decorator is what says so.

    A helper that ingests and yields hands its document to whoever entered
    it -- one call site, which can wait around it; a fixture hands its
    document to every test that requests it, none of which can. That
    asymmetry is the whole reason this arm keys on the decorator.

    The control ingests *and* yields, so its absence from the report is
    attributable to the missing fixture decorator and to nothing else. A
    version that merely returned would be excluded by the yield condition
    instead, leaving a walk that ignores the decorator entirely green here --
    which is to say, leaving the condition this test is named for unpinned.
    """
    assert _unwaited_fixture_yields(ast.parse(_SYNTHETIC_INGESTING_NON_FIXTURE_SOURCE)) == []


def test_unwaited_fixture_allowlist_has_no_stale_entries() -> None:
    """Every unwaited-fixture allowlist entry names a fixture the walk still reports.

    Carried for the reason the two staleness tests above give: an entry whose
    fixture was since given its wait, renamed or deleted lingers silently, and
    the next reader inherits a waiver for something already fixed.
    """
    stale: list[str] = []
    for rel, names in UNWAITED_FIXTURE_YIELD_ALLOWLIST.items():
        path = REPO_ROOT / rel
        reported = (
            {name for _, name in _unwaited_fixture_yields(ast.parse(path.read_bytes()))}
            if path.exists()
            else set()
        )
        stale.extend(f"{rel}: {name}" for name in names if name not in reported)

    assert not stale, (
        "UNWAITED_FIXTURE_YIELD_ALLOWLIST entries that the walk no longer "
        f"reports ({len(stale)}): {', '.join(stale)}. Drop each one — the "
        "fixture it exempted now waits, so the entry waives nothing."
    )
