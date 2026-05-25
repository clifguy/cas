"""Tests for initialize_services() dependency injection.

Verifies that initialize_services() accepts optional provider overrides
so that test fixtures can inject stubs instead of loading heavyweight
production providers (NomicEmbeddingProvider ~270 MB, Qwen3 ~16-20 GB).

Test IDs follow the pattern DI-NNN (Dependency Injection).
"""

from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.config import VaultConfig
from tests.sage.conftest import initialize_services_for_test

# ---------------------------------------------------------------------------
# DI-001: Injected embedding_provider is used by all services
# ---------------------------------------------------------------------------


async def test_di_001_injected_embedding_provider(minimal_vault_config_dict, tmp_vault_dir):
    """When embedding_provider is passed, initialize_services uses it
    instead of constructing a NomicEmbeddingProvider."""
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    stub_embed = StubEmbeddingProvider()

    async with initialize_services_for_test(
        config,
        embedding_provider=stub_embed,
    ) as services:
        # The stub should be the exact instance wired into services
        assert services.ingestion_service._embedding is stub_embed
        assert services.retrieval_service._embedding is stub_embed
        assert services.utilities_service._embedding is stub_embed


# ---------------------------------------------------------------------------
# DI-002: Injected abstraction_provider is used
# ---------------------------------------------------------------------------


async def test_di_002_injected_abstraction_provider(minimal_vault_config_dict, tmp_vault_dir):
    """When abstraction_provider is passed, initialize_services uses it
    instead of constructing one from config."""
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    stub_abstract = StubAbstractionProvider()

    async with initialize_services_for_test(
        config,
        abstraction_provider=stub_abstract,
    ) as services:
        assert services.ingestion_service._abstraction is stub_abstract


# ---------------------------------------------------------------------------
# DI-003: Injected content_store is used
# ---------------------------------------------------------------------------


async def test_di_003_injected_content_store(minimal_vault_config_dict, tmp_vault_dir):
    """When content_store is passed, initialize_services uses it
    instead of constructing a LanceDBContentStore."""
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    stub_cs = StubContentStore()

    async with initialize_services_for_test(
        config,
        content_store=stub_cs,
    ) as services:
        assert services.ingestion_service._content_store is stub_cs
        assert services.retrieval_service._content is stub_cs
        assert services.utilities_service._content is stub_cs


# ---------------------------------------------------------------------------
# DI-004: All three overrides together
# ---------------------------------------------------------------------------


async def test_di_004_all_overrides(minimal_vault_config_dict, tmp_vault_dir):
    """All three providers can be overridden simultaneously."""
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    stub_embed = StubEmbeddingProvider()
    stub_abstract = StubAbstractionProvider()
    stub_cs = StubContentStore()

    async with initialize_services_for_test(
        config,
        embedding_provider=stub_embed,
        abstraction_provider=stub_abstract,
        content_store=stub_cs,
    ) as services:
        assert services.ingestion_service._embedding is stub_embed
        assert services.ingestion_service._abstraction is stub_abstract
        assert services.ingestion_service._content_store is stub_cs
        assert services.retrieval_service._embedding is stub_embed
        assert services.retrieval_service._content is stub_cs
        assert services.utilities_service._embedding is stub_embed
        assert services.utilities_service._content is stub_cs


# ---------------------------------------------------------------------------
# DI-005: No overrides constructs production providers (type check only)
# ---------------------------------------------------------------------------


async def test_di_005_no_overrides_constructs_real_providers(
    minimal_vault_config_dict, tmp_vault_dir, monkeypatch
):
    """When no embedding / content_store overrides are passed, real
    production instances are created. Abstraction is injected explicitly
    because post CAS-ADR-030 the stack provider is built once at SAGE
    process startup and passed in; `initialize_services` no longer
    constructs one. We only check types here -- we do NOT want to verify
    model loading behavior, since that would defeat the purpose of this
    test suite.
    """
    # Force production path: CI sets SAGE_TEST_STUB_PROVIDERS=1 globally to
    # keep most tests off the real model (T-0018); this test specifically
    # verifies the production default and must clear that override.
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)
    config = VaultConfig.model_validate(minimal_vault_config_dict)

    async with initialize_services_for_test(
        config, abstraction_provider=StubAbstractionProvider()
    ) as services:
        from sage.adapters.content_store_lancedb import LanceDBContentStore
        from sage.adapters.embedding_nomic import NomicEmbeddingProvider

        embed = services.ingestion_service._embedding
        cs = services.ingestion_service._content_store

        assert isinstance(embed, NomicEmbeddingProvider)
        assert isinstance(cs, LanceDBContentStore)


# ---------------------------------------------------------------------------
# DI-006: Services are fully functional with stubs
# ---------------------------------------------------------------------------


async def test_di_006_services_functional_with_stubs(minimal_vault_config_dict, tmp_vault_dir):
    """Services initialized with all stubs can perform basic operations
    without errors (smoke test)."""
    config = VaultConfig.model_validate(minimal_vault_config_dict)

    async with initialize_services_for_test(
        config,
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
        content_store=StubContentStore(),
    ) as services:
        # Bootstrap owner should have run during init
        owner = await services.graph_store.get_user_by_display_name(config.vault.owner)
        assert owner is not None
        assert owner.display_name == config.vault.owner


# ---------------------------------------------------------------------------
# DI-008..DI-009: per-vault dispatch after CAS-ADR-030 / T-0103
#
# The factory dispatch that used to live in initialize_services has moved
# to the stack-startup helper (build_stack_abstraction_provider; see
# test_stack_abstraction_dispatch.py for STK-001..005). The per-vault
# dispatch in initialize_services is reduced to the disabled-gate opt-out:
#
#   1. SAGE_TEST_STUB_PROVIDERS=1                 -> Stub (belt-and-suspenders)
#   2. vault.abstraction.enabled is False         -> Stub (vault opted out)
#   3. otherwise                                  -> the injected stack provider
#
# DI-007 / DI-010 (env-var short-circuit), DI-011 / DI-012 (model/provider
# dispatch) move to STK-* at stack scope.
# ---------------------------------------------------------------------------


async def test_di_008_enabled_true_uses_injected_stack_provider(
    minimal_vault_config_dict, tmp_vault_dir, monkeypatch
):
    """When vault.abstraction.enabled is True, initialize_services wires
    the injected stack provider through to IngestionService.

    Anti-coincidence pair with DI-009: the only thing that differs between
    the two tests is the `enabled` flag; the assertion outcome must flip.
    """
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)

    cfg = dict(minimal_vault_config_dict)
    cfg["abstraction"] = {"enabled": True}
    config = VaultConfig.model_validate(cfg)

    sentinel = StubAbstractionProvider()  # any AbstractionProvider works

    async with initialize_services_for_test(config, abstraction_provider=sentinel) as services:
        assert services.ingestion_service._abstraction is sentinel


async def test_di_009_enabled_false_substitutes_stub_even_when_provider_injected(
    minimal_vault_config_dict, tmp_vault_dir, monkeypatch
):
    """When vault.abstraction.enabled is False, initialize_services must
    substitute StubAbstractionProvider even if the caller injected a real
    stack provider. The disabled gate is the only per-vault knob that
    affects which provider the vault sees (ADR-011 opt-in semantics,
    re-anchored by ADR-030).

    Anti-coincidence pair with DI-008: same config minus the `enabled`
    flag flip; the assertion outcome must change.
    """
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)

    cfg = dict(minimal_vault_config_dict)
    cfg["abstraction"] = {"enabled": False}
    config = VaultConfig.model_validate(cfg)

    sentinel = StubAbstractionProvider()
    sentinel.marker = "would-have-been-stack-provider"  # type: ignore[attr-defined]

    async with initialize_services_for_test(config, abstraction_provider=sentinel) as services:
        wired = services.ingestion_service._abstraction
        assert isinstance(wired, StubAbstractionProvider)
        # Anti-coincidence: must NOT be the injected sentinel.
        assert wired is not sentinel
        assert getattr(wired, "marker", None) is None
