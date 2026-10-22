"""Fake-only admission/auth publication checks; never import the real client."""

import ast
import contextlib
import fcntl
import io
import json
import os
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.sage_recovery import advance
from scripts.sage_recovery import recovery_checks as checks
from tests.operations import test_sage_recovery as fixtures

pytestmark = pytest.mark.usefixtures("recovery_settings")


class NativeBudgetTests(unittest.TestCase):
    azure = fixtures.ControllerTests.azure
    sleep = fixtures.ControllerTests.sleep
    publish = fixtures.ControllerTests.publish
    capture = fixtures.ControllerTests.capture
    verify = fixtures.ControllerTests.verify

    def setUp(self):
        fixtures.ControllerTests.setUp(self)
        self.controller.capture = types.MethodType(advance.Controller.capture, self.controller)
        self.controller._native_capture = self.native
        self.controller.run_id = "run"
        self.raw_calls = []

    def native(self, pod):
        self.raw_calls.append((self.at, pod))
        self.assertEqual(pod, self.pod)
        return b"fake native measurement", {
            "at": self.at.isoformat(),
            "replica": pod,
            "peak_process_rss_bytes": 1024**3,
            "peak_cgroup_bytes": 1024**3,
            "limit_bytes": 4 * 1024**3,
            "memory_limit_failures": 0,
            "cpu_quota": 2,
        }

    def test_all_explicit_sites_share_budget_and_can_reuse_sufficient_snapshot(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.controller.capture("first.txt", "old")
            self.controller.capture("same-phase.txt", "old")
            self.assertEqual(len(self.raw_calls), 1)
            self.sleep(1)
            self.controller.capture("pre-restart.txt", "old", minimum_time=self.at)
            self.assertEqual(len(self.raw_calls), 2)
            self.pod = "new"
            self.sleep(1)
            self.controller.capture("post-restart.txt", "new", minimum_time=self.at)
        self.assertEqual(len(self.raw_calls), 3)
        self.assertTrue(
            all(
                (b[0] - a[0]).total_seconds() >= 900
                for a, b in zip(self.raw_calls, self.raw_calls[1:])
            )
        )

    def test_new_controller_respects_persisted_budget(self):
        self.controller.capture("first.txt", "old")
        self.sleep(1)
        fresh = advance.Controller(
            self.controller.helper, self.root, self.azure, self.sleep, lambda: self.at
        )
        fresh.run_id = "run"
        fresh._native_capture = self.native
        with contextlib.redirect_stdout(io.StringIO()):
            fresh.capture("fresh-process.txt", "old")
        self.assertEqual(len(self.raw_calls), 2)
        self.assertGreaterEqual((self.raw_calls[1][0] - self.raw_calls[0][0]).total_seconds(), 900)

    def test_entire_run_obeys_admission_and_keeps_final_freshness(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.controller.run()
        self.assertTrue(
            all(
                (b[0] - a[0]).total_seconds() >= 900
                for a, b in zip(self.raw_calls, self.raw_calls[1:])
            )
        )
        window = json.loads((self.root / "window-validation.json").read_text())
        final = json.loads((self.root / "memory-after-stability-window.txt.json").read_text())
        self.assertGreaterEqual(
            checks.timestamp(final["started_at"]), checks.timestamp(window["end_utc"])
        )
        self.assertEqual(sum(a[:2] == ["revision", "restart"] for a in self.calls), 1)

    def test_retry_after_is_persisted_and_honored(self):
        normal = self.native
        count = 0

        def native(pod):
            nonlocal count
            count += 1
            if count == 1:
                self.raw_calls.append((self.at, pod))
                raise advance.NativeThrottled(1200)
            return normal(pod)

        self.controller._native_capture = native
        with contextlib.redirect_stdout(io.StringIO()):
            self.controller.capture("after429.txt", "old")
        self.assertGreaterEqual((self.raw_calls[1][0] - self.raw_calls[0][0]).total_seconds(), 1200)
        self.assertEqual(
            json.loads((self.root / "native-observation-throttle.json").read_text())[
                "retry_after_seconds"
            ],
            1200,
        )
        self.assertFalse(any(a[:2] == ["revision", "deactivate"] for a in self.calls))

    def test_repeated_throttle_pauses_without_deactivation_or_acceptance(self):
        count = 0

        def native(pod):
            nonlocal count
            count += 1
            raise advance.NativeThrottled(600)

        self.controller._native_capture = native
        self.controller.run = lambda: self.controller.capture("unknown.txt", "old")
        self.controller.stop = lambda reason: self.fail(
            "diagnostic error must not deactivate production"
        )
        with (
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(advance.ObservationUnavailable),
        ):
            advance.execute_controller(self.controller)
        self.assertEqual(count, 2)
        self.assertTrue((self.root / "verification-paused.json").exists())
        self.assertFalse((self.root / "stop").exists())
        self.assertFalse((self.root / "window-validation.json").exists())

    def test_generic_local_error_pauses_without_deactivation(self):
        def run():
            raise json.JSONDecodeError("fake cache race", "", 0)

        self.controller.run = run
        self.controller.stop = lambda reason: self.fail(
            "local error must not deactivate production"
        )
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(json.JSONDecodeError):
            advance.execute_controller(self.controller)
        self.assertTrue((self.root / "verification-paused.json").exists())

    def test_measured_safety_violation_still_stops(self):
        def run():
            raise checks.Unsafe("measured memory budget exceeded")

        stopped = []
        self.controller.run = run
        self.controller.stop = lambda reason: stopped.append(reason)
        advance.execute_controller(self.controller)
        self.assertEqual(stopped, ["measured memory budget exceeded"])
        self.assertFalse((self.root / "verification-paused.json").exists())

    def test_cache_cannot_bypass_memory_threshold(self):
        self.controller.capture("initial.txt", "old")
        data = json.loads((self.root / "native-capture-last.json").read_text())
        data["peak_process_rss_bytes"] = 4 * 1024**3
        checks.atomic(self.root / "native-capture-last.json", data)
        with self.assertRaises(checks.Unsafe):
            self.controller.capture("forbidden.txt", "old")


class NativeParserTests(unittest.TestCase):
    azure = fixtures.ControllerTests.azure
    sleep = fixtures.ControllerTests.sleep
    publish = fixtures.ControllerTests.publish
    capture = fixtures.ControllerTests.capture
    verify = fixtures.ControllerTests.verify

    def setUp(self):
        fixtures.ControllerTests.setUp(self)
        self.controller.identity = lambda expected: expected

    def parse(self, raw, code=0):
        proc = types.SimpleNamespace(poll=lambda: code, wait=lambda **kw: code)
        with (
            patch.object(advance.pty, "openpty", return_value=(101, 102)),
            patch.object(advance.os, "close"),
            patch.object(advance.os, "read", side_effect=[raw, b""]),
            patch.object(advance.select, "select", return_value=([101], [], [])),
            patch.object(advance.subprocess, "Popen", return_value=proc),
        ):
            return self.controller._native_capture("old")

    def raw(
        self,
        rss=1048576,
        peak=1073741824,
        limit=4294967296,
        failures=0,
        quota=200000,
        period=100000,
    ):
        return f"VmHWM: {rss} kB\n{peak}\n{limit}\n{failures}\n{quota}\n{period}\n".encode()

    def test_actual_cli_throttle_header_is_classified(self):
        with self.assertRaises(advance.NativeThrottled) as caught:
            self.parse(b"Handshake status 429 Too Many Requests - {'retry-after': '600'}", 1)
        self.assertEqual(caught.exception.retry_after, 600)

    def test_throttle_without_header_uses_conservative_backoff(self):
        with self.assertRaises(advance.NativeThrottled) as caught:
            self.parse(b"HTTP 429 Too Many Requests", 1)
        self.assertEqual(caught.exception.retry_after, 900)

    def test_unknown_exit_and_malformed_output_are_unavailable(self):
        for raw, code in [(b"diagnostic transport unavailable", 1), (b"partial", 0)]:
            with self.subTest(code=code), self.assertRaises(advance.ObservationUnavailable):
                self.parse(raw, code)

    def test_actual_measurements_still_enforce_each_safety_limit(self):
        cases = [
            dict(rss=4 * 1024**2),
            dict(peak=4 * 1024**3),
            dict(failures=1),
            dict(quota=400000),
            dict(limit=8 * 1024**3),
            dict(period=0),
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(checks.Unsafe):
                self.parse(self.raw(**case))
            self.assertTrue((self.root / "native-measurement-latest.json").is_file())

    def test_valid_native_measurement_keeps_replica_and_limits(self):
        raw, metrics = self.parse(self.raw())
        self.assertEqual(raw, self.raw())
        self.assertEqual(metrics["replica"], "old")
        self.assertEqual(metrics["cpu_quota"], 2)


class AuthPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.token = root / "fake-cache.json"
        self.config = root / "fake-config.json"
        self.token.write_text(json.dumps({"expires_on": 0, "accessToken": "fake-old"}))
        self.config.write_text(json.dumps({"sage-audience": "fake-audience"}))
        self.refreshes = 0
        from scripts.sage_recovery import client

        helper = Path(client.__file__)
        tree = ast.parse(helper.read_text())
        nodes = [
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in {"token", "_publish_token"}
        ]
        code = compile(ast.Module(body=nodes, type_ignores=[]), "<fake credential cache>", "exec")
        self.env = {
            "TOKEN": self.token,
            "CONFIG": self.config,
            "Path": Path,
            "settings": lambda: types.SimpleNamespace(
                token_file=self.token, audience="fake-audience"
            ),
            "json": json,
            "os": os,
            "fcntl": fcntl,
            "tempfile": tempfile,
            "time": types.SimpleNamespace(time=lambda: 1000),
            "subprocess": types.SimpleNamespace(check_output=self.refresh),
            "_TOKEN_LOCK": threading.RLock(),
        }
        exec(code, self.env)  # noqa: S102 - exact shipped AST with fake credentials only

    def refresh(self, *args, **kwargs):
        self.refreshes += 1
        return json.dumps({"expires_on": 9999, "accessToken": "fake-new"})

    def test_atomic_publication_and_two_refreshers(self):
        entered = threading.Event()
        release = threading.Event()
        second_attempt = threading.Event()
        errors = []
        values = []
        real_dump = json.dump

        def dump(data, stream):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test release missing")
            return real_dump(data, stream)

        class Lock:
            def __init__(self):
                self.lock = threading.RLock()

            def __enter__(self):
                if threading.current_thread().name == "second":
                    second_attempt.set()
                self.lock.acquire()

            def __exit__(self, *args):
                self.lock.release()

        self.env["_TOKEN_LOCK"] = Lock()
        self.env["json"] = types.SimpleNamespace(loads=json.loads, dump=dump)

        def call():
            try:
                values.append(self.env["token"]())
            except Exception as error:
                errors.append(type(error).__name__)

        first = threading.Thread(target=call, name="first")
        second = threading.Thread(target=call, name="second")
        first.start()
        self.assertTrue(entered.wait(5))
        self.assertEqual(json.loads(self.token.read_text())["accessToken"], "fake-old")
        second.start()
        self.assertTrue(second_attempt.wait(5))
        release.set()
        first.join(5)
        second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(values, ["fake-new", "fake-new"])
        self.assertEqual(self.refreshes, 1)
        self.assertEqual(self.token.stat().st_mode & 0o777, 0o600)

    def test_file_lock_covers_reads_across_independent_openers(self):
        self.token.write_text(json.dumps({"expires_on": 9999, "accessToken": "fake-new"}))
        lock_path = self.token.with_name(self.token.name + ".lock")
        held = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(held, fcntl.LOCK_EX)
        attempted = threading.Event()
        read = []
        errors = []
        original_flock = fcntl.flock

        def flock(fd, flags):
            attempted.set()
            return original_flock(fd, flags)

        self.env["fcntl"] = types.SimpleNamespace(flock=flock, LOCK_EX=fcntl.LOCK_EX)

        def call():
            try:
                read.append(self.env["token"]())
            except Exception as error:
                errors.append(type(error).__name__)

        worker = threading.Thread(target=call)
        worker.start()
        self.assertTrue(attempted.wait(5))
        self.assertEqual(read, [])
        original_flock(held, fcntl.LOCK_UN)
        os.close(held)
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(read, ["fake-new"])
        self.assertEqual(self.refreshes, 0)

    def test_invalid_refresh_never_replaces_prior_cache(self):
        self.env["subprocess"] = types.SimpleNamespace(check_output=lambda *a, **k: "{}")
        with self.assertRaises(RuntimeError):
            self.env["token"]()
        self.assertEqual(json.loads(self.token.read_text())["accessToken"], "fake-old")


if __name__ == "__main__":
    unittest.main()
