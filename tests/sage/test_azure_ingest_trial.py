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
            "vault_id": "perf-run-one",
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
        },
    }
    live = {
        "id": app,
        "resourceGroup": "rg-perf",
        "tags": {"benchmarkRun": "run-one"},
        "properties": {
            "environmentId": plan["target"]["environment_resource_id"],
            "configuration": {"ingress": {"fqdn": "perf.example.com"}},
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
    import hashlib

    import httpx

    from sage.utils.azure_ingest_trial import ingest_source

    path = tmp_path / "witness.md"
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

    def upload(request):
        assert "authorization" not in request.headers
        assert request.headers["x-upload-token"] == "scoped"
        assert request.content == path.read_bytes()
        return httpx.Response(
            201, json={"transfer_id": "transfer", "size": len(request.content), "sha256": digest}
        )

    original = httpx.AsyncClient
    async with original(
        transport=httpx.MockTransport(service), headers={"Authorization": "Bearer private"}
    ) as client:
        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(upload), **kw)
        )
        result = await ingest_source(client, "https://perf.example.com", "perf-one", path)
    assert result["document_id"] == "document"
    assert result["source_sha256"] == digest
    assert len(observed) == 2


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
