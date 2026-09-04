"""HTTP integration tests for the SAGE Core batch-ingest upload endpoint.

POST /sage_vaults/{vault_id}/documents:batch.

The hosted-profile bulk-ingest surface: multipart upload of file content
(no shared filesystem) + a JSON metadata envelope, run through the same
three-phase BatchIngestService the co-located profile drives, streaming
SSE progress. Pre-stream validation (empty list, metadata/file-count
mismatch, invalid metadata JSON, unknown vault) resolves synchronously
BEFORE the stream opens and returns an application/json ErrorResponse
with no SSE events emitted. Staged upload files are removed once the
stream is exhausted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.backend.asgi import create_bff_app
from app.backend.auth.config import BffAuthContext, BffAuthSettings
from app.backend.auth.sage_client import ObOSageClient
from app.backend.auth.session_store import InMemorySessionStore, Session
from app.backend.transport import HttpSageTransport
from sage import mcp_server
from sage.adapters.stubs import StubContentStore
from sage.app import _initialize_services, create_app
from sage.config import SageCoreConfig, VaultConfig
from sage.mcp_init import SAGEServices
from sage.models.enums import SourceType
from sage.services import batch_ingest_stream
from sage.services.batch_ingest import BatchIngestService, FileDescriptor
from sage.services.batch_ingest_stream import UploadedFile, stream_uploaded_batch_ingest


class _StubOidc:
    """Minimal OIDC stub: mints a fixed delegated SAGE token from a session."""

    def acquire_sage_token(self, token_cache: str) -> str:  # noqa: ARG002
        return "delegated-token"  # noqa: S105 -- test fixture token, not a real secret


def _parse_sse_events(text: str) -> list[dict]:
    """Parse an SSE response body into a list of JSON event payloads.

    Each event is a ``data: <json>\\n\\n`` block. Mirrors the helper in
    tests/sage/test_reabstract_deferred_router.py.
    """
    return [
        json.loads(line.replace("data: ", "", 1))
        for line in text.strip().split("\n")
        if line.startswith("data: ")
    ]


def _md_part(name: str, body: bytes) -> tuple[str, tuple[str, bytes, str]]:
    """Build one httpx multipart file part under the ``files`` field."""
    return ("files", (name, body, "text/markdown"))


@pytest.fixture
async def batch_app(minimal_vault_config_dict, monkeypatch):
    """FastAPI app with one markdown-enabled vault wired through the normal
    initialization path so the batch-ingest endpoint is reachable end-to-end."""
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    await _initialize_services(
        app,
        config,
        content_store_factory=lambda _brain: StubContentStore(),
    )
    vault_id = config.vault.id
    yield app, vault_id, config

    await asyncio.sleep(0.05)
    registry: dict[str, SAGEServices] = app.state.vault_registry
    if vault_id in registry:
        registry[vault_id].close_timing()
        await registry[vault_id].graph_store.close()
    mcp_server._vaults.clear()


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# B1 -- happy path (SSE shape)
# ---------------------------------------------------------------------------


async def test_b1_batch_upload_streams_progress_and_summary(batch_app):
    """Two uploaded markdown files yield 200 text/event-stream: a
    started+completed progress pair per file and one trailing summary
    whose documents_created.new == 2.

    Anti-coincidental-pass: a route that returned a canned/JSON body
    instead of running the pipeline fails the content-type assertion and
    the document-count assertion immediately.
    """
    app, vault_id, _config = batch_app
    metadata = {
        "infer_edges": True,
        "needs_review": True,
        "files": [{"source_type": "markdown"}, {"source_type": "markdown"}],
    }
    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[
                _md_part("alpha.md", b"# Alpha\n\nAlpha body."),
                _md_part("beta.md", b"# Beta\n\nBeta body."),
            ],
            data={"metadata": json.dumps(metadata)},
        )

    assert resp.status_code == 200, resp.text
    assert "text/event-stream" in resp.headers.get("content-type", ""), resp.headers

    events = _parse_sse_events(resp.text)
    progress = [e for e in events if e["event_type"] == "progress"]
    summaries = [e for e in events if e["event_type"] == "summary"]
    assert [p["status"] for p in progress] == ["started", "completed", "started", "completed"]
    assert len(summaries) == 1
    assert events[-1]["event_type"] == "summary", "summary event must be last"

    summary = summaries[0]
    assert summary["documents_created"]["new"] == 2
    assert isinstance(summary["edges_created"], dict)
    assert summary["error_count"] == 0
    # Each completed progress event carries the assigned document id.
    completed = [p for p in progress if p["status"] == "completed"]
    assert all("document_id" in p for p in completed)


# ---------------------------------------------------------------------------
# B2 -- provenance parity (AC4 core)
# ---------------------------------------------------------------------------


async def test_b2_upload_provenance_hashes_uploaded_bytes(batch_app):
    """A file uploaded through the endpoint lands with the same
    source_content_hash the desktop path would produce -- SHA-256 of the
    uploaded bytes, the canonical form every source adapter emits -- and
    with metadata_confirmed=False (needs_review default).

    Anti-coincidental-pass: hashing the staged temp path or a wrong file
    instead of the uploaded content would diverge from the independently
    computed digest; the content-sensitivity control (different bytes ->
    different hash) proves the hash tracks content, not the request.
    """
    app, vault_id, _config = batch_app
    services: SAGEServices = app.state.vault_registry[vault_id]
    content = b"# Provenance\n\nHashed from the uploaded bytes."
    expected_hash = "sha256:" + hashlib.sha256(content).hexdigest()

    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[_md_part("via_upload.md", content)],
            data={"metadata": json.dumps({"files": [{"source_type": "markdown"}]})},
        )
    assert resp.status_code == 200, resp.text
    completed = [
        e
        for e in _parse_sse_events(resp.text)
        if e["event_type"] == "progress" and e["status"] == "completed"
    ]
    assert len(completed) == 1
    upload_doc = await services.graph_store.get_document(completed[0]["document_id"])

    assert upload_doc.source_content_hash == expected_hash
    assert upload_doc.metadata_confirmed is False

    # Content-sensitivity control: different bytes -> different hash.
    other = b"# Other\n\nDifferent bytes entirely."
    async with _client(app) as client:
        resp2 = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[_md_part("other.md", other)],
            data={"metadata": json.dumps({"files": [{"source_type": "markdown"}]})},
        )
    other_completed = [
        e
        for e in _parse_sse_events(resp2.text)
        if e["event_type"] == "progress" and e["status"] == "completed"
    ]
    other_doc = await services.graph_store.get_document(other_completed[0]["document_id"])
    assert other_doc.source_content_hash != upload_doc.source_content_hash
    assert other_doc.source_content_hash == "sha256:" + hashlib.sha256(other).hexdigest()


# ---------------------------------------------------------------------------
# B3 -- filename preservation through temp staging
# ---------------------------------------------------------------------------


async def test_b3_staging_preserves_original_filename(batch_app):
    """The uploaded file's original name survives temp staging: the landed
    document's source_path carries the original stem, not a random
    mkdtemp filename.

    Anti-coincidental-pass: staging to a random ``mkstemp`` name (or
    hashing the temp path) would put that random name in source_path and
    fail the substring assertion.
    """
    app, vault_id, _config = batch_app
    services: SAGEServices = app.state.vault_registry[vault_id]
    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[_md_part("report_v2.md", b"# Report\n\nDistinctive name.")],
            data={"metadata": json.dumps({"files": [{"source_type": "markdown"}]})},
        )
    assert resp.status_code == 200, resp.text
    completed = [
        e
        for e in _parse_sse_events(resp.text)
        if e["event_type"] == "progress" and e["status"] == "completed"
    ]
    doc = await services.graph_store.get_document(completed[0]["document_id"])
    assert "report_v2" in doc.source_path, doc.source_path


# ---------------------------------------------------------------------------
# B4 -- per-file failure isolation (CAS-ADR-029)
# ---------------------------------------------------------------------------


async def test_b4_per_file_failure_isolation(batch_app):
    """One good file + one with an invalid source_type: the bad file emits
    progress(failed) and a summary error, the good file still ingests, and
    the batch is not rolled back.

    Anti-coincidental-pass: an atomic batch would abort on the bad file and
    produce zero completed documents; this asserts the good file completed
    AND error_count == 1.
    """
    app, vault_id, _config = batch_app
    metadata = {
        "files": [{"source_type": "markdown"}, {"source_type": "not_a_real_adapter"}],
    }
    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[
                _md_part("good.md", b"# Good\n\nIngestible."),
                _md_part("bad.md", b"# Bad\n\nUnknown source_type."),
            ],
            data={"metadata": json.dumps(metadata)},
        )
    assert resp.status_code == 200, resp.text
    events = _parse_sse_events(resp.text)
    statuses = [e["status"] for e in events if e["event_type"] == "progress"]
    assert statuses.count("completed") == 1
    assert statuses.count("failed") == 1
    summary = next(e for e in events if e["event_type"] == "summary")
    assert summary["error_count"] == 1
    assert summary["documents_created"]["new"] == 1
    assert summary["errors"][0]["filename"] == "bad.md"


@pytest.mark.asyncio
async def test_b9_refused_upload_entry_names_the_callers_file_with_code(
    batch_app, tmp_vault_dir, tmp_path
):
    """A retention refusal on an uploaded file reaches the summary as a typed
    entry whose detail names the file as the caller uploaded it, not the
    server-side staging path it was written to.

    The upload leg stages each part under a temporary directory and hands the
    staged path down the pipeline, so a refusal raised below that point knows
    only the staging location. The caller's own spelling is the upload's
    filename, and that is what the entry must carry.

    Anti-coincidental-pass: the upload is named with a directory component,
    so the caller's spelling (``inbox/refused.md``) differs from both the
    staged path and the sanitized basename the file is staged under; an
    implementation reporting either of those fails the equality. The explicit
    negative on the staging-directory prefix names the defect directly.
    Against the message-only collection this fails on the missing ``code``
    key.
    """
    app, vault_id, _config = batch_app
    imports = tmp_vault_dir / "sources" / "imports"
    imports.mkdir(parents=True, exist_ok=True)
    # Dangling, so retention falls through to its write exit and refuses there.
    (imports / "refused.md").symlink_to(tmp_path / "nowhere.md")

    metadata = {"files": [{"source_type": "markdown"}, {"source_type": "markdown"}]}
    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[
                _md_part("good.md", b"# Good\n\nIngestible."),
                _md_part("inbox/refused.md", b"# Refused\n\nRetention refuses this one."),
            ],
            data={"metadata": json.dumps(metadata)},
        )
    assert resp.status_code == 200, resp.text
    events = _parse_sse_events(resp.text)
    summary = next(e for e in events if e["event_type"] == "summary")
    assert summary["error_count"] == 1, summary
    assert summary["documents_created"]["new"] == 1
    entry = summary["errors"][0]
    assert entry["code"] == "vault_source_path_refused"
    assert entry["detail"] == {"source_path": "inbox/refused.md"}
    assert entry["source_path"] == "inbox/refused.md"
    assert entry["filename"] == "refused.md"
    assert "sage-batch-ingest-" not in json.dumps(entry)


# ---------------------------------------------------------------------------
# B6 -- pre-stream validation stays application/json
# ---------------------------------------------------------------------------


async def test_b6_invalid_metadata_json_is_json_400(batch_app):
    """Malformed `metadata` JSON returns 400 invalid_batch_metadata as
    application/json -- NOT a started text/event-stream body.

    Anti-coincidental-pass: a route that validated inside the SSE
    generator would have already emitted content-type text/event-stream;
    the application/json assertion catches it.
    """
    app, vault_id, _config = batch_app
    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[_md_part("a.md", b"# A\n\nbody")],
            data={"metadata": "{not valid json"},
        )
    assert resp.status_code == 400, resp.text
    assert "application/json" in resp.headers.get("content-type", "")
    assert resp.json()["code"] == "invalid_batch_metadata"


async def test_b6_count_mismatch_is_json_400(batch_app):
    """A metadata.files length that disagrees with the uploaded file count
    returns 400 invalid_batch_metadata as application/json."""
    app, vault_id, _config = batch_app
    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[_md_part("a.md", b"# A\n\nbody")],
            data={"metadata": json.dumps({"files": []})},
        )
    assert resp.status_code == 400, resp.text
    assert "application/json" in resp.headers.get("content-type", "")
    assert resp.json()["code"] == "invalid_batch_metadata"


async def test_b6_unknown_vault_is_json_404(batch_app):
    """An unregistered vault id resolves to 404 vault_not_found as
    application/json before the stream opens."""
    app, _vault_id, _config = batch_app
    async with _client(app) as client:
        resp = await client.post(
            "/sage_vaults/no_such_vault/documents:batch",
            files=[_md_part("a.md", b"# A\n\nbody")],
            data={"metadata": json.dumps({"files": [{"source_type": "markdown"}]})},
        )
    assert resp.status_code == 404, resp.text
    assert "application/json" in resp.headers.get("content-type", "")
    assert resp.json()["code"] == "vault_not_found"


async def test_b6_empty_upload_is_json_400(batch_app):
    """No uploaded files returns 400 empty_file_list as application/json."""
    app, vault_id, _config = batch_app
    async with _client(app) as client:
        # A single empty-named part forces multipart encoding with no real
        # file content; the handler treats an absent/empty upload as empty.
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            data={"metadata": json.dumps({"files": []})},
            files={"_force_multipart": ("", b"", "application/octet-stream")},
        )
    assert resp.status_code == 400, resp.text
    assert "application/json" in resp.headers.get("content-type", "")
    assert resp.json()["code"] == "empty_file_list"


# ---------------------------------------------------------------------------
# B7 -- staged upload files are cleaned up
# ---------------------------------------------------------------------------


async def test_b7_staging_dir_is_removed_after_stream(batch_app, monkeypatch):
    """The temporary staging directory created for the upload is removed
    once the SSE stream is exhausted.

    Anti-coincidental-pass: removing the ``finally`` cleanup leaves the
    staging directory on disk and fails the not-exists assertion.
    """
    app, vault_id, _config = batch_app
    created: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def _spy_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(path)
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", _spy_mkdtemp)

    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[_md_part("cleanup.md", b"# Cleanup\n\nbody")],
            data={"metadata": json.dumps({"files": [{"source_type": "markdown"}]})},
        )
    assert resp.status_code == 200, resp.text
    staging = [p for p in created if "sage-batch-ingest-" in p]
    assert staging, "endpoint did not create a staging directory"
    for path in staging:
        assert not os.path.exists(path), f"staging dir not cleaned up: {path}"


# ---------------------------------------------------------------------------
# B5 -- cross-profile summary parity (AC3)
# ---------------------------------------------------------------------------


async def test_b5_endpoint_summary_matches_direct_orchestrator(batch_app):
    """The summary the endpoint streams (the hosted path) equals the summary
    the in-process orchestrator produces (the co-located /app/ingest path)
    for a structurally equivalent input -- both drive the same
    BatchIngestService.

    Distinct bytes are used for the two paths only to dodge SAGE's
    hash-only duplicate-content detection; the source_type, metadata shape,
    and edge structure are identical, so the summary counts must match.
    Anti-coincidental-pass: a path that set needs_review differently would
    shift metadata_pending; one that skipped edge inference would shift
    edges_created.
    """
    app, vault_id, _config = batch_app
    services: SAGEServices = app.state.vault_registry[vault_id]

    # Hosted path: upload through the endpoint.
    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[_md_part("hosted_path.md", b"# Hosted\n\nvia the endpoint.")],
            data={
                "metadata": json.dumps(
                    {
                        "infer_edges": True,
                        "needs_review": True,
                        "files": [{"source_type": "markdown"}],
                    }
                )
            },
        )
    endpoint_summary = next(e for e in _parse_sse_events(resp.text) if e["event_type"] == "summary")

    # Co-located path: drive the orchestrator in-process.
    local_dir = Path(tempfile.mkdtemp(prefix="b5-local-"))
    local_file = local_dir / "colocated_path.md"
    local_file.write_bytes(b"# Co-located\n\nvia the in-process orchestrator.")
    direct = await BatchIngestService().run(
        files=[FileDescriptor(file_path=str(local_file), source_type="markdown")],
        vault_services=services,
        infer_edges=True,
        needs_review=True,
    )

    assert endpoint_summary["documents_created"] == {
        "new": direct.docs_new,
        "new_version": direct.docs_version,
    }
    assert endpoint_summary["edges_created"] == direct.edges_created
    assert endpoint_summary["metadata_pending"] == direct.metadata_pending
    assert endpoint_summary["error_count"] == direct.error_count


# ---------------------------------------------------------------------------
# B8 -- hosted path end-to-end: cloud BFF reverse proxy -> SAGE batch endpoint
# ---------------------------------------------------------------------------


async def test_b8_cloud_proxy_forwards_upload_to_batch_endpoint(batch_app):
    """A file uploaded to the standalone (cloud) BFF's reverse proxy is
    forwarded to the SAGE batch-ingest endpoint under the signed-in user's
    identity, the SSE stream is relayed back, and the document lands in the
    SAGE vault -- the AC4 hosted-path end-to-end (SPA -> BFF proxy -> SAGE).

    Anti-coincidental-pass: a proxy that mangled the multipart body would
    fail SAGE-side parsing (no completed event / no landed document); one
    that dropped the SSE content-type would fail the content-type assertion.
    """
    sage_app, vault_id, _config = batch_app
    services: SAGEServices = sage_app.state.vault_registry[vault_id]

    oidc = _StubOidc()
    settings = BffAuthSettings(
        tenant_id="t",
        client_id="c",
        client_secret="s",  # noqa: S106 -- test fixture, not a real secret
        sage_app_id_uri="api://sage",
        sage_base_url="http://sage.test",
    )
    store = InMemorySessionStore()
    await store.create_session(
        Session(
            session_id="sid-1",
            subject="user-1",
            claims={"name": "Test User"},
            token_cache="cache-blob",
            expires_at=time.time() + 3600,
        )
    )

    bff = create_bff_app(stack_config=SageCoreConfig(profile="cloud"))
    bff.state.bff_auth = BffAuthContext(settings=settings, oidc=oidc, store=store)
    sage_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=sage_app), base_url="http://sage.test"
    )
    bff.state.sage_transport = HttpSageTransport(
        ObOSageClient("http://sage.test", oidc, client=sage_client)
    )

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=bff),
            base_url="http://bff.test",
            cookies={settings.session_cookie_name: "sid-1"},
        ) as client:
            resp = await client.post(
                f"/sage_vaults/{vault_id}/documents:batch",
                files=[_md_part("uploaded.md", b"# Uploaded\n\nThrough the cloud proxy.")],
                data={"metadata": json.dumps({"files": [{"source_type": "markdown"}]})},
            )
    finally:
        await sage_client.aclose()

    assert resp.status_code == 200, resp.text
    assert "text/event-stream" in resp.headers.get("content-type", ""), resp.headers
    completed = [
        e
        for e in _parse_sse_events(resp.text)
        if e["event_type"] == "progress" and e["status"] == "completed"
    ]
    assert len(completed) == 1, resp.text
    doc = await services.graph_store.get_document(completed[0]["document_id"])
    assert doc is not None
    assert "uploaded" in doc.source_path


# ---------------------------------------------------------------------------
# B10-B13 -- same-named uploads stage apart
# ---------------------------------------------------------------------------


def _completed_by_index(events: list[dict]) -> dict[int, dict]:
    """Map each ``progress/completed`` event to its ``file_index``."""
    return {
        e["file_index"]: e
        for e in events
        if e["event_type"] == "progress" and e["status"] == "completed"
    }


async def test_b10_same_named_uploads_each_ingest_from_their_own_bytes(batch_app):
    """Two uploaded parts that share a filename both land, each carrying the
    provenance hash of its own bytes.

    Staging every part under one directory by basename lets a later
    same-named part replace an earlier one before either is ingested: one
    document lands with the last part's bytes, and the lost part is never
    reported.

    Anti-coincidental-pass: the hash is checked per ``file_index`` against
    that part's own body, so shared staging -- where index 0's document
    carries index 1's bytes -- fails on the first part even though two
    completed events may still be emitted. ``documents_created.new == 2``
    with distinct ids excludes the two parts collapsing into one document
    and a version of it.
    """
    app, vault_id, _config = batch_app
    services: SAGEServices = app.state.vault_registry[vault_id]
    bodies = [b"# Zero\n\nFirst part.", b"# One\n\nSecond part."]
    metadata = {"files": [{"source_type": "markdown"}, {"source_type": "markdown"}]}

    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[_md_part("dup.md", bodies[0]), _md_part("dup.md", bodies[1])],
            data={"metadata": json.dumps(metadata)},
        )
    assert resp.status_code == 200, resp.text
    events = _parse_sse_events(resp.text)
    summary = next(e for e in events if e["event_type"] == "summary")
    assert summary["error_count"] == 0, summary
    assert summary["documents_created"]["new"] == 2, summary

    completed = _completed_by_index(events)
    assert sorted(completed) == [0, 1]
    ids = {e["document_id"] for e in completed.values()}
    assert len(ids) == 2, completed

    hashes = []
    for index, body in enumerate(bodies):
        doc = await services.graph_store.get_document(completed[index]["document_id"])
        expected = "sha256:" + hashlib.sha256(body).hexdigest()
        assert doc.source_content_hash == expected, (index, doc.source_content_hash)
        hashes.append(doc.source_content_hash)
    assert hashes[0] != hashes[1]


async def test_b11_same_named_uploads_keep_the_parsed_stem_and_leak_no_separator(batch_app):
    """Whatever keeps two same-named parts apart in staging stays out of the
    vault: the first part is retained at exactly the path a single upload of
    that name lands at, and the second at retention's own content-hash
    disambiguation of it.

    Anti-coincidental-pass: the retained paths are pinned to their exact
    forms rather than to the absence of one spelling of a leak, so any
    staging segment surviving into retention -- numeric, prefixed, or a
    suffix on the basename -- fails the equality or the pattern; a lost
    part (one document) fails the count. B3 establishes the single-upload
    form the first equality assumes.
    """
    app, vault_id, _config = batch_app
    services: SAGEServices = app.state.vault_registry[vault_id]
    metadata = {"files": [{"source_type": "markdown"}, {"source_type": "markdown"}]}

    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[
                _md_part("report_v2.md", b"# Report\n\nFirst same-named part."),
                _md_part("report_v2.md", b"# Report\n\nSecond same-named part, marker."),
            ],
            data={"metadata": json.dumps(metadata)},
        )
    assert resp.status_code == 200, resp.text
    completed = _completed_by_index(_parse_sse_events(resp.text))
    assert sorted(completed) == [0, 1], completed

    first = await services.graph_store.get_document(completed[0]["document_id"])
    second = await services.graph_store.get_document(completed[1]["document_id"])
    assert first.source_path == "imports/report_v2.md", first.source_path
    assert re.fullmatch(r"imports/report_v2_[0-9a-f]{8}\.md", second.source_path), (
        second.source_path
    )
    for path in (first.source_path, second.source_path):
        assert "sage-batch-ingest-" not in path


async def test_b12_failed_same_named_part_is_identified_by_file_index(batch_app):
    """When several parts share a filename, each failed part's summary entry
    names its position in the batch, the same ``file_index`` the progress
    events report -- the only field that tells them apart, since on this leg
    ``filename`` and ``source_path`` are both the upload's name.

    Anti-coincidental-pass: two same-named parts fail, at positions 1 and 2,
    so the summary holds two entries identical in every field but the
    index; an index hard-coded to zero reports ``[0, 0]`` and one taken from
    the entry's ordinal among the errors reports ``[0, 1]``, and both fail
    the equality against the failed progress events' own indices. The
    equality-after-drop assertion pins that nothing else separates them.
    """
    app, vault_id, _config = batch_app
    metadata = {
        "files": [
            {"source_type": "markdown"},
            {"source_type": "not_a_real_adapter"},
            {"source_type": "not_a_real_adapter"},
        ],
    }
    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[
                _md_part("twin.md", b"# Twin\n\nThis one ingests."),
                _md_part("twin.md", b"# Twin\n\nThis one fails."),
                _md_part("twin.md", b"# Twin\n\nSo does this one."),
            ],
            data={"metadata": json.dumps(metadata)},
        )
    assert resp.status_code == 200, resp.text
    events = _parse_sse_events(resp.text)
    failed = [e for e in events if e["event_type"] == "progress" and e["status"] == "failed"]
    assert [e["file_index"] for e in failed] == [1, 2], failed
    assert sorted(_completed_by_index(events)) == [0]

    summary = next(e for e in events if e["event_type"] == "summary")
    assert summary["error_count"] == 2, summary
    entries = summary["errors"]
    assert [e["file_index"] for e in entries] == [e["file_index"] for e in failed] == [1, 2]
    assert [e["filename"] for e in entries] == ["twin.md", "twin.md"]
    assert [e["source_path"] for e in entries] == ["twin.md", "twin.md"]
    without_index = [{k: v for k, v in e.items() if k != "file_index"} for e in entries]
    assert without_index[0] == without_index[1], without_index


async def test_b13_stage_writes_same_named_parts_to_distinct_paths(monkeypatch):
    """The staging step hands the pipeline one path per part, each holding
    that part's bytes under the upload's own basename, even when two parts
    share a filename -- and the staging root is gone once the stream is
    exhausted.

    Anti-coincidental-pass: the bytes and basenames are read inside the
    patched pipeline consumer, while staging still exists, so a shared
    staged path reads the second part's bytes for the first descriptor and
    fails the equality; a basename-suffix scheme fails on the basename.
    """
    seen: list[tuple[str, bytes, str | None]] = []
    roots: set[str] = set()

    async def fake_stream(descriptors, vault_services, infer_edges=True, needs_review=True):
        for fd in descriptors:
            staged = Path(fd.file_path)
            seen.append((staged.name, staged.read_bytes(), fd.declared_source))
            roots.add(next(p for p in staged.parents if p.name.startswith("sage-batch-ingest-")))
        yield "data: {}\n\n"

    monkeypatch.setattr(batch_ingest_stream, "batch_ingest_sse_stream", fake_stream)
    uploads = [
        UploadedFile(filename="dup.md", content=b"zero", source_type="markdown"),
        UploadedFile(filename="dup.md", content=b"one", source_type="markdown"),
    ]

    chunks = [
        chunk async for chunk in stream_uploaded_batch_ingest(uploads, vault_services=object())
    ]

    assert chunks == ["data: {}\n\n"]
    assert seen == [("dup.md", b"zero", "dup.md"), ("dup.md", b"one", "dup.md")]
    assert len(roots) == 1, roots
    assert not os.path.exists(next(iter(roots)))


async def test_b14_degenerate_upload_names_stage_under_a_synthetic_basename(monkeypatch):
    """A part whose filename reduces to no usable basename -- ``"."``,
    ``".."``, or the empty string -- is staged under a synthetic name, each
    part holding its own bytes, while the caller's own spelling is what a
    refusal would name back.

    Anti-coincidental-pass: staging under the bare ``Path(name).name`` makes
    ``"."`` resolve to the part's staging directory itself, so the write
    raises ``IsADirectoryError`` before any assertion runs; ``".."`` keeps
    its name and writes to the staging root, failing the basename equality.
    The bytes are read inside the patched consumer, so the three parts must
    have landed in three distinct files.
    """
    seen: list[tuple[str, bytes, str | None]] = []

    async def fake_stream(descriptors, vault_services, infer_edges=True, needs_review=True):
        for fd in descriptors:
            staged = Path(fd.file_path)
            seen.append((staged.name, staged.read_bytes(), fd.declared_source))
        yield "data: {}\n\n"

    monkeypatch.setattr(batch_ingest_stream, "batch_ingest_sse_stream", fake_stream)
    uploads = [
        UploadedFile(filename=".", content=b"dot", source_type="markdown"),
        UploadedFile(filename="..", content=b"dotdot", source_type="markdown"),
        UploadedFile(filename="", content=b"empty", source_type="markdown"),
    ]

    chunks = [
        chunk async for chunk in stream_uploaded_batch_ingest(uploads, vault_services=object())
    ]

    assert chunks == ["data: {}\n\n"]
    assert seen == [
        ("upload_0", b"dot", "."),
        ("upload_1", b"dotdot", ".."),
        ("upload_2", b"empty", "upload_2"),
    ]


# ---------------------------------------------------------------------------
# B16 -- one summary spells a file one way
# ---------------------------------------------------------------------------


async def test_b16_edge_warning_names_the_upload_by_the_callers_own_filename(batch_app):
    """An edge dropped because one of its files failed to ingest names that
    file as the caller uploaded it -- the same spelling the error entry beside
    it carries, in the same summary.

    Two versions of one chain arrive as uploads; the newer one's source_type
    has no adapter, so its ingest raises and the ``supersedes`` edge the
    version_chain rule planned for the pair cannot resolve. The warning that
    records the drop reports both endpoints as file references, and neither
    may be the staging location the bytes were written to.

    Anti-coincidental-pass: the parts are staged under a real
    ``tempfile.mkdtemp`` root, so before the fix every one of these values is
    an absolute ``/var/folders/.../sage-batch-ingest-<rand>/<i>/`` path and the
    equality cannot pass by accident. The comparison is a whole-dict equality
    over all five fields, so a fix that spelled ``source`` and ``target`` but
    left ``detail`` interpolated from the raw ref still fails. ``target`` is
    load-bearing beyond ``source``: v1 ingests successfully and its ref *does*
    resolve, so a fix that only rewrote the unresolved side reports the staged
    path here. The final assertion is a whole-summary sweep rather than a field
    list, so a field added later that reintroduces the leak fails without
    anyone remembering to extend this test.
    """
    app, vault_id, _config = batch_app
    metadata = {
        "files": [
            {
                "source_type": "markdown",
                "parsed_metadata": {"title": "Report", "version": "v1", "doc_type": "note"},
            },
            {
                "source_type": "not_a_real_adapter",
                "parsed_metadata": {"title": "Report", "version": "v2", "doc_type": "note"},
            },
        ],
    }
    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[
                _md_part("report_v1.md", b"# Report\n\nFirst version."),
                _md_part("report_v2.md", b"# Report\n\nSecond version, which fails."),
            ],
            data={"metadata": json.dumps(metadata)},
        )
    assert resp.status_code == 200, resp.text
    summary = next(e for e in _parse_sse_events(resp.text) if e["event_type"] == "summary")

    assert summary["edge_warnings"] == [
        {
            "source": "report_v2.md",
            "target": "report_v1.md",
            "edge_type": "supersedes",
            "reason": "ingestion_failed",
            "detail": "Source file failed ingestion: report_v2.md",
        }
    ], summary["edge_warnings"]

    # The other half of the same summary spells the same file the same way.
    assert [e["source_path"] for e in summary["errors"]] == ["report_v2.md"], summary["errors"]

    assert "sage-batch-ingest-" not in json.dumps(summary), summary


async def test_b17_projection_failure_message_names_the_upload_not_the_vault(
    batch_app, monkeypatch
):
    """A file that ingests but cannot be projected reports the failure by the
    caller's own upload name, with neither the staging root nor the vault's
    storage root anywhere in the summary.

    The disclosure this closes was observed on exactly this surface: an
    adapter names the path it was handed, that path is the retained vault
    copy, and the text reaches the caller verbatim as the per-file
    ``message``. VSBB-068 pins the substitution at the seam and BIS-023 pins
    that an upload's declared name reaches the ingest, but neither says the
    two compose over the wire -- and this envelope has been through that
    before: an earlier disclosure in it was closed on one field and left
    standing on its sibling, because no test asked the whole payload.

    Anti-coincidental-pass: the assertion is a whole-summary sweep for both
    server-side roots plus a positive equality on the message, so it fails
    against an untranslated message (which names the storage root), against
    a translation that dropped the adapter's own diagnostic, and against one
    that substituted the staged path instead of the caller's name. The
    adapter reads the path off its argument at raise time rather than
    hard-coding one, so the message can only carry what the service actually
    handed it.
    """
    app, vault_id, config = batch_app
    services: SAGEServices = app.state.vault_registry[vault_id]
    adapter = services.ingestion_service._adapters[SourceType.MARKDOWN]

    async def failing_project(source_path, config=None):
        raise ValueError(f"Failed to open PDF {source_path}: broken header")

    monkeypatch.setattr(adapter, "project", failing_project)

    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[_md_part("quarterly.md", b"# Quarterly\n\nBody.")],
            data={"metadata": json.dumps({"files": [{"source_type": "markdown"}]})},
        )
    assert resp.status_code == 200, resp.text
    summary = next(e for e in _parse_sse_events(resp.text) if e["event_type"] == "summary")

    assert [e["message"] for e in summary["errors"]] == [
        "Failed to open PDF quarterly.md: broken header"
    ], summary["errors"]

    rendered = json.dumps(summary)
    assert "sage-batch-ingest-" not in rendered, summary
    assert str(config.vault.storage_root) not in rendered, summary


async def test_b18_non_valueerror_adapter_failure_also_names_the_upload(batch_app):
    """An adapter failure that is not a ``ValueError`` is respelled too.

    The seam cannot key on an exception type. An adapter that wraps its
    library's failure picks the type; one that lets the library's own
    exception through does not, and python-docx raises its own
    ``PackageNotFoundError`` -- naming the absolute path it was handed -- for
    any input that is not a zip. Typing the seam to the shape the pdf and
    pptx adapters happen to use leaves that one on the wire.

    Drives the real adapter and the real library: no monkeypatch, no stub
    exception. The part is genuinely not a zip, so the failure is the one a
    caller uploading a corrupt file actually gets.

    Anti-coincidental-pass: the positive assertion is a whole-message equality
    naming the upload, and the sweep is over the entire summary for the vault's
    own storage root -- which is what an untranslated message contains.

    What this pins, precisely: the **adapter's wrap**, end to end through the
    real library. It does *not* discriminate the seam's exception breadth,
    because the wrap makes this failure a ``ValueError`` before the seam sees
    it -- narrowing the seam back to ``ValueError`` leaves this test green.
    Two repairs were applied to one defect and they overlap here; VSBB-073 is
    the one that pins the breadth, using a failure no adapter wraps.
    """
    app, vault_id, config = batch_app
    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[("files", ("bogus.docx", b"not a zip at all", "application/octet-stream"))],
            data={"metadata": json.dumps({"files": [{"source_type": "docx"}]})},
        )
    assert resp.status_code == 200, resp.text
    summary = next(e for e in _parse_sse_events(resp.text) if e["event_type"] == "summary")

    assert summary["error_count"] == 1, summary
    entry = summary["errors"][0]
    assert entry["source_path"] == "bogus.docx", entry
    assert entry["message"] == (
        "Failed to open document bogus.docx: Package not found at 'bogus.docx'"
    ), entry

    rendered = json.dumps(summary)
    assert str(config.vault.storage_root) not in rendered, summary
    assert "sage-batch-ingest-" not in rendered, summary


# ---------------------------------------------------------------------------
# B15 -- batch-level pipeline failure after the response is committed
# ---------------------------------------------------------------------------


async def test_b15_pipeline_failure_ends_the_committed_stream(batch_app, monkeypatch):
    """A failure of the batch pipeline itself -- raised after the 200 has
    already been committed -- ends the request instead of leaving it open.

    The 400 and 404 refusals resolve before the stream opens and return a
    typed envelope; this one cannot, so it propagates and tears the response
    down. The client sees a stream that ended without a ``summary`` event.

    Anti-coincidental-pass: the wait does not cancel the request. Cancelling
    would throw into the generator at its queue wait, whose
    ``finally: await task`` re-raises the pipeline's own exception -- so a
    hung stream would surface the same failure a terminating one does, and
    an assertion on the exception alone would pass against the hang. The
    deadline reports the hang as itself instead. The staging directory
    assertion is the second half: a teardown that skipped the cleanup would
    satisfy the termination check alone.
    """
    app, vault_id, _config = batch_app
    roots: set[Path] = set()

    async def failing_run(self, files, vault_services, **kwargs):  # noqa: ANN001, ARG001
        staged = Path(files[0].file_path)
        roots.add(next(p for p in staged.parents if p.name.startswith("sage-batch-ingest-")))
        raise RuntimeError("pipeline boom")

    monkeypatch.setattr(BatchIngestService, "run", failing_run)

    metadata = json.dumps({"files": [{"source_type": "markdown"}], "infer_edges": False})

    async def _post() -> None:
        async with _client(app) as client:
            await client.post(
                f"/sage_vaults/{vault_id}/documents:batch",
                files=[_md_part("a.md", b"# A\n")],
                data={"metadata": metadata},
            )

    task = asyncio.create_task(_post())
    done, _pending = await asyncio.wait({task}, timeout=5.0)
    if task not in done:
        task.cancel()
        raise AssertionError("the endpoint did not terminate on its own within 5.0s")

    with pytest.raises(RuntimeError, match="pipeline boom"):
        task.result()

    assert len(roots) == 1, roots
    assert not next(iter(roots)).exists(), "staging survived the failed run"
