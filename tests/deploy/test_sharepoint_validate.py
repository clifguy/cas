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
    env: dict[str, str] | None = None,
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
        env={"PATH": os.environ.get("PATH", ""), **(env or {})},
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
    ("backend", "expected"),
    [("filesystem", "no"), ("document_store", "yes")],
)
def test_faithful_pre_then_post_all_pass(
    backend: str, expected: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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

    Each leg *states* its expectation via ``--expect-rewritten`` rather than only
    reading the verdict out of stdout, so a wrong observation fails inside the
    driver -- which is the production behaviour the cloud harness depends on --
    instead of only failing a string match here.
    """
    state = tmp_path / "state.json"
    expect = ["--expect-rewritten", expected]
    with _serve_real(tmp_path, monkeypatch, backend) as base:
        pre = _run_driver(base, "pre-restart", state, extra=expect)
        assert pre.returncode == 0, f"stdout={pre.stdout!r} stderr={pre.stderr!r}"
        assert "check=ingest status=PASS" in pre.stdout, pre.stdout
        assert "check=readback status=PASS" in pre.stdout, pre.stdout
        assert "check=provenance status=PASS" in pre.stdout, pre.stdout
        assert f"rewritten={expected}" in pre.stdout, pre.stdout
        assert "check=source_audit status=PASS" in pre.stdout, pre.stdout
        assert "result=pass" in pre.stdout

        post = _run_driver(base, "post-restart", state, extra=expect)
    assert post.returncode == 0, f"stdout={post.stdout!r} stderr={post.stderr!r}"
    assert "check=rediscover status=PASS" in post.stdout, post.stdout
    assert "check=readback status=PASS" in post.stdout, post.stdout
    assert "check=provenance status=PASS" in post.stdout, post.stdout
    assert f"rewritten={expected}" in post.stdout, post.stdout
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

#: Probe kind -> the id the stub mints for it. A mapping rather than a
#: conditional: an unknown kind must raise here, not resolve to whichever probe
#: sat on the else branch. A third kind added to the driver and to a
#: parametrize, while this mapping is forgotten, would otherwise test one probe
#: twice under a case id claiming otherwise.
_PROBE_DOC_IDS = {"markdown": _MARKDOWN_DOC_ID, "docx": _DOCX_DOC_ID}

#: How many probes a phase ingests. The driver derives the same quantity from
#: `len(ctx.probes)` and asserts the audit walked at least that many, so a
#: literal here that fell out of step would red the *positive control* rather
#: than anything under test -- the most expensive kind of red to diagnose.
_PROBE_COUNT = len(_PROBE_DOC_IDS)

_GOOD = "good"
_HASH_MISMATCH = "hash_mismatch"
#: An audit mode is "unhealthy:<kind>:<status>" -- the probe to report and the
#: status to report it under. Spelling the matrix as a parameter is what lets a
#: test assert that *each* probe is covered under *each* status; a fixed one
#: status per probe makes the coverage of either half an accident of assignment.
_UNHEALTHY_PREFIX = "unhealthy:"


def _unhealthy(kind: str, status: str) -> str:
    return f"{_UNHEALTHY_PREFIX}{kind}:{status}"


_MISSING_SOURCE = _unhealthy("markdown", "missing")
_EMPTY_INGEST = "empty_ingest"
_UNRELATED_UNHEALTHY = "unrelated_unhealthy"
_COLLAPSED_DIGESTS = "collapsed_digests"
_STORED_HASH_ADRIFT = "stored_hash_adrift"
_DUPLICATE_ACCEPTED = "duplicate_accepted"
_NO_REWRITE = "no_rewrite"
_UNDERCOUNTED_AUDIT = "undercounted_audit"


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
        return _PROBE_DOC_IDS["docx" if filename.endswith(".docx") else "markdown"]

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
            rewrites = filename.endswith(".docx") and mode != _NO_REWRITE
            retained = delivered + _STAMP if rewrites else delivered
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

            checked = max(len(docs), _PROBE_COUNT)
            if mode == _UNDERCOUNTED_AUDIT:
                # A walk that reached fewer records than there are probes, with
                # nothing in `entries` -- the shape a partial reload or a lagging
                # store listing produces.
                return _audit_report([], _PROBE_COUNT - 1)
            if mode == _UNRELATED_UNHEALTHY:
                # A document this run neither created nor can repair -- the
                # validation vault's accumulated residue. Both probes are healthy.
                return _audit_report([entry(_OLDER_DOC_ID, "missing")], checked + 1)
            if mode.startswith(_UNHEALTHY_PREFIX):
                # "<kind>:<status>" -- the probe kind and the status to report it
                # under, so the matrix is a parameter rather than a fixed
                # assignment of one status per probe.
                kind, _, status = mode[len(_UNHEALTHY_PREFIX) :].partition(":")
                return _audit_report([entry(_PROBE_DOC_IDS[kind], status)], checked)
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
    ("mode", "expected_checked"),
    [
        (_GOOD, _PROBE_COUNT),
        (_MISSING_SOURCE, _PROBE_COUNT),
        (_unhealthy("markdown", "symlinked"), _PROBE_COUNT),
        (_unhealthy("docx", "out_of_root"), _PROBE_COUNT),
        (_UNRELATED_UNHEALTHY, _PROBE_COUNT + 1),
        (_UNDERCOUNTED_AUDIT, _PROBE_COUNT - 1),
    ],
)
def test_stub_audit_report_matches_the_real_response_model(
    mode: str, expected_checked: int, tmp_path: Path
) -> None:
    """Every audit payload the stub can emit validates as a real
    ``SourceFileIntegrityReport`` and carries the whole summary partition.

    The driver deliberately reads no per-status count, so nothing in its own
    assertions would notice the stub drifting from the report it stands in for --
    which is how the stub came to carry a three-key summary two statuses after
    the service grew to five. The expected key set is derived from the service's
    own ``integrity_status`` literal rather than copied, so a status added there
    fails this test until the stub carries it too.

    The document count is carried per mode rather than as one lower bound. A
    single ``>= _PROBE_COUNT`` reads fine and excludes the undercounted mode by
    construction, which would leave the one payload whose whole purpose is to
    report *too few* documents as the only audit shape never checked against the
    real model — and it is the fixture the undercounted-walk gate depends on, so
    that gate would be resting on a payload the service might no longer be able
    to produce.
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
    assert report.total_documents_checked == expected_checked, (
        f"{mode}: expected the stub to report {expected_checked} documents checked, "
        f"got {report.total_documents_checked}"
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


@pytest.mark.parametrize("kind", ["markdown", "docx"])
@pytest.mark.parametrize("status", ["missing", "hash_mismatch", "symlinked", "out_of_root"])
def test_trap_unhealthy_probe_fails_audit(kind: str, status: str, tmp_path: Path) -> None:
    """An audit that reports *either* probe under *any* integrity status -> the
    audit check FAILs.

    Walked as a full ``kind x status`` matrix on purpose. The gate takes its
    verdict from a document being *named* by the audit, never from a per-status
    count, so it should cover every status the audit reports today and every one
    it may later learn to report -- and it should cover both probes. Assigning
    one status per probe would leave which half of that claim each case actually
    tests up to the assignment: a driver that audited only the markdown probe
    would be caught by whichever status happened to land on docx, and by nothing
    else. `symlinked` and `out_of_root` are additionally the two a count-reading
    gate silently dropped.
    """
    with rest_stub(_unhealthy(kind, status)) as base:
        proc = _run_driver(base, "pre-restart", tmp_path / "state.json")
    assert proc.returncode != 0, f"the {kind} probe reported {status} must fail the driver"
    assert "check=source_audit status=FAIL" in proc.stdout, proc.stdout
    assert f"{kind}={status}" in proc.stdout, (
        f"the verdict must name the probe and its status: {proc.stdout}"
    )


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


@pytest.mark.parametrize(
    ("mode", "expect", "passes"),
    [
        (_GOOD, "yes", True),
        (_NO_REWRITE, "yes", False),
        (_GOOD, "no", False),
        (_NO_REWRITE, "any", True),
    ],
)
def test_trap_expect_rewritten_is_enforced(
    mode: str, expect: str, passes: bool, tmp_path: Path
) -> None:
    """``--expect-rewritten`` decides the run when the observation contradicts it.

    Three of the provenance check's assertions only bite where the store actually
    rewrote, so a deployment that stopped rewriting would take that coverage with
    it and leave every verdict green -- the driver would simply observe, and
    report, the new truth. The ``(_NO_REWRITE, "yes")`` row is exactly that
    scenario, and it is the one this flag exists to turn red; the ``(_GOOD,
    "no")`` row proves the flag is not a one-way ratchet, and ``any`` stays
    permissive for a caller that does not know its binding.
    """
    with rest_stub(mode) as base:
        proc = _run_driver(
            base, "pre-restart", tmp_path / "state.json", extra=["--expect-rewritten", expect]
        )
    if passes:
        assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        assert "check=provenance status=PASS" in proc.stdout, proc.stdout
    else:
        assert proc.returncode != 0, "a contradicted expectation must fail the driver"
        assert "check=provenance status=FAIL" in proc.stdout, proc.stdout
        assert f"expected={expect}" in proc.stdout, proc.stdout


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("json_but_not_an_object", "[]"),
        (
            "content_not_a_string",
            json.dumps(
                {
                    "probes": {
                        "markdown": {
                            "document_id": "a1b2c3d4_x",
                            "content_hash": "sha256:aa",
                            "content": 123,
                        },
                        "docx": {
                            "document_id": "e5f6a7b8_y",
                            "content_hash": "sha256:bb",
                            "content": 123,
                        },
                    }
                }
            ),
        ),
        ("half_populated", json.dumps({"probes": {"markdown": {"document_id": "a1b2c3d4_x"}}})),
        ("not_json_at_all", "{{{"),
    ],
)
def test_malformed_state_file_still_reports_verdicts(
    label: str, payload: str, tmp_path: Path
) -> None:
    """A state file the post-restart phase cannot use degrades to "no state" and
    the phase reports it -- it never dies before printing a verdict.

    ``load_state`` runs outside ``run_phase``'s per-check exception handler, so a
    raise there costs the operator the entire report: exit status is still
    non-zero, but stdout is a traceback and a log consumer grepping for
    ``result=`` finds nothing. Every check must still emit its line, and the run
    must still fail -- degrading quietly to a green run would be far worse than
    the traceback.
    """
    state = tmp_path / "state.json"
    state.write_text(payload, encoding="utf-8")
    # No server needed: the checks fail on absent probe state before any request
    # that could succeed, and a refused connection is itself a reported verdict.
    proc = _run_driver("http://127.0.0.1:9", "post-restart", state, timeout="2")
    assert proc.returncode != 0, f"{label}: an unusable state file must fail the phase"
    assert "Traceback" not in proc.stderr, f"{label}: died before reporting: {proc.stderr}"
    for check in ("rediscover", "readback", "provenance", "source_audit"):
        assert f"check={check} " in proc.stdout, f"{label}: missing verdict for {check}"
    assert "result=fail phase=post-restart" in proc.stdout, proc.stdout
    assert proc.stdout.count("no_probe_state") == 3, (
        f"{label}: the three probe-dependent checks must each report it: {proc.stdout}"
    )


def test_trap_audit_that_walked_fewer_records_than_probes_fails(tmp_path: Path) -> None:
    """An audit reporting fewer documents checked than this run has probes -> the
    audit check FAILs.

    A healthy document is reported only by its *absence* from ``entries``, so an
    audit that never reached one of the probes is indistinguishable, from the
    entries alone, from one that reached it and found it healthy. The count is
    the only thing that separates them. Reported as one document checked against
    two probes ingested, the driver would otherwise print
    ``probes_healthy=2 checked=1`` -- claiming two probes were found healthy
    while naming the number that says only one was examined.
    """
    with rest_stub(_UNDERCOUNTED_AUDIT) as base:
        proc = _run_driver(base, "pre-restart", tmp_path / "state.json")
    assert proc.returncode != 0, "an audit that walked fewer records than probes must fail"
    assert "check=source_audit status=FAIL" in proc.stdout, proc.stdout
    assert "audit_checked_too_few" in proc.stdout, proc.stdout


def test_expect_rewritten_reads_its_environment_fallback(tmp_path: Path) -> None:
    """``$SP_VALIDATE_EXPECT_REWRITTEN`` supplies the expectation when no flag is given.

    Every other driver option reads an environment fallback, which is where the
    operator runbook puts its settings; this one did not, so the runbook's export
    block structurally could not carry it and the manual path kept running at the
    permissive default. Exercised against a store that does *not* rewrite while the
    environment expects one, so a fallback that were silently ignored would leave
    the run green.
    """
    with rest_stub(_NO_REWRITE) as base:
        proc = _run_driver(
            base,
            "pre-restart",
            tmp_path / "state.json",
            env={"SP_VALIDATE_EXPECT_REWRITTEN": "yes"},
        )
    assert proc.returncode != 0, "the environment expectation must be enforced like the flag"
    assert "check=provenance status=FAIL" in proc.stdout, proc.stdout
    assert "expected=yes" in proc.stdout, proc.stdout


def test_explicit_flag_overrides_the_environment_fallback(tmp_path: Path) -> None:
    """An explicit ``--expect-rewritten`` wins over the environment.

    Ordinary argparse precedence, asserted because the runbook now sets both — the
    export in the prerequisites block and the flag on each command — and an
    inversion would make the per-command flag decorative.

    On the environment setup here: deleting it leaves this test passing, because
    the flag alone produces the same verdict. That is unavoidable for a precedence
    test — when one input always wins, the losing input is unobservable in the
    result by construction, so no assertion can be made to read it. The setup is
    load-bearing against the rival rather than against its own removal: swapping
    the precedence so the environment wins reds this test, and that is the
    implementation it exists to exclude.
    """
    with rest_stub(_NO_REWRITE) as base:
        proc = _run_driver(
            base,
            "pre-restart",
            tmp_path / "state.json",
            extra=["--expect-rewritten", "no"],
            env={"SP_VALIDATE_EXPECT_REWRITTEN": "yes"},
        )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "check=provenance status=PASS rewritten=no expected=no" in proc.stdout, proc.stdout


def test_malformed_environment_expectation_is_rejected(tmp_path: Path) -> None:
    """An unrecognised ``$SP_VALIDATE_EXPECT_REWRITTEN`` fails the run, loudly.

    ``argparse`` checks ``choices`` only against a value it parsed off the command
    line, never against a default — so a mistyped environment value would arrive
    unchallenged and, matching no branch, behave exactly like ``any``. That is this
    option's own failure mode one level down: the operator who bothered to set an
    expectation, and fat-fingered it, would get precisely the unenforced green run
    they were trying to rule out.
    """
    with rest_stub(_GOOD) as base:
        proc = _run_driver(
            base,
            "pre-restart",
            tmp_path / "state.json",
            env={"SP_VALIDATE_EXPECT_REWRITTEN": "true"},
        )
    assert proc.returncode != 0, "a malformed expectation must not degrade to the default"
    assert "SP_VALIDATE_EXPECT_REWRITTEN" in proc.stderr, proc.stderr
    assert "check=" not in proc.stdout, (
        f"the run must be refused at argument parsing, before any check: {proc.stdout!r}"
    )


def test_stub_refuses_an_unknown_probe_kind_rather_than_substituting(tmp_path: Path) -> None:
    """An audit mode naming a probe kind the stub does not know fails the request
    outright, instead of reporting some other probe under that status.

    The mapping replaced a conditional whose else branch was a catch-all, and the
    catch-all's cost is misattribution rather than a miss: a third probe kind
    added to the driver and to a parametrize, with this mapping forgotten, would
    have quietly reported the markdown probe twice while the case id claimed the
    new kind was under test. Raising turns that into a loud failure at the stub.

    Asserted through the driver's own verdict, since that is where a substitution
    would have surfaced as a plausible-looking result.
    """
    with rest_stub(_unhealthy("no_such_kind", "missing")) as base:
        proc = _run_driver(base, "pre-restart", tmp_path / "state.json")
    assert proc.returncode != 0, "an unknown probe kind must not yield a usable report"
    assert "check=source_audit status=FAIL" in proc.stdout, proc.stdout
    assert "markdown=missing" not in proc.stdout, (
        f"the stub substituted the markdown probe for an unknown kind: {proc.stdout!r}"
    )
