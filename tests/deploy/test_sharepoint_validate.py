"""Gate for ``deploy/sharepoint_validate.py``.

The driver validates the document-store vault-source store live against a
deployed SAGE edge: ingest a probe document, read its source bytes back through
the port, and audit source-file integrity -- run either side of a container
restart. Three layers prove it:

* **Structural** -- ``--dry-run`` enumerates exactly the checks each phase runs,
  derived from the driver's own registry (a dropped check must surface here).
* **Faithful** -- run the driver (as a subprocess, the production caller path)
  against the *real* SAGE REST app served by uvicorn-in-thread with the
  Postgres storage binding and the filesystem vault-source binding standing in
  for the document-store binding (the driver speaks REST and is
  binding-agnostic, so the ingest -> retain -> readback -> audit chain is
  exercised end-to-end). This is the anti-divergence guard: a driver
  hand-fitted to the stubs below would pass the trap tests yet fail here
  against the genuine endpoints.
* **Trap** -- a controllable HTTP stub reproduces the pathologies a real server
  will not produce on demand (a readback whose bytes do not hash-match the
  ingest, an audit that reports the probe missing, an ingest summary with no
  document created). Each must make the driver FAIL -- proving every assertion
  in the driver is load-bearing, not decorative.

The trap and structural layers are standard-library only and always run; the
faithful layer is skipped only where ``sage`` / ``uvicorn`` cannot be imported.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

import pytest

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_DRIVER: Final[Path] = _REPO_ROOT / "deploy" / "sharepoint_validate.py"
_CI_VAULT_CONFIG: Final[Path] = _REPO_ROOT / "tests" / "fixtures" / "ci_test_vault_config.yaml"

try:  # the faithful layer needs the app; the structural and trap layers do not
    import uvicorn
    import yaml

    from sage.adapters.stubs import StubContentStore
    from sage.app import create_app
    from sage.config import VaultConfig

    _REAL_SERVER = True
except Exception:  # noqa: BLE001 -- absence just skips the faithful layer
    _REAL_SERVER = False

_NEEDS_REAL = pytest.mark.skipif(
    not _REAL_SERVER, reason="real-server layer needs sage + uvicorn importable"
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_driver(
    base_url: str,
    phase: str,
    state_file: Path,
    *,
    vault_id: str = "test",
    extra: list[str] | None = None,
    timeout: str = "20",
) -> subprocess.CompletedProcess[str]:
    """Run the driver under a fresh interpreter with an isolated environment.

    Only ``PATH`` is passed, so a real ``AUTH_TOKEN`` in the developer's shell
    cannot leak in (the faithful app and the trap stubs both run auth-off). The
    driver is stdlib-only, so the test interpreter runs it unchanged.
    """
    args = [
        sys.executable,
        str(_DRIVER),
        "--phase",
        phase,
        "--state-file",
        str(state_file),
        "--vault-id",
        vault_id,
        "--timeout",
        timeout,
    ]
    if base_url:
        args += ["--base-url", base_url]
    if extra:
        args += extra
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "")},
        timeout=int(float(timeout)) + 30,
    )


# --------------------------------------------------------------------------- #
# Structural layer                                                            #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        ("pre-restart", "ingest,readback,source_audit"),
        ("post-restart", "rediscover,readback,source_audit"),
    ],
)
def test_dry_run_lists_expected_checks(phase: str, expected: str, tmp_path: Path) -> None:
    """``--dry-run`` enumerates the phase's checks from the driver's registry and
    touches no network (no ``--base-url`` supplied).
    """
    proc = _run_driver("", phase, tmp_path / "state.json", extra=["--dry-run"])
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert f"phase={phase} checks={expected}" in proc.stdout, proc.stdout


# --------------------------------------------------------------------------- #
# Faithful layer: the real SAGE REST app, uvicorn-in-thread                   #
# --------------------------------------------------------------------------- #
@contextmanager
def _serve_real(tmp_path: Path) -> Iterator[str]:
    """Serve the real SAGE REST app with the Postgres storage binding (the test
    server named by ``SAGE_TEST_PG_DSN``, pinned by the root conftest) plus
    the filesystem vault-source binding, stub providers, and a single ``test``
    vault rooted in tmp.
    """
    raw = yaml.safe_load(_CI_VAULT_CONFIG.read_text(encoding="utf-8"))
    storage = tmp_path / "sources"
    brain = tmp_path / "brain"
    storage.mkdir(parents=True, exist_ok=True)
    brain.mkdir(parents=True, exist_ok=True)
    raw["vault"]["storage_root"] = str(storage)
    raw["vault"]["brain_root"] = str(brain)
    config = VaultConfig.model_validate(raw)

    os.environ["SAGE_TEST_STUB_PROVIDERS"] = "1"
    app = create_app(config=config, content_store_factory=lambda _root: StubContentStore())
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not getattr(server, "started", False):
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start within 30s")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@_NEEDS_REAL
def test_faithful_pre_then_post_all_pass(tmp_path: Path) -> None:
    """Ingest -> readback -> audit (pre-restart) then rediscover -> readback ->
    audit (post-restart) against the genuine REST app, sharing one state file --
    the divergence guard for both phases.
    """
    state = tmp_path / "state.json"
    with _serve_real(tmp_path) as base:
        pre = _run_driver(base, "pre-restart", state)
        assert pre.returncode == 0, f"stdout={pre.stdout!r} stderr={pre.stderr!r}"
        assert "check=ingest status=PASS" in pre.stdout, pre.stdout
        assert "check=readback status=PASS" in pre.stdout, pre.stdout
        assert "check=source_audit status=PASS" in pre.stdout, pre.stdout
        assert "result=pass" in pre.stdout

        post = _run_driver(base, "post-restart", state)
    assert post.returncode == 0, f"stdout={post.stdout!r} stderr={post.stderr!r}"
    assert "check=rediscover status=PASS" in post.stdout, post.stdout
    assert "check=readback status=PASS" in post.stdout, post.stdout
    assert "check=source_audit status=PASS" in post.stdout, post.stdout
    assert "result=pass" in post.stdout


# --------------------------------------------------------------------------- #
# Trap layer: a controllable REST stub                                        #
# --------------------------------------------------------------------------- #
def _extract_file_bytes(body: bytes, content_type: str) -> bytes:
    """Recover the uploaded ``files`` part's bytes from a multipart body so the
    stub can echo them back on readback (the good control) or corrupt them (the
    trap). Strips exactly the trailing CRLF the wire format adds, preserving the
    payload's own trailing newline.
    """
    boundary = content_type.split("boundary=", 1)[1].strip().encode()
    for segment in body.split(b"--" + boundary):
        if b'name="files"' in segment:
            payload = segment.split(b"\r\n\r\n", 1)[1]
            if payload.endswith(b"\r\n"):
                payload = payload[:-2]
            return payload
    return b""


def _sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode("utf-8")


_GOOD = "good"
_HASH_MISMATCH = "hash_mismatch"
_MISSING_SOURCE = "missing_source"
_EMPTY_INGEST = "empty_ingest"


@contextmanager
def rest_stub(mode: str) -> Iterator[str]:
    """A threaded HTTP stub for the four edge calls the driver makes. ``mode``
    selects a well-behaved surface or one of three pathologies.
    """
    port = _free_port()
    captured: dict[str, bytes] = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_a: object) -> None:
            return

        def _json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            raw = self._read_body()
            if path.endswith("/documents:batch"):
                captured["bytes"] = _extract_file_bytes(raw, self.headers.get("Content-Type", ""))
                if mode == _EMPTY_INGEST:
                    events = [
                        {
                            "event_type": "summary",
                            "documents_created": {"new": 0, "new_version": 0},
                            "error_count": 1,
                        }
                    ]
                else:
                    events = [
                        {
                            "event_type": "progress",
                            "status": "completed",
                            "document_id": "probe-1",
                            "file_index": 0,
                            "total_files": 1,
                            "filename": "probe.md",
                            "stage": "projection",
                        },
                        {
                            "event_type": "summary",
                            "documents_created": {"new": 1, "new_version": 0},
                            "error_count": 0,
                        },
                    ]
                body = _sse(events)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path.endswith("/admin/verify-source-files"):
                if mode == _MISSING_SOURCE:
                    self._json(
                        {
                            "vault_id": "test",
                            "total_documents_checked": 1,
                            "check_hashes": True,
                            "entries": [{"document_id": "probe-1", "integrity_status": "missing"}],
                            "summary": {"healthy": 0, "missing": 1, "hash_mismatch": 0},
                        }
                    )
                else:
                    self._json(
                        {
                            "vault_id": "test",
                            "total_documents_checked": 1,
                            "check_hashes": True,
                            "entries": [],
                            "summary": {"healthy": 1, "missing": 0, "hash_mismatch": 0},
                        }
                    )
                return
            self._json({"error": "unexpected"}, status=404)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path.endswith("/sage_vaults"):
                self._json([{"id": "test", "name": "Stub", "storage_root": "/x"}])  # type: ignore[arg-type]
                return
            if "/documents/" in path:
                stored = captured.get("bytes", b"")
                served = stored + b"X" if mode == _HASH_MISMATCH else stored
                self._json(
                    {
                        "content": base64.b64encode(served).decode("ascii"),
                        "content_size": len(served),
                        "content_hash": "sha256:" + hashlib.sha256(served).hexdigest(),
                    }
                )
                return
            self._json({"error": "unexpected"}, status=404)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_trap_good_stub_passes(tmp_path: Path) -> None:
    """Positive control for the trap layer: a well-behaved surface PASSes, so the
    FAIL assertions below are credited to the pathology, not the stub harness.
    """
    with rest_stub(_GOOD) as base:
        proc = _run_driver(base, "pre-restart", tmp_path / "state.json")
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "result=pass" in proc.stdout, proc.stdout


def test_trap_hash_mismatch_fails_readback(tmp_path: Path) -> None:
    """Readback bytes that do not hash-match the ingest mean the round-trip is not
    byte-faithful -> the readback check FAILs.
    """
    with rest_stub(_HASH_MISMATCH) as base:
        proc = _run_driver(base, "pre-restart", tmp_path / "state.json")
    assert proc.returncode != 0, "a non-faithful readback must fail the driver"
    assert "check=readback status=FAIL" in proc.stdout, proc.stdout


def test_trap_missing_source_fails_audit(tmp_path: Path) -> None:
    """An audit that reports the probe's source file missing -> the audit check
    FAILs even though ingest and readback passed.
    """
    with rest_stub(_MISSING_SOURCE) as base:
        proc = _run_driver(base, "pre-restart", tmp_path / "state.json")
    assert proc.returncode != 0, "a missing retained source must fail the driver"
    assert "check=source_audit status=FAIL" in proc.stdout, proc.stdout


def test_trap_empty_ingest_summary_fails_ingest(tmp_path: Path) -> None:
    """An ingest summary that created no document (and emitted no completed
    event) -> the ingest check FAILs.
    """
    with rest_stub(_EMPTY_INGEST) as base:
        proc = _run_driver(base, "pre-restart", tmp_path / "state.json")
    assert proc.returncode != 0, "an empty ingest must fail the driver"
    assert "check=ingest status=FAIL" in proc.stdout, proc.stdout
