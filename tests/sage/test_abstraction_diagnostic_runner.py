"""Durable reservation and honest population accounting for inactive diagnostics."""

import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from scripts import diagnose_abstraction_regressions as runner

SPEC = Path(__file__).resolve().parents[2] / "docs/development/abstraction-regression-campaign.json"


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "CAMPAIGN_HOME", tmp_path / "bindings")
    spec = json.loads(SPEC.read_text())
    spec["call_cap"] = 5
    spec_path = tmp_path / "spec.json"
    manifest_path = tmp_path / "manifest.json"
    ledger = tmp_path / "calls.sqlite"

    def create():
        spec_path.write_text(json.dumps(spec))
        runner.prepare(spec_path, manifest_path, ledger)
        return manifest_path

    return spec, create, manifest_path, ledger


def trial(spec, *, case=0, provider="qwen", stage="baseline", repeat=1):
    selected = spec["cases"][case]
    return {
        "case_id": selected["id"],
        "provider": provider,
        "arm": "hybrid",
        "stage": stage,
        "repeat": repeat,
        "selection_id": selected["selection_id"],
        "source_generation": selected["source_generation"],
    }


def output(request):
    text = request["case"]["source"]
    return {
        "raw": text,
        "licensed_repaired": text,
        "composed": runner.freeze_bytes(b"composed"),
        "stored_served": None,
        "usage": None,
    }


def test_cap_shared_across_providers_stages_failures_and_resume_never_dispatches(campaign):
    spec, create, path, _ = campaign
    create()
    calls = []

    def dispatch(request):
        calls.append(request)
        if len(calls) == 1:
            raise RuntimeError("simulated dispatch failure")
        # Reservation is committed and visible before external work.
        assert runner.status(path)["reserved_calls"] == len(calls)
        return output(request)

    with pytest.raises(RuntimeError):
        runner.dispatch_trial(path, trial(spec), dispatcher=dispatch)
    for i in range(4):
        runner.dispatch_trial(
            path,
            trial(
                spec,
                provider="anthropic" if i % 2 else "qwen",
                stage="repaired" if i % 2 else "validation",
            ),
            dispatcher=dispatch,
        )
    refusal = None
    try:
        runner.dispatch_trial(path, trial(spec), dispatcher=dispatch)
    except ValueError as exc:
        refusal = exc
    assert len(calls) == 5  # Over-cap refusal must happen before the callable.
    assert refusal is not None and "cap exhausted" in str(refusal)
    report = runner.status(path)
    assert report["reserved_calls"] == 5
    assert report["states"] == {"reserved": 0, "completed": 4, "failed": 1}
    assert len({r["attempt_id"] for r in report["records"]}) == 5
    assert report["records"][1]["result"]["raw"] != report["records"][1]["result"]["composed"]
    assert report["records"][1]["result"]["stored_served"] is None
    assert report["records"][1]["result"]["usage"] is None


def test_concurrent_workers_cannot_overbook_shared_ledger(campaign):
    spec, create, path, _ = campaign
    spec["call_cap"] = 20
    create()
    dispatched = []

    def dispatch(request):
        dispatched.append(request["attempt_id"])
        return output(request)

    def invoke(i):
        try:
            runner.dispatch_trial(
                path, trial(spec, provider="qwen" if i % 2 else "anthropic"), dispatcher=dispatch
            )
            return True
        except ValueError as exc:
            assert "cap exhausted" in str(exc)
            return False

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(invoke, range(35)))
    assert sum(outcomes) == len(dispatched) == 20
    assert runner.status(path)["remaining_calls"] == 0


def test_full_120_cap_spans_all_three_stage_allocations(campaign):
    spec, create, path, _ = campaign
    spec["call_cap"] = 120
    create()
    calls = []

    def dispatch(request):
        calls.append(request)
        return output(request)

    for stage in spec["stages"]:
        for _ in range(40):
            runner.dispatch_trial(path, trial(spec, stage=stage), dispatcher=dispatch)
    with pytest.raises(ValueError, match="cap exhausted"):
        runner.dispatch_trial(path, trial(spec), dispatcher=dispatch)
    assert len(calls) == 120
    assert runner.status(path)["stages"] == {"baseline": 40, "repaired": 40, "validation": 40}


def test_lost_process_reservation_stays_consumed(campaign):
    spec, create, path, _ = campaign
    spec["call_cap"] = 1
    create()

    def crash(request):
        raise SystemExit("process lost after dispatch reservation")

    with pytest.raises(SystemExit):
        runner.dispatch_trial(path, trial(spec), dispatcher=crash)
    assert runner.status(path)["reserved_calls"] == 1
    with pytest.raises(ValueError, match="cap exhausted"):
        runner.dispatch_trial(path, trial(spec), dispatcher=lambda _: pytest.fail("dispatched"))


def test_binding_prevents_budget_reset_by_changing_manifest_and_ledger(campaign, tmp_path):
    spec, create, path, _ = campaign
    create()
    runner.dispatch_trial(path, trial(spec), dispatcher=output)
    spec_path = tmp_path / "new-spec.json"
    spec_path.write_text(json.dumps(spec))
    with pytest.raises(FileExistsError):
        runner.prepare(
            spec_path,
            tmp_path / "new-manifest.json",
            tmp_path / "new-ledger.sqlite",
        )
    assert runner.status(path)["reserved_calls"] == 1


@pytest.mark.parametrize(
    "mutation", ["manifest", "frozen_bytes", "delete_row", "record", "identity"]
)
def test_tampered_manifest_or_ledger_never_dispatches(campaign, mutation):
    spec, create, path, ledger = campaign
    create()
    runner.dispatch_trial(path, trial(spec), dispatcher=output)
    if mutation in ("manifest", "frozen_bytes"):
        manifest = json.loads(path.read_text())
        if mutation == "manifest":
            manifest["annotation"] = "tampered after freezing"
        else:
            manifest["spec"]["cases"][0]["source"]["base64"] = "ZXZpbA=="
            manifest["spec_sha256"] = runner.digest(manifest["spec"])
            manifest["manifest_sha256"] = runner.digest(
                {k: v for k, v in manifest.items() if k != "manifest_sha256"}
            )
        path.write_text(json.dumps(manifest))
    else:
        with sqlite3.connect(ledger) as db:
            if mutation == "delete_row":
                db.execute("DELETE FROM attempts")
            elif mutation == "identity":
                db.execute("UPDATE meta SET identity = 'wrong'")
            else:
                payload = json.loads(
                    db.execute("SELECT payload FROM attempts WHERE ordinal = 1").fetchone()[0]
                )
                payload["error"] = "tampered"
                db.execute(
                    "UPDATE attempts SET payload = ? WHERE ordinal = 1", (json.dumps(payload),)
                )
    with pytest.raises(ValueError):
        runner.dispatch_trial(path, trial(spec), dispatcher=lambda _: pytest.fail("dispatched"))


def test_missing_ledger_is_never_recreated(campaign):
    spec, create, path, ledger = campaign
    create()
    ledger.unlink()
    with pytest.raises(sqlite3.OperationalError):
        runner.dispatch_trial(path, trial(spec), dispatcher=lambda _: pytest.fail("dispatched"))
    assert not ledger.exists()


@pytest.mark.parametrize(
    "field,value",
    [("call_cap", 121), ("repeat_policy", "retry_free"), ("stages", {"baseline": 121})],
)
def test_malformed_spec_is_rejected_before_preparation(campaign, field, value):
    spec, create, _, ledger = campaign
    spec[field] = value
    with pytest.raises(ValueError):
        create()
    assert not ledger.exists()


def test_source_generation_or_selection_cannot_drift(campaign):
    spec, create, path, _ = campaign
    create()
    for field in ("selection_id", "source_generation"):
        changed = {**trial(spec), field: "changed"}
        with pytest.raises(ValueError, match="source/selection"):
            runner.dispatch_trial(path, changed, dispatcher=lambda _: pytest.fail("dispatched"))
    assert runner.status(path)["reserved_calls"] == 0


def test_report_uses_existing_repeat_partition_and_missing_label_instrument(campaign):
    spec, create, path, _ = campaign
    # Same source and bytes in another split must be reported even if output differs.
    spec["cases"][1].update(
        source=copy.deepcopy(spec["cases"][0]["source"]),
        evidence=copy.deepcopy(spec["cases"][0]["evidence"]),
        source_generation=spec["cases"][0]["source_generation"],
        selection_id=spec["cases"][0]["selection_id"],
        source_id=spec["cases"][0]["source_id"],
        template_group=spec["cases"][0]["template_group"],
        split="held_out",
    )
    create()
    runner.dispatch_trial(path, trial(spec), dispatcher=output)
    runner.dispatch_trial(path, trial(spec, case=1, repeat=2), dispatcher=output)
    report = runner.status(path, report=True)
    assert not report["calibration_placeholder"]
    assert report["calibration"]["independent_pairs"] == 1
    assert not report["calibration"]["runtime_promotions"]
    for rule in report["calibration"]["rules"].values():
        assert not rule["ready_for_owner_acceptance"]
        assert {
            "repeated_output_is_not_independent_evidence",
            "source_leakage_between_splits",
            "template_leakage_between_splits",
            "evidence_leakage_between_splits",
            "independent_labels_missing",
            "positive_population_or_provider_minimum_not_met",
        } <= set(rule["blockers"])


def test_no_output_report_discloses_placeholder_and_no_live_dispatch(campaign):
    _, create, path, _ = campaign
    create()
    report = runner.status(path, report=True)
    assert report["calibration_placeholder"]
    assert not report["live_dispatch_enabled"]
    assert report["reserved_calls"] == 0


def test_unsafe_ledger_path_is_rejected(campaign):
    with pytest.raises(ValueError, match="outside repository"):
        runner.safe_ledger(SPEC.parent / "calls.sqlite")
    with pytest.raises(ValueError, match="cloud synced"):
        runner.safe_ledger(Path.home() / "Library/CloudStorage/example/calls.sqlite")


def test_cli_status_report_and_no_dispatch_operation(campaign, monkeypatch, capsys):
    _, create, path, _ = campaign
    create()
    for operation in ("status", "report"):
        monkeypatch.setattr("sys.argv", ["diagnostic", operation, "--manifest", str(path)])
        runner.main()
        result = json.loads(capsys.readouterr().out)
        assert result["reserved_calls"] == 0
        assert result["live_dispatch_enabled"] is False
    monkeypatch.setattr("sys.argv", ["diagnostic", "dispatch", "--manifest", str(path)])
    with pytest.raises(SystemExit):
        runner.main()


def test_stage_allocation_refuses_before_dispatch(campaign):
    spec, create, path, _ = campaign
    spec["call_cap"] = 120
    create()
    calls = []

    def dispatch(request):
        calls.append(request)
        return output(request)

    for _ in range(40):
        runner.dispatch_trial(path, trial(spec), dispatcher=dispatch)
    with pytest.raises(ValueError, match="stage allocation"):
        runner.dispatch_trial(path, trial(spec), dispatcher=dispatch)
    assert len(calls) == 40
    assert runner.status(path)["remaining_calls"] == 80
