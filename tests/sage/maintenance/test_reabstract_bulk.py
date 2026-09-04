"""The bulk reabstract sweep core (``sage.maintenance.reabstract_bulk``).

A cloud-profile vault has no bulk recovery path for documents stranded at a
terminal ``failed`` pipeline status: the deferred-abstract sweep enumerates
``abstraction_skipped`` only, and startup recovery deliberately leaves terminal
states alone. This core is that path -- a status-selected, dry-run-by-default
sweep that dispatches reabstraction per document and isolates per-document
failures so one bad row cannot abort the run.

The tests drive a recording graph store rather than ``StubGraphStore`` because
the enumeration contract under test is ``query_documents`` (which the stub
raises on) and, specifically, that it opts out of the default failed-pipeline
exclusion. Without that opt-out the sweep silently enumerates nothing, which is
the one failure mode that would read as success.
"""

import pytest

from sage.maintenance import reabstract_bulk as rb
from sage.models.enums import PipelineStatus, ReabstractOutcome

VAULT = "cas_smoke"


class RecordingGraphStore:
    """Minimal graph store recording every ``query_documents`` call."""

    def __init__(self, docs):
        self._docs = {d.id: d for d in docs}
        self.calls: list[dict] = []

    async def query_documents(
        self,
        filters=None,
        limit: int = 100,
        offset: int = 0,
        sort_by=None,
        sort_order=None,
        *,
        default_exclude_failed: bool = True,
    ):
        self.calls.append(
            {
                "filters": dict(filters or {}),
                "limit": limit,
                "offset": offset,
                "default_exclude_failed": default_exclude_failed,
            }
        )
        wanted = (filters or {}).get("pipeline_status")
        matched = [d for d in self._docs.values() if d.pipeline_status == wanted]
        if default_exclude_failed and "pipeline_status" not in (filters or {}):
            matched = [d for d in matched if d.pipeline_status != PipelineStatus.FAILED.value]
        return matched[offset : offset + limit], len(matched)

    async def get_document(self, document_id: str):
        return self._docs.get(document_id)

    def set_status(self, document_id: str, status: PipelineStatus) -> None:
        self._docs[document_id] = self._docs[document_id].model_copy(
            update={"pipeline_status": status}
        )


class RecordingIngestion:
    """Ingestion-service stand-in recording dispatches and settling statuses."""

    def __init__(self, graph, *, settles_to=PipelineStatus.ABSTRACTION_COMPLETE, raises_on=()):
        self._graph = graph
        self._settles_to = settles_to
        self._raises_on = set(raises_on)
        self.dispatched: list[str] = []

    async def reabstract(self, document_id: str) -> dict:
        self.dispatched.append(document_id)
        if document_id in self._raises_on:
            raise RuntimeError("simulated dispatch failure")
        self._graph.set_status(document_id, self._settles_to)
        return {"status": "reabstract_started", "document_id": document_id}


@pytest.fixture
def corpus(make_doc):
    """Three failed documents and two already-complete ones."""
    return [
        make_doc("f1", pipeline_status=PipelineStatus.FAILED),
        make_doc("f2", pipeline_status=PipelineStatus.FAILED),
        make_doc("f3", pipeline_status=PipelineStatus.FAILED),
        make_doc("ok1", pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE),
        make_doc("ok2", pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE),
    ]


# --- A. Status selector parsing -------------------------------------------


def test_selector_defaults_to_the_recovery_statuses():
    """The terminal statuses a document reaches with its abstract missing.

    abstraction_interrupted is among them because this sweep is how an operator
    reaches interrupted work without waiting for the restart that startup
    recovery needs.
    """
    assert rb.parse_status_selector("") == frozenset(
        {
            PipelineStatus.FAILED.value,
            PipelineStatus.ABSTRACTION_SKIPPED.value,
            PipelineStatus.ABSTRACTION_INTERRUPTED.value,
        }
    )
    assert rb.parse_status_selector(None) == rb.parse_status_selector("")


def test_selector_accepts_a_comma_separated_status_list():
    assert rb.parse_status_selector("failed, abstraction_skipped") == frozenset(
        {"failed", "abstraction_skipped"}
    )
    assert rb.parse_status_selector("abstraction_skipped,failed") == rb.parse_status_selector(
        "failed,abstraction_skipped"
    )


def test_selector_all_expands_to_the_full_vocabulary():
    # Asserted against the enum rather than a literal copy, so a status added
    # later is covered by ``all`` instead of being silently dropped.
    assert rb.parse_status_selector("all") == frozenset(s.value for s in PipelineStatus)


def test_unknown_status_token_is_named_in_the_refusal():
    with pytest.raises(ValueError) as exc:
        rb.parse_status_selector("failed,abstracton_skipped")
    message = str(exc.value)
    assert "abstracton_skipped" in message, "the refusal must name the offending token"
    assert "abstraction_skipped" in message, "the refusal must name the accepted vocabulary"
    assert "failed" in message


def test_selector_is_case_and_whitespace_normalized():
    assert rb.parse_status_selector("  FAILED  ") == frozenset({"failed"})


# --- B. Sweep core ---------------------------------------------------------


async def test_dry_run_reports_the_worklist_without_dispatching(corpus, capsys):
    graph = RecordingGraphStore(corpus)
    ingestion = RecordingIngestion(graph)

    rc = await rb.reabstract_bulk(
        graph_store=graph,
        ingestion_service=ingestion,
        vault_id=VAULT,
        statuses=frozenset({PipelineStatus.FAILED.value}),
        limit=None,
        reason="recovery",
        apply=False,
    )

    assert rc == 0
    assert ingestion.dispatched == []
    out = capsys.readouterr().out
    assert "3" in out, "the dry run must report the candidate count"
    assert "dry-run" in out


async def test_apply_dispatches_every_selected_document(corpus, capsys):
    graph = RecordingGraphStore(corpus)
    ingestion = RecordingIngestion(graph)

    rc = await rb.reabstract_bulk(
        graph_store=graph,
        ingestion_service=ingestion,
        vault_id=VAULT,
        statuses=frozenset({PipelineStatus.FAILED.value}),
        limit=None,
        reason="recovery",
        apply=True,
        poll_interval=0.0,
    )

    assert rc == 0
    assert len(ingestion.dispatched) == 3
    assert "dry-run" not in capsys.readouterr().out


async def test_only_selected_statuses_are_enumerated(make_doc):
    docs = [make_doc(s.value, pipeline_status=s) for s in PipelineStatus]
    graph = RecordingGraphStore(docs)
    expected = [d.id for d in docs if d.pipeline_status == PipelineStatus.FAILED.value]

    worklist, total = await rb.collect_worklist(
        graph_store=graph, statuses=frozenset({PipelineStatus.FAILED.value}), limit=None
    )

    assert expected, "the corpus must actually contain a failed document"
    assert [d.id for d in worklist] == expected
    assert total == len(expected)
    # Without this opt-out the storage layer's default failed-pipeline
    # exclusion returns nothing and the sweep reads as a clean empty run.
    assert graph.calls, "enumeration must go through query_documents"
    assert all(c["default_exclude_failed"] is False for c in graph.calls)


async def test_failed_selector_resumes_a_recovered_document_drops_out(make_doc):
    """Re-dispatch semantics, resume half: the worklist is recomputed from live
    pipeline status on every run, so under the ``failed`` selector a document
    recovered to ``abstraction_complete`` by an earlier partial run no longer
    matches. This is the property that makes a re-run of an interrupted sweep —
    operator re-dispatch or platform-level retry alike — a resume rather than a
    redo.
    """
    stranded = make_doc("stranded", pipeline_status=PipelineStatus.FAILED)
    recovered = make_doc("recovered", pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE)
    graph = RecordingGraphStore([stranded, recovered])

    worklist, total = await rb.collect_worklist(
        graph_store=graph, statuses=rb.parse_status_selector("failed"), limit=None
    )

    assert [d.id for d in worklist] == [stranded.id]
    assert total == 1, "a recovered document must leave the worklist entirely"


async def test_all_selector_redoes_a_recovered_document_stays(make_doc):
    """Re-dispatch semantics, redo half: ``all`` selects every pipeline status,
    so a document already recovered stays on the worklist and is swept again.
    An ``all`` implemented as the default statuses plus extras — or one that
    inherited the storage layer's failed-pipeline exclusion — would drop one of
    the two documents here.
    """
    stranded = make_doc("stranded", pipeline_status=PipelineStatus.FAILED)
    recovered = make_doc("recovered", pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE)
    graph = RecordingGraphStore([stranded, recovered])

    worklist, total = await rb.collect_worklist(
        graph_store=graph, statuses=rb.parse_status_selector("all"), limit=None
    )

    assert {d.id for d in worklist} == {stranded.id, recovered.id}
    assert total == 2


async def test_a_per_document_dispatch_failure_does_not_abort_the_sweep(corpus):
    graph = RecordingGraphStore(corpus)
    failing = corpus[1].id
    ingestion = RecordingIngestion(graph, raises_on={failing})

    report = await rb.run_sweep(
        graph_store=graph,
        ingestion_service=ingestion,
        vault_id=VAULT,
        worklist=[d for d in corpus if d.pipeline_status == PipelineStatus.FAILED.value],
        poll_interval=0.0,
    )

    assert len(ingestion.dispatched) == 3, "every document must still be attempted"
    assert report.reabstracted_count == 2
    assert report.failed_count == 1
    entry = next(e for e in report.entries if e.document_id == failing)
    assert entry.outcome == ReabstractOutcome.LLM_FAILURE
    assert "simulated dispatch failure" in (entry.error_message or "")


async def test_a_document_ending_non_complete_is_reported_as_a_failure(corpus):
    graph = RecordingGraphStore(corpus)
    ingestion = RecordingIngestion(graph, settles_to=PipelineStatus.FAILED)
    worklist = [corpus[0]]

    report = await rb.run_sweep(
        graph_store=graph,
        ingestion_service=ingestion,
        vault_id=VAULT,
        worklist=worklist,
        poll_interval=0.0,
    )

    assert report.failed_count == 1
    assert report.reabstracted_count == 0
    assert PipelineStatus.FAILED.value in (report.entries[0].error_message or "")


async def test_limit_caps_the_worklist(make_doc):
    docs = [make_doc(f"d{i}", pipeline_status=PipelineStatus.FAILED) for i in range(10)]
    graph = RecordingGraphStore(docs)

    worklist, total = await rb.collect_worklist(
        graph_store=graph, statuses=frozenset({PipelineStatus.FAILED.value}), limit=4
    )

    assert len(worklist) == 4
    assert total == 10, "the total must report every match, not just the capped slice"


async def test_empty_worklist_succeeds_and_says_so(capsys, make_doc):
    graph = RecordingGraphStore(
        [make_doc("ok", pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE)]
    )
    ingestion = RecordingIngestion(graph)

    rc = await rb.reabstract_bulk(
        graph_store=graph,
        ingestion_service=ingestion,
        vault_id=VAULT,
        statuses=frozenset({PipelineStatus.FAILED.value}),
        limit=None,
        reason="recovery",
        apply=True,
        poll_interval=0.0,
    )

    assert rc == 0
    assert ingestion.dispatched == []
    assert "nothing to do" in capsys.readouterr().out


async def test_aggregate_counts_match_the_per_document_entries(corpus):
    graph = RecordingGraphStore(corpus)
    ingestion = RecordingIngestion(graph, raises_on={corpus[0].id})

    report = await rb.run_sweep(
        graph_store=graph,
        ingestion_service=ingestion,
        vault_id=VAULT,
        worklist=[d for d in corpus if d.pipeline_status == PipelineStatus.FAILED.value],
        poll_interval=0.0,
    )

    total = report.reabstracted_count + report.skipped_pdf_count + report.failed_count
    assert total == len(report.entries)
    assert report.reabstracted_count == sum(
        1 for e in report.entries if e.outcome == ReabstractOutcome.SUCCESS
    )
    assert report.failed_count == sum(
        1 for e in report.entries if e.outcome == ReabstractOutcome.LLM_FAILURE
    )


async def test_a_failing_sweep_exits_non_zero(corpus):
    graph = RecordingGraphStore(corpus)
    ingestion = RecordingIngestion(graph, raises_on={corpus[0].id})

    rc = await rb.reabstract_bulk(
        graph_store=graph,
        ingestion_service=ingestion,
        vault_id=VAULT,
        statuses=frozenset({PipelineStatus.FAILED.value}),
        limit=None,
        reason="recovery",
        apply=True,
        poll_interval=0.0,
    )

    assert rc == 1


async def test_multi_status_worklist_follows_the_vocabulary_declaration_order(make_doc):
    """The worklist order is stable across runs, not set-iteration order.

    Anti-coincidental-pass: an implementation that iterates the selector set
    directly produces the same *membership* and differs only in order, so a
    single-status test cannot see it. Selecting two statuses and asserting the
    declaration order (indexing < failed, per the enum) is what discriminates.
    """
    docs = [
        make_doc("f_one", pipeline_status=PipelineStatus.FAILED),
        make_doc("i_one", pipeline_status=PipelineStatus.INDEXING_COMPLETE),
        make_doc("f_two", pipeline_status=PipelineStatus.FAILED),
    ]
    graph = RecordingGraphStore(docs)
    order = [s.value for s in PipelineStatus]
    assert order.index(PipelineStatus.INDEXING_COMPLETE.value) < order.index(
        PipelineStatus.FAILED.value
    ), "the fixture assumes indexing_complete precedes failed in the vocabulary"

    worklist, total = await rb.collect_worklist(
        graph_store=graph,
        statuses=frozenset({PipelineStatus.FAILED.value, PipelineStatus.INDEXING_COMPLETE.value}),
        limit=None,
    )

    assert total == 3
    statuses_in_order = [d.pipeline_status for d in worklist]
    assert statuses_in_order == [
        PipelineStatus.INDEXING_COMPLETE.value,
        PipelineStatus.FAILED.value,
        PipelineStatus.FAILED.value,
    ], "statuses must be visited in the vocabulary's declaration order"


async def test_the_cap_is_global_across_statuses_not_per_status(make_doc):
    """``limit`` bounds the whole run, not each status independently.

    Anti-coincidental-pass: an implementation that resets the cap per status
    returns the same documents whenever one status is selected, so only a
    multi-status selection separates it. With two statuses of two documents each
    and a cap of 3, a per-status cap yields 4.
    """
    docs = [
        make_doc("f_one", pipeline_status=PipelineStatus.FAILED),
        make_doc("f_two", pipeline_status=PipelineStatus.FAILED),
        make_doc("i_one", pipeline_status=PipelineStatus.INDEXING_COMPLETE),
        make_doc("i_two", pipeline_status=PipelineStatus.INDEXING_COMPLETE),
    ]
    graph = RecordingGraphStore(docs)

    worklist, total = await rb.collect_worklist(
        graph_store=graph,
        statuses=frozenset({PipelineStatus.FAILED.value, PipelineStatus.INDEXING_COMPLETE.value}),
        limit=3,
    )

    assert len(worklist) == 3, "the cap bounds the run, not each status"
    assert total == 4, "the total must count every match across every selected status"


def test_all_mixed_with_other_tokens_still_expands_to_the_vocabulary():
    """``all`` is a superset, so mixing it with a status is not an error.

    Anti-coincidental-pass: an implementation that only honors ``all`` as a
    lone token reports it as an unrecognized status -- naming a token the
    operator read in the input's own documentation.
    """
    assert rb.parse_status_selector("all,failed") == frozenset(s.value for s in PipelineStatus)
