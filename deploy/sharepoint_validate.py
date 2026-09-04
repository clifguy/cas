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

Two probes, because the store treats two file classes differently. A store is
permitted to retain a copy that is not byte-identical to what it was handed
(CAS-ADR-043 point 6); tenant document stores exercise that permission on Office
packages, rewriting them at rest, and leave other formats alone. So:

* a **markdown** probe pins byte fidelity where it holds -- what reads back is
  exactly what was ingested;
* a **docx** probe pins the contract that stands in for byte fidelity where it
  does not -- provenance is the digest of the bytes the caller delivered and is
  what content identity keys on, the as-stored digest is a separate recorded
  fact over the retained copy, and it is the as-stored digest an integrity audit
  compares a re-read against.

One probe cannot carry both: asserting byte-identity for a rewritten format
fails the validation for entirely correct behavior, and asserting the digest
split for a format nothing rewrites asserts nothing at all.

Two phases, run either side of an operator-driven container restart:

* ``pre-restart`` -- ingest both probes, read the markdown probe's bytes back,
  check the docx probe's provenance, audit source-file integrity.
* ``post-restart`` -- re-discover the vault, then re-run the readback, provenance
  and audit checks against the same two documents (now served from the store
  with no ephemeral local copy). This is the live proof the sources survived the
  restart.

Each probe's document id, filename, delivered bytes and their digest are written
to a state file by the pre-restart phase and read by the post-restart phase, so
the two invocations -- separated by the restart -- agree on which documents to
re-verify and can re-deliver the same content to a container that has forgotten
everything but what the store holds.

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
import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
import zipfile
from typing import Any, Callable

#: Default probe-state path when neither ``--state-file`` nor
#: ``$SP_VALIDATE_STATE_FILE`` is given: the temp dir, so a bare run from a repo
#: checkout leaves no untracked file behind. The two phases share it because the
#: temp dir is stable within a host.
_DEFAULT_STATE_FILE = os.path.join(tempfile.gettempdir(), "sharepoint-validate-state.json")

#: The checks each phase runs, in order. The post-restart phase re-runs the
#: readback, provenance and audit checks against the same probe documents the
#: pre-restart phase ingested, plus a re-discovery check that the vault itself
#: reloaded its configuration from the store after the restart.
PHASE_CHECKS: dict[str, list[str]] = {
    "pre-restart": ["ingest", "readback", "provenance", "source_audit"],
    "post-restart": ["rediscover", "readback", "provenance", "source_audit"],
}

#: State-file keys for the two probes, and the source_type each is ingested as.
_MARKDOWN = "markdown"
_DOCX = "docx"

#: ``--expect-rewritten`` values. ``any`` accepts whatever the store does, which
#: is the right default for a driver that does not know its binding; a caller
#: that does know states it, and turns three otherwise-conditional provenance
#: assertions into ones that cannot lapse quietly.
_EXPECT_ANY = "any"
_EXPECT_CHOICES = (_EXPECT_ANY, "yes", "no")

_PASS = "PASS"  # noqa: S105 -- a verdict token, not a secret
_FAIL = "FAIL"


class _Probe:
    """One ingested probe document: what was sent, and what SAGE made of it.

    ``content_hash`` is the digest of the bytes the driver *delivered*, which is
    what the record's provenance hash must equal under every binding. It is not
    necessarily the digest of what reads back -- see the module docstring.
    """

    def __init__(self, filename: str, content: bytes, content_hash: str) -> None:
        self.filename = filename
        self.content = content
        self.content_hash = content_hash
        self.document_id: str | None = None

    def to_state(self) -> dict[str, Any]:
        # The bytes travel with the record. The post-restart phase re-delivers
        # them to prove the restarted container still recognises the content as
        # already held, and they cannot be rebuilt from the far side of the
        # restart -- each run's probes are minted against a fresh nonce.
        return {
            "document_id": self.document_id,
            "content_hash": self.content_hash,
            "filename": self.filename,
            "content": base64.b64encode(self.content).decode("ascii"),
        }


class _Context:
    """Per-run state threaded across the checks of a single phase."""

    def __init__(
        self,
        base_url: str,
        vault_id: str,
        state_file: str,
        timeout: float,
        expect_rewritten: str = _EXPECT_ANY,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.vault_id = vault_id
        self.state_file = state_file
        self.timeout = timeout
        self.expect_rewritten = expect_rewritten
        token = os.environ.get("AUTH_TOKEN", "")
        self.auth: dict[str, str] = {"Authorization": f"Bearer {token}"} if token else {}
        # Populated by the ingest check (pre-restart) or loaded from the state
        # file (post-restart), keyed by _MARKDOWN / _DOCX.
        self.probes: dict[str, _Probe] = {}

    def vault_url(self, suffix: str) -> str:
        return f"{self.base_url}/sage_vaults/{self.vault_id}{suffix}"

    def probe_ids(self) -> dict[str, str]:
        """Every known probe's document id, keyed by probe kind."""
        return {kind: p.document_id for kind, p in self.probes.items() if p.document_id}

    def load_state(self) -> bool:
        """Recover both probes from the state file. False unless both are whole.

        Total by construction: a state file that is absent, unreadable, not JSON,
        or JSON of the wrong shape leaves ``probes`` empty and returns False,
        rather than raising. The return value is advisory -- what actually keeps a
        half-recovered run from reporting green is each check's own
        ``no_probe_state`` guard, which is where that guarantee is enforced. This
        method's job is only to ensure a malformed file cannot take the phase down
        before a single verdict line is printed: the caller runs outside the
        per-check exception handler, so a raise here costs the operator the whole
        report and leaves a log consumer nothing to grep for.
        """
        self.probes = {}
        try:
            with open(self.state_file, encoding="utf-8") as handle:
                state = json.load(handle)
            recorded = state.get("probes") or {}
            for kind in (_MARKDOWN, _DOCX):
                entry = recorded.get(kind) or {}
                content = base64.b64decode(entry.get("content") or "")
                probe = _Probe(
                    entry.get("filename") or "", content, entry.get("content_hash") or ""
                )
                probe.document_id = entry.get("document_id")
                self.probes[kind] = probe
        except Exception:  # noqa: BLE001 -- any malformed state is simply no state
            self.probes = {}
            return False
        return all(p.document_id and p.content_hash and p.content for p in self.probes.values())

    def write_state(self) -> None:
        """Persist both probes as one unit -- the pre-restart phase's whole
        handoff to the post-restart phase, and the only run residue the driver
        leaves behind.
        """
        with open(self.state_file, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "vault_id": self.vault_id,
                    "probes": {kind: p.to_state() for kind, p in self.probes.items()},
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


def _multipart(
    filename: str, content: bytes, metadata: str, part_content_type: str
) -> tuple[bytes, str]:
    """Encode the batch-ingest multipart body: a single ``files`` part plus the
    JSON ``metadata`` form field. Returns ``(body, content_type)``.

    ``part_content_type`` is the uploaded part's own media type, distinct from
    the returned multipart content type of the request as a whole.
    """
    boundary = "----sagevalidate" + uuid.uuid4().hex
    crlf = b"\r\n"
    parts: list[bytes] = [
        b"--" + boundary.encode(),
        f'Content-Disposition: form-data; name="files"; filename="{filename}"'.encode(),
        b"Content-Type: " + part_content_type.encode(),
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
# Probes                                                                       #
# --------------------------------------------------------------------------- #
#: Media type of the uploaded part for each probe kind.
_PART_CONTENT_TYPES = {
    _MARKDOWN: "text/markdown",
    _DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

_WORDPROCESSINGML_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_OPC_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICEDOC_RELS_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'


def _build_docx(nonce: str) -> bytes:
    """Build a minimal, valid WordprocessingML package carrying ``nonce``.

    Assembled here rather than loaded from a committed fixture because each run
    needs bytes no previous run delivered: a fixture would collide with its own
    predecessor under duplicate detection on the second run, and the provenance
    check needs a first ingest that succeeds.

    Five parts: the content-type map, the package relationships naming the main
    document part, that part, its own relationships, and a styles part.

    The styles part is the one whose necessity is not obvious, so: it is not
    needed to *open* the package -- a reader synthesises a default styles part
    when none is present, and a four-part variant loads and yields both
    paragraphs. What it is needed for is heading extraction. A paragraph carries
    a style *id* (``Heading1``); resolving that to the style *name* ("Heading 1")
    the adapter matches on requires the styles part, and a synthesised default
    defines no such mapping. Drop it and the package still parses, the text still
    extracts, and the heading silently disappears -- which is the failure worth
    naming here, because nothing errors.
    """
    content_types = (
        f"{_XML_DECL}"
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package'
        '.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd'
        '.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd'
        '.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        "</Types>"
    )
    package_rels = (
        f"{_XML_DECL}"
        f'<Relationships xmlns="{_OPC_RELS_NS}">'
        f'<Relationship Id="rId1" Type="{_OFFICEDOC_RELS_NS}/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )
    document_rels = (
        f"{_XML_DECL}"
        f'<Relationships xmlns="{_OPC_RELS_NS}">'
        f'<Relationship Id="rId1" Type="{_OFFICEDOC_RELS_NS}/styles" Target="styles.xml"/>'
        "</Relationships>"
    )
    styles = (
        f"{_XML_DECL}"
        f'<w:styles xmlns:w="{_WORDPROCESSINGML_NS}">'
        '<w:style w:type="paragraph" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="Heading 1"/></w:style>'
        "</w:styles>"
    )
    document = (
        f"{_XML_DECL}"
        f'<w:document xmlns:w="{_WORDPROCESSINGML_NS}"><w:body>'
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>'
        "Document-store vault-source validation probe</w:t></w:r></w:p>"
        f"<w:p><w:r><w:t>Validation nonce: {nonce}</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("_rels/.rels", package_rels)
        package.writestr("word/_rels/document.xml.rels", document_rels)
        package.writestr("word/styles.xml", styles)
        package.writestr("word/document.xml", document)
    return buffer.getvalue()


def _build_probes() -> dict[str, _Probe]:
    """Mint both probes for this run against a single shared nonce."""
    nonce = uuid.uuid4().hex
    markdown = (
        f"# Document-store vault-source validation probe\n\nValidation nonce: {nonce}\n"
    ).encode("utf-8")
    docx = _build_docx(nonce)
    return {
        _MARKDOWN: _Probe(f"sharepoint-validation-probe-{nonce}.md", markdown, _hash(markdown)),
        _DOCX: _Probe(f"sharepoint-validation-probe-{nonce}.docx", docx, _hash(docx)),
    }


class _IngestOutcome:
    """What one batch-ingest call reported, parsed once.

    The batch surface answers on an SSE stream whose summary event carries the
    creation counts and the per-file error envelopes. Both callers need the same
    fields, and both would otherwise re-derive them from the raw event list --
    so the parse lives here, where a change to the stream's contract has one
    place to land rather than two.
    """

    def __init__(self, status: int, events: list[dict[str, Any]]) -> None:
        self.status = status
        self.events = events
        self.summary: dict[str, Any] | None = next(
            (e for e in events if e.get("event_type") == "summary"), None
        )
        self.completed: dict[str, Any] | None = next(
            (
                e
                for e in events
                if e.get("event_type") == "progress"
                and e.get("status") == "completed"
                and e.get("document_id")
            ),
            None,
        )
        summary = self.summary or {}
        created = summary.get("documents_created") or {}
        self.created = int(created.get("new", 0)) + int(created.get("new_version", 0))
        self.error_count = int(summary.get("error_count", 0))
        errors = summary.get("errors") or []
        self.error_codes = {e.get("code") for e in errors if e.get("code")}
        self.named_ids = {(e.get("detail") or {}).get("existing_document_id") for e in errors}
        self.errors = errors

    @property
    def ok(self) -> bool:
        """The call reached the endpoint and the endpoint answered in full."""
        return self.status == 200 and self.summary is not None


def _ingest_probe(ctx: _Context, kind: str, probe: _Probe) -> _IngestOutcome:
    """POST one probe to the batch-ingest endpoint and parse what came back."""
    metadata = json.dumps(
        {
            "infer_edges": False,
            "needs_review": True,
            "files": [
                {
                    "source_type": kind,
                    "parsed_metadata": {
                        "doc_type": "document",
                        "title": "Document-store vault-source validation probe",
                    },
                }
            ],
        }
    )
    body, content_type = _multipart(
        probe.filename, probe.content, metadata, _PART_CONTENT_TYPES[kind]
    )
    status, raw = _http(
        ctx, "POST", ctx.vault_url("/documents:batch"), data=body, content_type=content_type
    )
    return _IngestOutcome(status, _parse_sse_events(raw))


# --------------------------------------------------------------------------- #
# Checks -- each returns (status, detail)                                      #
# --------------------------------------------------------------------------- #
def _check_ingest(ctx: _Context) -> tuple[str, str]:
    """Ingest both probe documents through the edge and confirm SAGE created them.

    With the document-store binding, ``retain_source`` uploads the bytes to the
    SharePoint library as a side effect of each ingest; the readback, provenance
    and audit checks prove they landed and are re-readable.
    """
    ctx.probes = _build_probes()
    created_total = 0
    for kind, probe in ctx.probes.items():
        outcome = _ingest_probe(ctx, kind, probe)
        if outcome.status != 200:
            return _FAIL, f"{kind} ingest_status={outcome.status}"
        if outcome.completed is None or outcome.summary is None:
            return _FAIL, f"{kind} no_completed_event events={len(outcome.events)}"
        if outcome.error_count != 0 or outcome.created < 1:
            return _FAIL, (
                f"{kind} error_count={outcome.error_count} "
                f"created={outcome.created} {outcome.errors}"
            )
        probe.document_id = outcome.completed["document_id"]
        created_total += outcome.created
    ctx.write_state()
    ids = " ".join(f"{kind}={pid}" for kind, pid in sorted(ctx.probe_ids().items()))
    return _PASS, f"{ids} created={created_total}"


def _check_readback(ctx: _Context) -> tuple[str, str]:
    """Read the markdown probe's source bytes back through the port and confirm
    they are byte-identical to what was ingested (hash match over the decoded
    body).

    Markdown only, deliberately. Byte-identity is the contract for a format the
    store retains verbatim, and pinning it is worth doing; it is *not* the
    contract for one the store rewrites at rest, where retrieval returns the
    retained copy (CAS-ADR-043). The docx probe's contract is the provenance
    check's.
    """
    probe = ctx.probes.get(_MARKDOWN)
    if probe is None or not probe.document_id or not probe.content_hash:
        return _FAIL, "no_probe_state"
    url = ctx.vault_url(f"/documents/{probe.document_id}") + "?include_content=true"
    status, raw = _http(ctx, "GET", url)
    if status != 200:
        return _FAIL, f"readback_status={status}"
    payload = json.loads(raw)
    encoded = payload.get("content")
    if not encoded:
        return _FAIL, "no_content"
    decoded = base64.b64decode(encoded)
    observed = _hash(decoded)
    if observed != probe.content_hash:
        return _FAIL, f"hash_mismatch observed={observed[:23]}.. bytes={len(decoded)}"
    return _PASS, f"hash_match bytes={len(decoded)}"


def _check_provenance(ctx: _Context) -> tuple[str, str]:
    """Confirm the docx probe's two digests mean the two different things
    CAS-ADR-043 point 6 assigns them, and that content identity keys on the one
    that crosses the port.

    Four assertions, none of which needs to know which binding is behind the
    edge -- whether the store rewrote is *observed*, by comparing the retrieved
    bytes against the delivered ones, and the strongest true statement is
    asserted for whichever case that observation reports:

    1. The recorded provenance digest is the digest of the bytes delivered.
       Under a store that rewrites, this is what a build recording only one
       digest gets wrong: it would report the retained copy's.
    2. An as-stored digest is recorded at all.
    3. It is the digest of what actually reads back. Where the store rewrote,
       that makes it distinct from provenance -- not as a further assertion but
       as a consequence of this one and the first: the retained bytes are not
       the delivered bytes, and each digest is pinned to its own set.
    4. Re-delivering the identical bytes is refused as a duplicate naming this
       same document. Duplicate detection keys on provenance; keyed to the
       as-stored digest instead it could never fire for a rewritten format,
       because the rewrite is not reproducible.

    Three of those four only bite when the store actually rewrote, so a
    deployment that quietly stopped rewriting would take most of this check's
    value with it while every verdict stayed green. Observation alone cannot
    catch that -- the driver would simply see, and report, the new truth. So the
    *caller* states what it expects: ``ctx.expect_rewritten`` is ``"any"`` by
    default, and set to ``"yes"``/``"no"`` by a caller that knows which binding
    is behind the edge. The check stays binding-agnostic; the expectation is the
    caller's knowledge, not the driver's assumption.

    The retained bytes come from the raw content endpoint rather than inlined
    into the record: a binary container is not scannable text, so inlining it
    into JSON is refused (CAS-ADR-039), and the raw byte channel is the
    sanctioned way to ask a binding for the file it holds.
    """
    probe = ctx.probes.get(_DOCX)
    if probe is None or not probe.document_id or not probe.content_hash:
        return _FAIL, "no_probe_state"
    status, raw = _http(ctx, "GET", ctx.vault_url(f"/documents/{probe.document_id}"))
    if status != 200:
        return _FAIL, f"record_status={status}"
    payload = json.loads(raw)
    provenance = payload.get("source_content_hash")
    as_stored = payload.get("stored_content_hash")

    status, retained = _http(ctx, "GET", ctx.vault_url(f"/documents/{probe.document_id}/content"))
    if status != 200:
        return _FAIL, f"content_status={status}"
    retrieved = _hash(retained)

    if provenance != probe.content_hash:
        return _FAIL, (
            f"provenance_not_delivered_digest recorded={str(provenance)[:23]}.. "
            f"delivered={probe.content_hash[:23]}.."
        )
    if not as_stored:
        return _FAIL, "no_stored_content_hash"
    rewritten = retrieved != probe.content_hash
    observed = "yes" if rewritten else "no"
    if as_stored != retrieved:
        return _FAIL, (
            f"stored_not_retrieved_digest stored={as_stored[:23]}.. "
            f"retrieved={retrieved[:23]}.. rewritten={observed}"
        )
    if ctx.expect_rewritten != _EXPECT_ANY and observed != ctx.expect_rewritten:
        return _FAIL, f"rewritten={observed} expected={ctx.expect_rewritten}"

    outcome = _ingest_probe(ctx, _DOCX, probe)
    if not outcome.ok:
        return _FAIL, f"redeliver_status={outcome.status} events={len(outcome.events)}"
    if (
        outcome.created
        or "duplicate_content" not in outcome.error_codes
        or probe.document_id not in outcome.named_ids
    ):
        return _FAIL, (
            f"redelivery_not_deduplicated created={outcome.created} "
            f"codes={sorted(c for c in outcome.error_codes if c)}"
        )
    return _PASS, f"rewritten={observed} expected={ctx.expect_rewritten} provenance_pinned"


def _check_source_audit(ctx: _Context) -> tuple[str, str]:
    """Run the source-file integrity audit and confirm both probes are healthy --
    their retained bytes are present, are the retained copy itself rather than a
    link to it, sit inside the vault's source tree, and hash to what the record
    expects.

    Scoped to this run's probes, and deliberately so. The audit covers the whole
    vault, but the vault this driver runs against is a disposable validation
    corpus that accumulates the residue of every previous run: a whole-vault
    verdict here would report on documents this validation neither created nor
    can repair, and one such document would red every subsequent run for good.
    What the driver ingested is what it can honestly speak for.

    The verdict is taken from ``entries`` membership rather than from any
    per-status count. The audit reports a document by naming it, and names it
    for every way a retained copy can be wrong; reading named statuses out of
    the summary instead would hold a copy of that list, and would pass silently
    on any status added after this line was written.

    A cost worth knowing before it surprises someone: the *verdict* is scoped to
    two documents, but the *work* is not. The audit walks every document in the
    vault, and with hash checking on, each one costs the store a metadata read
    plus a full streamed download. Each run adds two documents permanently --
    there is no delete route on this API -- so the walk grows by two per run and
    is paid twice, once per phase. Far enough out that is a timeout on a healthy
    deployment, which will read as a store outage rather than as accumulated
    residue. The request carries no way to scope the work from the client, so
    bounding it needs a purge capability the store side does not yet offer.
    """
    probe_ids = ctx.probe_ids()
    if len(probe_ids) < len(ctx.probes) or not probe_ids:
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
    checked = int(report.get("total_documents_checked", 0))
    reported = {e.get("document_id"): e.get("integrity_status") for e in entries}
    # Assert the audit examined at least the documents this run is speaking for.
    # A healthy document is reported only by its absence from `entries`, so a
    # report that walked fewer records than there are probes would pass
    # vacuously -- and would do it while claiming, in the PASS detail, to have
    # found every probe healthy.
    if checked < len(probe_ids):
        return _FAIL, f"audit_checked_too_few checked={checked} probes={len(probe_ids)}"
    hits = {kind: reported[pid] for kind, pid in probe_ids.items() if pid in reported}
    if hits:
        detail = " ".join(f"{kind}={status}" for kind, status in sorted(hits.items()))
        return _FAIL, f"probe_unhealthy {detail} checked={checked}"
    return _PASS, f"probes_healthy={len(probe_ids)} checked={checked}"


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
    "provenance": _check_provenance,
    "source_audit": _check_source_audit,
    "rediscover": _check_rediscover,
}


def run_phase(ctx: _Context, phase: str) -> int:
    """Run every check for ``phase``; print a verdict line per check and a final
    ``result`` line. Return ``0`` iff all checks passed.
    """
    # The post-restart phase verifies the documents the pre-restart phase
    # ingested; recover their ids and hashes from the state file before any check.
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
        "--expect-rewritten",
        choices=_EXPECT_CHOICES,
        default=os.environ.get("SP_VALIDATE_EXPECT_REWRITTEN", _EXPECT_ANY),
        help=(
            "whether the vault-source store is expected to rewrite Office packages at "
            "rest: 'yes' for a tenant document store, 'no' for a store that retains "
            "verbatim, 'any' (default: $SP_VALIDATE_EXPECT_REWRITTEN or 'any') to accept "
            "either. Several provenance assertions only bite where a rewrite occurs, so a "
            "caller that knows its binding should say so -- otherwise a store that stopped "
            "rewriting takes that coverage with it and every verdict stays green"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the checks the phase would run and exit, without touching the network",
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        print(f"phase={args.phase} checks={','.join(PHASE_CHECKS[args.phase])}")
        return 0

    # argparse validates `choices` only against a value it parsed off the command
    # line, never against a default -- so an unrecognized $SP_VALIDATE_EXPECT_REWRITTEN
    # would arrive here unchallenged and, matching no branch, behave exactly like
    # "any". That is this option's own failure mode one level down: an operator who
    # set the expectation and mistyped it would get the unenforced run they were
    # trying to avoid, and a green one.
    if args.expect_rewritten not in _EXPECT_CHOICES:
        parser.error(
            f"invalid $SP_VALIDATE_EXPECT_REWRITTEN {args.expect_rewritten!r}; "
            f"expected one of {', '.join(_EXPECT_CHOICES)}"
        )

    if not args.base_url:
        parser.error("--base-url (or $SAGE_BASE_URL) is required unless --dry-run")

    ctx = _Context(
        args.base_url, args.vault_id, args.state_file, args.timeout, args.expect_rewritten
    )
    return run_phase(ctx, args.phase)


if __name__ == "__main__":
    sys.exit(main())
