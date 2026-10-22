"""Copy an explicit nonsecret evidence allowlist; never infer live success."""

import json
import shutil

from .config import settings
from .recovery_checks import atomic, utc

ALLOW = [
    "health.json",
    "inventory.json",
    "config.json",
    "initial-backlog.json",
    "backlog-ids.json",
    "backlog-latest.json",
    "samples.jsonl",
    "cycles.jsonl",
    "monitor-run.json",
    "monitor-latest.json",
    "six-source-integrity.json",
    "structured-catalog.json",
    "metrics-latest.json",
    "recovery-events.json",
    "process-cpu-initial.txt",
    "window.json",
    "window-validation.json",
    "six-projection-search.json",
    "final-summary.json",
    "final-report.md",
    "authorization.json",
    "advance-state.json",
    "input-workload.json",
    "runtime-code-identity.txt",
    "image-metadata.json",
    "mcp-ordinary.json",
    "mcp-maintenance.json",
    "restart-issued.json",
    "restart-receipt.json",
    "postrestart-replicas.json",
    "postrestart-documents.json",
    "postrestart-health.json",
    "allocation-before-restart.json",
    "allocation-after-restart.json",
    "allocation-final.json",
    "replicas-final.json",
    "replicas-latest.json",
    "memory-before-controlled-restart.txt",
    "memory-before-controlled-restart.txt.json",
    "memory-after-stability-window.txt",
    "memory-after-stability-window.txt.json",
    "memory-after-restart.txt",
    "memory-after-restart.txt.json",
    "memory-latest.txt",
    "memory-latest.txt.json",
    "native-capture-budget.json",
    "native-capture-last.json",
    "native-capture-last.txt",
    "native-measurement-latest.json",
    "native-observation-throttle.json",
    "verification-paused.json",
    "stop-receipt.json",
    "stop-unconfirmed.json",
    "unattended-approval-checkpoint.json",
]


def evidence_status(path):
    if not path.is_file():
        return {"status": "unverified", "reason": "missing artifact"}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        return {"status": "unverified", "reason": "legacy artifact requires reconciliation"}
    rows = data.get("records", [])
    ids = [row.get("document_id") for row in rows]
    valid = (
        data.get("complete") is True
        and data.get("manifest_sha256") == settings().manifest_sha256
        and len(ids) == 6
        and set(ids) == set(settings().six_ids)
    )
    return {
        "status": "complete recorded evidence" if valid else "unverified",
        "recorded_at": data.get("at"),
        "scope": "saved evidence only; not live readback",
    }


def export(root=None, out=None):
    root = root or settings().root
    out = out or settings().evidence_out
    out.mkdir(exist_ok=True)
    for name in ALLOW:
        if (root / name).is_file():
            shutil.copyfile(root / name, out / name)
            (out / name).chmod(0o600)
    state = {
        "acceptance_state": "owner review required",
        "exported_at": utc().isoformat(),
        "source_integrity": evidence_status(root / "six-source-integrity.json"),
        "projections": evidence_status(root / "six-projection-search.json"),
        "stability": (
            "No acceptance claim: inspect complete paired window and controller evidence; "
            "perform independent final readback"
        ),
        "production_state": "Not queried by this exporter",
    }
    atomic(out / "state.json", state)
    return state


if __name__ == "__main__":
    print(json.dumps(export()))
