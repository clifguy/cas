"""Drive isolated service trials and controls after committed provisioning."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx

from deploy.performance_limits import deadline_scope, remaining
from deploy.performance_trial import (
    ROOT,
    export_evidence,
    materialize_trial,
    wait_serving,
    write_json,
)


def trial_config(scaffold, timing, abstraction):
    """Complete the real served scaffold, including the uploader's explicit vocabulary."""
    import copy

    cfg = copy.deepcopy(scaffold)
    cfg["vault"]["name"] = "Synthetic performance trial"
    cfg["vault"]["owner"] = "performance-trial"
    cfg.setdefault("timing", {})["enabled"] = timing
    cfg.setdefault("abstraction", {})["enabled"] = abstraction
    types = cfg["document_types"]["doc_types"]
    if not any(row["value"] == "misc" for row in types):
        types.append({"value": "misc", "label": "Synthetic document"})
    return cfg


def control_command(run_id, action, nonce):
    import shlex

    payload = json.dumps({"run_id": run_id, "action": action, "nonce": nonce})
    code = (
        "import os; f=os.open('/var/lib/sage/performance/control.json',"
        " os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600); os.write(f, ("
        + repr(payload)
        + ").encode()); os.close(f); print("
        + repr("PERF_CONTROL_ACK:" + nonce)
        + ")"
    )
    return "python -c " + shlex.quote(code)


def exec_control(plan, trial, action, nonce):
    """Use a real PTY for Azure's exec client, bound to a freshly verified replica."""
    import pty
    import select
    import subprocess

    from scripts.azure_ingest_trial import runtime_snapshot

    deadline = time.monotonic() + remaining(120)
    _, process = runtime_snapshot(trial)
    remaining(deadline - time.monotonic())
    master, slave = pty.openpty()
    argv = [
        "az",
        "containerapp",
        "exec",
        "-g",
        plan["resource_group"],
        "-n",
        "ingest-perf-" + plan["run_id"],
        "--revision",
        trial["target"]["revision_name"],
        "--replica",
        process["replica"],
        "--container",
        "sage",
        "--command",
        control_command(plan["run_id"], action, nonce),
    ]
    try:
        remaining(deadline - time.monotonic())
        child = subprocess.Popen(  # noqa: S603 - fixed executable, bounded argv
            argv, stdin=slave, stdout=slave, stderr=slave, close_fds=True
        )
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    text = bytearray()
    try:
        while child.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], min(1, max(0, deadline - time.monotonic())))[0]:
                try:
                    part = os.read(master, 4096)
                except OSError:
                    break
                text.extend(part)
                if len(text) > 65536:
                    raise ValueError("unbounded exec response")
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=1)
        if child.returncode != 0 or ("PERF_CONTROL_ACK:" + nonce).encode() not in text:
            raise RuntimeError("control not acknowledged; reconcile before retry")
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=1)
        os.close(master)


async def document(client, base, doc_id):
    response = await client.get(base + "/documents/" + doc_id)
    response.raise_for_status()
    row = response.json()
    return row.get("document", row)


async def settled(client, base, doc_id, deadline):
    while time.monotonic() < deadline:
        row = await document(client, base, doc_id)
        if row["pipeline_status"] in ("abstraction_complete", "abstraction_skipped"):
            return row
        if row["pipeline_status"] == "failed":
            raise RuntimeError("pipeline failed; reconciliation required")
        await asyncio.sleep(1)
    raise TimeoutError("pipeline did not settle")


async def _service_controls(plan, trial, client, base, doc_id, rest, outputs, output):
    import uuid

    from scripts.azure_ingest_trial import restart_trial

    controls = []
    limit = plan["limits"]["per_run_seconds"]
    for action in ("fail_inference", "cancel_inference", "fail_provider"):
        with deadline_scope(limit):
            async with asyncio.timeout(remaining(limit)):
                deadline = time.monotonic() + limit
                nonce = uuid.uuid4().hex
                await asyncio.to_thread(exec_control, plan, trial, action, nonce)
                response = await client.post(base + "/documents/" + doc_id + "/recompute-pipeline")
                response.raise_for_status()
                await asyncio.sleep(2)
                before = await document(client, base, doc_id)
                if action == "cancel_inference":
                    await restart_trial(trial)
                after = await settled(client, base, doc_id, deadline)
                controls.append(
                    {
                        "action": action,
                        "nonce": nonce,
                        "observed_pipeline_status": before["pipeline_status"],
                        "final_pipeline_status": after["pipeline_status"],
                    }
                )
    with deadline_scope(limit):
        async with asyncio.timeout(remaining(limit)):
            deadline = time.monotonic() + limit
            response = await client.post(base + "/documents/" + doc_id + "/recompute-pipeline")
            response.raise_for_status()
            while time.monotonic() < deadline:
                before = await document(client, base, doc_id)
                if before["pipeline_status"] == "indexing_in_progress":
                    break
                await asyncio.sleep(0.2)
            else:
                raise ValueError("could not establish active indexing before recovery control")
            await restart_trial(trial)
            after = await settled(client, base, doc_id, deadline)
            controls.append(
                {
                    "action": "interrupted_recovery",
                    "before": before["pipeline_status"],
                    "after": after["pipeline_status"],
                }
            )
    await asyncio.to_thread(export_evidence, plan, outputs, rest, output)
    records = []
    for path in (output / "cloud-evidence" / plan["run_id"]).glob("samples-*.jsonl"):
        records.extend(json.loads(line) for line in path.read_text().splitlines())
    for row in controls:
        if "nonce" in row and not any(
            x.get("kind") == "control" and x.get("nonce") == row["nonce"] for x in records
        ):
            raise ValueError("injection receipt absent; no control verdict")
    write_json(
        output / "service-controls.json", {"controls": controls, "outcome": "observed_and_settled"}
    )
    return controls


async def service_controls(plan, trial, client, base, doc_id, rest, outputs, output):
    # Four independently bounded controls; each refuses extension at the cloud
    # expiry. Inner deadlines are shared across request/restart/poll work.
    deadline = min(
        4 * plan["limits"]["per_run_seconds"],
        (datetime.fromisoformat(plan["expires_at"]) - datetime.now(UTC)).total_seconds(),
    )
    with deadline_scope(deadline):
        async with asyncio.timeout(remaining(deadline)):
            return await _service_controls(plan, trial, client, base, doc_id, rest, outputs, output)


async def run_services(plan, outputs, az, rest, output):
    from sage.auth import INGRESS_KEY_HEADER
    from scripts import azure_ingest_trial as trial_runner
    from scripts.azure_ingest_trial import collect, execute, runtime_snapshot

    original_azure = trial_runner.azure
    trial_runner.azure = az
    app = "ingest-perf-" + plan["run_id"]
    group = plan["resource_group"]
    key = az(
        "containerapp",
        "secret",
        "list",
        "-g",
        group,
        "-n",
        app,
        "--show-values",
        "--query",
        "[?name=='trial-ingress'].value | [0]",
    )

    def fresh_token():
        return rest.token(plan["binding"]["audience"] + "/.default")

    async def refresh_request(request):
        request.headers["Authorization"] = "Bearer " + await asyncio.to_thread(fresh_token)

    token = fresh_token()
    os.environ["SAGE_TRIAL_ACCESS_TOKEN"] = token
    os.environ["SAGE_TRIAL_INGRESS_KEY"] = key
    sources = json.loads((ROOT / "deploy/performance/source-manifest.json").read_text())
    # Witness first, then the two fixed measured sources.
    paths = [
        ROOT / "deploy/performance/sources" / n
        for n in ("witness.md", "narrative.md", "structured.csv")
    ]
    arms = [
        (plan["arms"][0], True, True),
        (plan["arms"][1], True, True),
        (plan["arms"][2], True, True),
        (plan["arms"][1], True, False),
        (plan["arms"][1], False, False),
    ]
    vaults = []
    try:
        for index, (arm, timing, abstraction) in enumerate(arms):
            if (
                datetime.now(UTC).timestamp() + 1200
                >= datetime.fromisoformat(plan["expires_at"]).timestamp()
            ):
                raise TimeoutError("service/export budget exhausted")
            current = materialize_trial(plan, outputs, az, output)
            await asyncio.to_thread(runtime_snapshot, current)
            az(
                "containerapp",
                "update",
                "-g",
                group,
                "-n",
                app,
                "--cpu",
                str(arm["cpu"]),
                "--memory",
                arm["memory"],
                "--workload-profile-name",
                arm["profile"],
            )
            await asyncio.to_thread(wait_serving, plan, az, arm)
            trial = materialize_trial(plan, outputs, az, output)
            vault = plan["vault_id"] + "_a" + str(index)
            trial["target"]["vault_id"] = vault
            trial_path = output / f"arm-{index}-manifest.json"
            write_json(trial_path, trial)
            origin = trial["target"]["origin"]
            async with httpx.AsyncClient(
                headers={"Authorization": "Bearer " + token, INGRESS_KEY_HEADER: key},
                follow_redirects=False,
                timeout=30,
                event_hooks={"request": [refresh_request]},
            ) as client:
                response = await client.get(
                    origin + "/sage_vaults/default-config", params={"vault_id": vault}
                )
                response.raise_for_status()
                cfg = trial_config(response.json(), timing, abstraction)
                response = await client.post(origin + "/sage_vaults", json={"config": cfg})
                response.raise_for_status()
                start = datetime.now(UTC).isoformat()
                args = SimpleNamespace(
                    manifest=trial_path,
                    sources=paths,
                    query="cobalt heron pipeline timing witness",
                    output=output / f"arm-{index}-service.jsonl",
                    minimum_samples=30,
                    token_refresh=fresh_token,
                )
                await execute(args)
                finish = datetime.now(UTC).isoformat()
                collect(
                    SimpleNamespace(
                        manifest=trial_path,
                        start_time=start,
                        end_time=finish,
                        output=output / f"arm-{index}-metrics.json",
                    )
                )
                rows = [json.loads(x) for x in args.output.read_text().splitlines()]
                completed = [x for x in rows if x["kind"] in ("witness", "completed_source")]
                if len(completed) != 3:
                    raise ValueError("missing completed source")
                for row in completed:
                    response = await client.get(
                        origin
                        + "/sage_vaults/"
                        + vault
                        + "/documents/"
                        + row["document_id"]
                        + "/content"
                    )
                    response.raise_for_status()
                    if hashlib.sha256(response.content).hexdigest() != row["source_sha256"]:
                        raise ValueError("retained source differs")
                vaults.append(
                    {
                        "id": vault,
                        "sources": sources,
                        "expected_terminal": "abstraction_complete"
                        if abstraction
                        else "abstraction_skipped",
                        "timing": timing,
                        "arm": arm,
                    }
                )
                write_json(output / "reconciliation-request.json", {"vaults": vaults})
                if index == 2:
                    narrative = next(
                        x
                        for x in completed
                        if x["source_sha256"]
                        == next(s["sha256"] for s in sources if s["name"] == "narrative.md")
                    )
                    await service_controls(
                        plan,
                        trial,
                        client,
                        origin + "/sage_vaults/" + vault,
                        narrative["document_id"],
                        rest,
                        outputs,
                        output,
                    )
            # Export after each arm so a later failure retains earlier evidence.
            await asyncio.to_thread(export_evidence, plan, outputs, rest, output)
        rest.blob(
            "PUT",
            outputs["exportUrl"] + "/reconcile-request.json",
            content=json.dumps({"vaults": vaults}).encode(),
            headers={"x-ms-blob-type": "BlockBlob", "If-None-Match": "*"},
        )
        return vaults
    finally:
        trial_runner.azure = original_azure
        os.environ.pop("SAGE_TRIAL_ACCESS_TOKEN", None)
        os.environ.pop("SAGE_TRIAL_INGRESS_KEY", None)
