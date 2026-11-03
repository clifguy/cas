"""The security workflow's Python tools install from hash-locked uv projects.

``security.yml`` runs pip-audit, zizmor and Checkov. Pinning a tool's own
version fixes only the top-level package: its dependencies resolve fresh on
every run, so a compromised release of any one of them would run inside the job
that is meant to catch exactly that. Each tool is therefore its own non-package
uv project under ``requirements/security/<tool>/``, with a committed
``uv.lock`` carrying a hash for every distribution. The workflow exports that
lock and installs it with ``--require-hashes``, so an unhashed or mismatched
package fails the install rather than being fetched.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Final

import yaml

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
WORKFLOW: Final[Path] = REPO_ROOT / ".github" / "workflows" / "security.yml"
REQUIREMENTS_DIR: Final[Path] = REPO_ROOT / "requirements" / "security"
DEPENDABOT: Final[Path] = REPO_ROOT / ".github" / "dependabot.yml"

_TOOLS: Final[tuple[str, ...]] = ("pip-audit", "zizmor", "checkov")


def _run_blocks() -> list[str]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return [
        str(step.get("run") or "")
        for job in (workflow.get("jobs") or {}).values()
        for step in job.get("steps") or []
    ]


def _project(tool: str) -> dict:
    return tomllib.loads((REQUIREMENTS_DIR / tool / "pyproject.toml").read_text(encoding="utf-8"))


def _unhashed_distributions(lock: dict) -> list[str]:
    """Locked registry distributions lacking a sha256 hash.

    The project's own virtual entry carries no distribution and is skipped; any
    other package must ship at least one artifact, and every artifact it lists
    must be hashed.
    """
    unhashed = []
    for package in lock.get("package", []):
        source = package.get("source", {})
        if "virtual" in source or "editable" in source:
            continue
        artifacts = list(package.get("wheels", []))
        if "sdist" in package:
            artifacts.append(package["sdist"])
        if not artifacts or any(
            not str(artifact.get("hash", "")).startswith("sha256:") for artifact in artifacts
        ):
            unhashed.append(f"{package['name']}=={package.get('version')}")
    return unhashed


def test_tool_projects_are_exactly_the_workflow_tools() -> None:
    projects = {path.parent.name for path in REQUIREMENTS_DIR.glob("*/pyproject.toml")}
    assert projects == set(_TOOLS)


def test_each_tool_is_an_exactly_pinned_non_package_project() -> None:
    for tool in _TOOLS:
        project = _project(tool)
        assert project["project"]["name"] == f"security-{tool}"
        assert project["tool"]["uv"]["package"] is False, f"{tool} must not build a package"
        # Any other uv setting (environments, required-environments) could narrow
        # the lock to some platforms while uv lock --check still passes.
        assert set(project["tool"]["uv"]) == {"package"}, f"{tool}: lock must stay universal"
        pins = [d for d in project["project"]["dependencies"] if re.match(rf"{tool}==\S+$", d)]
        assert len(pins) == 1, f"{tool}/pyproject.toml must pin {tool} exactly"


def test_each_tool_lock_hashes_every_distribution() -> None:
    for tool in _TOOLS:
        path = REQUIREMENTS_DIR / tool / "uv.lock"
        assert path.is_file(), f"missing {path.relative_to(REPO_ROOT)}"
        lock = tomllib.loads(path.read_text(encoding="utf-8"))
        names = {package["name"] for package in lock["package"]}
        assert tool in names, f"{tool}/uv.lock must resolve {tool}"
        assert not _unhashed_distributions(lock), f"{tool}: {_unhashed_distributions(lock)}"


def test_each_lock_resolves_the_pinned_version() -> None:
    for tool in _TOOLS:
        (pin,) = [d for d in _project(tool)["project"]["dependencies"] if d.startswith(f"{tool}==")]
        lock = tomllib.loads((REQUIREMENTS_DIR / tool / "uv.lock").read_text(encoding="utf-8"))
        versions = {p["version"] for p in lock["package"] if p["name"] == tool}
        assert versions == {pin.removeprefix(f"{tool}==")}, (tool, versions)


def test_workflow_installs_tools_from_their_locks_with_required_hashes() -> None:
    runs = _run_blocks()
    joined = "\n".join(runs)
    assert not re.search(r"\buvx\b", joined), "no tool may run through uvx, which hashes nothing"
    for tool in _TOOLS:
        project = f"requirements/security/{tool}"
        blocks = [run for run in runs if project in run]
        assert len(blocks) == 1, f"security.yml must install {tool} from {project} once"
        lines = blocks[0].splitlines()
        exports = [line for line in lines if "uv export" in line and project in line]
        assert exports, f"{tool}: install must export its lock"
        assert all("--locked" in line for line in exports), exports
        installs = [line for line in lines if "uv pip install" in line]
        assert installs, f"{tool}: install step must run uv pip install"
        assert all("--require-hashes" in line for line in installs), installs


def test_dependabot_tracks_the_tool_projects() -> None:
    updates = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))["updates"]
    security = [
        u
        for u in updates
        if any(d.startswith("/requirements/security") for d in u.get("directories", []))
        or str(u.get("directory", "")).startswith("/requirements/security")
    ]
    assert len(security) == 1, "the hashed tool pins need exactly one update source"
    (block,) = security
    assert block["package-ecosystem"] == "uv"
    assert "directory" not in block
    assert sorted(block["directories"]) == sorted(f"/requirements/security/{t}" for t in _TOOLS)
    assert block.get("open-pull-requests-limit", 5) > 0


def test_control_unhashed_distribution_is_found() -> None:
    lock = {
        "package": [
            {"name": "security-x", "version": "0", "source": {"virtual": "."}},
            {
                "name": "a",
                "version": "1.0",
                "sdist": {"hash": "sha256:00"},
                "wheels": [{"hash": "sha256:11"}],
            },
            {"name": "b", "version": "2.0", "wheels": [{"hash": "sha256:22"}, {"url": "x"}]},
            {"name": "c", "version": "3.0", "source": {"registry": "https://pypi.org/simple"}},
            {
                "name": "d",
                "version": "4.0",
                "sdist": {"url": "y"},
                "wheels": [{"hash": "sha256:44"}],
            },
        ]
    }
    assert _unhashed_distributions(lock) == ["b==2.0", "c==3.0", "d==4.0"]
