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
from sage.models.schemas import Document
from sage.services.documents import DocumentsService
from tests.helpers.store_refusal import STORE_BODY, store_refusal


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

    Anti-coincidental-pass: the assertion is that the call raises at all. This
    path returns a delivery object holding a not-yet-pulled chunk iterator, so
    a translation placed where it cannot cover the pre-stream reads would let
    the call return normally and fail here.
    """
    doc = await _delivered_doc(graph_store, refusing_source_store, "deadbeef_b4")
    refusing_source_store.refuse_stat = store_refusal(403, retryable=False, operation="stat source")
    documents = DocumentsService(graph_store, minimal_config)

    with pytest.raises(VaultSourceStoreRefusedError) as excinfo:
        await documents.get_document_content(doc.id)

    assert excinfo.value.status_code == 502


async def test_get_download_url_refusal_is_typed_not_unavailable(
    graph_store, minimal_config, refusing_source_store
):
    """A refusal while minting a download URL is reported as the store
    declining, not as the binding being unable to answer.

    Anti-coincidental-pass: this path already has a structured refusal for the
    binding that cannot mint a URL at all (``download_url_unavailable``, 501).
    That is a different fact -- a capability the binding lacks, rather than a
    store that declined -- and a rival letting the refusal fall through into it
    would satisfy a test asserting only that a typed error came out.
    """
    doc = await _delivered_doc(graph_store, refusing_source_store, "deadbeef_b5")
    refusing_source_store.refuse_stat = store_refusal(503, retryable=True, operation="stat source")
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
    passes every other test in this module and fails here.
    """
    doc = await _delivered_doc(graph_store, refusing_source_store, "deadbeef_b6")
    monkeypatch.setattr("sage.mcp_init.caller_local_filesystem_reachable", lambda: False)
    refusing_source_store.refuse_hash = store_refusal(403, retryable=False, operation="hash source")
    documents = DocumentsService(graph_store, minimal_config)

    with pytest.raises(VaultSourceStoreRefusedError) as excinfo:
        await documents.get_document_with_content(doc.id, False, "/caller/local/copy.md")

    assert excinfo.value.detail["operation"] == "hash source"
    assert excinfo.value.status_code == 502


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
