"""Exercise the actual deployment driver with an Azure command boundary."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def driver():
    spec = importlib.util.spec_from_file_location(
        "migration_driver", ROOT / "deploy/postgres-migration.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Azure:
    def __init__(self):
        self.calls = []
        self.job_drift = {}
        self.image_drift = None
        self.active_job = False
        self.status = "Succeeded"
        self.fence = ""
        self.revisions = '{"sage":["sage-rev"],"bff":["bff-rev"]}'
        self.outputs = {
            key: {"value": value}
            for key, value in {
                "postgresServerFqdn": "source.postgres.database.azure.com",
                "postgresDatabaseName": "sage",
                "bootstrapJobName": "bootstrap",
                "maintenanceJobName": "maintenance",
                "sageContainerAppName": "sage",
                "bffContainerAppName": "bff",
            }.items()
        }
        self.env = {
            key: value
            for key, value in {
                "PG_SOURCE_FQDN": "source.postgres.database.azure.com",
                "PG_TARGET_FQDN": "target.postgres.database.azure.com",
                "PG_MIGRATION_RUN_ID": "g17",
                "PG_SOURCE_MAJOR": "16",
                "PG_TARGET_MAJOR": "17",
                "PG_ADMIN_USER": "id-bootstrap",
                "AZURE_CLIENT_ID": "client",
                "SAGE_DB_ROLE": "id-sage",
                "BFF_DB_ROLE": "id-bff",
                "PG_DATABASE": "sage",
            }.items()
        }

    def __call__(self, *args):
        self.calls.append(args)
        prefix = args[:3]
        if prefix == ("deployment", "sub", "show"):
            return {"properties": {"outputs": self.outputs}}
        if prefix == ("deployment", "group", "show"):
            return {
                "properties": {
                    "outputs": {
                        "postgresServerFqdn": {"value": "target.postgres.database.azure.com"},
                        "postgresServerName": {"value": "target"},
                    }
                }
            }
        if prefix == ("containerapp", "job", "show"):
            migration = args[args.index("--name") + 1].startswith("job-pg-migration-")
            env = {**self.env, **(self.job_drift if migration else {})}
            return {
                "identity": {"userAssignedIdentities": {"identity-id": {}}},
                "location": "eastus",
                "properties": {
                    "environmentId": "environment-id",
                    "template": {
                        "containers": [
                            {
                                "image": self.image_drift
                                if migration and self.image_drift
                                else "registry/sage:1.0-abcdef",
                                "env": [{"name": k, "value": v} for k, v in env.items()],
                            }
                        ]
                    },
                    "configuration": {"registries": [{"server": "registry"}]},
                },
            }
        if prefix == ("containerapp", "job", "execution"):
            if args[3] == "list":
                return [{"properties": {"status": "Running"}}] if self.active_job else []
            return {"properties": {"status": self.status}}
        if prefix == ("containerapp", "revision", "list"):
            return [
                {"name": args[args.index("--name") + 1] + "-rev", "properties": {"active": True}}
            ]
        if prefix == ("containerapp", "job", "start"):
            return {"name": "execution"}
        if prefix == ("postgres", "flexible-server", "show"):
            name = args[args.index("--name") + 1]
            return {
                "name": name,
                "version": "16" if name == "source" else "17",
                "state": "Ready",
                "backup": {
                    "backupRetentionDays": 35,
                    "geoRedundantBackup": "Disabled" if name == "source" else "Enabled",
                },
                "network": {
                    "delegatedSubnetResourceId": "/vnets/main/subnets/postgres",
                    "privateDnsZoneArmResourceId": "dns",
                },
                "location": "eastus",
            }
        if args[:2] == ("identity", "show"):
            return {"principalId": "principal"}
        if args[:2] == ("group", "show"):
            return {
                "id": "group-id",
                "tags": {
                    "casPostgresMigration": self.fence,
                    "casPostgresMigrationRevisions": self.revisions,
                },
            }
        if args[:2] == ("lock", "list"):
            return [{"level": "CanNotDelete"}]
        if args[:2] == ("tag", "update"):
            value = args[args.index("--tags") + 1]
            if value.startswith("casPostgresMigration="):
                self.fence = "" if "Delete" in args else value.split("=", 1)[1]
            else:
                self.revisions = value.split("=", 1)[1]
        return {}


def test_migrate_orders_fence_stop_copy_and_verification():
    az = Azure()
    driver().migrate(az, "prod", "group", "g17", "g17", sleep=lambda _: None)
    mutations = [
        call
        for call in az.calls
        if call[:2] == ("tag", "update")
        or call[:3]
        in [("containerapp", "revision", "deactivate"), ("containerapp", "job", "start")]
    ]
    assert [c[:3] for c in mutations] == [
        ("tag", "update", "--resource-id"),
        ("containerapp", "revision", "deactivate"),
        ("containerapp", "revision", "deactivate"),
        ("containerapp", "job", "start"),
        ("tag", "update", "--resource-id"),
    ]
    assert "casPostgresMigration=copying:g17" in mutations[0]
    assert "casPostgresMigration=verified:g17" in mutations[-1]
    assert "--env-vars" not in mutations[-2]


@pytest.mark.parametrize("failure", ["confirmation", "running_job", "copy_failed"])
def test_migration_fails_closed(failure):
    az = Azure()
    az.active_job = failure == "running_job"
    az.status = "Failed" if failure == "copy_failed" else "Succeeded"
    with pytest.raises(
        ValueError,
        match={
            "confirmation": "confirmation",
            "running_job": "still active",
            "copy_failed": "job failed",
        }[failure],
    ):
        driver().migrate(
            az,
            "prod",
            "group",
            "g17",
            "wrong" if failure == "confirmation" else "g17",
            sleep=lambda _: None,
        )
    assert az.fence != "verified:g17"
    if failure == "copy_failed":
        assert az.fence == "copying:g17"
    else:
        assert not any(c[:2] == ("tag", "update") for c in az.calls)
    assert not any(c[:3] == ("containerapp", "revision", "activate") for c in az.calls)


def test_prepare_provisions_only_replacement_and_migration_job():
    az = Azure()
    driver().prepare(az, "prod", "group", "g17")
    writes = [c for c in az.calls if c[:3] == ("deployment", "group", "create")]
    assert len(writes) == 2
    assert any("infra/postgres-replacement.bicep" in c for c in writes)
    assert any("infra/modules/postgres-migration-job.bicep" in c for c in writes)
    assert not any(
        c[:2] == ("tag", "update") or c[:2] == ("containerapp", "revision") for c in az.calls
    )


@pytest.mark.parametrize(
    "state,allowed",
    [("copying:g17", True), ("verified:g17", True), ("cutover:g17", False), ("serving:g17", False)],
)
def test_rollback_only_before_cutover(state, allowed):
    az = Azure()
    az.fence = state
    az.revisions = '{"sage":["sage-rev"],"bff":["bff-rev"]}'
    if allowed:
        driver().rollback(az, "prod", "group", "g17", "g17", sleep=lambda _: None)
        assert az.fence == ""
        assert [
            c[c.index("--revision") + 1]
            for c in az.calls
            if c[:3] == ("containerapp", "revision", "activate")
        ] == ["sage-rev", "bff-rev"]
    else:
        with pytest.raises(ValueError, match="cutover"):
            driver().rollback(az, "prod", "group", "g17", "g17", sleep=lambda _: None)
        assert not any(c[:3] == ("containerapp", "job", "start") for c in az.calls)


def test_prepare_refuses_to_reconfigure_a_fenced_migration():
    az = Azure()
    az.fence = "copying:g17"
    with pytest.raises(ValueError, match="fence"):
        driver().prepare(az, "prod", "group", "g17")
    assert not any(c[:3] == ("deployment", "group", "create") for c in az.calls)


def test_initial_migration_records_source_revisions_before_stopping():
    az = Azure()
    az.revisions = ""
    driver().migrate(az, "prod", "group", "g17", "g17", sleep=lambda _: None)
    import json

    assert json.loads(az.revisions) == {"sage": ["sage-rev"], "bff": ["bff-rev"]}
    record = next(
        i
        for i, c in enumerate(az.calls)
        if any(a.startswith("casPostgresMigrationRevisions=") for a in c)
    )
    stop = next(
        i for i, c in enumerate(az.calls) if c[:3] == ("containerapp", "revision", "deactivate")
    )
    assert record < stop


@pytest.mark.parametrize("drift", ["database", "image"])
def test_migration_rejects_stale_prepared_job(drift):
    az = Azure()
    if drift == "database":
        az.job_drift = {"PG_DATABASE": "different"}
    else:
        az.image_drift = "registry/sage:old-image"
    with pytest.raises(ValueError, match="prepared"):
        driver().migrate(az, "prod", "group", "g17", "g17", sleep=lambda _: None)
    assert not any(c[:2] == ("tag", "update") for c in az.calls)
