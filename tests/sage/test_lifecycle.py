"""Lifecycle State Machine tests: BH-012 through BH-017.

Covers invalid transition error responses, domain-specific valid_actions,
pipeline warnings during transitions, and supersede with edge creation.
"""

import hashlib

import pytest
from datetime import datetime, timezone

from sage.api.errors import (
    DocumentNotFoundError,
    InvalidActionError,
    InvalidLifecycleTransitionError,
    MissingFieldError,
)
from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import Document, SetLifecycleRequest


def _id(name: str) -> str:
    """Translate a short test name to a shape-conformant document ID.

    The ID validator in sage/models/schemas.py requires the pattern
    ^[0-9a-f]{8}_[a-z0-9_]+$. Test fixtures use short readable names
    like "a1" or "doc_a"; this helper wraps them so the values still
    construct valid SetLifecycleRequest instances. Deterministic — the
    same name always yields the same id.
    """
    return f"{hashlib.sha256(name.encode()).hexdigest()[:8]}_{name}"


def _make_doc(
    doc_id: str,
    lifecycle_status: str = "active",
    pipeline_status: PipelineStatus = PipelineStatus.ABSTRACTION_COMPLETE,
) -> Document:
    """Helper to create a Document with sensible defaults."""
    now = datetime.now(timezone.utc)
    return Document(
        id=doc_id,
        title=f"Test {doc_id}",
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{doc_id}.md",
        source_content_hash=f"hash_{doc_id}",
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=pipeline_status,
    )


# ---------------------------------------------------------------------------
# BH-012: Invalid transition returns 409 with valid_actions
# ---------------------------------------------------------------------------

async def test_bh_012_invalid_transition_returns_409(graph_store, lifecycle_service):
    doc = _make_doc("doc_archived", lifecycle_status="archived")
    await graph_store.insert_document(doc)
    # Manually set to archived
    await graph_store.update_document("doc_archived", {"lifecycle_status": "archived"})

    with pytest.raises(InvalidLifecycleTransitionError) as exc_info:
        await lifecycle_service.set_lifecycle(
            "doc_archived",
            SetLifecycleRequest(action="complete"),
        )

    err = exc_info.value
    assert err.status_code == 409
    assert err.code == "invalid_lifecycle_transition"
    assert err.detail["current_state"] == "archived"
    assert err.detail["attempted_action"] == "complete"
    assert "reactivate" in err.detail["valid_actions"]


# ---------------------------------------------------------------------------
# BH-013: valid_actions reflects domain-specific transitions
# ---------------------------------------------------------------------------

async def test_bh_013_domain_specific_valid_actions(graph_store, extended_lifecycle_service):
    """Extended vault: a domain-defined 'file' action is valid from active."""
    doc = _make_doc("doc_active")
    await graph_store.insert_document(doc)

    # Try an action that is invalid from active
    with pytest.raises(InvalidLifecycleTransitionError) as exc_info:
        await extended_lifecycle_service.set_lifecycle(
            "doc_active",
            SetLifecycleRequest(action="reactivate"),
        )

    valid = exc_info.value.detail["valid_actions"]
    # Base actions
    assert "supersede" in valid
    assert "complete" in valid
    assert "archive" in valid
    # Domain-specific extension
    assert "file" in valid


# ---------------------------------------------------------------------------
# BH-014: Lifecycle transition allowed during pipeline, with warning
# ---------------------------------------------------------------------------

async def test_bh_014_transition_with_pipeline_warning(graph_store, lifecycle_service):
    doc = _make_doc("doc_indexing", pipeline_status=PipelineStatus.INDEXING_IN_PROGRESS)
    await graph_store.insert_document(doc)

    response = await lifecycle_service.set_lifecycle(
        "doc_indexing",
        SetLifecycleRequest(action="archive"),
    )

    assert response.document.lifecycle_status == "archived"
    # Pipeline status unchanged
    assert response.document.pipeline_status == PipelineStatus.INDEXING_IN_PROGRESS
    # Warnings present
    assert response.warnings is not None
    assert len(response.warnings) > 0
    assert "pipeline" in response.warnings[0].lower()


# ---------------------------------------------------------------------------
# BH-015: Lifecycle transition with no pipeline warning when terminal
# ---------------------------------------------------------------------------

async def test_bh_015_no_warning_when_pipeline_terminal(graph_store, lifecycle_service):
    doc = _make_doc("doc_complete_pipeline", pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE)
    await graph_store.insert_document(doc)

    response = await lifecycle_service.set_lifecycle(
        "doc_complete_pipeline",
        SetLifecycleRequest(action="archive"),
    )

    assert response.document.lifecycle_status == "archived"
    assert response.warnings is None or len(response.warnings) == 0


# ---------------------------------------------------------------------------
# BH-016: Supersede requires existing new_version_id
# ---------------------------------------------------------------------------

async def test_bh_016_supersede_requires_existing_version(graph_store, lifecycle_service):
    doc_old = _make_doc("doc_old")
    await graph_store.insert_document(doc_old)

    with pytest.raises(DocumentNotFoundError) as exc_info:
        await lifecycle_service.set_lifecycle(
            "doc_old",
            SetLifecycleRequest(action="supersede", new_version_id=_id("nonexistent_id")),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["document_id"] == _id("nonexistent_id")


async def test_bh_016_supersede_requires_new_version_id_field(graph_store, lifecycle_service):
    """Supersede without new_version_id raises 400."""
    doc = _make_doc("doc_no_version")
    await graph_store.insert_document(doc)

    with pytest.raises(MissingFieldError):
        await lifecycle_service.set_lifecycle(
            "doc_no_version",
            SetLifecycleRequest(action="supersede"),
        )


# ---------------------------------------------------------------------------
# BH-017: Supersede creates supersedes edge
# ---------------------------------------------------------------------------

async def test_bh_017_supersede_creates_edge(graph_store, lifecycle_service):
    doc_old = _make_doc(_id("doc_to_supersede"))
    doc_new = _make_doc(_id("doc_replacement"))
    await graph_store.insert_document(doc_old)
    await graph_store.insert_document(doc_new)

    response = await lifecycle_service.set_lifecycle(
        _id("doc_to_supersede"),
        SetLifecycleRequest(action="supersede", new_version_id=_id("doc_replacement")),
    )

    # doc_old transitions to archived
    assert response.document.lifecycle_status == "archived"

    # Supersedes edge exists: new -> old
    edges = await graph_store.get_edges_by_source(_id("doc_replacement"), "supersedes")
    assert len(edges) == 1
    assert edges[0].source_id == _id("doc_replacement")
    assert edges[0].target_id == _id("doc_to_supersede")
    assert edges[0].id  # auto-generated ID


# ---------------------------------------------------------------------------
# Additional: Unknown action returns 400 (not 409)
# ---------------------------------------------------------------------------

async def test_unknown_action_returns_400(graph_store, lifecycle_service):
    doc = _make_doc("doc_unknown_action")
    await graph_store.insert_document(doc)

    with pytest.raises(InvalidActionError) as exc_info:
        await lifecycle_service.set_lifecycle(
            "doc_unknown_action",
            SetLifecycleRequest(action="nonexistent_action"),
        )

    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# InvalidLifecycleTransitionError includes pipeline_status
# ---------------------------------------------------------------------------

async def test_invalid_transition_includes_pipeline_status(
    graph_store, lifecycle_service
):
    """InvalidLifecycleTransitionError detail includes pipeline_status."""
    doc = _make_doc(
        "doc_pipe_check",
        lifecycle_status="archived",
        pipeline_status=PipelineStatus.INDEXING_COMPLETE,
    )
    await graph_store.insert_document(doc)
    await graph_store.update_document(
        "doc_pipe_check", {"lifecycle_status": "archived"}
    )

    with pytest.raises(InvalidLifecycleTransitionError) as exc_info:
        await lifecycle_service.set_lifecycle(
            "doc_pipe_check",
            SetLifecycleRequest(action="complete"),
        )

    detail = exc_info.value.detail
    assert "pipeline_status" in detail
    assert detail["pipeline_status"] == "indexing_complete"
