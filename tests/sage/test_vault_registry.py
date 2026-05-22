"""Closure-pair tests for ``VaultRegistryService._build_vault_summary``.

T-0122 installs the second half of the F4 closure pair for the
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

from typing import Any

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
    - ``source_adapters`` is a non-empty dict (the field is ``dict``
      typed on ``VaultConfig``); the factory does not iterate it, but
      the sentinel matches the shape produced by
      ``VaultRegistryService.get_default_config``.
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
        source_adapters={"adapters": [{"source_type": "markdown", "enabled": True}]},
        metadata_extraction={"filename_extraction": {"separator": "_"}},
        edge_inference={"tier_assignments": []},
    )


def test_build_vault_summary_populates_every_vault_summary_field():
    """T-0122 (F4 closure pair, T1): every ``VaultSummary`` field is
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

    summary = VaultRegistryService._build_vault_summary(config, services, projects)

    # ---- VaultSummary: every field non-empty / non-None ----
    for field_name, field_info in VaultSummary.model_fields.items():
        value = getattr(summary, field_name)
        annotation = field_info.annotation
        # list-typed sub-collections default to ``[]`` and would pass a
        # naive ``value is not None`` check on an empty default; require
        # truthy for these and non-None for the rest (the T-0096 idiom).
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
        else:
            assert value is not None, (
                f"VaultSummary.{field_name} not populated by _build_vault_summary"
            )

    # ---- VaultDocTypeEntry: every field populated on first element ----
    assert summary.doc_types, "summary.doc_types empty — sub-model test cannot run"
    first_doc_type = summary.doc_types[0]
    for field_name, _field_info in VaultDocTypeEntry.model_fields.items():
        value = getattr(first_doc_type, field_name)
        assert value is not None, (
            f"VaultDocTypeEntry.{field_name} not populated by _build_vault_summary"
        )

    # ---- VaultLifecycleState: every field populated on first element ----
    assert summary.lifecycle_states, "summary.lifecycle_states empty — sub-model test cannot run"
    first_state = summary.lifecycle_states[0]
    for field_name, _field_info in VaultLifecycleState.model_fields.items():
        value = getattr(first_state, field_name)
        assert value is not None, (
            f"VaultLifecycleState.{field_name} not populated by _build_vault_summary"
        )
    # Additional anti-coincidental-pass guard: ``is_terminal`` defaults to
    # ``False`` and ``False is not None`` would pass the loop above. The
    # sentinel sets it ``True``; a factory that hard-codes the default
    # would still pass the loop but fail this explicit assertion.
    assert first_state.is_terminal is True, (
        "VaultLifecycleState.is_terminal not threaded from LifecycleState "
        "by _build_vault_summary (sentinel set is_terminal=True; factory "
        "regression to default False would not be caught by the "
        "value-is-not-None loop above)"
    )

    # ---- VaultAdapterInfo: every field populated on first element ----
    assert summary.adapters, "summary.adapters empty — sub-model test cannot run"
    first_adapter = summary.adapters[0]
    for field_name, field_info in VaultAdapterInfo.model_fields.items():
        value = getattr(first_adapter, field_name)
        annotation = field_info.annotation
        if annotation == list[str]:
            assert value, (
                f"VaultAdapterInfo.{field_name} not populated by "
                "_build_vault_summary (empty list would pass 'is not None')"
            )
        else:
            assert value is not None, (
                f"VaultAdapterInfo.{field_name} not populated by _build_vault_summary"
            )
