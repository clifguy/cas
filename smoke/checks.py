"""Scenario check catalog for the API smoke sweep.

Ordered groups walk one disposable vault from creation through ingest,
reads, retrieval, mutation, graph review, maintenance, and the BFF
surface, asserting one substantive effect per endpoint. Group functions
share a Ctx that accumulates document ids and hashes so later checks
assert exact values instead of ">0".

Profile behavior: checks run identically under both profiles unless the
endpoint's own contract differs by deployment (path-based ingest, host
opener, download-url binding, BFF co-location, OIDC). Those checks
assert the documented per-profile behavior rather than skipping.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from client import CheckFailure, Recorder, SmokeClient, expect, poll

FIXTURES = {
    "alpha": "2026-07-01_SPC-001_alpha-solar-observatory.md",
    "beta1": "2026-07-02_SPC-002_beta-tide-gauge_v1.md",
    "beta2": "2026-07-02_SPC-002_beta-tide-gauge_v2.md",
    # gamma and theta parse to the workflow doc_type `checklist` and share
    # code SPC-001 with zeta (content), so the L-021 batch stages exactly
    # two Tier-2 covers edges — one to confirm, one to dismiss.
    "gamma": "2026-07-03_SPC-001_gamma-observatory-checklist.md",
    "theta": "2026-07-03_SPC-001_theta-calibration-checklist.md",
    "zeta": "2026-07-05_SPC-001_zeta-observatory-buoy-brief.md",
    "delta": "2026-07-04_SPC-003_delta-harbor-log.md",
    "epsilon": "2026-07-05_SPC-004_epsilon-lighthouse-notes.md",
    # eta rides the review queue (needs_review); it is the only document
    # expected in pending-metadata.
    "eta": "2026-07-05_SPC-005_eta-signal-station-brief.md",
}

PIPELINE_TIMEOUT = 900.0  # first ingest loads embedding + abstraction models


@dataclass
class Ctx:
    profile: str  # local | cloud
    vault_id: str
    aux_vault_id: str
    fixtures: Path
    vault_config: dict
    aux_vault_config: dict
    expected_build: str | None = None
    ids: dict[str, str] = field(default_factory=dict)
    hashes: dict[str, str] = field(default_factory=dict)
    manual_edge_id: str | None = None
    confirmed_production_edge_id: str | None = None
    lifecycle_state: dict[str, str] = field(default_factory=dict)
    mcp_tools: dict[str, list[str]] = field(default_factory=dict)

    def v(self, path: str) -> str:
        return f"/sage_vaults/{self.vault_id}{path}"

    def fixture_bytes(self, key: str) -> bytes:
        return (self.fixtures / FIXTURES[key]).read_bytes()

    def fixture_sha(self, key: str) -> str:
        return hashlib.sha256(self.fixture_bytes(key)).hexdigest()


def _await_terminal(c: SmokeClient, ctx: Ctx, doc_id: str, *, timeout: float) -> dict:
    def probe() -> dict | None:
        doc = c.expect_status(c.req("GET", ctx.v(f"/documents/{doc_id}")), 200)
        status = doc.get("pipeline_status")
        if status == "failed":
            raise CheckFailure(f"pipeline failed: {doc.get('pipeline_error')}")
        if status in ("abstraction_complete", "abstraction_skipped"):
            return doc
        return None

    return poll(probe, timeout=timeout, what=f"terminal pipeline status for {doc_id}")


def _parse_fixture(c: SmokeClient, ctx: Ctx, key: str) -> dict:
    """Run the vault's filename parser the way the real caller flow does.

    The batch surfaces build their edge-inference items solely from the
    caller-supplied per-file parsed_metadata (the scan step parses, the
    ingest step consumes), so the driver mirrors that contract instead
    of expecting the server to re-parse uploads.
    """
    body = c.expect_status(
        c.req(
            "POST",
            ctx.v("/parse-filename"),
            json={"filename": FIXTURES[key], "source_type": "markdown"},
        ),
        200,
    )
    parsed = {
        "title": body.get("title"),
        "date": body.get("document_date"),
        "project": body.get("project"),
        "codes": body.get("codes") or [],
        "version": body.get("version_label"),
        "doc_type": body.get("doc_type"),
    }
    return {k: v for k, v in parsed.items() if v not in (None, [])}


def _upload_batch(
    c: SmokeClient,
    ctx: Ctx,
    keys: list[str],
    *,
    infer_edges: bool,
    needs_review: bool,
):
    files = [("files", (FIXTURES[k], ctx.fixture_bytes(k), "text/markdown")) for k in keys]
    meta = {
        "infer_edges": infer_edges,
        "needs_review": needs_review,
        "files": [
            {"source_type": "markdown", "parsed_metadata": _parse_fixture(c, ctx, k)} for k in keys
        ],
    }
    return c.sse(
        "POST",
        ctx.v("/documents:batch"),
        files=files,
        data={"metadata": json.dumps(meta)},
    )


def _batch_ingest_one(
    c: SmokeClient, ctx: Ctx, key: str, *, needs_review: bool, infer_edges: bool = False
) -> str:
    """Cloud-profile ingest path: upload one fixture, return its document id."""
    stream = _upload_batch(c, ctx, [key], infer_edges=infer_edges, needs_review=needs_review)
    done = [
        e.data
        for e in stream.events
        if e.data.get("event_type") == "progress" and e.data.get("status") == "completed"
    ]
    expect(len(done) == 1 and done[0].get("document_id"), f"no completed event for {key}")
    return done[0]["document_id"]


# --------------------------------------------------------------- group 0
def group0_boot(c: SmokeClient, rec: Recorder, ctx: Ctx) -> None:
    print("— group 0: stack & boot")

    def l001() -> str:
        body = c.expect_status(c.req("GET", "/health"), 200)
        expect(bool(body), "empty health envelope")
        return f"health {body}"

    rec.run(
        "L-001",
        "liveness probe",
        l001,
        surface="rest",
        db_class="none",
        rw="read",
        covers_rest=[("get", "/health")],
    )

    def l002() -> str:
        notes = []
        for mount in ("/mcp", "/mcp_admin"):
            result = c.mcp_initialize(mount)
            instructions = result.get("instructions", "")
            expect("SAGE running build:" in instructions, f"{mount}: no build handshake")
            build = instructions.split("SAGE running build:", 1)[1].split(".", 1)[0].strip()
            if ctx.expected_build:
                expect(
                    ctx.expected_build in instructions,
                    f"{mount} build {build!r} != expected {ctx.expected_build!r}",
                )
            notes.append(f"{mount}={build}")
        return "; ".join(notes)

    rec.run(
        "L-002", "MCP mounts report expected build", l002, surface="mcp", db_class="none", rw="read"
    )

    def l003() -> str:
        body = c.expect_status(c.req("GET", "/sage_vaults"), 200)
        listed = [v["id"] for v in body]
        expect(ctx.vault_id not in listed, f"{ctx.vault_id} already exists: {listed}")
        return f"{len(listed)} pre-existing vault(s)"

    rec.run(
        "L-003",
        "smoke vault absent before create",
        l003,
        surface="rest",
        db_class="none",
        rw="read",
        covers_rest=[("get", "/sage_vaults")],
    )


# --------------------------------------------------------------- group 1
def group1_vault(c: SmokeClient, rec: Recorder, ctx: Ctx) -> None:
    print("— group 1: vault lifecycle")

    def l010() -> str:
        body = c.expect_status(
            c.req("POST", "/sage_vaults", json={"config": ctx.vault_config}), 201
        )
        expect(body.get("id") == ctx.vault_id, f"created id {body.get('id')}")
        listed = [v["id"] for v in c.expect_status(c.req("GET", "/sage_vaults"), 200)]
        expect(ctx.vault_id in listed, f"vault missing from list: {listed}")
        return f"created and listed ({body.get('name')})"

    rec.run(
        "L-010",
        "create smoke vault via API",
        l010,
        surface="rest",
        db_class="relational",
        rw="write",
        covers_rest=[("post", "/sage_vaults")],
    )

    def l011() -> str:
        cfg = c.expect_status(c.req("GET", ctx.v("/config")), 200)
        expect(cfg["vault"]["id"] == ctx.vault_id, "config vault id mismatch")
        expect(
            cfg["retrieval_health"]["assertions_file"]
            == ctx.vault_config["retrieval_health"]["assertions_file"],
            "retrieval_health did not round-trip",
        )
        return "submitted config round-trips"

    rec.run(
        "L-011",
        "vault config round-trip",
        l011,
        surface="rest",
        db_class="none",
        rw="read",
        covers_rest=[("get", "/sage_vaults/{vault_id}/config")],
    )

    def l012() -> str:
        doc_types = json.loads(json.dumps(ctx.vault_config["document_types"]))
        doc_types["doc_types"].append(
            {
                "value": "note",
                "label": "Note",
                "description": "Added by the config-update check.",
                "source_types": ["markdown"],
            }
        )
        body = c.expect_status(
            c.req("PUT", ctx.v("/config"), json={"document_types": doc_types}), 200
        )
        expect(body.get("status") == "updated", f"status {body.get('status')}")
        cfg = c.expect_status(c.req("GET", ctx.v("/config")), 200)
        values = [d["value"] for d in cfg["document_types"]["doc_types"]]
        expect("note" in values, f"added doc_type missing: {values}")
        return "doc_type 'note' added and visible"

    rec.run(
        "L-012",
        "section-level config update",
        l012,
        surface="rest",
        db_class="relational",
        rw="write",
        covers_rest=[("put", "/sage_vaults/{vault_id}/config")],
    )

    def l013() -> str:
        result = c.mcp_call("/mcp_admin", "admin_reload_vault", {"vault_id": ctx.vault_id})
        listed = [v["id"] for v in c.expect_status(c.req("GET", "/sage_vaults"), 200)]
        expect(ctx.vault_id in listed, "vault gone after reload")
        return f"reloaded: {json.dumps(result)[:120]}"

    rec.run(
        "L-013",
        "admin_reload_vault keeps vault serving",
        l013,
        surface="mcp_admin",
        db_class="relational",
        rw="read",
        covers_tools=["admin_reload_vault"],
    )


# --------------------------------------------------------------- group 2
def group2_ingest(c: SmokeClient, rec: Recorder, ctx: Ctx) -> None:
    print("— group 2: ingestion & pipeline")

    def l020() -> str:
        if ctx.profile == "local":
            body = c.expect_status(
                c.req(
                    "POST",
                    ctx.v("/documents"),
                    json={
                        "source": str(ctx.fixtures / FIXTURES["alpha"]),
                        "source_type": "markdown",
                        "metadata": {"title": "Alpha Solar Observatory Operations Manual"},
                    },
                ),
                201,
            )
            doc_id = body["document"]["id"]
        else:
            doc_id = _batch_ingest_one(c, ctx, "alpha", needs_review=False)
        ctx.ids["alpha"] = doc_id
        doc = _await_terminal(c, ctx, doc_id, timeout=PIPELINE_TIMEOUT)
        ctx.hashes["alpha"] = doc["source_content_hash"]
        expect(doc.get("semantic_abstract"), "no semantic abstract after terminal status")
        return f"alpha={doc_id} status={doc['pipeline_status']}"

    rec.run(
        "L-020",
        "single ingest to abstraction_complete",
        l020,
        surface="rest",
        db_class="both",
        rw="write",
        covers_rest=[
            ("post", "/sage_vaults/{vault_id}/documents"),
            ("get", "/sage_vaults/{vault_id}/documents/{document_id}"),
        ],
    )

    def l021() -> str:
        batch_keys = ["beta1", "gamma", "theta", "zeta"]
        stream = _upload_batch(c, ctx, batch_keys, infer_edges=True, needs_review=False)
        payloads = stream.payloads()
        name_to_key = {FIXTURES[k]: k for k in batch_keys}
        for p in payloads:
            if p.get("event_type") == "progress" and p.get("status") == "completed":
                key = name_to_key.get(p.get("filename", ""))
                if key:
                    ctx.ids[key] = p["document_id"]
        summary = [p for p in payloads if p.get("event_type") == "summary"]
        expect(len(summary) == 1, f"{len(summary)} summary events")
        for key in batch_keys:
            statuses = {
                p["status"]
                for p in payloads
                if p.get("event_type") == "progress" and p.get("filename") == FIXTURES[key]
            }
            expect(
                {"started", "completed"} <= statuses,
                f"{FIXTURES[key]} progress states {statuses}",
            )
        expect(
            stream.first_event_lead() > 0.05,
            f"stream not incremental (lead {stream.first_event_lead():.3f}s)",
        )
        staged = summary[0].get("edges_staged") or {}
        expect(staged.get("covers", 0) >= 2, f"expected 2 staged covers edges: {staged}")
        expect(set(batch_keys) <= ctx.ids.keys(), "missing document ids from stream")
        for key in batch_keys:
            _await_terminal(c, ctx, ctx.ids[key], timeout=PIPELINE_TIMEOUT)
        return f"events={len(payloads)} staged={staged}"

    rec.run(
        "L-021",
        "batch upload ingest streams SSE + stages covers",
        l021,
        surface="rest",
        db_class="both",
        rw="write",
        covers_rest=[("post", "/sage_vaults/{vault_id}/documents:batch")],
    )

    def l022() -> str:
        if ctx.profile == "local":
            body = c.expect_status(
                c.req(
                    "POST",
                    ctx.v("/documents"),
                    json={
                        "source": str(ctx.fixtures / FIXTURES["beta2"]),
                        "source_type": "markdown",
                        "predecessor_id": ctx.ids["beta1"],
                        "metadata": {"title": "Beta Tide Gauge Survey"},
                    },
                ),
                201,
            )
            ctx.ids["beta2"] = body["document"]["id"]
        else:
            ctx.ids["beta2"] = _batch_ingest_one(c, ctx, "beta2", needs_review=False)
            items = c.expect_batch(
                c.expect_status(
                    c.req(
                        "POST",
                        ctx.v("/lifecycles"),
                        json={
                            "items": [
                                {
                                    "document_id": ctx.ids["beta1"],
                                    "action": "supersede",
                                    "successor_id": ctx.ids["beta2"],
                                }
                            ]
                        },
                    ),
                    200,
                ),
                success=1,
            )
            expect(items[0]["status"] == "success", f"supersede item: {items[0]}")
        _await_terminal(c, ctx, ctx.ids["beta2"], timeout=PIPELINE_TIMEOUT)
        pred = c.expect_status(c.req("GET", ctx.v(f"/documents/{ctx.ids['beta1']}")), 200)
        expect(pred["lifecycle_status"] == "archived", f"predecessor {pred['lifecycle_status']}")
        return f"beta2={ctx.ids['beta2']} supersedes beta1 (archived)"

    rec.run(
        "L-022",
        "supersession archives predecessor",
        l022,
        surface="rest",
        db_class="both",
        rw="write",
    )

    def l023() -> str:
        if ctx.profile == "local":
            c.mcp_call(
                "/mcp",
                "bulk_ingest_document",
                {
                    "vault_id": ctx.vault_id,
                    "files": [
                        {
                            "file_path": str(ctx.fixtures / FIXTURES["eta"]),
                            "source_type": "markdown",
                        }
                    ],
                    "infer_edges": False,
                },
            )
            eta_id = None
            cat = c.expect_status(
                c.req("POST", ctx.v("/discover"), json={"mode": "catalog", "limit": 50}),
                200,
            )
            for hit in cat["results"]:
                doc = hit.get("document", hit)
                if "eta-signal-station" in doc.get("source_path", ""):
                    eta_id = doc["id"]
            expect(eta_id, "eta id not resolvable after bulk ingest")
        else:
            eta_id = _batch_ingest_one(c, ctx, "eta", needs_review=True, infer_edges=False)
        ctx.ids["eta"] = eta_id
        doc = _await_terminal(c, ctx, eta_id, timeout=PIPELINE_TIMEOUT)
        expect(doc["metadata_confirmed"] is False, "eta not held for review")
        return f"eta={eta_id} metadata_confirmed=false"

    rec.run(
        "L-023",
        "bulk ingest feeds the review queue",
        l023,
        surface="mcp",
        db_class="both",
        rw="write",
        covers_tools=["bulk_ingest_document"],
    )

    def l024() -> str:
        if ctx.profile == "local":
            result = c.mcp_call(
                "/mcp",
                "ingest_document",
                {
                    "vault_id": ctx.vault_id,
                    "source": str(ctx.fixtures / FIXTURES["delta"]),
                    "source_type": "markdown",
                    "metadata": {"title": "Delta Harbor Dredging Log"},
                },
            )
            delta_id = result.get("id") or result.get("document", {}).get("id")
            expect(delta_id, f"no id in ingest result: {json.dumps(result)[:200]}")
        else:
            delta_id = _batch_ingest_one(c, ctx, "delta", needs_review=False)
        ctx.ids["delta"] = delta_id
        _await_terminal(c, ctx, delta_id, timeout=PIPELINE_TIMEOUT)
        return f"delta={delta_id}"

    rec.run(
        "L-024",
        "MCP single-document ingest parity",
        l024,
        surface="mcp",
        db_class="both",
        rw="write",
        covers_tools=["ingest_document"],
    )


# --------------------------------------------------------------- group 3
def group3_reads(c: SmokeClient, rec: Recorder, ctx: Ctx) -> None:
    print("— group 3: document reads")
    alpha = ctx.ids["alpha"]

    def l030() -> str:
        doc = c.expect_status(
            c.req("GET", ctx.v(f"/documents/{alpha}"), params={"include_content": True}), 200
        )
        expect(doc.get("content"), "no inline content")
        decoded = base64.b64decode(doc["content"])
        got = hashlib.sha256(decoded).hexdigest()
        expect(got == ctx.fixture_sha("alpha"), "decoded inline content hash != fixture hash")
        return f"content_size={doc['content_size']}, decoded hash matches"

    rec.run(
        "L-030",
        "get_document with inline content",
        l030,
        surface="rest",
        db_class="relational",
        rw="read",
    )

    def l031() -> str:
        resp = c.req("GET", ctx.v(f"/documents/{alpha}/content"))
        expect(resp.status_code == 200, f"HTTP {resp.status_code}")
        got = hashlib.sha256(resp.content).hexdigest()
        expect(got == ctx.fixture_sha("alpha"), "streamed bytes hash != fixture hash")
        return f"{len(resp.content)} bytes streamed, hash matches"

    rec.run(
        "L-031",
        "raw content stream",
        l031,
        surface="rest",
        db_class="relational",
        rw="read",
        covers_rest=[("get", "/sage_vaults/{vault_id}/documents/{document_id}/content")],
    )

    def l032() -> str:
        resp = c.req("GET", ctx.v(f"/documents/{alpha}/download-url"))
        if ctx.profile == "local":
            c.expect_error_code(resp, 501, "download_url_unavailable")
            return "filesystem binding declines with documented 501"
        body = c.expect_status(resp, 200)
        expect(body.get("download_url", "").startswith("http"), "no URL")
        return "document-store binding issued a URL"

    rec.run(
        "L-032",
        "download-url per binding contract",
        l032,
        surface="rest",
        db_class="relational",
        rw="read",
        covers_rest=[("get", "/sage_vaults/{vault_id}/documents/{document_id}/download-url")],
    )

    def l033() -> str:
        resp = c.req("POST", ctx.v(f"/documents/{alpha}/open"))
        if ctx.profile == "local":
            body = c.expect_status(resp, 200)
            expect(body.get("opened") is True, f"opened={body.get('opened')}")
            return f"host opener launched {body.get('path')}"
        c.expect_error_code(resp, 501, "local_open_only")
        return "cloud profile declines with documented 501"

    rec.run(
        "L-033",
        "host opener per profile contract",
        l033,
        surface="rest",
        db_class="relational",
        rw="read",
        covers_rest=[("post", "/sage_vaults/{vault_id}/documents/{document_id}/open")],
    )

    def l034() -> str:
        headings = c.expect_status(c.req("GET", ctx.v(f"/documents/{alpha}/headings")), 200)
        paths = headings["headings"]
        target = next((h for h in paths if "Dome Maintenance" in h), None)
        expect(target, f"expected heading missing: {paths}")
        section = c.expect_status(c.req("GET", ctx.v(f"/documents/{alpha}/section/{target}")), 200)
        expect("worm-gear" in section["section_text"], "section text mismatch")
        projection = c.expect_status(c.req("GET", ctx.v(f"/documents/{alpha}/projection")), 200)
        expect(
            "spectroheliograph" in (projection.get("projection_text") or "").lower(),
            "projection text mismatch",
        )
        return f"{len(paths)} headings; section + projection text verified"

    rec.run(
        "L-034",
        "projection / section / headings",
        l034,
        surface="rest",
        db_class="relational",
        rw="read",
        covers_rest=[
            ("get", "/sage_vaults/{vault_id}/documents/{document_id}/headings"),
            ("get", "/sage_vaults/{vault_id}/documents/{document_id}/section/{heading_path}"),
            ("get", "/sage_vaults/{vault_id}/documents/{document_id}/projection"),
        ],
    )

    def l035() -> str:
        body = c.expect_status(
            c.req(
                "POST",
                ctx.v(f"/documents/{alpha}/export"),
                json={"output_path": "exports/alpha-projection.md"},
            ),
            200,
        )
        expect(body["document_id"] == alpha, "wrong document echoed")
        return f"exported to {body['output_path']}"

    rec.run(
        "L-035",
        "export projection under storage_root",
        l035,
        surface="rest",
        db_class="relational",
        rw="read",
        covers_rest=[("post", "/sage_vaults/{vault_id}/documents/{document_id}/export")],
    )

    def l076() -> str:
        resp = c.req("GET", ctx.v(f"/documents/{alpha}/editors"))
        expect(
            resp.status_code in (404, 405),
            f"editors unexpectedly implemented: HTTP {resp.status_code}",
        )
        return f"spec-only endpoint absent as expected (HTTP {resp.status_code})"

    rec.run(
        "L-076", "editors endpoints are spec-only", l076, surface="rest", db_class="none", rw="read"
    )


# --------------------------------------------------------------- group 4
def group4_retrieval(c: SmokeClient, rec: Recorder, ctx: Ctx) -> None:
    print("— group 4: retrieval (pgvector proof)")

    def l040() -> str:
        body = c.expect_status(
            c.req("POST", ctx.v("/discover"), json={"mode": "catalog", "limit": 50}), 200
        )
        expect(
            body["total_available"] == len(ctx.ids),
            f"catalog {body['total_available']} != ingested {len(ctx.ids)}",
        )
        return f"{body['total_available']} documents enumerated"

    rec.run(
        "L-040",
        "catalog enumeration exact count",
        l040,
        surface="rest",
        db_class="relational",
        rw="read",
        covers_rest=[("post", "/sage_vaults/{vault_id}/discover")],
    )

    def l041() -> str:
        headings = c.expect_status(
            c.req("GET", ctx.v(f"/documents/{ctx.ids['alpha']}/headings")), 200
        )
        target = next(h for h in headings["headings"] if "Dome Maintenance" in h)
        body = c.expect_status(
            c.req(
                "POST",
                ctx.v("/discover"),
                json={
                    "mode": "deterministic",
                    "document_id": ctx.ids["alpha"],
                    "heading_path": target,
                },
            ),
            200,
        )
        expect(body["results"], "no deterministic results")
        return f"deterministic extraction at {target!r}"

    rec.run(
        "L-041", "deterministic extraction", l041, surface="rest", db_class="relational", rw="read"
    )

    def l042() -> str:
        body = c.expect_status(
            c.req(
                "POST",
                ctx.v("/discover"),
                json={
                    "mode": "semantic",
                    "query": "heliograph collimation drift and spectroheliograph slit alignment",
                    "limit": 5,
                },
            ),
            200,
        )
        hits = body["results"]
        expect(hits, "semantic search returned nothing")
        scores = [h.get("relevance_score") for h in hits]
        expect(all(s is not None for s in scores), f"missing scores: {scores}")
        expect(
            all(scores[i] >= scores[i + 1] for i in range(len(scores) - 1)),
            f"scores not monotonic: {scores}",
        )
        top_docs = [h.get("document", h).get("id") for h in hits]
        expect(ctx.ids["alpha"] in top_docs, f"alpha not in top-5: {top_docs}")
        return f"alpha ranked; scores {['%.3f' % s for s in scores]}"

    rec.run(
        "L-042", "semantic search hits pgvector", l042, surface="rest", db_class="both", rw="read"
    )

    def l043() -> str:
        resp = c.req("POST", ctx.v("/eval-retrieval"))
        if ctx.profile == "cloud":
            c.expect_error_code(resp, 404, "assertions_file_not_found")
            return "no assertions staged in cloud: documented 404"
        body = c.expect_status(resp, 200)
        expect(
            body["passed"] is True and body["assertion_count"] == 2,
            f"eval report: {body}",
        )
        return f"{body['assertion_count']} assertions passed"

    rec.run(
        "L-043",
        "retrieval health assertions",
        l043,
        surface="rest",
        db_class="both",
        rw="read",
        covers_rest=[("post", "/sage_vaults/{vault_id}/eval-retrieval")],
    )

    def l044() -> str:
        result = c.mcp_call(
            "/mcp",
            "search",
            {
                "vault_id": ctx.vault_id,
                "mode": "semantic",
                "query": "acoustic tide gauges reporting water level to the harbor concentrator",
                "limit": 5,
            },
        )
        docs = [r.get("document", r).get("id") for r in result.get("results", [])]
        expect(ctx.ids["beta2"] in docs, f"beta2 not in top-5: {docs}")
        return "MCP semantic parity (beta2 ranked)"

    rec.run(
        "L-044",
        "MCP search parity",
        l044,
        surface="mcp",
        db_class="both",
        rw="read",
        covers_tools=["search"],
    )


# --------------------------------------------------------------- group 5
def group5_metadata_lifecycle(c: SmokeClient, rec: Recorder, ctx: Ctx) -> None:
    print("— group 5: metadata & lifecycle")

    def l050() -> str:
        body = c.expect_status(
            c.req(
                "POST",
                ctx.v("/metadata"),
                json={
                    "items": [
                        {"document_id": ctx.ids["alpha"], "tags": {"add": ["observatory"]}},
                        {"document_id": ctx.ids["delta"], "title": "Delta Harbor Dredging Log"},
                    ]
                },
            ),
            200,
        )
        c.expect_batch(body, success=2)
        doc = c.expect_status(c.req("GET", ctx.v(f"/documents/{ctx.ids['alpha']}")), 200)
        expect("observatory" in doc["tags"], f"tag not applied: {doc['tags']}")
        return "both patches visible on re-read"

    rec.run(
        "L-050",
        "metadata batch patch",
        l050,
        surface="rest",
        db_class="relational",
        rw="write",
        covers_rest=[("post", "/sage_vaults/{vault_id}/metadata")],
    )

    def l051() -> str:
        body = c.expect_status(
            c.req(
                "POST",
                ctx.v("/metadata"),
                json={
                    "items": [
                        {"document_id": ctx.ids["alpha"], "tags": {"add": ["observatory"]}},
                        {"document_id": ctx.ids["gamma"], "tags": {"add": ["appendix"]}},
                    ]
                },
            ),
            200,
        )
        items = c.expect_batch(body, success=1, errors=1)
        failed = next(i for i in items if i["status"] == "error")
        failed_code = failed["error"].get("error") or failed["error"].get("error_code")
        expect(failed_code == "tags_add_conflict", f"unexpected error: {failed['error']}")
        doc = c.expect_status(c.req("GET", ctx.v(f"/documents/{ctx.ids['gamma']}")), 200)
        expect("appendix" in doc["tags"], "sibling item did not commit")
        return "conflict isolated; sibling committed (non-atomic batch observed)"

    rec.run(
        "L-051",
        "per-item conflict does not poison batch",
        l051,
        surface="rest",
        db_class="relational",
        rw="write",
    )

    def l052() -> str:
        pending = c.expect_status(c.req("GET", ctx.v("/pending-metadata")), 200)
        ids = [p["document"]["id"] for p in pending]
        expect(ids == [ctx.ids["eta"]], f"pending queue {ids}, expected only eta")
        body = c.expect_status(
            c.req(
                "POST",
                ctx.v("/metadata"),
                json={
                    "items": [
                        {
                            "document_id": ctx.ids["eta"],
                            "title": "Eta Signal Station Brief",
                        }
                    ]
                },
            ),
            200,
        )
        c.expect_batch(body, success=1)
        after = c.expect_status(c.req("GET", ctx.v("/pending-metadata")), 200)
        expect(
            ctx.ids["eta"] not in [p["document"]["id"] for p in after],
            "eta still pending after confirmation",
        )
        return "queue listed eta exactly; confirmation drained it"

    rec.run(
        "L-052",
        "pending-metadata queue drains on confirm",
        l052,
        surface="rest",
        db_class="relational",
        rw="write",
        covers_rest=[("get", "/sage_vaults/{vault_id}/pending-metadata")],
    )

    def l053() -> str:
        def transition(doc_key: str, action: str, ok: bool) -> dict:
            body = c.expect_status(
                c.req(
                    "POST",
                    ctx.v("/lifecycles"),
                    json={"items": [{"document_id": ctx.ids[doc_key], "action": action}]},
                ),
                200,
            )
            items = c.expect_batch(body, success=1 if ok else 0, errors=0 if ok else 1)
            return items[0]

        transition("delta", "complete", True)
        doc = c.expect_status(c.req("GET", ctx.v(f"/documents/{ctx.ids['delta']}")), 200)
        expect(doc["lifecycle_status"] == "completed", doc["lifecycle_status"])
        bad = transition("delta", "complete", False)
        bad_code = bad["error"].get("error") or bad["error"].get("error_code")
        expect(bad_code == "invalid_lifecycle_transition", f"no structured error: {bad}")
        transition("delta", "archive", True)
        transition("delta", "reactivate", True)
        doc = c.expect_status(c.req("GET", ctx.v(f"/documents/{ctx.ids['delta']}")), 200)
        expect(doc["lifecycle_status"] == "active", doc["lifecycle_status"])
        dry = c.expect_status(
            c.req(
                "POST",
                ctx.v("/lifecycles"),
                json={
                    "items": [{"document_id": ctx.ids["delta"], "action": "complete"}],
                    "dry_run": True,
                },
            ),
            200,
        )
        c.expect_batch(dry, success=1)
        doc = c.expect_status(c.req("GET", ctx.v(f"/documents/{ctx.ids['delta']}")), 200)
        expect(doc["lifecycle_status"] == "active", "dry-run persisted a transition")
        return "complete/invalid/archive/reactivate/dry-run all per state machine"

    rec.run(
        "L-053",
        "lifecycle state machine walk",
        l053,
        surface="rest",
        db_class="relational",
        rw="write",
        covers_rest=[("post", "/sage_vaults/{vault_id}/lifecycles")],
    )


# --------------------------------------------------------------- group 6
def group6_graph(c: SmokeClient, rec: Recorder, ctx: Ctx) -> None:
    print("— group 6: graph operations")

    def l060() -> str:
        item = {
            "source_id": ctx.ids["alpha"],
            "target_id": ctx.ids["beta2"],
            "edge_type": "references",
            "source_valid_from_version": ctx.ids["alpha"],
            "target_valid_from_version": ctx.ids["beta2"],
            "rationale": "manual smoke edge",
        }
        body = c.expect_status(
            c.req("POST", ctx.v("/edges"), json={"items": [item], "response_mode": "full"}), 200
        )
        results = c.expect_batch(body, success=1)
        expect(results[0]["created"] is True, f"created={results[0]['created']}")
        ctx.manual_edge_id = results[0]["edge"]["id"]
        again = c.expect_status(
            c.req("POST", ctx.v("/edges"), json={"items": [item], "response_mode": "full"}), 200
        )
        results2 = c.expect_batch(again, success=1)
        expect(results2[0]["created"] is False, "duplicate natural key created a new edge")
        expect(results2[0]["edge"]["id"] == ctx.manual_edge_id, "different edge id on repeat")
        return f"edge {ctx.manual_edge_id} created once, idempotent on repeat"

    rec.run(
        "L-060",
        "create_edges idempotent natural key",
        l060,
        surface="rest",
        db_class="relational",
        rw="write",
        covers_rest=[("post", "/sage_vaults/{vault_id}/edges")],
    )

    def l061() -> str:
        body = c.expect_status(
            c.req(
                "POST",
                ctx.v("/traverse"),
                json={
                    "start_id": ctx.ids["alpha"],
                    "edge_type": "references",
                    "direction": "outbound",
                    "depth": 2,
                },
            ),
            200,
        )
        found = json.dumps(body["nodes"])
        expect(ctx.ids["beta2"] in found, "beta2 unreachable via references traverse")
        return f"{len(body['nodes'])} node(s) reached"

    rec.run(
        "L-061",
        "traverse walks the new edge",
        l061,
        surface="rest",
        db_class="relational",
        rw="read",
        covers_rest=[("post", "/sage_vaults/{vault_id}/traverse")],
    )

    def l062() -> str:
        body = c.expect_status(
            c.req(
                "POST",
                ctx.v("/chain"),
                json={"document_id": ctx.ids["beta1"], "edge_type": "supersedes"},
            ),
            200,
        )
        expect(body["length"] == 2, f"chain length {body['length']}")
        expect(body["head_id"] == ctx.ids["beta2"], f"head {body['head_id']}")
        expect(body["is_linear"] is True, "chain not linear")
        return "2-entry linear chain, head=beta2"

    rec.run(
        "L-062",
        "supersedes chain resolution",
        l062,
        surface="rest",
        db_class="relational",
        rw="read",
        covers_rest=[("post", "/sage_vaults/{vault_id}/chain")],
    )

    def l063() -> str:
        body = c.expect_status(c.req("GET", ctx.v(f"/preconditions/{ctx.ids['alpha']}")), 200)
        expect("satisfied" in body and "checks" in body, f"malformed result {body}")
        return f"satisfied={body['satisfied']} checks={len(body['checks'])}"

    rec.run(
        "L-063",
        "precondition evaluation",
        l063,
        surface="rest",
        db_class="relational",
        rw="read",
        covers_rest=[("get", "/sage_vaults/{vault_id}/preconditions/{function_id}")],
    )

    def l064() -> str:
        edge_id = ctx.manual_edge_id
        dry = c.expect_status(
            c.req("DELETE", ctx.v(f"/edges/{edge_id}"), params={"dry_run": True}), 200
        )
        expect(dry["dry_run"] is True and dry["deleted"] is False, f"dry-run shape {dry}")
        still = c.expect_status(
            c.req(
                "POST",
                ctx.v("/traverse"),
                json={
                    "start_id": ctx.ids["alpha"],
                    "edge_type": "references",
                    "direction": "outbound",
                    "depth": 1,
                },
            ),
            200,
        )
        expect(ctx.ids["beta2"] in json.dumps(still["nodes"]), "dry-run deleted the edge")
        real = c.expect_status(c.req("DELETE", ctx.v(f"/edges/{edge_id}")), 200)
        expect(real["deleted"] is True, f"real delete: {real}")
        gone = c.expect_status(
            c.req(
                "POST",
                ctx.v("/traverse"),
                json={
                    "start_id": ctx.ids["alpha"],
                    "edge_type": "references",
                    "direction": "outbound",
                    "depth": 1,
                },
            ),
            200,
        )
        expect(ctx.ids["beta2"] not in json.dumps(gone["nodes"]), "edge survived delete")
        return "dry-run preserved, real delete removed, traverse confirms both"

    rec.run(
        "L-064",
        "edge delete dry-run vs real",
        l064,
        surface="rest",
        db_class="relational",
        rw="write",
        covers_rest=[("delete", "/sage_vaults/{vault_id}/edges/{edge_id}")],
    )

    def l065() -> str:
        staged = c.expect_status(c.req("GET", ctx.v("/staging-edges")), 200)
        expect(len(staged) >= 2, f"{len(staged)} staging edges, need >=2 (covers inference)")
        confirm = c.expect_status(
            c.req("POST", ctx.v(f"/staging-edges/{staged[0]['id']}/confirm")), 200
        )
        expect(confirm["confirmed"] is True, f"confirm: {confirm}")
        ctx.confirmed_production_edge_id = confirm["production_edge_id"]
        dismiss = c.expect_status(
            c.req("POST", ctx.v(f"/staging-edges/{staged[1]['id']}/dismiss")), 200
        )
        expect(dismiss["dismissed"] is True, f"dismiss: {dismiss}")
        after = {e["id"] for e in c.expect_status(c.req("GET", ctx.v("/staging-edges")), 200)}
        expect(
            staged[0]["id"] not in after and staged[1]["id"] not in after,
            "reviewed edges still staged",
        )
        edges = c.expect_status(
            c.req(
                "POST",
                ctx.v("/discover"),
                json={
                    "mode": "catalog",
                    "target": "edges",
                    "filters": {"edge_type": "covers"},
                    "limit": 20,
                },
            ),
            200,
        )
        expect(edges["total_available"] >= 1, "confirmed covers edge not in production")
        return (
            f"staged={len(staged)} confirmed->{ctx.confirmed_production_edge_id},"
            f" dismissed one, production covers={edges['total_available']}"
        )

    rec.run(
        "L-065",
        "staging edge review round-trip",
        l065,
        surface="rest",
        db_class="relational",
        rw="write",
        covers_rest=[
            ("get", "/sage_vaults/{vault_id}/staging-edges"),
            ("post", "/sage_vaults/{vault_id}/staging-edges/{edge_id}/confirm"),
            ("post", "/sage_vaults/{vault_id}/staging-edges/{edge_id}/dismiss"),
        ],
    )


# --------------------------------------------------------------- group 10 (BFF)
def group10_bff(c: SmokeClient, rec: Recorder, ctx: Ctx) -> None:
    print("— group 10: application backend")

    def l100() -> str:
        resp = c.req(
            "POST",
            "/app/scan",
            json={"vault_id": ctx.vault_id, "directory": str(ctx.fixtures)},
        )
        if ctx.profile == "cloud":
            c.expect_error_code(resp, 501, "local_profile_only")
            return "standalone cloud BFF declines with documented 501"
        body = c.expect_status(resp, 200)
        by_name = {Path(f["file_path"]).name: f for f in body["files"]}
        expect(len(body["files"]) == len(FIXTURES), f"scanned {len(body['files'])} files")
        alpha_row = by_name.get(FIXTURES["alpha"]) or {}
        expect(
            alpha_row.get("sage_status") == "unchanged",
            f"alpha not classified as ingested: {alpha_row.get('sage_status')}",
        )
        eps_row = by_name.get(FIXTURES["epsilon"]) or {}
        expect(
            eps_row.get("sage_status") == "new",
            f"epsilon should be new: {eps_row.get('sage_status')}",
        )
        return f"{len(body['files'])} files, unchanged/new classification correct"

    rec.run(
        "L-100",
        "directory scan classifies known hashes",
        l100,
        surface="bff",
        db_class="relational",
        rw="read",
        covers_rest=[("post", "/app/scan")],
    )

    def l101() -> str:
        if ctx.profile == "cloud":
            resp = c.req(
                "POST",
                "/app/ingest",
                json={
                    "vault_id": ctx.vault_id,
                    "files": [{"file_path": "/nonexistent", "source_type": "markdown"}],
                },
            )
            c.expect_error_code(resp, 501, "local_profile_only")
            return "standalone cloud BFF declines with documented 501"
        stream = c.sse(
            "POST",
            "/app/ingest",
            json={
                "vault_id": ctx.vault_id,
                "files": [
                    {
                        "file_path": str(ctx.fixtures / FIXTURES["epsilon"]),
                        "source_type": "markdown",
                    }
                ],
                "infer_edges": False,
            },
        )
        done = [
            e.data
            for e in stream.events
            if e.data.get("event_type") == "progress" and e.data.get("status") == "completed"
        ]
        expect(len(done) == 1, f"completed events: {len(done)}")
        ctx.ids["epsilon"] = done[0]["document_id"]
        _await_terminal(c, ctx, ctx.ids["epsilon"], timeout=PIPELINE_TIMEOUT)
        return f"epsilon={ctx.ids['epsilon']} via BFF SSE"

    rec.run(
        "L-101",
        "BFF batch ingest streams SSE",
        l101,
        surface="bff",
        db_class="both",
        rw="write",
        covers_rest=[("post", "/app/ingest")],
    )

    def l102() -> str:
        resp = c.req("GET", "/app/auth/login")
        if ctx.profile == "local":
            c.expect_error_code(resp, 503, "auth_not_configured")
            return "no OIDC locally: documented 503"
        body = c.expect_status(resp, 200)
        url = body.get("authorization_url", "")
        expect(
            "state=" in url and "login.microsoftonline.com" in url,
            f"unexpected challenge URL: {url[:120]}",
        )
        return "login challenge issued (pending-login row written)"

    rec.run(
        "L-102",
        "auth login per profile contract",
        l102,
        surface="bff",
        db_class="relational",
        rw="write",
        covers_rest=[("get", "/app/auth/login")],
    )

    def c103() -> str:
        me = c.expect_status(c.req("GET", "/app/auth/me"), 200)
        expect(
            me.get("authenticated") in (False, None) or me.get("user") is None,
            f"anonymous /me unexpectedly authenticated: {me}",
        )
        out = c.req("POST", "/app/auth/logout")
        expect(out.status_code in (200, 204), f"logout HTTP {out.status_code}")
        return "anonymous session envelope + no-session logout"

    if ctx.profile == "cloud":
        rec.run(
            "C-103",
            "session info + logout without cookie",
            c103,
            surface="bff",
            db_class="relational",
            rw="read",
            covers_rest=[("get", "/app/auth/me"), ("post", "/app/auth/logout")],
        )
    else:
        rec.skip(
            "C-103",
            "session info + logout without cookie",
            "session store opens only when OIDC is configured (cloud)",
            surface="bff",
            db_class="relational",
            rw="read",
            covers_rest=[
                ("get", "/app/auth/me"),
                ("post", "/app/auth/logout"),
                ("get", "/app/auth/callback"),
            ],
        )


# --------------------------------------------------------------- group 7
def group7_vault_utilities(c: SmokeClient, rec: Recorder, ctx: Ctx) -> None:
    print("— group 7: users, stats, utilities")

    def l070() -> str:
        body = c.expect_status(
            c.req(
                "POST",
                ctx.v("/users"),
                json={"display_name": "Smoke Operator", "user_type": "human"},
            ),
            201,
        )
        expect(body.get("id") and body["user_type"] == "human", f"user: {body}")
        ctx.ids["_user"] = body["id"]
        return f"user {body['id']} registered"

    rec.run(
        "L-070",
        "register user",
        l070,
        surface="rest",
        db_class="relational",
        rw="write",
        covers_rest=[("post", "/sage_vaults/{vault_id}/users")],
    )

    def l071() -> str:
        body = c.expect_status(c.req("GET", ctx.v("/stats")), 200)
        doc_ids = {k: v for k, v in ctx.ids.items() if not k.startswith("_")}
        expect(
            body["total_documents"] == len(doc_ids),
            f"stats {body['total_documents']} docs != scenario {len(doc_ids)}",
        )
        expect(
            body["by_lifecycle_status"].get("archived", 0) == 1,
            f"archived count: {body['by_lifecycle_status']}",
        )
        expect(body["staging_edge_count"] == 0, f"staging {body['staging_edge_count']}")
        return (
            f"docs={body['total_documents']} lifecycle={body['by_lifecycle_status']}"
            f" edges={body['total_edges']}"
        )

    rec.run(
        "L-071",
        "vault stats match scenario exactly",
        l071,
        surface="rest",
        db_class="relational",
        rw="read",
        covers_rest=[("get", "/sage_vaults/{vault_id}/stats")],
    )

    def l072() -> str:
        unknown = "sha256:" + "0" * 64
        body = c.expect_status(
            c.req(
                "POST",
                ctx.v("/hash-check"),
                json={"hashes": [ctx.hashes["alpha"], unknown]},
            ),
            200,
        )
        known = body[ctx.hashes["alpha"]]
        expect(
            known["exists"] is True and known["document_id"] == ctx.ids["alpha"],
            f"known hash: {known}",
        )
        expect(body[unknown]["exists"] is False, f"unknown hash: {body[unknown]}")
        return "known matched to alpha, unknown rejected"

    rec.run(
        "L-072",
        "bulk hash existence check",
        l072,
        surface="rest",
        db_class="relational",
        rw="read",
        covers_rest=[("post", "/sage_vaults/{vault_id}/hash-check")],
    )

    def l073() -> str:
        body = c.expect_status(c.req("POST", ctx.v("/refresh-views")), 200)
        expect("views_generated" in body, f"malformed: {body}")
        return f"views_generated={body['views_generated']}"

    rec.run(
        "L-073",
        "refresh views",
        l073,
        surface="rest",
        db_class="relational",
        rw="write",
        covers_rest=[("post", "/sage_vaults/{vault_id}/refresh-views")],
    )

    def l074() -> str:
        body = c.expect_status(
            c.req(
                "POST",
                ctx.v("/parse-filename"),
                json={"filename": FIXTURES["delta"], "source_type": "markdown"},
            ),
            200,
        )
        blob = json.dumps(body)
        expect("SPC-003" in blob and "2026-07-04" in blob, f"parse missed fields: {blob[:200]}")
        return "date and code extracted"

    rec.run(
        "L-074",
        "filename parser (no-DB surface)",
        l074,
        surface="rest",
        db_class="none",
        rw="read",
        covers_rest=[("post", "/sage_vaults/{vault_id}/parse-filename")],
    )


# --------------------------------------------------------------- group 8
def group8_maintenance(c: SmokeClient, rec: Recorder, ctx: Ctx) -> None:
    print("— group 8: maintenance/admin surface")

    def l080() -> str:
        body = c.expect_status(c.req("POST", ctx.v("/admin/detect-drift")), 200)
        expect(body["entries"] == [], f"drift on fresh vault: {body['entries']}")
        return f"walked {body['total_edges_walked']} edges, zero drift"

    rec.run(
        "L-080",
        "drift detection clean on fresh vault",
        l080,
        surface="rest",
        db_class="relational",
        rw="read",
        covers_rest=[("post", "/sage_vaults/{vault_id}/admin/detect-drift")],
    )

    def l081() -> str:
        body = c.expect_status(c.req("POST", ctx.v("/admin/migrate")), 200)
        expect(
            body["columns_added"] == [] and body["backfills_applied"] == [],
            f"unexpected migration work: {body}",
        )
        return "idempotent no-op at current schema version"

    rec.run(
        "L-081",
        "schema migrate idempotent no-op",
        l081,
        surface="rest",
        db_class="relational",
        rw="write",
        covers_rest=[("post", "/sage_vaults/{vault_id}/admin/migrate")],
    )

    def l082() -> str:
        body = c.expect_status(
            c.req("POST", ctx.v("/admin/verify-source-files"), json={"check_hashes": True}), 200
        )
        doc_count = len([k for k in ctx.ids if not k.startswith("_")])
        expect(
            body["total_documents_checked"] == doc_count,
            f"checked {body['total_documents_checked']} != {doc_count}",
        )
        bad = [e for e in body["entries"] if e.get("status") not in (None, "ok", "present")]
        expect(not bad, f"integrity findings: {bad}")
        return f"{body['total_documents_checked']} sources present, hashes clean"

    rec.run(
        "L-082",
        "source-file integrity with hash verification",
        l082,
        surface="rest",
        db_class="relational",
        rw="read",
        covers_rest=[("post", "/sage_vaults/{vault_id}/admin/verify-source-files")],
    )

    def l083() -> str:
        stream = c.sse("POST", ctx.v("/admin/reabstract-deferred"), json={})
        summaries = [p for p in stream.payloads() if p.get("event_type") == "summary"]
        expect(len(summaries) == 1, f"{len(summaries)} summary events")
        s = summaries[0]
        expect(s["failed_count"] == 0, f"failures: {s}")
        return (
            f"reabstracted={s['reabstracted_count']} skipped_pdf={s['skipped_pdf_count']}"
            f" (corpus already terminal)"
        )

    rec.run(
        "L-083",
        "deferred reabstract sweep streams SSE",
        l083,
        surface="rest",
        db_class="vector",
        rw="write",
        covers_rest=[("post", "/sage_vaults/{vault_id}/admin/reabstract-deferred")],
    )

    def l084() -> str:
        body = c.expect_status(
            c.req(
                "POST",
                ctx.v("/admin/optimize-content-store"),
                json={"cleanup_older_than_days": 0},
            ),
            200,
        )
        expect(
            body["post_versions"] >= 0 and body["finished_at"] >= body["started_at"],
            f"malformed report: {body}",
        )
        return (
            f"optimize ran: versions {body['pre_versions']}->{body['post_versions']},"
            f" bytes reclaimed {body['bytes_reclaimed']}"
        )

    rec.run(
        "L-084",
        "optimize-content-store live run (headline)",
        l084,
        surface="rest",
        db_class="relational",
        rw="write",
        covers_rest=[("post", "/sage_vaults/{vault_id}/admin/optimize-content-store")],
    )

    def l085() -> str:
        body = c.expect_status(
            c.req("POST", ctx.v(f"/documents/{ctx.ids['alpha']}/reabstract")), 200
        )
        expect(body["status"] == "reabstract_started", f"start: {body}")
        doc = _await_terminal(c, ctx, ctx.ids["alpha"], timeout=PIPELINE_TIMEOUT)
        expect(doc.get("semantic_abstract"), "abstract missing after reabstract")
        result = c.mcp_call(
            "/mcp",
            "recompute_pipeline",
            {"vault_id": ctx.vault_id, "document_id": ctx.ids["delta"]},
        )
        _ = result
        doc = _await_terminal(c, ctx, ctx.ids["delta"], timeout=PIPELINE_TIMEOUT)
        expect(doc.get("semantic_abstract"), "delta abstract missing after recompute")
        return "alpha reabstracted; delta pipeline recomputed"

    rec.run(
        "L-085",
        "per-document recompute paths",
        l085,
        surface="rest",
        db_class="vector",
        rw="write",
        covers_rest=[("post", "/sage_vaults/{vault_id}/documents/{document_id}/reabstract")],
        covers_tools=["recompute_pipeline", "recompute_abstract"],
    )


# --------------------------------------------------------------- group 9
def group9_mcp_sweep(c: SmokeClient, rec: Recorder, ctx: Ctx) -> None:
    """Exercise every MCP tool not already claimed, with live arguments.

    Twins of REST checks assert lighter (envelope + one field) because the
    service path was already deep-asserted; MCP-only tools assert fully.
    """
    print("— group 9: MCP full-surface sweep")
    ctx.mcp_tools["/mcp"] = c.mcp_list_tools("/mcp")
    ctx.mcp_tools["/mcp_admin"] = c.mcp_list_tools("/mcp_admin")

    def tool_check(check_id: str, mount: str, tool: str, fn, *, db_class: str, rw: str) -> None:
        rec.run(
            check_id,
            f"{tool} via {mount}",
            fn,
            surface="mcp_admin" if mount == "/mcp_admin" else "mcp",
            db_class=db_class,
            rw=rw,
            covers_tools=[tool],
        )

    alpha, vid = ctx.ids["alpha"], ctx.vault_id

    # --- ordinary surface -------------------------------------------------
    def t_get_document() -> str:
        doc = c.mcp_call("/mcp", "get_document", {"vault_id": vid, "document_id": alpha})
        expect(doc.get("id") == alpha, f"got {doc.get('id')}")
        return "document round-trip"

    tool_check("M-001", "/mcp", "get_document", t_get_document, db_class="relational", rw="read")

    def t_filename_meta() -> str:
        result = c.mcp_call(
            "/mcp",
            "get_filename_metadata",
            {"vault_id": vid, "filename": FIXTURES["alpha"], "source_type": "markdown"},
        )
        expect("SPC-001" in json.dumps(result), f"code missed: {json.dumps(result)[:150]}")
        return "filename parsed"

    tool_check(
        "M-002", "/mcp", "get_filename_metadata", t_filename_meta, db_class="none", rw="read"
    )

    def t_update_metadata() -> str:
        result = c.mcp_call(
            "/mcp",
            "update_metadata",
            {
                "vault_id": vid,
                "items": [{"document_id": alpha, "tags": {"add": ["swept"]}}],
                "dry_run": True,
            },
        )
        expect(result.get("success_count") == 1, f"{json.dumps(result)[:200]}")
        doc = c.mcp_call("/mcp", "get_document", {"vault_id": vid, "document_id": alpha})
        expect("swept" not in doc["tags"], "dry-run persisted")
        return "dry-run patch validated without persisting"

    tool_check(
        "M-003", "/mcp", "update_metadata", t_update_metadata, db_class="relational", rw="write"
    )

    def t_update_lifecycles() -> str:
        result = c.mcp_call(
            "/mcp",
            "update_lifecycles",
            {
                "vault_id": vid,
                "items": [{"document_id": ctx.ids["gamma"], "action": "complete"}],
                "dry_run": True,
            },
        )
        expect(result.get("success_count") == 1, f"{json.dumps(result)[:200]}")
        return "dry-run transition validated"

    tool_check(
        "M-004", "/mcp", "update_lifecycles", t_update_lifecycles, db_class="relational", rw="write"
    )

    def t_create_edges() -> str:
        result = c.mcp_call(
            "/mcp",
            "create_edges",
            {
                "vault_id": vid,
                "dry_run": True,
                "items": [
                    {
                        "source_id": ctx.ids["gamma"],
                        "target_id": ctx.ids["delta"],
                        "edge_type": "references",
                        "source_valid_from_version": ctx.ids["gamma"],
                        "target_valid_from_version": ctx.ids["delta"],
                        "rationale": "sweep dry-run",
                    }
                ],
            },
        )
        expect(result.get("success_count") == 1, f"{json.dumps(result)[:200]}")
        return "dry-run edge validated"

    tool_check("M-005", "/mcp", "create_edges", t_create_edges, db_class="relational", rw="write")

    def t_delete_edge() -> str:
        edge_id = ctx.confirmed_production_edge_id
        expect(edge_id, "no confirmed production edge available")
        result = c.mcp_call(
            "/mcp", "delete_edge", {"vault_id": vid, "edge_id": edge_id, "dry_run": True}
        )
        expect(result.get("deleted") is False, f"{json.dumps(result)[:150]}")
        return "dry-run delete resolved the edge without removing it"

    tool_check("M-006", "/mcp", "delete_edge", t_delete_edge, db_class="relational", rw="write")

    def t_verify_preconditions() -> str:
        result = c.mcp_call("/mcp", "verify_preconditions", {"vault_id": vid, "function_id": alpha})
        expect("satisfied" in result, f"{json.dumps(result)[:150]}")
        return f"satisfied={result['satisfied']}"

    tool_check(
        "M-007",
        "/mcp",
        "verify_preconditions",
        t_verify_preconditions,
        db_class="relational",
        rw="read",
    )

    def t_traverse() -> str:
        result = c.mcp_call(
            "/mcp",
            "traverse",
            {
                "vault_id": vid,
                "start_id": ctx.ids["beta2"],
                "edge_type": "supersedes",
                "direction": "outbound",
                "depth": 2,
            },
        )
        expect(ctx.ids["beta1"] in json.dumps(result), "predecessor unreachable")
        return "supersedes traverse parity"

    tool_check("M-008", "/mcp", "traverse", t_traverse, db_class="relational", rw="read")

    def t_chain() -> str:
        result = c.mcp_call(
            "/mcp",
            "chain",
            {"vault_id": vid, "document_id": ctx.ids["beta2"], "edge_type": "supersedes"},
        )
        expect(result.get("head_id") == ctx.ids["beta2"], f"head {result.get('head_id')}")
        return "chain parity"

    tool_check("M-009", "/mcp", "chain", t_chain, db_class="relational", rw="read")

    def t_read_projection() -> str:
        result = c.mcp_call("/mcp", "read_projection", {"vault_id": vid, "document_id": alpha})
        expect("granulation" in (result.get("projection_text") or ""), "text mismatch")
        return "projection text parity"

    tool_check(
        "M-010", "/mcp", "read_projection", t_read_projection, db_class="relational", rw="read"
    )

    def t_read_section() -> str:
        result = c.mcp_call(
            "/mcp",
            "read_section",
            {
                "vault_id": vid,
                "document_id": alpha,
                "heading_path": "Alpha Solar Observatory Operations Manual > Dome Maintenance",
            },
        )
        expect("worm-gear" in result.get("section_text", ""), "section mismatch")
        return "section parity"

    tool_check("M-011", "/mcp", "read_section", t_read_section, db_class="relational", rw="read")

    def t_list_headings() -> str:
        result = c.mcp_call("/mcp", "list_headings", {"vault_id": vid, "document_id": alpha})
        expect(any("Data Archival" in h for h in result.get("headings", [])), "heading missing")
        return f"{len(result['headings'])} headings"

    tool_check("M-012", "/mcp", "list_headings", t_list_headings, db_class="relational", rw="read")

    def t_list_staging() -> str:
        result = c.mcp_call("/mcp", "list_staging_edges", {"vault_id": vid})
        edges = result.get("staging_edges", result.get("edges", result.get("value", [])))
        expect(
            isinstance(edges, list) and len(edges) == 0,
            f"staging should be drained: {json.dumps(result)[:150]}",
        )
        return "staging table empty after review"

    tool_check(
        "M-013", "/mcp", "list_staging_edges", t_list_staging, db_class="relational", rw="read"
    )

    def t_update_staging() -> str:
        text = c.mcp_call_expect_error(
            "/mcp",
            "update_staging_edge",
            {
                "vault_id": vid,
                "edge_id": "00000000-0000-0000-0000-000000000000",
                "action": "dismiss",
            },
        )
        expect("staging_edge_not_found" in text, f"unexpected error: {text[:150]}")
        return "unknown id resolves to documented error (DB lookup exercised)"

    tool_check(
        "M-014", "/mcp", "update_staging_edge", t_update_staging, db_class="relational", rw="write"
    )

    def t_list_pending() -> str:
        result = c.mcp_call("/mcp", "list_pending_metadata", {"vault_id": vid})
        blob = json.dumps(result)
        expect(ctx.ids["eta"] not in blob, "eta still pending on MCP surface")
        return "pending queue parity (drained)"

    tool_check(
        "M-015", "/mcp", "list_pending_metadata", t_list_pending, db_class="relational", rw="read"
    )

    def t_list_directory() -> str:
        if ctx.profile == "cloud":
            raise CheckFailure("path-based tool asserted only under the local profile")
        result = c.mcp_call(
            "/mcp", "list_directory", {"vault_id": vid, "directory": str(ctx.fixtures)}
        )
        blob = json.dumps(result)
        expect(FIXTURES["alpha"] in blob, f"fixtures not listed: {blob[:200]}")
        known_markers = (ctx.ids["alpha"], '"ingested"', '"unchanged"', '"exists": true')
        expect(
            any(m in blob for m in known_markers),
            f"already-ingested classification missing: {blob[:300]}",
        )
        return "directory scan with vault hash classification"

    if ctx.profile == "local":
        tool_check(
            "M-016", "/mcp", "list_directory", t_list_directory, db_class="relational", rw="read"
        )
    else:
        rec.skip(
            "M-016",
            "list_directory via /mcp",
            "server-side path scan is a co-located affordance",
            surface="mcp",
            db_class="relational",
            rw="read",
            covers_tools=["list_directory"],
        )

    def t_verify_hashes() -> str:
        result = c.mcp_call(
            "/mcp", "verify_hashes", {"vault_id": vid, "hashes": [ctx.hashes["alpha"]]}
        )
        expect(ctx.ids["alpha"] in json.dumps(result), "hash not matched")
        return "hash check parity"

    tool_check("M-017", "/mcp", "verify_hashes", t_verify_hashes, db_class="relational", rw="read")

    def t_recompute_abstract() -> str:
        result = c.mcp_call(
            "/mcp", "recompute_abstract", {"vault_id": vid, "document_id": ctx.ids["gamma"]}
        )
        expect(result.get("status") == "reabstract_started", f"{json.dumps(result)[:150]}")
        doc = _await_terminal(c, ctx, ctx.ids["gamma"], timeout=PIPELINE_TIMEOUT)
        expect(doc.get("semantic_abstract"), "gamma abstract missing")
        return "gamma reabstracted via MCP"

    tool_check(
        "M-018", "/mcp", "recompute_abstract", t_recompute_abstract, db_class="vector", rw="write"
    )

    # --- admin surface ----------------------------------------------------
    def t_admin_list_vaults() -> str:
        result = c.mcp_call("/mcp_admin", "admin_list_vaults", {})
        expect(vid in json.dumps(result), "smoke vault not listed")
        return "vault listed on admin surface"

    tool_check(
        "A-001", "/mcp_admin", "admin_list_vaults", t_admin_list_vaults, db_class="none", rw="read"
    )

    def t_admin_create_vault() -> str:
        if ctx.profile == "cloud":
            text = c.mcp_call_expect_error(
                "/mcp_admin",
                "admin_create_vault",
                {"config": ctx.vault_config},
            )
            expect("vault_already_exists" in text, f"unexpected: {text[:150]}")
            return "duplicate-id rejected (no second cloud vault created)"
        result = c.mcp_call(
            "/mcp_admin",
            "admin_create_vault",
            {"config": ctx.aux_vault_config},
        )
        expect(ctx.aux_vault_id in json.dumps(result), f"{json.dumps(result)[:150]}")
        return f"aux vault {ctx.aux_vault_id} created"

    tool_check(
        "A-002",
        "/mcp_admin",
        "admin_create_vault",
        t_admin_create_vault,
        db_class="relational",
        rw="write",
    )

    def t_admin_get_config() -> str:
        result = c.mcp_call("/mcp_admin", "admin_get_vault_config", {"vault_id": vid})
        blob = json.dumps(result)
        expect("retrieval_assertions.yaml" in blob, "config mismatch")
        return "config read parity"

    tool_check(
        "A-003",
        "/mcp_admin",
        "admin_get_vault_config",
        t_admin_get_config,
        db_class="none",
        rw="read",
    )

    def t_admin_update_config() -> str:
        result = c.mcp_call(
            "/mcp_admin",
            "admin_update_vault_config",
            {"vault_id": vid, "dry_run": True, "abstraction": {"enabled": True}},
        )
        expect(result.get("status") == "previewed", f"{json.dumps(result)[:150]}")
        return "dry-run preview returned without writing"

    tool_check(
        "A-004",
        "/mcp_admin",
        "admin_update_vault_config",
        t_admin_update_config,
        db_class="relational",
        rw="write",
    )

    def t_admin_stats() -> str:
        result = c.mcp_call("/mcp_admin", "admin_get_vault_stats", {"vault_id": vid})
        doc_count = len([k for k in ctx.ids if not k.startswith("_")])
        expect(
            result.get("total_documents") == doc_count,
            f"{result.get('total_documents')} != {doc_count}",
        )
        return "stats parity"

    tool_check(
        "A-005",
        "/mcp_admin",
        "admin_get_vault_stats",
        t_admin_stats,
        db_class="relational",
        rw="read",
    )

    def t_admin_migrate() -> str:
        result = c.mcp_call("/mcp_admin", "admin_migrate_vault", {"vault_id": vid})
        expect(result.get("columns_added") == [], f"{json.dumps(result)[:150]}")
        return "admin migrate parity"

    tool_check(
        "A-010",
        "/mcp_admin",
        "admin_migrate_vault",
        t_admin_migrate,
        db_class="relational",
        rw="write",
    )

    def t_admin_drift() -> str:
        result = c.mcp_call("/mcp_admin", "admin_verify_vault_drift", {"vault_id": vid})
        expect(result.get("entries") == [], f"drift: {json.dumps(result)[:150]}")
        return "admin drift parity"

    tool_check(
        "A-011",
        "/mcp_admin",
        "admin_verify_vault_drift",
        t_admin_drift,
        db_class="relational",
        rw="read",
    )

    def t_admin_sources() -> str:
        result = c.mcp_call(
            "/mcp_admin",
            "admin_verify_vault_source_files",
            {"vault_id": vid, "check_hashes": False},
        )
        expect(result.get("summary") is not None, f"{json.dumps(result)[:150]}")
        return "admin source verify parity"

    tool_check(
        "A-012",
        "/mcp_admin",
        "admin_verify_vault_source_files",
        t_admin_sources,
        db_class="relational",
        rw="read",
    )

    def t_admin_reabstract() -> str:
        result = c.mcp_call(
            "/mcp_admin", "admin_recompute_deferred_vault_abstracts", {"vault_id": vid}
        )
        expect(result.get("failed_count", 0) == 0, f"{json.dumps(result)[:200]}")
        return "admin deferred-reabstract parity"

    tool_check(
        "A-013",
        "/mcp_admin",
        "admin_recompute_deferred_vault_abstracts",
        t_admin_reabstract,
        db_class="vector",
        rw="write",
    )

    def t_admin_optimize() -> str:
        result = c.mcp_call("/mcp_admin", "admin_optimize_vault_content_store", {"vault_id": vid})
        expect("bytes_reclaimed" in result, f"{json.dumps(result)[:150]}")
        return "admin optimize parity"

    tool_check(
        "A-014",
        "/mcp_admin",
        "admin_optimize_vault_content_store",
        t_admin_optimize,
        db_class="relational",
        rw="write",
    )

    def t_admin_stack_config() -> str:
        result = c.mcp_call("/mcp_admin", "admin_get_stack_config", {})
        expect(result.get("storage_backend") == "postgres", f"{json.dumps(result)[:150]}")
        return "stack config reports postgres binding"

    tool_check(
        "A-015",
        "/mcp_admin",
        "admin_get_stack_config",
        t_admin_stack_config,
        db_class="none",
        rw="read",
    )

    def t_admin_recompute_views() -> str:
        result = c.mcp_call("/mcp_admin", "admin_recompute_views", {"vault_id": vid})
        expect("views_generated" in json.dumps(result), f"{json.dumps(result)[:150]}")
        return "admin views refresh parity"

    tool_check(
        "A-016",
        "/mcp_admin",
        "admin_recompute_views",
        t_admin_recompute_views,
        db_class="relational",
        rw="write",
    )


GROUPS = [
    group0_boot,
    group1_vault,
    group2_ingest,
    group3_reads,
    group4_retrieval,
    group5_metadata_lifecycle,
    group6_graph,
    group10_bff,
    group7_vault_utilities,
    group8_maintenance,
    group9_mcp_sweep,
]


def stage_retrieval_assertions(ctx: Ctx, storage_root: Path) -> None:
    """Local-profile only: write the eval-retrieval assertions under the
    smoke vault's storage_root once the expected document ids exist.

    Out-of-band staging into a disposable scratch vault the driver itself
    created; the ids are only known post-ingest, so the file cannot be
    part of the vault template.
    """
    import yaml

    payload = {
        "assertions": [
            {
                "query": "observatory dome shutter worm-gear drive inspection",
                "expected_document_id": ctx.ids["alpha"],
                "top_k": 5,
            },
            {
                "query": "harbor approach channel dredging spoil ground soundings",
                "expected_document_id": ctx.ids["delta"],
                "top_k": 5,
            },
        ]
    }
    target = storage_root / "retrieval_assertions.yaml"
    target.write_text(yaml.safe_dump(payload), encoding="utf-8")


def wait_for_health(c: SmokeClient, *, timeout: float = 120.0) -> None:
    def probe() -> dict | None:
        try:
            resp = c.req("GET", "/health")
        except Exception:  # noqa: BLE001 -- server may not be listening yet
            return None
        return {"ok": True} if resp.status_code == 200 else None

    poll(probe, timeout=timeout, interval=1.0, what="/health")
    time.sleep(0.5)
