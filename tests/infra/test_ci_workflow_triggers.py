"""Structural gate for the CI workflow's trigger, cancellation, and scan shape.

Locks the parts of ``.github/workflows/ci.yml`` that decide *when* CI runs and
*what tree each run sees*, so the merge queue that lands pull requests onto the
default branch is actually served rather than silently bypassed. Each invariant
below guards a failure that reports green:

* **One run per push.** A bare ``push:`` trigger fires alongside ``pull_request``
  on the same commit, so every push to a pull-request branch runs the whole
  workflow twice. Restricting ``push`` to the default branch and adding
  ``merge_group`` gives one run per push, one run per queue entry, and one run
  per landing.
* **No trigger carries a filter it is not entitled to.** ``push`` is
  branch-restricted by design; the other two must be unconditional. Every
  narrowing key -- ``paths``, ``types``, ``branches`` -- leaves the event set and
  the push branch list exactly as a correct workflow's, so it survives every
  other assertion here untouched while silently deciding CI does not run. A
  ``types: [opened]`` on ``pull_request`` stops CI on every later push to a
  branch; a ``paths`` key stops the repo-wide gates, which read Markdown and the
  git tree and can legitimately fail on a documentation-only change.
* **Cancellation reaches only an ephemeral run.** Two independent ways to break
  that, so both are asserted. A ``cancel-in-progress`` that consults
  ``github.ref``, or that carries a literal ``true`` operand, cancels beyond the
  events it names. And a concurrency ``group`` that keys a push by ref rather
  than by commit puts every landing in one group, where the forge holds one
  running and one pending run and drops the pending one when a third arrives --
  taking away the ``main`` push run whose existence the branch-protection
  document justifies.
* **The secret scan has an arm per event, and each ranged arm scans something.**
  The scan branches on the event name to build a commit range. An event with no
  arm of its own falls to the default, which scans the full commit history of
  every branch rather than what this event introduced -- so it can fail on a
  secret that has sat on the default branch for months, which no pull request
  ever exercises. And a range that resolves to no commits is not a scan at all:
  the scanner exits zero on an empty range and on an invalid one alike, so a
  required check can report success having examined nothing.

Two further checks reconcile the captured ruleset in
``docs/process/branch_protection.md``. Every required status-check context must
name a real job, so a renamed job cannot quietly stop being required; and the
document's human-readable table of required checks must agree with the
machine-readable capture beside it, which is the direction that has actually
drifted — the document asserted in prose something its own JSON contradicted.

These checks read the tracked workflow YAML and the tracked process document
only -- no forge API, no Actions runner -- so they run in the ordinary Python
test job. The controls at the bottom drive each detector against synthetic input
so a matcher that silently matches nothing cannot pass the gate vacuously.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Final

import yaml

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR: Final[Path] = REPO_ROOT / ".github" / "workflows"
CI_WORKFLOW: Final[Path] = WORKFLOWS_DIR / "ci.yml"
BRANCH_PROTECTION_DOC: Final[Path] = REPO_ROOT / "docs" / "process" / "branch_protection.md"

# The exact trigger set that yields one run per push. ``pull_request`` covers
# work in progress, ``merge_group`` covers the queue's merged-tree run, and
# ``push`` -- restricted to the branches below -- covers the landing itself and
# any direct push made under the ruleset's admin bypass.
EXPECTED_TRIGGERS: Final[frozenset[str]] = frozenset({"pull_request", "merge_group", "push"})
EXPECTED_PUSH_BRANCHES: Final[tuple[str, ...]] = ("main",)

# Filter keys each trigger may carry. ``push`` is branch-restricted by design;
# the other two must be unconditional, because any narrowing key on them leaves
# every other assertion in this file satisfied while deciding CI does not run.
_ALLOWED_TRIGGER_FILTERS: Final[dict[str, frozenset[str]]] = {
    "push": frozenset({"branches"}),
    "pull_request": frozenset(),
    "merge_group": frozenset(),
}

# The events whose runs may be cancelled when superseded: a pull-request run, and
# a queue run orphaned by a dequeue-and-requeue on an unchanged base. A ``main``
# push run is never cancellable.
CANCELLABLE_EVENTS: Final[frozenset[str]] = frozenset({"pull_request", "merge_group"})

# An event-name equality test inside a workflow expression. Whitespace is elastic
# because the expression is hand-written YAML; the operands are not.
_CANCEL_EVENT_RE: Final[re.Pattern[str]] = re.compile(r"github\.event_name\s*==\s*'(\w+)'")

# A bare boolean operand. ``... || true`` names the right events and then cancels
# everything regardless, which reads as correct to anyone checking the event list.
_LITERAL_OPERAND_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![\w.])(?:true|false)(?![\w.])", re.IGNORECASE
)

# A ``push``-conditional inside the concurrency group expression.
_GROUP_PUSH_ARM_RE: Final[re.Pattern[str]] = re.compile(r"github\.event_name\s*==\s*'push'")

# A ``case`` arm label on a line of its own -- ``pull_request)``, ``push)``,
# ``*)``. Anchored to the whole line so a subshell or a glob inside an arm body
# cannot be mistaken for an arm of its own.
_CASE_ARM_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*([A-Za-z_*][A-Za-z0-9_|*]*)\)\s*$", re.MULTILINE
)

# A fenced JSON block in a Markdown document.
_JSON_BLOCK_RE: Final[re.Pattern[str]] = re.compile(r"```json\n(.*?)\n```", re.DOTALL)

# The ruleset whose captured definition the process document reproduces.
_RULESET_NAME: Final[str] = "main-protection"

# A row of the required-status-checks table: a backticked job name in the first
# column, followed by its purpose. Anchored to the line so prose elsewhere in the
# document that happens to backtick a job name is not read as a table row.
_REQUIRED_CHECK_ROW_RE: Final[re.Pattern[str]] = re.compile(
    r"^\|\s*`([^`]+)`\s*\|[^|]*\|\s*$", re.MULTILINE
)


# ---------------------------------------------------------------------------
# Detectors (pure over a parsed workflow, a script, or document text, so the
# controls at the bottom can drive them directly)
# ---------------------------------------------------------------------------


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _on_block(workflow: dict) -> dict:
    """Return the workflow trigger mapping.

    PyYAML parses the bare ``on:`` key as the boolean ``True`` under YAML 1.1
    truthy-token rules, so the trigger block is keyed by ``True`` rather than the
    string ``"on"``.
    """
    block = workflow.get(True)
    if block is None:
        block = workflow.get("on")
    return block or {}


def _trigger_events(workflow: dict) -> set[str]:
    """The set of events the workflow runs on."""
    return set(_on_block(workflow))


def _push_branches(workflow: dict) -> list[str]:
    """Branches the ``push`` trigger is restricted to.

    An unrestricted ``push:`` parses as ``None`` and yields the empty list, which
    is the duplicate-run shape this gate rejects -- not an absent trigger.
    """
    push = _on_block(workflow).get("push")
    if not isinstance(push, dict):
        return []
    return list(push.get("branches") or [])


def _disallowed_trigger_filters(workflow: dict) -> dict[str, list[str]]:
    """``{event: [keys]}`` for each trigger carrying a filter it may not carry.

    Generic rather than a list of known-bad keys, because the failure is the
    *category*: every narrowing key leaves the event set and the push branch list
    exactly as a correct workflow's, so it passes every other assertion in this
    file while deciding CI does not run. ``types: [opened]`` on ``pull_request``
    stops CI on every later push to a branch; ``paths`` stops the repo-wide gates,
    which read Markdown and the git tree and can legitimately fail on a
    documentation-only change.
    """
    filtered: dict[str, list[str]] = {}
    for event, spec in _on_block(workflow).items():
        if not isinstance(spec, dict):
            continue
        allowed = _ALLOWED_TRIGGER_FILTERS.get(str(event), frozenset())
        extra = sorted(str(k) for k in spec if k not in allowed)
        if extra:
            filtered[str(event)] = extra
    return filtered


def _concurrency_group(workflow: dict) -> str:
    """The raw concurrency ``group`` expression, as written."""
    return str((workflow.get("concurrency") or {}).get("group", ""))


def _cancel_in_progress(workflow: dict) -> str:
    """The raw ``cancel-in-progress`` expression, as written."""
    concurrency = workflow.get("concurrency") or {}
    return str(concurrency.get("cancel-in-progress", ""))


def _cancellable_events(expression: str) -> set[str]:
    """The event names a ``cancel-in-progress`` expression tests for."""
    return set(_CANCEL_EVENT_RE.findall(expression))


def _cancels_exactly(expression: str, events: frozenset[str]) -> bool:
    """True when the expression cancels those events and nothing else.

    Three independent ways to fail, all of which leave a plausible-looking
    expression. Naming the wrong set of events. Consulting ``github.ref``, which
    is the pre-queue form: it evaluates true for the queue's temporary ref and
    for every branch, so it reaches runs the event list says it does not.
    Carrying a literal ``true`` or ``false`` operand, which short-circuits the
    whole condition while leaving the event names in place to reassure a reader.
    """
    return (
        _cancellable_events(expression) == set(events)
        and "github.ref" not in expression
        and not _LITERAL_OPERAND_RE.search(expression)
    )


def _group_keys_push_by_commit(expression: str) -> bool:
    """True when the concurrency group distinguishes pushes by commit, not by ref.

    A per-ref group on the default branch holds one running and one pending run,
    so a third landing inside the test job's window drops the second one's
    pending run entirely. Both stated reasons for running on the default branch
    at all -- an admin-bypass push, and a coverage artifact belonging to a
    default-branch commit -- die with that dropped run.
    """
    return (
        "github.sha" in expression
        and "github.ref" in expression
        and bool(_GROUP_PUSH_ARM_RE.search(expression))
    )


def _steps(job: object) -> list[dict]:
    if not isinstance(job, dict):
        return []
    return [s for s in (job.get("steps") or []) if isinstance(s, dict)]


def _job_run_text(job: dict) -> str:
    """All ``run:`` script of a job, joined in step order."""
    return "\n".join(s.get("run", "") for s in _steps(job))


def _uncommented_run_text(job: dict) -> str:
    """``_job_run_text`` with shell ``#`` comment lines removed, so a flag named
    only in a comment explaining its absence is not read as a live argument."""
    return "\n".join(
        line for line in _job_run_text(job).splitlines() if not line.lstrip().startswith("#")
    )


def _case_arm_labels(run: str) -> set[str]:
    """The labels of the ``case`` arms in a shell script.

    An event absent from this set has no arm of its own and therefore falls to
    the default arm.
    """
    return {m.group(1) for m in _CASE_ARM_RE.finditer(run)}


def _case_arm(run: str, label: str) -> str:
    """The body of the ``case`` arm labelled ``label``: from ``label)`` to the
    arm's closing ``;;``, so a per-arm assertion cannot be satisfied by another
    arm's text."""
    start = run.index(f"{label})")
    return run[start : run.index(";;", start)]


def _step_env(job: dict, run_marker: str) -> dict:
    """The ``env`` mapping of the step whose script contains ``run_marker``."""
    for step in _steps(job):
        if run_marker in step.get("run", ""):
            return step.get("env") or {}
    return {}


def _secret_scan_job(workflow: dict) -> dict:
    """The job that runs the secret scanner, located by what it does rather than
    by its name, so a rename does not silently empty the check."""
    for job in (workflow.get("jobs") or {}).values():
        if isinstance(job, dict) and "gitleaks detect" in _job_run_text(job):
            return job
    return {}


def _captured_ruleset(text: str) -> dict:
    """The captured live-ruleset definition reproduced in a Markdown document."""
    for match in _JSON_BLOCK_RE.finditer(text):
        try:
            candidate = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("name") == _RULESET_NAME:
            return candidate
    return {}


def _rule(ruleset: dict, rule_type: str) -> dict:
    for rule in ruleset.get("rules") or []:
        if isinstance(rule, dict) and rule.get("type") == rule_type:
            return rule
    return {}


def _required_contexts(ruleset: dict) -> list[str]:
    """Every status-check context the captured ruleset requires."""
    parameters = _rule(ruleset, "required_status_checks").get("parameters") or {}
    return [
        check["context"]
        for check in parameters.get("required_status_checks") or []
        if isinstance(check, dict) and "context" in check
    ]


def _documented_required_jobs(text: str) -> list[str]:
    """Job names from the required-status-checks table's first column.

    The document states in prose that these names match the workflow's job keys
    verbatim. That claim is only as good as the table's agreement with the
    captured ruleset beside it, which is the pairing asserted below.
    """
    rows = _REQUIRED_CHECK_ROW_RE.findall(text)
    return [name for name in rows if name not in {"Job"}]


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_ci_triggers_are_pull_request_merge_group_and_push_to_main() -> None:
    """CI runs on pull requests, on merge-group entries, and on pushes to the
    default branch only.

    Asserted by equality rather than containment: an extra unrestricted trigger
    is exactly the regression -- it doubles every run on a pull-request branch --
    and a containment assertion would not see it.
    """
    workflow = _load(CI_WORKFLOW)
    assert _trigger_events(workflow) == set(EXPECTED_TRIGGERS), (
        f"ci.yml triggers must be exactly {sorted(EXPECTED_TRIGGERS)}; "
        f"found {sorted(_trigger_events(workflow))}"
    )
    assert tuple(_push_branches(workflow)) == EXPECTED_PUSH_BRANCHES, (
        "ci.yml's push trigger must be restricted to "
        f"{list(EXPECTED_PUSH_BRANCHES)}; found {_push_branches(workflow)}. "
        "An unrestricted push trigger fires alongside pull_request on the same "
        "commit, running the whole workflow twice per push."
    )
    assert _disallowed_trigger_filters(workflow) == {}, (
        "a ci.yml trigger carries a filter it may not: push may be restricted to "
        "branches, and pull_request and merge_group must be unconditional. Any "
        "narrowing key leaves the event set and the branch list looking correct "
        "while deciding CI does not run. Offenders: "
        f"{_disallowed_trigger_filters(workflow)}"
    )


def test_concurrency_group_distinguishes_pushes_by_commit() -> None:
    """Each push to the default branch gets its own concurrency group.

    Separable from the cancellation check below and not implied by it. The forge
    holds one running and one pending run per group, so a shared per-ref group on
    the default branch drops the middle run of three quick landings whatever
    ``cancel-in-progress`` says. Both reasons the branch-protection document
    gives for running on the default branch at all die with that dropped run.
    """
    group = _concurrency_group(_load(CI_WORKFLOW))
    assert _group_keys_push_by_commit(group), (
        "the concurrency group must key a push by github.sha and everything else "
        f"by github.ref; found: {group!r}"
    )


def test_only_pull_request_and_merge_group_runs_are_cancelled() -> None:
    """A default-branch push run is never cancelled; ephemeral runs are.

    The queue ref carries the pull request number, so a *later* entry never
    shares a group with an earlier one and cannot cancel it. The queue run that
    is reachable here is the orphan left by a dequeue-and-requeue on an unchanged
    base, which must be cancelled: leaving it running parks the live re-queued
    entry behind it for a full CI duration.
    """
    expression = _cancel_in_progress(_load(CI_WORKFLOW))
    assert _cancels_exactly(expression, CANCELLABLE_EVENTS), (
        "ci.yml's cancel-in-progress must name exactly "
        f"{sorted(CANCELLABLE_EVENTS)}, must not consult github.ref, and must "
        f"carry no literal boolean operand; found: {expression!r}"
    )


def test_secret_scan_has_an_arm_for_every_trigger_event() -> None:
    """The secret scan branches on every event the workflow triggers on.

    An event with no arm of its own falls to the default, which scans the whole
    full commit history of every branch rather than the range this event
    introduced, so it can fail on a secret that has sat on the default branch for
    months. No pull request ever exercises that mode.
    """
    workflow = _load(CI_WORKFLOW)
    job = _secret_scan_job(workflow)
    assert job, "no job running `gitleaks detect` was found in ci.yml"

    arms = _case_arm_labels(_job_run_text(job))
    missing = _trigger_events(workflow) - arms
    assert not missing, (
        f"the secret scan has no case arm for {sorted(missing)}; those events "
        "fall to the default arm, which scans full history instead of the "
        f"commit range this event introduced. Arms present: {sorted(arms)}"
    )


def test_secret_scan_refuses_a_range_that_selects_nothing() -> None:
    """A ranged scan resolves its range and fails when it selects no commits.

    The scanner exits zero on an empty range and on an invalid one alike, so
    without this the required check reports success having examined nothing. Two
    reachable ways in. A merge-shaped head under a first-parent range selects
    nothing at all, which is decided by the queue rule's ``merge_method`` rather
    than by anything in this file. And a mis-wired commit id makes the range
    invalid rather than empty, which is equally green.

    Asserted on the script rather than on a rendered range, because the guard is
    what has to exist; the ranges themselves are covered per arm above. Comment
    lines are stripped first: this file's own explanation of why ``--no-merges``
    is absent names the flag, and a raw substring scan reads that as its presence.
    """
    run = _uncommented_run_text(_secret_scan_job(_load(CI_WORKFLOW)))
    assert "rev-list --count" in run, (
        "the secret scan must resolve its range with `git rev-list --count` "
        "before scanning, so an empty or invalid range fails loudly instead of "
        f"reporting a scan that examined nothing. Script: {run!r}"
    )
    assert "--no-merges" not in run, (
        "the ranged arms must not pass --no-merges: against a merge-shaped head "
        "a first-parent range then selects zero commits, which is the empty scan "
        f"the guard exists to catch. Script: {run!r}"
    )


def test_secret_scan_merge_group_arm_scopes_to_the_group_commits() -> None:
    """The merge-group arm scans the group's own commits, not the whole tree.

    An arm that exists but builds no range satisfies the arm-coverage check above
    while defeating its purpose, so the range and the payload wiring are asserted
    separately here.
    """
    job = _secret_scan_job(_load(CI_WORKFLOW))
    run = _job_run_text(job)
    arm = _case_arm(run, "merge_group")

    assert "MERGE_GROUP_BASE_SHA" in arm and "MERGE_GROUP_HEAD_SHA" in arm, (
        f"the merge_group arm must build its range from the group's base and "
        f"head commits; arm body: {arm!r}"
    )
    assert ".." in arm, f"the merge_group arm must pass a commit range; arm body: {arm!r}"
    # Containment says nothing about position. A reversed range names both
    # commits and still reads as a range, but resolves to no commits at all, so
    # the scan would pass vacuously and a secret could land through the queue.
    assert arm.index("MERGE_GROUP_BASE_SHA") < arm.index("MERGE_GROUP_HEAD_SHA"), (
        "the merge_group arm's range must run base..head, not head..base; a "
        f"reversed range selects no commits and scans nothing. Arm body: {arm!r}"
    )

    env = _step_env(job, "gitleaks detect")
    assert "merge_group.base_sha" in str(env.get("MERGE_GROUP_BASE_SHA", "")), (
        "MERGE_GROUP_BASE_SHA must be wired to the merge_group payload's base "
        f"commit; step env: {env}"
    )
    assert "merge_group.head_sha" in str(env.get("MERGE_GROUP_HEAD_SHA", "")), (
        "MERGE_GROUP_HEAD_SHA must be wired to the merge_group payload's head "
        f"commit; step env: {env}"
    )


def test_captured_ruleset_contexts_are_real_ci_jobs() -> None:
    """Every required status check names a job this workflow actually defines.

    The process document is the source of truth for the ruleset, and the ruleset
    names its checks by job name. A renamed job leaves the ruleset waiting on a
    context nothing will ever report, which stalls the queue rather than failing
    it.
    """
    ruleset = _captured_ruleset(BRANCH_PROTECTION_DOC.read_text(encoding="utf-8"))
    jobs = set(_load(CI_WORKFLOW).get("jobs") or {})
    unknown = [c for c in _required_contexts(ruleset) if c not in jobs]
    assert not unknown, (
        f"captured ruleset requires status checks with no matching ci.yml job: "
        f"{unknown}. Jobs defined: {sorted(jobs)}"
    )


def test_documented_required_checks_match_the_captured_ruleset() -> None:
    """The document's required-checks table and its captured ruleset agree.

    The reconciliation above only catches a context naming no job. It cannot
    catch the direction that actually drifts: the human-readable table and the
    machine-readable capture disagreeing with each other, which leaves the
    document asserting in prose something its own JSON block contradicts.
    """
    text = BRANCH_PROTECTION_DOC.read_text(encoding="utf-8")
    documented = sorted(_documented_required_jobs(text))
    captured = sorted(_required_contexts(_captured_ruleset(text)))
    assert documented, (
        "no required-status-check table rows were found in "
        f"{BRANCH_PROTECTION_DOC.name}; the comparison below would be vacuous"
    )
    assert documented == captured, (
        "the required-status-checks table and the captured ruleset disagree. "
        f"Table: {documented}. Captured: {captured}."
    )


def test_captured_ruleset_block_parses_and_requires_checks() -> None:
    """The captured ruleset is present, parses, and requires at least one check.

    Blind-spot control for the check above, whose quantifier is vacuously true
    when the extractor returns nothing.
    """
    ruleset = _captured_ruleset(BRANCH_PROTECTION_DOC.read_text(encoding="utf-8"))
    assert ruleset, (
        f"no captured ruleset named {_RULESET_NAME!r} found in {BRANCH_PROTECTION_DOC.name}"
    )
    assert _required_contexts(ruleset), (
        "the captured ruleset requires no status checks at all, which would make "
        "the context reconciliation above pass vacuously"
    )


# ---------------------------------------------------------------------------
# Anti-coincidental-pass controls
#
# Prove each detector fires on the regression it exists to catch and clears the
# legitimate form, NOT that the committed workflow happens to be shaped right.
# Without these, a matcher that silently matches nothing passes every gate above.
# ---------------------------------------------------------------------------


_SYNTHETIC_DUPLICATE_TRIGGER: Final[str] = "on:\n  push:\n  pull_request:\n"

_SYNTHETIC_SINGLE_TRIGGER: Final[str] = (
    "on:\n  pull_request:\n  merge_group:\n  push:\n    branches: [main]\n"
)

_SYNTHETIC_PATH_GATED_TRIGGER: Final[str] = (
    "on:\n  pull_request:\n  merge_group:\n  push:\n    branches: [main]\n    paths: ['sage/**']\n"
)

# The rival the review found: `pull_request` narrowed to a single activity type,
# so no later push to a branch runs CI. Every other trigger assertion holds.
_SYNTHETIC_TYPE_GATED_TRIGGER: Final[str] = (
    "on:\n  pull_request:\n    types: [opened]\n  merge_group:\n  push:\n    branches: [main]\n"
)

_SYNTHETIC_REQUIRED_CHECK_TABLE: Final[str] = (
    "| Job | Purpose |\n"
    "|---|---|\n"
    "| `test` | Full suite. |\n"
    "| `lint` | Formatting. |\n"
    "\nProse that mentions `eslint` outside the table must not be read as a row.\n"
)

_SYNTHETIC_SCAN_WITHOUT_MERGE_GROUP: Final[str] = (
    'case "$EVENT_NAME" in\n'
    "  pull_request)\n"
    '    log_opts="a..b"\n'
    "    ;;\n"
    "  push)\n"
    '    log_opts="c..d"\n'
    "    ;;\n"
    "  *)\n"
    '    log_opts=""\n'
    "    ;;\n"
    "esac\n"
)

_SYNTHETIC_CAPTURED_RULESET: Final[str] = (
    "```json\n"
    "{\n"
    '  "name": "main-protection",\n'
    '  "rules": [\n'
    "    {\n"
    '      "type": "required_status_checks",\n'
    '      "parameters": {\n'
    '        "required_status_checks": [{"context": "no-such-job"}]\n'
    "      }\n"
    "    }\n"
    "  ]\n"
    "}\n"
    "```\n"
)


def test_trigger_detector_reads_the_truthy_on_key() -> None:
    """``_on_block`` finds the trigger block despite PyYAML keying bare ``on:``
    as the boolean ``True`` -- otherwise every trigger assertion above compares
    against an empty set."""
    workflow = yaml.safe_load(_SYNTHETIC_SINGLE_TRIGGER)
    assert _trigger_events(workflow) == {"pull_request", "merge_group", "push"}


def test_trigger_detector_flags_an_unrestricted_push() -> None:
    """The duplicate-run shape is caught: a bare ``push:`` yields no branch
    restriction, and the trigger set is missing ``merge_group``."""
    workflow = yaml.safe_load(_SYNTHETIC_DUPLICATE_TRIGGER)
    assert _push_branches(workflow) == []
    assert _trigger_events(workflow) != set(EXPECTED_TRIGGERS)


def test_trigger_detector_clears_the_restricted_push() -> None:
    """The legitimate form reads back as restricted to the default branch."""
    workflow = yaml.safe_load(_SYNTHETIC_SINGLE_TRIGGER)
    assert _push_branches(workflow) == ["main"]


def test_cancel_detector_rejects_the_ref_keyed_expression() -> None:
    """The pre-queue expression is rejected. It names no event at all and reaches
    every branch, including the default one this must never cancel."""
    assert not _cancels_exactly("${{ github.ref != 'refs/heads/main' }}", CANCELLABLE_EVENTS)


def test_cancel_detector_rejects_a_hybrid_expression() -> None:
    """Naming the events is not enough: an expression that still consults
    ``github.ref`` is rejected, so a partial edit cannot satisfy the gate."""
    assert not _cancels_exactly(
        "${{ (github.event_name == 'pull_request' || github.event_name == 'merge_group') "
        "&& github.ref != 'refs/heads/main' }}",
        CANCELLABLE_EVENTS,
    )


def test_cancel_detector_rejects_a_literal_operand() -> None:
    """The rival the review named: an expression that lists the right events and
    then cancels everything anyway. It reads as correct to anyone checking the
    event list, so the detector rejects a bare boolean operand outright."""
    assert not _cancels_exactly(
        "${{ github.event_name == 'pull_request' || github.event_name == 'merge_group' || true }}",
        CANCELLABLE_EVENTS,
    )


def test_cancel_detector_rejects_a_narrower_event_set() -> None:
    """Cancelling only pull requests leaves an orphaned queue run in place, which
    parks the live re-queued entry behind it, so the set is asserted by equality
    rather than by containment."""
    assert not _cancels_exactly("${{ github.event_name == 'pull_request' }}", CANCELLABLE_EVENTS)


def test_cancel_detector_accepts_the_event_keyed_expression() -> None:
    """The legitimate form clears."""
    assert _cancels_exactly(
        "${{ github.event_name == 'pull_request' || github.event_name == 'merge_group' }}",
        CANCELLABLE_EVENTS,
    )


def test_group_detector_flags_a_push_keyed_by_ref() -> None:
    """A per-ref group is caught and the per-commit form clears. The first is the
    shape under which the forge drops the middle run of three quick landings."""
    assert not _group_keys_push_by_commit("${{ github.workflow }}-${{ github.ref }}")
    assert _group_keys_push_by_commit(
        "${{ github.workflow }}-${{ github.event_name == 'push' && github.sha || github.ref }}"
    )


def test_case_arm_detector_flags_a_missing_arm() -> None:
    """A scan script with no merge-group arm is caught, and the arms that are
    present are read correctly."""
    arms = _case_arm_labels(_SYNTHETIC_SCAN_WITHOUT_MERGE_GROUP)
    assert arms == {"pull_request", "push", "*"}
    assert "merge_group" not in arms


def test_trigger_filter_detector_flags_a_path_gated_trigger() -> None:
    """A trigger carrying a path filter is caught, and the legitimate form is
    not. The other two assertions are the point: the gated workflow's event set
    and branch list are identical to a correct one's, so every other trigger
    assertion passes straight over it."""
    gated = yaml.safe_load(_SYNTHETIC_PATH_GATED_TRIGGER)
    assert _disallowed_trigger_filters(gated) == {"push": ["paths"]}
    assert _trigger_events(gated) == set(EXPECTED_TRIGGERS)
    assert _push_branches(gated) == ["main"]
    assert _disallowed_trigger_filters(yaml.safe_load(_SYNTHETIC_SINGLE_TRIGGER)) == {}


def test_trigger_filter_detector_flags_a_narrowed_pull_request() -> None:
    """The rival the review found: ``types: [opened]`` stops CI on every later
    push to a branch while leaving the event set, the branch list and the path
    filters all correct. Only the entitlement check sees it."""
    gated = yaml.safe_load(_SYNTHETIC_TYPE_GATED_TRIGGER)
    assert _disallowed_trigger_filters(gated) == {"pull_request": ["types"]}
    assert _trigger_events(gated) == set(EXPECTED_TRIGGERS)
    assert _push_branches(gated) == ["main"]


def test_concurrency_group_detector_reads_the_group_expression() -> None:
    """The group detector reads the expression rather than returning a constant
    empty string, which would make the assertion above fail open on a rewrite."""
    assert "github.ref" in _concurrency_group(
        yaml.safe_load("concurrency:\n  group: w-${{ github.ref }}\n")
    )
    assert "github.ref" not in _concurrency_group(
        yaml.safe_load("concurrency:\n  group: ${{ github.workflow }}\n")
    )


def test_required_check_table_detector_reads_the_table() -> None:
    """The table parser finds the backticked job names and ignores the header
    row, so the table-versus-capture comparison cannot pass on an empty list."""
    assert _documented_required_jobs(_SYNTHETIC_REQUIRED_CHECK_TABLE) == ["test", "lint"]


def test_captured_ruleset_detector_flags_an_unknown_context() -> None:
    """A captured ruleset requiring a context with no matching job is caught, and
    the extractor reads the block rather than returning nothing."""
    ruleset = _captured_ruleset(_SYNTHETIC_CAPTURED_RULESET)
    assert ruleset.get("name") == _RULESET_NAME
    assert _required_contexts(ruleset) == ["no-such-job"]
