"""Provider-neutral records emitted at the ingestion service's abstraction seam.

The abstraction path has two entry points -- initial ingest and reabstract --
and two provider families whose vocabularies overlap only at wall-clock time.
The records under test live at the one seam both entry points pass through.

Two records share the seam. The latency record carries only fields every
provider can answer for; a provider with more to say emits its own record
alongside it. The faithfulness record reports an unattested acronym gloss
found in the generated abstract (CAS-ADR-020 clause (e)); it observes and
never mutates, so the returned abstract stays byte-identical to the
provider's trimmed output.
"""

import json
import logging
from pathlib import Path

import pytest

from sage.adapters.interfaces import AbstractionProvider
from sage.models.enums import SourceType
from sage.models.schemas import IngestRequest

TIMING_LOGGER = "sage.abstraction.timing"
FAITHFULNESS_LOGGER = "sage.abstraction.faithfulness"

# Fields whose meaning is specific to the local MLX implementation. They
# belong on that provider's own record, never on the neutral one. Phase
# counts and rates are the clearest case: a hosted API reports no prefill.
#
# `document_chars` is deliberately absent -- both records carry it, and it
# means the same thing on each.
IMPLEMENTATION_SPECIFIC_FIELDS = (
    "prompt_tokens",
    "prefill_ms",
    "prefill_tps",
    "decode_ms",
    "decode_tps",
    "retained_tokens",
    "input_tokens",
)


class _NamedStubProvider(AbstractionProvider):
    """Abstraction provider whose class name identifies it in the record.

    Records the size of what it submitted, so a test can compare the figure
    the record reports against the one that actually reached a model.
    """

    def __init__(self) -> None:
        self.sent_chars: int | None = None

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        self.sent_chars = len(text)
        return "A stub abstract sentence."


class _OtherNamedStubProvider(_NamedStubProvider):
    """A second, differently-named provider for the naming test."""


class _ReducingStubProvider(_NamedStubProvider):
    """A provider that fits its input to a budget before submitting it.

    Stands in for any provider that shrinks an over-length document to a
    model's input ceiling: the service handed over the whole projection, but
    only a prefix of it ever reached a model.
    """

    _BUDGET_CHARS = 64

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        submitted = text[: self._BUDGET_CHARS]
        self.sent_chars = len(submitted)
        return "A stub abstract sentence."


class _RaisingProvider(AbstractionProvider):
    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        raise RuntimeError("provider unavailable (simulated)")


class _GlossingStubProvider(AbstractionProvider):
    """A provider whose abstract glosses an acronym with a fixed expansion.

    Whether the gloss is attested is decided entirely by the source text a
    test pairs it with.
    """

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        return "This document describes the QZE (Quantum Zeta Exchange) protocol."


def _records(caplog):
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == TIMING_LOGGER]


def _faithfulness_records(caplog):
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == FAITHFULNESS_LOGGER]


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
    assert record["document_chars"] == len(text)
    assert "input_chars" not in record
    assert record["document_words"] == len(text.split())
    assert "input_words" not in record
    assert record["max_tokens"] > 0
    assert record["abstract_chars"] > 0
    assert record["duration_ms"] >= 0


async def test_neutral_record_measures_the_document_not_the_request(ingestion_service, caplog):
    """A provider that reduces its input does not change what the record says.

    `document_chars` measures the projection text the service handed the
    provider. What the provider forwarded, after fitting it to some model's
    input ceiling, is the provider's own business and is reported by the
    provider itself -- in tokens, where this figure is in characters. The two
    are not convertible, so the record must not invite the reading that they
    are the same number.
    """
    text = "A document about record linkage across clinical systems. " * 20
    provider = _ReducingStubProvider()
    ingestion_service._abstraction = provider

    with caplog.at_level(logging.INFO, logger=TIMING_LOGGER):
        await ingestion_service._generate_abstract_text(text, "adr")

    assert provider.sent_chars < len(text)
    assert _records(caplog)[0]["document_chars"] == len(text)


async def test_neutral_record_matches_submitted_size_when_nothing_reduced(
    ingestion_service, caplog
):
    """Control: with no reduction, document and request are the same size.

    Anti-coincidental-pass: this is what makes the reduction case above mean
    something. A figure pinned to the full text satisfies that test while
    being wrong for every provider; a figure taken from whatever the provider
    forwarded satisfies this one while being wrong for a reducing provider.
    Only a figure that measures the document passes both.
    """
    text = "A short document about record linkage."
    provider = _NamedStubProvider()
    ingestion_service._abstraction = provider

    with caplog.at_level(logging.INFO, logger=TIMING_LOGGER):
        await ingestion_service._generate_abstract_text(text, "adr")

    assert provider.sent_chars == len(text)
    assert _records(caplog)[0]["document_chars"] == len(text)


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


# ── Faithfulness record ─────────────────────────────────────────────


async def test_seam_emits_record_for_unattested_gloss(ingestion_service, caplog):
    """An unattested gloss emits a faithfulness record; the abstract survives.

    The byte-identity assertion on the return value is what proves the
    check's non-mutating posture: a seam that repaired the gloss early
    would hide the very signal its error rate is to be calibrated on.
    """
    text = "A document about the QZE protocol and its message framing."
    ingestion_service._abstraction = _GlossingStubProvider()

    with caplog.at_level(logging.INFO, logger=FAITHFULNESS_LOGGER):
        result = await ingestion_service._generate_abstract_text(text, "adr", document_id="doc-1")

    records = _faithfulness_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record["layer"] == "abstraction"
    assert record["label"] == "unattested_gloss"
    assert record["provider"] == "_GlossingStubProvider"
    assert record["document_id"] == "doc-1"
    assert record["acronym"] == "QZE"
    assert record["expansion"] == "Quantum Zeta Exchange"
    assert result == "This document describes the QZE (Quantum Zeta Exchange) protocol."


async def test_seam_stays_silent_for_attested_gloss(ingestion_service, caplog):
    """A gloss the source supplies emits nothing.

    Anti-coincidental-pass: without this, the unattested-gloss test above
    passes on a seam wired to log every gloss unconditionally -- a detector
    with no attestation check at all.
    """
    text = "The Quantum Zeta Exchange (QZE) protocol frames messages between services."
    ingestion_service._abstraction = _GlossingStubProvider()

    with caplog.at_level(logging.INFO, logger=FAITHFULNESS_LOGGER):
        await ingestion_service._generate_abstract_text(text, "adr", document_id="doc-1")

    assert _faithfulness_records(caplog) == []


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
