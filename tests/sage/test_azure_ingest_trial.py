"""Trial guards reject target drift before any Azure data-plane write."""

import pytest

from sage.utils.azure_ingest_trial import latency_summary, validate_target


def target():
    app = "/subscriptions/sub/resourceGroups/rg-perf/providers/Microsoft.App/containerApps/perf"
    plan = {
        "run_id": "run-one",
        "source": {"resource_group": "rg-prod", "app_resource_id": app + "-prod"},
        "target": {
            "resource_group": "rg-perf",
            "app_resource_id": app,
            "vault_id": "perf_run_one",
            "config_secret_name": "sage-cloud-config",
            "environment_resource_id": app.replace(
                "containerApps/perf", "managedEnvironments/perf"
            ),
            "postgres_resource_id": app.replace(
                "Microsoft.App/containerApps/perf", "Microsoft.DBforPostgreSQL/flexibleServers/perf"
            ),
            "image_digest": "registry/app@sha256:" + "a" * 64,
            "cpu": 2.0,
            "memory": "4Gi",
            "origin": "https://perf.example.com",
            "revision_name": "perf--approved",
            "restart_before_trial": True,
        },
    }
    live = {
        "id": app,
        "resourceGroup": "rg-perf",
        "tags": {"benchmarkRun": "run-one"},
        "properties": {
            "environmentId": plan["target"]["environment_resource_id"],
            "provisioningState": "Succeeded",
            "latestRevisionName": "perf--approved",
            "latestReadyRevisionName": "perf--approved",
            "configuration": {
                "activeRevisionsMode": "Single",
                "ingress": {
                    "fqdn": "perf.example.com",
                    "traffic": [{"latestRevision": True, "weight": 100}],
                },
            },
            "template": {
                "scale": {"minReplicas": 1, "maxReplicas": 1},
                "volumes": [
                    {
                        "name": "config",
                        "storageType": "Secret",
                        "secrets": [{"secretRef": "sage-cloud-config", "path": "config.yaml"}],
                    }
                ],
                "containers": [
                    {
                        "name": "sage",
                        "image": plan["target"]["image_digest"],
                        "resources": {"cpu": 2.0, "memory": "4Gi"},
                        "env": [{"name": "SAGE_CONFIG_PATH", "value": "/etc/sage/config.yaml"}],
                        "volumeMounts": [{"volumeName": "config", "mountPath": "/etc/sage"}],
                    }
                ],
            },
        },
    }
    return plan, live


def test_exact_isolated_target_is_accepted():
    plan, live = target()
    assert validate_target(plan, live) == "https://perf.example.com"


@pytest.mark.parametrize(
    "field,value",
    [
        ("resource_group", "rg-prod"),
        ("vault_id", "cas"),
        ("image_digest", "registry/app:latest"),
        ("cpu", 4),
        ("origin", "https://other.example.com"),
        ("origin", "https://perf.example.com:444"),
    ],
)
def test_changed_target_refused(field, value):
    plan, live = target()
    plan["target"][field] = value
    with pytest.raises(ValueError):
        validate_target(plan, live)


def test_wrong_owner_or_replica_count_refused():
    plan, live = target()
    live["tags"]["benchmarkRun"] = "other"
    with pytest.raises(ValueError, match="ownership"):
        validate_target(plan, live)
    live["tags"]["benchmarkRun"] = plan["run_id"]
    live["properties"]["template"]["scale"]["maxReplicas"] = 2
    with pytest.raises(ValueError, match="replica"):
        validate_target(plan, live)


def test_failed_probe_cannot_be_hidden_in_latency_summary():
    samples = [
        {"kind": kind, "success": True, "seconds": 0.1}
        for kind in ("lightweight", "semantic", "keyword")
    ]
    assert latency_summary(samples)["semantic"]["count"] == 1
    samples[1]["success"] = False
    with pytest.raises(ValueError):
        latency_summary(samples)


async def test_digest_bound_upload_keeps_bearer_out_of_byte_leg(tmp_path, monkeypatch):
    await _exercise_digest_bound_upload(tmp_path, monkeypatch, ".md")


async def test_csv_upload_uses_structured_data_and_scoped_byte_auth(tmp_path, monkeypatch):
    await _exercise_digest_bound_upload(tmp_path, monkeypatch, ".csv")


async def _exercise_digest_bound_upload(tmp_path, monkeypatch, suffix):
    import hashlib

    import httpx

    from sage.utils.azure_ingest_trial import ingest_source

    path = tmp_path / ("witness" + suffix)
    path.write_text("A positive search witness.")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    observed = []

    def service(request):
        import json

        payload = json.loads(request.content)
        observed.append(payload)
        assert request.headers["authorization"] == "Bearer private"
        if "source" in payload:
            return httpx.Response(
                200,
                json={
                    "status": "upload_required",
                    "max_bytes": 1000,
                    "token_header": "X-Upload-Token",
                    "uploads": [
                        {
                            "url": "https://perf.example.com/upload",
                            "token": "scoped",
                            "transfer_id": "transfer",
                            "sha256": digest,
                        }
                    ],
                },
            )
        assert payload["transfer_token"] == "scoped"
        return httpx.Response(
            201,
            json={
                "pipeline_status": "abstraction_complete",
                "document": {"id": "document", "source_content_hash": "sha256:" + digest},
            },
        )

    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from sage.auth import INGRESS_KEY_HEADER, IngressKeyGuard

    uploaded = []

    async def upload(scope, receive, send):
        request = Request(scope, receive)
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert request.headers["x-upload-token"] == "scoped"
        raw = await request.body()
        assert raw == path.read_bytes()
        uploaded.append(raw)
        await JSONResponse(
            {"transfer_id": "transfer", "size": len(raw), "sha256": digest}, status_code=201
        )(scope, receive, send)

    original = httpx.AsyncClient
    async with original(
        transport=httpx.MockTransport(service),
        headers={
            "Authorization": "Bearer private",
            INGRESS_KEY_HEADER: "trial-key",
            "Cookie": "private-cookie",
        },
    ) as client:
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kw: original(
                transport=httpx.ASGITransport(app=IngressKeyGuard(upload, key="trial-key")), **kw
            ),
        )
        result = await ingest_source(client, "https://perf.example.com", "perf-one", path)
    assert result["document_id"] == "document"
    assert result["source_sha256"] == digest
    assert len(observed) == 2
    assert uploaded == [path.read_bytes()]


def test_deployed_cloud_config_must_point_to_owned_database_and_source_root():
    import hashlib

    from sage.utils.azure_ingest_trial import validate_backend

    plan, _ = target()
    text = (
        "profile: cloud\nabstraction:\n  provider: anthropic\npostgres:\n"
        "  host: perf.postgres.example.com\n  database: perf\ndocument_store:\n"
        "  root_path: performance/run-one\n"
    )
    plan["target"].update(
        stack_config_sha256=hashlib.sha256(text.encode()).hexdigest(),
        postgres_database="perf",
        vault_source_root="performance/run-one",
    )
    pg = {
        "id": plan["target"]["postgres_resource_id"],
        "tags": {"benchmarkRun": "run-one"},
        "properties": {"fullyQualifiedDomainName": "perf.postgres.example.com"},
    }
    validate_backend(plan, pg, text)
    changed = text.replace("perf.postgres.example.com", "prod.postgres.example.com")
    plan["target"]["stack_config_sha256"] = hashlib.sha256(changed.encode()).hexdigest()
    with pytest.raises(ValueError, match="database"):
        validate_backend(plan, pg, changed)


def test_preparation_entrypoint_is_read_only_and_leaves_operational_fields_unresolved(
    tmp_path, monkeypatch
):
    import json
    import sys

    import scripts.azure_ingest_trial as cli

    plan, live = target()
    live.update(name="perf", location="eastus2")
    calls = []

    def azure(*args):
        calls.append(args)
        assert args[:2] == ("containerapp", "show")
        return live

    output = tmp_path / "plan.json"
    monkeypatch.setattr(cli, "azure", azure)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "azure_ingest_trial",
            "prepare",
            "--source-group",
            "rg-prod",
            "--source-app",
            "app-prod",
            "--run-id",
            "one",
            "--output",
            str(output),
        ],
    )
    assert cli.main() == 0
    result = json.loads(output.read_text())
    assert result["target"]["resource_group"] != result["source"]["resource_group"]
    assert result["target"]["image_digest"] is None
    assert result["target"]["stack_config_sha256"] is None
    assert result["required_before_operational_approval"]
    assert len(calls) == 1


def test_runtime_benchmark_entrypoint_is_in_the_shipped_source_tree():
    import subprocess
    import sys

    run = subprocess.run(
        [sys.executable, "-m", "sage.utils.embedding_benchmark", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0
    assert "--batch-size" in run.stdout
    assert "--uninstrumented" in run.stdout


def serving_target():
    import copy

    plan, live = target()
    revision = {
        "name": plan["target"]["revision_name"],
        "id": live["id"] + "/revisions/" + plan["target"]["revision_name"],
        "properties": {
            "active": True,
            "healthState": "Healthy",
            "provisioningState": "Provisioned",
            "runningState": "Running",
            "replicas": 1,
            "trafficWeight": 100,
            "template": copy.deepcopy(live["properties"]["template"]),
        },
    }
    return plan, live, revision


def replica(revision, container_id):
    return {
        "name": "replica-" + container_id,
        "id": revision["id"] + "/replicas/replica-" + container_id,
        "properties": {
            "runningState": "Running",
            "containers": [
                {
                    "name": "sage",
                    "containerId": container_id,
                    "ready": True,
                    "started": True,
                    "runningState": "Running",
                    "restartCount": 0,
                }
            ],
        },
    }


@pytest.mark.parametrize(
    "drift", ["failed-latest", "split-traffic", "old-template", "other-active"]
)
def test_serving_revision_guard_refuses_desired_versus_actual_drift(drift):
    import copy

    from sage.utils.azure_ingest_trial import validate_serving_revision

    plan, live, revision = serving_target()
    assert validate_serving_revision(plan, live, [revision]) == revision
    revisions = [revision]
    if drift == "failed-latest":
        live["properties"]["latestReadyRevisionName"] = "perf--old"
    elif drift == "split-traffic":
        live["properties"]["configuration"]["ingress"]["traffic"] = [
            {"revisionName": "perf--old", "weight": 50},
            {"revisionName": revision["name"], "weight": 50},
        ]
    elif drift == "old-template":
        revision["properties"]["template"]["containers"][0]["resources"]["cpu"] = 4
    else:
        revisions.append(copy.deepcopy(revision))
        revisions[-1]["name"] = "perf--other"
    with pytest.raises(ValueError):
        validate_serving_revision(plan, live, revisions)


@pytest.mark.parametrize("replace_process", [True, False])
async def test_trial_restart_requires_new_ready_process_and_rechecks_config(
    monkeypatch, replace_process
):
    import asyncio

    import scripts.azure_ingest_trial as cli

    plan, live, revision = serving_target()
    live["name"] = "perf"
    calls = []
    checks = []
    restarted = False

    def azure(*args):
        nonlocal restarted
        calls.append(args)
        if args[:3] == ("containerapp", "revision", "restart"):
            restarted = True
            return None
        if args[:2] == ("containerapp", "show"):
            return live
        if args[:3] == ("containerapp", "revision", "list"):
            return [revision]
        if args[:3] == ("containerapp", "replica", "list"):
            return [replica(revision, "new" if restarted and replace_process else "old")]
        if args[:2] == ("resource", "show"):
            return {"id": plan["target"]["postgres_resource_id"]}
        if args[:3] == ("containerapp", "secret", "list"):
            return "approved-secret"
        raise AssertionError(args)

    monkeypatch.setattr(cli, "azure", azure)
    monkeypatch.setattr(cli, "validate_backend", lambda p, pg, text: checks.append(text))
    original_sleep = asyncio.sleep
    sleeps = 0

    async def bounded_sleep(delay):
        nonlocal sleeps
        sleeps += 1
        if not replace_process or sleeps > 2:
            raise TimeoutError("unchanged process cannot prove the secret was loaded")
        await original_sleep(0)

    monkeypatch.setattr(cli.asyncio, "sleep", bounded_sleep)
    if replace_process:
        _, process = await cli.restart_trial(plan)
        assert process["container_id"] == "new"
    else:
        with pytest.raises(TimeoutError):
            await cli.restart_trial(plan)
    assert restarted
    assert checks == ["approved-secret", "approved-secret"]
    assert len([c for c in calls if c[:3] == ("containerapp", "revision", "restart")]) == 1


async def test_trial_restart_requires_explicit_approval_and_valid_target(monkeypatch):
    import scripts.azure_ingest_trial as cli

    plan, _, _ = serving_target()
    plan["target"]["restart_before_trial"] = False
    calls = []
    monkeypatch.setattr(cli, "azure", lambda *args: calls.append(args))
    with pytest.raises(ValueError, match="explicitly approve"):
        await cli.restart_trial(plan)
    assert calls == []


@pytest.mark.parametrize("fault", ["not-ready", "other-revision", "two-replicas"])
def test_ready_replica_refuses_missing_process_evidence(fault):
    from sage.utils.azure_ingest_trial import ready_replica

    _, _, revision = serving_target()
    rows = [replica(revision, "one")]
    assert ready_replica(revision, rows)["container_id"] == "one"
    if fault == "not-ready":
        rows[0]["properties"]["containers"][0]["ready"] = False
    elif fault == "other-revision":
        rows[0]["id"] = "other/revisions/old/replicas/one"
    else:
        rows.append(replica(revision, "two"))
    with pytest.raises(ValueError):
        ready_replica(revision, rows)


async def test_execute_refuses_runtime_guard_before_any_http_write(tmp_path, monkeypatch):
    import json
    from argparse import Namespace

    import scripts.azure_ingest_trial as cli

    plan, _, _ = serving_target()
    plan["proposed_limits"] = {"per_run_seconds": 60}
    manifest = tmp_path / "plan.json"
    manifest.write_text(json.dumps(plan))
    sources = [tmp_path / "witness.md", tmp_path / "bulk.csv"]
    for path in sources:
        path.write_text("bounded source")
    checked = []

    async def refuse(plan):
        checked.append(plan["run_id"])
        raise ValueError("runtime guard refused")

    def no_http(**kwargs):
        raise AssertionError("HTTP must not begin before runtime guard")

    monkeypatch.setattr(cli, "restart_trial", refuse)
    monkeypatch.setattr(cli.httpx, "AsyncClient", no_http)
    monkeypatch.setenv("SAGE_TRIAL_ACCESS_TOKEN", "test")
    with pytest.raises(ValueError, match="runtime guard refused"):
        await cli.execute(
            Namespace(manifest=manifest, sources=sources, output=tmp_path / "out.jsonl")
        )
    assert checked == [plan["run_id"]]


async def test_trial_restart_refuses_unsafe_target_before_control_write(monkeypatch):
    import scripts.azure_ingest_trial as cli

    plan, live, revision = serving_target()
    live["name"] = "perf"
    plan["target"]["resource_group"] = "rg-prod"
    calls = []

    def azure(*args):
        calls.append(args)
        if args[:2] == ("containerapp", "show"):
            return live
        if args[:3] == ("containerapp", "revision", "list"):
            return [revision]
        raise AssertionError("unsafe target reached a later operation")

    monkeypatch.setattr(cli, "azure", azure)
    with pytest.raises(ValueError, match="distinct resource group"):
        await cli.restart_trial(plan)
    assert len(calls) == 2


def test_trial_vault_is_a_valid_postgres_schema():
    from sage.storage.postgres.schema import refuse_reserved_schema

    plan, live = target()
    plan["target"]["vault_id"] = "perf_run_one"
    refuse_reserved_schema(plan["target"]["vault_id"])
    assert validate_target(plan, live) == "https://perf.example.com"


def test_reviewed_script_secret_must_be_mounted_at_approved_entry_path():
    plan, live = target()
    plan["target"]["mounted_script_paths"] = {"trial-runtime": "/etc/sage/performance_runtime.py"}
    template = live["properties"]["template"]
    mount = template["containers"][0]["volumeMounts"][0]
    volume = next(x for x in template["volumes"] if x["name"] == mount["volumeName"])
    expected = mount["mountPath"] + "/performance_runtime.py"
    plan["target"]["mounted_script_paths"]["trial-runtime"] = expected
    volume["secrets"].append({"secretRef": "trial-runtime", "path": "performance_runtime.py"})
    validate_target(plan, live)
    volume["secrets"][-1]["path"] = "unused.py"
    with pytest.raises(ValueError, match="script"):
        validate_target(plan, live)
