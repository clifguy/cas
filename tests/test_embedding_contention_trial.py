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
