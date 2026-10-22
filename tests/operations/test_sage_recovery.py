"""Hermetic recovery regression checks: no real client or CLI is invoked."""

import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.sage_recovery import advance, monitor, progress, save_evidence
from scripts.sage_recovery import recovery_checks as checks
from scripts.sage_recovery.config import settings

pytestmark = pytest.mark.usefixtures("recovery_settings")

NOW = dt.datetime(2026, 10, 21, 23, tzinfo=dt.timezone.utc)


def request(label, at=NOW, **changes):
    row = {
        "run_id": "run",
        "label": label,
        "at": at.isoformat(),
        "started_at": at.isoformat(),
        "ok": True,
        "seconds": 1,
    }
    if label in {"semantic", "keyword"}:
        row["matched_document"] = settings().indexed_document
    row.update(changes)
    return row


def cycle(at=NOW):
    return {
        "run_id": "run",
        "at": at.isoformat(),
        "requests": [request(label, at) for label in sorted(checks.LABELS)],
    }


def replica(name="old", **changes):
    container = {"name": "sage", "ready": True, "runningState": "Running", "restartCount": 0}
    container.update(changes)
    return [{"name": name, "properties": {"containers": [container]}}]


def documents():
    return [
        {
            "id": i,
            "lifecycle_status": "active",
            "pipeline_status": "abstraction_complete",
            "pipeline_error": None,
        }
        for i in settings().expected_ids
    ]


class CheckTests(unittest.TestCase):
    def test_backlog_exact_control(self):
        self.assertTrue(checks.check_backlog(documents(), complete=True))

    def test_backlog_rejects_missing_extra_duplicate_and_wrong(self):
        docs = documents()
        for value in [
            docs[:1],
            docs[:-1],
            docs + [docs[0]],
            docs[:-1] + [docs[0]],
            docs[:-1] + [dict(docs[-1], id="wrong")],
        ]:
            with self.subTest(length=len(value)), self.assertRaises(checks.Unsafe):
                checks.check_backlog(value, complete=True)

    def test_backlog_rejects_errors_and_archival(self):
        for changes in [
            {"pipeline_error": "bad"},
            {"pipeline_status": "abstraction_interrupted"},
            {"lifecycle_status": "archived"},
        ]:
            docs = documents()
            docs[0].update(changes)
            with self.assertRaises(checks.Unsafe):
                checks.check_backlog(docs, complete=True)

    def test_replica_control(self):
        self.assertEqual(checks.check_replica(replica(), "old"), "old")

    def test_replica_absent_replaced_unready_restarted_empty(self):
        for rows in [
            [],
            replica("new"),
            replica(ready=False),
            replica(restartCount=1),
            [{"name": "old", "properties": {"containers": []}}],
            replica() + replica(),
        ]:
            with self.subTest(rows=rows), self.assertRaises(checks.Unsafe):
                checks.check_replica(rows, "old")

    def test_probe_positive_control(self):
        for label in ["semantic", "keyword"]:
            self.assertTrue(
                checks.probe_ok(
                    label,
                    {
                        "status": 200,
                        "body": {"results": [{"document": {"id": settings().indexed_document}}]},
                    },
                )
            )

    def test_probe_empty_wrong_and_error(self):
        for label in ["semantic", "keyword"]:
            for response in [
                {"status": 200, "body": {"results": []}},
                {"status": 200, "body": {"results": [{"document": {"id": "wrong"}}]}},
                {"status": 503, "body": {}},
            ]:
                self.assertFalse(checks.probe_ok(label, response))

    def test_snapshot_complete_control(self):
        self.assertEqual(len(checks.check_snapshot(cycle(), "run", NOW)), 6)

    def test_snapshot_stale_wrong_run_missing_label_future(self):
        stale = cycle(NOW - dt.timedelta(seconds=46))
        wrong = cycle()
        wrong["run_id"] = "other"
        missing = cycle()
        missing["requests"].pop()
        for value in [stale, wrong, missing, cycle(NOW + dt.timedelta(seconds=1))]:
            with self.assertRaises(checks.Unsafe):
                checks.check_snapshot(value, "run", NOW)

    def window(self):
        return [cycle(NOW + dt.timedelta(seconds=x)) for x in range(0, 1801, 10)]

    def test_window_full_control(self):
        result = checks.check_window(self.window(), "run", NOW, NOW + dt.timedelta(minutes=30))
        self.assertEqual(result["cycles"], 181)

    def test_window_empty_stale_gap_wrong_run(self):
        rows = self.window()
        for value in [
            [],
            rows[:4],
            rows[:10] + rows[20:],
            [dict(row, run_id="other") for row in rows],
        ]:
            with self.assertRaises(checks.Unsafe):
                checks.check_window(value, "run", NOW, NOW + dt.timedelta(minutes=30))

    def test_window_single_failure_or_slow_not_hidden_by_recovery(self):
        for change in [{"ok": False}, {"seconds": 6}, {"matched_document": "wrong"}]:
            rows = self.window()
            row = next(r for r in rows[30]["requests"] if r["label"] == "semantic")
            row.update(change)
            with self.assertRaises(checks.Unsafe):
                checks.check_window(rows, "run", NOW, NOW + dt.timedelta(minutes=30))

    def test_window_light_p95_budget(self):
        rows = self.window()
        for c in rows:
            next(r for r in c["requests"] if r["label"] == "document")["seconds"] = 3
        with self.assertRaises(checks.Unsafe):
            checks.check_window(rows, "run", NOW, NOW + dt.timedelta(minutes=30))

    def test_window_inflight_at_end_failure(self):
        rows = self.window()
        row = rows[-1]["requests"][0]
        row["at"] = (NOW + dt.timedelta(seconds=1805)).isoformat()
        row["ok"] = False
        with self.assertRaises(checks.Unsafe):
            checks.check_window(rows, "run", NOW, NOW + dt.timedelta(minutes=30))

    def test_manifest_rejects_truncated_or_changed_data(self):
        for data in [b"[]", b'[{"filename":"only.csv"}]']:
            with (
                patch.object(Path, "read_bytes", return_value=data),
                self.assertRaises(checks.Unsafe),
            ):
                checks.originals()

    def test_export_missing_is_not_verified(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "in"
            out = Path(temp) / "out"
            root.mkdir()
            result = save_evidence.export(root, out)
            self.assertEqual(result["source_integrity"]["status"], "unverified")
            self.assertEqual(result["production_state"], "Not queried by this exporter")

    def test_export_truncated_is_not_verified(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / "evidence.json"
            checks.atomic(
                p,
                {
                    "complete": True,
                    "manifest_sha256": settings().manifest_sha256,
                    "records": [{"document_id": settings().six_ids[0]}],
                },
            )
            self.assertEqual(save_evidence.evidence_status(p)["status"], "unverified")

    def test_probe_latency_includes_auth_refresh(self):
        class Helper:
            def request(self, *args, **kwargs):
                return {"status": 200, "seconds": 1, "body": {}}

        with patch.object(monitor.time, "monotonic", side_effect=[100, 107]):
            row = monitor.probe(Helper(), "run", "health", "/health", "GET", None)
        self.assertEqual(row["seconds"], 7)
        self.assertEqual(row["http_seconds"], 1)
        with self.assertRaises(checks.Unsafe):
            checks.check_sample(row)

    def test_progress_empty_reports_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            result = progress.summarize(Path(temp), NOW)
            self.assertEqual(result["observer"], "unverified or stale")
            self.assertIsNone(result["backlog"])
            self.assertIsNone(result["replicas"])

    def test_progress_reads_new_schema_and_ignores_old_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            checks.atomic(root / "monitor-run.json", {"run_id": "run"})
            checks.atomic(root / "monitor-latest.json", cycle())
            checks.atomic(
                root / "backlog-latest.json",
                {"at": NOW.isoformat(), "run_id": "run", "documents": documents()},
            )
            checks.atomic(
                root / "replicas-latest.json",
                {"at": NOW.isoformat(), "run_id": "run", "replicas": replica("current")},
            )
            (root / "samples.jsonl").write_text('{"kind":"replicas","replicas":["stale"]}\n')
            result = progress.summarize(root, NOW)
            self.assertEqual(result["backlog"], {"abstraction_complete": 16})
            self.assertEqual(result["replicas"][0]["name"], "current")
            stale = progress.summarize(root, NOW + dt.timedelta(seconds=61))
            self.assertIsNone(stale["backlog"])
            self.assertIsNone(stale["replicas"])

    def test_progress_wrong_run_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            checks.atomic(root / "monitor-run.json", {"run_id": "other"})
            checks.atomic(root / "monitor-latest.json", cycle())
            result = progress.summarize(root, NOW)
            self.assertEqual(result["observer"], "unverified or stale")

    def test_import_is_no_network_or_cli(self):
        import importlib

        with (
            patch("subprocess.run", side_effect=AssertionError("CLI invoked")),
            patch(
                "importlib.util.spec_from_file_location",
                side_effect=AssertionError("client imported"),
            ),
        ):
            importlib.reload(advance)
            importlib.reload(monitor)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.at = NOW
        self.pod = "old"
        self.calls = []
        self.source_checks = 0
        checks.atomic(self.root / "monitor-run.json", {"run_id": "run"})
        self.publish()

        class Helper:
            def request(self, path, timeout=20):
                i = path.rsplit("/", 1)[-1]
                return {"status": 200, "body": next(d for d in documents() if d["id"] == i)}

        self.controller = advance.Controller(
            Helper(), self.root, self.azure, self.sleep, lambda: self.at
        )
        self.controller.capture = self.capture
        self.controller.verify = self.verify

    def publish(self):
        value = cycle(self.at)
        checks.atomic(self.root / "monitor-latest.json", value)
        with (self.root / "cycles.jsonl").open("a") as f:
            f.write(json.dumps(value) + "\n")

    def sleep(self, seconds):
        self.at += dt.timedelta(seconds=seconds)
        self.publish()

    def capture(self, name, pod, minimum_time=None):
        self.assertEqual(self.pod, pod)
        self.controller.last_memory = self.at

    def verify(self):
        self.source_checks += 1
        checks.check_backlog(self.controller.backlog(), complete=True)

    def azure(self, args):
        self.calls.append(args)
        template = {
            "containers": [
                {
                    "name": "sage",
                    "image": settings().image,
                    "resources": {"cpu": 2, "memory": "4Gi"},
                }
            ]
        }
        if args == ["show"]:
            return {"properties": {"latestRevisionName": settings().revision, "template": template}}
        if args[:2] == ["revision", "show"]:
            return {
                "name": settings().revision,
                "properties": {"active": True, "template": template},
            }
        if args[:2] == ["replica", "list"]:
            return replica(self.pod)
        if args[:2] == ["revision", "restart"]:
            self.pod = "new"
            return None
        raise AssertionError("Unexpected command " + repr(args))

    def test_guard_respects_native_capture_cooldown(self):
        self.controller.run_id = "run"
        self.controller.last_memory = self.at
        calls = []
        normal = self.capture

        def capture(name, pod):
            calls.append(self.at)
            normal(name, pod)

        self.controller.capture = capture
        self.sleep(60)
        self.controller.guard("old")
        self.assertEqual(calls, [])
        self.sleep(839)
        self.controller.guard("old")
        self.assertEqual(calls, [])
        self.sleep(1)
        self.controller.guard("old")
        self.assertEqual(len(calls), 1)
        self.sleep(60)
        self.controller.guard("old")
        self.assertEqual(len(calls), 1)

    def test_full_simulation_exactly_one_restart_then_true_window(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.controller.run()
        self.assertEqual(sum(args[:2] == ["revision", "restart"] for args in self.calls), 1)
        result = json.loads((self.root / "window-validation.json").read_text())
        self.assertGreaterEqual(
            (
                checks.timestamp(result["end_utc"]) - checks.timestamp(result["start_utc"])
            ).total_seconds(),
            1800,
        )
        self.assertEqual(self.source_checks, 2)
        self.assertEqual(
            json.loads((self.root / "advance-state.json").read_text())["phase"],
            "stability_window_elapsed",
        )

    def test_existing_intent_never_replays(self):
        (self.root / "restart-issued.json").write_text("{}")
        with self.assertRaises(checks.Unsafe):
            self.controller.run()
        self.assertFalse(any(args[:2] == ["revision", "restart"] for args in self.calls))

    def test_missing_monitor_never_restarts(self):
        (self.root / "monitor-latest.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.controller.run()
        self.assertFalse(any(args[:2] == ["revision", "restart"] for args in self.calls))

    def test_replacement_during_verification_never_restarts(self):
        def changed():
            self.pod = "replacement"

        self.controller.verify = changed
        with (
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises((checks.Unsafe, AssertionError)),
        ):
            self.controller.run()
        self.assertFalse(any(args[:2] == ["revision", "restart"] for args in self.calls))

    def test_failed_window_never_finishes(self):
        normal = self.sleep

        def failure(seconds):
            normal(seconds)
            if (self.root / "window.json").exists():
                value = cycle(self.at)
                value["requests"][0]["ok"] = False
                checks.atomic(self.root / "monitor-latest.json", value)

        self.controller.sleep = failure
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(checks.Unsafe):
            self.controller.run()
        self.assertFalse((self.root / "window-validation.json").exists())

    def test_inflight_boundary_failure_never_finishes(self):
        normal_sleep = self.sleep
        normal_verify = self.verify
        delayed = []
        published = False

        def publish_failure():
            nonlocal published
            self.at += dt.timedelta(seconds=3)
            checks.atomic(self.root / "monitor-latest.json", delayed[0])
            with (self.root / "cycles.jsonl").open("a") as stream:
                stream.write(json.dumps(delayed[0]) + "\n")
            published = True
            normal_sleep(10)  # A later good snapshot must not hide the failure.

        def sleep(seconds):
            window = self.root / "window.json"
            if window.exists():
                end = checks.timestamp(json.loads(window.read_text())["start_utc"]) + dt.timedelta(
                    minutes=30
                )
                if self.at + dt.timedelta(seconds=seconds) >= end and not delayed:
                    self.at = end
                    value = cycle(end - dt.timedelta(seconds=1))
                    next(row for row in value["requests"] if row["label"] == "semantic").update(
                        ok=False, at=(end + dt.timedelta(seconds=2)).isoformat()
                    )
                    delayed.append(value)
                    return
                if delayed and not published:
                    publish_failure()
                    return
            normal_sleep(seconds)

        def verify():
            normal_verify()
            # This fallback reproduces the original bug if the boundary wait is removed.
            if self.source_checks == 2 and delayed and not published:
                publish_failure()

        self.controller.sleep = sleep
        self.controller.verify = verify
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(checks.Unsafe):
            self.controller.run()
        self.assertFalse((self.root / "window-validation.json").exists())

    def test_final_replica_drift_never_finishes(self):
        normal = self.capture

        def changed(name, pod, minimum_time=None):
            normal(name, pod)
            if name == "memory-after-stability-window.txt":
                self.pod = "unexpected"

        self.controller.capture = changed
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(checks.Unsafe):
            self.controller.run()
        self.assertFalse((self.root / "window-validation.json").exists())


if __name__ == "__main__":
    unittest.main()
