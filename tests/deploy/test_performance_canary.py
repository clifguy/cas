"""The embedding canary reaches real entry points without service dependencies."""

import copy
from datetime import UTC, datetime, timedelta

import pytest

from deploy import performance_bootstrap as bootstrap
from deploy import performance_trial as trial
from tests.deploy.test_performance_trial import plan


def canary():
    p = plan()
    now = datetime.now(UTC)
    p.update(
        phase="embedding_canary",
        created_at=now.isoformat(),
        expires_at=(now + timedelta(hours=1)).isoformat(),
    )
    p["limits"].update(window_seconds=3600, incremental_usd=5)
    p["candidate"].update(
        commit="eb994495b66837cca23cc891487f83ad434c0c4f",
        image="acrwofzhg37aohom.azurecr.io/sage@sha256:feaa6269b1244467b7d03d7bfa371209fa7c88e73569f36a2b8ec469291cec2c",
    )
    p["source"] = {"resource_group": "rg-prod"}
    p["binding"] = {k: v for k, v in p["binding"].items() if k in ("registry_id", "tenant_id")}
    p["binding"]["registry_id"] = p["binding"]["registry_id"].replace(
        "registries/registry", "registries/acrwofzhg37aohom"
    )
    p["arms"] = [{"profile": "Consumption", "cpu": 2, "memory": "4Gi"}]
    return p


def test_canary_needs_only_image_model_evidence_and_expiry():
    p = trial.validate_plan(canary())
    parameters = trial.manifest_parameters(p, "operator", "unused")
    values = {k: v["value"] for k, v in parameters["parameters"].items()}
    assert values["embeddingOnly"] is True
    assert (
        not {"keyVaultName", "siteId", "driveId", "abstractionModel", "ingressKey"} & values.keys()
    )
    definitions = bootstrap.role_definitions(p)
    assert len(definitions) == 1
    assert set(definitions[0]["Actions"]) == set(
        bootstrap.DEPLOY_ACTIONS
        + [
            "Microsoft.App/locations/usages/read",
            "Microsoft.Authorization/roleAssignments/read",
            "Microsoft.Authorization/roleDefinitions/read",
            "Microsoft.Resources/subscriptions/read",
            "Microsoft.Resources/subscriptions/resourceGroups/read",
        ]
    )
    assignments = bootstrap.expected_assignments(p, "22222222-2222-2222-2222-222222222222")
    assert all("KeyVault" not in a["scope"] for a in assignments)
    assert len(assignments) == 9


@pytest.mark.parametrize(
    "key,value", [("phase", "unknown"), ("arms", [{"profile": "D8", "cpu": 2, "memory": "8Gi"}])]
)
def test_canary_refuses_unknown_phase_and_dedicated_capacity(key, value):
    p = canary()
    p[key] = value
    with pytest.raises(
        ValueError, match="unknown experiment phase" if key == "phase" else "fixed-memory"
    ):
        trial.validate_plan(p)


def test_canary_refuses_baseline_replacement():
    p = canary()
    p["candidate"]["commit"] = "a" * 40
    with pytest.raises(ValueError, match="baseline"):
        trial.validate_plan(p)


def test_canary_preflight_never_requests_graph_or_production_secrets(monkeypatch):
    p = trial.validate_plan(canary())
    calls = []

    def az(*args):
        calls.append(args)
        if args == ("account", "show"):
            return {"id": p["subscription_id"], "tenantId": p["binding"]["tenant_id"]}
        if args[:3] == ("acr", "manifest", "show-metadata"):
            return {"digest": p["candidate"]["image"].split("@")[1]}
        raise AssertionError(args)

    monkeypatch.setattr(trial, "baseline_ci", lambda p: "exact-ci")
    assert trial.preflight(p, az, object())["candidate_ci"] == "exact-ci"
    assert len(calls) == 2


class ExperimentCloud:
    """Remote Azure transport substitute; controller/commands execute unchanged."""

    def __init__(self, p, output):
        import json

        from tests.deploy.test_performance_bootstrap import Cloud

        self.base = Cloud(output)
        self.plan, self.output = p, output
        self.rows, self.blobs, self.executions = {}, {}, []
        self.effects = []
        self.fail = None
        self.job = None
        self.json = json
        self.runtime_grant = {
            "id": p["binding"]["registry_id"]
            + "/providers/Microsoft.Authorization/roleAssignments/"
            + "77777777-7777-7777-7777-777777777777",
            "scope": p["binding"]["registry_id"],
            "principalId": "33333333-3333-3333-3333-333333333333",
            "principalType": "ServicePrincipal",
            "roleDefinitionId": "/subscriptions/"
            + p["subscription_id"]
            + "/providers/Microsoft.Authorization/roleDefinitions/"
            + trial.ROLE_IDS["acr"],
            "condition": None,
            "conditionVersion": None,
        }
        self.outputs = {
            "automationName": "aa-perf-" + p["run_id"],
            "exportUrl": "https://stperf1234567890123.blob.core.windows.net/evidence/"
            + p["run_id"],
            "servicePrincipalId": "33333333-3333-3333-3333-333333333333",
            "expiryPrincipalId": "44444444-4444-4444-4444-444444444444",
            "serviceClientId": "55555555-5555-5555-5555-555555555555",
            "sourceRoleIds": [self.runtime_grant["id"]],
            "stackOutputs": {},
        }

    def __call__(self, *args, **kw):
        import json

        if args[:3] == ("acr", "manifest", "show-metadata"):
            return {"digest": self.plan["candidate"]["image"].split("@")[1]}
        if args[:1] == ("rest",):
            if "usages?" in args[args.index("--url") + 1]:
                return {
                    "value": [
                        {
                            "name": {"value": "ManagedEnvironmentCount"},
                            "currentValue": 1,
                            "limit": 10,
                        }
                    ]
                }
        if args[:2] == ("deployment", "sub"):
            params = json.loads(
                __import__("pathlib").Path(args[args.index("--parameters") + 1][1:]).read_text()
            )["parameters"]
            access = params["configureAccess"]["value"]
            stack = params["deployStack"]["value"]
            phase = "stack" if stack else "access" if access else "foundation"
            self.effects.append((phase, args[2]))
            if self.fail == phase + "-" + args[2]:
                raise RuntimeError(self.fail)
            if args[2] == "what-if":
                row = self.base.groups[self.plan["resource_group"]]
                return {
                    "status": "Succeeded",
                    "changes": [{"changeType": "Modify", "resourceId": row["id"], "after": row}],
                }
            if args[2] == "validate":
                return {}
            if access and self.runtime_grant not in self.base.assignments:
                self.base.assignments.append(copy.deepcopy(self.runtime_grant))
            if access and self.fail == "access-after-create":
                raise RuntimeError("uncertain access create after actual effect")
            if stack:
                run = self.plan["run_id"]
                name = "bench-" + run
                rid = (
                    self.base.groups[self.plan["resource_group"]]["id"]
                    + "/providers/Microsoft.App/jobs/"
                    + name
                )
                self.job = {
                    "id": rid,
                    "name": name,
                    "tags": {"benchmarkRun": run},
                    "properties": {
                        "provisioningState": "Succeeded",
                        "configuration": {"replicaTimeout": 30, "replicaRetryLimit": 0},
                        "template": {
                            "containers": [
                                {
                                    "name": "benchmark",
                                    "image": self.plan["candidate"]["image"],
                                    "resources": {"cpu": 2, "memory": "4Gi"},
                                    "env": [
                                        {
                                            "name": "PERF_BENCH_CASE",
                                            "value": json.dumps(
                                                {
                                                    "shape": "short",
                                                    "batch": 1,
                                                    "threads": 2,
                                                    "mode": "direct",
                                                }
                                            ),
                                        }
                                    ],
                                }
                            ]
                        },
                    },
                }
                self.outputs["stackOutputs"] = {
                    "benchmarkJobName": name,
                    "environmentId": "environment",
                }
                self.base.resources[self.plan["resource_group"]] = [
                    {
                        "id": rid,
                        "type": "Microsoft.App/jobs",
                        "tags": {"benchmarkRun": run, "expiresAt": self.plan["expires_at"]},
                    }
                ]
            return {"properties": {"outputs": {k: {"value": v} for k, v in self.outputs.items()}}}
        if args[:3] == ("containerapp", "job", "show"):
            return copy.deepcopy(self.job)
        if args[:4] == ("containerapp", "job", "execution", "list"):
            return copy.deepcopy(self.executions)
        if args[:4] == ("containerapp", "job", "execution", "show"):
            if self.fail == "workload":
                raise RuntimeError("workload")
            return {"properties": {"status": "Succeeded"}}
        if args[:2] == ("identity", "show"):
            return {"principalId": self.outputs["servicePrincipalId"]}
        if args[:2] == ("group", "delete"):
            name = args[args.index("-n") + 1]
            if self.fail == "cleanup":
                raise TimeoutError("cleanup")
            scope = self.base.groups[name]["id"]
            self.base.assignments = [
                r for r in self.base.assignments if not r["scope"].startswith(scope)
            ]
            self.base.groups.pop(name, None)
            self.base.resources.pop(name, None)
            return None
        if args[:2] == ("group", "exists") and args[args.index("-n") + 1] == "rg-ingest-perf-prior":
            return False
        return self.base(*args)

    def token(self, scope):
        import base64
        import json

        assert scope == "https://management.azure.com/.default"
        return (
            "x."
            + base64.urlsafe_b64encode(
                json.dumps({"oid": "22222222-2222-2222-2222-222222222222"}).encode()
            ).decode()
            + ".x"
        )

    def arm(self, method, path, *, body=None, raw=None, api=None):
        import json

        import httpx

        def response(value):
            return httpx.Response(200, json=value)

        if "/roleAssignments/" in path:
            found = next((r for r in self.base.assignments if r["id"] == path), None)
            if found is None:
                raise trial.RemoteError(404)
            if method == "DELETE":
                if found["principalId"] != self.outputs["servicePrincipalId"]:
                    assert (self.output / "operator-cleanup-intent.json").exists()
                self.base.assignments.remove(found)
                return response({})
            return response({"id": path, "properties": found})
        if "/roleDefinitions/" in path and method == "GET":
            rows = self.base.definitions.get(path.rsplit("/", 1)[1], [])
            if not rows:
                raise trial.RemoteError(404)
            return response(rows[0])
        if "/control-prior/" in path or "rg-ingest-perf-control-prior/" in path:
            proof = self.plan["expiry_proof"]
            if path.endswith("/content"):
                return httpx.Response(
                    200, content=(trial.ROOT / "deploy/performance_cleanup.ps1").read_bytes()
                )
            if path.endswith("/output"):
                return httpx.Response(200, content=b'{"outcome":"deleted"}\n')
            return response(
                {
                    "properties": {
                        "status": "Completed",
                        "runbook": {"name": "expire"},
                        "parameters": {
                            "SubscriptionId": self.plan["subscription_id"],
                            "TrialGroup": "rg-ingest-perf-prior",
                            "RunId": "prior",
                            "ExpiresAt": proof["expires_at"],
                            "Action": "Inspect" if self.fail == "expiry" else "Expire",
                        },
                    }
                }
            )
        if self.fail == "expiry-publish" and method == "POST" and path.endswith("/publish"):
            raise RuntimeError("expiry-publish")
        if self.fail == "expiry-schedule" and method == "PUT" and "/schedules/" in path:
            raise RuntimeError("expiry-schedule")
        if self.job and path == self.job["id"] and method == "PATCH":
            self.job["properties"]["template"] = copy.deepcopy(body["properties"]["template"])
            return response({})
        if path.endswith("/start"):
            env = {
                x["name"]: x.get("value")
                for x in self.job["properties"]["template"]["containers"][0]["env"]
            }
            nonce = env.get("PERF_PROBE_NONCE")
            label = "probe" if nonce else "measurement"
            self.effects.append(("start", label))
            if self.fail == "probe" and nonce:
                raise RuntimeError("probe")
            if nonce:
                self.blobs["probe-" + nonce + ".json"] = json.dumps(
                    {"kind": "evidence_probe", "run_id": self.plan["run_id"], "nonce": nonce}
                ).encode()
            else:
                self.blobs["samples-measurement.jsonl"] = b'{"kind":"benchmark_complete"}\n'
            execution = {
                "name": "execution-" + str(len(self.executions)),
                "id": self.job["id"] + "/executions/execution-" + str(len(self.executions)),
            }
            self.executions.append(execution)
            return response(execution)
        if method in ("PUT", "POST"):
            self.rows[path] = copy.deepcopy(body) if raw is None else raw
            return response({})
        if path.endswith("/draft/content") or path.endswith("/content"):
            return httpx.Response(
                200, content=(trial.ROOT / "deploy/performance_cleanup.ps1").read_bytes()
            )
        if "/jobs/" in path:
            return response({"properties": {"status": "Completed"}})
        if path.endswith("/runbooks/expire"):
            return response({"properties": {"state": "Published"}})
        return response(self.rows[path])

    def blob(self, method, url, *, params=None, **kwargs):
        import httpx

        if self.fail == "read":
            raise PermissionError("evidence denied")
        if params and params.get("comp") == "list":
            names = "".join(
                "<Blob><Name>" + self.plan["run_id"] + "/" + name + "</Name></Blob>"
                for name in self.blobs
            )
            return httpx.Response(
                200,
                content=(
                    "<EnumerationResults><Blobs>" + names + "</Blobs></EnumerationResults>"
                ).encode(),
            )
        return httpx.Response(200, content=self.blobs[url.rsplit("/", 1)[1]])


def command_setup(tmp_path, monkeypatch, *, expiry_only=False):
    import hashlib
    import json
    import sys

    from tests.deploy.test_performance_bootstrap import ENVIRONMENT, PRINCIPAL

    p = canary()
    p["limits"]["per_run_seconds"] = 30
    if expiry_only:
        p["expiry_only"] = True
    else:
        p["expiry_proof"] = {
            "run_id": "prior",
            "expires_at": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
            "job_id": "66666666-6666-6666-6666-666666666666",
        }
    p.pop("created_at")
    p.pop("expires_at")
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(p))
    digest = hashlib.sha256(spec.read_bytes()).hexdigest()
    operator = tmp_path / "operator"
    operator.mkdir()
    cloud = ExperimentCloud(p, operator)
    monkeypatch.setattr(trial, "Azure", lambda: cloud)
    monkeypatch.setattr(trial, "Rest", lambda: cloud)
    monkeypatch.setattr(trial, "baseline_ci", lambda p: "exact-main-ci")
    import httpx

    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: httpx.Response(
            200,
            json={
                "Items": [
                    {"meterName": "Standard vCPU Active Usage", "retailPrice": 0.000024},
                    {"meterName": "Standard Memory Active Usage", "retailPrice": 0.000003},
                ]
            },
            request=httpx.Request("GET", "https://prices.azure.com"),
        ),
    )
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_REPOSITORY", "clifguy/cas")

    def invoke(module, args):
        monkeypatch.setattr(sys, "argv", ["command", *args])
        return module.main()

    # Human bootstrap is a separate phase even when pytest itself runs in CI.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    invoke(
        bootstrap,
        [
            "bootstrap",
            "--spec",
            str(spec),
            "--approved-sha256",
            digest,
            "--environment",
            ENVIRONMENT,
            "--principal",
            PRINCIPAL,
            "--output",
            str(operator),
        ],
    )
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    receipt = operator / "bootstrap-receipt.json"
    cloud.plan = json.loads(receipt.read_text())["plan"]
    rd = hashlib.sha256(receipt.read_bytes()).hexdigest()
    out = tmp_path / "ci"
    invoke(
        bootstrap,
        [
            "handoff",
            "--receipt",
            str(receipt),
            "--approved-bootstrap-sha256",
            rd,
            "--spec",
            str(spec),
            "--approved-sha256",
            digest,
            "--environment",
            ENVIRONMENT,
            "--principal",
            PRINCIPAL,
            "--output",
            str(out),
        ],
    )
    cloud.output = out
    args = ["--spec", str(spec), "--approved-sha256", digest, "--output", str(out)]
    return p, cloud, out, lambda operation, *extra: invoke(trial, [operation, *args, *extra])


def test_real_commands_bootstrap_handoff_run_probe_and_export(tmp_path, monkeypatch):
    import json

    _, cloud, out, invoke = command_setup(tmp_path, monkeypatch)
    assert invoke("run") == 0
    assert [x for x in cloud.effects if x[0] == "start"] == [
        ("start", "probe"),
        ("start", "measurement"),
    ]
    assert json.loads((out / "outcome.json").read_text())["outcome"] == "canary_complete"
    assert json.loads((out / "export-state.json").read_text())["status"] == "complete"
    assert invoke("export") == 0


@pytest.mark.parametrize(
    "failure",
    [
        "foundation-what-if",
        "foundation-create",
        "access-what-if",
        "access-create",
        "access-after-create",
        "expiry-publish",
        "expiry-schedule",
        "stack-what-if",
        "stack-create",
        "probe",
        "read",
        "workload",
    ],
)
def test_real_command_failure_never_claims_completion_or_restarts_work(
    tmp_path, monkeypatch, failure
):
    import json

    _, cloud, out, invoke = command_setup(tmp_path, monkeypatch)
    cloud.fail = failure
    with pytest.raises((RuntimeError, PermissionError)):
        invoke("run")
    assert json.loads((out / "outcome.json").read_text())["outcome"] == "partial_or_not_started"
    if failure in ("probe", "read"):
        assert ("start", "measurement") not in cloud.effects
    assert (out / "runtime-plan.json").exists()


def test_expiry_only_command_arms_independent_cleanup_without_compute(tmp_path, monkeypatch):
    import json

    _, cloud, out, invoke = command_setup(tmp_path, monkeypatch, expiry_only=True)
    assert invoke("run") == 0
    assert ("stack", "create") not in cloud.effects
    assert json.loads((out / "outcome.json").read_text())["outcome"] == "expiry_rehearsal_armed"


def test_real_commands_cleanup_and_grant_absence(tmp_path, monkeypatch):
    import hashlib
    import json

    _, cloud, out, invoke = command_setup(tmp_path, monkeypatch)
    invoke("run")
    digest = hashlib.sha256((out / "export-receipt.json").read_bytes()).hexdigest()
    assert invoke("cleanup", "--export-receipt-sha256", digest) == 0
    assert cloud.base.groups == {}
    assert cloud.base.assignments
    assert (
        json.loads((out / "operator-grants-pending.json").read_text())["access_cleanup_complete"]
        is False
    )
    finish_operator_cleanup(out, monkeypatch)
    assert cloud.base.assignments == []
    assert json.loads((out / "operator-grants-revoked.json").read_text())["absent"]


@pytest.mark.parametrize("failure", ["foundation-create", "access-create", "access-after-create"])
def test_empty_foundation_export_and_cleanup_are_authenticated(tmp_path, monkeypatch, failure):
    import hashlib
    import json

    _, cloud, out, invoke = command_setup(tmp_path, monkeypatch)
    cloud.fail = failure
    with pytest.raises(RuntimeError):
        invoke("run")
    cloud.fail = None
    assert invoke("export") == 0
    assert json.loads((out / "export-receipt.json").read_text())["foundation_only"]
    digest = hashlib.sha256((out / "export-receipt.json").read_bytes()).hexdigest()
    assert invoke("cleanup", "--export-receipt-sha256", digest) == 0
    assert cloud.base.groups == {}


def test_export_failure_invalidates_old_proof_and_interrupted_cleanup_resumes(
    tmp_path, monkeypatch
):
    import hashlib
    import json

    _, cloud, out, invoke = command_setup(tmp_path, monkeypatch)
    invoke("run")
    digest = hashlib.sha256((out / "export-receipt.json").read_bytes()).hexdigest()
    cloud.fail = "read"
    with pytest.raises(PermissionError):
        invoke("export")
    with pytest.raises(ValueError):
        invoke("cleanup", "--export-receipt-sha256", digest)
    assert cloud.base.groups
    cloud.fail = None
    invoke("export")
    digest = hashlib.sha256((out / "export-receipt.json").read_bytes()).hexdigest()
    cloud.fail = "cleanup"
    with pytest.raises(TimeoutError):
        invoke("cleanup", "--export-receipt-sha256", digest)
    cloud.fail = None
    assert invoke("cleanup", "--export-receipt-sha256", digest) == 0
    finish_operator_cleanup(out, monkeypatch)
    assert json.loads((out / "operator-grants-revoked.json").read_text())["absent"]


def test_role_preview_binds_real_concrete_principal_and_rejects_failed_arm_shapes(
    tmp_path, monkeypatch
):
    import json

    from deploy.performance_preview import inventory

    p = trial.validate_plan(canary())
    principal = "22222222-2222-2222-2222-222222222222"
    params = trial.manifest_parameters(p, principal, "", access=True)
    identities = {
        "servicePrincipalId": "33333333-3333-3333-3333-333333333333",
        "expiryPrincipalId": "44444444-4444-4444-4444-444444444444",
        "exportUrl": "https://stperf1234567890123.blob.core.windows.net/evidence/" + p["run_id"],
    }
    rid = (
        p["binding"]["registry_id"]
        + "/providers/Microsoft.Authorization/roleAssignments/55555555-5555-5555-5555-555555555555"
    )
    result = {
        "status": "Succeeded",
        "changes": [
            {
                "changeType": "Create",
                "resourceId": rid,
                "after": {
                    "properties": {
                        "principalId": identities["servicePrincipalId"],
                        "roleDefinitionId": "/providers/Microsoft.Authorization/roleDefinitions/"
                        + trial.ROLE_IDS["acr"],
                    }
                },
            }
        ],
    }
    calls = []

    def az(*args, **kw):
        calls.append(args)
        actual = json.loads(
            __import__("pathlib").Path(args[args.index("--parameters") + 1][1:]).read_text()
        )
        assert (
            actual["parameters"]["accessPrincipals"]["value"]["servicePrincipalId"]
            == identities["servicePrincipalId"]
        )
        return result if args[2] == "what-if" else {}

    trial.apply_template(
        p,
        az,
        params,
        trial.ROOT / "infra/performance.bicep",
        preview=True,
        output=tmp_path,
        identities=identities,
    )
    trial.apply_template(
        p,
        az,
        params,
        trial.ROOT / "infra/performance.bicep",
        output=tmp_path,
        identities=identities,
    )
    assert [x[2] for x in calls] == ["validate", "what-if", "create"]
    for mutation in ["unsupported", "unresolved", "foreign", "delete"]:
        changed = copy.deepcopy(result)
        row = changed["changes"][0]
        if mutation == "unsupported":
            row["changeType"] = "Unsupported"
        elif mutation == "unresolved":
            row["after"]["properties"]["principalId"] = (
                "[reference(resourceId('Microsoft.ManagedIdentity/userAssignedIdentities','id')).principalId]"
            )
        elif mutation == "foreign":
            row["resourceId"] = row["resourceId"].replace("rg-prod", "foreign")
        else:
            row["changeType"] = "Delete"
        with pytest.raises(ValueError):
            inventory(p, changed, params, identities)
    identities["servicePrincipalId"] = "[reference(identity).principalId]"
    with pytest.raises(ValueError, match="concrete"):
        trial.apply_template(
            p,
            az,
            params,
            trial.ROOT / "infra/performance.bicep",
            preview=True,
            output=tmp_path,
            identities=identities,
        )


def test_independent_inspection_cannot_substitute_for_expiry_deletion(tmp_path, monkeypatch):
    _, cloud, _, invoke = command_setup(tmp_path, monkeypatch)
    cloud.fail = "expiry"
    with pytest.raises(ValueError, match="independent expiry"):
        invoke("run")
    assert ("stack", "create") not in cloud.effects


def test_real_archive_commands_encrypt_decrypt_and_read_back(tmp_path, monkeypatch):
    import hashlib
    import json
    import sys

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from deploy import performance_archive as archive

    _, _, out, invoke = command_setup(tmp_path, monkeypatch)
    invoke("run")
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    private = tmp_path / "private.pem"
    private.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    spec = tmp_path / "recipient.json"
    spec.write_text(
        json.dumps(
            {
                "evidence_public_key_pem": key.public_key()
                .public_bytes(
                    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
                )
                .decode()
            }
        )
    )
    encrypted = tmp_path / "sealed" / "evidence.enc"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "archive",
            "seal",
            "--source",
            str(out),
            "--output",
            str(encrypted),
            "--spec",
            str(spec),
            "--approved-sha256",
            hashlib.sha256(spec.read_bytes()).hexdigest(),
        ],
    )
    assert archive.main() == 0
    restored = tmp_path / "restored"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "archive",
            "decrypt",
            "--source",
            str(encrypted),
            "--output",
            str(restored),
            "--private-key",
            str(private),
        ],
    )
    assert archive.main() == 0
    expected = {str(p.relative_to(out)): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    assert {
        str(p.relative_to(restored)): p.read_bytes() for p in restored.rglob("*") if p.is_file()
    } == expected


def finish_operator_cleanup(out, monkeypatch):
    import hashlib
    import sys

    from tests.deploy.test_performance_bootstrap import ENVIRONMENT, PRINCIPAL

    spec = out.parent / "spec.json"
    receipt = out / "bootstrap-receipt.json"
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bootstrap",
            "cleanup",
            "--receipt",
            str(receipt),
            "--approved-bootstrap-sha256",
            hashlib.sha256(receipt.read_bytes()).hexdigest(),
            "--spec",
            str(spec),
            "--approved-sha256",
            hashlib.sha256(spec.read_bytes()).hexdigest(),
            "--environment",
            ENVIRONMENT,
            "--principal",
            PRINCIPAL,
            "--output",
            str(out),
        ],
    )
    bootstrap.main()


def test_operator_cleanup_checks_receipt_subscription_with_foreign_cli_default(
    tmp_path, monkeypatch
):
    import hashlib

    _, cloud, out, invoke = command_setup(tmp_path, monkeypatch)
    invoke("run")
    before = copy.deepcopy(cloud.base.assignments)
    target = cloud.plan["subscription_id"]
    calls = []

    def foreign_default(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("account", "show"):
            return {"id": "99999999-9999-9999-9999-999999999999", "tenantId": "tenant"}
        if args[0] in ("group", "resource", "identity"):
            scoped = "--subscription" in args and args[args.index("--subscription") + 1] == target
            if not scoped:
                if args[:2] == ("group", "exists"):
                    return False  # An authenticated empty foreign subscription.
                raise AssertionError("unbound group/resource command")
        return cloud(*args, **kwargs)

    monkeypatch.setattr(trial, "Azure", lambda: foreign_default)
    with pytest.raises(ValueError, match="resource cleanup"):
        finish_operator_cleanup(out, monkeypatch)
    assert cloud.base.assignments == before
    assert not (out / "operator-grants-revoked.json").exists()
    invoke("export")
    digest = hashlib.sha256((out / "export-receipt.json").read_bytes()).hexdigest()
    invoke("cleanup", "--export-receipt-sha256", digest)
    assert cloud.base.groups == {}
    finish_operator_cleanup(out, monkeypatch)
    assert (out / "operator-grants-revoked.json").exists()
    assert all("--subscription" in a for a in calls if a[0] in ("group", "resource", "identity"))


def test_exact_canary_grants_allow_required_subscription_role_readbacks(tmp_path, monkeypatch):
    _, cloud, out, invoke = command_setup(tmp_path, monkeypatch)
    from tests.deploy.test_performance_bootstrap import PRINCIPAL

    actions = {
        action
        for binding in bootstrap.expected_assignments(cloud.plan, PRINCIPAL)
        if binding["scope"] == "/subscriptions/" + cloud.plan["subscription_id"]
        for definition in bootstrap.role_definitions(cloud.plan)
        if binding["roleDefinitionId"].endswith("/" + definition["Id"])
        for action in definition["Actions"]
    }
    observed = []

    def permission_checked(*args, **kwargs):
        if args[:2] == ("role", "assignment") and args[2] == "list":
            required = "Microsoft.Authorization/roleAssignments/read"
        elif args[:3] == ("role", "definition", "list"):
            required = "Microsoft.Authorization/roleDefinitions/read"
        else:
            return cloud(*args, **kwargs)
        if required not in actions:
            raise PermissionError("exact subscription role lacks " + required)
        assert args[args.index("--subscription") + 1] == cloud.plan["subscription_id"]
        observed.append(required)
        return cloud(*args, **kwargs)

    monkeypatch.setattr(trial, "Azure", lambda: permission_checked)
    assert invoke("run") == 0
    assert set(observed) == {
        "Microsoft.Authorization/roleAssignments/read",
        "Microsoft.Authorization/roleDefinitions/read",
    }


def test_rehearsal_absence_is_read_in_plan_subscription(tmp_path, monkeypatch):
    _, cloud, _, _ = command_setup(tmp_path, monkeypatch)
    target = cloud.plan["subscription_id"]

    def foreign_default(*args, **kwargs):
        if args[:2] == ("group", "exists"):
            # The target still contains the rehearsal group; foreign default is empty.
            return "--subscription" in args and args[args.index("--subscription") + 1] == target
        return cloud(*args, **kwargs)

    with pytest.raises(ValueError, match="rehearsal group remains"):
        trial.verify_expiry_rehearsal(cloud.plan, foreign_default, cloud)


@pytest.mark.parametrize("fault", ["lost-response", "invalid-execution", "accepted-read-denied"])
def test_uncertain_start_retains_intent_and_never_replays_measurement(tmp_path, monkeypatch, fault):
    import json

    import httpx

    _, cloud, out, invoke = command_setup(tmp_path, monkeypatch)
    original = cloud.arm
    original_plan = (out / "runtime-plan.json").read_bytes()
    failed = False

    def uncertain(method, path, **kwargs):
        nonlocal failed
        result = original(method, path, **kwargs)
        if path.endswith("/start") and cloud.effects[-1] == ("start", "measurement") and not failed:
            failed = True
            if fault == "lost-response":
                raise TimeoutError("accepted start response lost")
            if fault == "invalid-execution":
                return httpx.Response(200, json={"id": "foreign", "name": "execution"})
            return httpx.Response(202)
        return result

    original_cli = cloud.__call__

    def cli(*args, **kwargs):
        if (
            fault == "accepted-read-denied"
            and failed
            and args[:4] == ("containerapp", "job", "execution", "list")
        ):
            raise PermissionError("accepted execution read denied")
        return original_cli(*args, **kwargs)

    monkeypatch.setattr(cloud, "arm", uncertain)
    monkeypatch.setattr(trial, "Azure", lambda: cli)
    with pytest.raises((TimeoutError, ValueError, PermissionError)):
        invoke("run")
    receipt = json.loads((out / "benchmark-000-dispatch.json").read_text())
    assert receipt["outcome"] == "dispatch_intent"
    assert receipt["plan_sha256"] == trial.plan_digest(cloud.plan)
    assert len([x for x in cloud.effects if x == ("start", "measurement")]) == 1
    assert json.loads((out / "outcome.json").read_text())["outcome"] == "partial_or_not_started"
    starts = [x for x in cloud.effects if x[0] == "start"]
    with pytest.raises((ValueError, PermissionError), match="reconcile|denied"):
        invoke("run")
    assert [x for x in cloud.effects if x[0] == "start"] == starts
    assert (out / "runtime-plan.json").read_bytes() == original_plan
    assert len([x for x in cloud.effects if x == ("start", "measurement")]) == 1
    invoke("export")
    assert not json.loads((out / "export-receipt.json").read_text()).get("foundation_only")


def test_started_canary_cannot_reprovision_or_downgrade_export_receipt(tmp_path, monkeypatch):
    import json

    _, cloud, out, invoke = command_setup(tmp_path, monkeypatch)
    original = cloud.arm

    def lost_response(method, path, **kwargs):
        result = original(method, path, **kwargs)
        if path.endswith("/start") and cloud.effects[-1] == ("start", "measurement"):
            raise TimeoutError("accepted start response lost")
        return result

    monkeypatch.setattr(cloud, "arm", lost_response)
    with pytest.raises(TimeoutError):
        invoke("run")
    receipt = (out / "provision-receipt.json").read_bytes()
    clock = (out / "runtime-plan.json").read_bytes()
    effects = list(cloud.effects)
    with pytest.raises(ValueError, match="reconcile"):
        invoke("run")
    assert cloud.effects == effects
    assert (out / "provision-receipt.json").read_bytes() == receipt
    assert (out / "runtime-plan.json").read_bytes() == clock
    assert invoke("export") == 0
    assert not json.loads((out / "export-receipt.json").read_text()).get("foundation_only")


@pytest.mark.parametrize(
    "fault",
    [
        "runtime-json",
        "spec-digest",
        "provision-json",
        "provision-binding",
        "phase",
        "legacy-downgrade",
    ],
)
def test_failed_export_entry_revokes_stale_cleanup_proof(tmp_path, monkeypatch, fault):
    import hashlib
    import json

    _, cloud, out, invoke = command_setup(tmp_path, monkeypatch)
    invoke("run")
    old_digest = hashlib.sha256((out / "export-receipt.json").read_bytes()).hexdigest()
    cloud.blobs["late-measurement.jsonl"] = b'{"partial":"new durable evidence"}\n'
    target = out / ("runtime-plan.json" if fault == "runtime-json" else "provision-receipt.json")
    original = target.read_bytes()
    spec = out.parent / "spec.json"
    spec_original = spec.read_bytes()
    if fault.endswith("json"):
        target.write_text("invalid JSON")
    elif fault == "spec-digest":
        spec.write_bytes(spec_original + b"\n")
    else:
        receipt = json.loads(original)
        if fault == "provision-binding":
            receipt["plan_sha256"] = "f" * 64
        else:
            receipt["phase"] = "controller_created" if fault == "legacy-downgrade" else "unknown"
        target.write_text(json.dumps(receipt))
    export = (
        (lambda: trial.export_checkpoint(cloud.plan, cloud, cloud, out))
        if fault in ("provision-json", "provision-binding", "phase")
        else (lambda: invoke("export"))
    )
    if fault == "legacy-downgrade":
        assert invoke("export") == 0
        assert not json.loads((out / "export-receipt.json").read_text()).get("foundation_only")
    else:
        with pytest.raises(ValueError):
            export()
        assert json.loads((out / "export-state.json").read_text())["status"] != "complete"
        assert not (out / "export-receipt.json").exists()
    target.write_bytes(original)
    spec.write_bytes(spec_original)
    groups = copy.deepcopy(cloud.base.groups)
    with pytest.raises(ValueError):
        invoke("cleanup", "--export-receipt-sha256", old_digest)
    assert cloud.base.groups == groups
    assert invoke("export") == 0
    assert (out / "cloud-evidence" / cloud.plan["run_id"] / "late-measurement.jsonl").is_file()
    digest = hashlib.sha256((out / "export-receipt.json").read_bytes()).hexdigest()
    assert invoke("cleanup", "--export-receipt-sha256", digest) == 0
