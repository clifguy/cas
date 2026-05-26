"""Tests for the repair-source-content-hash one-shot script.

Exercises the service-level entry point ``repair_with_services`` against
a real graph store fixture. The script normalizes
``source_content_hash`` values that were persisted in bare 64-char hex
form (pre part 3/3) back to the ``Sha256Str`` contract
(``sha256:`` + 64 hex).

Defensive contract: dry-run is the default and writes nothing; values
that are neither canonical nor bare-hex are reported but not
heuristically rewritten; clean records are not touched.
"""

from datetime import datetime, timezone

from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import Document
from scripts.repair_source_content_hash import repair_with_services

_BARE_HEX = "4f45d79f2d4041bf4c0bcbeec8245e93b5b0f152dbafff58153f14c38b82d5aa"
_CANONICAL = f"sha256:{_BARE_HEX}"
_OTHER_BARE_HEX = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def _make_doc(doc_id: str, source_content_hash: str) -> Document:
    # model_construct is required: this helper deliberately seeds storage with
    # bare-hex source_content_hash values that the Sha256Str validator would
    # reject if construction ran through it.
    now = datetime.now(timezone.utc)
    return Document.model_construct(
        id=doc_id,
        title=f"Doc {doc_id}",
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{doc_id}.md",
        source_content_hash=source_content_hash,
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
        document_date=None,
    )


async def _seed_mixed_hashes(graph_store):
    """Four docs: canonical, bare-hex, another bare-hex, malformed."""
    await graph_store.insert_document(_make_doc("clean", _CANONICAL))
    await graph_store.insert_document(_make_doc("bare", _BARE_HEX))
    await graph_store.insert_document(_make_doc("bare_other", _OTHER_BARE_HEX))
    await graph_store.insert_document(_make_doc("garbage", "not-a-hash"))


async def test_repair_dry_run_reports_targets_without_writing(graph_store):
    """Dry-run plans rewrites for bare-hex records; the DB is unchanged."""
    await _seed_mixed_hashes(graph_store)

    result = await repair_with_services(graph=graph_store, execute=False)

    targets = {(t.doc_id, t.old_value, t.new_value) for t in result.targets}
    assert ("bare", _BARE_HEX, _CANONICAL) in targets
    assert ("bare_other", _OTHER_BARE_HEX, f"sha256:{_OTHER_BARE_HEX}") in targets
    assert not any(t.doc_id == "clean" for t in result.targets)
    assert not any(t.doc_id == "garbage" for t in result.targets)
    assert result.rewrites_applied == 0

    bare_after = await graph_store.get_document("bare")
    assert bare_after.source_content_hash == _BARE_HEX


async def test_repair_execute_rewrites_bare_hex_records(graph_store):
    """With execute=True the bare-hex records are normalized to the canonical shape."""
    await _seed_mixed_hashes(graph_store)

    result = await repair_with_services(graph=graph_store, execute=True)

    assert result.rewrites_applied == 2

    bare_after = await graph_store.get_document("bare")
    assert bare_after.source_content_hash == _CANONICAL

    bare_other_after = await graph_store.get_document("bare_other")
    assert bare_other_after.source_content_hash == f"sha256:{_OTHER_BARE_HEX}"

    clean_after = await graph_store.get_document("clean")
    assert clean_after.source_content_hash == _CANONICAL

    garbage_after = await graph_store.get_document("garbage")
    assert garbage_after.source_content_hash == "not-a-hash"


async def test_repair_skips_non_bare_hex_without_raising(graph_store):
    """Values that are neither canonical nor bare-hex are reported, not rewritten."""
    await graph_store.insert_document(_make_doc("garbage", "not-a-hash"))

    result = await repair_with_services(graph=graph_store, execute=True)

    assert result.rewrites_applied == 0
    assert any(s.doc_id == "garbage" for s in result.skipped)

    after = await graph_store.get_document("garbage")
    assert after.source_content_hash == "not-a-hash"


async def test_repair_is_idempotent(graph_store):
    """A second --execute run after the first finds no targets."""
    await _seed_mixed_hashes(graph_store)
    first = await repair_with_services(graph=graph_store, execute=True)
    assert first.rewrites_applied == 2

    second = await repair_with_services(graph=graph_store, execute=True)
    assert second.rewrites_applied == 0
    assert not second.targets
