"""Manual, serialized Azure migration orchestration; never runs on import.

The resource-group fence survives a failed job or runner. Only the separate
serving deployment, after its health checks, can advance it to serving.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

Azure = Callable[..., Any]
ROOT = Path(__file__).resolve().parents[1]


def azure(*args: str) -> Any:
    executable = shutil.which("az")
    if executable is None:
        raise RuntimeError("Azure CLI is required")
    # Arguments are separate strings; deployment values never enter a shell.
    result = subprocess.run(  # noqa: S603
        [executable, *args, "--output", "json", "--only-show-errors"],
        check=True,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


def output(deployment: dict, name: str) -> str:
    value = deployment["properties"]["outputs"][name]["value"]
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing deployment output: {name}")
    return value


def coordinates(
    az: Azure, environment: str, group: str, generation: str
) -> tuple[dict, dict, dict]:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,11}", generation):
        raise ValueError("invalid generation")
    deployment = az("deployment", "sub", "show", "--name", environment)
    job = az(
        "containerapp",
        "job",
        "show",
        "--resource-group",
        group,
        "--name",
        output(deployment, "bootstrapJobName"),
    )
    env = {
        entry["name"]: entry.get("value", "")
        for entry in job["properties"]["template"]["containers"][0]["env"]
    }
    for key in ("PG_ADMIN_USER", "AZURE_CLIENT_ID", "SAGE_DB_ROLE", "BFF_DB_ROLE", "PG_DATABASE"):
        if not env.get(key):
            raise ValueError(f"bootstrap job lacks {key}")
    if env["PG_DATABASE"] != output(deployment, "postgresDatabaseName"):
        raise ValueError("bootstrap database differs from serving deployment")
    return deployment, job, env


def replacement(az: Azure, group: str, generation: str) -> dict:
    return az(
        "deployment",
        "group",
        "show",
        "--resource-group",
        group,
        "--name",
        f"postgres-replacement-{generation}",
    )


def check_servers(az: Azure, group: str, source: str, target: str) -> dict:
    if source == target:
        raise ValueError("source and replacement are identical")
    versions = json.loads((ROOT / "versions.json").read_text())["postgres"]
    answer = {}
    for fqdn, major in ((source, versions["deploy_major"]), (target, versions["dev_major"])):
        server = az(
            "postgres",
            "flexible-server",
            "show",
            "--resource-group",
            group,
            "--name",
            fqdn.split(".")[0],
        )
        if (
            server["state"] != "Ready"
            or server["version"] != major
            or not server["network"].get("delegatedSubnetResourceId")
        ):
            raise ValueError(
                "server readiness, major, or private networking differs from the migration contract"
            )
        if fqdn == target:
            if server["backup"] != {
                **server["backup"],
                "backupRetentionDays": 35,
                "geoRedundantBackup": "Enabled",
            }:
                raise ValueError("replacement backup policy is not ready")
            locks = az(
                "lock",
                "list",
                "--resource-group",
                group,
                "--resource-name",
                server["name"],
                "--resource-type",
                "Microsoft.DBforPostgreSQL/flexibleServers",
            )
            if not any(lock.get("level") == "CanNotDelete" for lock in locks):
                raise ValueError("replacement deletion lock is absent")
            answer = server
    return answer


def prepare(az: Azure, environment: str, group: str, generation: str) -> None:
    resource_group = az("group", "show", "--name", group)
    if (resource_group.get("tags") or {}).get("casPostgresMigration"):
        raise ValueError("cannot reconfigure a replacement while a migration fence exists")
    deployment, job, env = coordinates(az, environment, group, generation)
    source = output(deployment, "postgresServerFqdn")
    server = az(
        "postgres",
        "flexible-server",
        "show",
        "--resource-group",
        group,
        "--name",
        source.split(".")[0],
    )
    identity_ids = list(job["identity"]["userAssignedIdentities"])
    if len(identity_ids) != 1:
        raise ValueError("bootstrap must select exactly one administrator identity")
    identity_id = identity_ids[0]
    identity = az("identity", "show", "--ids", identity_id)
    subnet = server["network"]["delegatedSubnetResourceId"]
    az(
        "deployment",
        "group",
        "create",
        "--resource-group",
        group,
        "--name",
        f"postgres-replacement-{generation}",
        "--template-file",
        "infra/postgres-replacement.bicep",
        "--parameters",
        f"location={server['location']}",
        f"environmentName={environment}",
        f"serverGeneration={generation}",
        f"delegatedSubnetId={subnet}",
        f"vnetId={subnet.rsplit('/subnets/', 1)[0]}",
        f"aadAdminObjectId={identity['principalId']}",
        f"aadAdminPrincipalName={env['PG_ADMIN_USER']}",
        f"databaseName={env['PG_DATABASE']}",
    )
    target = output(replacement(az, group, generation), "postgresServerFqdn")
    check_servers(az, group, source, target)
    container = job["properties"]["template"]["containers"][0]
    image = container["image"]
    if image.endswith(":latest") or (":" not in image and "@sha256:" not in image):
        raise ValueError("bootstrap image is not pinned")
    az(
        "deployment",
        "group",
        "create",
        "--resource-group",
        group,
        "--name",
        f"postgres-migration-job-{generation}",
        "--template-file",
        "infra/modules/postgres-migration-job.bicep",
        "--parameters",
        f"location={job['location']}",
        f"environmentName={environment}",
        f"acaEnvironmentId={job['properties']['environmentId']}",
        f"image={image}",
        f"acrLoginServer={job['properties']['configuration']['registries'][0]['server']}",
        f"identityId={identity_id}",
        f"identityClientId={env['AZURE_CLIENT_ID']}",
        f"adminUser={env['PG_ADMIN_USER']}",
        f"sourceFqdn={source}",
        f"targetFqdn={target}",
        f"databaseName={env['PG_DATABASE']}",
        f"sageRole={env['SAGE_DB_ROLE']}",
        f"bffRole={env['BFF_DB_ROLE']}",
        f"runId={generation}",
    )
    run_preflight(az, group, f"job-pg-migration-{environment}", sleep=time.sleep)
    print("Replacement and migration permissions verified. Serving consumers are unchanged.")


def migrate(
    az: Azure,
    environment: str,
    group: str,
    generation: str,
    confirm: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    if confirm != generation or not confirm:
        raise ValueError("migration requires the generation as downtime confirmation")
    deployment, bootstrap_job, bootstrap_env = coordinates(az, environment, group, generation)
    source = output(deployment, "postgresServerFqdn")
    target = output(replacement(az, group, generation), "postgresServerFqdn")
    check_servers(az, group, source, target)
    migration_job = f"job-pg-migration-{environment}"
    configured = az(
        "containerapp", "job", "show", "--resource-group", group, "--name", migration_job
    )
    env = {
        entry["name"]: entry.get("value")
        for entry in configured["properties"]["template"]["containers"][0]["env"]
    }
    versions = json.loads((ROOT / "versions.json").read_text())["postgres"]
    expected_env = {
        key: bootstrap_env[key]
        for key in (
            "PG_DATABASE",
            "PG_ADMIN_USER",
            "AZURE_CLIENT_ID",
            "SAGE_DB_ROLE",
            "BFF_DB_ROLE",
        )
    }
    expected_env.update(
        PG_SOURCE_MAJOR=versions["deploy_major"], PG_TARGET_MAJOR=versions["dev_major"]
    )
    if (
        any(env.get(key) != value for key, value in expected_env.items())
        or configured["properties"]["template"]["containers"][0]["image"]
        != bootstrap_job["properties"]["template"]["containers"][0]["image"]
    ):
        raise ValueError(
            "prepared migration job has stale database, identity, major, or image settings"
        )
    if any(
        env.get(key) != value
        for key, value in {
            "PG_SOURCE_FQDN": source,
            "PG_TARGET_FQDN": target,
            "PG_MIGRATION_RUN_ID": generation,
        }.items()
    ):
        raise ValueError(
            "prepared migration job does not match the selected servers and generation"
        )
    for name in (
        output(deployment, "bootstrapJobName"),
        output(deployment, "maintenanceJobName"),
        migration_job,
    ):
        executions = az(
            "containerapp", "job", "execution", "list", "--resource-group", group, "--name", name
        )
        if any(
            execution["properties"]["status"] not in ("Succeeded", "Failed", "Stopped")
            for execution in executions
        ):
            raise ValueError("another database job is still active")
    resource_group = az("group", "show", "--name", group)
    state = (resource_group.get("tags") or {}).get("casPostgresMigration", "")
    if state not in ("", f"copying:{generation}", f"verified:{generation}"):
        raise ValueError("another migration or serving generation owns the fence")

    # Recheck permissions immediately before any downtime, including on retries.
    run_preflight(az, group, migration_job, sleep=sleep)

    def tag(state: str) -> None:
        az(
            "tag",
            "update",
            "--resource-id",
            resource_group["id"],
            "--operation",
            "Merge",
            "--tags",
            f"casPostgresMigration={state}:{generation}",
        )

    app_names = (
        output(deployment, "sageContainerAppName"),
        output(deployment, "bffContainerAppName"),
    )
    saved = (resource_group.get("tags") or {}).get("casPostgresMigrationRevisions")
    if not saved:
        revisions = {
            name: [
                revision["name"]
                for revision in az(
                    "containerapp", "revision", "list", "--resource-group", group, "--name", name
                )
                if revision["properties"]["active"]
            ]
            for name in app_names
        }
        if not all(revisions.values()):
            raise ValueError("cannot establish the active source revisions")
        saved = json.dumps(revisions, separators=(",", ":"))
        if len(saved) > 256:
            raise ValueError("revision recovery record exceeds the resource tag limit")
        az(
            "tag",
            "update",
            "--resource-id",
            resource_group["id"],
            "--operation",
            "Merge",
            "--tags",
            f"casPostgresMigrationRevisions={saved}",
        )
    revisions = json.loads(saved)
    if set(revisions) != set(app_names):
        raise ValueError("saved source revisions belong to different apps")
    tag("copying")
    for names in revisions.values():
        for revision in names:
            az(
                "containerapp",
                "revision",
                "deactivate",
                "--resource-group",
                group,
                "--revision",
                revision,
            )
    run_mode(az, group, migration_job, "migrate", sleep=sleep)
    tag("verified")
    print(
        "Migration verified. Apps remain stopped and source CONNECT remains fenced. "
        "Use the separate serving deployment for cutover."
    )


def run_preflight(az: Azure, group: str, job: str, *, sleep: Callable[[float], None]) -> None:
    run_mode(az, group, job, "preflight", sleep=sleep)


def run_mode(az: Azure, group: str, job: str, mode: str, *, sleep: Callable[[float], None]) -> None:
    # Update the selected container, then inherit the complete deployed template.
    # Start-time overrides reconstruct the container and drop unstated settings.
    # The workflow's tenant concurrency lock covers this update/start pair.
    az(
        "containerapp",
        "job",
        "update",
        "--resource-group",
        group,
        "--name",
        job,
        "--container-name",
        "migration",
        "--args",
        mode,
    )
    execution = az("containerapp", "job", "start", "--resource-group", group, "--name", job)["name"]
    wait_job(az, group, job, execution, sleep=sleep)


def wait_job(
    az: Azure, group: str, job: str, execution: str, *, sleep: Callable[[float], None]
) -> None:
    deadline = time.monotonic() + 7500
    while time.monotonic() < deadline:
        status = az(
            "containerapp",
            "job",
            "execution",
            "show",
            "--resource-group",
            group,
            "--name",
            job,
            "--job-execution-name",
            execution,
        )["properties"]["status"]
        if status == "Succeeded":
            return
        if status in ("Failed", "Stopped"):
            raise ValueError("database job failed; existing source fence state is unchanged")
        sleep(min(30, max(0, deadline - time.monotonic())))
    raise ValueError("database job polling timed out; existing source fence state is unchanged")


def rollback(
    az: Azure,
    environment: str,
    group: str,
    generation: str,
    confirm: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    if confirm != generation or not confirm:
        raise ValueError("rollback requires the generation as confirmation")
    resource_group = az("group", "show", "--name", group)
    tags = resource_group.get("tags") or {}
    if tags.get("casPostgresMigration") not in (f"copying:{generation}", f"verified:{generation}"):
        raise ValueError("rollback is refused once cutover has begun")
    deployment, _, _ = coordinates(az, environment, group, generation)
    revisions = json.loads(tags["casPostgresMigrationRevisions"])
    if set(revisions) != {
        output(deployment, "sageContainerAppName"),
        output(deployment, "bffContainerAppName"),
    }:
        raise ValueError("rollback revisions do not match the serving deployment")
    for name in (
        output(deployment, "bootstrapJobName"),
        output(deployment, "maintenanceJobName"),
        f"job-pg-migration-{environment}",
    ):
        executions = az(
            "containerapp", "job", "execution", "list", "--resource-group", group, "--name", name
        )
        if any(
            item["properties"]["status"] not in ("Succeeded", "Failed", "Stopped")
            for item in executions
        ):
            raise ValueError("wait for all database jobs to terminate before rollback")
    # The standing bootstrap still points at the incumbent before any cutover.
    # It converges the standard workload CONNECT/CREATE and extension grants.
    bootstrap = output(deployment, "bootstrapJobName")
    execution = az("containerapp", "job", "start", "--resource-group", group, "--name", bootstrap)[
        "name"
    ]
    wait_job(az, group, bootstrap, execution, sleep=sleep)
    for names in revisions.values():
        for revision in names:
            az(
                "containerapp",
                "revision",
                "activate",
                "--resource-group",
                group,
                "--revision",
                revision,
            )
    az(
        "tag",
        "update",
        "--resource-id",
        resource_group["id"],
        "--operation",
        "Delete",
        "--tags",
        f"casPostgresMigration={tags['casPostgresMigration']}",
        f"casPostgresMigrationRevisions={tags['casPostgresMigrationRevisions']}",
    )
    print(
        "Source grants and revisions restored. Run the normal serving deployment preflight. "
        "The replacement is retained for inspection."
    )


def main() -> None:
    environment, group, generation = (
        os.environ[key] for key in ("ENVIRONMENT_NAME", "RESOURCE_GROUP_NAME", "GENERATION")
    )
    action = os.environ["MIGRATION_ACTION"]
    if action == "prepare":
        prepare(azure, environment, group, generation)
    elif action == "migrate":
        migrate(azure, environment, group, generation, os.environ.get("MIGRATION_CONFIRM", ""))
    elif action == "rollback":
        rollback(azure, environment, group, generation, os.environ.get("MIGRATION_CONFIRM", ""))
    else:
        raise ValueError("unknown migration action")


if __name__ == "__main__":
    main()
