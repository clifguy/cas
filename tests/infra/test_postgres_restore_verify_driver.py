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


def server(name: str, *, tags: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "id": f"/subscriptions/s/resourceGroups/{GROUP}/providers/"
        f"Microsoft.DBforPostgreSQL/flexibleServers/{name}",
        "name": name,
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

    def __call__(self, *args: str) -> Any:
        self.calls.append(args)
        prefix = args[:3]
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
            return list(self.servers)
        if prefix == ("postgres", "flexible-server", "restore") or prefix == (
            "postgres",
            "flexible-server",
            "geo-restore",
        ):
            name = args[args.index("--name") + 1]
            created = server(name)
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
            return list(self.jobs)
        if prefix == ("containerapp", "job", "delete"):
            name = args[args.index("--name") + 1]
            if not self.job_delete_is_a_lie:
                self.jobs = [entry for entry in self.jobs if entry["name"] != name]
            return None
        if prefix == ("containerapp", "job", "start"):
            return None
        if args[:4] == ("containerapp", "job", "execution", "list"):
            return [{"name": "exec-1", "properties": {"status": self.status}}]
        if prefix == ("deployment", "group", "create"):
            self.jobs.append({"name": "job-pg-restore-verify-prod"})
            return None
        raise AssertionError(f"unexpected az call: {args}")

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


GEO_SUBNET = (
    "/subscriptions/s/resourceGroups/rg-cas-dr/providers/Microsoft.Network/"
    "virtualNetworks/vnet-dr/subnets/postgres"
)
GEO_DNS_ZONE = (
    "/subscriptions/s/resourceGroups/rg-cas-dr/providers/Microsoft.Network/"
    "privateDnsZones/dr.private.postgres.database.azure.com"
)


def test_a_geo_restore_names_its_region_and_its_own_network(module: ModuleType, az: Azure) -> None:
    result = provision(
        module,
        az,
        geo_location="centralus",
        geo_subnet=GEO_SUBNET,
        geo_dns_zone=GEO_DNS_ZONE,
    )
    call = az.call("postgres", "flexible-server", "geo-restore")
    assert az.argument(call, "--location") == "centralus"
    # The destination region needs its own network. Passing the serving subnet
    # would name a resource in the wrong region entirely.
    assert az.argument(call, "--subnet") == GEO_SUBNET
    assert az.argument(call, "--private-dns-zone") == GEO_DNS_ZONE
    assert az.argument(call, "--subnet") != SUBNET
    assert result["mode"] == "geo-restore"


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


def test_a_job_that_never_finishes_raises_rather_than_looping(
    module: ModuleType, az: Azure
) -> None:
    az.status = "Running"
    with pytest.raises(TimeoutError, match="terminal status"):
        verify(module, az, poll_seconds=0.0, budget_seconds=0.0)


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
    module.cleanup(az, environment=ENVIRONMENT, group=GROUP, run_id=RUN_ID)
    assert any(entry["name"].endswith("-rvother") for entry in az.servers)


def test_an_untagged_drill_shaped_server_is_surfaced_for_inspection(
    module: ModuleType, az: Azure
) -> None:
    az.servers.append(server(f"{SERVING_NAME}-rvorphan"))
    assert module.adoptable(az, GROUP, RUN_ID) == [f"{SERVING_NAME}-rvorphan"]
    module.cleanup(az, environment=ENVIRONMENT, group=GROUP, run_id=RUN_ID)
    assert any(entry["name"].endswith("-rvorphan") for entry in az.servers)


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
    for call in az.calls:
        if not set(call) & mutating:
            continue
        named = call[call.index("--name") + 1] if "--name" in call else ""
        assert named not in serving, f"a mutating call named a serving resource: {call}"
        # The one precedent for re-pointing a deployed job is the maintenance
        # dispatch's env merge. A drill must never reach for it: flipping the
        # bootstrap job's address is how production gets bootstrapped elsewhere.
        assert "--set-env-vars" not in call
        assert call[:3] != ("containerapp", "job", "update")
        assert call[:2] != ("containerapp", "update")
