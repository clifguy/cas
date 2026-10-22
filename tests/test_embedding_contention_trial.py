"""Behavioral acceptance controls for the real-image embedding trial."""

import copy

import pytest

from scripts.embedding_contention_trial import trial_passed, unexpected_pipeline_errors


def valid_report():
    return {
        "phase": "recover",
        "failures": [],
        "semantic_max": 0.5,
        "lightweight_p95": 0.1,
        "lightweight_max": 0.2,
        "large_bulk_progress_during_queries": 3,
        "memory": {"process_peak_bytes": 1024**3, "memory.peak": str(2 * 1024**3)},
        "allowed_incomplete_ids": ["large"],
        "files": [
            {
                "id": "small",
                "pipeline_status": "abstraction_skipped",
                "pipeline_error": None,
                "reconstruction_matches": True,
                "stored_chunks": 3,
                "vectors_valid": True,
                "retained_source_matches": True,
            },
            {
                "id": "large",
                "pipeline_status": "indexing_in_progress",
                "pipeline_error": None,
                "claim_active": True,
                "retained_source_matches": True,
            },
        ],
        "recovery_control": {
            "interrupted": True,
            "claim_released_on_stop": True,
            "settled": True,
            "claim_released_after_settle": True,
            "reconstruction_matches": True,
            "vectors_valid": True,
            "retained_source_matches": True,
            "same_identity": True,
        },
        "duplicate_recovery_enqueues": 0,
        "shutdown": {"claims_released": True, "queue_empty": True, "failed_ids": []},
    }


def test_trial_accepts_documented_live_large_work_with_settled_recovery_control():
    assert trial_passed(valid_report())


@pytest.mark.parametrize(
    "change",
    [
        "failed",
        "corrupted",
        "missing_vectors",
        "stranded",
        "unfinished_control",
        "duplicate",
        "error",
        "resource",
    ],
)
def test_trial_rejects_false_recovery_acceptance(change):
    report = copy.deepcopy(valid_report())
    large = report["files"][-1]
    if change == "failed":
        large.update(pipeline_status="failed", pipeline_error="inference failed")
    elif change == "corrupted":
        large.update(
            pipeline_status="abstraction_skipped",
            stored_chunks=10,
            vectors_valid=True,
            reconstruction_matches=False,
        )
    elif change == "missing_vectors":
        report["files"][0]["vectors_valid"] = False
    elif change == "stranded":
        large["claim_active"] = False
    elif change == "unfinished_control":
        report["recovery_control"]["settled"] = False
    elif change == "duplicate":
        report["duplicate_recovery_enqueues"] = 1
    elif change == "error":
        large["pipeline_error"] = "failure after progress"
    else:
        report["memory"]["process_peak_bytes"] = 4 * 1024**3
    assert not trial_passed(report)


@pytest.mark.parametrize(
    "error,status,expected",
    [
        (
            "abstraction work was dropped before it completed: the queue draining it was stopped",
            "abstraction_interrupted",
            [],
        ),
        ("inference failed before stop", "abstraction_interrupted", ["large"]),
        (
            "abstraction work was dropped before it completed: the queue draining it was stopped",
            "failed",
            ["large"],
        ),
    ],
)
def test_deliberate_shutdown_cannot_hide_an_unrelated_error(error, status, expected):
    report = valid_report()
    report["shutdown"]["failed_ids"] = unexpected_pipeline_errors(
        [{"id": "large", "pipeline_status": status, "pipeline_error": error}], {"large"}
    )
    assert report["shutdown"]["failed_ids"] == expected
    assert trial_passed(report) is (not expected)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.actions = {}

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += 1
        action = self.actions.pop(self.now, None)
        if action:
            action()


def readiness_setup():
    from types import SimpleNamespace

    from scripts.embedding_contention_trial import BulkReadiness

    clock = FakeClock()
    readiness = BulkReadiness(clock=clock)
    provider = SimpleNamespace(count_tokens=lambda text: len(text))
    preparation = {}

    def project(document_id, projection):
        preparation["size"] = lambda: provider.count_tokens("candidate")
        preparation["drive"]()
        return [SimpleNamespace(document_id=document_id)]

    async def embed(chunks):
        return "submitted"

    ingestion = SimpleNamespace(_chunk_projection=project, _embed_chunks=embed)
    readiness.instrument(ingestion, provider)
    return clock, readiness, ingestion, provider, preparation


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected_time", [("slow", 150), ("stall", 120), ("cap", 300)])
async def test_readiness_target_progress_is_bounded(mode, expected_time):
    import asyncio
    from types import SimpleNamespace

    clock, readiness, ingestion, _, preparation = readiness_setup()
    checkpoints = []

    async def checkpoint():
        checkpoints.append(readiness.snapshot())

    def drive():
        if mode != "stall":
            for tick in range(30, 301, 30):
                clock.actions[tick] = preparation["size"]
        if mode == "slow":
            clock.actions[150] = lambda: readiness.run_inference(
                ["LARGE_BULK_TRIAL"], lambda texts: [[1.0]]
            )
        return asyncio.run(readiness.wait(checkpoint, sleep=clock.sleep))

    # Projection and sizing run on a worker thread in production. A separate
    # thread here also catches implementations using asyncio-local scope.
    preparation["drive"] = drive
    if mode == "slow":
        await asyncio.to_thread(
            ingestion._chunk_projection, "large", SimpleNamespace(text="LARGE_BULK_TRIAL")
        )
    else:
        with pytest.raises(TimeoutError):
            await asyncio.to_thread(
                ingestion._chunk_projection, "large", SimpleNamespace(text="LARGE_BULK_TRIAL")
            )
    assert clock.now == expected_time
    assert checkpoints
    expected_calls = {"stall": 0, "slow": 4, "cap": 10}
    assert readiness.snapshot()["sizing_completions"] == expected_calls[mode]


@pytest.mark.asyncio
async def test_unrelated_sizing_and_active_claim_do_not_extend_readiness():
    from types import SimpleNamespace

    clock, readiness, ingestion, provider, preparation = readiness_setup()
    preparation["drive"] = lambda: provider.count_tokens("unrelated")
    ingestion._chunk_projection("baseline", SimpleNamespace(text="AuditEvent"))
    for tick in range(30, 301, 30):
        clock.actions[tick] = lambda: ingestion._chunk_projection(
            "witness", SimpleNamespace(text="RECOVERY_WITNESS")
        )

    async def checkpoint():
        # Observing active state cannot manufacture completed target work.
        readiness.snapshot()

    with pytest.raises(TimeoutError):
        await readiness.wait(checkpoint, sleep=clock.sleep)
    assert clock.now == 120
    assert readiness.snapshot()["sizing_completions"] == 0


@pytest.mark.asyncio
async def test_submission_without_inference_entry_never_becomes_ready():
    from types import SimpleNamespace

    clock, readiness, ingestion, _, preparation = readiness_setup()
    preparation["drive"] = lambda: None
    chunks = ingestion._chunk_projection("large", SimpleNamespace(text="LARGE_BULK_TRIAL"))
    assert await ingestion._embed_chunks(chunks) == "submitted"
    assert readiness.snapshot()["stage"] == "submitted"

    async def checkpoint():
        pass

    with pytest.raises(TimeoutError):
        await readiness.wait(checkpoint, sleep=clock.sleep)
    assert clock.now == 120


def test_inference_requires_target_marker_and_preserves_result_and_failure():
    _, readiness, _, _, _ = readiness_setup()
    result = [[1.0]]
    assert readiness.run_inference(["AuditEvent"], lambda texts: result) is result
    assert not readiness.started.is_set()
    assert readiness.run_inference(["LARGE_BULK_TRIAL"], lambda texts: result) is result
    assert readiness.started.is_set()
    assert readiness.snapshot()["stage"] == "inference"
    error = ValueError("real inference failed")

    def fail(texts):
        raise error

    with pytest.raises(ValueError) as raised:
        readiness.run_inference(["LARGE_BULK_TRIAL"], fail)
    assert raised.value is error
    report = valid_report()
    report["failures"] = readiness.failures
    assert report["failures"][0]["kind"] == "model_inference"
    assert report["failures"][0]["source_group"] == "largest"
    assert not trial_passed(report)


def test_sizing_failure_does_not_count_as_progress_and_cleans_up_scope():
    from types import SimpleNamespace

    from scripts.embedding_contention_trial import BulkReadiness

    readiness = BulkReadiness(clock=FakeClock())
    error = ValueError("token sizing failed")

    def count(text):
        if text == "fail":
            raise error
        return 7

    provider = SimpleNamespace(count_tokens=count)

    def project(document_id, projection):
        return provider.count_tokens("fail")

    ingestion = SimpleNamespace(_chunk_projection=project, _embed_chunks=None)
    readiness.instrument(ingestion, provider)
    with pytest.raises(ValueError) as raised:
        ingestion._chunk_projection("large", SimpleNamespace(text="LARGE_BULK_TRIAL"))
    assert raised.value is error
    assert provider.count_tokens("outside") == 7
    assert readiness.snapshot()["sizing_completions"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "initialize", "startup", "body"])
async def test_observer_precedes_startup_recovery_and_restores_initializer(failure):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from scripts.embedding_contention_trial import BulkReadiness, observed_lifespan

    error = ValueError("original failure")
    clock = FakeClock()
    readiness = BulkReadiness(target_id="recovered-large", clock=clock)
    provider = SimpleNamespace(count_tokens=lambda text: 7)

    def project(document_id, projection):
        return [SimpleNamespace(document_id=document_id, tokens=provider.count_tokens("text"))]

    async def embed(chunks):
        pass

    services = SimpleNamespace(
        ingestion_service=SimpleNamespace(
            _embedding=provider,
            _chunk_projection=project,
            _embed_chunks=embed,
        )
    )

    async def initialize(sentinel):
        assert sentinel is app
        if failure == "initialize":
            raise error
        return services

    owner = SimpleNamespace(initialize_services=initialize)
    initialized = []

    @asynccontextmanager
    async def lifespan(app):
        returned = await owner.initialize_services(app)
        assert returned is services
        # This models automatic recovery *before* the lifespan yields. The
        # persisted identity permits selecting its projection without a marker.
        chunks = returned.ingestion_service._chunk_projection(
            "recovered-large", SimpleNamespace(text="retained projection")
        )
        assert chunks[0].tokens == 7
        assert readiness.snapshot()["sizing_completions"] == 1
        if failure == "startup":
            raise error
        yield

    app = SimpleNamespace(router=SimpleNamespace(lifespan_context=lifespan))

    async def run():
        async with observed_lifespan(app, owner, readiness, initialized.append):
            assert initialized == [services]
            assert readiness.snapshot()["prepared_chunks"] == 1
            if failure == "body":
                raise error

    if failure:
        with pytest.raises(ValueError) as raised:
            await run()
        assert raised.value is error
    else:
        await run()
    assert owner.initialize_services is initialize


@pytest.mark.asyncio
@pytest.mark.parametrize("activity", ["other_thread", "nested_projection"])
async def test_unrelated_activity_during_target_preparation_cannot_refresh_deadline(activity):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    from scripts.embedding_contention_trial import BulkReadiness

    clock = FakeClock()
    readiness = BulkReadiness(clock=clock)
    provider = SimpleNamespace(count_tokens=lambda text: len(text))

    async def checkpoint():
        pass

    def project(document_id, projection):
        if document_id == "large":
            return asyncio.run(readiness.wait(checkpoint, sleep=clock.sleep))
        return provider.count_tokens("RECOVERY_WITNESS")

    ingestion = SimpleNamespace(_chunk_projection=project, _embed_chunks=None)
    readiness.instrument(ingestion, provider)
    with ThreadPoolExecutor(max_workers=1) as unrelated:

        def activity_tick():
            if activity == "other_thread":
                unrelated.submit(provider.count_tokens, "baseline").result()
            else:
                ingestion._chunk_projection("witness", SimpleNamespace(text="RECOVERY_WITNESS"))

        for tick in range(30, 301, 30):
            clock.actions[tick] = activity_tick
        with pytest.raises(TimeoutError):
            await asyncio.to_thread(
                ingestion._chunk_projection, "large", SimpleNamespace(text="LARGE_BULK_TRIAL")
            )
    assert clock.now == 120
    assert readiness.snapshot()["sizing_completions"] == 0


@pytest.mark.asyncio
async def test_absolute_watchdog_bounds_a_stalled_checkpoint():
    import asyncio

    from scripts.embedding_contention_trial import BulkReadiness

    # A clock that stays fixed deliberately prevents the polling deadline from
    # rescuing a missing wall-clock watchdog while the diagnostic read hangs.
    readiness = BulkReadiness(clock=lambda: 0)
    entered = asyncio.Event()

    async def checkpoint():
        entered.set()
        await asyncio.Future()

    task = asyncio.create_task(readiness.wait(checkpoint, absolute_timeout=0.01))
    try:
        done, _ = await asyncio.wait({task}, timeout=0.2)
        assert entered.is_set()
        assert task in done, "stalled diagnostics escaped the readiness watchdog"
        with pytest.raises(TimeoutError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
