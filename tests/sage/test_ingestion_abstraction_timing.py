"""Provider-neutral records emitted at the ingestion service's abstraction seam.

The abstraction path has two entry points -- initial ingest and reabstract --
and two provider families whose vocabularies overlap only at wall-clock time.
The records under test live at the one seam both entry points pass through.

The latency record carries only fields every provider can answer for; a
provider with more to say emits its own record alongside it. The
faithfulness records report four finding classes. An unattested acronym
gloss (CAS-ADR-020 clause (e)) is recorded and then collapsed to its bare
acronym -- the repair posture clause (h) admits once the check's error
rate is measured -- so the returned abstract carries no unattested claim.
The structure-echo check (clause (k)), the fabricated-cardinal check
(clause (e), a separate finding class), and the type-restating-opener
check (clause (f)) have no adjudicated measurement of their own and
record only. Attested glosses and the recording-only checks leave the
abstract byte-identical to the provider's trimmed output.
"""

import json
import logging
from pathlib import Path

import pytest

from sage.adapters.interfaces import AbstractionProvider
from sage.config import VaultConfig
from sage.models.enums import SourceType
from sage.models.schemas import IngestRequest
from sage.services.ingestion import IngestionService

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


class _PluralGlossingStubProvider(AbstractionProvider):
    """A provider whose gloss pluralizes the expansion's final word.

    Paired with a source attesting the singular, its output is the
    measured near-miss shape: attested to a human reader, a mismatch to
    an exact substring test.
    """

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        return "This document describes the QZE (Quantum Zeta Exchanges) protocol."


#: Named at module scope so the non-mutating assertion below compares against
#: the exact bytes the provider returned rather than restating them, which is
#: what makes that assertion a byte-identity check instead of a prefix check.
#: It ends on a complete sentence, so the sentence-boundary trim is a no-op and
#: any difference between output and result is the check having edited it.
_OUTLINE_ABSTRACT = "# Part 2: Sections 7-11\n\n## 7. Integration\n\nThe center integrates."


class _OutliningStubProvider(AbstractionProvider):
    """A provider that answers with document structure instead of prose.

    Mirrors the measured breach: given a source ending in a directive, the
    model produced the document that directive asked for, headings and all.
    """

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        return _OUTLINE_ABSTRACT


#: A transcript-shaped source with exactly three turn headings, so a stub's
#: asserted turn count is judged against a derivable value of 3.
_TURN_TRANSCRIPT = (
    "### Turn 1 — Reviewer\n\nOpening remarks on the draft.\n\n"
    "### Turn 2 — Author\n\nA reply addressing the remarks.\n\n"
    "### Turn 3 — Reviewer\n\nClosing notes.\n"
)

#: Named at module scope for the same byte-identity reason as
#: _OUTLINE_ABSTRACT: the non-mutating assertion compares against the exact
#: bytes the provider returned. Ends on a complete sentence, so the
#: sentence-boundary trim is a no-op.
_MISCOUNTING_ABSTRACT = "The transcript unfolds across twenty-six turns of discussion."


class _MiscountingStubProvider(AbstractionProvider):
    """A provider whose abstract asserts a turn count the source contradicts.

    Mirrors the measured breach: an abstract stating an exact count of a
    source-derivable unit that the source neither exhibits nor states.
    """

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        return _MISCOUNTING_ABSTRACT


class _AccurateCountStubProvider(AbstractionProvider):
    """A provider whose asserted turn count agrees with the source."""

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        return "The transcript unfolds across three turns of discussion."


#: Three turn headings whose numbering never states the value 3, so a claim
#: of "three turns" agrees with the derivable count while remaining
#: unattested -- the sub-class that separates the record's two fields.
_GAPPED_TURN_TRANSCRIPT = (
    "### Turn 2 — Reviewer\n\nOpening remarks on the draft.\n\n"
    "### Turn 4 — Author\n\nA reply addressing the remarks.\n\n"
    "### Turn 6 — Reviewer\n\nClosing notes.\n"
)

#: Named at module scope for the same byte-identity reason as
#: _OUTLINE_ABSTRACT: the non-mutating assertion compares against the exact
#: bytes the provider returned. Ends on a complete sentence, so the
#: sentence-boundary trim is a no-op; the parenthetical is not a gloss
#: candidate (the hyphenated id falls outside the acronym shape), so the
#: clause (e) repair leaves it untouched.
_TYPE_RESTATING_ABSTRACT = (
    "This document serves as an accepted Architecture Decision Record "
    "(ADR-029) that revises the retention policy."
)


class _TypeRestatingStubProvider(AbstractionProvider):
    """A provider whose abstract opens by restating the document's type.

    Mirrors the measured breach. Whether the opener is a finding is
    decided entirely by the doc_type a test pairs it with.
    """

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        return _TYPE_RESTATING_ABSTRACT


class _ContentfulOpenerStubProvider(AbstractionProvider):
    """A provider whose opener mentions a type word as content, not class."""

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        return "This document describes the partitioning of the ticket store across vaults."


class _GlossedOpenerStubProvider(AbstractionProvider):
    """A provider whose type-restating opener carries a collapsible gloss.

    Paired with a source that does not attest the expansion, the clause
    (e) repair rewrites the opener before the recording-only checks read
    it -- the one case where check ordering is observable in the record.
    """

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        return "This document serves as an Architecture Decision Record (ADR) governing retention."


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
    assert record["vault_id"] == "test_vault"
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


async def test_seam_records_and_collapses_an_unattested_gloss(ingestion_service, caplog):
    """An unattested gloss is recorded and collapsed to its bare acronym.

    The full-string assertion on the return value proves the repair
    happened and that nothing else changed: every byte outside the gloss
    span is pinned. Named rivals: a record-only seam returns the gloss
    intact and fails the result assertion; a repair that truncates or
    regenerates fails the exact string; a repair without a record, or
    whose record omits the disposition, fails the record assertions.
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
    assert record["vault_id"] == "test_vault"
    assert record["document_id"] == "doc-1"
    assert record["acronym"] == "QZE"
    assert record["expansion"] == "Quantum Zeta Exchange"
    assert record["action"] == "collapsed"
    # The record describes the inspected (pre-repair) abstract: a seam
    # that logged after collapsing would report the repaired length.
    assert record["abstract_chars"] == len(
        "This document describes the QZE (Quantum Zeta Exchange) protocol."
    )
    assert result == "This document describes the QZE protocol."


async def test_seam_stays_silent_for_attested_gloss(ingestion_service, caplog):
    """A gloss the source supplies emits nothing and survives byte-identical.

    Anti-coincidental-pass: without this, the unattested-gloss test above
    passes on a seam wired to log and collapse every gloss unconditionally
    -- a detector with no attestation check at all. The byte-identity
    assertion is the asymmetric-cost rule in executable form: repair
    touches only the finding class whose measured false-positive rate is
    zero, and an attested gloss is not a finding at all.
    """
    text = "The Quantum Zeta Exchange (QZE) protocol frames messages between services."
    ingestion_service._abstraction = _GlossingStubProvider()

    with caplog.at_level(logging.INFO, logger=FAITHFULNESS_LOGGER):
        result = await ingestion_service._generate_abstract_text(text, "adr", document_id="doc-1")

    assert _faithfulness_records(caplog) == []
    assert result == "This document describes the QZE (Quantum Zeta Exchange) protocol."


async def test_seam_leaves_a_near_miss_gloss_unflagged_and_unmodified(ingestion_service, caplog):
    """A last-word plural of an attested expansion is neither recorded nor repaired.

    Kills a promotion wired to the strict pre-widening detector: that seam
    would flag and collapse exactly the near-miss glosses a human reads as
    attested -- the one finding class whose measured false-positive rate
    was nonzero, which the widened attestation exists to empty before any
    mutation is allowed.
    """
    text = "The Quantum Zeta Exchange (QZE) protocol frames messages between services."
    ingestion_service._abstraction = _PluralGlossingStubProvider()

    with caplog.at_level(logging.INFO, logger=FAITHFULNESS_LOGGER):
        result = await ingestion_service._generate_abstract_text(text, "adr", document_id="doc-1")

    assert _faithfulness_records(caplog) == []
    assert result == "This document describes the QZE (Quantum Zeta Exchanges) protocol."


async def test_seam_emits_record_for_structure_echo(ingestion_service, caplog):
    """Structural markup in an abstract emits a record naming what tripped it."""
    ingestion_service._abstraction = _OutliningStubProvider()

    with caplog.at_level(logging.INFO, logger=FAITHFULNESS_LOGGER):
        await ingestion_service._generate_abstract_text(
            "A transcript ending in a task brief.", "chat_transcript", document_id="doc-2"
        )

    records = [r for r in _faithfulness_records(caplog) if r["label"] == "structure_echo"]
    assert [r["kind"] for r in records] == ["heading", "heading"]
    assert records[0]["line"] == _OUTLINE_ABSTRACT.splitlines()[0]
    assert records[0]["provider"] == "_OutliningStubProvider"
    assert records[0]["vault_id"] == "test_vault"
    assert records[0]["document_id"] == "doc-2"


async def test_seam_leaves_a_structure_echo_abstract_unmodified(ingestion_service, caplog):
    """The check records; it does not repair.

    Byte identity against the provider's own output is what proves the
    non-mutating posture CAS-ADR-020 requires until the check's error rate is
    measured. A prefix assertion would not: a seam that kept the first line
    and discarded the flagged remainder passes ``startswith`` while doing
    exactly the repair this posture forbids.
    """
    ingestion_service._abstraction = _OutliningStubProvider()

    with caplog.at_level(logging.INFO, logger=FAITHFULNESS_LOGGER):
        result = await ingestion_service._generate_abstract_text(
            "A transcript ending in a task brief.", "chat_transcript", document_id="doc-2"
        )

    assert result == _OUTLINE_ABSTRACT


async def test_seam_stays_silent_for_a_prose_abstract(ingestion_service, caplog):
    """Prose emits nothing.

    Anti-coincidental-pass: without this, the test above passes on a seam
    wired to log unconditionally -- a detector that never inspects anything.
    """
    ingestion_service._abstraction = _NamedStubProvider()

    with caplog.at_level(logging.INFO, logger=FAITHFULNESS_LOGGER):
        await ingestion_service._generate_abstract_text(
            "An ordinary document.", "adr", document_id="doc-3"
        )

    assert [r for r in _faithfulness_records(caplog) if r["label"] == "structure_echo"] == []


async def test_seam_records_a_fabricated_cardinal(ingestion_service, caplog):
    """A fabricated turn count emits a record with the adjudication fields.

    The ``"action" not in record`` assertion pins the recording posture: a
    seam promoted to repair grows that field, and this test is the one
    that fails when it does before an adjudicated measurement licenses it.
    """
    ingestion_service._abstraction = _MiscountingStubProvider()

    with caplog.at_level(logging.INFO, logger=FAITHFULNESS_LOGGER):
        await ingestion_service._generate_abstract_text(
            _TURN_TRANSCRIPT, "chat_transcript", document_id="doc-4"
        )

    records = [r for r in _faithfulness_records(caplog) if r["label"] == "fabricated_cardinal"]
    assert len(records) == 1
    record = records[0]
    assert record["layer"] == "abstraction"
    assert record["provider"] == "_MiscountingStubProvider"
    assert record["vault_id"] == "test_vault"
    assert record["document_id"] == "doc-4"
    assert record["surface"] == "twenty six turns"
    assert record["value"] == 26
    assert record["unit"] == "turn"
    assert record["derived"] == 3
    assert record["attested"] is False
    assert record["agrees_with_derived"] is False
    assert record["document_chars"] == len(_TURN_TRANSCRIPT)
    assert record["abstract_chars"] == len(_MISCOUNTING_ABSTRACT)
    assert "action" not in record


async def test_seam_record_distinguishes_agreement_from_attestation(ingestion_service, caplog):
    """An agreeing-but-unattested claim records True and False respectively.

    Anti-coincidental-pass: every other seam case asserts
    ``agrees_with_derived`` only where it is False, so a seam that
    hardcodes the field instead of comparing value to derived passes them
    all; only this pairing -- agreement True, attestation False -- forces
    the two fields to come from separate computations.
    """
    ingestion_service._abstraction = _AccurateCountStubProvider()

    with caplog.at_level(logging.INFO, logger=FAITHFULNESS_LOGGER):
        await ingestion_service._generate_abstract_text(
            _GAPPED_TURN_TRANSCRIPT, "chat_transcript", document_id="doc-5"
        )

    [record] = [r for r in _faithfulness_records(caplog) if r["label"] == "fabricated_cardinal"]
    assert record["value"] == 3
    assert record["derived"] == 3
    assert record["agrees_with_derived"] is True
    assert record["attested"] is False
    assert "action" not in record


async def test_seam_leaves_a_miscounting_abstract_unmodified(ingestion_service, caplog):
    """The check records; it does not repair.

    Byte identity against the provider's own output proves the
    non-mutating posture CAS-ADR-020 requires until the check's error rate
    is measured. A prefix assertion would not: a seam that deleted the
    flagged sentence passes ``startswith`` while doing exactly the repair
    this posture forbids.
    """
    ingestion_service._abstraction = _MiscountingStubProvider()

    with caplog.at_level(logging.INFO, logger=FAITHFULNESS_LOGGER):
        result = await ingestion_service._generate_abstract_text(
            _TURN_TRANSCRIPT, "chat_transcript", document_id="doc-4"
        )

    assert result == _MISCOUNTING_ABSTRACT


async def test_seam_stays_silent_for_an_accurate_count(ingestion_service, caplog):
    """An agreeing, attested count emits nothing and survives byte-identical.

    Anti-coincidental-pass: without this, the tests above pass on a seam
    wired to log every cardinal claim unconditionally -- a detector with
    no derivation or attestation check at all.
    """
    ingestion_service._abstraction = _AccurateCountStubProvider()

    with caplog.at_level(logging.INFO, logger=FAITHFULNESS_LOGGER):
        result = await ingestion_service._generate_abstract_text(
            _TURN_TRANSCRIPT, "chat_transcript", document_id="doc-4"
        )

    assert [r for r in _faithfulness_records(caplog) if r["label"] == "fabricated_cardinal"] == []
    assert result == "The transcript unfolds across three turns of discussion."


async def test_seam_records_a_type_restating_opener(ingestion_service, caplog):
    """A type-restating opener emits a record with the adjudication fields.

    The ``"action" not in record`` assertion pins the recording posture: a
    seam promoted to repair grows that field, and this test is the one
    that fails when it does before an adjudicated measurement licenses it.
    """
    text = "A policy document about retention."
    ingestion_service._abstraction = _TypeRestatingStubProvider()

    with caplog.at_level(logging.INFO, logger=FAITHFULNESS_LOGGER):
        await ingestion_service._generate_abstract_text(text, "adr", document_id="doc-6")

    records = [r for r in _faithfulness_records(caplog) if r["label"] == "type_restating_opener"]
    assert len(records) == 1
    record = records[0]
    assert record["layer"] == "abstraction"
    assert record["provider"] == "_TypeRestatingStubProvider"
    assert record["vault_id"] == "test_vault"
    assert record["document_id"] == "doc-6"
    assert record["doc_type"] == "adr"
    assert record["surface"] == "Architecture Decision Record"
    assert record["verb"] == "serves as"
    assert record["form"] == "expansion"
    assert record["opener"] == _TYPE_RESTATING_ABSTRACT
    assert record["document_chars"] == len(text)
    assert record["abstract_chars"] == len(_TYPE_RESTATING_ABSTRACT)
    assert "action" not in record


async def test_seam_leaves_a_type_restating_abstract_unmodified(ingestion_service, caplog):
    """The check records; it does not repair.

    Byte identity against the provider's own output proves the
    non-mutating posture CAS-ADR-020 requires until the check's error rate
    is measured. A prefix assertion would not: a seam that rewrote or
    stripped the opener passes ``endswith`` or a length band while doing
    exactly the repair this posture forbids.
    """
    ingestion_service._abstraction = _TypeRestatingStubProvider()

    with caplog.at_level(logging.INFO, logger=FAITHFULNESS_LOGGER):
        result = await ingestion_service._generate_abstract_text(
            "A policy document about retention.", "adr", document_id="doc-6"
        )

    assert result == _TYPE_RESTATING_ABSTRACT


async def test_seam_stays_silent_for_a_contentful_opener(ingestion_service, caplog):
    """An opener that mentions a type word as content emits nothing.

    Anti-coincidental-pass: without this, the tests above pass on a seam
    wired to log whenever the type word appears anywhere in the opener --
    a detector with no complement gates at all.
    """
    ingestion_service._abstraction = _ContentfulOpenerStubProvider()

    with caplog.at_level(logging.INFO, logger=FAITHFULNESS_LOGGER):
        result = await ingestion_service._generate_abstract_text(
            "A document about vault partitioning.", "ticket", document_id="doc-7"
        )

    assert [r for r in _faithfulness_records(caplog) if r["label"] == "type_restating_opener"] == []
    assert result == "This document describes the partitioning of the ticket store across vaults."


async def test_seam_keys_the_check_to_the_documents_doc_type(ingestion_service, caplog):
    """The same opener is a finding only against the type it restates.

    Anti-coincidental-pass: every other case pairs the stub with the
    matching type, so a seam that hardcodes a type -- or never passes the
    parameter through -- passes them all; only the mismatched pairing
    fails it.
    """
    ingestion_service._abstraction = _TypeRestatingStubProvider()

    with caplog.at_level(logging.INFO, logger=FAITHFULNESS_LOGGER):
        await ingestion_service._generate_abstract_text(
            "A policy document about retention.", "ticket", document_id="doc-6"
        )

    assert [r for r in _faithfulness_records(caplog) if r["label"] == "type_restating_opener"] == []


async def test_type_opener_check_reads_the_gloss_repaired_abstract(ingestion_service, caplog):
    """The record describes the stored abstract, not the model's raw output.

    The clause (e) repair rewrites this opener -- the unattested gloss
    collapses to its bare acronym -- before the recording-only checks
    run. A check placed before the repair reports the expansion form and
    the pre-repair length, describing text that was never stored; the
    ``form == "token"`` assertion is what catches that ordering.
    """
    text = "A policy document about retention."
    collapsed = "This document serves as an ADR governing retention."
    ingestion_service._abstraction = _GlossedOpenerStubProvider()

    with caplog.at_level(logging.INFO, logger=FAITHFULNESS_LOGGER):
        result = await ingestion_service._generate_abstract_text(text, "adr", document_id="doc-8")

    [record] = [r for r in _faithfulness_records(caplog) if r["label"] == "type_restating_opener"]
    assert record["form"] == "token"
    assert record["surface"] == "ADR"
    assert record["abstract_chars"] == len(collapsed)
    assert result == collapsed


async def test_type_opener_records_carry_the_emitting_vaults_id(
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    minimal_vault_config_dict,
    caplog,
):
    """The new finding class names its vault like the three before it.

    Same construction as the multi-vault case above: two services with
    distinct vault ids, both emitting the same document id, so only an id
    read from the emitting instance's own config passes.
    """

    def _service_for(vault_id: str) -> IngestionService:
        config = VaultConfig.model_validate(
            {
                **minimal_vault_config_dict,
                "vault": {**minimal_vault_config_dict["vault"], "id": vault_id},
            }
        )
        return IngestionService(
            graph_store=graph_store,
            lock_manager=lock_manager,
            content_store=stub_content_store,
            embedding_provider=stub_embedding_provider,
            abstraction_provider=_TypeRestatingStubProvider(),
            config=config,
        )

    first = _service_for("test_vault")
    second = _service_for("other_vault")
    text = "A policy document about retention."

    with caplog.at_level(logging.INFO, logger=FAITHFULNESS_LOGGER):
        await first._generate_abstract_text(text, "adr", document_id="doc-6")
        await second._generate_abstract_text(text, "adr", document_id="doc-6")

    opener_records = [
        r for r in _faithfulness_records(caplog) if r["label"] == "type_restating_opener"
    ]
    assert [r["vault_id"] for r in opener_records] == ["test_vault", "other_vault"]


async def test_seam_records_carry_the_emitting_vaults_id(
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    minimal_vault_config_dict,
    caplog,
):
    """Every seam record names the vault whose service emitted it.

    All loaded vaults share the process-global timing and faithfulness
    loggers, so a record is attributable only by what it carries.
    Anti-coincidental-pass: two services with distinct vault ids are
    both constructed before either emits, and both emit the same
    document id. A hardcoded id fails the second service's records; an
    id read from anywhere shared -- module state, a last-constructed
    config -- fails the first service's, since the second construction
    would have overwritten it. Only an id read from the emitting
    instance's own config passes both, which is what keeps records with
    identical document ids distinguishable by vault.
    """

    def _service_for(vault_id: str) -> IngestionService:
        config = VaultConfig.model_validate(
            {
                **minimal_vault_config_dict,
                "vault": {**minimal_vault_config_dict["vault"], "id": vault_id},
            }
        )
        return IngestionService(
            graph_store=graph_store,
            lock_manager=lock_manager,
            content_store=stub_content_store,
            embedding_provider=stub_embedding_provider,
            abstraction_provider=_GlossingStubProvider(),
            config=config,
        )

    first = _service_for("test_vault")
    second = _service_for("other_vault")
    text = "A document about the QZE protocol and its message framing."

    with (
        caplog.at_level(logging.INFO, logger=TIMING_LOGGER),
        caplog.at_level(logging.INFO, logger=FAITHFULNESS_LOGGER),
    ):
        await first._generate_abstract_text(text, "adr", document_id="doc-1")
        await second._generate_abstract_text(text, "adr", document_id="doc-1")

    assert [r["vault_id"] for r in _records(caplog)] == ["test_vault", "other_vault"]
    assert [r["vault_id"] for r in _faithfulness_records(caplog)] == [
        "test_vault",
        "other_vault",
    ]


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
