"""A vault-source store refusal on a delivery read reaches the caller typed.

The delivery paths -- inline content, write-to-path, the download recipe, the
raw byte stream, and the download URL -- all read the retained source through
the vault-source store. When the store declines, the caller is owed the same
typed answer the repair and the retention already give, rather than a bare 500
against a spec that never mentions the case.

Each test drives the real ``DocumentStoreVaultSourceStore`` and the real
dispatch, faking only the Graph transport, so a refusal travels the production
path from the binding to the service boundary.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from sage.api.errors import (
    VaultSourceStoreRefusedError,
    VaultSourceStoreUnavailableError,
)
from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import Document, IngestRequest
from sage.services.documents import DocumentsService
from tests.helpers.store_refusal import STORE_BODY, refuse_after, store_refusal


def _sha256_of(content: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(content).hexdigest()


async def _delivered_doc(gs, fake, did: str = "deadbeef_dd") -> Document:
    """Record a document whose retained copy lives on the faked store."""
    sp = f"imports/{did}.md"
    body = f"{did} retained body".encode()
    fake.sources[sp] = body
    now = datetime.now(timezone.utc)
    doc = Document(
        id=did,
        title=f"Delivery test {did}",
        source_type=SourceType.MARKDOWN,
        source_path=sp,
        lifecycle_status="active",
        source_content_hash=_sha256_of(body),
        stored_content_hash=_sha256_of(body),
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
    )
    await gs.insert_document(doc)
    return doc


async def test_inline_content_read_refusal_is_typed(
    graph_store, minimal_config, refusing_source_store
):
    """A store that declines the bytes an inline read asked for reaches the
    caller as ``vault_source_store_refused``, naming the operation it declined.

    Anti-coincidental-pass: ``detail["operation"]`` is asserted to be the read
    rather than merely present, so a translation wrapped around the existence
    probe alone -- which would report the stat, or nothing -- fails here.
    """
    doc = await _delivered_doc(graph_store, refusing_source_store, "deadbeef_b1")
    refusing_source_store.refuse_read = store_refusal(403, retryable=False, operation="read source")
    documents = DocumentsService(graph_store, minimal_config)

    with pytest.raises(VaultSourceStoreRefusedError) as excinfo:
        await documents.get_document_with_content(doc.id, True, None)

    exc = excinfo.value
    assert exc.code == "vault_source_store_refused"
    assert exc.status_code == 502
    assert exc.detail["source_path"] == doc.source_path
    assert exc.detail["operation"] == "read source"
    assert exc.detail["store_status"] == 403


async def test_inline_content_existence_refusal_is_typed_not_a_missing_file(
    graph_store, minimal_config, refusing_source_store
):
    """A refusal on the existence probe is a fact about the store, not about
    the document.

    Anti-coincidental-pass: the binding reports a genuinely absent source by
    returning "not present", and only a store that declined on some *other*
    footing raises. Conflating the two would tell a caller their document is
    gone when the store merely declined to answer, so the existing
    ``content_file_missing`` 404 is asserted against explicitly -- a rival that
    swallowed every store difficulty into that code passes a test asserting
    only "some error was raised".
    """
    doc = await _delivered_doc(graph_store, refusing_source_store, "deadbeef_b2")
    refusing_source_store.refuse_stat = store_refusal(403, retryable=False, operation="stat source")
    documents = DocumentsService(graph_store, minimal_config)

    with pytest.raises(VaultSourceStoreRefusedError) as excinfo:
        await documents.get_document_with_content(doc.id, True, None)

    assert excinfo.value.code == "vault_source_store_refused"
    assert excinfo.value.code != "content_file_missing"
    assert excinfo.value.status_code == 502


async def test_write_to_path_stream_refusal_is_typed_and_leaves_no_partial_file(
    graph_store, minimal_config, refusing_source_store, tmp_path
):
    """A refusal while the retained source is streaming to the caller's path is
    typed, and the partial target the delivery had opened is removed.

    Anti-coincidental-pass: the translation has to sit *outside* the cleanup
    that unlinks a partial target, not in place of it. A rival that intercepts
    the refusal before the cleanup runs still produces the right error and
    leaves a zero-length file behind, which is what the ``exists`` assertion
    catches -- and that file would then trip the target-must-not-exist check on
    the caller's retry.
    """
    doc = await _delivered_doc(graph_store, refusing_source_store, "deadbeef_b3")
    target = tmp_path / "workspace" / "copy.md"
    target.parent.mkdir(parents=True)
    refusing_source_store.refuse_stream = store_refusal(
        429, retryable=True, operation="stream source"
    )
    documents = DocumentsService(graph_store, minimal_config)

    with pytest.raises(VaultSourceStoreUnavailableError) as excinfo:
        await documents.get_document_with_content(doc.id, False, str(target))

    assert excinfo.value.code == "vault_source_store_unavailable"
    assert excinfo.value.status_code == 503
    assert not target.exists(), "the partial target must not survive the refusal"


async def test_get_document_content_refusal_is_typed_before_the_stream_opens(
    graph_store, minimal_config, refusing_source_store
):
    """The raw byte channel resolves its store reads before it hands back a
    delivery, so a refusal on those reads is typed rather than surfacing as a
    truncated stream.

    Anti-coincidental-pass: the refusal is armed to fire on the *second* store
    read of the region -- the size -- rather than the existence probe that
    comes first. A wrap covering only the probe passes when the probe is the
    one refused, so refusing the probe proves nothing about the region's
    extent; refusing past it does. The size read is also what the response's
    Content-Length promises, so leaving it untyped is not cosmetic.
    """
    doc = await _delivered_doc(graph_store, refusing_source_store, "deadbeef_b4")
    refusing_source_store.refuse_stat = refuse_after(
        1, store_refusal(403, retryable=False, operation="stat source")
    )
    documents = DocumentsService(graph_store, minimal_config)

    with pytest.raises(VaultSourceStoreRefusedError) as excinfo:
        await documents.get_document_content(doc.id)

    assert excinfo.value.status_code == 502


async def test_get_download_url_refusal_is_typed_not_unavailable(
    graph_store, minimal_config, refusing_source_store
):
    """A refusal while minting a download URL is reported as the store
    declining, not as the binding being unable to answer.

    Anti-coincidental-pass: the refusal is armed on the URL mint itself, not on
    the existence probe that precedes it, so a wrap stopping after the probe
    fails here. Expressing that rival at all required the fake to gain a
    refusal it never carried -- on the real binding the mint is a store round
    trip that can be throttled like any other, and a fake that could only ever
    answer it left the narrow wrap and the wide one indistinguishable. The
    test also excludes ``download_url_unavailable`` (501), a different fact: a
    capability the binding lacks, not a store that declined.
    """
    doc = await _delivered_doc(graph_store, refusing_source_store, "deadbeef_b5")
    refusing_source_store.refuse_download_url = store_refusal(
        503, retryable=True, operation="mint download url"
    )
    documents = DocumentsService(graph_store, minimal_config)

    with pytest.raises(VaultSourceStoreUnavailableError) as excinfo:
        await documents.get_document_download_url(doc.id)

    assert excinfo.value.code == "vault_source_store_unavailable"
    assert excinfo.value.code != "download_url_unavailable"
    assert excinfo.value.status_code == 503


async def test_download_recipe_hash_refusal_is_typed(
    graph_store, minimal_config, refusing_source_store, monkeypatch
):
    """The recipe branch reads a digest and a size from the store before it
    promises them to the caller, and a refusal on that read is typed.

    Anti-coincidental-pass: this branch is reached only when the caller's own
    filesystem is out of the server's reach, and it is the one delivery site
    that hashes. A translation covering the ordinary delivery region alone
    passes every other test in this module and fails here. The second arm aims
    past the hash at the region's last store read, so a wrap that stopped after
    the hash -- the call this test would otherwise be built around -- fails on
    it; both the digest and the size are promises the caller verifies the
    fetched bytes against, so neither may reach it as an untyped error.
    """
    doc = await _delivered_doc(graph_store, refusing_source_store, "deadbeef_b6")
    monkeypatch.setattr("sage.mcp_init.caller_local_filesystem_reachable", lambda: False)
    refusing_source_store.refuse_hash = store_refusal(403, retryable=False, operation="hash source")
    documents = DocumentsService(graph_store, minimal_config)

    with pytest.raises(VaultSourceStoreRefusedError) as excinfo:
        await documents.get_document_with_content(doc.id, False, "/caller/local/copy.md")

    assert excinfo.value.detail["operation"] == "hash source"
    assert excinfo.value.status_code == 502

    # The size read follows the hash in the same region; refuse past the stat
    # the branch opens with, so this arm lands on it rather than on the probe.
    refusing_source_store.refuse_hash = None
    refusing_source_store.refuse_stat = refuse_after(
        1, store_refusal(503, retryable=True, operation="stat source")
    )
    with pytest.raises(VaultSourceStoreUnavailableError):
        await documents.get_document_with_content(doc.id, False, "/caller/local/copy2.md")


async def test_delivery_refusal_message_omits_the_store_body(
    graph_store, minimal_config, refusing_source_store
):
    """The store's own response body does not travel onto the error the caller
    receives, however useful it is in the log.

    Anti-coincidental-pass: the sentinel is asserted present on the binding's
    own exception first, so a test that passed because the refusal never
    carried a body -- and therefore proved nothing about what the translation
    drops -- fails on that assertion instead.
    """
    doc = await _delivered_doc(graph_store, refusing_source_store, "deadbeef_b7")
    refusal = store_refusal(507, retryable=False, operation="read source")
    assert STORE_BODY in str(refusal), "the binding must carry the body for the log"
    refusing_source_store.refuse_read = refusal
    documents = DocumentsService(graph_store, minimal_config)

    with pytest.raises(VaultSourceStoreRefusedError) as excinfo:
        await documents.get_document_with_content(doc.id, True, None)

    assert STORE_BODY not in excinfo.value.message
    assert STORE_BODY not in json.dumps(excinfo.value.detail)


@pytest.mark.parametrize(
    ("retryable", "expected"),
    [(True, VaultSourceStoreUnavailableError), (False, VaultSourceStoreRefusedError)],
)
async def test_delivery_transience_follows_the_binding_not_the_status(
    graph_store, minimal_config, refusing_source_store, retryable, expected
):
    """Whether a refusal is transient is the binding's finding, carried on the
    refusal. The delivery translation reports it rather than re-deriving it.

    Anti-coincidental-pass: both arms refuse with the *same* status and differ
    only in the flag the binding set, so a translation that read transience off
    the status code satisfies at most one arm.
    """
    doc = await _delivered_doc(graph_store, refusing_source_store, "deadbeef_b8")
    refusing_source_store.refuse_read = store_refusal(
        404, retryable=retryable, operation="read source"
    )
    documents = DocumentsService(graph_store, minimal_config)

    with pytest.raises(expected):
        await documents.get_document_with_content(doc.id, True, None)


# ---------------------------------------------------------------------------
# Re-projection reads the retained source through the same store
# ---------------------------------------------------------------------------


async def test_recompute_pipeline_source_read_refusal_is_typed(
    graph_store, minimal_config, ingestion_service, refusing_source_store
):
    """Re-projection reads the retained source back through the store, and a
    refusal on that read is typed like every other read of it.

    The read has three callers -- the operator-facing recompute, the ingest
    pipeline's own projection stage, and the worker's re-projection -- so the
    translation belongs at the read rather than at any one of them.

    Anti-coincidental-pass: the refusal is armed on the *read* and not on the
    existence probe that precedes it, and `source_reads` would stay at zero if
    the probe had refused instead. The companion below arms the probe, which
    separates a read-only wrap from a probe-only one; neither says anything
    about *where* the wrap sits, because both reach the read through this one
    caller. The placement rival -- a wrap lifted from the read up to this
    caller -- is excluded by the ingest test after them, which reaches the
    same read through a different caller and is the only test here that fails
    against it.
    """
    doc = await _delivered_doc(graph_store, refusing_source_store, "deadbeef_rp")
    refusing_source_store.refuse_read = store_refusal(403, retryable=False, operation="read source")

    with pytest.raises(VaultSourceStoreRefusedError) as excinfo:
        await ingestion_service.recompute_pipeline(doc.id)

    assert excinfo.value.code == "vault_source_store_refused"
    assert excinfo.value.status_code == 502
    assert excinfo.value.detail["source_path"] == doc.source_path
    assert excinfo.value.detail["operation"] == "read source"
    assert refusing_source_store.source_stats >= 1, "the existence probe must have run"


async def test_reprojection_read_refusal_is_typed_at_the_read_not_the_caller(
    graph_store, minimal_config, ingestion_service, refusing_source_store
):
    """The existence probe the re-projection makes before the read is typed too.

    Anti-coincidental-pass: pairs with the test above. That one arms the read
    and this one arms the probe, and the two store calls sit on either side of
    a `SourceFileNotFoundError` raise -- so a translation covering only the
    read leaves this one raw, and one covering only the probe leaves that one
    raw. Neither test alone separates a wrap around the pair from a wrap
    around either half, and neither constrains placement -- see the ingest
    test below for that. This also asserts the refusal is not folded into the
    absent-source error the probe raises when the store answers honestly.
    """
    doc = await _delivered_doc(graph_store, refusing_source_store, "deadbeef_rq")
    refusing_source_store.refuse_stat = store_refusal(429, retryable=True, operation="stat source")

    with pytest.raises(VaultSourceStoreUnavailableError) as excinfo:
        await ingestion_service.recompute_pipeline(doc.id)

    assert excinfo.value.code == "vault_source_store_unavailable"
    assert excinfo.value.code != "source_file_not_found"
    assert refusing_source_store.source_reads == 0


async def test_ingest_projection_read_refusal_is_typed(
    ingestion_service, refusing_source_store, tmp_path
):
    """The ingest pipeline's own projection stage reads the retained source back
    through the store, and a refusal there is typed too.

    Retention and the read-back are separate store operations on the same
    ingest: the bytes are written, then read back to project them. The write
    succeeding does not make the read safe, and the read sits outside the
    translation the retention runs under.

    Anti-coincidental-pass: this is the only test that reaches the re-projection
    read through a caller other than the recompute, and it is what excludes the
    placement rival the two tests above cannot. A wrap lifted off the read and
    re-opened around ``recompute_pipeline`` satisfies both of those and leaves
    this path raw -- which is the gap the review found on this branch, on this
    exact caller. ``source_uploads`` is asserted non-zero so the refusal is
    proven to have landed on the read-back rather than on the retention, which
    would exercise the retention's own long-standing translation instead.
    """
    external = tmp_path / "external.md"
    external.write_text("# External\n\nBody the ingest retains and then reads back.\n")
    refusing_source_store.refuse_read = store_refusal(403, retryable=False, operation="read source")

    with pytest.raises(VaultSourceStoreRefusedError) as excinfo:
        await ingestion_service.ingest(
            IngestRequest(source=str(external), source_type=SourceType.MARKDOWN)
        )

    assert excinfo.value.code == "vault_source_store_refused"
    assert excinfo.value.status_code == 502
    assert excinfo.value.detail["operation"] == "read source"
    assert refusing_source_store.source_uploads >= 1, (
        "the retention must have succeeded, so the refusal landed on the read-back"
    )
