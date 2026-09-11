"""Operator-driven isolated restore drill; control plane only, never on import.

A configured backup is not a demonstrated restore. This driver provisions a
throwaway server from the serving server's own backup, runs the read-only
verification job against it from inside the VNet, and tears the drill down again.
It never connects to a database: every database read happens in the job, under the
job's identity, in a read-only session.

Three refusals are the point of the script rather than incidental validation. A
restore target may not be, or collide with, any server that already exists, so a
drill cannot land on the database of record. A recovery point outside the server's
actual backup window is rejected before any billable resource is created, because
a 35-day retention setting does not mean a server has 35 days of history. And
cleanup deletes only a server carrying this drill's own ownership tag, and proves
the deletion by re-reading rather than by trusting an exit code.

Restoring into the serving subnet and private DNS zone is deliberate and is the
drill's one accepted isolation limitation: an Azure restore of a private-access
server yields another private-access server, which nothing can reach unless it
sits where the job already runs. The restored server is a distinct resource with a
distinct address; no serving binding is touched.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

Azure = Callable[..., Any]
ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "infra/modules/postgres-restore-verify-job.bicep"

# Marks every resource this drill creates. Cleanup adopts nothing without it.
DRILL_TAG = "casRestoreDrill"

REPORT_PREFIX = "CAS_RESTORE_VERIFY_REPORT "

# A throwaway server needs no second copy of its backups in the paired region.
DRILL_GEO_REDUNDANCY = "Disabled"

RUN_ID_PATTERN = re.compile(r"^[a-z0-9]{1,16}$")


def azure(*args: str) -> Any:
    executable = shutil.which("az")
    if executable is None:
        raise RuntimeError("Azure CLI is required")
    # Arguments are separate strings; no value ever enters a shell.
    result = subprocess.run(  # noqa: S603
        [executable, *args, "--output", "json", "--only-show-errors"],
        check=False,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    if result.returncode != 0:
        # Carry the CLI's own message. A drill that reports only an exit status
        # tells the operator that something failed but not what Azure objected
        # to, which is the one thing they need in order to decide what to do.
        raise RuntimeError(
            f"az {' '.join(args[:3])} failed with status {result.returncode}: "
            f"{result.stderr.strip() or '(no stderr)'}"
        )
    return json.loads(result.stdout) if result.stdout.strip() else None


def output(deployment: dict, name: str) -> str:
    value = deployment["properties"]["outputs"][name]["value"]
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing deployment output: {name}")
    return value


def coordinates(az: Azure, environment: str, group: str) -> tuple[dict, dict, dict]:
    """The serving deployment, its bootstrap job, and that job's settings."""
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


def servers(az: Azure, group: str) -> list[dict]:
    return az("postgres", "flexible-server", "list", "--resource-group", group) or []


def drill_server_name(serving_name: str, run_id: str) -> str:
    if not RUN_ID_PATTERN.match(run_id):
        raise ValueError("drill run id must be 1-16 lowercase alphanumerics")
    name = f"{serving_name}-rv{run_id}"
    if len(name) > 63:
        raise ValueError(f"drill server name exceeds 63 characters: {name}")
    return name


def assert_target_is_new(existing: list[dict], target: str) -> None:
    names = {server["name"] for server in existing}
    if target in names:
        raise ValueError(
            f"refusing to restore over the existing server {target!r}; choose a new "
            "drill run id or clean up the previous drill first"
        )


def assert_within_window(server: dict, restore_time: str) -> datetime:
    """A recovery point outside the real window is refused before it costs money."""
    point = datetime.fromisoformat(restore_time)
    if point.tzinfo is None:
        raise ValueError("restore time must carry an explicit UTC offset")
    earliest = datetime.fromisoformat(server["backup"]["earliestRestoreDate"])
    now = datetime.now(timezone.utc)
    if point < earliest:
        raise ValueError(
            f"restore point {point.isoformat()} precedes the server's earliest "
            f"restore point {earliest.isoformat()}; the retention setting is a "
            "ceiling, not the history the server has actually accumulated"
        )
    if point > now:
        raise ValueError(f"restore point {point.isoformat()} is in the future")
    return point


def provision(
    az: Azure,
    *,
    environment: str,
    group: str,
    run_id: str,
    restore_time: str,
    geo_location: str | None = None,
    geo_subnet: str | None = None,
    geo_dns_zone: str | None = None,
) -> dict:
    if geo_location and not (geo_subnet and geo_dns_zone):
        # Measured against the live tenant: Azure refuses to geo-restore a
        # private-access server without network parameters, and will not place
        # it on a public endpoint instead. Regional recovery therefore needs a
        # delegated subnet and a private DNS zone *in the destination region*
        # before it can be exercised at all. Refuse here rather than issuing a
        # call the control plane rejects, so the prerequisite is legible.
        raise ValueError(
            f"a geo-restore into {geo_location!r} requires --geo-subnet and "
            "--geo-dns-zone naming a delegated subnet and private DNS zone in "
            "that region; Azure will not geo-restore a private-access server "
            "onto a public endpoint"
        )
    deployment, _job, _env = coordinates(az, environment, group)
    serving_name = output(deployment, "postgresServerName")
    serving_fqdn = output(deployment, "postgresServerFqdn")
    existing = servers(az, group)
    serving = next((s for s in existing if s["name"] == serving_name), None)
    if serving is None:
        raise ValueError(f"serving server {serving_name!r} not found in {group!r}")
    target = drill_server_name(serving_name, run_id)
    assert_target_is_new(existing, target)
    assert_within_window(serving, restore_time)

    network = serving["network"]
    command = [
        "postgres",
        "flexible-server",
        "geo-restore" if geo_location else "restore",
        "--resource-group",
        group,
        "--name",
        target,
        "--source-server",
        serving_name,
        "--restore-time",
        restore_time,
        "--geo-redundant-backup",
        DRILL_GEO_REDUNDANCY,
        "--yes",
    ]
    if geo_location:
        command += [
            "--location",
            geo_location,
            "--subnet",
            geo_subnet,
            "--private-dns-zone",
            geo_dns_zone,
        ]
    else:
        # Same subnet and zone as the serving server: an Azure restore of a
        # private-access server has nowhere else the verification job can reach.
        command += [
            "--subnet",
            network["delegatedSubnetResourceId"],
            "--private-dns-zone",
            network["privateDnsZoneArmResourceId"],
        ]
    restored = az(*command)
    # Incremental: a plain tag write replaces the whole collection, and the
    # restore inherits the source's own tags. Adding the ownership mark must not
    # be the operation that erases them.
    az(
        "resource",
        "tag",
        "--is-incremental",
        "--tags",
        f"{DRILL_TAG}={run_id}",
        "--ids",
        restored["id"],
    )
    return {
        "target": target,
        "target_fqdn": restored["fullyQualifiedDomainName"],
        "serving_name": serving_name,
        "serving_fqdn": serving_fqdn,
        "restore_time": restore_time,
        "mode": "geo-restore" if geo_location else "point-in-time",
        "run_id": run_id,
    }


def verify_job_name(environment: str) -> str:
    return f"job-pg-restore-verify-{environment}"


def assert_usable_image(candidate: str, deployed: str) -> str:
    """An image the drill may run in the serving network, pinned and in-registry.

    A drill legitimately runs code the tenant has not deployed: the verification
    entrypoint is new until the change that adds it lands. So an override is
    allowed, but only within the tenant's own registry and only with an explicit
    tag. Anything else would let a drill pull an arbitrary image into the subnet
    that holds the database of record.
    """
    repository, _, tag = candidate.rpartition(":")
    if not repository or "/" not in repository or "/" in tag:
        raise ValueError(f"refusing an unpinned image reference: {candidate}")
    if tag in ("", "latest"):
        raise ValueError(f"refusing an unpinned image reference: {candidate}")
    registry = candidate.split("/", 1)[0]
    if registry != deployed.split("/", 1)[0]:
        raise ValueError(
            f"refusing an image from {registry!r}; the drill may only run images "
            f"from the tenant's own registry {deployed.split('/', 1)[0]!r}"
        )
    return candidate


def verify(
    az: Azure,
    *,
    environment: str,
    group: str,
    run_id: str,
    target_fqdn: str,
    restore_time: str,
    sentinel_schema: str,
    sentinel_before: str,
    sentinel_after: str,
    image: str | None = None,
    poll_seconds: float = 20.0,
    # The job's own replica timeout plus a margin for scheduling and image pull.
    budget_seconds: float = 3900.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    deployment, job, env = coordinates(az, environment, group)
    deployed = job["properties"]["template"]["containers"][0]["image"]
    image = assert_usable_image(image or deployed, deployed)
    identity_id = next(iter(job["identity"]["userAssignedIdentities"]))
    serving_fqdn = output(deployment, "postgresServerFqdn")
    if target_fqdn.casefold() == serving_fqdn.casefold():
        raise ValueError("refusing to verify against the serving server")
    az(
        "deployment",
        "group",
        "create",
        "--resource-group",
        group,
        "--name",
        f"postgres-restore-verify-{run_id}",
        "--template-file",
        str(MODULE),
        "--parameters",
        f"location={job['location']}",
        f"environmentName={environment}",
        f"acaEnvironmentId={job['properties']['environmentId']}",
        f"image={image}",
        f"acrLoginServer={image.split('/', 1)[0]}",
        f"identityId={identity_id}",
        f"identityClientId={env['AZURE_CLIENT_ID']}",
        f"adminUser={env['PG_ADMIN_USER']}",
        f"restoreFqdn={target_fqdn}",
        f"servingFqdn={serving_fqdn}",
        f"databaseName={env['PG_DATABASE']}",
        f"sageRole={env['SAGE_DB_ROLE']}",
        f"bffRole={env['BFF_DB_ROLE']}",
        f"restorePoint={restore_time}",
        f"sentinelSchema={sentinel_schema}",
        f"sentinelBeforeId={sentinel_before}",
        f"sentinelAfterId={sentinel_after}",
    )
    name = verify_job_name(environment)
    az("containerapp", "job", "start", "--resource-group", group, "--name", name)
    return wait_job(
        az,
        group,
        name,
        poll_seconds=poll_seconds,
        budget_seconds=budget_seconds,
        sleep=sleep,
    )


def wait_job(
    az: Azure,
    group: str,
    name: str,
    *,
    poll_seconds: float,
    budget_seconds: float,
    sleep: Callable[[float], None],
) -> dict:
    deadline = time.monotonic() + budget_seconds
    while True:
        executions = (
            az(
                "containerapp",
                "job",
                "execution",
                "list",
                "--resource-group",
                group,
                "--name",
                name,
            )
            or []
        )
        latest = executions[0] if executions else None
        status = (latest or {}).get("properties", {}).get("status")
        if status in ("Succeeded", "Failed", "Stopped"):
            return {"execution": (latest or {}).get("name"), "status": status}
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"verification job {name!r} did not reach a terminal status inside "
                f"{budget_seconds:.0f}s; stop the Azure execution before retrying"
            )
        sleep(poll_seconds)


def cleanup(az: Azure, *, environment: str, group: str, run_id: str) -> dict:
    """Delete only this drill's own resources, and prove they are gone."""
    removed: list[str] = []
    name = verify_job_name(environment)
    jobs = az("containerapp", "job", "list", "--resource-group", group) or []
    if any(entry["name"] == name for entry in jobs):
        az("containerapp", "job", "delete", "--resource-group", group, "--name", name, "--yes")
        removed.append(name)

    for server in servers(az, group):
        tags = server.get("tags") or {}
        if tags.get(DRILL_TAG) != run_id:
            continue
        az(
            "postgres",
            "flexible-server",
            "delete",
            "--resource-group",
            group,
            "--name",
            server["name"],
            "--yes",
        )
        removed.append(server["name"])

    # An accepted delete is not a removal. Re-read both collections.
    surviving_servers = [
        server["name"]
        for server in servers(az, group)
        if (server.get("tags") or {}).get(DRILL_TAG) == run_id
    ]
    surviving_jobs = [
        entry["name"]
        for entry in (az("containerapp", "job", "list", "--resource-group", group) or [])
        if entry["name"] == name
    ]
    if surviving_servers or surviving_jobs:
        raise RuntimeError(
            "drill resources survived their delete: "
            + ", ".join(sorted(surviving_servers + surviving_jobs))
        )
    return {"removed": removed, "run_id": run_id}


def adoptable(az: Azure, group: str, run_id: str) -> list[str]:
    """Servers an operator must inspect by hand: drill-shaped but not drill-tagged."""
    return [
        server["name"]
        for server in servers(az, group)
        if "-rv" in server["name"] and (server.get("tags") or {}).get(DRILL_TAG) != run_id
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Isolated PostgreSQL restore drill")
    parser.add_argument("action", choices=("provision", "verify", "cleanup"))
    parser.add_argument("--environment", required=True)
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--restore-time")
    parser.add_argument("--geo-location")
    parser.add_argument("--geo-subnet", help="Delegated subnet id in the geo-restore region.")
    parser.add_argument("--geo-dns-zone", help="Private DNS zone id in the geo-restore region.")
    parser.add_argument("--target-fqdn")
    parser.add_argument("--sentinel-schema")
    parser.add_argument("--sentinel-before")
    parser.add_argument("--sentinel-after")
    parser.add_argument(
        "--image",
        help="Pinned image from the tenant registry; defaults to the deployed one.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    common = {
        "environment": arguments.environment,
        "group": arguments.resource_group,
        "run_id": arguments.run_id,
    }
    if arguments.action == "provision":
        if not arguments.restore_time:
            raise ValueError("provision requires --restore-time")
        result = provision(
            azure,
            restore_time=arguments.restore_time,
            geo_location=arguments.geo_location,
            geo_subnet=arguments.geo_subnet,
            geo_dns_zone=arguments.geo_dns_zone,
            **common,
        )
    elif arguments.action == "verify":
        required = (
            arguments.target_fqdn,
            arguments.restore_time,
            arguments.sentinel_schema,
            arguments.sentinel_before,
            arguments.sentinel_after,
        )
        if not all(required):
            raise ValueError(
                "verify requires --target-fqdn, --restore-time, --sentinel-schema, "
                "--sentinel-before and --sentinel-after"
            )
        result = verify(
            azure,
            target_fqdn=arguments.target_fqdn,
            restore_time=arguments.restore_time,
            sentinel_schema=arguments.sentinel_schema,
            sentinel_before=arguments.sentinel_before,
            sentinel_after=arguments.sentinel_after,
            image=arguments.image,
            **common,
        )
    else:
        result = cleanup(azure, **common)
    print(json.dumps(result, sort_keys=True, default=str))
    # A verification job that reached a terminal *failure* is not a successful
    # drill. Reporting its status in the payload while exiting 0 would read as
    # success to anything wrapping this driver, which is how a drill comes to be
    # believed rather than demonstrated.
    if result.get("status") not in (None, "Succeeded"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
