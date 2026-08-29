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
* **Structure** -- no module under ``sage/`` or ``scripts/`` writes a
  successful terminal ``pipeline_status`` straight to the store; every such
  transition goes through ``_stamp_pipeline_status``. A per-site fix is
  correct on the day it lands and silently incomplete the next time a
  transition is added, which is how the defect arose in the first place. The
  walk covers whole trees rather than the one service that carries the
  transitions today, so a transition added in a sibling module -- or in the
  operator tooling that drives the same stages out of band -- is caught as
  well.

  The rule is stated over what the walk can *read*, not over what it happens
  to recognize. A call site that binds ``pipeline_status`` to an opaque
  expression offends just as a spelled-out successful terminal does, because
  the walk cannot rule out that the expression carries one. Restoring a
  snapshotted status is exactly that shape, and a walk that read only
  literals would let it through.
"""

from __future__ import annotations

import ast
import textwrap
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
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
from scripts import reproject_active_documents as reprojection_script

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

# Trees the structural walk covers. ``sage/`` carries the pipeline itself;
# ``scripts/`` carries the operator tooling that drives the same stages out of
# band and writes the same column.
WALK_ROOTS: Final[tuple[Path, ...]] = (REPO_ROOT / "sage", REPO_ROOT / "scripts")

STALE_ERROR: Final[str] = "abstraction failed after 3 attempts; last error: stale detail"

_SUCCESS_VALUES: Final[frozenset[str]] = frozenset(
    status.value for status in SUCCESSFUL_TERMINAL_PIPELINE_STATUSES
)
_ALL_STATUS_VALUES: Final[frozenset[str]] = frozenset(status.value for status in PipelineStatus)
_ALL_STATUS_MEMBERS: Final[frozenset[str]] = frozenset(status.name for status in PipelineStatus)


# --------------------------------------------------------------------------
# Structural detector
# --------------------------------------------------------------------------


def _spelled_status(node: ast.expr) -> str | None:
    """Return the pipeline status ``node`` spells, or None when it is opaque.

    Both spellings a call site can use are read: the string literal
    (``"abstraction_complete"``) and the enum member
    (``PipelineStatus.ABSTRACTION_COMPLETE.value``). Anything else -- a bare
    name, an attribute on some other object, a call -- is opaque, and the walk
    reports that rather than guessing which status it carries.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value if node.value in _ALL_STATUS_VALUES else None
    # PipelineStatus.ABSTRACTION_COMPLETE.value -> strip the trailing .value
    if isinstance(node, ast.Attribute) and node.attr == "value":
        node = node.value
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == PipelineStatus.__name__
        and node.attr in _ALL_STATUS_MEMBERS
    ):
        return PipelineStatus[node.attr].value
    return None


def _unrouted_pipeline_status_writes(source: str) -> list[int]:
    """Line numbers of ``update_document`` calls that set the status unrouted.

    A call qualifies when one of its arguments is a dict literal binding
    ``"pipeline_status"`` to a value the walk cannot read and confirm sits
    outside the successful-terminal set. Two shapes therefore offend: a
    successful terminal spelled outright, and an opaque expression that might
    be one. Handing a snapshotted status straight to the store is the second
    shape; a walk that read only literals could not see it.

    Calls that hand the status to ``_stamp_pipeline_status`` instead do not
    build such a dict at the call site and are invisible to the walk -- which
    is the point.

    The blind spot is the dict assembled on an earlier line and passed by
    name. That shape is invisible here, and necessarily so: it is how the
    seam itself writes, so a walk that saw it could not tell the seam from
    the sites the seam exists to replace. A call site determined to evade
    this gate can therefore do so by binding its dict to a variable first.
    The gate raises the cost of writing the status unrouted; it does not make
    it impossible.
    """
    offenders: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "update_document"):
            continue
        for arg in (*node.args, *(keyword.value for keyword in node.keywords)):
            if not isinstance(arg, ast.Dict):
                continue
            for key, value in zip(arg.keys, arg.values):
                if not (isinstance(key, ast.Constant) and key.value == "pipeline_status"):
                    continue
                spelled = _spelled_status(value)
                if spelled is None or spelled in _SUCCESS_VALUES:
                    offenders.append(node.lineno)
    return sorted(set(offenders))


def test_terminal_success_writes_route_through_stamp_helper():
    """No module under the walked trees sets a terminal status unrouted.

    ``_stamp_pipeline_status`` owns the rule that a successful terminal
    transition clears ``pipeline_error``. A transition that writes the status
    itself bypasses the rule, whether or not it happens to remember the field
    today -- and a transition that writes a status the walk cannot read is
    indistinguishable from one that does.

    Anti-coincidental-pass: the detector is exercised against synthetic
    source in ``test_detector_flags_an_unrouted_status_write``, so a green
    result here means the trees are clean rather than that the walk is inert.
    ``test_gate_walk_covers_the_operator_scripts`` pins that both trees are
    actually reached, so the walk cannot pass by finding no files.
    """
    offenders = {
        path.relative_to(REPO_ROOT).as_posix(): lines
        for root in WALK_ROOTS
        for path in sorted(root.rglob("*.py"))
        if (lines := _unrouted_pipeline_status_writes(path.read_text()))
    }
    assert offenders == {}, (
        "a pipeline_status the walk cannot clear of being a successful "
        f"terminal is written directly to the store at {offenders}. Route the "
        "transition through _stamp_pipeline_status so the pipeline_error "
        "clear cannot be omitted."
    )


def test_gate_walk_covers_the_operator_scripts():
    """Both walked trees resolve and hold Python to walk.

    Trap: a root that does not exist yields no files, so the gate above
    reports an empty offender map and passes over a tree it never read. The
    operator scripts are the half most recently added to the walk and the
    half a refactor is most likely to move.

    What this does not reach: the gate consuming only part of the tuple it
    is handed. Both tests read ``WALK_ROOTS`` rather than the set of files
    the walk actually opened, so a gate narrowed to one root while the
    constant keeps both would leave the pair green.
    """
    assert [root.name for root in WALK_ROOTS] == ["sage", "scripts"]
    for root in WALK_ROOTS:
        assert root.is_dir(), f"walk root {root} is not a directory"
        assert any(root.rglob("*.py")), f"walk root {root} holds no Python modules"


def test_detector_flags_an_unrouted_status_write():
    """The walk has teeth: every unrouted spelling is detected.

    Trap: a matcher that silently matches nothing reports an empty offender
    list forever and the gate above passes on any tree at all. The opaque
    forms carry the second half of that trap -- a matcher that reads only
    literals is blind to exactly the restore this rule was widened to cover.

    ``unknown_status_form`` carries a third: a matcher that returns any
    string it finds, rather than only a string the enum defines, reads a
    typo'd status as a clean non-terminal one. It would pass every other
    case here, and it would wave through a write that no status transition
    can honour.
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
    opaque_name_form = textwrap.dedent(
        """
        async def stage(self, document_id, prior_status):
            await self._store.update_document(
                document_id,
                {"pipeline_status": prior_status, "updated_at": "now"},
            )
        """
    )
    opaque_call_form = textwrap.dedent(
        """
        async def stage(self, document_id):
            await self._store.update_document(
                document_id,
                {"pipeline_status": snapshot()},
            )
        """
    )
    keyword_form = textwrap.dedent(
        """
        async def stage(self, document_id, prior_status):
            await self._store.update_document(
                document_id,
                updates={"pipeline_status": prior_status},
            )
        """
    )
    unknown_status_form = textwrap.dedent(
        """
        async def stage(self, document_id):
            await self._store.update_document(
                document_id,
                {"pipeline_status": "abstracton_complete"},
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

    assert _unrouted_pipeline_status_writes(literal_form)
    assert _unrouted_pipeline_status_writes(enum_form)
    assert _unrouted_pipeline_status_writes(opaque_name_form)
    assert _unrouted_pipeline_status_writes(opaque_call_form)
    assert _unrouted_pipeline_status_writes(keyword_form)
    assert _unrouted_pipeline_status_writes(unknown_status_form)
    assert _unrouted_pipeline_status_writes(routed_form) == []
    assert _unrouted_pipeline_status_writes(non_terminal_form) == []


def test_script_reads_the_canonical_terminal_status_set():
    """The re-projection script does not restate the terminal status set.

    A local copy is correct on the day it is written and drifts the first
    time an eighth status joins the enum as a terminal one -- at which point
    the script silently stops restoring it.
    """
    assert not hasattr(reprojection_script, "_TERMINAL_PIPELINE_STATES"), (
        "the script still carries a local terminal-status set; it should read "
        "sage.models.enums.TERMINAL_PIPELINE_STATUSES instead"
    )
    assert reprojection_script.TERMINAL_PIPELINE_STATUSES is TERMINAL_PIPELINE_STATUSES


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


# --------------------------------------------------------------------------
# Behaviour: the re-projection script's terminal-status restore
# --------------------------------------------------------------------------


def _services_for(config, graph_store, ingestion, content_store) -> SimpleNamespace:
    """The four collaborators the re-projection path actually reads.

    The script's parameter names the full per-vault services tuple, whose
    other members the re-projection path never touches. Standing the whole
    graph up would add setup without adding assertion strength.
    """
    return SimpleNamespace(
        config=config,
        graph_store=graph_store,
        ingestion_service=ingestion,
        content_store=content_store,
    )


async def _reproject(config, graph_store, ingestion, content_store) -> None:
    """Run the script's per-vault re-projection over the markdown fixtures."""
    await reprojection_script.reproject_vault_with_services(
        config.vault.id,
        _services_for(config, graph_store, ingestion, content_store),
        execute=True,
        allow_hash_drift=False,
        source_types=frozenset({SourceType.MARKDOWN.value}),
    )


async def _ingest_fixture(ingestion_service, tmp_vault_dir, name: str) -> str:
    """Ingest one markdown fixture through the real pipeline; return its id."""
    _write_source(tmp_vault_dir, f"reports/{name}.md")
    result = await ingestion_service.ingest(
        IngestRequest(source=f"reports/{name}.md", source_type=SourceType.MARKDOWN)
    )
    return result.document.id


async def test_reprojection_restore_clears_stale_pipeline_error(
    tmp_vault_dir,
    graph_store,
    ingestion_service,
    stub_content_store,
    minimal_config,
):
    """Restoring a successful terminal status clears a stale failure record.

    The restore is the one transition that reached a successful terminal
    status without the seam that owns the clear. Routing it through the seam
    makes the script behave as the service does: a document that re-projects
    cleanly stops reporting a failure it has recovered from.

    Anti-coincidental-pass: a document with no recorded error ends at null for
    free, so the seeded error is asserted non-null immediately before the
    re-projection. The clear cannot come from Stage 2 either -- that stage
    stamps ``indexing_complete``, which is not a successful terminal status
    and triggers no clear. The restored status is asserted alongside the
    cleared field, so a restore that never fired (leaving the document at
    ``indexing_complete``) fails here rather than passing quietly.
    """
    document_id = await _ingest_fixture(ingestion_service, tmp_vault_dir, "restore_clears")
    ingested = await graph_store.get_document(document_id)
    prior_status = ingested.pipeline_status
    assert prior_status in SUCCESSFUL_TERMINAL_PIPELINE_STATUSES

    await graph_store.update_document(document_id, {"pipeline_error": STALE_ERROR})
    seeded = await graph_store.get_document(document_id)
    assert seeded.pipeline_error == STALE_ERROR

    await _reproject(minimal_config, graph_store, ingestion_service, stub_content_store)

    repaired = await graph_store.get_document(document_id)
    assert repaired.pipeline_status == prior_status
    assert repaired.pipeline_error is None


async def test_reprojection_restore_preserves_a_failed_status_error(
    tmp_vault_dir,
    graph_store,
    ingestion_service,
    stub_content_store,
    minimal_config,
):
    """Restoring ``failed`` keeps the failure it describes.

    Anti-coincidental-pass: a helper that cleared on every write would satisfy
    every assertion that a repaired document ends at null while erasing the
    record of a document that has not recovered at all. This is the case that
    separates the two.
    """
    document_id = await _ingest_fixture(ingestion_service, tmp_vault_dir, "restore_preserves")
    await graph_store.update_document(
        document_id,
        {"pipeline_status": PipelineStatus.FAILED.value, "pipeline_error": STALE_ERROR},
    )

    await _reproject(minimal_config, graph_store, ingestion_service, stub_content_store)

    restored = await graph_store.get_document(document_id)
    assert restored.pipeline_status == PipelineStatus.FAILED
    assert restored.pipeline_error == STALE_ERROR


async def test_reprojection_leaves_a_non_terminal_status_alone(
    tmp_vault_dir,
    graph_store,
    ingestion_service,
    stub_content_store,
    minimal_config,
):
    """A document short of a terminal status keeps Stage 2's own stamp.

    The restore is gated on terminal-set membership, and the set is now read
    from the enum module rather than restated locally. A restore that fired
    unconditionally would leave the two tests above green while reinstating a
    non-terminal status over the one Stage 2 just wrote.
    """
    document_id = await _ingest_fixture(ingestion_service, tmp_vault_dir, "restore_skipped")
    await graph_store.update_document(
        document_id, {"pipeline_status": PipelineStatus.PROJECTION_COMPLETE.value}
    )

    await _reproject(minimal_config, graph_store, ingestion_service, stub_content_store)

    reprojected = await graph_store.get_document(document_id)
    assert reprojected.pipeline_status == PipelineStatus.INDEXING_COMPLETE
