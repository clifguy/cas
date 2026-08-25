"""Provider-neutral abstraction latency record emitted by the ingestion service.

The abstraction path has two entry points -- initial ingest and reabstract --
and two provider families whose vocabularies overlap only at wall-clock time.
The record under test lives at the one seam both entry points pass through and
carries only fields every provider can answer for; a provider with more to say
emits its own record alongside this one.
"""

import json
import logging
from pathlib import Path

import pytest

from sage.adapters.interfaces import AbstractionProvider
from sage.models.enums import SourceType
from sage.models.schemas import IngestRequest

TIMING_LOGGER = "sage.abstraction.timing"

# Fields whose meaning is specific to the local MLX implementation. They
# belong on that provider's own record, never on the neutral one.
IMPLEMENTATION_SPECIFIC_FIELDS = (
    "prefill_ms",
    "decode_ms",
    "retained_tokens",
    "input_tokens",
    "tokens_per_second",
)


class _NamedStubProvider(AbstractionProvider):
    """Abstraction provider whose class name identifies it in the record."""

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        return "A stub abstract sentence."


class _OtherNamedStubProvider(_NamedStubProvider):
    """A second, differently-named provider for the naming test."""


class _RaisingProvider(AbstractionProvider):
    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        raise RuntimeError("provider unavailable (simulated)")


def _records(caplog):
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == TIMING_LOGGER]


def _create_test_file(
    tmp_vault_dir: Path, relative_path: str, content: str = "# Test\n\nTest content."
) -> Path:
    full_path = tmp_vault_dir / "sources" / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content)
    return full_path


# ── Record shape ────────────────────────────────────────────────────


async def test_neutral_record_emitted_with_all_fields(ingestion_service, caplog):
    """One record per abstraction, carrying the provider-neutral fields."""
    text = "A document about record linkage across clinical systems."

    with caplog.at_level(logging.INFO, logger=TIMING_LOGGER):
        await ingestion_service._generate_abstract_text(text, "adr")

    records = _records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record["label"] == "abstract"
    assert record["input_chars"] == len(text)
    assert record["input_words"] == len(text.split())
    assert record["max_tokens"] > 0
    assert record["abstract_chars"] > 0
    assert record["duration_ms"] >= 0


@pytest.mark.parametrize("provider_cls", [_NamedStubProvider, _OtherNamedStubProvider])
async def test_neutral_record_names_the_serving_provider(ingestion_service, caplog, provider_cls):
    """The `provider` field reflects whichever provider actually served.

    Anti-coincidental-pass: parametrized over two providers so a hardcoded
    string, or one read from configuration rather than from the object in
    hand, fails on the second case.
    """
    ingestion_service._abstraction = provider_cls()

    with caplog.at_level(logging.INFO, logger=TIMING_LOGGER):
        await ingestion_service._generate_abstract_text("some document text", "adr")

    assert _records(caplog)[0]["provider"] == provider_cls.__name__


async def test_neutral_record_carries_no_implementation_specific_field(ingestion_service, caplog):
    """The neutral record stays neutral.

    Anti-coincidental-pass: emitting one fat record carrying every field the
    local provider knows would satisfy every other test here while making the
    record meaningless for a hosted provider, which has no prefill phase to
    report and would emit nulls forever.
    """
    with caplog.at_level(logging.INFO, logger=TIMING_LOGGER):
        await ingestion_service._generate_abstract_text("some document text", "adr")

    record = _records(caplog)[0]
    for field in IMPLEMENTATION_SPECIFIC_FIELDS:
        assert field not in record, f"neutral record leaked {field}"


async def test_neutral_record_absent_when_abstraction_raises(ingestion_service, caplog):
    """A failed generation emits no record and propagates unchanged.

    Anti-coincidental-pass: a `finally`-block emit would report a duration for
    a call that produced nothing, reading downstream as work delivered. Every
    happy-path test here passes with that bug in place.
    """
    ingestion_service._abstraction = _RaisingProvider()

    with caplog.at_level(logging.INFO, logger=TIMING_LOGGER):
        with pytest.raises(RuntimeError, match="provider unavailable"):
            await ingestion_service._generate_abstract_text("some document text", "adr")

    assert _records(caplog) == []


async def test_abstract_text_unchanged_by_instrumentation(ingestion_service):
    """The returned abstract is the trimmed provider output, unmodified."""
    ingestion_service._abstraction = _NamedStubProvider()

    result = await ingestion_service._generate_abstract_text("some document text", "adr")

    assert result == "A stub abstract sentence."


# ── Both entry paths ────────────────────────────────────────────────


async def test_neutral_record_emitted_on_initial_ingest_path(
    tmp_vault_dir, graph_store, ingestion_service, caplog
):
    """The initial-ingest path emits the record."""
    _create_test_file(tmp_vault_dir, "reports/timing_ingest.md")
    request = IngestRequest(source="reports/timing_ingest.md", source_type=SourceType.MARKDOWN)

    with caplog.at_level(logging.INFO, logger=TIMING_LOGGER):
        await ingestion_service.ingest(request)

    records = _records(caplog)
    assert len(records) == 1
    assert records[0]["label"] == "abstract"


async def test_neutral_record_emitted_on_reabstract_path(
    tmp_vault_dir, graph_store, ingestion_service, caplog
):
    """The reabstract path emits the record too.

    Anti-coincidental-pass: paired with the initial-ingest test above. An emit
    placed in one caller rather than in the seam both share passes whichever
    path happened to be tested and silently covers nothing on the other.
    """
    _create_test_file(tmp_vault_dir, "reports/timing_reabstract.md")
    request = IngestRequest(source="reports/timing_reabstract.md", source_type=SourceType.MARKDOWN)
    result = await ingestion_service.ingest(request)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=TIMING_LOGGER):
        await ingestion_service._execute_abstract_from_chunks(result.document.id, "adr")

    records = _records(caplog)
    assert len(records) == 1
    assert records[0]["label"] == "abstract"
