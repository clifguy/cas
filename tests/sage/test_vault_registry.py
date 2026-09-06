"""Closure-pair tests for ``VaultRegistryService._build_vault_summary``.

These install the second half of the F4 closure pair for the
``VaultConfig -> VaultSummary`` projection (plus its three sub-models
``VaultDocTypeEntry``, ``VaultLifecycleState``, ``VaultAdapterInfo``)
at ``sage/services/vault_registry.py``. ``_build_vault_summary`` is the
single owning factory; this module adds the exhaustive-fields test that
fails closed when a field is added to any of the four destination models
but is not wired through the factory.

Per the *CAS Projection-Point Audit Conventions* steering document
(cas vault, doc_type=steering_document), every projection point owes a
closure pair: a single owning factory and an exhaustive-fields test
keyed on ``model_fields.items()`` so the assertion grows automatically
with the model.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import psycopg
from pydantic_core import PydanticUndefined

from sage.adapters.stubs import StubAbstractionProvider
from sage.config import (
    DocTypeEntry,
    DocumentTypesConfig,
    LifecycleConfig,
    LifecycleState,
    LifecycleTransition,
    VaultConfig,
    VaultIdentity,
)
from sage.models.enums import SourceType
from sage.models.schemas import (
    VaultAdapterInfo,
    VaultDocTypeEntry,
    VaultLifecycleState,
    VaultSummary,
)
from sage.services.vault_registry import VaultRegistryService
from sage.vault_source_binding import DiscoveredVault


class _SentinelAdapter:
    """Minimal ``SourceAdapter`` stand-in exposing ``EXTENSIONS``.

    ``_build_vault_summary`` reads ``adapter.EXTENSIONS`` for each entry
    in ``services.ingestion_service.registered_adapters`` to populate
    ``VaultAdapterInfo.extensions``. The list is non-empty so a
    regression that drops the field would trip the exhaustive-fields
    truthiness check rather than passing on an empty default.
    """

    EXTENSIONS: list[str] = [".md", ".markdown"]


class _SentinelIngestionService:
    """Stand-in for ``services.ingestion_service`` with one registered
    adapter so the ``adapters`` sub-collection is non-empty (otherwise
    ``VaultAdapterInfo.model_fields`` would not be exercised)."""

    registered_adapters: dict[SourceType, Any] = {SourceType.MARKDOWN: _SentinelAdapter()}


class _SentinelServices:
    """Minimal ``SAGEServices`` stand-in covering the single attribute
    ``_build_vault_summary`` reads off ``services`` — ``ingestion_service``.
    No other ``SAGEServices`` attributes are touched by the factory."""

    ingestion_service = _SentinelIngestionService()


def _vault_config_with_every_summary_field() -> VaultConfig:
    """Build a ``VaultConfig`` whose every sub-section consumed by
    ``_build_vault_summary`` has at least one non-default entry, and
    whose fields surfaced on ``VaultSummary`` (or its sub-models) are
    set to distinct non-default sentinels.

    Anti-coincidental-pass discipline:

    - ``vault.description`` is set to a non-empty string (the field
      defaults to ``None`` on ``VaultIdentity``; a factory that omits
      it would leave ``VaultSummary.description=None`` and pass a naive
      ``is not None`` check only because of the sentinel value).
    - ``lifecycle.states`` contains exactly one ``LifecycleState`` with
      ``is_terminal=True`` (the field defaults to ``False``; the
      sentinel forces a regression that hard-codes ``False`` to be
      detectable, since the sub-model truthiness check on
      ``VaultLifecycleState.is_terminal`` requires the value-not-None
      branch and the lifecycle-state assertion below additionally
      asserts ``is_terminal is True``).
    - ``document_types.doc_types`` contains exactly one ``DocTypeEntry``
      with a non-default ``label`` so ``VaultDocTypeEntry.label`` is
      observably populated.
    - ``adapter_defaults`` is a non-empty dict (the field is ``dict``
      typed on ``VaultConfig``); the factory does not iterate it, but
      the sentinel proves the section is settable. Adapter *availability*
      is process-wide (CAS-ADR-046), so nothing in the summary's adapter
      list derives from vault config.
    """
    return VaultConfig(
        vault=VaultIdentity(
            id="sentinel_vault",
            name="Sentinel Vault",
            description="sentinel description for closure-pair test",
            owner="sentinel_owner",
            storage_root="/tmp/sentinel/sources",
            brain_root="/tmp/sentinel/brain",
            visibility="personal",
        ),
        document_types=DocumentTypesConfig(
            doc_types=[
                DocTypeEntry(
                    value="sentinel_doc_type",
                    label="Sentinel Doc Type",
                    description="sentinel doc type description",
                ),
            ],
        ),
        lifecycle=LifecycleConfig(
            states=[
                LifecycleState(
                    value="sentinel_state",
                    label="Sentinel State",
                    description="sentinel lifecycle state description",
                    is_terminal=True,
                    satisfies_dependency=True,
                ),
            ],
            transitions=[
                LifecycleTransition(
                    from_state="(new)",
                    action="ingest",
                    to_state="sentinel_state",
                ),
            ],
            base_states_required=False,
        ),
        adapter_defaults={"docx": {"heading_style_map": {"Sentinel Style": 1}}},
        metadata_extraction={"filename_extraction": {"separator": "_"}},
        edge_inference={"tier_assignments": []},
    )


def test_build_vault_summary_populates_every_vault_summary_field():
    """F4 closure pair, T1: every ``VaultSummary`` field is
    populated by ``_build_vault_summary`` from a sentinel ``VaultConfig``
    whose every consumed sub-section has at least one non-default entry.

    The loop iterates ``VaultSummary.model_fields.items()`` so the
    assertion grows automatically when a field is added to
    ``VaultSummary``; if the new field is not wired through
    ``_build_vault_summary``, the loop trips the assertion. Sub-model
    exhaustive-fields assertions follow on the first element of each
    sub-collection (``doc_types``, ``lifecycle_states``, ``adapters``)
    so a field added to ``VaultDocTypeEntry``, ``VaultLifecycleState``,
    or ``VaultAdapterInfo`` is caught by the same closure pair (per the
    ticket's decision to bundle the three sub-models under the parent
    factory's closure rather than instrument each individually).
    """
    config = _vault_config_with_every_summary_field()
    services = _SentinelServices()
    projects = ["sentinel_project_alpha", "sentinel_project_beta"]

    summary = VaultRegistryService._build_vault_summary(
        config, services, projects, document_count=11
    )

    # The count is caller-supplied, so a factory that dropped it would
    # surface a zero -- which the int branch below refuses -- but a factory
    # that wired the wrong source would not; pin the sentinel value too.
    assert summary.document_count == 11
    assert "storage_root" not in VaultSummary.model_fields

    # ---- VaultSummary: every field non-empty / non-None ----
    # Three-branch closure-test idiom: list/dict-annotation branch
    # catches empty/falsy defaults; non-None-default-scalar branch catches
    # coincidental passes where Pydantic supplies the default and the value
    # would still satisfy ``is not None``; else falls back to non-None.
    for field_name, field_info in VaultSummary.model_fields.items():
        value = getattr(summary, field_name)
        annotation = field_info.annotation
        default = field_info.default
        if (
            annotation == list[str]
            or annotation == list[VaultDocTypeEntry]
            or annotation == list[VaultLifecycleState]
            or annotation == list[VaultAdapterInfo]
            or annotation == (dict | None)
        ):
            assert value, (
                f"VaultSummary.{field_name} not populated by _build_vault_summary "
                "(empty/falsy default would pass a naive 'is not None' check)"
            )
        elif annotation is int:
            assert value != 0, (
                f"VaultSummary.{field_name} is zero — a required int that the "
                "factory hard-codes or drops would pass a naive 'is not None' check"
            )
        elif default is not PydanticUndefined and default is not None:
            assert value != default, (
                f"VaultSummary.{field_name} matches its default ({default!r}) — "
                "_build_vault_summary may have dropped this field (coincidental "
                "pass: Pydantic supplies the default and 'is not None' would still pass)"
            )
        else:
            assert value is not None, (
                f"VaultSummary.{field_name} not populated by _build_vault_summary"
            )

    # ---- VaultDocTypeEntry: every field populated on first element ----
    assert summary.doc_types, "summary.doc_types empty — sub-model test cannot run"
    first_doc_type = summary.doc_types[0]
    for field_name, field_info in VaultDocTypeEntry.model_fields.items():
        value = getattr(first_doc_type, field_name)
        default = field_info.default
        if default is not PydanticUndefined and default is not None:
            assert value != default, (
                f"VaultDocTypeEntry.{field_name} matches its default ({default!r}) — "
                "_build_vault_summary may have dropped this field (coincidental pass)"
            )
        else:
            assert value is not None, (
                f"VaultDocTypeEntry.{field_name} not populated by _build_vault_summary"
            )

    # ---- VaultLifecycleState: every field populated on first element ----
    # The ``is_terminal`` field defaults to ``False`` and the sentinel sets it
    # ``True``; the non-None-default-scalar branch catches a factory
    # regression where Pydantic would supply the default and the prior
    # ``is not None`` idiom would pass coincidentally.
    assert summary.lifecycle_states, "summary.lifecycle_states empty — sub-model test cannot run"
    first_state = summary.lifecycle_states[0]
    for field_name, field_info in VaultLifecycleState.model_fields.items():
        value = getattr(first_state, field_name)
        default = field_info.default
        if default is not PydanticUndefined and default is not None:
            assert value != default, (
                f"VaultLifecycleState.{field_name} matches its default ({default!r}) — "
                "_build_vault_summary may have dropped this field (coincidental pass)"
            )
        else:
            assert value is not None, (
                f"VaultLifecycleState.{field_name} not populated by _build_vault_summary"
            )

    # ---- VaultAdapterInfo: every field populated on first element ----
    assert summary.adapters, "summary.adapters empty — sub-model test cannot run"
    first_adapter = summary.adapters[0]
    for field_name, field_info in VaultAdapterInfo.model_fields.items():
        value = getattr(first_adapter, field_name)
        annotation = field_info.annotation
        default = field_info.default
        if annotation == list[str]:
            assert value, (
                f"VaultAdapterInfo.{field_name} not populated by "
                "_build_vault_summary (empty list would pass 'is not None')"
            )
        elif default is not PydanticUndefined and default is not None:
            assert value != default, (
                f"VaultAdapterInfo.{field_name} matches its default ({default!r}) — "
                "_build_vault_summary may have dropped this field (coincidental pass)"
            )
        else:
            assert value is not None, (
                f"VaultAdapterInfo.{field_name} not populated by _build_vault_summary"
            )


# ---------------------------------------------------------------------------
# list_vaults resilience + self-healing registry reconcile
# ---------------------------------------------------------------------------
#
# list_vaults fans a per-vault graph-store query over every registered vault.
# A vault whose backing schema was dropped out of band (e.g. by a completed
# teardown) makes that query raise; without a per-vault guard, one dead vault
# fails the whole listing. When such a vault is also gone from vault-source
# discovery, it is evicted from the live registry so the stale entry does not
# linger. These tests pin both behaviors and are anti-coincidental: each names
# the regression it catches.


class _FakeGraphStore:
    """Per-vault graph store that either returns counts or raises, with a
    controllable storage-presence probe (defaults to present, matching a
    healthy vault) and a controllable total-document count whose call is
    recorded so a test can prove the listing actually asked for it."""

    def __init__(
        self,
        *,
        counts: dict[str, int] | None = None,
        raises: BaseException | None = None,
        storage_present: bool = True,
        total: int = 0,
        total_raises: BaseException | None = None,
    ) -> None:
        self._counts = counts
        self._raises = raises
        self._storage_present = storage_present
        self._total = total
        self._total_raises = total_raises
        self.total_count_calls = 0

    async def storage_present(self, vault_id: str) -> bool:
        return self._storage_present

    async def get_document_counts_by_field(self, field: str) -> dict[str, int]:
        if self._raises is not None:
            raise self._raises
        return dict(self._counts or {})

    async def get_total_document_count(self) -> int:
        self.total_count_calls += 1
        if self._total_raises is not None:
            raise self._total_raises
        return self._total


class _FakeIngestionService:
    """Stand-in exposing the one adapter ``_build_vault_summary`` reads plus a
    ``stop_worker`` spy the eviction path calls."""

    def __init__(self) -> None:
        self.registered_adapters: dict[SourceType, Any] = {SourceType.MARKDOWN: _SentinelAdapter()}
        self.stop_worker_called = False

    async def stop_worker(self) -> None:
        self.stop_worker_called = True


class _FakeServices:
    """Minimal ``SAGEServices`` stand-in covering exactly what ``list_vaults``
    and the eviction path touch: ``config``, ``graph_store``,
    ``ingestion_service``, and the two ``close_*`` handles (spied so a test can
    assert whether eviction ran)."""

    def __init__(
        self,
        *,
        config: VaultConfig,
        counts: dict[str, int] | None = None,
        raises: BaseException | None = None,
        storage_present: bool = True,
        total: int = 0,
        total_raises: BaseException | None = None,
        ingestion_service: Any = None,
    ) -> None:
        self.config = config
        self.graph_store = _FakeGraphStore(
            counts=counts,
            raises=raises,
            storage_present=storage_present,
            total=total,
            total_raises=total_raises,
        )
        # A real IngestionService can be supplied where what the eviction does
        # to the documents its worker was carrying is the assertion; the spy is
        # enough for the tests that only ask whether the stop ran.
        self.ingestion_service = ingestion_service or _FakeIngestionService()
        self.close_timing_called = False
        self.close_storage_called = False

    def close_timing(self) -> None:
        self.close_timing_called = True

    async def close_storage(self) -> None:
        self.close_storage_called = True


class _FakeSourceStore:
    """Vault-source store whose ``discover()`` yields a controlled id set (or raises)."""

    def __init__(
        self, *, discovered_ids: list[str] | None = None, discover_raises: bool = False
    ) -> None:
        self._ids = discovered_ids or []
        self._discover_raises = discover_raises

    def discover(self) -> list[DiscoveredVault]:
        if self._discover_raises:
            raise RuntimeError("vault-source discovery failed")
        return [DiscoveredVault(config_path=None, vault_id=vid) for vid in self._ids]


async def _unused_initialize_services(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("initialize_services must not be called by list_vaults")


def _patch_source_store(monkeypatch: Any, store: _FakeSourceStore) -> None:
    """Route the service's on-demand vault-source-store resolution to ``store``.

    ``list_vaults`` resolves the store the same way ``create_vault`` does -- a
    late import of ``resolve_stack_vault_source_store`` (plus the stack-config
    and vault-root accessors) from ``sage.mcp_init``. Patching all three keeps
    the unit test independent of any running lifespan.
    """
    monkeypatch.setattr("sage.mcp_init.get_stack_config", lambda: None)
    monkeypatch.setattr("sage.mcp_init.get_vault_root", lambda: None)
    monkeypatch.setattr("sage.mcp_init.resolve_stack_vault_source_store", lambda *a, **k: store)


def _undefined_table() -> psycopg.errors.UndefinedTable:
    """The exact error production raises when a vault's schema was dropped."""
    return psycopg.errors.UndefinedTable('relation "documents" does not exist')


async def test_list_vaults_reports_total_document_count(monkeypatch: Any) -> None:
    """Each listed vault carries the store's total document count, and the
    listing asked the store for it.

    Anti-coincidental: the totals are distinct non-zero and zero sentinels on
    two vaults, so a count wired to the wrong vault, hard-coded, or derived
    from the project counts (which the ``IS NOT NULL`` filter under-reports)
    fails; the call counter proves the value came from the store's total
    rather than from a default.
    """
    config = _vault_config_with_every_summary_field()
    populated = _FakeServices(config=config, counts={"proj_a": 2}, total=7)
    empty = _FakeServices(config=config, counts={}, total=0)
    registry: dict[str, Any] = {"populated": populated, "empty": empty}
    svc = VaultRegistryService(registry, _unused_initialize_services)
    _patch_source_store(monkeypatch, _FakeSourceStore(discovered_ids=["populated", "empty"]))

    result = await svc.list_vaults()

    assert [s.document_count for s in result] == [7, 0]
    assert populated.graph_store.total_count_calls == 1
    assert empty.graph_store.total_count_calls == 1


async def test_list_vaults_skips_a_vault_whose_count_query_fails(
    monkeypatch: Any, caplog: Any
) -> None:
    """A vault whose total-count query raises is skipped like any other
    store error, and the listing survives.

    Anti-coincidental: the failing vault is registered FIRST and its
    project-count query succeeds, so only a count query placed outside the
    per-vault guard would propagate; the healthy vault listing proves the
    loop continued.
    """
    config = _vault_config_with_every_summary_field()
    broken = _FakeServices(config=config, counts={"proj_a": 1}, total_raises=_undefined_table())
    healthy = _FakeServices(config=config, counts={"proj_a": 1}, total=3)
    registry: dict[str, Any] = {"broken": broken, "healthy": healthy}
    svc = VaultRegistryService(registry, _unused_initialize_services)
    _patch_source_store(monkeypatch, _FakeSourceStore(discovered_ids=["broken", "healthy"]))

    with caplog.at_level(logging.ERROR, logger="sage.services.vault_registry"):
        result = await svc.list_vaults()

    assert [s.document_count for s in result] == [3]
    assert "broken" in registry  # config still discovered: kept, not evicted
    assert any(r.levelno == logging.ERROR and "broken" in r.getMessage() for r in caplog.records)


async def test_list_vaults_evicts_a_vault_whose_schema_and_config_are_gone(
    monkeypatch: Any, caplog: Any
) -> None:
    """A registered vault whose store errors AND whose config is gone from
    discovery is skipped from the listing and evicted from the registry; the
    surviving vault still lists.

    Anti-coincidental: ``gone`` is registered FIRST, so a loop that aborted on
    the first error would never reach ``healthy`` -- asserting ``healthy`` lists
    proves the loop continued past the error, not merely that it did not raise.
    Asserting ``gone`` left the registry (and its stop_worker/close_storage
    ran) proves the evict fired, not a bare ``except: pass``.
    """
    config = _vault_config_with_every_summary_field()
    gone = _FakeServices(config=config, raises=_undefined_table())
    healthy = _FakeServices(config=config, counts={"proj_a": 2})
    registry: dict[str, Any] = {"gone": gone, "healthy": healthy}
    svc = VaultRegistryService(registry, _unused_initialize_services)
    _patch_source_store(monkeypatch, _FakeSourceStore(discovered_ids=["healthy"]))

    with caplog.at_level(logging.WARNING, logger="sage.services.vault_registry"):
        result = await svc.list_vaults()

    assert len(result) == 1  # only the healthy vault lists
    assert result[0].id == "sentinel_vault"
    assert "gone" not in registry  # evicted
    assert gone.ingestion_service.stop_worker_called
    assert gone.close_timing_called
    assert gone.close_storage_called
    assert any(r.levelno == logging.WARNING and "gone" in r.getMessage() for r in caplog.records)


async def test_list_vaults_eviction_settles_dropped_abstraction_work(
    monkeypatch: Any, tmp_vault_dir: Any, minimal_config: Any, graph_store: Any
) -> None:
    """Eviction tears down a real worker, and the work it was carrying is
    settled at abstraction_interrupted rather than stranded.

    This path has no successor to recover onto -- the vault is gone from
    discovery and nothing rebuilds it -- so a recovery keyed to the
    replacement entry a reload installs would never reach these documents.
    The reload/hot-swap path is a separate teardown, covered in
    ``test_mcp_init.py``.

    A real IngestionService stands in for the spy: the assertion is what the
    document's stored pipeline_status became, which a spy cannot show.
    """
    from sage.models.enums import PipelineStatus
    from tests.sage.test_abstraction_queue import (
        _build_service,
        _GatedAbstractionProvider,
        _seed_indexed_doc,
    )

    ingestion = _build_service(graph_store, minimal_config, StubAbstractionProvider())
    doc_id = await _seed_indexed_doc(ingestion, tmp_vault_dir, "samples/ev1.md")
    gated = _GatedAbstractionProvider()
    ingestion._abstraction = gated
    await ingestion.reabstract(doc_id)
    await asyncio.wait_for(gated.entered.wait(), timeout=2.0)
    assert (
        await graph_store.get_document(doc_id)
    ).pipeline_status == PipelineStatus.ABSTRACTION_IN_PROGRESS

    config = _vault_config_with_every_summary_field()
    gone = _FakeServices(config=config, raises=_undefined_table(), ingestion_service=ingestion)
    registry: dict[str, Any] = {"gone": gone}
    svc = VaultRegistryService(registry, _unused_initialize_services)
    _patch_source_store(monkeypatch, _FakeSourceStore(discovered_ids=[]))

    try:
        await svc.list_vaults()
    finally:
        gated.gate.set()

    assert "gone" not in registry  # the evict actually fired
    doc = await graph_store.get_document(doc_id)
    assert doc.pipeline_status == PipelineStatus.ABSTRACTION_INTERRUPTED
    assert doc.pipeline_error
    assert doc_id not in ingestion._inflight


async def test_list_vaults_skips_but_keeps_a_transiently_failing_vault(
    monkeypatch: Any, caplog: Any
) -> None:
    """A vault whose store errors but whose config is STILL present in discovery
    is skipped from the listing but NOT evicted -- a transient store error must
    not destroy the registry entry.

    Anti-coincidental: an unconditional evict (dropping the discovery-membership
    guard, or reaching for the ``config_locator(...) is None`` shortcut that
    false-positives on the cloud binding) would remove ``flaky``; asserting it
    remains -- and that its stop_worker/close_storage did NOT run -- fails such
    a regression.
    """
    config = _vault_config_with_every_summary_field()
    flaky = _FakeServices(config=config, raises=_undefined_table())
    healthy = _FakeServices(config=config, counts={"proj_a": 1})
    registry: dict[str, Any] = {"flaky": flaky, "healthy": healthy}
    svc = VaultRegistryService(registry, _unused_initialize_services)
    _patch_source_store(monkeypatch, _FakeSourceStore(discovered_ids=["flaky", "healthy"]))

    with caplog.at_level(logging.ERROR, logger="sage.services.vault_registry"):
        result = await svc.list_vaults()

    assert len(result) == 1  # healthy listed, flaky skipped
    assert "flaky" in registry  # NOT evicted (config still present)
    assert not flaky.ingestion_service.stop_worker_called
    assert not flaky.close_storage_called
    assert any(r.levelno == logging.ERROR and "flaky" in r.getMessage() for r in caplog.records)


async def test_list_vaults_survives_a_reconcile_failure(monkeypatch: Any, caplog: Any) -> None:
    """If the reconcile itself fails (discovery raises), ``list_vaults`` still
    returns the surviving vaults and leaves the errored vault in place -- the
    reconcile is best-effort and must never re-break the listing.

    Anti-coincidental: without wrapping the reconcile in its own try/except, a
    raising ``discover()`` would propagate and fail the whole call; asserting
    the healthy vault still lists proves the failure was contained.
    """
    config = _vault_config_with_every_summary_field()
    broken = _FakeServices(config=config, raises=_undefined_table())
    healthy = _FakeServices(config=config, counts={"proj_a": 1})
    registry: dict[str, Any] = {"broken": broken, "healthy": healthy}
    svc = VaultRegistryService(registry, _unused_initialize_services)
    _patch_source_store(monkeypatch, _FakeSourceStore(discover_raises=True))

    with caplog.at_level(logging.ERROR, logger="sage.services.vault_registry"):
        result = await svc.list_vaults()

    assert len(result) == 1  # healthy still lists
    assert "broken" in registry  # couldn't determine -> left in place
    assert any("reconcile" in r.getMessage().lower() for r in caplog.records)


async def test_list_vaults_evicts_a_torn_down_vault_masked_by_search_path_fallback(
    monkeypatch: Any, caplog: Any
) -> None:
    """An out-of-band schema drop does not reliably make the count query raise:
    the per-vault search_path resolves the store's unqualified table names
    against a later entry, so a torn-down vault's queries can keep *succeeding*
    against tables that are not its own. The explicit storage-presence probe
    must catch it: the vault is skipped from the listing and, with its config
    also gone from discovery, evicted.

    Anti-coincidental: ``masked`` returns healthy-looking counts (no exception
    is ever raised), so a regression back to error-only reconciliation would
    list it and leave it registered; the not-listed + evicted assertions fail
    exactly that.
    """
    config = _vault_config_with_every_summary_field()
    masked = _FakeServices(config=config, counts={"proj_ghost": 3}, storage_present=False)
    healthy = _FakeServices(config=config, counts={"proj_a": 2})
    registry: dict[str, Any] = {"masked": masked, "healthy": healthy}
    svc = VaultRegistryService(registry, _unused_initialize_services)
    _patch_source_store(monkeypatch, _FakeSourceStore(discovered_ids=["healthy"]))

    with caplog.at_level(logging.WARNING, logger="sage.services.vault_registry"):
        result = await svc.list_vaults()

    assert len(result) == 1  # only the healthy vault lists
    assert result[0].id == "sentinel_vault"
    assert "masked" not in registry  # evicted despite the succeeding query
    assert masked.ingestion_service.stop_worker_called
    assert masked.close_timing_called
    assert masked.close_storage_called
    assert any(r.levelno == logging.WARNING and "masked" in r.getMessage() for r in caplog.records)


async def test_list_vaults_skips_but_keeps_a_backing_absent_vault_still_discovered(
    monkeypatch: Any, caplog: Any
) -> None:
    """Probe says the durable backing is gone but the config is STILL present in
    discovery: skipped from the listing but NOT evicted -- a half-completed
    teardown (or a manual schema drop) must not destroy the registry entry; a
    restart re-bootstraps the schema from the surviving config.

    Anti-coincidental: an evict keyed on the probe alone (dropping the
    discovery-membership guard) would remove ``halfway``; asserting it remains
    -- and that its stop_worker/close_storage did NOT run -- fails such a
    regression.
    """
    config = _vault_config_with_every_summary_field()
    halfway = _FakeServices(config=config, counts={"proj_ghost": 1}, storage_present=False)
    healthy = _FakeServices(config=config, counts={"proj_a": 1})
    registry: dict[str, Any] = {"halfway": halfway, "healthy": healthy}
    svc = VaultRegistryService(registry, _unused_initialize_services)
    _patch_source_store(monkeypatch, _FakeSourceStore(discovered_ids=["halfway", "healthy"]))

    with caplog.at_level(logging.ERROR, logger="sage.services.vault_registry"):
        result = await svc.list_vaults()

    assert len(result) == 1  # healthy listed, halfway skipped
    assert "halfway" in registry  # NOT evicted (config still present)
    assert not halfway.ingestion_service.stop_worker_called
    assert not halfway.close_storage_called
    assert any(r.levelno == logging.ERROR and "halfway" in r.getMessage() for r in caplog.records)


async def test_graph_store_port_reports_storage_present_by_default() -> None:
    """Backends whose durable backing cannot vanish independently of the open
    store handle inherit a True probe from the port, so ``list_vaults``'
    presence check is a no-op for them (``StubGraphStore`` inherits the
    default rather than overriding it)."""
    from sage.adapters.stubs import StubGraphStore

    assert await StubGraphStore().storage_present("any_vault") is True
