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
from sage.services.batch_ingest import BatchIngestService, FileDescriptor


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
