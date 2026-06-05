"""Optimistic-concurrency tests for `expected_head_version` on supersede.

Implements CAS-ADR-038 Primitive C. Covers the success path, stale
rejection, back-compat omission, parallel ingesters racing the same
predecessor, retry workflow, predecessor-active ordering, the
predecessor-required validator, error-envelope shape, and transport
round-trips on both FastMCP and FastAPI.

Mirrors the structure of `tests/sage/test_expected_version.py`
(Primitive B for scalar metadata writes). The defining test is T4:
two parallel ingests targeting the same predecessor with the same
`expected_head_version` — exactly one supersede lands and the chain
remains linear.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from sage import mcp_server as _mcp
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.api.errors import StaleChainHeadError
from sage.app import _initialize_services, create_app
from sage.config import VaultConfig
from sage.mcp_server import (
    get_document,
    ingest_document,
    traverse,
)
from sage.mcp_server import (
    update_metadata as _update_metadata_bulk,
)
from tests.sage.conftest import initialize_services_for_test


async def update_metadata(vault_id, document_id, **kwargs):
    """Singleton-shaped shim around the post-CAS-ADR-029 consolidated tool.

    Wraps the call as a length-1 ``items`` collection and unwraps the
    per-item result envelope back to a singleton-shape response so
    existing test assertions continue to apply.
    """
    dry_run = kwargs.pop("dry_run", False)
    item = {"document_id": document_id, **kwargs}
    result = await _update_metadata_bulk(vault_id=vault_id, items=[item], dry_run=dry_run)
    if isinstance(result, dict) and "error" in result and "results" not in result:
        return result
    if isinstance(result, dict) and result.get("results"):
        per = result["results"][0]
        if per.get("status") == "error":
            err = per.get("error") or {}
            return {
                "error": err.get("error"),
                "message": err.get("message"),
                "detail": err.get("detail"),
            }
        out = {"document": per.get("document"), "dry_run": dry_run}
        if "warnings" in per and per["warnings"]:
            out["warnings"] = per["warnings"]
        if "changes" in per:
            out["changes"] = per["changes"]
        return out
    return result


@pytest.fixture
async def vault_services(minimal_vault_config_dict, tmp_vault_dir):
    """Initialize SAGE services and register them in the MCP vault registry.

    Mirrors the fixture in `test_expected_version.py` so the new tests
    reuse the same MCP wire-path harness. Seeds a `test/` source
    directory; per-test source files are written under it by the
    individual tests as needed.
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    async with initialize_services_for_test(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        _mcp._vaults["test_vault"] = services

        sources = tmp_vault_dir / "sources"
        test_dir = sources / "test"
        test_dir.mkdir(parents=True, exist_ok=True)

        try:
            yield services, test_dir
        finally:
            await asyncio.sleep(0.5)
            _mcp._vaults.pop("test_vault", None)


def _parse(result):
    """Parse a tool's result (dict or JSON string)."""
    if isinstance(result, dict):
        return result
    return json.loads(result)


_TERMINAL_PIPELINE_STATES = {"abstraction_complete", "abstraction_skipped", "failed"}


async def _wait_terminal(doc_id: str) -> dict:
    """Poll `get_document` until pipeline_status is terminal, then return it.

    Without this gate, the background abstraction pipeline can advance
    `updated_at` between a test's read and its compare-and-swap call,
    producing a `stale_chain_head` from the pipeline race rather than
    the assertion under test. Mirror of `_seed_doc` in the Primitive B
    test surface.
    """
    for _ in range(150):
        doc = _parse(await get_document("test_vault", doc_id))
        if doc.get("pipeline_status") in _TERMINAL_PIPELINE_STATES:
            return doc
        await asyncio.sleep(0.02)
    raise AssertionError(f"document {doc_id!r} pipeline did not reach terminal state in 3s")


async def _seed_chain_head(test_dir, name: str = "seed") -> dict:
    """Write a source file, ingest it, and return the terminal-state doc dict.

    Returns the parsed `get_document` payload, including a stable
    `updated_at` whose wire form is the chain-head version token.
    """
    src = test_dir / f"{name}.md"
    src.write_text(f"# {name}\n\nSeed content for {name}.")
    initial = _parse(await ingest_document("test_vault", f"test/{name}.md", "markdown"))
    assert "error" not in initial, f"seed ingest failed: {initial!r}"
    return await _wait_terminal(initial["id"])


async def _write_source(test_dir, name: str, body: str) -> str:
    """Write a unique source file under `test_dir/` and return its
    vault-relative path. Each call must use unique content so the
    duplicate-content guard does not pre-empt the supersede check.
    """
    src = test_dir / f"{name}.md"
    src.write_text(body)
    return f"test/{name}.md"


# ---------------------------------------------------------------------------
# T8: Error-envelope structural test
# ---------------------------------------------------------------------------


def test_t8_stale_chain_head_error_envelope_shape():
    """The public SAGEError surfaces with code `stale_chain_head`, status
    409, and a four-field detail payload (predecessor_id,
    expected_head_version, current_head_id, current_head_version).
    Mirror of `test_expected_version.py::test_t8_stale_read_error_envelope_shape`.
    """
    exc = StaleChainHeadError(
        predecessor_id="d1",
        expected_head_version="v_old",
        current_head_id="d1",
        current_head_version="v_new",
    )
    assert exc.code == "stale_chain_head"
    assert exc.status_code == 409
    assert exc.detail == {
        "predecessor_id": "d1",
        "expected_head_version": "v_old",
        "current_head_id": "d1",
        "current_head_version": "v_new",
    }


# ---------------------------------------------------------------------------
# T1, T3: Matching-version happy path and back-compat omission
# ---------------------------------------------------------------------------


async def test_t1_matching_expected_head_version_succeeds(vault_services):
    """Caller passes the current head's `updated_at`; supersede succeeds.

    Linear chain: D1 → D2 (one supersedes edge inbound to D1, D1 archived).
    """
    _, test_dir = vault_services
    d1 = await _seed_chain_head(test_dir, "d1")
    v0 = d1["updated_at"]

    new_source = await _write_source(test_dir, "d2", "# D2\n\nRevision of d1.")
    result = _parse(
        await ingest_document(
            "test_vault",
            new_source,
            "markdown",
            predecessor_id=d1["id"],
            expected_head_version=v0,
        )
    )
    assert "error" not in result, f"unexpected error: {result!r}"
    d2_id = result["id"]
    assert d2_id != d1["id"]

    # Predecessor must be archived.
    d1_after = _parse(await get_document("test_vault", d1["id"]))
    assert d1_after["lifecycle_status"] == "archived"

    # Exactly one inbound supersedes edge into D1, from D2.
    inbound = _parse(
        await traverse(
            "test_vault",
            start_id=d1["id"],
            edge_type="supersedes",
            direction="inbound",
            depth=1,
        )
    )
    edges = [n for n in inbound["nodes"] if n.get("edge")]
    assert len(edges) == 1, f"expected exactly one inbound supersedes edge; got {edges!r}"
    assert edges[0]["edge"]["source_id"] == d2_id
    assert edges[0]["edge"]["target_id"] == d1["id"]


async def test_t3_omitted_expected_head_version_preserves_back_compat(vault_services):
    """Omitting `expected_head_version` keeps the pre-Primitive-C contract.

    A supersede with `predecessor_id` and no `expected_head_version`
    must succeed without any version check. Regression guard for
    callers that have not adopted the contract.
    """
    _, test_dir = vault_services
    d1 = await _seed_chain_head(test_dir, "d1")

    new_source = await _write_source(test_dir, "d2", "# D2\n\nBack-compat path.")
    result = _parse(
        await ingest_document(
            "test_vault",
            new_source,
            "markdown",
            predecessor_id=d1["id"],
        )
    )
    assert "error" not in result, (
        f"omitted expected_head_version must succeed without check: {result!r}"
    )


# ---------------------------------------------------------------------------
# T2: Stale rejection (also surfaces the structured 409 envelope)
# ---------------------------------------------------------------------------


async def test_t2_stale_expected_head_version_returns_structured_409(vault_services):
    """A mismatched `expected_head_version` rejects with the structured
    `stale_chain_head` 409 envelope. No new document inserted, no
    supersedes edge created, predecessor remains active.
    """
    _, test_dir = vault_services
    d1 = await _seed_chain_head(test_dir, "d1")
    v0 = d1["updated_at"]

    # Out-of-band advance: update_metadata bumps the predecessor's
    # updated_at without superseding it. v0 is now stale.
    bumped = _parse(
        await update_metadata("test_vault", d1["id"], title="renamed", expected_version=v0)
    )
    assert "error" not in bumped
    v_current = bumped["document"]["updated_at"]
    assert v_current != v0

    new_source = await _write_source(test_dir, "d2", "# D2\n\nShould not land.")
    result = _parse(
        await ingest_document(
            "test_vault",
            new_source,
            "markdown",
            predecessor_id=d1["id"],
            expected_head_version=v0,
        )
    )
    assert result["error"] == "stale_chain_head"
    assert result["detail"]["predecessor_id"] == d1["id"]
    assert result["detail"]["expected_head_version"] == v0
    assert result["detail"]["current_head_id"] == d1["id"]
    assert result["detail"]["current_head_version"] == v_current

    # State unchanged: D1 still active, no inbound supersedes edge.
    d1_after = _parse(await get_document("test_vault", d1["id"]))
    assert d1_after["lifecycle_status"] == "active"
    inbound = _parse(
        await traverse(
            "test_vault",
            start_id=d1["id"],
            edge_type="supersedes",
            direction="inbound",
            depth=1,
        )
    )
    edges = [n for n in inbound["nodes"] if n.get("edge")]
    assert edges == [], f"stale rejection must not create a supersedes edge; got {edges!r}"


# ---------------------------------------------------------------------------
# T4: Parallel-ingest race — exactly one writer lands; chain stays linear
# ---------------------------------------------------------------------------


async def test_t4_parallel_same_version_one_wins_one_stale(vault_services):
    """Two parallel ingests both holding `expected_head_version=V0` and
    `predecessor_id=current_head.id`: exactly one supersede lands and
    the other rejects with `stale_chain_head` whose `current_head_id`
    and `current_head_version` point to the winner's new head. The
    supersedes chain remains linear (no fork) — the defining test of
    the fix. Runs 25 iterations on fresh chains to surface interleaving
    races.
    """
    _, test_dir = vault_services

    for i in range(25):
        # Fresh chain per iteration: a brand-new head with a stable
        # terminal updated_at. Each iteration's source files carry
        # distinct content so the duplicate-content guard never trips.
        head = await _seed_chain_head(test_dir, f"iter_{i}_head")
        head_id = head["id"]
        v_baseline = head["updated_at"]

        a_src = await _write_source(test_dir, f"iter_{i}_a", f"# A{i}\n\nA-side body iter={i}.")
        b_src = await _write_source(test_dir, f"iter_{i}_b", f"# B{i}\n\nB-side body iter={i}.")
        results = await asyncio.gather(
            ingest_document(
                "test_vault",
                a_src,
                "markdown",
                predecessor_id=head_id,
                expected_head_version=v_baseline,
            ),
            ingest_document(
                "test_vault",
                b_src,
                "markdown",
                predecessor_id=head_id,
                expected_head_version=v_baseline,
            ),
        )
        parsed = [_parse(r) for r in results]
        successes = [p for p in parsed if "error" not in p]
        stale = [p for p in parsed if p.get("error") == "stale_chain_head"]
        assert len(successes) == 1, f"iteration {i}: expected exactly one success; got {parsed!r}"
        assert len(stale) == 1, (
            f"iteration {i}: expected exactly one stale_chain_head; got {parsed!r}"
        )

        # Loser observes winner's new head id and version.
        winner = successes[0]
        winner_id = winner["id"]
        # Wait for the winner's updated_at to be stable (background
        # pipeline). Then compare the stale envelope's
        # current_head_version against the head's *post-supersede*
        # version (the predecessor's archive-time updated_at, which is
        # what the loser's fresh re-read inside the lock observed).
        head_post = _parse(await get_document("test_vault", head_id))
        assert head_post["lifecycle_status"] == "archived"
        assert stale[0]["detail"]["predecessor_id"] == head_id
        assert stale[0]["detail"]["expected_head_version"] == v_baseline
        # The current_head_version is the predecessor's updated_at at
        # the moment the loser's check ran — which equals the
        # archive-time updated_at the winner's transition wrote.
        assert stale[0]["detail"]["current_head_version"] == head_post["updated_at"], (
            f"iteration {i}: stale envelope's current_head_version must equal "
            f"the predecessor's post-supersede updated_at; got "
            f"{stale[0]['detail']['current_head_version']!r} vs "
            f"head_post {head_post['updated_at']!r}"
        )

        # Chain linearity: exactly one inbound supersedes edge into head_id.
        inbound = _parse(
            await traverse(
                "test_vault",
                start_id=head_id,
                edge_type="supersedes",
                direction="inbound",
                depth=1,
            )
        )
        edges = [n for n in inbound["nodes"] if n.get("edge")]
        assert len(edges) == 1, (
            f"iteration {i}: chain forked — expected one inbound edge, got {len(edges)}: {edges!r}"
        )
        assert edges[0]["edge"]["source_id"] == winner_id


# ---------------------------------------------------------------------------
# T5: Retry-after-stale end-to-end
# ---------------------------------------------------------------------------


async def test_t5_retry_after_stale_succeeds_with_current_head(vault_services):
    """End-to-end recovery: stale supersede returns the current head's
    id and version in its detail; caller uses `current_head_version`
    directly (no refetch) to construct the retry call. The retry
    succeeds and the chain advances.

    Uses an out-of-band `update_metadata` bump for the version
    advance so the predecessor stays the chain head (still
    `lifecycle_status=active`). The supersede-as-racer case is covered
    by T6 — once a racer archives the predecessor, the existing
    `supersede_target_not_active` guard fires before the version
    check, and the caller's retry path is "pivot to the new head" not
    "supply current_head_version".
    """
    _, test_dir = vault_services
    d1 = await _seed_chain_head(test_dir, "d1")
    v0 = d1["updated_at"]

    # A racer bumps d1's updated_at without superseding it.
    bumped = _parse(
        await update_metadata("test_vault", d1["id"], title="racer", expected_version=v0)
    )
    assert "error" not in bumped
    v_after_racer = bumped["document"]["updated_at"]
    assert v_after_racer != v0

    # The original caller holds v0; their supersede is now stale.
    caller_src = await _write_source(test_dir, "caller", "# Caller\n\nFirst attempt.")
    stale = _parse(
        await ingest_document(
            "test_vault",
            caller_src,
            "markdown",
            predecessor_id=d1["id"],
            expected_head_version=v0,
        )
    )
    assert stale["error"] == "stale_chain_head"
    assert stale["detail"]["current_head_id"] == d1["id"]
    retry_token = stale["detail"]["current_head_version"]
    assert retry_token == v_after_racer

    # Use the envelope's retry token directly — no separate refetch.
    retry_src = await _write_source(test_dir, "caller_retry", "# Caller retry\n\nSecond attempt.")
    retry_result = _parse(
        await ingest_document(
            "test_vault",
            retry_src,
            "markdown",
            predecessor_id=d1["id"],
            expected_head_version=retry_token,
        )
    )
    assert "error" not in retry_result, f"retry must succeed: {retry_result!r}"

    # Chain advanced: D1 archived, exactly one inbound supersedes edge.
    d1_after = _parse(await get_document("test_vault", d1["id"]))
    assert d1_after["lifecycle_status"] == "archived"
    inbound = _parse(
        await traverse(
            "test_vault",
            start_id=d1["id"],
            edge_type="supersedes",
            direction="inbound",
            depth=1,
        )
    )
    edges = [n for n in inbound["nodes"] if n.get("edge")]
    assert len(edges) == 1
    assert edges[0]["edge"]["source_id"] == retry_result["id"]


# ---------------------------------------------------------------------------
# T6: Pre-validation ordering — archived predecessor surfaces the existing error
# ---------------------------------------------------------------------------


async def test_t6_archived_predecessor_surfaces_existing_supersede_target_not_active(
    vault_services,
):
    """When the predecessor is already archived at call time, the
    existing `supersede_target_not_active` 409 still fires (it is the
    first check, ordered before the version check). `expected_head_version`
    is *additive*: it does not replace the existing "predecessor must be
    active" guard. Regression guard for validation ordering.
    """
    _, test_dir = vault_services
    d1 = await _seed_chain_head(test_dir, "d1")
    v0 = d1["updated_at"]

    # Out-of-band supersede: D1 → D2. D1 is now archived.
    advance_src = await _write_source(test_dir, "d2_advance", "# D2\n\nAdvance to archive d1.")
    advance = _parse(
        await ingest_document(
            "test_vault",
            advance_src,
            "markdown",
            predecessor_id=d1["id"],
            expected_head_version=v0,
        )
    )
    assert "error" not in advance

    # Caller still holds D1 as predecessor + the pre-archive version.
    late_src = await _write_source(test_dir, "late", "# Late\n\nLate-comer.")
    result = _parse(
        await ingest_document(
            "test_vault",
            late_src,
            "markdown",
            predecessor_id=d1["id"],
            expected_head_version=v0,
        )
    )
    assert result["error"] == "supersede_target_not_active", (
        f"expected supersede_target_not_active to fire before the version check; got {result!r}"
    )


# ---------------------------------------------------------------------------
# T7: `expected_head_version` without `predecessor_id` is a caller error
# ---------------------------------------------------------------------------


async def test_t7_expected_head_version_without_predecessor_id_is_400(vault_services):
    """A caller bug: supplying `expected_head_version` without a
    `predecessor_id` to anchor it has no defined meaning. Fail loud with
    a 400 rather than silently ignoring the parameter.
    """
    _, test_dir = vault_services
    bare_src = await _write_source(test_dir, "bare", "# Bare\n\nNo predecessor.")
    result = _parse(
        await ingest_document(
            "test_vault",
            bare_src,
            "markdown",
            expected_head_version="any-string",
        )
    )
    assert result.get("error") == "expected_head_version_requires_predecessor", (
        f"expected structured 400; got {result!r}"
    )


# ---------------------------------------------------------------------------
# T9: FastMCP wire-path round-trip for the stale_chain_head envelope
# ---------------------------------------------------------------------------


async def test_t9_fastmcp_wire_path_surfaces_stale_chain_head_envelope(vault_services):
    """Through the JSON-RPC dispatch (call_tool), a stale
    `expected_head_version` produces `{"error": "stale_chain_head",
    "message": ..., "detail": {...}}` on real MCP transport. Pins
    MCP/HTTP transport symmetry.
    """
    from mcp.types import TextContent

    _, test_dir = vault_services
    d1 = await _seed_chain_head(test_dir, "d1")
    v0 = d1["updated_at"]

    bumped = _parse(
        await update_metadata("test_vault", d1["id"], title="advance", expected_version=v0)
    )
    v_current = bumped["document"]["updated_at"]

    rpc_src = await _write_source(test_dir, "rpc", "# RPC\n\nMCP wire-path body.")
    response = await _mcp.mcp.call_tool(
        "ingest_document",
        {
            "vault_id": "test_vault",
            "source": rpc_src,
            "source_type": "markdown",
            "predecessor_id": d1["id"],
            "expected_head_version": v0,
        },
    )
    assert isinstance(response, list)
    assert len(response) == 1
    assert isinstance(response[0], TextContent)
    envelope = json.loads(response[0].text)
    assert envelope["error"] == "stale_chain_head"
    assert envelope["detail"]["predecessor_id"] == d1["id"]
    assert envelope["detail"]["expected_head_version"] == v0
    assert envelope["detail"]["current_head_id"] == d1["id"]
    assert envelope["detail"]["current_head_version"] == v_current


# ---------------------------------------------------------------------------
# T10: FastAPI / HTTP transport round-trip
# ---------------------------------------------------------------------------


@pytest.fixture
async def http_app(minimal_vault_config_dict, monkeypatch, tmp_vault_dir):
    """Boot the ASGI app and expose `(app, vault_id, test_dir)`.

    Mirrors the `seeded_http_app` fixture in `test_expected_version.py`
    but without a pre-seeded document — the ingest tests need to do
    their own seeding so the source file ends up on disk where the
    real ingestion service can read it.
    """
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    await _initialize_services(
        app,
        config,
        content_store_factory=lambda _brain: StubContentStore(),
    )
    vault_id = config.vault.id

    sources = tmp_vault_dir / "sources"
    test_dir = sources / "test"
    test_dir.mkdir(parents=True, exist_ok=True)

    yield app, vault_id, test_dir

    await asyncio.sleep(0.1)
    if vault_id in app.state.vault_registry:
        app.state.vault_registry[vault_id].close_timing()
        await app.state.vault_registry[vault_id].graph_store.close()
    _mcp._vaults.clear()


async def test_t10_http_post_stale_expected_head_version_returns_409_envelope(http_app):
    """POST /sage_vaults/{vault_id}/documents with stale
    `expected_head_version` returns 409 with the structured
    `ErrorResponse(code="stale_chain_head", message=..., detail={...})`
    body. Confirms the FastAPI `sage_error_handler` routes the new
    exception correctly.
    """
    app, vault_id, test_dir = http_app

    seed_src = test_dir / "seed.md"
    seed_src.write_text("# Seed\n\nHTTP-path seed body.")
    new_src = test_dir / "new.md"
    new_src.write_text("# New\n\nHTTP-path supersede body.")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        seeded = await client.post(
            f"/sage_vaults/{vault_id}/documents",
            json={"source": "test/seed.md", "source_type": "markdown"},
        )
        assert seeded.status_code == 201, seeded.text
        seeded_doc = seeded.json()["document"]
        d1_id = seeded_doc["id"]

        # Wait for terminal pipeline_status so updated_at is stable.
        for _ in range(150):
            current = await client.get(f"/sage_vaults/{vault_id}/documents/{d1_id}")
            if current.json().get("pipeline_status") in _TERMINAL_PIPELINE_STATES:
                break
            await asyncio.sleep(0.02)
        v0 = current.json()["updated_at"]

        # Out-of-band bump so v0 is stale. Post-CAS-ADR-029, the metadata
        # endpoint is POST /metadata with an items[] body.
        bump = await client.post(
            f"/sage_vaults/{vault_id}/metadata",
            json={"items": [{"document_id": d1_id, "title": "advance"}]},
        )
        assert bump.status_code == 200, bump.text
        bump_body = bump.json()
        assert bump_body["success_count"] == 1, bump_body
        v_current = bump_body["results"][0]["document"]["updated_at"]
        assert v_current != v0

        response = await client.post(
            f"/sage_vaults/{vault_id}/documents",
            json={
                "source": "test/new.md",
                "source_type": "markdown",
                "predecessor_id": d1_id,
                "expected_head_version": v0,
            },
        )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "stale_chain_head"
    assert body["detail"]["predecessor_id"] == d1_id
    assert body["detail"]["expected_head_version"] == v0
    assert body["detail"]["current_head_id"] == d1_id
    assert body["detail"]["current_head_version"] == v_current
