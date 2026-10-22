"""Isolated Azure ingestion trial validation and caller-local byte delivery."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import httpx

from sage.models.schemas import IngestRequest


def validate_target(plan: dict, live: dict) -> str:
    """Bind a prepared trial to a distinct live app before any data-plane write."""
    target = plan["target"]
    source = plan["source"]
    if target["resource_group"].casefold() == source["resource_group"].casefold():
        raise ValueError("trial must use a distinct resource group")
    if live["id"].casefold() != target["app_resource_id"].casefold():
        raise ValueError("live app identity differs from approved target")
    if live["resourceGroup"].casefold() != target["resource_group"].casefold():
        raise ValueError("wrong resource group")
    if live.get("tags", {}).get("benchmarkRun") != plan["run_id"]:
        raise ValueError("missing exact trial ownership tag")
    if not re.fullmatch(r"perf-[a-z0-9-]{1,40}", target["vault_id"]):
        raise ValueError("trial requires an isolated performance vault")
    if (
        live["properties"]["environmentId"].casefold()
        != target["environment_resource_id"].casefold()
    ):
        raise ValueError("environment differs from approved target")
    group_segment = "/resourcegroups/" + target["resource_group"].casefold() + "/"
    for key in ("environment_resource_id", "postgres_resource_id"):
        if group_segment not in target[key].casefold():
            raise ValueError("trial backend must be in its owned resource group")
    template = live["properties"]["template"]
    if template["scale"]["minReplicas"] != 1 or template["scale"]["maxReplicas"] != 1:
        raise ValueError("trial requires exactly one replica")
    containers = template["containers"]
    if len(containers) != 1 or containers[0]["image"] != target["image_digest"]:
        raise ValueError("immutable image differs from the approved target")
    if "@sha256:" not in target["image_digest"]:
        raise ValueError("trial requires an immutable image digest")
    if (
        containers[0]["resources"]["cpu"] != target["cpu"]
        or containers[0]["resources"]["memory"] != target["memory"]
    ):
        raise ValueError("resource allocation differs from the approved target")
    if containers[0].get("command") != target.get("command") or containers[0].get(
        "args"
    ) != target.get("args"):
        raise ValueError("container command differs from the approved image entry")
    environment = {row["name"]: row.get("value") for row in containers[0].get("env", [])}
    config_path = environment.get("SAGE_CONFIG_PATH")
    secret_name = target["config_secret_name"]
    if not re.fullmatch("[a-z0-9-]+", secret_name) or not config_path:
        raise ValueError("missing approved mounted cloud configuration")
    mounted = False
    for mount in containers[0].get("volumeMounts", []):
        for volume in template.get("volumes", []):
            if volume["name"] == mount["volumeName"] and volume.get("storageType") == "Secret":
                for item in volume.get("secrets", []):
                    if item["secretRef"] == secret_name:
                        path = PurePosixPath(mount["mountPath"]) / item["path"]
                        mounted = mounted or str(path) == config_path
    if not mounted:
        raise ValueError("approved cloud configuration is not mounted at the active path")
    origin = target["origin"]
    url = urlsplit(origin)
    if url.scheme != "https" or url.username or url.password or url.path not in ("", "/"):
        raise ValueError("invalid trusted origin")
    if url.hostname != live["properties"]["configuration"]["ingress"]["fqdn"]:
        raise ValueError("origin differs from the verified live app")
    if url.port not in (None, 443) or url.query or url.fragment:
        raise ValueError("invalid origin port or suffix")
    if target["app_resource_id"].casefold() == source["app_resource_id"].casefold():
        raise ValueError("production app cannot be a trial target")
    return origin.rstrip("/")


async def ingest_source(client: httpx.AsyncClient, origin: str, vault_id: str, path: Path) -> dict:
    """Deliver one digest-bound source and complete its synchronous REST ingest."""
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    payload = {
        "source": str(path.resolve()),
        "source_type": "markdown" if path.suffix == ".md" else "csv",
        "sha256": digest,
        "metadata": {"title": path.stem, "doc_type": "misc"},
        "needs_review": False,
    }
    # Use the same schema as the served route before sending anything.
    IngestRequest.model_validate(payload)
    endpoint = origin + "/sage_vaults/" + vault_id + "/documents"
    started = time.monotonic()
    minted = await client.post(endpoint, json=payload)
    minted.raise_for_status()
    recipe = minted.json()
    if recipe.get("status") != "upload_required" or len(recipe.get("uploads", [])) != 1:
        raise ValueError("expected one upload recipe; reconcile the response before retrying")
    item = recipe["uploads"][0]
    parsed = urlsplit(item["url"])
    trusted = urlsplit(origin)
    if (
        (parsed.scheme, parsed.hostname, parsed.port or 443)
        != (trusted.scheme, trusted.hostname, trusted.port or 443)
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("untrusted upload origin")
    if item.get("sha256", "").removeprefix("sha256:") != digest or len(raw) > recipe["max_bytes"]:
        raise ValueError("upload digest binding or size mismatch")
    # Byte legs use only the scoped transfer credential, never bearer auth.
    async with httpx.AsyncClient(follow_redirects=False, timeout=120) as bytes_client:
        response = await bytes_client.put(
            item["url"], content=raw, headers={recipe["token_header"]: item["token"]}
        )
        response.raise_for_status()
        receipt = response.json()
    if (
        receipt["transfer_id"] != item["transfer_id"]
        or receipt["size"] != len(raw)
        or receipt["sha256"].removeprefix("sha256:") != digest
    ):
        raise ValueError("upload receipt mismatch")
    payload.pop("source")
    payload["transfer_token"] = item["token"]
    response = await client.post(endpoint, json=payload)
    response.raise_for_status()
    result = response.json()
    document = result["document"]
    if result["pipeline_status"] not in ("abstraction_complete", "abstraction_skipped"):
        raise ValueError("ingest did not complete successfully")
    if document["source_content_hash"].removeprefix("sha256:") != digest:
        raise ValueError("source identity changed")
    return {
        "document_id": document["id"],
        "source_sha256": digest,
        "source_bytes": len(raw),
        "elapsed_seconds": time.monotonic() - started,
        "pipeline_status": result["pipeline_status"],
    }


def latency_summary(samples: list[dict]) -> dict:
    """Keep failures and missing positive witnesses out of successful latency claims."""
    import math

    output = {}
    for kind in ("lightweight", "semantic", "keyword"):
        group = [s for s in samples if s["kind"] == kind]
        if not group or any(not s["success"] for s in group):
            raise ValueError("missing or failed responsiveness samples")
        values = sorted(s["seconds"] for s in group)
        output[kind] = {
            "count": len(values),
            "p95_seconds": values[math.ceil(0.95 * len(values)) - 1],
            "maximum_seconds": max(values),
        }
    return output


def emit_jsonl(handle: Any, row: dict) -> None:
    handle.write(json.dumps(row) + "\n")
    handle.flush()


def validate_backend(plan: dict, postgres: dict, stack_text: str) -> None:
    """Verify the deployed cloud config actually points at the isolated backend."""
    import yaml

    target = plan["target"]
    if postgres["id"].casefold() != target["postgres_resource_id"].casefold():
        raise ValueError("wrong database server identity")
    if postgres.get("tags", {}).get("benchmarkRun") != plan["run_id"]:
        raise ValueError("database ownership differs from the approved trial")
    if hashlib.sha256(stack_text.encode()).hexdigest() != target["stack_config_sha256"]:
        raise ValueError("deployed stack configuration changed")
    config = yaml.safe_load(stack_text)
    if (
        config.get("profile") != "cloud"
        or config.get("abstraction", {}).get("provider") != "anthropic"
    ):
        raise ValueError("trial requires the representative cloud profile")
    pg = config["postgres"]
    if (
        pg["host"] != postgres["properties"]["fullyQualifiedDomainName"]
        or pg["database"] != target["postgres_database"]
    ):
        raise ValueError("deployed app does not use the isolated database")
    root = config["document_store"]["root_path"]
    if root != target["vault_source_root"] or root != "performance/" + plan["run_id"]:
        raise ValueError("deployed vault sources are not isolated to this run")
