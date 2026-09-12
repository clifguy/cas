"""Operator-driven isolated restore drill; control plane only, never on import.

A configured backup is not a demonstrated restore. This driver provisions a
throwaway server from the serving server's own backup, runs the read-only
verification job against it from inside the VNet, and tears the drill down again.
It never connects to a database: every database read happens in the job, under the
job's identity, in a read-only session.

Its refusals are the point of the script rather than incidental validation. A
restore target may not be, or collide with, any server that already exists, so a
drill cannot land on the database of record. A recovery point outside the server's
actual backup window is rejected before any billable resource is created, because
a 35-day retention setting does not mean a server has 35 days of history. And
cleanup deletes only a server carrying this drill's own ownership tag, and proves
the deletion by re-reading rather than by trusting an exit code.

A point-in-time drill restores into the serving subnet and private DNS zone, and
that is its one accepted isolation limitation: an Azure restore of a
private-access server yields another private-access server, which nothing can
reach unless it sits where the job already runs. The restored server is a distinct
resource with a distinct address; no serving binding is touched.

A geo drill has no such limitation, because it cannot have it. The serving network
is in the wrong region, so ``footprint`` first builds a throwaway network, private
DNS zone and Container Apps environment in the destination region, inside a
resource group of the drill's own. The restore, the verification job and the
teardown all stay inside that group, and cleanup deletes the group only once every
resource in it is shown to be the drill's.
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
FOOTPRINT_MODULE = ROOT / "infra/modules/postgres-geo-drill-footprint.bicep"

# The footprint module's outputs, by the names this driver reads them under.
FOOTPRINT_OUTPUTS = ("postgresSubnetId", "privateDnsZoneId", "environmentId", "location")

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


def drill_group_name(environment: str, run_id: str) -> str:
    """The resource group a geo drill owns outright, derivable from the run alone."""
    return f"rg-cas-{environment}-geodrill-{assert_run_id(run_id)}"


def footprint_deployment_name(run_id: str) -> str:
    return f"geo-drill-footprint-{run_id}"


def same_region(left: str, right: str) -> bool:
    # The control plane reports display names ("East US 2") where the CLI takes
    # programmatic ones ("eastus2"); compare the two spellings as one.
    return "".join(left.split()).casefold() == "".join(right.split()).casefold()


def resource_group_of(resource_id: str) -> str:
    match = re.search(r"/resourceGroups/([^/]+)/", resource_id, re.IGNORECASE)
    if match is None:
        raise ValueError(f"not a resource-group-scoped resource id: {resource_id}")
    return match.group(1)


def assert_in_drill_group(resource_id: str, drill_group: str) -> str:
    """A geo restore may use only network the drill itself created."""
    if resource_group_of(resource_id).casefold() != drill_group.casefold():
        raise ValueError(
            f"refusing geo-restore network {resource_id!r}: it must belong to this "
            f"drill's own resource group {drill_group!r}, never to the serving "
            "network or another workload's"
        )
    return resource_id


def paired_regions(az: Azure, region: str) -> list[str]:
    """The regions Azure pairs with ``region``, whichever spelling of its name is given."""
    for entry in az("account", "list-locations") or []:
        if same_region(entry.get("name", ""), region):
            return [
                pair["name"] for pair in (entry.get("metadata") or {}).get("pairedRegion") or []
            ]
    return []


def footprint(az: Azure, *, environment: str, group: str, run_id: str, geo_location: str) -> dict:
    """Build the destination-region network and compute a geo restore needs."""
    deployment, _job, _env = coordinates(az, environment, group)
    serving_name = output(deployment, "postgresServerName")
    serving = next((s for s in servers(az, group) if s["name"] == serving_name), None)
    if serving is None:
        raise ValueError(f"serving server {serving_name!r} not found in {group!r}")
    if same_region(serving["location"], geo_location):
        raise ValueError(
            f"refusing a geo drill into {geo_location!r}: that is the serving region, "
            "so the drill would demonstrate nothing about losing it"
        )
    pairs = paired_regions(az, serving["location"])
    if not any(same_region(pair, geo_location) for pair in pairs):
        raise ValueError(
            f"refusing a geo drill into {geo_location!r}: geo-redundant backup restores "
            f"only into the serving region's paired region ({', '.join(pairs) or 'none'})"
        )
    if serving["backup"].get("geoRedundantBackup") != "Enabled":
        raise ValueError(
            f"serving server {serving_name!r} has no geo-redundant backup; there is "
            "nothing in another region to restore from"
        )
    drill_group = drill_group_name(environment, run_id)
    if az("group", "exists", "--name", drill_group):
        raise ValueError(
            f"resource group {drill_group!r} already exists; choose a new drill run id "
            "or clean up the previous drill first"
        )
    az(
        "group",
        "create",
        "--name",
        drill_group,
        "--location",
        geo_location,
        "--tags",
        f"{DRILL_TAG}={run_id}",
    )
    built = az(
        "deployment",
        "group",
        "create",
        "--resource-group",
        drill_group,
        "--name",
        footprint_deployment_name(run_id),
        "--template-file",
        str(FOOTPRINT_MODULE),
        "--parameters",
        f"location={geo_location}",
        f"environmentName={environment}",
        f"runId={run_id}",
    )
    subnet, zone, environment_id, location = (output(built, name) for name in FOOTPRINT_OUTPUTS)
    return {
        "resource_group": drill_group,
        "location": location,
        "postgres_subnet_id": subnet,
        "private_dns_zone_id": zone,
        "environment_id": environment_id,
        "run_id": run_id,
    }


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
    target_group = group
    if geo_location:
        target_group = drill_group_name(environment, run_id)
        assert_in_drill_group(geo_subnet, target_group)
        assert_in_drill_group(geo_dns_zone, target_group)
    deployment, _job, _env = coordinates(az, environment, group)
    serving_name = output(deployment, "postgresServerName")
    serving_fqdn = output(deployment, "postgresServerFqdn")
    existing = servers(az, group)
    serving = next((s for s in existing if s["name"] == serving_name), None)
    if serving is None:
        raise ValueError(f"serving server {serving_name!r} not found in {group!r}")
    target = drill_server_name(serving_name, run_id)
    if target_group != group:
        existing = existing + servers(az, target_group)
    assert_target_is_new(existing, target)
    assert_within_window(serving, restore_time)

    network = serving["network"]
    command = [
        "postgres",
        "flexible-server",
        "geo-restore" if geo_location else "restore",
        "--resource-group",
        target_group,
        "--name",
        target,
        "--source-server",
        # By resource id for a geo restore: the target sits in the drill's group,
        # where a bare name would resolve to no server at all.
        serving["id"] if geo_location else serving_name,
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
        "resource_group": target_group,
        "run_id": run_id,
    }


def verify_job_name(environment: str) -> str:
    return f"job-pg-restore-verify-{environment}"


def assert_usable_image(candidate: str, deployed: str) -> str:
    """An image the drill may run, pinned and in-registry.

    A drill legitimately runs code the tenant has not deployed: the verification
    entrypoint is new until the change that adds it lands. So an override is
    allowed, but only within the tenant's own registry and only with an explicit
    tag. Anything else would let a drill pull an arbitrary image into a network
    that can reach a copy of the database of record — the serving subnet itself
    for a point-in-time drill, the drill's own network for a geo drill.
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
    geo_location: str | None = None,
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
    job_group = group
    location = job["location"]
    environment_id = job["properties"]["environmentId"]
    if geo_location:
        # The serving environment cannot reach a server in another region's
        # network, so the job runs in the environment the footprint built.
        job_group = drill_group_name(environment, run_id)
        built = az(
            "deployment",
            "group",
            "show",
            "--resource-group",
            job_group,
            "--name",
            footprint_deployment_name(run_id),
        )
        state = ((built or {}).get("properties") or {}).get("provisioningState")
        if state != "Succeeded":
            raise ValueError(
                f"the footprint for run {run_id!r} is {state or 'in an unknown state'}, "
                "not Succeeded; run cleanup --geo-location, then footprint again"
            )
        location = output(built, "location")
        environment_id = output(built, "environmentId")
        if not same_region(location, geo_location):
            raise ValueError(
                f"the footprint for run {run_id!r} is in {location!r}, not "
                f"{geo_location!r}; verify in the region the footprint was built in"
            )
    az(
        "deployment",
        "group",
        "create",
        "--resource-group",
        job_group,
        "--name",
        f"postgres-restore-verify-{run_id}",
        "--template-file",
        str(MODULE),
        "--parameters",
        f"location={location}",
        f"environmentName={environment}",
        f"acaEnvironmentId={environment_id}",
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
    # Poll the execution this start created, by name. Reading the job's execution
    # list and taking the newest entry looks equivalent and is not: a just-started
    # execution is briefly absent from that list, so the newest entry is the
    # *previous* run — already terminal on a second drill against the same job,
    # which would report its status and its logs as if they were this run's.
    started = az("containerapp", "job", "start", "--resource-group", job_group, "--name", name)
    execution = (started or {}).get("name")
    if not execution:
        raise RuntimeError(f"job start returned no execution name for {name!r}")
    return wait_job(
        az,
        job_group,
        name,
        execution,
        poll_seconds=poll_seconds,
        budget_seconds=budget_seconds,
        sleep=sleep,
    )


def wait_job(
    az: Azure,
    group: str,
    name: str,
    execution: str,
    *,
    poll_seconds: float,
    budget_seconds: float,
    sleep: Callable[[float], None],
) -> dict:
    deadline = time.monotonic() + budget_seconds
    while True:
        current = az(
            "containerapp",
            "job",
            "execution",
            "show",
            "--resource-group",
            group,
            "--name",
            name,
            "--job-execution-name",
            execution,
        )
        status = (current or {}).get("properties", {}).get("status")
        if status in ("Succeeded", "Failed", "Stopped"):
            return {"execution": execution, "status": status}
        if time.monotonic() >= deadline:
            # Name the execution. This is the one path that does not print the
            # payload, so without it the operator is told to stop an execution
            # they could only identify from the list read this loop exists to
            # avoid trusting.
            raise TimeoutError(
                f"verification job {name!r} did not reach a terminal status inside "
                f"{budget_seconds:.0f}s; stop Azure execution {execution!r} before "
                "retrying"
            )
        sleep(poll_seconds)


def cleanup(
    az: Azure,
    *,
    environment: str,
    group: str,
    run_id: str,
    geo_location: str | None = None,
    poll_seconds: float = 30.0,
    budget_seconds: float = 3600.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Delete only this drill's own resources, and prove they are gone."""
    if geo_location:
        return cleanup_geo(
            az,
            environment=environment,
            run_id=run_id,
            poll_seconds=poll_seconds,
            budget_seconds=budget_seconds,
            sleep=sleep,
        )
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
    # Drill-shaped servers this run does not own are reported, never deleted. The
    # operator surface is this result, so a teardown that merely skipped them
    # would leave them invisible to everyone but a reader of the source.
    return {"removed": removed, "run_id": run_id, "inspect": adoptable(az, group, run_id)}


def cleanup_geo(
    az: Azure,
    *,
    environment: str,
    run_id: str,
    poll_seconds: float,
    budget_seconds: float,
    sleep: Callable[[float], None],
) -> dict:
    """Delete a geo drill's resource group, but only a group wholly the drill's own.

    Deleting a group deletes everything in it, so the group's tag alone is not
    enough: every resource inside must carry this run's mark as well. The one
    exception is the verification job, which its module leaves untagged and which
    is recognised by its exact name and type together.

    The delete is issued without waiting and its completion polled under the
    drill's own budget. Removing a Container Apps environment takes long enough
    that a synchronous delete would run against the per-command timeout, and an
    overrun there surfaces as a bare timeout rather than as this driver's report.
    """
    drill_group = drill_group_name(environment, run_id)
    if not az("group", "exists", "--name", drill_group):
        return {"removed": [], "run_id": run_id, "resource_group": drill_group}
    found = az("group", "show", "--name", drill_group)
    if ((found or {}).get("tags") or {}).get(DRILL_TAG) != run_id:
        raise ValueError(
            f"refusing to delete {drill_group!r}: it is not tagged for drill run {run_id!r}"
        )
    job = verify_job_name(environment)
    foreign = sorted(
        entry["name"]
        for entry in (az("resource", "list", "--resource-group", drill_group) or [])
        if (entry.get("tags") or {}).get(DRILL_TAG) != run_id
        and not (entry["type"].casefold() == "microsoft.app/jobs" and entry["name"] == job)
    )
    if foreign:
        raise ValueError(
            f"refusing to delete {drill_group!r}: it holds resources this drill does "
            "not own: " + ", ".join(foreign)
        )
    az("group", "delete", "--name", drill_group, "--yes", "--no-wait")
    # An accepted delete is not a removal. Re-read until the group is gone.
    deadline = time.monotonic() + budget_seconds
    while az("group", "exists", "--name", drill_group):
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"drill group {drill_group!r} still exists {budget_seconds:.0f}s after its "
                "delete was accepted; Azure may still be removing it, and rerunning "
                "cleanup --geo-location is safe"
            )
        sleep(poll_seconds)
    return {"removed": [drill_group], "run_id": run_id, "resource_group": drill_group}


def adoptable(az: Azure, group: str, run_id: str) -> list[str]:
    """Servers an operator must inspect by hand: drill-shaped but not drill-tagged."""
    return [
        server["name"]
        for server in servers(az, group)
        if "-rv" in server["name"] and (server.get("tags") or {}).get(DRILL_TAG) != run_id
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Isolated PostgreSQL restore drill")
    parser.add_argument("action", choices=("footprint", "provision", "verify", "cleanup"))
    parser.add_argument("--environment", required=True)
    parser.add_argument(
        "--resource-group", required=True, help="The serving deployment's resource group."
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--restore-time")
    parser.add_argument(
        "--geo-location",
        help="Destination region; selects the geo drill for every action that takes it.",
    )
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


def assert_run_id(run_id: str) -> str:
    """Every action's run id, checked once at the boundary rather than per action.

    Only ``provision`` routed it through a pattern check, because only it builds a
    server name from it. The other two interpolate it into an ARM deployment name
    and a tag comparison, where a malformed value fails at the control plane
    instead of at the parser.
    """
    if not RUN_ID_PATTERN.match(run_id):
        raise ValueError("drill run id must be 1-16 lowercase alphanumerics")
    return run_id


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    assert_run_id(arguments.run_id)
    common = {
        "environment": arguments.environment,
        "group": arguments.resource_group,
        "run_id": arguments.run_id,
    }
    if arguments.action == "footprint":
        if not arguments.geo_location:
            raise ValueError("footprint requires --geo-location")
        result = footprint(azure, geo_location=arguments.geo_location, **common)
    elif arguments.action == "provision":
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
            geo_location=arguments.geo_location,
            **common,
        )
    else:
        result = cleanup(azure, geo_location=arguments.geo_location, **common)
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
