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
* **No trigger is path-filtered.** A ``paths`` key leaves the event set and the
  branch list identical, so it survives every other trigger assertion untouched
  while stopping CI on most pushes. The repo-wide gates read Markdown and the git
  tree, so a documentation-only change can legitimately fail them and must run.
* **Cancellation reaches only a superseded pull-request run.** Two independent
  ways to break that. A ``cancel-in-progress`` keyed on ``github.ref`` evaluates
  true for the queue's temporary ``gh-readonly-queue/...`` ref, so a later entry
  can cancel the run the merge is waiting on. And a concurrency ``group`` that
  drops ``github.ref`` collapses every pull request into one group, so a
  correct-looking event-gated cancellation reaches across pull requests. Both are
  asserted, because either alone passes while the other is breached.
* **Path filtering sees the merge group's base.** ``dorny/paths-filter`` reads
  the merge-group base and head commits from the webhook payload but does not
  fetch them. Under the default depth-1 checkout that base object is absent, the
  filter mis-reports, and the frontend lint job that depends on it is skipped --
  which the forge counts as a *satisfied* required check. The gate would be
  bypassed on every queued landing with nothing red to show for it, so the
  requirement is a full-history checkout in any job that runs the filter.
* **The secret scan has an arm per event.** The scan branches on the event name
  to build a commit range. An event with no arm of its own falls to the default,
  which scans the whole working tree instead of the range -- a mode no pull
  request ever exercises, and one that can fail on content already on the default
  branch.

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

# Action name fragments. Matched as substrings of a step's ``uses``, so a
# version bump (``@v4`` to ``@v5``) does not silently drop a job from the scan.
_PATHS_FILTER_ACTION: Final[str] = "dorny/paths-filter"
_CHECKOUT_ACTION: Final[str] = "actions/checkout"

# The only ``fetch-depth`` that leaves a merge-group base commit resolvable.
_FULL_HISTORY: Final[int] = 0

# A ``cancel-in-progress`` expression gated on the triggering event being a pull
# request. The whitespace is elastic because the expression is hand-written YAML,
# but the operands are not: an expression that also consults ``github.ref`` is
# rejected outright below, since that is the form which cancels queue runs.
_EVENT_GATED_CANCEL_RE: Final[re.Pattern[str]] = re.compile(
    r"github\.event_name\s*==\s*'pull_request'"
)

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


def _path_filtered_triggers(workflow: dict) -> dict[str, list[str]]:
    """``{event: [filter keys]}`` for each trigger carrying a path filter.

    Workflow-level path filtering is a different thing from the job-level
    filtering that gates the frontend jobs, and it is not wanted here: the
    public-posture and collection-integrity gates read Markdown and the git tree,
    so a documentation-only change can legitimately fail them and must still run.
    A ``paths`` key also survives every branch and event assertion untouched,
    which is why it needs its own check.
    """
    filtered: dict[str, list[str]] = {}
    for event, spec in _on_block(workflow).items():
        if not isinstance(spec, dict):
            continue
        keys = [k for k in ("paths", "paths-ignore") if k in spec]
        if keys:
            filtered[str(event)] = keys
    return filtered


def _concurrency_group(workflow: dict) -> str:
    """The raw concurrency ``group`` expression, as written."""
    return str((workflow.get("concurrency") or {}).get("group", ""))


def _cancel_in_progress(workflow: dict) -> str:
    """The raw ``cancel-in-progress`` expression, as written."""
    concurrency = workflow.get("concurrency") or {}
    return str(concurrency.get("cancel-in-progress", ""))


def _cancels_only_pull_requests(expression: str) -> bool:
    """True when the expression can only ever cancel a pull-request run.

    Both halves matter. The event-name test is what limits cancellation to pull
    requests; the ``github.ref`` exclusion is what rejects the ref-keyed form,
    which evaluates true for the queue's temporary ref and so leaves a
    merge-group run cancellable.
    """
    return bool(_EVENT_GATED_CANCEL_RE.search(expression)) and "github.ref" not in expression


def _iter_workflow_files() -> list[Path]:
    """Every committed workflow definition under ``.github/workflows/``."""
    return sorted(
        p for p in WORKFLOWS_DIR.iterdir() if p.is_file() and p.suffix in (".yml", ".yaml")
    )


def _steps(job: object) -> list[dict]:
    if not isinstance(job, dict):
        return []
    return [s for s in (job.get("steps") or []) if isinstance(s, dict)]


def _runs_paths_filter(job: object) -> bool:
    return any(_PATHS_FILTER_ACTION in str(s.get("uses", "")) for s in _steps(job))


def _paths_filter_job_labels(workflow: dict, label: str) -> set[str]:
    """``{"<label>:<job>"}`` for each job that runs the path-filter action."""
    return {
        f"{label}:{name}"
        for name, job in (workflow.get("jobs") or {}).items()
        if _runs_paths_filter(job)
    }


def _shallow_paths_filter_offenders(workflow: dict, label: str) -> dict[str, str]:
    """``{"<label>:<job>": reason}`` for each path-filter job that checks out
    shallowly.

    A job qualifies only if it runs the filter; the checkout depth of every other
    job is none of this detector's business.
    """
    offenders: dict[str, str] = {}
    for name, job in (workflow.get("jobs") or {}).items():
        if not _runs_paths_filter(job):
            continue
        depths = [
            (s.get("with") or {}).get("fetch-depth")
            for s in _steps(job)
            if _CHECKOUT_ACTION in str(s.get("uses", ""))
        ]
        if _FULL_HISTORY not in [d for d in depths if d is not None]:
            reason = f"checkout fetch-depth {depths}" if depths else "no checkout step"
            offenders[f"{label}:{name}"] = reason
    return offenders


def _job_run_text(job: dict) -> str:
    """All ``run:`` script of a job, joined in step order."""
    return "\n".join(s.get("run", "") for s in _steps(job))


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
    assert _path_filtered_triggers(workflow) == {}, (
        "no ci.yml trigger may carry a path filter: the public-posture and "
        "collection-integrity gates read Markdown and the git tree, so a "
        "documentation-only change can legitimately fail them and must still "
        f"run. Path-filtered triggers: {_path_filtered_triggers(workflow)}"
    )


def test_concurrency_group_is_per_ref() -> None:
    """Runs are grouped per ref, so cancellation never crosses pull requests.

    Paired with the cancellation check below, and separable from it: a group that
    drops ``github.ref`` collapses every pull request into one group, at which
    point an event-gated ``cancel-in-progress`` — correct on its own terms —
    cancels one pull request's run when another is pushed to.
    """
    group = _concurrency_group(_load(CI_WORKFLOW))
    assert "github.ref" in group, (
        "the concurrency group must include github.ref so a cancellation cannot "
        f"reach another pull request's run; found: {group!r}"
    )


def test_merge_group_and_default_branch_runs_are_never_cancelled() -> None:
    """Only a superseded pull-request run may be cancelled.

    The queue's run carries a ``gh-readonly-queue/...`` ref, so a
    ``cancel-in-progress`` keyed on ``github.ref`` treats it as cancellable and a
    later queue entry can kill the run the merge is waiting on.
    """
    expression = _cancel_in_progress(_load(CI_WORKFLOW))
    assert _cancels_only_pull_requests(expression), (
        "ci.yml's cancel-in-progress must gate on github.event_name == "
        f"'pull_request' and must not consult github.ref; found: {expression!r}"
    )


def test_paths_filter_jobs_check_out_full_history() -> None:
    """Any job running the path-filter action checks out full history.

    On a merge-group run the action resolves the group's base commit from the
    local object store. A depth-1 checkout does not contain it, so the filter
    mis-reports and the required frontend lint job that depends on its output is
    skipped -- which the forge counts as satisfied. The bypass is green, which is
    why it needs a gate rather than a code review.
    """
    offenders: dict[str, str] = {}
    located: set[str] = set()
    for path in _iter_workflow_files():
        workflow = _load(path)
        offenders.update(_shallow_paths_filter_offenders(workflow, path.name))
        located |= _paths_filter_job_labels(workflow, path.name)

    assert located, (
        "no job running "
        f"{_PATHS_FILTER_ACTION} was found in any workflow -- the scan located "
        "nothing, so this gate would pass vacuously. If the action was renamed "
        "or removed, update _PATHS_FILTER_ACTION."
    )
    assert not offenders, (
        "path-filter jobs must check out with fetch-depth: 0 so a merge-group "
        f"base commit resolves locally; offenders: {offenders}"
    )


def test_scan_reaches_all_workflow_files() -> None:
    """The path-filter scan visits every committed workflow, so a real one cannot
    be silently excluded from discovery."""
    scanned = {p.name for p in _iter_workflow_files()}
    expected = {"infra.yml", "ci.yml", "build-images.yml", "maintenance.yml"}
    assert expected <= scanned, f"workflow scan must reach {sorted(expected - scanned)}"


def test_secret_scan_has_an_arm_for_every_trigger_event() -> None:
    """The secret scan branches on every event the workflow triggers on.

    An event with no arm of its own falls to the default, which scans the whole
    working tree rather than the commit range. That mode is never exercised by a
    pull request, so it can fail the queue on content already on the default
    branch.
    """
    workflow = _load(CI_WORKFLOW)
    job = _secret_scan_job(workflow)
    assert job, "no job running `gitleaks detect` was found in ci.yml"

    arms = _case_arm_labels(_job_run_text(job))
    missing = _trigger_events(workflow) - arms
    assert not missing, (
        f"the secret scan has no case arm for {sorted(missing)}; those events "
        "fall to the default arm, which scans the working tree instead of the "
        f"commit range. Arms present: {sorted(arms)}"
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

_SYNTHETIC_REQUIRED_CHECK_TABLE: Final[str] = (
    "| Job | Purpose |\n"
    "|---|---|\n"
    "| `test` | Full suite. |\n"
    "| `lint` | Formatting. |\n"
    "\nProse that mentions `eslint` outside the table must not be read as a row.\n"
)

_SYNTHETIC_SHALLOW_FILTER: Final[str] = (
    "jobs:\n"
    "  paths-filter:\n"
    "    steps:\n"
    "      - uses: actions/checkout@v7\n"
    "      - uses: dorny/paths-filter@v4\n"
    "  bystander:\n"
    "    steps:\n"
    "      - uses: actions/checkout@v7\n"
)

_SYNTHETIC_FULL_HISTORY_FILTER: Final[str] = (
    "jobs:\n"
    "  paths-filter:\n"
    "    steps:\n"
    "      - uses: actions/checkout@v7\n"
    "        with:\n"
    "          fetch-depth: 0\n"
    "      - uses: dorny/paths-filter@v4\n"
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
    """The pre-queue expression -- the actual regression -- is rejected. It
    evaluates true for a ``gh-readonly-queue/...`` ref, so the queue's run is
    cancellable under it."""
    assert not _cancels_only_pull_requests("${{ github.ref != 'refs/heads/main' }}")


def test_cancel_detector_rejects_a_hybrid_expression() -> None:
    """Naming the event is not enough: an expression that still consults
    ``github.ref`` is rejected, so a partial edit cannot satisfy the gate."""
    assert not _cancels_only_pull_requests(
        "${{ github.event_name == 'pull_request' && github.ref != 'refs/heads/main' }}"
    )


def test_cancel_detector_accepts_the_event_keyed_expression() -> None:
    """The legitimate form clears."""
    assert _cancels_only_pull_requests("${{ github.event_name == 'pull_request' }}")


def test_paths_filter_detector_flags_a_shallow_checkout() -> None:
    """A path-filter job with a default checkout is flagged, and a job that does
    not run the filter is left alone."""
    workflow = yaml.safe_load(_SYNTHETIC_SHALLOW_FILTER)
    assert _shallow_paths_filter_offenders(workflow, "synthetic.yml") == {
        "synthetic.yml:paths-filter": "checkout fetch-depth [None]"
    }


def test_paths_filter_detector_clears_a_full_history_checkout() -> None:
    """The legitimate form produces no offender."""
    workflow = yaml.safe_load(_SYNTHETIC_FULL_HISTORY_FILTER)
    assert _shallow_paths_filter_offenders(workflow, "synthetic.yml") == {}


def test_paths_filter_discovery_finds_a_synthetic_job() -> None:
    """The job locator actually fires, rather than returning an empty set that
    would make the located-something assertion pass against an empty scan."""
    workflow = yaml.safe_load(_SYNTHETIC_SHALLOW_FILTER)
    assert _paths_filter_job_labels(workflow, "synthetic.yml") == {"synthetic.yml:paths-filter"}


def test_case_arm_detector_flags_a_missing_arm() -> None:
    """A scan script with no merge-group arm is caught, and the arms that are
    present are read correctly."""
    arms = _case_arm_labels(_SYNTHETIC_SCAN_WITHOUT_MERGE_GROUP)
    assert arms == {"pull_request", "push", "*"}
    assert "merge_group" not in arms


def test_path_filter_detector_flags_a_path_gated_trigger() -> None:
    """A trigger carrying a path filter is caught, and one without is not. Both
    directions matter: a ``paths`` key leaves the event set and the branch list
    identical, so every other trigger assertion passes over it."""
    gated = yaml.safe_load(_SYNTHETIC_PATH_GATED_TRIGGER)
    assert _path_filtered_triggers(gated) == {"push": ["paths"]}
    assert _trigger_events(gated) == set(EXPECTED_TRIGGERS)
    assert _push_branches(gated) == ["main"]
    assert _path_filtered_triggers(yaml.safe_load(_SYNTHETIC_SINGLE_TRIGGER)) == {}


def test_concurrency_group_detector_reads_the_group_expression() -> None:
    """The group detector reads the expression rather than returning a constant
    empty string, which would make the per-ref assertion fail open on rewrite."""
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
