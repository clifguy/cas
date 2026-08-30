"""Unit tests for the type-restating-opener detector.

CAS-ADR-020 clause (f) instructs the abstraction prompt against restating
metadata the discovering agent already sees, and the type-restating-opener
check enforces one surface shape of that constraint: an abstract whose
opening sentence classifies the document as an instance of its own
doc_type. The check itself reports findings and mutates nothing; the
repair its measurement licensed is a separate function, exercised in
``test_abstraction_type_opener_repair.py``.

The check is a proxy and is documented as one. It anchors on the
predicate-complement frame the measured breaches took ("This document
serves as an architecture decision record...") -- subject-position type
naming ("The guideline...") is the prompt's own sanctioned style and
passes, as does a restatement phrased through an unregistered verb or
synonym.

The detector needs no source text: the breach is relative to the
document's doc_type metadata, which the caller supplies. That makes these
tests pure text arithmetic with no inference runtime.
"""

from types import SimpleNamespace

from sage.adapters.abstraction_utils import TypeRestatingOpener, find_type_restating_opener
from sage.adapters.interfaces import SYNTHETIC_HEADER_HEADING_PATH
from scripts.audit_abstraction_glosses import AuditEntry, build_entries
from scripts.audit_abstraction_type_openers import (
    TypeOpenerAuditFinding,
    audit_type_opener_entries,
    render_manifest,
    summarize_by_doc_type,
)
from scripts.reabstract_deferred import _load_ids_file

_OBSERVED_ADR_OPENER = (
    "This document serves as an accepted Architecture Decision Record "
    "(ADR-029) that revises the retention policy."
)


class TestFindTypeRestatingOpener:
    def test_serves_as_expansion_opener_is_flagged(self):
        """The observed breach shape: spelled-out expansion after 'serves as'.

        A raw-token-only matcher passes every other positive below and
        fails here, because the expansion begins before the parenthetical
        token and carries no doc_type token of its own.
        """
        [finding] = find_type_restating_opener(_OBSERVED_ADR_OPENER, "adr")

        assert finding.form == "expansion"
        assert finding.verb == "serves as"
        assert finding.surface == "Architecture Decision Record"
        assert finding.doc_type == "adr"

    def test_raw_token_opener_is_flagged(self):
        """The doc_type token itself in the complement.

        A registry-only matcher knows the conventional expansion but not
        the bare token and stays wrongly silent here.
        """
        [finding] = find_type_restating_opener(
            "This document is an ADR that revises retention.", "adr"
        )

        assert finding.form == "token"
        assert finding.verb == "is"
        assert finding.surface == "ADR"

    def test_id_suffixed_token_matches(self):
        """A numbered instance name still restates the type.

        An exact-equality token comparison misses the '-029' suffix and
        stays wrongly silent.
        """
        [finding] = find_type_restating_opener(
            "This document is ADR-029, which revises retention.", "adr"
        )

        assert finding.surface == "ADR-029"

    def test_underscore_doc_type_matches_word_sequence(self):
        """A multi-word doc_type matches as its space-joined words.

        Matching the raw underscored string finds no 'steering_document'
        in prose and stays wrongly silent.
        """
        [finding] = find_type_restating_opener(
            "This document is a steering document governing ticket conventions.",
            "steering_document",
        )

        assert finding.form == "token"
        assert finding.surface == "steering document"

    def test_describes_type_noun_is_flagged(self):
        """'describes a <type>' classifies just as 'is a <type>' does.

        A verb registry without 'describes' -- the verb one measured
        breach used -- stays wrongly silent here.
        """
        [finding] = find_type_restating_opener(
            "This document describes a ticket requesting a new opener check.", "ticket"
        )

        assert finding.verb == "describes"
        assert finding.surface == "ticket"

    def test_contentful_complement_stays_silent(self):
        """The load-bearing negative.

        The opener holds the subject, a registered verb, and the type
        word within the complement window -- a detector keyed on mere
        type-word presence passes every positive above and only this
        test fails it. The connective 'of' marks the complement as
        contentful: the document is about the ticket store, not claimed
        to be a ticket.
        """
        abstract = "This document describes the partitioning of the ticket store across vaults."

        assert find_type_restating_opener(abstract, "ticket") == []

    def test_non_deictic_subject_stays_silent(self):
        """The classifying frame requires a generic-artifact subject.

        Here a registered verb and an in-window type word are both
        present, so neither of those gates silences the opener -- only
        the subject gate does. A detector without it fires on any
        sentence whose predicate happens to hold the type word; every
        other negative in this suite is silenced by some other gate, so
        this is the one that exercises the subject anchor alone.
        """
        abstract = "The retention policy is a ticket obligation for every maintainer."

        assert find_type_restating_opener(abstract, "ticket") == []

    def test_type_words_in_subject_stay_silent(self):
        """A title mention in subject position is not a classification.

        A whole-sentence phrase scan without the subject and verb anchors
        fires on the type words in the subject noun phrase.
        """
        abstract = "The ticket conventions steering document prescribes required fields."

        assert find_type_restating_opener(abstract, "steering_document") == []
        assert find_type_restating_opener(abstract, "ticket") == []

    def test_type_word_beyond_window_stays_silent(self):
        """A type word deep in the predicate is content, not classification.

        No connective intervenes here, so only the complement window
        bound silences it; an unbounded complement scan fires.
        """
        abstract = (
            "This document describes one especially thorny, repeatedly "
            "reopened, never quite resolved ticket."
        )

        assert find_type_restating_opener(abstract, "ticket") == []

    def test_type_word_in_later_sentence_stays_silent(self):
        """Only the opener is in scope.

        The second sentence would fire were it the opener; a
        whole-abstract scan reports it.
        """
        abstract = (
            "This document explains the retention policy revision. "
            "This document is an ADR in the maintenance series."
        )

        assert find_type_restating_opener(abstract, "adr") == []

    def test_hyphenated_compound_does_not_match(self):
        """A type word bound into a compound names something else.

        Reusing the attestation normalizer's hyphen-to-space split makes
        'ticket-store' match the type 'ticket'.
        """
        abstract = "This document is a ticket-store migration plan."

        assert find_type_restating_opener(abstract, "ticket") == []

    def test_none_doc_type_is_silent(self):
        """No doc_type, no restatement to detect."""
        assert find_type_restating_opener(_OBSERVED_ADR_OPENER, None) == []

    def test_matching_is_case_insensitive(self):
        """Casing and typographic quotes do not hide a restatement.

        A case-sensitive comparison misses the shouted form; an
        ASCII-only edge strip leaves curly quotes on the token and the
        quoted form unmatched.
        """
        assert find_type_restating_opener("THIS DOCUMENT IS A TICKET ABOUT RETENTION.", "ticket")
        assert find_type_restating_opener(
            "This document is a “ticket” for the retention work.", "ticket"
        )

    def test_at_most_one_finding_per_opener(self):
        """One opener is one breach, whatever matches inside it.

        The observed shape carries both the spelled-out expansion and the
        suffixed token; per-surface-form reporting would double-weight
        the breach in any rate measured over these records.
        """
        findings = find_type_restating_opener(_OBSERVED_ADR_OPENER, "adr")

        assert len(findings) == 1

    def test_empty_abstract_is_silent(self):
        assert find_type_restating_opener("", "adr") == []
        assert find_type_restating_opener("   \n  ", "adr") == []

    def test_finding_is_hashable_and_carries_adjudication_fields(self):
        """The record names what tripped it, so a breach is adjudicable."""
        [finding] = find_type_restating_opener(_OBSERVED_ADR_OPENER, "adr")

        assert isinstance(finding, TypeRestatingOpener)
        assert finding.opener == _OBSERVED_ADR_OPENER
        assert finding.doc_type == "adr"
        assert {finding}


class TestAuditTypeOpenerEntries:
    """The corpus audit's pure core, exercised against a synthetic catalog.

    Storage access lives in the script's ``run``; the core reuses the same
    catalog shape as the gloss and cardinal audits, so the three
    instruments read one reconstruction of each document's source.
    """

    def test_only_documents_with_findings_report(self):
        """Flagged, clean, and type-less entries are told apart.

        A core that reports every audited document fails on the clean
        entry; one that assumes every entry carries a doc_type crashes on
        the type-less one.
        """
        flagged = AuditEntry(
            doc_id="doc-flagged",
            lifecycle_status="active",
            abstract=_OBSERVED_ADR_OPENER,
            source_text="Body text.",
            doc_type="adr",
        )
        clean = AuditEntry(
            doc_id="doc-clean",
            lifecycle_status="active",
            abstract="This document revises the retention policy for archived chunks.",
            source_text="Body text.",
            doc_type="adr",
        )
        typeless = AuditEntry(
            doc_id="doc-typeless",
            lifecycle_status="completed",
            abstract=_OBSERVED_ADR_OPENER,
            source_text="Body text.",
            doc_type=None,
        )

        findings = audit_type_opener_entries([flagged, clean, typeless])

        assert [f.doc_id for f in findings] == ["doc-flagged"]
        assert findings[0].lifecycle_status == "active"
        assert findings[0].doc_type == "adr"
        [opener] = findings[0].findings
        assert opener.form == "expansion"

    def test_legacy_entries_without_doc_type_stay_silent(self):
        """An entry built without the field audits cleanly.

        This is the construction shape every pre-existing gloss and
        cardinal audit test uses; making ``doc_type`` a required field
        would break the shared catalog they all read.
        """
        legacy = AuditEntry(
            doc_id="doc-legacy",
            lifecycle_status="active",
            abstract=_OBSERVED_ADR_OPENER,
            source_text="Body text.",
        )

        assert audit_type_opener_entries([legacy]) == []

    async def test_build_entries_populates_doc_type(self):
        """The shared catalog carries each document's doc_type.

        A ``build_entries`` that never populates the field leaves the
        real audit permanently silent while the synthetic-catalog tests
        above pass.
        """
        doc = SimpleNamespace(
            id="doc-1",
            semantic_abstract=_OBSERVED_ADR_OPENER,
            lifecycle_status="active",
            doc_type="adr",
        )
        chunks = [
            SimpleNamespace(heading_path=SYNTHETIC_HEADER_HEADING_PATH, content="Header."),
            SimpleNamespace(heading_path=["Body"], content="Body text."),
        ]

        class _GraphStore:
            async def list_all_documents(self):
                return [doc]

        class _ContentStore:
            async def get_all_chunks(self, doc_id):
                return chunks

        services = SimpleNamespace(graph_store=_GraphStore(), content_store=_ContentStore())

        [entry] = await build_entries(services)

        assert entry.doc_type == "adr"
        assert entry.source_text == "Body text."


class TestRenderManifest:
    """The audit's adjudication manifest.

    The manifest is read twice: once by a reviewer adjudicating every
    finding, and once by machinery that replays the flagged set. Both
    readings constrain the rendering -- the first wants each finding's
    evidence beside its id, the second wants nothing but ids to survive
    ``scripts.reabstract_deferred._load_ids_file``.
    """

    def _finding(self, doc_id, *, doc_type="adr", opener=_OBSERVED_ADR_OPENER, lifecycle="active"):
        [detected] = find_type_restating_opener(opener, doc_type)
        return TypeOpenerAuditFinding(
            doc_id=doc_id,
            lifecycle_status=lifecycle,
            doc_type=doc_type,
            findings=(detected,),
        )

    def test_carries_a_provenance_header(self):
        """The header names what selected the ids.

        A bare list of ids read a week later says nothing about which
        vault it describes or how many documents it was drawn from, and
        the same ids under a different detector are a different set.
        """
        text = render_manifest(
            [self._finding("doc-a")],
            vault_id="example_vault",
            total_audited=42,
            measured_at="2026-08-29T12:00:00Z",
        )

        assert "# vault: example_vault" in text
        assert "# documents_audited: 42" in text
        assert "# documents_with_type_restating_opener: 1" in text
        assert "# measured_at: 2026-08-29T12:00:00Z" in text

    def test_is_consumable_as_an_ids_file(self, tmp_path):
        """Every non-id line is a comment the id loader skips.

        Asserting the ids appear in the text would pass on a rendering
        whose evidence lines are also read as ids; round-tripping through
        the real loader is what catches that.
        """
        manifest = tmp_path / "openers.txt"
        manifest.write_text(
            render_manifest(
                [self._finding("doc-a"), self._finding("doc-b")],
                vault_id="example_vault",
                total_audited=2,
                measured_at="2026-08-29T12:00:00Z",
            )
        )

        assert _load_ids_file(manifest) == ["doc-a", "doc-b"]

    def test_folds_a_multiline_opener_onto_one_comment_line(self, tmp_path):
        """An opener carrying a newline stays inside its comment.

        Abstracts are stored prose and may wrap mid-sentence. Interpolated
        raw, the tail of such an opener becomes a bare line -- which the id
        loader reads as a document id, handing a replay pass a worklist
        entry that is a fragment of English.
        """
        wrapped = (
            "This document serves as an accepted Architecture Decision\nRecord "
            "(ADR-029) that revises the retention policy."
        )
        manifest = tmp_path / "openers.txt"
        manifest.write_text(
            render_manifest(
                [self._finding("doc-a", opener=wrapped)],
                vault_id="example_vault",
                total_audited=1,
                measured_at="2026-08-29T12:00:00Z",
            )
        )

        assert _load_ids_file(manifest) == ["doc-a"]
        assert "Record (ADR-029) that revises" in manifest.read_text()

    def test_ordering_is_independent_of_catalog_order(self):
        """Two runs over the same findings render byte-identical text.

        A manifest that reordered itself with the vault's enumeration
        order could not be diffed against an earlier one, which is how
        the recalibration check confirms a narrowing introduced nothing.
        """
        a = self._finding("doc-a", doc_type="adr")
        b = self._finding("doc-b", doc_type="adr")
        kwargs = {
            "vault_id": "example_vault",
            "total_audited": 2,
            "measured_at": "2026-08-29T12:00:00Z",
        }

        assert render_manifest([a, b], **kwargs) == render_manifest([b, a], **kwargs)

    def test_groups_ids_by_doc_type(self):
        """Findings of one type are adjacent, so adjudication runs by type.

        The measured breach is concentrated by doc_type rather than spread
        evenly, so a reviewer adjudicates a type at a time.
        """
        ticket = self._finding("doc-z", doc_type="ticket", opener="This document is a ticket.")
        adr_a = self._finding("doc-a")
        adr_b = self._finding("doc-b")

        text = render_manifest(
            [adr_b, ticket, adr_a],
            vault_id="example_vault",
            total_audited=3,
            measured_at="2026-08-29T12:00:00Z",
        )
        ids = [line for line in text.splitlines() if line and not line.startswith("#")]

        assert ids == ["doc-a", "doc-b", "doc-z"]


class TestSummarizeByDocType:
    """The report's per-type breakdown."""

    def test_counts_findings_per_doc_type_ordered_by_count(self):
        """The heaviest type reads first, ties broken by name.

        The fixture is ordered so that first appearance runs opposite to
        count -- the singleton type is seen first and the heaviest last.
        A summary that preserved the catalog's enumeration would render
        this fixture exactly backwards, which is the point: enumeration
        order buries the concentration the measurement exists to show.
        """
        findings = [
            TypeOpenerAuditFinding("d1", "active", "work_plan", ()),
            TypeOpenerAuditFinding("d2", "active", "adr", ()),
            TypeOpenerAuditFinding("d3", "active", "ticket", ()),
            TypeOpenerAuditFinding("d4", "active", "adr", ()),
            TypeOpenerAuditFinding("d5", "active", "ticket", ()),
            TypeOpenerAuditFinding("d6", "active", "ticket", ()),
        ]

        assert summarize_by_doc_type(findings) == [("ticket", 3), ("adr", 2), ("work_plan", 1)]

    def test_empty_findings_summarize_to_nothing(self):
        assert summarize_by_doc_type([]) == []
