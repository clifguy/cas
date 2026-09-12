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
    DuplicateContentError,
    InvalidLifecycleTransitionError,
    MissingFieldError,
    RelocationProvenanceMismatchError,
    ReservedTransitionError,
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
#
# The digest member is the one exception, and it is an exception by
# decision rather than by convenience. CAS-ADR-050 Decision 10 makes the
# pointer name the bytes that travelled and requires each half to prove
# that against digests it already records, so a pointer carrying a digest
# belonging to nothing is refused rather than stored. Every call that
# expects to succeed therefore derives
# ``source_content_hash`` from its own document or file, while the other
# four members stay distinct exactly as before. The literal defaults below
# are kept for the calls that expect a refusal, and they are safe as
# defaults only because they describe no document anywhere.


def _sha(token: str) -> str:
    return "sha256:" + (f"{abs(hash(token)):064x}")[:64]


def _file_digest(vault_dir: Path, relative_path: str) -> str:
    """The digest a delivered file will be recorded under.

    Hashes through the same helper the ingest path uses, so a test
    deriving an expected digest cannot disagree with the value the service
    computes over the same bytes.
    """
    from sage.vault_source_binding import hash_file

    return hash_file(vault_dir / "sources" / relative_path)


def _origin_pointer(
    document_id: str = "00000011_origin_head",
    source_content_hash: str = "sha256:" + "ab" * 32,
) -> RelocationPointer:
    return RelocationPointer(
        vault_id="origin_vault",
        document_id=document_id,
        server_address="https://origin.example",
        source_content_hash=source_content_hash,
        relocated_at=datetime(2026, 5, 18, 9, 0, tzinfo=timezone.utc),
    )


def _destination_pointer(
    document_id: str = "00000022_destination_root",
    source_content_hash: str = "sha256:" + "cd" * 32,
) -> RelocationPointer:
    return RelocationPointer(
        vault_id="destination_vault",
        document_id=document_id,
        server_address="https://destination.example",
        source_content_hash=source_content_hash,
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
    destination = _destination_pointer(source_content_hash=doc.source_content_hash)
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

    destination = _destination_pointer(source_content_hash=accepted.source_content_hash)
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


async def test_relocate_holds_the_pointer_to_this_document_s_own_digest(
    graph_store, lifecycle_service
):
    """A pointer whose digest is not this document's is refused; its own is taken.

    Both arms in one test, because they are one rule and an
    implementation is as likely to get half of it. The accept arm is what
    rules out the mutation this check invites most: a guard that refuses
    every relocation satisfies the refuse arm and nothing else.

    The refusal names both digests, so a caller can see which of the two
    it got wrong without a second call.

    Both fixtures leave ``stored_content_hash`` unset, so their two
    recorded digests coincide and this test cannot distinguish which of
    them the origin admits. That distinction is
    ``test_relocate_accounts_for_either_digest_the_origin_records``'s, and
    it is a real distinction rather than a redundancy: the origin admits
    either.
    """
    mismatched = _make_doc("00000023_digest_mismatch")
    matching = _make_doc("00000024_digest_match")
    await graph_store.insert_document(mismatched)
    await graph_store.insert_document(matching)

    foreign = _sha("belongs-to-no-document")
    assert foreign != mismatched.source_content_hash

    with pytest.raises(RelocationProvenanceMismatchError) as excinfo:
        await lifecycle_service._set_lifecycle(
            mismatched.id,
            SetLifecycleRequest(
                action="relocate",
                relocated_to=_destination_pointer(source_content_hash=foreign),
            ),
        )

    assert excinfo.value.code == "relocated_to_provenance_mismatch"
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["field"] == "relocated_to"
    assert excinfo.value.detail["pointer_content_hash"] == foreign
    assert excinfo.value.detail["document_content_hash"] == mismatched.source_content_hash

    # Refused before any write, on the template the pointerless refusal set.
    untouched = await graph_store.get_document(mismatched.id)
    assert untouched.lifecycle_status == "active"
    assert untouched.relocated_to is None

    # The accept arm.
    await lifecycle_service._set_lifecycle(
        matching.id,
        SetLifecycleRequest(
            action="relocate",
            relocated_to=_destination_pointer(source_content_hash=matching.source_content_hash),
        ),
    )
    assert (await graph_store.get_document(matching.id)).lifecycle_status == "relocated"


async def test_relocate_accounts_for_either_digest_the_origin_records(
    graph_store, lifecycle_service
):
    """The origin admits its provenance digest or its as-stored digest, and no third.

    CAS-ADR-050 Decision 10 makes the pointer name the bytes that
    travelled, and the origin cannot know which of its two byte-sets the
    caller took: a caller still holding the file it originally ingested
    relocates that, while a caller that does not fetches what the vault
    serves, which is the retained copy. Both are faithful relocations, so
    both digests are admitted.

    The two coincide for every document whose store kept the delivered
    bytes verbatim, which is every other fixture in this file and the
    whole filesystem binding, so this is the only place the difference is
    observable. Here they are made to differ, which is the split
    CAS-ADR-043 permits a rewriting binding to produce -- an Office
    package stamped by a document store at rest.

    Three arms, and the third is what keeps this from being a relaxation.
    Admitting two known values is not admitting any value: a digest
    matching neither is still refused, and the refusal names both
    admissible values so a caller sees what it can act on. The third arm
    asserts the second of them, which is also what makes its fixture's
    ``stored_content_hash`` load-bearing rather than decorative.
    """
    as_stored = _sha("the-rewritten-copy")
    by_provenance = _make_doc("00000025_relocated_by_original", stored_content_hash=as_stored)
    by_retained = _make_doc("00000026_relocated_by_retained", stored_content_hash=as_stored)
    refused = _make_doc("00000038_neither_digest", stored_content_hash=as_stored)
    for doc in (by_provenance, by_retained, refused):
        await graph_store.insert_document(doc)
    assert by_provenance.source_content_hash != by_provenance.stored_content_hash, (
        "without a real divergence between the two digests this test asserts nothing"
    )

    # The caller still held the file it originally delivered.
    await lifecycle_service._set_lifecycle(
        by_provenance.id,
        SetLifecycleRequest(
            action="relocate",
            relocated_to=_destination_pointer(
                source_content_hash=by_provenance.source_content_hash
            ),
        ),
    )
    assert (await graph_store.get_document(by_provenance.id)).lifecycle_status == "relocated"

    # The caller fetched what the vault serves, which is the stamped copy.
    # This arm is the one the previous rule refused, and refusing it made a
    # relocation out of a rewriting store impossible.
    await lifecycle_service._set_lifecycle(
        by_retained.id,
        SetLifecycleRequest(
            action="relocate",
            relocated_to=_destination_pointer(source_content_hash=as_stored),
        ),
    )
    stored = await graph_store.get_document(by_retained.id)
    assert stored.lifecycle_status == "relocated"
    assert stored.relocated_to.source_content_hash == as_stored

    # A digest describing neither recorded form is still refused.
    with pytest.raises(RelocationProvenanceMismatchError) as excinfo:
        await lifecycle_service._set_lifecycle(
            refused.id,
            SetLifecycleRequest(
                action="relocate",
                relocated_to=_destination_pointer(
                    source_content_hash=_sha("bytes-this-vault-never-held")
                ),
            ),
        )
    assert excinfo.value.code == "relocated_to_provenance_mismatch"
    assert excinfo.value.detail["document_content_hash"] == refused.source_content_hash
    # Both admissible digests are named, so a refused caller need not infer
    # the second. Asserting it is also what makes this fixture's
    # ``stored_content_hash`` load-bearing: without this line, deleting the
    # override leaves the arm passing while the refusal silently stops
    # reporting the value the caller would act on.
    assert excinfo.value.detail["also_accounted_content_hash"] == as_stored
    untouched = await graph_store.get_document(refused.id)
    assert untouched.lifecycle_status == "active"
    assert untouched.relocated_to is None


async def test_the_digest_refusal_reaches_the_bulk_item_envelope(graph_store, lifecycle_service):
    """The digest refusal travels the batch surface, as its sibling does.

    The pointerless refusal is already pinned here; this is the same
    assertion for the check added beside it, because a refusal raised
    from a deeper point in the same function is not guaranteed to be
    translated by the envelope that translates the shallower one.
    """
    refused = _make_doc("00000027_bulk_digest_refused")
    accepted = _make_doc("00000028_bulk_digest_accepted")
    await graph_store.insert_document(refused)
    await graph_store.insert_document(accepted)

    response = await lifecycle_service.bulk_set_lifecycle(
        BulkLifecycleRequest(
            items=[
                BulkLifecycleItem(
                    document_id=refused.id,
                    action="relocate",
                    relocated_to=_destination_pointer(
                        source_content_hash=_sha("foreign-to-the-batch")
                    ),
                ),
                BulkLifecycleItem(
                    document_id=accepted.id,
                    action="relocate",
                    relocated_to=_destination_pointer(
                        source_content_hash=accepted.source_content_hash
                    ),
                ),
            ]
        )
    )

    assert response.error_count == 1
    assert response.success_count == 1
    failed, succeeded = response.results
    assert failed.error["error"] == "relocated_to_provenance_mismatch"
    assert (await graph_store.get_document(refused.id)).lifecycle_status == "active"
    assert succeeded.status == "success"


async def test_a_dry_run_relocate_reaches_the_same_digest_verdict(graph_store, lifecycle_service):
    """A dry run refuses what the run it previews would refuse.

    A preview that reported a relocation as available and then failed on
    the real call would be worse than no preview: the caller has already
    written the destination half by the time it asks.
    """
    doc = _make_doc("00000029_dry_run_digest")
    await graph_store.insert_document(doc)

    with pytest.raises(RelocationProvenanceMismatchError) as excinfo:
        await lifecycle_service._set_lifecycle(
            doc.id,
            SetLifecycleRequest(
                action="relocate",
                relocated_to=_destination_pointer(source_content_hash=_sha("not-this-one")),
                dry_run=True,
            ),
        )
    assert excinfo.value.code == "relocated_to_provenance_mismatch"

    preview = await lifecycle_service._set_lifecycle(
        doc.id,
        SetLifecycleRequest(
            action="relocate",
            relocated_to=_destination_pointer(source_content_hash=doc.source_content_hash),
            dry_run=True,
        ),
    )
    assert preview.document.lifecycle_status == "relocated"
    assert (await graph_store.get_document(doc.id)).lifecycle_status == "active", (
        "a dry run must leave the document alone"
    )


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

    The relocate arm also pins an ordering. Its pointer carries the
    fixture default digest, which belongs to no document, so both this
    refusal and the digest refusal are available; asserting the
    qualifier code holds the digest check below the qualifier checks.
    A caller supplying a field the action does not take has not yet
    described a coherent call, and telling it about its digest first
    would answer a question it has not asked.
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
        SetLifecycleRequest(
            action="relocate",
            relocated_to=_destination_pointer(source_content_hash=relocating.source_content_hash),
        ),
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
        SetLifecycleRequest(
            action="relocate",
            relocated_to=_destination_pointer(source_content_hash=relocated.source_content_hash),
        ),
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


async def test_the_service_holds_the_reservation_a_lenient_config_broke(
    graph_store, lock_manager, minimal_vault_config_dict
):
    """A vault already serving a reserved-breaking table is refused at the service.

    The configuration validator refuses these tables, but that is not the
    whole barrier and it was wrong to treat it as one: ``load_vault_config``
    validates every on-disk configuration leniently, because rejecting a
    file would drop its vault from the registry and put it out of reach of
    the surfaces that could repair it. So a vault can be serving one of
    these tables right now, and the service is the only thing standing
    between it and a broken document. The lenient context below is exactly
    the one that path uses.

    Three shapes, and the third is why this test exists. A and B are the
    two the earlier review probed, and they distinguish a guard keyed on
    the landing state from one keyed on the action's name. C reaches the
    supersede branch, which builds its own update and never writes a
    pointer: before the reservation was enforced ahead of the branch
    split, C was *accepted* and left a document resting in the terminal
    state naming nowhere, with no transition able to repair it.
    """
    import copy

    from sage.services.lifecycle import LifecycleService

    def _leniently(extra_row: dict) -> VaultConfig:
        raw = copy.deepcopy(minimal_vault_config_dict)
        raw["lifecycle"]["transitions"].append(extra_row)
        # Strict validation refuses it -- asserted, so this fixture cannot
        # quietly become a table the validator would have allowed anyway.
        with pytest.raises(ValidationError):
            VaultConfig.model_validate(raw)
        return VaultConfig.model_validate(raw, context={"lifecycle_validation": "warn"})

    async def _service(extra_row: dict) -> LifecycleService:
        return LifecycleService(
            graph_store, lock_manager, _leniently(extra_row), StubContentStore()
        )

    # A — another action reaching the state. Refused by the reservation.
    doc_a = _make_doc("00000030_aliased_entry")
    await graph_store.insert_document(doc_a)
    service_a = await _service({"from_state": "active", "action": "exile", "to_state": "relocated"})
    with pytest.raises(ReservedTransitionError) as exile_exc:
        await service_a._set_lifecycle(doc_a.id, SetLifecycleRequest(action="exile"))
    assert exile_exc.value.code == "reserved_transition"
    assert exile_exc.value.detail["to_state"] == "relocated"
    assert (await graph_store.get_document(doc_a.id)).lifecycle_status == "active"

    # B — the relocate action landing somewhere else. Refused likewise, so
    # no pointer is stamped onto a document that can still be reactivated.
    doc_b = _make_doc("00000031_stray_landing", lifecycle_status="completed")
    await graph_store.insert_document(doc_b)
    service_b = await _service(
        {"from_state": "completed", "action": "relocate", "to_state": "archived"}
    )
    with pytest.raises(ReservedTransitionError):
        await service_b._set_lifecycle(
            doc_b.id,
            SetLifecycleRequest(action="relocate", relocated_to=_destination_pointer()),
        )
    stored_b = await graph_store.get_document(doc_b.id)
    assert stored_b.lifecycle_status == "completed"
    assert stored_b.relocated_to is None

    # C — a supersede row landing in the state. The pointer check alone
    # passes this (it lands in `relocated` and a pointer was supplied) and
    # the supersede branch then writes neither, so only a reservation
    # checked ahead of the branch split refuses it.
    doc_c = _make_doc("00000032_supersede_entry", lifecycle_status="completed")
    successor = _make_doc("00000033_successor")
    await graph_store.insert_document(doc_c)
    await graph_store.insert_document(successor)
    service_c = await _service(
        {
            "from_state": "completed",
            "action": "supersede",
            "to_state": "relocated",
            "creates_edge": "supersedes",
        }
    )
    with pytest.raises(ReservedTransitionError):
        await service_c._set_lifecycle(
            doc_c.id,
            SetLifecycleRequest(
                action="supersede",
                successor_id=successor.id,
                relocated_to=_destination_pointer(),
            ),
        )
    stored_c = await graph_store.get_document(doc_c.id)
    assert stored_c.lifecycle_status == "completed"
    assert stored_c.relocated_to is None


async def test_ingest_is_refused_by_a_configuration_that_lands_it_in_relocated(
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_vault_config_dict,
    tmp_vault_dir,
):
    """A vault landing fresh ingests in the terminal state is refused at ingest.

    The ingest half of the reservation the lifecycle service already
    holds. The landing state is read from the configured ``(new)`` row,
    so a table naming ``relocated`` there would insert a document already
    resting in the state -- carrying ``relocated_from`` and no
    ``relocated_to``, with no action able to leave the state and repair
    it. That is the one shape the state exists to rule out, reached
    without any relocation having happened.

    Refused at the service rather than left to the configuration
    validator, for the reason the sibling test states: an on-disk
    configuration loads leniently, because rejecting the file would drop
    its vault out of reach of the surfaces that could fix it. So a vault
    can be serving this table now.

    The control arm is the same service over the ordinary table, which
    ingests. Without it a guard written as "refuse when the vault
    declares a relocated state at all" -- which every vault in this file
    does -- would pass the refusal above and break every ingest.
    """
    import copy

    def _service(landing_state: str, *, lenient: bool) -> IngestionService:
        raw = copy.deepcopy(minimal_vault_config_dict)
        for row in raw["lifecycle"]["transitions"]:
            if row["from_state"] == "(new)":
                row["to_state"] = landing_state
        if lenient:
            # Strict validation refuses it -- asserted, so this fixture
            # cannot quietly become a table the validator would have
            # allowed anyway, which would make the refusal below a test of
            # nothing.
            with pytest.raises(ValidationError):
                VaultConfig.model_validate(raw)
            config = VaultConfig.model_validate(raw, context={"lifecycle_validation": "warn"})
        else:
            config = VaultConfig.model_validate(raw)
        return IngestionService(
            graph_store=graph_store,
            lock_manager=lock_manager,
            content_store=stub_content_store,
            embedding_provider=stub_embedding_provider,
            abstraction_provider=stub_abstraction_provider,
            config=config,
            source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
        )

    _write_md(tmp_vault_dir, "landing-refused.md", "# Landing\n\nBody.\n")

    with pytest.raises(ReservedTransitionError) as excinfo:
        await _service("relocated", lenient=True).ingest(
            IngestRequest(source="landing-refused.md", source_type=SourceType.MARKDOWN)
        )
    assert excinfo.value.code == "reserved_transition"
    assert excinfo.value.detail["to_state"] == "relocated"
    assert excinfo.value.detail["attempted_action"] == "ingest"
    assert await graph_store.list_all_documents() == [], (
        "a refused landing state must not insert a document"
    )

    # The control: the ordinary table over the same service shape.
    result = await _service("active", lenient=False).ingest(
        IngestRequest(source="landing-refused.md", source_type=SourceType.MARKDOWN)
    )
    assert (await graph_store.get_document(result.document.id)).lifecycle_status == "active"


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
        SetLifecycleRequest(
            action="relocate",
            relocated_to=_destination_pointer(source_content_hash=target.source_content_hash),
        ),
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
    origin = _origin_pointer(source_content_hash=_file_digest(tmp_vault_dir, "relocated-in.md"))

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
    delivered = _file_digest(tmp_vault_dir, "refreshed.md")
    first = _origin_pointer("00000012_first_origin", source_content_hash=delivered)
    # Differs from ``first`` in every member except the digest, which both
    # must carry: the same bytes are being relocated either way, and a
    # pointer naming a different digest is now refused rather than stored.
    second = RelocationPointer(
        vault_id="second_origin_vault",
        document_id="00000013_second_origin",
        server_address="https://second-origin.example",
        source_content_hash=delivered,
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


async def test_ingest_holds_the_pointer_to_the_delivered_bytes_digest(
    tmp_vault_dir, ingestion_service, graph_store
):
    """A pointer whose digest is not the delivered bytes' is refused.

    The destination half of the same rule the origin half holds, and the
    accept arm is carried by ``test_ingest_persists_the_inbound_pointer``
    above, which now derives its digest from the file it writes. Kept
    separate rather than folded in because that test is about the pointer
    being persisted and this one is about the call being refused, and a
    reader looking for either should not have to read both.
    """
    _write_md(tmp_vault_dir, "digest-mismatch.md", "# Mismatched\n\nBody.\n")
    delivered = _file_digest(tmp_vault_dir, "digest-mismatch.md")
    foreign = _sha("delivered-nothing-like-this")
    assert foreign != delivered

    with pytest.raises(RelocationProvenanceMismatchError) as excinfo:
        await ingestion_service.ingest(
            IngestRequest(
                source="digest-mismatch.md",
                source_type=SourceType.MARKDOWN,
                relocated_from=_origin_pointer(source_content_hash=foreign),
            )
        )

    assert excinfo.value.code == "relocated_from_provenance_mismatch"
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["field"] == "relocated_from"
    assert excinfo.value.detail["pointer_content_hash"] == foreign
    assert excinfo.value.detail["document_content_hash"] == delivered

    # Nothing landed: the refusal is not a half-completed ingest.
    assert (
        await graph_store.find_documents_by_hashes([delivered], prefer_lifecycle_statuses=()) == {}
    )


async def test_a_refused_relocation_ingest_retains_nothing(
    tmp_vault_dir, tmp_path, ingestion_service, graph_store
):
    """A refused destination write leaves no retained file behind.

    The assertion that places the check rather than merely adding it. An
    external import copies the caller's file into the vault before the
    record is built, so a digest check placed beside the two sibling
    digest refusals -- which sit below retention and are harmless there,
    because both mean the bytes are already present and retention reuses
    the existing copy -- would refuse a *novel* file after copying it in,
    leaving a retained file with no document row. No audit walks for that,
    and this file's own ingest path records the same mistake having been
    made once before, for the doc_type gate.

    An absolute source is what exercises it: a vault-relative source is
    retained in place, so nothing is copied and nothing can be orphaned.
    """
    external = tmp_path / "arriving-from-elsewhere.md"
    external.write_text("# Arriving\n\nBody.\n")

    def tree() -> set[str]:
        root = tmp_vault_dir / "sources"
        return {str(f.relative_to(root)) for f in root.rglob("*") if f.is_file()}

    before = tree()

    with pytest.raises(RelocationProvenanceMismatchError):
        await ingestion_service.ingest(
            IngestRequest(
                source=str(external),
                source_type=SourceType.MARKDOWN,
                relocated_from=_origin_pointer(source_content_hash=_sha("not-these-bytes")),
            )
        )

    assert tree() == before, (
        "a refused relocation must not leave the caller's bytes retained in the vault"
    )


async def test_force_reingest_refuses_a_mismatched_pointer(
    tmp_vault_dir, ingestion_service, graph_store
):
    """The force branch is held to the same rule as the first write.

    The force path assembles its own update dict rather than rebuilding
    the record, which is why the pointer's refresh needed its own test
    above; for the same reason a check wired only into the new-document
    branch would leave this one open.
    """
    _write_md(tmp_vault_dir, "force-digest.md", "# Force\n\nBody.\n")
    delivered = _file_digest(tmp_vault_dir, "force-digest.md")
    good = _origin_pointer(source_content_hash=delivered)

    first = await ingestion_service.ingest(
        IngestRequest(
            source="force-digest.md", source_type=SourceType.MARKDOWN, relocated_from=good
        )
    )

    with pytest.raises(RelocationProvenanceMismatchError) as excinfo:
        await ingestion_service.ingest(
            IngestRequest(
                source="force-digest.md",
                source_type=SourceType.MARKDOWN,
                force=True,
                relocated_from=_origin_pointer(source_content_hash=_sha("wrong-on-the-force-arm")),
            )
        )
    assert excinfo.value.code == "relocated_from_provenance_mismatch"

    # The refused force call left the recorded pointer as it was.
    _assert_pointer_equals((await graph_store.get_document(first.document.id)).relocated_from, good)


async def test_a_dry_run_relocation_ingest_reaches_the_same_digest_verdict(
    tmp_vault_dir, ingestion_service
):
    """The preview refuses what the run it previews would refuse.

    The preview resolves and hashes the source without retaining it, and
    computes no projection, so it has no as-stored digest to fall back
    on. A check inherited from the guard the preview already uses for the
    identical-content refusal would therefore go quiet exactly where the
    real run refuses -- green-lighting the one call the run rejects.
    """
    _write_md(tmp_vault_dir, "dry-digest.md", "# Dry\n\nBody.\n")
    delivered = _file_digest(tmp_vault_dir, "dry-digest.md")

    with pytest.raises(RelocationProvenanceMismatchError) as excinfo:
        await ingestion_service.ingest(
            IngestRequest(
                source="dry-digest.md",
                source_type=SourceType.MARKDOWN,
                dry_run=True,
                relocated_from=_origin_pointer(source_content_hash=_sha("previewed-wrong")),
            )
        )
    assert excinfo.value.code == "relocated_from_provenance_mismatch"

    preview = await ingestion_service.ingest(
        IngestRequest(
            source="dry-digest.md",
            source_type=SourceType.MARKDOWN,
            dry_run=True,
            relocated_from=_origin_pointer(source_content_hash=delivered),
        )
    )
    assert preview.dry_run is True
    assert preview.would_create is True


async def test_a_pointer_mismatch_outranks_duplicate_content(tmp_vault_dir, ingestion_service):
    """A call that is both a duplicate and a digest mismatch reports the pointer.

    Pinned rather than left to be rediscovered by whoever reads
    ``duplicate_content`` and expects it. The pointer is the caller's
    assertion about what the call *is*; the duplicate verdict is about
    what the vault already holds. Answering the second first would tell a
    caller its relocation was refused as an ordinary re-ingest.
    """
    body = "# Shared\n\nIdentical bytes.\n"
    _write_md(tmp_vault_dir, "already-here.md", body)
    _write_md(tmp_vault_dir, "arriving-again.md", body)
    delivered = _file_digest(tmp_vault_dir, "already-here.md")
    assert delivered == _file_digest(tmp_vault_dir, "arriving-again.md")

    await ingestion_service.ingest(
        IngestRequest(source="already-here.md", source_type=SourceType.MARKDOWN)
    )

    # Control: without the pointer this is the duplicate refusal, so the
    # test below is not merely asserting the only error available.
    with pytest.raises(DuplicateContentError):
        await ingestion_service.ingest(
            IngestRequest(source="arriving-again.md", source_type=SourceType.MARKDOWN)
        )

    with pytest.raises(RelocationProvenanceMismatchError) as excinfo:
        await ingestion_service.ingest(
            IngestRequest(
                source="arriving-again.md",
                source_type=SourceType.MARKDOWN,
                relocated_from=_origin_pointer(source_content_hash=_sha("neither-of-these")),
            )
        )
    assert excinfo.value.code == "relocated_from_provenance_mismatch"


async def test_the_digest_comparison_accepts_the_bare_hex_spelling(
    tmp_vault_dir, ingestion_service, graph_store
):
    """A correct digest in a non-canonical spelling is still correct.

    What this pins is modest and worth stating plainly: the typed alias
    canonicalizes the pointer's value on model construction, so the
    service compares two canonical strings and the spelling never reaches
    it. The test guards the alias staying in front of this field rather
    than the comparison itself -- an exact-match comparison behind a
    field that stopped normalizing would start reading a spelling
    difference as a relocation mismatch, and nothing else here would say
    so.
    """
    _write_md(tmp_vault_dir, "spelling.md", "# Spelling\n\nBody.\n")
    delivered = _file_digest(tmp_vault_dir, "spelling.md")
    bare = delivered.removeprefix("sha256:").upper()
    assert bare != delivered

    result = await ingestion_service.ingest(
        IngestRequest(
            source="spelling.md",
            source_type=SourceType.MARKDOWN,
            relocated_from=_origin_pointer(source_content_hash=bare),
        )
    )

    stored = await graph_store.get_document(result.document.id)
    assert stored.relocated_from.source_content_hash == delivered


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
    origin = _origin_pointer(source_content_hash=_file_digest(tmp_vault_dir, "v1.md"))

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
    # The head that relocates is v2, so the pointer carries v2's digest.
    destination = _destination_pointer(source_content_hash=_file_digest(tmp_vault_dir, "out-v2.md"))

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

    # The confirming check. Both vaults hold the same source at the same
    # digest here because both are filesystem-bound and retain what they
    # were handed; under a binding that rewrites its copy at rest the two
    # would differ, and the digest that relates them is the travelled one.
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
