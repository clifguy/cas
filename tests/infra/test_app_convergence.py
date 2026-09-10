"""Execute the shipped convergence step against a stateful Azure CLI boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def initial_state() -> dict:
    apps = {}
    outputs = {
        "acrLoginServer": "registry.example",
        "postgresServerFqdn": "target.postgres.database.azure.com",
    }
    for kind, container, mount in [("sage", "sage", "/etc/sage"), ("bff", "cas-bff", "/etc/cas")]:
        name = f"app-{kind}"
        revision = f"{name}--approved"
        config = "postgres:\n  host: target.postgres.database.azure.com"
        template = {
            "containers": [
                {
                    "name": container,
                    "image": f"registry.example/{kind}:approved-image",
                    "env": [{"name": "SAGE_CONFIG_PATH", "value": f"{mount}/config.cloud.yaml"}],
                    "volumeMounts": [{"volumeName": "config", "mountPath": mount}],
                }
            ],
            "volumes": [
                {
                    "name": "config",
                    "storageType": "Secret",
                    "secrets": [{"secretRef": f"{kind}-cloud-config", "path": "config.cloud.yaml"}],
                }
            ],
        }
        apps[name] = {
            "properties": {
                "latestRevisionName": revision,
                "configuration": {"activeRevisionsMode": "Single"},
            },
            "secrets": [{"name": f"{kind}-cloud-config", "value": config}],
            "revisions": [
                {"name": revision, "properties": {"active": False, "template": template}}
            ],
        }
        outputs.update(
            {
                f"{kind}ContainerAppName": name,
                f"{kind}ContainerAppRevision": revision,
                f"{kind}CloudConfigHash": str(
                    uuid.uuid5(uuid.UUID("11fb06fb-712d-4ddd-98c7-e71bbd588830"), config)
                ),
            }
        )
    return {
        "deployment": {
            "properties": {
                "provisioningState": "Succeeded",
                "parameters": {
                    k: {"value": v}
                    for k, v in {
                        "postgresGeneration": "pg17",
                        "imageTag": "approved-image",
                        "resourceGroupName": "group",
                    }.items()
                },
                "outputs": {k: {"value": v} for k, v in outputs.items()},
            }
        },
        "fence": "cutover:pg17",
        "apps": apps,
    }


def fake_azure() -> None:
    path = Path(os.environ["AZURE_STATE"])
    state = json.loads(path.read_text())
    args = sys.argv[1:]
    with Path(os.environ["AZURE_CALLS"]).open("a") as stream:
        stream.write(json.dumps(args) + "\n")

    def arg(name: str) -> str:
        return args[args.index(name) + 1]

    if state.get("read_failure") and args[:3] == ["containerapp", "revision", "list"]:
        sys.exit(7)
    if args[:3] == ["deployment", "sub", "show"]:
        result = state["deployment"]
        if "--query" in args:
            result = result["properties"]["outputs"][arg("--query").split(".")[2]]["value"]
    elif args[:2] == ["group", "show"]:
        result = {"tags": {"casPostgresMigration": state["fence"]}}
    elif args[:2] == ["containerapp", "show"]:
        result = {"name": arg("--name"), "properties": state["apps"][arg("--name")]["properties"]}
    elif args[:3] == ["containerapp", "secret", "list"]:
        result = state["apps"][arg("--name")]["secrets"]
        if "--show-values" not in args:
            result = [{"name": s["name"]} for s in result]
    elif args[:3] == ["containerapp", "revision", "list"]:
        result = state["apps"][arg("--name")]["revisions"]
        if "--query" in args:
            result = next((r["name"] for r in result if r["properties"]["active"]), "")
    elif args[:3] in (
        ["containerapp", "revision", "restart"],
        ["containerapp", "revision", "activate"],
    ):
        revisions = state["apps"][arg("--name")]["revisions"]
        revision = next((r for r in revisions if r["name"] == arg("--revision")), None)
        if revision is None or (args[2] == "restart" and not revision["properties"]["active"]):
            print("revision does not exist or is inactive", file=sys.stderr)
            sys.exit(8)
        if state.get("fail_app") == arg("--name"):
            sys.exit(9)
        if not state.get("no_effect"):
            revision["properties"]["active"] = True
        result = {}
        path.write_text(json.dumps(state))
    else:
        raise AssertionError(args)
    print(result if isinstance(result, str) else json.dumps(result))


def run_step(
    tmp_path: Path, state: dict, generation: str = "pg17"
) -> tuple[subprocess.CompletedProcess, list[list[str]], dict]:
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state))
    calls_path = tmp_path / "calls.jsonl"
    calls_path.write_text("")
    az = tmp_path / "az"
    az.write_text(
        f"#!{sys.executable}\nimport runpy\n"
        f'runpy.run_path({str(Path(__file__).resolve())!r}, run_name="__main__")\n'
    )
    az.chmod(0o755)
    python = tmp_path / "python3"
    if not python.exists():
        python.symlink_to(sys.executable)
    workflow = yaml.safe_load((ROOT / ".github/workflows/infra.yml").read_text())
    step = next(
        s for s in workflow["jobs"]["deploy"]["steps"] if s.get("name") == "Converge the app tier"
    )
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "AZURE_STATE": str(state_path),
            "AZURE_CALLS": str(calls_path),
            "ENVIRONMENT_NAME": "prod",
            "RESOURCE_GROUP_NAME": "group",
            "POSTGRES_GENERATION": generation,
            "IMAGE_TAG": "approved-image",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    return (
        result,
        [json.loads(line) for line in calls_path.read_text().splitlines()],
        json.loads(state_path.read_text()),
    )


def mutations(calls: list[list[str]]) -> list[tuple[str, str, str]]:
    return [
        (c[2], c[c.index("--name") + 1], c[c.index("--revision") + 1])
        for c in calls
        if c[:3]
        in (["containerapp", "revision", "activate"], ["containerapp", "revision", "restart"])
    ]


@pytest.mark.parametrize("active", [(), ("sage",), ("sage", "bff")])
def test_converges_exact_approved_revisions(tmp_path: Path, active: tuple[str, ...]) -> None:
    state = initial_state()
    for kind in active:
        state["apps"][f"app-{kind}"]["revisions"][0]["properties"]["active"] = True
    result, calls, after = run_step(tmp_path, state)
    assert result.returncode == 0, result.stderr
    assert mutations(calls) == [
        ("restart" if k in active else "activate", f"app-{k}", f"app-{k}--approved")
        for k in ("sage", "bff")
    ]
    assert all(a["revisions"][0]["properties"]["active"] for a in after["apps"].values())
    first_write = next(
        i
        for i, c in enumerate(calls)
        if c[:3]
        in (["containerapp", "revision", "activate"], ["containerapp", "revision", "restart"])
    )
    assert {
        c[c.index("--name") + 1]
        for c in calls[:first_write]
        if c[:3] == ["containerapp", "secret", "list"]
    } == {"app-sage", "app-bff"}


@pytest.mark.parametrize(
    "defect",
    [
        "deployment",
        "generation",
        "group",
        "image_parameter",
        "fence",
        "missing_revision",
        "missing_hash",
        "config",
        "latest",
        "image",
        "mount",
        "multiple",
        "foreign_active",
        "read_failure",
        "missing_fqdn",
        "no_candidate",
        "unknown_active",
        "mode",
        "secret_unavailable",
    ],
)
def test_invalid_evidence_refuses_both_apps(tmp_path: Path, defect: str) -> None:
    state = initial_state()
    props = state["deployment"]["properties"]
    app = state["apps"]["app-bff"]
    revision = app["revisions"][0]
    if defect == "deployment":
        props["provisioningState"] = "Failed"
    elif defect in ("generation", "group", "image_parameter"):
        key = {
            "generation": "postgresGeneration",
            "group": "resourceGroupName",
            "image_parameter": "imageTag",
        }[defect]
        props["parameters"][key]["value"] = "wrong"
    elif defect == "fence":
        state["fence"] = "copying:pg17"
    elif defect == "missing_revision":
        del props["outputs"]["bffContainerAppRevision"]
    elif defect == "missing_hash":
        del props["outputs"]["bffCloudConfigHash"]
    elif defect == "config":
        app["secrets"][0]["value"] = "postgres:\n  host: source.example"
    elif defect == "latest":
        app["properties"]["latestRevisionName"] = "app-bff--other"
    elif defect == "image":
        revision["properties"]["template"]["containers"][0]["image"] = "registry.example/bff:old"
    elif defect == "mount":
        revision["properties"]["template"]["volumes"][0]["secrets"][0]["secretRef"] = "old-config"
    elif defect in ("multiple", "foreign_active"):
        revision["properties"]["active"] = defect == "multiple"
        app["revisions"].append({"name": "app-bff--other", "properties": {"active": True}})
    elif defect == "read_failure":
        state["read_failure"] = True
    elif defect == "missing_fqdn":
        del props["outputs"]["postgresServerFqdn"]
    elif defect == "no_candidate":
        app["revisions"] = []
    elif defect == "unknown_active":
        revision["properties"]["active"] = None
    elif defect == "mode":
        app["properties"]["configuration"]["activeRevisionsMode"] = "Multiple"
    elif defect == "secret_unavailable":
        del app["secrets"][0]["value"]
    result, calls, _ = run_step(tmp_path, state)
    assert result.returncode != 0
    assert not mutations(calls)
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("failure", ["fail_app", "no_effect"])
def test_failed_activation_stops_convergence(tmp_path: Path, failure: str) -> None:
    state = initial_state()
    state[failure] = "app-sage" if failure == "fail_app" else True
    result, calls, _ = run_step(tmp_path, state)
    assert result.returncode != 0
    assert mutations(calls) == [("activate", "app-sage", "app-sage--approved")]


def test_retry_after_partial_activation(tmp_path: Path) -> None:
    state = initial_state()
    state["fail_app"] = "app-bff"
    result, _, after = run_step(tmp_path, state)
    assert result.returncode != 0
    assert after["apps"]["app-sage"]["revisions"][0]["properties"]["active"]
    del after["fail_app"]
    result, calls, _ = run_step(tmp_path, after)
    assert result.returncode == 0, result.stderr
    assert mutations(calls) == [
        ("restart", "app-sage", "app-sage--approved"),
        ("activate", "app-bff", "app-bff--approved"),
    ]


@pytest.mark.parametrize("generation,fence", [("", ""), ("pg17", "serving:pg17")])
def test_normal_deployment_fence(tmp_path: Path, generation: str, fence: str) -> None:
    state = initial_state()
    state["deployment"]["properties"]["parameters"]["postgresGeneration"]["value"] = generation
    state["fence"] = fence
    result, calls, _ = run_step(tmp_path, state, generation)
    assert result.returncode == 0, result.stderr
    assert (
        f"Approved generation: {generation or 'baseline'}; fence: {fence or 'none'}"
        in result.stdout
    )
    assert mutations(calls) == [
        ("activate", "app-sage", "app-sage--approved"),
        ("activate", "app-bff", "app-bff--approved"),
    ]


def test_deployment_approval_contract() -> None:
    module = (ROOT / "infra/modules/container-apps.bicep").read_text()
    main = (ROOT / "infra/main.bicep").read_text()
    for kind in ("sage", "bff"):
        assert f"output {kind}CloudConfigHash string = guid({kind}ConfigYaml)" in module
        assert f"value: {kind}ConfigYaml" in module
        assert (
            f"output {kind}ContainerAppRevision string = {kind}App.properties.latestRevisionName"
            in module
        )
        for suffix in ("CloudConfigHash", "ContainerAppRevision"):
            assert f"output {kind}{suffix} string = containerApps.outputs.{kind}{suffix}" in main
    # The exact generated config being fingerprinted carries the shared serving FQDN.
    assert module.count("'  host: ${postgresServerFqdn}'") == 2
    assert "postgresServerFqdn: postgres.outputs.postgresServerFqdn" in main


def test_convergence_failure_cannot_release_fence() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/infra.yml").read_text())
    steps = workflow["jobs"]["deploy"]["steps"]
    names = [s.get("name") for s in steps]
    convergence = names.index("Converge the app tier")
    preflight = names.index("Post-deploy preflight gate")
    release = names.index("Release verified migration fence after preflight")
    assert convergence < preflight < release
    for i in (convergence, preflight, release):
        assert steps[i].get("if", "success()") in ("success()", "${{ success() }}")
        assert not steps[i].get("continue-on-error", False)


if __name__ == "__main__":
    fake_azure()
