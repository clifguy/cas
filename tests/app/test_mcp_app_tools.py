"""Tests for new MCP tools (TEST-APP-MCP-001 through MCP-027).

Covers 9 new tools: 7 SAGE API tools + 2 app backend tools.
Direct function calls bypassing MCP transport, matching the existing
MCP test pattern in tests/sage/test_mcp_server.py.
"""

import asyncio
import hashlib
import inspect
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

import sage.mcp_server as _mcp
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.config import VaultConfig
from sage.mcp_server import (
    bulk_ingest_document,
    delete_edge,
    get_document,
    get_vault_stats,
    ingest_document,
    list_directory,
    list_pending_metadata,
    list_staging_edges,
    list_vaults,
    search,
    update_staging_edge,
    verify_hash,
)
from sage.models.enums import EdgeType, PipelineStatus, SourceType
from sage.models.schemas import Document, StagingEdge
from tests.helpers.pipeline_wait import await_tool_idle
from tests.sage.conftest import initialize_services_for_test

_DOC_ID_RE = re.compile(r"^[0-9a-f]{8}_[a-z0-9_]+$")


async def _await_document_idle(services, doc_id):
    """Wait until a document is safe for a caller to act on, and return it.

    Thin adapter over the shared wait, reading through the tool surface so the
    poll observes what a caller of these tools would observe. The predicate --
    terminal status *and* no in-flight claim -- lives in
    ``tests/helpers/pipeline_wait.py`` for the whole suite.
    """

    async def fetch():
        return _parse(await get_document("test_vault", doc_id))

    return await await_tool_idle(fetch, doc_id, service=services.ingestion_service)


def _id(name: str) -> str:
    """Translate a short test name to a shape-conformant document ID.

    The ID validator in sage/models/schemas.py requires the pattern
    ^[0-9a-f]{8}_[a-z0-9_]+$. Test fixtures use short readable names
    like "pending-doc-1"; this helper wraps them so the values still
    construct valid Document instances. Idempotent: an already-canonical
    id passes through unchanged.
    """
    if _DOC_ID_RE.fullmatch(name):
        return name
    slug = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "n"
    return f"{hashlib.sha256(name.encode()).hexdigest()[:8]}_{slug}"


def _eid(name: str) -> str:
    """Deterministic canonical-UUID edge id derived from a short test name."""
    return str(uuid.uuid5(uuid.NAMESPACE_OID, f"sage-test-edge:{name}"))


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _sha(name: str) -> str:
    """Deterministic canonical Sha256 from a short test name.

    The Sha256Str validator requires `^sha256:[0-9a-f]{64}$`. Test
    fixtures historically used short readable strings like
    f"hash_{doc_id}" or "sha256:abc"; this helper maps any such
    name to a stable canonical Sha256. Idempotent.
    """
    if _SHA256_RE.fullmatch(name):
        return name
    return "sha256:" + hashlib.sha256(f"sage-test-hash:{name}".encode()).hexdigest()


_STG_001 = _eid("staging-001")
_STG_TEST = _eid("staging-test")
_GONE_001 = _eid("gone-001")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_vault_config_dict(tmp_path, vault_id: str, vault_name: str):
    brain_dir = tmp_path / vault_id / "brain"
    brain_dir.mkdir(parents=True, exist_ok=True)
    sources_dir = tmp_path / vault_id / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    return {
        "vault": {
            "id": vault_id,
            "name": vault_name,
            "description": f"Test vault: {vault_name}",
            "owner": "testuser",
            "storage_root": str(sources_dir),
            "brain_root": str(brain_dir),
            "visibility": "personal",
        },
        "document_types": {
            "doc_types": [
                {"value": "design_spec", "label": "Report Draft"},
                {"value": "checklist", "label": "Checklist"},
                {"value": "note", "label": "Note"},
            ],
        },
        "lifecycle": {
            "base_states_required": True,
            "states": [
                {"value": "active", "label": "Active"},
                {"value": "completed", "label": "Completed"},
                {"value": "archived", "label": "Archived", "is_terminal": True},
                {"value": "relocated", "label": "Relocated", "is_terminal": True},
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
                {"from_state": "active", "action": "relocate", "to_state": "relocated"},
            ],
        },
        "metadata_extraction": {
            "filename_extraction": {
                "separator": "_",
                "known_code_patterns": ["^[A-Z][A-Z0-9]{1,7}$", "^[A-Z]+-\\d+$"],
                "keyword_to_doc_type": [
                    {"keyword": "Checklist", "doc_type": "checklist"},
                ],
                "code_to_doc_type": [
                    {"code": "PV06", "doc_type": "design_spec"},
                ],
            },
        },
        "edge_inference": {
            "tier_assignments": [
                {
                    "edge_type": "supersedes",
                    "tier": 1,
                    "inference_rules": [{"method": "version_chain"}],
                },
                {
                    "edge_type": "covers",
                    "tier": 2,
                    "inference_rules": [{"method": "filename_code_match"}],
                },
            ],
        },
    }


def _parse(result: str | dict) -> object:
    if isinstance(result, dict):
        return result
    return json.loads(result)


@pytest.fixture
async def two_vaults(tmp_path):
    """Register two vaults in the MCP vault registry."""
    c1 = VaultConfig.model_validate(_make_vault_config_dict(tmp_path, "test_vault", "Test Vault"))
    c2 = VaultConfig.model_validate(
        _make_vault_config_dict(tmp_path, "second_vault", "Second Vault")
    )
    async with (
        initialize_services_for_test(
            c1,
            content_store=StubContentStore(),
            embedding_provider=StubEmbeddingProvider(),
            abstraction_provider=StubAbstractionProvider(),
        ) as s1,
        initialize_services_for_test(
            c2,
            content_store=StubContentStore(),
            embedding_provider=StubEmbeddingProvider(),
            abstraction_provider=StubAbstractionProvider(),
        ) as s2,
    ):
        _mcp._vaults["test_vault"] = s1
        _mcp._vaults["second_vault"] = s2
        try:
            yield s1, s2
        finally:
            await asyncio.sleep(0.1)
            _mcp._vaults.pop("test_vault", None)
            _mcp._vaults.pop("second_vault", None)


@pytest.fixture
async def single_vault(tmp_path):
    """Register one vault with test files."""
    config = VaultConfig.model_validate(
        _make_vault_config_dict(tmp_path, "test_vault", "Test Vault")
    )
    async with initialize_services_for_test(
        config,
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        _mcp._vaults["test_vault"] = services

        # Create test source files
        sources = Path(config.vault.storage_root)
        (sources / "sample.md").write_text("# Sample\n\nContent.")
        (sources / "second.md").write_text("# Second\n\nMore content.")

        try:
            yield services, config
        finally:
            await asyncio.sleep(0.3)
            _mcp._vaults.pop("test_vault", None)


@pytest.fixture
async def tier3_vault(tmp_path):
    """Register one vault whose ``ticket`` doc_type declares a metadata_schema.

    ``single_vault``'s doc_types declare none, and a doc_type with no schema
    refuses every Tier-3 payload, so the tests that need a payload to *land*
    need a vault that accepts one.
    """
    config_dict = _make_vault_config_dict(tmp_path, "tier3_vault", "Tier3 Vault")
    config_dict["document_types"]["doc_types"].append(
        {
            "value": "strict_ticket",
            "label": "Strict Ticket",
            "metadata_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["ticket_id"],
                "properties": {"ticket_id": {"type": "string"}},
            },
        }
    )
    config_dict["document_types"]["doc_types"].append(
        {
            "value": "ticket",
            "label": "Ticket",
            "metadata_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "ticket_id": {"type": "string", "pattern": "^T-\\d{4}$"},
                    "ticket_priority": {"type": "string", "enum": ["high", "medium", "low"]},
                },
            },
        }
    )
    config = VaultConfig.model_validate(config_dict)
    async with initialize_services_for_test(
        config,
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        _mcp._vaults["tier3_vault"] = services
        try:
            yield services, config
        finally:
            await asyncio.sleep(0.3)
            _mcp._vaults.pop("tier3_vault", None)


@pytest.fixture
async def empty_registry():
    """Empty vault registry (saves/restores to avoid cross-module interference)."""
    saved = dict(_mcp._vaults)
    _mcp._vaults.clear()
    yield
    _mcp._vaults.clear()
    _mcp._vaults.update(saved)


# ---------------------------------------------------------------------------
# 1. list_vaults (MCP-001, MCP-002)
# ---------------------------------------------------------------------------


class TestSageListVaults:
    async def test_mcp_001_returns_all_vaults(self, two_vaults, monkeypatch):
        """list_vaults returns all registered vaults in envelope.

        The entry key set is pinned exactly: the response carries a per-vault
        document count and no server-side layout (``storage_root``), and a
        renamed rather than removed field fails the equality. One vault's
        store reports a non-zero total so the count is proven to come from
        that vault's store rather than from a default.
        """
        s1, _s2 = two_vaults

        async def seven() -> int:
            return 7

        monkeypatch.setattr(s1.graph_store, "get_total_document_count", seven)

        result = _parse(await list_vaults())
        assert result["count"] == 2
        assert isinstance(result["vaults"], list)
        by_id = {v["id"]: v for v in result["vaults"]}
        assert set(by_id) == {"test_vault", "second_vault"}
        for v in result["vaults"]:
            assert set(v) == {"id", "name", "description", "document_count"}
        assert by_id["test_vault"]["document_count"] == 7
        assert by_id["second_vault"]["document_count"] == 0

    async def test_mcp_002_empty_returns_envelope(self, empty_registry):
        """list_vaults with no vaults returns envelope with empty list."""
        result = _parse(await list_vaults())
        assert result["vaults"] == []
        assert result["count"] == 0


# ---------------------------------------------------------------------------
# 2. get_vault_stats (MCP-003, MCP-004, MCP-005)
# ---------------------------------------------------------------------------


class TestSageVaultStats:
    async def test_mcp_003_returns_stats_and_health(self, single_vault):
        """get_vault_stats returns statistics and health indicators."""
        services, config = single_vault
        # Ingest a document first
        doc = _parse(await ingest_document("test_vault", "sample.md", "markdown"))
        await _await_document_idle(services, doc["id"])

        result = _parse(await get_vault_stats("test_vault"))
        assert result["total_documents"] >= 1
        assert "by_lifecycle_status" in result
        assert "by_doc_type" in result
        assert "by_source_type" in result
        assert "total_edges" in result
        assert "staging_edge_count" in result
        assert "health" in result
        h = result["health"]
        assert "pending_metadata_count" in h
        assert "pending_edge_count" in h
        assert "deferred_abstract_count" in h
        assert "failed_ingestion_count" in h

    async def test_mcp_004_empty_vault_zero_counts(self, single_vault):
        """get_vault_stats for empty vault returns zero counts."""
        result = _parse(await get_vault_stats("test_vault"))
        assert result["total_documents"] == 0
        assert result["total_edges"] == 0
        assert result["staging_edge_count"] == 0

    async def test_mcp_005_unknown_vault_error(self, single_vault):
        """get_vault_stats for unknown vault returns error."""
        result = _parse(await get_vault_stats("nonexistent"))
        assert result["error"] == "vault_not_found"

    async def test_stats_omit_sqlite_size_bytes(self, single_vault):
        """The retired sqlite_size_bytes field is absent; the backend-neutral
        graph_store_size_bytes carries the signal.

        A lingering field with a stale default would pass a naive presence
        check silently; asserting the key's absence catches it.
        """
        services, config = single_vault
        doc = _parse(await ingest_document("test_vault", "sample.md", "markdown"))
        await _await_document_idle(services, doc["id"])

        result = _parse(await get_vault_stats("test_vault"))
        assert "sqlite_size_bytes" not in result
        assert result["graph_store_size_bytes"] > 0

    def test_vault_stats_response_model_has_no_sqlite_size_bytes_field(self):
        """Pydantic posture check: catches re-introduction on the model
        independent of the producer."""
        from sage.models.schemas import VaultStatsResponse

        assert "sqlite_size_bytes" not in VaultStatsResponse.model_fields

    async def test_content_store_size_bytes_nonzero_after_indexing(self, single_vault):
        """content_store_size_bytes reflects actual content-store directory size."""
        services, config = single_vault
        doc = _parse(await ingest_document("test_vault", "sample.md", "markdown"))
        await _await_document_idle(services, doc["id"])

        result = _parse(await get_vault_stats("test_vault"))
        assert result["content_store_size_bytes"] > 0

    async def test_storage_sizes_for_empty_vault(self, single_vault):
        """Empty vault reports provisioned-relation overhead, no content.

        Guards the meters' wiring on a fresh vault: a meter that fails to
        resolve its relations degrades to 0, so the nonzero floor below
        distinguishes "measured an empty relation" from "measured nothing".
        """
        result = _parse(await get_vault_stats("test_vault"))
        assert "sqlite_size_bytes" not in result
        # Empty relations still carry index metapages and heap headers, so
        # both live meters read nonzero overhead even before first ingest.
        assert result["graph_store_size_bytes"] > 0
        assert result["content_store_size_bytes"] > 0
        # Chunk count is 0 before any indexing
        assert result["content_store_row_count"] == 0

    async def test_content_store_row_count_nonzero_after_indexing(self, single_vault):
        """content_store_row_count reflects the indexed row count across the surfaces."""
        services, config = single_vault
        doc = _parse(await ingest_document("test_vault", "sample.md", "markdown"))
        await _await_document_idle(services, doc["id"])

        result = _parse(await get_vault_stats("test_vault"))
        assert result["content_store_row_count"] > 0


# ---------------------------------------------------------------------------
# 3. verify_hash (MCP-006, MCP-007)
# ---------------------------------------------------------------------------


class TestSageHashCheck:
    async def test_mcp_006_returns_matches(self, single_vault):
        """verify_hash returns match results."""
        services, config = single_vault
        # Ingest to get a known hash
        doc_result = _parse(await ingest_document("test_vault", "sample.md", "markdown"))
        doc_hash = doc_result["source_content_hash"]
        await _await_document_idle(services, doc_result["id"])

        # Well-formed but absent. A malformed stand-in (e.g. "sha256:unknown")
        # no longer reports exists=false -- it rejects the request outright;
        # see test_mcp_006c.
        absent = "sha256:" + "0" * 64

        result = _parse(await verify_hash("test_vault", [doc_hash, absent]))["matches"]
        assert result[doc_hash]["exists"] is True
        assert result[doc_hash]["document_id"] == doc_result["id"]
        assert result[absent]["exists"] is False

    async def test_mcp_006b_bare_hex_resolves_to_the_same_document(self, single_vault):
        """A digest submitted without the algorithm prefix still matches.

        The result is keyed by the canonical form, not by the spelling supplied.
        """
        services, _config = single_vault
        doc_result = _parse(await ingest_document("test_vault", "sample.md", "markdown"))
        doc_hash = doc_result["source_content_hash"]
        await _await_document_idle(services, doc_result["id"])
        bare = doc_hash.removeprefix("sha256:")

        result = _parse(await verify_hash("test_vault", [bare]))["matches"]
        assert result[doc_hash]["exists"] is True
        assert result[doc_hash]["document_id"] == doc_result["id"]
        assert bare not in result

    async def test_mcp_006c_malformed_hash_rejects_the_request(self, single_vault):
        """A malformed entry rejects the whole call rather than reporting a miss.

        Batch validation is all-or-nothing, matching the REST body-validation
        surface: one unusable hash is a caller error, not a data condition.

        The input is bare on purpose. A prefixed malformed value such as
        "sha256:unknown" canonicalizes to itself, so asserting on it could not
        separate an envelope reporting the caller's string from one reporting
        the normalization attempt; "unknown" and "sha256:unknown" differ, so
        the detail assertion below discriminates the two.
        """
        result = _parse(await verify_hash("test_vault", ["unknown"]))
        assert result["error"] == "invalid_sha256"
        assert result["detail"]["sha256"] == "unknown"

    async def test_mcp_007_empty_list(self, single_vault):
        """verify_hash with empty list returns empty matches."""
        result = _parse(await verify_hash("test_vault", []))
        assert result["matches"] == {}


# ---------------------------------------------------------------------------
# 4. list_staging_edges (MCP-008, MCP-009)
# ---------------------------------------------------------------------------


class TestSageListStagingEdges:
    async def test_mcp_008_returns_staging_edges(self, single_vault):
        """list_staging_edges returns Tier 2 edges in envelope."""
        services, config = single_vault
        # Ingest two docs and create a staging edge
        r1 = _parse(await ingest_document("test_vault", "sample.md", "markdown"))
        r2 = _parse(await ingest_document("test_vault", "second.md", "markdown"))
        await _await_document_idle(services, r1["id"])
        await _await_document_idle(services, r2["id"])

        staging = StagingEdge(
            id=_STG_001,
            source_id=r1["id"],
            target_id=r2["id"],
            edge_type=EdgeType.COVERS,
            inference_evidence="Test evidence",
            confidence_tier=2,
            created_at=datetime.now(timezone.utc),
        )
        await services.graph_store.insert_staging_edge(staging)

        result = _parse(await list_staging_edges("test_vault"))
        assert result["count"] == 1
        assert result["vault_id"] == "test_vault"
        assert result["items"][0]["id"] == _STG_001
        assert result["items"][0]["edge_type"] == "covers"

    async def test_mcp_009_empty_when_none(self, single_vault):
        """list_staging_edges returns envelope when none exist."""
        result = _parse(await list_staging_edges("test_vault"))
        assert result["items"] == []
        assert result["count"] == 0
        assert result["vault_id"] == "test_vault"
        assert result["status"] == "no_staging_edges"


# ---------------------------------------------------------------------------
# 5. sage_confirm/dismiss_staging_edge (MCP-010, MCP-011, MCP-012)
# ---------------------------------------------------------------------------


class TestStagingEdgeActions:
    async def _setup_staging(self, services):
        """Ingest docs and create a staging edge, return IDs."""
        r1 = _parse(await ingest_document("test_vault", "sample.md", "markdown"))
        r2 = _parse(await ingest_document("test_vault", "second.md", "markdown"))
        await _await_document_idle(services, r1["id"])
        await _await_document_idle(services, r2["id"])
        staging = StagingEdge(
            id=_STG_TEST,
            source_id=r1["id"],
            target_id=r2["id"],
            edge_type=EdgeType.COVERS,
            inference_evidence="Test evidence",
            confidence_tier=2,
            created_at=datetime.now(timezone.utc),
        )
        await services.graph_store.insert_staging_edge(staging)
        return r1["id"], r2["id"]

    async def test_mcp_010_confirm_moves_to_production(self, single_vault):
        """update_staging_edge(action='confirm') promotes to production."""
        services, config = single_vault
        await self._setup_staging(services)

        result = _parse(await update_staging_edge("test_vault", _STG_TEST, "confirm"))
        assert result["confirmed"] is True
        assert "production_edge_id" in result

        # Staging edge gone
        listing = _parse(await list_staging_edges("test_vault"))
        assert listing["count"] == 0

    async def test_mcp_011_dismiss_deletes(self, single_vault):
        """update_staging_edge(action='dismiss') deletes from staging."""
        services, config = single_vault
        await self._setup_staging(services)

        result = _parse(await update_staging_edge("test_vault", _STG_TEST, "dismiss"))
        assert result["dismissed"] is True

        listing = _parse(await list_staging_edges("test_vault"))
        assert listing["count"] == 0

    async def test_mcp_012_nonexistent_returns_error(self, single_vault):
        """update_staging_edge against non-existent edge returns error."""
        result = _parse(await update_staging_edge("test_vault", _GONE_001, "confirm"))
        assert "error" in result

    async def test_invalid_action_returns_error(self, single_vault):
        """update_staging_edge with action outside {confirm, dismiss}
        returns a structured error (the action enum is enforced inside the
        tool, not silently passed through to the service layer)."""
        services, config = single_vault
        await self._setup_staging(services)

        result = _parse(await update_staging_edge("test_vault", _STG_TEST, "approve"))
        # The declared code, not a mention of it inside an internal_error.
        assert result["error"] == "invalid_action"
        assert result["detail"] == {
            "attempted_action": "approve",
            "known_actions": ["confirm", "dismiss"],
        }

        # Anti-coincidental-pass: the staging edge must still exist
        # (the dispatch must not silently fall through to one of the
        # branches on an invalid action).
        listing = _parse(await list_staging_edges("test_vault"))
        assert listing["count"] == 1

    async def test_confirm_with_existing_production_edge_is_idempotent(self, single_vault):
        """If a production edge with the same natural-key triple
        already exists when a staging edge is confirmed, the promotion
        is idempotent: the staging row is consumed and the response
        carries the pre-existing production edge id (no IntegrityError
        leaks to the caller).
        """
        from sage.models.enums import EdgeType
        from sage.models.schemas import LinkRequest

        services, _config = single_vault
        doc_a_id, doc_b_id = await self._setup_staging(services)

        # Pre-create the production edge that the staging edge will
        # collide with on confirm. The setup helper stages a COVERS
        # edge, so the pre-existing edge must match.
        await services.graph_ops_service._create_edge_strict(
            LinkRequest(
                source_id=doc_a_id,
                target_id=doc_b_id,
                edge_type=EdgeType.COVERS,
                source_valid_from_version=doc_a_id,
                target_valid_from_version=doc_b_id,
                rationale="pre-existing production edge",
            )
        )

        result = _parse(await update_staging_edge("test_vault", _STG_TEST, "confirm"))
        # The promotion is idempotent: no error surfaced.
        assert result["confirmed"] is True
        assert "production_edge_id" in result

        # Exactly one production edge exists between the pair.
        edges = await services.graph_store.get_edges_by_source(doc_a_id, "covers")
        assert len(edges) == 1

        # Staging edge is consumed.
        listing = _parse(await list_staging_edges("test_vault"))
        assert listing["count"] == 0


# ---------------------------------------------------------------------------
# Edge_id validation across MCP tools that take edge_id directly
# ---------------------------------------------------------------------------


class TestEdgeIdValidation:
    """Negative tests proving non-canonical / invalid edge_id input is normalized
    or rejected at the MCP-tool boundary, not silently passed to storage where
    a non-canonical UUID would miss the canonical-form lookup.
    """

    @pytest.mark.parametrize(
        "tool_fn",
        [
            lambda v, e: delete_edge(v, e),
            lambda v, e: update_staging_edge(v, e, "confirm"),
            lambda v, e: update_staging_edge(v, e, "dismiss"),
        ],
        ids=[
            "delete_edge",
            "sage_update_staging_edge_confirm",
            "sage_update_staging_edge_dismiss",
        ],
    )
    @pytest.mark.parametrize(
        "bad_input",
        ["not-a-uuid", "", "12345", "deadbeef-dead-beef"],
        ids=["random_text", "empty", "digits", "truncated_uuid"],
    )
    async def test_invalid_uuid_rejected(self, single_vault, tool_fn, bad_input):
        result = _parse(await tool_fn("test_vault", bad_input))
        assert "error" in result
        assert result["error"] == "invalid_edge_id"
        assert result["detail"]["edge_id"] == bad_input

    @pytest.mark.parametrize(
        "action",
        ["confirm", "dismiss"],
        ids=["confirm", "dismiss"],
    )
    async def test_non_canonical_uuid_normalized_to_lookup(self, single_vault, action):
        """A staging edge created with canonical id X is found by URN-prefixed lookup."""
        services, _config = single_vault
        # Set up the staging edge with the canonical id
        r1 = _parse(await ingest_document("test_vault", "sample.md", "markdown"))
        r2 = _parse(await ingest_document("test_vault", "second.md", "markdown"))
        await _await_document_idle(services, r1["id"])
        await _await_document_idle(services, r2["id"])
        canonical = _eid("normalize-target")
        staging = StagingEdge(
            id=canonical,
            source_id=r1["id"],
            target_id=r2["id"],
            edge_type=EdgeType.COVERS,
            inference_evidence="Test",
            confidence_tier=2,
            created_at=datetime.now(timezone.utc),
        )
        await services.graph_store.insert_staging_edge(staging)

        # Call with URN-prefixed (non-canonical) input — should normalize and succeed
        non_canonical = f"urn:uuid:{canonical}"
        result = _parse(await update_staging_edge("test_vault", non_canonical, action))
        # Either confirmed=True or dismissed=True; the absence of "error" proves
        # the non-canonical input was normalized before the storage lookup.
        assert "error" not in result, f"non-canonical input not normalized: {result}"


# ---------------------------------------------------------------------------
# 6. list_pending_metadata (MCP-013, MCP-014)
# ---------------------------------------------------------------------------


class TestSagePendingMetadata:
    async def test_mcp_013_returns_pending(self, tmp_path):
        """list_pending_metadata returns documents awaiting confirmation in envelope.

        Under CAS-ADR-021, metadata_confirmed=False is driven by the
        caller's IngestRequest.needs_review flag, not by vault config.
        The MCP tool surface does not yet expose needs_review (Chunk 4),
        so the test seeds a pending document by inserting it directly
        via the graph store. This keeps the test focused on
        list_pending_metadata's behavior, decoupled from how a document
        comes to be unconfirmed.
        """
        cfg_dict = _make_vault_config_dict(tmp_path, "review_vault", "Review Required Vault")
        config = VaultConfig.model_validate(cfg_dict)
        async with initialize_services_for_test(
            config,
            content_store=StubContentStore(),
            embedding_provider=StubEmbeddingProvider(),
            abstraction_provider=StubAbstractionProvider(),
        ) as services:
            _mcp._vaults["review_vault"] = services
            try:
                now = datetime.now(timezone.utc)
                pending_doc = Document(
                    id=_id("pending-doc-1"),
                    title="Pending Sample",
                    source_type=SourceType.MARKDOWN,
                    source_path="sample.md",
                    lifecycle_status="active",
                    source_content_hash=_sha("pending-doc-1"),
                    adapter_version="1.0",
                    created_by="testuser",
                    created_at=now,
                    last_modified_by="testuser",
                    updated_at=now,
                    projected_at=now,
                    pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
                    metadata_confirmed=False,
                )
                await services.graph_store.insert_document(pending_doc)

                result = _parse(await list_pending_metadata("review_vault"))
                assert result["vault_id"] == "review_vault"
                assert result["count"] >= 1
                assert result["status"] == "pending_review"
                assert "document" in result["items"][0]
            finally:
                await asyncio.sleep(0.3)
                _mcp._vaults.pop("review_vault", None)
            _mcp._vaults.pop("review_vault", None)

    async def test_mcp_014_empty_when_none(self, single_vault):
        """list_pending_metadata returns envelope when none pending."""
        result = _parse(await list_pending_metadata("test_vault"))
        assert result["items"] == []
        assert result["count"] == 0
        assert result["vault_id"] == "test_vault"
        assert result["status"] == "no_pending_metadata"
        assert result["total_available"] == 0

    @staticmethod
    async def _seed_pending(services, count: int) -> list[str]:
        """Insert ``count`` unconfirmed documents whose paths carry a parsed
        date, and return their ids in id order."""
        now = datetime.now(timezone.utc)
        ids = []
        for n in range(count):
            doc = Document(
                id=_id(f"mcp-pending-{n}"),
                title=f"Pending {n}",
                source_type=SourceType.MARKDOWN,
                source_path=f"2026-04-1{n}_pending_{n}.md",
                document_date=f"2026-04-1{n}",
                lifecycle_status="active",
                source_content_hash=_sha(f"mcp-pending-{n}"),
                adapter_version="1.0",
                created_by="testuser",
                created_at=now,
                last_modified_by="testuser",
                updated_at=now,
                projected_at=now,
                pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
                metadata_confirmed=False,
            )
            await services.graph_store.insert_document(doc)
            ids.append(doc.id)
        return sorted(ids)

    async def test_full_rows_carry_extracted_fields(self, single_vault):
        """At or under the threshold the rows are full and carry the same
        extracted-field annotations the HTTP surface serves -- not an empty
        map standing in for them."""
        services, _ = single_vault
        await self._seed_pending(services, 2)

        result = _parse(await list_pending_metadata("test_vault"))

        assert result["response_mode"] == "full"
        assert result["total_available"] == 2
        for item in result["items"]:
            assert item["extracted_fields"]["title"]["value"] == item["document"]["title"]
            assert item["extracted_fields"]["document_date"]["value"].startswith("2026-04-1")

    async def test_five_rows_is_still_full(self, single_vault):
        services, _ = single_vault
        await self._seed_pending(services, 5)

        result = _parse(await list_pending_metadata("test_vault"))
        assert result["count"] == 5
        assert result["response_mode"] == "full"

    async def test_pages_and_defaults_to_light_over_threshold(self, single_vault):
        services, _ = single_vault
        ids = await self._seed_pending(services, 7)

        default = _parse(await list_pending_metadata("test_vault"))
        assert default["response_mode"] == "light"
        assert default["total_available"] == 7
        assert default["count"] == 7
        assert [i["id"] for i in default["items"]] == ids
        assert "document" not in default["items"][0]

        page = _parse(
            await list_pending_metadata("test_vault", limit=3, offset=3, response_mode="full")
        )
        assert page["response_mode"] == "full"
        assert (page["limit"], page["offset"], page["count"]) == (3, 3, 3)
        assert [i["document"]["id"] for i in page["items"]] == ids[3:6]

    @pytest.mark.parametrize(
        "kwargs,parameter",
        [
            ({"limit": 101}, "limit"),
            ({"offset": -1}, "offset"),
            ({"response_mode": "medium"}, "response_mode"),
        ],
    )
    async def test_refuses_out_of_range_paging(self, single_vault, kwargs, parameter):
        result = _parse(await list_pending_metadata("test_vault", **kwargs))
        assert result["error"] == "invalid_parameter", result
        assert result["detail"]["parameter"] == parameter


# ---------------------------------------------------------------------------
# 7. list_directory (MCP-015, MCP-016, MCP-017, MCP-018)
# ---------------------------------------------------------------------------


class TestAppScanDirectory:
    async def test_mcp_015_returns_files_with_parsed_metadata(self, single_vault, tmp_path):
        """list_directory returns files with parsed metadata."""
        services, config = single_vault
        scan_dir = tmp_path / "scan_inbox"
        scan_dir.mkdir()
        (scan_dir / "2026-03-09_EXAMPLE_PV06_Claim-Set_v7.md").write_text("# Test")
        (scan_dir / "notes.txt").write_text("txt file")

        result = _parse(await list_directory("test_vault", str(scan_dir)))
        assert "files" in result
        assert "warnings" in result
        files = result["files"]
        md = [f for f in files if f["file_path"].endswith(".md")]
        assert len(md) == 1
        assert md[0]["source_type"] == "markdown"
        assert md[0]["sage_status"] == "new"
        assert "parsed_metadata" in md[0]
        pm = md[0]["parsed_metadata"]
        assert pm["title"] == "Claim-Set"
        assert "PV06" in pm["codes"]

    async def test_every_registry_extension_is_reported_ingestable(self, single_vault, tmp_path):
        """Scan offers every file the process-wide registry can handle.

        Vault configuration declares no adapter availability (CAS-ADR-046),
        so a scan row is an offer to ingest exactly what an ingest of the
        same file would accept. The unmapped extension is the discriminating
        half: without it, a status computation that had collapsed to always
        reporting `new` would satisfy the first assertion too.
        """
        scan_dir = tmp_path / "mixed_inbox"
        scan_dir.mkdir()
        (scan_dir / "handled.md").write_text("# Handled")
        (scan_dir / "unhandled.zzz").write_text("not a known format")

        result = _parse(await list_directory("test_vault", str(scan_dir)))
        by_suffix = {Path(f["file_path"]).suffix: f for f in result["files"]}

        assert by_suffix[".md"]["source_type"] == "markdown"
        assert by_suffix[".md"]["sage_status"] == "new"
        assert by_suffix[".zzz"]["source_type"] is None
        assert by_suffix[".zzz"]["sage_status"] == "no_adapter"

    async def test_adapter_disabled_status_is_retired(self):
        """No scan surface still declares the retired status.

        The status existed only to label a file whose extension mapped to
        an adapter the vault had switched off. With enablement gone there is
        nothing it could describe, and leaving it declared would invite a
        consumer to branch on a value the scan can never emit.
        """
        import typing

        from app.backend.models import ScanResultResponse
        from sage.services.scan import ScanResult

        declared = typing.get_args(ScanResultResponse.model_fields["sage_status"].annotation)
        assert "adapter_disabled" not in declared
        assert set(declared) == {"new", "modified", "unchanged", "no_adapter"}

        source = inspect.getsource(ScanResult)
        assert "adapter_disabled" not in source

    async def test_mcp_016_invalid_directory_error(self, single_vault):
        """list_directory with invalid directory returns error."""
        result = _parse(await list_directory("test_vault", "/nonexistent/path"))
        assert result["error"] == "invalid_directory"

    async def test_mcp_017_respects_max_depth(self, single_vault, tmp_path):
        """list_directory respects max_depth."""
        services, config = single_vault
        scan_dir = tmp_path / "depth_test"
        scan_dir.mkdir()
        (scan_dir / "top.md").write_text("# Top")
        sub = scan_dir / "sub"
        sub.mkdir()
        (sub / "nested.md").write_text("# Nested")

        result = _parse(await list_directory("test_vault", str(scan_dir), max_depth=0))
        paths = [f["file_path"] for f in result["files"]]
        assert any("top.md" in p for p in paths)
        assert not any("nested.md" in p for p in paths)

    async def test_mcp_018_permission_warnings(self, single_vault, tmp_path):
        """list_directory reports permission errors as warnings."""
        services, config = single_vault
        scan_dir = tmp_path / "perm_test"
        scan_dir.mkdir()
        (scan_dir / "ok.md").write_text("# OK")

        result = _parse(await list_directory("test_vault", str(scan_dir)))
        assert isinstance(result["warnings"], list)

    async def test_scan_response_carries_truncated_flag(self, single_vault, tmp_path, monkeypatch):
        """list_directory reports whether the scan was cut by a ceiling.

        An in-bounds scan carries ``truncated: false``; a scan cut by the
        file ceiling carries ``truncated: true`` plus a warning, so a
        partial listing is never mistaken for a complete one.
        """
        import sage.services.scan as scan_module

        scan_dir = tmp_path / "truncation_test"
        scan_dir.mkdir()
        (scan_dir / "one.md").write_text("# One")
        (scan_dir / "two.md").write_text("# Two")

        result = _parse(await list_directory("test_vault", str(scan_dir)))
        assert result["truncated"] is False

        monkeypatch.setattr(scan_module, "MAX_SCAN_FILES", 1)
        result = _parse(await list_directory("test_vault", str(scan_dir)))
        assert result["truncated"] is True
        assert len(result["files"]) == 1
        assert any("file" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# 8. bulk_ingest_document (MCP-019, MCP-020, MCP-021, MCP-022)
# ---------------------------------------------------------------------------


class TestAppBatchIngest:
    async def test_mcp_019_returns_summary_with_edges(self, single_vault):
        """bulk_ingest_document processes files and returns summary with edge counts."""
        services, config = single_vault
        sources = Path(config.vault.storage_root)
        v1 = sources / "report_v1.md"
        v1.write_text("# Report v1\n\nFirst.")
        v2 = sources / "report_v2.md"
        v2.write_text("# Report v2\n\nSecond.")

        result = _parse(
            await bulk_ingest_document(
                "test_vault",
                [
                    {
                        "file_path": str(v1),
                        "source_type": "markdown",
                        "parsed_metadata": {
                            "title": "Report",
                            "codes": ["PV06"],
                            "version": "v1",
                            "doc_type": "design_spec",
                        },
                    },
                    {
                        "file_path": str(v2),
                        "source_type": "markdown",
                        "parsed_metadata": {
                            "title": "Report",
                            "codes": ["PV06"],
                            "version": "v2",
                            "doc_type": "design_spec",
                        },
                    },
                ],
            )
        )
        assert "documents_created" in result
        assert result["documents_created"]["new"] == 2
        assert "edges_created" in result
        assert "edges_staged" in result
        assert "edges_dropped" in result
        assert result["error_count"] == 0
        # Should have a supersedes edge
        assert result["edges_created"].get("supersedes", 0) >= 1

    async def test_mcp_021_continues_after_error(self, single_vault):
        """bulk_ingest_document continues after per-file error."""
        services, config = single_vault
        sources = Path(config.vault.storage_root)
        good = sources / "good_file.md"
        good.write_text("# Good\n\nContent.")

        result = _parse(
            await bulk_ingest_document(
                "test_vault",
                [
                    {"file_path": str(good), "source_type": "markdown"},
                    {"file_path": "/nonexistent/bad.md", "source_type": "markdown"},
                ],
            )
        )
        assert result["error_count"] == 1
        assert result["documents_created"]["new"] >= 1
        assert len(result["errors"]) == 1
        assert "bad.md" in result["errors"][0]["filename"]

    async def test_mcp_022_empty_list_error(self, single_vault):
        """bulk_ingest_document with empty file list returns error."""
        result = _parse(await bulk_ingest_document("test_vault", []))
        assert "error" in result
        assert result["error"] == "empty_file_list"

    async def test_undeclared_file_entry_key_names_the_accepted_set(self, single_vault):
        """An undeclared key in any file entry is refused before anything is ingested.

        The undeclared key sits on the second entry, so the refusal has to walk
        the batch rather than inspect the first entry, and its parameter path
        matches the one the application API gives. The refusal names what an
        entry does accept, because naming only the offending key costs a round
        trip and guessing is not a recovery. The control ingests the same two
        entries without the key and must create both documents: an entry the
        refused call had ingested first would come back as duplicate content
        instead.
        """
        from sage.app_tools import _FILE_ENTRY_FIELDS

        _, config = single_vault
        sources = Path(config.vault.storage_root)
        first = {"file_path": str(sources / "sample.md"), "source_type": "markdown"}
        second = {"file_path": str(sources / "second.md"), "source_type": "markdown"}

        refused = _parse(
            await bulk_ingest_document("test_vault", [first, {**second, "bogus_field_x": 1}])
        )
        control = _parse(await bulk_ingest_document("test_vault", [first, second]))

        assert refused["error"] == "undeclared_key", refused
        assert refused["detail"]["parameter"] == "files.1"
        assert refused["detail"]["key"] == "bogus_field_x"
        assert refused["detail"]["recognized"] == sorted(_FILE_ENTRY_FIELDS)
        assert refused["detail"]["example"]
        assert control["error_count"] == 0, control
        assert control["documents_created"]["new"] == 2

    async def test_undeclared_parsed_metadata_key_names_the_accepted_set(self, single_vault):
        """An undeclared key in any entry's parsed_metadata is refused, not discarded.

        As above, the key sits on the second entry and the control must ingest
        both. The parsed_metadata omits ``title``, which this tool, unlike the
        application API, does not require; closing the keys must not start
        requiring it. The accepted set is the shared model's, asserted against
        the model rather than a written-out list.
        """
        from sage.models.schemas import BatchIngestParsedMetadata

        _, config = single_vault
        sources = Path(config.vault.storage_root)
        metadata = {"codes": ["PV06"], "version": "v1"}
        first = {"file_path": str(sources / "sample.md"), "source_type": "markdown"}

        def second(parsed: dict) -> dict:
            return {
                "file_path": str(sources / "second.md"),
                "source_type": "markdown",
                "parsed_metadata": parsed,
            }

        refused = _parse(
            await bulk_ingest_document(
                "test_vault", [first, second({**metadata, "bogus_field_x": 1})]
            )
        )
        control = _parse(await bulk_ingest_document("test_vault", [first, second(metadata)]))

        assert refused["error"] == "undeclared_key", refused
        assert refused["detail"]["parameter"] == "files.1.parsed_metadata"
        assert refused["detail"]["key"] == "bogus_field_x"
        assert refused["detail"]["recognized"] == sorted(BatchIngestParsedMetadata.model_fields)
        assert control["error_count"] == 0, control
        assert control["documents_created"]["new"] == 2

    @pytest.mark.parametrize(
        ("parsed", "parameter"),
        [
            pytest.param({"codes": "PV06"}, "files.1.parsed_metadata.codes", id="codes-string"),
            pytest.param({"title": 5}, "files.1.parsed_metadata.title", id="title-int"),
            pytest.param(
                {"codes": ["PV06", 5]}, "files.1.parsed_metadata.codes.1", id="codes-item-int"
            ),
            pytest.param({"tags": "alpha"}, "files.1.parsed_metadata.tags", id="tags-string"),
            pytest.param(
                {"tags": ["alpha", 5]}, "files.1.parsed_metadata.tags.1", id="tags-item-int"
            ),
            pytest.param("PV06", "files.1.parsed_metadata", id="not-a-mapping"),
        ],
    )
    async def test_wrong_typed_parsed_metadata_is_refused(
        self, single_vault, parsed, parameter, monkeypatch
    ):
        """A parsed_metadata value of the wrong type refuses the call before any delivery.

        Anti-coincidental-pass: a string ``codes`` used to split into one code
        per character and a non-string ``title`` failed in edge planning with an
        untranslated error, so the code and location assertions exclude both.
        The defect sits on the second entry, and the control -- the same
        entries with well-typed values -- must create both documents, which an
        entry the refused call had ingested first would turn into duplicates.
        A spy on the delivery gate shows the refusal precedes it: a check placed
        inside the delivery block, after tokens are redeemed, returns the same
        refusal but enters the gate first.
        """
        import sage.app_tools as app_tools

        deliveries: list[object] = []
        real_delivery = app_tools.caller_local_delivery

        def delivery_spy(*args, **kwargs):
            deliveries.append(args)
            return real_delivery(*args, **kwargs)

        monkeypatch.setattr(app_tools, "caller_local_delivery", delivery_spy)
        _, config = single_vault
        sources = Path(config.vault.storage_root)
        first = {"file_path": str(sources / "sample.md"), "source_type": "markdown"}

        def second(value: object) -> dict:
            return {
                "file_path": str(sources / "second.md"),
                "source_type": "markdown",
                "parsed_metadata": value,
            }

        refused = _parse(await bulk_ingest_document("test_vault", [first, second(parsed)]))
        assert deliveries == []
        control = _parse(
            await bulk_ingest_document(
                "test_vault", [first, second({"codes": ["PV06"], "title": "Second"})]
            )
        )

        assert refused["error"] == "invalid_parameter", refused
        assert refused["detail"]["parameter"] == parameter, refused
        assert control["error_count"] == 0, control
        assert control["documents_created"]["new"] == 2
        assert len(deliveries) == 1

    async def test_an_undeclared_name_wins_over_a_bad_value_in_the_same_object(self, single_vault):
        """Within one ``parsed_metadata``, the undeclared name is the answer.

        A malformed value and an undeclared key sit in the same object, and
        the two are refused under different codes, so which one wins is
        observable rather than a matter of wording. The name wins because a
        caller who removed the key would still have to be told about the
        value, while a caller told about the value first would fix it and be
        refused again for the key.
        """
        from sage.models.schemas import BatchIngestParsedMetadata

        _, config = single_vault
        sources = Path(config.vault.storage_root)
        entries = [
            {"file_path": str(sources / "sample.md"), "source_type": "markdown"},
            {
                "file_path": str(sources / "second.md"),
                "source_type": "markdown",
                "parsed_metadata": {"codes": "PV06", "bogus_field_x": 1},
            },
        ]

        refused = _parse(await bulk_ingest_document("test_vault", entries))

        assert refused["error"] == "undeclared_key", refused
        assert refused["detail"]["parameter"] == "files.1.parsed_metadata", refused
        assert refused["detail"]["key"] == "bogus_field_x", refused
        assert refused["detail"]["recognized"] == sorted(BatchIngestParsedMetadata.model_fields)

    async def test_the_two_batch_surfaces_recognize_different_entry_field_sets(self):
        """The entry-level accepted sets differ, and that is correct.

        An upload's bytes arrive as file parts, so a Core API entry declares no
        name for them; this tool's entries name a path or a transfer token.
        Stated here so a later reading of "both surfaces refuse the same key at
        the same place" does not grow into a parity assertion that is false --
        the shared properties are the location, the code and the choice among
        several, not the set a key is refused against.
        """
        from sage.app_tools import _FILE_ENTRY_FIELDS, _PARSED_METADATA_FIELDS
        from sage.models.schemas import BatchIngestFileMetadata, BatchIngestParsedMetadata

        upload_entry = set(BatchIngestFileMetadata.model_fields)

        assert "file_path" in _FILE_ENTRY_FIELDS
        assert "file_path" not in upload_entry
        assert upload_entry < set(_FILE_ENTRY_FIELDS)
        assert _PARSED_METADATA_FIELDS == set(BatchIngestParsedMetadata.model_fields)

    async def test_undeclared_name_on_a_later_entry_wins_over_an_earlier_bad_value(
        self, single_vault
    ):
        """Every entry's names are refused before any entry's values are checked.

        Anti-coincidental-pass: entry 0 carries a wrong-typed value and entry 1
        an undeclared key, so a check interleaving names and values per entry
        would report entry 0's ``codes`` rather than entry 1's key.
        """
        _, config = single_vault
        sources = Path(config.vault.storage_root)
        entries = [
            {
                "file_path": str(sources / "sample.md"),
                "source_type": "markdown",
                "parsed_metadata": {"codes": "PV06"},
            },
            {
                "file_path": str(sources / "second.md"),
                "source_type": "markdown",
                "parsed_metadata": {"bogus_field_x": 1},
            },
        ]

        refused = _parse(await bulk_ingest_document("test_vault", entries))

        assert refused["error"] == "undeclared_key", refused
        assert refused["detail"]["parameter"] == "files.1.parsed_metadata", refused
        assert refused["detail"]["key"] == "bogus_field_x", refused

    async def test_undeclared_name_wins_over_a_codes_and_tags_conflict(self, single_vault):
        """Names are settled before the codes-and-tags conflict is asked about.

        Anti-coincidental-pass: entry 0 carries the conflict and entry 1 an
        undeclared key, so a conflict check placed before the name refusal
        reports entry 0's ``tags`` rather than entry 1's key.
        """
        _, config = single_vault
        sources = Path(config.vault.storage_root)
        entries = [
            {
                "file_path": str(sources / "sample.md"),
                "source_type": "markdown",
                "parsed_metadata": {"codes": ["PV06"], "tags": ["alpha"]},
            },
            {
                "file_path": str(sources / "second.md"),
                "source_type": "markdown",
                "parsed_metadata": {"bogus_field_x": 1},
            },
        ]

        refused = _parse(await bulk_ingest_document("test_vault", entries))

        assert refused["error"] == "undeclared_key", refused
        assert refused["detail"]["parameter"] == "files.1.parsed_metadata", refused
        assert refused["detail"]["key"] == "bogus_field_x", refused

    async def test_undeclared_name_wins_over_a_malformed_digest(self, single_vault):
        """One entry, an undeclared key and a malformed ``sha256``: the key is reported,
        as the Core API upload and the application's ingest route report it."""
        _, config = single_vault
        entry = {
            "file_path": str(Path(config.vault.storage_root) / "sample.md"),
            "source_type": "markdown",
            "sha256": "not-a-digest",
        }

        control = _parse(await bulk_ingest_document("test_vault", [entry]))
        refused = _parse(await bulk_ingest_document("test_vault", [{**entry, "bogus_field_x": 1}]))

        assert control["error"] == "invalid_sha256", control
        assert refused["error"] == "undeclared_key", refused
        assert refused["detail"]["parameter"] == "files.0", refused
        assert refused["detail"]["keys"] == ["bogus_field_x"], refused

    @pytest.mark.parametrize("dry_run", [False, True])
    async def test_every_undeclared_key_in_one_object_is_named(self, single_vault, dry_run):
        """Two undeclared keys, written in reverse sorted order, are named once, sorted."""
        _, config = single_vault
        entry = {
            "file_path": str(Path(config.vault.storage_root) / "sample.md"),
            "source_type": "markdown",
            "parsed_metadata": {"zulu_x": 1, "alpha_x": 2},
        }

        refused = _parse(await bulk_ingest_document("test_vault", [entry], dry_run=dry_run))

        assert refused["error"] == "undeclared_key", refused
        assert refused["detail"]["parameter"] == "files.0.parsed_metadata", refused
        assert refused["detail"]["keys"] == ["alpha_x", "zulu_x"], refused
        assert refused["detail"]["key"] == "alpha_x", refused

    async def test_transfer_token_entry_is_not_an_undeclared_key(self, single_vault):
        """``transfer_token`` is a declared delivery shape, so it reaches the transfer gate."""
        result = _parse(
            await bulk_ingest_document(
                "test_vault", [{"transfer_token": "not-a-real-token", "source_type": "markdown"}]
            )
        )

        assert result.get("error") != "invalid_parameter", result
        assert result.get("error") == "transfer_token_invalid", result

    async def test_sha256_entry_is_not_an_undeclared_key(self, single_vault):
        """``sha256`` is a declared entry name, so a well-formed one reaches ingest."""
        _, config = single_vault
        src = Path(config.vault.storage_root) / "declared_sha.md"
        body = b"# Declared sha256\n"
        src.write_bytes(body)

        result = _parse(
            await bulk_ingest_document(
                "test_vault",
                [
                    {
                        "file_path": str(src),
                        "source_type": "markdown",
                        "sha256": hashlib.sha256(body).hexdigest(),
                    }
                ],
            )
        )

        assert result.get("error") is None, result
        assert result["error_count"] == 0, result
        assert result["documents_created"]["new"] == 1

    async def test_bulk_colocated_digest_mismatch_is_per_file_error(self, single_vault):
        """A declared digest one file's bytes lack is that file's error, not the batch's.

        Anti-coincidental-pass: the mismatched entry is second, so the refusal
        must be located at its own index; and the first entry must still be
        created, so a whole-call refusal fails.
        """
        _, config = single_vault
        sources = Path(config.vault.storage_root)
        good, bad = sources / "digest_good.md", sources / "digest_bad.md"
        good.write_bytes(b"# Good digest\n")
        bad.write_bytes(b"# Bad digest\n")

        result = _parse(
            await bulk_ingest_document(
                "test_vault",
                [
                    {
                        "file_path": str(good),
                        "source_type": "markdown",
                    },
                    {
                        "file_path": str(bad),
                        "source_type": "markdown",
                        "sha256": hashlib.sha256(b"not the bad file").hexdigest(),
                    },
                ],
            )
        )

        assert result.get("error") is None, result
        assert result["error_count"] == 1, result
        assert result["documents_created"]["new"] == 1
        (entry,) = result["errors"]
        assert entry["code"] == "source_digest_mismatch"
        assert entry["file_index"] == 1

    async def test_bulk_malformed_sha256_refuses_whole_call(self, single_vault):
        """A malformed digest on any entry refuses the batch before anything is ingested.

        The malformed value sits on the second entry, and the control ingests
        the same first entry afterwards and must create it: had the refused
        call ingested it, the control would find duplicate content.
        """
        _, config = single_vault
        src = Path(config.vault.storage_root) / "before_malformed.md"
        src.write_bytes(b"# Before malformed\n")
        first = {"file_path": str(src), "source_type": "markdown"}
        second = {
            "file_path": str(src.with_name("second_malformed.md")),
            "source_type": "markdown",
            "sha256": "nothex",
        }

        refused = _parse(await bulk_ingest_document("test_vault", [first, second]))
        control = _parse(await bulk_ingest_document("test_vault", [first]))

        assert refused.get("error") == "invalid_sha256", refused
        assert refused["detail"]["files.1.sha256"] == "nothex"
        assert control["error_count"] == 0, control
        assert control["documents_created"]["new"] == 1

    async def test_needs_review_false_commits_metadata_as_authoritative(self, single_vault):
        """``needs_review=False`` commits the caller's metadata and queues nothing.

        Asserted against a paired control: the same call at the default must
        land every document in the queue. Without that arm a vault that simply
        never queued would pass the False arm.

        Anti-coincidental-pass: a ``needs_review`` accepted at the signature and
        dropped before the service call -- the defect this closes, wearing a new
        argument -- leaves both arms queued, so the False arm fails on
        ``metadata_confirmed`` and on the empty queue alike. The two arms carry
        distinct bytes because identical bytes are refused as duplicate content.
        """
        _, config = single_vault
        sources = Path(config.vault.storage_root)
        authoritative = sources / "authoritative.md"
        authoritative.write_text("# Authoritative\n\nCommitted as supplied.")
        queued = sources / "queued.md"
        queued.write_text("# Queued\n\nAwaiting confirmation.")

        def entry(path: Path, title: str) -> dict:
            return {
                "file_path": str(path),
                "source_type": "markdown",
                "parsed_metadata": {"title": title, "doc_type": "note"},
            }

        committed = _parse(
            await bulk_ingest_document(
                "test_vault",
                [entry(authoritative, "Authoritative")],
                needs_review=False,
            )
        )
        assert committed["error_count"] == 0, committed
        assert committed["documents_created"]["new"] == 1
        assert committed["metadata_pending"] == 0, committed
        pending_after_commit = _parse(await list_pending_metadata("test_vault"))
        assert pending_after_commit["items"] == [], pending_after_commit

        reviewed = _parse(await bulk_ingest_document("test_vault", [entry(queued, "Queued")]))
        assert reviewed["error_count"] == 0, reviewed
        assert reviewed["metadata_pending"] == 1, reviewed
        pending_after_review = _parse(await list_pending_metadata("test_vault"))

        queued_titles = {i["document"]["title"] for i in pending_after_review["items"]}
        assert queued_titles == {"Queued"}, pending_after_review

    async def test_per_file_tier3_metadata_lands_on_each_document(self, tier3_vault):
        """Each entry's Tier-3 payload reaches its own document, by value.

        Anti-coincidental-pass: the two entries carry *different* payloads, so a
        plumbing defect that smeared one across the batch, or let the last
        entry's win, fails. Asserting only that a payload is present somewhere
        would not -- containment says nothing about which document got which.
        """
        _, config = tier3_vault
        sources = Path(config.vault.storage_root)
        first = sources / "ticket_one.md"
        first.write_text("# Ticket one\n\nFirst body.")
        second = sources / "ticket_two.md"
        second.write_text("# Ticket two\n\nSecond body.")

        result = _parse(
            await bulk_ingest_document(
                "tier3_vault",
                [
                    {
                        "file_path": str(first),
                        "source_type": "markdown",
                        "parsed_metadata": {"title": "Ticket one", "doc_type": "ticket"},
                        "tier3_metadata": {"ticket_id": "T-0001", "ticket_priority": "high"},
                    },
                    {
                        "file_path": str(second),
                        "source_type": "markdown",
                        "parsed_metadata": {"title": "Ticket two", "doc_type": "ticket"},
                        "tier3_metadata": {"ticket_id": "T-0002", "ticket_priority": "low"},
                    },
                ],
                needs_review=False,
            )
        )

        assert result["error_count"] == 0, result
        assert result["documents_created"]["new"] == 2
        catalog = _parse(await search(vault_id="tier3_vault", mode="catalog"))
        by_title = {
            r["document"]["title"]: r["document"]["tier3_metadata"] for r in catalog["results"]
        }
        assert by_title == {
            "Ticket one": {"ticket_id": "T-0001", "ticket_priority": "high"},
            "Ticket two": {"ticket_id": "T-0002", "ticket_priority": "low"},
        }

    async def test_tier3_schema_violation_is_a_per_file_error(self, tier3_vault):
        """A payload the doc_type's schema rejects is that file's error, not the batch's.

        Anti-coincidental-pass: a refusal raised for the whole call would also
        report the violation, so the sibling document's existence and the error
        count are what discriminate -- one file refused, the other ingested.
        """
        _, config = tier3_vault
        sources = Path(config.vault.storage_root)
        good = sources / "ticket_good.md"
        good.write_text("# Ticket good\n\nValid payload.")
        bad = sources / "ticket_bad.md"
        bad.write_text("# Ticket bad\n\nPayload the schema rejects.")

        result = _parse(
            await bulk_ingest_document(
                "tier3_vault",
                [
                    {
                        "file_path": str(good),
                        "source_type": "markdown",
                        "parsed_metadata": {"title": "Ticket good", "doc_type": "ticket"},
                        "tier3_metadata": {"ticket_id": "T-0003"},
                    },
                    {
                        "file_path": str(bad),
                        "source_type": "markdown",
                        "parsed_metadata": {"title": "Ticket bad", "doc_type": "ticket"},
                        "tier3_metadata": {"ticket_id": "not-a-ticket-id"},
                    },
                ],
                needs_review=False,
            )
        )

        assert result.get("error") is None, result
        assert result["error_count"] == 1, result
        assert result["documents_created"]["new"] == 1, result
        assert result["errors"][0]["file_index"] == 1, result
        assert result["errors"][0]["code"] == "tier3_schema_violation", result

    async def test_per_file_tags_land_whole_on_the_document(self, single_vault):
        """``tags`` is carried as a list, so a tag containing a comma stays one tag.

        Anti-coincidental-pass: ``codes`` reaches the same field through a
        comma-join and a downstream re-split, so routing ``tags`` that way would
        turn the probe tag into two. The probe is chosen to make that visible.
        A second entry supplies no tags and is asserted to have none, so an
        implementation applying one entry's tags across the batch fails too --
        the comma probe alone says nothing about which documents they reached.
        """
        _, config = single_vault
        sources = Path(config.vault.storage_root)
        src = sources / "tagged.md"
        src.write_text("# Tagged\n\nBody.")
        untagged = sources / "untagged.md"
        untagged.write_text("# Untagged\n\nDifferent body.")

        result = _parse(
            await bulk_ingest_document(
                "test_vault",
                [
                    {
                        "file_path": str(src),
                        "source_type": "markdown",
                        "parsed_metadata": {
                            "title": "Tagged",
                            "doc_type": "note",
                            "tags": ["alpha,beta", "gamma"],
                        },
                    },
                    {
                        "file_path": str(untagged),
                        "source_type": "markdown",
                        "parsed_metadata": {"title": "Untagged", "doc_type": "note"},
                    },
                ],
                needs_review=False,
            )
        )

        assert result["error_count"] == 0, result
        catalog = _parse(await search(vault_id="test_vault", mode="catalog"))
        by_title = {r["document"]["title"]: r["document"] for r in catalog["results"]}
        assert by_title["Tagged"]["tags"] == ["alpha,beta", "gamma"], by_title["Tagged"]
        assert by_title["Untagged"]["tags"] == [], by_title["Untagged"]

    async def test_codes_and_tags_together_are_refused(self, single_vault, monkeypatch):
        """An entry supplying both is refused before any delivery, located at ``tags``.

        Anti-coincidental-pass: an implementation detecting the conflict per
        file, after the tokens are redeemed, returns the same envelope. The
        delivery spy is what separates the two -- the refusal has to precede the
        gate. The control, the same entries with one of the two, must ingest.
        """
        import sage.app_tools as app_tools

        deliveries: list[object] = []
        real_delivery = app_tools.caller_local_delivery

        def delivery_spy(*args, **kwargs):
            deliveries.append(args)
            return real_delivery(*args, **kwargs)

        monkeypatch.setattr(app_tools, "caller_local_delivery", delivery_spy)
        _, config = single_vault
        sources = Path(config.vault.storage_root)
        first = {"file_path": str(sources / "sample.md"), "source_type": "markdown"}

        def second(parsed: dict) -> dict:
            return {
                "file_path": str(sources / "second.md"),
                "source_type": "markdown",
                "parsed_metadata": parsed,
            }

        refused = _parse(
            await bulk_ingest_document(
                "test_vault", [first, second({"codes": ["PV06"], "tags": ["alpha"]})]
            )
        )
        assert deliveries == []
        control = _parse(
            await bulk_ingest_document("test_vault", [first, second({"tags": ["alpha"]})])
        )

        assert refused["error"] == "invalid_parameter", refused
        assert refused["detail"]["parameter"] == "files.1.parsed_metadata.tags", refused
        assert control["error_count"] == 0, control
        assert control["documents_created"]["new"] == 2
        assert len(deliveries) == 1

    async def test_tier3_metadata_entry_is_not_an_undeclared_key(self, tier3_vault):
        """``tier3_metadata`` is a declared entry name, so a payload reaches ingest."""
        _, config = tier3_vault
        src = Path(config.vault.storage_root) / "declared_tier3.md"
        src.write_text("# Declared tier3\n\nBody.")

        result = _parse(
            await bulk_ingest_document(
                "tier3_vault",
                [
                    {
                        "file_path": str(src),
                        "source_type": "markdown",
                        "parsed_metadata": {"title": "Declared tier3", "doc_type": "ticket"},
                        "tier3_metadata": {"ticket_id": "T-0004"},
                    }
                ],
            )
        )

        assert result.get("error") is None, result
        assert result["error_count"] == 0, result
        assert result["documents_created"]["new"] == 1

    async def test_empty_tier3_payload_is_a_payload_not_an_absence(self, tier3_vault):
        """``tier3_metadata: {}`` is validated; an omitted key is not.

        At ingest a payload that is not ``None`` overrides whatever tier-3
        metadata the adapter extracted and is then validated, so an explicit
        empty mapping is refused by a schema declaring a required field while
        an omitted key leaves validation with nothing to check. Folding ``{}``
        to ``None`` at this boundary ingests the file instead, giving the same
        entry a different outcome from the one the Core API batch route gives
        it -- which
        ``test_b37_an_explicit_empty_tier3_payload_is_a_payload_not_an_absence``
        asserts from the other side.

        Anti-coincidental-pass: the doc_type's ``required`` field is what makes
        the two separable at all -- against a no-schema or all-optional
        doc_type both arms succeed and this passes against the divergence it
        exists to catch. The omitted-key arm is the paired control: it must
        still ingest, or the test would also pass against a boundary that
        refused every entry.
        """
        _, config = tier3_vault
        sources = Path(config.vault.storage_root)
        empty_src = sources / "strict_empty.md"
        empty_src.write_text("# Strict empty\n\nBody.")
        omitted_src = sources / "strict_omitted.md"
        omitted_src.write_text("# Strict omitted\n\nOther body.")

        async def ingest(path: Path, entry: dict) -> dict:
            return _parse(
                await bulk_ingest_document(
                    "tier3_vault",
                    [
                        {
                            "file_path": str(path),
                            "source_type": "markdown",
                            "parsed_metadata": {"doc_type": "strict_ticket"},
                            **entry,
                        }
                    ],
                    infer_edges=False,
                    needs_review=False,
                )
            )

        empty = await ingest(empty_src, {"tier3_metadata": {}})
        omitted = await ingest(omitted_src, {})

        assert empty["error_count"] == 1, empty
        assert empty["errors"][0]["code"] == "tier3_schema_violation", empty
        assert omitted["error_count"] == 0, omitted
        assert omitted["documents_created"]["new"] == 1, omitted

    @pytest.mark.parametrize(
        "value", [pytest.param("T-0001", id="string"), pytest.param(["T-0001"], id="list")]
    )
    async def test_non_mapping_tier3_metadata_is_refused(self, single_vault, value):
        """A ``tier3_metadata`` that is not a mapping refuses the call, located at itself."""
        _, config = single_vault
        sources = Path(config.vault.storage_root)

        refused = _parse(
            await bulk_ingest_document(
                "test_vault",
                [
                    {"file_path": str(sources / "sample.md"), "source_type": "markdown"},
                    {
                        "file_path": str(sources / "second.md"),
                        "source_type": "markdown",
                        "tier3_metadata": value,
                    },
                ],
            )
        )

        assert refused["error"] == "invalid_parameter", refused
        assert refused["detail"]["parameter"] == "files.1.tier3_metadata", refused


# ---------------------------------------------------------------------------
# 9. Cross-Cutting Conventions (MCP-023, MCP-024, MCP-025)
# ---------------------------------------------------------------------------


class TestMCPConventions:
    async def test_mcp_023_all_return_serializable_dicts(self, single_vault, tmp_path):
        """All tools return dicts that are JSON-serializable."""
        services, config = single_vault
        sources = Path(config.vault.storage_root)
        scan_dir = tmp_path / "json_test"
        scan_dir.mkdir()
        (scan_dir / "test.md").write_text("# Test")

        results = [
            await list_vaults(),
            await get_vault_stats("test_vault"),
            await verify_hash("test_vault", []),
            await list_staging_edges("test_vault"),
            await list_pending_metadata("test_vault"),
            await list_directory("test_vault", str(scan_dir)),
            await bulk_ingest_document(
                "test_vault",
                [
                    {"file_path": str(sources / "sample.md"), "source_type": "markdown"},
                ],
            ),
        ]
        for r in results:
            assert isinstance(r, dict)
            json.dumps(r, default=str)  # Should not raise

    async def test_mcp_024_unknown_vault_error(self, single_vault):
        """App tools with unknown vault_id return structured error."""
        result = _parse(await list_directory("nonexistent", "/tmp"))
        assert result["error"] == "vault_not_found"

    async def test_mcp_025_tool_naming_convention(self):
        """Tool naming follows the verb-convention naming rule (CAS-ADR-033).

        Post the verb-sweep rename, MCP tool names omit the legacy
        ``sage_``, ``sage_admin_``, and ``app_`` inner prefixes (the
        two-server design in CAS-ADR-034 makes them vestigial) and
        begin with a canonical verb. The deeper conformance gate lives
        in ``tests/sage/test_mcp_rename_compliance.py``; this test
        keeps a smoke-level smoke check colocated with the app-tool
        suite.
        """
        sage_tools = [
            # Vault-scoped tools carry no surface prefix (CAS-ADR-029):
            # placement is the surface-assignment table's alone. The
            # record-collection hash query is the plural-noun
            # ``verify_hashes``.
            "list_vaults",
            "get_vault_stats",
            "verify_hashes",
            "list_staging_edges",
            "update_staging_edge",
            "list_pending_metadata",
        ]
        app_tools = ["list_directory", "bulk_ingest_document"]

        legacy_prefixes = ("sage_", "sage_admin_", "app_")
        for name in sage_tools + app_tools:
            assert not name.startswith(legacy_prefixes), (
                f"Tool {name!r} still carries a legacy inner prefix. "
                "See CAS-ADR-033 for the verb convention."
            )


# ---------------------------------------------------------------------------
# 10. search catalog mode (MCP-026, MCP-027)
# ---------------------------------------------------------------------------


class TestSageDiscoverCatalog:
    async def _seed_docs(self, services):
        """Insert 5 documents for catalog mode tests."""
        from sage.models.schemas import Document

        gs = services.graph_store
        now = datetime.now(timezone.utc)

        def _doc(doc_id, doc_type="design_spec", tags=None, lifecycle="active"):
            return Document(
                id=_id(doc_id),
                title=f"Test {doc_id}",
                source_type=SourceType.MARKDOWN,
                source_path=f"test/{doc_id}.md",
                lifecycle_status=lifecycle,
                source_content_hash=_sha(doc_id),
                adapter_version="0.1.0",
                created_by="testuser",
                created_at=now,
                last_modified_by="testuser",
                updated_at=now,
                projected_at=now,
                pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
                doc_type=doc_type,
                tags=tags or [],
            )

        await gs.insert_document(_doc("doc_a", "design_spec", ["PV07"]))
        await gs.insert_document(_doc("doc_b", "glossary", ["PV07"]))
        await gs.insert_document(_doc("doc_c", "design_spec", ["PV08"]))

    async def test_mcp_026_catalog_returns_filtered(self, single_vault):
        """search catalog mode returns filtered documents."""
        services, config = single_vault
        await self._seed_docs(services)

        result = _parse(
            await search(
                vault_id="test_vault",
                mode="catalog",
                scope="filtered",
                filters={"tags": ["PV07"]},
            )
        )

        assert result["mode"] == "catalog"
        assert result["total_available"] == 2
        assert len(result["results"]) == 2
        result_ids = {r["document"]["id"] for r in result["results"]}
        assert result_ids == {_id("doc_a"), _id("doc_b")}
        # No chunk content or relevance scores
        for r in result["results"]:
            assert r.get("chunk_content") is None
            assert r.get("relevance_score") is None

    async def test_mcp_027_catalog_pagination_offset(self, single_vault):
        """search catalog mode pagination with offset."""
        services, config = single_vault
        await self._seed_docs(services)

        resp1 = _parse(
            await search(
                vault_id="test_vault",
                mode="catalog",
                limit=2,
                offset=0,
            )
        )
        resp2 = _parse(
            await search(
                vault_id="test_vault",
                mode="catalog",
                limit=2,
                offset=2,
            )
        )

        assert resp1["total_available"] == 3
        assert len(resp1["results"]) == 2
        assert resp2["total_available"] == 3
        assert len(resp2["results"]) == 1

        ids1 = {r["document"]["id"] for r in resp1["results"]}
        ids2 = {r["document"]["id"] for r in resp2["results"]}
        assert len(ids1 & ids2) == 0  # No overlap

    async def test_catalog_edges_through_app_wrapper(self, single_vault):
        """Sage_discover(target="edges") wired end-to-end through
        the app-layer MCP adapter. Confirms the new target dispatch
        propagates through the same path that doc-target catalog uses
        and that the response shape arrives at the wire intact.
        """
        from sage.models.enums import EdgeType
        from sage.models.schemas import Document, Edge

        services, _ = single_vault
        gs = services.graph_store
        now = datetime.now(timezone.utc)

        def _doc(doc_id):
            return Document(
                id=_id(doc_id),
                title=f"T0157 {doc_id}",
                source_type=SourceType.MARKDOWN,
                source_path=f"test/{doc_id}.md",
                lifecycle_status="active",
                source_content_hash=_sha(doc_id),
                adapter_version="0.1.0",
                created_by="testuser",
                created_at=now,
                last_modified_by="testuser",
                updated_at=now,
                projected_at=now,
                pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
            )

        await gs.insert_document(_doc("t0157_src"))
        await gs.insert_document(_doc("t0157_tgt"))
        await gs.insert_edge(
            Edge(
                id=str(uuid.uuid4()),
                source_id=_id("t0157_src"),
                target_id=_id("t0157_tgt"),
                edge_type=EdgeType.REFERENCES,
                rationale="t0157 app-smoke",
                created_at=now,
            )
        )

        result = _parse(
            await search(
                vault_id="test_vault",
                mode="catalog",
                target="edges",
                filters={"source_id": _id("t0157_src")},
                response_mode="full",
            )
        )

        assert result["mode"] == "catalog"
        assert result["target"] == "edges"
        assert result["total_available"] == 1
        hit = result["results"][0]
        assert hit["source_id"] == _id("t0157_src")
        assert hit["target_id"] == _id("t0157_tgt")
        assert hit["edge_type"] == "references"
        assert hit["rationale"] == "t0157 app-smoke"

    async def test_catalog_facets_through_app_wrapper(self, single_vault):
        """search(target="facets") wired end-to-end through the
        app-layer MCP adapter. Confirms the facet dispatch propagates
        through the same path that doc-target catalog uses and that the
        facet rows arrive at the wire intact.
        """
        from sage.models.schemas import Document

        services, _ = single_vault
        gs = services.graph_store
        now = datetime.now(timezone.utc)

        def _doc(doc_id, doc_type=None, tags=None):
            return Document(
                id=_id(doc_id),
                title=f"Facet {doc_id}",
                source_type=SourceType.MARKDOWN,
                source_path=f"test/{doc_id}.md",
                lifecycle_status="active",
                source_content_hash=_sha(doc_id),
                adapter_version="0.1.0",
                created_by="testuser",
                created_at=now,
                last_modified_by="testuser",
                updated_at=now,
                projected_at=now,
                pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
                doc_type=doc_type,
                tags=tags or [],
            )

        await gs.insert_document(_doc("facet_a", doc_type="note", tags=["alpha"]))
        await gs.insert_document(_doc("facet_b", doc_type="note"))

        result = _parse(
            await search(
                vault_id="test_vault",
                mode="catalog",
                target="facets",
            )
        )

        assert result["mode"] == "catalog"
        assert result["target"] == "facets"
        assert result["total_available"] == 2
        by_field = {r["field"]: r for r in result["results"]}
        assert by_field["doc_type"]["values"] == {"note": 2}
        assert by_field["doc_type"]["total_distinct"] == 1
        assert by_field["tags"]["values"] == {"alpha": 1}
        assert by_field["tags"]["total_distinct"] == 1

    async def test_default_facets_response_bounded_at_scale(self, single_vault):
        """The default facets call stays under a fixed size bound on a
        vault whose tag vocabulary dwarfs its declared vocabularies.

        The regression this pins: an unbounded tag facet once grew with
        the corpus until the orientation call -- the recommended FIRST
        call against an unfamiliar vault -- exceeded the calling
        client's tool-result ceiling. The fixture makes the trap
        structural rather than statistical: 240 distinct ~88-char tags
        mean the uncapped payload necessarily exceeds the bound (the
        in-test arithmetic guard asserts so), so the size assertion
        cannot pass while the default cap is broken.
        """
        from sage.models.schemas import Document

        services, _ = single_vault
        gs = services.graph_store
        now = datetime.now(timezone.utc)

        n_docs, tags_per_doc, tag_width = 120, 2, 88
        for i in range(n_docs):
            doc_id = _id(f"widetag_{i:04d}")
            tags = [
                f"tag-{i * tags_per_doc + j:04d}-".ljust(tag_width, "x")
                for j in range(tags_per_doc)
            ]
            await gs.insert_document(
                Document(
                    id=doc_id,
                    title=f"Wide-tag doc {i:04d}",
                    source_type=SourceType.MARKDOWN,
                    source_path=f"test/widetag_{i:04d}.md",
                    lifecycle_status="active",
                    source_content_hash=_sha(doc_id),
                    adapter_version="0.1.0",
                    created_by="testuser",
                    created_at=now,
                    last_modified_by="testuser",
                    updated_at=now,
                    projected_at=now,
                    pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
                    doc_type="note",
                    tags=tags,
                )
            )

        size_bound = 16 * 1024
        vocabulary = n_docs * tags_per_doc
        # Arithmetic guard: the uncapped tag payload alone must exceed
        # the bound, so a short-tag fixture cannot make the size
        # assertion pass by luck while the cap is broken.
        assert vocabulary * tag_width > size_bound

        raw = await search(vault_id="test_vault", mode="catalog", target="facets")
        result = _parse(raw)

        tags_row = next(r for r in result["results"] if r["field"] == "tags")
        assert len(tags_row["values"]) == 50
        assert tags_row["total_distinct"] == vocabulary
        serialized = raw if isinstance(raw, str) else json.dumps(raw)
        assert len(serialized.encode()) < size_bound, (
            f"default facets response must stay bounded; got "
            f"{len(serialized.encode())} bytes against {size_bound}"
        )

    async def _seed_portfolio(self, services, n: int):
        """Seed ``n`` ticket-shaped docs through the graph store."""
        gs = services.graph_store
        now = datetime.now(timezone.utc)
        for i in range(n):
            doc_id = _id(f"t0091_mcp_{i:04d}")
            await gs.insert_document(
                Document(
                    id=doc_id,
                    title=f"Test ticket portfolio entry {i:04d}",
                    source_type=SourceType.MARKDOWN,
                    source_path=f"imports/t0091_mcp_{i:04d}.md",
                    lifecycle_status="active",
                    source_content_hash=_sha(doc_id),
                    adapter_version="0.1.0",
                    created_by="testuser",
                    created_at=now,
                    last_modified_by="testuser",
                    updated_at=now,
                    projected_at=now,
                    pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
                    doc_type="ticket",
                    tags=["ticket", "phase-2", "sage"],
                    tier3_metadata={
                        "ticket_id": f"T-9{i:04d}",
                        "ticket_type": "feature",
                        "ticket_priority": "medium",
                    },
                )
            )

    async def test_mcp_028_catalog_budget_hint_surfaces_through_mcp_wrapper(
        self, single_vault, monkeypatch
    ):
        """Budget hint survives Pydantic→dict serialization across the MCP boundary."""
        monkeypatch.setenv("SAGE_MCP_INLINE_BUDGET_BYTES", "4096")
        services, _ = single_vault
        await self._seed_portfolio(services, 60)

        result = _parse(
            await search(
                vault_id="test_vault",
                mode="catalog",
                filters={"doc_type": "ticket"},
                limit=100,
            )
        )

        assert result["mode"] == "catalog"
        hints = result.get("hints")
        assert hints is not None, "hints dict not surfaced through MCP wrapper"
        assert hints.get("reason") == "response_exceeds_inline_budget"
        assert hints.get("budget_bytes") == 4096
        recommended = hints.get("recommended_limit")
        assert isinstance(recommended, int)
        assert 1 <= recommended < 100

    async def test_degraded_catalog_response_fits_the_delivered_ceiling(self, single_vault):
        """The degrade delivers inline, measured on the bytes the client bounds.

        Every service-level test of this change measures the response the
        service built. The runtime encodes that dict a second time, with
        indentation, and the whole claim -- that a caller gets a structured
        answer instead of a file path -- is about the encoding the client
        counts. So the bytes are read off the TextContent the runtime
        produced, at the production budget, on a portfolio shaped like a real
        one.

        Anti-coincidental-pass: the full-shape arm is asserted strictly over
        the ceiling in the same delivered units. Without it the degrade arm
        is satisfied by a payload that was never over budget, and the pair
        would report a crossing that never happened. The row-key assertion
        is the second half: a policy that announced the degrade in its hint
        and left the rows alone satisfies the reason check and fails here.

        The row count is derived from the budget for the same reason its
        service-level siblings derive theirs -- fixed at a literal, it would
        stop crossing the line the first time the budget was recalibrated
        upward, and take the guarantee with it.
        """
        from mcp.types import TextContent

        from sage.services.retrieval import DEFAULT_MCP_INLINE_BUDGET_BYTES

        row_count = DEFAULT_MCP_INLINE_BUDGET_BYTES // 500
        services, _ = single_vault
        await self._seed_portfolio(services, row_count)

        async def delivered(**kwargs) -> tuple[int, dict]:
            out = await _mcp.mcp.call_tool(
                "search",
                {
                    "vault_id": "test_vault",
                    "mode": "catalog",
                    "filters": {"doc_type": "ticket"},
                    "limit": row_count,
                    **kwargs,
                },
            )
            text = next(c.text for c in out if isinstance(c, TextContent))
            return len(text.encode("utf-8")), json.loads(text)

        full_bytes, full = await delivered(response_mode="full")
        assert full_bytes > DEFAULT_MCP_INLINE_BUDGET_BYTES, (
            f"{row_count} rows delivered {full_bytes}B in the full shape, which "
            f"does not overrun the {DEFAULT_MCP_INLINE_BUDGET_BYTES}-byte budget; "
            "the fixture no longer crosses the line it exists to cross"
        )
        assert full["hints"]["reason"] == "response_exceeds_inline_budget"

        degraded_bytes, degraded = await delivered()
        assert degraded_bytes <= DEFAULT_MCP_INLINE_BUDGET_BYTES, (
            f"degraded response delivered {degraded_bytes}B against the "
            f"{DEFAULT_MCP_INLINE_BUDGET_BYTES}-byte budget"
        )
        assert degraded["hints"]["reason"] == "catalog_response_degraded_to_light"
        assert degraded["hints"]["carried_shape"] == "DocumentSummaryLight"
        assert len(degraded["results"]) == len(full["results"]) == row_count

        light_keys = {"id", "title", "lifecycle_status", "doc_type", "tier3_metadata"}
        for row in degraded["results"]:
            assert set(row["document"]) <= light_keys, (
                f"degraded row carries fields outside the light shape: "
                f"{set(row['document']) - light_keys}"
            )

    async def test_excerpted_scored_response_fits_the_delivered_ceiling(self, single_vault):
        """A keyword response over the ceiling is excerpted and delivered inline.

        Measured on the TextContent the runtime produced, at the production
        budget, because the claim -- a caller gets passages instead of a file
        path -- is about the encoding the client counts, and the store here is
        the real content store rather than the stub the service-level tests
        search.

        Anti-coincidental-pass: the ``response_mode="full"`` arm, which
        suppresses the excerpt, is asserted strictly over the ceiling in the
        same delivered units, so the excerpted arm cannot be satisfied by a
        payload that was never over budget. The passage assertion is the
        second half: a policy that announced the excerpt and left the
        passages whole satisfies the reason check and fails there.
        """
        from mcp.types import TextContent

        from sage.adapters.interfaces import Chunk
        from sage.services.retrieval import DEFAULT_MCP_INLINE_BUDGET_BYTES

        services, _ = single_vault
        passage = "Quarterly filing procedure, section body text. " * 250
        row_count = 10
        assert row_count * len(passage) > DEFAULT_MCP_INLINE_BUDGET_BYTES
        embedder = StubEmbeddingProvider()
        now = datetime.now(timezone.utc)
        for i in range(row_count):
            doc_id = _id(f"scored_excerpt_mcp_{i:02d}")
            await services.graph_store.insert_document(
                Document(
                    id=doc_id,
                    title=f"Filing procedure {i:02d}",
                    source_type=SourceType.MARKDOWN,
                    source_path=f"imports/scored_excerpt_mcp_{i:02d}.md",
                    lifecycle_status="active",
                    source_content_hash=_sha(doc_id),
                    adapter_version="0.1.0",
                    created_by="testuser",
                    created_at=now,
                    last_modified_by="testuser",
                    updated_at=now,
                    projected_at=now,
                    pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
                    doc_type="ticket",
                )
            )
            chunk = Chunk(
                document_id=doc_id,
                heading_path="Procedure",
                content=passage,
                chunk_index=0,
                doc_type="ticket",
                lifecycle_status="active",
            )
            [chunk.embedding] = await embedder.embed([passage])
            await services.content_store.index_chunks(doc_id, [chunk])

        async def delivered(**kwargs) -> tuple[int, dict]:
            out = await _mcp.mcp.call_tool(
                "search",
                {
                    "vault_id": "test_vault",
                    "mode": "keyword",
                    "query": "filing procedure",
                    "limit": row_count,
                    **kwargs,
                },
            )
            text = next(c.text for c in out if isinstance(c, TextContent))
            return len(text.encode("utf-8")), json.loads(text)

        full_bytes, full = await delivered(response_mode="full")
        assert len(full["results"]) == row_count
        assert full_bytes > DEFAULT_MCP_INLINE_BUDGET_BYTES, (
            f"{row_count} passages delivered {full_bytes}B whole, which does not "
            f"overrun the {DEFAULT_MCP_INLINE_BUDGET_BYTES}-byte budget; the "
            "fixture no longer crosses the line it exists to cross"
        )
        assert full["hints"]["reason"] == "response_exceeds_inline_budget"

        excerpted_bytes, excerpted = await delivered()
        assert excerpted_bytes <= DEFAULT_MCP_INLINE_BUDGET_BYTES, (
            f"excerpted response delivered {excerpted_bytes}B against the "
            f"{DEFAULT_MCP_INLINE_BUDGET_BYTES}-byte budget"
        )
        hints = excerpted["hints"]
        assert hints["reason"] == "scored_response_excerpted"
        assert hints["excerpted_count"] == row_count
        assert len(excerpted["results"]) == row_count
        for hit in excerpted["results"]:
            assert hit["chunk_content"] == passage[: hints["excerpt_chars"]]

    async def test_facets_budget_hint_surfaces_through_mcp_wrapper(self, single_vault):
        """The facets budget hint survives serialization across the MCP boundary.

        The failure this whole guarantee exists for happened at the
        tool-result boundary, not inside the service, so a service-level
        assertion never reaches it. Runs at the production budget on a
        vault whose fifty widest tags overrun it on width alone -- the
        count cap is satisfied and truncated nothing, as the exact
        fifty-of-fifty assertion below records, so only a byte-aware
        element can be firing.
        """
        from sage.models.schemas import Document
        from sage.services.retrieval import (
            DEFAULT_FACET_VALUE_LIMIT,
            DEFAULT_MCP_INLINE_BUDGET_BYTES,
        )

        services, _ = single_vault
        gs = services.graph_store
        now = datetime.now(timezone.utc)

        # Derived from the budget rather than fixed against it: a literal
        # width keeps its distance from this guard only until the budget
        # moves, and then narrows toward it without anything going red.
        tag_width = 2 * DEFAULT_MCP_INLINE_BUDGET_BYTES // DEFAULT_FACET_VALUE_LIMIT
        assert DEFAULT_FACET_VALUE_LIMIT * tag_width > DEFAULT_MCP_INLINE_BUDGET_BYTES

        for i in range(DEFAULT_FACET_VALUE_LIMIT):
            doc_id = _id(f"overwide_{i:04d}")
            await gs.insert_document(
                Document(
                    id=doc_id,
                    title=f"Over-wide-tag doc {i:04d}",
                    source_type=SourceType.MARKDOWN,
                    source_path=f"test/overwide_{i:04d}.md",
                    lifecycle_status="active",
                    source_content_hash=_sha(doc_id),
                    adapter_version="0.1.0",
                    created_by="testuser",
                    created_at=now,
                    last_modified_by="testuser",
                    updated_at=now,
                    projected_at=now,
                    pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
                    doc_type="note",
                    tags=[f"tag-{i:04d}-".ljust(tag_width, "x")],
                )
            )

        result = _parse(await search(vault_id="test_vault", mode="catalog", target="facets"))

        tags_row = next(r for r in result["results"] if r["field"] == "tags")
        assert len(tags_row["values"]) == DEFAULT_FACET_VALUE_LIMIT
        assert tags_row["total_distinct"] == DEFAULT_FACET_VALUE_LIMIT

        hints = result.get("hints")
        assert hints is not None, "facets hints dict not surfaced through MCP wrapper"
        assert hints.get("reason") == "facets_response_exceeds_inline_budget"
        assert hints.get("budget_bytes") == DEFAULT_MCP_INLINE_BUDGET_BYTES
        recommended = hints.get("recommended_facet_value_limit")
        assert isinstance(recommended, int)
        assert 1 < recommended < DEFAULT_FACET_VALUE_LIMIT
        assert "recommended_limit" not in hints

    async def test_facets_recommended_re_call_fits_the_delivered_ceiling(self, single_vault):
        """The re-call the hint names fits, measured on the delivered bytes.

        This is the assertion the whole guarantee is about, and the only
        one taken at the boundary the original failure crossed. Every
        other test in this change measures the service's own view of the
        payload; the runtime encodes the dict the tool returns a second
        time, with indentation, and a recommendation fitted to the
        service's view with no margin has nothing to absorb the
        difference.

        Anti-coincidental-pass: the first call is asserted over the
        ceiling in the same delivered units, so the pair is a real
        crossing rather than a payload that was never over budget.
        Measuring the tool's return value instead of the transport's
        text passes against a service that measures the wrong encoding
        -- which is the defect this exists to catch -- so the bytes are
        read off the TextContent the runtime produced.

        The fixture is high-cardinality with short values, not the wide
        tags the sibling tests use, and that choice is what makes the
        test discriminate. Indentation costs bytes per element rather
        than per byte of content, so on tags a couple of thousand
        characters wide the two encodings differ by about 1% and a
        compact measurement still lands under; on a vocabulary of
        forty-character tags they differ by around 17%, and a compact
        search recommends a cap whose delivered payload overruns by
        kilobytes. Verified by mutation: this test goes green against
        the compact encoding on the wide-tag shape and red on this one.

        The vocabulary is sized from the budget -- one value per tag
        width -- so the crossing stays a crossing when the budget is
        recalibrated. Fixed at a literal, it would stop overrunning the
        budget entirely the first time the budget rose past it, and the
        mutation property above would go with it.
        """
        from mcp.types import TextContent

        from sage.services.retrieval import DEFAULT_MCP_INLINE_BUDGET_BYTES

        tag_width = 40
        vocabulary = DEFAULT_MCP_INLINE_BUDGET_BYTES // tag_width
        services, _ = single_vault
        await self._seed_short_tags(services, vocabulary, tags_per_doc=10, tag_width=tag_width)

        async def delivered(**kwargs) -> tuple[int, dict]:
            out = await _mcp.mcp.call_tool(
                "search",
                {
                    "vault_id": "test_vault",
                    "mode": "catalog",
                    "target": "facets",
                    "facet_value_limit": vocabulary,
                    **kwargs,
                },
            )
            text = next(c.text for c in out if isinstance(c, TextContent))
            return len(text.encode("utf-8")), json.loads(text)

        first_bytes, first = await delivered()
        assert first_bytes > DEFAULT_MCP_INLINE_BUDGET_BYTES
        recommended = first["hints"]["recommended_facet_value_limit"]

        second_bytes, second = await delivered(facet_value_limit=recommended)
        assert second_bytes <= DEFAULT_MCP_INLINE_BUDGET_BYTES, (
            f"the re-call the hint named delivered {second_bytes} bytes against a "
            f"{DEFAULT_MCP_INLINE_BUDGET_BYTES}-byte ceiling"
        )
        assert "hints" not in second or "recommended_facet_value_limit" not in second["hints"]
        tags_row = next(r for r in second["results"] if r["field"] == "tags")
        assert len(tags_row["values"]) == recommended

    async def _seed_short_tags(
        self, services, vocabulary: int, *, tags_per_doc: int, tag_width: int
    ):
        """A tag vocabulary of ``vocabulary`` distinct short values.

        Spread across documents rather than one per document, so a
        high-cardinality vocabulary costs a tenth of the inserts. Counts
        are uniform, so the top-of-ordering prefix is purely value-ASC.
        """
        gs = services.graph_store
        now = datetime.now(timezone.utc)
        for doc_index in range(vocabulary // tags_per_doc):
            doc_id = _id(f"shorttag_{doc_index:04d}")
            base = doc_index * tags_per_doc
            await gs.insert_document(
                Document(
                    id=doc_id,
                    title=f"Short-tag doc {doc_index:04d}",
                    source_type=SourceType.MARKDOWN,
                    source_path=f"test/shorttag_{doc_index:04d}.md",
                    lifecycle_status="active",
                    source_content_hash=_sha(doc_id),
                    adapter_version="0.1.0",
                    created_by="testuser",
                    created_at=now,
                    last_modified_by="testuser",
                    updated_at=now,
                    projected_at=now,
                    pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
                    doc_type="note",
                    tags=[
                        f"tag-{base + j:04d}-".ljust(tag_width, "x") for j in range(tags_per_doc)
                    ],
                )
            )
