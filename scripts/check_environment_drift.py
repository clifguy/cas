#!/usr/bin/env python3
"""Check that every deployment environment carries its required protections.

Each workflow that signs in to Azure binds a GitHub deployment environment, and
the deploy identity's federated credential trusts tokens minted for that
environment and nothing else. The environment's protection rules are therefore
what stands between a pushed branch and a token the cloud accepts. Two rules
are required of every environment the repository has:

* **branch-policy** -- a custom deployment-branch policy admitting exactly the
  ``main`` branch. The "protected branches" mode is not accepted: it admits any
  branch a ruleset protects, which is a wider set than ``main`` alone.
* **admin-bypass** -- ``can_admins_bypass`` is false, so an administrator's
  credential cannot push a run past any protection rule added later, such as a
  wait timer, a reviewer or a custom rule.

A required reviewer is deliberately not required, nor refused. It cannot stop a
caller that holds the owner's credentials, which can approve a pending
deployment through the API, so the branch policy carries the gate.

These live in repository settings, which carry no commit, so only a read of the
live settings can see one removed. The ruleset-drift workflow runs this check on
a schedule beside the ruleset comparison.

The rules apply to *every* environment rather than to a named list. An
environment no workflow uses is still one a newly added workflow could bind, so
an unused environment is either protected or deleted. A listing with no
environments at all fails too: it is what a read that silently saw nothing looks
like, and an absent field fails its rule for the same reason.

Every field read here is returned at repository-read scope for a public
repository, so the workflow's default token suffices.

Usage::

    python -m scripts.check_environment_drift                         # check live
    python -m scripts.check_environment_drift --repo owner/name
    python -m scripts.check_environment_drift --environments-file e.json   # offline

The offline file is a JSON list of environment records in the forge's shape,
each carrying its deployment-branch policies under ``branch_policies``. Exit
status is 0 when every environment is protected and 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Final, NamedTuple
from urllib.parse import quote

REQUIRED_BRANCH: Final[str] = "main"

# The listing endpoints' maximum page size. A repository with more environments
# than this fails the completeness check below rather than being half-read.
_PAGE: Final[str] = "?per_page=100"

_ABSENT: Final[str] = "<absent>"


class Violation(NamedTuple):
    """One environment failing one required rule, with what is there now."""

    environment: str
    rule: str
    live: Any


# --- pure logic -------------------------------------------------------------


def check_environment(env: dict[str, Any]) -> list[Violation]:
    """Every required rule this environment fails."""
    name = str(env.get("name", "<unnamed>"))
    found: list[Violation] = []

    policy = env.get("deployment_branch_policy")
    branches = sorted(
        (str(p.get("name", _ABSENT)), str(p.get("type", _ABSENT)))
        for p in env.get("branch_policies") or []
        if isinstance(p, dict)
    )
    if not (
        isinstance(policy, dict)
        and policy.get("custom_branch_policies") is True
        and policy.get("protected_branches") is False
        and branches == [(REQUIRED_BRANCH, "branch")]
    ):
        found.append(
            Violation(name, "branch-policy", {"policy": policy, "branch_policies": branches})
        )

    bypass = env.get("can_admins_bypass", _ABSENT)
    if bypass is not False:
        found.append(Violation(name, "admin-bypass", bypass))

    return found


def check_environments(environments: list[dict[str, Any]]) -> list[Violation]:
    """Every violation across the repository's environments, in listing order.

    An empty listing is itself a violation: it cannot be told apart from a read
    that returned nothing.
    """
    if not environments:
        return [Violation("<none>", "no-environments", [])]
    return [violation for env in environments for violation in check_environment(env)]


def format_report(violations: list[Violation]) -> str:
    """A human-readable rendering naming each environment, rule and live value."""
    if not violations:
        return "deployment environments: every environment carries its required protections."
    lines = [f"deployment environments: {len(violations)} required protection(s) missing.", ""]
    for violation in violations:
        lines += [
            f"  {violation.environment}: {violation.rule}",
            f"      live: {json.dumps(violation.live, sort_keys=True, default=str)}",
        ]
    lines += [
        "",
        "Restore the protection in the environment's settings, or delete an environment "
        "nothing uses; docs/process/azure-deployment.md states what each requires.",
    ]
    return "\n".join(lines)


# --- I/O edge ---------------------------------------------------------------


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a forge CLI command; a failed read raises rather than passing."""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr}")
    return proc


def _get(endpoint: str) -> dict[str, Any]:
    return json.loads(_run(["gh", "api", endpoint]).stdout)


def fetch_live_environments(repo: str | None) -> list[dict[str, Any]]:
    """Each environment, with its deployment-branch policies folded in.

    Refuses a listing shorter than the total the forge reports, or one that
    reports no total, so a page that was not read cannot pass as an environment
    that does not exist.
    """
    base = f"repos/{repo}" if repo else "repos/{owner}/{repo}"
    listing = _get(f"{base}/environments{_PAGE}")
    environments = list(listing.get("environments") or [])
    if len(environments) != listing.get("total_count"):
        raise RuntimeError(
            f"environment listing incomplete: read {len(environments)} of "
            f"{listing.get('total_count')}"
        )
    for env in environments:
        name = quote(str(env["name"]), safe="")
        policies = _get(f"{base}/environments/{name}/deployment-branch-policies{_PAGE}")
        branch_policies = list(policies.get("branch_policies") or [])
        if len(branch_policies) != policies.get("total_count"):
            raise RuntimeError(f"branch-policy listing for {env['name']} incomplete")
        env["branch_policies"] = branch_policies
    return environments


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that every deployment environment carries its required protections."
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GH_REPO", ""),
        help="owner/name of the repository (defaults to $GH_REPO, then the working directory).",
    )
    parser.add_argument(
        "--environments-file",
        help="read the environment records from a file instead of the API (offline use).",
    )
    args = parser.parse_args(argv)

    if args.environments_file:
        environments = json.loads(Path(args.environments_file).read_text(encoding="utf-8"))
    else:
        environments = fetch_live_environments(args.repo or None)

    violations = check_environments(environments)
    print(format_report(violations))
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
