"""MCP adapter tests.

Verifies that each MCP tool correctly translates to the underlying SAGE
service calls, returns well-formed JSON, and propagates errors as
structured error responses rather than exceptions.

Tests call the tool functions directly (bypassing MCP transport) with
a pre-initialized vault registry, matching how the existing test suite
tests services directly rather than through HTTP.
"""

import asyncio
import json

import pytest

import sage.mcp_server as _mcp
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.api.errors import SAGEError
from sage.config import VaultConfig
from sage.mcp_init import initialize_services
from sage.mcp_server import (
    sage_admin_migrate_vault,
    sage_check_preconditions,
    sage_discover,
    sage_get_document,
    sage_ingest,
    sage_link,
    sage_parse_filename,
    sage_reabstract,
    sage_read_projection,
    sage_refresh_views,
    sage_reload_vault,
    sage_set_lifecycle,
    sage_traverse,
    sage_update_metadata,
    sage_update_vault_config,
    sage_vault_stats,
)
from sage.mcp_server import (
    sage_unlink as _sage_unlink_tool,
)
from sage.models.enums import EdgeType as _EdgeType
from sage.services._dry_run import DRY_RUN_SENTINEL_EDGE_ID as _DRY_RUN_SENTINEL_EDGE_ID
from tests.sage.conftest import initialize_services_for_test
from tests.sage.test_ingestion_metadata_extraction import _pim_vault_config_dict


@pytest.fixture
async def vault_services(minimal_vault_config_dict, tmp_vault_dir):
    """Initialize SAGE services and register them in the MCP vault registry."""
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    async with initialize_services_for_test(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        _mcp._vaults["test_vault"] = services

        # Create a test source file
        sources = tmp_vault_dir / "sources"
        test_dir = sources / "test"
        test_dir.mkdir(parents=True, exist_ok=True)
        (test_dir / "sample.md").write_text("# Sample Document\n\nSample content.")
        (test_dir / "second.md").write_text("# Second Document\n\nDifferent content.")

        try:
            yield services
        finally:
            await asyncio.sleep(0.5)
            _mcp._vaults.pop("test_vault", None)


def _parse(result: str | dict) -> dict:
    """Parse a tool's result (dict or JSON string)."""
    if isinstance(result, dict):
        return result
    return json.loads(result)


# ---------------------------------------------------------------------------
# Vault routing
# ---------------------------------------------------------------------------


async def test_unknown_vault_returns_error(vault_services):
    result = _parse(await sage_get_document("nonexistent_vault", "deadbeef_doc"))
    assert result["error"] == "unknown_vault"
    assert "nonexistent_vault" in result["message"]


async def test_unknown_vault_lists_available(vault_services):
    result = _parse(await sage_get_document("nonexistent_vault", "deadbeef_doc"))
    assert "test_vault" in result["message"]


# ---------------------------------------------------------------------------
# _error_response: distinguish vault-routing failures from other ValueErrors.
# Pre-fix, every non-SAGEError ValueError was labeled `unknown_vault`,
# which masked unrelated bugs (e.g., a date-parse failure deep in traverse).
# ---------------------------------------------------------------------------


def test_error_response_value_error_returns_internal_error():
    """A generic ValueError is no longer mislabeled as unknown_vault."""
    from sage.mcp_server import _error_response

    result = _error_response(ValueError("boom"))
    assert result["error"] == "internal_error"
    assert result["message"] == "boom"


def test_error_response_vault_not_found_returns_unknown_vault():
    """The unknown_vault label is reserved for actual vault-routing failures."""
    from sage.mcp_server import VaultNotFoundError, _error_response

    result = _error_response(VaultNotFoundError("Unknown vault_id: x"))
    assert result["error"] == "unknown_vault"
    assert "Unknown vault_id: x" in result["message"]


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


async def test_ingest_returns_document(vault_services):
    result = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    assert "id" in result
    assert result["source_path"] == "test/sample.md"
    assert result["source_type"] == "markdown"


async def test_ingest_duplicate_returns_error(vault_services):
    await sage_ingest("test_vault", "test/sample.md", "markdown")
    await asyncio.sleep(0.1)
    result = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    assert result["error"] == "duplicate_content"


async def test_ingest_force_bypasses_duplicate(vault_services):
    await sage_ingest("test_vault", "test/sample.md", "markdown")
    await asyncio.sleep(0.1)
    result = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown", force=True))
    assert "id" in result
    assert "error" not in result


async def test_ingest_missing_file_returns_error(vault_services):
    result = _parse(await sage_ingest("test_vault", "no/such/file.md", "markdown"))
    assert result["error"] == "source_file_not_found"


# ---------------------------------------------------------------------------
# CAS-ADR-021: sage_ingest accepts needs_review and metadata; new
# sage_parse_filename MCP tool returns parsed fields side-effect-free.
# ---------------------------------------------------------------------------


@pytest.fixture
async def pim_vault_services(tmp_vault_dir):
    """Initialize a EXAMPLE-style vault (with filename_extraction) for the
    sage_parse_filename test. Registered in the MCP vault registry under
    its config-declared id (test_metadata_vault).
    """
    config = VaultConfig.model_validate(_pim_vault_config_dict(tmp_vault_dir))
    async with initialize_services_for_test(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        _mcp._vaults["test_metadata_vault"] = services
        try:
            yield services
        finally:
            await asyncio.sleep(0.5)
            _mcp._vaults.pop("test_metadata_vault", None)


async def test_ad021_013_sage_ingest_accepts_metadata_and_needs_review(
    vault_services,
):
    """sage_ingest threads metadata + needs_review through to the
    pipeline. Default needs_review=False commits caller-supplied
    metadata as authoritative (metadata_confirmed=True).
    """
    result = _parse(
        await sage_ingest(
            "test_vault",
            "test/sample.md",
            "markdown",
            metadata={"title": "Caller Title", "doc_type": "memo"},
        )
    )
    # No error from the tool surface
    assert "error" not in result
    # Caller metadata applied to the document record
    assert result["title"] == "Caller Title"
    assert result["doc_type"] == "memo"
    # Default needs_review=False -> caller-authoritative ingest
    assert result["metadata_confirmed"] is True


async def test_ad021_014_sage_parse_filename_returns_parsed_fields(
    pim_vault_services,
):
    """sage_parse_filename returns parsed fields for a filename
    matching the vault's pattern, and creates no documents.
    """
    graph_store = pim_vault_services.graph_store

    documents_before = await graph_store.list_all_documents()
    pending_before = await graph_store.list_pending_metadata_documents()
    assert documents_before == []
    assert pending_before == []

    result = _parse(
        await sage_parse_filename(
            "test_metadata_vault",
            "2026-03-09_EXAMPLE_PV06_Claim-Set_v6.md",
            "markdown",
        )
    )

    assert result["title"] == "Claim-Set"
    assert result["project"] == "EXAMPLE"
    assert result["version_label"] == "v6.0"
    assert result["document_date"] == "2026-03-09"
    assert result["doc_type"] == "design_spec"
    assert result["codes"] == ["PV06"]

    documents_after = await graph_store.list_all_documents()
    pending_after = await graph_store.list_pending_metadata_documents()
    assert documents_after == [], "sage_parse_filename must not create document records"
    assert pending_after == [], "sage_parse_filename must not enqueue pending_metadata entries"


# ---------------------------------------------------------------------------
# Get document
# ---------------------------------------------------------------------------


async def test_get_document_returns_full_record(vault_services):
    ingest_result = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    result = _parse(await sage_get_document("test_vault", doc_id))
    assert result["id"] == doc_id
    assert result["title"] == "Sample Document"


async def test_get_document_not_found(vault_services):
    result = _parse(await sage_get_document("test_vault", "deadbeef_nonexistent"))
    assert result["error"] == "document_not_found"


# ---------------------------------------------------------------------------
# Update metadata
# ---------------------------------------------------------------------------


async def test_update_metadata_partial(vault_services):
    """Scalar fields set verbatim; tags.add appends the patch entries to
    whatever the document already carries. Strict-superset check on tags:
    under add-only patch semantics, pre-existing adapter tags survive,
    so an exact equality would over-assert; a contains-check would
    under-assert. The post-patch set must equal pre-patch ∪ {alpha, beta}.
    """
    ingest_result = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]
    pre_patch_tags = set(_parse(await sage_get_document("test_vault", doc_id))["tags"])

    # Sage_update_metadata now returns UpdateMetadataResponse
    # ({document, dry_run}); unwrap before asserting on document fields.
    result = _parse(
        await sage_update_metadata(
            "test_vault",
            doc_id,
            title="Renamed Document",
            tags={"add": ["alpha", "beta"]},
            doc_type="note",
        )
    )
    assert result["dry_run"] is False
    doc = result["document"]
    assert doc["title"] == "Renamed Document"
    assert set(doc["tags"]) == pre_patch_tags | {"alpha", "beta"}
    assert doc["doc_type"] == "note"


async def test_update_metadata_sets_document_date(vault_services):
    """sage_update_metadata accepts and persists a document_date string,
    and the value is readable via sage_get_document. Catches the wiring
    fault where a parameter is declared on the MCP tool but not threaded
    into UpdateMetadataRequest.
    """
    ingest_result = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    # Wrapper response.
    updated = _parse(
        await sage_update_metadata(
            "test_vault",
            doc_id,
            document_date="2026-04-28",
        )
    )
    assert updated["document"]["document_date"] == "2026-04-28"

    fetched = _parse(await sage_get_document("test_vault", doc_id))
    assert fetched["document_date"] == "2026-04-28"


async def test_update_metadata_invalid_doc_type(vault_services):
    ingest_result = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    result = _parse(await sage_update_metadata("test_vault", doc_id, doc_type="invalid_type"))
    assert result["error"] == "invalid_doc_type"


# ---------------------------------------------------------------------------
# Update metadata: TagsPatch flow through the MCP boundary (CAS-ADR-028)
# ---------------------------------------------------------------------------


async def test_update_metadata_tags_add_only(vault_services):
    ingest_result = _parse(
        await sage_ingest(
            "test_vault", "test/sample.md", "markdown", metadata={"tags": "alpha,beta"}
        )
    )
    doc_id = ingest_result["id"]

    # Wrapper response.
    result = _parse(await sage_update_metadata("test_vault", doc_id, tags={"add": ["gamma"]}))
    assert set(result["document"]["tags"]) == {"alpha", "beta", "gamma"}


async def test_update_metadata_tags_remove_only(vault_services):
    ingest_result = _parse(
        await sage_ingest(
            "test_vault", "test/sample.md", "markdown", metadata={"tags": "alpha,beta"}
        )
    )
    doc_id = ingest_result["id"]

    # Wrapper response.
    result = _parse(await sage_update_metadata("test_vault", doc_id, tags={"remove": ["alpha"]}))
    assert result["document"]["tags"] == ["beta"]


async def test_update_metadata_tags_add_and_remove(vault_services):
    ingest_result = _parse(
        await sage_ingest("test_vault", "test/sample.md", "markdown", metadata={"tags": "old,keep"})
    )
    doc_id = ingest_result["id"]

    # Wrapper response.
    result = _parse(
        await sage_update_metadata("test_vault", doc_id, tags={"add": ["new"], "remove": ["old"]})
    )
    assert set(result["document"]["tags"]) == {"keep", "new"}


async def test_update_metadata_tags_add_conflict(vault_services):
    """Adding a tag already present returns 400 tag_add_conflict
    carrying current_tags in the detail."""
    ingest_result = _parse(
        await sage_ingest("test_vault", "test/sample.md", "markdown", metadata={"tags": "alpha"})
    )
    doc_id = ingest_result["id"]

    result = _parse(await sage_update_metadata("test_vault", doc_id, tags={"add": ["alpha"]}))
    assert result["error"] == "tag_add_conflict"
    assert result["detail"]["tags"] == ["alpha"]
    assert "alpha" in result["detail"]["current_tags"]


async def test_update_metadata_tags_remove_conflict(vault_services):
    ingest_result = _parse(
        await sage_ingest("test_vault", "test/sample.md", "markdown", metadata={"tags": "alpha"})
    )
    doc_id = ingest_result["id"]

    result = _parse(
        await sage_update_metadata("test_vault", doc_id, tags={"remove": ["never_here"]})
    )
    assert result["error"] == "tag_remove_conflict"
    assert result["detail"]["tags"] == ["never_here"]


async def test_update_metadata_tags_legacy_form_rejected(vault_services):
    """Bare-list tags returns structured legacy_form with a worked example."""
    ingest_result = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    result = _parse(
        await sage_update_metadata("test_vault", doc_id, tags=["a", "b"])  # type: ignore[arg-type]
    )
    assert result["error"] == "legacy_form"
    assert result["detail"]["field"] == "tags"
    assert "add" in result["detail"]["example"]


async def test_update_metadata_tier3_legacy_form_rejected(vault_services):
    """Bare-dict tier3_metadata returns structured legacy_form."""
    ingest_result = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    result = _parse(
        await sage_update_metadata("test_vault", doc_id, tier3_metadata={"some_key": "value"})
    )
    assert result["error"] == "legacy_form"
    assert result["detail"]["field"] == "tier3_metadata"


async def test_update_metadata_empty_tags_patch_rejected(vault_services):
    """tags={} is degenerate; Pydantic returns a validation error."""
    ingest_result = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    result = _parse(await sage_update_metadata("test_vault", doc_id, tags={}))
    # The empty dict has no recognized op keys -- routed through Pydantic
    # validation; the MCP error envelope carries an error code or message.
    assert "error" in result


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_set_lifecycle_archive(vault_services):
    ingest_result = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    result = _parse(await sage_set_lifecycle("test_vault", doc_id, "archive"))
    assert result["document"]["lifecycle_status"] == "archived"


async def test_set_lifecycle_invalid_transition(vault_services):
    ingest_result = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    result = _parse(await sage_set_lifecycle("test_vault", doc_id, "reactivate"))
    assert result["error"] == "invalid_lifecycle_transition"
    assert "valid_actions" in result["detail"]


async def test_set_lifecycle_unknown_action(vault_services):
    ingest_result = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    result = _parse(await sage_set_lifecycle("test_vault", doc_id, "explode"))
    assert result["error"] == "invalid_action"


# Dry_run rollout closes the asymmetry with the bulk variant.
# The service layer (LifecycleService.set_lifecycle) and the
# SetLifecycleRequest/Response schemas already carry dry_run; these
# tests pin the MCP wrapper plumbing.


@pytest.mark.filterwarnings("error:Pydantic serializer warnings:UserWarning")
async def test_set_lifecycle_dry_run_archive_returns_dry_run_true_and_leaves_state(
    vault_services,
):
    """T1: dry_run=True returns dry_run=True and the would-be
    archived state, but the persisted document is still active.

    Paired with test_set_lifecycle_real_run_archive_... (positive
    control): together they catch both directions of wrapper bugs (drop
    dry_run vs. hardcode dry_run=True)."""
    ingest_result = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    response = _parse(await sage_set_lifecycle("test_vault", doc_id, "archive", dry_run=True))

    assert response["dry_run"] is True
    assert response["document"]["lifecycle_status"] == "archived"  # would-be

    persisted = _parse(await sage_get_document("test_vault", doc_id))
    assert persisted["lifecycle_status"] == "active", (
        "dry_run=True must not persist the lifecycle transition; "
        "the wrapper is dropping dry_run on the floor if this fails."
    )


async def test_set_lifecycle_real_run_archive_returns_dry_run_false_and_changes_state(
    vault_services,
):
    """T2: positive control for T1. Without dry_run, the wrapper
    must persist the transition and echo dry_run=False."""
    ingest_result = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    response = _parse(await sage_set_lifecycle("test_vault", doc_id, "archive"))

    assert response["dry_run"] is False
    persisted = _parse(await sage_get_document("test_vault", doc_id))
    assert persisted["lifecycle_status"] == "archived"


@pytest.mark.filterwarnings("error:Pydantic serializer warnings:UserWarning")
async def test_set_lifecycle_dry_run_supersede_returns_sentinel_edge_and_persists_nothing(
    vault_services,
):
    """T3: dry-run supersede populates created_edge with the
    nil-UUID sentinel id, leaves the predecessor active, and persists
    no supersedes edge.

    Anti-coincidental-pass: the sage_traverse zero-edge assertion guards
    against a wrapper that echoes dry_run=True in the envelope but
    actually persists the supersede. The sentinel id is asserted against
    the imported constant, not a literal."""
    doc_a = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await sage_ingest("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)

    response = _parse(
        await sage_set_lifecycle(
            "test_vault",
            doc_a["id"],
            "supersede",
            successor_id=doc_b["id"],
            dry_run=True,
        )
    )

    assert response["dry_run"] is True
    assert response["created_edge"] is not None
    assert response["created_edge"]["id"] == _DRY_RUN_SENTINEL_EDGE_ID
    assert response["created_edge"]["source_id"] == doc_b["id"]
    assert response["created_edge"]["target_id"] == doc_a["id"]

    persisted = _parse(await sage_get_document("test_vault", doc_a["id"]))
    assert persisted["lifecycle_status"] == "active"

    traversal = _parse(
        await sage_traverse(
            "test_vault",
            doc_b["id"],
            edge_type="supersedes",
            direction="outbound",
        )
    )
    # sage_traverse returns {start_id, nodes: [...]} where nodes is the
    # set of reachable documents (zero on dry-run since no edge exists).
    assert traversal["nodes"] == [], (
        "dry_run=True supersede must not persist a supersedes edge; "
        f"sage_traverse from {doc_b['id']} returned {traversal['nodes']!r}."
    )


async def test_set_lifecycle_dry_run_invalid_action_error_envelope_matches_real_run(
    vault_services,
):
    """T4: same-validator paired check. invalid_action error
    envelope must be identical whether dry_run is set or not — confirms
    dry_run does not skip or alter validators."""
    ingest_result = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    real = _parse(await sage_set_lifecycle("test_vault", doc_id, "explode"))
    dry = _parse(await sage_set_lifecycle("test_vault", doc_id, "explode", dry_run=True))

    assert real["error"] == "invalid_action"
    assert dry["error"] == "invalid_action"
    # Full envelope equality is stricter than a detail-only check — any
    # divergence (extra field, different message) on the dry-run path
    # fails the test. invalid_action carries no detail payload in this
    # vault, but the envelope-equality guard would catch a future change
    # that started populating one only on the real path.
    assert real == dry


# ---------------------------------------------------------------------------
# Graph operations: link
# ---------------------------------------------------------------------------


async def test_link_creates_edge(vault_services):
    doc_a = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await sage_ingest("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)

    result = _parse(
        await sage_link(
            "test_vault",
            doc_a["id"],
            doc_b["id"],
            "supersedes",
            rationale="test link",
        )
    )
    # Sage_link now returns LinkResponse-shaped {edge, created,
    # existing_rationale, dry_run}; unwrap edge for field assertions.
    assert result["dry_run"] is False
    edge = result["edge"]
    assert edge["source_id"] == doc_a["id"]
    assert edge["target_id"] == doc_b["id"]
    assert edge["edge_type"] == "supersedes"
    assert "id" in edge


async def test_link_self_referential_error(vault_services):
    doc = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.3)

    result = _parse(await sage_link("test_vault", doc["id"], doc["id"], "references"))
    assert result["error"] == "self_referential_edge"


async def test_t0080_sage_link_explicit_rationale_kind(vault_services):
    """T7. sage_link accepts an optional ``rationale_kind`` argument
    and persists it verbatim on the edge — even when the rationale text
    would otherwise derive to a different kind. Tests with a non-default
    discriminator (``version_chain``) so a system that ignored
    rationale_kind and always returned ``manual`` would fail.
    """
    doc_a = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await sage_ingest("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)

    result = _parse(
        await sage_link(
            "test_vault",
            doc_a["id"],
            doc_b["id"],
            "supersedes",
            rationale="caller knows this is from version_chain inference",
            rationale_kind="version_chain",
        )
    )
    assert result.get("error") is None, result
    # Wrapper response.
    assert result["edge"]["rationale_kind"] == "version_chain"


async def test_t0080_sage_link_derives_rationale_kind_from_prefix(vault_services):
    """T7. sage_link derives rationale_kind from the rationale text
    prefix when the caller omits the explicit argument. A
    ``[version_chain]`` prefix yields ``rationale_kind=version_chain``.
    """
    doc_a = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await sage_ingest("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)

    result = _parse(
        await sage_link(
            "test_vault",
            doc_a["id"],
            doc_b["id"],
            "supersedes",
            rationale="[version_chain] v2 supersedes v1",
        )
    )
    assert result.get("error") is None, result
    # Wrapper response.
    assert result["edge"]["rationale_kind"] == "version_chain"


async def test_t0080_sage_link_defaults_to_manual(vault_services):
    """T7. sage_link defaults rationale_kind to ``manual`` when neither
    an explicit kind nor a recognized rationale prefix is supplied.
    """
    doc_a = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await sage_ingest("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)

    result = _parse(
        await sage_link(
            "test_vault",
            doc_a["id"],
            doc_b["id"],
            "supersedes",
            rationale="just a freeform note",
        )
    )
    assert result.get("error") is None, result
    # Wrapper response.
    assert result["edge"]["rationale_kind"] == "manual"


async def test_link_idempotent_returns_created_flag(vault_services):
    """Re-calling sage_link with the same natural-key triple
    returns ``created=False`` and preserves the original rationale."""
    doc_a = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await sage_ingest("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)

    first = _parse(
        await sage_link(
            "test_vault",
            doc_a["id"],
            doc_b["id"],
            "supersedes",
            rationale="original rationale",
        )
    )
    # Wrapper-level fields (created, existing_rationale, dry_run)
    # remain at the top level; edge fields live under result["edge"].
    assert first["created"] is True
    assert first.get("existing_rationale") is None
    original_edge_id = first["edge"]["id"]

    second = _parse(
        await sage_link(
            "test_vault",
            doc_a["id"],
            doc_b["id"],
            "supersedes",
            rationale="DIFFERENT rationale on second call",
        )
    )
    assert second["created"] is False
    assert second["edge"]["id"] == original_edge_id
    # The pre-existing rationale is surfaced so callers can detect drift.
    assert second["existing_rationale"] == "original rationale"


# ---------------------------------------------------------------------------
# Graph operations: check_preconditions
# ---------------------------------------------------------------------------


async def test_check_preconditions_no_deps(vault_services):
    doc = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.3)

    result = _parse(await sage_check_preconditions("test_vault", doc["id"]))
    assert result["function_id"] == doc["id"]
    assert result["satisfied"] is True
    assert result["checks"] == []


# ---------------------------------------------------------------------------
# Graph operations: traverse
# ---------------------------------------------------------------------------


async def test_traverse_returns_nodes(vault_services):
    doc_a = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await sage_ingest("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)
    await sage_link("test_vault", doc_a["id"], doc_b["id"], "supersedes")

    result = _parse(await sage_traverse("test_vault", doc_a["id"]))
    assert result["start_id"] == doc_a["id"]
    assert len(result["nodes"]) == 1
    assert result["nodes"][0]["document"]["id"] == doc_b["id"]


async def test_traverse_no_edges(vault_services):
    doc = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.3)

    result = _parse(await sage_traverse("test_vault", doc["id"]))
    assert result["start_id"] == doc["id"]
    assert result["nodes"] == []


# ---------------------------------------------------------------------------
# Graph operations: anchor-bearing link and retracts (CAS-ADR-017, Chunk 8)
# ---------------------------------------------------------------------------


async def test_link_transitive_both_requires_anchors(vault_services):
    """covers is transitive_both; omitting anchors via MCP surfaces a 400."""
    doc_a = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await sage_ingest("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)

    # No anchor fields -> edge_anchor_policy_violation
    result = _parse(await sage_link("test_vault", doc_a["id"], doc_b["id"], "covers"))
    assert result["error"] == "edge_anchor_policy_violation"
    assert result["detail"]["resolution_policy"] == "transitive_both"

    # Same call with anchors populated -> 201 persistence
    result = _parse(
        await sage_link(
            "test_vault",
            doc_a["id"],
            doc_b["id"],
            "covers",
            source_valid_from_version=doc_a["id"],
            target_valid_from_version=doc_b["id"],
        )
    )
    # Wrapper response.
    edge = result["edge"]
    assert edge["edge_type"] == "covers"
    assert edge["resolution_policy"] == "transitive_both"
    assert edge["source_valid_from_version"] == doc_a["id"]
    assert edge["target_valid_from_version"] == doc_b["id"]


async def test_link_retracts_round_trip(vault_services):
    """Retracts a covers edge through the MCP wrapper and verifies the
    retracts edge round-trips with null target and the correct
    retracted_edge_id."""
    doc_a = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await sage_ingest("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)

    covers = _parse(
        await sage_link(
            "test_vault",
            doc_a["id"],
            doc_b["id"],
            "covers",
            source_valid_from_version=doc_a["id"],
            target_valid_from_version=doc_b["id"],
        )
    )
    # Wrapper response.
    assert "id" in covers["edge"]
    covers_edge_id = covers["edge"]["id"]

    # Bad retracted_edge_id -> retract_target_not_edge
    # Use a valid-shape UUID that doesn't exist in the store; the runtime
    # check inside graph_ops then surfaces retract_target_not_edge. A
    # malformed-shape value would short-circuit at LinkRequest validation
    # and yield a generic ValidationError instead.
    import uuid as _uuid

    bad = _parse(
        await sage_link(
            "test_vault",
            doc_a["id"],
            None,
            "retracts",
            source_valid_from_version=doc_a["id"],
            retracted_edge_id=str(_uuid.uuid4()),
        )
    )
    assert bad["error"] == "retract_target_not_edge"

    retract = _parse(
        await sage_link(
            "test_vault",
            doc_a["id"],
            None,
            "retracts",
            source_valid_from_version=doc_a["id"],
            retracted_edge_id=covers_edge_id,
        )
    )
    # Wrapper response.
    retract_edge = retract["edge"]
    assert retract_edge["edge_type"] == "retracts"
    assert retract_edge["resolution_policy"] == "none"
    assert retract_edge["retracted_edge_id"] == covers_edge_id
    assert retract_edge.get("target_id") is None


# ---------------------------------------------------------------------------
# Graph operations: traverse with debug=True (CAS-ADR-017, Chunk 8)
# ---------------------------------------------------------------------------


async def test_traverse_debug_populates_resolution_path(vault_services):
    doc_a = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await sage_ingest("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)
    await sage_link(
        "test_vault",
        doc_a["id"],
        doc_b["id"],
        "covers",
        source_valid_from_version=doc_a["id"],
        target_valid_from_version=doc_b["id"],
    )

    off = _parse(await sage_traverse("test_vault", doc_a["id"], edge_type="covers"))
    # debug defaults to False -> resolution_path is absent (exclude_none)
    assert off.get("resolution_path") is None

    on = _parse(await sage_traverse("test_vault", doc_a["id"], edge_type="covers", debug=True))
    path = on.get("resolution_path") or []
    assert any(e["event_type"] == "anchor_hit" for e in path)


# ---------------------------------------------------------------------------
# Retrieval: discover
# ---------------------------------------------------------------------------


async def test_discover_semantic(vault_services):
    await sage_ingest("test_vault", "test/sample.md", "markdown")
    await asyncio.sleep(0.5)

    result = _parse(await sage_discover("test_vault", "semantic", query="sample content"))
    assert result["mode"] == "semantic"
    assert isinstance(result["results"], list)


async def test_discover_catalog_sort_by_title_through_mcp_wrapper(vault_services):
    """Sort_by / sort_order on the MCP wrapper must reach DiscoverRequest.

    Ingest two documents with distinct titles, then verify that asc and desc
    sort_order values produce reversed orderings. Catches the wrapper silently
    dropping either parameter on the floor.
    """
    await sage_ingest("test_vault", "test/sample.md", "markdown")
    await sage_ingest("test_vault", "test/second.md", "markdown")
    await asyncio.sleep(0.3)

    asc_result = _parse(
        await sage_discover(
            "test_vault",
            mode="catalog",
            sort_by="title",
            sort_order="asc",
            limit=10,
        )
    )
    desc_result = _parse(
        await sage_discover(
            "test_vault",
            mode="catalog",
            sort_by="title",
            sort_order="desc",
            limit=10,
        )
    )

    titles_asc = [hit["document"]["title"] for hit in asc_result["results"]]
    titles_desc = [hit["document"]["title"] for hit in desc_result["results"]]

    # Precondition guard: both seeded docs surfaced so the ordering
    # assertions below are non-trivial.
    assert len(titles_asc) == 2
    assert "Sample Document" in titles_asc
    assert "Second Document" in titles_asc

    # Proves sort_by="title" reached DiscoverRequest. Default catalog
    # ordering is lifecycle_status then document_date desc; on two
    # freshly-ingested same-status docs it does not deterministically
    # alphabetize.
    assert titles_asc == sorted(titles_asc)

    # Proves sort_order reached DiscoverRequest. If sort_order were dropped,
    # both calls would reduce to the same request and this would fail.
    assert titles_asc == list(reversed(titles_desc))


async def test_discover_deterministic(vault_services):
    doc = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.5)

    result = _parse(
        await sage_discover(
            "test_vault",
            "deterministic",
            document_id=doc["id"],
            heading_path="Sample Document",
        )
    )
    assert result["mode"] == "deterministic"
    assert len(result["results"]) > 0


async def test_discover_semantic_missing_query(vault_services):
    result = _parse(await sage_discover("test_vault", "semantic"))
    assert result["error"] == "missing_query"


# ---------------------------------------------------------------------------
# Retrieval: discover — ADR-028 error envelope on parameter validation
# ---------------------------------------------------------------------------


async def test_discover_invalid_mode(vault_services):
    """Unknown mode value returns typed invalid_mode envelope, not internal_error."""
    result = _parse(await sage_discover("test_vault", mode="bogus"))
    assert result["error"] == "invalid_mode"
    assert result["detail"]["mode"] == "bogus"
    assert set(result["detail"]["valid_modes"]) == {
        "semantic",
        "keyword",
        "catalog",
        "deterministic",
    }
    assert "bogus" in result["message"]


async def test_discover_unknown_filter_key(vault_services):
    """Unknown filter key (AC: a) returns unknown_filter_key envelope
    rather than silently dropping the key."""
    result = _parse(
        await sage_discover("test_vault", mode="catalog", filters={"tickett_id": "T-0001"})
    )
    assert result["error"] == "unknown_filter_key"
    assert result["detail"]["key"] == "tickett_id"
    valid_keys = set(result["detail"]["valid_keys"])
    assert {
        "doc_type",
        "project",
        "lifecycle_status",
        "tags",
        "document_ids",
        "pipeline_status",
        "tier3_metadata",
    } <= valid_keys
    # A worked example helps the caller self-correct without a probe round-trip.
    assert "tier3_metadata" in result["detail"]["example"]


async def test_discover_invalid_filter_shape(vault_services):
    """Wrong type for a known filter key (AC: b) returns
    invalid_filter_shape envelope with the offending field named."""
    result = _parse(await sage_discover("test_vault", mode="catalog", filters={"tags": 42}))
    assert result["error"] == "invalid_filter_shape"
    assert result["detail"]["field"] == "tags"
    assert "list" in result["detail"]["expected_type"]
    assert "int" in result["detail"]["received_type"]


async def test_discover_mode_parameter_mismatch_catalog_with_heading_path(vault_services):
    """Catalog mode with heading_path (AC: c) returns
    mode_parameter_mismatch envelope. heading_path is deterministic-only."""
    result = _parse(await sage_discover("test_vault", mode="catalog", heading_path="Section 1"))
    assert result["error"] == "mode_parameter_mismatch"
    assert result["detail"]["mode"] == "catalog"
    assert result["detail"]["forbidden_param"] == "heading_path"
    assert "deterministic" in result["detail"]["allowed_modes"]


async def test_discover_mode_parameter_mismatch_deterministic_with_query(vault_services):
    """Deterministic mode with query set: deterministic does not search."""
    doc = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.5)
    result = _parse(
        await sage_discover(
            "test_vault",
            mode="deterministic",
            document_id=doc["id"],
            heading_path="Sample Document",
            query="ignored",
        )
    )
    assert result["error"] == "mode_parameter_mismatch"
    assert result["detail"]["mode"] == "deterministic"
    assert result["detail"]["forbidden_param"] == "query"


async def test_discover_semantic_missing_query_still_typed(vault_services):
    """Regression guard: the existing service-layer missing_query envelope
    must not be folded into mode_parameter_mismatch by."""
    result = _parse(await sage_discover("test_vault", "semantic"))
    assert result["error"] == "missing_query"


async def test_discover_semantic_happy_path_unchanged(vault_services):
    """Regression guard: success-path response shape is preserved."""
    await sage_ingest("test_vault", "test/sample.md", "markdown")
    await asyncio.sleep(0.5)
    result = _parse(await sage_discover("test_vault", "semantic", query="sample content"))
    assert result["mode"] == "semantic"
    assert isinstance(result["results"], list)


# ---------------------------------------------------------------------------
# Utilities: read_projection
# ---------------------------------------------------------------------------


async def test_read_projection(vault_services):
    doc = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.5)

    result = _parse(await sage_read_projection("test_vault", doc["id"]))
    assert result["document_id"] == doc["id"]
    assert "projection_text" in result
    assert len(result["projection_text"]) > 0
    assert "title" in result
    # write_to_path was not requested, so delivery fields stay null
    assert result.get("written_to") is None
    assert result.get("content_size") is None


async def test_read_projection_not_found(vault_services):
    result = _parse(await sage_read_projection("test_vault", "deadbeef_nonexistent"))
    assert result["error"] == "document_not_found"


async def test_read_projection_write_to_path_writes_file_and_returns_metadata(
    vault_services, tmp_path
):
    """Sage_read_projection(write_to_path=...) writes the projection
    text bytes to the absolute path and returns metadata only (no inline
    text). Replaces the pre-audit sage_export_projection MCP tool.
    """
    doc = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.5)
    target = tmp_path / "out.md"

    result = _parse(await sage_read_projection("test_vault", doc["id"], write_to_path=str(target)))

    assert result["document_id"] == doc["id"]
    assert result["written_to"] == str(target)
    assert result["content_size"] > 0
    # write-mode response must not double-ship the text inline
    assert result.get("projection_text") is None
    # Anti-coincidental-pass: the file must actually exist and have
    # non-empty contents matching the reported size.
    assert target.exists()
    written = target.read_bytes()
    assert len(written) == result["content_size"]
    assert len(written) > 0


async def test_read_projection_write_to_path_existing_target_errors(vault_services, tmp_path):
    doc = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.5)
    target = tmp_path / "existing.md"
    target.write_text("pre-existing")

    result = _parse(await sage_read_projection("test_vault", doc["id"], write_to_path=str(target)))

    assert result["error"] == "write_path_exists"
    # File must not have been clobbered.
    assert target.read_text() == "pre-existing"


async def test_read_projection_write_to_path_relative_errors(vault_services):
    doc = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.5)

    result = _parse(
        await sage_read_projection("test_vault", doc["id"], write_to_path="relative.md")
    )

    assert result["error"] == "write_path_invalid"


# ---------------------------------------------------------------------------
# Utilities: refresh_views
# ---------------------------------------------------------------------------


async def test_refresh_views(vault_services):
    await sage_ingest("test_vault", "test/sample.md", "markdown")

    result = _parse(await sage_refresh_views("test_vault"))
    assert result["vault_id"] == "test_vault"
    assert isinstance(result["views_generated"], int)
    assert result["views_generated"] >= 1


# ---------------------------------------------------------------------------
# Vault reload
# ---------------------------------------------------------------------------


async def test_reload_vault_reinitializes_services(vault_services):
    """Reload replaces services with a fresh instance and returns stats."""
    # Ingest a document so we can verify data survives reload
    await sage_ingest("test_vault", "test/sample.md", "markdown")
    await asyncio.sleep(0.3)

    old_services = _mcp._vaults["test_vault"]
    result = _parse(await sage_reload_vault("test_vault"))

    assert result["vault_id"] == "test_vault"
    assert result["reloaded"] is True
    assert result["document_count"] >= 1
    # Services instance should be replaced
    assert _mcp._vaults["test_vault"] is not old_services


async def test_reload_vault_closes_old_graph_store(vault_services):
    """Old GraphStore connections are closed after reload, and the
    closed store enforces the CAS-ADR-036 barrier: post-close dispatch
    raises rather than silently re-opening a connection.
    """
    old_graph_store = vault_services.graph_store
    assert old_graph_store._executor is not None

    await sage_reload_vault("test_vault")

    # Old store should be shut down
    assert old_graph_store._executor is None
    assert old_graph_store._all_connections == []

    # Barrier semantics: dispatch through the closed store's _run
    # boundary raises rather than silently degrading.
    with pytest.raises(RuntimeError, match="closed"):
        await old_graph_store.list_all_documents()


async def test_reload_vault_unknown_vault_returns_error(vault_services):
    """Reload on a nonexistent vault returns structured error."""
    result = _parse(await sage_reload_vault("nonexistent_vault"))
    assert result["error"] == "unknown_vault"
    assert "nonexistent_vault" in result["message"]


async def test_reload_vault_sees_external_changes(vault_services):
    """After external DB changes and reload, fresh services see current state.

    Simulates the core use case: data modified outside the MCP process,
    then reload picks up the new state.
    """
    # Ingest through the current services
    await sage_ingest("test_vault", "test/sample.md", "markdown")
    await asyncio.sleep(0.3)

    # Verify document is visible
    stats_before = _parse(await sage_vault_stats("test_vault"))
    assert stats_before["total_documents"] == 1

    # Simulate external modification: insert a document directly into the
    # database file, bypassing the in-process services. This mimics what
    # happens when the FastAPI server or another process writes to the DB.
    import sqlite3
    import uuid
    from datetime import datetime, timezone

    db_path = vault_services.graph_store._db_path
    conn = sqlite3.connect(str(db_path))
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO documents
           (id, title, source_path, source_type, source_content_hash,
            adapter_version, created_by, last_modified_by,
            lifecycle_status, pipeline_status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(uuid.uuid4()),
            "Externally Added",
            "external/doc.md",
            "markdown",
            "sha256:external_test_hash",
            "1.0",
            "test",
            "test",
            "active",
            "abstraction_complete",
            now,
            now,
        ),
    )
    conn.commit()
    conn.close()

    # Reload vault to pick up external changes
    await sage_reload_vault("test_vault")
    await asyncio.sleep(0.1)

    # Fresh services should see both documents
    stats_after = _parse(await sage_vault_stats("test_vault"))
    assert stats_after["total_documents"] == 2


async def test_reload_vault_failure_keeps_old_services_in_registry(vault_services, monkeypatch):
    """AC2: a failed reload leaves _vaults pointing at functional old services.

    Trap (anti-coincidental): a literal try/restore that re-installs the (closed)
    old reference would pass the identity check but fail the "graph store still
    open" assertion. Both checks must hold.
    """
    import sage.mcp_init as _mcp_init

    old = _mcp._vaults["test_vault"]

    async def failing_initialize_services(*args, **kwargs):
        raise SAGEError(
            code="schema_migration_required",
            message="simulated failure for T-0183 atomicity test",
            status_code=409,
        )

    # Patch both call sites so the test exercises the failure path whether
    # sage_reload_vault still has the inline initialize_services call (pre-refactor)
    # or delegates to reload_vault_in_registry which uses sage.mcp_init's binding
    # (post-refactor).
    monkeypatch.setattr(_mcp_init, "initialize_services", failing_initialize_services)
    monkeypatch.setattr(_mcp, "initialize_services", failing_initialize_services)

    result = _parse(await sage_reload_vault("test_vault"))

    # (a) Error envelope returned, not an exception
    assert result.get("error") == "schema_migration_required"
    assert "simulated failure" in result["message"]

    # (b) Registry slot still points at the SAME object (identity check)
    assert _mcp._vaults["test_vault"] is old

    # (c) The old services are still FUNCTIONAL — graph store is not closed.
    # The internal-state assertions match the idiom used by
    # test_reload_vault_closes_old_graph_store (above) for the closure
    # detection: _executor goes to None and _all_connections is cleared by
    # GraphStore.close(). The behavioural co-assertion below (per
    # TEST-SAGE-BH-137) confirms the post-CAS-ADR-036 dispatch barrier did
    # not engage — a successful list_all_documents() through _run is the
    # contrapositive of the close-barrier RuntimeError.
    assert old.graph_store._executor is not None, (
        "old graph_store was closed; restore-on-failure did not preserve it"
    )
    assert old.graph_store._all_connections, (
        "old graph_store has no live connections; close() was called"
    )
    live_docs = await old.graph_store.list_all_documents()
    assert isinstance(live_docs, list)


async def test_reload_vault_failure_releases_partially_allocated_resources(
    vault_services, monkeypatch
):
    """AC2 + Risk: a failed reload must not leak background threads.

    `_build_vault_timers` calls `flusher.start()` before initialize_services
    returns. If initialize_services raises after that point without
    transactional cleanup, the new vault's timing thread runs forever.

    This test asserts that the failed reload does not increase the count of
    live `sage-timing-flush` threads.
    """
    import threading

    def _count_timing_threads() -> int:
        return sum(1 for t in threading.enumerate() if t.name.startswith("sage-timing-flush"))

    pre_count = _count_timing_threads()

    # Patch UserService.bootstrap_owner to raise inside initialize_services.
    # That method runs AFTER timing thread + graph store + content store have
    # been constructed, so this exercises the late-stage cleanup path.
    from sage.services.user_service import UserService

    async def raising_bootstrap(self):
        raise SAGEError(
            code="schema_migration_required",
            message="simulated late-stage failure for T-0183 cleanup test",
            status_code=409,
        )

    monkeypatch.setattr(UserService, "bootstrap_owner", raising_bootstrap)

    result = _parse(await sage_reload_vault("test_vault"))

    # (a) Error envelope returned
    assert result.get("error") == "schema_migration_required"

    # (b) No new timing threads leaked from the failed partial initialization.
    # Brief grace period for thread.join() inside cleanup to complete.
    await asyncio.sleep(0.1)
    post_count = _count_timing_threads()
    assert post_count <= pre_count, (
        f"Timing thread leaked on failed reload: pre={pre_count}, post={post_count}"
    )


async def test_reload_vault_stops_old_timing_thread(vault_services, monkeypatch):
    """AC3 (reconciliation): MCP reload path now stops the old vault's
    timing thread on success (parity with the FastAPI path via
    reload_vault_in_registry).

    Trap (anti-coincidental): the current inline MCP code skips
    timing_thread.stop() entirely; only the registry version stops it. After
    delegation, both paths must stop the thread. The assert_called_once_with
    is the trap.
    """
    from unittest.mock import MagicMock

    # The fixture's services may or may not have a real timing_thread (depends
    # on TimingConfig defaults). Install a fake we can observe regardless.
    fake_thread = MagicMock()
    fake_thread.stop = MagicMock()
    _mcp._vaults["test_vault"].timing_thread = fake_thread

    result = _parse(await sage_reload_vault("test_vault"))
    assert result["reloaded"] is True

    fake_thread.stop.assert_called_once_with(timeout=1.0)


async def test_reload_vault_preserves_content_store_factory_across_two_reloads(
    minimal_vault_config_dict, tmp_vault_dir
):
    """AC3 (reconciliation): content_store_factory survives across
    multiple successive reloads.

    Trap (anti-coincidental): a single-reload test would pass against the
    pre-refactor inline code (which already carries factory forward). The
    second reload is the trap — it verifies the factory survives the
    delegation path twice in a row (i.e., the new code reads factory from old
    on every reload, not just once).
    """

    def my_factory(_brain_root):
        return StubContentStore()

    config = VaultConfig.model_validate(minimal_vault_config_dict)
    async with initialize_services_for_test(
        config,
        content_store_factory=my_factory,
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        _mcp._vaults["factory_vault"] = services
        try:
            assert _mcp._vaults["factory_vault"].content_store_factory is my_factory

            # First reload
            result1 = _parse(await sage_reload_vault("factory_vault"))
            assert result1["reloaded"] is True
            assert _mcp._vaults["factory_vault"].content_store_factory is my_factory

            # Second reload — the real anti-coincidental check
            result2 = _parse(await sage_reload_vault("factory_vault"))
            assert result2["reloaded"] is True
            assert _mcp._vaults["factory_vault"].content_store_factory is my_factory
            assert isinstance(_mcp._vaults["factory_vault"].content_store, StubContentStore)
        finally:
            # Post-reload registry slot may be a fresh bundle; close it
            # before the helper exits. The helper only closes the
            # original ``services`` (idempotent if reload already did).
            current = _mcp._vaults.get("factory_vault")
            if current is not None and current is not services:
                await current.graph_store.close()
            _mcp._vaults.pop("factory_vault", None)


async def test_reload_vault_picks_up_yaml_edits(minimal_vault_config_dict, tmp_vault_dir, tmp_path):
    """Reload re-reads vault_config.yaml from disk and reflects edits.

    Documents the contract: when a vault was loaded from a YAML file and
    the file is later edited, sage_reload_vault must pick up the new
    values, not silently reuse the in-memory config.
    """
    import yaml as _yaml

    from sage.config import load_vault_config

    config_path = tmp_path / "vault_config.yaml"
    initial_config_dict = _copy_dict(minimal_vault_config_dict)
    initial_config_dict["abstraction"] = {"enabled": True}
    config_path.write_text(_yaml.safe_dump(initial_config_dict))

    config = load_vault_config(config_path)
    async with initialize_services_for_test(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
        config_path=config_path,
    ) as services:
        _mcp._vaults["yaml_reload_vault"] = services
        try:
            # Sanity check: starting state matches what we wrote
            assert _mcp._vaults["yaml_reload_vault"].config.abstraction.enabled is True

            # Edit the YAML on disk
            edited = _copy_dict(initial_config_dict)
            edited["abstraction"]["enabled"] = False
            config_path.write_text(_yaml.safe_dump(edited))

            # Reload
            result = _parse(await sage_reload_vault("yaml_reload_vault"))
            assert result["reloaded"] is True

            # In-memory config now reflects the edit
            assert _mcp._vaults["yaml_reload_vault"].config.abstraction.enabled is False, (
                "sage_reload_vault did not re-read the YAML from disk"
            )
        finally:
            # Post-reload registry slot may be a fresh bundle; close it
            # before the helper exits. The helper only closes the
            # original ``services`` (idempotent if reload already did).
            current = _mcp._vaults.get("yaml_reload_vault")
            if current is not None and current is not services:
                await current.graph_store.close()
            _mcp._vaults.pop("yaml_reload_vault", None)


def _copy_dict(d: dict) -> dict:
    import copy as _copy

    return _copy.deepcopy(d)


# ---------------------------------------------------------------------------
# Outer-sequence atomicity at the MCP envelope surface: verifies the
# restructured service methods (yaml-write+reload rollback;
# build-new-first migration) are wired through the MCP envelope,
# mirroring the inner-reload reload-failure surface tests above.
# ---------------------------------------------------------------------------


@pytest.fixture
async def vault_services_with_registry(minimal_vault_config_dict, tmp_vault_dir, monkeypatch):
    """Parallel to ``vault_services`` but wires a real ``VaultRegistryService``
    into the services bundle so calls that need ``_registry_service`` -- such
    as ``sage_update_vault_config`` and ``sage_admin_migrate_vault`` -- can
    reach the registry-reload code path. Also installs stub providers via
    ``SAGE_TEST_STUB_PROVIDERS=1`` and a ``content_store_factory`` so reload
    paths don't try to build LanceDB.
    """
    from sage.services.vault_registry import VaultRegistryService

    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    registry_service = VaultRegistryService(_mcp._vaults, initialize_services)
    async with initialize_services_for_test(
        config,
        registry_service=registry_service,
        content_store_factory=lambda _brain: StubContentStore(),
    ) as services:
        _mcp._vaults["test_vault"] = services
        try:
            yield services
        finally:
            await asyncio.sleep(0.1)
            # Re-read the registry at teardown -- a successful migrate or
            # reload swaps the slot, and the local ``services`` binding
            # becomes stale. If the slot was swapped, the post-swap bundle
            # needs an explicit close here; the helper's exit only closes
            # the original ``services.graph_store`` (idempotent if reload
            # already closed it).
            current = _mcp._vaults.get("test_vault")
            if current is not None and current is not services:
                await current.graph_store.close()
            _mcp._vaults.pop("test_vault", None)


async def test_sage_update_vault_config_atomicity_via_mcp_surface(
    vault_services_with_registry, monkeypatch, tmp_path, tmp_vault_dir
):
    """C1: an MCP ``sage_update_vault_config`` call that fails at the
    inner reload step rolls back the on-disk yaml and leaves the registry
    slot identity unchanged.

    Trap (anti-coincidental): the registry-preservation half of this
    assertion is already guaranteed by the inner-reload build-new-first
    contract. The trap that *only* the outer-sequence rollback satisfies
    is the yaml-rollback half -- a write-first, reload-second
    implementation persists the new yaml on disk even when the reload
    raises.
    """
    import yaml as _yaml

    import sage.mcp_init as _mcp_init

    # Isolate yaml writes to a tmp dir; otherwise the MCP path touches
    # ``~/sage_vaults/test_vault/vault_config.yaml`` on the live host.
    monkeypatch.setattr("sage.vault_management._VAULTS_ROOT", tmp_path / "sage_vaults")

    # First, seed the on-disk yaml with a known state via a successful
    # MCP call. After this call the registry slot is freshly swapped by
    # the reload step, so ``old`` below captures the post-seed services.
    seed_result = _parse(
        await sage_update_vault_config(
            vault_id="test_vault",
            vault={
                "id": "test_vault",
                "name": "MCP Pre Failure",
                "owner": "testuser",
                "storage_root": str(tmp_vault_dir / "sources"),
                "brain_root": str(tmp_vault_dir / "brain"),
                "visibility": "personal",
            },
        )
    )
    assert "error" not in seed_result, seed_result

    config_path = tmp_path / "sage_vaults" / "test_vault" / "vault_config.yaml"
    pre_call_dict = _yaml.safe_load(config_path.read_text())
    assert pre_call_dict["vault"]["name"] == "MCP Pre Failure"

    old = _mcp._vaults["test_vault"]

    async def failing_initialize_services(*args, **kwargs):
        raise SAGEError(
            code="schema_migration_required",
            message="simulated reload failure for outer-sequence atomicity MCP test",
            status_code=409,
        )

    monkeypatch.setattr(_mcp_init, "initialize_services", failing_initialize_services)
    monkeypatch.setattr(_mcp, "initialize_services", failing_initialize_services)

    result = _parse(
        await sage_update_vault_config(
            vault_id="test_vault",
            vault={
                "id": "test_vault",
                "name": "MCP Should Not Persist",
                "owner": "testuser",
                "storage_root": str(tmp_vault_dir / "sources"),
                "brain_root": str(tmp_vault_dir / "brain"),
                "visibility": "personal",
            },
        )
    )

    assert result.get("error") == "schema_migration_required"
    assert "simulated reload failure" in result["message"]

    # Registry slot identity unchanged (inner-reload build-new-first
    # contract).
    assert _mcp._vaults["test_vault"] is old

    # Yaml rolled back (outer-sequence atomicity at the MCP surface).
    post_call_dict = _yaml.safe_load(config_path.read_text())
    assert post_call_dict == pre_call_dict, (
        "MCP-path yaml-rollback failed: on-disk yaml carries the failed call's body. "
        f"Expected name={pre_call_dict['vault']['name']!r}, "
        f"got name={post_call_dict['vault']['name']!r}."
    )


async def test_sage_admin_migrate_vault_atomicity_via_mcp_surface(
    vault_services_with_registry, monkeypatch
):
    """C2: an MCP ``sage_admin_migrate_vault`` call whose post-migration
    reload fails returns a structured error envelope and leaves the
    original graph_store live in the registry.

    Trap (anti-coincidental): a close-then-migrate sequence runs
    ``self._graph_store.close()`` before fresh-handle migration, so by
    the time the reload-failure propagates to the MCP envelope,
    ``_executor`` on the registry's graph_store is None. Deferring the
    close into reload's success path keeps the live graph_store
    initialized; the live graph_store assertion is the trap.
    """
    import sage.mcp_init as _mcp_init
    from sage.storage.graph_store import GraphStore as _RealGraphStore
    from sage.storage.migrations import Migration

    # Force fake pending work so migrate_vault enters the migration branch.
    fake_pending = [
        Migration(
            table="documents",
            column="synthetic_pending_column_c2",
            ddl="ALTER TABLE documents ADD COLUMN synthetic_pending_column_c2 TEXT",
        )
    ]
    monkeypatch.setattr(
        "sage.services.maintenance.pending_migrations",
        lambda conn, plan=None: fake_pending,
    )

    # Migration succeeds via no-op fresh-handle subclass; failure must
    # arrive at the reload step, not the migration.
    class NoOpFreshGraphStore(_RealGraphStore):
        async def initialize(self, migrate: bool = False) -> None:
            return None

    monkeypatch.setattr("sage.services.maintenance.GraphStore", NoOpFreshGraphStore)

    async def failing_initialize_services(*args, **kwargs):
        raise SAGEError(
            code="schema_migration_required",
            message="simulated post-migration reload failure for MCP atomicity test",
            status_code=409,
        )

    monkeypatch.setattr(_mcp_init, "initialize_services", failing_initialize_services)
    monkeypatch.setattr(_mcp, "initialize_services", failing_initialize_services)

    old = _mcp._vaults["test_vault"]
    assert old.graph_store._executor is not None

    result = _parse(await sage_admin_migrate_vault(vault_id="test_vault"))

    assert result.get("error") == "schema_migration_required"
    assert "simulated post-migration reload failure" in result["message"]

    # Registry slot identity unchanged and graph_store still live: the
    # outer migration-then-reload sequence wrapped the close in reload's
    # success path. Behavioural co-assertion per TEST-SAGE-BH-137 confirms
    # the CAS-ADR-036 dispatch barrier did not engage.
    assert _mcp._vaults["test_vault"] is old
    assert old.graph_store._executor is not None, (
        "live graph_store was closed before the MCP-surface reload failure"
    )
    assert old.graph_store._all_connections, (
        "live graph_store has no live connections after MCP-surface reload failure"
    )
    live_docs = await old.graph_store.list_all_documents()
    assert isinstance(live_docs, list)


# ---------------------------------------------------------------------------
# Reabstract
# ---------------------------------------------------------------------------


async def test_reabstract_returns_started_status(vault_services):
    """BH-122: sage_reabstract should return a JSON response with
    status='reabstract_started' and the document_id, not the full
    document (fire-and-forget pattern)."""
    ingest_result = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    # sage_ingest dispatches Stages 2-3 in the background (BH-130).
    # Wait for indexing to commit chunks so reabstract has a projection
    # to work with.
    for _ in range(200):
        doc = _parse(await sage_get_document("test_vault", doc_id))
        if doc.get("pipeline_status") in {
            "indexing_complete",
            "abstraction_in_progress",
            "abstraction_complete",
            "abstraction_skipped",
        }:
            break
        await asyncio.sleep(0.05)

    result = _parse(await sage_reabstract("test_vault", doc_id))
    assert "error" not in result
    assert result["status"] == "reabstract_started"
    assert result["document_id"] == doc_id


async def test_reabstract_unknown_vault(vault_services):
    """sage_reabstract should return an error for unknown vault_id."""
    result = _parse(await sage_reabstract("nonexistent_vault", "deadbeef_doc"))
    assert result["error"] == "unknown_vault"


async def test_reabstract_document_not_found(vault_services):
    """sage_reabstract should return document_not_found for unknown doc."""
    result = _parse(await sage_reabstract("test_vault", "deadbeef_nonexistent"))
    assert result["error"] == "document_not_found"


async def test_sage_reabstract_mcp_tool_returns_409_on_concurrent_call(vault_services):
    """A second sage_reabstract call against the same document_id while the
    first is mid-flight must return the structured 409 error envelope
    (no exception propagated past the MCP boundary).
    """
    from datetime import datetime

    ingest_result = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_id = ingest_result["id"]

    for _ in range(200):
        doc = _parse(await sage_get_document("test_vault", doc_id))
        if doc.get("pipeline_status") in {
            "indexing_complete",
            "abstraction_in_progress",
            "abstraction_complete",
            "abstraction_skipped",
        }:
            break
        await asyncio.sleep(0.05)

    entered = asyncio.Event()
    gate = asyncio.Event()

    async def gated_abstract(text: str, max_tokens: int, doc_type: str | None) -> str:
        entered.set()
        await gate.wait()
        return "gated abstract"

    vault_services.ingestion_service._abstraction.generate_abstract = gated_abstract

    first = _parse(await sage_reabstract("test_vault", doc_id))
    assert first.get("status") == "reabstract_started"

    await asyncio.wait_for(entered.wait(), timeout=2.0)

    try:
        second = _parse(await sage_reabstract("test_vault", doc_id))
        assert second["error"] == "reabstract_document_already_in_flight"
        assert second["detail"]["document_id"] == doc_id
        # detail["start_time"] is an ISO 8601 string; just confirm it parses.
        datetime.fromisoformat(second["detail"]["start_time"])
    finally:
        gate.set()
        await asyncio.sleep(0.3)


# ---------------------------------------------------------------------------
# First-class edge enumeration via sage_discover(target="edges")
# ---------------------------------------------------------------------------


async def test_t0157_sage_discover_edges_happy_path(vault_services):
    """28. End-to-end happy path via the MCP tool: target=edges + mode=catalog
    returns a serialized envelope with target field, results array, and
    total_available.
    """
    doc_a = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await sage_ingest("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)
    # supersedes has resolution_policy=none so no anchor version
    # requirements; using it keeps the fixture small while still
    # exercising the edge enumeration path.
    link_result = _parse(
        await sage_link(
            "test_vault",
            doc_a["id"],
            doc_b["id"],
            "supersedes",
            rationale="t0157 fixture",
        )
    )

    # Sage_link returns wrapper; edge fields live under "edge".
    actual_source = link_result["edge"]["source_id"]
    actual_target = link_result["edge"]["target_id"]

    result = _parse(
        await sage_discover(
            "test_vault",
            mode="catalog",
            target="edges",
            filters={"source_id": actual_source},
            response_mode="full",
        )
    )
    assert result.get("target") == "edges"
    assert result.get("mode") == "catalog"
    assert result["total_available"] >= 1, (
        f"expected at least 1 edge from {actual_source}, got 0. result={result}"
    )
    assert isinstance(result["results"], list)
    hit = result["results"][0]
    for key in ("edge_id", "source_id", "target_id", "edge_type", "rationale"):
        assert key in hit, f"full envelope missing {key}: {hit.keys()}"
    assert hit["source_id"] == actual_source
    assert hit["target_id"] == actual_target
    assert hit["edge_type"] == "supersedes"


async def test_t0157_sage_discover_edges_light_round_trips_through_serializer(vault_services):
    """29. Light mode round-trips through serialize(): the envelope keys
    on the wire match what the model produced.
    """
    doc_a = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await sage_ingest("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)
    link = _parse(await sage_link("test_vault", doc_a["id"], doc_b["id"], "supersedes"))

    result = _parse(
        await sage_discover(
            "test_vault",
            mode="catalog",
            target="edges",
            # Sage_link returns wrapper; source_id is under "edge".
            filters={"source_id": link["edge"]["source_id"]},
            response_mode="light",
        )
    )
    hit = result["results"][0]
    # serialize() uses exclude_none=True so light rows on the wire should
    # carry exactly the identity columns. The dict is JSON, not a Pydantic
    # model, so we check key presence directly.
    assert set(hit.keys()) == {"edge_id", "source_id", "target_id", "edge_type"}


async def test_t0157_sage_discover_edges_target_edges_with_semantic_returns_error(vault_services):
    """28b. target=edges combined with a non-catalog mode is rejected via
    the typed mode_parameter_mismatch error envelope.
    """
    result = _parse(
        await sage_discover(
            "test_vault",
            mode="semantic",
            target="edges",
            query="anything",
        )
    )
    assert result["error"] == "mode_parameter_mismatch", result


def test_t0157_sage_discover_docstring_carries_edge_example():
    """30. sage_discover docstring documents the target="edges" dispatch
    with a worked example. This is a guard test that fails closed when
    the cross-tool documentation contract breaks (e.g., a later edit
    drops the example).
    """
    doc = sage_discover.__doc__
    assert doc is not None
    assert 'target="edges"' in doc, (
        "sage_discover docstring must carry a worked example for the "
        "target='edges' dispatch (T-0157)"
    )


def test_t0157_sage_unlink_docstring_points_at_edge_discovery():
    """31. sage_unlink docstring references sage_discover(target="edges")
    as the canonical path to discover edge_id. Guard test.
    """
    doc = _sage_unlink_tool.__doc__
    assert doc is not None
    assert 'target="edges"' in doc, (
        "sage_unlink docstring must point at sage_discover(target='edges') "
        "as the canonical edge_id discovery path (T-0157)"
    )


def test_t0157_retracts_edge_type_docstring_points_at_edge_discovery():
    """32. EdgeType class docstring documents the discovery path for
    edge_id when minting a retracts edge. Guard test.
    """
    doc = _EdgeType.__doc__
    assert doc is not None
    assert 'target="edges"' in doc, (
        "EdgeType class docstring must point at sage_discover(target='edges') "
        "for retracts edge_id discovery (T-0157)"
    )


# ---------------------------------------------------------------------------
# Document_id alias on sage_traverse + docstring clarification on sage_link
# ---------------------------------------------------------------------------
#
# MCP tools should converge on `document_id` as the canonical
# parameter name for "the document being operated on". sage_traverse historically
# uses `start_id`; this section verifies that `document_id` is accepted as an
# alias (both forms work, exactly one must be supplied). sage_link keeps its
# semantic `source_id`/`target_id` distinction; the docstring is clarified to
# state both are `documents.id` values.


async def test_t0155_traverse_accepts_document_id_alias(vault_services):
    """T1. sage_traverse accepts `document_id` as a keyword alias for
    `start_id`. Happy path: alias resolves to the same traversal result
    as the canonical name.
    """
    doc_a = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await sage_ingest("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)
    await sage_link("test_vault", doc_a["id"], doc_b["id"], "supersedes")

    result = _parse(await sage_traverse(vault_id="test_vault", document_id=doc_a["id"]))
    # Response shape unchanged: `start_id` remains the response key.
    assert result["start_id"] == doc_a["id"]
    assert len(result["nodes"]) == 1
    # Asserting the *correct* neighbor (doc_b) defeats any coincidental pass
    # where the alias was dropped and the function defaulted to some other id.
    assert result["nodes"][0]["document"]["id"] == doc_b["id"]


async def test_t0155_traverse_accepts_start_id_kwarg(vault_services):
    """T2. sage_traverse continues to accept `start_id` as a keyword
    argument after the alias is added. Back-compat guard.
    """
    doc_a = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await sage_ingest("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)
    await sage_link("test_vault", doc_a["id"], doc_b["id"], "supersedes")

    result = _parse(await sage_traverse(vault_id="test_vault", start_id=doc_a["id"]))
    assert result["start_id"] == doc_a["id"]
    assert len(result["nodes"]) == 1
    assert result["nodes"][0]["document"]["id"] == doc_b["id"]


async def test_t0155_traverse_accepts_start_id_positional(vault_services):
    """T3. sage_traverse continues to accept `start_id` positionally
    after the alias is added. Back-compat guard for the form used by
    the vast majority of existing tests.
    """
    doc_a = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await sage_ingest("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)
    await sage_link("test_vault", doc_a["id"], doc_b["id"], "supersedes")

    result = _parse(await sage_traverse("test_vault", doc_a["id"]))
    assert result["start_id"] == doc_a["id"]
    assert len(result["nodes"]) == 1
    assert result["nodes"][0]["document"]["id"] == doc_b["id"]


async def test_t0155_traverse_rejects_both_kwargs(vault_services):
    """T4. sage_traverse rejects supplying both `start_id` and
    `document_id` (even with equal values). Strict ambiguity rule:
    exactly one must be supplied.
    """
    doc = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.3)

    result = _parse(
        await sage_traverse(
            vault_id="test_vault",
            start_id=doc["id"],
            document_id=doc["id"],
        )
    )
    assert result["error"] == "ambiguous_document_identifier"
    # Detail must name both parameter names verbatim, so callers can
    # see which fields are in conflict.
    assert "start_id" in result["detail"]["supplied"]
    assert "document_id" in result["detail"]["supplied"]


async def test_t0155_traverse_rejects_missing_identifier(vault_services):
    """T5. sage_traverse rejects neither `start_id` nor `document_id`
    being supplied. Specific code (not a downstream `document_not_found`
    or generic ValidationError) confirms the validation branch fired.
    """
    result = _parse(await sage_traverse(vault_id="test_vault"))
    assert result["error"] == "missing_document_identifier"
    # Detail must enumerate the accepted parameter names so the caller
    # learns the alias without trial-and-error.
    assert "start_id" in result["detail"]["accepted"]
    assert "document_id" in result["detail"]["accepted"]


async def test_t0155_traverse_rejects_positional_plus_alias_kwarg(vault_services):
    """T6. sage_traverse rejects positional `start_id` plus keyword
    `document_id`. Mixing the two forms of the same logical argument
    is treated as the both-supplied case, not as silent precedence.
    """
    doc = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.3)

    result = _parse(await sage_traverse("test_vault", doc["id"], document_id=doc["id"]))
    assert result["error"] == "ambiguous_document_identifier"
    assert "start_id" in result["detail"]["supplied"]
    assert "document_id" in result["detail"]["supplied"]


def test_t0155_traverse_docstring_documents_alias():
    """T7. sage_traverse docstring documents the `document_id` alias
    inline on the `start_id` Args entry (not just in prose elsewhere).
    Guard test: ensures the ticket's docstring requirement lands at
    the parameter site where an MCP caller browsing the schema will
    see it.
    """
    import re
    import textwrap

    doc = sage_traverse.__doc__
    assert doc is not None
    dedented = textwrap.dedent(doc)
    # Anchor the match to the start_id: line of the Args section: the
    # word document_id must appear on the same line where start_id is
    # being described. A loose `"document_id" in doc` would pass
    # coincidentally if document_id appeared in unrelated prose.
    assert re.search(r"start_id:[^\n]*document_id", dedented), (
        "sage_traverse docstring must document `document_id` as an alias "
        "inline on the start_id Args entry (T-0155)"
    )


def test_t0155_link_docstring_clarifies_endpoint_shape():
    """T8. sage_link docstring's Args section describes `source_id`
    and `target_id` as `documents.id` / `document_id` values. Guard
    against the docstring update being skipped or being only in the
    prose body, missing the per-parameter Args entries.
    """
    import re
    import textwrap

    doc = sage_link.__doc__
    assert doc is not None
    dedented = textwrap.dedent(doc)
    # Each Args entry for source_id and target_id must carry the
    # shape clarification on its own line.
    assert re.search(r"source_id:[^\n]*(document_id|documents\.id)", dedented), (
        "sage_link Args entry for source_id must clarify it is a "
        "documents.id / document_id value (T-0155)"
    )
    assert re.search(r"target_id:[^\n]*(document_id|documents\.id)", dedented), (
        "sage_link Args entry for target_id must clarify it is a "
        "documents.id / document_id value (T-0155)"
    )


def test_t0155_set_lifecycle_docstring_clarifies_successor_id_shape():
    """T9. sage_set_lifecycle docstring's Args section describes
    `successor_id` as a `documents.id` / `document_id` value.
    Parallel-pattern guard: `successor_id` is a semantic-role
    document-id parameter analogous to source_id/target_id on
    sage_link; the same docstring clarification applies (
    principle, surfaced via F4 review).
    """
    import re
    import textwrap

    from sage.mcp_server import sage_set_lifecycle

    doc = sage_set_lifecycle.__doc__
    assert doc is not None
    dedented = textwrap.dedent(doc)
    assert re.search(r"successor_id:[^\n]*(document_id|documents\.id)", dedented), (
        "sage_set_lifecycle Args entry for successor_id must clarify "
        "it is a documents.id / document_id value (T-0155)"
    )
