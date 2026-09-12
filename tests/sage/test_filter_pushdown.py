"""Push retrieval-service filters into SQL.

Two hot paths in ``sage/services/retrieval.py`` previously fetched the
entire vault via ``list_all_documents()`` and applied filters in Python:

* ``_content_filters()`` — resolves doc-level filters into a
  ``document_id`` allowlist that pre-filters the chunk store.
* ``_list_filtered()`` — enumerates docs for keyword query ``"*"``.

Both must push ``doc_type`` / ``project`` / ``lifecycle_status`` /
``pipeline_status`` / ``tags`` / ``document_ids`` filters into the SQL
``query_documents()`` call instead.

Test coverage:

* T1-T3: behavior equivalence across the AC-named filters.
* T4: ``scope=AUTHORITATIVE`` must continue to work via the Python
  post-pass (``authority_scope`` is not a SQL column predicate today).
* T5-T6: anti-coincidental-pass gates -- ``list_all_documents()`` must
  no longer be called from either site. Monkeypatched to raise; the
  optimization tests fail if a future change silently reverts to the
  full-vault fetch.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

import pytest

from sage.adapters.interfaces import Chunk
from sage.config import VaultConfig
from sage.models.enums import (
    PipelineStatus,
    RetrievalMode,
    RetrievalScope,
    SourceType,
)
from sage.models.schemas import (
    DiscoverRequest,
    Document,
    RetrievalFilters,
)
from sage.services.retrieval import RetrievalService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DOC_ID_RE = re.compile(r"^[0-9a-f]{8}_[a-z0-9_]+$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _id(name: str) -> str:
    if _DOC_ID_RE.fullmatch(name):
        return name
    return f"{hashlib.sha256(name.encode()).hexdigest()[:8]}_{name}"


def _sha(name: str) -> str:
    if _SHA256_RE.fullmatch(name):
        return name
    return "sha256:" + hashlib.sha256(f"sage-test-hash:{name}".encode()).hexdigest()


def _make_doc(
    short_name: str,
    lifecycle_status: str = "active",
    pipeline_status: PipelineStatus = PipelineStatus.ABSTRACTION_COMPLETE,
    project: str | None = None,
    doc_type: str | None = None,
    authority_scope: str | None = None,
    tags: list[str] | None = None,
) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=_id(short_name),
        title=f"Test {short_name}",
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{short_name}.md",
        lifecycle_status=lifecycle_status,
        source_content_hash=_sha(short_name),
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=pipeline_status,
        project=project,
        doc_type=doc_type,
        authority_scope=authority_scope,
        tags=tags or [],
    )


async def _index_marker(
    content_store,
    embedding_provider,
    doc,
    marker: str = "alpha-marker",
) -> None:
    """Index a single chunk containing the marker term so BM25 finds it.

    Stamps the parent document's lifecycle_status, project, and doc_type
    on the chunk row so LanceDB pre-filter pushdown matches the
    chunk when the filter is active. Mirrors what production ingest does
    at ``_stage2_indexing`` time.
    """
    chunk = Chunk(
        document_id=doc.id,
        heading_path="Body",
        content=f"This document contains the {marker} term.",
        chunk_index=0,
        doc_type=doc.doc_type,
        lifecycle_status=doc.lifecycle_status,
        project=doc.project,
    )
    [embedding] = await embedding_provider.embed([chunk.content])
    chunk.embedding = embedding
    await content_store.index_chunks(doc.id, [chunk])


@pytest.fixture
def filter_pushdown_retrieval_service(
    graph_store, stub_content_store, stub_embedding_provider, minimal_config
):
    return RetrievalService(
        graph_store=graph_store,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        config=minimal_config,
    )


async def _seed_mixed_vault(graph_store, content_store, embedding_provider):
    """Five docs spanning the dimensions needs to exercise:

    * d_active_alpha -- lifecycle=active, project=alpha, authority=None
    * d_completed_alpha -- lifecycle=completed, project=alpha, authority=None
    * d_active_beta -- lifecycle=active, project=beta, authority=None
    * d_authoritative -- lifecycle=active, project=alpha, authority="alpha-domain"
    * d_failed -- lifecycle=active, project=alpha, authority=None,
                            pipeline_status=FAILED (must never appear in results)

    Each non-failed doc gets one chunk containing "alpha-marker" so a
    keyword search for that term retrieves them when the filter allows.
    """
    docs = {
        "d_active_alpha": _make_doc(
            "d_active_alpha",
            lifecycle_status="active",
            project="alpha",
            doc_type="note",
        ),
        "d_completed_alpha": _make_doc(
            "d_completed_alpha",
            lifecycle_status="completed",
            project="alpha",
            doc_type="note",
        ),
        "d_active_beta": _make_doc(
            "d_active_beta",
            lifecycle_status="active",
            project="beta",
            doc_type="note",
        ),
        "d_authoritative": _make_doc(
            "d_authoritative",
            lifecycle_status="active",
            project="alpha",
            doc_type="note",
            authority_scope="alpha-domain",
        ),
        "d_failed": _make_doc(
            "d_failed",
            lifecycle_status="active",
            project="alpha",
            doc_type="note",
            pipeline_status=PipelineStatus.FAILED,
        ),
    }
    for doc in docs.values():
        await graph_store.insert_document(doc)
    for short_name, doc in docs.items():
        if doc.pipeline_status == PipelineStatus.FAILED:
            # Failed docs have no chunks; matches the production
            # invariant that chunking is gated on a healthy pipeline.
            continue
        await _index_marker(content_store, embedding_provider, doc)
    return docs


# ---------------------------------------------------------------------------
# T1, T2 — _content_filters() behavior equivalence
# ---------------------------------------------------------------------------


async def test_content_filters_resolves_project_and_lifecycle(
    graph_store, stub_content_store, stub_embedding_provider, filter_pushdown_retrieval_service
):
    """Keyword search with project=alpha + lifecycle_status=active must
    only surface the two docs that satisfy both filters. The completed
    alpha doc and the active beta doc are excluded; the failed doc is
    structurally excluded (no chunks).

    Exercises _content_filters() at retrieval.py:179 end-to-end: filter
    resolution -> document_id allowlist -> chunk-store pre-filter ->
    response."""
    docs = await _seed_mixed_vault(graph_store, stub_content_store, stub_embedding_provider)

    response = await filter_pushdown_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.KEYWORD,
            query="alpha-marker",
            filters=RetrievalFilters(
                project="alpha",
                lifecycle_status="active",
            ),
            limit=10,
        )
    )

    returned_ids = {hit.document.id for hit in response.results}
    assert returned_ids == {docs["d_active_alpha"].id, docs["d_authoritative"].id}


async def test_content_filters_zero_match_short_circuits_with_hints(
    graph_store, stub_content_store, stub_embedding_provider, filter_pushdown_retrieval_service
):
    """When the filter set matches zero docs in SQL, the service must
    short-circuit to an empty response with hints surfacing the active
    filters. has_doc_constraints is True (filters were present) but
    matching ids are empty -> caller sees what filtered out."""
    await _seed_mixed_vault(graph_store, stub_content_store, stub_embedding_provider)

    response = await filter_pushdown_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.KEYWORD,
            query="alpha-marker",
            filters=RetrievalFilters(project="nonexistent-project"),
            limit=10,
        )
    )

    assert response.results == []
    assert response.total_available == 0
    assert response.hints is not None
    assert response.hints.get("total_before_filtering") == 0
    active = response.hints.get("active_filters") or {}
    assert active.get("project") == "nonexistent-project"


# ---------------------------------------------------------------------------
# T3, T4 — _list_filtered() behavior equivalence + AUTHORITATIVE scope
# ---------------------------------------------------------------------------


async def test_list_filtered_returns_filter_matched_hits_excluding_failed(
    graph_store, stub_content_store, stub_embedding_provider, filter_pushdown_retrieval_service
):
    """Keyword query '*' triggers _list_filtered(). With
    lifecycle_status=active, the response must include the three active
    non-failed docs and exclude the completed and failed ones."""
    docs = await _seed_mixed_vault(graph_store, stub_content_store, stub_embedding_provider)

    response = await filter_pushdown_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.KEYWORD,
            query="*",
            filters=RetrievalFilters(lifecycle_status="active"),
            limit=100,
        )
    )

    returned_ids = {hit.document.id for hit in response.results}
    assert returned_ids == {
        docs["d_active_alpha"].id,
        docs["d_active_beta"].id,
        docs["d_authoritative"].id,
    }
    assert docs["d_completed_alpha"].id not in returned_ids
    assert docs["d_failed"].id not in returned_ids


async def test_list_filtered_authoritative_scope_survives_refactor(
    graph_store, stub_content_store, stub_embedding_provider, filter_pushdown_retrieval_service
):
    """``scope=AUTHORITATIVE`` is the one filter that cannot be expressed
    as a SQL column predicate today; the Python post-pass on
    ``authority_scope`` must survive the refactor. Without it, this
    test returns all active docs instead of just the authoritative one."""
    docs = await _seed_mixed_vault(graph_store, stub_content_store, stub_embedding_provider)

    response = await filter_pushdown_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.KEYWORD,
            query="*",
            scope=RetrievalScope.AUTHORITATIVE,
            limit=100,
        )
    )

    returned_ids = {hit.document.id for hit in response.results}
    assert returned_ids == {docs["d_authoritative"].id}


# ---------------------------------------------------------------------------
# T5, T6 — anti-coincidental-pass gates
# ---------------------------------------------------------------------------


async def test_content_filters_does_not_call_list_all_documents(
    graph_store,
    stub_content_store,
    stub_embedding_provider,
    filter_pushdown_retrieval_service,
    monkeypatch,
):
    """Optimization gate for _content_filters(): list_all_documents()
    must never be invoked from this path. Monkeypatched to raise; the
    test passes iff the implementation does not call it.

    If this fails but the equivalence tests still pass, the
    implementation has been silently reverted to the full-vault fetch.
    """
    await _seed_mixed_vault(graph_store, stub_content_store, stub_embedding_provider)

    async def _forbidden(*_args, **_kwargs):
        raise AssertionError(
            "list_all_documents() must not be called from _content_filters() (T-0076)"
        )

    monkeypatch.setattr(graph_store, "list_all_documents", _forbidden)

    response = await filter_pushdown_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.KEYWORD,
            query="alpha-marker",
            filters=RetrievalFilters(
                project="alpha",
                lifecycle_status="active",
            ),
            limit=10,
        )
    )
    # The call returns -- list_all_documents was never invoked.
    # Cross-check via the result set so a future stub that swallows
    # the AssertionError still produces a wrong result here.
    assert response.results, "filter resolution returned an empty allowlist"


async def test_list_filtered_does_not_call_list_all_documents(
    graph_store,
    stub_content_store,
    stub_embedding_provider,
    filter_pushdown_retrieval_service,
    monkeypatch,
):
    """Optimization gate for _list_filtered(): list_all_documents() must
    never be invoked from the keyword '*' path. Same reasoning as
    test_content_filters_does_not_call_list_all_documents."""
    await _seed_mixed_vault(graph_store, stub_content_store, stub_embedding_provider)

    async def _forbidden(*_args, **_kwargs):
        raise AssertionError(
            "list_all_documents() must not be called from _list_filtered() (T-0076)"
        )

    monkeypatch.setattr(graph_store, "list_all_documents", _forbidden)

    response = await filter_pushdown_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.KEYWORD,
            query="*",
            filters=RetrievalFilters(lifecycle_status="active"),
            limit=100,
        )
    )
    assert response.results, "_list_filtered returned an empty hit set"


# ---------------------------------------------------------------------------
# T7, T8 — exclude_terminal_lifecycle across retrieval modes
# ---------------------------------------------------------------------------


async def _seed_retired_pair(graph_store, content_store, embedding_provider):
    """One open and one retired document, both carrying the marker term.

    Deliberately not folded into ``_seed_mixed_vault``: that seed has no
    terminal-state document, and adding one there would change the
    expected id sets of every test above.
    """
    docs = {
        "d_open": _make_doc("d_open_retired_pair", lifecycle_status="active", doc_type="note"),
        "d_retired": _make_doc(
            "d_retired_retired_pair", lifecycle_status="archived", doc_type="note"
        ),
    }
    for doc in docs.values():
        await graph_store.insert_document(doc)
        await _index_marker(content_store, embedding_provider, doc)
    return docs


async def test_terminal_exclusion_holds_on_the_keyword_path(
    graph_store, stub_content_store, stub_embedding_provider, filter_pushdown_retrieval_service
):
    """The exclusion is not a catalog-only affordance.

    The predicate is a negation, which the chunk-row pre-filter cannot
    render -- it emits equality and set membership only. So the flag has
    to travel the non-pushdown route and resolve through the graph
    store into a document-id allowlist. An implementation wired into
    the catalog SQL alone satisfies every count-to-list test and fails
    here, silently widening any keyword or semantic caller that asks
    for the same constraint.
    """
    docs = await _seed_retired_pair(graph_store, stub_content_store, stub_embedding_provider)

    response = await filter_pushdown_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.KEYWORD,
            query="alpha-marker",
            filters=RetrievalFilters(exclude_terminal_lifecycle=True),
            limit=100,
        )
    )

    returned_ids = {hit.document.id for hit in response.results}
    assert docs["d_retired"].id not in returned_ids
    assert docs["d_open"].id in returned_ids

    # Paired arm: without the flag the retired document is reachable, so
    # the exclusion above is the flag's doing and not the seed's.
    unfiltered = await filter_pushdown_retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="alpha-marker", limit=100)
    )
    assert docs["d_retired"].id in {hit.document.id for hit in unfiltered.results}


async def test_terminal_exclusion_is_a_no_op_when_no_state_is_terminal(
    graph_store,
    stub_content_store,
    stub_embedding_provider,
    minimal_vault_config_dict,
):
    """A vault declaring no terminal state excludes nothing.

    The rival this excludes is an implementation that reads the empty
    set as "nothing is admitted" rather than "nothing is excluded" --
    an inversion that turns a vault with no terminal state into a vault
    where every document is retired. It returns nothing here.

    One rival it does **not** exclude, stated because an earlier
    wording claimed otherwise: emitting the clause unconditionally over
    an empty array. `<> ALL('{}')` is true for every row, which the
    adjacent comment in the graph store says outright, so that
    implementation returns both documents and passes. It is wasted SQL
    rather than a defect, and no assertion here can or should separate
    it from the correct one.

    The complement-enumeration rival also passes, for a reason specific
    to this fixture: with no state terminal the complement is every
    declared state, and both seeded documents are in one. The test that
    separates negation from complement is the undeclared-state case
    below, which is where the two actually part.
    """
    # `relocated` is dropped rather than un-flagged: the engine refuses a
    # configuration that clears its terminal flag, so a vault with no
    # terminal state at all is only expressible outside the base floor.
    # Dropping it costs this test nothing -- it is the empty terminal set
    # the test is about, not which states produce it.
    lifecycle = minimal_vault_config_dict["lifecycle"]
    lifecycle["base_states_required"] = False
    lifecycle["states"] = [s for s in lifecycle["states"] if s["value"] != "relocated"]
    lifecycle["transitions"] = [t for t in lifecycle["transitions"] if t["action"] != "relocate"]
    for state in lifecycle["states"]:
        state.pop("is_terminal", None)
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    assert config.lifecycle.terminal_states() == frozenset()

    service = RetrievalService(
        graph_store=graph_store,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        config=config,
    )
    docs = await _seed_retired_pair(graph_store, stub_content_store, stub_embedding_provider)

    response = await service.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            filters=RetrievalFilters(exclude_terminal_lifecycle=True),
            limit=100,
        )
    )

    returned_ids = {hit.document.id for hit in response.results}
    assert returned_ids >= {docs["d_open"].id, docs["d_retired"].id}


async def test_terminal_exclusion_is_named_in_the_empty_result_hints(
    graph_store, stub_content_store, stub_embedding_provider, filter_pushdown_retrieval_service
):
    """A caller culled by the exclusion is told which constraint culled them.

    Every other filter reaches ``active_filters``, and the hint block
    exists so an empty response is distinguishable from a true zero. A
    filter missing from it produces the worse of the two failures: a
    caller sees "nothing matched" alongside a filter list that does not
    explain the emptiness, and reads the vault as empty.

    Reported as the flag rather than as the states it resolved to. The
    caller named a rule; the states are the vault's and are not
    something the caller asked for or can be held to.
    """
    await _seed_retired_pair(graph_store, stub_content_store, stub_embedding_provider)

    response = await filter_pushdown_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.KEYWORD,
            query="alpha-marker",
            filters=RetrievalFilters(
                exclude_terminal_lifecycle=True,
                project="nonexistent-project",
            ),
            limit=10,
        )
    )

    assert response.results == []
    assert response.hints is not None
    active = response.hints.get("active_filters") or {}
    assert active.get("exclude_terminal_lifecycle") is True
    assert "exclude_lifecycle_statuses" not in active


async def test_terminal_exclusion_narrows_the_resolution_not_just_the_output(
    graph_store, stub_content_store, stub_embedding_provider, filter_pushdown_retrieval_service
):
    """The exclusion has to reach the SQL, not only the Python gate.

    The sibling keyword test above cannot see the difference. Both
    ``_content_filters``' document-id resolution and ``_passes_scope``
    stand between a retired document and the caller, so an
    implementation that adds the post-filter and never touches the
    query returns the same rows -- having fetched, ranked, and then
    thrown away chunks that could not survive. That is the shape a
    filter silently stops being a filter in.

    A corpus whose every match is retired makes the two visible. When
    the exclusion reaches SQL the resolution comes back empty and the
    chunk search never runs, so ``total_before_filtering`` is 0. When it
    reaches only the post-filter the chunks are fetched and then culled,
    and the same field reports how many.
    """
    doc = _make_doc("d_only_retired", lifecycle_status="archived", doc_type="note")
    await graph_store.insert_document(doc)
    await _index_marker(stub_content_store, stub_embedding_provider, doc)

    response = await filter_pushdown_retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.KEYWORD,
            query="alpha-marker",
            filters=RetrievalFilters(exclude_terminal_lifecycle=True),
            limit=10,
        )
    )

    assert response.results == []
    assert response.hints is not None
    assert response.hints.get("total_before_filtering") == 0, (
        "the exclusion was applied after the chunk search rather than as a "
        "constraint on it; the filter never reached the query"
    )


async def test_a_state_the_config_no_longer_declares_survives_the_exclusion(
    graph_store, stub_content_store, stub_embedding_provider, filter_pushdown_retrieval_service
):
    """The one property that separates negation from complement enumeration.

    The predicate drops the states the vault calls terminal. The rival
    admits the states it calls non-terminal, and on every document either
    vault config describes the two agree. They part on a document whose
    stored state the config does not list at all -- a state retired from
    the config after documents had come to rest in it, which the store
    permits because ``lifecycle_status`` is a plain column. The negation
    keeps that document, because its state is not among the excluded
    ones. The complement drops it, because its state is not among the
    admitted ones, and drops it silently: the caller asked to exclude
    retired documents and lost an unretired one.

    This is the property both code comments and the filter's own
    description claim, and until this test nothing held them to it. Every
    other test in this file seeds declared states, where the two
    implementations agree, so a complement rival passes all of them.
    """
    declared = {state.value for state in filter_pushdown_retrieval_service._config.lifecycle.states}
    orphan_state = "retired_from_config"
    assert orphan_state not in declared, "the state must be one the config does not list"

    orphan = _make_doc("d_orphan_state", doc_type="note")
    orphan.lifecycle_status = orphan_state
    await graph_store.insert_document(orphan)
    await _index_marker(stub_content_store, stub_embedding_provider, orphan)

    retired = _make_doc("d_orphan_control", lifecycle_status="archived", doc_type="note")
    await graph_store.insert_document(retired)
    await _index_marker(stub_content_store, stub_embedding_provider, retired)

    for mode, query in ((RetrievalMode.CATALOG, None), (RetrievalMode.KEYWORD, "alpha-marker")):
        response = await filter_pushdown_retrieval_service.discover(
            DiscoverRequest(
                mode=mode,
                query=query,
                filters=RetrievalFilters(exclude_terminal_lifecycle=True),
                limit=100,
            )
        )
        returned = {hit.document.id for hit in response.results}
        assert orphan.id in returned, (
            f"{mode.value}: a document in an undeclared state was dropped; the exclusion "
            f"is admitting the declared non-terminal states rather than excluding the "
            f"terminal ones"
        )
        # The paired control: the exclusion is working in this same
        # response, so the survival above is the predicate's doing and
        # not an exclusion that failed to run at all.
        assert retired.id not in returned, f"{mode.value}: the terminal document was not excluded"
