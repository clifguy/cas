"""Hash-check behaviour, from ``VaultConfigService.hash_check`` up to its MCP tool.

The model-level normalization of ``Sha256Str`` is covered in
``test_alias_invariants.py`` and ``test_request_validators.py``; the per-transport
``invalid_sha256`` envelopes in ``test_api_integration.py`` and ``test_mcp_server.py``.

Two layers are covered here. First, what the service does with the canonical values it
receives: a non-canonical spelling reaches the stored document, variant spellings of one
digest collapse to a single entry, no input is omitted from the result, and the
empty-list case never touches the store. Second, the ``verify_hashes`` tool's own request
construction, which the service-level tests cannot reach because they build their
requests directly.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sage.adapters.stubs import StubGraphStore
from sage.models.enums import SourceType
from sage.models.schemas import Document, HashCheckRequest
from sage.services.vault_config import VaultConfigService

pytestmark = pytest.mark.asyncio

_DIGEST = "dc9fe77d99393de7562ed397253b4fa3f8c985bcd3aabc86b5d05bb3e9fc9bb9"
_CANONICAL = f"sha256:{_DIGEST}"
_STORED_DOC_ID = "15d2cc75_a_stored_document"


class _RecordingGraphStore(StubGraphStore):
    """``StubGraphStore`` plus a record of what each hash lookup was asked for.

    Subclasses rather than reimplements so the lookup keeps the stub's real
    matching semantics; the base already records ``close_calls`` for the same
    kind of assertion. The record is what lets a test distinguish "the store was
    never consulted" from "the store was consulted and returned nothing", which
    a ``{}`` result alone cannot.
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[str]] = []

    async def find_documents_by_hashes(self, hashes: list[str]) -> dict[str, str]:
        self.calls.append(list(hashes))
        return await super().find_documents_by_hashes(hashes)


def _document(doc_id: str, content_hash: str) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=doc_id,
        title="Stored document",
        source_type=SourceType.MARKDOWN,
        source_path="stored.md",
        lifecycle_status="active",
        source_content_hash=content_hash,
        adapter_version="1.0",
        created_by="test",
        created_at=now,
        last_modified_by="test",
        updated_at=now,
    )


async def _service(
    stored_hash: str | None = None,
) -> tuple[VaultConfigService, _RecordingGraphStore]:
    """Build the service over a stub holding at most one document.

    ``stored_hash`` is written through ``Document``, so the stub holds whatever
    spelling ``Sha256Str`` produced rather than a hand-written fixture key --
    the lookup is therefore matched against a real post-alias value.
    """
    store = _RecordingGraphStore()
    if stored_hash is not None:
        await store.insert_document(_document(_STORED_DOC_ID, stored_hash))
    # content_store / config / registry_service are unused on the hash_check path.
    return VaultConfigService(store, None, None, None), store


async def test_bare_hex_resolves_to_a_document_stored_under_the_canonical_form():
    """A non-canonical spelling must reach the document stored under the canonical one.

    The store holds only the canonical spelling -- asserted here, so the test cannot
    pass by the fixture happening to store the bare form.
    """
    service, store = await _service(_CANONICAL)
    # The stored spelling is canonical, so a bare-hex hit cannot come from the
    # fixture happening to hold the bare form.
    assert await store.get_document(_STORED_DOC_ID) is not None
    assert (await store.get_document(_STORED_DOC_ID)).source_content_hash == _CANONICAL

    result = await service.hash_check(HashCheckRequest(hashes=[_DIGEST]))

    assert result[_CANONICAL].exists is True
    assert result[_CANONICAL].document_id == _STORED_DOC_ID
    # The lookup was issued in canonical form, not as the caller spelled it.
    assert store.calls == [[_CANONICAL]]


async def test_uppercase_hex_resolves_to_the_same_document():
    service, _ = await _service(_CANONICAL)

    result = await service.hash_check(HashCheckRequest(hashes=[_DIGEST.upper()]))

    assert result[_CANONICAL].exists is True
    assert result[_CANONICAL].document_id == _STORED_DOC_ID


async def test_variant_spellings_of_one_digest_collapse_to_a_single_entry():
    """Keys are canonical, so two spellings cannot produce two disagreeing rows."""
    service, _ = await _service(_CANONICAL)

    result = await service.hash_check(
        HashCheckRequest(hashes=[_DIGEST, _CANONICAL, _DIGEST.upper()])
    )

    assert len(result) == 1
    assert list(result) == [_CANONICAL]
    assert result[_CANONICAL].exists is True


async def test_unmatched_hash_is_present_with_exists_false_not_omitted():
    """An absent hash gets an entry; nothing is dropped from the result."""
    absent = "sha256:" + "b" * 64
    service, _ = await _service(_CANONICAL)

    result = await service.hash_check(HashCheckRequest(hashes=[_CANONICAL, absent]))

    assert set(result) == {_CANONICAL, absent}
    assert result[_CANONICAL].exists is True
    assert result[absent].exists is False
    assert result[absent].document_id is None


async def test_all_unknown_returns_a_full_dict_not_an_empty_one():
    """The distinction the docstrings used to deny: all-unknown is not empty."""
    a = "sha256:" + "a" * 64
    b = "sha256:" + "b" * 64
    service, _ = await _service()

    result = await service.hash_check(HashCheckRequest(hashes=[a, b]))

    assert result != {}
    assert len(result) == 2
    assert all(m.exists is False for m in result.values())


async def test_empty_list_short_circuits_without_consulting_the_store():
    service, store = await _service(_CANONICAL)

    result = await service.hash_check(HashCheckRequest(hashes=[]))

    assert result == {}
    # The load-bearing half: empty result *because* nothing was asked, not
    # because a lookup came back empty.
    assert store.calls == []


# ---------------------------------------------------------------------------
# The verify_hashes MCP wrapper.
#
# These cover the wrapper's own request construction, which the service tests
# above never reach: they build their requests directly. They are also the only
# Postgres-free coverage of that construction site -- the app-surface tests that
# also catch a restored `HashCheckRequest.model_construct(...)` bypass need a
# live DSN, so without these a bypass regression is invisible to any run that
# lacks one.
# ---------------------------------------------------------------------------


async def _verify_hashes_tool(stored_hash: str | None = None):
    """Register the real tool against a fake vault, no Postgres involved."""
    from mcp.server.fastmcp import FastMCP

    from sage.mcp_server import _error_response, _serialize
    from sage.sage_api_tools import register_sage_tools

    service, store = await _service(stored_hash)

    class _Services:
        vault_config_service = service

    tools = register_sage_tools(
        FastMCP("test"),
        lambda vault_id: _Services(),
        _serialize,
        _error_response,
        lambda: {},
        lambda: None,
    )
    return tools["verify_hashes"], store


async def test_tool_normalizes_bare_hex_before_the_lookup():
    """Guards the removal of the model_construct validation bypass.

    With the bypass restored the raw bare-hex string reaches the store, misses,
    and the caller silently gets exists=false -- the defect this change fixes.
    """
    tool, store = await _verify_hashes_tool(_CANONICAL)

    result = await tool(vault_id="test_vault", hashes=[_DIGEST])

    assert store.calls == [[_CANONICAL]], "bare hex must be canonicalized before lookup"
    assert result[_CANONICAL]["exists"] is True
    assert result[_CANONICAL]["document_id"] == _STORED_DOC_ID


async def test_tool_rejects_malformed_hash_with_the_invalid_sha256_envelope():
    """Malformed input must reject loudly rather than surface as exists=false."""
    tool, store = await _verify_hashes_tool()

    result = await tool(vault_id="test_vault", hashes=["deadbeef"])

    assert result["error"] == "invalid_sha256"
    # CAS-ADR-040: the envelope names the value as the caller supplied it,
    # not the normalization attempt ("sha256:deadbeef").
    assert result["detail"]["sha256"] == "deadbeef"
    # And nothing malformed reached the store to masquerade as a miss.
    assert store.calls == []
