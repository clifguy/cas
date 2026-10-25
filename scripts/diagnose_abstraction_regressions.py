"""Inactive synthetic campaign. CLI prepares, inspects and reports; never calls providers.

Run with ``.venv/bin/python -m scripts.diagnose_abstraction_regressions``.
The injected dispatcher is a deterministic test seam, not live-call authorization.
"""

import argparse
import base64
import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Callable

from sage.config import VaultAbstractionConfig
from sage.models.abstraction_evidence import decode_package
from sage.services.bounded_abstraction import abstraction_request
from scripts.evaluate_abstraction_publication import calibration_report

HARD_CALL_CAP = 120
CAMPAIGN_ID = "abstraction-factual-regressions-v1"
CAMPAIGN_HOME = Path.home() / ".local/state/cas/diagnostics"
OUTPUT_STAGES = ("raw", "licensed_repaired", "composed", "stored_served")


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def freeze_bytes(value: bytes) -> dict:
    return {"base64": base64.b64encode(value).decode(), "sha256": hashlib.sha256(value).hexdigest()}


def thaw_bytes(value: dict) -> bytes:
    raw = base64.b64decode(value["base64"], validate=True)
    if hashlib.sha256(raw).hexdigest() != value["sha256"]:
        raise ValueError("frozen bytes hash mismatch")
    return raw


def admitted_input(spec: dict, case: dict, arm: str) -> dict:
    """Freeze the actual request/check seam, including complete-prompt fitting.

    Source bytes remain a separate identity; they are never a scoring baseline.
    The arm changes representation while preserving runtime attestation policy.
    """
    config = VaultAbstractionConfig.model_validate(
        {**spec["request_config"], "hybrid_description": arm == "hybrid"}
    )
    request = abstraction_request(decode_package(json.loads(thaw_bytes(case["evidence"]))), config)
    return {
        "config": config.model_dump(mode="json"),
        "authored": freeze_bytes(request.text.authored_text.encode()),
        "provider_text": freeze_bytes(
            str(request.provider_text if arm == "hybrid" else request.text).encode()
        ),
    }


def report_input(spec: dict, records: list[dict]) -> dict:
    cases = []
    for record in records:
        if record["state"] != "completed":
            continue
        trial = record["trial"]
        case = next(c for c in spec["cases"] if c["id"] == trial["case_id"])
        output = record["result"]["licensed_repaired"]
        if output is None:
            continue
        cases.append(
            {
                "id": record["attempt_id"],
                "source_id": case["source_id"],
                "family": case["family"],
                "template_group": case["template_group"],
                "provider": trial["provider"],
                "split": case["split"],
                "origin": "reconstructed",
                "authored": thaw_bytes(record["admitted_authored"]).decode(),
                "candidate": thaw_bytes(output).decode(),
                "expected": None,
                "faithful": None,
                "fallback_safe": None,
                "fallback_useful": None,
                "served_safe": None,
            }
        )
    if not cases:
        case = spec["cases"][0]
        cases = [
            {
                "id": "unexecuted-placeholder",
                "source_id": case["source_id"],
                "family": case["family"],
                "template_group": case["template_group"],
                "provider": "qwen",
                "split": "development",
                "origin": "reconstructed",
                "authored": thaw_bytes(admitted_input(spec, case, "hybrid")["authored"]).decode(),
                "candidate": "",
                "expected": None,
                "faithful": None,
            }
        ]
    return {
        "schema_version": 1,
        "implementation_sha256": spec.get("implementation_sha256"),
        "candidate_stage": "post_licensed_repairs",
        "labels_status": "unadjudicated",
        "named_regressions": {},
        "cases": cases,
    }


def validate_spec(spec: dict) -> None:
    if spec.get("schema_version") != 1 or spec.get("campaign_id") != CAMPAIGN_ID:
        raise ValueError("unknown campaign schema/identity")
    if type(spec.get("call_cap")) is not int or not 1 <= spec["call_cap"] <= HARD_CALL_CAP:
        raise ValueError("invalid call cap")
    if spec.get("repeat_policy") != "count_every_attempt_never_independent":
        raise ValueError("invalid repeat policy")
    if spec.get("stages") != {"baseline": 40, "repaired": 40, "validation": 40}:
        raise ValueError("unfrozen stage allocation")
    if spec.get("arms") != ["legacy", "hybrid"] or spec.get("max_repeats") != 3:
        raise ValueError("unfrozen arms/repeats")
    VaultAbstractionConfig.model_validate(spec["request_config"])
    providers = spec.get("providers", {})
    if set(providers) != {"qwen", "anthropic"}:
        raise ValueError("invalid provider roster")
    for config in providers.values():
        if not all(key in config for key in ("model", "revision", "settings")):
            raise ValueError("provider identity/settings missing")
        if not isinstance(config["model"], str) or not isinstance(config["settings"], dict):
            raise ValueError("invalid provider settings")
    cases = spec.get("cases", [])
    if len(cases) != 10 or len({c["id"] for c in cases}) != 10:
        raise ValueError("expected ten unique frozen case families")
    for case in cases:
        for key in (
            "id",
            "source_id",
            "family",
            "template_group",
            "expected_behavior",
            "selection_id",
            "source_generation",
        ):
            if not isinstance(case.get(key), str) or not case[key].strip():
                raise ValueError("missing case identity/expectation")
        if case.get("split") not in ("development", "held_out"):
            raise ValueError("unknown split")
        for key in ("source", "evidence"):
            thaw_bytes(case[key]).decode("utf-8")
        if case["source_generation"] != "sha256:" + case["source"]["sha256"]:
            raise ValueError("source generation hash mismatch")
        if case["selection_id"] != case["evidence"]["sha256"]:
            raise ValueError("selection evidence hash mismatch")
    for value in spec.get("retained_artifacts", {}).values():
        thaw_bytes(value)
    calibration_report(report_input(spec, []))


def safe_ledger(path: Path) -> Path:
    path = path.resolve()
    repo = Path(__file__).resolve().parents[1]
    inside_checkout = any((parent / ".git").exists() for parent in path.parents)
    if (
        path.is_relative_to(repo)
        or inside_checkout
        or any(p in path.parts for p in ("CloudStorage", "Mobile Documents", "Dropbox", "OneDrive"))
    ):
        raise ValueError("ledger must be outside repository and cloud synced directories")
    return path


def prepare(spec_path: Path, manifest_path: Path, ledger_path: Path) -> dict:
    spec = json.loads(spec_path.read_text())
    validate_spec(spec)
    ledger_path = safe_ledger(ledger_path)
    campaign_home = safe_ledger(CAMPAIGN_HOME)
    manifest = {
        "spec": spec,
        "spec_sha256": digest(spec),
        "ledger": str(ledger_path),
        "campaign_home": str(campaign_home),
    }
    manifest["manifest_sha256"] = digest(manifest)
    if manifest_path.exists():
        raise ValueError("frozen manifest already exists")
    campaign_home.mkdir(parents=True, exist_ok=True)
    binding = campaign_home / (CAMPAIGN_ID + ".binding.json")
    with binding.open("x") as stream:
        stream.write(
            json.dumps({"ledger": str(ledger_path), "manifest_sha256": manifest["manifest_sha256"]})
        )
    fd = os.open(ledger_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with sqlite3.connect(ledger_path) as db:
        db.execute("CREATE TABLE meta (identity TEXT NOT NULL, reserved INTEGER NOT NULL)")
        db.execute("INSERT INTO meta VALUES (?, 0)", (manifest["manifest_sha256"],))
        db.execute(
            "CREATE TABLE attempts (ordinal INTEGER PRIMARY KEY, payload TEXT NOT NULL, "
            "sha256 TEXT NOT NULL)"
        )
    with manifest_path.open("x") as stream:
        stream.write(json.dumps(manifest, indent=2) + "\n")
    return manifest


def load_manifest(path: Path) -> dict:
    manifest = json.loads(path.read_text())
    identity = manifest.get("manifest_sha256")
    if digest({k: v for k, v in manifest.items() if k != "manifest_sha256"}) != identity:
        raise ValueError("manifest integrity mismatch")
    if digest(manifest["spec"]) != manifest["spec_sha256"]:
        raise ValueError("spec integrity mismatch")
    validate_spec(manifest["spec"])
    campaign_home = safe_ledger(Path(manifest["campaign_home"]))
    if str(campaign_home) != manifest["campaign_home"] or campaign_home != safe_ledger(
        CAMPAIGN_HOME
    ):
        raise ValueError("campaign home must be frozen and absolute")
    binding = json.loads((campaign_home / (CAMPAIGN_ID + ".binding.json")).read_text())
    if binding != {"ledger": manifest["ledger"], "manifest_sha256": identity}:
        raise ValueError("campaign binding mismatch")
    if str(safe_ledger(Path(manifest["ledger"]))) != manifest["ledger"]:
        raise ValueError("ledger path must be frozen and absolute")
    return manifest


def connect(manifest: dict) -> sqlite3.Connection:
    # rw fails closed on a missing ledger, never resets a consumed budget.
    db = sqlite3.connect(Path(manifest["ledger"]).as_uri() + "?mode=rw", uri=True, timeout=30)
    db.execute("PRAGMA synchronous=FULL")
    return db


def validate_trial(spec: dict, trial: dict) -> None:
    if set(trial) != {
        "case_id",
        "provider",
        "arm",
        "stage",
        "repeat",
        "selection_id",
        "source_generation",
    }:
        raise ValueError("invalid trial identity")
    if (
        trial["case_id"] not in {c["id"] for c in spec["cases"]}
        or trial["provider"] not in spec["providers"]
        or trial["arm"] not in spec["arms"]
        or trial["stage"] not in spec["stages"]
        or type(trial["repeat"]) is not int
        or not 1 <= trial["repeat"] <= spec["max_repeats"]
        or not all(
            isinstance(trial[k], str) and trial[k] for k in ("selection_id", "source_generation")
        )
    ):
        raise ValueError("trial outside frozen manifest")
    case = next(c for c in spec["cases"] if c["id"] == trial["case_id"])
    if any(trial[k] != case[k] for k in ("selection_id", "source_generation")):
        raise ValueError("trial source/selection changed")


def read_records(db: sqlite3.Connection, manifest: dict) -> list[dict]:
    meta = db.execute("SELECT identity, reserved FROM meta").fetchall()
    rows = db.execute("SELECT ordinal, payload, sha256 FROM attempts ORDER BY ordinal").fetchall()
    if meta != [(manifest["manifest_sha256"], len(rows))]:
        raise ValueError("ledger identity/count mismatch")
    if len(rows) > min(HARD_CALL_CAP, manifest["spec"]["call_cap"]):
        raise ValueError("ledger exceeds call cap")
    records = []
    for ordinal, payload, checksum in rows:
        record = json.loads(payload)
        if ordinal != len(records) + 1 or digest(record) != checksum:
            raise ValueError("ledger record integrity mismatch")
        if record["state"] not in ("reserved", "completed", "failed"):
            raise ValueError("invalid ledger state")
        validate_trial(manifest["spec"], record["trial"])
        case = next(c for c in manifest["spec"]["cases"] if c["id"] == record["trial"]["case_id"])
        admitted = admitted_input(manifest["spec"], case, record["trial"]["arm"])
        if (
            record["admitted_authored"] != admitted["authored"]
            or record["request_config"] != admitted["config"]
            or record["provider_text"] != admitted["provider_text"]
        ):
            raise ValueError("trial admitted evidence/config mismatch")
        records.append(record)
    return records


def dispatch_trial(manifest_path: Path, trial: dict, *, dispatcher: Callable[[dict], dict]) -> dict:
    """Reserve durably before an injected single-call dispatch, including retries.

    A crash leaves a consumed reserved slot. No live adapter/authorization route
    exists. Callable must make at most one synthetic call and must not retry.
    """
    trial = json.loads(canonical(trial))
    manifest = load_manifest(manifest_path)
    spec = manifest["spec"]
    validate_trial(spec, trial)
    case = next(c for c in spec["cases"] if c["id"] == trial["case_id"])
    admitted = admitted_input(spec, case, trial["arm"])
    with connect(manifest) as db:
        db.execute("BEGIN IMMEDIATE")
        records = read_records(db, manifest)
        if len(records) >= min(HARD_CALL_CAP, spec["call_cap"]):
            raise ValueError("campaign call cap exhausted; dispatch refused")
        if (
            sum(r["trial"]["stage"] == trial["stage"] for r in records)
            >= spec["stages"][trial["stage"]]
        ):
            raise ValueError("stage allocation exhausted; dispatch refused")
        record = {
            "attempt_id": str(uuid.uuid4()),
            "trial": trial,
            "admitted_authored": admitted["authored"],
            "request_config": admitted["config"],
            "provider_text": admitted["provider_text"],
            "state": "reserved",
            "result": None,
            "error": None,
        }
        ordinal = len(records) + 1
        db.execute(
            "INSERT INTO attempts VALUES (?, ?, ?)",
            (ordinal, canonical(record).decode(), digest(record)),
        )
        db.execute("UPDATE meta SET reserved = reserved + 1")
    case = next(c for c in spec["cases"] if c["id"] == trial["case_id"])
    request = {
        "attempt_id": record["attempt_id"],
        "trial": trial,
        "case": case,
        "admitted_authored": record["admitted_authored"],
        "request_config": record["request_config"],
        "provider_text": record["provider_text"],
        "provider": spec["providers"][trial["provider"]],
    }
    try:
        output = dispatcher(json.loads(canonical(request)))
        if set(output) != {*OUTPUT_STAGES, "usage"}:
            raise ValueError("dispatcher must separate all output stages and usage")
        for stage in OUTPUT_STAGES:
            if output[stage] is not None:
                thaw_bytes(output[stage]).decode("utf-8")
        canonical(output)
        record.update(state="completed", result=output)
    except Exception as exc:
        record.update(state="failed", error=type(exc).__name__)
        raise
    finally:
        with connect(manifest) as db:
            db.execute("BEGIN IMMEDIATE")
            read_records(db, manifest)
            db.execute(
                "UPDATE attempts SET payload = ?, sha256 = ? WHERE ordinal = ?",
                (canonical(record).decode(), digest(record), ordinal),
            )
    return record


def status(manifest_path: Path, *, report: bool = False) -> dict:
    manifest = load_manifest(manifest_path)
    with connect(manifest) as db:
        db.execute("BEGIN")
        records = read_records(db, manifest)
    result = {
        "campaign_id": CAMPAIGN_ID,
        "reserved_calls": len(records),
        "remaining_calls": min(HARD_CALL_CAP, manifest["spec"]["call_cap"]) - len(records),
        "states": {
            s: sum(r["state"] == s for r in records) for s in ("reserved", "completed", "failed")
        },
        "stages": {
            s: sum(r["trial"]["stage"] == s for r in records) for s in manifest["spec"]["stages"]
        },
        "records": records,
        "live_dispatch_enabled": False,
    }
    if report:
        result["calibration"] = calibration_report(report_input(manifest["spec"], records))
        result["calibration_placeholder"] = not any(
            r["state"] == "completed" and r["result"]["licensed_repaired"] is not None
            for r in records
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "status", "report"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--ledger", type=Path)
    args = parser.parse_args()
    if args.operation == "prepare":
        if args.spec is None or args.ledger is None:
            parser.error("prepare requires --spec and --ledger")
        result = prepare(args.spec, args.manifest, args.ledger)
    else:
        result = status(args.manifest, report=args.operation == "report")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
