#!/usr/bin/env python3
"""Live end-to-end validation driver for the document-store vault-source store.

The cloud deployment profile retains each ingested document's source bytes in a
SharePoint document library, reached under the SAGE workload's managed identity
(CAS-ADR-043). The binding is proven in isolation by unit tests against a mocked
Graph; this driver proves it *live* against a deployed SAGE edge: a document
ingested through the authenticated REST surface lands its source bytes in the
store, survives a container restart, reads back through the vault-source port,
and passes the source-file integrity audit.

It is pure validation of the existing binding -- per the CAS-ADR-043
weakest-binding rule it adds no port capability, no new endpoint, and no new
Graph operation. Every endpoint it calls already exists.

Two phases, run either side of an operator-driven container restart:

* ``pre-restart`` -- ingest a probe document, read its bytes back, audit
  source-file integrity.
* ``post-restart`` -- re-discover the vault, re-read the probe bytes (now served
  from the store with no ephemeral local copy), and re-audit. This is the live
  proof the source survived the restart.

The probe's document id and content hash are written to a state file by the
pre-restart phase and read by the post-restart phase, so the two invocations --
separated by the restart -- agree on which document to re-verify.

Standard library only -- the driver runs on a stock runner without the project
virtualenv. The bearer token is read from ``AUTH_TOKEN`` in the environment
(never argv, so it never lands in the process table); an empty token sends no
``Authorization`` header, which is how an auth-off server is driven in tests.

Output: one ``check=<id> status=<PASS|FAIL|SKIP> <detail>`` line per check, then
a final ``result=<pass|fail> phase=<phase>`` line. Exit ``0`` iff every check in
the phase passed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable

#: Default probe-state path when neither ``--state-file`` nor
#: ``$SP_VALIDATE_STATE_FILE`` is given: the temp dir, so a bare run from a repo
#: checkout leaves no untracked file behind. The two phases share it because the
#: temp dir is stable within a host.
_DEFAULT_STATE_FILE = os.path.join(tempfile.gettempdir(), "sharepoint-validate-state.json")

#: The checks each phase runs, in order. The post-restart phase re-runs the
#: readback and audit against the same probe document the pre-restart phase
#: ingested, plus a re-discovery check that the vault itself reloaded its
#: configuration from the store after the restart.
PHASE_CHECKS: dict[str, list[str]] = {
    "pre-restart": ["ingest", "readback", "source_audit"],
    "post-restart": ["rediscover", "readback", "source_audit"],
}

_PASS = "PASS"  # noqa: S105 -- a verdict token, not a secret
_FAIL = "FAIL"


class _Context:
    """Per-run state threaded across the checks of a single phase."""

    def __init__(self, base_url: str, vault_id: str, state_file: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.vault_id = vault_id
        self.state_file = state_file
        self.timeout = timeout
        token = os.environ.get("AUTH_TOKEN", "")
        self.auth: dict[str, str] = {"Authorization": f"Bearer {token}"} if token else {}
        # Populated by the ingest check (pre-restart) or loaded from the state
        # file (post-restart): the probe document's id and canonical content hash.
        self.document_id: str | None = None
        self.content_hash: str | None = None
        self.probe_filename: str | None = None

    def vault_url(self, suffix: str) -> str:
        return f"{self.base_url}/sage_vaults/{self.vault_id}{suffix}"

    def load_state(self) -> bool:
        try:
            with open(self.state_file, encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError):
            return False
        self.document_id = state.get("document_id")
        self.content_hash = state.get("content_hash")
        self.probe_filename = state.get("probe_filename")
        return bool(self.document_id and self.content_hash)

    def write_state(self) -> None:
        with open(self.state_file, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "vault_id": self.vault_id,
                    "document_id": self.document_id,
                    "content_hash": self.content_hash,
                    "probe_filename": self.probe_filename,
                },
                handle,
            )


# --------------------------------------------------------------------------- #
# HTTP (stdlib only)                                                          #
# --------------------------------------------------------------------------- #
def _http(
    ctx: _Context,
    method: str,
    url: str,
    *,
    data: bytes | None = None,
    content_type: str | None = None,
) -> tuple[int, bytes]:
    """Issue a request and return ``(status, body)``, treating a non-2xx as a
    value (status + body) rather than an exception, so a check can inspect the
    edge's own diagnostic on rejection.
    """
    headers = dict(ctx.auth)
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=ctx.timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _hash(data: bytes) -> str:
    """The canonical ``sha256:<hex>`` form SAGE stores as ``source_content_hash``."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _multipart(filename: str, content: bytes, metadata: str) -> tuple[bytes, str]:
    """Encode the batch-ingest multipart body: a single ``files`` part plus the
    JSON ``metadata`` form field. Returns ``(body, content_type)``.
    """
    boundary = "----sagevalidate" + uuid.uuid4().hex
    crlf = b"\r\n"
    parts: list[bytes] = [
        b"--" + boundary.encode(),
        f'Content-Disposition: form-data; name="files"; filename="{filename}"'.encode(),
        b"Content-Type: text/markdown",
        b"",
        content,
        b"--" + boundary.encode(),
        b'Content-Disposition: form-data; name="metadata"',
        b"",
        metadata.encode(),
        b"--" + boundary.encode() + b"--",
        b"",
    ]
    return crlf.join(parts), f"multipart/form-data; boundary={boundary}"


def _parse_sse_events(body: bytes) -> list[dict[str, Any]]:
    """Extract the JSON payloads from an SSE ``data: <json>`` stream.

    The batch-ingest stream emits ``data: <json>\\n\\n`` frames only (the event
    variant is the ``event_type`` field inside the JSON), so a line-oriented
    scan of the ``data:`` lines recovers every progress and summary event.
    """
    events: list[dict[str, Any]] = []
    for line in body.decode("utf-8", "replace").splitlines():
        if line.startswith("data:"):
            try:
                events.append(json.loads(line[len("data:") :].strip()))
            except ValueError:
                continue
    return events


# --------------------------------------------------------------------------- #
# Checks -- each returns (status, detail)                                      #
# --------------------------------------------------------------------------- #
def _check_ingest(ctx: _Context) -> tuple[str, str]:
    """Ingest a probe document through the edge and confirm SAGE created it.

    With the document-store binding, ``retain_source`` uploads the bytes to the
    SharePoint library as a side effect of this ingest; the readback and audit
    checks prove they landed and are re-readable.
    """
    nonce = uuid.uuid4().hex
    ctx.probe_filename = f"sharepoint-validation-probe-{nonce}.md"
    content = (
        f"# Document-store vault-source validation probe\n\nValidation nonce: {nonce}\n"
    ).encode("utf-8")
    ctx.content_hash = _hash(content)
    metadata = json.dumps(
        {
            "infer_edges": False,
            "needs_review": True,
            "files": [
                {
                    "source_type": "markdown",
                    "parsed_metadata": {
                        "doc_type": "document",
                        "title": "Document-store vault-source validation probe",
                    },
                }
            ],
        }
    )
    body, content_type = _multipart(ctx.probe_filename, content, metadata)
    status, raw = _http(
        ctx, "POST", ctx.vault_url("/documents:batch"), data=body, content_type=content_type
    )
    if status != 200:
        return _FAIL, f"ingest_status={status}"
    events = _parse_sse_events(raw)
    summary = next((e for e in events if e.get("event_type") == "summary"), None)
    completed = next(
        (
            e
            for e in events
            if e.get("event_type") == "progress"
            and e.get("status") == "completed"
            and e.get("document_id")
        ),
        None,
    )
    if completed is None or summary is None:
        return _FAIL, f"no_completed_event events={len(events)}"
    error_count = int(summary.get("error_count", 0))
    created = summary.get("documents_created", {})
    new = int(created.get("new", 0)) + int(created.get("new_version", 0))
    if error_count != 0 or new < 1:
        return _FAIL, f"error_count={error_count} created={new}"
    ctx.document_id = completed["document_id"]
    ctx.write_state()
    return _PASS, f"doc={ctx.document_id} created={new}"


def _check_readback(ctx: _Context) -> tuple[str, str]:
    """Read the probe's source bytes back through the port and confirm the bytes
    are byte-identical to what was ingested (hash match over the decoded body).
    """
    if not ctx.document_id or not ctx.content_hash:
        return _FAIL, "no_probe_state"
    url = ctx.vault_url(f"/documents/{ctx.document_id}") + "?include_content=true"
    status, raw = _http(ctx, "GET", url)
    if status != 200:
        return _FAIL, f"readback_status={status}"
    payload = json.loads(raw)
    encoded = payload.get("content")
    if not encoded:
        return _FAIL, "no_content"
    decoded = base64.b64decode(encoded)
    observed = _hash(decoded)
    if observed != ctx.content_hash:
        return _FAIL, f"hash_mismatch observed={observed[:23]}.. bytes={len(decoded)}"
    return _PASS, f"hash_match bytes={len(decoded)}"


def _check_source_audit(ctx: _Context) -> tuple[str, str]:
    """Run the source-file integrity audit and confirm the probe is healthy --
    its retained bytes are present and hash-match the recorded content hash.
    """
    if not ctx.document_id:
        return _FAIL, "no_probe_state"
    status, raw = _http(
        ctx,
        "POST",
        ctx.vault_url("/admin/verify-source-files"),
        data=json.dumps({"check_hashes": True}).encode("utf-8"),
        content_type="application/json",
    )
    if status != 200:
        return _FAIL, f"audit_status={status}"
    report = json.loads(raw)
    entries = report.get("entries", [])
    summary = report.get("summary", {})
    unhealthy = {e.get("document_id") for e in entries}
    missing = int(summary.get("missing", 0))
    mismatch = int(summary.get("hash_mismatch", 0))
    checked = int(report.get("total_documents_checked", 0))
    # Assert the audit actually examined the corpus -- a 200 carrying an empty
    # report (zero documents checked) would otherwise pass vacuously, since a
    # healthy document is reported only by its absence from `entries`.
    if checked < 1:
        return _FAIL, "audit_checked_nothing"
    if ctx.document_id in unhealthy or missing or mismatch:
        return _FAIL, (
            f"missing={missing} hash_mismatch={mismatch} "
            f"probe_unhealthy={ctx.document_id in unhealthy}"
        )
    return _PASS, f"healthy={summary.get('healthy')} checked={checked}"


def _check_rediscover(ctx: _Context) -> tuple[str, str]:
    """Confirm the vault reloaded its configuration from the store after the
    restart -- it is present in the vault listing.
    """
    status, raw = _http(ctx, "GET", f"{ctx.base_url}/sage_vaults")
    if status != 200:
        return _FAIL, f"list_status={status}"
    vaults = json.loads(raw)
    ids = {v.get("id") for v in vaults}
    if ctx.vault_id not in ids:
        return _FAIL, f"vault_absent vaults={len(ids)}"
    return _PASS, f"vault_present vaults={len(ids)}"


_CHECKS: dict[str, Callable[[_Context], tuple[str, str]]] = {
    "ingest": _check_ingest,
    "readback": _check_readback,
    "source_audit": _check_source_audit,
    "rediscover": _check_rediscover,
}


def run_phase(ctx: _Context, phase: str) -> int:
    """Run every check for ``phase``; print a verdict line per check and a final
    ``result`` line. Return ``0`` iff all checks passed.
    """
    # The post-restart phase verifies the document the pre-restart phase
    # ingested; recover its id and hash from the state file before any check.
    if phase == "post-restart":
        ctx.load_state()
    ok = True
    for name in PHASE_CHECKS[phase]:
        try:
            status, detail = _CHECKS[name](ctx)
        except Exception as exc:  # noqa: BLE001 -- a check crash is a check failure
            status, detail = _FAIL, f"error={type(exc).__name__}:{exc}"
        if status != _PASS:
            ok = False
        print(f"check={name} status={status} {detail}".rstrip())
    print(f"result={'pass' if ok else 'fail'} phase={phase}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase", required=True, choices=sorted(PHASE_CHECKS), help="validation phase to run"
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SAGE_BASE_URL", ""),
        help="scheme+host of the SAGE edge (default: $SAGE_BASE_URL)",
    )
    parser.add_argument(
        "--vault-id",
        default=os.environ.get("SP_VALIDATE_VAULT_ID", "test"),
        help="vault to validate against (default: $SP_VALIDATE_VAULT_ID or 'test')",
    )
    parser.add_argument(
        "--state-file",
        default=os.environ.get("SP_VALIDATE_STATE_FILE", _DEFAULT_STATE_FILE),
        help="probe-state file the pre-restart phase writes and post-restart reads",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="per-request timeout (seconds)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the checks the phase would run and exit, without touching the network",
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        print(f"phase={args.phase} checks={','.join(PHASE_CHECKS[args.phase])}")
        return 0

    if not args.base_url:
        parser.error("--base-url (or $SAGE_BASE_URL) is required unless --dry-run")

    ctx = _Context(args.base_url, args.vault_id, args.state_file, args.timeout)
    return run_phase(ctx, args.phase)


if __name__ == "__main__":
    sys.exit(main())
