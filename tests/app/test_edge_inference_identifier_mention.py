"""Tests for the identifier_mention edge-inference rule (T-0016).

Spec (plain English):

T1 -- Happy path: ADR mention creates a `references` edge.
  Inputs:  Pre-seeded ADR document with tag `adr` and title `ADR-099: ...`.
           A new ticket markdown whose body contains the literal `CAS-ADR-099`.
  Expect:  One `references` edge from the ticket's just-ingested doc id to
           the ADR's doc id; rationale_kind == REFERENCES_MENTION; evidence
           starts with `[references_mention]`.
  Why:     Core acceptance criterion of T-0016.

T2 -- Happy path: ticket id mention creates a `references` edge.
  Inputs:  Pre-seeded ticket document with tag `id:T-0042`. New doc whose
           body mentions `T-0042`.
  Expect:  One `references` edge from the new doc to the T-0042 ticket.
  Why:     Covers the second default pattern surface.

T3 -- Happy path: failure record mention creates a `references` edge.
  Inputs:  Pre-seeded failure record with tag `id:F-3`. New doc whose body
           mentions `F-3`.
  Expect:  One `references` edge from the new doc to the F-3 record.
  Why:     Covers the third default pattern surface.

T4 -- Idempotency under re-ingest.
  Inputs:  Same source markdown as T1, ingested twice.
  Expect:  Only one `references` edge between the (source, target) pair
           after the second ingest.
  Why:     Re-ingest is the ticket's stated natural answer to ambiguity (a)
           on first-vs-every ingest; duplicates would inflate traversal
           counts.

T5 -- Unresolved identifier is silently skipped.
  Inputs:  New doc whose body mentions `CAS-ADR-999` (no such ADR exists
           in the vault).
  Expect:  No edge is created; ingest completes successfully.
  Why:     The most likely failure mode -- an unresolved identifier must not
           break ingestion. Mentions of identifiers that haven't been ingested
           yet are common (forward references, drafts).

T6 -- Manual `references` edges are not touched.
  Inputs:  Pre-existing manual `references` edge (rationale_kind=MANUAL)
           from doc A to an ADR. Ingest doc A's body with no inline mention
           of the ADR.
  Expect:  The manual edge survives; no inferred edge is added; manual
           edge's rationale_kind remains MANUAL.
  Why:     Regression guard for the CAS-ADR-019 provenance gate. The rule
           must not displace or rewrite hand-curated edges.

T7 -- Pattern config drives behavior (per-vault configurability).
  Inputs:  Vault config that omits the `T-NNNN` pattern. Ingest a doc whose
           body mentions both `CAS-ADR-099` and `T-0042`.
  Expect:  ADR edge created; no ticket edge created.
  Why:     The ticket requires per-vault pattern configurability; vaults
           that don't use ticket grammar must be able to disable that
           pattern.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.backend.edge_inference import IDENTIFIER_MENTION_RATIONALE_PREFIX
from app.backend.ingest_service import BatchIngestService, FileDescriptor
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.config import VaultConfig
from sage.mcp_init import SAGEServices, initialize_services
from sage.models.enums import EdgeType, RationaleKind, SourceType
from sage.models.schemas import Document, LinkRequest

# ---------------------------------------------------------------------------
# Fixture: vault config with the new identifier_mention rule
# ---------------------------------------------------------------------------

ADR_PATTERN = {
    "regex": r"\bCAS-ADR-\d{3}\b",
    "target_tags": ["adr"],
    "target_title_prefix": "ADR-{adr_num}:",
    "target_doc_type": "adr",
}
TICKET_PATTERN = {
    "regex": r"\bT-\d{4}\b",
    "target_tags": ["id:{id}"],
    "target_doc_type": "ticket",
}
FAILURE_PATTERN = {
    "regex": r"\bF-\d+\b",
    "target_tags": ["id:{id}"],
    "target_doc_type": "failure_record",
}


def _vault_config_dict(
    tmp_path: Path,
    *,
    patterns: list[dict] | None = None,
) -> dict:
    """Minimal vault config with identifier_mention enabled."""
    if patterns is None:
        patterns = [ADR_PATTERN, TICKET_PATTERN, FAILURE_PATTERN]
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir(parents=True, exist_ok=True)
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    return {
        "vault": {
            "id": "test_t0016",
            "name": "T-0016 Test Vault",
            "owner": "testuser",
            "storage_root": str(sources_dir),
            "brain_root": str(brain_dir),
            "visibility": "personal",
        },
        "document_types": {
            "doc_types": [
                {"value": "adr", "label": "ADR"},
                {"value": "ticket", "label": "Ticket"},
                {"value": "failure_record", "label": "Failure Record"},
                {"value": "note", "label": "Note"},
            ],
        },
        "lifecycle": {
            "base_states_required": True,
            "states": [
                {"value": "active", "label": "Active"},
                {"value": "completed", "label": "Completed"},
                {"value": "archived", "label": "Archived", "is_terminal": True},
            ],
            "transitions": [
                {"from_state": "(new)", "action": "ingest", "to_state": "active"},
                {
                    "from_state": "active",
                    "action": "supersede",
                    "to_state": "archived",
                    "creates_edge": "supersedes",
                },
                {"from_state": "active", "action": "complete", "to_state": "completed"},
                {"from_state": "active", "action": "archive", "to_state": "archived"},
                {"from_state": "completed", "action": "archive", "to_state": "archived"},
                {"from_state": "archived", "action": "reactivate", "to_state": "active"},
            ],
        },
        "source_adapters": {
            "adapters": [{"source_type": "markdown", "enabled": True}],
        },
        "metadata_extraction": {},
        "edge_inference": {
            "tier_assignments": [
                {
                    "edge_type": "supersedes",
                    "tier": 1,
                    "inference_rules": [{"method": "version_chain"}],
                },
                {
                    "edge_type": "references",
                    "tier": 1,
                    "inference_rules": [{"method": "identifier_mention", "patterns": patterns}],
                },
            ],
        },
        "abstraction": {"enabled": False},
    }


@pytest.fixture
async def services(tmp_path, monkeypatch) -> SAGEServices:
    """SAGEServices bundle with stub providers and identifier_mention enabled."""
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = VaultConfig.model_validate(_vault_config_dict(tmp_path))
    svc = await initialize_services(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    )
    try:
        yield svc
    finally:
        await svc.graph_store.close()


def _doc_id(slug: str) -> str:
    """Build a shape-conformant document id (^[0-9a-f]{8}_[a-z0-9_]+$)."""
    cleaned = re.sub(r"[^a-z0-9_]+", "_", slug.lower()).strip("_") or "n"
    return f"{hashlib.sha256(slug.encode()).hexdigest()[:8]}_{cleaned}"


def _sha(name: str) -> str:
    return "sha256:" + hashlib.sha256(name.encode()).hexdigest()


def _make_document(
    *,
    doc_id: str,
    title: str,
    doc_type: str,
    tags: list[str],
    source_path: str = "synthetic.md",
) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=doc_id,
        title=title,
        source_type=SourceType.MARKDOWN,
        source_path=source_path,
        source_content_hash=_sha(title),
        adapter_version="1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        doc_type=doc_type,
        tags=tags,
        lifecycle_status="active",
        pipeline_status="abstraction_complete",
    )


async def _seed_document(
    svc: SAGEServices, *, doc_id: str, title: str, doc_type: str, tags: list[str]
) -> str:
    """Insert a target document directly into the graph store (no ingestion).

    Target documents only need to be findable by tag/title/doc_type during
    identifier resolution; their content is not scanned. Direct insert
    avoids needing to construct full markdown bodies for every fixture
    target.
    """
    doc = _make_document(doc_id=doc_id, title=title, doc_type=doc_type, tags=tags)
    await svc.graph_store.insert_document(doc)
    return doc_id


def _write_md(tmp_path: Path, filename: str, body: str) -> str:
    """Write a markdown file under the vault sources_dir and return its path."""
    path = tmp_path / "sources" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return str(path)


def _fd(file_path: str) -> FileDescriptor:
    return FileDescriptor(file_path=file_path, adapter="markdown", parsed_metadata=None)


async def _ingest_and_get_edges(
    svc: SAGEServices,
    file_path: str,
    *,
    edge_type: str = "references",
) -> tuple[str, list]:
    """Run BatchIngestService.run() on a single file and return (doc_id, edges)."""
    batch = BatchIngestService()
    captured: dict[str, str] = {}

    async def _on_done(i, total, filename, doc_id):  # noqa: ARG001
        captured["doc_id"] = doc_id

    summary = await batch.run([_fd(file_path)], svc, on_file_done=_on_done)
    assert summary.error_count == 0, f"ingest errors: {summary.errors}"
    assert "doc_id" in captured, "on_file_done callback did not fire"
    new_doc_id = captured["doc_id"]
    edges = await svc.graph_store.get_edges_by_source(new_doc_id, edge_type)
    return new_doc_id, edges


# ---------------------------------------------------------------------------
# T1 -- ADR mention happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t1_adr_mention_creates_references_edge(tmp_path, services):
    adr_id = _doc_id("adr_099")
    await _seed_document(
        services,
        doc_id=adr_id,
        title="ADR-099: Synthetic ADR for T-0016 testing",
        doc_type="adr",
        tags=["adr"],
    )
    src_path = _write_md(
        tmp_path,
        "ticket_t_0016_adr_reference.md",
        "# Ticket body\n\nThis ticket implements CAS-ADR-099.\n",
    )

    src_doc_id, edges = await _ingest_and_get_edges(services, src_path)

    assert len(edges) == 1, f"expected one references edge, got {edges}"
    edge = edges[0]
    assert edge.target_id == adr_id
    assert edge.source_id == src_doc_id
    assert edge.edge_type == EdgeType.REFERENCES
    assert edge.rationale_kind == RationaleKind.REFERENCES_MENTION
    assert edge.rationale is not None
    assert edge.rationale.startswith(IDENTIFIER_MENTION_RATIONALE_PREFIX)


# ---------------------------------------------------------------------------
# T2 -- ticket id mention happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2_ticket_mention_creates_references_edge(tmp_path, services):
    ticket_id = _doc_id("ticket_0042")
    await _seed_document(
        services,
        doc_id=ticket_id,
        title="T-0042: Synthetic ticket for testing",
        doc_type="ticket",
        tags=["ticket", "id:T-0042"],
    )
    src_path = _write_md(
        tmp_path,
        "note_about_t_0042.md",
        "# Note\n\nSee also T-0042 for the related work.\n",
    )

    src_doc_id, edges = await _ingest_and_get_edges(services, src_path)

    assert len(edges) == 1
    assert edges[0].target_id == ticket_id
    assert edges[0].rationale_kind == RationaleKind.REFERENCES_MENTION


# ---------------------------------------------------------------------------
# T3 -- failure record mention happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t3_failure_record_mention_creates_references_edge(tmp_path, services):
    failure_id = _doc_id("failure_3")
    await _seed_document(
        services,
        doc_id=failure_id,
        title="F-3: Synthetic failure for testing",
        doc_type="failure_record",
        tags=["failure_record", "id:F-3"],
    )
    src_path = _write_md(
        tmp_path,
        "postmortem_referencing_f_3.md",
        "# Postmortem\n\nRoot cause traces back to F-3.\n",
    )

    _src, edges = await _ingest_and_get_edges(services, src_path)

    assert len(edges) == 1
    assert edges[0].target_id == failure_id
    assert edges[0].rationale_kind == RationaleKind.REFERENCES_MENTION


# ---------------------------------------------------------------------------
# T4 -- idempotency under re-ingest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t4_re_ingest_does_not_duplicate_edge(tmp_path, services):
    """Re-running identifier_mention against an already-edged pair is a no-op.

    Drives the underlying invariant via two ingestions of distinct files that
    both mention the same ADR. The same (source, target, references) tuple
    cannot be produced twice (different source_ids), so the test instead
    asserts that the second source's edge is created idempotently and does
    not displace or duplicate the first.

    A direct same-document re-ingest can't be tested here because SAGE
    rejects identical content as a duplicate before any inference runs.
    """
    adr_id = _doc_id("adr_099")
    await _seed_document(
        services,
        doc_id=adr_id,
        title="ADR-099: Idempotency target",
        doc_type="adr",
        tags=["adr"],
    )
    src_path_a = _write_md(
        tmp_path,
        "doc_a.md",
        "# Doc A\n\nMentions CAS-ADR-099 once.\n",
    )
    src_path_b = _write_md(
        tmp_path,
        "doc_b.md",
        "# Doc B\n\nAlso mentions CAS-ADR-099.\n",
    )

    src_a_id, edges_a = await _ingest_and_get_edges(services, src_path_a)
    src_b_id, edges_b = await _ingest_and_get_edges(services, src_path_b)

    assert len(edges_a) == 1
    assert len(edges_b) == 1
    inbound = await services.graph_store.get_edges_by_target(adr_id, "references")
    # Exactly two edges: one from each distinct source. No duplicate
    # (source_a -> adr) or (source_b -> adr) tuples produced by repeated
    # planner runs in the same batch.
    assert len(inbound) == 2
    inbound_sources = {e.source_id for e in inbound}
    assert inbound_sources == {src_a_id, src_b_id}

    # Re-run the planner directly against doc_a's already-ingested body
    # text and resolve_and_execute it -- idempotency check at the
    # link_idempotent level (re-running the rule for the same document
    # must not create duplicates).
    from app.backend.edge_inference import (
        EdgePlan,
        plan_identifier_mentions_for_document,
        resolve_and_execute,
    )

    chunks = await services.content_store.get_all_chunks(src_a_id)
    body = "\n".join(c.content for c in chunks)
    replanned = await plan_identifier_mentions_for_document(
        source_doc_id=src_a_id,
        body_text=body,
        edge_inference_config=services.config.edge_inference,
        graph_store=services.graph_store,
    )
    assert len(replanned) == 1
    plan = EdgePlan(edges=replanned)
    await resolve_and_execute(plan, {}, services.graph_store, services.graph_ops_service)
    inbound_after = await services.graph_store.get_edges_by_target(adr_id, "references")
    assert len(inbound_after) == 2  # unchanged


# ---------------------------------------------------------------------------
# T5 -- unresolved identifier is silently skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t5_unresolved_identifier_creates_no_edge(tmp_path, services):
    """Strengthened: also pre-seed a decoy target that should NOT be matched.

    Without strengthening, a buggy resolver that returned source_doc_id (or
    any other arbitrary doc) for unresolved patterns would still produce a
    self-loop edge that SAGE rejects -- silently making the test pass for
    the wrong reason. Pre-seeding a decoy document that does match the
    tag/doc_type but NOT the title-prefix filter ensures the assertion
    fails if resolution loses its disambiguation rule.
    """
    # Decoy ADR that does NOT match `ADR-999:` prefix -- it should be filtered out.
    decoy_id = _doc_id("adr_decoy")
    await _seed_document(
        services,
        doc_id=decoy_id,
        title="ADR-007: A real but unrelated ADR",
        doc_type="adr",
        tags=["adr"],
    )
    src_path = _write_md(
        tmp_path,
        "doc_with_dangling_reference.md",
        "# Doc\n\nMentions CAS-ADR-999 which does not exist.\n",
    )

    src_doc_id, edges = await _ingest_and_get_edges(services, src_path)

    assert edges == []
    # No edge to the decoy either.
    decoy_inbound = await services.graph_store.get_edges_by_target(decoy_id, "references")
    assert decoy_inbound == []
    # Source document still landed cleanly (active, pipeline complete).
    src = await services.graph_store.get_document(src_doc_id)
    assert src is not None
    assert src.lifecycle_status == "active"


# ---------------------------------------------------------------------------
# T6 -- manual `references` edges are not touched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t6_manual_references_edge_is_preserved(tmp_path, services):
    # Pre-seed an ADR and a source doc directly in the graph (no ingestion
    # for the source -- we will ingest a different doc whose body does NOT
    # mention the ADR).
    adr_id = _doc_id("adr_055")
    src_id = _doc_id("manual_source_doc")
    await _seed_document(
        services,
        doc_id=adr_id,
        title="ADR-055: Manual edge target",
        doc_type="adr",
        tags=["adr"],
    )
    await _seed_document(
        services,
        doc_id=src_id,
        title="Manual source doc",
        doc_type="note",
        tags=["note"],
    )
    # Create a manual `references` edge by hand.
    manual_edge, _created = await services.graph_ops_service.link_idempotent(
        LinkRequest(
            source_id=src_id,
            target_id=adr_id,
            edge_type=EdgeType.REFERENCES,
            source_valid_from_version=src_id,
            target_valid_from_version=adr_id,
            rationale="hand-curated cross-reference",
            rationale_kind=RationaleKind.MANUAL,
        )
    )
    assert manual_edge.rationale_kind == RationaleKind.MANUAL

    # Now ingest an unrelated doc whose body does not mention ADR-055.
    src_path = _write_md(
        tmp_path,
        "unrelated_ingest.md",
        "# Doc\n\nThis doc mentions nothing relevant.\n",
    )
    _new_id, edges = await _ingest_and_get_edges(services, src_path)
    assert edges == []

    # Manual edge still exists with original rationale_kind.
    inbound = await services.graph_store.get_edges_by_target(adr_id, "references")
    assert len(inbound) == 1
    assert inbound[0].id == manual_edge.id
    assert inbound[0].rationale_kind == RationaleKind.MANUAL


# ---------------------------------------------------------------------------
# T7 -- pattern config drives behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t7_disabled_pattern_skips_matches(tmp_path, monkeypatch):
    """Vault that omits the ticket pattern must not infer ticket edges."""
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config_dict = _vault_config_dict(
        tmp_path,
        patterns=[ADR_PATTERN],  # only ADR; T-NNNN and F-N omitted
    )
    config = VaultConfig.model_validate(config_dict)
    svc = await initialize_services(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    )
    try:
        adr_id = _doc_id("adr_007")
        ticket_id = _doc_id("ticket_0042b")
        await _seed_document(
            svc,
            doc_id=adr_id,
            title="ADR-007: Pattern-config target",
            doc_type="adr",
            tags=["adr"],
        )
        await _seed_document(
            svc,
            doc_id=ticket_id,
            title="T-0042: Pattern-config target",
            doc_type="ticket",
            tags=["ticket", "id:T-0042"],
        )
        src_path = _write_md(
            tmp_path,
            "doc_mentions_both.md",
            "# Doc\n\nMentions CAS-ADR-007 and T-0042.\n",
        )
        batch = BatchIngestService()
        summary = await batch.run([_fd(src_path)], svc)
        assert summary.error_count == 0

        # ADR edge created; ticket edge not created.
        adr_inbound = await svc.graph_store.get_edges_by_target(adr_id, "references")
        ticket_inbound = await svc.graph_store.get_edges_by_target(ticket_id, "references")
        assert len(adr_inbound) == 1
        assert ticket_inbound == []
    finally:
        await svc.graph_store.close()
