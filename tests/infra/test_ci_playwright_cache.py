"""The e2e job's Playwright browser cache is keyed on Playwright and saved on main.

The browsers Playwright downloads are decided by the installed
``playwright-core`` version alone. Keyed on a hash of the whole app lockfile,
every unrelated dependency bump wrote a fresh copy of the same browsers, and
each pull request saved one more in its own ref scope, which only that pull
request's later runs can read. Those copies push the repository past its
Actions cache budget, and the eviction that follows lands on the image layer
cache every build reads.

So the key names the ``playwright-core`` version read from the lockfile, the job
restores without saving, and a separate save step writes only on the default
branch. The version step is run here against the tracked lockfile and against
a synthetic one, so the check exercises the command CI runs rather than its
text.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Final

import yaml

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
CI_WORKFLOW: Final[Path] = REPO_ROOT / ".github" / "workflows" / "ci.yml"
LOCKFILE: Final[Path] = REPO_ROOT / "app" / "package-lock.json"
E2E_JOB: Final[str] = "playwright-e2e"
CACHE_PATH: Final[str] = "~/.cache/ms-playwright"
VERSION_STEP_ID: Final[str] = "playwright-version"
RESTORE_STEP_ID: Final[str] = "playwright-cache"


def _steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"][E2E_JOB]["steps"]


def _index(steps: list[dict[str, Any]], predicate) -> int:
    matches = [i for i, step in enumerate(steps) if predicate(step)]
    assert len(matches) == 1, matches
    return matches[0]


def _uses(step: dict[str, Any], action: str) -> bool:
    return str(step.get("uses", "")).split("@")[0] == action


def _caches_browsers(step: dict[str, Any]) -> bool:
    return (step.get("with") or {}).get("path") == CACHE_PATH


def _run_version_step(cwd: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    steps = _steps()
    command = steps[_index(steps, lambda s: s.get("id") == VERSION_STEP_ID)]["run"]
    output = tmp_path / "github_output"
    output.write_text("")
    env = dict(os.environ, GITHUB_OUTPUT=str(output))
    # The runner's default for a `run:` step that declares no `shell:` is
    # `bash -e {0}`: -e without pipefail or -u.
    return subprocess.run(
        ["bash", "-e", "-c", command],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


def _output_lines(tmp_path: Path) -> list[str]:
    return (tmp_path / "github_output").read_text().splitlines()


def test_no_step_both_restores_and_saves_the_browsers():
    """The combined action saves on every miss, pull requests included."""
    offenders = [s for s in _steps() if _uses(s, "actions/cache") and _caches_browsers(s)]
    assert not offenders, offenders


def test_browser_cache_key_names_the_playwright_version():
    steps = _steps()
    restore = steps[
        _index(steps, lambda s: _uses(s, "actions/cache/restore") and _caches_browsers(s))
    ]
    assert restore.get("id") == RESTORE_STEP_ID
    key = restore["with"]["key"]
    assert f"steps.{VERSION_STEP_ID}.outputs.version" in key
    assert "hashFiles" not in key


def test_save_writes_the_restored_key_and_only_on_main():
    steps = _steps()
    restore = steps[
        _index(steps, lambda s: _uses(s, "actions/cache/restore") and _caches_browsers(s))
    ]
    save = steps[_index(steps, lambda s: _uses(s, "actions/cache/save") and _caches_browsers(s))]
    assert save["with"]["key"] == restore["with"]["key"]
    # Compared whole: a containment check passes a negated or widened condition.
    condition = " ".join(str(save.get("if", "")).split())
    assert condition == (
        f"github.ref == 'refs/heads/main' && steps.{RESTORE_STEP_ID}.outputs.cache-hit != 'true'"
    )


def test_steps_run_in_dependency_order():
    """Version before restore; save only after the browsers are installed."""
    steps = _steps()
    version = _index(steps, lambda s: s.get("id") == VERSION_STEP_ID)
    restore = _index(steps, lambda s: _uses(s, "actions/cache/restore") and _caches_browsers(s))
    install = _index(steps, lambda s: "playwright install --with-deps" in str(s.get("run", "")))
    save = _index(steps, lambda s: _uses(s, "actions/cache/save") and _caches_browsers(s))
    assert version < restore < install < save


def test_version_step_emits_the_locked_playwright_core_version(tmp_path):
    locked = json.loads(LOCKFILE.read_text(encoding="utf-8"))["packages"][
        "node_modules/playwright-core"
    ]["version"]
    result = _run_version_step(REPO_ROOT, tmp_path)
    assert result.returncode == 0, result.stderr
    assert _output_lines(tmp_path) == [f"version={locked}"]


def test_control_version_step_follows_the_lockfile(tmp_path):
    """A synthetic lockfile's version comes back, so the output is not a constant."""
    tree = tmp_path / "tree"
    (tree / "app").mkdir(parents=True)
    lock = {"packages": {"node_modules/playwright-core": {"version": "9.8.7"}}}
    (tree / "app" / "package-lock.json").write_text(json.dumps(lock))
    result = _run_version_step(tree, tmp_path)
    assert result.returncode == 0, result.stderr
    assert _output_lines(tmp_path) == ["version=9.8.7"]


def test_control_version_step_fails_without_playwright_core(tmp_path):
    """No locked version must stop the job rather than key the cache on nothing."""
    tree = tmp_path / "tree"
    (tree / "app").mkdir(parents=True)
    (tree / "app" / "package-lock.json").write_text(json.dumps({"packages": {}}))
    result = _run_version_step(tree, tmp_path)
    assert result.returncode != 0
    assert not any(line.startswith("version=") for line in _output_lines(tmp_path))
