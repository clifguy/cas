"""Read Azure minute metrics; absent datapoints never establish a zero count."""

import datetime as dt
import json
import subprocess

from .config import settings
from .recovery_checks import atomic, timestamp


def main(start_time):
    cfg = settings()
    timestamp(start_time)
    resource = (
        f"/subscriptions/{cfg.subscription}/resourceGroups/{cfg.resource_group}"
        f"/providers/Microsoft.App/containerApps/{cfg.app}"
    )
    end = dt.datetime.now(dt.timezone.utc).isoformat()
    result = subprocess.run(
        [
            "az",
            "monitor",
            "metrics",
            "list",
            "--resource",
            resource,
            "--metric",
            "UsageNanoCores",
            "WorkingSetBytes",
            "RestartCount",
            "Replicas",
            "--aggregation",
            "Average",
            "Maximum",
            "--interval",
            "PT1M",
            "--start-time",
            start_time,
            "--end-time",
            end,
            "--filter",
            f"revisionName eq '{cfg.revision}'",
            "--output",
            "json",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=90,
    )
    data = json.loads(result.stdout)
    atomic(cfg.root / "metrics-latest.json", data)
    for metric in data["value"]:
        values = [
            x["maximum"]
            for series in metric["timeseries"]
            for x in series["data"]
            if x.get("maximum") is not None
        ]
        print(metric["name"]["value"], max(values) if values else "no datapoints", len(values))
