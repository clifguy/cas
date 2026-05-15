"""Cross-surface tests for tier3_metadata (T-0004 Phase 1).

Coverage:

* VaultConfig.model_post_init builds a validator-cache keyed by doc_type.
* VaultConfig.tier3_validator returns None for doc_types with no
  metadata_schema; the service layer treats that as 400 (strict
  no-loose-mode per the T-0004 design).
* sage_ingest validates tier3_metadata against the resolved doc_type's
  schema BEFORE inserting the document; failures raise
  Tier3SchemaViolationError without creating a row.
* sage_update_metadata takes Tier3Patch (`{set, unset}`) and applies
  the patch in memory; the merged result is validated against the
  (possibly newly-set) doc_type. Strict-conflict on `unset` of an
  absent key.
* sage_discover catalog and semantic filters honor the new tier3 clause;
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
        "source_adapters": {"adapters": [{"source_type": "markdown", "enabled": True}]},
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
        adapter=SourceType.MARKDOWN,
        metadata={"doc_type": "misc"},
        tier3_metadata={"anything": 1},
    )

    with pytest.raises(Tier3SchemaViolationError) as excinfo:
        await tier3_ingestion_service.ingest(request)
    assert excinfo.value.code == "tier3_schema_violation"
    assert excinfo.value.detail["doc_type"] == "misc"
    assert "no metadata_schema declared" in excinfo.value.detail["message"]


async def test_ingest_with_invalid_tier3_rejects_with_path(tmp_vault_dir, tier3_ingestion_service):
    _write_md(tmp_vault_dir, "fr.md", "# Failure\n\nBody.")

    request = IngestRequest(
        source="fr.md",
        adapter=SourceType.MARKDOWN,
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
        adapter=SourceType.MARKDOWN,
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
                adapter=SourceType.MARKDOWN,
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
            adapter=SourceType.MARKDOWN,
            metadata={"doc_type": "failure_record"},
            tier3_metadata={"severity": "low", "fix_commit": "abc123"},
        )
    )

    await tier3_metadata_service.update_metadata(
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
            adapter=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0001"},
        )
    )

    with pytest.raises(Tier3SchemaViolationError):
        await tier3_metadata_service.update_metadata(
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
            adapter=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0001"},
        )
    )

    # Reclassify as failure_record AND patch tier3 to fit failure_record's
    # schema. The merged dict carries the legacy ticket_id (which would
    # fail failure_record validation) -- so unset it in the same call.
    await tier3_metadata_service.update_metadata(
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


async def test_update_metadata_with_no_tier3_leaves_stored_value_untouched(
    tmp_vault_dir, tier3_ingestion_service, tier3_metadata_service, graph_store
):
    _write_md(tmp_vault_dir, "doc.md", "# Doc\n\nBody.")
    initial = await tier3_ingestion_service.ingest(
        IngestRequest(
            source="doc.md",
            adapter=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0042"},
        )
    )

    # Update something else; tier3 must survive.
    await tier3_metadata_service.update_metadata(
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
            adapter=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0001", "ticket_priority": "low"},
        )
    )

    await tier3_metadata_service.update_metadata(
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
            adapter=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0001"},
        )
    )

    with pytest.raises(Tier3UnsetConflictError) as excinfo:
        await tier3_metadata_service.update_metadata(
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
            adapter=SourceType.MARKDOWN,
            metadata={"doc_type": "ticket"},
            tier3_metadata={"ticket_id": "T-0001", "ticket_priority": "low"},
        )
    )

    await tier3_metadata_service.update_metadata(
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
            adapter=SourceType.MARKDOWN,
            metadata={"doc_type": "failure_record"},
            tier3_metadata={"severity": "high"},
        )
    )

    with pytest.raises(Tier3SchemaViolationError):
        await tier3_metadata_service.update_metadata(
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
            adapter=SourceType.MARKDOWN,
            metadata={"doc_type": "failure_record"},
            tier3_metadata={"severity": "high", "fix_commit": "abc1234"},
        )
    )
    d2 = await ingestion_service.ingest(
        IngestRequest(
            source="f2.md",
            adapter=SourceType.MARKDOWN,
            metadata={"doc_type": "failure_record"},
            tier3_metadata={"severity": "high", "fix_commit": None},
        )
    )
    d3 = await ingestion_service.ingest(
        IngestRequest(
            source="f3.md",
            adapter=SourceType.MARKDOWN,
            metadata={"doc_type": "failure_record"},
            tier3_metadata={"severity": "low", "fix_commit": None},
        )
    )
    d4 = await ingestion_service.ingest(
        IngestRequest(
            source="t1.md",
            adapter=SourceType.MARKDOWN,
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
            filters=RetrievalFilters(tier3={"severity": "high"}),
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
                tier3={"fix_commit": None},
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
                tier3={"severity": "high"},
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
            filters=RetrievalFilters(doc_type="failure_record", tier3={"severity": "high"}),
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
