"""The supersede precondition is derived from the vault's transition table.

`IngestionService.ingest` runs a pre-projection fail-fast on the supersede
predecessor's lifecycle state. The fail-fast exists so that pipeline work
stays behind cheap validity checks, and it must agree with the vault's
configured lifecycle transition table by construction rather than by
coincidence: a vault that declares `supersede` from a state other than
`active` gets a transition the table honours, and the fail-fast must not
reject it before the table is consulted.

These tests pin three properties:

- the predicate is table-derived, so a configured non-`active` supersede
  transition works end to end;
- the fail-fast still short-circuits before projection, so the performance
  property that motivated it survives the correctness fix;
- every enforcement point on the ingest surface reports one error code for
  one condition.
"""

import copy
from types import SimpleNamespace

import pytest

from sage.api.errors import (
    InvalidLifecycleTransitionError,
    SupersedeTargetNotActiveError,
)
from sage.config import (
    LifecycleTransition,
    TransitionTable,
    VaultConfig,
    build_transition_table,
)
from sage.models.enums import RationaleKind, SourceType
from sage.models.schemas import IngestRequest, SetLifecycleRequest
from sage.services.ingestion import IngestionService
from sage.services.lifecycle import LifecycleService
from sage.source_adapters.markdown_adapter import MarkdownAdapter


def _seed_file(tmp_vault_dir, relative: str, content: str):
    full = tmp_vault_dir / "sources" / relative
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    return full


class _ProjectionSpy:
    """Wraps a source adapter and counts `project()` calls.

    Used to prove the predecessor fail-fast short-circuits before Stage 1
    projection rather than merely returning the right error afterwards.
    """

    def __init__(self, inner):
        self._inner = inner
        self.project_calls = 0

    async def project(self, path, config):
        self.project_calls += 1
        return await self._inner.project(path, config)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _config_allowing_supersede_from(config_dict: dict, *extra_states: str) -> VaultConfig:
    """Return a config whose table permits `supersede` from extra states.

    The base dict already declares `active -> supersede -> archived`; each
    extra state gets its own supersede row landing in `archived`.
    """
    mutated = copy.deepcopy(config_dict)
    for state in extra_states:
        mutated["lifecycle"]["transitions"].append(
            {
                "from_state": state,
                "action": "supersede",
                "to_state": "archived",
                "creates_edge": "supersedes",
            }
        )
    return VaultConfig.model_validate(mutated)


def _build_services(
    config,
    graph_store,
    lock_manager,
    content_store,
    embedding_provider,
    abstraction_provider,
    adapter=None,
):
    """Build a lifecycle + ingestion pair from one config object.

    Both services must share the same config: an ingestion service built
    from a different config than its lifecycle service would exercise two
    different transition tables and make a table-driven assertion
    meaningless.
    """
    lifecycle = LifecycleService(graph_store, lock_manager, config, content_store)
    ingestion = IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=content_store,
        embedding_provider=embedding_provider,
        abstraction_provider=abstraction_provider,
        config=config,
        source_adapters={SourceType.MARKDOWN: adapter or MarkdownAdapter()},
        lifecycle_service=lifecycle,
    )
    return lifecycle, ingestion


def test_states_allowing_returns_configured_from_states(minimal_vault_config_dict):
    """`states_allowing` reports exactly the states a table permits.

    Asserted against a strict subset of the declared states: an
    implementation that returned every declared state, or every state
    with any transition at all, would pass a single-state assertion but
    fails this one. `archived` declares `reactivate` and no supersede,
    so it must not appear.
    """
    config = _config_allowing_supersede_from(minimal_vault_config_dict, "completed")
    table = build_transition_table(config)

    assert table.states_allowing("supersede") == ["active", "completed"]
    assert table.states_allowing("reactivate") == ["archived"]
    assert table.states_allowing("no_such_action") == []


def test_states_allowing_excludes_the_ingest_pseudo_state(minimal_vault_config_dict):
    """The `(new)` row is not a state a document can be in.

    `TransitionTable` drops `(new)` rows on construction because they are
    not user-invocable; `states_allowing` inherits that and must never
    offer `(new)` as a state a caller could transition from.

    Anti-coincidental-pass: both `(new)` assertions are satisfied by an
    implementation that returns an empty list for every action, so they
    do not discriminate on their own. The non-empty control below is what
    excludes that rival and makes this a gate rather than a restatement
    of emptiness.
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    table = build_transition_table(config)

    assert table.states_allowing("supersede") == ["active"], (
        "control: a table reporting nothing for every action would "
        "satisfy the `(new)`-absence assertions vacuously"
    )
    assert table.states_allowing("ingest") == []
    assert "(new)" not in table.states_allowing("supersede")


def test_landing_states_reports_every_state_the_action_lands_in():
    """`landing_states` derives the arrival set from the table.

    Two supersede rows landing in different states must both be
    reported. Asserting set equality on a two-landing table excludes an
    implementation that hardcodes `archived`, and the `reactivate` row
    excludes one that pools the `to_state` of every action.
    """
    table = TransitionTable(
        [
            LifecycleTransition(
                from_state="active",
                action="supersede",
                to_state="archived",
                creates_edge="supersedes",
            ),
            LifecycleTransition(
                from_state="completed",
                action="supersede",
                to_state="retired",
                creates_edge="supersedes",
            ),
            LifecycleTransition(
                from_state="archived",
                action="reactivate",
                to_state="active",
                creates_edge=None,
            ),
        ]
    )

    assert table.landing_states("supersede") == {"archived", "retired"}
    assert table.landing_states("reactivate") == {"active"}
    assert table.landing_states("no_such_action") == set()


async def test_supersede_from_table_permitted_non_active_state_succeeds(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_vault_config_dict,
):
    """A vault that declares `completed -> supersede -> archived` gets it.

    This is the defect the hardcoded fail-fast caused: the transition is
    configured and the table would honour it, but a literal `!= "active"`
    check rejected the request before the table was ever consulted. Fails
    against the pre-fix tree with `supersede_target_not_active`.
    """
    config = _config_allowing_supersede_from(minimal_vault_config_dict, "completed")
    lifecycle, ingestion = _build_services(
        config,
        graph_store,
        lock_manager,
        stub_content_store,
        stub_embedding_provider,
        stub_abstraction_provider,
    )

    _seed_file(tmp_vault_dir, "tdp_v1.md", "# V1\n\nOriginal.")
    _seed_file(tmp_vault_dir, "tdp_v2.md", "# V2\n\nRevised.")

    v1 = await ingestion.ingest(IngestRequest(source="tdp_v1.md", source_type=SourceType.MARKDOWN))
    await lifecycle._set_lifecycle(v1.document.id, SetLifecycleRequest(action="complete"))

    predecessor = await graph_store.get_document(v1.document.id)
    assert predecessor.lifecycle_status == "completed", (
        "precondition: the predecessor must rest at `completed` for this test "
        "to exercise the non-active supersede path"
    )

    v2 = await ingestion.ingest(
        IngestRequest(
            source="tdp_v2.md",
            source_type=SourceType.MARKDOWN,
            predecessor_id=v1.document.id,
        )
    )

    edges = await graph_store.get_edges_by_source(v2.document.id)
    supersedes = [e for e in edges if e.edge_type == "supersedes"]
    assert len(supersedes) == 1
    assert supersedes[0].target_id == v1.document.id

    flipped = await graph_store.get_document(v1.document.id)
    assert flipped.lifecycle_status == "archived"


async def test_fail_fast_short_circuits_before_projection(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_vault_config_dict,
):
    """The predecessor check runs before Stage 1 projection.

    The fail-fast exists for pipeline cost, not just for correctness, so
    a correctness fix that moved the check after projection would satisfy
    every other test in this file while giving up the property that
    motivated the check.

    Anti-coincidental-pass: the positive control is what makes the
    zero-call assertion mean anything — a spy never wired into
    `source_adapters` reports zero calls against any implementation.

    Scope: this pins the check ahead of *projection* only. A check placed
    after source retention but still before projection would pass here,
    so the ordering of the check against `retain_source` is not gated by
    this test.
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    spy = _ProjectionSpy(MarkdownAdapter())
    lifecycle, ingestion = _build_services(
        config,
        graph_store,
        lock_manager,
        stub_content_store,
        stub_embedding_provider,
        stub_abstraction_provider,
        adapter=spy,
    )

    _seed_file(tmp_vault_dir, "ffs_v1.md", "# V1\n\nOriginal.")
    _seed_file(tmp_vault_dir, "ffs_v2.md", "# V2\n\nRevised.")

    v1 = await ingestion.ingest(IngestRequest(source="ffs_v1.md", source_type=SourceType.MARKDOWN))
    assert spy.project_calls == 1, "positive control: a real ingest must project"

    await lifecycle._set_lifecycle(v1.document.id, SetLifecycleRequest(action="archive"))
    calls_before = spy.project_calls

    with pytest.raises(SupersedeTargetNotActiveError):
        await ingestion.ingest(
            IngestRequest(
                source="ffs_v2.md",
                source_type=SourceType.MARKDOWN,
                predecessor_id=v1.document.id,
            )
        )

    assert spy.project_calls == calls_before, "the rejected supersede must not have run projection"


async def test_rejection_reports_table_derived_allowed_states_default_table(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_vault_config_dict,
):
    """Under the default table the detail still reads `active`.

    Backward-compatibility guard: existing callers key remediation prose
    off `required_state`, and every vault in service declares supersede
    only from `active`.
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    lifecycle, ingestion = _build_services(
        config,
        graph_store,
        lock_manager,
        stub_content_store,
        stub_embedding_provider,
        stub_abstraction_provider,
    )

    _seed_file(tmp_vault_dir, "dt_v1.md", "# V1\n\nOriginal.")
    _seed_file(tmp_vault_dir, "dt_v2.md", "# V2\n\nRevised.")

    v1 = await ingestion.ingest(IngestRequest(source="dt_v1.md", source_type=SourceType.MARKDOWN))
    await lifecycle._set_lifecycle(v1.document.id, SetLifecycleRequest(action="archive"))

    with pytest.raises(SupersedeTargetNotActiveError) as exc_info:
        await ingestion.ingest(
            IngestRequest(
                source="dt_v2.md",
                source_type=SourceType.MARKDOWN,
                predecessor_id=v1.document.id,
            )
        )

    err = exc_info.value
    assert err.status_code == 409
    assert err.detail["predecessor_id"] == v1.document.id
    assert err.detail["current_state"] == "archived"
    assert err.detail["required_state"] == "active"
    assert err.detail["allowed_states"] == ["active"]


async def test_rejection_reports_table_derived_allowed_states_custom_table(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_vault_config_dict,
):
    """A table permitting two source states reports both.

    This is the load-bearing case for the table-derived predicate:
    `"active or completed"` cannot be produced by the hardcoded literal
    the fix removes, so it cannot pass coincidentally.
    """
    config = _config_allowing_supersede_from(minimal_vault_config_dict, "completed")
    lifecycle, ingestion = _build_services(
        config,
        graph_store,
        lock_manager,
        stub_content_store,
        stub_embedding_provider,
        stub_abstraction_provider,
    )

    _seed_file(tmp_vault_dir, "ct_v1.md", "# V1\n\nOriginal.")
    _seed_file(tmp_vault_dir, "ct_v2.md", "# V2\n\nRevised.")

    v1 = await ingestion.ingest(IngestRequest(source="ct_v1.md", source_type=SourceType.MARKDOWN))
    await lifecycle._set_lifecycle(v1.document.id, SetLifecycleRequest(action="archive"))

    with pytest.raises(SupersedeTargetNotActiveError) as exc_info:
        await ingestion.ingest(
            IngestRequest(
                source="ct_v2.md",
                source_type=SourceType.MARKDOWN,
                predecessor_id=v1.document.id,
            )
        )

    err = exc_info.value
    assert err.detail["current_state"] == "archived"
    assert err.detail["allowed_states"] == ["active", "completed"]
    assert err.detail["required_state"] == "active or completed"


async def test_prepare_supersede_raises_supersede_target_not_active(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_vault_config_dict,
):
    """`prepare_supersede` reports the ingest surface's code.

    It is reached only from the ingest path, under the predecessor lock,
    and fires when a racer changed the predecessor's state after the
    fail-fast passed. Reporting a different code than the fail-fast for
    the same condition would make the caller-visible error depend on
    timing.
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    lifecycle, ingestion = _build_services(
        config,
        graph_store,
        lock_manager,
        stub_content_store,
        stub_embedding_provider,
        stub_abstraction_provider,
    )

    _seed_file(tmp_vault_dir, "ps_v1.md", "# V1\n\nOriginal.")
    v1 = await ingestion.ingest(IngestRequest(source="ps_v1.md", source_type=SourceType.MARKDOWN))
    await lifecycle._set_lifecycle(v1.document.id, SetLifecycleRequest(action="archive"))
    archived = await graph_store.get_document(v1.document.id)

    with pytest.raises(SupersedeTargetNotActiveError) as exc_info:
        lifecycle.prepare_supersede(archived, "00000000_successor")

    err = exc_info.value
    assert err.status_code == 409
    assert err.detail["predecessor_id"] == v1.document.id
    assert err.detail["current_state"] == "archived"


def test_prepare_supersede_carries_caller_edge_provenance():
    """The built edge carries caller-supplied rationale and kind.

    An auto-inference caller committing the transition atomically stamps
    its provenance on the supersedes edge here, because the chain-repair
    provenance gate reads the typed `rationale_kind` column: an edge that
    landed with the default `manual` kind would downgrade every future
    repair of its chain to staging. Omitting the kwargs must preserve
    the prior defaults — that is the existing single-ingest contract.
    """
    svc = LifecycleService.__new__(LifecycleService)
    svc._table = TransitionTable(
        [
            LifecycleTransition(
                from_state="active",
                action="supersede",
                to_state="archived",
                creates_edge="supersedes",
            )
        ]
    )
    predecessor = SimpleNamespace(id="0000000b_pred", lifecycle_status="active")

    stamped = svc.prepare_supersede(
        predecessor,
        "0000000a_succ",
        rationale="[version_chain] v2 supersedes v1",
        rationale_kind=RationaleKind.VERSION_CHAIN,
    )
    assert stamped.edge.rationale == "[version_chain] v2 supersedes v1"
    assert stamped.edge.rationale_kind is RationaleKind.VERSION_CHAIN
    assert stamped.edge.source_id == "0000000a_succ"
    assert stamped.edge.target_id == "0000000b_pred"
    assert stamped.predecessor_updates["lifecycle_status"] == "archived"

    default = svc.prepare_supersede(predecessor, "0000000a_succ")
    assert default.edge.rationale is None
    assert default.edge.rationale_kind is RationaleKind.MANUAL


async def test_force_reingest_supersede_surfaces_the_ingest_surface_code(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_vault_config_dict,
):
    """The force-reingest branch reports the same code as the other two.

    Force-reingest supersedes through `_set_lifecycle` rather than
    `prepare_supersede`. `_set_lifecycle` is the `update_lifecycles`
    surface and raises `invalid_lifecycle_transition`; on the ingest
    surface that rejection must carry the ingest surface's code, or the
    caller-visible error for one condition depends on which branch ran.

    The rejection is injected rather than driven end to end. Reaching
    this call requires a predecessor the fail-fast accepted, and for
    such a predecessor `_set_lifecycle` would succeed — the branch is
    reachable only when a racer flips the state in the window between
    the two, which is not deterministically stageable. Injecting the
    exception the racer would cause tests the translation seam itself;
    an end-to-end variant would pass off the fail-fast and never
    execute the code under test.
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    lifecycle, ingestion = _build_services(
        config,
        graph_store,
        lock_manager,
        stub_content_store,
        stub_embedding_provider,
        stub_abstraction_provider,
    )

    _seed_file(tmp_vault_dir, "fr_v1.md", "# V1\n\nOriginal.")
    _seed_file(tmp_vault_dir, "fr_other.md", "# Other\n\nDistinct content.")

    v1 = await ingestion.ingest(IngestRequest(source="fr_v1.md", source_type=SourceType.MARKDOWN))
    other = await ingestion.ingest(
        IngestRequest(source="fr_other.md", source_type=SourceType.MARKDOWN)
    )

    calls: list[str] = []

    async def _raise_as_racer(doc_id, request):
        calls.append(doc_id)
        raise InvalidLifecycleTransitionError(
            "archived", "supersede", ["reactivate"], pipeline_status=None
        )

    lifecycle._set_lifecycle = _raise_as_racer

    # force=True plus a content hash already held by `other` selects the
    # force-reingest branch; the predecessor's hash differs, so the
    # identical-content guard does not fire first.
    with pytest.raises(SupersedeTargetNotActiveError) as exc_info:
        await ingestion.ingest(
            IngestRequest(
                source="fr_other.md",
                source_type=SourceType.MARKDOWN,
                predecessor_id=v1.document.id,
                force=True,
            )
        )

    assert calls == [v1.document.id], (
        "the force-reingest branch must have been the path under test; "
        "an empty list means the rejection came from somewhere else"
    )
    assert exc_info.value.detail["predecessor_id"] == v1.document.id
    assert other.document.id  # the hash-match record the force branch reused
