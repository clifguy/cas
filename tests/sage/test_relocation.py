"""Cross-vault document relocation (CAS-ADR-050).

A document moves between vaults by ordinary re-ingestion into the
destination plus a terminal ``relocated`` state in the origin, with a
symmetric pair of inert metadata pointers linking the two. No new
operation, and no engine behaviour that follows a pointer.

The tests are grouped by what they hold: the pointer model and its
storage round trip, the lifecycle action and the state's refusals,
the ingest half, the non-propagation rule, the read surfaces, and
finally the whole round trip across two vaults on separate schemas.

What is deliberately absent, because CAS-ADR-050 accepts each as a
consequence rather than a defect: no detector for an interrupted
relocation, no repair for ``depends_on`` edges stranded in the origin,
and no cross-vault traversal. ``test_relocated_target_stays_unsatisfied``
and the tail of the round-trip test assert the third of those positively
-- a dependency does not resolve across the boundary -- so a future
change that quietly added cross-vault resolution fails here.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.api.errors import (
    InvalidLifecycleTransitionError,
    MissingFieldError,
    SupersedeTargetNotActiveError,
    UnexpectedFieldError,
)
from sage.config import VaultConfig
from sage.models.enums import EdgeType, PipelineStatus, SourceType
from sage.models.schemas import (
    BulkLifecycleItem,
    BulkLifecycleRequest,
    Document,
    Edge,
    IngestRequest,
    RelocationPointer,
    SetLifecycleRequest,
)
from sage.services.graph_ops import GraphOpsService
from sage.services.ingestion import IngestionService
from sage.services.lifecycle import LifecycleService
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from sage.storage.locks import DocumentLockManager

# ---------------------------------------------------------------------------
# Pointer values, distinct in every member
# ---------------------------------------------------------------------------
#
# Every fixture below builds pointers whose members differ from each
# other's in all five positions. That is deliberate and load-bearing: an
# implementation that serves one column for both directions, or that
# transposes two members, produces objects that are still well-formed and
# would satisfy any assertion phrased as "a pointer is present". Only
# distinct values make the wrong one visible.


def _sha(token: str) -> str:
    return "sha256:" + (f"{abs(hash(token)):064x}")[:64]


def _origin_pointer(document_id: str = "00000011_origin_head") -> RelocationPointer:
    return RelocationPointer(
        vault_id="origin_vault",
        document_id=document_id,
        server_address="https://origin.example",
        source_content_hash="sha256:" + "ab" * 32,
        relocated_at=datetime(2026, 5, 18, 9, 0, tzinfo=timezone.utc),
    )


def _destination_pointer(document_id: str = "00000022_destination_root") -> RelocationPointer:
    return RelocationPointer(
        vault_id="destination_vault",
        document_id=document_id,
        server_address="https://destination.example",
        source_content_hash="sha256:" + "cd" * 32,
        relocated_at=datetime(2026, 5, 19, 9, 0, tzinfo=timezone.utc),
    )


def _assert_pointer_equals(actual: RelocationPointer, expected: RelocationPointer) -> None:
    """Compare member by member rather than by object equality.

    Object equality would pass an implementation that parsed one stored
    column twice: both sides would then be equal to each other and unequal
    to what was written, and only a per-member comparison against the
    expected literal catches it.
    """
    assert actual is not None
    assert actual.vault_id == expected.vault_id
    assert actual.document_id == expected.document_id
    assert actual.server_address == expected.server_address
    assert actual.source_content_hash == expected.source_content_hash
    assert actual.relocated_at == expected.relocated_at


def _make_doc(doc_id: str, **overrides) -> Document:
    now = datetime.now(timezone.utc)
    base = dict(
        id=doc_id,
        title=f"Document {doc_id}",
        source_type=SourceType.MARKDOWN,
        source_path=f"imports/{doc_id}.md",
        lifecycle_status="active",
        source_content_hash=_sha(doc_id),
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        pipeline_status=PipelineStatus.PROJECTION_COMPLETE,
    )
    base.update(overrides)
    return Document(**base)


def _write_md(vault_dir: Path, relative_path: str, body: str) -> Path:
    full_path = vault_dir / "sources" / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(body)
    return full_path


# ---------------------------------------------------------------------------
# The pointer model
# ---------------------------------------------------------------------------


def test_pointer_rejects_a_malformed_counterpart_identifier():
    """The counterpart identifiers carry the same typed aliases as any other.

    Both arms in one test: a model that rejected every input would satisfy
    the refusal on its own, and the accepting arm is what rules that out.
    A pointer naming an unparseable document is worse than no pointer,
    because it looks like provenance a caller could follow.
    """
    accepted = RelocationPointer(
        vault_id="origin_vault",
        document_id="00000011_origin_head",
        source_content_hash="sha256:" + "ab" * 32,
        relocated_at=datetime(2026, 5, 18, 9, 0, tzinfo=timezone.utc),
    )
    assert accepted.document_id == "00000011_origin_head"
    assert accepted.server_address is None

    with pytest.raises(ValidationError, match="document_id"):
        RelocationPointer(
            vault_id="origin_vault",
            document_id="not a document id",
            source_content_hash="sha256:" + "ab" * 32,
            relocated_at=datetime(2026, 5, 18, 9, 0, tzinfo=timezone.utc),
        )

    with pytest.raises(ValidationError, match="vault_id"):
        RelocationPointer(
            vault_id="Not A Vault Id",
            document_id="00000011_origin_head",
            source_content_hash="sha256:" + "ab" * 32,
            relocated_at=datetime(2026, 5, 18, 9, 0, tzinfo=timezone.utc),
        )


# ---------------------------------------------------------------------------
# Storage round trip
# ---------------------------------------------------------------------------


async def test_both_pointers_round_trip_through_storage(graph_store):
    """A document carrying both pointers reads back with both intact.

    The two pointers differ in every member, so a store that wrote one
    column and read it for both -- the easiest way to get this wrong while
    keeping the shape valid -- fails on the first mismatched member rather
    than passing on a well-formed object.
    """
    origin = _origin_pointer()
    destination = _destination_pointer()
    doc = _make_doc(
        "00000001_round_trip",
        relocated_from=origin,
        relocated_to=destination,
    )
    await graph_store.insert_document(doc)

    stored = await graph_store.get_document(doc.id)
    _assert_pointer_equals(stored.relocated_from, origin)
    _assert_pointer_equals(stored.relocated_to, destination)


async def test_pointer_update_persists(graph_store):
    """An update writes a pointer onto a document that had none."""
    doc = _make_doc("00000002_update_target")
    await graph_store.insert_document(doc)
    assert (await graph_store.get_document(doc.id)).relocated_to is None

    destination = _destination_pointer()
    await graph_store.update_document(doc.id, {"relocated_to": destination})

    _assert_pointer_equals((await graph_store.get_document(doc.id)).relocated_to, destination)


def test_read_path_tolerates_a_row_without_the_relocation_columns():
    """A row from a schema the additive columns have not reached maps to nulls.

    Anti-coincidental-pass: the keys are absent from the row entirely, not
    present and null, which is what a ``SELECT *`` against a vault opened
    before the columns existed returns. A subscript lookup raises KeyError
    on this row, so the test fails loudly if the read path stops tolerating
    the pre-migration shape.
    """
    from sage.storage.postgres.graph_store import PostgresGraphStore

    now_iso = datetime(2026, 5, 21, 10, 30, tzinfo=timezone.utc).isoformat()
    row = {
        "id": "00000003_premigration",
        "title": "T",
        "source_type": SourceType.MARKDOWN.value,
        "source_path": "/x/1.md",
        "lifecycle_status": "active",
        "version_label": None,
        "project": None,
        "tags": None,
        "authority_scope": None,
        "doc_type": None,
        "source_content_hash": "sha256:" + "ab" * 32,
        "adapter_version": "1",
        "created_by": "t",
        "created_at": now_iso,
        "last_modified_by": "t",
        "updated_at": now_iso,
        "projected_at": None,
        "indexed_at": None,
        "source_modified_at": None,
        "document_date": None,
        "semantic_abstract": None,
        "pipeline_status": PipelineStatus.PROJECTION_COMPLETE.value,
        "pipeline_error": None,
        "tier3_metadata": None,
        "metadata_confirmed": False,
    }
    assert "relocated_from" not in row
    assert "relocated_to" not in row

    doc = PostgresGraphStore._row_to_document(row)
    assert doc.relocated_from is None
    assert doc.relocated_to is None


# ---------------------------------------------------------------------------
# The lifecycle action
# ---------------------------------------------------------------------------


async def test_relocate_moves_the_head_and_records_the_pointer_together(
    graph_store, lifecycle_service
):
    """One call moves the state and writes where the document went.

    Both halves asserted, and the pointer asserted absent beforehand. A
    test that checked only the state would pass against an implementation
    that dropped the pointer, which is the divergence the single-call
    requirement exists to prevent; a test that checked only the pointer
    without the ``is None`` precondition would pass against one that never
    wrote it, if some earlier step had.
    """
    doc = _make_doc("00000004_relocating")
    await graph_store.insert_document(doc)
    assert (await graph_store.get_document(doc.id)).relocated_to is None

    # Count the writes as well as read the result. Without the count, an
    # implementation that moved the state in one statement and wrote the
    # pointer in a second satisfies both assertions below while leaving
    # exactly the window -- a document in the relocated state naming no
    # destination -- that "in the same call" exists to close.
    writes: list[dict] = []
    original_update = graph_store.update_document

    async def recording_update(document_id: str, updates: dict):
        writes.append(dict(updates))
        return await original_update(document_id, updates)

    graph_store.update_document = recording_update
    destination = _destination_pointer()
    try:
        await lifecycle_service._set_lifecycle(
            doc.id, SetLifecycleRequest(action="relocate", relocated_to=destination)
        )
    finally:
        graph_store.update_document = original_update

    assert len(writes) == 1, "the state and the pointer must move in one write, not two"
    assert "lifecycle_status" in writes[0]
    assert "relocated_to" in writes[0]

    stored = await graph_store.get_document(doc.id)
    assert stored.lifecycle_status == "relocated"
    _assert_pointer_equals(stored.relocated_to, destination)


async def test_relocate_without_a_pointer_is_refused_and_writes_nothing(
    graph_store, lifecycle_service
):
    """A pointerless relocate refuses, and the document is untouched.

    The second half is the real assertion. A validation placed after the
    state write would raise the same error while leaving a document
    resting in the relocated state with nothing naming its destination --
    exactly the shape the state exists to rule out, and exactly what a
    code-only assertion would wave through.
    """
    doc = _make_doc("00000005_no_pointer")
    await graph_store.insert_document(doc)

    with pytest.raises(MissingFieldError) as excinfo:
        await lifecycle_service._set_lifecycle(doc.id, SetLifecycleRequest(action="relocate"))

    assert excinfo.value.code == "missing_relocated_to"
    assert excinfo.value.status_code == 400

    unchanged = await graph_store.get_document(doc.id)
    assert unchanged.lifecycle_status == "active"
    assert unchanged.relocated_to is None


async def test_relocate_refusal_reaches_the_bulk_item_envelope(graph_store, lifecycle_service):
    """The refusal survives the batch surface a caller actually uses.

    The bulk tool validates item shape up front, so a per-item error that
    the service raises has to travel out through the envelope rather than
    escaping the batch. A sibling item succeeding in the same call is what
    shows the batch did not abort.
    """
    refused = _make_doc("00000006_bulk_refused")
    accepted = _make_doc("00000007_bulk_accepted")
    await graph_store.insert_document(refused)
    await graph_store.insert_document(accepted)

    destination = _destination_pointer()
    response = await lifecycle_service.bulk_set_lifecycle(
        BulkLifecycleRequest(
            items=[
                BulkLifecycleItem(document_id=refused.id, action="relocate"),
                BulkLifecycleItem(
                    document_id=accepted.id, action="relocate", relocated_to=destination
                ),
            ]
        )
    )

    assert response.error_count == 1
    assert response.success_count == 1
    failed, succeeded = response.results
    assert failed.error["error"] == "missing_relocated_to"
    assert (await graph_store.get_document(refused.id)).lifecycle_status == "active"

    assert succeeded.status == "success"
    _assert_pointer_equals((await graph_store.get_document(accepted.id)).relocated_to, destination)


async def test_a_qualifier_is_refused_by_the_action_that_does_not_take_it(
    graph_store, lifecycle_service
):
    """Each action refuses the other's qualifier, and writes nothing.

    Both directions in one test, because the two halves are one rule and
    an implementation is as likely to guard one as both. The spec has
    claimed `successor_id` forbidden outside `supersede` since it was
    written while the service only ignored it; that gap is closed here
    alongside the new field rather than left as an inconsistency between
    two neighbouring descriptions.

    The state assertions are what make this more than a code check: a
    guard placed after the write would raise the same error while having
    already moved the document.
    """
    for_relocate = _make_doc("00000016_relocate_with_successor")
    for_archive = _make_doc("00000017_archive_with_pointer")
    successor = _make_doc("00000018_successor")
    await graph_store.insert_document(for_relocate)
    await graph_store.insert_document(for_archive)
    await graph_store.insert_document(successor)

    with pytest.raises(UnexpectedFieldError) as relocate_exc:
        await lifecycle_service._set_lifecycle(
            for_relocate.id,
            SetLifecycleRequest(
                action="relocate",
                relocated_to=_destination_pointer(),
                successor_id=successor.id,
            ),
        )
    assert relocate_exc.value.code == "unexpected_successor_id"
    assert relocate_exc.value.status_code == 400
    assert relocate_exc.value.detail["attempted_action"] == "relocate"
    assert relocate_exc.value.detail["required_action"] == "supersede"
    assert (await graph_store.get_document(for_relocate.id)).lifecycle_status == "active"

    with pytest.raises(UnexpectedFieldError) as archive_exc:
        await lifecycle_service._set_lifecycle(
            for_archive.id,
            SetLifecycleRequest(action="archive", relocated_to=_destination_pointer()),
        )
    assert archive_exc.value.code == "unexpected_relocated_to"
    assert archive_exc.value.detail["attempted_action"] == "archive"
    assert archive_exc.value.detail["required_action"] == "relocate"
    stored = await graph_store.get_document(for_archive.id)
    assert stored.lifecycle_status == "active"
    assert stored.relocated_to is None


async def test_each_action_still_accepts_its_own_qualifier(graph_store, lifecycle_service):
    """The negative control for the refusal above.

    A guard written as "refuse any qualifier" rather than "refuse the
    wrong one" passes the previous test and fails here.
    """
    predecessor = _make_doc("00000019_predecessor")
    successor = _make_doc("00000020_successor")
    relocating = _make_doc("00000021_relocating")
    for doc in (predecessor, successor, relocating):
        await graph_store.insert_document(doc)

    await lifecycle_service._set_lifecycle(
        predecessor.id, SetLifecycleRequest(action="supersede", successor_id=successor.id)
    )
    assert (await graph_store.get_document(predecessor.id)).lifecycle_status == "archived"

    await lifecycle_service._set_lifecycle(
        relocating.id,
        SetLifecycleRequest(action="relocate", relocated_to=_destination_pointer()),
    )
    assert (await graph_store.get_document(relocating.id)).lifecycle_status == "relocated"


async def test_no_transition_leaves_the_relocated_state(graph_store, lifecycle_service):
    """Reactivation out of ``relocated`` is refused, proved by attempting it.

    Asserted behaviourally rather than by reading the transition table,
    because the table is what the implementation would be derived from and
    reading it would test nothing. The control arm reactivates an
    *archived* document through the same service in the same test: without
    it, a service whose ``reactivate`` was broken outright would pass.
    """
    relocated = _make_doc("00000008_relocated_head")
    archived = _make_doc("00000009_archived", lifecycle_status="archived")
    await graph_store.insert_document(relocated)
    await graph_store.insert_document(archived)
    await lifecycle_service._set_lifecycle(
        relocated.id,
        SetLifecycleRequest(action="relocate", relocated_to=_destination_pointer()),
    )

    with pytest.raises(InvalidLifecycleTransitionError) as excinfo:
        await lifecycle_service._set_lifecycle(
            relocated.id, SetLifecycleRequest(action="reactivate")
        )

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["current_state"] == "relocated"
    assert excinfo.value.detail["attempted_action"] == "reactivate"
    assert excinfo.value.detail["valid_actions"] == []

    # Control: the same action, the same service, a state that permits it.
    reactivated = await lifecycle_service._set_lifecycle(
        archived.id, SetLifecycleRequest(action="reactivate")
    )
    assert reactivated.document.lifecycle_status == "active"


def test_a_configuration_may_not_give_the_relocated_state_a_way_out(minimal_vault_config_dict):
    """The engine constrains ``relocated`` by name where it constrains no other state.

    The state's whole distinction from archival is the omission, so a vault
    left free to declare a reactivation out of it would make the state a
    synonym for ``archived`` while still satisfying the base floor. The
    accepting arm is the same table without the offending row, which is
    what keeps this from passing on a validator that refuses everything.
    """
    lifecycle = minimal_vault_config_dict["lifecycle"]
    assert VaultConfig.model_validate(minimal_vault_config_dict) is not None

    with_exit = {
        **minimal_vault_config_dict,
        "lifecycle": {
            **lifecycle,
            "transitions": [
                *lifecycle["transitions"],
                {"from_state": "relocated", "action": "reactivate", "to_state": "active"},
            ],
        },
    }
    with pytest.raises(ValidationError, match="no transition may leave 'relocated'"):
        VaultConfig.model_validate(with_exit)

    non_terminal = {
        **minimal_vault_config_dict,
        "lifecycle": {
            **lifecycle,
            "states": [
                {**s, "is_terminal": False} if s["value"] == "relocated" else s
                for s in lifecycle["states"]
            ],
        },
    }
    with pytest.raises(ValidationError, match="must declare is_terminal"):
        VaultConfig.model_validate(non_terminal)


async def test_relocated_target_stays_unsatisfied(graph_store, lifecycle_service, minimal_config):
    """A dependency on a relocated document does not resolve across the boundary.

    Checked twice in one test, before and after the move. Without the
    before arm, a precondition that never resolved for an unrelated reason
    would report unsatisfied afterwards too and the test would pass having
    shown nothing. CAS-ADR-050 accepts the stranded dependency as a
    consequence; what it forbids is the engine reaching into the
    destination to satisfy it, and that is what the ``actual`` value pins.
    """
    dependent = _make_doc("00000010_dependent")
    target = _make_doc("00000011_target")
    await graph_store.insert_document(dependent)
    await graph_store.insert_document(target)
    graph_ops = GraphOpsService(graph_store, minimal_config)
    await graph_store.insert_edge(
        Edge(
            id="11111111-1111-4111-8111-111111111111",
            source_id=dependent.id,
            target_id=target.id,
            edge_type=EdgeType.DEPENDS_ON,
            created_at=datetime.now(timezone.utc),
        )
    )

    before = await graph_ops.check_preconditions(dependent.id)
    assert before.satisfied is True
    assert before.checks[0].actual == "active"

    await lifecycle_service._set_lifecycle(
        target.id,
        SetLifecycleRequest(action="relocate", relocated_to=_destination_pointer()),
    )

    after = await graph_ops.check_preconditions(dependent.id)
    assert after.satisfied is False
    assert after.checks[0].satisfied is False
    assert after.checks[0].actual == "relocated"
    assert after.checks[0].required == "active or completed"


# ---------------------------------------------------------------------------
# The ingest half
# ---------------------------------------------------------------------------


async def test_ingest_persists_the_inbound_pointer(tmp_vault_dir, ingestion_service, graph_store):
    """The destination write records where the document came from.

    ``relocated_to`` asserted null in the same test: an implementation that
    wrote the supplied value to both columns satisfies the first assertion
    and fails here, and nothing about the destination half should claim the
    document has also left again.
    """
    _write_md(tmp_vault_dir, "relocated-in.md", "# Moved\n\nBody.\n")
    origin = _origin_pointer()

    result = await ingestion_service.ingest(
        IngestRequest(
            source="relocated-in.md",
            source_type=SourceType.MARKDOWN,
            relocated_from=origin,
        )
    )

    stored = await graph_store.get_document(result.document.id)
    _assert_pointer_equals(stored.relocated_from, origin)
    assert stored.relocated_to is None


async def test_ordinary_ingest_records_no_pointer(tmp_vault_dir, ingestion_service, graph_store):
    """An ingest that names no origin leaves both pointers null.

    The negative control for the test above: without it, an implementation
    stamping a pointer onto every document would pass that one.
    """
    _write_md(tmp_vault_dir, "ordinary.md", "# Ordinary\n\nBody.\n")

    result = await ingestion_service.ingest(
        IngestRequest(source="ordinary.md", source_type=SourceType.MARKDOWN)
    )

    stored = await graph_store.get_document(result.document.id)
    assert stored.relocated_from is None
    assert stored.relocated_to is None


async def test_force_reingest_refreshes_the_inbound_pointer(
    tmp_vault_dir, ingestion_service, graph_store
):
    """A force-reingest naming a different origin replaces the recorded one.

    The two pointers differ in every member, so a force path that kept the
    first one -- the failure mode this covers, since the force branch
    assembles its own update dict rather than rebuilding the record --
    fails on whichever member is compared first rather than on none.
    """
    _write_md(tmp_vault_dir, "refreshed.md", "# Refreshed\n\nBody.\n")
    first = _origin_pointer("00000012_first_origin")
    second = RelocationPointer(
        vault_id="second_origin_vault",
        document_id="00000013_second_origin",
        server_address="https://second-origin.example",
        source_content_hash="sha256:" + "ef" * 32,
        relocated_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )

    result = await ingestion_service.ingest(
        IngestRequest(source="refreshed.md", source_type=SourceType.MARKDOWN, relocated_from=first)
    )
    _assert_pointer_equals(
        (await graph_store.get_document(result.document.id)).relocated_from, first
    )

    forced = await ingestion_service.ingest(
        IngestRequest(
            source="refreshed.md",
            source_type=SourceType.MARKDOWN,
            force=True,
            relocated_from=second,
        )
    )

    assert forced.document.id == result.document.id
    _assert_pointer_equals(
        (await graph_store.get_document(result.document.id)).relocated_from, second
    )

    # The omit arm, and the one that makes the guard load-bearing: an
    # implementation writing the request's value unconditionally clears the
    # pointer on every ordinary force-reingest -- an undo of the move that
    # no caller asked for -- and passes every assertion above it.
    await ingestion_service.ingest(
        IngestRequest(source="refreshed.md", source_type=SourceType.MARKDOWN, force=True)
    )
    _assert_pointer_equals(
        (await graph_store.get_document(result.document.id)).relocated_from, second
    )


# ---------------------------------------------------------------------------
# Non-propagation through supersession
# ---------------------------------------------------------------------------


async def test_supersession_does_not_carry_the_inbound_pointer_forward(
    tmp_vault_dir, ingestion_service, graph_store
):
    """A successor of a relocated-into document carries no pointer of its own.

    This is the assertion most at risk of passing vacuously: the
    successor's field is null both when propagation is correctly
    suppressed and when nothing ever populated the predecessor.
    The predecessor is therefore asserted *positively*, member by member,
    after the supersession, in the same test. The mutation that proves the
    pair load-bearing is adding ``relocated_from`` to the field tuple in
    ``_compute_chain_inheritance``, which turns this red.
    """
    _write_md(tmp_vault_dir, "v1.md", "# V1\n\nOriginal.\n")
    _write_md(tmp_vault_dir, "v2.md", "# V2\n\nRevised.\n")
    origin = _origin_pointer()

    v1 = await ingestion_service.ingest(
        IngestRequest(source="v1.md", source_type=SourceType.MARKDOWN, relocated_from=origin)
    )
    v2 = await ingestion_service.ingest(
        IngestRequest(
            source="v2.md", source_type=SourceType.MARKDOWN, predecessor_id=v1.document.id
        )
    )

    successor = await graph_store.get_document(v2.document.id)
    assert successor.relocated_from is None, (
        "a later version of a relocated document was not itself relocated; "
        "a pointer copied onto it would assert otherwise"
    )

    predecessor = await graph_store.get_document(v1.document.id)
    _assert_pointer_equals(predecessor.relocated_from, origin)


async def test_a_relocated_head_cannot_be_superseded(
    tmp_vault_dir, ingestion_service, graph_store, lifecycle_service
):
    """A relocated head refuses a successor, so the outbound pointer never travels.

    Named for what it holds rather than for its sibling's property. The
    outbound pointer cannot propagate through a supersession because no
    supersession is possible once the head has relocated -- the transition
    table permits no `supersede` out of the state. That refusal is the
    stronger guarantee, and it is what this test asserts; the question of
    suppressing a propagating write never arises on this side.

    The refusal is pinned to its exact code. A bare `Exception` matched on
    the substring "supersede" would pass against any failure in the ingest
    path whose message happened to mention it, including one unrelated to
    the state under test.
    """
    _write_md(tmp_vault_dir, "out-v1.md", "# Out V1\n\nOriginal.\n")
    _write_md(tmp_vault_dir, "out-v2.md", "# Out V2\n\nRevised.\n")
    destination = _destination_pointer()

    v1 = await ingestion_service.ingest(
        IngestRequest(source="out-v1.md", source_type=SourceType.MARKDOWN)
    )
    v2 = await ingestion_service.ingest(
        IngestRequest(
            source="out-v2.md", source_type=SourceType.MARKDOWN, predecessor_id=v1.document.id
        )
    )
    # The head is v2 after the supersession, so it is the one that relocates.
    await lifecycle_service._set_lifecycle(
        v2.document.id,
        SetLifecycleRequest(action="relocate", relocated_to=destination),
    )

    _write_md(tmp_vault_dir, "out-v3.md", "# Out V3\n\nLater.\n")
    with pytest.raises(SupersedeTargetNotActiveError) as excinfo:
        await ingestion_service.ingest(
            IngestRequest(
                source="out-v3.md",
                source_type=SourceType.MARKDOWN,
                predecessor_id=v2.document.id,
            )
        )
    assert excinfo.value.code == "supersede_target_not_active"
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["current_state"] == "relocated"
    assert excinfo.value.detail["predecessor_id"] == v2.document.id

    head = await graph_store.get_document(v2.document.id)
    _assert_pointer_equals(head.relocated_to, destination)
    assert (await graph_store.get_document(v1.document.id)).relocated_to is None


# ---------------------------------------------------------------------------
# Read surfaces
# ---------------------------------------------------------------------------


async def test_both_pointers_surface_on_the_catalog_projection(graph_store, minimal_config):
    """A catalog pass reports both directions without a follow-up read.

    Distinct values per direction, so a projection serving one field for
    both fails rather than matching.
    """
    from sage.models.schemas import DiscoverRequest, RetrievalFilters
    from sage.services.retrieval import RetrievalService

    origin = _origin_pointer()
    destination = _destination_pointer()
    doc = _make_doc(
        "00000014_catalog",
        relocated_from=origin,
        relocated_to=destination,
    )
    await graph_store.insert_document(doc)

    service = RetrievalService(
        graph_store=graph_store,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        config=minimal_config,
    )
    response = await service.discover(
        DiscoverRequest(
            mode="catalog",
            filters=RetrievalFilters(document_ids=[doc.id]),
            response_mode="full",
            limit=10,
        )
    )

    assert len(response.results) == 1
    summary = response.results[0].document
    _assert_pointer_equals(summary.relocated_from, origin)
    _assert_pointer_equals(summary.relocated_to, destination)


# ---------------------------------------------------------------------------
# The whole round trip, across two vaults
# ---------------------------------------------------------------------------


@pytest.fixture
async def destination_vault(pg_dsn, tmp_path, minimal_vault_config_dict):
    """A second vault on its own Postgres schema.

    The Postgres binding names each vault's schema by its vault id, so two
    vaults are isolated in the durable store. The session fixture
    provisions exactly one disposable schema, though, and a round-trip test
    run against one schema would show two "vaults" that can see each
    other's documents -- passing while proving nothing. This fixture opens
    a second schema so the isolation under test is real, and the round-trip
    test asserts it directly rather than trusting this docstring.
    """
    import psycopg

    from sage.storage.postgres.graph_store import PostgresGraphStore
    from sage.storage.postgres.pool import pool_from_conninfo
    from sage.storage.postgres.schema import assert_disposable_target, bootstrap_schema

    schema = assert_disposable_target("sage_test_dest_" + os.urandom(4).hex())
    async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
        await bootstrap_schema(conn, schema=schema, extensions=["vector"])

    vault_dir = tmp_path / "destination"
    (vault_dir / "sources").mkdir(parents=True, exist_ok=True)
    config_dict = {
        **minimal_vault_config_dict,
        "vault": {
            **minimal_vault_config_dict["vault"],
            "id": "destination_vault",
            "name": "Destination Vault",
            "storage_root": str(vault_dir / "sources"),
            "brain_root": str(vault_dir / "brain"),
        },
    }
    config = VaultConfig.model_validate(config_dict)

    pool = pool_from_conninfo(pg_dsn, search_path=f"{schema},public")
    await pool.open()
    try:
        store = PostgresGraphStore(pool)
        await store.initialize(migrate=True)
        locks = DocumentLockManager()
        lifecycle = LifecycleService(store, locks, config, StubContentStore())
        ingestion = IngestionService(
            graph_store=store,
            lock_manager=locks,
            content_store=StubContentStore(),
            embedding_provider=StubEmbeddingProvider(),
            abstraction_provider=StubAbstractionProvider(),
            config=config,
            source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
            lifecycle_service=lifecycle,
        )
        yield {"store": store, "ingestion": ingestion, "dir": vault_dir}
        await store.close()
    finally:
        await pool.close()
        async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')  # noqa: S608


async def test_full_round_trip_across_two_vaults(
    tmp_vault_dir,
    ingestion_service,
    graph_store,
    lifecycle_service,
    minimal_config,
    destination_vault,
):
    """Ingest, supersede twice, relocate the head, and read both sides.

    The destination is written first, per CAS-ADR-050: an interrupted
    relocation then leaves its evidence on the document a reader is most
    likely to hold, rather than leaving the origin naming a document that
    does not exist.

    The origin keeps its three versions and its source bytes at the same
    digest; the destination holds a fresh single-version chain whose root
    points back. Both sides carry the same source digest, which is the
    check that makes the pointer usable without a global coordinate.
    """
    body = "# Moving Document\n\nThe body that travels.\n"
    _write_md(tmp_vault_dir, "mover-v1.md", body)
    _write_md(tmp_vault_dir, "mover-v2.md", body + "\nSecond revision.\n")
    _write_md(tmp_vault_dir, "mover-v3.md", body + "\nThird revision.\n")

    v1 = await ingestion_service.ingest(
        IngestRequest(source="mover-v1.md", source_type=SourceType.MARKDOWN)
    )
    v2 = await ingestion_service.ingest(
        IngestRequest(
            source="mover-v2.md", source_type=SourceType.MARKDOWN, predecessor_id=v1.document.id
        )
    )
    v3 = await ingestion_service.ingest(
        IngestRequest(
            source="mover-v3.md", source_type=SourceType.MARKDOWN, predecessor_id=v2.document.id
        )
    )
    head_hash = v3.document.source_content_hash

    # A dependency inside the origin, so the stranding is observable below.
    dependent = _make_doc("00000015_origin_dependent")
    await graph_store.insert_document(dependent)
    graph_ops = GraphOpsService(graph_store, minimal_config)
    await graph_store.insert_edge(
        Edge(
            id="22222222-2222-4222-8222-222222222222",
            source_id=dependent.id,
            target_id=v3.document.id,
            edge_type=EdgeType.DEPENDS_ON,
            created_at=datetime.now(timezone.utc),
        )
    )

    # --- The destination half, written first. ---
    dest_dir = destination_vault["dir"]
    _write_md(dest_dir, "mover-v3.md", body + "\nThird revision.\n")
    destination_doc = await destination_vault["ingestion"].ingest(
        IngestRequest(
            source="mover-v3.md",
            source_type=SourceType.MARKDOWN,
            relocated_from=RelocationPointer(
                vault_id="test_vault",
                document_id=v3.document.id,
                server_address="https://origin.example",
                source_content_hash=head_hash,
                relocated_at=datetime(2026, 6, 2, 10, 0, tzinfo=timezone.utc),
            ),
        )
    )

    # --- The origin half. ---
    await lifecycle_service._set_lifecycle(
        v3.document.id,
        SetLifecycleRequest(
            action="relocate",
            relocated_to=RelocationPointer(
                vault_id="destination_vault",
                document_id=destination_doc.document.id,
                server_address="https://destination.example",
                source_content_hash=destination_doc.document.source_content_hash,
                relocated_at=datetime(2026, 6, 2, 10, 0, tzinfo=timezone.utc),
            ),
        ),
    )

    # The origin keeps everything it had.
    origin_chain = await graph_store.chain_walk(v3.document.id, "supersedes")
    assert len(origin_chain["documents"]) == 3
    origin_head = await graph_store.get_document(v3.document.id)
    assert origin_head.lifecycle_status == "relocated"
    assert origin_head.relocated_to.vault_id == "destination_vault"
    assert origin_head.relocated_to.document_id == destination_doc.document.id
    assert origin_head.source_content_hash == head_hash
    assert (Path(minimal_config.vault.storage_root) / origin_head.source_path).exists()

    # The destination begins a fresh chain that points back.
    dest_store = destination_vault["store"]
    dest_head = await dest_store.get_document(destination_doc.document.id)
    assert len((await dest_store.chain_walk(dest_head.id, "supersedes"))["documents"]) == 1
    assert dest_head.relocated_from.vault_id == "test_vault"
    assert dest_head.relocated_from.document_id == v3.document.id
    assert dest_head.relocated_to is None

    # The confirming check: both vaults hold the same source at the same digest.
    assert dest_head.source_content_hash == origin_head.source_content_hash

    # The two vaults really are separate stores. Without this, a test run
    # against one schema would satisfy everything above while proving none
    # of it.
    assert await dest_store.get_document(v1.document.id) is None
    assert await graph_store.get_document(dest_head.id) is None

    # Nothing resolves across the boundary: the stranded dependency stays
    # unsatisfied inside the origin, which CAS-ADR-050 accepts and this
    # pins so a future cross-vault resolution cannot land unnoticed.
    preconditions = await graph_ops.check_preconditions(dependent.id)
    assert preconditions.satisfied is False
    assert preconditions.checks[0].actual == "relocated"
