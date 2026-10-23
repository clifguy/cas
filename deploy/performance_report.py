"""Produce a conservative report from retained Azure measurements, including gaps."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path


def summarize(output: Path, plan: dict) -> dict:
    records = []
    gaps = []
    for path in sorted((output / "cloud-evidence" / plan["run_id"]).glob("samples-*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                gaps.append("Incomplete record in " + path.name)
    benchmarks = defaultdict(list)
    traces, samples, identities = [], [], []
    for row in records:
        kind = row.get("kind")
        if kind == "benchmark_record":
            case, record = row["case"], row["record"]
            if record.get("outcome") == "completed" and "texts_per_second" in record:
                key = json.dumps(case, sort_keys=True)
                benchmarks[key].append(record)
            if record.get("kind") == "identity":
                identities.append({"case": case, **record})
        elif kind == "stage_record":
            traces.append(row.get("trace", row.get("record", {})))
        elif kind == "process_sample":
            samples.append(row)
    matrix = []
    for key, rows in sorted(benchmarks.items()):
        matrix.append(
            {
                "case": json.loads(key),
                "repetitions": len(rows),
                "median_texts_per_second": statistics.median(x["texts_per_second"] for x in rows),
                "median_tokens_per_second": statistics.median(
                    x["tokens_per_second"] for x in rows if x.get("tokens_per_second") is not None
                )
                if any(x.get("tokens_per_second") is not None for x in rows)
                else None,
                "maximum_process_peak_bytes": max(
                    x["resources_after"]["process_peak_bytes"] for x in rows
                ),
            }
        )
    arms = []
    for path in sorted(output.glob("arm-*-service.jsonl")):
        rows = [json.loads(x) for x in path.read_text().splitlines()]
        arms.append(
            {
                "file": path.name,
                "summary": [x for x in rows if x["kind"] == "summary"],
                "sources": [x for x in rows if x["kind"] in ("witness", "completed_source")],
                "failures": [x for x in rows if x["kind"] == "failure"],
            }
        )
    if not matrix:
        gaps.append("No completed Azure microbenchmark repetitions")
    if not traces:
        gaps.append("No retained stage traces")
    if not samples:
        gaps.append("No process/cgroup resource samples")
    if not (output / "service-controls.json").exists():
        gaps.append("Fault and recovery controls incomplete")
    reconciliation = output / "cloud-evidence" / plan["run_id"] / "reconciliation.json"
    if not reconciliation.exists():
        gaps.append("Source, passage and vector reconciliation incomplete")
    if (output / "pruned-matrix.json").exists():
        gaps.append("Matrix pruned to preserve the approved expiry/export window")
    return {
        "run_id": plan["run_id"],
        "baseline": plan["candidate"],
        "arms": arms,
        "microbenchmarks": matrix,
        "model_identities": identities,
        "stage_traces": traces,
        "resource_sample_count": len(samples),
        "gaps": gaps,
        "reconciliation": json.loads(reconciliation.read_text())
        if reconciliation.exists()
        else None,
        "interpretation": [
            "Stage totals overlap; do not sum them into elapsed latency.",
            "Queue wait and worker execution have separate clocks.",
            "Consumption 2 CPU/4 GiB versus D8 includes a hosting and memory difference.",
            "Only D8 2 versus 4 CPU holds hosting and memory fixed.",
            "Service overhead pairs disable abstraction; report separately from baseline arms.",
            "Missing or empty Azure metric series are unknown, never zero.",
            "Individual trial arms and three repetitions do not establish a production SLO.",
            "Batch throughput does not prove embedding caused the original deployment delay.",
        ],
    }


def write_report(output: Path, plan: dict) -> dict:
    report = summarize(output, plan)
    (output / "report.json").write_text(json.dumps(report, indent=2))
    lines = [
        "# T-0761 Azure measurements",
        "",
        "Baseline: `" + plan["candidate"]["image"] + "`.",
        "",
        "| Case | Repetitions | Median texts/s | Peak process MiB |",
        "|---|---:|---:|---:|",
    ]
    for row in report["microbenchmarks"]:
        lines.append(
            "| "
            + json.dumps(row["case"], sort_keys=True)
            + " | "
            + str(row["repetitions"])
            + " | "
            + f"{row['median_texts_per_second']:.3f} | "
            + f"{row['maximum_process_peak_bytes'] / 1024**2:.1f} |"
        )
    lines += ["", "Interpretation and limitations:", ""]
    lines += ["- " + x for x in report["interpretation"]]
    lines += ["", "Missing evidence:", ""]
    lines += ["- " + x for x in report["gaps"]] or ["- None detected by this report."]
    lines += [
        "",
        "Raw arm summaries, per-operation stage clocks, CPU/memory samples, "
        "Azure metrics, source hashes and reconciliation are retained beside this report. "
        "Reproduce through the manual Azure ingestion performance workflow using the "
        "retained exact specification, digest and a fresh run identity.",
        "",
    ]
    (output / "report.md").write_text("\n".join(lines))
    return report
