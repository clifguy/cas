"""Tests for the ``scripts.reabstract_deferred`` worklist filter and
``--all`` flag wiring.

The production change extracts an inline list comprehension into a pure
``_build_worklist`` helper and adds an ``--all`` argparse flag that
toggles the helper's ``include_all_statuses`` parameter. These tests
cover the helper's filter semantics and the argparse plumbing in
isolation -- no async, no service init, no MCP server.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from sage.models.enums import PipelineStatus
from scripts.reabstract_deferred import (
    WAIT_MISSING,
    WAIT_TIMEOUT,
    _build_parser,
    _build_worklist,
    _load_ids_file,
    _wait_for_terminal,
)


def _doc(doc_id: str, *, status: str, source_type: str = "markdown") -> SimpleNamespace:
    """Minimal stand-in for ``Document`` carrying only the two fields
    ``_build_worklist`` reads. Duck-typed; mirrors the runtime contract
    where ``Document.pipeline_status`` and ``Document.source_type`` are
    string-valued (StrEnum)."""
    return SimpleNamespace(id=doc_id, pipeline_status=status, source_type=source_type)


@pytest.fixture
def mixed_docs() -> list[SimpleNamespace]:
    """Five-doc fixture covering the status and source_type combinations
    each test asserts against. Each row is named for the assertion it
    surfaces."""
    return [
        _doc("d_complete", status=PipelineStatus.ABSTRACTION_COMPLETE.value),
        _doc("d_skipped", status=PipelineStatus.ABSTRACTION_SKIPPED.value),
        _doc("d_failed", status=PipelineStatus.FAILED.value),
        _doc("d_indexing", status=PipelineStatus.INDEXING_COMPLETE.value),
        _doc("d_skipped_pdf", status=PipelineStatus.ABSTRACTION_SKIPPED.value, source_type="pdf"),
    ]


# ---------------------------------------------------------------------------
# T1: default (no --all) filters to abstraction_skipped, excludes PDFs.
# ---------------------------------------------------------------------------


def test_build_worklist_default_filters_to_abstraction_skipped(mixed_docs):
    worklist = _build_worklist(
        mixed_docs,
        include_all_statuses=False,
        include_pdf=False,
    )
    ids = [d.id for d in worklist]
    assert ids == ["d_skipped"]


# ---------------------------------------------------------------------------
# T2: --all enumerates every non-PDF document regardless of status.
# ---------------------------------------------------------------------------


def test_build_worklist_all_includes_every_non_pdf_status(mixed_docs):
    worklist = _build_worklist(
        mixed_docs,
        include_all_statuses=True,
        include_pdf=False,
    )
    ids = sorted(d.id for d in worklist)
    assert ids == ["d_complete", "d_failed", "d_indexing", "d_skipped"]


# ---------------------------------------------------------------------------
# T3: --all with --include-pdf returns every document.
# ---------------------------------------------------------------------------


def test_build_worklist_all_with_include_pdf_returns_everything(mixed_docs):
    worklist = _build_worklist(
        mixed_docs,
        include_all_statuses=True,
        include_pdf=True,
    )
    ids = sorted(d.id for d in worklist)
    assert ids == ["d_complete", "d_failed", "d_indexing", "d_skipped", "d_skipped_pdf"]


# ---------------------------------------------------------------------------
# T4: default with --include-pdf still excludes non-skipped statuses.
# ---------------------------------------------------------------------------


def test_build_worklist_default_with_include_pdf_includes_skipped_pdf(mixed_docs):
    worklist = _build_worklist(
        mixed_docs,
        include_all_statuses=False,
        include_pdf=True,
    )
    ids = sorted(d.id for d in worklist)
    assert ids == ["d_skipped", "d_skipped_pdf"]


# ---------------------------------------------------------------------------
# T5: --all defaults to False so existing invocations are unchanged.
# ---------------------------------------------------------------------------


def test_argparse_all_flag_default_false():
    args = _build_parser().parse_args(["cas"])
    assert args.all is False
    assert args.include_pdf is False
    assert args.vault_id == "cas"


# ---------------------------------------------------------------------------
# T6: --all parses as a store_true flag without disturbing other flags.
# ---------------------------------------------------------------------------


def test_argparse_all_flag_parses():
    args = _build_parser().parse_args(["cas", "--all"])
    assert args.all is True
    assert args.include_pdf is False


# ---------------------------------------------------------------------------
# Explicit-id mode. The predicate modes answer "whichever documents currently
# look like this"; this one answers "these documents", which is what a pass
# reproducing an earlier survey needs -- the set is fixed when the survey ran,
# not re-derived from live state at each invocation.
# ---------------------------------------------------------------------------


def test_build_worklist_named_ids_returns_them_in_request_order(mixed_docs):
    worklist = _build_worklist(
        mixed_docs,
        include_all_statuses=False,
        include_pdf=False,
        document_ids=["d_complete", "d_skipped"],
    )
    assert [d.id for d in worklist] == ["d_complete", "d_skipped"]


def test_build_worklist_named_ids_honor_a_pdf_without_the_flag(mixed_docs):
    """A named id is honored whatever the predicate filters would say.

    Naming a document is a stronger statement than any predicate the other
    modes apply, so neither the status filter nor the PDF skip may quietly
    drop it. A pass that silently omitted a document it was handed would
    report success for one it never touched.
    """
    worklist = _build_worklist(
        mixed_docs,
        include_all_statuses=False,
        include_pdf=False,
        document_ids=["d_skipped_pdf"],
    )
    assert [d.id for d in worklist] == ["d_skipped_pdf"]


def test_build_worklist_named_ids_raise_on_an_unknown_id(mixed_docs):
    """An id absent from the vault fails the run rather than shortening it."""
    with pytest.raises(KeyError, match="no_such_doc"):
        _build_worklist(
            mixed_docs,
            include_all_statuses=False,
            include_pdf=False,
            document_ids=["d_skipped", "no_such_doc"],
        )


def test_build_worklist_named_ids_reject_a_duplicate(mixed_docs):
    """A repeated id is a defect in the caller's list, not a request to
    abstract the same document twice."""
    with pytest.raises(ValueError, match="d_skipped"):
        _build_worklist(
            mixed_docs,
            include_all_statuses=False,
            include_pdf=False,
            document_ids=["d_skipped", "d_skipped"],
        )


# ---------------------------------------------------------------------------
# Ids-file loading.
# ---------------------------------------------------------------------------


def test_load_ids_file_keeps_order_and_drops_comments_and_blanks(tmp_path):
    """The file a survey writes carries a provenance header; it is context
    for the reader, not part of the worklist."""
    path = tmp_path / "ids.txt"
    path.write_text("# vault: example_vault\n# window: 32768\n\ndoc_b\n  doc_a  \n\ndoc_c\n")
    assert _load_ids_file(path) == ["doc_b", "doc_a", "doc_c"]


def test_load_ids_file_rejects_a_file_with_no_ids(tmp_path):
    """An empty list must not read downstream as 'no filter'.

    The degenerate case is the dangerous one: an empty worklist is
    indistinguishable from 'nothing to do', while an empty *filter* would
    sweep the whole vault. Failing here is the only thing standing between a
    truncated manifest and an unintended full-vault pass.
    """
    path = tmp_path / "ids.txt"
    path.write_text("# header only\n\n")
    with pytest.raises(ValueError):
        _load_ids_file(path)


def test_parser_rejects_ids_file_together_with_all():
    """The two modes contradict each other; argparse settles it."""
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["example_vault", "--ids-file", "ids.txt", "--all"])


# ---------------------------------------------------------------------------
# The post-dispatch wait: settles on every terminal status and gives up on a
# document that stops advancing toward one.
# ---------------------------------------------------------------------------


class _FakeGraphStore:
    """Graph store stand-in returning a scripted status per get_document call.

    The final entry repeats, so a poll that never settles keeps reading the
    same non-terminal status rather than exhausting the script.
    """

    def __init__(self, statuses: list[str | None]) -> None:
        self._statuses = statuses
        self.calls = 0

    async def get_document(self, document_id: str):
        index = min(self.calls, len(self._statuses) - 1)
        self.calls += 1
        status = self._statuses[index]
        return None if status is None else SimpleNamespace(id=document_id, pipeline_status=status)


async def test_script_wait_settles_on_abstraction_skipped():
    """abstraction_skipped is terminal for this wait.

    The sweep's own default worklist is built from abstraction_skipped, so a
    document that settles back where it started is the likeliest non-success
    outcome here -- and the restated terminal set this replaced omitted it,
    which left the poll running forever against a document that had finished.

    Anti-coincidental-pass: the store's scripted second reading is the
    terminal one, so a wait that ignored the status entirely would hit the
    ceiling and return the timeout sentinel instead. The ceiling is passed
    explicitly and small: on the default two-hour one this test would spin
    against that rival rather than fail, and a gate that hangs where it
    should redden reports nothing.
    """
    store = _FakeGraphStore(
        [
            PipelineStatus.ABSTRACTION_IN_PROGRESS.value,
            PipelineStatus.ABSTRACTION_SKIPPED.value,
        ]
    )

    status = await asyncio.wait_for(
        _wait_for_terminal(store, "d1", 0.0, timeout_seconds=1.0), timeout=10
    )

    assert status == PipelineStatus.ABSTRACTION_SKIPPED.value
    assert store.calls >= 2


async def test_script_wait_gives_up_on_a_document_that_never_settles():
    """A document stuck non-terminal is abandoned at the ceiling.

    Without one the sweep stalls on a single document with no further output
    and no way for the operator to tell a slow generation from a dead one --
    the abstraction worker is cancelled on teardown, which drops queued jobs
    and leaves their documents stranded mid-pipeline with nothing left to
    advance them.

    Anti-coincidental-pass: the fixture never yields a terminal status, so a
    wait that returned early on any reading would fail the sentinel assertion
    rather than pass by luck.
    """
    store = _FakeGraphStore([PipelineStatus.ABSTRACTION_IN_PROGRESS.value])

    status = await asyncio.wait_for(
        _wait_for_terminal(store, "d1", 0.0, timeout_seconds=0.05), timeout=10
    )

    assert status == WAIT_TIMEOUT


async def test_script_wait_prefers_a_settled_status_over_an_elapsed_deadline():
    """With the ceiling already elapsed, a settled document still reports its
    status; only a non-terminal one reports the sentinel.

    The pair is the gate: the settled arm alone passes against any deadline
    that is never reached, and the timeout arm alone passes against a
    deadline-first ordering. Together they pin both.
    """
    settled = _FakeGraphStore([PipelineStatus.ABSTRACTION_COMPLETE.value])
    stranded = _FakeGraphStore([PipelineStatus.ABSTRACTION_IN_PROGRESS.value])

    assert (
        await _wait_for_terminal(settled, "d1", 0.0, timeout_seconds=0.0)
        == PipelineStatus.ABSTRACTION_COMPLETE.value
    )
    assert await _wait_for_terminal(stranded, "d1", 0.0, timeout_seconds=0.0) == WAIT_TIMEOUT


async def test_script_wait_reports_a_vanished_document():
    """A document deleted mid-flight returns its own sentinel, not a status."""
    store = _FakeGraphStore([None])

    assert (
        await asyncio.wait_for(
            _wait_for_terminal(store, "d1", 0.0, timeout_seconds=1.0), timeout=10
        )
        == WAIT_MISSING
    )
