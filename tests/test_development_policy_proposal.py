"""Check the inactive proposal's composition without installing it."""

import json
import re
from fnmatch import fnmatch
from pathlib import Path

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
    path = PROPOSED / "project.json"
    assert path.is_file(), "inactive executable binding proposal is missing"
    return json.loads(path.read_text())


def test_core_operations_have_required_project_policy() -> None:
    bindings = profile()["bindings"]
    policy = next(item for item in bindings if item["id"] == "cas-context")
    assert set(policy["operations"]) == CORE
    assert policy["required"] is True
    assert policy["source"]["path"] == "docs/development/project-policy.md"
    for binding in bindings:
        assert binding["required"] is True
        assert (ROOT / binding["source"]["path"]).is_file()


def test_review_replaces_only_supported_review_operations() -> None:
    bindings = profile()["bindings"]
    replacements = [item for item in bindings if item["composition"] == "replace"]
    assert len(replacements) == 1
    review = replacements[0]
    assert review["responsibility"] == "review"
    assert set(review["operations"]) == {"commit", "review-pr"}
    assert review["source"]["path"] == ".claude/skills/cas-code-review/SKILL.md"


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


def test_proposal_does_not_install_governing_pointer() -> None:
    assert not (ROOT / ".development-skills/project.json").exists()
    for name in ("AGENTS.md", "CLAUDE.md"):
        text = (ROOT / name).read_text()
        assert "docs/development/project-policy.md" not in text
        assert "docs/development/proposed/" not in text


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
