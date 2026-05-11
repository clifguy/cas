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
from sage.mcp_init import initialize_services

# ---------------------------------------------------------------------------
# DI-001: Injected embedding_provider is used by all services
# ---------------------------------------------------------------------------


async def test_di_001_injected_embedding_provider(minimal_vault_config_dict, tmp_vault_dir):
    """When embedding_provider is passed, initialize_services uses it
    instead of constructing a NomicEmbeddingProvider."""
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    stub_embed = StubEmbeddingProvider()

    services = await initialize_services(
        config,
        embedding_provider=stub_embed,
    )

    try:
        # The stub should be the exact instance wired into services
        assert services.ingestion_service._embedding is stub_embed
        assert services.retrieval_service._embedding is stub_embed
        assert services.utilities_service._embedding is stub_embed
    finally:
        await services.graph_store.close()


# ---------------------------------------------------------------------------
# DI-002: Injected abstraction_provider is used
# ---------------------------------------------------------------------------


async def test_di_002_injected_abstraction_provider(minimal_vault_config_dict, tmp_vault_dir):
    """When abstraction_provider is passed, initialize_services uses it
    instead of constructing one from config."""
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    stub_abstract = StubAbstractionProvider()

    services = await initialize_services(
        config,
        abstraction_provider=stub_abstract,
    )

    try:
        assert services.ingestion_service._abstraction is stub_abstract
    finally:
        await services.graph_store.close()


# ---------------------------------------------------------------------------
# DI-003: Injected content_store is used
# ---------------------------------------------------------------------------


async def test_di_003_injected_content_store(minimal_vault_config_dict, tmp_vault_dir):
    """When content_store is passed, initialize_services uses it
    instead of constructing a LanceDBContentStore."""
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    stub_cs = StubContentStore()

    services = await initialize_services(
        config,
        content_store=stub_cs,
    )

    try:
        assert services.ingestion_service._content_store is stub_cs
        assert services.retrieval_service._content is stub_cs
        assert services.utilities_service._content is stub_cs
    finally:
        await services.graph_store.close()


# ---------------------------------------------------------------------------
# DI-004: All three overrides together
# ---------------------------------------------------------------------------


async def test_di_004_all_overrides(minimal_vault_config_dict, tmp_vault_dir):
    """All three providers can be overridden simultaneously."""
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    stub_embed = StubEmbeddingProvider()
    stub_abstract = StubAbstractionProvider()
    stub_cs = StubContentStore()

    services = await initialize_services(
        config,
        embedding_provider=stub_embed,
        abstraction_provider=stub_abstract,
        content_store=stub_cs,
    )

    try:
        assert services.ingestion_service._embedding is stub_embed
        assert services.ingestion_service._abstraction is stub_abstract
        assert services.ingestion_service._content_store is stub_cs
        assert services.retrieval_service._embedding is stub_embed
        assert services.retrieval_service._content is stub_cs
        assert services.utilities_service._embedding is stub_embed
        assert services.utilities_service._content is stub_cs
    finally:
        await services.graph_store.close()


# ---------------------------------------------------------------------------
# DI-005: No overrides constructs production providers (type check only)
# ---------------------------------------------------------------------------


async def test_di_005_no_overrides_constructs_real_providers(
    minimal_vault_config_dict, tmp_vault_dir, monkeypatch
):
    """When no overrides are passed, real production providers are created.

    We only check types here -- we do NOT want to verify model loading
    behavior, since that would defeat the purpose of this test suite.
    """
    # Force production path: CI sets SAGE_TEST_STUB_PROVIDERS=1 globally to
    # keep most tests off the real model (T-0018); this test specifically
    # verifies the production default and must clear that override.
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)
    config = VaultConfig.model_validate(minimal_vault_config_dict)

    services = await initialize_services(config)

    try:
        from sage.adapters.content_store_lancedb import LanceDBContentStore
        from sage.adapters.embedding_nomic import NomicEmbeddingProvider

        embed = services.ingestion_service._embedding
        cs = services.ingestion_service._content_store
        abstract = services.ingestion_service._abstraction

        assert isinstance(embed, NomicEmbeddingProvider)
        assert isinstance(cs, LanceDBContentStore)
        # With no abstraction config, should get StubAbstractionProvider
        assert isinstance(abstract, StubAbstractionProvider)
    finally:
        await services.graph_store.close()


# ---------------------------------------------------------------------------
# DI-006: Services are fully functional with stubs
# ---------------------------------------------------------------------------


async def test_di_006_services_functional_with_stubs(minimal_vault_config_dict, tmp_vault_dir):
    """Services initialized with all stubs can perform basic operations
    without errors (smoke test)."""
    config = VaultConfig.model_validate(minimal_vault_config_dict)

    services = await initialize_services(
        config,
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
        content_store=StubContentStore(),
    )

    try:
        # Bootstrap owner should have run during init
        owner = await services.graph_store.get_user_by_display_name(config.vault.owner)
        assert owner is not None
        assert owner.display_name == config.vault.owner
    finally:
        await services.graph_store.close()
