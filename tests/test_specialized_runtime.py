"""Source routing contracts; actual agent obedience is separately observed in trials."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "docs/development/operations"


@pytest.mark.parametrize("name", ["deploy", "azure-review", "smoke-test", "batch"])
def test_procedures_enter_actual_host_selector(name: str) -> None:
    source = (OPS / f"{name}.md").read_text()
    assert "[Runtime selection](references/runtime.md)" in source
    assert "references/codex-runtime.md" not in source
    assert "collaboration.spawn_agent" not in source
    for href in re.findall(r"\]\((references/[^)#]+)", source):
        assert (OPS / href).is_file(), href


@pytest.mark.parametrize("host", ["codex", "claude"])
def test_selector_closes_over_both_host_adapters(host: str) -> None:
    source = (OPS / "references/runtime.md").read_text()
    assert f"({host}-runtime.md)" in source
    assert "project-policy/references/runtime.md" in source
    assert "actual executing host" in source
    assert "Unknown or ambiguous" in source
    assert "directory" in source
    adapter = (OPS / f"references/{host}-runtime.md").read_text()
    assert f"{host}-workflow-runtime.md" in adapter
    assert "terminal" in adapter


def test_batch_contract_preserves_writer_and_completion_boundaries() -> None:
    source = (OPS / "references/runtime.md").read_text()
    for requirement in [
        "one active writer",
        "STATUS",
        "terminal",
        "focused continuation",
        "stopped",
        "requested",
        "observed",
        "wrong",
        "unknown",
    ]:
        assert requirement in source
    batch = (OPS / "batch.md").read_text()
    assert "worker lifecycle" in batch
    assert "## Host dispatch and recovery constraints" in batch
    assert "Fresh Codex" not in batch
    assert "Do not inspect or change Claude-style isolation settings" not in batch


def test_deploy_uses_selected_wait_mechanics() -> None:
    source = (OPS / "deploy.md").read_text()
    assert "Silent-loop mechanics for Codex" not in source
    assert "available Codex execution/session" not in source
    assert "selected host adapter" in source
    for retained in [
        "3000-second",
        "120 seconds",
        "Do not reset the deadline",
        "one exact-SHA lookup",
        "stop the local watcher",
    ]:
        assert retained in source


@pytest.mark.parametrize("name", ["deploy", "azure-deploy-review", "batch"])
def test_distribution_entrypoints_require_actual_host_selection(name: str) -> None:
    source = (ROOT / f"docs/development/distribution/skills/{name}/SKILL.md").read_text()
    assert "docs/development/operations/references/runtime.md" in source
    assert "actual executing host" in source
