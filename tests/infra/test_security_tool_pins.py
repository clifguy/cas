"""The security workflow's Python tools install from hash-locked requirements.

``security.yml`` runs pip-audit, zizmor and Checkov. Pinning a tool's own
version fixes only the top-level package: its dependencies resolve fresh on
every run, so a compromised release of any one of them would run inside the job
that is meant to catch exactly that. Each tool therefore installs from a
committed requirements file carrying a hash for every package, with
``--require-hashes`` so an unhashed or mismatched package fails the install
rather than being fetched.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Final

import yaml

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
WORKFLOW: Final[Path] = REPO_ROOT / ".github" / "workflows" / "security.yml"
REQUIREMENTS_DIR: Final[Path] = REPO_ROOT / "requirements" / "security"
DEPENDABOT: Final[Path] = REPO_ROOT / ".github" / "dependabot.yml"
VERSIONS: Final[Path] = REPO_ROOT / "versions.json"

_TOOLS: Final[tuple[str, ...]] = ("pip-audit", "zizmor", "checkov")

_REQUIREMENT_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==\S+")


def _run_blocks() -> list[str]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return [
        str(step.get("run") or "")
        for job in (workflow.get("jobs") or {}).values()
        for step in job.get("steps") or []
    ]


def _unhashed_requirements(text: str) -> list[str]:
    """Pinned requirements lacking a ``--hash``, reading continuation lines."""
    entries = re.split(r"\n(?=[A-Za-z0-9])", text.replace("\\\n", " "))
    return [
        entry.split()[0]
        for entry in entries
        if _REQUIREMENT_RE.match(entry) and "--hash=sha256:" not in entry
    ]


def test_each_tool_has_a_fully_hashed_requirements_file() -> None:
    for tool in _TOOLS:
        path = REQUIREMENTS_DIR / f"{tool}.txt"
        assert path.is_file(), f"missing {path.relative_to(REPO_ROOT)}"
        text = path.read_text(encoding="utf-8")
        assert re.search(rf"^{re.escape(tool)}==", text, re.MULTILINE), (
            f"{path.name} must pin {tool}"
        )
        assert (REQUIREMENTS_DIR / f"{tool}.in").is_file(), f"missing the {tool}.in source"
        assert not _unhashed_requirements(text), f"{path.name}: {_unhashed_requirements(text)}"


def test_workflow_installs_tools_with_required_hashes() -> None:
    runs = "\n".join(_run_blocks())
    assert not re.search(r"\buvx\b", runs), "no tool may run through uvx, which hashes nothing"
    for tool in _TOOLS:
        installs = [
            line for line in runs.splitlines() if f"requirements/security/{tool}.txt" in line
        ]
        assert installs, f"security.yml must install {tool} from requirements/security/{tool}.txt"
        assert all("--require-hashes" in line for line in installs), installs


def test_lock_python_matches_the_declared_version() -> None:
    """Each lock is compiled for the Python the workflows run, declared once in
    versions.json; a bump there must recompile the locks, or the install could
    need wheels the lock does not hash."""
    declared = json.loads(VERSIONS.read_text(encoding="utf-8"))["python"]["version"]
    for tool in _TOOLS:
        header = (REQUIREMENTS_DIR / f"{tool}.txt").read_text(encoding="utf-8")
        match = re.search(r"--python-version (\S+)", header)
        assert match, f"{tool}.txt must record the Python it was compiled for"
        assert match.group(1) == declared, (
            f"{tool}.txt was compiled for Python {match.group(1)}; versions.json declares "
            f"{declared}. Recompile it per the instructions in {tool}.in."
        )


def test_dependabot_tracks_the_tool_requirements() -> None:
    updates = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))["updates"]
    assert any(
        u.get("package-ecosystem") == "pip" and u.get("directory") == "/requirements/security"
        for u in updates
    ), "the hashed tool pins need an update source, or they stop receiving fixes"


def test_control_unhashed_requirement_is_found() -> None:
    text = (
        "a==1.0 \\\n    --hash=sha256:00\n"
        "b==2.0\n"
        "    # via a\n"
        "c==3.0 \\\n    --hash=sha256:11 \\\n    --hash=sha256:22\n"
    )
    assert _unhashed_requirements(text) == ["b==2.0"]
