"""Tests for the ``scripts.reabstract_deferred`` worklist filter and
``--all`` flag wiring (T-0101).

The production change extracts an inline list comprehension into a pure
``_build_worklist`` helper and adds an ``--all`` argparse flag that
toggles the helper's ``include_all_statuses`` parameter. These tests
cover the helper's filter semantics and the argparse plumbing in
isolation -- no async, no service init, no MCP server.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sage.models.enums import PipelineStatus
from scripts.reabstract_deferred import _build_parser, _build_worklist


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
