"""The serving baseline is independent of the retained migration contract."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import yaml

from scripts.check_ruleset_drift import extract_captured_ruleset
from tests.infra.test_postgres_migration_driver import Azure, driver

ROOT = Path(__file__).resolve().parents[2]


def test_serving_baseline_and_retained_migration() -> None:
    postgres = json.loads((ROOT / "versions.json").read_text())["postgres"]
    assert postgres["deploy_major"] == postgres["dev_major"] == "17"
    assert postgres["migration"] == {"source_major": "16", "target_major": "17"}


def test_converged_ci_preserves_required_coverage() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    assert "storage-deploy-floor" not in jobs
    assert jobs["test"]["services"]["postgres"]["image"] == (
        "pgvector/pgvector:pg${{ needs.versions.outputs.postgres_dev_major }}"
    )
    commands = [step.get("run", "") for step in jobs["test"]["steps"]]
    suite = [command for command in commands if ".venv/bin/pytest" in command]
    assert len(suite) == 1
    assert suite[0].strip().endswith("tests/")
    assert "--cov=sage --cov=app" in suite[0]
    ruleset = extract_captured_ruleset((ROOT / "docs/process/branch_protection.md").read_text())
    checks = next(rule for rule in ruleset["rules"] if rule["type"] == "required_status_checks")
    assert {check["context"] for check in checks["parameters"]["required_status_checks"]} == {
        "test",
        "lint",
        "lint-imports",
        "gitleaks",
        "eslint",
        "versions",
    }


def test_compiled_serving_major_ignores_development_bump(tmp_path: Path) -> None:
    dev_major = "18"
    shutil.copytree(ROOT / "infra", tmp_path / "infra")
    manifest = json.loads((ROOT / "versions.json").read_text())
    manifest["postgres"]["dev_major"] = dev_major
    (tmp_path / "versions.json").write_text(json.dumps(manifest))
    result = subprocess.run(
        ["az", "bicep", "build", "--file", str(tmp_path / "infra/main.bicep"), "--stdout"],
        capture_output=True,
        text=True,
        check=True,
    )
    template = json.loads(result.stdout)
    expression = template["variables"]["postgresMajor"]
    assert ".dev_major" not in expression
    assert ".migration.target_major" in expression
    selection = re.fullmatch(
        r"\[if\(empty\(parameters\('postgresGeneration'\)\), "
        r"variables\('([^']+)'\)\.postgres\.deploy_major, "
        r"variables\('([^']+)'\)\.postgres\.migration\.target_major\)\]",
        expression,
    )
    assert selection
    loaded = template["variables"][selection[2]]["postgres"]
    assert loaded["dev_major"] == dev_major
    assert loaded["migration"]["target_major"] == "17"
    postgres = next(
        r
        for r in template["resources"]
        if "postgresVersion" in r.get("properties", {}).get("parameters", {})
    )
    assert (
        postgres["properties"]["parameters"]["postgresVersion"]["value"]
        == "[variables('postgresMajor')]"
    )
    assert (
        postgres["properties"]["parameters"]["serverGeneration"]["value"]
        == "[parameters('postgresGeneration')]"
    )


def test_migration_server_validation_uses_fixed_endpoints() -> None:
    module = driver()
    az = Azure()
    target = module.check_servers(
        az, "group", "source.postgres.database.azure.com", "target.postgres.database.azure.com"
    )
    assert target.get("name") == "target" and target.get("version") == "17"
    assert [
        call[call.index("--name") + 1]
        for call in az.calls
        if call[:3] == ("postgres", "flexible-server", "show")
    ] == ["source", "target"]
