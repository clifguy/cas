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
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.backend.asgi import create_bff_app
from app.backend.auth.config import BffAuthContext
from app.backend.auth.sage_client import ObOSageClient
from app.backend.auth.session_store import InMemorySessionStore
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
from tests.helpers.bff_session import StubOidc, bff_settings, live_session, sessioned_client
from tests.sage._dry_run_helpers import assert_state_unchanged, state_snapshot


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

    oidc = StubOidc()
    store = InMemorySessionStore()
    await store.create_session(live_session())

    bff = create_bff_app(stack_config=SageCoreConfig(profile="cloud"))
    bff.state.bff_auth = BffAuthContext(settings=bff_settings(), oidc=oidc, store=store)
    sage_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=sage_app), base_url="http://sage.test"
    )
    bff.state.sage_transport = HttpSageTransport(
        ObOSageClient("http://sage.test", oidc, client=sage_client)
    )

    try:
        async with sessioned_client(bff) as client:
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

    async def fake_stream(
        descriptors, vault_services, infer_edges=True, needs_review=True, dry_run=False
    ):
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

    async def fake_stream(
        descriptors, vault_services, infer_edges=True, needs_review=True, dry_run=False
    ):
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


# ---------------------------------------------------------------------------
# B19-B23 -- dry run on the hosted upload path
# ---------------------------------------------------------------------------


def _tree(root: Path) -> dict[str, tuple[int, int]]:
    """Every file under ``root``, keyed by path, valued by (size, mtime_ns).

    Deliberately not the graph-state fingerprint's job: retention writes a
    file and no row, so a snapshot of documents and edges would report a
    clean dry run over a vault whose import area had just been written to.
    """
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _summary_of(resp: httpx.Response) -> dict:
    """The trailing summary event of an SSE batch-ingest response."""
    return next(e for e in _parse_sse_events(resp.text) if e["event_type"] == "summary")


async def test_b19_dry_run_previews_every_upload_and_persists_nothing(batch_app):
    """A mixed batch under a dry run: one novel file, one carrying bytes the
    vault already holds, and one naming a doc_type the vault has never
    declared. Previews and errors together account for the batch, in batch
    order, and nothing is written.

    The refusal arm is an undeclared doc_type rather than a missing source:
    on an upload path the bytes are always present by construction, so the
    co-located test's source_file_not_found arm is unreachable here. The
    vocabulary gate reads only caller metadata and the vault config, which
    is why it still refuses above the preview branch.

    Anti-coincidental-pass: a route that read the flag off the envelope and
    dropped it, or a generator that took it and did not forward it, reports
    ``dry_run: false`` with no previews; an implementation whose preview
    branch sat below source retention leaves the graph fingerprint clean and
    the vault-tree listing changed, which is why both are asserted. Two
    rivals are NOT excluded here, and neither is a gap: an implementation
    that emits ``previews`` on every run is excluded by the real run below,
    which asserts the key is absent; and one that plans edges on a dry run
    is excluded by the plan-builder test at the end of this section. This
    batch cannot reach the second on its own -- ``edges_created`` is empty
    under either implementation, because these filenames carry no version
    token and so plan no ``version_chain`` edge either way. ``infer_edges``
    is set true for realism, not as a control.
    """
    app, vault_id, config = batch_app
    services: SAGEServices = app.state.vault_registry[vault_id]
    storage_root = Path(config.vault.storage_root)

    held_bytes = b"# Held\n\nAlready in the vault.\n"
    async with _client(app) as client:
        seed = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[_md_part("held.md", held_bytes)],
            data={"metadata": json.dumps({"files": [{"source_type": "markdown"}]})},
        )
    assert seed.status_code == 200, seed.text
    assert _summary_of(seed)["documents_created"]["new"] == 1, seed.text

    before = await state_snapshot(services.graph_store, services.content_store)
    tree_before = _tree(storage_root)

    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[
                _md_part("novel.md", b"# Novel\n\nNot yet held.\n"),
                _md_part("held_again.md", held_bytes),
                _md_part("undeclared.md", b"# Undeclared\n\nbody\n"),
            ],
            data={
                "metadata": json.dumps(
                    {
                        "dry_run": True,
                        "infer_edges": True,
                        "files": [
                            {"source_type": "markdown"},
                            {"source_type": "markdown"},
                            {
                                "source_type": "markdown",
                                "parsed_metadata": {"doc_type": "no_such_type"},
                            },
                        ],
                    }
                )
            },
        )

    assert resp.status_code == 200, resp.text
    summary = _summary_of(resp)

    assert summary["dry_run"] is True
    assert summary["documents_created"] == {"new": 0, "new_version": 0}
    assert summary["edges_created"] == {}

    previews = summary["previews"]
    assert len(previews) == 2, previews
    novel_preview, held_preview = previews
    assert novel_preview["would_create"] is True
    assert held_preview["would_create"] is False
    assert held_preview["duplicate_of"] is not None

    assert summary["error_count"] == 1
    (error,) = summary["errors"]
    assert error["code"] == "invalid_doc_type", error
    assert error["source_path"] == "undeclared.md", error

    after = await state_snapshot(services.graph_store, services.content_store)
    assert_state_unchanged(before, after)
    assert _tree(storage_root) == tree_before, (
        "A dry run retained an uploaded source into the vault tree. "
        "Retention is the first irreversible act of an ingest; a preview "
        "must branch above it, and the graph fingerprint cannot see it."
    )


async def test_b20_real_run_reports_no_previews_and_persists(batch_app):
    """The same upload without the flag persists and carries no previews.

    The negative control for the dry-run cases: without it, a route that had
    stopped persisting for some unrelated reason would satisfy every
    "nothing was written" assertion they make.

    Anti-coincidental-pass: this is also the only case that excludes an
    implementation emitting ``previews`` unconditionally. A dry run
    asserting the key is present cannot tell that rival from the correct
    code, so the absence assertion here is load-bearing for the pair rather
    than a restatement of the dry-run case. It is likewise the only case
    that would red if the route stopped persisting altogether, which is why
    it asserts a new document row and a grown vault tree rather than
    reading the summary alone.
    """
    app, vault_id, config = batch_app
    services: SAGEServices = app.state.vault_registry[vault_id]
    storage_root = Path(config.vault.storage_root)

    before = await state_snapshot(services.graph_store, services.content_store)
    tree_before = _tree(storage_root)

    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[_md_part("real.md", b"# Real\n\nA run that persists.\n")],
            data={
                "metadata": json.dumps(
                    {"infer_edges": True, "files": [{"source_type": "markdown"}]}
                )
            },
        )

    assert resp.status_code == 200, resp.text
    summary = _summary_of(resp)

    assert summary["dry_run"] is False
    assert "previews" not in summary, summary
    assert summary["documents_created"]["new"] == 1
    assert summary["error_count"] == 0, summary["errors"]

    after = await state_snapshot(services.graph_store, services.content_store)
    assert set(after.documents) - set(before.documents), "a real run wrote no document row"
    assert _tree(storage_root) != tree_before, "a real run retained no source"


async def test_b21_dry_run_carries_an_empty_previews_list_when_every_upload_refuses(
    batch_app,
):
    """A dry run whose only file is refused still carries ``previews``, empty.

    The field is keyed on the flag rather than on the list, so an all-refused
    dry run stays distinguishable on the wire from a real run, which omits
    the key entirely.

    Anti-coincidental-pass: keying the field on list emptiness instead drops
    it from this response and reds the assertion below. The rival this does
    not exclude is an implementation that emits the key unconditionally,
    which is what the real-run case is for; the two together pin the keying,
    and neither does on its own.
    """
    app, vault_id, _config = batch_app

    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[_md_part("refused.md", b"# Refused\n\nbody\n")],
            data={
                "metadata": json.dumps(
                    {
                        "dry_run": True,
                        "files": [
                            {
                                "source_type": "markdown",
                                "parsed_metadata": {"doc_type": "no_such_type"},
                            }
                        ],
                    }
                )
            },
        )

    assert resp.status_code == 200, resp.text
    summary = _summary_of(resp)
    assert summary["dry_run"] is True
    assert summary["previews"] == []
    assert summary["error_count"] == 1
    assert summary["errors"][0]["code"] == "invalid_doc_type"


async def test_b22_dry_run_still_refuses_an_unknown_vault_before_the_stream(batch_app):
    """A dry run does not move the boundary at which a caller learns the
    vault does not exist: the 404 still resolves synchronously, as JSON,
    with no SSE events emitted.

    This is a removal guard on the route's vault dependency, not a gate on
    the dry run, and the distinction is worth stating rather than leaving a
    reader to infer a coverage claim it does not make. The dependency
    resolves before the handler body reads the envelope, so no arrangement
    of the flag can reach the boundary and no rival implementation of the
    dry run makes this red. What does make it red is the dependency going
    missing, which the router conformance gate also pins structurally.
    """
    app, _vault_id, _config = batch_app

    async with _client(app) as client:
        resp = await client.post(
            "/sage_vaults/no_such_vault/documents:batch",
            files=[_md_part("a.md", b"# A\n")],
            data={
                "metadata": json.dumps({"dry_run": True, "files": [{"source_type": "markdown"}]})
            },
        )

    assert resp.status_code == 404, resp.text
    assert "application/json" in resp.headers.get("content-type", "")
    assert resp.json()["code"] == "vault_not_found"
    assert "data: " not in resp.text


async def test_b23_dry_run_does_not_build_an_edge_plan(batch_app, monkeypatch):
    """``infer_edges`` is overridden by ``dry_run`` on the upload path too. An
    edge plan resolves against document ids a preview never mints, so building
    one would cost reads to produce a plan that could only be discarded.

    The observable is the plan builder itself, not the edge counts. Those are
    empty whether or not the planning phase runs -- only the resolution phase
    writes edges, and it is unreachable without a plan -- so asserting them
    proves nothing, which is the trap the co-located sibling records having
    fallen into and this test exists to avoid repeating at the route.
    """
    app, vault_id, _config = batch_app
    calls: list[object] = []

    async def _record(*args, **kwargs):
        calls.append(args)
        raise AssertionError("the edge-planning phase must not run on a dry run")

    monkeypatch.setattr(BatchIngestService, "_build_edge_plan", _record)

    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[_md_part("planless.md", b"# Planless\n\nbody\n")],
            data={
                "metadata": json.dumps(
                    {
                        "dry_run": True,
                        "infer_edges": True,
                        "files": [{"source_type": "markdown"}],
                    }
                )
            },
        )

    assert resp.status_code == 200, resp.text
    assert calls == []
    summary = _summary_of(resp)
    assert summary["dry_run"] is True
    assert summary["edges_created"] == {}
    assert summary["edges_dropped"] == 0


# ---------------------------------------------------------------------------
# B24 -- a malformed per-file date is reported with its typed code
# ---------------------------------------------------------------------------


async def test_b24_malformed_per_file_date_reports_its_typed_code(batch_app):
    """A file whose parsed date is not a calendar date fails alone, and its
    entry carries ``invalid_document_date`` with the offending value -- the
    code the batch contract lists for that failure -- rather than a bare
    message with no code to branch on.

    The second file carries a valid date and must ingest: it is the control
    that the refusal is the one file's date and not a batch-wide rejection.
    """
    app, vault_id, _config = batch_app

    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[
                _md_part("impossible.md", b"# Impossible\n\nbody\n"),
                _md_part("dated.md", b"# Dated\n\nbody\n"),
            ],
            data={
                "metadata": json.dumps(
                    {
                        "infer_edges": False,
                        "files": [
                            {
                                "source_type": "markdown",
                                "parsed_metadata": {"title": "Impossible", "date": "2026-02-30"},
                            },
                            {
                                "source_type": "markdown",
                                "parsed_metadata": {"title": "Dated", "date": "2026-02-28"},
                            },
                        ],
                    }
                )
            },
        )

    assert resp.status_code == 200, resp.text
    summary = _summary_of(resp)

    assert summary["documents_created"]["new"] == 1, summary
    assert summary["error_count"] == 1, summary
    (error,) = summary["errors"]
    assert error["file_index"] == 0, error
    assert error["code"] == "invalid_document_date", error
    assert error["detail"]["document_date"] == "2026-02-30", error
    # The typed error's own message, not the raw validation text, which
    # renders the whole request with the staged location in it.
    assert error["message"].startswith("document_date '2026-02-30'"), error

    # The progress event reports the same message as the entry.
    (failed,) = [e for e in _parse_sse_events(resp.text) if e.get("status") == "failed"]
    assert failed["file_index"] == 0, failed
    assert failed["error"] == error["message"], failed


# ---------------------------------------------------------------------------
# B25-B30 -- undeclared names in the metadata envelope's file entries
# ---------------------------------------------------------------------------


def _two_file_metadata(second: dict) -> dict:
    """A two-file envelope whose second entry is ``second``."""
    return {"infer_edges": False, "files": [{"source_type": "markdown"}, second]}


def _two_parts() -> list[tuple[str, tuple[str, bytes, str]]]:
    return [
        _md_part("first.md", b"# First\n\nFirst body.\n"),
        _md_part("second.md", b"# Second\n\nSecond body.\n"),
    ]


def _spy_on_staging(monkeypatch) -> list[object]:
    """Count calls into the staging-and-stream generator the route hands off to.

    The spy delegates, so a request the route accepts still ingests; a refusal
    raised before the hand-off leaves the list empty.
    """
    from sage.api.routers import ingestion as ingestion_router

    calls: list[object] = []
    real = ingestion_router.stream_uploaded_batch_ingest

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(ingestion_router, "stream_uploaded_batch_ingest", spy)
    return calls


def _assert_invalid_parameter(resp: httpx.Response, parameter: str, value: object) -> None:
    assert resp.status_code == 422, resp.text
    assert "application/json" in resp.headers.get("content-type", ""), resp.headers
    body = resp.json()
    assert body["code"] == "invalid_parameter", body
    assert body["detail"]["parameter"] == parameter, body
    assert body["detail"]["value"] == value, body


async def test_b25_undeclared_parsed_metadata_key_is_refused_before_staging(batch_app, monkeypatch):
    """An undeclared key in an entry's ``parsed_metadata`` refuses the call.

    Anti-coincidental-pass: the key sits on the second entry, so a refusal
    that inspected only the first would miss it; the staging spy and the
    state fingerprint show nothing was staged or written, which a refusal
    raised inside the stream would violate; and the control -- the same
    request without the key -- must ingest both files, so the refusal is the
    key's and not some other defect in the request.
    """
    app, vault_id, _config = batch_app
    services: SAGEServices = app.state.vault_registry[vault_id]
    calls = _spy_on_staging(monkeypatch)
    parsed = {"codes": ["PV06"], "version": "v1"}

    def metadata(extra: dict) -> str:
        return json.dumps(
            _two_file_metadata({"source_type": "markdown", "parsed_metadata": {**parsed, **extra}})
        )

    before = await state_snapshot(services.graph_store, services.content_store)
    async with _client(app) as client:
        refused = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=_two_parts(),
            data={"metadata": metadata({"bogus_field_x": 1})},
        )
    after = await state_snapshot(services.graph_store, services.content_store)

    _assert_invalid_parameter(refused, "files.1.parsed_metadata.bogus_field_x", 1)
    assert calls == []
    assert_state_unchanged(before, after)

    async with _client(app) as client:
        control = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=_two_parts(),
            data={"metadata": metadata({})},
        )
    assert control.status_code == 200, control.text
    summary = _summary_of(control)
    assert summary["error_count"] == 0, summary
    assert summary["documents_created"]["new"] == 2, summary


async def test_b26_undeclared_file_entry_key_is_invalid_parameter(batch_app, monkeypatch):
    """An undeclared key on a file entry itself is refused the same way.

    Anti-coincidental-pass: the envelope's own validation already rejects
    this key, so the assertion that matters is the code and location -- the
    generic ``invalid_batch_metadata`` would fail them. The control proves
    the rest of the request is sound.
    """
    app, vault_id, _config = batch_app
    calls = _spy_on_staging(monkeypatch)

    async with _client(app) as client:
        refused = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=_two_parts(),
            data={
                "metadata": json.dumps(
                    _two_file_metadata({"source_type": "markdown", "bogus_field_x": "x"})
                )
            },
        )
        _assert_invalid_parameter(refused, "files.1.bogus_field_x", "x")
        assert calls == []

        control = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=_two_parts(),
            data={"metadata": json.dumps(_two_file_metadata({"source_type": "markdown"}))},
        )
    assert control.status_code == 200, control.text
    assert _summary_of(control)["documents_created"]["new"] == 2, control.text


@pytest.mark.parametrize(
    "entries",
    [
        pytest.param(
            [
                {"source_type": "markdown"},
                {"source_type": "markdown", "parsed_metadata": {"zeta_key": 1, "alpha_key": 2}},
            ],
            id="two-keys-in-one-parsed-metadata",
        ),
        pytest.param(
            [
                {"source_type": "markdown"},
                {
                    "source_type": "markdown",
                    "parsed_metadata": {"alpha_key": 1},
                    "zeta_key": 2,
                },
            ],
            id="entry-level-key-after-parsed-metadata",
        ),
        pytest.param(
            [
                {"source_type": "markdown", "parsed_metadata": {"zeta_key": 1}},
                {"source_type": "markdown", "alpha_key": 2},
            ],
            id="keys-on-both-entries",
        ),
    ],
)
async def test_b27_undeclared_key_location_matches_the_mcp_tool(batch_app, entries):
    """With several undeclared keys, the route reports the one the MCP tool does.

    The expected refusal is computed by the MCP tool's own check over the same
    entries rather than written by hand. Anti-coincidental-pass: in the first
    case the keys are written in reverse order, so reporting whichever error
    validation lists first names the other key. Validation happens to list the
    remaining two cases in the tool's order already; they pin the index and
    entry-before-parsed-metadata ordering against a selector that sorted on the
    key name alone.
    """
    from sage.api.errors import InvalidParameterError
    from sage.app_tools import _refuse_undeclared_entry_fields

    with pytest.raises(InvalidParameterError) as expected:
        _refuse_undeclared_entry_fields([{"file_path": "x.md", **entry} for entry in entries])

    app, vault_id, _config = batch_app
    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=_two_parts(),
            data={"metadata": json.dumps({"infer_edges": False, "files": entries})},
        )

    _assert_invalid_parameter(
        resp, expected.value.detail["parameter"], expected.value.detail["value"]
    )


async def test_b28_title_omitted_or_null_is_seeded_from_the_stem(batch_app):
    """Closing the keys does not make ``title`` required, and a null title
    means the same as an omitted one: the file stem seeds it.

    Anti-coincidental-pass: each body's heading differs from its stem, so a
    title drawn from content rather than the stem fails.
    """
    app, vault_id, _config = batch_app
    services: SAGEServices = app.state.vault_registry[vault_id]

    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[
                _md_part("alpha_stem.md", b"# Unrelated Heading\n\nAlpha body.\n"),
                _md_part("beta_stem.md", b"# Unrelated Heading\n\nBeta body.\n"),
            ],
            data={
                "metadata": json.dumps(
                    {
                        "infer_edges": False,
                        "files": [
                            {"source_type": "markdown", "parsed_metadata": {"version": "v1"}},
                            {"source_type": "markdown", "parsed_metadata": {"title": None}},
                        ],
                    }
                )
            },
        )

    assert resp.status_code == 200, resp.text
    assert _summary_of(resp)["documents_created"]["new"] == 2, resp.text
    completed = [
        e
        for e in _parse_sse_events(resp.text)
        if e["event_type"] == "progress" and e["status"] == "completed"
    ]
    titles = [
        (await services.graph_store.get_document(event["document_id"])).title for event in completed
    ]
    assert titles == ["alpha_stem", "beta_stem"]


@pytest.mark.parametrize(
    "envelope",
    [
        pytest.param(
            {"files": [{"source_type": "markdown"}], "bogus_field_x": 1},
            id="envelope-root-key",
        ),
        pytest.param(
            {"files": [{"source_type": "markdown", "parsed_metadata": {"codes": "PV07"}}]},
            id="codes-not-a-list",
        ),
        pytest.param(
            {"files": [{"source_type": "markdown", "parsed_metadata": {"title": 5}}]},
            id="title-not-a-string",
        ),
    ],
)
async def test_b29_other_envelope_defects_stay_invalid_batch_metadata(batch_app, envelope):
    """Only an undeclared key in a file entry moves to ``invalid_parameter``.

    Anti-coincidental-pass: an implementation that translated every envelope
    validation failure would report these as ``invalid_parameter`` too.
    """
    app, vault_id, _config = batch_app
    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[_md_part("a.md", b"# A\n\nbody")],
            data={"metadata": json.dumps(envelope)},
        )

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "invalid_batch_metadata", resp.text


async def test_b31_undeclared_key_wins_over_a_malformed_value(batch_app):
    """An entry carrying both an undeclared key and a malformed value is
    refused for the key, as the MCP tool refuses names before values."""
    app, vault_id, _config = batch_app
    envelope = {
        "files": [
            {"source_type": "markdown", "parsed_metadata": {"codes": "PV07", "bogus_field_x": 1}}
        ]
    }
    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[_md_part("a.md", b"# A\n\nbody")],
            data={"metadata": json.dumps(envelope)},
        )

    _assert_invalid_parameter(resp, "files.0.parsed_metadata.bogus_field_x", 1)


async def test_b30_first_party_upload_envelope_is_accepted(batch_app):
    """The envelope the application's upload client builds is not refused.

    Mirrors ``uploadBatchIngest`` in ``app/src/api/ingest.ts``: the three
    batch flags and one ``source_type``-only entry per file.
    """
    app, vault_id, _config = batch_app
    envelope = {
        "infer_edges": False,
        "needs_review": True,
        "dry_run": False,
        "files": [{"source_type": "markdown"}],
    }
    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[_md_part("client.md", b"# Client\n\nbody")],
            data={"metadata": json.dumps(envelope)},
        )

    assert resp.status_code == 200, resp.text
    assert _summary_of(resp)["documents_created"]["new"] == 1, resp.text


def test_parsed_metadata_input_treats_null_as_omitted():
    """The shared conversion seeds the title and codes whether a key is
    absent or null, and passes supplied values through unchanged.

    Anti-coincidental-pass: ``parsed.get("title", default)`` keeps an
    explicit null, which the null row catches.
    """
    from sage.services.batch_ingest import ParsedMetadataInput, parsed_metadata_input

    assert parsed_metadata_input(None, "stem") is None
    assert parsed_metadata_input({}, "stem") == ParsedMetadataInput(title="stem")
    assert parsed_metadata_input({"title": None, "codes": None}, "stem") == ParsedMetadataInput(
        title="stem"
    )
    full = {
        "title": "Given",
        "date": "2026-01-02",
        "project": "P",
        "codes": ["PV06"],
        "version": "v1",
        "doc_type": "note",
    }
    assert parsed_metadata_input(full, "stem") == ParsedMetadataInput(**full)


# ---------------------------------------------------------------------------
# B32 -- B36: caller-settable needs_review and per-file typed metadata
# ---------------------------------------------------------------------------


@pytest.fixture
async def tier3_batch_app(minimal_vault_config_dict, monkeypatch):
    """``batch_app`` over a vault whose ``ticket`` doc_type declares a schema.

    ``minimal_vault_config_dict`` declares none, and a doc_type with no
    ``metadata_schema`` refuses every Tier-3 payload, so a test that needs a
    payload to land needs a vault that accepts one.
    """
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config_dict = dict(minimal_vault_config_dict)
    config_dict["document_types"] = {
        "doc_types": [
            *minimal_vault_config_dict["document_types"]["doc_types"],
            {
                "value": "strict_ticket",
                "label": "Strict Ticket",
                "metadata_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["ticket_id"],
                    "properties": {"ticket_id": {"type": "string"}},
                },
            },
            {
                "value": "ticket",
                "label": "Ticket",
                "metadata_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "ticket_id": {"type": "string", "pattern": r"^T-\d{4}$"},
                        "ticket_priority": {"type": "string", "enum": ["high", "medium", "low"]},
                    },
                },
            },
        ]
    }
    config = VaultConfig.model_validate(config_dict)
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


async def test_b32_needs_review_false_commits_metadata_as_authoritative(batch_app):
    """``needs_review=false`` commits the caller's metadata and queues nothing.

    Anti-coincidental-pass: the paired control -- the same upload at the
    default -- must land queued. Without it a path that never queued would
    pass the false arm. The two arms upload distinct bytes, since identical
    bytes are refused as duplicate content.
    """
    app, vault_id, _config = batch_app
    services: SAGEServices = app.state.vault_registry[vault_id]

    async def _ingest(name: str, body: bytes, **envelope) -> dict:
        async with _client(app) as client:
            resp = await client.post(
                f"/sage_vaults/{vault_id}/documents:batch",
                files=[_md_part(name, body)],
                data={
                    "metadata": json.dumps(
                        {
                            "infer_edges": False,
                            "files": [{"source_type": "markdown"}],
                            **envelope,
                        }
                    )
                },
            )
        assert resp.status_code == 200, resp.text
        completed = [
            e
            for e in _parse_sse_events(resp.text)
            if e["event_type"] == "progress" and e["status"] == "completed"
        ]
        assert len(completed) == 1, resp.text
        return {"summary": _summary_of(resp), "document_id": completed[0]["document_id"]}

    committed = await _ingest(
        "authoritative.md", b"# Authoritative\n\nCommitted as supplied.\n", needs_review=False
    )
    queued = await _ingest("queued.md", b"# Queued\n\nAwaiting confirmation.\n")

    committed_doc = await services.graph_store.get_document(committed["document_id"])
    queued_doc = await services.graph_store.get_document(queued["document_id"])

    assert committed_doc.metadata_confirmed is True
    assert committed["summary"]["metadata_pending"] == 0, committed["summary"]
    assert queued_doc.metadata_confirmed is False
    assert queued["summary"]["metadata_pending"] == 1, queued["summary"]


async def test_b33_per_file_tier3_and_tags_land_on_each_document(tier3_batch_app):
    """Each entry's Tier-3 payload and tags reach its own document, by value.

    Anti-coincidental-pass: the two entries carry *different* payloads, so a
    smear across the batch, or the last entry's winning, fails. One tag
    contains a comma, which the ``codes`` join-and-resplit path would turn
    into two tags. Only the first entry supplies tags and the second is
    asserted to have none, so an implementation applying one entry's tags to
    the whole batch fails too -- the comma probe alone would not separate it,
    since it says nothing about which documents the tags reached.
    """
    app, vault_id, _config = tier3_batch_app
    services: SAGEServices = app.state.vault_registry[vault_id]

    entries = [
        {
            "source_type": "markdown",
            "parsed_metadata": {
                "title": "Ticket one",
                "doc_type": "ticket",
                "tags": ["alpha,beta", "gamma"],
            },
            "tier3_metadata": {"ticket_id": "T-0001", "ticket_priority": "high"},
        },
        {
            "source_type": "markdown",
            "parsed_metadata": {"title": "Ticket two", "doc_type": "ticket"},
            "tier3_metadata": {"ticket_id": "T-0002", "ticket_priority": "low"},
        },
    ]

    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[
                _md_part("ticket_one.md", b"# Ticket one\n\nFirst body.\n"),
                _md_part("ticket_two.md", b"# Ticket two\n\nSecond body.\n"),
            ],
            data={
                "metadata": json.dumps(
                    {"infer_edges": False, "needs_review": False, "files": entries}
                )
            },
        )

    assert resp.status_code == 200, resp.text
    summary = _summary_of(resp)
    assert summary["error_count"] == 0, summary
    completed = [
        e
        for e in _parse_sse_events(resp.text)
        if e["event_type"] == "progress" and e["status"] == "completed"
    ]
    docs = [await services.graph_store.get_document(e["document_id"]) for e in completed]
    by_title = {d.title: d for d in docs}

    assert by_title["Ticket one"].tier3_metadata == {
        "ticket_id": "T-0001",
        "ticket_priority": "high",
    }
    assert by_title["Ticket two"].tier3_metadata == {
        "ticket_id": "T-0002",
        "ticket_priority": "low",
    }
    assert by_title["Ticket one"].tags == ["alpha,beta", "gamma"]
    assert by_title["Ticket two"].tags == []


async def test_b34_tier3_schema_violation_is_a_per_file_error(tier3_batch_app):
    """A payload the doc_type's schema rejects is that file's error, not the batch's.

    Anti-coincidental-pass: a refusal for the whole call reports the same
    violation, so the sibling document's creation and the error count are what
    discriminate -- one file refused, the other ingested. Those three say
    nothing about *when* the refusal happened, so the state fingerprint is
    taken around the run and compared against the sibling's single insert: an
    implementation that wrote the bad file's row and then raised would report
    exactly the same summary, with an orphan row left behind. The service
    docstring claims validation is strictly upstream of the insert on the real
    run, and only the dry-run test checked it.
    """
    app, vault_id, _config = tier3_batch_app
    services: SAGEServices = app.state.vault_registry[vault_id]
    before = await state_snapshot(services.graph_store, services.content_store)

    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[
                _md_part("ticket_good.md", b"# Ticket good\n\nValid payload.\n"),
                _md_part("ticket_bad.md", b"# Ticket bad\n\nPayload the schema rejects.\n"),
            ],
            data={
                "metadata": json.dumps(
                    {
                        "infer_edges": False,
                        "needs_review": False,
                        "files": [
                            {
                                "source_type": "markdown",
                                "parsed_metadata": {"doc_type": "ticket"},
                                "tier3_metadata": {"ticket_id": "T-0003"},
                            },
                            {
                                "source_type": "markdown",
                                "parsed_metadata": {"doc_type": "ticket"},
                                "tier3_metadata": {"ticket_id": "not-a-ticket-id"},
                            },
                        ],
                    }
                )
            },
        )

    assert resp.status_code == 200, resp.text
    summary = _summary_of(resp)
    assert summary["documents_created"]["new"] == 1, summary
    assert summary["error_count"] == 1, summary
    assert summary["errors"][0]["file_index"] == 1, summary
    assert summary["errors"][0]["code"] == "tier3_schema_violation", summary

    # The refused file left nothing behind: the only new document is the
    # sibling's, so validation ran before the insert rather than after it.
    after = await state_snapshot(services.graph_store, services.content_store)
    titles = {
        (await services.graph_store.get_document(e["document_id"])).title
        for e in _parse_sse_events(resp.text)
        if e["event_type"] == "progress" and e["status"] == "completed"
    }
    assert len(after.documents) == len(before.documents) + 1, (before, after)
    assert len(titles) == 1, titles


async def test_b35_codes_and_tags_conflict_location_matches_the_mcp_tool(batch_app, monkeypatch):
    """An entry supplying both is refused where the MCP tool refuses it.

    The expected refusal is computed by the tool's own check over the same
    entries rather than written by hand, as B27 does for undeclared keys.
    Anti-coincidental-pass: the conflict sits on the second entry and the
    staging spy must stay empty, so a check placed inside the stream -- which
    would return the same envelope -- fails.
    """
    from sage.api.errors import InvalidParameterError
    from sage.app_tools import _parsed_metadata_of, _refuse_codes_and_tags_together

    entries = [
        {"source_type": "markdown"},
        {"source_type": "markdown", "parsed_metadata": {"codes": ["PV06"], "tags": ["alpha"]}},
    ]
    with pytest.raises(InvalidParameterError) as expected:
        _refuse_codes_and_tags_together(
            _parsed_metadata_of([{"file_path": "x.md", **entry} for entry in entries])
        )

    staged = _spy_on_staging(monkeypatch)
    app, vault_id, _config = batch_app
    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=_two_parts(),
            data={"metadata": json.dumps({"infer_edges": False, "files": entries})},
        )

    assert staged == [], staged
    _assert_invalid_parameter(
        resp, expected.value.detail["parameter"], expected.value.detail["value"]
    )


async def test_b36_dry_run_reports_a_tier3_violation_without_persisting(tier3_batch_app):
    """A dry run refuses the same payload and writes nothing.

    Anti-coincidental-pass: the state fingerprint is compared before and
    after, so a preview path that skipped Tier-3 validation (reporting a
    clean preview) or that persisted (reporting the refusal but writing)
    each fail on a different assertion.
    """
    app, vault_id, _config = tier3_batch_app
    services: SAGEServices = app.state.vault_registry[vault_id]
    before = await state_snapshot(services.graph_store, services.content_store)

    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents:batch",
            files=[_md_part("ticket_bad.md", b"# Ticket bad\n\nRejected payload.\n")],
            data={
                "metadata": json.dumps(
                    {
                        "infer_edges": False,
                        "dry_run": True,
                        "files": [
                            {
                                "source_type": "markdown",
                                "parsed_metadata": {"doc_type": "ticket"},
                                "tier3_metadata": {"ticket_id": "not-a-ticket-id"},
                            }
                        ],
                    }
                )
            },
        )

    assert resp.status_code == 200, resp.text
    summary = _summary_of(resp)
    after = await state_snapshot(services.graph_store, services.content_store)

    assert summary["dry_run"] is True, summary
    assert summary["previews"] == [], summary
    assert summary["error_count"] == 1, summary
    assert summary["errors"][0]["code"] == "tier3_schema_violation", summary
    assert_state_unchanged(before, after)


async def test_b37_an_explicit_empty_tier3_payload_is_a_payload_not_an_absence(tier3_batch_app):
    """``tier3_metadata: {}`` is validated; an omitted key is not.

    At ingest a payload that is not ``None`` overrides whatever tier-3 metadata
    the adapter extracted and is then validated, so an explicit empty mapping
    is refused by a schema declaring a required field while an omitted key
    leaves validation with nothing to check. A surface folding ``{}`` to
    ``None`` ingests the file instead, giving the same entry a different
    outcome depending on which batch surface carried it. The MCP tool asserts
    the same two outcomes in
    ``TestAppBatchIngest.test_empty_tier3_payload_is_a_payload_not_an_absence``.

    Anti-coincidental-pass: the doc_type's ``required`` field is what makes the
    two separable at all -- against a no-schema or all-optional doc_type both
    arms succeed and the test passes against the divergence it exists to catch.
    The omitted-key arm is the paired control: it must still ingest, or the
    test would also pass against a surface that refused every entry.
    """
    app, vault_id, _config = tier3_batch_app

    async def _ingest(name: str, body: bytes, entry: dict) -> dict:
        async with _client(app) as client:
            resp = await client.post(
                f"/sage_vaults/{vault_id}/documents:batch",
                files=[_md_part(name, body)],
                data={
                    "metadata": json.dumps(
                        {
                            "infer_edges": False,
                            "needs_review": False,
                            "files": [
                                {
                                    "source_type": "markdown",
                                    "parsed_metadata": {"doc_type": "strict_ticket"},
                                    **entry,
                                }
                            ],
                        }
                    )
                },
            )
        assert resp.status_code == 200, resp.text
        return _summary_of(resp)

    empty = await _ingest("strict_empty.md", b"# Strict empty\n\nBody.\n", {"tier3_metadata": {}})
    omitted = await _ingest("strict_omitted.md", b"# Strict omitted\n\nOther.\n", {})

    assert empty["error_count"] == 1, empty
    assert empty["errors"][0]["code"] == "tier3_schema_violation", empty
    assert omitted["error_count"] == 0, omitted
    assert omitted["documents_created"]["new"] == 1, omitted
