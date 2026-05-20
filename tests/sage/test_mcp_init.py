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


# ---------------------------------------------------------------------------
# DI-007: SAGE_TEST_STUB_PROVIDERS=1 stubs the abstraction provider even
# when the vault config would otherwise enable a real model (T-0029).
# ---------------------------------------------------------------------------


async def test_di_007_stub_env_var_overrides_qwen3_config(
    minimal_vault_config_dict, tmp_vault_dir, monkeypatch
):
    """When SAGE_TEST_STUB_PROVIDERS=1 is set and the vault config enables
    abstraction with a real model id, initialize_services must still
    construct StubAbstractionProvider rather than loading Qwen3.

    Regression guard for the T-0029 interim guardrail: the kernel-panic
    failure profile in F-8 hinges on a second Qwen3 process loading
    alongside the running MCP server. Tests must not be a path to that.
    """
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    cfg = dict(minimal_vault_config_dict)
    # Force the config branch that previously would have constructed
    # Qwen3AbstractionProvider: enabled=True AND a non-null model id.
    cfg_abstraction = dict(cfg.get("abstraction", {}))
    cfg_abstraction["enabled"] = True
    cfg_abstraction["model"] = "mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit"
    cfg["abstraction"] = cfg_abstraction
    config = VaultConfig.model_validate(cfg)

    services = await initialize_services(config)

    try:
        abstract = services.ingestion_service._abstraction
        assert isinstance(abstract, StubAbstractionProvider), (
            "SAGE_TEST_STUB_PROVIDERS=1 must override the config-driven "
            "Qwen3 path; got %r" % type(abstract).__name__
        )
    finally:
        await services.graph_store.close()


# ---------------------------------------------------------------------------
# DI-008..DI-012: explicit provider-field dispatch (T-0099)
#
# The dispatch contract (sage/mcp_init.py) is:
#   1. SAGE_TEST_STUB_PROVIDERS=1                 -> Stub  (env override)
#   2. config.abstraction.enabled is False        -> Stub  (disabled gate)
#   3. config.abstraction.model is None           -> Stub  (no model id)
#   4. config.abstraction.provider == "stub"      -> Stub  (explicit opt-out)
#   5. config.abstraction.provider == "qwen3-mlx" -> Qwen3 (factory dispatch)
#
# These tests exercise rules 2-5; DI-007 already covers rule 1.
# ---------------------------------------------------------------------------


def _qwen3_enabled_abstraction_block(model_id: str = "mlx-community/test-model") -> dict:
    """Abstraction block that would yield Qwen3 under the new dispatch
    (enabled, model set). The ``provider`` key is the dispatch knob.
    """
    return {
        "enabled": True,
        "model": model_id,
    }


async def test_di_008_provider_qwen3_mlx_dispatches_to_qwen3(
    minimal_vault_config_dict, tmp_vault_dir, monkeypatch
):
    """provider="qwen3-mlx" routes through ``get_qwen3_abstraction_provider``
    with the configured model_id. We monkeypatch the factory to a sentinel
    so the real MLX load is not triggered (CLAUDE.md RAM-budget rule).

    Anti-coincidental-pass: the assertion checks both (a) the sentinel is
    the exact instance wired into IngestionService and (b) the factory was
    called once with the configured ``model_id``. DI-009 supplies the
    negative pair (provider="stub" with the same config must NOT call the
    factory).
    """
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)

    cfg = dict(minimal_vault_config_dict)
    abstraction_block = _qwen3_enabled_abstraction_block(model_id="mlx-community/test-qwen3")
    abstraction_block["provider"] = "qwen3-mlx"
    cfg["abstraction"] = abstraction_block
    config = VaultConfig.model_validate(cfg)

    sentinel = StubAbstractionProvider()  # any AbstractionProvider works
    calls: list[dict] = []

    def fake_factory(*, model_id: str, **kwargs):
        calls.append({"model_id": model_id, **kwargs})
        return sentinel

    # The mcp_init dispatch lazily imports ``get_qwen3_abstraction_provider``
    # from ``sage.adapters.abstraction_qwen3``; patching the attribute on
    # that source module changes what the lazy import resolves to.
    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.get_qwen3_abstraction_provider",
        fake_factory,
    )

    services = await initialize_services(config)

    try:
        assert services.ingestion_service._abstraction is sentinel
        assert len(calls) == 1
        assert calls[0]["model_id"] == "mlx-community/test-qwen3"
    finally:
        await services.graph_store.close()


async def test_di_009_provider_stub_dispatches_to_stub_without_loading_qwen3(
    minimal_vault_config_dict, tmp_vault_dir, monkeypatch
):
    """provider="stub" returns StubAbstractionProvider even when the rest of
    the abstraction block would (under the OLD dispatch) construct Qwen3.

    This is the killer test for the refactor: without the new dispatch,
    ``enabled=True`` and a non-null ``model`` route through the Qwen3
    branch. The monkeypatched factory raises if called, so a regression
    that ignores ``provider`` fires immediately.
    """
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)

    cfg = dict(minimal_vault_config_dict)
    abstraction_block = _qwen3_enabled_abstraction_block(model_id="mlx-community/test-qwen3")
    abstraction_block["provider"] = "stub"
    cfg["abstraction"] = abstraction_block
    config = VaultConfig.model_validate(cfg)

    def must_not_call(*args, **kwargs):
        raise AssertionError(
            "get_qwen3_abstraction_provider must not be called when provider='stub'"
        )

    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.get_qwen3_abstraction_provider",
        must_not_call,
    )

    services = await initialize_services(config)

    try:
        assert isinstance(services.ingestion_service._abstraction, StubAbstractionProvider)
    finally:
        await services.graph_store.close()


async def test_di_010_env_var_beats_provider_qwen3_mlx(
    minimal_vault_config_dict, tmp_vault_dir, monkeypatch
):
    """SAGE_TEST_STUB_PROVIDERS=1 short-circuits before the provider field
    is consulted, even when provider="qwen3-mlx" would otherwise dispatch
    to Qwen3.

    Anti-coincidental-pass: paired with DI-008 (which uses the same config
    minus the env var and gets Qwen3). The difference between the two
    outcomes proves the env var is what flipped the decision.
    """
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")

    cfg = dict(minimal_vault_config_dict)
    abstraction_block = _qwen3_enabled_abstraction_block()
    abstraction_block["provider"] = "qwen3-mlx"
    cfg["abstraction"] = abstraction_block
    config = VaultConfig.model_validate(cfg)

    # Belt-and-suspenders: if the env-var short-circuit is broken, the
    # factory monkeypatch ensures the test fails loudly instead of
    # silently loading MLX.
    def must_not_call(*args, **kwargs):
        raise AssertionError("Qwen3 factory must not be called when SAGE_TEST_STUB_PROVIDERS=1")

    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.get_qwen3_abstraction_provider",
        must_not_call,
    )

    services = await initialize_services(config)

    try:
        assert isinstance(services.ingestion_service._abstraction, StubAbstractionProvider)
    finally:
        await services.graph_store.close()


async def test_di_011_enabled_false_yields_stub_regardless_of_provider(
    minimal_vault_config_dict, tmp_vault_dir, monkeypatch
):
    """``enabled=False`` short-circuits dispatch to Stub even when
    provider="qwen3-mlx". The disabled gate must beat the provider field
    (ADR-011 opt-in semantics).

    Anti-coincidental-pass: DI-008 is the positive pair (enabled=True
    with provider="qwen3-mlx" -> Qwen3). This test confirms enabled=False
    flips the decision.
    """
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)

    cfg = dict(minimal_vault_config_dict)
    cfg["abstraction"] = {
        "enabled": False,
        "model": "mlx-community/test-qwen3",
        "provider": "qwen3-mlx",
    }
    config = VaultConfig.model_validate(cfg)

    def must_not_call(*args, **kwargs):
        raise AssertionError("Qwen3 factory must not be called when abstraction.enabled is False")

    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.get_qwen3_abstraction_provider",
        must_not_call,
    )

    services = await initialize_services(config)

    try:
        assert isinstance(services.ingestion_service._abstraction, StubAbstractionProvider)
    finally:
        await services.graph_store.close()


async def test_di_012_no_model_yields_stub_regardless_of_provider(
    minimal_vault_config_dict, tmp_vault_dir, monkeypatch
):
    """``model is None`` short-circuits dispatch to Stub even when
    provider="qwen3-mlx". Cannot construct Qwen3 without a model id.

    Anti-coincidental-pass: same pairing logic as DI-011.
    """
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)

    cfg = dict(minimal_vault_config_dict)
    cfg["abstraction"] = {
        "enabled": True,
        # model deliberately omitted -> defaults to None
        "provider": "qwen3-mlx",
    }
    config = VaultConfig.model_validate(cfg)

    def must_not_call(*args, **kwargs):
        raise AssertionError("Qwen3 factory must not be called when abstraction.model is None")

    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.get_qwen3_abstraction_provider",
        must_not_call,
    )

    services = await initialize_services(config)

    try:
        assert isinstance(services.ingestion_service._abstraction, StubAbstractionProvider)
    finally:
        await services.graph_store.close()
