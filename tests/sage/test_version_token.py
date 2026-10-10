"""The document version token behind CAS-ADR-038 Primitives B and C.

``update_metadata``'s ``expected_version`` and ``ingest_document``'s
``expected_head_version`` compare against ``version_token``, which only a
caller-meaningful write advances: a metadata patch, a lifecycle transition, or
a replacement of the document's content. Pipeline work -- status stamps,
abstract writes, re-projection -- leaves it alone, so a token read while a
document is still being indexed or abstracted stays valid.

The earlier token, the document's ``updated_at``, is still accepted with its
earlier comparison and is deprecated: a call that uses it is warned.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from sage import mcp_server as _mcp
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.config import VaultConfig
from sage.mcp_server import get_document, ingest_document, update_lifecycles
from sage.mcp_server import update_metadata as _update_metadata_bulk
from sage.models.enums import PipelineStatus
from tests.helpers.pipeline_wait import await_tool_idle
from tests.sage.conftest import initialize_services_for_test

VAULT = "test_vault"


@pytest.fixture
async def vault_services(minimal_vault_config_dict, tmp_vault_dir):
    """Services registered in the MCP vault registry, with a writable source dir."""
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    async with initialize_services_for_test(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        _mcp._vaults[VAULT] = services
        test_dir = tmp_vault_dir / "sources" / "test"
        test_dir.mkdir(parents=True, exist_ok=True)
        try:
            yield services, test_dir
        finally:
            _mcp._vaults.pop(VAULT, None)


def _parse(result):
    return result if isinstance(result, dict) else json.loads(result)


async def _read(doc_id: str) -> dict:
    return _parse(await get_document(VAULT, doc_id))


async def _settled(services, doc_id: str) -> dict:
    """The document once its background pipeline has settled and released its claim.

    The tests below drive pipeline writes themselves, so they start from a
    quiet document; the background stages would otherwise interleave with
    the writes under test.
    """

    async def fetch():
        return await _read(doc_id)

    return await await_tool_idle(
        fetch, doc_id, service=services.ingestion_service, attempts=150, delay=0.02
    )


async def _seed(services, test_dir, name: str) -> dict:
    (test_dir / f"{name}.md").write_text(f"# {name}\n\nContent for {name}.")
    result = _parse(await ingest_document(VAULT, f"test/{name}.md", "markdown"))
    assert "error" not in result, result
    return await _settled(services, result["id"])


async def _patch(doc_id: str, **fields) -> dict:
    """One update_metadata item; returns that item's result."""
    result = _parse(
        await _update_metadata_bulk(vault_id=VAULT, items=[{"document_id": doc_id, **fields}])
    )
    return result["results"][0]


def _deprecation_warnings(warnings) -> list[str]:
    return [w for w in (warnings or []) if w.startswith("Deprecated:")]


# ---------------------------------------------------------------------------
# The token's shape
# ---------------------------------------------------------------------------


async def test_a_new_document_starts_at_version_one(vault_services):
    services, test_dir = vault_services
    doc = await _seed(services, test_dir, "fresh")
    assert doc["version_token"] == "1"


# ---------------------------------------------------------------------------
# Pipeline writes leave it alone
# ---------------------------------------------------------------------------


async def _stamp(services, doc_id):
    await services.ingestion_service._stamp_pipeline_status(
        doc_id, PipelineStatus.INDEXING_COMPLETE
    )


async def _stamp_if_non_terminal(services, doc_id):
    await services.ingestion_service._stamp_pipeline_status(
        doc_id, PipelineStatus.ABSTRACTION_IN_PROGRESS
    )
    await services.ingestion_service._stamp_if_non_terminal(
        doc_id, PipelineStatus.ABSTRACTION_INTERRUPTED
    )


async def _abstract_write(services, doc_id):
    await services.ingestion_service.update_semantic_abstract(doc_id, "A replacement abstract.")


async def _reproject(services, doc_id):
    await services.ingestion_service._reproject_from_source(doc_id)


async def _recompute(services, doc_id):
    await services.ingestion_service.recompute_pipeline(doc_id)
    await _settled(services, doc_id)


@pytest.mark.parametrize(
    "write",
    [_stamp, _stamp_if_non_terminal, _abstract_write, _reproject, _recompute],
    ids=["status-stamp", "stamp-if-non-terminal", "abstract-write", "reproject", "recompute"],
)
async def test_a_pipeline_write_leaves_the_token_unchanged(vault_services, write):
    """Each pipeline writer advances ``updated_at`` and not ``version_token``.

    Anti-coincidental-pass: ``updated_at`` must move, so the write is known to
    have landed; a writer that silently did nothing would pass the token
    assertion while proving nothing.
    """
    services, test_dir = vault_services
    before = await _seed(services, test_dir, "piped")

    await write(services, before["id"])

    after = await _read(before["id"])
    assert after["updated_at"] != before["updated_at"], "the pipeline write must have landed"
    assert after["version_token"] == before["version_token"]


# ---------------------------------------------------------------------------
# A token read mid-pipeline stays valid
# ---------------------------------------------------------------------------


async def test_a_patch_succeeds_with_a_token_read_before_a_pipeline_write(vault_services):
    services, test_dir = vault_services
    doc = await _seed(services, test_dir, "patched")
    token = doc["version_token"]

    await _stamp(services, doc["id"])

    result = await _patch(doc["id"], title="Renamed", expected_version=token)
    assert result["status"] == "success", result
    assert result["document"]["title"] == "Renamed"
    assert not _deprecation_warnings(result.get("warnings"))


async def test_a_supersede_succeeds_with_a_token_read_before_a_pipeline_write(vault_services):
    services, test_dir = vault_services
    head = await _seed(services, test_dir, "head")
    token = head["version_token"]

    await _stamp(services, head["id"])

    (test_dir / "head_v2.md").write_text("# head\n\nRevised content.")
    result = _parse(
        await ingest_document(
            VAULT,
            "test/head_v2.md",
            "markdown",
            predecessor_id=head["id"],
            expected_head_version=token,
        )
    )
    assert "error" not in result, result
    assert not _deprecation_warnings(result.get("warnings"))
    assert (await _read(head["id"]))["lifecycle_status"] == "archived"


# ---------------------------------------------------------------------------
# A real concurrent write is still refused
# ---------------------------------------------------------------------------


async def test_a_concurrent_patch_is_refused_with_the_winners_token(vault_services):
    services, test_dir = vault_services
    doc = await _seed(services, test_dir, "raced")
    token = doc["version_token"]

    winner = await _patch(doc["id"], title="First", expected_version=token)
    assert winner["status"] == "success", winner

    loser = await _patch(doc["id"], title="Second", expected_version=token)
    assert loser["status"] == "error"
    assert loser["error"]["error"] == "stale_read"
    assert loser["error"]["detail"]["expected_version"] == token
    assert loser["error"]["detail"]["current_version"] == winner["document"]["version_token"]


async def test_a_supersede_after_a_concurrent_patch_is_refused(vault_services):
    services, test_dir = vault_services
    head = await _seed(services, test_dir, "contended")
    token = head["version_token"]

    patched = await _patch(head["id"], title="Retitled")
    assert patched["status"] == "success", patched

    (test_dir / "contended_v2.md").write_text("# contended\n\nRevised content.")
    result = _parse(
        await ingest_document(
            VAULT,
            "test/contended_v2.md",
            "markdown",
            predecessor_id=head["id"],
            expected_head_version=token,
        )
    )
    assert result["error"] == "stale_chain_head", result
    assert result["detail"]["current_head_version"] == patched["document"]["version_token"]
    assert (await _read(head["id"]))["lifecycle_status"] == "active"


# ---------------------------------------------------------------------------
# Caller-meaningful writes advance it by exactly one
# ---------------------------------------------------------------------------


async def test_a_metadata_patch_advances_the_token_by_one(vault_services):
    services, test_dir = vault_services
    doc = await _seed(services, test_dir, "bumped")
    result = await _patch(doc["id"], title="Bumped")
    assert int(result["document"]["version_token"]) == int(doc["version_token"]) + 1


async def test_a_lifecycle_transition_advances_the_token_by_one(vault_services):
    services, test_dir = vault_services
    doc = await _seed(services, test_dir, "completed")
    result = _parse(
        await update_lifecycles(VAULT, items=[{"document_id": doc["id"], "action": "complete"}])
    )
    assert result["results"][0]["status"] == "success", result
    after = await _read(doc["id"])
    assert after["lifecycle_status"] == "completed"
    assert int(after["version_token"]) == int(doc["version_token"]) + 1


async def test_a_supersede_advances_the_predecessor_token_by_one(vault_services):
    services, test_dir = vault_services
    head = await _seed(services, test_dir, "retired")
    (test_dir / "retired_v2.md").write_text("# retired\n\nRevised content.")
    result = _parse(
        await ingest_document(VAULT, "test/retired_v2.md", "markdown", predecessor_id=head["id"])
    )
    assert "error" not in result, result
    after = await _read(head["id"])
    assert int(after["version_token"]) == int(head["version_token"]) + 1


async def test_a_force_reingest_advances_the_token_by_one(vault_services):
    services, test_dir = vault_services
    doc = await _seed(services, test_dir, "forced")
    result = _parse(await ingest_document(VAULT, "test/forced.md", "markdown", force=True))
    assert "error" not in result, result
    assert result["id"] == doc["id"]
    after = await _settled(services, doc["id"])
    assert int(after["version_token"]) == int(doc["version_token"]) + 1


async def test_concurrent_store_writes_each_advance_the_token(vault_services):
    """Two writes that race past the service lock both count.

    The increment happens in the UPDATE itself, so it holds across processes
    that share no lock. A read-modify-write in Python would lose one of them.
    """
    services, test_dir = vault_services
    doc = await _seed(services, test_dir, "atomic")
    store = services.ingestion_service._store

    await asyncio.gather(
        store.update_document(doc["id"], {"title": "One"}),
        store.update_document(doc["id"], {"version_label": "two"}),
    )

    after = await _read(doc["id"])
    assert int(after["version_token"]) == int(doc["version_token"]) + 2


# ---------------------------------------------------------------------------
# The deprecated updated_at form
# ---------------------------------------------------------------------------


async def test_an_updated_at_token_still_works_and_is_warned(vault_services):
    services, test_dir = vault_services
    doc = await _seed(services, test_dir, "legacy")

    result = await _patch(doc["id"], title="Legacy", expected_version=doc["updated_at"])
    assert result["status"] == "success", result
    warnings = _deprecation_warnings(result.get("warnings"))
    assert len(warnings) == 1
    assert "expected_version" in warnings[0]
    assert "version_token" in warnings[0]


async def test_a_stale_updated_at_token_is_refused_in_its_own_form(vault_services):
    """The deprecated form keeps its old comparison, pipeline writes included."""
    services, test_dir = vault_services
    doc = await _seed(services, test_dir, "legacy_stale")

    await _stamp(services, doc["id"])
    current = await _read(doc["id"])

    result = await _patch(doc["id"], title="Late", expected_version=doc["updated_at"])
    assert result["status"] == "error"
    assert result["error"]["error"] == "stale_read"
    assert result["error"]["detail"]["current_version"] == current["updated_at"]


async def test_an_updated_at_head_token_still_supersedes_and_is_warned(vault_services):
    services, test_dir = vault_services
    head = await _seed(services, test_dir, "legacy_head")
    (test_dir / "legacy_head_v2.md").write_text("# legacy_head\n\nRevised content.")
    result = _parse(
        await ingest_document(
            VAULT,
            "test/legacy_head_v2.md",
            "markdown",
            predecessor_id=head["id"],
            expected_head_version=head["updated_at"],
        )
    )
    assert "error" not in result, result
    warnings = _deprecation_warnings(result.get("warnings"))
    assert len(warnings) == 1
    assert "expected_head_version" in warnings[0]


# ---------------------------------------------------------------------------
# A write made while the passages are being indexed reaches them
# ---------------------------------------------------------------------------


@pytest.fixture
def held_indexing(vault_services, monkeypatch):
    """Hold the indexing stage between its read of the document and its write.

    Indexing reads the document, embeds its passages, then writes them stamped
    with what it read. The embedder is the wait in between, so holding it opens
    the window a caller's write lands in when it follows the ingest at once.
    """
    services, _ = vault_services
    embedder = services.ingestion_service._embedding
    original = embedder.embed
    entered = asyncio.Event()
    release = asyncio.Event()

    async def held(texts):
        entered.set()
        await release.wait()
        return await original(texts)

    monkeypatch.setattr(embedder, "embed", held)
    return entered, release


async def test_a_patch_during_indexing_reaches_the_passages(vault_services, held_indexing):
    """The passages carry the patched doc_type and title, not the values indexing read.

    Anti-coincidental-pass: the patch is applied while indexing is held after
    its read, which the ``entered`` event proves, and the expected values differ
    from the ingested ones, so passages stamped from that read fail both checks.
    """
    from sage.services.passage_structure import indexed_structure

    services, test_dir = vault_services
    entered, release = held_indexing
    (test_dir / "midway.md").write_text("# midway\n\n## Part\n\nBody text.")
    ingested = _parse(await ingest_document(VAULT, "test/midway.md", "markdown"))
    assert "error" not in ingested, ingested
    await asyncio.wait_for(entered.wait(), timeout=5)

    patched = await _patch(
        ingested["id"],
        doc_type="memo",
        title="Renamed midway",
        expected_version=ingested["version_token"],
    )
    assert patched["status"] == "success", patched
    release.set()
    await _settled(services, ingested["id"])

    chunks = services.ingestion_service._content_store._store[ingested["id"]]
    assert chunks, "indexing must have written passages"
    assert {c.doc_type for c in chunks} == {"memo"}
    assert all(
        c.indexed_structure == indexed_structure(c.heading_path, "Renamed midway") for c in chunks
    )


async def test_a_supersede_during_indexing_reaches_the_predecessors_passages(
    vault_services, held_indexing
):
    services, test_dir = vault_services
    entered, release = held_indexing
    (test_dir / "early.md").write_text("# early\n\nFirst version.")
    head = _parse(await ingest_document(VAULT, "test/early.md", "markdown"))
    assert "error" not in head, head
    await asyncio.wait_for(entered.wait(), timeout=5)

    (test_dir / "early_v2.md").write_text("# early\n\nSecond version.")
    successor = _parse(
        await ingest_document(
            VAULT,
            "test/early_v2.md",
            "markdown",
            predecessor_id=head["id"],
            expected_head_version=head["version_token"],
        )
    )
    assert "error" not in successor, successor
    release.set()
    await _settled(services, head["id"])
    await _settled(services, successor["id"])

    chunks = services.ingestion_service._content_store._store[head["id"]]
    assert chunks, "indexing must have written passages"
    assert {c.lifecycle_status for c in chunks} == {"archived"}


async def test_an_explicit_supersede_advances_the_predecessor_token_by_one(vault_services):
    """The lifecycle supersede, which writes through the store's atomic supersede."""
    services, test_dir = vault_services
    old = await _seed(services, test_dir, "older")
    new = await _seed(services, test_dir, "newer")
    result = _parse(
        await update_lifecycles(
            VAULT,
            items=[{"document_id": old["id"], "action": "supersede", "successor_id": new["id"]}],
        )
    )
    assert result["results"][0]["status"] == "success", result
    after = await _read(old["id"])
    assert after["lifecycle_status"] == "archived"
    assert int(after["version_token"]) == int(old["version_token"]) + 1


async def test_the_store_refuses_to_set_the_token(vault_services):
    """The token only advances; a write naming it is refused before any SQL runs."""
    services, test_dir = vault_services
    doc = await _seed(services, test_dir, "unsettable")
    store = services.ingestion_service._store
    with pytest.raises(ValueError, match="version_token is advanced, never set"):
        await store.update_document(doc["id"], {"version_token": "9"})
    assert (await _read(doc["id"]))["version_token"] == doc["version_token"]
