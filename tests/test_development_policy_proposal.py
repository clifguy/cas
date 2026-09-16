"""Check the inactive proposal's composition without installing it."""

import json
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
