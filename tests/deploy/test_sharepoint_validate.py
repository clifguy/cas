"""Gate for ``deploy/sharepoint_validate.py``.

The driver validates the document-store vault-source store live against a
deployed SAGE edge: ingest two probe documents, read the markdown probe's source
bytes back through the port, check the docx probe's provenance, and audit
source-file integrity -- run either side of a container restart. Three layers
prove it:

* **Structural** -- ``--dry-run`` enumerates exactly the checks each phase runs,
  derived from the driver's own registry (a dropped check must surface here).
* **Faithful** -- run the driver (as a subprocess, the production caller path)
  against the *real* SAGE REST app served by uvicorn-in-thread with the Postgres
  storage binding, over **both** vault-source bindings. The filesystem leg
  retains every upload verbatim; the document-store leg (a faked Graph
  transport) rewrites Office packages at rest, so the provenance / as-stored
  split the docx probe exists for is produced rather than assumed. This is the
  anti-divergence guard: a driver hand-fitted to the stubs below would pass the
  trap tests yet fail here against the genuine endpoints, and a driver that
  assumed a rewriting store would fail the filesystem leg.

  What this layer does *not* establish is that the tenant store rewrites, or
  that it rewrites this way. The fake reproduces the shape of the divergence so
  the driver can be shown to handle it; whether a given deployment produces one
  is a live fact, observable only in the driver's own ``rewritten=`` verdict
  against that deployment. A green run here is evidence about the driver, not
  about the store.
* **Trap** -- a controllable HTTP stub reproduces the pathologies a real server
  will not produce on demand (a readback whose bytes do not hash-match the
  ingest, an audit that reports a probe unhealthy, an ingest summary with no
  document created, and the shape of a build that recorded one digest where
  CAS-ADR-043 point 6 requires two). Each must make the driver FAIL -- proving
  every assertion in the driver is load-bearing, not decorative.

The trap and structural layers are standard-library only and always run; the
faithful layer is skipped where ``sage`` / ``uvicorn`` cannot be imported or
``SAGE_TEST_PG_DSN`` is unset (the real app's lifespan opens Postgres-backed
vault storage, so without a configured test server startup can only fail
against the sentinel host the root conftest pins).
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import subprocess
import sys
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

import pytest

from tests.deploy._stub_server import serve_threaded, serve_uvicorn

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_DRIVER: Final[Path] = _REPO_ROOT / "deploy" / "sharepoint_validate.py"
_CI_VAULT_CONFIG: Final[Path] = _REPO_ROOT / "tests" / "fixtures" / "ci_test_vault_config.yaml"

try:  # the faithful layer needs the app; the structural and trap layers do not
    import uvicorn  # noqa: F401 -- importability is the faithful layer's skip signal
    import yaml

    from sage.adapters.stubs import StubContentStore
    from sage.app import create_app
    from sage.config import VaultConfig

    _REAL_SERVER = True
except Exception:  # noqa: BLE001 -- absence just skips the faithful layer
    _REAL_SERVER = False

# Presence at import is the right signal: the isolation provisioner rewrites an
# existing DSN to a per-process throwaway but never sets one from nothing.
_PG_DSN = os.environ.get("SAGE_TEST_PG_DSN")

_NEEDS_REAL = pytest.mark.skipif(
    not (_REAL_SERVER and _PG_DSN),
    reason="real-server layer needs sage + uvicorn importable and SAGE_TEST_PG_DSN set",
)


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
        ("pre-restart", "ingest,readback,provenance,source_audit"),
        ("post-restart", "rediscover,readback,provenance,source_audit"),
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
def _serve_real(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str) -> Iterator[str]:
    """Serve the real SAGE REST app with the Postgres storage binding (the test
    server named by ``SAGE_TEST_PG_DSN``, pinned by the root conftest) plus stub
    providers, a single ``test`` vault rooted in tmp, and ``backend`` bound as
    the vault-source store.

    Both legs run the real dispatch in ``build_stack_vault_source_store`` via the
    env override. The ``document_store`` leg fakes only the Graph transport: one
    ``FakeGraphClient`` stands in for the library, and it reproduces the store's
    rewrite-at-rest of Office packages, so the docx probe meets a genuinely
    non-byte-faithful retention rather than an assumed one.
    """
    raw = yaml.safe_load(_CI_VAULT_CONFIG.read_text(encoding="utf-8"))
    storage = tmp_path / "sources"
    brain = tmp_path / "brain"
    storage.mkdir(parents=True, exist_ok=True)
    brain.mkdir(parents=True, exist_ok=True)
    raw["vault"]["storage_root"] = str(storage)
    raw["vault"]["brain_root"] = str(brain)
    config = VaultConfig.model_validate(raw)

    monkeypatch.setenv("SAGE_TEST_VAULT_SOURCE_BACKEND", backend)
    if backend == "document_store":
        from tests.helpers.fake_graph_client import FakeGraphClient

        fake = FakeGraphClient()
        monkeypatch.setattr(
            "sage.vault_source_document_store.build_sharepoint_graph_client",
            lambda *args, **kwargs: fake,
        )

    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    app = create_app(config=config, content_store_factory=lambda _root: StubContentStore())
    with serve_uvicorn(app) as base_url:
        yield base_url


@_NEEDS_REAL
@pytest.mark.parametrize(
    ("backend", "expected_rewritten"),
    [("filesystem", "rewritten=no"), ("document_store", "rewritten=yes")],
)
def test_faithful_pre_then_post_all_pass(
    backend: str, expected_rewritten: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ingest -> readback -> provenance -> audit (pre-restart) then rediscover ->
    readback -> provenance -> audit (post-restart) against the genuine REST app,
    sharing one state file -- the divergence guard for both phases.

    Parametrized over both vault-source bindings, and the two legs prove
    different things. ``filesystem`` retains verbatim, so the driver must reach
    ``rewritten=no`` and still pass: a driver that assumed a rewriting store
    would fail here. ``document_store`` rewrites the docx package at rest, so the
    driver must reach ``rewritten=yes`` -- the only leg where the provenance /
    as-stored inequality and the duplicate-rejection assertion have anything to
    bite on.
    """
    state = tmp_path / "state.json"
    with _serve_real(tmp_path, monkeypatch, backend) as base:
        pre = _run_driver(base, "pre-restart", state)
        assert pre.returncode == 0, f"stdout={pre.stdout!r} stderr={pre.stderr!r}"
        assert "check=ingest status=PASS" in pre.stdout, pre.stdout
        assert "check=readback status=PASS" in pre.stdout, pre.stdout
        assert "check=provenance status=PASS" in pre.stdout, pre.stdout
        assert expected_rewritten in pre.stdout, pre.stdout
        assert "check=source_audit status=PASS" in pre.stdout, pre.stdout
        assert "result=pass" in pre.stdout

        post = _run_driver(base, "post-restart", state)
    assert post.returncode == 0, f"stdout={post.stdout!r} stderr={post.stderr!r}"
    assert "check=rediscover status=PASS" in post.stdout, post.stdout
    assert "check=readback status=PASS" in post.stdout, post.stdout
    assert "check=provenance status=PASS" in post.stdout, post.stdout
    assert expected_rewritten in post.stdout, post.stdout
    assert "check=source_audit status=PASS" in post.stdout, post.stdout
    assert "result=pass" in post.stdout


@_NEEDS_REAL
def test_faithful_docx_probe_ingests_under_markdown_only_doc_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The docx probe ingests against a vault whose only ``doc_type`` declares
    ``source_types: [markdown]``.

    ``source_types`` constrains *filename-based* doc_type resolution, not ingest;
    the driver supplies ``doc_type`` explicitly, so neither this fixture nor the
    seeded cloud vault config needs a ``docx`` entry, and no operator re-seed of
    the deployed library is required to run the validation. That is a claim about
    code this driver does not own, so it is pinned rather than assumed: if this
    reds, both vault configs and a runbook re-seed step come back into scope.

    Bound to the filesystem leg because the claim sits above the port: doc_type
    resolution happens before anything is retained, so the vault-source binding
    cannot affect the outcome. The rewriting leg is where the *retention* half is
    proved, in ``test_faithful_pre_then_post_all_pass``.
    """
    declared = yaml.safe_load(_CI_VAULT_CONFIG.read_text(encoding="utf-8"))
    source_types = declared["document_types"]["doc_types"][0]["source_types"]
    assert source_types == ["markdown"], (
        f"precondition: the fixture must declare markdown only, not {source_types!r}"
    )

    with _serve_real(tmp_path, monkeypatch, "filesystem") as base:
        proc = _run_driver(base, "pre-restart", tmp_path / "state.json")
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "check=ingest status=PASS" in proc.stdout, proc.stdout
    assert "docx=" in proc.stdout, f"the docx probe must have been created: {proc.stdout}"


# --------------------------------------------------------------------------- #
# Trap layer: a controllable REST stub                                        #
# --------------------------------------------------------------------------- #
def _extract_upload(body: bytes, content_type: str) -> tuple[str, bytes]:
    """Recover the uploaded ``files`` part's filename and bytes from a multipart
    body, so the stub can key on the probe kind and echo the bytes back on
    readback (the good control) or corrupt them (the trap). Strips exactly the
    trailing CRLF the wire format adds, preserving the payload's own trailing
    newline -- and, for the docx probe, the package's final bytes.
    """
    boundary = content_type.split("boundary=", 1)[1].strip().encode()
    for segment in body.split(b"--" + boundary):
        if b'name="files"' in segment:
            headers, payload = segment.split(b"\r\n\r\n", 1)
            if payload.endswith(b"\r\n"):
                payload = payload[:-2]
            marker = b'filename="'
            start = headers.find(marker) + len(marker)
            filename = headers[start : headers.find(b'"', start)].decode("utf-8", "replace")
            return filename, payload
    return "", b""


def _sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode("utf-8")


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


#: What a store that rewrites Office packages at rest appends here. Structurally
#: inert and never empty, which is all the stub needs: the point is only that the
#: retained bytes are not the delivered bytes.
_STAMP = b"<stub-rewrite-at-rest/>"

#: The five keys ``verify_vault_source_files`` reports, so the stub cannot drift
#: from the report shape it stands in for. On the real service they sum to
#: ``total_documents_checked`` by construction.
_SUMMARY_KEYS: Final[tuple[str, ...]] = (
    "healthy",
    "missing",
    "hash_mismatch",
    "symlinked",
    "out_of_root",
)


def _audit_report(entries: list[dict[str, Any]], checked: int) -> dict[str, Any]:
    """A full-shape integrity report: every summary key present, the counts
    derived from ``entries`` and ``healthy`` the residual, exactly as the service
    computes them.
    """
    summary = dict.fromkeys(_SUMMARY_KEYS, 0)
    for entry in entries:
        summary[entry["integrity_status"]] += 1
    summary["healthy"] = checked - len(entries)
    return {
        "vault_id": "test",
        "total_documents_checked": checked,
        "check_hashes": True,
        "entries": entries,
        "summary": summary,
    }


#: Document ids the stub mints. Well-formed in SAGE's own shape -- 8 hex
#: characters, an underscore, then a slug -- because the report they appear in is
#: validated against the real response model, which rejects anything else.
_MARKDOWN_DOC_ID = "a1b2c3d4_validation_probe_markdown"
_DOCX_DOC_ID = "e5f6a7b8_validation_probe_docx"
_OLDER_DOC_ID = "0f1e2d3c_residue_of_an_earlier_run"

_GOOD = "good"
_HASH_MISMATCH = "hash_mismatch"
_MISSING_SOURCE = "missing_source"
_EMPTY_INGEST = "empty_ingest"
_SYMLINKED_PROBE = "symlinked_probe"
_OUT_OF_ROOT_PROBE = "out_of_root_probe"
_UNRELATED_UNHEALTHY = "unrelated_unhealthy"
_COLLAPSED_DIGESTS = "collapsed_digests"
_STORED_HASH_ADRIFT = "stored_hash_adrift"
_DUPLICATE_ACCEPTED = "duplicate_accepted"


@contextmanager
def rest_stub(mode: str) -> Iterator[str]:
    """A threaded HTTP stub for the edge calls the driver makes. ``mode`` selects
    a well-behaved surface or one of the pathologies.

    The well-behaved surface is a *rewriting* store: a docx upload is retained
    with a stamp appended, its record carries the delivered digest as provenance
    and the retained copy's digest as-stored, and a re-delivery of the same bytes
    is refused as a duplicate keyed on provenance. That is the cloud's behaviour,
    so it is what the positive control asserts against.
    """
    # document id -> (delivered bytes, retained bytes). A re-delivery is
    # recognised the way the service recognises one: by content, not by name.
    docs: dict[str, tuple[bytes, bytes]] = {}

    def _doc_id(filename: str) -> str:
        return _DOCX_DOC_ID if filename.endswith(".docx") else _MARKDOWN_DOC_ID

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

        def _sse_response(self, events: list[dict[str, Any]]) -> None:
            body = _sse(events)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _ingest_events(self, filename: str, delivered: bytes) -> list[dict[str, Any]]:
            """The SSE events for one upload: a duplicate refusal when this
            content is already held, otherwise a creation.
            """
            if mode == _EMPTY_INGEST:
                return [
                    {
                        "event_type": "summary",
                        "documents_created": {"new": 0, "new_version": 0},
                        "error_count": 1,
                        "errors": [],
                    }
                ]
            doc_id = _doc_id(filename)
            known = docs.get(doc_id)
            duplicate = known is not None and known[0] == delivered
            if duplicate and mode != _DUPLICATE_ACCEPTED:
                return [
                    {
                        "event_type": "summary",
                        "documents_created": {"new": 0, "new_version": 0},
                        "error_count": 1,
                        "errors": [
                            {
                                "file_index": 0,
                                "filename": filename,
                                "source_path": filename,
                                "message": "Duplicate content detected",
                                "code": "duplicate_content",
                                "detail": {
                                    "existing_document_id": doc_id,
                                    "source_content_hash": _sha(delivered),
                                },
                            }
                        ],
                    }
                ]
            retained = delivered + _STAMP if filename.endswith(".docx") else delivered
            docs[doc_id] = (delivered, retained)
            return [
                {
                    "event_type": "progress",
                    "status": "completed",
                    "document_id": doc_id,
                    "file_index": 0,
                    "total_files": 1,
                    "filename": filename,
                    "stage": "projection",
                },
                {
                    "event_type": "summary",
                    "documents_created": {"new": 1, "new_version": 0},
                    "error_count": 0,
                    "errors": [],
                },
            ]

        def _audit(self) -> dict[str, Any]:
            def entry(doc_id: str, status: str) -> dict[str, Any]:
                return {
                    "document_id": doc_id,
                    "title": "Document-store vault-source validation probe",
                    "source_path": f"imports/{doc_id}",
                    "lifecycle_status": "active",
                    "integrity_status": status,
                    "expected_content_hash": _sha(b""),
                    "observed_content_hash": None,
                }

            checked = max(len(docs), 1)
            if mode == _MISSING_SOURCE:
                return _audit_report([entry(_MARKDOWN_DOC_ID, "missing")], checked)
            if mode == _SYMLINKED_PROBE:
                return _audit_report([entry(_MARKDOWN_DOC_ID, "symlinked")], checked)
            if mode == _OUT_OF_ROOT_PROBE:
                return _audit_report([entry(_DOCX_DOC_ID, "out_of_root")], checked)
            if mode == _UNRELATED_UNHEALTHY:
                # A document this run neither created nor can repair -- the
                # validation vault's accumulated residue. Both probes are healthy.
                return _audit_report([entry(_OLDER_DOC_ID, "missing")], checked + 1)
            return _audit_report([], checked)

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            raw = self._read_body()
            if path.endswith("/documents:batch"):
                filename, delivered = _extract_upload(raw, self.headers.get("Content-Type", ""))
                self._sse_response(self._ingest_events(filename, delivered))
                return
            if path.endswith("/admin/verify-source-files"):
                self._json(self._audit())
                return
            self._json({"error": "unexpected"}, status=404)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path.endswith("/sage_vaults"):
                self._json([{"id": "test", "name": "Stub", "document_count": 0}])  # type: ignore[arg-type]
                return
            if path.endswith("/content") and "/documents/" in path:
                # The raw byte channel a binary container is read through: it
                # serves the retained copy, which for a rewritten format is not
                # what was delivered.
                _, retained = docs.get(path.rsplit("/", 2)[-2], (b"", b""))
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(retained)))
                self.end_headers()
                self.wfile.write(retained)
                return
            if "/documents/" in path:
                delivered, retained = docs.get(path.rsplit("/", 1)[-1], (b"", b""))
                served = retained + b"X" if mode == _HASH_MISMATCH else retained
                payload: dict[str, Any] = {
                    "content": base64.b64encode(served).decode("ascii"),
                    "content_size": len(served),
                    "source_content_hash": _sha(delivered),
                    "stored_content_hash": _sha(retained),
                }
                if mode == _COLLAPSED_DIGESTS:
                    # The shape that preceded the two-digest split: one recorded
                    # field, holding the retained copy's digest.
                    payload["source_content_hash"] = _sha(retained)
                    payload.pop("stored_content_hash")
                elif mode == _STORED_HASH_ADRIFT:
                    payload["stored_content_hash"] = _sha(b"neither delivered nor retained")
                self._json(payload)
                return
            self._json({"error": "unexpected"}, status=404)

    with serve_threaded(lambda _base_url: _Handler) as base_url:
        yield base_url


@pytest.mark.skipif(not _REAL_SERVER, reason="needs the real response model to validate against")
@pytest.mark.parametrize(
    "mode", [_GOOD, _MISSING_SOURCE, _SYMLINKED_PROBE, _OUT_OF_ROOT_PROBE, _UNRELATED_UNHEALTHY]
)
def test_stub_audit_report_matches_the_real_response_model(mode: str, tmp_path: Path) -> None:
    """Every audit payload the stub can emit validates as a real
    ``SourceFileIntegrityReport`` and carries the whole summary partition.

    The driver deliberately reads no per-status count, so nothing in its own
    assertions would notice the stub drifting from the report it stands in for --
    which is how the stub came to carry a three-key summary two statuses after
    the service grew to five. The expected key set is derived from the service's
    own ``integrity_status`` literal rather than copied, so a status added there
    fails this test until the stub carries it too.
    """
    from typing import get_args

    from sage.models.schemas import SourceFileIntegrityEntry, SourceFileIntegrityReport

    statuses = get_args(SourceFileIntegrityEntry.model_fields["integrity_status"].annotation)
    expected_keys = {"healthy", *statuses}

    # Drive one full phase first, so the report under test is the one the stub
    # emits with probes ingested rather than the empty-vault special case. The
    # document count below is what makes that setup load-bearing: an empty vault
    # reports one checked document and still partitions correctly, so without a
    # count assertion this run would be scaffolding no assertion reads.
    with rest_stub(mode) as base:
        _run_driver(base, "pre-restart", tmp_path / "state.json")
        raw = _audit_report_for(base)

    report = SourceFileIntegrityReport.model_validate(raw)
    assert report.total_documents_checked >= 2, (
        "the report must cover the two probes the phase just ingested, not an empty vault"
    )
    assert set(report.summary) == expected_keys, (
        f"stub summary {sorted(report.summary)} does not partition the report the way the "
        f"service does ({sorted(expected_keys)})"
    )
    assert sum(report.summary.values()) == report.total_documents_checked, (
        "the five counts must sum to the documents checked, as the service computes them"
    )


def _audit_report_for(base_url: str) -> dict[str, Any]:
    """POST the audit endpoint on a running stub and return the parsed report."""
    request = urllib.request.Request(  # noqa: S310 -- a loopback stub this test started
        f"{base_url}/sage_vaults/test/admin/verify-source-files",
        data=json.dumps({"check_hashes": True}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 -- same
        return json.loads(response.read())


def test_trap_good_stub_passes(tmp_path: Path) -> None:
    """Positive control for the trap layer: a well-behaved rewriting surface
    PASSes, so the FAIL assertions below are credited to the pathology, not the
    stub harness. The docx probe reaches ``rewritten=yes``, so the assertions
    that only bite against a rewriting store are the ones under test.
    """
    with rest_stub(_GOOD) as base:
        proc = _run_driver(base, "pre-restart", tmp_path / "state.json")
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "check=provenance status=PASS rewritten=yes" in proc.stdout, proc.stdout
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
    """An audit that reports a probe's source file missing -> the audit check
    FAILs even though ingest and readback passed.
    """
    with rest_stub(_MISSING_SOURCE) as base:
        proc = _run_driver(base, "pre-restart", tmp_path / "state.json")
    assert proc.returncode != 0, "a missing retained source must fail the driver"
    assert "check=source_audit status=FAIL" in proc.stdout, proc.stdout


@pytest.mark.parametrize(
    ("mode", "status"),
    [(_SYMLINKED_PROBE, "symlinked"), (_OUT_OF_ROOT_PROBE, "out_of_root")],
)
def test_trap_non_hash_integrity_status_fails_audit(mode: str, status: str, tmp_path: Path) -> None:
    """An audit that reports a probe under an integrity status that is neither
    ``missing`` nor ``hash_mismatch`` -> the audit check FAILs.

    The gate takes its verdict from a document being *named* by the audit, never
    from a per-status count, so it covers every status the audit reports today
    and every status it may later learn to report. These two are the ones a
    count-reading gate silently dropped.
    """
    with rest_stub(mode) as base:
        proc = _run_driver(base, "pre-restart", tmp_path / "state.json")
    assert proc.returncode != 0, f"a probe reported {status} must fail the driver"
    assert "check=source_audit status=FAIL" in proc.stdout, proc.stdout
    assert status in proc.stdout, f"the verdict must name the status: {proc.stdout}"


def test_trap_unrelated_document_unhealthy_passes_audit(tmp_path: Path) -> None:
    """An audit that reports a document this run did not create, while both probes
    are healthy -> the audit check PASSes.

    The scoping is deliberate. The vault under validation is disposable and
    accumulates the residue of every previous run; a whole-vault verdict would
    hand this validation a permanent red over a document it neither created nor
    can repair.
    """
    with rest_stub(_UNRELATED_UNHEALTHY) as base:
        proc = _run_driver(base, "pre-restart", tmp_path / "state.json")
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "check=source_audit status=PASS" in proc.stdout, proc.stdout


def test_trap_collapsed_digests_fails_provenance(tmp_path: Path) -> None:
    """A record carrying one digest where CAS-ADR-043 point 6 requires two --
    provenance holding the retained copy's digest, no as-stored field -> the
    provenance check FAILs.

    This is the shape that preceded the split, and against a rewriting store it
    is exactly wrong: the recorded provenance is not the digest of what the
    caller delivered, so nothing keyed to content identity can match a caller's
    own original.
    """
    with rest_stub(_COLLAPSED_DIGESTS) as base:
        proc = _run_driver(base, "pre-restart", tmp_path / "state.json")
    assert proc.returncode != 0, "a collapsed digest pair must fail the driver"
    assert "check=provenance status=FAIL" in proc.stdout, proc.stdout
    assert "provenance_not_delivered_digest" in proc.stdout, proc.stdout


def test_trap_stored_hash_adrift_fails_provenance(tmp_path: Path) -> None:
    """An as-stored digest over neither the delivered nor the retained bytes ->
    the provenance check FAILs.

    Both fields are present and they differ, so a gate asserting only that the
    two digests are unequal would pass here. The as-stored digest is the value an
    integrity audit compares a re-read against; a digest over bytes nobody holds
    makes that comparison meaningless.
    """
    with rest_stub(_STORED_HASH_ADRIFT) as base:
        proc = _run_driver(base, "pre-restart", tmp_path / "state.json")
    assert proc.returncode != 0, "an as-stored digest over unknown bytes must fail the driver"
    assert "check=provenance status=FAIL" in proc.stdout, proc.stdout
    assert "stored_not_retrieved_digest" in proc.stdout, proc.stdout


def test_trap_duplicate_reingest_accepted_fails_provenance(tmp_path: Path) -> None:
    """A re-delivery of the identical docx bytes that lands a second document
    instead of being refused -> the provenance check FAILs.

    The behavioural half of the same regression: duplicate detection keyed to the
    as-stored digest rather than to provenance can never fire for a format the
    store rewrites, because the rewrite is not reproducible and the same bytes
    come to rest as a different digest every time.
    """
    with rest_stub(_DUPLICATE_ACCEPTED) as base:
        proc = _run_driver(base, "pre-restart", tmp_path / "state.json")
    assert proc.returncode != 0, "an accepted re-delivery must fail the driver"
    assert "check=provenance status=FAIL" in proc.stdout, proc.stdout
    assert "redelivery_not_deduplicated" in proc.stdout, proc.stdout


def test_trap_empty_ingest_summary_fails_ingest(tmp_path: Path) -> None:
    """An ingest summary that created no document (and emitted no completed
    event) -> the ingest check FAILs.
    """
    with rest_stub(_EMPTY_INGEST) as base:
        proc = _run_driver(base, "pre-restart", tmp_path / "state.json")
    assert proc.returncode != 0, "an empty ingest must fail the driver"
    assert "check=ingest status=FAIL" in proc.stdout, proc.stdout
