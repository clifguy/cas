"""Cross-surface tests for tier3_metadata (Phase 1).

Coverage:

* VaultConfig.model_post_init builds a validator-cache keyed by doc_type.
* VaultConfig.tier3_validator returns None for doc_types with no
  metadata_schema; the service layer treats that as 400 (strict
  no-loose-mode per the design).
* ingest_document validates tier3_metadata against the resolved doc_type's
  schema BEFORE inserting the document; failures raise
  Tier3SchemaViolationError without creating a row.
* update_metadata takes Tier3Patch (`{set, unset}`) and applies
  the patch in memory; the merged result is validated against the
  (possibly newly-set) doc_type. Strict-conflict on `unset` of an
  absent key.
* search catalog and semantic filters honor the new tier3 clause;
  a null filter value matches absent-or-null stored fields.
* A malformed metadata_schema in vault config surfaces at construction
  time as VaultConfigValidationError, not at first ingest.

Plumbing-shape coverage (IngestRequest, UpdateMetadataRequest,
RetrievalFilters) lives in tests/sage/test_request_validators.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sage.api.errors import (
    Tier3DocTypeChangeStaleKeysError,
    Tier3SchemaViolationError,
    Tier3UnsetConflictError,
    VaultConfigValidationError,
)
from sage.config import VaultConfig
from sage.models.enums import RetrievalMode, SourceType
from sage.models.schemas import (
    DiscoverRequest,
    IngestRequest,
    RetrievalFilters,
    Tier3Patch,
    UpdateMetadataRequest,
)
from sage.services.ingestion import IngestionService
from sage.services.lifecycle import LifecycleService
from sage.services.metadata import MetadataService
from sage.services.retrieval import RetrievalService, _tier3_matches
from sage.source_adapters.markdown_adapter import MarkdownAdapter

# ---------------------------------------------------------------------------
# Helpers and fixtures
# ---------------------------------------------------------------------------


def _config_dict_with_tier3(tmp_vault_dir: Path) -> dict:
    """A vault config with two doc_types declaring metadata_schema and one
    without. Mirrors the cas-vault shape (failure_record, ticket) plus a
    plain doc_type for the no-schema rejection case."""
    return {
        "vault": {
            "id": "test_tier3_vault",
            "name": "Test Tier3 Vault",
            "owner": "testuser",
            "storage_root": str(tmp_vault_dir / "sources"),
            "brain_root": str(tmp_vault_dir / "brain"),
            "visibility": "personal",
        },
        "document_types": {
            "doc_types": [
                {
                    "value": "failure_record",
                    "label": "Failure Record",
                    "metadata_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "severity": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                            },
                            "fix_commit": {"type": ["string", "null"]},
                        },
                    },
                },
                {
                    "value": "ticket",
                    "label": "Ticket",
                    "metadata_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "ticket_id": {"type": "string", "pattern": "^T-\\d{4}$"},
                            "ticket_priority": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                            },
                        },
                    },
                },
                {"value": "misc", "label": "Miscellaneous"},
                {
                    "value": "bare_record",
                    "label": "Bare Record",
                    "metadata_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {},
                    },
                },
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
                {"from_state": "active", "action": "complete", "to_state": "completed"},
                {"from_state": "active", "action": "archive", "to_state": "archived"},
                {"from_state": "completed", "action": "archive", "to_state": "archived"},
                {"from_state": "archived", "action": "reactivate", "to_state": "active"},
            ],
        },
        "metadata_extraction": {},
        "edge_inference": {},
    }


@pytest.fixture
def tier3_config(tmp_vault_dir):
    return VaultConfig.model_validate(_config_dict_with_tier3(tmp_vault_dir))


@pytest.fixture
def tier3_ingestion_service(
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    tier3_config,
):
    lifecycle = LifecycleService(graph_store, lock_manager, tier3_config)
    return IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=stub_abstraction_provider,
        config=tier3_config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
        lifecycle_service=lifecycle,
    )


@pytest.fixture
def tier3_metadata_service(graph_store, lock_manager, tier3_config, stub_content_store):
    return MetadataService(graph_store, lock_manager, tier3_config, stub_content_store)


@pytest.fixture
def tier3_retrieval_service(graph_store, stub_content_store, stub_embedding_provider, tier3_config):
    return RetrievalService(
        graph_store=graph_store,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        config=tier3_config,
    )


def _write_md(tmp_vault_dir: Path, relative_path: str, body: str) -> None:
    full_path = tmp_vault_dir / "sources" / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(body)


# ---------------------------------------------------------------------------
# VaultConfig validator cache
# ---------------------------------------------------------------------------


def test_validator_cache_built_for_doc_types_with_schema(tier3_config):
    assert tier3_config.tier3_validator("failure_record") is not None
    assert tier3_config.tier3_validator("ticket") is not None
    assert tier3_config.tier3_validator("misc") is None
    assert tier3_config.tier3_validator("nonexistent") is None


def test_malformed_metadata_schema_surfaces_at_construction(tmp_vault_dir):
    """A JSON Schema fragment with an invalid `type` value should fail
    config construction, not first ingest call."""
    config_dict = _config_dict_with_tier3(tmp_vault_dir)
    # Corrupt the schema: `type` must be a string or array of strings.
    config_dict["document_types"]["doc_types"][0]["metadata_schema"]["type"] = 42
    with pytest.raises(Exception):  # noqa: BLE001 -- either SchemaError or wrapped
        VaultConfig.model_validate(config_dict)


def test_load_vault_config_propagates_schema_error(tmp_vault_dir, tmp_path):
    """A malformed metadata_schema surfaces as jsonschema.SchemaError from
    load_vault_config. The wrapping into VaultConfigValidationError happens
    one layer up in sage.vault_management._validate_config (covered by
    test_vault_management_wraps_schema_error)."""
    import jsonschema
    import yaml

    from sage.config import load_vault_config

    config_dict = _config_dict_with_tier3(tmp_vault_dir)
    config_dict["document_types"]["doc_types"][0]["metadata_schema"]["type"] = "not_a_real_type"
    config_path = tmp_path / "vault_config.yaml"
    config_path.write_text(yaml.safe_dump(config_dict))

    with pytest.raises(jsonschema.SchemaError):
        load_vault_config(config_path)


def test_vault_management_wraps_schema_error(tmp_vault_dir):
    """sage.vault_management._validate_config catches jsonschema.SchemaError
    and re-raises as VaultConfigValidationError with a detail that points
    into the offending metadata_schema fragment."""
    from sage.vault_management import _validate_config

    config_dict = _config_dict_with_tier3(tmp_vault_dir)
    config_dict["document_types"]["doc_types"][0]["metadata_schema"]["type"] = "not_a_real_type"

    with pytest.raises(VaultConfigValidationError) as excinfo:
        _validate_config(config_dict)
    detail_errors = excinfo.value.detail["errors"]
    assert any("metadata_schema" in err for err in detail_errors)


# ---------------------------------------------------------------------------
# Ingestion service: tier3 validation pre-insert
# ---------------------------------------------------------------------------


async def test_ingest_with_no_schema_doc_type_rejects_tier3(tmp_vault_dir, tier3_ingestion_service):
    """The strict no-loose-mode decision: a tier3_metadata payload sent
    against a doc_type with no metadata_schema must 400 with code
    tier3_schema_violation."""
    _write_md(tmp_vault_dir, "loose.md", "# Loose\n\nBody.")

    request = IngestRequest(
        source="loose.md",
        source_type=SourceType.MARKDOWN,
        metadata={"doc_type": "misc"},
        tier3_metadata={"anything": 1},
    )

    with pytest.raises(Tier3SchemaViolationError) as excinfo:
        await tier3_ingestion_service.ingest(request)
    assert excinfo.value.code == "tier3_schema_violation"
    assert excinfo.value.detail["doc_type"] == "misc"
    assert "no metadata_schema declared" in excinfo.value.detail["message"]


async def test_ingest_with_no_schema_doc_type_and_empty_tier3_passes(
    tmp_vault_dir, tier3_ingestion_service, graph_store
):
    """An explicit empty tier3 dict against a no-schema doc_type
    is accepted at ingest, mirroring the MetadataService Wart 2 carve-out
    so the ingest-vs-update behavior stays symmetric. Anti-coincidental:
    the doc is actually created (the carve-out is a `return`, not a swap
    of the error class). Storage probe confirms the row exists and the
    no-tier3 case round-trips as None or an empty dict."""
    _write_md(tmp_vault_dir, "empty.md", "# Empty\n\nBody.")

    request = IngestRequest(
        source="empty.md",
        source_type=SourceType.MARKDOWN,
        metadata={"doc_type": "misc"},
        tier3_metadata={},
    )

    result = await tier3_ingestion_service.ingest(request)
    assert result.document.doc_type == "misc"

    # Anti-coincidental storage probe: the row must actually exist.
    fresh = await graph_store.get_document(result.document.id)
    assert fresh.doc_type == "misc"
    # Storage may normalize {} to None or keep it as {}; either is fine
    # for a no-schema doc_type because there's nothing to validate.
    assert not fresh.tier3_metadata  # both None and {} satisfy `not`


async def test_ingest_with_invalid_tier3_rejects_with_path(tmp_vault_dir, tier3_ingestion_service):
    _write_md(tmp_vault_dir, "fr.md", "# Failure\n\nBody.")

    request = IngestRequest(
        source="fr.md",
        source_type=SourceType.MARKDOWN,
        metadata={"doc_type": "failure_record"},
        tier3_metadata={"severity": "purple"},  # not in enum
    )

    with pytest.raises(Tier3SchemaViolationError) as excinfo:
        await tier3_ingestion_service.ingest(request)
    assert excinfo.value.code == "tier3_schema_violation"
    assert excinfo.value.detail["doc_type"] == "failure_record"
    # jsonschema.ValidationError.json_path begins with "$" for the root and
    # carries the offending property name; the exact format is documented as
    # the standard JSON Pointer-like accessor syntax.
    assert "severity" in excinfo.value.detail["path"]


async def test_ingest_with_valid_tier3_roundtrips(
    tmp_vault_dir, tier3_ingestion_service, graph_store
):
    _write_md(tmp_vault_dir, "fr.md", "# Failure\n\nBody.")

    request = IngestRequest(
        source="fr.md",
        source_type=SourceType.MARKDOWN,
        metadata={"doc_type": "failure_record"},
        tier3_metadata={"severity": "high", "fix_commit": None},
    )
    result = await tier3_ingestion_service.ingest(request)

    fresh = await graph_store.get_document(result.document.id)
    assert fresh.tier3_metadata == {"severity": "high", "fix_commit": None}


async def test_ingest_pre_insert_validation_does_not_orphan(
    tmp_vault_dir, tier3_ingestion_service, graph_store
):
    """The validation must run BEFORE insert_document; a failure should
    leave no row in the documents table."""
    _write_md(tmp_vault_dir, "orphan.md", "# Orphan\n\nBody.")

    docs_before, _ = await graph_store.query_documents()
    count_before = len(docs_before)

    with pytest.raises(Tier3SchemaViolationError):
        await tier3_ingestion_service.ingest(
            IngestRequest(
                source="orphan.md",
                source_type=SourceType.MARKDOWN,
                metadata={"doc_type": "failure_record"},
                tier3_metadata={"severity": 42},  # wrong type
            )
        )

    docs_after, _ = await graph_store.query_documents()
    assert len(docs_after) == count_before


# ---------------------------------------------------------------------------
# Metadata service: tier3 patch semantics (CAS-ADR-028 update revision)
# ---------------------------------------------------------------------------


async def test_update_metadata_patches_tier3_set_unset(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store
):
    """Patch semantics: `set` overwrites named keys; keys not mentioned
    are preserved (no deep-merge wipe). This is the load-bearing
    assertion of patch-vs-replace: with the prior replace semantics,
    setting only ticket_priority would have wiped ticket_id."""
    _write_md(tmp_vault_dir, "fr.md", "# Failure\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="fr.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "failure_record"},
            tier3_metadata={"severity": "low", "fix_commit": "abc123"},
        )
    )

    await tier3_metadata_service._update_metadata(
        initial.document.id,
        UpdateMetadataRequest(tier3_metadata=Tier3Patch(set={"severity": "high"})),
        modified_by="tester",
    )

    fresh = await graph_store.get_document(initial.document.id)
    assert fresh.tier3_metadata == {"severity": "high", "fix_commit": "abc123"}


async def test_update_metadata_with_tier3_validates_against_doc_type(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service
):
    """Post-merge validation: a set value that fails the doc_type schema
    surfaces as Tier3SchemaViolationError."""
    _write_md(tmp_vault_dir, "ticket.md", "# Ticket\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="ticket.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0001"},
        )
    )

    with pytest.raises(Tier3SchemaViolationError):
        await tier3_metadata_service._update_metadata(
            initial.document.id,
            UpdateMetadataRequest(tier3_metadata=Tier3Patch(set={"ticket_id": "BAD-FORMAT"})),
            modified_by="tester",
        )


async def test_update_metadata_with_changing_doc_type_uses_new_schema(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store
):
    """When the caller updates both doc_type and tier3_metadata in one
    call, post-merge validation runs against the new doc_type's schema."""
    _write_md(tmp_vault_dir, "doc.md", "# Doc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="doc.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0001"},
        )
    )

    # Reclassify as failure_record AND patch tier3 to fit failure_record's
    # schema. The merged dict carries the legacy ticket_id (which would
    # fail failure_record validation) -- so unset it in the same call.
    await tier3_metadata_service._update_metadata(
        initial.document.id,
        UpdateMetadataRequest(
            doc_type="failure_record",
            tier3_metadata=Tier3Patch(set={"severity": "high"}, unset=["ticket_id"]),
        ),
        modified_by="tester",
    )

    fresh = await graph_store.get_document(initial.document.id)
    assert fresh.doc_type == "failure_record"
    assert fresh.tier3_metadata == {"severity": "high"}


# ---------------------------------------------------------------------------
# Doc_type change paired with tier3 ops must reject when merged
# tier3 carries keys absent from the new doc_type's schema. The caller is
# told the exact `unset` list needed; storage is untouched.
# ---------------------------------------------------------------------------


async def test_update_metadata_doc_type_change_with_stale_keys_rejects_before_write(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store
):
    """TS-1: stale keys raise Tier3DocTypeChangeStaleKeysError; storage
    unchanged. Anti-coincidental: re-read confirms no commit happened."""
    _write_md(tmp_vault_dir, "doc.md", "# Doc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="doc.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0001", "ticket_priority": "high"},
        )
    )

    with pytest.raises(Tier3DocTypeChangeStaleKeysError) as excinfo:
        await tier3_metadata_service._update_metadata(
            initial.document.id,
            UpdateMetadataRequest(
                doc_type="failure_record",
                tier3_metadata=Tier3Patch(set={"severity": "high"}),
            ),
            modified_by="tester",
        )
    assert excinfo.value.code == "tier3_doc_type_change_stale_keys"
    assert excinfo.value.detail["previous_doc_type"] == "ticket"
    assert excinfo.value.detail["new_doc_type"] == "failure_record"
    assert excinfo.value.detail["stale_keys"] == ["ticket_id", "ticket_priority"]

    # Anti-coincidental storage probe: the row must be unchanged.
    fresh = await graph_store.get_document(initial.document.id)
    assert fresh.doc_type == "ticket"
    assert fresh.tier3_metadata == {"ticket_id": "T-0001", "ticket_priority": "high"}


async def test_update_metadata_tier3_only_patch_does_not_trigger_stale_keys_check(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service
):
    """TS-3: no doc_type change in the request -- the new pre-check must
    not fire even when the set value violates the existing schema. The
    existing tier3_schema_violation path is what catches the bad value."""
    _write_md(tmp_vault_dir, "doc.md", "# Doc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="doc.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0001"},
        )
    )

    with pytest.raises(Tier3SchemaViolationError) as excinfo:
        await tier3_metadata_service._update_metadata(
            initial.document.id,
            UpdateMetadataRequest(
                tier3_metadata=Tier3Patch(set={"ticket_priority": "purple"}),
            ),
            modified_by="tester",
        )
    # Anti-coincidental: assert the OLD code fired, not the new one.
    assert excinfo.value.code == "tier3_schema_violation"
    assert not isinstance(excinfo.value, Tier3DocTypeChangeStaleKeysError)


async def test_update_metadata_doc_type_set_to_current_value_does_not_trigger_stale_keys_check(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store
):
    """TS-4: explicitly setting doc_type to its current value is a no-op
    for the type; the new pre-check must use equality vs. presence."""
    _write_md(tmp_vault_dir, "doc.md", "# Doc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="doc.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0001"},
        )
    )

    await tier3_metadata_service._update_metadata(
        initial.document.id,
        UpdateMetadataRequest(
            doc_type="ticket",
            tier3_metadata=Tier3Patch(set={"ticket_priority": "high"}),
        ),
        modified_by="tester",
    )

    fresh = await graph_store.get_document(initial.document.id)
    assert fresh.doc_type == "ticket"
    assert fresh.tier3_metadata == {"ticket_id": "T-0001", "ticket_priority": "high"}


async def test_update_metadata_doc_type_change_to_no_schema_doc_type_rejects_stale_keys(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store
):
    """TS-4b: broadened scope -- when the new doc_type has no metadata_schema,
    every merged key is stale. Caller is told exactly what to unset rather
    than receiving the less-actionable no-schema variant of
    tier3_schema_violation."""
    _write_md(tmp_vault_dir, "doc.md", "# Doc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="doc.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0001", "ticket_priority": "high"},
        )
    )

    with pytest.raises(Tier3DocTypeChangeStaleKeysError) as excinfo:
        await tier3_metadata_service._update_metadata(
            initial.document.id,
            UpdateMetadataRequest(
                doc_type="misc",
                tier3_metadata=Tier3Patch(set={"orphan": "value"}),
            ),
            modified_by="tester",
        )
    assert excinfo.value.detail["new_doc_type"] == "misc"
    # All merged keys are stale because misc has no metadata_schema.
    assert excinfo.value.detail["stale_keys"] == [
        "orphan",
        "ticket_id",
        "ticket_priority",
    ]

    # Anti-coincidental storage probe.
    fresh = await graph_store.get_document(initial.document.id)
    assert fresh.doc_type == "ticket"
    assert fresh.tier3_metadata == {"ticket_id": "T-0001", "ticket_priority": "high"}


async def test_update_metadata_doc_type_change_falls_through_to_schema_validator(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store
):
    """TS-6: caller correctly unsets stale keys but supplies a value that
    violates the new schema's enum. The new pre-check passes (no stale
    keys); the existing tier3_schema_violation path fires. Proves the
    pre-check does not swallow downstream validation."""
    _write_md(tmp_vault_dir, "doc.md", "# Doc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="doc.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0001"},
        )
    )

    with pytest.raises(Tier3SchemaViolationError) as excinfo:
        await tier3_metadata_service._update_metadata(
            initial.document.id,
            UpdateMetadataRequest(
                doc_type="failure_record",
                tier3_metadata=Tier3Patch(
                    set={"severity": "purple"},  # not in failure_record enum
                    unset=["ticket_id"],
                ),
            ),
            modified_by="tester",
        )
    assert excinfo.value.code == "tier3_schema_violation"
    assert not isinstance(excinfo.value, Tier3DocTypeChangeStaleKeysError)
    assert excinfo.value.detail["doc_type"] == "failure_record"
    # Storage unchanged (pre-write validation).
    fresh = await graph_store.get_document(initial.document.id)
    assert fresh.doc_type == "ticket"
    assert fresh.tier3_metadata == {"ticket_id": "T-0001"}


# ---------------------------------------------------------------------------
# Close the two reconciliation gaps left open by.
#
# Wart 1: a doc_type change with no Tier3Patch must still revalidate the
# stored tier3 against the new schema. added the stale-keys
# pre-check inside the `if request.tier3_metadata is not None:` guard, so
# the gap is the no-patch path -- stored stale keys silently survived.
#
# Wart 2: an empty merged tier3 dict must be accepted against a no-schema
# doc_type. _validate_tier3 used to raise the no-schema variant of
# tier3_schema_violation unconditionally when the validator was None,
# blocking reclassification to no-schema via `Tier3Patch.unset` of every
# key.
# ---------------------------------------------------------------------------


async def test_update_metadata_doc_type_change_no_tier3_patch_with_stale_stored_keys_rejects(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store
):
    """Wart 1 primary: doc_type change with no Tier3Patch and stale
    stored keys raises Tier3DocTypeChangeStaleKeysError pre-write. Anti-
    coincidental probe: storage unchanged. Specific error code (not a
    generic tier3_schema_violation) -- the caller is told which keys to
    unset, mirroring the error envelope."""
    _write_md(tmp_vault_dir, "doc.md", "# Doc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="doc.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0001", "ticket_priority": "high"},
        )
    )

    with pytest.raises(Tier3DocTypeChangeStaleKeysError) as excinfo:
        await tier3_metadata_service._update_metadata(
            initial.document.id,
            UpdateMetadataRequest(doc_type="failure_record"),
            modified_by="tester",
        )
    assert excinfo.value.code == "tier3_doc_type_change_stale_keys"
    assert excinfo.value.detail["previous_doc_type"] == "ticket"
    assert excinfo.value.detail["new_doc_type"] == "failure_record"
    assert excinfo.value.detail["stale_keys"] == ["ticket_id", "ticket_priority"]
    assert excinfo.value.detail["merged_tier3_keys"] == ["ticket_id", "ticket_priority"]

    # Anti-coincidental storage probe.
    fresh = await graph_store.get_document(initial.document.id)
    assert fresh.doc_type == "ticket"
    assert fresh.tier3_metadata == {"ticket_id": "T-0001", "ticket_priority": "high"}


async def test_update_metadata_doc_type_change_no_tier3_patch_with_empty_stored_passes(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store
):
    """Wart 1 happy path + Wart 2 happy path: doc_type change to a
    no-schema target with empty stored tier3 and no Tier3Patch must
    succeed. Anti-coincidental: doc_type flipped on disk AND stored
    tier3 was not overwritten by an empty merged dict (write discipline)."""
    _write_md(tmp_vault_dir, "doc.md", "# Doc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="doc.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "bare_record"},
            # No tier3_metadata at ingest -- stored as None.
        )
    )
    stored_before = await graph_store.get_document(initial.document.id)
    assert stored_before.doc_type == "bare_record"

    await tier3_metadata_service._update_metadata(
        initial.document.id,
        UpdateMetadataRequest(doc_type="misc"),
        modified_by="tester",
    )

    fresh = await graph_store.get_document(initial.document.id)
    assert fresh.doc_type == "misc"
    # Write discipline: tier3 was not supplied, so the merged dict must
    # not have been written back -- whatever was stored before survives.
    assert fresh.tier3_metadata == stored_before.tier3_metadata


async def test_update_metadata_doc_type_change_to_no_schema_with_unset_all_succeeds(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store
):
    """Wart 2 primary: reclassify to a no-schema doc_type while
    unsetting every legacy tier3 key. The merged dict is {}; the new
    doc_type has no schema. This must succeed. Without the fix, the
    call raises tier3_schema_violation with the 'no metadata_schema
    declared' message -- that pre-fix shape is the negative-baseline
    probe."""
    _write_md(tmp_vault_dir, "doc.md", "# Doc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="doc.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0001", "ticket_priority": "high"},
        )
    )

    await tier3_metadata_service._update_metadata(
        initial.document.id,
        UpdateMetadataRequest(
            doc_type="misc",
            tier3_metadata=Tier3Patch(unset=["ticket_id", "ticket_priority"]),
        ),
        modified_by="tester",
    )

    fresh = await graph_store.get_document(initial.document.id)
    assert fresh.doc_type == "misc"
    assert fresh.tier3_metadata == {}


async def test_update_metadata_doc_type_change_to_no_schema_with_nonempty_merged_still_rejects(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store
):
    """Wart 2 anti-coincidental: the empty-dict carve-out must not
    loosen rejection for non-empty payloads against no-schema doc_types.
    The doc_type-change path's stale-keys pre-check fires first because
    the new doc_type has no allowed properties (allowed == set()), so a
    non-empty merged dict surfaces Tier3DocTypeChangeStaleKeysError --
    NOT Tier3SchemaViolationError. This pins down ordering."""
    _write_md(tmp_vault_dir, "doc.md", "# Doc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="doc.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0001", "ticket_priority": "high"},
        )
    )

    with pytest.raises(Tier3DocTypeChangeStaleKeysError) as excinfo:
        await tier3_metadata_service._update_metadata(
            initial.document.id,
            UpdateMetadataRequest(
                doc_type="misc",
                tier3_metadata=Tier3Patch(unset=["ticket_id"]),
            ),
            modified_by="tester",
        )
    assert excinfo.value.detail["new_doc_type"] == "misc"
    assert excinfo.value.detail["stale_keys"] == ["ticket_priority"]

    # Anti-coincidental storage probe.
    fresh = await graph_store.get_document(initial.document.id)
    assert fresh.doc_type == "ticket"
    assert fresh.tier3_metadata == {"ticket_id": "T-0001", "ticket_priority": "high"}


async def test_update_metadata_no_doc_type_change_set_against_no_schema_still_rejects(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store
):
    """Wart 2 scope guard: the empty-dict carve-out in
    _validate_tier3 must not silently allow tier3 set-ops against
    no-schema doc_types in the non-doc-type-change path. A Tier3Patch
    with set ops produces a non-empty merged dict, which must still
    raise tier3_schema_violation with the no-schema-declared message."""
    _write_md(tmp_vault_dir, "doc.md", "# Doc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="doc.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "misc"},
            # No tier3 at ingest -- misc has no schema.
        )
    )

    with pytest.raises(Tier3SchemaViolationError) as excinfo:
        await tier3_metadata_service._update_metadata(
            initial.document.id,
            UpdateMetadataRequest(
                tier3_metadata=Tier3Patch(set={"foo": "bar"}),
            ),
            modified_by="tester",
        )
    assert excinfo.value.code == "tier3_schema_violation"
    assert excinfo.value.detail["doc_type"] == "misc"
    assert "no metadata_schema declared" in excinfo.value.detail["message"]

    # Anti-coincidental storage probe.
    fresh = await graph_store.get_document(initial.document.id)
    assert fresh.doc_type == "misc"


async def test_update_metadata_with_no_tier3_leaves_stored_value_untouched(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store
):
    _write_md(tmp_vault_dir, "doc.md", "# Doc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="doc.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0042"},
        )
    )

    # Update something else; tier3 must survive.
    await tier3_metadata_service._update_metadata(
        initial.document.id,
        UpdateMetadataRequest(title="New Title"),
        modified_by="tester",
    )
    fresh = await graph_store.get_document(initial.document.id)
    assert fresh.tier3_metadata == {"ticket_id": "T-0042"}


async def test_update_metadata_tier3_unset_only_removes_named_keys(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store
):
    """`unset` without `set`: named keys disappear; others survive."""
    _write_md(tmp_vault_dir, "ticket.md", "# Ticket\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="ticket.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0001", "ticket_priority": "low"},
        )
    )

    await tier3_metadata_service._update_metadata(
        initial.document.id,
        UpdateMetadataRequest(tier3_metadata=Tier3Patch(unset=["ticket_priority"])),
        modified_by="tester",
    )

    fresh = await graph_store.get_document(initial.document.id)
    assert fresh.tier3_metadata == {"ticket_id": "T-0001"}


async def test_update_metadata_tier3_unset_absent_key_conflict(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service
):
    """Strict conflict: unsetting an absent tier3 key raises
    Tier3UnsetConflictError with current_tier3_keys in the detail."""
    _write_md(tmp_vault_dir, "ticket.md", "# Ticket\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="ticket.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0001"},
        )
    )

    with pytest.raises(Tier3UnsetConflictError) as excinfo:
        await tier3_metadata_service._update_metadata(
            initial.document.id,
            UpdateMetadataRequest(tier3_metadata=Tier3Patch(unset=["never_was_here"])),
            modified_by="tester",
        )
    assert excinfo.value.code == "tier3_unset_conflict"
    assert excinfo.value.detail["keys"] == ["never_was_here"]
    assert "ticket_id" in excinfo.value.detail["current_tier3_keys"]


async def test_update_metadata_tier3_set_overwrites_existing_key(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store
):
    """`set` is literal: overwriting an existing key is not a conflict."""
    _write_md(tmp_vault_dir, "ticket.md", "# Ticket\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="ticket.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0001", "ticket_priority": "low"},
        )
    )

    await tier3_metadata_service._update_metadata(
        initial.document.id,
        UpdateMetadataRequest(tier3_metadata=Tier3Patch(set={"ticket_priority": "high"})),
        modified_by="tester",
    )
    fresh = await graph_store.get_document(initial.document.id)
    assert fresh.tier3_metadata["ticket_priority"] == "high"


async def test_update_metadata_tier3_post_merge_validation_catches_set_of_unknown_property(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service
):
    """Post-merge validation: a `set` op that introduces an additional
    property to a strict (additionalProperties=false) schema surfaces
    as Tier3SchemaViolationError. The check fires on the merged dict
    -- the patch payload alone looks fine, but the merge produces a
    schema-invalid state."""
    _write_md(tmp_vault_dir, "fr.md", "# Failure\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="fr.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "failure_record"},
            tier3_metadata={"severity": "high"},
        )
    )

    with pytest.raises(Tier3SchemaViolationError):
        await tier3_metadata_service._update_metadata(
            initial.document.id,
            UpdateMetadataRequest(tier3_metadata=Tier3Patch(set={"unknown_field": "x"})),
            modified_by="tester",
        )


# ---------------------------------------------------------------------------
# Retrieval filter: catalog mode
# ---------------------------------------------------------------------------


async def _seed_failure_records(tmp_vault_dir, ingestion_service):
    """Create three failure_record docs with distinct tier3 fields and one
    ticket as a foil."""
    _write_md(tmp_vault_dir, "f1.md", "# F1\n\nBody.")
    _write_md(tmp_vault_dir, "f2.md", "# F2\n\nBody.")
    _write_md(tmp_vault_dir, "f3.md", "# F3\n\nBody.")
    _write_md(tmp_vault_dir, "t1.md", "# T1\n\nBody.")

    d1 = await ingestion_service.ingest(
        IngestRequest(
            source="f1.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "failure_record"},
            tier3_metadata={"severity": "high", "fix_commit": "abc1234"},
        )
    )
    d2 = await ingestion_service.ingest(
        IngestRequest(
            source="f2.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "failure_record"},
            tier3_metadata={"severity": "high", "fix_commit": None},
        )
    )
    d3 = await ingestion_service.ingest(
        IngestRequest(
            source="f3.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "failure_record"},
            tier3_metadata={"severity": "low", "fix_commit": None},
        )
    )
    d4 = await ingestion_service.ingest(
        IngestRequest(
            source="t1.md",
            source_type=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-9999", "ticket_priority": "high"},
        )
    )
    return d1.document, d2.document, d3.document, d4.document


async def test_catalog_filter_by_tier3_equality(
    tmp_vault_dir, tier3_ingestion_service, tier3_retrieval_service
):
    d1, d2, d3, _d4 = await _seed_failure_records(tmp_vault_dir, tier3_ingestion_service)

    response = await tier3_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            filters=RetrievalFilters(tier3_metadata={"severity": "high"}),
        )
    )
    returned_ids = {hit.document.id for hit in response.results}
    assert d1.id in returned_ids
    assert d2.id in returned_ids
    assert d3.id not in returned_ids


async def test_catalog_filter_by_tier3_null_field(
    tmp_vault_dir, tier3_ingestion_service, tier3_retrieval_service
):
    """A None filter value matches stored fields that are null OR absent."""
    d1, d2, d3, d4 = await _seed_failure_records(tmp_vault_dir, tier3_ingestion_service)

    response = await tier3_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            filters=RetrievalFilters(
                doc_type="failure_record",
                tier3_metadata={"fix_commit": None},
            ),
        )
    )
    returned_ids = {hit.document.id for hit in response.results}
    assert d2.id in returned_ids
    assert d3.id in returned_ids
    assert d1.id not in returned_ids  # has fix_commit
    assert d4.id not in returned_ids  # different doc_type


async def test_catalog_filter_by_tier3_ands_with_other_filters(
    tmp_vault_dir, tier3_ingestion_service, tier3_retrieval_service
):
    d1, d2, _d3, _d4 = await _seed_failure_records(tmp_vault_dir, tier3_ingestion_service)

    response = await tier3_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            filters=RetrievalFilters(
                doc_type="failure_record",
                tier3_metadata={"severity": "high"},
            ),
        )
    )
    returned_ids = {hit.document.id for hit in response.results}
    assert returned_ids == {d1.id, d2.id}


async def test_catalog_filter_tier3_pagination_after_post_filter(
    tmp_vault_dir, tier3_ingestion_service, tier3_retrieval_service
):
    """Total count and slicing must be applied AFTER the tier3 post-filter
    so the caller sees a coherent paginator."""
    await _seed_failure_records(tmp_vault_dir, tier3_ingestion_service)

    response = await tier3_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            filters=RetrievalFilters(
                doc_type="failure_record", tier3_metadata={"severity": "high"}
            ),
            limit=1,
            offset=0,
        )
    )
    assert len(response.results) == 1
    assert response.total_available == 2


# ---------------------------------------------------------------------------
# _tier3_matches helper — direct unit coverage of the matching predicate
# ---------------------------------------------------------------------------


def test_tier3_matches_empty_filter_matches_anything():
    assert _tier3_matches(None, {}) is True
    assert _tier3_matches({"x": 1}, {}) is True


def test_tier3_matches_none_filter_matches_absent_or_null():
    assert _tier3_matches(None, {"x": None}) is True
    assert _tier3_matches({}, {"x": None}) is True
    assert _tier3_matches({"x": None}, {"x": None}) is True
    assert _tier3_matches({"x": "value"}, {"x": None}) is False


def test_tier3_matches_exact_value():
    assert _tier3_matches({"x": "value"}, {"x": "value"}) is True
    assert _tier3_matches({"x": "other"}, {"x": "value"}) is False


def test_tier3_matches_ands_multiple_keys():
    stored = {"x": "a", "y": "b"}
    assert _tier3_matches(stored, {"x": "a", "y": "b"}) is True
    assert _tier3_matches(stored, {"x": "a", "y": "c"}) is False


# ---------------------------------------------------------------------------
# SQL pushdown of tier3 filters into json_extract predicates
# ---------------------------------------------------------------------------


async def test_t0075_catalog_filter_unindexed_tier3_value_returns_correct_subset(
    tmp_vault_dir, tier3_ingestion_service, tier3_retrieval_service
):
    """JSON1 pushdown must work for tier3 fields that have no expression
    index. ``fix_commit`` is declared on failure_record's metadata_schema
    but is not one of the three indexed canonical keys (ticket_id,
    failure_id, tool_name) -- the predicate falls through to a table
    scan and must still return the right rows."""
    d1, _d2, _d3, _d4 = await _seed_failure_records(tmp_vault_dir, tier3_ingestion_service)

    response = await tier3_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            filters=RetrievalFilters(
                doc_type="failure_record",
                tier3_metadata={"fix_commit": "abc1234"},
            ),
        )
    )
    returned_ids = {hit.document.id for hit in response.results}
    assert returned_ids == {d1.id}


async def test_t0075_catalog_filter_unknown_tier3_key_raises_against_doc_type_schema(
    tmp_vault_dir, tier3_ingestion_service, tier3_retrieval_service
):
    """A tier3 key not declared in the resolved doc_type's metadata_schema
    must raise Tier3SchemaViolationError before the query reaches SQL.
    A typo'd key would otherwise silently match zero rows -- the AC
    explicitly says this should error so the caller knows."""
    await _seed_failure_records(tmp_vault_dir, tier3_ingestion_service)

    with pytest.raises(Tier3SchemaViolationError) as excinfo:
        await tier3_retrieval_service.discover(
            DiscoverRequest(
                mode=RetrievalMode.CATALOG,
                filters=RetrievalFilters(
                    doc_type="ticket",
                    tier3_metadata={"tickett_id": "T-0001"},  # typo
                ),
            )
        )
    assert excinfo.value.detail["doc_type"] == "ticket"
    assert "tickett_id" in str(excinfo.value.detail.get("message", "")) or "tickett_id" in str(
        excinfo.value.detail.get("path", "")
    )


async def test_t0075_catalog_filter_pushes_tier3_into_sql_not_python(
    tmp_vault_dir,
    tier3_ingestion_service,
    tier3_retrieval_service,
    graph_store,
    monkeypatch,
):
    """The optimization gate: the SQL emitted for a catalog-mode tier3
    filter must carry the jsonb ``->>`` predicate, and the legacy
    wide-fetch / Python-post-filter phase must not fire.

    Captured by wrapping the store's row/scalar fetch seams. If this
    test breaks but the behavior-equivalence tests still pass, the
    optimization has been silently reverted to the 10M-row fallback:
    those tests cannot tell a SQL predicate from an in-Python filter
    over an unbounded fetch.
    """
    await _seed_failure_records(tmp_vault_dir, tier3_ingestion_service)

    captured: list[tuple[str, int | None]] = []

    real_fetch_rows = graph_store._fetch_rows
    real_fetch_scalar = graph_store._fetch_scalar

    async def tracing_fetch_rows(sql, params=()):
        rows = await real_fetch_rows(sql, params)
        captured.append((sql, len(rows)))
        return rows

    async def tracing_fetch_scalar(sql, params=()):
        captured.append((sql, None))
        return await real_fetch_scalar(sql, params)

    monkeypatch.setattr(graph_store, "_fetch_rows", tracing_fetch_rows)
    monkeypatch.setattr(graph_store, "_fetch_scalar", tracing_fetch_scalar)

    response = await tier3_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            filters=RetrievalFilters(
                doc_type="failure_record",
                tier3_metadata={"severity": "high"},
            ),
        )
    )

    joined = "\n".join(sql for sql, _ in captured)
    assert "tier3_metadata->>'severity'" in joined, joined
    # The legacy wide-fetch issued a `LIMIT 10000000` on the documents
    # table. The new path uses the request limit (default 10).
    assert "LIMIT 10000000" not in joined, joined
    # No Python-side post-filter: the row fetch carrying the tier3
    # predicate returns exactly the rows the caller receives. A wide
    # fetch narrowed in Python would return more SQL rows than results.
    predicate_row_counts = [
        n for sql, n in captured if n is not None and "tier3_metadata->>'severity'" in sql
    ]
    assert predicate_row_counts, "no row fetch carried the tier3 predicate"
    assert predicate_row_counts == [len(response.results)]
    assert len(response.results) == 2  # the two high-severity seeds


async def test_t0075_catalog_filter_rejects_sql_unsafe_tier3_key(
    tmp_vault_dir, tier3_ingestion_service, tier3_retrieval_service
):
    """Defense-in-depth: a tier3 key that contains characters outside
    [A-Za-z0-9_] must be rejected before reaching the SQL builder.
    The whitelist-against-metadata_schema check at the service layer
    will already block this, but the storage-layer format check is the
    last-line fence that guarantees no caller-supplied string ever
    interpolates into a JSON path without being validated.
    """
    await _seed_failure_records(tmp_vault_dir, tier3_ingestion_service)

    with pytest.raises((Tier3SchemaViolationError, ValueError)):
        await tier3_retrieval_service.discover(
            DiscoverRequest(
                mode=RetrievalMode.CATALOG,
                filters=RetrievalFilters(
                    tier3_metadata={"severity') OR 1=1 --": "x"},
                ),
            )
        )
