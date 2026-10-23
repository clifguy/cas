"""Trial-only wrapper mounted alongside the immutable baseline image.

No public fault endpoint is added. Only an operator with container exec access can
place a one-shot, run-bound control file in the isolated replica. Source text,
credentials and exception details never enter observations.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
import platform
import resource
import runpy
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def process_sample():
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    row = {
        "pid": os.getpid(),
        "process_peak_bytes": peak * (1 if sys.platform == "darwin" else 1024),
        "process_cpu_seconds": time.process_time(),
        "at": datetime.now(UTC).isoformat(),
    }
    for name in (
        "cpu.stat",
        "cpu.max",
        "memory.current",
        "memory.peak",
        "memory.max",
        "memory.events",
    ):
        path = Path("/sys/fs/cgroup") / name
        if path.is_file():
            row[name] = path.read_text()[:4096]
    return row


class Budget:
    """Reserve before every SDK HTTP attempt, including model/count-token calls.

    An uncertain reservation is never retried into a provider call. Reservations
    can overcount after a failure; they cannot authorize extra calls after restart.
    """

    def __init__(self, store, run_id, limit):
        if type(limit) is not int or not 1 <= limit <= 30:
            raise ValueError("provider limit must be one through thirty")
        self.store, self.run_id, self.limit = store, run_id, limit

    def reserve(self, input_bytes=0):
        for _ in range(8):
            value, etag = self.store.read_counter()
            if value is None:
                value = {"run_id": self.run_id, "used": 0}
            if (
                value.get("run_id") != self.run_id
                or type(value.get("used")) is not int
                or value["used"] < 0
            ):
                raise RuntimeError("provider budget identity/state refused")
            if value["used"] >= self.limit:
                raise RuntimeError("provider request budget exhausted")
            if (
                type(input_bytes) is not int
                or input_bytes < 0
                or value.get("input_bytes", 0) + input_bytes > 10_000_000
            ):
                raise RuntimeError("provider input budget exhausted")
            updated = {"run_id": self.run_id, "used": value["used"] + 1}
            if input_bytes or "input_bytes" in value:
                updated["input_bytes"] = value.get("input_bytes", 0) + input_bytes
            if self.store.write_counter(updated, etag):
                return updated["used"]
        raise RuntimeError("provider budget contention refused")


class Evidence:
    """Managed-identity REST access to this run's private evidence container."""

    def __init__(self, url, run_id, credential=None, client=None):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.startswith("stperf")
            or not parsed.hostname.endswith(".blob.core.windows.net")
            or parsed.path != "/evidence/" + run_id
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid isolated export target")
        if credential is None:
            from azure.identity import ManagedIdentityCredential

            credential = ManagedIdentityCredential(client_id=os.environ["AZURE_CLIENT_ID"])
        self.credential = credential
        self.client = client or httpx.Client(timeout=15, follow_redirects=False)
        self.url = url
        self.stream = (
            f"samples-{os.environ.get('HOSTNAME', 'replica')}-{os.getpid()}-{time.time_ns()}.jsonl"
        )
        self.started = False

    def request(self, method, name, *, content=None, headers=None, params=None):
        auth = {
            "Authorization": "Bearer "
            + self.credential.get_token("https://storage.azure.com/.default").token,
            "x-ms-version": "2023-11-03",
            "x-ms-date": datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S GMT"),
        }
        return self.client.request(
            method,
            self.url + "/" + name,
            content=content,
            headers={**auth, **(headers or {})},
            params=params,
        )

    def read_counter(self):
        r = self.request("GET", "provider-budget.json")
        if r.status_code == 404:
            return None, None
        r.raise_for_status()
        return r.json(), r.headers["etag"]

    def write_counter(self, value, etag):
        condition = {"If-Match": etag} if etag else {"If-None-Match": "*"}
        r = self.request(
            "PUT",
            "provider-budget.json",
            content=json.dumps(value).encode(),
            headers={"x-ms-blob-type": "BlockBlob", **condition},
        )
        if r.status_code in (409, 412):
            return False
        r.raise_for_status()
        return True

    def emit(self, row):
        if not self.started:
            r = self.request(
                "PUT",
                self.stream,
                content=b"",
                headers={"x-ms-blob-type": "AppendBlob", "If-None-Match": "*"},
            )
            r.raise_for_status()
            self.started = True
        data = (json.dumps(row, separators=(",", ":")) + "\n").encode()
        if len(data) > 16384:
            raise RuntimeError("bounded evidence record exceeded")
        r = self.request("PUT", self.stream, content=data, params={"comp": "appendblock"})
        r.raise_for_status()


CONTROL_PATH = Path("/var/lib/sage/performance/control.json")


def take_control(run_id, path=CONTROL_PATH, actions=None):
    if path.is_symlink():
        raise RuntimeError("control symlink refused")
    if not path.exists():
        return None
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW)) as handle:
        value = json.load(handle)
    if (
        value.get("run_id") != run_id
        or value.get("action") not in ("fail_inference", "cancel_inference", "fail_provider")
        or not isinstance(value.get("nonce"), str)
    ):
        raise RuntimeError("invalid isolated control")
    if actions is not None and value["action"] not in actions:
        return None
    path.unlink()
    return value


def install_hooks(run_id, budget, emit, control_path=CONTROL_PATH):
    import anthropic

    from sage.adapters.embedding_nomic import NomicEmbeddingProvider

    original_encode = NomicEmbeddingProvider._embed_sync
    original_embed = NomicEmbeddingProvider.embed
    original_client = anthropic.AsyncAnthropic.__init__
    counters = {"inference_started": 0, "inference_finished": 0, "inference_failed": 0}
    control_entry = contextvars.ContextVar("trial_control_entry", default=None)
    lock = threading.Lock()

    def encode(self, texts):
        control = take_control(run_id, control_path, {"fail_inference"})
        with lock:
            counters["inference_started"] += 1
        entered = control_entry.get()
        if entered is not None:
            entered.set()
        try:
            if control and control["action"] == "fail_inference":
                emit(
                    {
                        "kind": "control",
                        "action": control["action"],
                        "nonce": control["nonce"],
                        "outcome": "injected_before_encode",
                    }
                )
                raise RuntimeError("isolated synthetic inference failure")
            return original_encode(self, texts)
        except BaseException:
            with lock:
                counters["inference_failed"] += 1
            raise
        finally:
            with lock:
                counters["inference_finished"] += 1

    async def embed(self, texts):
        control = take_control(run_id, control_path, {"cancel_inference"})
        if control is None:
            return await original_embed(self, texts)
        entered = threading.Event()
        context_token = control_entry.set(entered)
        try:
            task = asyncio.create_task(original_embed(self, texts))
        finally:
            control_entry.reset(context_token)
        try:
            if not await asyncio.to_thread(entered.wait, 30):
                raise TimeoutError("controlled inference did not enter")
            task.cancel()
            emit(
                {
                    "kind": "control",
                    "action": "cancel_inference",
                    "nonce": control["nonce"],
                    "outcome": "cancelled_after_worker_entry",
                }
            )
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def client(self, *args, **kwargs):
        kwargs["max_retries"] = 0
        original_client(self, *args, **kwargs)

        async def before_request(request):
            body = request.content
            payload = json.loads(body) if body else {}
            if payload.get("max_tokens", 0) > 1500:
                raise RuntimeError("trial output token limit exceeded")
            used = await asyncio.to_thread(budget.reserve, len(body))
            emit({"kind": "provider_attempt", "reserved": used, "sdk_retries": 0})
            control = take_control(run_id, control_path, {"fail_provider"})
            if control:
                emit(
                    {
                        "kind": "control",
                        "action": "fail_provider",
                        "nonce": control["nonce"],
                        "outcome": "injected_before_http_send",
                    }
                )
                raise RuntimeError("isolated synthetic provider failure")

        self._client.event_hooks.setdefault("request", []).append(before_request)

    NomicEmbeddingProvider._embed_sync = encode
    NomicEmbeddingProvider.embed = embed
    anthropic.AsyncAnthropic.__init__ = client
    return counters


def main():
    import re

    import yaml

    from sage import build_info

    run_id = os.environ["PERF_RUN_ID"]
    if not re.fullmatch("[a-z][a-z0-9-]{2,19}", run_id):
        raise ValueError("invalid run")
    cfg = yaml.safe_load(Path(os.environ["SAGE_CONFIG_PATH"]).read_text())
    if (
        cfg["profile"] != "cloud"
        or cfg["document_store"]["root_path"] != "performance/" + run_id
        or not cfg["postgres"]["host"].startswith("psql-perf-" + run_id + "-")
    ):
        raise ValueError("runtime refuses non-isolated cloud binding")
    if datetime.now(UTC) >= datetime.fromisoformat(os.environ["PERF_EXPIRES_AT"]):
        raise RuntimeError("trial expired")
    CONTROL_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if CONTROL_PATH.parent.is_symlink() or CONTROL_PATH.parent.stat().st_uid != os.getuid():
        raise ValueError("invalid private control directory")
    store = Evidence(os.environ["PERF_EXPORT_URL"], run_id)
    budget = Budget(store, run_id, int(os.environ["PERF_PROVIDER_LIMIT"]))
    emit_lock = threading.Lock()
    sequence = 0

    def emit(row):
        nonlocal sequence
        with emit_lock:
            sequence += 1
            record = {
                "run_id": run_id,
                "sequence": sequence,
                "replica": os.environ.get("HOSTNAME"),
                "at": datetime.now(UTC).isoformat(),
                **row,
            }
            store.emit(record)
            print(json.dumps(record, separators=(",", ":")), flush=True)

    emit(
        {
            "kind": "runtime_identity",
            "architecture": platform.machine(),
            "baseline_release": build_info.RELEASE_VERSION,
            "baseline_identity": build_info.BUILD_IDENTITY,
            "wrapper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "sampling_seconds": 5,
            "sdk_retries": 0,
        }
    )
    import logging

    class PerformanceExport(logging.Handler):
        def emit(self, record):
            payload = json.loads(record.getMessage())
            emit({"kind": "stage_record", "record": payload})

    logging.getLogger("sage.ingestion.performance").addHandler(PerformanceExport())
    counters = install_hooks(run_id, budget, emit)

    def sample():
        while True:
            try:
                if datetime.now(UTC) >= datetime.fromisoformat(os.environ["PERF_EXPIRES_AT"]):
                    os._exit(78)
                emit({"kind": "process_sample", **process_sample(), **dict(counters)})
            except BaseException:
                # Lost durable evidence makes further work uninterpretable.
                print('{"kind":"evidence_failure","outcome":"stop_required"}', flush=True)
                os._exit(71)
            time.sleep(5)

    threading.Thread(target=sample, daemon=True).start()
    # Explicitly preserve the baseline image's network binding, not runpy's argv.
    sys.argv = ["sage", "--host", "0.0.0.0", "--port", "8000"]  # noqa: S104 -- baseline container ingress binding
    runpy.run_module("sage", run_name="__main__")


if __name__ == "__main__":
    main()
