"""`ingest_document` and `bulk_ingest_document` dry-run.

Five categories:

A. State isolation — nothing is persisted, nothing is retained into the
   vault, and no pipeline stage runs.
B. Verdicts — the duplicate verdict, the content hash, the doc_type
   precedence chain, and the supersession verdict.
C. Requirement set — reported on a successful preview and carried in the
   detail of every `tier3_schema_violation`, from either service.
D. Honest limitation — a preview cannot consult the adapter's own tier3
   extraction, and says so.
E. Delivery gate — a preview reads a transfer token's bytes without
   spending it.

Plus the bulk arm and the lifecycle-vocabulary refusal the repointed
`update_lifecycles` docstring now promises.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from sage.api.errors import (
    DuplicateContentError,
    IdenticalContentSupersedeError,
    InvalidActionError,
    InvalidDocTypeError,
    SourceFileNotFoundError,
    SupersedeTargetNotActiveError,
    Tier3SchemaViolationError,
)
from sage.config import VaultConfig
from sage.models.enums import SourceType
from sage.models.schemas import (
    IngestPreview,
    IngestRequest,
    SetLifecycleRequest,
    Tier3Patch,
    UpdateMetadataRequest,
)
from sage.services.batch_ingest import (
    BatchIngestService,
    FileDescriptor,
    ParsedMetadataInput,
)
from sage.services.ingestion import IngestionService
from sage.services.lifecycle import LifecycleService
from sage.services.metadata import MetadataService
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from sage.vault_source_binding import hash_file
from tests.sage._dry_run_helpers import assert_state_unchanged, state_snapshot

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
#
# A vault declaring four doc_types that differ on exactly the axes the
# requirement set has to separate:
#
#   strict_record  schema WITH a ``required`` list, ``source_types``, and
#                  ``unique_keys`` -- everything populated.
#   loose_record   schema with properties but NO ``required``, and
#                  ``source_types: null``. This is the common shape in the
#                  live cas vault, and the one where reporting only the
#                  required list would name nothing at all.
#   bare_record    a declared schema with zero properties. Refuses every
#                  payload, and is separated from ``misc`` only by
#                  ``has_metadata_schema``.
#   misc           no schema whatsoever.


def _config_dict(tmp_vault_dir: Path) -> dict:
    return {
        "vault": {
            "id": "test_ingest_dry_run_vault",
            "name": "Ingest Dry Run Vault",
            "owner": "testuser",
            "storage_root": str(tmp_vault_dir / "sources"),
            "brain_root": str(tmp_vault_dir / "brain"),
            "visibility": "personal",
        },
        "document_types": {
            "doc_types": [
                {
                    "value": "strict_record",
                    "label": "Strict Record",
                    "source_types": ["markdown"],
                    "metadata_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "record_id": {"type": "string"},
                            "severity": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                            },
                            "note": {"type": ["string", "null"]},
                        },
                        "required": ["record_id", "severity"],
                    },
                    "unique_keys": ["record_id"],
                },
                {
                    "value": "loose_record",
                    "label": "Loose Record",
                    "source_types": None,
                    "metadata_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "ticket_id": {"type": "string"},
                            "close_pr": {"type": ["integer", "null"]},
                        },
                    },
                },
                {
                    "value": "bare_record",
                    "label": "Bare Record",
                    "source_types": ["markdown", "docx"],
                    "metadata_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {},
                    },
                },
                {"value": "misc", "label": "Miscellaneous"},
            ]
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
        # A filename pattern, so a doc_type inferred from the name is
        # distinguishable from the ``misc`` fallback. Consumed only where
        # ``needs_review`` is set, which is where filename inference runs.
        "metadata_extraction": {
            "filename_extraction": {
                "pattern": "{date}_{project}_{code}_{title}",
                "separator": "_",
                "project_identifier": "CAS",
                "segment_fields": {
                    "date": "doc_date",
                    "project": "project",
                    "code": "doc_code",
                    "title": "title",
                },
                "known_code_patterns": ["^LR$"],
                "code_to_doc_type": [{"code": "LR", "doc_type": "loose_record"}],
            }
        },
        "edge_inference": {},
    }


@pytest.fixture
def dry_config(tmp_vault_dir):
    return VaultConfig.model_validate(_config_dict(tmp_vault_dir))


@pytest.fixture
def dry_lifecycle_service(graph_store, lock_manager, dry_config):
    return LifecycleService(graph_store, lock_manager, dry_config)


@pytest.fixture
def dry_ingestion_service(
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    dry_config,
    dry_lifecycle_service,
):
    return IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=stub_abstraction_provider,
        config=dry_config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
        lifecycle_service=dry_lifecycle_service,
    )


@pytest.fixture
def sealed_config(tmp_vault_dir):
    """`dry_config` plus a terminal state no supersession lands in.

    Scoped to the one test that needs it rather than added to `dry_config`,
    whose action vocabulary another test pins as an exact list -- widening the
    shared vault to serve this one would put a state that test has no interest
    in inside its assertion.

    Why the state has to be both terminal and supersession-surviving: on the
    base table `archived` is at once the only terminal state and the only
    supersede landing, so `supersession_surviving_states()` and the complement
    of `terminal_states()` compute to the same set. A caller reading the second
    when the contract names the first is invisible to any assertion made
    against a base vault. `sealed` is in the first and not in the second.
    """
    raw = _config_dict(tmp_vault_dir)
    raw["lifecycle"]["states"].append({"value": "sealed", "label": "Sealed", "is_terminal": True})
    raw["lifecycle"]["transitions"].append(
        {"from_state": "active", "action": "seal", "to_state": "sealed"}
    )
    return VaultConfig.model_validate(raw)


@pytest.fixture
def sealed_ingestion_service(
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    sealed_config,
):
    return IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=stub_abstraction_provider,
        config=sealed_config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
        lifecycle_service=LifecycleService(graph_store, lock_manager, sealed_config),
    )


@pytest.fixture
def dry_metadata_service(graph_store, lock_manager, dry_config, stub_content_store):
    return MetadataService(graph_store, lock_manager, dry_config, stub_content_store)


def _write(tmp_vault_dir: Path, relative_path: str, body: str) -> Path:
    full = tmp_vault_dir / "sources" / relative_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body)
    return full


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


# ---------------------------------------------------------------------------
# (A) State isolation
# ---------------------------------------------------------------------------


async def test_dry_run_ingest_persists_nothing(
    tmp_vault_dir, dry_ingestion_service, graph_store, stub_content_store
):
    """A preview leaves documents, edges and chunk metadata byte-identical."""
    source = _write(tmp_vault_dir, "novel.md", "# Novel\n\nBody text.")
    before = await state_snapshot(graph_store, stub_content_store)

    preview = await dry_ingestion_service.ingest(
        IngestRequest(
            source=str(source),
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc"},
            dry_run=True,
        )
    )

    after = await state_snapshot(graph_store, stub_content_store)
    assert isinstance(preview, IngestPreview)
    assert preview.dry_run is True
    assert_state_unchanged(before, after)


async def test_dry_run_ingest_does_not_write_the_vault_tree(tmp_vault_dir, dry_ingestion_service):
    """Nothing is retained. The file listing under the vault's storage root
    is identical across the call, which the graph-state fingerprint cannot
    see: a retained copy is a file, not a row."""
    outside = tmp_vault_dir / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    source = outside / "external.md"
    source.write_text("# External\n\nDelivered from outside the vault.")

    storage_root = tmp_vault_dir / "sources"
    storage_root.mkdir(parents=True, exist_ok=True)
    before = _tree(storage_root)

    await dry_ingestion_service.ingest(
        IngestRequest(
            source=str(source),
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc"},
            dry_run=True,
        )
    )

    assert _tree(storage_root) == before, (
        "A dry-run ingest retained the source into the vault tree. "
        "Retention is the first irreversible act of an ingest; a preview "
        "must branch above it."
    )


async def test_dry_run_ingest_runs_no_pipeline(
    tmp_vault_dir, dry_ingestion_service, stub_content_store, stub_abstraction_provider
):
    """No projection, no indexing, no abstraction. The content store gains
    no chunks, which is the observable trace each of the three would leave."""
    source = _write(tmp_vault_dir, "pipeline.md", "# Pipeline\n\nBody.")
    chunk_keys_before = set(stub_content_store._store)

    await dry_ingestion_service.ingest(
        IngestRequest(
            source=str(source),
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc"},
            dry_run=True,
        )
    )

    assert set(stub_content_store._store) == chunk_keys_before


# ---------------------------------------------------------------------------
# (B) Verdicts
# ---------------------------------------------------------------------------


async def test_dry_run_reports_duplicate_verdict(tmp_vault_dir, dry_ingestion_service):
    """Bytes already held come back as ``would_create=false`` naming the
    document that holds them -- a verdict, not a ``duplicate_content`` raise."""
    source = _write(tmp_vault_dir, "dupe.md", "# Dupe\n\nSame bytes both times.")
    real = await dry_ingestion_service.ingest(
        IngestRequest(
            source=str(source), source_type=SourceType.MARKDOWN, metadata={"doc_type": "misc"}
        )
    )

    preview = await dry_ingestion_service.ingest(
        IngestRequest(
            source=str(source),
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc"},
            dry_run=True,
        )
    )

    assert preview.would_create is False
    assert preview.duplicate_of == real.document.id


async def test_dry_run_reports_no_duplicate_for_novel_bytes(tmp_vault_dir, dry_ingestion_service):
    """Negative control for the test above: a constant ``duplicate_of``
    fails one of the pair whichever constant it is."""
    _write(tmp_vault_dir, "held.md", "# Held\n\nAlready in the vault.")
    await dry_ingestion_service.ingest(
        IngestRequest(
            source="held.md", source_type=SourceType.MARKDOWN, metadata={"doc_type": "misc"}
        )
    )
    novel = _write(tmp_vault_dir, "novel2.md", "# Novel\n\nDifferent bytes entirely.")

    preview = await dry_ingestion_service.ingest(
        IngestRequest(
            source=str(novel),
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc"},
            dry_run=True,
        )
    )

    assert preview.would_create is True
    assert preview.duplicate_of is None


async def test_dry_run_reports_a_duplicate_for_the_same_bytes_at_a_different_path(
    tmp_vault_dir, dry_ingestion_service
):
    """The duplicate verdict is keyed on the bytes, not on where they sit.

    The novel/held pair above varies path and content together, so a lookup
    keyed on the source path would produce the same two verdicts and pass
    both. This is the case that separates them: identical bytes at a path
    the vault has never seen must still report the document that holds
    them. It is also the re-homing case a caller meets for real, when a
    file is moved and re-ingested from its new location.
    """
    original = _write(tmp_vault_dir, "rehome_before.md", "# Re-home\n\nIdentical bytes.")
    held = await dry_ingestion_service.ingest(
        IngestRequest(
            source=str(original),
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc"},
        )
    )
    moved = _write(tmp_vault_dir, "moved/rehome_after.md", "# Re-home\n\nIdentical bytes.")

    preview = await dry_ingestion_service.ingest(
        IngestRequest(
            source=str(moved),
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc"},
            dry_run=True,
        )
    )

    assert preview.duplicate_of == held.document.id
    assert preview.would_create is False


async def test_dry_run_duplicate_verdict_names_the_same_document_as_the_refusal(
    tmp_vault_dir, graph_store, dry_ingestion_service
):
    """Preview and real run name the same document, and it is the survivor.

    Two claims, and neither alone is enough. That ``duplicate_of`` is
    non-null holds under every rule, including the arbitrary one this
    replaces; that preview and refusal agree holds trivially if both are
    arbitrary in the same direction on the same call. Pinning the id to the
    surviving document is what makes the pair mean something.

    The retired sibling is seeded at the store level -- the service refuses a
    byte-identical supersession outright -- and its id is pinned to
    ``00000000_`` so it sorts below any service-generated id. Given a higher
    one, the survivor would win on the tie-break alone.
    """
    source = _write(tmp_vault_dir, "verdict_survivor.md", "# Verdict\n\nShared bytes.")
    request = IngestRequest(
        source=str(source), source_type=SourceType.MARKDOWN, metadata={"doc_type": "misc"}
    )
    surviving = (await dry_ingestion_service.ingest(request)).document

    retired = surviving.model_copy(
        update={
            "id": "00000000_retired_verdict_sibling",
            "source_path": "verdict_retired_sibling.md",
            "lifecycle_status": "archived",
        }
    )
    await graph_store.insert_document(retired)

    preview = await dry_ingestion_service.ingest(
        IngestRequest(
            source=str(source),
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc"},
            dry_run=True,
        )
    )
    with pytest.raises(DuplicateContentError) as exc_info:
        await dry_ingestion_service.ingest(request)

    assert preview.duplicate_of == surviving.id
    assert preview.duplicate_of == exc_info.value.detail["existing_document_id"]


async def test_dry_run_states_the_vaults_surviving_states_to_the_hash_lookup(
    tmp_vault_dir, graph_store, sealed_config, sealed_ingestion_service, monkeypatch
):
    """The preview asks under the vault's rule, not a preference of its own.

    An argument assertion, because the preview's verdict is identical under
    every rule on a vault holding one document per hash -- the shape of every
    other fixture in this section. What it guards is the preview drifting
    from the refusal: two call sites, one contract, and only the argument
    shows they still agree before a colliding vault makes them disagree
    visibly.

    The vault declares a `sealed` state for this test's benefit, and the two
    guards below say why. Without a state outside `{active, completed}` a
    preview hard-coding that literal passes; without a state that is terminal
    yet survives supersession, a preview complementing `terminal_states()`
    passes. Both are rules the port's contract forbids and neither is visible
    against a base lifecycle.
    """
    lifecycle = sealed_config.lifecycle
    expected = lifecycle.supersession_surviving_states()
    declared = frozenset(state.value for state in lifecycle.states)
    assert "sealed" in expected, "vault no longer widens the surviving set"
    assert expected != declared - lifecycle.terminal_states(), (
        "vault no longer separates the surviving set from the non-terminal set"
    )
    seen: list[frozenset[str]] = []
    real = graph_store.find_documents_by_hashes

    async def recording(hashes, *, prefer_lifecycle_statuses):
        seen.append(prefer_lifecycle_statuses)
        return await real(hashes, prefer_lifecycle_statuses=prefer_lifecycle_statuses)

    source = _write(tmp_vault_dir, "preview_preference.md", "# Preview\n\nPreference.")
    monkeypatch.setattr(graph_store, "find_documents_by_hashes", recording)

    await sealed_ingestion_service.ingest(
        IngestRequest(
            source=str(source),
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc"},
            dry_run=True,
        )
    )

    assert seen, "the preview consulted no hash lookup at all"
    assert all(pref == expected for pref in seen), seen


async def test_dry_run_reports_the_delivered_content_hash(tmp_vault_dir, dry_ingestion_service):
    """The hash is the delivered digest, computed here independently rather
    than asserted merely non-null. Reporting a projected digest instead
    would diverge under a binding that rewrites its copy at rest."""
    source = _write(tmp_vault_dir, "hashed.md", "# Hashed\n\nKnown bytes.")
    expected = hash_file(source)

    preview = await dry_ingestion_service.ingest(
        IngestRequest(
            source=str(source),
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc"},
            dry_run=True,
        )
    )

    assert preview.source_content_hash == expected


async def test_dry_run_resolves_a_vault_relative_source(tmp_vault_dir, dry_ingestion_service):
    """A relative source names a location under the vault's storage root,
    and the preview resolves and hashes it there.

    The preview carries its own three-branch source resolution, mirroring
    the real path's minus every retain. The absolute branch is exercised
    by the hash and verdict tests above; this is the relative one, where a
    divergence would report a missing source for a file the real run would
    have found.
    """
    source = _write(tmp_vault_dir, "nested/relative.md", "# Relative\n\nBody.")

    preview = await dry_ingestion_service.ingest(
        IngestRequest(
            source="nested/relative.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc"},
            dry_run=True,
        )
    )

    assert preview.source_content_hash == hash_file(source)
    assert preview.would_create is True


async def test_dry_run_refuses_a_missing_source(dry_ingestion_service):
    """A preview locates the source exactly as a real run does, and names
    the caller's own spelling back when it cannot."""
    with pytest.raises(SourceFileNotFoundError) as excinfo:
        await dry_ingestion_service.ingest(
            IngestRequest(
                source="/nowhere/absent.md",
                source_type=SourceType.MARKDOWN,
                dry_run=True,
            )
        )
    assert excinfo.value.detail["source"] == "/nowhere/absent.md"


@pytest.mark.parametrize(
    ("caller_doc_type", "expected"),
    [
        ("loose_record", "loose_record"),
        (None, "misc"),
    ],
)
async def test_dry_run_resolves_doc_type_by_caller_then_fallback(
    tmp_vault_dir, dry_ingestion_service, caller_doc_type, expected
):
    """Caller metadata wins; absent everything, the resolution falls through
    to ``misc`` -- the same chain the real path applies."""
    source = _write(tmp_vault_dir, f"dt-{expected}.md", "# DT\n\nBody.")
    metadata = {"doc_type": caller_doc_type} if caller_doc_type else None

    preview = await dry_ingestion_service.ingest(
        IngestRequest(
            source=str(source),
            source_type=SourceType.MARKDOWN,
            metadata=metadata,
            dry_run=True,
        )
    )

    assert preview.resolved_doc_type == expected


async def test_dry_run_inherits_doc_type_from_the_predecessor(tmp_vault_dir, dry_ingestion_service):
    """With no caller value, a supersession inherits the predecessor's
    doc_type -- the rung of the chain between caller and ``misc``."""
    _write(tmp_vault_dir, "pred.md", "# Predecessor\n\nOriginal body.")
    pred = await dry_ingestion_service.ingest(
        IngestRequest(
            source="pred.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "loose_record"},
            tier3_metadata={"ticket_id": "fixture-alpha"},
        )
    )
    successor = _write(tmp_vault_dir, "succ.md", "# Successor\n\nRevised body.")

    preview = await dry_ingestion_service.ingest(
        IngestRequest(
            source=str(successor),
            source_type=SourceType.MARKDOWN,
            predecessor_id=pred.document.id,
            dry_run=True,
        )
    )

    assert preview.resolved_doc_type == "loose_record"
    assert preview.would_supersede is True
    assert preview.predecessor_id == pred.document.id


async def test_dry_run_reports_no_supersession_without_a_predecessor(
    tmp_vault_dir, dry_ingestion_service
):
    """Negative control for ``would_supersede``."""
    source = _write(tmp_vault_dir, "standalone.md", "# Standalone\n\nBody.")
    preview = await dry_ingestion_service.ingest(
        IngestRequest(
            source=str(source),
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc"},
            dry_run=True,
        )
    )
    assert preview.would_supersede is False
    assert preview.predecessor_id is None


async def test_dry_run_refuses_a_predecessor_the_table_will_not_supersede(
    tmp_vault_dir, dry_ingestion_service, dry_lifecycle_service
):
    """The supersede gate raises under dry run exactly as under a real run,
    carrying the states the vault's table does admit."""
    _write(tmp_vault_dir, "archived.md", "# Archived\n\nBody.")
    pred = await dry_ingestion_service.ingest(
        IngestRequest(
            source="archived.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc"},
        )
    )
    await dry_lifecycle_service._set_lifecycle(
        pred.document.id, SetLifecycleRequest(action="archive")
    )
    successor = _write(tmp_vault_dir, "succ2.md", "# Successor\n\nBody.")

    with pytest.raises(SupersedeTargetNotActiveError) as excinfo:
        await dry_ingestion_service.ingest(
            IngestRequest(
                source=str(successor),
                source_type=SourceType.MARKDOWN,
                predecessor_id=pred.document.id,
                dry_run=True,
            )
        )
    assert excinfo.value.detail["allowed_states"] == ["active"]


async def test_dry_run_refuses_an_identical_content_supersede(tmp_vault_dir, dry_ingestion_service):
    """A no-op edit is refused under dry run with its own code, not reported
    as an ordinary duplicate."""
    source = _write(tmp_vault_dir, "same.md", "# Same\n\nUnchanged body.")
    pred = await dry_ingestion_service.ingest(
        IngestRequest(
            source=str(source), source_type=SourceType.MARKDOWN, metadata={"doc_type": "misc"}
        )
    )

    with pytest.raises(IdenticalContentSupersedeError):
        await dry_ingestion_service.ingest(
            IngestRequest(
                source=str(source),
                source_type=SourceType.MARKDOWN,
                predecessor_id=pred.document.id,
                dry_run=True,
            )
        )


async def test_dry_run_with_force_would_create_over_a_duplicate(
    tmp_vault_dir, dry_ingestion_service
):
    """``force`` is what turns a hash collision into a re-ingest rather than
    a refusal, and the verdict follows it."""
    source = _write(tmp_vault_dir, "forced.md", "# Forced\n\nBody.")
    await dry_ingestion_service.ingest(
        IngestRequest(
            source=str(source), source_type=SourceType.MARKDOWN, metadata={"doc_type": "misc"}
        )
    )

    preview = await dry_ingestion_service.ingest(
        IngestRequest(
            source=str(source),
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc"},
            force=True,
            dry_run=True,
        )
    )

    assert preview.duplicate_of is not None
    assert preview.would_create is True


# ---------------------------------------------------------------------------
# (C) Requirement set
# ---------------------------------------------------------------------------


async def test_successful_preview_carries_the_requirement_set(tmp_vault_dir, dry_ingestion_service):
    """Reported on a success, not only on a refusal: a caller learns the
    shape without having to provoke an error to be told it."""
    source = _write(tmp_vault_dir, "ok.md", "# OK\n\nBody.")

    preview = await dry_ingestion_service.ingest(
        IngestRequest(
            source=str(source),
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "strict_record"},
            tier3_metadata={"record_id": "R-1", "severity": "high"},
            dry_run=True,
        )
    )

    req = preview.requirements
    assert req.doc_type == "strict_record"
    assert req.is_declared is True
    assert req.required_tier3_fields == ["record_id", "severity"]
    assert req.declared_tier3_fields == ["note", "record_id", "severity"]
    assert req.unique_tier3_fields == ["record_id"]
    assert req.permitted_source_types == ["markdown"]


async def test_refusal_names_required_and_declared_fields(tmp_vault_dir, dry_ingestion_service):
    """A doc_type declaring ``required`` reports it, alongside the full
    declared property set."""
    source = _write(tmp_vault_dir, "missing.md", "# Missing\n\nBody.")

    with pytest.raises(Tier3SchemaViolationError) as excinfo:
        await dry_ingestion_service.ingest(
            IngestRequest(
                source=str(source),
                source_type=SourceType.MARKDOWN,
                metadata={"doc_type": "strict_record"},
                tier3_metadata={"note": "no id, no severity"},
                dry_run=True,
            )
        )

    req = excinfo.value.detail["requirements"]
    assert req["required_tier3_fields"] == ["record_id", "severity"]
    assert req["declared_tier3_fields"] == ["note", "record_id", "severity"]


async def test_refusal_names_declared_fields_when_required_is_empty(
    tmp_vault_dir, dry_ingestion_service
):
    """The common shape: no ``required`` list at all, the refusal coming
    from ``additionalProperties: false``.

    Reporting only the required set would name NOTHING here, which is the
    failure the requirement set exists to prevent. The declared names are
    what make this refusal actionable.
    """
    source = _write(tmp_vault_dir, "extra.md", "# Extra\n\nBody.")

    with pytest.raises(Tier3SchemaViolationError) as excinfo:
        await dry_ingestion_service.ingest(
            IngestRequest(
                source=str(source),
                source_type=SourceType.MARKDOWN,
                metadata={"doc_type": "loose_record"},
                tier3_metadata={"not_a_declared_key": "x"},
                dry_run=True,
            )
        )

    req = excinfo.value.detail["requirements"]
    assert req["required_tier3_fields"] == []
    assert req["declared_tier3_fields"] == ["close_pr", "ticket_id"]


async def test_refusal_distinguishes_unconstrained_from_empty_source_types(
    tmp_vault_dir, dry_ingestion_service
):
    """``null`` means unconstrained; it is not the same as an empty list,
    and collapsing the two would tell a caller nothing is permitted."""
    source = _write(tmp_vault_dir, "st.md", "# ST\n\nBody.")

    with pytest.raises(Tier3SchemaViolationError) as excinfo:
        await dry_ingestion_service.ingest(
            IngestRequest(
                source=str(source),
                source_type=SourceType.MARKDOWN,
                metadata={"doc_type": "loose_record"},
                tier3_metadata={"bogus": 1},
                dry_run=True,
            )
        )
    assert excinfo.value.detail["requirements"]["permitted_source_types"] is None

    with pytest.raises(Tier3SchemaViolationError) as excinfo:
        await dry_ingestion_service.ingest(
            IngestRequest(
                source=str(source),
                source_type=SourceType.MARKDOWN,
                metadata={"doc_type": "bare_record"},
                tier3_metadata={"bogus": 1},
                dry_run=True,
            )
        )
    assert excinfo.value.detail["requirements"]["permitted_source_types"] == [
        "docx",
        "markdown",
    ]


async def test_refusal_distinguishes_no_schema_from_an_empty_schema(
    tmp_vault_dir, dry_ingestion_service
):
    """``bare_record`` declares a schema with no properties; ``misc``
    declares no schema. Both refuse a payload and both report an empty
    ``declared_tier3_fields``, so only ``has_metadata_schema`` separates
    them -- which is why it is read off the declaration rather than
    derived from the field list."""
    source = _write(tmp_vault_dir, "schemas.md", "# Schemas\n\nBody.")

    with pytest.raises(Tier3SchemaViolationError) as excinfo:
        await dry_ingestion_service.ingest(
            IngestRequest(
                source=str(source),
                source_type=SourceType.MARKDOWN,
                metadata={"doc_type": "bare_record"},
                tier3_metadata={"anything": 1},
                dry_run=True,
            )
        )
    bare = excinfo.value.detail["requirements"]
    assert bare["has_metadata_schema"] is True
    assert bare["declared_tier3_fields"] == []

    with pytest.raises(Tier3SchemaViolationError) as excinfo:
        await dry_ingestion_service.ingest(
            IngestRequest(
                source=str(source),
                source_type=SourceType.MARKDOWN,
                metadata={"doc_type": "misc"},
                tier3_metadata={"anything": 1},
                dry_run=True,
            )
        )
    plain = excinfo.value.detail["requirements"]
    assert plain["has_metadata_schema"] is False
    assert plain["declared_tier3_fields"] == []


async def test_refusal_marks_an_undeclared_doc_type_on_a_legacy_row(
    tmp_vault_dir, dry_ingestion_service, dry_metadata_service, graph_store
):
    """``is_declared=false`` reports a doc_type the vault does not declare.

    Unreachable through ingest now that the vocabulary gate refuses such a
    value outright, and that is the point of testing it here instead: the
    population it describes is the rows that landed *before* the gate, and
    a patch against one of them is the only door still open onto them. The
    row is aged into that state directly, because no supported call can
    produce it any more.
    """
    _write(tmp_vault_dir, "legacy.md", "# Legacy\n\nBody.")
    doc = await dry_ingestion_service.ingest(
        IngestRequest(
            source="legacy.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc"},
        )
    )
    await graph_store.update_document(doc.document.id, {"doc_type": "retired_record"})

    with pytest.raises(Tier3SchemaViolationError) as excinfo:
        await dry_metadata_service._update_metadata(
            doc.document.id,
            UpdateMetadataRequest(tier3_metadata=Tier3Patch(set={"anything": 1})),
            modified_by="tester",
        )

    req = excinfo.value.detail["requirements"]
    assert req["is_declared"] is False
    assert req["doc_type"] == "retired_record"


async def test_refusal_is_enriched_on_a_real_run_too(tmp_vault_dir, dry_ingestion_service):
    """The requirement set is a property of the refusal, not of dry run.
    A real-run failure leaves the caller with the same question."""
    source = _write(tmp_vault_dir, "realrun.md", "# Real\n\nBody.")

    with pytest.raises(Tier3SchemaViolationError) as excinfo:
        await dry_ingestion_service.ingest(
            IngestRequest(
                source=str(source),
                source_type=SourceType.MARKDOWN,
                metadata={"doc_type": "strict_record"},
                tier3_metadata={"note": "incomplete"},
            )
        )

    req = excinfo.value.detail["requirements"]
    assert req["required_tier3_fields"] == ["record_id", "severity"]


async def test_update_metadata_refusal_carries_the_same_requirement_set(
    tmp_vault_dir, dry_ingestion_service, dry_metadata_service
):
    """One error code means one thing. A detail that varied by which
    service raised it would not be one contract."""
    _write(tmp_vault_dir, "patchme.md", "# Patch\n\nBody.")
    doc = await dry_ingestion_service.ingest(
        IngestRequest(
            source="patchme.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "strict_record"},
            tier3_metadata={"record_id": "R-9", "severity": "low"},
        )
    )

    with pytest.raises(Tier3SchemaViolationError) as excinfo:
        await dry_metadata_service._update_metadata(
            doc.document.id,
            UpdateMetadataRequest(tier3_metadata=Tier3Patch(set={"severity": "nonsense"})),
            modified_by="tester",
        )

    req = excinfo.value.detail["requirements"]
    assert req["doc_type"] == "strict_record"
    assert req["required_tier3_fields"] == ["record_id", "severity"]
    assert req["declared_tier3_fields"] == ["note", "record_id", "severity"]
    assert req["unique_tier3_fields"] == ["record_id"]
    assert req["permitted_source_types"] == ["markdown"]


# ---------------------------------------------------------------------------
# (D) Honest limitation
# ---------------------------------------------------------------------------


async def test_preview_marks_tier3_unvalidated_when_the_caller_supplied_none(
    tmp_vault_dir, dry_ingestion_service
):
    """A preview does not project, so it cannot reach the adapter's own
    tier3 extraction. Reporting ``tier3_validated=true`` here would promise
    a verdict on a payload the preview never saw."""
    source = _write(tmp_vault_dir, "notier3.md", "# No Tier3\n\nBody.")

    without = await dry_ingestion_service.ingest(
        IngestRequest(
            source=str(source),
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "loose_record"},
            dry_run=True,
        )
    )
    assert without.tier3_validated is False

    with_payload = await dry_ingestion_service.ingest(
        IngestRequest(
            source=str(source),
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "loose_record"},
            tier3_metadata={"ticket_id": "fixture-beta"},
            dry_run=True,
        )
    )
    assert with_payload.tier3_validated is True


# ---------------------------------------------------------------------------
# (E) Lifecycle vocabulary
# ---------------------------------------------------------------------------


async def test_invalid_action_names_the_whole_known_vocabulary(
    tmp_vault_dir, dry_ingestion_service, dry_lifecycle_service
):
    """The refusal a caller who does not know the vocabulary actually hits.

    An action absent from every transition row never reaches the
    transition validator, so ``invalid_lifecycle_transition`` and its
    ``valid_actions`` are unreachable here: enriching that error instead
    would leave this caller with nothing.
    """
    _write(tmp_vault_dir, "vocab.md", "# Vocab\n\nBody.")
    doc = await dry_ingestion_service.ingest(
        IngestRequest(
            source="vocab.md", source_type=SourceType.MARKDOWN, metadata={"doc_type": "misc"}
        )
    )

    with pytest.raises(InvalidActionError) as excinfo:
        await dry_lifecycle_service._set_lifecycle(
            doc.document.id,
            SetLifecycleRequest(action="deprecate", dry_run=True),
        )

    # ``ingest`` is absent deliberately: the ``(new)`` row is the pipeline's
    # own landing transition, not something a caller invokes, and the
    # transition table keeps it out of the action roster. Reporting it would
    # hand a caller an action that always refuses.
    assert excinfo.value.detail["known_actions"] == [
        "archive",
        "complete",
        "reactivate",
        "supersede",
    ]


# ---------------------------------------------------------------------------
# (F) The bulk arm
# ---------------------------------------------------------------------------


def _batch_services(
    dry_config, dry_ingestion_service, dry_lifecycle_service, graph_store, lock_manager
):
    """A vault-services stand-in carrying only what a dry batch may touch.

    ``graph_ops_service`` is deliberately absent. Phase 3 is the only caller
    of it, and a dry run must not reach Phase 3, so a regression that did
    would fail here on the missing attribute rather than quietly creating
    edges for documents that were never inserted.
    """
    return SimpleNamespace(
        ingestion_service=dry_ingestion_service,
        lifecycle_service=dry_lifecycle_service,
        config=dry_config,
        graph_store=graph_store,
        lock_manager=lock_manager,
    )


async def test_bulk_dry_run_previews_every_item_and_persists_nothing(
    tmp_vault_dir,
    dry_config,
    dry_ingestion_service,
    dry_lifecycle_service,
    graph_store,
    lock_manager,
    stub_content_store,
):
    """A mixed batch: one novel file, one whose bytes are already held, one
    carrying a doc_type through ``parsed_metadata``, and one that cannot be
    found. Previews and errors together account for the batch, in batch
    order, and nothing is written.

    The refusal arm is a missing source rather than a typed-metadata
    violation because the batch descriptor carries no tier3 channel at all:
    ``ParsedMetadataInput`` reaches doc_type but not the typed payload, so a
    tier3 refusal is unreachable from here by construction. The
    requirement-set enrichment is pinned on the single-item path, where a
    caller can actually supply one.
    """
    held = _write(tmp_vault_dir, "bulk_held.md", "# Held\n\nAlready in the vault.")
    await dry_ingestion_service.ingest(
        IngestRequest(
            source=str(held), source_type=SourceType.MARKDOWN, metadata={"doc_type": "misc"}
        )
    )
    novel = _write(tmp_vault_dir, "bulk_novel.md", "# Novel\n\nNot yet held.")
    typed = _write(tmp_vault_dir, "bulk_typed.md", "# Typed\n\nCarries a doc_type.")

    services = _batch_services(
        dry_config, dry_ingestion_service, dry_lifecycle_service, graph_store, lock_manager
    )
    before = await state_snapshot(graph_store, stub_content_store)

    summary = await BatchIngestService().run(
        files=[
            FileDescriptor(file_path=str(novel), source_type="markdown"),
            FileDescriptor(file_path=str(held), source_type="markdown"),
            FileDescriptor(
                file_path=str(typed),
                source_type="markdown",
                parsed_metadata=ParsedMetadataInput(title="Typed", doc_type="loose_record"),
            ),
            FileDescriptor(
                file_path=str(tmp_vault_dir / "sources" / "absent.md"),
                source_type="markdown",
            ),
        ],
        vault_services=services,
        infer_edges=True,
        dry_run=True,
    )

    after = await state_snapshot(graph_store, stub_content_store)
    assert_state_unchanged(before, after)

    assert summary.dry_run is True
    assert summary.docs_new == 0
    assert summary.docs_version == 0
    assert len(summary.previews) == 3
    novel_preview, held_preview, typed_preview = summary.previews
    assert novel_preview.would_create is True
    assert held_preview.would_create is False
    assert held_preview.duplicate_of is not None
    assert typed_preview.resolved_doc_type == "loose_record"
    assert typed_preview.requirements.declared_tier3_fields == ["close_pr", "ticket_id"]

    assert summary.error_count == 1
    (error,) = summary.errors
    assert error.code == "source_file_not_found"


async def test_bulk_dry_run_skips_edge_inference(
    tmp_vault_dir,
    dry_config,
    dry_ingestion_service,
    dry_lifecycle_service,
    graph_store,
    lock_manager,
    monkeypatch,
):
    """``infer_edges=True`` is overridden by ``dry_run``. An edge plan
    resolves against document ids a preview never mints, so building one
    would cost reads to produce a plan that could only be discarded.

    The observable is the plan builder itself, not the edge counts. Those
    stay zero whether or not Phase 1 runs, because only Phase 3 writes
    edges -- an earlier version of this test asserted them and reddened
    under mutation only because a built plan makes Phase 3 reach a
    fixture attribute deliberately withheld, which is an accident rather
    than a control.
    """
    a = _write(tmp_vault_dir, "edge_a.md", "# A\n\nBody.")
    services = _batch_services(
        dry_config, dry_ingestion_service, dry_lifecycle_service, graph_store, lock_manager
    )

    service = BatchIngestService()
    calls: list[object] = []

    async def _record(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Phase 1 must not run on a dry run")

    monkeypatch.setattr(service, "_build_edge_plan", _record)

    summary = await service.run(
        files=[FileDescriptor(file_path=str(a), source_type="markdown")],
        vault_services=services,
        infer_edges=True,
        dry_run=True,
    )

    assert calls == []
    assert summary.edges_created == {}
    assert summary.edges_staged == {}
    assert summary.edges_dropped == 0


async def test_bulk_summary_dict_omits_previews_on_a_real_run(
    tmp_vault_dir,
    dry_config,
    dry_ingestion_service,
    dry_lifecycle_service,
    graph_store,
    lock_manager,
):
    """Negative control on the wire shape. ``previews`` is present only on a
    dry run; ``dry_run`` itself always echoes."""
    a = _write(tmp_vault_dir, "wire.md", "# Wire\n\nBody.")
    services = _batch_services(
        dry_config, dry_ingestion_service, dry_lifecycle_service, graph_store, lock_manager
    )

    dry = (
        await BatchIngestService().run(
            files=[FileDescriptor(file_path=str(a), source_type="markdown")],
            vault_services=services,
            infer_edges=False,
            dry_run=True,
        )
    ).to_dict()
    assert dry["dry_run"] is True
    assert len(dry["previews"]) == 1

    real = (
        await BatchIngestService().run(
            files=[FileDescriptor(file_path=str(a), source_type="markdown")],
            vault_services=services,
            infer_edges=False,
        )
    ).to_dict()
    assert real["dry_run"] is False
    assert "previews" not in real


# ---------------------------------------------------------------------------
# (G) The doc_type vocabulary gate
# ---------------------------------------------------------------------------


async def test_ingest_refuses_an_undeclared_doc_type(tmp_vault_dir, dry_ingestion_service):
    """A doc_type the vault never declared is refused at ingest, naming the
    vocabulary that would satisfy it.

    Until this gate, ``update_metadata`` refused such a value while ingest
    committed it, so a misspelling entered the vault through one door and
    was rejected at the other. The refusal is the same error the update
    path raises, because it is the same rule.
    """
    source = _write(tmp_vault_dir, "typo.md", "# Typo\n\nBody.")

    with pytest.raises(InvalidDocTypeError) as excinfo:
        await dry_ingestion_service.ingest(
            IngestRequest(
                source=str(source),
                source_type=SourceType.MARKDOWN,
                metadata={"doc_type": "stering_document"},
            )
        )

    assert excinfo.value.detail["doc_type"] == "stering_document"
    assert excinfo.value.detail["valid_types"] == [
        "bare_record",
        "loose_record",
        "misc",
        "strict_record",
    ]


async def test_ingest_accepts_every_declared_doc_type(tmp_vault_dir, dry_ingestion_service):
    """Negative control. A gate that refused everything would pass the test
    above; this one pins that each declared value still commits."""
    for i, dt in enumerate(("misc", "loose_record", "bare_record")):
        source = _write(tmp_vault_dir, f"declared-{i}.md", f"# Declared {i}\n\nBody {i}.")
        result = await dry_ingestion_service.ingest(
            IngestRequest(
                source=str(source),
                source_type=SourceType.MARKDOWN,
                metadata={"doc_type": dt},
            )
        )
        assert result.document.doc_type == dt


async def test_dry_run_refuses_an_undeclared_doc_type_too(tmp_vault_dir, dry_ingestion_service):
    """The gate runs in real-run order, so a preview reaches it. Without
    this the preview would report ``is_declared=false`` on a call that a
    real run refuses outright, which is a verdict rather than a warning."""
    source = _write(tmp_vault_dir, "typo2.md", "# Typo\n\nBody.")

    with pytest.raises(InvalidDocTypeError):
        await dry_ingestion_service.ingest(
            IngestRequest(
                source=str(source),
                source_type=SourceType.MARKDOWN,
                metadata={"doc_type": "not_a_type"},
                dry_run=True,
            )
        )


async def test_ingest_admits_an_absent_doc_type(tmp_vault_dir, dry_ingestion_service):
    """Omitting the field is not naming an unknown one. The resolution
    chain falls through to the vault default, which every vault declares,
    so a gate keyed on the resolved value must not fire here."""
    source = _write(tmp_vault_dir, "nodt.md", "# No doc_type\n\nBody.")
    result = await dry_ingestion_service.ingest(
        IngestRequest(source=str(source), source_type=SourceType.MARKDOWN)
    )
    assert result.document.doc_type == "misc"


async def test_ingest_admits_an_inherited_doc_type_on_supersede(
    tmp_vault_dir, dry_ingestion_service
):
    """Predecessor inheritance feeds the same resolved value the gate reads,
    so a successor that names nothing inherits a declared type and passes.
    A gate placed before resolution would refuse this."""
    _write(tmp_vault_dir, "inh-pred.md", "# Pred\n\nBody.")
    pred = await dry_ingestion_service.ingest(
        IngestRequest(
            source="inh-pred.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "loose_record"},
        )
    )
    successor = _write(tmp_vault_dir, "inh-succ.md", "# Succ\n\nRevised body.")

    result = await dry_ingestion_service.ingest(
        IngestRequest(
            source=str(successor),
            source_type=SourceType.MARKDOWN,
            predecessor_id=pred.document.id,
        )
    )
    assert result.document.doc_type == "loose_record"


# ---------------------------------------------------------------------------
# (H) Regression guards for behaviours the review found unpinned
# ---------------------------------------------------------------------------


async def test_preview_parses_the_filename_of_a_store_resident_source(
    tmp_vault_dir, dry_ingestion_service, monkeypatch
):
    """A source present in the store but absent from the local tree still
    has its filename parsed, because the real path parses it.

    The parser reads the stem and never opens the file, so the branch has
    no reason to skip it -- and skipping it resolved the doc_type
    differently in a preview than in the ingest it previewed. Reachable
    only under a non-filesystem binding, which is why the store is stood
    in for here: under the filesystem binding the store *is* the local
    tree, so the branch cannot be entered at all.
    """

    class _StoreResident:
        def source_exists(self, vault_id, storage_root, source):
            return True

    monkeypatch.setattr(
        "sage.mcp_init.resolve_stack_vault_source_store",
        lambda _config: _StoreResident(),
    )

    preview = await dry_ingestion_service.ingest(
        IngestRequest(
            source="imports/2026-09-11_CAS_LR_only-in-the-store.md",
            source_type=SourceType.MARKDOWN,
            needs_review=True,
            dry_run=True,
        )
    )

    assert preview.resolved_doc_type == "loose_record", (
        "The filename maps its code to loose_record. Falling through to "
        "misc means the parse was skipped on this branch."
    )
    # The bytes were never delivered on this branch and no prior record
    # holds them, so there is no digest to inherit and none is invented.
    assert preview.source_content_hash is None


async def test_an_undeclared_doc_type_refuses_before_the_source_is_retained(
    tmp_vault_dir, dry_ingestion_service
):
    """The vocabulary gate runs above retention, so a refused ingest leaves
    no retained file behind.

    Placed below retention the gate still refused, and still refused with
    the right error -- so a test asserting only the raise passes either
    way. What separates the two is the vault tree: a refusal after
    retention leaves a copied file with no row, which no audit walks.
    """
    outside = tmp_vault_dir / "outside_gate"
    outside.mkdir(parents=True, exist_ok=True)
    source = outside / "misspelled.md"
    source.write_text("# Misspelled\n\nBody.")

    storage_root = tmp_vault_dir / "sources"
    storage_root.mkdir(parents=True, exist_ok=True)
    before = _tree(storage_root)

    with pytest.raises(InvalidDocTypeError):
        await dry_ingestion_service.ingest(
            IngestRequest(
                source=str(source),
                source_type=SourceType.MARKDOWN,
                metadata={"doc_type": "stering_document"},
            )
        )

    assert _tree(storage_root) == before, (
        "A refused ingest retained its source before refusing, orphaning "
        "the copy. The gate reads only request metadata and vault config, "
        "so it belongs above the first irreversible act."
    )


async def test_bulk_dry_run_carries_an_empty_previews_list_when_all_files_refuse(
    tmp_vault_dir,
    dry_config,
    dry_ingestion_service,
    dry_lifecycle_service,
    graph_store,
    lock_manager,
):
    """``previews`` is keyed on the flag, not on the list being non-empty.

    A dry run whose files were every one refused has an empty previews
    list and a populated errors list. Keying the emission on emptiness
    drops the key entirely there, making that run indistinguishable on
    the wire from a real one apart from the echo -- and the contract says
    the field is present on a dry run.
    """
    services = _batch_services(
        dry_config, dry_ingestion_service, dry_lifecycle_service, graph_store, lock_manager
    )

    summary = await BatchIngestService().run(
        files=[
            FileDescriptor(
                file_path=str(tmp_vault_dir / "sources" / "absent-one.md"),
                source_type="markdown",
            ),
            FileDescriptor(
                file_path=str(tmp_vault_dir / "sources" / "absent-two.md"),
                source_type="markdown",
            ),
        ],
        vault_services=services,
        infer_edges=False,
        dry_run=True,
    )

    assert summary.previews == []
    assert summary.error_count == 2

    wire = summary.to_dict()
    assert "previews" in wire, (
        "A dry run in which every file was refused must still carry the "
        "previews key; the errors list is what accounts for the batch."
    )
    assert wire["previews"] == []
