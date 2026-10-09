"""The CI model cache is keyed on the pinned model and remote-code revisions.

A static key restores whatever an earlier run cached, whichever commit that
was. Keyed on the pins, a cache is reused only while the pins are unchanged,
and a new pin fetches its own commit. The key is emitted by running the
step's own command, so this exercises what CI runs rather than its text.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Final

import yaml

from sage.adapters.embedding_nomic import MODEL_PINS

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
CI_WORKFLOW: Final[Path] = REPO_ROOT / ".github" / "workflows" / "ci.yml"
TEST_JOB: Final[str] = "test"
PIN_STEP_ID: Final[str] = "model-pins"
CACHE_PATH: Final[str] = "~/.cache/huggingface"


def _steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"][TEST_JOB]["steps"]


def _index(steps: list[dict[str, Any]], predicate) -> int:
    matches = [i for i, step in enumerate(steps) if predicate(step)]
    assert len(matches) == 1, matches
    return matches[0]


def _cache_step_index(steps: list[dict[str, Any]]) -> int:
    return _index(
        steps,
        lambda s: (
            str(s.get("uses", "")).startswith("actions/cache@")
            and (s.get("with") or {}).get("path") == CACHE_PATH
        ),
    )


def test_model_cache_key_is_the_emitted_pin_key():
    steps = _steps()
    key = steps[_cache_step_index(steps)]["with"]["key"]
    assert f"steps.{PIN_STEP_ID}.outputs.key" in key
    assert "rev1" not in key


def test_pin_step_runs_after_install_and_before_the_cache_restore():
    """The pin step needs the installed environment; the restore needs its output."""
    steps = _steps()
    install = _index(steps, lambda s: "uv sync" in str(s.get("run", "")))
    pins = _index(steps, lambda s: s.get("id") == PIN_STEP_ID)
    assert install < pins < _cache_step_index(steps)


def test_pin_step_emits_every_pinned_revision(tmp_path):
    """Running the step's command writes a key naming each pinned commit."""
    steps = _steps()
    command = steps[_index(steps, lambda s: s.get("id") == PIN_STEP_ID)]["run"]
    output = tmp_path / "github_output"
    output.write_text("")
    env = dict(os.environ, GITHUB_OUTPUT=str(output))
    subprocess.run(["bash", "-euo", "pipefail", "-c", command], cwd=REPO_ROOT, env=env, check=True)
    lines = output.read_text().splitlines()
    assert len(lines) == 1 and lines[0].startswith("key="), lines
    for pin in MODEL_PINS.values():
        assert pin.revision in lines[0]
        assert pin.code_revision in lines[0]
