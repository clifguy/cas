"""Tests for the Dependabot alert triage script and its workflow.

The triage script polls open Dependabot alerts, keeps only the *actionable*
ones (a fix is published and the advisory is not on an accepted-risk
allowlist), and opens / updates / closes a single tracking issue. These tests
exercise the pure filter / format / decide logic directly and assert the
structural shape of the scheduled workflow and the allowlist file.

The I/O edge (GitHub API read, issue mutation) is intentionally thin and is
verified out-of-band via the workflow's manual dispatch; only the pure logic
is unit-tested here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from scripts.triage_dependabot_alerts import (
    ISSUE_FOOTER_MARKER,
    actionable_alerts,
    decide_action,
    format_issue_body,
    load_allowlist,
)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
WORKFLOW: Final[Path] = REPO_ROOT / ".github" / "workflows" / "dependabot-triage.yml"
ALLOWLIST: Final[Path] = REPO_ROOT / ".github" / "dependabot-triage-allowlist.yml"


def _alert(
    *,
    ghsa: str = "GHSA-aaaa-bbbb-cccc",
    fixed: str | None = "1.2.3",
    severity: str = "high",
    state: str = "open",
    name: str = "examplepkg",
    ecosystem: str = "pip",
    number: int = 1,
) -> dict[str, Any]:
    """Build a Dependabot alert dict in the shape the REST API returns.

    ``fixed=None`` models the no-published-fix shape (a null
    ``first_patched_version``) — the case the actionable filter must drop.
    """
    first_patched = {"identifier": fixed} if fixed is not None else None
    return {
        "number": number,
        "state": state,
        "html_url": f"https://github.com/o/r/security/dependabot/{number}",
        "dependency": {"package": {"ecosystem": ecosystem, "name": name}},
        "security_advisory": {"ghsa_id": ghsa, "severity": severity},
        "security_vulnerability": {
            "severity": severity,
            "vulnerable_version_range": "< 1.2.3",
            "first_patched_version": first_patched,
        },
    }


# --- actionable_alerts ------------------------------------------------------


def test_actionable_includes_open_fixable_not_allowlisted() -> None:
    """A1: open + has-fix + not allowlisted is the actionable case."""
    alerts = [_alert(ghsa="GHSA-1", fixed="1.2.3")]
    assert len(actionable_alerts(alerts, set())) == 1


def test_actionable_excludes_no_fix() -> None:
    """A2: an open alert with no published fix (the torch shape) is dropped."""
    alerts = [_alert(ghsa="GHSA-nofix", fixed=None)]
    assert actionable_alerts(alerts, set()) == []


def test_actionable_excludes_allowlisted() -> None:
    """A3: an otherwise-actionable alert whose ghsa is allowlisted is dropped."""
    alerts = [_alert(ghsa="GHSA-skip", fixed="2.0.0")]
    assert actionable_alerts(alerts, {"GHSA-skip"}) == []


def test_actionable_excludes_non_open() -> None:
    """A4: a non-open (dismissed/fixed) alert is dropped even with a fix."""
    alerts = [_alert(ghsa="GHSA-done", fixed="1.0.0", state="fixed")]
    assert actionable_alerts(alerts, set()) == []


def test_actionable_retains_all_actionable() -> None:
    """A5: every actionable alert survives the filter."""
    alerts = [_alert(ghsa=f"GHSA-{i}", number=i, name=f"pkg{i}") for i in range(3)]
    assert len(actionable_alerts(alerts, set())) == 3


# --- format_issue_body ------------------------------------------------------


def test_format_body_contains_fields_and_marker() -> None:
    """F1: the body surfaces package, severity, fix version, and the marker."""
    actionable = actionable_alerts(
        [_alert(name="starlette", severity="high", fixed="1.3.1", ghsa="GHSA-x")],
        set(),
    )
    body = format_issue_body(actionable)
    assert "starlette" in body
    assert "high" in body.lower()
    assert "1.3.1" in body
    assert ISSUE_FOOTER_MARKER in body


def test_format_body_stable_order_by_severity() -> None:
    """F2: ordering is deterministic regardless of input order (no churn)."""
    a = _alert(name="zlib", severity="low", ghsa="GHSA-z", number=1)
    b = _alert(name="alpha", severity="critical", ghsa="GHSA-a", number=2)
    c = _alert(name="mid", severity="high", ghsa="GHSA-m", number=3)
    body1 = format_issue_body(actionable_alerts([a, b, c], set()))
    body2 = format_issue_body(actionable_alerts([c, a, b], set()))
    assert body1 == body2
    assert body1.index("alpha") < body1.index("mid") < body1.index("zlib")


# --- decide_action ----------------------------------------------------------


@pytest.mark.parametrize(
    "n_actionable,existing_issue,expected",
    [
        (1, None, "open"),
        (1, 42, "update"),
        (0, 42, "close"),
        (0, None, "noop"),
    ],
    ids=["open", "update", "close", "noop"],
)
def test_decide_action(n_actionable: int, existing_issue: int | None, expected: str) -> None:
    """D1-D4: the open/update/close/noop decision matrix."""
    actionable = [_alert(ghsa=f"GHSA-{i}", number=i) for i in range(n_actionable)]
    assert decide_action(actionable, existing_issue) == expected


# --- load_allowlist ---------------------------------------------------------


def test_load_allowlist_parses_ghsa_ids(tmp_path: Path) -> None:
    """L1: the accepted list reduces to a set of ghsa ids."""
    p = tmp_path / "allow.yml"
    p.write_text(
        "accepted:\n  - ghsa_id: GHSA-1\n    reason: r1\n  - ghsa_id: GHSA-2\n    reason: r2\n",
        encoding="utf-8",
    )
    assert load_allowlist(p) == {"GHSA-1", "GHSA-2"}


def test_load_allowlist_missing_file_is_empty(tmp_path: Path) -> None:
    """L2: a missing allowlist file is an empty set, not an error."""
    assert load_allowlist(tmp_path / "nope.yml") == set()


def test_load_allowlist_empty_accepted(tmp_path: Path) -> None:
    """An empty accepted list reduces to an empty set."""
    p = tmp_path / "a.yml"
    p.write_text("accepted: []\n", encoding="utf-8")
    assert load_allowlist(p) == set()


# --- workflow + allowlist file structure ------------------------------------


def _on_block(workflow: dict[str, Any]) -> dict[str, Any]:
    """Return the workflow trigger mapping.

    PyYAML parses a bare ``on:`` key as the boolean ``True`` under YAML 1.1
    rules, so the trigger block is keyed by ``True`` rather than ``"on"``.
    """
    block = workflow.get(True)
    if block is None:
        block = workflow.get("on")
    return block or {}


def test_workflow_has_schedule_and_dispatch() -> None:
    """W1: the workflow runs on a cron schedule and supports manual dispatch."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    on = _on_block(workflow)
    assert "workflow_dispatch" in on, "workflow must allow manual dispatch"
    schedule = on.get("schedule") or []
    assert any("cron" in entry for entry in schedule), "workflow must declare a cron schedule"


def test_workflow_least_privilege_permissions() -> None:
    """W2: least-privilege — contents:read + issues:write, nothing more."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    perms = workflow.get("permissions") or {}
    assert perms.get("contents") == "read"
    assert perms.get("issues") == "write"
    assert "id-token" not in perms, "no OIDC token needed"
    assert "security-events" not in perms, (
        "security-events does not grant Dependabot-alert read; do not request it"
    )


def test_workflow_references_pat_secret_and_script() -> None:
    """W3: the alerts read uses the PAT secret and invokes the triage module."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "secrets.DEPENDABOT_ALERTS_TOKEN" in text, (
        "alerts read must use the fine-grained PAT secret"
    )
    assert "scripts.triage_dependabot_alerts" in text, (
        "workflow must invoke the triage script module"
    )


def test_allowlist_file_parses_with_accepted_list() -> None:
    """W4: the allowlist file exists and carries a list-valued accepted key."""
    data = yaml.safe_load(ALLOWLIST.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert isinstance(data.get("accepted"), list)
