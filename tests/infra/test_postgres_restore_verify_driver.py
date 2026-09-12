"""Exercise the actual restore-drill driver against an Azure command boundary.

The fake answers control-plane reads; the refusals, the argument construction and
the removal proof are the real implementation. Every assertion about a command
argument is made on the argument's *value*, because a flag's presence says
nothing about what it was pointed at.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]

GROUP = "rg-cas-prod"
ENVIRONMENT = "prod"
RUN_ID = "20260911a"
SERVING_NAME = "psql-prod-token-pg17"
SERVING_FQDN = f"{SERVING_NAME}.postgres.database.azure.com"
RETAINED_NAME = "psql-prod-token"
TARGET_NAME = f"{SERVING_NAME}-rv{RUN_ID}"
TARGET_FQDN = f"{TARGET_NAME}.postgres.database.azure.com"
SUBNET = (
    "/subscriptions/s/resourceGroups/rg-cas-prod/providers/Microsoft.Network/"
    "virtualNetworks/vnet-prod/subnets/postgres"
)
DNS_ZONE = (
    "/subscriptions/s/resourceGroups/rg-cas-prod/providers/Microsoft.Network/"
    "privateDnsZones/prod.private.postgres.database.azure.com"
)
IMAGE = "acrtoken.azurecr.io/sage:2.2.78-542508a"
# Relative to now, not pinned: the window check compares against the wall clock,
# so a fixed literal would start failing the day it drifts into the past.
_NOW = datetime.now(timezone.utc)
EARLIEST = _NOW - timedelta(days=2)
RESTORE_TIME = (_NOW - timedelta(minutes=30)).isoformat()


def driver() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "restore_verify_driver", ROOT / "deploy/postgres-restore-verify.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GEO_LOCATION = "centralus"
DRILL_GROUP = f"rg-cas-{ENVIRONMENT}-geodrill-{RUN_ID}"
DRILL_ENVIRONMENT_ID = (
    f"/subscriptions/s/resourceGroups/{DRILL_GROUP}/providers/Microsoft.App/"
    f"managedEnvironments/cae-{ENVIRONMENT}-geodrill-{RUN_ID}"
)


def group_of(resource_id: str) -> str:
    return resource_id.split("/resourceGroups/", 1)[1].split("/", 1)[0]


def server(name: str, *, tags: dict[str, str] | None = None, group: str = GROUP) -> dict[str, Any]:
    return {
        "id": f"/subscriptions/s/resourceGroups/{group}/providers/"
        f"Microsoft.DBforPostgreSQL/flexibleServers/{name}",
        "name": name,
        "type": "Microsoft.DBforPostgreSQL/flexibleServers",
        "location": "East US 2",
        "fullyQualifiedDomainName": f"{name}.postgres.database.azure.com",
        "tags": tags or {},
        "backup": {
            "earliestRestoreDate": EARLIEST.isoformat(),
            "backupRetentionDays": 35,
            "geoRedundantBackup": "Enabled",
        },
        "network": {
            "delegatedSubnetResourceId": SUBNET,
            "privateDnsZoneArmResourceId": DNS_ZONE,
            "publicNetworkAccess": "Disabled",
        },
    }


class Azure:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.servers = [server(SERVING_NAME), server(RETAINED_NAME)]
        self.jobs = [{"name": "job-pg-bootstrap-prod"}]
        self.status = "Succeeded"
        self.image = IMAGE
        self.delete_is_a_lie = False
        self.job_delete_is_a_lie = False
        self.group_delete_is_a_lie = False
        # How many existence reads a deleted group survives before it is gone: a
        # delete issued without waiting returns while Azure is still removing it.
        self.group_polls_before_gone = 0
        self._pending_group_removal: dict[str, int] = {}
        self.started = 0
        # Resource groups by name, deployments by (group, name), and resources a
        # group holds beyond the servers and jobs tracked above.
        self.groups: dict[str, dict[str, Any]] = {}
        self.deployments: dict[tuple[str, str], dict[str, Any]] = {}
        self.resources: list[dict[str, Any]] = []

    @staticmethod
    def _group(args: tuple[str, ...]) -> str:
        return args[args.index("--resource-group") + 1] if "--resource-group" in args else GROUP

    def __call__(self, *args: str) -> Any:
        self.calls.append(args)
        prefix = args[:3]
        if args[:2] == ("group", "exists"):
            name = args[args.index("--name") + 1]
            if name in self._pending_group_removal:
                if self._pending_group_removal[name] > 0:
                    self._pending_group_removal[name] -= 1
                else:
                    del self._pending_group_removal[name]
                    self._remove_group(name)
            return name in self.groups
        if args[:2] == ("account", "list-locations"):
            return [
                {
                    "name": "eastus2",
                    "displayName": "East US 2",
                    "metadata": {"pairedRegion": [{"name": "centralus"}]},
                },
                {
                    "name": "centralus",
                    "displayName": "Central US",
                    "metadata": {"pairedRegion": [{"name": "eastus2"}]},
                },
                {
                    "name": "westus3",
                    "displayName": "West US 3",
                    "metadata": {"pairedRegion": [{"name": "eastus"}]},
                },
            ]
        if args[:2] == ("group", "create"):
            name = args[args.index("--name") + 1]
            key, _, tag = args[args.index("--tags") + 1].partition("=")
            self.groups[name] = {
                "name": name,
                "location": args[args.index("--location") + 1],
                "tags": {key: tag},
            }
            return self.groups[name]
        if args[:2] == ("group", "show"):
            name = args[args.index("--name") + 1]
            if name not in self.groups:
                raise RuntimeError(f"ResourceGroupNotFound: {name}")
            return self.groups[name]
        if args[:2] == ("group", "delete"):
            name = args[args.index("--name") + 1]
            if not self.group_delete_is_a_lie:
                if self.group_polls_before_gone:
                    self._pending_group_removal[name] = self.group_polls_before_gone
                else:
                    self._remove_group(name)
            return None
        if args[:2] == ("resource", "list"):
            group = self._group(args)
            held = [s for s in self.servers if group_of(s["id"]) == group]
            held += [
                {
                    "id": f"/subscriptions/s/resourceGroups/{group}/providers/"
                    f"Microsoft.App/jobs/{j['name']}",
                    "name": j["name"],
                    "type": "Microsoft.App/jobs",
                    "tags": j.get("tags"),
                }
                for j in self.jobs
                if j.get("group", GROUP) == group
            ]
            held += [r for r in self.resources if group_of(r["id"]) == group]
            return [
                {key: entry.get(key) for key in ("id", "name", "type", "tags")} for entry in held
            ]
        if prefix == ("deployment", "group", "show"):
            key = (self._group(args), args[args.index("--name") + 1])
            if key not in self.deployments:
                raise RuntimeError(f"DeploymentNotFound: {key[1]}")
            return self.deployments[key]
        if prefix == ("deployment", "sub", "show"):
            return {
                "properties": {
                    "outputs": {
                        key: {"value": value}
                        for key, value in {
                            "postgresServerFqdn": SERVING_FQDN,
                            "postgresServerName": SERVING_NAME,
                            "postgresDatabaseName": "sage",
                            "bootstrapJobName": "job-pg-bootstrap-prod",
                        }.items()
                    }
                }
            }
        if prefix == ("containerapp", "job", "show"):
            return {
                "location": "eastus2",
                "identity": {"userAssignedIdentities": {"identity-id": {}}},
                "properties": {
                    "environmentId": "/subscriptions/s/managedEnvironments/cae-prod",
                    "template": {
                        "containers": [
                            {
                                "image": self.image,
                                "env": [
                                    {"name": key, "value": value}
                                    for key, value in {
                                        "AZURE_CLIENT_ID": "client",
                                        "PG_ADMIN_USER": "id-pg-bootstrap-prod",
                                        "SAGE_DB_ROLE": "id-sage-prod",
                                        "BFF_DB_ROLE": "id-cas-bff-prod",
                                        "PG_DATABASE": "sage",
                                    }.items()
                                ],
                            }
                        ]
                    },
                },
            }
        if prefix == ("postgres", "flexible-server", "list"):
            group = self._group(args)
            return [s for s in self.servers if group_of(s["id"]) == group]
        if prefix == ("postgres", "flexible-server", "restore") or prefix == (
            "postgres",
            "flexible-server",
            "geo-restore",
        ):
            name = args[args.index("--name") + 1]
            created = server(name, group=self._group(args))
            self.servers.append(created)
            return created
        if prefix == ("postgres", "flexible-server", "delete"):
            name = args[args.index("--name") + 1]
            if not self.delete_is_a_lie:
                self.servers = [s for s in self.servers if s["name"] != name]
            return None
        if prefix[:2] == ("resource", "tag"):
            identifier = args[args.index("--ids") + 1]
            key, _, tag = args[args.index("--tags") + 1].partition("=")
            for entry in self.servers:
                if entry["id"] == identifier:
                    entry["tags"][key] = tag
            return None
        if prefix == ("containerapp", "job", "list"):
            group = self._group(args)
            return [entry for entry in self.jobs if entry.get("group", GROUP) == group]
        if prefix == ("containerapp", "job", "delete"):
            name = args[args.index("--name") + 1]
            group = self._group(args)
            if not self.job_delete_is_a_lie:
                self.jobs = [
                    entry
                    for entry in self.jobs
                    if not (entry["name"] == name and entry.get("group", GROUP) == group)
                ]
            return None
        if prefix == ("containerapp", "job", "start"):
            self.started += 1
            return {"name": f"exec-{self.started}"}
        if args[:4] == ("containerapp", "job", "execution", "show"):
            # Keyed by the execution name the caller asked for. A driver that
            # polled the job's execution list instead would be served the stale
            # entry below, which is the defect this shape exists to expose.
            requested = args[args.index("--job-execution-name") + 1]
            return {"name": requested, "properties": {"status": self.status}}
        if args[:4] == ("containerapp", "job", "execution", "list"):
            # Newest-first, and deliberately missing the just-started execution:
            # this is the window in which element 0 is the *previous* run.
            return [{"name": "exec-stale", "properties": {"status": "Succeeded"}}]
        if prefix == ("deployment", "group", "create"):
            group = self._group(args)
            template = Path(args[args.index("--template-file") + 1]).name
            if template == "postgres-geo-drill-footprint.bicep":
                return self._deploy_footprint(group, args)
            self.jobs.append({"name": "job-pg-restore-verify-prod", "group": group})
            return None
        raise AssertionError(f"unexpected az call: {args}")

    def _remove_group(self, name: str) -> None:
        self.groups.pop(name, None)
        self.servers = [s for s in self.servers if group_of(s["id"]) != name]
        self.jobs = [j for j in self.jobs if j.get("group", GROUP) != name]
        self.resources = [r for r in self.resources if group_of(r["id"]) != name]

    def _deploy_footprint(self, group: str, args: tuple[str, ...]) -> dict[str, Any]:
        parameters = dict(value.split("=", 1) for value in args if "=" in value)
        base = f"/subscriptions/s/resourceGroups/{group}/providers/"
        tags = {"casRestoreDrill": parameters["runId"]}
        vnet = f"{base}Microsoft.Network/virtualNetworks/vnet-geodrill"
        zone = (
            f"{base}Microsoft.Network/privateDnsZones/geodrill.private.postgres.database.azure.com"
        )
        self.resources += [
            {"id": vnet, "name": "vnet-geodrill", "type": "Microsoft.Network/virtualNetworks"},
            {"id": zone, "name": "geodrill", "type": "Microsoft.Network/privateDnsZones"},
            {
                "id": DRILL_ENVIRONMENT_ID,
                "name": "cae-geodrill",
                "type": "Microsoft.App/managedEnvironments",
            },
        ]
        for entry in self.resources[-3:]:
            entry["tags"] = dict(tags)
        outputs = {
            "postgresSubnetId": f"{vnet}/subnets/postgres",
            "privateDnsZoneId": zone,
            "environmentId": DRILL_ENVIRONMENT_ID,
            "location": parameters["location"],
        }
        deployment = {
            "properties": {
                "provisioningState": "Succeeded",
                "outputs": {key: {"value": value} for key, value in outputs.items()},
            }
        }
        self.deployments[(group, args[args.index("--name") + 1])] = deployment
        return deployment

    def call(self, *prefix: str) -> tuple[str, ...]:
        matches = [args for args in self.calls if args[: len(prefix)] == prefix]
        assert len(matches) == 1, f"expected exactly one {prefix} call, got {len(matches)}"
        return matches[0]

    @staticmethod
    def argument(call: tuple[str, ...], flag: str) -> str:
        assert flag in call, f"{flag} absent from {call}"
        return call[call.index(flag) + 1]


@pytest.fixture
def az() -> Azure:
    return Azure()


@pytest.fixture
def module() -> ModuleType:
    return driver()


def provision(module: ModuleType, az: Azure, **overrides: Any) -> dict:
    settings = {
        "environment": ENVIRONMENT,
        "group": GROUP,
        "run_id": RUN_ID,
        "restore_time": RESTORE_TIME,
    }
    settings.update(overrides)
    return module.provision(az, **settings)


# --- 16. the restore lands where it was told to, by value --------------------


def test_restore_carries_the_serving_network_by_value(module: ModuleType, az: Azure) -> None:
    result = provision(module, az)
    call = az.call("postgres", "flexible-server", "restore")
    assert az.argument(call, "--subnet") == SUBNET
    assert az.argument(call, "--private-dns-zone") == DNS_ZONE
    assert az.argument(call, "--source-server") == SERVING_NAME
    assert az.argument(call, "--restore-time") == RESTORE_TIME
    assert az.argument(call, "--name") == TARGET_NAME
    assert result["target_fqdn"] == TARGET_FQDN
    assert result["mode"] == "point-in-time"


def test_the_drill_server_takes_no_geo_redundant_backup(module: ModuleType, az: Azure) -> None:
    provision(module, az)
    call = az.call("postgres", "flexible-server", "restore")
    assert az.argument(call, "--geo-redundant-backup") == "Disabled"


def test_the_drill_server_is_tagged_for_adoption(module: ModuleType, az: Azure) -> None:
    provision(module, az)
    created = next(entry for entry in az.servers if entry["name"] == TARGET_NAME)
    assert created["tags"][module.DRILL_TAG] == RUN_ID


def test_the_ownership_mark_is_written_incrementally(module: ModuleType, az: Azure) -> None:
    # A plain tag write replaces the whole collection. Adding the drill's mark
    # must not be the operation that erases the tags the restore inherited.
    provision(module, az)
    call = az.call("resource", "tag")
    assert "--is-incremental" in call


def network_in(group: str) -> tuple[str, str]:
    base = f"/subscriptions/s/resourceGroups/{group}/providers/Microsoft.Network/"
    return (
        f"{base}virtualNetworks/vnet-geodrill/subnets/postgres",
        f"{base}privateDnsZones/geodrill.private.postgres.database.azure.com",
    )


GEO_SUBNET, GEO_DNS_ZONE = network_in(DRILL_GROUP)


def test_a_geo_restore_names_its_region_and_its_own_network(module: ModuleType, az: Azure) -> None:
    result = provision(
        module,
        az,
        geo_location=GEO_LOCATION,
        geo_subnet=GEO_SUBNET,
        geo_dns_zone=GEO_DNS_ZONE,
    )
    call = az.call("postgres", "flexible-server", "geo-restore")
    assert az.argument(call, "--location") == GEO_LOCATION
    # The destination region needs its own network. Passing the serving subnet
    # would name a resource in the wrong region entirely.
    assert az.argument(call, "--subnet") == GEO_SUBNET
    assert az.argument(call, "--private-dns-zone") == GEO_DNS_ZONE
    assert az.argument(call, "--subnet") != SUBNET
    # The restored server belongs to the drill's own group, so teardown is that
    # group's deletion and the serving group gains nothing.
    assert az.argument(call, "--resource-group") == DRILL_GROUP
    # By resource id, not name: a bare name resolves against the *target* group,
    # where no server of that name exists.
    serving_id = next(s["id"] for s in az.servers if s["name"] == SERVING_NAME)
    assert az.argument(call, "--source-server") == serving_id
    assert result["mode"] == "geo-restore"
    assert result["resource_group"] == DRILL_GROUP


@pytest.mark.parametrize(
    "subnet_group, zone_group",
    [
        # The serving network itself.
        (GROUP, DRILL_GROUP),
        (DRILL_GROUP, GROUP),
        # Another workload's network in the destination region.
        ("rg-other-centralus", DRILL_GROUP),
        # A group whose name merely begins with the drill group's.
        (f"{DRILL_GROUP}-other", DRILL_GROUP),
        (DRILL_GROUP, f"{DRILL_GROUP}-other"),
        # A different drill run's footprint.
        (f"rg-cas-{ENVIRONMENT}-geodrill-other", DRILL_GROUP),
    ],
)
def test_a_geo_network_outside_the_drill_group_is_refused(
    module: ModuleType, az: Azure, subnet_group: str, zone_group: str
) -> None:
    subnet = network_in(subnet_group)[0]
    zone = network_in(zone_group)[1]
    with pytest.raises(ValueError, match="own resource group"):
        provision(module, az, geo_location=GEO_LOCATION, geo_subnet=subnet, geo_dns_zone=zone)
    assert not [
        args for args in az.calls if args[:3] == ("postgres", "flexible-server", "geo-restore")
    ]


def test_a_geo_target_already_in_the_drill_group_is_refused(module: ModuleType, az: Azure) -> None:
    az.servers.append(server(TARGET_NAME, group=DRILL_GROUP))
    with pytest.raises(ValueError, match="existing server"):
        provision(
            module, az, geo_location=GEO_LOCATION, geo_subnet=GEO_SUBNET, geo_dns_zone=GEO_DNS_ZONE
        )
    assert not [
        args for args in az.calls if args[:3] == ("postgres", "flexible-server", "geo-restore")
    ]


# --- the destination-region footprint ----------------------------------------


def footprint(module: ModuleType, az: Azure, **overrides: Any) -> dict:
    settings = {
        "environment": ENVIRONMENT,
        "group": GROUP,
        "run_id": RUN_ID,
        "geo_location": GEO_LOCATION,
    }
    settings.update(overrides)
    return module.footprint(az, **settings)


def test_the_footprint_lands_in_its_own_group_in_the_destination_region(
    module: ModuleType, az: Azure
) -> None:
    result = footprint(module, az)
    created = az.call("group", "create")
    assert az.argument(created, "--name") == DRILL_GROUP
    assert az.argument(created, "--location") == GEO_LOCATION
    assert az.argument(created, "--tags") == f"{module.DRILL_TAG}={RUN_ID}"
    deployed = az.call("deployment", "group", "create")
    assert az.argument(deployed, "--resource-group") == DRILL_GROUP
    assert Path(az.argument(deployed, "--template-file")) == (
        ROOT / "infra/modules/postgres-geo-drill-footprint.bicep"
    )
    parameters = dict(value.split("=", 1) for value in deployed if "=" in value)
    assert parameters["location"] == GEO_LOCATION
    assert parameters["runId"] == RUN_ID
    assert parameters["environmentName"] == ENVIRONMENT
    assert result["resource_group"] == DRILL_GROUP
    assert result["postgres_subnet_id"] == GEO_SUBNET
    assert result["private_dns_zone_id"] == GEO_DNS_ZONE
    assert result["environment_id"] == DRILL_ENVIRONMENT_ID


@pytest.mark.parametrize("region", ["eastus2", "East US 2", "EASTUS2"])
def test_a_footprint_in_the_serving_region_is_refused(
    module: ModuleType, az: Azure, region: str
) -> None:
    # A "geo" drill inside the serving region demonstrates nothing about losing it.
    with pytest.raises(ValueError, match="serving region"):
        footprint(module, az, geo_location=region)
    assert not [args for args in az.calls if args[:2] == ("group", "create")]


def test_a_footprint_for_a_server_without_geo_backup_is_refused(
    module: ModuleType, az: Azure
) -> None:
    serving = next(s for s in az.servers if s["name"] == SERVING_NAME)
    serving["backup"]["geoRedundantBackup"] = "Disabled"
    with pytest.raises(ValueError, match="geo-redundant backup"):
        footprint(module, az)
    assert not [args for args in az.calls if args[:2] == ("group", "create")]


def test_an_existing_drill_group_is_refused(module: ModuleType, az: Azure) -> None:
    az.groups[DRILL_GROUP] = {"name": DRILL_GROUP, "location": GEO_LOCATION, "tags": {}}
    with pytest.raises(ValueError, match="already exists"):
        footprint(module, az)
    assert not [args for args in az.calls if args[:2] == ("group", "create")]
    assert not [args for args in az.calls if args[:3] == ("deployment", "group", "create")]


def test_footprint_requires_a_geo_location(module: ModuleType, az: Azure, monkeypatch) -> None:
    monkeypatch.setattr(module, "azure", az)
    with pytest.raises(ValueError, match="--geo-location"):
        module.main(
            [
                "footprint",
                "--environment",
                ENVIRONMENT,
                "--resource-group",
                GROUP,
                "--run-id",
                RUN_ID,
            ]
        )
    assert az.calls == []


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"geo_subnet": GEO_SUBNET},
        {"geo_dns_zone": GEO_DNS_ZONE},
    ],
)
def test_a_geo_restore_without_destination_network_is_refused(
    module: ModuleType, az: Azure, extra: dict
) -> None:
    # Measured against the live tenant: Azure refuses to geo-restore a
    # private-access server without network parameters and will not fall back to
    # a public endpoint. Refusing here keeps the prerequisite legible instead of
    # surfacing as a control-plane rejection minutes later.
    with pytest.raises(ValueError, match="geo-subnet"):
        provision(module, az, geo_location="centralus", **extra)
    assert not [
        args for args in az.calls if args[:3] == ("postgres", "flexible-server", "geo-restore")
    ]


def test_a_failed_az_call_carries_the_clis_own_message(module: ModuleType) -> None:
    # A drill that reports only an exit status tells the operator that something
    # failed but not what Azure objected to.
    import subprocess

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="ERROR: Specify network parameters if you are geo-restoring"
        )

    original = module.subprocess.run
    module.subprocess.run = fake_run
    try:
        with pytest.raises(RuntimeError, match="Specify network parameters"):
            module.azure("postgres", "flexible-server", "geo-restore")
    finally:
        module.subprocess.run = original


# --- 17. the target may not collide with anything that exists ----------------


@pytest.mark.parametrize("existing", [SERVING_NAME, RETAINED_NAME])
def test_a_target_colliding_with_an_existing_server_is_refused(
    module: ModuleType, az: Azure, existing: str
) -> None:
    with pytest.raises(ValueError, match="existing server"):
        module.assert_target_is_new(az.servers, existing)


def test_a_repeated_run_id_is_refused_rather_than_restoring_over_the_drill(
    module: ModuleType, az: Azure
) -> None:
    provision(module, az)
    with pytest.raises(ValueError, match="existing server"):
        provision(module, az)


def test_the_drill_name_never_equals_the_serving_name(module: ModuleType) -> None:
    assert module.drill_server_name(SERVING_NAME, RUN_ID) != SERVING_NAME


@pytest.mark.parametrize("run_id", ["", "Bad", "has-hyphen", "x" * 17])
def test_a_malformed_run_id_is_refused(module: ModuleType, run_id: str) -> None:
    with pytest.raises(ValueError):
        module.drill_server_name(SERVING_NAME, run_id)


@pytest.mark.parametrize("action", ["footprint", "provision", "verify", "cleanup"])
def test_every_action_validates_the_run_id(
    module: ModuleType, az: Azure, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    # Only `provision` built a server name from the run id, so only it checked the
    # pattern. The other two interpolate it into an ARM deployment name and a tag
    # comparison, where a malformed value fails at the control plane rather than
    # at the parser.
    monkeypatch.setattr(module, "azure", az)
    # `main` builds the wait with the production sleep and a 3900s budget, so a
    # regression in the terminal set would make this test sleep toward a 65-minute
    # deadline rather than fail. The suite has no timeout plugin to bound it.
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    with pytest.raises(ValueError, match="lowercase alphanumerics"):
        module.main(
            [
                action,
                "--environment",
                ENVIRONMENT,
                "--resource-group",
                GROUP,
                "--run-id",
                "Not Valid",
            ]
        )
    assert az.calls == []


def test_an_overlong_drill_name_is_refused(module: ModuleType) -> None:
    with pytest.raises(ValueError, match="63"):
        module.drill_server_name("p" * 60, "abc")


# --- 18. the recovery point must be inside the real window -------------------


def test_a_point_before_the_earliest_restore_point_is_refused(
    module: ModuleType, az: Azure
) -> None:
    early = (EARLIEST - timedelta(hours=1)).isoformat()
    with pytest.raises(ValueError, match="earliest restore point"):
        provision(module, az, restore_time=early)
    # Refused before anything billable exists.
    assert not [args for args in az.calls if args[:3] == ("postgres", "flexible-server", "restore")]


def test_a_future_point_is_refused(module: ModuleType, az: Azure) -> None:
    later = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="future"):
        provision(module, az, restore_time=later)


def test_a_naive_timestamp_is_refused(module: ModuleType, az: Azure) -> None:
    with pytest.raises(ValueError, match="UTC offset"):
        provision(module, az, restore_time="2026-09-11T02:30:00")


def test_the_window_check_reads_the_server_not_the_retention_setting(
    module: ModuleType, az: Azure
) -> None:
    # A 35-day retention setting on a server two days old must not admit a
    # 30-day-old recovery point.
    subject = server(SERVING_NAME)
    assert subject["backup"]["backupRetentionDays"] == 35
    with pytest.raises(ValueError, match="earliest restore point"):
        module.assert_within_window(subject, (EARLIEST - timedelta(days=28)).isoformat())


# --- verification dispatch ---------------------------------------------------


def verify(module: ModuleType, az: Azure, **overrides: Any) -> dict:
    settings = {
        "environment": ENVIRONMENT,
        "group": GROUP,
        "run_id": RUN_ID,
        "target_fqdn": TARGET_FQDN,
        "restore_time": RESTORE_TIME,
        "sentinel_schema": "cloud_validation",
        "sentinel_before": "aaaa_before",
        "sentinel_after": "bbbb_after",
        "sleep": lambda _seconds: None,
    }
    settings.update(overrides)
    return module.verify(az, **settings)


def test_verification_passes_both_addresses_to_the_job(module: ModuleType, az: Azure) -> None:
    verify(module, az)
    call = az.call("deployment", "group", "create")
    parameters = dict(value.split("=", 1) for value in call if "=" in value)
    assert parameters["restoreFqdn"] == TARGET_FQDN
    assert parameters["servingFqdn"] == SERVING_FQDN
    assert parameters["sentinelBeforeId"] == "aaaa_before"
    assert parameters["sentinelAfterId"] == "bbbb_after"
    assert parameters["restorePoint"] == RESTORE_TIME
    assert parameters["image"] == IMAGE


def test_verification_against_the_serving_address_is_refused(module: ModuleType, az: Azure) -> None:
    with pytest.raises(ValueError, match="serving"):
        verify(module, az, target_fqdn=SERVING_FQDN.upper())
    assert not [args for args in az.calls if args[:3] == ("deployment", "group", "create")]


@pytest.mark.parametrize("image", ["acrtoken.azurecr.io/sage:latest", "acrtoken.azurecr.io/sage"])
def test_an_unpinned_image_is_refused(module: ModuleType, az: Azure, image: str) -> None:
    az.image = image
    with pytest.raises(ValueError, match="unpinned"):
        verify(module, az)


def test_an_override_from_the_tenant_registry_is_accepted(module: ModuleType, az: Azure) -> None:
    # A drill runs the verification entrypoint before the change carrying it has
    # been deployed, so the deployed tag cannot be the only image it may run.
    drill = "acrtoken.azurecr.io/sage:restore-drill-20260911a"
    verify(module, az, image=drill)
    call = az.call("deployment", "group", "create")
    parameters = dict(value.split("=", 1) for value in call if "=" in value)
    assert parameters["image"] == drill
    assert parameters["acrLoginServer"] == "acrtoken.azurecr.io"


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/someone/sage:2.2.78",
        "docker.io/library/postgres:17",
        "evil.azurecr.io/sage:2.2.78",
    ],
)
def test_an_override_from_another_registry_is_refused(
    module: ModuleType, az: Azure, image: str
) -> None:
    # Pinning alone is not enough: a pinned image from somewhere else would still
    # pull an arbitrary container into the subnet holding the serving database.
    with pytest.raises(ValueError, match="own registry"):
        verify(module, az, image=image)
    assert not [args for args in az.calls if args[:3] == ("deployment", "group", "create")]


@pytest.mark.parametrize("image", ["acrtoken.azurecr.io/sage", "acrtoken.azurecr.io/sage:latest"])
def test_an_unpinned_override_is_refused(module: ModuleType, az: Azure, image: str) -> None:
    with pytest.raises(ValueError, match="unpinned"):
        verify(module, az, image=image)


@pytest.mark.parametrize("status", ["Failed", "Stopped"])
def test_a_terminal_failure_exits_non_zero(
    module: ModuleType, az: Azure, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    # Reporting a failed status in the payload while exiting 0 reads as success
    # to anything wrapping this driver, which is how a drill comes to be believed
    # rather than demonstrated.
    az.status = status
    monkeypatch.setattr(module, "azure", az)
    # `main` builds the wait with the production sleep and a 3900s budget, so a
    # regression in the terminal set would make this test sleep toward a 65-minute
    # deadline rather than fail. The suite has no timeout plugin to bound it.
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    code = module.main(
        [
            "verify",
            "--environment",
            ENVIRONMENT,
            "--resource-group",
            GROUP,
            "--run-id",
            RUN_ID,
            "--target-fqdn",
            TARGET_FQDN,
            "--restore-time",
            RESTORE_TIME,
            "--sentinel-schema",
            "cloud_validation",
            "--sentinel-before",
            "aaaa_before",
            "--sentinel-after",
            "bbbb_after",
        ]
    )
    assert code == 1


def test_a_succeeded_job_exits_zero(
    module: ModuleType, az: Azure, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "azure", az)
    # `main` builds the wait with the production sleep and a 3900s budget, so a
    # regression in the terminal set would make this test sleep toward a 65-minute
    # deadline rather than fail. The suite has no timeout plugin to bound it.
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    code = module.main(
        [
            "verify",
            "--environment",
            ENVIRONMENT,
            "--resource-group",
            GROUP,
            "--run-id",
            RUN_ID,
            "--target-fqdn",
            TARGET_FQDN,
            "--restore-time",
            RESTORE_TIME,
            "--sentinel-schema",
            "cloud_validation",
            "--sentinel-before",
            "aaaa_before",
            "--sentinel-after",
            "bbbb_after",
        ]
    )
    assert code == 0


def test_the_polled_execution_is_the_one_start_created(module: ModuleType, az: Azure) -> None:
    # A just-started execution is briefly absent from the job's execution list,
    # so the newest listed entry is the previous run — already terminal on a
    # second drill against the same job. The fake serves a stale `Succeeded`
    # entry from `execution list`, so a driver reading that list reports the
    # wrong run's status and points the operator at the wrong logs.
    az.status = "Failed"
    result = verify(module, az)
    assert result["execution"] == "exec-1"
    assert result["status"] == "Failed"
    show = az.call("containerapp", "job", "execution", "show")
    assert az.argument(show, "--job-execution-name") == "exec-1"


def test_a_start_returning_no_execution_name_raises(module: ModuleType, az: Azure) -> None:
    monkey = az.__call__

    def without_name(*args: str) -> Any:
        if args[:3] == ("containerapp", "job", "start"):
            az.calls.append(args)
            return None
        return monkey(*args)

    with pytest.raises(RuntimeError, match="no execution name"):
        verify(module, without_name)


def test_a_job_that_never_finishes_raises_rather_than_looping(
    module: ModuleType, az: Azure
) -> None:
    az.status = "Running"
    with pytest.raises(TimeoutError, match="terminal status"):
        verify(module, az, poll_seconds=0.0, budget_seconds=0.0)


def test_the_timeout_names_the_execution_it_tells_the_operator_to_stop(
    module: ModuleType, az: Azure
) -> None:
    # The one terminal path that prints no payload. Without the name the operator
    # could only recover it from an execution listing, which is the read this loop
    # exists to avoid trusting.
    az.status = "Running"
    with pytest.raises(TimeoutError, match="exec-1"):
        verify(module, az, poll_seconds=0.0, budget_seconds=0.0)


def test_the_loop_waits_between_polls(module: ModuleType, az: Azure) -> None:
    # Nothing else observes that the loop sleeps at all: the never-finishes test
    # sets the budget to zero, so an implementation that polled in a tight spin
    # passes every other assertion here.
    az.status = "Running"
    waits: list[float] = []

    class Stop(Exception):
        pass

    def record(seconds: float) -> None:
        waits.append(seconds)
        raise Stop

    # The budget is small on purpose. An implementation that never sleeps would
    # otherwise spin here for the whole budget in real wall-clock time — hanging
    # the suite instead of failing it, which is the very defect this test exists
    # to catch. Half a second bounds that to a fast, ordinary failure.
    with pytest.raises(Stop):
        verify(module, az, poll_seconds=7.5, budget_seconds=0.5, sleep=record)
    assert waits == [7.5]


def geo_drill(module: ModuleType, az: Azure) -> None:
    """Footprint, restore and verification, in the order an operator runs them."""
    built = footprint(module, az)
    provision(
        module,
        az,
        geo_location=GEO_LOCATION,
        geo_subnet=built["postgres_subnet_id"],
        geo_dns_zone=built["private_dns_zone_id"],
    )
    verify(module, az, geo_location=GEO_LOCATION)


def test_geo_verification_runs_the_job_in_the_drill_environment(
    module: ModuleType, az: Azure
) -> None:
    footprint(module, az)
    verify(module, az, geo_location=GEO_LOCATION)
    deployments = [args for args in az.calls if args[:3] == ("deployment", "group", "create")]
    job = next(
        args
        for args in deployments
        if "--template-file" in args
        and Path(az.argument(args, "--template-file")).name == "postgres-restore-verify-job.bicep"
    )
    assert az.argument(job, "--resource-group") == DRILL_GROUP
    parameters = dict(value.split("=", 1) for value in job if "=" in value)
    assert parameters["location"] == GEO_LOCATION
    # The serving environment cannot reach a server in another region's network.
    assert parameters["acaEnvironmentId"] == DRILL_ENVIRONMENT_ID
    assert parameters["acaEnvironmentId"] != "/subscriptions/s/managedEnvironments/cae-prod"
    assert parameters["restoreFqdn"] == TARGET_FQDN
    assert parameters["servingFqdn"] == SERVING_FQDN
    assert az.argument(az.call("containerapp", "job", "start"), "--resource-group") == DRILL_GROUP
    show = az.call("containerapp", "job", "execution", "show")
    assert az.argument(show, "--resource-group") == DRILL_GROUP


def test_geo_verification_without_a_footprint_fails_before_deploying(
    module: ModuleType, az: Azure
) -> None:
    with pytest.raises(RuntimeError, match="DeploymentNotFound"):
        verify(module, az, geo_location=GEO_LOCATION)
    assert not [args for args in az.calls if args[:3] == ("deployment", "group", "create")]
    assert not [args for args in az.calls if args[:3] == ("containerapp", "job", "start")]


def test_geo_verification_in_a_region_other_than_the_footprints_is_refused(
    module: ModuleType, az: Azure
) -> None:
    footprint(module, az)
    with pytest.raises(ValueError, match="footprint"):
        verify(module, az, geo_location="westus3")
    assert len([args for args in az.calls if args[:3] == ("deployment", "group", "create")]) == 1


def geo_cleanup(module: ModuleType, az: Azure, **overrides: Any) -> dict:
    settings = {
        "environment": ENVIRONMENT,
        "group": GROUP,
        "run_id": RUN_ID,
        "geo_location": GEO_LOCATION,
        "sleep": lambda _seconds: None,
    }
    settings.update(overrides)
    return module.cleanup(az, **settings)


def test_geo_cleanup_deletes_without_waiting_and_polls_for_removal(
    module: ModuleType, az: Azure
) -> None:
    # A group delete can outlast the driver's per-command timeout, so it is issued
    # without waiting and the removal is proven by polling under the drill's own
    # budget. Two existence reads return true before the group is gone.
    geo_drill(module, az)
    az.group_polls_before_gone = 2
    waits: list[float] = []
    result = geo_cleanup(module, az, poll_seconds=7.5, sleep=waits.append)
    assert "--no-wait" in az.call("group", "delete")
    assert waits == [7.5, 7.5]
    assert result["removed"] == [DRILL_GROUP]
    assert DRILL_GROUP not in az.groups


def test_geo_cleanup_deletes_the_drill_group_and_proves_it(module: ModuleType, az: Azure) -> None:
    geo_drill(module, az)
    result = geo_cleanup(module, az)
    deleted = az.call("group", "delete")
    assert az.argument(deleted, "--name") == DRILL_GROUP
    # The removal proof is a read *after* the delete, not the delete's exit code.
    index = az.calls.index(deleted)
    assert any(args[:2] == ("group", "exists") for args in az.calls[index + 1 :])
    assert result["removed"] == [DRILL_GROUP]
    assert DRILL_GROUP not in az.groups
    assert {entry["name"] for entry in az.servers} == {SERVING_NAME, RETAINED_NAME}


@pytest.mark.parametrize("tags", [{}, {"casRestoreDrill": "other"}])
def test_geo_cleanup_refuses_a_group_not_tagged_for_this_run(
    module: ModuleType, az: Azure, tags: dict[str, str]
) -> None:
    az.groups[DRILL_GROUP] = {"name": DRILL_GROUP, "location": GEO_LOCATION, "tags": tags}
    with pytest.raises(ValueError, match="not tagged"):
        geo_cleanup(module, az)
    assert not [args for args in az.calls if args[:2] == ("group", "delete")]
    assert DRILL_GROUP in az.groups


def test_geo_cleanup_refuses_a_group_holding_a_resource_it_does_not_own(
    module: ModuleType, az: Azure
) -> None:
    geo_drill(module, az)
    az.resources.append(
        {
            "id": f"/subscriptions/s/resourceGroups/{DRILL_GROUP}/providers/"
            "Microsoft.Compute/virtualMachines/vm-someone-else",
            "name": "vm-someone-else",
            "type": "Microsoft.Compute/virtualMachines",
            "tags": {},
        }
    )
    with pytest.raises(ValueError, match="vm-someone-else"):
        geo_cleanup(module, az)
    assert not [args for args in az.calls if args[:2] == ("group", "delete")]


@pytest.mark.parametrize(
    "name, refused",
    [("job-pg-restore-verify-prod", False), ("job-pg-bootstrap-prod", True)],
)
def test_geo_cleanup_tolerates_only_the_named_verification_job_untagged(
    module: ModuleType, az: Azure, name: str, refused: bool
) -> None:
    # The verification job module carries no tags, so the one untagged resource a
    # drill group may legitimately hold is that job, by name. Any other job is not
    # this drill's.
    footprint(module, az)
    az.jobs.append({"name": name, "group": DRILL_GROUP})
    # The tolerated arm proves nothing unless the untagged job is really among what
    # cleanup reads, so confirm it through the same listing cleanup uses.
    listed = az("resource", "list", "--resource-group", DRILL_GROUP)
    assert [entry["tags"] for entry in listed if entry["name"] == name] == [None]
    if refused:
        with pytest.raises(ValueError, match=name):
            geo_cleanup(module, az)
        assert not [args for args in az.calls if args[:2] == ("group", "delete")]
    else:
        assert geo_cleanup(module, az)["removed"] == [DRILL_GROUP]


def test_geo_cleanup_fails_when_the_group_survives_its_delete(
    module: ModuleType, az: Azure
) -> None:
    # An accepted delete is not a removal: a group that never goes away exhausts
    # the budget and is named, rather than reported removed.
    geo_drill(module, az)
    az.group_delete_is_a_lie = True
    with pytest.raises(TimeoutError, match=DRILL_GROUP):
        geo_cleanup(module, az, poll_seconds=0.0, budget_seconds=0.0)


@pytest.mark.parametrize("region", ["westus3", "West US 3"])
def test_a_footprint_outside_the_serving_regions_pair_is_refused(
    module: ModuleType, az: Azure, region: str
) -> None:
    # Geo-redundant backup restores only into the paired region. Any other
    # destination builds a footprint the control plane then refuses to restore into.
    with pytest.raises(ValueError, match="paired"):
        footprint(module, az, geo_location=region)
    assert not [args for args in az.calls if args[:2] == ("group", "create")]


def test_the_pair_is_recognised_by_its_display_name(module: ModuleType, az: Azure) -> None:
    footprint(module, az, geo_location="Central US")
    assert az.argument(az.call("group", "create"), "--location") == "Central US"


@pytest.mark.parametrize(
    "state, keep_outputs",
    [("Failed", False), ("Failed", True), ("Running", True), ("Canceled", True)],
)
def test_geo_verification_against_an_unfinished_footprint_is_refused(
    module: ModuleType, az: Azure, state: str, keep_outputs: bool
) -> None:
    # The state decides, not the outputs: the cases that keep a full set of outputs
    # are the ones a check on missing outputs would wave through.
    footprint(module, az)
    deployment = az.deployments[(DRILL_GROUP, f"geo-drill-footprint-{RUN_ID}")]
    outputs = deployment["properties"]["outputs"] if keep_outputs else None
    deployment["properties"] = {"provisioningState": state, "outputs": outputs}
    with pytest.raises(ValueError, match="footprint"):
        verify(module, az, geo_location=GEO_LOCATION)
    assert len([args for args in az.calls if args[:3] == ("deployment", "group", "create")]) == 1
    assert not [args for args in az.calls if args[:3] == ("containerapp", "job", "start")]


def test_geo_cleanup_of_an_absent_group_removes_nothing(module: ModuleType, az: Azure) -> None:
    result = geo_cleanup(module, az)
    assert result["removed"] == []
    assert not [args for args in az.calls if args[:2] == ("group", "delete")]


# --- 19 and 20. cleanup removes only its own, and proves it ------------------


def test_cleanup_removes_the_drill_server_and_the_job(module: ModuleType, az: Azure) -> None:
    provision(module, az)
    verify(module, az)
    result = module.cleanup(az, environment=ENVIRONMENT, group=GROUP, run_id=RUN_ID)
    assert set(result["removed"]) == {TARGET_NAME, "job-pg-restore-verify-prod"}
    assert {entry["name"] for entry in az.servers} == {SERVING_NAME, RETAINED_NAME}


def test_cleanup_never_deletes_a_server_it_did_not_tag(module: ModuleType, az: Azure) -> None:
    provision(module, az)
    module.cleanup(az, environment=ENVIRONMENT, group=GROUP, run_id=RUN_ID)
    deletes = [
        az.argument(args, "--name")
        for args in az.calls
        if args[:3] == ("postgres", "flexible-server", "delete")
    ]
    assert deletes == [TARGET_NAME]
    assert SERVING_NAME not in deletes and RETAINED_NAME not in deletes


def test_cleanup_ignores_a_drill_server_from_another_run(module: ModuleType, az: Azure) -> None:
    az.servers.append(server(f"{SERVING_NAME}-rvother", tags={module.DRILL_TAG: "other"}))
    result = module.cleanup(az, environment=ENVIRONMENT, group=GROUP, run_id=RUN_ID)
    assert any(entry["name"].endswith("-rvother") for entry in az.servers)
    # Surviving is not enough: a predecessor drill's leftover is tagged, just not
    # with this run's id, and the runbook mandates one drill at a time — so it can
    # only be an uncleaned predecessor and must reach the operator. An
    # implementation reporting only *untagged* servers leaves it out and still
    # passes every other assertion in this file.
    assert result["inspect"] == [f"{SERVING_NAME}-rvother"]


def test_an_untagged_drill_shaped_server_is_surfaced_for_inspection(
    module: ModuleType, az: Azure
) -> None:
    # Assert on cleanup's own result, not on the helper. The operator surface is
    # the result; a teardown that merely skipped the orphan would leave it
    # invisible to everyone but a reader of the source, and a test calling the
    # helper directly would pass over that gap.
    az.servers.append(server(f"{SERVING_NAME}-rvorphan"))
    result = module.cleanup(az, environment=ENVIRONMENT, group=GROUP, run_id=RUN_ID)
    assert result["inspect"] == [f"{SERVING_NAME}-rvorphan"]
    assert any(entry["name"].endswith("-rvorphan") for entry in az.servers)


def test_cleanup_reports_nothing_to_inspect_when_no_orphan_exists(
    module: ModuleType, az: Azure
) -> None:
    provision(module, az)
    result = module.cleanup(az, environment=ENVIRONMENT, group=GROUP, run_id=RUN_ID)
    assert result["inspect"] == []


def test_cleanup_fails_when_a_delete_is_accepted_but_nothing_is_removed(
    module: ModuleType, az: Azure
) -> None:
    # The mutation probe for the removal proof: az returns success, the resource
    # stays. A cleanup that trusted the exit code would report a clean teardown.
    provision(module, az)
    az.delete_is_a_lie = True
    with pytest.raises(RuntimeError, match="survived their delete"):
        module.cleanup(az, environment=ENVIRONMENT, group=GROUP, run_id=RUN_ID)


def test_cleanup_fails_when_the_job_delete_is_accepted_but_nothing_is_removed(
    module: ModuleType, az: Azure
) -> None:
    # The server half of the removal proof is not the whole of it. A cleanup that
    # re-read servers but never re-read jobs would pass the server arm above while
    # leaving a job behind, so the job half needs its own lying delete.
    provision(module, az)
    verify(module, az)
    az.job_delete_is_a_lie = True
    with pytest.raises(RuntimeError, match="survived their delete"):
        module.cleanup(az, environment=ENVIRONMENT, group=GROUP, run_id=RUN_ID)


# --- 21. serving bindings are never touched ----------------------------------


def test_no_call_ever_mutates_a_serving_resource(module: ModuleType, az: Azure) -> None:
    provision(module, az)
    verify(module, az)
    module.cleanup(az, environment=ENVIRONMENT, group=GROUP, run_id=RUN_ID)
    mutating = {"update", "delete", "restart", "start", "create", "tag", "revision"}
    serving = {SERVING_NAME, RETAINED_NAME, "job-pg-bootstrap-prod", SERVING_FQDN}
    # Resource ids as well as names: the one tag write addresses its target by
    # `--ids`, so a resolver reading only `--name` yields "" for exactly the call
    # that could reach a serving server, and passes whatever it was pointed at.
    serving_ids = {
        entry["id"] for entry in az.servers if entry["name"] in {SERVING_NAME, RETAINED_NAME}
    }
    assert serving_ids, "the fixture must carry serving ids for this test to constrain anything"
    for call in az.calls:
        if not set(call) & mutating:
            continue
        addressed = set()
        for flag in ("--name", "--ids"):
            if flag in call:
                addressed.add(call[call.index(flag) + 1])
        assert not addressed & serving, f"a mutating call named a serving resource: {call}"
        assert not addressed & serving_ids, f"a mutating call addressed a serving id: {call}"
        # The one precedent for re-pointing a deployed job is the maintenance
        # dispatch's env merge. A drill must never reach for it: flipping the
        # bootstrap job's address is how production gets bootstrapped elsewhere.
        assert "--set-env-vars" not in call
        assert call[:3] != ("containerapp", "job", "update")
        assert call[:2] != ("containerapp", "update")


def test_a_geo_drill_never_mutates_a_serving_resource(module: ModuleType, az: Azure) -> None:
    geo_drill(module, az)
    geo_cleanup(module, az)
    # A geo drill's every write belongs to the drill group. The restore verbs are
    # mutating here even though the point-in-time test's vocabulary omits them:
    # they are the calls that decide which group gains a server.
    mutating = {
        "update",
        "delete",
        "restart",
        "start",
        "create",
        "tag",
        "revision",
        "restore",
        "geo-restore",
    }
    serving = {SERVING_NAME, RETAINED_NAME, "job-pg-bootstrap-prod", SERVING_FQDN, GROUP}
    serving_ids = {
        entry["id"] for entry in az.servers if entry["name"] in {SERVING_NAME, RETAINED_NAME}
    }
    writes = [call for call in az.calls if set(call) & mutating]
    assert serving_ids, "the fixture must carry serving ids for this test to constrain anything"
    assert writes, "the drill must issue writes for this test to constrain anything"
    for call in writes:
        addressed = set()
        for flag in ("--name", "--ids", "--resource-group"):
            if flag in call:
                addressed.add(call[call.index(flag) + 1])
        assert not addressed & serving, f"a geo drill write addressed serving state: {call}"
        assert not addressed & serving_ids, f"a geo drill write addressed a serving id: {call}"
        assert "--set-env-vars" not in call


def test_a_geo_network_in_the_drill_group_is_accepted_whatever_its_casing(
    module: ModuleType, az: Azure
) -> None:
    # Azure spells the group segment both ways in ids it returns, and group names
    # are case-insensitive. Every other fixture here uses one canonical casing, so
    # only this case separates a case-insensitive match from a literal one.
    subnet, zone = network_in(DRILL_GROUP.upper())
    provision(
        module,
        az,
        geo_location=GEO_LOCATION,
        geo_subnet=subnet.replace("/resourceGroups/", "/resourcegroups/"),
        geo_dns_zone=zone,
    )
    call = az.call("postgres", "flexible-server", "geo-restore")
    assert az.argument(call, "--subnet").startswith(
        "/subscriptions/s/resourcegroups/RG-CAS-PROD-GEODRILL-"
    )


def test_geo_cleanup_does_not_admit_another_resource_type_under_the_jobs_name(
    module: ModuleType, az: Azure
) -> None:
    # The exemption is the verification job by name *and* type. A name-only
    # exemption would delete an untagged resource of any kind that happened to
    # carry the job's name.
    footprint(module, az)
    az.resources.append(
        {
            "id": f"/subscriptions/s/resourceGroups/{DRILL_GROUP}/providers/"
            "Microsoft.Compute/virtualMachines/job-pg-restore-verify-prod",
            "name": "job-pg-restore-verify-prod",
            "type": "Microsoft.Compute/virtualMachines",
            "tags": {},
        }
    )
    with pytest.raises(ValueError, match="job-pg-restore-verify-prod"):
        geo_cleanup(module, az)
    assert not [args for args in az.calls if args[:2] == ("group", "delete")]
