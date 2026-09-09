"""Transition gates: creating a replacement must not move serving consumers."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_replacement_configuration_preserves_incumbent() -> None:
    from tests.infra.test_postgres_module import _resource_block, _strip_line_comments

    module = _strip_line_comments((ROOT / "infra/modules/postgres.bicep").read_text())
    assert "param serverGeneration string = ''" in module
    assert "empty(serverGeneration)" in module
    server = _resource_block(module, "server")
    assert "backupRetentionDays: 35" in server
    assert "geoRedundantBackup: geoRedundantBackup" in server
    assert "param geoRedundantBackup string = 'Disabled'" in module
    replacement = _strip_line_comments((ROOT / "infra/postgres-replacement.bicep").read_text())
    assert "'modules/postgres.bicep'" in replacement
    assert "postgresVersion: versions.postgres.dev_major" in replacement
    assert "geoRedundantBackup: 'Enabled'" in replacement
    assert "serverGeneration: serverGeneration" in replacement
    assert "@minLength(1)" in replacement


def test_provisioning_does_not_switch_consumers() -> None:
    from tests.infra.test_postgres_module import _strip_line_comments

    main = _strip_line_comments((ROOT / "infra/main.bicep").read_text())
    assert "param postgresGeneration string = ''" in main
    assert "serverGeneration: postgresGeneration" in main
    assert "empty(postgresGeneration) ? 'Disabled' : 'Enabled'" in main
    assert "postgresServerName" in main
    assert "postgresServerMajor" in main
    # Every standing consumer uses the SAME selected module, not a second name formula.
    assert main.count("postgresServerFqdn: postgres.outputs.postgresServerFqdn") == 3


def test_mutating_workflows_share_tenant_serialization() -> None:
    for filename in ("infra.yml", "maintenance.yml", "postgres-migration.yml"):
        workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
        expected_group = (
            "cas-tenant-${{ inputs.environment || github.ref }}"
            if filename == "infra.yml"
            else "cas-tenant-${{ inputs.environment }}"
        )
        assert workflow["concurrency"]["group"] == expected_group, filename
        assert workflow["concurrency"]["cancel-in-progress"] is False
        text = (ROOT / ".github/workflows" / filename).read_text()
        assert "postgres-migration-guard.sh" in text, filename


@pytest.mark.parametrize("answer,code,expected", [("17", 0, "17"), ("", 0, ""), ("17", 1, "")])
def test_version_probe_targets_selected_server(
    tmp_path: Path, answer: str, code: int, expected: str
) -> None:
    az = tmp_path / "az"
    az.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" > "$CALLS"\nprintf "%s" "$ANSWER"\nexit "$CODE"\n'
    )
    az.chmod(0o755)
    calls = tmp_path / "calls"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:/usr/bin:/bin",
        "CALLS": str(calls),
        "ANSWER": answer,
        "CODE": str(code),
        "PREFLIGHT_POSTGRES_SERVER_NAME": "selected-server",
    }
    result = subprocess.run(
        ["bash", str(ROOT / "deploy/postgres-version-probe.sh"), "group"],
        env=env,
        text=True,
        capture_output=True,
    )
    assert "flexible-server show" in calls.read_text()
    assert "--name selected-server" in calls.read_text()
    if expected:
        assert result.returncode == 0 and result.stdout.strip() == expected
    else:
        assert result.returncode != 0 and not result.stdout.strip()


def test_migration_client_covers_both_declared_majors() -> None:
    from tests.deploy.test_sage_container_image import _runtime_stage_text
    from tests.helpers.versions import postgres_client_major

    major = postgres_client_major()
    assert f"postgresql-client-{major}" in _runtime_stage_text()


@pytest.mark.parametrize(
    "exists,state,generation,serving,code,allowed",
    [
        ("false", "", "", "", 0, True),
        ("false", "", "g17", "", 0, False),
        ("true", "", "g17", "psql-prod-old", 0, False),
        ("true", "", "g17", "psql-prod-old-g17", 0, True),
        ("true", "copying:g17", "g17", "", 0, False),
        ("true", "verified:g17", "g17", "", 0, True),
        ("true", "cutover:g17", "g17", "", 0, True),
        ("true", "serving:g17", "", "", 0, False),
        ("true", "serving:g17", "g17", "", 0, True),
        ("true", "verified:other", "g17", "", 0, False),
        ("true", "", "", "", 1, False),
    ],
)
def test_deployment_guard(
    tmp_path: Path, exists: str, state: str, generation: str, serving: str, code: int, allowed: bool
) -> None:
    az = tmp_path / "az"
    az.write_text("""#!/bin/sh
[ "$CODE" = 0 ] || exit "$CODE"
case "$*" in
  "containerapp job list "*) exit 0 ;;
  "group exists "*) printf '%s' "$EXISTS" ;;
  "group show "*) [ "$EXISTS" = true ] || exit 1; printf '%s' "$STATE" ;;
  "deployment sub show "*) printf '%s' "$SERVING" ;;
  *) exit 99 ;;
esac
""")
    az.chmod(0o755)
    result = subprocess.run(
        ["bash", str(ROOT / "deploy/postgres-migration-guard.sh"), "deploy", "group", generation],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "EXISTS": exists,
            "STATE": state,
            "SERVING": serving,
            "CODE": str(code),
            "ENVIRONMENT_NAME": "prod",
        },
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is allowed, result.stderr


def test_cutover_fence_survives_resource_group_redeployment() -> None:
    from tests.infra.test_postgres_module import _resource_block, _strip_line_comments

    main = _strip_line_comments((ROOT / "infra/main.bicep").read_text())
    group = _resource_block(main, "rg")
    assert "casPostgresMigration: 'cutover:${postgresGeneration}'" in group
    assert "union(tags," in group


@pytest.mark.parametrize("ref,allowed", [("refs/heads/main", True), ("refs/heads/feature", False)])
def test_migration_dispatch_checks_main_before_login(ref: str, allowed: bool) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/postgres-migration.yml").read_text())
    job = workflow["jobs"]["migration"]
    assert "if" not in job, "wrong-branch dispatch must fail rather than report a skipped success"
    guard = job["steps"][0]
    assert guard["name"] == "Require main"
    result = subprocess.run(
        ["bash", "-e", "-c", guard["run"]],
        env={**os.environ, "GITHUB_REF": ref},
        capture_output=True,
    )
    assert (result.returncode == 0) is allowed
