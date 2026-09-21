"""The retrieval-health assertions file is read through the vault-source store.

``eval_retrieval`` names its assertions file by a storage-root-relative path
and reads it through the vault-source store, addressed the way a document's
``source_path`` is (CAS-ADR-043). The same configuration therefore reaches the
file under either binding: on the local tree under the filesystem binding, and
in the vault's folder in the document store under the document-store binding,
where the storage root is inert local disk. Both request surfaces share that
one read, so the operation behaves alike under both deployment profiles
(CAS-ADR-052).

Each case runs against the real ``DocumentStoreVaultSourceStore`` with only the
Graph transport faked, so the document-store arm exercises the binding's own
addressing rather than a stand-in for it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sage.adapters.stubs import StubContentStore
from sage.api.errors import (
    AssertionsFileInvalidError,
    AssertionsFileNotFoundError,
    VaultSourceStoreRefusedError,
)
from sage.config import RetrievalHealthConfig
from sage.services.utilities import UtilitiesService
from tests.helpers.store_refusal import store_refusal
from tests.helpers.vault_source_selection import select_vault_source_binding

pytestmark = pytest.mark.asyncio

_HIT_ID = "aaaaaaaa_store_hit"
_ASSERTIONS = "retrieval_assertions.yaml"
_VALID = yaml.safe_dump(
    {"assertions": [{"query": "found query", "expected_document_id": _HIT_ID, "top_k": 5}]}
).encode()

_BOTH = pytest.mark.parametrize("backend", ["filesystem", "document_store"])


class _NoEmbedding:
    async def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover
        raise AssertionError("ranking is fixed; nothing should be embedded")


@pytest.fixture
def fixed_ranking(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every query ranks exactly the hit document first."""

    async def ranked(self: UtilitiesService, query: str, limit: int) -> list[str]:
        return [_HIT_ID]

    monkeypatch.setattr(UtilitiesService, "_eval_ranked_document_ids", ranked)


def _storage_root(config) -> Path:
    root = Path(config.vault.storage_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _service(graph_store, config, assertions_file: str) -> UtilitiesService:
    config.retrieval_health = RetrievalHealthConfig(assertions_file=assertions_file)
    return UtilitiesService(
        graph_store=graph_store,
        content_store=StubContentStore(),
        embedding_provider=_NoEmbedding(),
        config=config,
    )


def _seed(backend: str, fake, config, path: str, body: bytes) -> None:
    """Place ``body`` where the selected binding keeps ``path``."""
    if backend == "filesystem":
        target = _storage_root(config) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    else:
        fake.sources[path] = body


@_BOTH
async def test_assertions_are_read_through_the_active_binding(
    backend, monkeypatch, graph_store, minimal_config, fixed_ranking
):
    """SB-1: the configured path reaches the file under either binding.

    Anti-coincidental-pass: the document-store arm asserts that no copy exists
    on the local tree, so a pass there can only come from the store read --
    a read of local disk, the behaviour this replaces, finds nothing.
    """
    fake = select_vault_source_binding(monkeypatch, backend)
    _seed(backend, fake, minimal_config, _ASSERTIONS, _VALID)
    service = _service(graph_store, minimal_config, _ASSERTIONS)

    report = await service.eval_retrieval()

    assert report.passed is True
    assert report.assertion_count == 1
    assert report.failure_count == 0
    if backend == "document_store":
        assert not (_storage_root(minimal_config) / _ASSERTIONS).exists()
        assert fake.source_reads == 1


@_BOTH
async def test_a_missing_assertions_file_is_not_found_on_either_binding(
    backend, monkeypatch, graph_store, minimal_config
):
    """SB-2: an absent file is ``assertions_file_not_found`` (404) on both bindings.

    Anti-coincidental-pass: the document-store binding reports a missing key
    as a store refusal when read directly, so a read not preceded by an
    existence check surfaces as a 502, never this 404.
    """
    select_vault_source_binding(monkeypatch, backend)
    service = _service(graph_store, minimal_config, _ASSERTIONS)

    with pytest.raises(AssertionsFileNotFoundError) as excinfo:
        await service.eval_retrieval()

    assert excinfo.value.status_code == 404
    assert excinfo.value.detail["assertions_file"] == _ASSERTIONS


@_BOTH
async def test_an_assertions_path_leaving_the_storage_root_is_refused(
    backend, monkeypatch, graph_store, minimal_config
):
    """SB-3: a path escaping the storage root is refused before the store is asked.

    Anti-coincidental-pass: a valid assertions file sits at the escaped
    location on both arms, so without the containment check the run would
    succeed; and the document-store arm counts the store's stats and reads, so
    the refusal is proven to precede both.
    """
    fake = select_vault_source_binding(monkeypatch, backend)
    escaped = "../outside.yaml"
    if backend == "filesystem":
        (_storage_root(minimal_config).parent / "outside.yaml").write_bytes(_VALID)
    else:
        fake.sources[escaped] = _VALID
    service = _service(graph_store, minimal_config, escaped)

    with pytest.raises(AssertionsFileInvalidError) as excinfo:
        await service.eval_retrieval()

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["assertions_file"] == escaped
    assert "storage root" in excinfo.value.detail["reason"]
    if backend == "document_store":
        assert (fake.source_stats, fake.source_reads) == (0, 0)


async def test_an_absolute_assertions_path_is_refused(monkeypatch, graph_store, minimal_config):
    """SB-3a: an absolute path leaves the storage root as surely as ``..`` does.

    Joined onto the storage root, an absolute path replaces it outright, so a
    local read would honour it. Anti-coincidental-pass: a valid file sits at the
    absolute location, so a containment check that only looks for ``..``
    segments lets the run succeed here.
    """
    select_vault_source_binding(monkeypatch, "filesystem")
    outside = _storage_root(minimal_config).parent / "absolute.yaml"
    outside.write_bytes(_VALID)
    service = _service(graph_store, minimal_config, str(outside))

    with pytest.raises(AssertionsFileInvalidError) as excinfo:
        await service.eval_retrieval()

    assert excinfo.value.detail["assertions_file"] == str(outside)
    assert "storage root" in excinfo.value.detail["reason"]


async def test_malformed_yaml_read_from_the_store_is_invalid(
    monkeypatch, graph_store, minimal_config
):
    """SB-4: bytes pulled from the document store are parsed as the local file was."""
    fake = select_vault_source_binding(monkeypatch, "document_store")
    fake.sources[_ASSERTIONS] = b"assertions: [unclosed"
    service = _service(graph_store, minimal_config, _ASSERTIONS)

    with pytest.raises(AssertionsFileInvalidError) as excinfo:
        await service.eval_retrieval()

    assert excinfo.value.detail["assertions_file"] == _ASSERTIONS


async def test_a_store_refusal_on_the_read_is_typed(monkeypatch, graph_store, minimal_config):
    """SB-5: a store declining the read reaches the caller as ``vault_source_store_refused``.

    ``detail["operation"]`` echoes the refusal the fake was given, so on its own
    it says nothing about which probe raised. The stat count does: one stat
    succeeded before the read was refused, so the refusal came from the read,
    after the existence probe had answered.
    """
    fake = select_vault_source_binding(monkeypatch, "document_store")
    fake.sources[_ASSERTIONS] = _VALID
    fake.refuse_read = store_refusal(403, retryable=False, operation="read source")
    service = _service(graph_store, minimal_config, _ASSERTIONS)

    with pytest.raises(VaultSourceStoreRefusedError) as excinfo:
        await service.eval_retrieval()

    exc = excinfo.value
    assert exc.status_code == 502
    assert exc.detail["operation"] == "read source"
    assert exc.detail["source_path"] == _ASSERTIONS
    assert fake.source_stats == 1


async def test_a_store_refusal_on_the_existence_probe_is_typed(
    monkeypatch, graph_store, minimal_config
):
    """SB-6: a store declining the existence probe is typed too, never a 404 or 500.

    Anti-coincidental-pass: the file is present in the store, so a probe whose
    refusal were swallowed as "absent" would surface as
    ``assertions_file_not_found``; and no read is counted, so the refusal is
    proven to come from the probe rather than the read after it.
    """
    fake = select_vault_source_binding(monkeypatch, "document_store")
    fake.sources[_ASSERTIONS] = _VALID
    fake.refuse_stat = store_refusal(403, retryable=False, operation="stat source")
    service = _service(graph_store, minimal_config, _ASSERTIONS)

    with pytest.raises(VaultSourceStoreRefusedError) as excinfo:
        await service.eval_retrieval()

    exc = excinfo.value
    assert exc.status_code == 502
    assert exc.detail["operation"] == "stat source"
    assert fake.source_reads == 0
