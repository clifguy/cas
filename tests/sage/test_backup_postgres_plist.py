"""Structural tests for the Postgres-backup LaunchAgent template.

The checked-in plist is a placeholder template: the runbook substitutes the
venv-python, repo, backup-dir, and log-dir paths at install time. These tests
assert the template is well-formed and complete enough for launchd to schedule
the thrice-daily backup once substituted, and -- critically -- that it carries no
personal filesystem path (which the public-posture gate also forbids repo-wide).
"""

from __future__ import annotations

import plistlib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLIST_PATH = _REPO_ROOT / "scripts" / "launchd" / "local.cas.sage.postgres-backup.plist"

_PLACEHOLDERS = ("{{PYTHON}}", "{{REPO}}", "{{BACKUP_DIR}}", "{{LOG_DIR}}")


def test_plist_parses_and_has_required_keys() -> None:
    """The template parses and carries the keys launchd needs to fire correctly.

    A malformed plist launchd silently refuses to load; a missing or single-entry
    ``StartCalendarInterval`` would fire on the wrong cadence. The cadence is
    thrice daily -- 02:00 / 12:00 / 18:00 -- so the interval must be a list of
    three dicts, each with an hour and minute.
    """
    with _PLIST_PATH.open("rb") as fh:
        plist = plistlib.load(fh)

    assert plist["Label"] == "local.cas.sage.postgres-backup"

    program = plist["ProgramArguments"]
    assert isinstance(program, list) and program
    # The script the agent runs and the --dir flag it passes are both present.
    assert any("scripts/backup_postgres.py" in str(arg) for arg in program)
    assert "--dir" in program

    interval = plist["StartCalendarInterval"]
    assert isinstance(interval, list)
    hours = sorted(entry["Hour"] for entry in interval)
    assert hours == [2, 12, 18]
    assert all(entry["Minute"] == 0 for entry in interval)

    # Stdout/stderr are captured so a failed run is diagnosable.
    assert plist["StandardOutPath"]
    assert plist["StandardErrorPath"]


def test_plist_carries_no_personal_paths_and_all_placeholders() -> None:
    """No baked-in personal path; every substitution placeholder is present.

    ``/Users/`` must be absent (the install substitutes real paths), and each
    placeholder the runbook substitutes must exist so the install instructions
    have a target for every host-specific value.
    """
    text = _PLIST_PATH.read_text(encoding="utf-8")
    assert "/Users/" not in text
    for token in _PLACEHOLDERS:
        assert token in text, f"missing placeholder {token}"
