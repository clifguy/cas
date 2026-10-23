"""Exercise real SDK hooks and blob conditional writes without provider traffic."""

import json
from types import SimpleNamespace

import httpx
import pytest

from deploy.performance_runtime import Budget, Evidence, install_hooks, take_control


class Counter:
    def __init__(self):
        self.value = None
        self.etag = None

    def read_counter(self):
        return self.value, self.etag

    def write_counter(self, value, etag):
        assert etag == self.etag
        self.value = value
        self.etag = str(value["used"])
        return True


@pytest.mark.asyncio
async def test_actual_sdk_reserves_each_http_attempt_and_blocks_over_budget(monkeypatch, tmp_path):
    import anthropic

    from sage.adapters.embedding_nomic import NomicEmbeddingProvider

    original = anthropic.AsyncAnthropic.__init__
    embed = NomicEmbeddingProvider.embed
    encode = NomicEmbeddingProvider._embed_sync
    effects = []

    async def send(request):
        effects.append(request.url.path)
        return httpx.Response(
            200, json={"data": [], "has_more": False, "first_id": None, "last_id": None}
        )

    try:
        store = Counter()
        records = []
        install_hooks(
            "trial-one",
            Budget(store, "trial-one", 1),
            records.append,
            control_path=tmp_path / "control.json",
        )
        async with anthropic.AsyncAnthropic(
            api_key="synthetic", http_client=httpx.AsyncClient(transport=httpx.MockTransport(send))
        ) as client:
            await client.models.list()
            with pytest.raises(anthropic.APIConnectionError):
                await client.models.list()
        assert effects == ["/v1/models"]
        assert store.value["used"] == 1
        assert len([x for x in records if x["kind"] == "provider_attempt"]) == 1
    finally:
        monkeypatch.setattr(anthropic.AsyncAnthropic, "__init__", original)
        monkeypatch.setattr(NomicEmbeddingProvider, "embed", embed)
        monkeypatch.setattr(NomicEmbeddingProvider, "_embed_sync", encode)


def test_blob_counter_real_http_conflict_and_foreign_state_refused():
    calls = []

    def send(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(404)
        assert request.headers["If-None-Match"] == "*"
        return httpx.Response(412)

    store = Evidence(
        "https://stperfunit.blob.core.windows.net/evidence/trial-one",
        "trial-one",
        credential=SimpleNamespace(get_token=lambda *a: SimpleNamespace(token="test")),
        client=httpx.Client(transport=httpx.MockTransport(send)),
    )
    with pytest.raises(RuntimeError, match="contention"):
        Budget(store, "trial-one", 2).reserve()
    assert len(calls) == 16
    store = Counter()
    store.value = {"run_id": "other-run", "used": 0}
    with pytest.raises(RuntimeError, match="identity"):
        Budget(store, "trial-one", 2).reserve()
    assert store.value["used"] == 0


def test_boundary_control_is_not_consumed_by_another_stage(tmp_path):
    path = tmp_path / "control.json"
    path.write_text(json.dumps({"run_id": "trial-one", "action": "fail_provider", "nonce": "one"}))
    assert take_control("trial-one", path, {"fail_inference"}) is None
    assert path.exists()
    assert take_control("trial-one", path, {"fail_provider"})["nonce"] == "one"
    assert not path.exists()


def test_report_distinguishes_retained_evidence_from_absence(tmp_path):
    from deploy.performance_report import summarize

    plan = {"run_id": "trial-one", "candidate": {"image": "digest"}}
    absent = summarize(tmp_path, plan)
    assert "No retained stage traces" in absent["gaps"]
    root = tmp_path / "cloud-evidence/trial-one"
    root.mkdir(parents=True)
    (root / "samples-one.jsonl").write_text(
        "\n".join(
            json.dumps(x)
            for x in [
                {
                    "kind": "stage_record",
                    "record": {"stages": {"embedding.queue": {"total_ms": 100}}},
                },
                {"kind": "process_sample", "process_cpu_seconds": 3},
            ]
        )
        + "\n"
    )
    got = summarize(tmp_path, plan)
    assert got["stage_traces"][0]["stages"]["embedding.queue"]["total_ms"] == 100
    assert got["resource_sample_count"] == 1
    assert "No retained stage traces" not in got["gaps"]
    assert "Source, passage and vector reconciliation incomplete" in got["gaps"]


@pytest.mark.asyncio
async def test_cancellation_waits_for_its_own_actual_executor_entry(monkeypatch, tmp_path):
    import asyncio
    import threading

    import anthropic

    from sage.adapters.embedding_nomic import NomicEmbeddingProvider

    events = {name: threading.Event() for name in ("first", "second", "controlled")}
    releases = {name: threading.Event() for name in events}

    def encode(self, texts):
        name = texts[0]
        events[name].set()
        if not releases[name].wait(5):
            raise TimeoutError("test release not supplied")
        return [[1.0] + [0.0] * 767]

    original_client = anthropic.AsyncAnthropic.__init__
    original_embed = NomicEmbeddingProvider.embed
    monkeypatch.setattr(NomicEmbeddingProvider, "_embed_sync", encode)
    path = tmp_path / "control.json"
    provider = object.__new__(NomicEmbeddingProvider)
    provider._executor = None
    tasks = []
    records = []
    try:
        install_hooks(
            "trial-one", Budget(Counter(), "trial-one", 2), records.append, control_path=path
        )
        tasks.append(asyncio.create_task(provider.embed(["first"])))
        assert await asyncio.to_thread(events["first"].wait, 2)
        tasks.append(asyncio.create_task(provider.embed(["second"])))
        await asyncio.sleep(0.02)
        path.write_text(
            json.dumps({"run_id": "trial-one", "action": "cancel_inference", "nonce": "own"})
        )
        controlled = asyncio.create_task(provider.embed(["controlled"]))
        tasks.append(controlled)
        await asyncio.sleep(0.02)
        releases["first"].set()
        assert await asyncio.to_thread(events["second"].wait, 2)
        await asyncio.sleep(0.05)
        assert not controlled.done(), "another caller falsely witnessed controlled worker entry"
        assert not events["controlled"].is_set()
        releases["second"].set()
        assert await asyncio.to_thread(events["controlled"].wait, 2)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(controlled, 2)
        assert not releases["controlled"].is_set()
        assert any(x.get("nonce") == "own" for x in records)
    finally:
        for event in releases.values():
            event.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        if provider._executor:
            provider._executor.shutdown(wait=True)
        monkeypatch.setattr(anthropic.AsyncAnthropic, "__init__", original_client)
        monkeypatch.setattr(NomicEmbeddingProvider, "embed", original_embed)


def test_real_default_scaffold_is_completed_and_accepts_uploader_vocabulary():
    from deploy.performance_driver import trial_config
    from sage.config import VaultConfig
    from sage.models.schemas import IngestRequest
    from sage.services.ingestion import IngestionService
    from sage.services.vault_registry import VaultRegistryService

    scaffold = VaultRegistryService.get_default_config("perf_trial_one_a0")
    assert "timing" not in scaffold
    for timing, abstraction in [(True, True), (True, False), (False, False)]:
        cfg = VaultConfig.model_validate(trial_config(scaffold, timing, abstraction))
        assert cfg.timing.enabled is timing
        assert cfg.abstraction.enabled is abstraction
        service = object.__new__(IngestionService)
        service._config = cfg
        for source_type in ("markdown", "structured_data"):
            request = IngestRequest(
                source="synthetic", source_type=source_type, metadata={"doc_type": "misc"}
            )
            service._validate_caller_doc_type(request)
    assert "timing" not in scaffold
    assert all(row["value"] != "misc" for row in scaffold["document_types"]["doc_types"])


@pytest.mark.parametrize(
    "slow_stage,preparation,limit",
    [("parse", 2, 1), ("emit", 2, 1), ("parse", 1, 3), ("emit", 1, 3)],
)
def test_benchmark_lower_limit_includes_preparation(monkeypatch, slow_stage, preparation, limit):
    import sys
    from datetime import UTC, datetime, timedelta

    from deploy import performance_benchmark as subject

    clock = [0.0]
    events = []
    monkeypatch.setenv("PERF_PER_RUN_SECONDS", str(limit))
    monkeypatch.setenv(
        "PERF_BENCH_CASE",
        json.dumps({"shape": "short", "batch": 1, "threads": 1, "mode": "direct"}),
    )
    monkeypatch.setenv("PERF_RUN_ID", "trial-one")
    monkeypatch.setenv("PERF_EXPORT_URL", "unused")
    monkeypatch.setenv("PERF_EXPIRES_AT", (datetime.now(UTC) + timedelta(hours=1)).isoformat())

    class Store:
        def __init__(self, *args):
            pass

        def emit(self, row):
            if slow_stage == "emit":
                clock[0] += preparation

    class Child:
        done = False

        def poll(self):
            return 0 if self.done else None

        def terminate(self):
            self.done = True
            events.append("terminated")

        def wait(self, **kwargs):
            return 0

    monkeypatch.setitem(sys.modules, "performance_runtime", SimpleNamespace(Evidence=Store))
    monkeypatch.setattr(subject.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(subject.time, "sleep", lambda n: clock.__setitem__(0, clock[0] + n))

    def spawn(*args, **kwargs):
        events.append("spawned")
        return Child()

    monkeypatch.setattr(subject.subprocess, "Popen", spawn)
    original_loads = subject.json.loads

    def parse(raw):
        if slow_stage == "parse":
            clock[0] += preparation
        return original_loads(raw)

    monkeypatch.setattr(subject.json, "loads", parse)
    with pytest.raises(TimeoutError):
        subject.main()
    assert clock[0] == max(preparation, limit)
    assert events == ([] if preparation >= limit else ["spawned", "terminated"])


@pytest.mark.asyncio
async def test_service_run_limit_includes_restart_and_retains_partial_evidence(
    tmp_path, monkeypatch
):
    import asyncio

    from scripts import azure_ingest_trial as subject

    manifest = tmp_path / "trial.json"
    manifest.write_text(json.dumps({"proposed_limits": {"per_run_seconds": 1}}))
    output = tmp_path / "measurements.jsonl"
    effects = []

    async def stalled_restart(args):
        output.write_text(json.dumps({"kind": "initialization"}) + "\n")
        effects.append("restart_started")
        await asyncio.sleep(2)
        effects.append("restart_finished")

    monkeypatch.setattr(subject, "_execute", stalled_restart)
    with pytest.raises(TimeoutError):
        await subject.execute(SimpleNamespace(manifest=manifest, output=output))
    assert effects == ["restart_started"]
    assert json.loads(output.read_text().splitlines()[-1])["outcome"] == "partial_or_uncertain"


@pytest.mark.asyncio
async def test_control_limit_covers_exec_before_any_recompute(tmp_path, monkeypatch):
    import threading

    from deploy import performance_driver as subject

    def stalled_control(*args):
        threading.Event().wait(2)

    class Client:
        async def post(self, *args):
            pytest.fail("expired control must not start recomputation")

    monkeypatch.setattr(subject, "exec_control", stalled_control)
    with pytest.raises(TimeoutError):
        await subject._service_controls(
            {"limits": {"per_run_seconds": 1}},
            {},
            Client(),
            "https://isolated",
            "doc",
            None,
            {},
            tmp_path,
        )
