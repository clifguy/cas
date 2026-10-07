"""Repo-wide supply-chain gates over the workflows and the pre-commit hooks.

Three properties, each one a way for code or credentials the repository did not
review to enter a run:

* **The OIDC token is held only where it is spent.** ``id-token: write`` lets a
  job mint a token the cloud's federated credential may accept. A job that never
  signs in gains nothing from holding it, and any step it runs -- including a
  compromised action -- could mint one. So no workflow grants it at workflow
  level, and every job that holds it either signs in to Azure itself, calls a
  reusable workflow whose job signs in, or is named below with its reason.
* **Every action is pinned by commit.** A tag can be moved to different code
  after review; a full commit SHA cannot. Local reusable workflows are part of
  this repository and are pinned by the checkout itself.
* **Every pre-commit hook repository is pinned by commit**, for the same reason.

The detectors are pure functions over parsed YAML; the controls at the end feed
them synthetic input, so a matcher that never fires cannot pass vacuously.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final

import yaml

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR: Final[Path] = REPO_ROOT / ".github" / "workflows"
PRE_COMMIT: Final[Path] = REPO_ROOT / ".pre-commit-config.yaml"

_SHA_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")
_ACTION_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@([^@\s]+)$")

# Caller jobs whose id-token grant is spent by the reusable workflow they call,
# keyed (workflow, job) -> called workflow. The called workflow must contain a
# job that both holds the token and signs in.
_DELEGATED: Final[dict[tuple[str, str], str]] = {
    ("infra.yml", "build"): "build-images.yml",
}

# Jobs that hold the token without spending it, with the reason. A reusable
# workflow may only narrow the permissions its caller grants, so a caller of a
# workflow whose job requests the token must grant it even on an arm that never
# signs in. The token such a job mints names a pull-request or branch subject,
# which no federated credential on the deploy identity accepts (the deploy
# identity's subjects are environment subjects only; see
# docs/process/azure-deployment.md).
_EXEMPT: Final[dict[tuple[str, str], str]] = {
    ("ci.yml", "container"): (
        "calls build-images.yml with push off; the called job requests the token for its "
        "push arm, which this caller never takes"
    ),
}

_EXPECTED_WORKFLOWS: Final[tuple[str, ...]] = (
    "build-images.yml",
    "ci.yml",
    "dependabot-triage.yml",
    "infra.yml",
    "maintenance.yml",
    "postgres-migration.yml",
    "ruleset-drift.yml",
    "security.yml",
    "sharepoint-validate.yml",
)


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


def _workflows() -> dict[str, dict[str, Any]]:
    return {
        path.name: yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for path in sorted(WORKFLOWS_DIR.iterdir())
        if path.is_file() and path.suffix in (".yml", ".yaml")
    }


def _grants_id_token(block: Any) -> bool:
    return isinstance(block, dict) and block.get("id-token") == "write" or block == "write-all"


def _step_signs_in(step: dict[str, Any]) -> bool:
    uses = str(step.get("uses") or "")
    run = str(step.get("run") or "")
    return (
        uses.startswith("azure/login@")
        or "deploy/azure-federated-signin.sh" in run
        or ("az login" in run and "--federated-token" in run)
    )


def _job_signs_in(job: dict[str, Any]) -> bool:
    return any(_step_signs_in(step) for step in job.get("steps") or [] if isinstance(step, dict))


def _workflow_level_grants(workflows: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(name for name, wf in workflows.items() if _grants_id_token(wf.get("permissions")))


def _unspent_job_grants(workflows: dict[str, dict[str, Any]]) -> list[str]:
    """Jobs holding the token that neither sign in, delegate, nor are exempt."""
    offenders: list[str] = []
    for name, wf in workflows.items():
        for job_name, job in (wf.get("jobs") or {}).items():
            if not isinstance(job, dict) or not _grants_id_token(job.get("permissions")):
                continue
            key = (name, job_name)
            if _job_signs_in(job) or key in _EXEMPT:
                continue
            callee = _DELEGATED.get(key)
            if callee and str(job.get("uses") or "") == f"./.github/workflows/{callee}":
                continue
            offenders.append(f"{name}:{job_name}")
    return sorted(offenders)


def _unpinned_actions(workflows: dict[str, dict[str, Any]]) -> list[str]:
    """Every ``uses:`` that is neither a local path nor pinned to a full SHA."""
    offenders: list[str] = []
    for name, wf in workflows.items():
        for job_name, job in (wf.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            refs = [job.get("uses")] + [
                step.get("uses") for step in job.get("steps") or [] if isinstance(step, dict)
            ]
            for ref in refs:
                if not ref or str(ref).startswith("./"):
                    continue
                match = _ACTION_RE.match(str(ref))
                if not match or not _SHA_RE.match(match.group(1)):
                    offenders.append(f"{name}:{job_name}: {ref}")
    return sorted(offenders)


def _unpinned_hooks(config: dict[str, Any]) -> list[str]:
    return sorted(
        f"{repo.get('repo')}@{repo.get('rev')}"
        for repo in config.get("repos") or []
        if repo.get("repo") not in ("local", "meta") and not _SHA_RE.match(str(repo.get("rev")))
    )


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------


def test_scan_reaches_all_workflow_files() -> None:
    missing = sorted(set(_EXPECTED_WORKFLOWS) - set(_workflows()))
    assert not missing, f"the supply-chain gates must scan {missing}"


def test_no_workflow_level_id_token_grant() -> None:
    offenders = _workflow_level_grants(_workflows())
    assert not offenders, (
        f"grant id-token: write on the job that signs in, not the whole workflow: {offenders}"
    )


def test_id_token_held_only_by_jobs_that_sign_in() -> None:
    offenders = _unspent_job_grants(_workflows())
    assert not offenders, f"these jobs hold id-token: write without signing in: {offenders}"


def test_exemptions_and_delegations_are_live() -> None:
    """Each named exception still exists and still holds the token, and each
    delegate's called workflow still has a job that holds it and signs in."""
    workflows = _workflows()
    for name, job_name in [*_EXEMPT, *_DELEGATED]:
        job = (workflows[name].get("jobs") or {}).get(job_name)
        assert job and _grants_id_token(job.get("permissions")), (
            f"{name}:{job_name} is listed as an exception but no longer holds the token; "
            "remove the entry"
        )
    for callee in _DELEGATED.values():
        jobs = (workflows[callee].get("jobs") or {}).values()
        assert any(_grants_id_token(j.get("permissions")) and _job_signs_in(j) for j in jobs)
    # The exemption's reason is that the caller never takes the push arm; a
    # caller that pushes would sign in with a branch or pull-request subject.
    container = workflows["ci.yml"]["jobs"]["container"]
    assert (container.get("with") or {}).get("push") is False


def _signing_jobs_without_grant(workflows: dict[str, dict[str, Any]]) -> list[str]:
    """Jobs that sign in to Azure without holding the token at job level."""
    return sorted(
        f"{name}:{job_name}"
        for name, wf in workflows.items()
        for job_name, job in (wf.get("jobs") or {}).items()
        if isinstance(job, dict)
        and _job_signs_in(job)
        and not _grants_id_token(job.get("permissions"))
    )


def test_every_signing_job_holds_the_token() -> None:
    """With no workflow-level grant to fall back on, a job that signs in must
    request the token itself, or its sign-in fails on dispatch."""
    offenders = _signing_jobs_without_grant(_workflows())
    assert not offenders, f"these jobs sign in without id-token: write: {offenders}"


def test_every_action_is_pinned_by_commit() -> None:
    offenders = _unpinned_actions(_workflows())
    assert not offenders, f"pin these to a full commit SHA: {offenders}"


def test_every_pre_commit_hook_is_pinned_by_commit() -> None:
    config = yaml.safe_load(PRE_COMMIT.read_text(encoding="utf-8"))
    assert config.get("repos"), "no hook repositories parsed; the gate would be vacuous"
    offenders = _unpinned_hooks(config)
    assert not offenders, f"pin these hook revisions to a full commit SHA: {offenders}"


# ---------------------------------------------------------------------------
# Anti-coincidental controls
# ---------------------------------------------------------------------------

_SHA: Final[str] = "a" * 40


def test_control_workflow_level_grant_is_found() -> None:
    wf = {"permissions": {"contents": "read", "id-token": "write"}, "jobs": {}}
    assert _workflow_level_grants({"x.yml": wf}) == ["x.yml"]
    assert _workflow_level_grants({"x.yml": {"permissions": "write-all"}}) == ["x.yml"]


def test_control_unspent_job_grant_is_found_and_spent_ones_pass() -> None:
    grant = {"contents": "read", "id-token": "write"}
    workflows = {
        "x.yml": {
            "jobs": {
                "idle": {"permissions": grant, "steps": [{"run": "make"}]},
                "login": {"permissions": grant, "steps": [{"uses": f"azure/login@{_SHA}"}]},
                "renew": {
                    "permissions": grant,
                    "steps": [{"run": "deploy/azure-federated-signin.sh"}],
                },
                "caller": {"permissions": grant, "uses": "./.github/workflows/other.yml"},
            }
        }
    }
    assert _unspent_job_grants(workflows) == ["x.yml:caller", "x.yml:idle"]


def test_control_signing_job_without_grant_is_found() -> None:
    login = [{"uses": f"azure/login@{_SHA}"}]
    workflows = {
        "x.yml": {
            "jobs": {
                "bare": {"steps": login},
                "granted": {"permissions": {"id-token": "write"}, "steps": login},
            }
        }
    }
    assert _signing_jobs_without_grant(workflows) == ["x.yml:bare"]


def test_control_unpinned_actions_are_found() -> None:
    steps = [
        {"uses": "actions/checkout@v4"},
        {"uses": "actions/checkout@abc1234"},
        {"uses": "actions/checkout@main"},
        {"uses": f"actions/checkout@{_SHA}"},
        {"uses": f"github/codeql-action/init@{_SHA}"},
        {"uses": "./.github/actions/local"},
    ]
    workflows = {
        "x.yml": {
            "jobs": {"j": {"steps": steps}, "r": {"uses": "org/repo/.github/workflows/w.yml@v1"}}
        }
    }
    assert _unpinned_actions(workflows) == [
        "x.yml:j: actions/checkout@abc1234",
        "x.yml:j: actions/checkout@main",
        "x.yml:j: actions/checkout@v4",
        "x.yml:r: org/repo/.github/workflows/w.yml@v1",
    ]


def test_control_unpinned_hooks_are_found() -> None:
    config = {
        "repos": [
            {"repo": "https://github.com/a/b", "rev": "v1.2.3"},
            {"repo": "https://github.com/a/c", "rev": _SHA},
            {"repo": "local", "hooks": []},
        ]
    }
    assert _unpinned_hooks(config) == ["https://github.com/a/b@v1.2.3"]
