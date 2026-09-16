"""Check selected candidate composition without activating live authority."""

import json
import re
import shutil
from fnmatch import fnmatch
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROPOSED = ROOT / "docs/development/proposed"
CORE = {
    "kickoff",
    "commit",
    "review-pr",
    "disposition",
    "converge-review",
    "merge",
    "push",
    "pull-request",
}


def profile() -> dict:
    path = ROOT / ".development-skills/project.json"
    assert path.is_file(), "selected executable binding candidate is missing"
    return json.loads(path.read_text())


def test_core_operations_have_required_project_policy() -> None:
    bindings = profile()["bindings"]
    policy = next(item for item in bindings if item["id"] == "cas-context")
    assert set(policy["operations"]) == CORE
    assert policy["required"] is True
    assert policy["source"]["path"] == "docs/development/project-policy.md"
    for binding in bindings:
        assert binding["required"] is True
        if binding["id"] == "cas-activation-receipt":
            assert binding["source"]["path"] == ".development-skills/activation-receipt.md"
        else:
            assert (ROOT / binding["source"]["path"]).is_file()


def test_review_replaces_only_supported_review_operations() -> None:
    bindings = profile()["bindings"]
    replacements = [item for item in bindings if item["composition"] == "replace"]
    assert len(replacements) == 1
    review = replacements[0]
    assert review["responsibility"] == "review"
    assert set(review["operations"]) == {"commit", "review-pr"}
    assert review["source"]["path"] == ".claude/skills/cas-code-review/SKILL.md"


def test_ticketing_selects_ticket_procedure_without_enabling_triage_on_writes() -> None:
    bindings = profile()["bindings"]
    selected = [item for item in bindings if "ticketing" in item["operations"]]
    assert {item["id"] for item in selected} == {"cas-ticketing", "cas-activation-receipt"}
    assert selected[0]["composition"] == "supplement"
    assert selected[0]["source"]["path"] == "docs/development/ticket-procedures.md"
    assert not any("next" in item["operations"] for item in bindings)
    # Read-only triage is selected by its explicit consumer, not all ticket writes.
    assert not any(
        item["source"]["path"] == "docs/development/operations/triage.md" for item in selected
    )


def test_conditional_gates_have_precise_candidate_paths() -> None:
    bindings = {item["id"]: item for item in profile()["bindings"]}
    assert set(bindings["cas-real-model"]["paths"]) == {
        "sage/adapters/abstraction_qwen3.py",
        "sage/adapters/embedding_nomic.py",
    }
    for path in bindings["cas-real-model"]["paths"]:
        assert (ROOT / path).is_file(), f"real-model adapter missing: {path}"
    assert bindings["cas-azure-review"]["composition"] == "supplement"


def test_azure_scope_covers_existing_deployment_and_image_inputs() -> None:
    azure = next(item for item in profile()["bindings"] if item["id"] == "cas-azure-review")
    assert set(azure["operations"]) == {"commit", "review-pr", "disposition"}
    witnesses = {
        "infra/main.bicep",
        "deploy/bootstrap/entra-app-registrations.sh",
        ".github/workflows/build-images.yml",
        ".github/workflows/postgres-migration.yml",
        ".github/workflows/maintenance.yml",
        ".github/workflows/sharepoint-validate.yml",
        "Dockerfile",
        "Dockerfile.bff",
        "pyproject.toml",
        "uv.lock",
        "scripts/bootstrap_postgres.py",
    }
    assert all((ROOT / path).is_file() for path in witnesses)
    missing = {
        path for path in witnesses if not any(fnmatch(path, pattern) for pattern in azure["paths"])
    }
    assert not missing, f"deployment/image inputs omitted: {sorted(missing)}"
    assert not any(fnmatch("docs/development/release.md", pattern) for pattern in azure["paths"])


def test_candidate_selection_is_explicit_and_live_activation_fails_closed() -> None:
    guide = (ROOT / "CLAUDE.md").read_text()
    assert "docs/development/project-policy.md" in guide
    assert ".development-skills/project.json" in guide
    assert "cas-triage" in guide and "declared_in" in guide
    bindings = profile()["bindings"]
    receipt = next(row for row in bindings if row["id"] == "cas-activation-receipt")
    assert receipt["required"] is True
    assert set(receipt["operations"]) == {op for row in bindings for op in row["operations"]}
    assert receipt["source"]["path"] == ".development-skills/activation-receipt.md"


def test_azure_scope_covers_declared_python_execution_roots() -> None:
    modules = set()
    declarations = [ROOT / "Dockerfile", ROOT / "Dockerfile.bff", *ROOT.glob("infra/**/*.bicep")]
    for declaration in declarations:
        content = declaration.read_text()
        if declaration.suffix == ".bicep":
            assert len(re.findall(r"\bcommand:", content)) == len(
                re.findall(r"\bcommand:\s*\[", content)
            ), "nonliteral job commands need explicit execution-root coverage"
        arrays = re.findall(r"(?:ENTRYPOINT|command:)\s*(\[[^\]]+\])", content, re.S)
        for array in arrays:
            command = re.findall(r"[\"']([^\"']+)[\"']", array)
            if "-m" in command:
                modules.add(command[command.index("-m") + 1])
    expected_modules = {
        "sage",
        "app.backend",
        "sage.storage.postgres.cloud_bootstrap",
        "sage.maintenance.cloud_job",
        "sage.maintenance.postgres_migration",
        "sage.maintenance.postgres_restore_verify",
    }
    assert expected_modules <= modules, (
        f"execution declaration population missing: {sorted(expected_modules - modules)}"
    )
    azure = next(item for item in profile()["bindings"] if item["id"] == "cas-azure-review")
    omitted = set()
    for module in modules:
        relative = module.replace(".", "/")
        path = relative + ".py"
        if not (ROOT / path).is_file():
            path = relative + "/__main__.py"
        assert (ROOT / path).is_file(), f"declared execution root missing: {module}"
        if not any(fnmatch(path, pattern) for pattern in azure["paths"]):
            omitted.add(path)
    assert not omitted, f"declared execution roots omitted: {sorted(omitted)}"


def test_specialized_operations_have_required_canonical_bindings() -> None:
    bindings = {item["id"]: item for item in profile()["bindings"]}
    for operation in ("smoke-test", "deploy", "batch"):
        binding = bindings["cas-" + operation]
        assert binding["operations"] == [operation]
        assert binding["responsibility"] == operation + "-procedure"
        assert binding["required"] is True
        assert binding["source"]["path"] == f"docs/development/operations/{operation}.md"
    assert bindings["cas-azure-review"]["source"]["path"] == (
        "docs/development/operations/azure-review.md"
    )


@pytest.mark.parametrize("receipt_present", [False, True])
def test_policy_conformance_accepts_both_activation_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, receipt_present: bool
) -> None:
    source_root = ROOT
    shutil.copytree(source_root / "docs", tmp_path / "docs")
    shutil.copytree(source_root / ".development-skills", tmp_path / ".development-skills")
    canonical = Path(".claude/skills/cas-code-review/SKILL.md")
    (tmp_path / canonical).parent.mkdir(parents=True)
    shutil.copyfile(source_root / canonical, tmp_path / canonical)
    shutil.copyfile(source_root / "CLAUDE.md", tmp_path / "CLAUDE.md")
    receipt = tmp_path / ".development-skills/activation-receipt.md"
    receipt.unlink(missing_ok=True)
    if receipt_present:
        receipt.write_text("Synthetic fixture only; no live authority evidence.\n")
    assert receipt.exists() is receipt_present
    monkeypatch.setattr(__import__(__name__, fromlist=["ROOT"]), "ROOT", tmp_path)
    test_core_operations_have_required_project_policy()
    test_candidate_selection_is_explicit_and_live_activation_fails_closed()
