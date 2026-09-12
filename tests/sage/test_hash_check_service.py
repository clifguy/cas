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
from sage.config import VaultConfig
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
        self.preferences: list[frozenset[str]] = []

    async def find_documents_by_hashes(
        self, hashes: list[str], *, prefer_lifecycle_statuses: frozenset[str]
    ) -> dict[str, str]:
        self.calls.append(list(hashes))
        self.preferences.append(prefer_lifecycle_statuses)
        return await super().find_documents_by_hashes(
            hashes, prefer_lifecycle_statuses=prefer_lifecycle_statuses
        )


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


# A real config, because the hash lookup asks under the vault's own rule for
# which document represents a hash. The base lifecycle is the whole point: it
# is what makes `supersession_surviving_states()` return anything to assert.
# Paths need not exist -- nothing on this path reads them.
_CONFIG = VaultConfig.model_validate(
    {
        "vault": {
            "id": "test_vault",
            "name": "Test Vault",
            "owner": "testuser",
            "storage_root": "/unused/sources",
            "brain_root": "/unused/brain",
            "visibility": "personal",
        },
        "document_types": {"doc_types": [{"value": "note", "label": "Note"}]},
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
        "metadata_extraction": {},
        "edge_inference": {},
    }
)


# The same vault with one more state a supersession does not retire into, and
# that state is terminal. Both properties are load-bearing and they close
# different rivals.
#
# Not-retired-into: the base lifecycle's surviving set is exactly
# `{active, completed}`, so an assertion made against a base vault cannot
# separate "read the vault's rule" from "hard-coded that literal" -- the one
# thing the port's contract forbids, the set being the vault's to declare.
#
# Terminal: on the base lifecycle `archived` is at once the only terminal
# state and the only supersede landing, so `supersession_surviving_states()`
# and the complement of `terminal_states()` are the same set, and a call site
# reading the second when it means the first is invisible. `sealed` is in the
# first and not in the second.
_EXTENDED_CONFIG = VaultConfig.model_validate(
    {
        **_CONFIG.model_dump(mode="json"),
        "lifecycle": {
            **_CONFIG.lifecycle.model_dump(mode="json"),
            "states": [
                *_CONFIG.lifecycle.model_dump(mode="json")["states"],
                {"value": "sealed", "label": "Sealed", "is_terminal": True},
            ],
            "transitions": [
                *_CONFIG.lifecycle.model_dump(mode="json")["transitions"],
                {"from_state": "active", "action": "seal", "to_state": "sealed"},
            ],
        },
    }
)


async def _service(
    stored_hash: str | None = None,
    config: VaultConfig = _CONFIG,
) -> tuple[VaultConfigService, _RecordingGraphStore]:
    """Build the service over a stub holding at most one document.

    ``stored_hash`` is written through ``Document``, so the stub holds whatever
    spelling ``Sha256Str`` produced rather than a hand-written fixture key --
    the lookup is therefore matched against a real post-alias value.
    """
    store = _RecordingGraphStore()
    if stored_hash is not None:
        await store.insert_document(_document(_STORED_DOC_ID, stored_hash))
    # content_store / registry_service are unused on the hash_check path.
    return VaultConfigService(store, None, config, None), store


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


async def test_hash_check_names_the_surviving_document_when_several_carry_the_hash():
    """Among several documents on one hash, the named one is the survivor.

    The surviving document carries the *higher* id on purpose. Given the lower
    one it would win on the id tie-break alone and this would pin nothing.
    """
    retired = _document("00000001_retired_holder", _CANONICAL).model_copy(
        update={"lifecycle_status": "archived", "source_path": "retired.md"}
    )
    surviving = _document("00000002_surviving_holder", _CANONICAL)
    service, store = await _service()
    await store.insert_document(retired)
    await store.insert_document(surviving)

    result = await service.hash_check(HashCheckRequest(hashes=[_CANONICAL]))

    assert result[_CANONICAL].exists is True
    assert result[_CANONICAL].document_id == surviving.id


async def test_hash_check_states_the_vaults_surviving_states_to_the_store():
    """The lookup is issued under the vault's rule, not one of the service's.

    An argument assertion. Every other fixture in this file holds at most one
    document per hash, where an empty preference and the vault's own set give
    the same answer -- so only the recorded argument distinguishes a service
    that asked under the vault's rule from one that did not ask for any.

    Run against the *extended* vault, which separates two further rivals the
    two named above do not reach. One is a service that hard-coded the base
    lifecycle's ``{active, completed}``: against a base vault that literal and
    the derived set are the same value, so the assertion would hold while the
    contract -- the set is the vault's to declare -- was being broken. The
    other is a service reading ``terminal_states()`` and complementing it,
    which is a different rule that happens to agree on the base lifecycle.
    """
    service, store = await _service(_CANONICAL, config=_EXTENDED_CONFIG)
    lifecycle = _EXTENDED_CONFIG.lifecycle
    surviving = lifecycle.supersession_surviving_states()
    declared = frozenset(state.value for state in lifecycle.states)
    assert "sealed" in surviving, (
        "extended config no longer widens the surviving set; the assertion "
        "below would pass against a hard-coded base-lifecycle literal"
    )
    assert surviving != declared - lifecycle.terminal_states(), (
        "extended config no longer separates the surviving set from the "
        "non-terminal set; the assertion below would pass against a service "
        "reading terminal_states() instead"
    )

    await service.hash_check(HashCheckRequest(hashes=[_CANONICAL]))

    assert store.preferences == [surviving]


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
