"""SAGE-side uniqueness enforcement for declared-unique tier3_metadata fields.

Coverage for (CAS-ADR-031):

* Substrate: ``unique_keys`` round-trips through document_types.schema.json
  and the DocTypeEntry Pydantic model; the cross-field validator rejects
  entries that don't reference a metadata_schema property.
* GraphStore: partial UNIQUE expression indexes enforce uniqueness on
  declared (doc_type, field) pairs scoped to chain heads; collisions
  raise `Tier3UniqueViolation` translated by the service layer into the
  public `Tier3UniqueConstraintViolation`. The supersession-lineage
  exception (a successor inheriting its predecessor's identifier) is
  honored because the predecessor is flipped out of `is_chain_head=1`
  before the successor inserts.
* Migration: `MaintenanceService.migrate_vault` activates declared
  unique_keys by creating partial UNIQUE indexes when the portfolio is
  clean and refuses to activate while collisions exist; the per-doc_type
  scan groups documents by supersession chain so a chain whose members
  share an identifier is one logical artifact rather than a collision.
* Cross-surface: the SAGEError carries the structured 409 envelope
  (doc_type, field, colliding_value, existing_document_id) so callers
  detect and respond programmatically.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from sage.api.errors import (
    Tier3UniqueConstraintViolation,
    VaultConfigValidationError,
)
from sage.config import DocTypeEntry, VaultConfig
from sage.models.enums import EdgeType, ResolutionPolicy, SourceType
from sage.models.schemas import (
    Document,
    Edge,
    IngestRequest,
    RationaleKind,
)
from sage.services.ingestion import IngestionService
from sage.services.lifecycle import LifecycleService
from sage.services.maintenance import MaintenanceService
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from sage.storage.tier3_uniqueness import (
    Tier3UniqueViolation,
    tier3_unique_index_name,
)

# ---------------------------------------------------------------------------
# Fixtures: vault config with declared unique_keys
# ---------------------------------------------------------------------------


def _config_dict_with_unique_keys(tmp_vault_dir: Path) -> dict:
    """Vault config opting `ticket.ticket_id` into uniqueness, mirroring
    the shape lands but in an isolated test vault."""
    return {
        "vault": {
            "id": "test_tier3_unique_vault",
            "name": "Test Tier3 Unique Vault",
            "owner": "testuser",
            "storage_root": str(tmp_vault_dir / "sources"),
            "brain_root": str(tmp_vault_dir / "brain"),
            "visibility": "personal",
        },
        "document_types": {
            "doc_types": [
                {
                    "value": "ticket",
                    "label": "Ticket",
                    "metadata_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "ticket_id": {"type": "string", "pattern": r"^T-\d{4}$"},
                            "ticket_priority": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                            },
                        },
                    },
                    "unique_keys": ["ticket_id"],
                },
                {
                    "value": "failure_record",
                    "label": "Failure Record",
                    "metadata_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "failure_id": {"type": "string"},
                            "severity": {"type": "string"},
                        },
                    },
                    "unique_keys": ["failure_id"],
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
        "metadata_extraction": {},
        "edge_inference": {
            "tier_assignments": [
                {
                    "edge_type": "supersedes",
                    "tier": 1,
                    "inference_rules": [{"method": "version_chain"}],
                }
            ]
        },
    }


@pytest.fixture
def unique_keys_config(tmp_vault_dir):
    return VaultConfig.model_validate(_config_dict_with_unique_keys(tmp_vault_dir))


@pytest.fixture
def unique_keys_ingestion_service(
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    unique_keys_config,
):
    lifecycle = LifecycleService(graph_store, lock_manager, unique_keys_config)
    return IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=stub_abstraction_provider,
        config=unique_keys_config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
        lifecycle_service=lifecycle,
    )


@pytest.fixture
def unique_keys_maintenance_service(graph_store, unique_keys_config, stub_content_store):
    return MaintenanceService(
        vault_id="test_tier3_unique_vault",
        graph_store=graph_store,
        config=unique_keys_config,
        registry_service=None,
        content_store=stub_content_store,
    )


def _write_md(tmp_vault_dir: Path, relative_path: str, body: str) -> None:
    full_path = tmp_vault_dir / "sources" / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(body)


# ---------------------------------------------------------------------------
# T1, T2: Substrate / vault-config Pydantic round-trip + cross-field validator
# ---------------------------------------------------------------------------


def test_t1_unique_keys_round_trips_through_pydantic(tmp_vault_dir):
    """T1: a vault_config with `unique_keys: ['ticket_id']` parses and the
    DocTypeEntry round-trips the field. Anti-coincidental-pass: removing
    the new field from DocTypeEntry would drop the value, breaking the
    assertion."""
    cfg = VaultConfig.model_validate(_config_dict_with_unique_keys(tmp_vault_dir))
    ticket_dt = next(dt for dt in cfg.document_types.doc_types if dt.value == "ticket")
    assert ticket_dt.unique_keys == ["ticket_id"]
    failure_dt = next(dt for dt in cfg.document_types.doc_types if dt.value == "failure_record")
    assert failure_dt.unique_keys == ["failure_id"]
    # The misc doc_type has no unique_keys declared: None preserves the
    # pre-default-off shape.
    misc_dt = next(dt for dt in cfg.document_types.doc_types if dt.value == "misc")
    assert misc_dt.unique_keys is None


def test_t2_cross_field_validator_rejects_nonexistent_field(tmp_vault_dir):
    """T2: `unique_keys: ['nonexistent']` raises a ValidationError naming
    the doc_type and the missing field. Anti-coincidental: dropping the
    validator would let the config parse cleanly."""
    config_dict = _config_dict_with_unique_keys(tmp_vault_dir)
    ticket_idx = next(
        i
        for i, dt in enumerate(config_dict["document_types"]["doc_types"])
        if dt["value"] == "ticket"
    )
    config_dict["document_types"]["doc_types"][ticket_idx]["unique_keys"] = ["nonexistent_field"]

    with pytest.raises(Exception) as excinfo:
        VaultConfig.model_validate(config_dict)
    msg = str(excinfo.value)
    assert "nonexistent_field" in msg
    assert "ticket" in msg


def test_t2_cross_field_validator_rejects_doc_type_without_metadata_schema(tmp_vault_dir):
    """T2 (extension): declaring unique_keys on a doc_type with no
    metadata_schema is a config error -- the constraint would target a
    field SAGE doesn't validate."""
    config_dict = _config_dict_with_unique_keys(tmp_vault_dir)
    misc_idx = next(
        i
        for i, dt in enumerate(config_dict["document_types"]["doc_types"])
        if dt["value"] == "misc"
    )
    config_dict["document_types"]["doc_types"][misc_idx]["unique_keys"] = ["anything"]

    with pytest.raises(Exception) as excinfo:
        VaultConfig.model_validate(config_dict)
    assert "anything" in str(excinfo.value)


def test_t2_vault_management_wraps_unique_keys_error(tmp_vault_dir):
    """The cross-field validator surfaces through vault_management's
    error-wrapping layer as VaultConfigValidationError."""
    from sage.vault_management import _validate_config

    config_dict = _config_dict_with_unique_keys(tmp_vault_dir)
    ticket_idx = next(
        i
        for i, dt in enumerate(config_dict["document_types"]["doc_types"])
        if dt["value"] == "ticket"
    )
    config_dict["document_types"]["doc_types"][ticket_idx]["unique_keys"] = ["nope"]

    with pytest.raises(VaultConfigValidationError) as excinfo:
        _validate_config(config_dict)
    detail_errors = excinfo.value.detail["errors"]
    assert any("nope" in err for err in detail_errors)


def test_t1_dt_entry_without_unique_keys_preserves_none_default():
    """Anti-coincidental control: a DocTypeEntry with no unique_keys
    declared has the value `None`, not `[]`. Ensures the default doesn't
    drift to a falsy-but-different value that the cross-field validator
    might handle differently."""
    dt = DocTypeEntry(value="memo", label="Memo")
    assert dt.unique_keys is None


# ---------------------------------------------------------------------------
# T3, T4: JSON schema accepts unique_keys and rejects malformed payloads
# ---------------------------------------------------------------------------


def _document_types_schema() -> dict:
    schema_path = (
        Path(__file__).resolve().parents[2] / "docs" / "fs" / "sage" / "document_types.schema.json"
    )
    return json.loads(schema_path.read_text())


def test_t3_schema_accepts_unique_keys_list_of_strings():
    """T3: the document_types JSON schema admits `unique_keys: list[str]`.
    Anti-coincidental: reverting the schema addition (no `unique_keys` in
    `properties` + `additionalProperties: false`) makes the fragment
    fail validation."""
    schema = _document_types_schema()
    fragment = {
        "doc_types": [
            {
                "value": "ticket",
                "label": "Ticket",
                "metadata_schema": {
                    "type": "object",
                    "properties": {"ticket_id": {"type": "string"}},
                },
                "unique_keys": ["ticket_id"],
            }
        ]
    }
    jsonschema.Draft202012Validator(schema).validate(fragment)


def test_t4_schema_rejects_string_unique_keys():
    """T4: `unique_keys` must be a list; passing a string fails validation."""
    schema = _document_types_schema()
    fragment = {
        "doc_types": [
            {
                "value": "ticket",
                "label": "Ticket",
                "unique_keys": "ticket_id",
            }
        ]
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(fragment)


def test_t4_schema_rejects_non_string_items():
    """T4 (extension): each entry must be a string."""
    schema = _document_types_schema()
    fragment = {
        "doc_types": [
            {
                "value": "ticket",
                "label": "Ticket",
                "unique_keys": [42],
            }
        ]
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(fragment)


def test_t4_schema_rejects_duplicate_unique_keys_entries():
    """`uniqueItems: true` on the items array surfaces duplicates as a
    schema violation. Anti-coincidental for the schema-author who might
    forget the `uniqueItems` constraint."""
    schema = _document_types_schema()
    fragment = {
        "doc_types": [
            {
                "value": "ticket",
                "label": "Ticket",
                "unique_keys": ["ticket_id", "ticket_id"],
            }
        ]
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(fragment)


# ---------------------------------------------------------------------------
# T5-T10: GraphStore enforcement
# ---------------------------------------------------------------------------


def _doc_id(prefix: str, slug: str) -> str:
    """Generate a Document id matching the canonical format
    ``^[0-9a-f]{8}_[a-z0-9_]+$`` (typed-alias constraint).

    The 8-character hex prefix is derived from a deterministic hash of
    `prefix`+`slug` so each call site produces a stable id without
    importing the production `generate_document_id` helper (which also
    requires a vault-relative source_path).
    """
    import hashlib

    digest = hashlib.sha256(f"{prefix}/{slug}".encode()).hexdigest()[:8]
    safe_slug = slug.lower().replace("-", "_").replace(" ", "_")
    return f"{digest}_{safe_slug}"


def _make_ticket_doc(doc_id: str, ticket_id: str, lifecycle_status: str = "active") -> Document:
    from datetime import datetime, timezone

    # Each call site supplies a short tag (e.g., "doc-1") so the resulting
    # id is stable and human-recognizable in failure messages but still
    # passes the canonical-id regex.
    canonical_id = _doc_id("ticket", f"{doc_id}_{ticket_id}")
    now = datetime.now(timezone.utc)
    # Vary source_path and content hash per id so the duplicate-content
    # gate doesn't fire before the tier3-uniqueness gate does.
    hash_payload = f"{canonical_id}{ticket_id}{lifecycle_status}"
    import hashlib

    digest = hashlib.sha256(hash_payload.encode()).hexdigest()
    return Document(
        id=canonical_id,
        title=f"Ticket {ticket_id}",
        source_type=SourceType.MARKDOWN,
        source_path=f"tickets/{canonical_id}.md",
        lifecycle_status=lifecycle_status,
        doc_type="ticket",
        source_content_hash=f"sha256:{digest}",
        adapter_version="0.5.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        tier3_metadata={"ticket_id": ticket_id, "ticket_priority": "medium"},
    )


def _make_failure_doc(doc_id: str, failure_id: str) -> Document:
    from datetime import datetime, timezone

    canonical_id = _doc_id("failure", f"{doc_id}_{failure_id}")
    now = datetime.now(timezone.utc)
    import hashlib

    digest = hashlib.sha256(f"{canonical_id}{failure_id}".encode()).hexdigest()
    return Document(
        id=canonical_id,
        title=f"Failure {failure_id}",
        source_type=SourceType.MARKDOWN,
        source_path=f"failures/{canonical_id}.md",
        lifecycle_status="active",
        doc_type="failure_record",
        source_content_hash=f"sha256:{digest}",
        adapter_version="0.5.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        tier3_metadata={"failure_id": failure_id, "severity": "high"},
    )


async def _activate_unique_index(graph_store, doc_type: str, field: str) -> None:
    """Create the partial UNIQUE index for a (doc_type, field) pair.

    The migration path drives this via `MaintenanceService.migrate_vault`
    in production; tests that exercise GraphStore directly call this
    helper to put the index in place before triggering collisions.
    """
    await graph_store.ensure_tier3_unique_index(doc_type, field)


async def test_t5_insert_with_colliding_tier3_raises_violation(graph_store):
    """T5: a second ingest with the same `tier3_metadata.ticket_id` raises
    `Tier3UniqueViolation` (storage-layer signal). Anti-coincidental:
    skipping the partial UNIQUE index leaves both inserts succeeding."""
    await _activate_unique_index(graph_store, "ticket", "ticket_id")
    first = _make_ticket_doc("doc-1", "T-0001")
    await graph_store.insert_document(first)

    second = _make_ticket_doc("doc-2", "T-0001")
    with pytest.raises(Tier3UniqueViolation) as excinfo:
        await graph_store.insert_document(second)
    assert excinfo.value.doc_type == "ticket"
    assert excinfo.value.field == "ticket_id"
    assert excinfo.value.colliding_value == "T-0001"
    assert excinfo.value.existing_document_id == first.id


async def test_t6_archived_chain_head_still_holds_identifier(graph_store):
    """T6: an archived ticket that was never superseded remains a chain
    head (`is_chain_head = 1`); a new unrelated insert with the same
    identifier collides. Anti-coincidental: a WHERE clause that excluded
    archived rows would let this second insert succeed."""
    await _activate_unique_index(graph_store, "ticket", "ticket_id")
    archived = _make_ticket_doc("doc-1", "T-0001", lifecycle_status="archived")
    await graph_store.insert_document(archived)

    new_doc = _make_ticket_doc("doc-2", "T-0001")
    with pytest.raises(Tier3UniqueViolation):
        await graph_store.insert_document(new_doc)


async def test_t7_supersession_with_shared_identifier_succeeds(
    tmp_vault_dir, unique_keys_ingestion_service, graph_store
):
    """T7: ingest with `predecessor_id` set and the same
    `ticket_id` succeeds; predecessor is flipped out of `is_chain_head`
    before the successor inserts. Anti-coincidental: failing to reorder
    the atomic supersede path causes the partial UNIQUE to fire on the
    successor's insert."""
    await _activate_unique_index(graph_store, "ticket", "ticket_id")
    _write_md(tmp_vault_dir, "ticket_v1.md", "# Ticket v1\n\nBody v1.")
    first = await unique_keys_ingestion_service.ingest(
        IngestRequest(
            source="ticket_v1.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0001", "ticket_priority": "medium"},
        )
    )

    _write_md(tmp_vault_dir, "ticket_v2.md", "# Ticket v2\n\nBody v2.")
    successor = await unique_keys_ingestion_service.ingest(
        IngestRequest(
            source="ticket_v2.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0001", "ticket_priority": "high"},
            predecessor_id=first.document.id,
        )
    )
    pred_fresh = await graph_store.get_document(first.document.id)
    succ_fresh = await graph_store.get_document(successor.document.id)
    assert pred_fresh.lifecycle_status == "archived"
    assert succ_fresh.lifecycle_status == "active"
    assert succ_fresh.tier3_metadata["ticket_id"] == "T-0001"
    assert succ_fresh.tier3_metadata["ticket_priority"] == "high"


async def test_t8_second_unrelated_ingest_after_supersession_still_collides(
    tmp_vault_dir, unique_keys_ingestion_service, graph_store
):
    """T8 (functional substitute for a true concurrent-race test):
    after a chain head holds ``, a third unrelated insert with the
    same identifier still collides. Validates that the partial UNIQUE
    constraint stays live across the supersession transition rather than
    being dropped by the reordering."""
    await _activate_unique_index(graph_store, "ticket", "ticket_id")
    _write_md(tmp_vault_dir, "t1.md", "# T1\n\nBody.")
    first = await unique_keys_ingestion_service.ingest(
        IngestRequest(
            source="t1.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0001", "ticket_priority": "medium"},
        )
    )

    _write_md(tmp_vault_dir, "t2.md", "# T2\n\nBody.")
    await unique_keys_ingestion_service.ingest(
        IngestRequest(
            source="t2.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0001", "ticket_priority": "high"},
            predecessor_id=first.document.id,
        )
    )

    _write_md(tmp_vault_dir, "t3.md", "# T3\n\nBody.")
    with pytest.raises(Tier3UniqueConstraintViolation) as excinfo:
        await unique_keys_ingestion_service.ingest(
            IngestRequest(
                source="t3.md",
                source_type=SourceType.MARKDOWN,
                metadata={"doc_type": "ticket"},
                tier3_metadata={"ticket_id": "T-0001", "ticket_priority": "low"},
            )
        )
    assert excinfo.value.detail["field"] == "ticket_id"
    assert excinfo.value.detail["colliding_value"] == "T-0001"


async def test_t9_uniqueness_scoped_per_doc_type(graph_store):
    """T9: a ticket and a failure_record carrying the same literal value
    in different fields and doc_types are NOT a collision. Anti-
    coincidental: creating a single index per field (without doc_type
    scoping) would falsely reject this pair."""
    await _activate_unique_index(graph_store, "ticket", "ticket_id")
    await _activate_unique_index(graph_store, "failure_record", "failure_id")
    ticket_doc = _make_ticket_doc("doc-t", "T-0001")
    failure_doc = _make_failure_doc("doc-f", "T-0001")
    await graph_store.insert_document(ticket_doc)
    # No collision: different doc_type and different field name.
    await graph_store.insert_document(failure_doc)
    # Both rows are present.
    docs, _ = await graph_store.query_documents()
    ids = {d.id for d in docs}
    assert {ticket_doc.id, failure_doc.id}.issubset(ids)


async def test_t10_null_tier3_field_does_not_collide(graph_store):
    """T10: documents whose `tier3_metadata` is missing the declared
    field do not collide on the absent value. Anti-coincidental: a
    partial index without `WHERE json_extract(...) IS NOT NULL` would
    collide on the second null."""
    await _activate_unique_index(graph_store, "ticket", "ticket_id")
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    doc_a = Document(
        id=_doc_id("ticket", "no_tier3_a"),
        title="A",
        source_type=SourceType.MARKDOWN,
        source_path="a.md",
        lifecycle_status="active",
        doc_type="ticket",
        source_content_hash=f"sha256:{'c' * 64}",
        adapter_version="0.5.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        tier3_metadata=None,
    )
    doc_b = Document(
        id=_doc_id("ticket", "no_tier3_b"),
        title="B",
        source_type=SourceType.MARKDOWN,
        source_path="b.md",
        lifecycle_status="active",
        doc_type="ticket",
        source_content_hash=f"sha256:{'d' * 64}",
        adapter_version="0.5.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        tier3_metadata=None,
    )
    await graph_store.insert_document(doc_a)
    await graph_store.insert_document(doc_b)


# ---------------------------------------------------------------------------
# T11, T12: Cross-surface error envelope
# ---------------------------------------------------------------------------


def test_t11_violation_carries_structured_409_envelope():
    """T11: the public SAGEError surfaces with code
    `tier3_unique_constraint_violation`, status 409, and the four-field
    detail payload. The FastAPI exception handler converts SAGEError
    subclasses uniformly via `ErrorResponse`; this test exercises the
    error's own contract, which the handler relies on."""
    exc = Tier3UniqueConstraintViolation(
        doc_type="ticket",
        field="ticket_id",
        colliding_value="T-0001",
        existing_document_id="abc123_ticket",
    )
    assert exc.code == "tier3_unique_constraint_violation"
    assert exc.status_code == 409
    assert exc.detail == {
        "doc_type": "ticket",
        "field": "ticket_id",
        "colliding_value": "T-0001",
        "existing_document_id": "abc123_ticket",
    }


async def test_t12_ingestion_service_translates_storage_signal(
    tmp_vault_dir, unique_keys_ingestion_service, graph_store
):
    """T12: IngestionService catches the storage-layer
    `Tier3UniqueViolation` and re-raises the public
    `Tier3UniqueConstraintViolation`. Anti-coincidental: forgetting to
    catch and translate would leak a storage-layer Exception through the
    MCP envelope as an unhandled error."""
    await _activate_unique_index(graph_store, "ticket", "ticket_id")
    _write_md(tmp_vault_dir, "first.md", "# First\n\nBody.")
    await unique_keys_ingestion_service.ingest(
        IngestRequest(
            source="first.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0042", "ticket_priority": "low"},
        )
    )

    _write_md(tmp_vault_dir, "second.md", "# Second\n\nBody.")
    with pytest.raises(Tier3UniqueConstraintViolation) as excinfo:
        await unique_keys_ingestion_service.ingest(
            IngestRequest(
                source="second.md",
                source_type=SourceType.MARKDOWN,
                metadata={"doc_type": "ticket"},
                tier3_metadata={"ticket_id": "T-0042", "ticket_priority": "high"},
            )
        )
    assert excinfo.value.code == "tier3_unique_constraint_violation"
    assert excinfo.value.detail["colliding_value"] == "T-0042"


# ---------------------------------------------------------------------------
# T13, T14: Migration scan respects supersession-chain exception
# ---------------------------------------------------------------------------


async def test_t13_migration_scan_reports_unrelated_collisions(
    graph_store, unique_keys_maintenance_service
):
    """T13: two unrelated documents sharing `ticket_id=''` (no
    supersedes edge between them) are surfaced as a collision when the
    `unique_keys` opt-in is scanned. Anti-coincidental: skipping the
    chain-head pass and counting raw rows would still flag this case,
    but T14 verifies the converse."""
    # Insert two unrelated active tickets with the same ticket_id BEFORE
    # the partial UNIQUE index is activated (otherwise the second insert
    # would be rejected by the index itself). The migration scan must
    # surface this as a collision so the operator resolves it.
    a = _make_ticket_doc("doc-a", "T-0001")
    b = _make_ticket_doc("doc-b", "T-0001")
    await graph_store.insert_document(a)
    await graph_store.insert_document(b)

    collisions = await unique_keys_maintenance_service.scan_tier3_uniqueness_collisions(
        "ticket", "ticket_id"
    )
    assert len(collisions) == 1
    coll = collisions[0]
    assert coll.doc_type == "ticket"
    assert coll.field == "ticket_id"
    assert coll.value == "T-0001"
    assert set(coll.document_ids) == {a.id, b.id}


async def test_t14_migration_scan_treats_supersession_chain_as_one_artifact(
    graph_store, unique_keys_maintenance_service
):
    """T14: a chain of three tickets all sharing `ticket_id=''`
    connected by supersedes edges is one logical artifact. Only the
    chain head has `is_chain_head=1`; the migration scan sees a single
    chain head and reports no collision. Anti-coincidental: ignoring
    the supersedes-edge lineage flag would surface 3 colliding rows."""
    import uuid
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    a = _make_ticket_doc("a", "T-0001", lifecycle_status="archived")
    b = _make_ticket_doc("b", "T-0001", lifecycle_status="archived")
    c = _make_ticket_doc("c", "T-0001", lifecycle_status="active")
    await graph_store.insert_document(a)
    await graph_store.insert_document(b)
    await graph_store.insert_document(c)

    # B supersedes A, C supersedes B. The trigger
    # `trg_tier3_chain_head_on_supersedes` flips the target's
    # `is_chain_head` to 0 on each edge insert.
    edge_b_a = Edge(
        id=str(uuid.uuid4()),
        source_id=b.id,
        target_id=a.id,
        edge_type=EdgeType.SUPERSEDES,
        resolution_policy=ResolutionPolicy.TRANSITIVE_BOTH,
        created_at=now,
        rationale_kind=RationaleKind.VERSION_CHAIN,
    )
    edge_c_b = Edge(
        id=str(uuid.uuid4()),
        source_id=c.id,
        target_id=b.id,
        edge_type=EdgeType.SUPERSEDES,
        resolution_policy=ResolutionPolicy.TRANSITIVE_BOTH,
        created_at=now,
        rationale_kind=RationaleKind.VERSION_CHAIN,
    )
    await graph_store.insert_edge(edge_b_a)
    await graph_store.insert_edge(edge_c_b)

    collisions = await unique_keys_maintenance_service.scan_tier3_uniqueness_collisions(
        "ticket", "ticket_id"
    )
    assert collisions == []


# ---------------------------------------------------------------------------
# T15, T16: Migration refuses to activate on collision; creates index when clean
# ---------------------------------------------------------------------------


async def _index_exists_in_catalog(pg_pool, doc_type: str, field: str) -> bool:
    """Check the partial UNIQUE index in pg_indexes directly.

    Bypasses the store's own ``tier3_unique_index_exists`` so an
    activation bug and a broken existence check cannot mask each other.
    """
    name = tier3_unique_index_name(doc_type, field)
    async with pg_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() AND indexname = %s",
            (name,),
        )
        return await cur.fetchone() is not None


async def test_t15_migrate_vault_refuses_activation_on_collision(
    graph_store, unique_keys_maintenance_service, pg_pool
):
    """T15: `migrate_vault` returns the collision in
    `tier3_uniqueness_collisions` and the partial UNIQUE index is NOT
    created. Anti-coincidental: unconditionally creating the index would
    leave the constraint live despite the collision; we verify both the
    report shape and the absence of the index in pg_indexes."""
    await graph_store.insert_document(_make_ticket_doc("a", "T-0001"))
    await graph_store.insert_document(_make_ticket_doc("b", "T-0001"))

    report = await unique_keys_maintenance_service.migrate_vault()
    # The ticket.ticket_id activation is blocked by the collision; the
    # failure_record.failure_id pair has no collision so it activates.
    activated_pairs = {(a.doc_type, a.field) for a in report.tier3_uniqueness_activations}
    assert ("ticket", "ticket_id") not in activated_pairs
    assert ("failure_record", "failure_id") in activated_pairs
    ticket_collisions = [c for c in report.tier3_uniqueness_collisions if c.doc_type == "ticket"]
    assert len(ticket_collisions) == 1
    assert ticket_collisions[0].field == "ticket_id"

    # Verify against the catalog: the blocked pair's index is absent
    # (a follow-on colliding insert would NOT raise) while the clean
    # pair's index landed.
    assert not await _index_exists_in_catalog(pg_pool, "ticket", "ticket_id")
    assert await _index_exists_in_catalog(pg_pool, "failure_record", "failure_id")


async def test_t16_migrate_vault_creates_index_on_clean_portfolio(
    graph_store, unique_keys_maintenance_service, pg_pool
):
    """T16: a portfolio without collisions activates cleanly: the partial
    UNIQUE index is created and a subsequent colliding insert raises
    `Tier3UniqueViolation`. Anti-coincidental: gating index creation on
    the wrong boolean (creating only when collisions exist) would leave
    the constraint absent on a clean vault."""
    # No documents in the portfolio: nothing to collide on.
    report = await unique_keys_maintenance_service.migrate_vault()
    activated_pairs = {(a.doc_type, a.field) for a in report.tier3_uniqueness_activations}
    assert ("ticket", "ticket_id") in activated_pairs
    assert ("failure_record", "failure_id") in activated_pairs
    assert report.tier3_uniqueness_collisions == []

    # Index now exists in the catalog.
    assert await _index_exists_in_catalog(pg_pool, "ticket", "ticket_id")
    assert await _index_exists_in_catalog(pg_pool, "failure_record", "failure_id")

    # And it actually enforces -- inserting a colliding pair raises.
    await graph_store.insert_document(_make_ticket_doc("a", "T-0001"))
    with pytest.raises(Tier3UniqueViolation):
        await graph_store.insert_document(_make_ticket_doc("b", "T-0001"))


async def test_t16_migration_is_idempotent(unique_keys_maintenance_service):
    """Re-running `migrate_vault` on an already-activated vault produces
    the same activations and no side effects. The CREATE UNIQUE INDEX
    IF NOT EXISTS makes index creation idempotent; this guards the
    higher-level service path."""
    first = await unique_keys_maintenance_service.migrate_vault()
    second = await unique_keys_maintenance_service.migrate_vault()
    assert {(a.doc_type, a.field) for a in first.tier3_uniqueness_activations} == {
        (a.doc_type, a.field) for a in second.tier3_uniqueness_activations
    }


# ---------------------------------------------------------------------------
# Defensive: GraphStore validation fences on tier3 identifiers
# ---------------------------------------------------------------------------


async def test_validate_tier3_identifier_rejects_sql_injection(graph_store):
    """The defense-in-depth fence in `_validate_tier3_identifier` rejects
    a malformed field name even if the cross-field validator was bypassed
    by direct GraphStore API use. SQL injection via index name is the
    failure this guards against."""
    with pytest.raises(ValueError):
        await graph_store.ensure_tier3_unique_index("ticket", "id; DROP TABLE documents;--")
    with pytest.raises(ValueError):
        await graph_store.ensure_tier3_unique_index("ticket'; --", "ticket_id")


# ---------------------------------------------------------------------------
# Sanity: index naming convention is stable
# ---------------------------------------------------------------------------


def test_index_name_convention():
    assert tier3_unique_index_name("ticket", "ticket_id") == "idx_tier3_unique_ticket_ticket_id"
    assert (
        tier3_unique_index_name("failure_record", "failure_id")
        == "idx_tier3_unique_failure_record_failure_id"
    )
