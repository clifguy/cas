"""The retrieval health check must exercise the retrieval a caller gets.

``eval_retrieval`` answers whether a vault still returns the documents an
operator expects for a set of known queries. That answer is only worth having
if it is measured on the path a caller actually uses: vector and keyword arms
fused, with the boosts applied. Measured on the vector arm alone it stays green
through a keyword-side regression, which is the failure mode this module pins.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from sage.adapters.interfaces import Chunk
from sage.adapters.stubs import StubContentStore
from sage.api.errors import AssertionsFileInvalidError, AssertionsNotConfiguredError
from sage.models.enums import SourceType
from sage.models.schemas import Document
from sage.services.utilities import UtilitiesService

pytestmark = pytest.mark.asyncio

_TARGET = "00000001_target"
_DECOY = "00000002_decoy"
_ABSENT = "00000003_absent"


def _doc(document_id: str, title: str) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=document_id,
        title=title,
        source_type=SourceType.MARKDOWN,
        source_path=f"imports/{document_id}.md",
        lifecycle_status="active",
        source_content_hash=f"sha256:{0:064x}",
        adapter_version="1",
        created_by="t",
        created_at=now,
        last_modified_by="t",
        updated_at=now,
        doc_type="adr",
    )


class _FixedEmbeddingProvider:
    """Embeds every text to the decoy's vector.

    The point of the fixture: the query embedding is identical to the decoy's
    and orthogonal to the target's, so the vector arm alone ranks the decoy
    first and can never surface the target. Only a keyword arm can.
    """

    def __init__(self, dim: int = 8) -> None:
        self._dim = dim
        self.decoy = [1.0] + [0.0] * (dim - 1)
        self.target = [0.0] * (dim - 1) + [1.0]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(self.decoy) for _ in texts]


async def _utilities_with_corpus(graph_store, config):
    """Seed a corpus whose target is reachable by keyword and not by vector."""
    content_store = StubContentStore()
    embedding = _FixedEmbeddingProvider()

    await graph_store.insert_document(_doc(_TARGET, "Target Document"))
    await graph_store.insert_document(_doc(_DECOY, "Decoy Document"))

    await content_store.index_chunks(
        _TARGET,
        [
            Chunk(
                document_id=_TARGET,
                heading_path="Body",
                content="zephyrword appears here and nowhere else",
                embedding=embedding.target,
                chunk_index=0,
            )
        ],
    )
    await content_store.index_chunks(
        _DECOY,
        [
            Chunk(
                document_id=_DECOY,
                heading_path="Body",
                content="entirely unrelated prose",
                embedding=embedding.decoy,
                chunk_index=0,
            )
        ],
    )
    return UtilitiesService(
        graph_store=graph_store,
        content_store=content_store,
        embedding_provider=embedding,
        config=config,
    )


def _write_assertions(config, assertions: list[dict]) -> str:
    """Write the assertions file where the service resolves it from."""
    root = Path(config.vault.storage_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / "retrieval_assertions.yaml"
    path.write_text(yaml.safe_dump({"assertions": assertions}), encoding="utf-8")
    return path.name


def _with_assertions_file(config, filename: str):
    config.retrieval_health = (
        type(config)
        .model_fields["retrieval_health"]
        .annotation.__args__[0](assertions_file=filename)
    )
    return config


async def test_a_keyword_only_match_satisfies_an_assertion(graph_store, minimal_config):
    """The check finds a document only the keyword arm can reach.

    The regression this closes: measured on the vector arm alone, the decoy
    wins every query and this assertion fails no matter how healthy keyword
    retrieval is -- so the check could not report on it at all.
    """
    utilities = await _utilities_with_corpus(graph_store, minimal_config)
    filename = _write_assertions(
        minimal_config,
        [{"query": "zephyrword", "expected_document_id": _TARGET, "top_k": 1}],
    )
    _with_assertions_file(minimal_config, filename)

    result = await utilities.eval_retrieval()

    assert result.passed, (
        f"the health check missed a document its keyword arm reaches: {result.failures}"
    )
    assert result.assertion_count == 1
    assert result.failure_count == 0


async def test_a_genuinely_absent_document_still_fails(graph_store, minimal_config):
    """The control: the check must still be able to report a failure.

    Without this, a check rewritten to pass everything would look identical to
    a check that works.
    """
    utilities = await _utilities_with_corpus(graph_store, minimal_config)
    filename = _write_assertions(
        minimal_config,
        [{"query": "wordthatappearsnowhere", "expected_document_id": _TARGET, "top_k": 1}],
    )
    _with_assertions_file(minimal_config, filename)

    result = await utilities.eval_retrieval()

    assert not result.passed
    assert result.failure_count == 1
    [failure] = result.failures
    assert failure.expected_document_id == _TARGET
    assert failure.found is False


async def test_unconfigured_assertions_still_raise(graph_store, minimal_config):
    """The existing contract is unchanged by the path this now measures."""
    utilities = await _utilities_with_corpus(graph_store, minimal_config)
    minimal_config.retrieval_health = None

    with pytest.raises(AssertionsNotConfiguredError):
        await utilities.eval_retrieval()


async def test_a_missed_assertion_reports_its_rank_rather_than_raising(graph_store, minimal_config):
    """The diagnostic lookup stays inside the request contract's ceiling.

    On a miss the check looks further down the ranking to report where the
    expected document actually landed. That wider lookup runs through the same
    request model, whose limit is capped, so an unclamped multiple would raise
    a validation error from inside the health check instead of reporting the
    failure the check exists to report.
    """
    utilities = await _utilities_with_corpus(graph_store, minimal_config)
    filename = _write_assertions(
        minimal_config,
        [{"query": "zephyrword", "expected_document_id": _ABSENT, "top_k": 40}],
    )
    _with_assertions_file(minimal_config, filename)

    result = await utilities.eval_retrieval()

    assert not result.passed
    assert result.failure_count == 1, "the miss was reported rather than raised"


async def test_a_top_k_above_the_ceiling_is_rejected_as_a_malformed_assertion(
    graph_store, minimal_config
):
    """A ``top_k`` the request model cannot express is a file error.

    Reported at load as a malformed assertion rather than surfacing as a
    validation error from deep inside the run, so the operator is told which
    file is wrong.
    """
    utilities = await _utilities_with_corpus(graph_store, minimal_config)
    filename = _write_assertions(
        minimal_config,
        [{"query": "zephyrword", "expected_document_id": _TARGET, "top_k": 500}],
    )
    _with_assertions_file(minimal_config, filename)

    with pytest.raises(AssertionsFileInvalidError):
        await utilities.eval_retrieval()
