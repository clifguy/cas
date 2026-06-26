#!/usr/bin/env python3
"""Triage open Dependabot alerts into a single tracking issue.

Polls the repository's open Dependabot alerts, keeps only the *actionable*
ones (an upstream fix is published and the advisory is not on the accepted-risk
allowlist), and reconciles one tracking issue: opened when actionable alerts
appear, updated in place while they persist, closed once they clear. Advisories
with no published fix are excluded automatically, so the issue reflects work
that can actually be done rather than raw alert volume.

The alerts read needs a token with Dependabot-alerts read access — the built-in
Actions token cannot read that endpoint — supplied via ``GH_DEPENDABOT_TOKEN``.
Issue reads and writes use the ambient ``gh`` credential (``GH_TOKEN``), which
needs only ``issues: write``. With no alerts token configured the script logs
and exits 0, so the surrounding automation stays dormant rather than failing
until the token is provisioned.

Usage::

    python -m scripts.triage_dependabot_alerts             # reconcile the issue
    python -m scripts.triage_dependabot_alerts --dry-run   # print, do not mutate
    python -m scripts.triage_dependabot_alerts --dry-run --alerts-file sample.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

TRACKING_LABEL = "dependabot-triage"
ISSUE_TITLE = "Dependabot: actionable security alerts"
ISSUE_FOOTER_MARKER = "<!-- dependabot-triage:managed -->"
DEFAULT_ALLOWLIST_PATH = (
    Path(__file__).resolve().parent.parent / ".github" / "dependabot-triage-allowlist.yml"
)

# GitHub reports severity as critical/high/medium/low on the vulnerability and
# critical/high/moderate/low on the advisory; rank both spellings together.
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "moderate": 2, "low": 3}


# --- pure logic -------------------------------------------------------------


def actionable_alerts(alerts: list[dict[str, Any]], allowlist: set[str]) -> list[dict[str, Any]]:
    """Keep open alerts that have a published fix and are not allowlisted.

    An alert is actionable iff it is ``open``, carries a non-null
    ``first_patched_version`` (an upstream fix exists), and its advisory id is
    not in ``allowlist``. Filtering on fix-availability is what makes the
    downstream issue track actionable work rather than raw alert count.
    """
    result: list[dict[str, Any]] = []
    for alert in alerts:
        if alert.get("state") != "open":
            continue
        vuln = alert.get("security_vulnerability") or {}
        if not (vuln.get("first_patched_version") or {}).get("identifier"):
            continue
        ghsa = (alert.get("security_advisory") or {}).get("ghsa_id")
        if ghsa in allowlist:
            continue
        result.append(alert)
    return result


def _sort_key(alert: dict[str, Any]) -> tuple[int, str]:
    vuln = alert.get("security_vulnerability") or {}
    severity = str(vuln.get("severity") or "").lower()
    package = (alert.get("dependency") or {}).get("package") or {}
    return (_SEVERITY_RANK.get(severity, 99), str(package.get("name") or "").lower())


def format_issue_body(actionable: list[dict[str, Any]]) -> str:
    """Render the tracking-issue body as a stable, severity-sorted table.

    Ordering is deterministic (severity rank, then package name) so an
    update-in-place produces an identical body when the alert set is unchanged,
    avoiding spurious re-notification.
    """
    rows = sorted(actionable, key=_sort_key)
    lines = [
        f"## {len(rows)} actionable Dependabot alert(s)",
        "",
        "Security advisories with a published fix. Remediate each with a "
        "scoped lockfile bump; this issue closes automatically once the alerts "
        "clear.",
        "",
        "| Package | Ecosystem | Severity | Fixed in | Advisory |",
        "| --- | --- | --- | --- | --- |",
    ]
    for alert in rows:
        package = (alert.get("dependency") or {}).get("package") or {}
        vuln = alert.get("security_vulnerability") or {}
        fixed = (vuln.get("first_patched_version") or {}).get("identifier") or "?"
        ghsa = (alert.get("security_advisory") or {}).get("ghsa_id") or ""
        url = alert.get("html_url") or ""
        lines.append(
            f"| {package.get('name', '?')} | {package.get('ecosystem', '?')} "
            f"| {vuln.get('severity', '?')} | {fixed} | [{ghsa}]({url}) |"
        )
    lines += ["", ISSUE_FOOTER_MARKER]
    return "\n".join(lines)


def decide_action(actionable: list[dict[str, Any]], existing_open_issue: int | None) -> str:
    """Map (actionable alerts, existing open issue) to a reconcile action."""
    if actionable:
        return "open" if existing_open_issue is None else "update"
    return "close" if existing_open_issue is not None else "noop"


def load_allowlist(path: str | Path) -> set[str]:
    """Read accepted-risk advisory ids from the allowlist file.

    The file is a mapping with an ``accepted`` list of ``{ghsa_id, reason}``
    entries. A missing file is an empty allowlist, not an error.
    """
    p = Path(path)
    if not p.is_file():
        return set()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    accepted = data.get("accepted") or []
    return {
        entry["ghsa_id"] for entry in accepted if isinstance(entry, dict) and entry.get("ghsa_id")
    }


# --- I/O edge ---------------------------------------------------------------


def _run(
    cmd: list[str], *, token: str | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if token:
        env["GH_TOKEN"] = token
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if check and proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr}")
    return proc


def _run_with_body(cmd: list[str], body: str) -> None:
    proc = subprocess.run(cmd, input=body, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr}")


def fetch_open_alerts(repo: str, token: str) -> list[dict[str, Any]]:
    proc = _run(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repo}/dependabot/alerts?state=open&per_page=100",
        ],
        token=token,
    )
    return json.loads(proc.stdout)


def find_open_issue(repo: str, label: str) -> int | None:
    proc = _run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--label",
            label,
            "--state",
            "open",
            "--json",
            "number",
            "--limit",
            "1",
        ]
    )
    items = json.loads(proc.stdout or "[]")
    return items[0]["number"] if items else None


def ensure_label(repo: str, label: str) -> None:
    # --force makes this idempotent: create if absent, update if present.
    _run(
        [
            "gh",
            "label",
            "create",
            label,
            "--repo",
            repo,
            "--description",
            "Tracking issue for actionable Dependabot alerts",
            "--color",
            "B60205",
            "--force",
        ],
        check=False,
    )


def _create_issue(repo: str, body: str) -> None:
    ensure_label(repo, TRACKING_LABEL)
    _run_with_body(
        [
            "gh",
            "issue",
            "create",
            "--repo",
            repo,
            "--label",
            TRACKING_LABEL,
            "--title",
            ISSUE_TITLE,
            "--body-file",
            "-",
        ],
        body,
    )


def _update_issue(repo: str, number: int, body: str) -> None:
    _run_with_body(
        ["gh", "issue", "edit", str(number), "--repo", repo, "--body-file", "-"],
        body,
    )


def _close_issue(repo: str, number: int) -> None:
    _run(
        [
            "gh",
            "issue",
            "close",
            str(number),
            "--repo",
            repo,
            "--comment",
            "All actionable Dependabot alerts are resolved; closing.",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile a tracking issue from open Dependabot alerts."
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GH_REPO", ""),
        help="owner/name of the repository (defaults to $GH_REPO).",
    )
    parser.add_argument(
        "--allowlist",
        default=str(DEFAULT_ALLOWLIST_PATH),
        help="path to the accepted-risk allowlist YAML.",
    )
    parser.add_argument(
        "--alerts-file",
        help="read alert JSON from a file instead of the API (offline use).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the decision and issue body without mutating anything.",
    )
    args = parser.parse_args(argv)

    allowlist = load_allowlist(args.allowlist)

    if args.alerts_file:
        alerts = json.loads(Path(args.alerts_file).read_text(encoding="utf-8"))
    else:
        token = os.environ.get("GH_DEPENDABOT_TOKEN", "").strip()
        if not token:
            print(
                "no Dependabot alerts token configured (GH_DEPENDABOT_TOKEN); skipping",
                file=sys.stderr,
            )
            return 0
        if not args.repo:
            print("no repository configured (--repo or $GH_REPO); skipping", file=sys.stderr)
            return 0
        alerts = fetch_open_alerts(args.repo, token)

    actionable = actionable_alerts(alerts, allowlist)

    # Issue reconciliation needs gh credentials; skip it in offline mode.
    online = not args.alerts_file and bool(args.repo)
    existing = find_open_issue(args.repo, TRACKING_LABEL) if online else None
    decision = decide_action(actionable, existing)
    body = format_issue_body(actionable) if actionable else ""

    if args.dry_run:
        print(f"decision: {decision} (actionable={len(actionable)}, existing_issue={existing})")
        if body:
            print(body)
        return 0

    if decision == "open":
        _create_issue(args.repo, body)
    elif decision == "update" and existing is not None:
        _update_issue(args.repo, existing, body)
    elif decision == "close" and existing is not None:
        _close_issue(args.repo, existing)

    print(f"decision: {decision} (actionable={len(actionable)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
