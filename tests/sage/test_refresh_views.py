"""Refresh views tests: BH-043 through BH-048.

Tests for refresh_views symlink-based browsable folder views.
"""

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import Document
from sage.services.utilities import UtilitiesService
from sage.storage.graph_store import GraphStore

_DOC_ID_RE = re.compile(r"^[0-9a-f]{8}_[a-z0-9_]+$")


def _id(name: str) -> str:
    """Translate a short test name to a shape-conformant document ID.

    The ID validator in sage/models/schemas.py requires the pattern
    ^[0-9a-f]{8}_[a-z0-9_]+$. Test fixtures use short readable names
    like "doc_a"; this helper wraps them so the values still construct
    valid Document instances. Idempotent: an already-canonical id
    passes through unchanged so wrapping is safe to apply at every
    call site.
    """
    if _DOC_ID_RE.fullmatch(name):
        return name
    return f"{hashlib.sha256(name.encode()).hexdigest()[:8]}_{name}"


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_test_document(
    graph_store: GraphStore,
    doc_id: str,
    title: str,
    source_path: str,
    lifecycle_status: str = "active",
    doc_type: str | None = None,
    pipeline_status: PipelineStatus = PipelineStatus.ABSTRACTION_COMPLETE,
) -> Document:
    """Insert a document directly into the graph store for testing."""
    now = datetime.now(timezone.utc)
    doc = Document(
        id=doc_id,
        title=title,
        source_type=SourceType.MARKDOWN,
        source_path=source_path,
        lifecycle_status=lifecycle_status,
        doc_type=doc_type,
        source_content_hash=_sha(doc_id),
        adapter_version="1.0.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        indexed_at=now if pipeline_status != PipelineStatus.FAILED else None,
        pipeline_status=pipeline_status,
        pipeline_error="test failure" if pipeline_status == PipelineStatus.FAILED else None,
    )
    await graph_store.insert_document(doc)
    return doc


def _create_source_file(storage_root: Path, source_path: str) -> Path:
    """Create a dummy source file at the given path."""
    full_path = storage_root / source_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(f"# {source_path}\n\nContent for {source_path}.")
    return full_path


@pytest.fixture
def utilities_service(graph_store, stub_content_store, stub_embedding_provider, minimal_config):
    return UtilitiesService(
        graph_store=graph_store,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        config=minimal_config,
    )


# ---------------------------------------------------------------------------
# BH-043: refresh_views generates by_doc_type and by_lifecycle directories
# ---------------------------------------------------------------------------


async def test_bh043_both_view_dimensions_generated(
    graph_store,
    utilities_service,
    minimal_config,
    tmp_vault_dir,
):
    """Both by_doc_type/ and by_lifecycle/ views are always generated."""
    storage_root = Path(minimal_config.vault.storage_root)

    # Create source files and documents
    _create_source_file(storage_root, "patents/doc_a.md")
    _create_source_file(storage_root, "patents/doc_b.md")
    _create_source_file(storage_root, "glossaries/doc_c.md")

    await _insert_test_document(
        graph_store,
        _id("doc_a"),
        "Doc A",
        "patents/doc_a.md",
        lifecycle_status="active",
        doc_type="patent",
    )
    await _insert_test_document(
        graph_store,
        _id("doc_b"),
        "Doc B",
        "patents/doc_b.md",
        lifecycle_status="archived",
        doc_type="patent",
    )
    await _insert_test_document(
        graph_store,
        _id("doc_c"),
        "Doc C",
        "glossaries/doc_c.md",
        lifecycle_status="active",
        doc_type="glossary",
    )

    result = await utilities_service.refresh_views()

    assert result.vault_id == "test_vault"
    # by_doc_type: patent, glossary = 2
    # by_lifecycle: active, archived = 2
    assert result.views_generated >= 3

    views_root = storage_root / "views"

    # by_doc_type checks
    patent_dir = views_root / "by_doc_type" / "patent"
    assert patent_dir.exists()
    patent_links = list(patent_dir.iterdir())
    assert len(patent_links) == 2

    glossary_dir = views_root / "by_doc_type" / "glossary"
    assert glossary_dir.exists()
    glossary_links = list(glossary_dir.iterdir())
    assert len(glossary_links) == 1

    # by_lifecycle checks
    active_dir = views_root / "by_lifecycle" / "active"
    assert active_dir.exists()
    active_links = list(active_dir.iterdir())
    assert len(active_links) == 2  # doc_a and doc_c

    archived_dir = views_root / "by_lifecycle" / "archived"
    assert archived_dir.exists()
    archived_links = list(archived_dir.iterdir())
    assert len(archived_links) == 1  # doc_b


# ---------------------------------------------------------------------------
# BH-044: Symlinks point to original source files via relative paths
# ---------------------------------------------------------------------------


async def test_bh044_symlinks_point_to_source_files(
    graph_store,
    utilities_service,
    minimal_config,
):
    """Symlinks target original source files via relative paths."""
    storage_root = Path(minimal_config.vault.storage_root)

    source_file = _create_source_file(storage_root, "patents/claim_set.md")
    await _insert_test_document(
        graph_store,
        _id("doc_cs"),
        "Claim Set",
        "patents/claim_set.md",
        doc_type="patent",
    )

    await utilities_service.refresh_views()

    patent_dir = storage_root / "views" / "by_doc_type" / "patent"
    links = list(patent_dir.iterdir())
    assert len(links) == 1

    link = links[0]
    assert link.name == "claim_set.md"
    assert link.is_symlink()

    # Symlink target is relative (BH-044)
    raw_target = os.readlink(str(link))
    assert not os.path.isabs(raw_target), f"Expected relative symlink, got: {raw_target}"

    # Symlink resolves to the actual source file
    assert link.resolve() == source_file.resolve()


# ---------------------------------------------------------------------------
# BH-045: refresh_views performs full regeneration
# ---------------------------------------------------------------------------


async def test_bh045_full_regeneration(
    graph_store,
    utilities_service,
    minimal_config,
):
    """Full regeneration: stale symlinks removed after lifecycle change."""
    storage_root = Path(minimal_config.vault.storage_root)
    _create_source_file(storage_root, "test/doc_a.md")

    await _insert_test_document(
        graph_store,
        _id("doc_a"),
        "Doc A",
        "test/doc_a.md",
        lifecycle_status="active",
        doc_type="note",
    )

    # First refresh: doc_a is active
    await utilities_service.refresh_views()
    active_dir = storage_root / "views" / "by_lifecycle" / "active"
    assert active_dir.exists()
    assert len(list(active_dir.iterdir())) == 1

    # Simulate lifecycle change by updating the document directly
    await graph_store.update_document(_id("doc_a"), {"lifecycle_status": "archived"})

    # Second refresh: doc_a is now archived
    await utilities_service.refresh_views()

    # active/ should not exist (no documents in active state)
    active_dir = storage_root / "views" / "by_lifecycle" / "active"
    assert not active_dir.exists()

    # archived/ should contain doc_a
    archived_dir = storage_root / "views" / "by_lifecycle" / "archived"
    assert archived_dir.exists()
    assert len(list(archived_dir.iterdir())) == 1


# ---------------------------------------------------------------------------
# BH-046: Failed-pipeline documents appear in views
# ---------------------------------------------------------------------------


async def test_bh046_failed_pipeline_documents_in_views(
    graph_store,
    utilities_service,
    minimal_config,
):
    """Failed-pipeline documents appear in both view dimensions."""
    storage_root = Path(minimal_config.vault.storage_root)
    _create_source_file(storage_root, "test/failed.md")

    await _insert_test_document(
        graph_store,
        _id("doc_fail"),
        "Failed Doc",
        "test/failed.md",
        lifecycle_status="active",
        doc_type="patent",
        pipeline_status=PipelineStatus.FAILED,
    )

    await utilities_service.refresh_views()

    # Document appears in both views despite failed pipeline
    active_dir = storage_root / "views" / "by_lifecycle" / "active"
    assert active_dir.exists()
    assert len(list(active_dir.iterdir())) == 1

    patent_dir = storage_root / "views" / "by_doc_type" / "patent"
    assert patent_dir.exists()
    assert len(list(patent_dir.iterdir())) == 1


# ---------------------------------------------------------------------------
# BH-047: Empty doc_type or lifecycle status produces no directory
# ---------------------------------------------------------------------------


async def test_bh047_empty_categories_no_directory(
    graph_store,
    utilities_service,
    minimal_config,
):
    """Only non-empty categories produce directories; views_generated counts them."""
    storage_root = Path(minimal_config.vault.storage_root)
    _create_source_file(storage_root, "test/only.md")

    await _insert_test_document(
        graph_store,
        _id("doc_only"),
        "Only Doc",
        "test/only.md",
        lifecycle_status="active",
        doc_type="patent",
    )

    result = await utilities_service.refresh_views()

    # Exactly 2 directories: by_doc_type/patent and by_lifecycle/active
    assert result.views_generated == 2

    views_root = storage_root / "views"
    doc_type_dirs = list((views_root / "by_doc_type").iterdir())
    lifecycle_dirs = list((views_root / "by_lifecycle").iterdir())
    assert len(doc_type_dirs) == 1
    assert doc_type_dirs[0].name == "patent"
    assert len(lifecycle_dirs) == 1
    assert lifecycle_dirs[0].name == "active"


# ---------------------------------------------------------------------------
# BH-048: Documents with null doc_type excluded from by_doc_type view
# ---------------------------------------------------------------------------


async def test_bh048_null_doc_type_excluded(
    graph_store,
    utilities_service,
    minimal_config,
):
    """Documents with null doc_type excluded from by_doc_type/, present in by_lifecycle/."""
    storage_root = Path(minimal_config.vault.storage_root)
    _create_source_file(storage_root, "test/untyped.md")

    await _insert_test_document(
        graph_store,
        _id("doc_untyped"),
        "Untyped Doc",
        "test/untyped.md",
        lifecycle_status="active",
        doc_type=None,
    )

    await utilities_service.refresh_views()

    views_root = storage_root / "views"

    # by_doc_type/ should have no subdirectories
    by_doc_type = views_root / "by_doc_type"
    if by_doc_type.exists():
        assert len(list(by_doc_type.iterdir())) == 0

    # by_lifecycle/active/ should contain the document
    active_dir = views_root / "by_lifecycle" / "active"
    assert active_dir.exists()
    assert len(list(active_dir.iterdir())) == 1


# ---------------------------------------------------------------------------
# Edge case: empty vault
# ---------------------------------------------------------------------------


async def test_empty_vault_produces_no_views(
    utilities_service,
    minimal_config,
):
    """Empty vault produces views_generated: 0 and no directories."""
    storage_root = Path(minimal_config.vault.storage_root)

    result = await utilities_service.refresh_views()

    assert result.views_generated == 0
    views_root = storage_root / "views"
    # views/ directory might not even be created, or is empty
    if views_root.exists():
        by_doc_type = views_root / "by_doc_type"
        by_lifecycle = views_root / "by_lifecycle"
        if by_doc_type.exists():
            assert len(list(by_doc_type.iterdir())) == 0
        if by_lifecycle.exists():
            assert len(list(by_lifecycle.iterdir())) == 0


# ---------------------------------------------------------------------------
# Edge case: filename collision
# ---------------------------------------------------------------------------


async def test_filename_collision_handled(
    graph_store,
    utilities_service,
    minimal_config,
):
    """Two documents with the same filename get distinct symlink names."""
    storage_root = Path(minimal_config.vault.storage_root)
    _create_source_file(storage_root, "v1/spec.md")
    _create_source_file(storage_root, "v2/spec.md")

    await _insert_test_document(
        graph_store,
        _id("doc_v1"),
        "Spec V1",
        "v1/spec.md",
        lifecycle_status="active",
        doc_type="patent",
    )
    await _insert_test_document(
        graph_store,
        _id("doc_v2"),
        "Spec V2",
        "v2/spec.md",
        lifecycle_status="active",
        doc_type="patent",
    )

    await utilities_service.refresh_views()

    patent_dir = storage_root / "views" / "by_doc_type" / "patent"
    links = sorted(patent_dir.iterdir())
    assert len(links) == 2

    # Both should be valid symlinks
    for link in links:
        assert link.is_symlink()
        assert link.resolve().exists()

    # Names should be distinct
    names = {link.name for link in links}
    assert len(names) == 2
