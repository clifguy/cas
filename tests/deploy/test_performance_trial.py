"""Behavioral safety controls for the opt-in isolated Azure trial."""

import copy
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import pytest

from deploy.performance_trial import owned_inventory, validate_plan

NOW = datetime(2026, 10, 23, tzinfo=UTC)


@lru_cache
def evidence_public_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    return (
        rsa.generate_private_key(public_exponent=65537, key_size=3072)
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def plan():
    return {
        "schema_version": 1,
        "evidence_public_key_pem": evidence_public_key(),
        "run_id": "trial-one",
        "subscription_id": "11111111-1111-1111-1111-111111111111",
        "location": "eastus2",
        "created_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(hours=4)).isoformat(),
        "source": {"resource_group": "rg-prod", "app": "ca-sage-prod"},
        "candidate": {"commit": "e" * 40, "image": "registry.azurecr.io/sage@sha256:" + "a" * 64},
        "binding": {
            "site_id": "host,site,web",
            "drive_id": "drive",
            "audience": "api://audience",
            "tenant_id": "tenant",
            "registry_id": (
                "/subscriptions/11111111-1111-1111-1111-111111111111/"
                "resourceGroups/rg-prod/providers/Microsoft.ContainerRegistry/registries/registry"
            ),
            "key_vault_id": (
                "/subscriptions/11111111-1111-1111-1111-111111111111/"
                "resourceGroups/rg-prod/providers/Microsoft.KeyVault/vaults/keys"
            ),
        },
        "limits": {
            "window_seconds": 14400,
            "per_run_seconds": 900,
            "incremental_usd": 50,
            "provider_calls": 30,
        },
        "arms": [
            {"profile": "Consumption", "cpu": 2, "memory": "4Gi"},
            {"profile": "D8", "cpu": 2, "memory": "8Gi"},
            {"profile": "D8", "cpu": 4, "memory": "8Gi"},
        ],
    }


def test_valid_plan_derives_exact_isolated_names_and_schema():
    p = validate_plan(plan(), NOW)
    assert p["resource_group"] == "rg-ingest-perf-trial-one"
    assert p["controller_group"] == "rg-ingest-perf-control-trial-one"
    assert p["vault_id"] == "perf_trial_one"
    assert p["vault_source_root"] == "performance/trial-one"


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "../prod"),
        ("subscription_id", "other"),
        ("expires_at", NOW.isoformat()),
        ("expires_at", (NOW + timedelta(hours=5)).isoformat()),
        ("resource_group", "rg-prod"),
        ("controller_group", "rg-prod"),
        ("vault_source_root", "vaults"),
        ("vault_id", "cas"),
    ],
)
def test_unapproved_identity_or_expiry_refused(field, value):
    p = plan()
    p[field] = value
    with pytest.raises(ValueError):
        validate_plan(p, NOW)


@pytest.mark.parametrize(
    "field,value",
    [
        ("window_seconds", 14401),
        ("per_run_seconds", 901),
        ("incremental_usd", 51),
        ("provider_calls", 31),
        ("provider_calls", True),
    ],
)
def test_limit_expansion_refused(field, value):
    p = plan()
    p["limits"][field] = value
    with pytest.raises(ValueError):
        validate_plan(p, NOW)


def test_moving_image_and_cpu_only_false_control_refused():
    p = plan()
    p["candidate"]["image"] = "registry.azurecr.io/sage:latest"
    with pytest.raises(ValueError):
        validate_plan(p, NOW)
    p = plan()
    p["arms"][2]["memory"] = "16Gi"
    with pytest.raises(ValueError):
        validate_plan(p, NOW)
    p = plan()
    p["arms"][0]["memory"] = "8Gi"
    with pytest.raises(ValueError):
        validate_plan(p, NOW)


def inventory():
    p = validate_plan(plan(), NOW)
    group = {
        "id": "/subscriptions/" + p["subscription_id"] + "/resourceGroups/" + p["resource_group"],
        "name": p["resource_group"],
        "tags": {"benchmarkRun": p["run_id"], "expiresAt": p["expires_at"]},
    }
    resource = {
        "id": group["id"] + "/providers/Microsoft.App/containerApps/trial",
        "tags": copy.deepcopy(group["tags"]),
    }
    return p, group, [resource]


def test_cleanup_reads_exact_owned_inventory_and_refuses_every_drift():
    p, group, resources = inventory()
    assert owned_inventory(p, group, resources) == resources
    for subject, field, value in [
        (group, "name", "rg-prod"),
        (group, "id", "/subscriptions/other/resourceGroups/" + p["resource_group"]),
        (
            resources[0],
            "id",
            "/subscriptions/other/resourceGroups/other/providers/Microsoft.App/containerApps/trial",
        ),
        (group, "tags", {}),
        (resources[0], "tags", {}),
    ]:
        old = subject[field]
        subject[field] = value
        with pytest.raises(ValueError):
            owned_inventory(p, group, resources)
        subject[field] = old


def test_runtime_counts_real_process_and_reserves_across_restart():
    from deploy.performance_runtime import Budget, process_sample

    sample = process_sample()
    assert sample["process_peak_bytes"] > 0
    assert sample["process_cpu_seconds"] >= 0
    assert sample["pid"] > 0

    class Store:
        value = None
        version = 0

        def read_counter(self):
            return self.value, str(self.version)

        def write_counter(self, value, etag):
            assert etag == str(self.version)
            self.value = value
            self.version += 1
            return True

    store = Store()
    Budget(store, "trial-one", 2).reserve()
    Budget(store, "trial-one", 2).reserve()
    assert store.value == {"run_id": "trial-one", "used": 2}
    with pytest.raises(RuntimeError, match="budget"):
        Budget(store, "trial-one", 2).reserve()
    with pytest.raises(RuntimeError):
        Budget(store, "different", 3).reserve()


def test_uncertain_budget_reservation_prevents_provider_execution():
    from deploy.performance_runtime import Budget

    class Store:
        def read_counter(self):
            return None, "0"

        def write_counter(self, value, etag):
            raise TimeoutError("uncertain write")

    called = []
    with pytest.raises(TimeoutError):
        Budget(Store(), "trial-one", 2).reserve()
        called.append("provider")
    assert called == []


def test_no_billable_stack_before_independent_expiry(tmp_path):
    from deploy.performance_trial import provision

    p = validate_plan(plan(), NOW)
    p["candidate"]["abstraction_model"] = "model-pin"
    calls = []

    def az(*args):
        calls.append(("read", args))
        return False

    def apply(p, az, parameters, path, preview=False, **kwargs):
        mode = parameters["parameters"]["deployStack"]["value"]
        calls.append(("apply", mode, preview))
        return {"properties": {"outputs": {"automationName": {"value": "owned-account"}}}}

    def denied(*args):
        raise RuntimeError("expiry not enabled")

    with pytest.raises(RuntimeError, match="expiry"):
        provision(
            p,
            az,
            None,
            principal="principal",
            ingress_key="secret",
            output=tmp_path,
            precheck=lambda *x: None,
            apply=apply,
            enable=denied,
        )
    assert calls == [
        ("read", ("group", "exists", "-n", p["resource_group"])),
        ("read", ("group", "exists", "-n", p["controller_group"])),
        ("apply", False, True),
        ("apply", False, False),
        ("apply", False, True),
        ("apply", False, False),
    ]
    assert not (tmp_path / "expiry-receipt.json").exists()
    assert "secret" not in (tmp_path / "provision-receipt.json").read_text()
    calls.clear()

    def precheck(*args):
        raise ValueError("wrong subscription")

    with pytest.raises(ValueError):
        provision(
            p,
            az,
            None,
            principal="p",
            ingress_key="s",
            output=tmp_path,
            precheck=precheck,
            apply=apply,
            enable=denied,
        )
    assert calls == []


def test_full_order_places_expiry_before_stack(tmp_path):
    from deploy.performance_trial import provision

    p = validate_plan(plan(), NOW)
    p["candidate"]["abstraction_model"] = "model-pin"
    effects = []

    def apply(p, az, params, path, preview=False, **kwargs):
        effects.append(
            ("stack" if params["parameters"]["deployStack"]["value"] else "foundation", preview)
        )
        return {"properties": {"outputs": {"automationName": {"value": "owned"}}}}

    def expiry(*args):
        effects.append("expiry")
        return {"verified": True}

    assert provision(
        p,
        lambda *a: False,
        None,
        principal="p",
        ingress_key="s",
        output=tmp_path,
        precheck=lambda *a: effects.append("preflight"),
        apply=apply,
        enable=expiry,
    ) == {"automationName": "owned"}
    assert effects == [
        "preflight",
        ("foundation", True),
        ("foundation", False),
        ("foundation", True),
        ("foundation", False),
        "expiry",
        ("stack", True),
        ("stack", False),
    ]


def test_reconciliation_detects_coverage_order_and_bad_norm():
    from types import SimpleNamespace

    from deploy.performance_reconcile import verify_rows

    chunks = [SimpleNamespace(chunk_index=0, heading_path="A", content="body", section_index=0)]
    rows = [(0, "A", "body", 0, [1.0] + [0.0] * 767)]
    verify_rows(chunks, rows)
    for invalid in [
        [],
        [(1, "A", "body", 0, rows[0][4])],
        [(0, "A", "other", 0, rows[0][4])],
        [(0, "A", "body", 0, [0.0] * 768)],
        [(0, "A", "body", 0, [float("nan")] * 768)],
    ]:
        with pytest.raises(ValueError):
            verify_rows(chunks, invalid)


def test_accepted_job_dispatch_pins_unique_new_execution_without_retry(tmp_path, monkeypatch):
    from deploy import performance_trial as subject

    p = validate_plan(plan(), NOW)
    job_id = (
        "/subscriptions/"
        + p["subscription_id"]
        + "/resourceGroups/"
        + p["resource_group"]
        + "/providers/Microsoft.App/jobs/benchmark"
    )
    job = {
        "id": job_id,
        "tags": {"benchmarkRun": p["run_id"]},
        "properties": {
            "configuration": {"replicaTimeout": 900, "replicaRetryLimit": 0},
            "template": {"containers": []},
        },
    }
    execution = {"id": job_id + "/executions/new", "name": "new"}
    lists = iter([[], [execution]])
    effects = []

    def az(*args):
        if args[:3] == ("containerapp", "job", "show"):
            return job
        return next(lists)

    class Rest:
        def arm(self, method, path, **kwargs):
            effects.append((method, path, kwargs))
            return type("Response", (), {"status_code": 202, "content": b""})()

    monkeypatch.setattr(
        subject,
        "wait_job",
        lambda az, group, job, name, deadline: {
            "name": name,
            "properties": {"status": "Succeeded"},
        },
    )
    got = subject.start_template_job(
        p, az, Rest(), "benchmark", {"containers": []}, tmp_path, "case"
    )
    assert got["name"] == "new"
    assert len(effects) == 1
    assert effects[0][0:2] == ("POST", job_id + "/start")
    assert "body" not in effects[0][2]


def test_unowned_cleanup_never_revokes_external_access(tmp_path):
    import json

    from deploy.performance_trial import cleanup

    p, group, resources = inventory()
    group["tags"] = {}
    import hashlib

    from deploy.performance_trial import export_evidence, plan_digest
    from tests.deploy.test_performance_export import Snapshot

    outputs = {"exportUrl": "https://stperfunit.blob.core.windows.net/evidence/trial-one"}
    export_evidence(p, outputs, Snapshot(), tmp_path)
    proof = hashlib.sha256((tmp_path / "export-receipt.json").read_bytes()).hexdigest()
    (tmp_path / "provision-receipt.json").write_text(
        json.dumps(
            {
                "outputs": outputs,
                "plan_sha256": plan_digest(p),
            }
        )
    )
    effects = []

    def az(*args):
        effects.append(args)
        if args[:2] == ("group", "exists"):
            return True
        if args[:2] == ("group", "show"):
            return group
        return resources

    class Rest:
        def graph(self, *args):
            pytest.fail("unowned inventory reached Graph revocation")

        def arm(self, *args, **kwargs):
            pytest.fail("unowned inventory reached role revocation")

    with pytest.raises(ValueError, match="unowned"):
        cleanup(p, az, Rest(), tmp_path, export_receipt_sha256=proof)
    assert not any("delete" in x for x in effects)
