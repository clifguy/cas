"""Describe current run evidence without treating absent or historical rows as current."""

import json

from .config import settings
from .recovery_checks import timestamp, utc


def summarize(root=None, now=None):
    root = root or settings().root
    now = now or utc()

    def read(name):
        path = root / name
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except ValueError, OSError:
            return None

    manifest = read("monitor-run.json") or {}
    run_id = manifest.get("run_id")

    def current(name, maximum_age=60):
        value = read(name)
        if not run_id or not isinstance(value, dict) or value.get("run_id") != run_id:
            return None
        try:
            age = (now - timestamp(value["at"])).total_seconds()
        except ValueError, KeyError, TypeError:
            return None
        return value if 0 <= age <= maximum_age else None

    snapshot = current("monitor-latest.json", 45)
    backlog = current("backlog-latest.json")
    replicas = current("replicas-latest.json")
    states = {}
    if backlog:
        for document in backlog["documents"]:
            state = document["pipeline_status"]
            states[state] = states.get(state, 0) + 1
    requests = snapshot["requests"] if snapshot else []
    semantic = [row["seconds"] for row in requests if row["label"] == "semantic"]
    return {
        "at": now.isoformat(),
        "run_id": run_id,
        "observer": "fresh" if snapshot else "unverified or stale",
        "latest_cycle_requests": len(requests),
        "latest_cycle_errors": [row["label"] for row in requests if not row["ok"]],
        "latest_semantic_seconds": semantic[0] if semantic else None,
        "backlog": states if backlog else None,
        "backlog_observed_at": backlog["at"] if backlog else None,
        "replicas": replicas["replicas"] if replicas else None,
        "replicas_observed_at": replicas["at"] if replicas else None,
        "acceptance": (
            "No acceptance claim; inspect final window-validation and controller evidence"
        ),
    }


if __name__ == "__main__":
    print(json.dumps(summarize(), indent=2))
