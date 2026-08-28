"""``pipeline_error`` does not outlive the failure it describes.

``pipeline_error`` is the field a health surface most naturally watches: it
is either null or a failure message. That reading only holds if a document
that recovers stops carrying the message. Before this rule was enforced, the
only writes that cleared the field were Stage-1 re-projections, so any repair
that reached a successful terminal status without re-projecting left a
permanent false failure record behind.

Two things are pinned here:

* **Behaviour** -- reaching ``abstraction_skipped`` clears a
  ``pipeline_error`` recorded earlier. The ``abstraction_complete`` half of
  the rule is pinned end-to-end in ``test_abstraction_queue.py``, where a
  reabstract is the repair path that reaches it.
* **Structure** -- no module under ``sage/`` writes a successful terminal
  ``pipeline_status`` straight to the store; every such transition goes
  through ``_stamp_pipeline_status``. A per-site fix is correct on the day it
  lands and silently incomplete the next time a transition is added, which is
  how the defect arose in the first place. The walk covers the whole package
  rather than the one service that carries the transitions today, so a
  transition added in a sibling module is caught as well.
"""

from __future__ import annotations

import ast
import textwrap
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from sage.config import VaultConfig
from sage.models.enums import (
    SUCCESSFUL_TERMINAL_PIPELINE_STATUSES,
    TERMINAL_PIPELINE_STATUSES,
    PipelineStatus,
    SourceType,
)
from sage.models.schemas import IngestRequest
from sage.services.ingestion import IngestionService
from sage.source_adapters.markdown_adapter import MarkdownAdapter

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
SAGE_ROOT: Final[Path] = REPO_ROOT / "sage"

STALE_ERROR: Final[str] = "abstraction failed after 3 attempts; last error: stale detail"

_SUCCESS_VALUES: Final[frozenset[str]] = frozenset(
    status.value for status in SUCCESSFUL_TERMINAL_PIPELINE_STATUSES
)
_SUCCESS_MEMBERS: Final[frozenset[str]] = frozenset(
    status.name for status in SUCCESSFUL_TERMINAL_PIPELINE_STATUSES
)


# --------------------------------------------------------------------------
# Structural detector
# --------------------------------------------------------------------------


def _is_successful_terminal_literal(node: ast.expr) -> bool:
    """True when ``node`` spells a successful terminal pipeline status.

    Matches both the string literal (``"abstraction_complete"``) and the enum
    spelling (``PipelineStatus.ABSTRACTION_COMPLETE.value``), so neither form
    slips past the walk.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value in _SUCCESS_VALUES
    # PipelineStatus.ABSTRACTION_COMPLETE.value -> strip the trailing .value
    if isinstance(node, ast.Attribute) and node.attr == "value":
        node = node.value
    return isinstance(node, ast.Attribute) and node.attr in _SUCCESS_MEMBERS


def _inline_terminal_success_writes(source: str) -> list[int]:
    """Line numbers of ``update_document`` calls that inline a success status.

    A call qualifies when one of its positional arguments is a dict literal
    binding the key ``"pipeline_status"`` to a successful terminal value.
    Calls that hand the status to ``_stamp_pipeline_status`` instead do not
    build such a dict at the call site and are invisible to the walk -- which
    is the point.
    """
    offenders: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "update_document"):
            continue
        for arg in node.args:
            if not isinstance(arg, ast.Dict):
                continue
            for key, value in zip(arg.keys, arg.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "pipeline_status"
                    and _is_successful_terminal_literal(value)
                ):
                    offenders.append(node.lineno)
    return sorted(set(offenders))


def test_terminal_success_writes_route_through_stamp_helper():
    """No module under ``sage/`` writes a successful-terminal status directly.

    ``_stamp_pipeline_status`` owns the rule that such a transition clears
    ``pipeline_error``. A transition that writes the status itself bypasses
    the rule, whether or not it happens to remember the field today.

    Anti-coincidental-pass: the detector is exercised against synthetic
    source in ``test_detector_flags_an_inline_terminal_write``, so a green
    result here means the tree is clean rather than that the walk is inert.
    """
    offenders = {
        path.relative_to(REPO_ROOT).as_posix(): lines
        for path in sorted(SAGE_ROOT.rglob("*.py"))
        if (lines := _inline_terminal_success_writes(path.read_text()))
    }
    assert offenders == {}, (
        "a successful terminal pipeline_status is written directly to the "
        f"store at {offenders}. Route the transition through "
        "_stamp_pipeline_status so the pipeline_error clear cannot be "
        "omitted."
    )


def test_detector_flags_an_inline_terminal_write():
    """The walk has teeth: both spellings of an inline write are detected.

    Trap: a matcher that silently matches nothing reports an empty offender
    list forever and the gate above passes on any tree at all.
    """
    literal_form = textwrap.dedent(
        """
        async def stage(self, document_id):
            await self._store.update_document(
                document_id,
                {"pipeline_status": "abstraction_complete", "updated_at": "now"},
            )
        """
    )
    enum_form = textwrap.dedent(
        """
        async def stage(self, document_id):
            await self._store.update_document(
                document_id,
                {"pipeline_status": PipelineStatus.ABSTRACTION_SKIPPED.value},
            )
        """
    )
    routed_form = textwrap.dedent(
        """
        async def stage(self, document_id):
            await self._stamp_pipeline_status(
                document_id, PipelineStatus.ABSTRACTION_COMPLETE
            )
        """
    )
    non_terminal_form = textwrap.dedent(
        """
        async def stage(self, document_id):
            await self._store.update_document(
                document_id,
                {"pipeline_status": PipelineStatus.PROJECTION_COMPLETE.value},
            )
        """
    )

    assert _inline_terminal_success_writes(literal_form)
    assert _inline_terminal_success_writes(enum_form)
    assert _inline_terminal_success_writes(routed_form) == []
    assert _inline_terminal_success_writes(non_terminal_form) == []


@pytest.mark.parametrize(
    "status",
    [
        PipelineStatus.INDEXING_IN_PROGRESS,
        PipelineStatus.INDEXING_COMPLETE,
        PipelineStatus.ABSTRACTION_IN_PROGRESS,
    ],
)
async def test_non_terminal_transition_preserves_pipeline_error(
    status,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_vault_config_dict,
    tmp_vault_dir,
):
    """A transition short of success leaves the recorded failure in place.

    The clear is scoped to successful terminal statuses, not applied to every
    write. A helper that cleared unconditionally would satisfy every test that
    asserts a repaired document ends null, while erasing the failure record
    the moment a retry is *dispatched* -- so a reabstract that dies in flight
    would leave the document at ``abstraction_in_progress`` describing no
    failure at all.

    Anti-coincidental-pass: this is the rival the clearing tests cannot
    exclude on their own, because both implementations agree on the end state
    and differ only in between.
    """
    service = _service_with(
        dict(minimal_vault_config_dict),
        MarkdownAdapter(),
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=stub_abstraction_provider,
    )
    _write_source(tmp_vault_dir, f"reports/preserve_{status.value}.md")
    result = await service.ingest(
        IngestRequest(source=f"reports/preserve_{status.value}.md", source_type=SourceType.MARKDOWN)
    )
    document_id = result.document.id
    await graph_store.update_document(document_id, {"pipeline_error": STALE_ERROR})

    await service._stamp_pipeline_status(document_id, status)

    fetched = await graph_store.get_document(document_id)
    assert fetched.pipeline_status == status
    assert fetched.pipeline_error == STALE_ERROR


def test_successful_terminal_statuses_are_the_non_failed_terminals():
    """The success set is the terminal set minus the one failure state.

    Derived rather than restated, so an eighth pipeline status added as a
    terminal success cannot quietly sit outside the clearing rule.
    """
    assert SUCCESSFUL_TERMINAL_PIPELINE_STATUSES == TERMINAL_PIPELINE_STATUSES - {
        PipelineStatus.FAILED
    }


# --------------------------------------------------------------------------
# Behaviour: abstraction_skipped clears a stale error
# --------------------------------------------------------------------------


class _EmptyTextAdapter(MarkdownAdapter):
    """Projects a source to whitespace-only text, driving the BH-134 skip."""

    async def project(self, source_path, config=None):
        projection = await super().project(source_path, config)
        return replace(projection, text="   \n\n  ")


def _write_source(tmp_vault_dir: Path, relative_path: str) -> Path:
    full_path = tmp_vault_dir / "sources" / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text("# Skipped\n\nBody text.")
    return full_path


def _service_with(config_dict, adapter, **overrides) -> IngestionService:
    return IngestionService(
        config=VaultConfig.model_validate(config_dict),
        source_adapters={SourceType.MARKDOWN: adapter},
        **overrides,
    )


@pytest.mark.parametrize("skip_reason", ["abstraction_disabled", "empty_projection_text"])
async def test_abstraction_skipped_clears_stale_pipeline_error(
    skip_reason,
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_vault_config_dict,
):
    """Reaching abstraction_skipped clears an error recorded by an earlier attempt.

    Both skip branches are covered: abstraction disabled in vault config, and
    a projection whose text is empty.

    No production path reaches abstraction_skipped with a pre-existing error
    today -- ingest and recompute_pipeline both clear at Stage 1 -- so the
    stage is driven directly rather than through an unreachable end-to-end
    route. The rule is stated for every successful terminal status, and this
    is where the second one is held to it.

    Anti-coincidental-pass: a document with no recorded error ends at null for
    free. The seeded error is asserted non-null immediately before the stage
    runs, so the clear is proved to do work.
    """
    config_dict = dict(minimal_vault_config_dict)
    if skip_reason == "abstraction_disabled":
        config_dict["abstraction"] = {"enabled": False}
        adapter = MarkdownAdapter()
    else:
        adapter = _EmptyTextAdapter()

    service = _service_with(
        config_dict,
        adapter,
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=stub_abstraction_provider,
    )

    source_path = _write_source(tmp_vault_dir, f"reports/{skip_reason}.md")
    result = await service.ingest(
        IngestRequest(source=f"reports/{skip_reason}.md", source_type=SourceType.MARKDOWN)
    )
    document_id = result.document.id

    # Stand in for the failure this document recovered from: the field is a
    # plain column, and the point is that the next successful terminal
    # transition must not preserve whatever it holds.
    await graph_store.update_document(document_id, {"pipeline_error": STALE_ERROR})
    seeded = await graph_store.get_document(document_id)
    assert seeded.pipeline_error == STALE_ERROR

    projection = await adapter.project(source_path, None)
    await service._execute_pipeline_stages(document_id, projection, seeded.doc_type)

    repaired = await graph_store.get_document(document_id)
    assert repaired.pipeline_status == PipelineStatus.ABSTRACTION_SKIPPED
    assert repaired.pipeline_error is None
