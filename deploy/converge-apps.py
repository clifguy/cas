"""Converge only the app revisions and configurations approved by the deployment."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import PurePosixPath
from typing import Any, Callable

Azure = Callable[..., Any]
# ARM guid() uses RFC 4122 version 5 with this documented namespace.
ARM_NAMESPACE = uuid.UUID("11fb06fb-712d-4ddd-98c7-e71bbd588830")


def azure(*args: str) -> Any:
    executable = shutil.which("az")
    if executable is None:
        raise ValueError("Azure CLI is required")
    result = subprocess.run(  # noqa: S603 -- argument array, never shell evaluation
        [executable, *args, "--output", "json", "--only-show-errors"],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def output(deployment: dict, key: str) -> str:
    value = deployment["properties"]["outputs"][key]["value"]
    require(isinstance(value, str) and bool(value), f"missing deployment output: {key}")
    return value


def validate_app(
    az: Azure, deployment: dict, group: str, kind: str, image: str
) -> tuple[str, str, bool]:
    app = output(deployment, f"{kind}ContainerAppName")
    approved = output(deployment, f"{kind}ContainerAppRevision")
    fingerprint = output(deployment, f"{kind}CloudConfigHash")
    properties = az("containerapp", "show", "--name", app, "--resource-group", group)["properties"]
    require(
        properties["configuration"]["activeRevisionsMode"] == "Single",
        f"{kind}: expected single revision mode",
    )
    require(
        properties["latestRevisionName"] == approved,
        f"{kind}: latest revision differs from deployment approval",
    )
    revisions = az(
        "containerapp", "revision", "list", "--all", "--name", app, "--resource-group", group
    )
    matches = [r for r in revisions if r["name"] == approved]
    require(len(matches) == 1, f"{kind}: approved revision missing or ambiguous")
    active = [r["name"] for r in revisions if r["properties"]["active"] is True]
    require(active in ([], [approved]), f"{kind}: unexpected active revisions")
    revision = matches[0]["properties"]
    require(type(revision["active"]) is bool, f"{kind}: invalid activation state")
    template = revision["template"]
    containers = template["containers"]
    require(len(containers) == 1, f"{kind}: unexpected containers")
    container = containers[0]
    require(
        container["name"] == ("sage" if kind == "sage" else "cas-bff"),
        f"{kind}: unexpected container identity",
    )
    require(container["image"] == image, f"{kind}: image differs from approved build")
    config_paths = [e.get("value") for e in container["env"] if e["name"] == "SAGE_CONFIG_PATH"]
    expected_path = f"/etc/{'sage' if kind == 'sage' else 'cas'}/config.cloud.yaml"
    require(config_paths == [expected_path], f"{kind}: unexpected config path")
    mounts = [
        m
        for m in container["volumeMounts"]
        if m["mountPath"] == str(PurePosixPath(expected_path).parent)
    ]
    require(len(mounts) == 1, f"{kind}: config mount missing or ambiguous")
    volumes = [v for v in template["volumes"] if v["name"] == mounts[0]["volumeName"]]
    require(
        len(volumes) == 1 and volumes[0]["storageType"] == "Secret",
        f"{kind}: config volume missing or ambiguous",
    )
    secret_name = f"{kind}-cloud-config"
    require(
        volumes[0]["secrets"] == [{"secretRef": secret_name, "path": "config.cloud.yaml"}],
        f"{kind}: revision mounts an unapproved config",
    )
    secrets = az(
        "containerapp",
        "secret",
        "list",
        "--name",
        app,
        "--resource-group",
        group,
        "--show-values",
        "--query",
        f"[?name=='{secret_name}']",
    )
    configs = [s.get("value") for s in secrets if s["name"] == secret_name]
    require(len(configs) == 1 and isinstance(configs[0], str), f"{kind}: config value unavailable")
    require(
        str(uuid.uuid5(ARM_NAMESPACE, configs[0])) == fingerprint,
        f"{kind}: config differs from approved deployment",
    )
    return app, approved, revision["active"]


def converge(az: Azure, environment: str, group: str, generation: str, image_tag: str) -> None:
    require(
        bool(environment and group and image_tag),
        "environment, resource group, and image tag are required",
    )
    deployment = az("deployment", "sub", "show", "--name", environment)
    props = deployment["properties"]
    require(props["provisioningState"] == "Succeeded", "deployment did not succeed")
    for key, value in {
        "postgresGeneration": generation,
        "resourceGroupName": group,
        "imageTag": image_tag,
    }.items():
        require(props["parameters"][key]["value"] == value, f"deployment parameter mismatch: {key}")
    output(deployment, "postgresServerFqdn")
    fence = (az("group", "show", "--name", group).get("tags") or {}).get("casPostgresMigration", "")
    require(
        fence in (f"cutover:{generation}", f"serving:{generation}") if generation else fence == "",
        "migration fence does not permit app convergence",
    )
    print(f"Approved generation: {generation or 'baseline'}; fence: {fence or 'none'}")
    registry = output(deployment, "acrLoginServer")
    # Validate both apps before changing either. Never use saved rollback revisions
    # as approval for the target database or select the first historical revision.
    plans = [
        validate_app(az, deployment, group, kind, f"{registry}/{kind}:{image_tag}")
        for kind in ("sage", "bff")
    ]
    require(plans[0][0] != plans[1][0], "deployment names the same app twice")
    for app, revision, active in plans:
        operation = "restart" if active else "activate"
        az(
            "containerapp",
            "revision",
            operation,
            "--name",
            app,
            "--resource-group",
            group,
            "--revision",
            revision,
        )
        revisions = az(
            "containerapp", "revision", "list", "--all", "--name", app, "--resource-group", group
        )
        require(
            [r["name"] for r in revisions if r["properties"]["active"] is True] == [revision],
            "approved revision did not become the sole active revision",
        )
        print(f"{app}: {operation} accepted for {revision}; serving readiness requires preflight")


def main() -> int:
    try:
        converge(
            azure,
            os.environ["ENVIRONMENT_NAME"],
            os.environ["RESOURCE_GROUP_NAME"],
            os.environ.get("POSTGRES_GENERATION", ""),
            os.environ["IMAGE_TAG"],
        )
    except (
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        subprocess.SubprocessError,
        OSError,
    ) as exc:
        # Azure output can include secret values; never print subprocess output or
        # malformed payloads. Validation errors contain only fixed diagnostics.
        message = (
            str(exc)
            if type(exc) is ValueError
            else f"{type(exc).__name__}: deployment evidence or Azure operation unavailable"
        )
        print(f"App convergence refused: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
