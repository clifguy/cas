"""Prepare an isolated Azure trial or execute an explicitly approved trial manifest.

Preparation reads Azure metadata only. Execution restarts the approved isolated
revision and writes synthetic sources to its verified trial vault; it never
provisions Azure resources.
"""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

from sage.utils.azure_ingest_trial import (
    emit_jsonl,
    ingest_source,
    latency_summary,
    ready_replica,
    validate_backend,
    validate_serving_revision,
    validate_target,
)


def azure(*args):
    result = subprocess.run(
        ["az", *args, "-o", "json", "--only-show-errors"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("Azure metadata read failed; inspect the authenticated CLI separately")
    return json.loads(result.stdout)


def prepare(args):
    if not re.fullmatch("[a-z0-9-]{1,32}", args.run_id):
        raise ValueError("run id must be a short resource-name suffix")
    source = azure(
        "containerapp",
        "show",
        "-g",
        args.source_group,
        "-n",
        args.source_app,
        "--query",
        "{id:id,resourceGroup:resourceGroup,location:location,properties:{environmentId:properties.environmentId,template:{containers:properties.template.containers[].{image:image,resources:resources},scale:properties.template.scale}}}",
    )
    subscription = source["id"].split("/")[2]
    group = "rg-ingest-perf-" + args.run_id
    app = "ingest-perf-" + args.run_id
    plan = {
        "schema_version": 1,
        "run_id": args.run_id,
        "prepared_at_epoch": time.time(),
        "source": {
            "app_resource_id": source["id"],
            "resource_group": source["resourceGroup"],
            "snapshot": source,
        },
        "target": {
            "resource_group": group,
            "app_resource_id": (
                f"/subscriptions/{subscription}/resourceGroups/{group}"
                f"/providers/Microsoft.App/containerApps/{app}"
            ),
            "vault_id": "perf-" + args.run_id,
            "environment_resource_id": None,
            "postgres_resource_id": None,
            "postgres_database": None,
            "stack_config_sha256": None,
            "config_secret_name": "sage-cloud-config",
            "vault_source_root": "performance/" + args.run_id,
            "origin": None,
            "revision_name": None,
            "restart_before_trial": False,
            "image_digest": None,
            "cpu": 2.0,
            "memory": "4Gi",
            "location": source["location"],
        },
        "proposed_limits": {
            "window_seconds": 14400,
            "per_run_seconds": 900,
            "incremental_usd": 50,
            "provider_calls": 30,
        },
        "required_before_operational_approval": [
            "Exact immutable candidate image and source identities; matching pinned model/runtime",
            "Private isolated PostgreSQL 17 B2s/32GiB and separate cloud vault-source root",
            "Independent trial resource group, environment, identity, network and ownership tags",
            "ARM preview and quota validation; complete priced inventory within proposed limit",
            "Validated fixed-memory two/four CPU control, separate from Consumption resource pairs",
            "Independent expiry/cleanup, exact deletion list and evidence export destination",
            "Cloud profile, hosted abstraction model and bounded provider usage verified",
            "Operator approval of provisioning/deployment/trials, isolated restart and manifest",
        ],
        "acceptance_pending": [
            "Azure stage/resource/throughput measurements",
            "Cancellation, failure and interrupted-recovery controls",
            "Vector/coverage reconciliation and cleanup readbacks",
        ],
    }
    with args.output.open("x") as handle:
        json.dump(plan, handle, indent=2)


def runtime_snapshot(plan):
    """Read and validate the serving configuration without exposing secret bytes."""
    target = plan["target"]
    live = azure("containerapp", "show", "--ids", target["app_resource_id"])
    common = ("-g", target["resource_group"], "-n", live["name"])
    revisions = azure("containerapp", "revision", "list", "--all", *common)
    revision = validate_serving_revision(plan, live, revisions)
    postgres = azure("resource", "show", "--ids", target["postgres_resource_id"])
    config = azure(
        "containerapp",
        "secret",
        "list",
        *common,
        "--show-values",
        "--query",
        "[?name=='" + target["config_secret_name"] + "'].value | [0]",
    )
    validate_backend(plan, postgres, config)
    replicas = azure(
        "containerapp", "replica", "list", *common, "--revision", target["revision_name"]
    )
    return live, ready_replica(revision, replicas)


async def restart_trial(plan):
    """Load the approved secret in a newly observed, ready isolated process."""
    target = plan["target"]
    if target.get("restart_before_trial") is not True:
        raise ValueError("manifest must explicitly approve the isolated trial restart")
    live, prior = await asyncio.to_thread(runtime_snapshot, plan)
    # All ownership, backend and serving checks precede this sole control-plane write.
    await asyncio.to_thread(
        azure,
        "containerapp",
        "revision",
        "restart",
        "-g",
        target["resource_group"],
        "-n",
        live["name"],
        "--revision",
        target["revision_name"],
    )
    async with asyncio.timeout(600):
        while True:
            try:
                current, process = await asyncio.to_thread(runtime_snapshot, plan)
                if process["container_id"] != prior["container_id"]:
                    return current, process
            except ValueError:
                # Readiness may be transient during replacement; no data-plane write yet.
                pass
            await asyncio.sleep(5)


async def execute(args):
    plan = json.loads(args.manifest.read_text())
    vault = plan["target"]["vault_id"]
    limit = plan["proposed_limits"]["per_run_seconds"]
    if not isinstance(limit, int) or not 1 <= limit <= 900:
        raise ValueError("invalid per-run deadline")
    if (
        len(args.sources) < 2
        or len(args.sources) > 4
        or any(p.stat().st_size > 32 * 1024**2 for p in args.sources)
    ):
        raise ValueError("trial requires two to four bounded sources")
    token = os.environ["SAGE_TRIAL_ACCESS_TOKEN"]
    samples = []
    with args.output.open("x") as output:
        emit_jsonl(
            output,
            {
                "kind": "initialization",
                "run_id": plan["run_id"],
                "outcome": "restart_pending",
                "target": plan["target"],
            },
        )
        live, process = await restart_trial(plan)
        origin = validate_target(plan, live)
        emit_jsonl(
            output,
            {
                "kind": "identity",
                "run_id": plan["run_id"],
                "target": plan["target"],
                "serving_process": process,
                "started_at_epoch": time.time(),
            },
        )
        headers = {"Authorization": "Bearer " + token}
        if os.environ.get("SAGE_TRIAL_INGRESS_KEY"):
            from sage.auth import INGRESS_KEY_HEADER

            headers[INGRESS_KEY_HEADER] = os.environ["SAGE_TRIAL_INGRESS_KEY"]
        async with httpx.AsyncClient(
            headers=headers, follow_redirects=False, timeout=limit
        ) as client:
            try:
                async with asyncio.timeout(limit):
                    base = origin + "/sage_vaults/" + vault
                    inventory = await client.post(
                        base + "/discover", json={"mode": "catalog", "limit": 0}
                    )
                    inventory.raise_for_status()
                    if inventory.json()["total_available"] != 0:
                        raise ValueError("trial vault is not empty; reconcile before retrying")
                    # The first supplied source is a small positive witness; seed it before load.
                    witness = await ingest_source(client, origin, vault, args.sources[0])
                    emit_jsonl(output, {"kind": "witness", **witness})
                    base = origin + "/sage_vaults/" + vault

                    async def probe(kind):
                        start = time.monotonic()
                        payload = {
                            "mode": kind,
                            "query": args.query,
                            "limit": 5,
                            "filters": {"document_ids": [witness["document_id"]]},
                        }
                        if kind == "lightweight":
                            payload = {"mode": "catalog", "limit": 1}
                        response = await client.post(
                            base + "/discover",
                            json=payload,
                            timeout=5 if kind == "lightweight" else 12,
                        )
                        response.raise_for_status()
                        success = kind == "lightweight" or bool(response.json().get("results"))
                        row = {
                            "kind": kind,
                            "seconds": time.monotonic() - start,
                            "success": success,
                            "observed_at_epoch": time.time(),
                        }
                        samples.append(row)
                        emit_jsonl(output, row)
                        if not success:
                            raise ValueError("positive search witness was absent")

                    # Validate witnesses before claiming concurrent responsiveness.
                    for kind in ("lightweight", "keyword", "semantic"):
                        await probe(kind)
                    baseline_count = len(samples)
                    for source in args.sources[1:]:
                        task = asyncio.create_task(ingest_source(client, origin, vault, source))
                        try:
                            while not task.done():
                                for kind in ("lightweight", "keyword", "semantic"):
                                    await probe(kind)
                                await asyncio.sleep(0.5)
                            emit_jsonl(output, {"kind": "completed_source", **await task})
                        finally:
                            if not task.done():
                                task.cancel()
                                await asyncio.gather(task, return_exceptions=True)
                    _, final_process = await asyncio.to_thread(runtime_snapshot, plan)
                    if final_process != process:
                        raise ValueError("trial process changed; measurements are invalid")
                    concurrent = samples[baseline_count:]
                    summary = latency_summary(concurrent)
                    if any(value["count"] < args.minimum_samples for value in summary.values()):
                        raise ValueError("insufficient concurrent samples for acceptance")
                    if (
                        summary["semantic"]["maximum_seconds"] > 12
                        or summary["lightweight"]["p95_seconds"] > 2
                        or summary["lightweight"]["maximum_seconds"] > 5
                    ):
                        raise ValueError("responsiveness budget exceeded")
                    emit_jsonl(
                        output,
                        {
                            "kind": "summary",
                            "latency": summary,
                            "outcome": "measurement_complete",
                            "acceptance_pending": plan["acceptance_pending"],
                        },
                    )
            except BaseException as exc:
                emit_jsonl(
                    output,
                    {
                        "kind": "failure",
                        "exception_class": type(exc).__name__,
                        "outcome": "partial_or_uncertain",
                        "recovery": "Reconcile exact documents and source hashes before retrying",
                    },
                )
                raise


def collect(args):
    """Read available Azure metrics for exactly the verified isolated resources."""
    plan = json.loads(args.manifest.read_text())
    live = azure("containerapp", "show", "--ids", plan["target"]["app_resource_id"])
    validate_target(plan, live)
    rows = []
    for key in ("app_resource_id", "postgres_resource_id"):
        resource_id = plan["target"][key]
        resource = azure("resource", "show", "--ids", resource_id)
        if resource.get("tags", {}).get("benchmarkRun") != plan["run_id"]:
            raise ValueError("metric resource is not owned by this trial")
        definitions = azure("monitor", "metrics", "list-definitions", "--resource", resource_id)
        selected = [
            row["name"]["value"]
            for row in definitions
            if any(
                word in row["name"]["value"].lower()
                for word in (
                    "cpu",
                    "memory",
                    "working",
                    "replica",
                    "restart",
                    "connection",
                    "iops",
                    "credit",
                )
            )
        ]
        metrics = []
        for name in selected:
            metrics.append(
                azure(
                    "monitor",
                    "metrics",
                    "list",
                    "--resource",
                    resource_id,
                    "--metric",
                    name,
                    "--start-time",
                    args.start_time,
                    "--end-time",
                    args.end_time,
                    "--interval",
                    "PT1M",
                )
            )
        rows.append(
            {
                "resource_id": resource_id,
                "available_metrics": [row["name"]["value"] for row in definitions],
                "observations": metrics,
            }
        )
    with args.output.open("x") as handle:
        json.dump(
            {
                "run_id": plan["run_id"],
                "start": args.start_time,
                "end": args.end_time,
                "resources": rows,
                "resolution": "PT1M",
                "interpretation": (
                    "Missing metrics remain unknown; "
                    "correlate with process/cgroup samples and traces"
                ),
            },
            handle,
            indent=2,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--source-group", required=True)
    prep.add_argument("--source-app", required=True)
    prep.add_argument("--run-id", required=True)
    prep.add_argument("--output", type=Path, required=True)
    run = commands.add_parser("execute")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--sources", type=Path, nargs="+", required=True)
    run.add_argument("--query", required=True)
    run.add_argument("--minimum-samples", type=int, choices=range(30, 301), default=30)
    run.add_argument("--output", type=Path, required=True)
    metrics = commands.add_parser("collect")
    metrics.add_argument("--manifest", type=Path, required=True)
    metrics.add_argument("--start-time", required=True)
    metrics.add_argument("--end-time", required=True)
    metrics.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args)
    elif args.command == "collect":
        collect(args)
    else:
        asyncio.run(execute(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
