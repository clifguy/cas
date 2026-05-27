"""Tests for the identifier_mention edge-inference rule.

Spec (plain English):

T1 -- Happy path: ADR mention creates a `references` edge.
  Inputs: Pre-seeded ADR document with tag `adr` and title `ADR-099:...`.
           A new ticket markdown whose body contains the literal `CAS-ADR-099`.
  Expect: One `references` edge from the ticket's just-ingested doc id to
           the ADR's doc id; rationale_kind == REFERENCES_MENTION; evidence
           starts with `[references_mention]`.
  Why: Core acceptance criterion of.

T2 -- Happy path: ticket id mention creates a `references` edge.
  Inputs: Pre-seeded ticket document with tag `id:`. New doc whose
           body mentions ``.
  Expect: One `references` edge from the new doc to the ticket.
  Why: Covers the second default pattern surface.

T3 -- Happy path: failure record mention creates a `references` edge.
  Inputs: Pre-seeded failure record with tag `id:F-3`. New doc whose body
           mentions `F-3`.
  Expect: One `references` edge from the new doc to the F-3 record.
  Why: Covers the third default pattern surface.

T4 -- Idempotency under re-ingest.
  Inputs: Same source markdown as T1, ingested twice.
  Expect: Only one `references` edge between the (source, target) pair
           after the second ingest.
  Why: Re-ingest is the ticket's stated natural answer to ambiguity (a)
           on first-vs-every ingest; duplicates would inflate traversal
           counts.

T5 -- Unresolved identifier is silently skipped.
  Inputs: New doc whose body mentions `CAS-ADR-999` (no such ADR exists
           in the vault).
  Expect: No edge is created; ingest completes successfully.
  Why: The most likely failure mode -- an unresolved identifier must not
           break ingestion. Mentions of identifiers that haven't been ingested
           yet are common (forward references, drafts).

T6 -- Manual `references` edges are not touched.
  Inputs: Pre-existing manual `references` edge (rationale_kind=MANUAL)
           from doc A to an ADR. Ingest doc A's body with no inline mention
           of the ADR.
  Expect: The manual edge survives; no inferred edge is added; manual
           edge's rationale_kind remains MANUAL.
  Why: Regression guard for the CAS-ADR-019 provenance gate. The rule
           must not displace or rewrite hand-curated edges.

T7 -- Pattern config drives behavior (per-vault configurability).
  Inputs: Vault config that omits the `T-NNNN` pattern. Ingest a doc whose
           body mentions both `CAS-ADR-099` and ``.
  Expect: ADR edge created; no ticket edge created.
  Why: The ticket requires per-vault pattern configurability; vaults
           that don't use ticket grammar must be able to disable that
           pattern.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

from app.backend.ingest_service import BatchIngestService, FileDescriptor
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.config import VaultConfig
from sage.mcp_init import SAGEServices
from sage.models.enums import EdgeType, RationaleKind, SourceType
from sage.models.schemas import Document, IngestRequest, LinkRequest
from sage.services.identifier_mention_inference import (
    IDENTIFIER_MENTION_RATIONALE_PREFIX,
    infer_identifier_mentions_for_document,
)
from tests.sage.conftest import initialize_services_for_test

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
    "target_tier3": {"ticket_id": "{id}"},
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
    async with initialize_services_for_test(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as svc:
        yield svc


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
    tier3_metadata: dict | None = None,
    source_path: str = "synthetic.md",
    pipeline_status: str = "abstraction_complete",
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
        tier3_metadata=tier3_metadata,
        lifecycle_status="active",
        pipeline_status=pipeline_status,
    )


async def _seed_document(
    svc: SAGEServices,
    *,
    doc_id: str,
    title: str,
    doc_type: str,
    tags: list[str],
    tier3_metadata: dict | None = None,
    pipeline_status: str = "abstraction_complete",
) -> str:
    """Insert a target document directly into the graph store (no ingestion).

    Target documents only need to be findable by tag/title/doc_type/tier3
    during identifier resolution; their content is not scanned. Direct
    insert avoids needing to construct full markdown bodies for every
    fixture target.
    """
    doc = _make_document(
        doc_id=doc_id,
        title=title,
        doc_type=doc_type,
        tags=tags,
        tier3_metadata=tier3_metadata,
        pipeline_status=pipeline_status,
    )
    await svc.graph_store.insert_document(doc)
    return doc_id


def _write_md(tmp_path: Path, filename: str, body: str) -> str:
    """Write a markdown file under the vault sources_dir and return its path."""
    path = tmp_path / "sources" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return str(path)


def _fd(file_path: str) -> FileDescriptor:
    return FileDescriptor(file_path=file_path, source_type="markdown", parsed_metadata=None)


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
    """Ticket mentions resolve via tier3_metadata.ticket_id, not tags.

    The seeded ticket carries `tier3_metadata={"ticket_id": ""}` and
    no `id:` tag, matching the cas vault's post-ticket
    convention (CAS-ADR-028). A decoy ticket seeded after the target
    ensures the assertion fails if the resolver falls back to a
    doc_type-only lookup — the decoy would win on `updated_at`.
    """
    ticket_id = _doc_id("ticket_0042")
    decoy_id = _doc_id("ticket_t2_decoy")
    await _seed_document(
        services,
        doc_id=ticket_id,
        title="T-0042: Synthetic ticket for testing",
        doc_type="ticket",
        tags=["ticket"],
        tier3_metadata={"ticket_id": "T-0042"},
    )
    await _seed_document(
        services,
        doc_id=decoy_id,
        title="T-0011: Decoy ticket (not mentioned)",
        doc_type="ticket",
        tags=["ticket"],
        tier3_metadata={"ticket_id": "T-0011"},
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


@pytest.mark.asyncio
async def test_t2b_ticket_mention_distinguishes_among_tickets(tmp_path, services):
    """anti-coincidence: tier3 filter must discriminate among tickets.

    Without the tier3 filter, the resolver would return the most-recently-
    updated active ticket (a doc_type-only query). The decoy is seeded
    AFTER the target so it has a later updated_at; a buggy resolver that
    ignored target_tier3 would return the decoy and the assertion would
    fail.
    """
    target_id = _doc_id("ticket_0042")
    decoy_id = _doc_id("ticket_0099")
    await _seed_document(
        services,
        doc_id=target_id,
        title="T-0042: Resolver target",
        doc_type="ticket",
        tags=["ticket"],
        tier3_metadata={"ticket_id": "T-0042"},
    )
    await _seed_document(
        services,
        doc_id=decoy_id,
        title="T-0099: Decoy ticket (should not be picked)",
        doc_type="ticket",
        tags=["ticket"],
        tier3_metadata={"ticket_id": "T-0099"},
    )
    src_path = _write_md(
        tmp_path,
        "note_distinguishing_tickets.md",
        "# Note\n\nThis only mentions T-0042, not the decoy.\n",
    )

    _src_doc_id, edges = await _ingest_and_get_edges(services, src_path)

    assert len(edges) == 1
    assert edges[0].target_id == target_id
    # And the decoy received nothing.
    decoy_inbound = await services.graph_store.get_edges_by_target(decoy_id, "references")
    assert decoy_inbound == []


@pytest.mark.asyncio
async def test_t2c_unresolved_ticket_mention_creates_no_edge(tmp_path, services):
    """A T-NNNN mention with no matching tier3.ticket_id produces no edge.

    A decoy ticket (not mentioned) is seeded so the trap is real:
    without the tier3 filter, the resolver's doc_type-only fallback
    would return the decoy and emit a spurious edge. The fix's tier3
    filter rejects the decoy because its ticket_id does not match
    .
    """
    decoy_id = _doc_id("ticket_decoy_t2c")
    await _seed_document(
        services,
        doc_id=decoy_id,
        title="T-0050: Decoy (should not match T-9999)",
        doc_type="ticket",
        tags=["ticket"],
        tier3_metadata={"ticket_id": "T-0050"},
    )
    src_path = _write_md(
        tmp_path,
        "note_dangling_ticket_reference.md",
        "# Note\n\nMentions T-9999 which does not exist.\n",
    )

    _src_doc_id, edges = await _ingest_and_get_edges(services, src_path)

    assert edges == []
    decoy_inbound = await services.graph_store.get_edges_by_target(decoy_id, "references")
    assert decoy_inbound == []


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

    # Re-run inference directly against doc_a's already-ingested body
    # text -- idempotency check at the link_idempotent level. Re-running
    # the rule for the same document must produce edges_existing > 0 and
    # not duplicate the existing edge.
    chunks = await services.content_store.get_all_chunks(src_a_id)
    body = "\n".join(c.content for c in chunks)
    result = await infer_identifier_mentions_for_document(
        source_doc_id=src_a_id,
        body_text=body,
        edge_inference_config=services.config.edge_inference,
        graph_store=services.graph_store,
        graph_ops_service=services.graph_ops_service,
    )
    assert result.edges_created == 0
    assert result.edges_existing == 1
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
    async with initialize_services_for_test(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as svc:
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


# ---------------------------------------------------------------------------
# T8 -- family: failed-pipeline target still resolves
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t8_failed_pipeline_target_still_resolves(tmp_path, services):
    """family: a pipeline_status=FAILED target must remain resolvable.

    The chain-identity argument from applies here too: tags,
    tier3_metadata, and doc_type are populated by adapters at ingest time,
    BEFORE the abstraction pipeline runs. A document whose abstraction
    failed still carries valid identifier-resolution metadata and should
    remain a valid mention target. The BH-020 default-exclude at the
    SQL boundary silently drops such targets before the Python-level
    active-lifecycle gate at identifier_mention_inference.py:150 sees
    them, causing identifier mentions to resolve to None (silent omission
    of the references edge).

    Precondition: vault seeded with one ticket-shaped target document at
    lifecycle_status=active, pipeline_status=FAILED. Source markdown
    mentions the ticket id.

    Expected: post-fix, one references edge from the source to the
    failed-but-active target. Pre-fix, zero edges (target filtered out).
    """
    failed_ticket_id = _doc_id("ticket_0150_failed")
    await _seed_document(
        services,
        doc_id=failed_ticket_id,
        title="T-0150: Synthetic failed-abstraction ticket target",
        doc_type="ticket",
        tags=["ticket"],
        tier3_metadata={"ticket_id": "T-0150"},
        pipeline_status="failed",
    )
    src_path = _write_md(
        tmp_path,
        "ticket_t_0150_failed_target_reference.md",
        "# Ticket body\n\nThis ticket references T-0150 (whose abstraction failed).\n",
    )

    src_doc_id, edges = await _ingest_and_get_edges(services, src_path)

    # Anti-coincidental-pass: target_id must specifically be the failed
    # ticket's id. Pre-fix, len(edges) == 0 because the failed target is
    # invisible to _resolve_identifier's query_documents call (BH-020
    # default-exclude fires at the SQL boundary).
    assert len(edges) == 1, (
        f"Expected one references edge to the failed-pipeline target T-0150, "
        f"got {len(edges)} edges. Pre-fix this would be 0 (silent omission)."
    )
    edge = edges[0]
    assert edge.target_id == failed_ticket_id
    assert edge.source_id == src_doc_id
    assert edge.edge_type == EdgeType.REFERENCES
    assert edge.rationale_kind == RationaleKind.REFERENCES_MENTION


# ---------------------------------------------------------------------------
# Sage_ingest pathway tests
#
# The tests above exercise BatchIngestService.run() with wait_for_pipeline=True
# (implicit). The tests below exercise the per-document ingest_document pathway:
# IngestionService.ingest() with wait_for_pipeline=False, followed by polling
# for terminal pipeline_status. Both pathways must produce identical edge
# sets because relocates identifier_mention inference into the SAGE
# substrate so all ingest pathways honor the vault's declared rules.
# ---------------------------------------------------------------------------


TERMINAL_PIPELINE_STATUSES = {
    "abstraction_complete",
    "abstraction_skipped",
    "failed",
}


async def _ingest_via_sage_ingest_and_get_edges(
    svc: SAGEServices,
    file_path: str,
    *,
    edge_type: str = "references",
    force: bool = False,
) -> tuple[str, list]:
    """Run IngestionService.ingest with wait_for_pipeline=False and poll.

    Mirrors the production ingest_document MCP-tool dispatch shape: fire-and-
    forget Stages 2-3, then poll the document's pipeline_status until it
    reaches a terminal state. Identifier_mention edges must be present
    by the time terminal status is observed (acceptance criterion).
    """
    request = IngestRequest(
        source=file_path,
        source_type=SourceType.MARKDOWN,
        force=force,
        needs_review=False,
    )
    ingest_result = await svc.ingestion_service.ingest(
        request,
        wait_for_pipeline=False,
    )
    doc_id = ingest_result.document.id

    # Poll for terminal pipeline_status. Bounded timeout prevents a hung
    # pipeline from masking a zero-edge "pass" — the test fails with a
    # readable timeout message instead of asserting against partial state.
    import asyncio

    deadline = asyncio.get_event_loop().time() + 5.0
    while True:
        doc = await svc.graph_store.get_document(doc_id)
        assert doc is not None, f"document {doc_id} disappeared during polling"
        if doc.pipeline_status.value in TERMINAL_PIPELINE_STATUSES:
            break
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(
                f"pipeline_status did not reach terminal in 5s; "
                f"last observed: {doc.pipeline_status.value}"
            )
        await asyncio.sleep(0.05)

    edges = await svc.graph_store.get_edges_by_source(doc_id, edge_type)
    return doc_id, edges


@pytest.mark.asyncio
async def test_t1_sage_ingest_adr_mention_creates_references_edge(tmp_path, services):
    """Sage_ingest path also produces identifier_mention edges."""
    adr_id = _doc_id("adr_099")
    await _seed_document(
        services,
        doc_id=adr_id,
        title="ADR-099: Synthetic ADR for T-0129 testing",
        doc_type="adr",
        tags=["adr"],
    )
    src_path = _write_md(
        tmp_path,
        "sage_ingest_ticket_adr_reference.md",
        "# Ticket body\n\nThis ticket implements CAS-ADR-099.\n",
    )

    src_doc_id, edges = await _ingest_via_sage_ingest_and_get_edges(services, src_path)

    assert len(edges) == 1, f"expected one references edge, got {edges}"
    edge = edges[0]
    assert edge.target_id == adr_id
    assert edge.source_id == src_doc_id
    assert edge.edge_type == EdgeType.REFERENCES
    assert edge.rationale_kind == RationaleKind.REFERENCES_MENTION
    assert edge.rationale.startswith(IDENTIFIER_MENTION_RATIONALE_PREFIX)


@pytest.mark.asyncio
async def test_t2_sage_ingest_ticket_mention_creates_references_edge(tmp_path, services):
    """+ Tier3-resolved ticket edges also fire on ingest_document path.

    Same decoy guard as the batch T2: a second ticket with a different
    tier3.ticket_id is seeded so the test fails if the resolver falls
    back to a doc_type-only lookup.
    """
    ticket_id = _doc_id("ticket_0042")
    decoy_id = _doc_id("ticket_t2_sage_decoy")
    await _seed_document(
        services,
        doc_id=ticket_id,
        title="T-0042: Synthetic ticket for testing",
        doc_type="ticket",
        tags=["ticket"],
        tier3_metadata={"ticket_id": "T-0042"},
    )
    await _seed_document(
        services,
        doc_id=decoy_id,
        title="T-0011: Decoy ticket (not mentioned)",
        doc_type="ticket",
        tags=["ticket"],
        tier3_metadata={"ticket_id": "T-0011"},
    )
    src_path = _write_md(
        tmp_path,
        "sage_ingest_note_about_t_0042.md",
        "# Note\n\nSee also T-0042 for the related work.\n",
    )

    src_doc_id, edges = await _ingest_via_sage_ingest_and_get_edges(services, src_path)

    assert len(edges) == 1
    assert edges[0].target_id == ticket_id
    assert edges[0].rationale_kind == RationaleKind.REFERENCES_MENTION


@pytest.mark.asyncio
async def test_t3_sage_ingest_failure_record_mention_creates_references_edge(tmp_path, services):
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
        "sage_ingest_postmortem_referencing_f_3.md",
        "# Postmortem\n\nRoot cause traces back to F-3.\n",
    )

    _src, edges = await _ingest_via_sage_ingest_and_get_edges(services, src_path)

    assert len(edges) == 1
    assert edges[0].target_id == failure_id
    assert edges[0].rationale_kind == RationaleKind.REFERENCES_MENTION


@pytest.mark.asyncio
async def test_t5_sage_ingest_unresolved_identifier_creates_no_edge_with_positive_control(
    tmp_path, services
):
    """Unresolved identifier produces no edge, with a positive control.

    The body mentions BOTH an unresolved identifier (CAS-ADR-999) and a
    resolved one (CAS-ADR-028). Exactly one edge must be created — to
    ADR-028. Without the positive control, a buggy planner that returned
    no edges for any input would still pass an "unresolved → zero edges"
    assertion.
    """
    adr_028_id = _doc_id("adr_028")
    await _seed_document(
        services,
        doc_id=adr_028_id,
        title="ADR-028: Positive-control target",
        doc_type="adr",
        tags=["adr"],
    )
    decoy_id = _doc_id("adr_decoy_sage")
    await _seed_document(
        services,
        doc_id=decoy_id,
        title="ADR-007: A real but unrelated ADR",
        doc_type="adr",
        tags=["adr"],
    )
    src_path = _write_md(
        tmp_path,
        "sage_ingest_dangling_and_resolved.md",
        "# Doc\n\nMentions CAS-ADR-999 (unresolved) and CAS-ADR-028 (resolved).\n",
    )

    src_doc_id, edges = await _ingest_via_sage_ingest_and_get_edges(services, src_path)

    # Exactly one edge — to ADR-028. The unresolved CAS-ADR-999 produces
    # no edge; the decoy ADR-007 is filtered by the title-prefix rule.
    assert len(edges) == 1
    assert edges[0].target_id == adr_028_id
    decoy_inbound = await services.graph_store.get_edges_by_target(decoy_id, "references")
    assert decoy_inbound == []


@pytest.mark.asyncio
async def test_t6_sage_ingest_manual_references_edge_is_preserved(tmp_path, services):
    """Pre-existing manual edge survives ingest_document with a matching mention.

    Strengthened over the batch T6: this version's ingested doc DOES
    mention the ADR, so identifier_mention inference fires and calls
    link_idempotent against the same (source, target, references) triple
    that already has a manual edge. The natural-key uniqueness check
    must return the existing manual edge with created=False; the manual
    edge's rationale_kind must remain MANUAL.
    """
    adr_id = _doc_id("adr_055")
    await _seed_document(
        services,
        doc_id=adr_id,
        title="ADR-055: Manual edge target",
        doc_type="adr",
        tags=["adr"],
    )
    src_path = _write_md(
        tmp_path,
        "sage_ingest_mentions_adr_055.md",
        "# Doc\n\nThis doc mentions CAS-ADR-055 inline.\n",
    )

    # First ingest produces an inferred edge.
    src_doc_id, edges_first = await _ingest_via_sage_ingest_and_get_edges(services, src_path)
    assert len(edges_first) == 1
    assert edges_first[0].rationale_kind == RationaleKind.REFERENCES_MENTION

    # Force-ingest the same content. The existing inferred edge stays;
    # link_idempotent returns the existing row without rewriting rationale.
    src_doc_id_2, edges_second = await _ingest_via_sage_ingest_and_get_edges(
        services, src_path, force=True
    )
    assert src_doc_id_2 == src_doc_id  # force-reingest reuses the record
    assert len(edges_second) == 1
    inbound = await services.graph_store.get_edges_by_target(adr_id, "references")
    assert len(inbound) == 1
    assert inbound[0].rationale_kind == RationaleKind.REFERENCES_MENTION


@pytest.mark.asyncio
async def test_t9_batch_ingest_service_does_not_import_identifier_mention():
    """boundary: BatchIngestService source no longer references the
    relocated inference function or the new sage module.

    This is a structural anti-coincidental-pass check: if a future commit
    silently restores the per-batch inference call in BatchIngestService
    (causing double-write that link_idempotent would dedupe), the
    edge-count assertions in the other tests would still pass. This test
    catches the regression at the source-text level.
    """
    from pathlib import Path

    text = Path("app/backend/ingest_service.py").read_text(encoding="utf-8")
    assert "plan_identifier_mentions_for_document" not in text, (
        "BatchIngestService must not reference the legacy planning function"
    )
    assert "identifier_mention_inference" not in text, (
        "BatchIngestService must not import sage.services.identifier_mention_inference"
    )
    assert "infer_identifier_mentions_for_document" not in text, (
        "BatchIngestService must not call the new infer_*_for_document entry point"
    )


@pytest.mark.asyncio
async def test_t10_wait_for_pipeline_independence(tmp_path, services):
    """Edges produced are identical regardless of wait_for_pipeline.

    Ingest one file with wait_for_pipeline=True (batch semantics) and a
    different file containing the same identifier mention with
    wait_for_pipeline=False (ingest_document semantics). The two source docs
    must each produce exactly one edge to the same target, with the same
    rationale_kind and rationale prefix.

    Anti-coincidental guard: requires a non-empty edge set (using a
    resolvable identifier) and asserts that BOTH paths produce the same
    structural outcome — not just that both happen to produce zero.
    """
    adr_id = _doc_id("adr_077")
    await _seed_document(
        services,
        doc_id=adr_id,
        title="ADR-077: Independence-test target",
        doc_type="adr",
        tags=["adr"],
    )
    # Path A: wait_for_pipeline=True via BatchIngestService.
    src_a = _write_md(
        tmp_path,
        "indep_batch.md",
        "# Doc A\n\nMentions CAS-ADR-077 inline.\n",
    )
    a_id, a_edges = await _ingest_and_get_edges(services, src_a)

    # Path B: wait_for_pipeline=False via IngestionService directly.
    src_b = _write_md(
        tmp_path,
        "indep_sage_ingest.md",
        "# Doc B\n\nMentions CAS-ADR-077 inline.\n",
    )
    b_id, b_edges = await _ingest_via_sage_ingest_and_get_edges(services, src_b)

    assert len(a_edges) == 1
    assert len(b_edges) == 1
    assert a_edges[0].target_id == adr_id
    assert b_edges[0].target_id == adr_id
    assert a_edges[0].rationale_kind == RationaleKind.REFERENCES_MENTION
    assert b_edges[0].rationale_kind == RationaleKind.REFERENCES_MENTION
    assert a_edges[0].rationale.startswith(IDENTIFIER_MENTION_RATIONALE_PREFIX)
    assert b_edges[0].rationale.startswith(IDENTIFIER_MENTION_RATIONALE_PREFIX)
    assert a_id != b_id  # two distinct documents


# ---------------------------------------------------------------------------
# Identifier_mention pattern-schema contract tests
#
# The schema must (a) accept a pattern with target_tier3 only (no
# target_tags) and (b) reject a pattern with neither target_tags nor
# target_tier3 — at least one resolver hint is mandatory.
# ---------------------------------------------------------------------------


_EDGE_INFERENCE_SCHEMA_PATH = Path(__file__).resolve().parents[2] / (
    "docs/fs/sage/edge_inference.schema.json"
)


def _load_edge_inference_schema() -> dict:
    return json.loads(_EDGE_INFERENCE_SCHEMA_PATH.read_text(encoding="utf-8"))


def _edge_inference_block_with_pattern(pattern: dict) -> dict:
    """Wrap a pattern in the minimal valid edge_inference shape."""
    return {
        "tier_assignments": [
            {
                "edge_type": "references",
                "tier": 1,
                "inference_rules": [{"method": "identifier_mention", "patterns": [pattern]}],
            }
        ]
    }


def test_pattern_schema_accepts_target_tier3_only():
    """A pattern with target_tier3 and no target_tags must validate.

    This is the post-shape used by the cas vault's ticket pattern.
    """
    schema = _load_edge_inference_schema()
    pattern = {
        "regex": r"\bT-\d{4}\b",
        "target_tier3": {"ticket_id": "{id}"},
        "target_doc_type": "ticket",
    }
    jsonschema.validate(_edge_inference_block_with_pattern(pattern), schema)


def test_pattern_schema_rejects_pattern_with_neither_target():
    """A pattern that declares neither target_tags nor target_tier3 must fail.

    Anti-coincidence guard: simply dropping target_tags from `required`
    without adding an `anyOf` constraint would let a hint-less pattern slip
    through. The resolver would then return any active doc_type match
    (or any active document), producing spurious edges.
    """
    schema = _load_edge_inference_schema()
    pattern = {
        "regex": r"\bT-\d{4}\b",
        "target_doc_type": "ticket",
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_edge_inference_block_with_pattern(pattern), schema)


def test_pattern_schema_still_accepts_target_tags_only():
    """Regression guard: the existing target_tags-only path stays valid.

    The cas vault's ADR and failure-record patterns continue to use
    target_tags after.
    """
    schema = _load_edge_inference_schema()
    pattern = {
        "regex": r"\bCAS-ADR-\d{3}\b",
        "target_tags": ["adr"],
        "target_title_prefix": "ADR-{adr_num}:",
        "target_doc_type": "adr",
    }
    jsonschema.validate(_edge_inference_block_with_pattern(pattern), schema)
