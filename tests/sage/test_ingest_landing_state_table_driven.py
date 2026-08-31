"""The ingest landing state is read from the vault's transition table.

The vault config declares the ingestion transition as a `(new)` row —
`(new) -> ingest -> <state>` — but `(new)` rows are deliberately dropped
from the user-invocable transition table on construction. The landing
state must nonetheless be readable: a vault that declares a non-`active`
landing state gets it, and a config surface that appears settable but is
ignored is a defect.

These tests pin three properties:

- `TransitionTable.ingest_landing_state` reports the configured `(new)`
  row's target, and raises for a table built without one rather than
  substituting a state the vault never declared;
- reading the row does not make it invocable: `ingest` stays unknown to
  the user-facing action surface;
- `IngestionService.ingest` lands new documents in the configured state
  end to end, under both the base lifecycle and a divergent one.
"""

import copy

import pytest

from sage.config import LifecycleTransition, TransitionTable, VaultConfig, build_transition_table
from sage.models.enums import SourceType
from sage.models.schemas import IngestRequest
from sage.services.ingestion import IngestionService
from sage.services.lifecycle import LifecycleService
from sage.source_adapters.markdown_adapter import MarkdownAdapter


def _config_landing_ingest_in(config_dict: dict, state: str) -> VaultConfig:
    """Return a config whose `(new)` row lands ingest in `state`.

    Declares the state, rewrites the `(new)` row's target, and adds an
    `activate` transition out of it so the state is not a dead end. The
    base states and actions stay declared.
    """
    mutated = copy.deepcopy(config_dict)
    mutated["lifecycle"]["states"].append({"value": state, "label": state.title()})
    for transition in mutated["lifecycle"]["transitions"]:
        if transition["from_state"] == "(new)":
            transition["to_state"] = state
    mutated["lifecycle"]["transitions"].append(
        {"from_state": state, "action": "activate", "to_state": "active"}
    )
    return VaultConfig.model_validate(mutated)


def _seed_file(tmp_vault_dir, relative: str, content: str):
    full = tmp_vault_dir / "sources" / relative
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    return full


def _build_services(
    config,
    graph_store,
    lock_manager,
    content_store,
    embedding_provider,
    abstraction_provider,
):
    """Build a lifecycle + ingestion pair sharing one config object."""
    lifecycle = LifecycleService(graph_store, lock_manager, config, content_store)
    ingestion = IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=content_store,
        embedding_provider=embedding_provider,
        abstraction_provider=abstraction_provider,
        config=config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
        lifecycle_service=lifecycle,
    )
    return lifecycle, ingestion


def test_ingest_landing_state_default(minimal_config):
    """Under the base lifecycle the landing state is `active`."""
    table = build_transition_table(minimal_config)
    assert table.ingest_landing_state() == "active"


def test_ingest_landing_state_reads_configured_new_row(minimal_vault_config_dict):
    """A configured non-`active` landing state is reported.

    `draft` is unproducible by a hardcoded `active`, so this fails against
    an implementation that never reads the `(new)` row.
    """
    config = _config_landing_ingest_in(minimal_vault_config_dict, "draft")
    table = build_transition_table(config)
    assert table.ingest_landing_state() == "draft"


def test_ingest_landing_state_requires_new_row():
    """A table built without a `(new)` row has no landing state to report.

    Only reachable by direct construction; a validated config always
    carries the row. Raising beats guessing: a substituted default could
    land documents in a state the vault never declared, leaving them
    untransitionable.
    """
    table = TransitionTable(
        [
            LifecycleTransition(from_state="active", action="archive", to_state="archived"),
        ]
    )
    with pytest.raises(ValueError, match=r"\(new\)"):
        table.ingest_landing_state()


def test_capturing_landing_state_keeps_ingest_uninvocable(minimal_vault_config_dict):
    """Reading the `(new)` row must not reintroduce it as invocable.

    An implementation that stopped skipping `(new)` rows in order to read
    them would register `ingest` as a known action and `(new)` as an
    occupiable state; all three assertions trip on that.
    """
    config = _config_landing_ingest_in(minimal_vault_config_dict, "draft")
    table = build_transition_table(config)

    assert table.is_known_action("ingest") is False
    assert table.states_allowing("ingest") == []
    assert table.get_valid_actions("draft") == ["activate"]


async def test_ingest_lands_in_configured_state_end_to_end(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_vault_config_dict,
):
    """A vault landing ingest in `draft` gets `draft` documents.

    This is the defect the hardcode caused: the `(new)` row is configured
    and readable, but a literal `active` ignored it. Fails against an
    implementation whose accessor exists but is unused at the call site.
    """
    config = _config_landing_ingest_in(minimal_vault_config_dict, "draft")
    _, ingestion = _build_services(
        config,
        graph_store,
        lock_manager,
        stub_content_store,
        stub_embedding_provider,
        stub_abstraction_provider,
    )

    _seed_file(tmp_vault_dir, "landing_draft.md", "# Draft landing\n\nBody.")
    result = await ingestion.ingest(
        IngestRequest(source="landing_draft.md", source_type=SourceType.MARKDOWN)
    )

    stored = await graph_store.get_document(result.document.id)
    assert stored.lifecycle_status == "draft"


async def test_ingest_lands_in_active_under_base_lifecycle(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_config,
):
    """Under the base lifecycle a new document lands in `active`.

    Names the invariant previous suites only asserted incidentally, so a
    table-driven rewrite that mis-reads the base `(new)` row is caught
    here rather than in an unrelated test's setup.
    """
    _, ingestion = _build_services(
        minimal_config,
        graph_store,
        lock_manager,
        stub_content_store,
        stub_embedding_provider,
        stub_abstraction_provider,
    )

    _seed_file(tmp_vault_dir, "landing_active.md", "# Active landing\n\nBody.")
    result = await ingestion.ingest(
        IngestRequest(source="landing_active.md", source_type=SourceType.MARKDOWN)
    )

    stored = await graph_store.get_document(result.document.id)
    assert stored.lifecycle_status == "active"
