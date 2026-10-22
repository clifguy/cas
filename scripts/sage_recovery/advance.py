"""Guarded recovery controller. Explicit execution only; import is side-effect free."""

import concurrent.futures
import datetime as dt
import fcntl
import json
import os
import pty
import re
import select
import subprocess
import sys
import time

from .config import settings
from .recovery_checks import (
    Unsafe,
    atomic,
    check_backlog,
    check_replica,
    check_sample,
    check_snapshot,
    check_window,
    require,
    timestamp,
    utc,
)


def common():
    cfg = settings()
    return [
        "--name",
        cfg.app,
        "--resource-group",
        cfg.resource_group,
        "--subscription",
        cfg.subscription,
    ]


# Azure exec is a diagnostic connection with a service-side throttle.
NATIVE_CAPTURE_INTERVAL_SECONDS = 15 * 60


class ObservationUnavailable(RuntimeError):
    """Observation failed; never interpret it as a measured server failure."""


class NativeThrottled(ObservationUnavailable):
    def __init__(self, retry_after):
        super().__init__("Azure native observation throttled")
        self.retry_after = retry_after


def az(args):
    try:
        result = subprocess.run(
            ["az", "containerapp"] + args + common() + ["--output", "json"],
            capture_output=True,
            text=True,
            timeout=90,
            check=True,
        )
        return json.loads(result.stdout) if result.stdout.strip() else None
    except (subprocess.SubprocessError, ValueError, OSError) as error:
        raise ObservationUnavailable(
            "Azure command outcome unavailable; reconcile mutations before retry"
        ) from error


def client():
    from . import client as helper

    return helper


class Controller:
    def __init__(self, helper, root=None, azure=az, sleep=time.sleep, clock=utc):
        root = root or settings().root
        self.helper, self.root, self.azure, self.sleep, self.clock = (
            helper,
            root,
            azure,
            sleep,
            clock,
        )
        self.run_id = None
        self.original = None
        self.last_memory = None
        self.started_at = self.clock()

    def save(self, name, value):
        atomic(self.root / name, value)

    def event(self, phase, **fields):
        value = {"at": self.clock().isoformat(), "phase": phase, "run_id": self.run_id, **fields}
        self.save("advance-state.json", value)
        print(json.dumps(value), flush=True)

    def replicas(self):
        return self.azure(["replica", "list", "--revision", settings().revision])

    def allocation(self):
        app = self.azure(["show"])
        require(
            app["properties"]["latestRevisionName"] == settings().revision, "latest revision drift"
        )
        revision = self.azure(["revision", "show", "--revision", settings().revision])
        require(
            revision["name"] == settings().revision
            and revision["properties"].get("active") is True,
            "target revision inactive or changed",
        )
        for template in [app["properties"]["template"], revision["properties"]["template"]]:
            containers = template["containers"]
            require(len(containers) == 1, "allocation container population changed")
            container = containers[0]
            require(
                container["name"] == "sage" and container["image"] == settings().image,
                "container or image changed",
            )
            require(
                container["resources"]["cpu"] == 2 and container["resources"]["memory"] == "4Gi",
                "allocation changed",
            )
        return {
            "revision": settings().revision,
            "image": settings().image,
            "resources": containers[0]["resources"],
        }

    def identity(self, expected=None):
        rows = self.replicas()
        name = check_replica(rows, expected)
        require(
            rows[0]["properties"]["containers"][0].get("name") == "sage",
            "unexpected container name",
        )
        self.save(
            "replicas-latest.json",
            {"at": self.clock().isoformat(), "run_id": self.run_id, "replicas": rows},
        )
        return name

    def backlog(self):
        def get(document_id):
            result = self.helper.request(
                f"/sage_vaults/{settings().vault_id}/documents/" + document_id, timeout=20
            )
            require(result["status"] == 200, "backlog read failed")
            require(result["body"]["id"] == document_id, "backlog response identity mismatch")
            return result["body"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            docs = list(pool.map(get, settings().expected_ids))
        check_backlog(docs)
        self.save(
            "backlog-latest.json",
            {"at": self.clock().isoformat(), "run_id": self.run_id, "documents": docs},
        )
        return docs

    def observer(self, strict=False, allow_failures=False):
        require(not (self.root / "stop").exists(), "stop marker is set")
        manifest = json.loads((self.root / "monitor-run.json").read_text())
        require(manifest["run_id"] == self.run_id, "observer changed")
        snapshot = json.loads((self.root / "monitor-latest.json").read_text())
        rows = check_snapshot(snapshot, self.run_id, self.clock())
        if strict:
            for row in rows:
                check_sample(row)
        elif not allow_failures:
            cycles = self.cycles()
            for label in {x["label"] for x in rows}:
                last = [x for c in cycles for x in c["requests"] if x["label"] == label][-3:]
                require(
                    not (len(last) == 3 and all(not x["ok"] for x in last)),
                    "three consecutive failed " + label + " probes",
                )
        return snapshot

    def cycles(self):
        # Writer commits one complete JSON line; a concurrently partial trailing line
        # is ignored here but can never be promoted into a complete cycle.
        text = (self.root / "cycles.jsonl").read_text()
        lines = text.splitlines()
        if text and not text.endswith("\n"):
            lines = lines[:-1]
        return [value for line in lines if (value := json.loads(line)).get("run_id") == self.run_id]

    def _observation_wait_checks(self, expected):
        self.allocation()
        self.identity(expected)
        self.observer(strict=(self.root / "window.json").exists())

    def capture(self, filename, expected, minimum_time=None):
        """Every native call shares one persisted, app-scoped admission budget."""
        minimum_time = minimum_time or self.started_at
        budget_path = self.root / "native-capture-budget.json"
        cache_path = self.root / "native-capture-last.json"
        scope = settings().native_scope
        deadline = self.clock() + dt.timedelta(minutes=45)
        with (self.root / "native-capture.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ObservationUnavailable("another native observation owns admission") from error
            for attempt in range(2):
                self._observation_wait_checks(expected)
                if cache_path.exists():
                    cached = json.loads(cache_path.read_text())
                    if (
                        cached.get("scope") == scope
                        and cached.get("run_id") == self.run_id
                        and cached.get("replica") == expected
                        and minimum_time
                        <= timestamp(cached["started_at"])
                        <= timestamp(cached["at"])
                        <= self.clock()
                        and (self.clock() - timestamp(cached["at"])).total_seconds()
                        <= NATIVE_CAPTURE_INTERVAL_SECONDS
                    ):
                        raw = (self.root / "native-capture-last.txt").read_bytes()
                        self._save_capture(filename, raw, cached)
                        return
                budget = json.loads(budget_path.read_text()) if budget_path.exists() else None
                if budget:
                    if budget.get("scope") != scope:
                        raise ObservationUnavailable("native observation budget scope mismatch")
                    eligible = timestamp(budget["next_allowed_utc"])
                    if eligible > deadline:
                        raise ObservationUnavailable(
                            "native observation backoff exceeds bounded wait"
                        )
                    if self.clock() < eligible:
                        self.event(
                            "waiting_for_native_observation",
                            not_before_utc=eligible.isoformat(),
                            replica=expected,
                        )
                    while self.clock() < eligible:
                        self._observation_wait_checks(expected)
                        self.sleep(min(10, (eligible - self.clock()).total_seconds()))
                self._observation_wait_checks(expected)
                began = self.clock()
                ledger = {
                    "scope": scope,
                    "last_attempt_utc": began.isoformat(),
                    "next_allowed_utc": (
                        began + dt.timedelta(seconds=NATIVE_CAPTURE_INTERVAL_SECONDS)
                    ).isoformat(),
                    "outcome": "intent_recorded",
                }
                atomic(budget_path, ledger)
                try:
                    raw, metrics = self._native_capture(expected)
                except NativeThrottled as error:
                    ledger.update(
                        outcome="throttled",
                        retry_after_seconds=error.retry_after,
                        next_allowed_utc=(
                            self.clock()
                            + dt.timedelta(
                                seconds=max(error.retry_after, NATIVE_CAPTURE_INTERVAL_SECONDS)
                            )
                        ).isoformat(),
                    )
                    atomic(budget_path, ledger)
                    self.save("native-observation-throttle.json", ledger)
                    if attempt == 1:
                        raise ObservationUnavailable(
                            "native observation remains throttled; verification paused"
                        ) from error
                    continue
                except ObservationUnavailable:
                    ledger.update(outcome="unavailable")
                    atomic(budget_path, ledger)
                    raise
                metrics = {
                    **metrics,
                    "started_at": began.isoformat(),
                    "scope": scope,
                    "run_id": self.run_id,
                }
                ledger.update(
                    outcome="complete",
                    next_allowed_utc=(
                        self.clock() + dt.timedelta(seconds=NATIVE_CAPTURE_INTERVAL_SECONDS)
                    ).isoformat(),
                )
                atomic(budget_path, ledger)
                temp = self.root / "native-capture-last.txt.tmp"
                temp.write_bytes(raw)
                temp.chmod(0o600)
                os.replace(temp, self.root / "native-capture-last.txt")
                atomic(cache_path, metrics)
                self._save_capture(filename, raw, metrics)
                return

    def _save_capture(self, filename, raw, metrics):
        require(
            metrics["limit_bytes"] == 4 * 1024**3 and metrics["cpu_quota"] == 2,
            "actual resource limits changed",
        )
        require(
            metrics["peak_process_rss_bytes"] <= 3 * 1024**3
            and metrics["peak_cgroup_bytes"] < metrics["limit_bytes"]
            and metrics["memory_limit_failures"] == 0,
            "memory budget exceeded",
        )
        path = self.root / filename
        path.write_bytes(raw)
        path.chmod(0o600)
        self.save(filename + ".json", metrics)
        self.last_memory = timestamp(metrics["at"])

    def _native_capture(self, expected):
        self.identity(expected)
        command = (
            "cat /proc/1/status "
            "/sys/fs/cgroup/memory/memory.max_usage_in_bytes "
            "/sys/fs/cgroup/memory/memory.limit_in_bytes "
            "/sys/fs/cgroup/memory/memory.failcnt "
            "/sys/fs/cgroup/cpu/cpu.cfs_quota_us /sys/fs/cgroup/cpu/cpu.cfs_period_us"
        )
        args = (
            ["az", "containerapp", "exec"]
            + common()
            + [
                "--revision",
                settings().revision,
                "--replica",
                expected,
                "--container",
                "sage",
                "--command",
                command,
            ]
        )
        master, slave = pty.openpty()
        process = subprocess.Popen(
            args, stdin=slave, stdout=slave, stderr=slave, start_new_session=True
        )
        os.close(slave)
        output = bytearray()
        try:
            until = time.monotonic() + 60
            while time.monotonic() < until:
                if select.select([master], [], [], 0.2)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                elif process.poll() is not None:
                    break
            try:
                code = process.wait(timeout=5)
            except subprocess.TimeoutExpired as error:
                raise ObservationUnavailable("native memory observation timed out") from error
            if code != 0:
                text = output.decode(errors="replace")
                if re.search(r"(?:status|HTTP)\s*429|429 Too Many Requests", text, re.I):
                    retry = re.search(r"retry-after['\"]?\s*[:=]\s*['\"]?(\d+)", text, re.I)
                    raise NativeThrottled(
                        int(retry[1]) if retry else NATIVE_CAPTURE_INTERVAL_SECONDS
                    )
                raise ObservationUnavailable("native memory observation command failed")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            os.close(master)
        self.identity(expected)
        text = output.decode(errors="replace")
        high = re.search(r"VmHWM:\s+(\d+) kB", text)
        values = [int(x.strip()) for x in text.splitlines() if x.strip().isdigit()]
        if high is None or len(values) != 5:
            raise ObservationUnavailable("native memory observation malformed")
        peak, limit, failures, quota, period = values
        rss = int(high[1]) * 1024
        metrics = {
            "at": self.clock().isoformat(),
            "replica": expected,
            "peak_process_rss_bytes": rss,
            "peak_cgroup_bytes": peak,
            "limit_bytes": limit,
            "memory_limit_failures": failures,
            "cpu_quota": quota / period if period > 0 else None,
        }
        # Keep the actual measurement even when it proves a safety violation.
        self.save("native-measurement-latest.json", metrics)
        require(
            limit == 4 * 1024**3 and period > 0 and quota / period == 2,
            "actual resource limits changed",
        )
        require(rss <= 3 * 1024**3 and peak < limit and failures == 0, "memory budget exceeded")
        return bytes(output), metrics

    def guard(self, expected, strict=False):
        self.allocation()
        self.identity(expected)
        self.observer(strict)
        if (
            self.last_memory is None
            or (self.clock() - self.last_memory).total_seconds() >= NATIVE_CAPTURE_INTERVAL_SECONDS
        ):
            self.capture("memory-latest.txt", expected)
            self.observer(strict)

    def await_completed_boundary(self, boundary, expected):
        until = time.monotonic() + 60
        while True:
            self.allocation()
            self.identity(expected)
            snapshot = self.observer(allow_failures=True)
            # Observer publishes cycles serially. A completed cycle entirely after
            # the boundary proves all earlier in-flight requests were published.
            if all(timestamp(row["started_at"]) >= boundary for row in snapshot["requests"]):
                self.observer(strict=True)
                return
            require(time.monotonic() < until, "observer did not settle the window boundary")
            self.sleep(5)

    def verify(self):
        # These programs have no mutation operations and publish only complete results.
        began = self.clock()
        for script in ["verify_sources.py", "verify_projections.py"]:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.sage_recovery",
                    "--config",
                    str(settings().config_path),
                    script.removesuffix(".py").replace("_", "-"),
                    "--run-id",
                    self.run_id,
                ],
                check=True,
                timeout=900,
            )
        for artifact in ["six-source-integrity.json", "six-projection-search.json"]:
            data = json.loads((self.root / artifact).read_text())
            require(
                data.get("complete") is True and data.get("run_id") == self.run_id,
                "incomplete or wrong-run six-source evidence",
            )
            require(
                data.get("manifest_sha256") == settings().manifest_sha256,
                "six-source evidence baseline mismatch",
            )
            require(began <= timestamp(data["at"]) <= self.clock(), "stale six-source evidence")
            ids = [row["document_id"] for row in data["records"]]
            require(
                len(ids) == 6 and set(ids) == set(settings().six_ids),
                "incomplete six-source evidence population",
            )
        check_backlog(self.backlog(), complete=True)

    def stop(self, reason):
        self.event("stop_condition", reason=reason)
        (self.root / "stop").touch()
        try:
            # On target drift never operate on a replacement deployment.
            self.allocation()
            result = self.azure(["revision", "deactivate", "--revision", settings().revision])
            target = self.azure(["revision", "show", "--revision", settings().revision])
            require(target["properties"].get("active") is False, "deactivation not confirmed")
            self.save(
                "stop-receipt.json",
                {
                    "at": self.clock().isoformat(),
                    "result": result,
                    "confirmed_inactive": True,
                    "reason": reason,
                },
            )
        except Exception as error:
            self.save(
                "stop-unconfirmed.json",
                {
                    "at": self.clock().isoformat(),
                    "reason": reason,
                    "error_type": type(error).__name__,
                    "action": "manual reconciliation required; no retry",
                },
            )
            raise Unsafe(
                "Stop condition; deactivation unconfirmed, manual reconciliation required"
            ) from error
        raise Unsafe("Stop condition; target revision confirmed inactive")

    def run(self):
        require(
            not (self.root / "restart-issued.json").exists(),
            "restart marker exists; reconcile manually, never replay",
        )
        require(
            not (self.root / "window.json").exists(), "window marker exists; reconcile manually"
        )
        require(not (self.root / "stop").exists(), "stop marker exists; reconcile manually")
        self.run_id = json.loads((self.root / "monitor-run.json").read_text())["run_id"]
        self.allocation()
        self.original = self.identity()
        self.observer()
        self.event("waiting_for_backlog", original_replica=self.original, run_id=self.run_id)
        until = time.monotonic() + 86400
        while True:
            self.guard(self.original)
            docs = self.backlog()
            if check_backlog(docs):
                break
            require(
                not any(d.get("pipeline_status") == "failed" for d in docs),
                "pipeline failure requires supported diagnosis",
            )
            require(time.monotonic() < until, "24-hour backlog deadline; no success claimed")
            self.sleep(20)
        self.event("verifying_six_projections")
        terminal_at = self.clock()
        self.verify()
        self.capture(
            "memory-before-controlled-restart.txt", self.original, minimum_time=terminal_at
        )
        self.guard(self.original, strict=True)
        check_backlog(self.backlog(), complete=True)
        # Revalidate immediately before committing a single-use restart intent.
        self.guard(self.original, strict=True)
        marker = self.root / "restart-issued.json"
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(
                {
                    "at": self.clock().isoformat(),
                    "revision": settings().revision,
                    "old_replica": self.original,
                    "run_id": self.run_id,
                },
                stream,
            )
            stream.flush()
            os.fsync(stream.fileno())
        result = self.azure(["revision", "restart", "--revision", settings().revision])
        self.save("restart-receipt.json", {"at": self.clock().isoformat(), "result": result})
        until = time.monotonic() + 600
        new = None
        while time.monotonic() < until:
            self.allocation()
            self.observer(allow_failures=True)  # Expected restart warm-up failures are excluded.
            rows = self.replicas()
            if len(rows) == 1 and rows[0]["name"] != self.original:
                try:
                    new = self.identity()
                    break
                except Unsafe:
                    pass
            self.sleep(10)
        require(new is not None, "restart did not converge within ten minutes")
        # Establish a fresh clean cycle after readiness, excluding intentional warm-up.
        ready_at = self.clock()
        until = time.monotonic() + 60
        while True:
            self.allocation()
            self.identity(new)
            snapshot = self.observer(allow_failures=True)
            if all(timestamp(x["started_at"]) >= ready_at for x in snapshot["requests"]):
                self.observer(strict=True)
                break
            require(time.monotonic() < until, "no clean postrestart probe cycle")
            self.sleep(5)
        self.capture("memory-after-restart.txt", new, minimum_time=ready_at)
        check_backlog(self.backlog(), complete=True)
        start = self.clock()
        self.save(
            "window.json",
            {
                "start_utc": start.isoformat(),
                "revision": settings().revision,
                "replica": new,
                "run_id": self.run_id,
                "initial_backlog_successful_documents": 16,
                "controlled_restart_verified": True,
            },
        )
        self.event("stability_window_started", replica=new)
        end = start + dt.timedelta(minutes=30)
        while self.clock() < end:
            self.guard(new, strict=True)
            self.sleep(10)
        self.guard(new, strict=True)
        self.await_completed_boundary(end, new)
        result = check_window(self.cycles(), self.run_id, start, end)
        self.verify()
        self.capture("memory-after-stability-window.txt", new, minimum_time=end)
        self.guard(new, strict=True)
        self.save("allocation-final.json", self.allocation())
        final = self.replicas()
        check_replica(final, new)
        self.save("replicas-final.json", final)
        # Final verification may overlap publication of probe results. Reconsume
        # complete history at the acceptance boundary, never the earlier snapshot.
        result = check_window(self.cycles(), self.run_id, start, end)
        self.save("window-validation.json", result)
        self.event("stability_window_elapsed", **result)


def execute_controller(controller):
    try:
        controller.run()
    except Unsafe as error:
        controller.stop(str(error))
    except Exception as error:
        controller.event("verification_paused", reason=type(error).__name__)
        controller.save(
            "verification-paused.json",
            {
                "at": controller.clock().isoformat(),
                "reason": type(error).__name__,
                "effect": "local controller stopped; production not deactivated",
                "action": (
                    "diagnose observation failure and reconcile any restart intent before resuming"
                ),
            },
        )
        raise


def main():
    require(settings().control_enabled, "Control requires explicit --execute authorization")
    with (settings().root / "controller.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        controller = Controller(client())
        # Existing intents always require reconciliation; do not deactivate on replay.
        for marker in ["restart-issued.json", "window.json", "stop"]:
            require(
                not (settings().root / marker).exists(),
                marker + " exists; manual reconciliation required",
            )
        execute_controller(controller)


if __name__ == "__main__":
    main()
