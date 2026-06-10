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
    # keep most tests off the real model; this test specifically
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
# DI-008..DI-009: per-vault dispatch after CAS-ADR-030 /
#
# The factory dispatch that used to live in initialize_services has moved
# to the stack-startup helper (build_stack_abstraction_provider; see
# test_stack_abstraction_dispatch.py for STK-001..005). The per-vault
# dispatch in initialize_services is reduced to the disabled-gate opt-out:
#
# 1. SAGE_TEST_STUB_PROVIDERS=1 -> Stub (belt-and-suspenders)
# 2. vault.abstraction.enabled is False -> Stub (vault opted out)
# 3. otherwise -> the injected stack provider
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


# ---------------------------------------------------------------------------
# DI-013..DI-015: durable-storage binding dispatch (CAS-ADR-042).
# The default-store construction moved from hardcoded SQLite/LanceDB branches
# to the storage provisioner resolved through the profile seam. Injection
# precedence is unchanged: explicit instance > factory > provisioner default.
# ---------------------------------------------------------------------------


async def test_di_013_injected_stores_never_consult_the_provisioner(
    minimal_vault_config_dict, tmp_vault_dir, monkeypatch
):
    """With both stores injected, `initialize_services` succeeds without
    resolving the storage provisioner at all.

    Anti-coincidental-pass: the storage-binding factory is monkeypatched to
    raise, so a dispatch that consulted the provisioner even when both slots
    are filled (quietly building a second store set and leaking its pool)
    fails this test loudly. Hermetic: no database of either backend is touched.
    """
    import sage.mcp_init as _mcp_init
    from sage.adapters.stubs import StubGraphStore

    def exploding_factory(_cfg):
        raise AssertionError("storage provisioner consulted despite full injection")

    monkeypatch.setattr(_mcp_init, "build_stack_storage_provisioner", exploding_factory)

    config = VaultConfig.model_validate(minimal_vault_config_dict)
    async with initialize_services_for_test(
        config,
        graph_store=StubGraphStore(),
        content_store=StubContentStore(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        assert isinstance(services.graph_store, StubGraphStore)
        assert services.storage is None


async def test_di_014_no_overrides_postgres_backend_builds_postgres_stores(
    minimal_vault_config_dict, tmp_vault_dir, monkeypatch, pg_dsn
):
    """With the stack config selecting `postgres` and the test env override
    cleared, a no-override `initialize_services` builds `PostgresGraphStore`
    and `PostgresContentStore` over a per-vault pool, records the storage
    handle on the services, and `close_storage()` closes the pool.

    This is the binding flip itself. Anti-coincidental-pass drill (run during
    verification): reverting the dispatch to the prior hardcoded
    SQLite/LanceDB branches must fail the isinstance assertions here.
    """
    import copy
    import uuid

    import psycopg

    from sage.adapters.content_store_postgres import PostgresContentStore
    from sage.config import SageCoreConfig
    from sage.mcp_init import set_stack_config
    from sage.storage.postgres.graph_store import PostgresGraphStore
    from tests.sage.conftest import stack_postgres_config_from_dsn

    monkeypatch.delenv("SAGE_TEST_STORAGE_BACKEND", raising=False)

    vault_id = f"sage_test_{uuid.uuid4().hex[:10]}"
    cfg_dict = copy.deepcopy(minimal_vault_config_dict)
    cfg_dict["vault"]["id"] = vault_id
    config = VaultConfig.model_validate(cfg_dict)

    stack = SageCoreConfig(
        storage_backend="postgres",
        postgres=stack_postgres_config_from_dsn(pg_dsn, monkeypatch),
    )
    set_stack_config(stack)
    try:
        async with initialize_services_for_test(
            config, abstraction_provider=StubAbstractionProvider()
        ) as services:
            assert isinstance(services.graph_store, PostgresGraphStore)
            assert isinstance(services.content_store, PostgresContentStore)
            assert services.storage is not None
            pool = services.storage.pool
            assert pool is not None and not pool.closed
        # initialize_services_for_test teardown ran close_storage().
        assert pool.closed
    finally:
        set_stack_config(None)
        async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{vault_id}" CASCADE')


async def test_di_015_postgres_services_run_ingest_search_traverse_lifecycle(
    minimal_vault_config_dict, tmp_vault_dir, monkeypatch, pg_dsn
):
    """Service-level end-to-end on Postgres-backed services: ingest a chain
    of two documents, keyword-search them, traverse the supersedes edge, and
    run a lifecycle transition.

    The store-level parity suite (parametrized `graph_store` fixture) covers
    each operation in isolation; this is the service-layer composition the
    cutover relies on — including the per-vault search_path isolation, the
    chain-head trigger installed at vault open, and Postgres FTS serving the
    keyword arm.
    """
    import asyncio
    import copy
    import uuid

    import psycopg

    from sage.config import SageCoreConfig
    from sage.mcp_init import set_stack_config
    from sage.models.enums import RetrievalMode, SourceType
    from sage.models.schemas import (
        BulkLifecycleItem,
        BulkLifecycleRequest,
        DiscoverRequest,
        IngestRequest,
        TraverseRequest,
    )
    from tests.sage.conftest import stack_postgres_config_from_dsn

    monkeypatch.delenv("SAGE_TEST_STORAGE_BACKEND", raising=False)

    vault_id = f"sage_test_{uuid.uuid4().hex[:10]}"
    cfg_dict = copy.deepcopy(minimal_vault_config_dict)
    cfg_dict["vault"]["id"] = vault_id
    config = VaultConfig.model_validate(cfg_dict)

    def _write_source(name: str, body: str) -> None:
        path = tmp_vault_dir / "sources" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    async def _await_terminal_pipeline(services, doc_id: str) -> None:
        from sage.models.enums import TERMINAL_PIPELINE_STATUSES

        for _ in range(100):
            doc = await services.graph_store.get_document(doc_id)
            assert doc is not None
            if doc.pipeline_status in TERMINAL_PIPELINE_STATUSES:
                return
            await asyncio.sleep(0.1)
        raise AssertionError(f"document {doc_id} never reached a terminal pipeline status")

    stack = SageCoreConfig(
        storage_backend="postgres",
        postgres=stack_postgres_config_from_dsn(pg_dsn, monkeypatch),
    )
    set_stack_config(stack)
    try:
        async with initialize_services_for_test(
            config, abstraction_provider=StubAbstractionProvider()
        ) as services:
            try:
                # Ingest a two-document supersedes chain.
                _write_source("alpha_v1.md", "# Alpha\n\nZirconium fastener torque table.\n")
                _write_source("alpha_v2.md", "# Alpha v2\n\nZirconium fastener torque table.\n")
                first = await services.ingestion_service.ingest(
                    IngestRequest(
                        source="alpha_v1.md",
                        source_type=SourceType.MARKDOWN,
                        metadata={"title": "Alpha"},
                    )
                )
                second = await services.ingestion_service.ingest(
                    IngestRequest(
                        source="alpha_v2.md",
                        source_type=SourceType.MARKDOWN,
                        metadata={"title": "Alpha v2"},
                        predecessor_id=first.document.id,
                    )
                )
                await _await_terminal_pipeline(services, second.document.id)

                # Keyword search runs on the Postgres FTS arm.
                response = await services.retrieval_service.discover(
                    DiscoverRequest(mode=RetrievalMode.KEYWORD, query="zirconium fastener")
                )
                hit_ids = [h.document.id for h in response.results]
                assert second.document.id in hit_ids

                # Traverse the supersedes edge written at ingest.
                traversal = await services.graph_ops_service.traverse(
                    TraverseRequest(
                        start_id=second.document.id,
                        edge_type="supersedes",
                        direction="outbound",
                    )
                )
                traversed_ids = {n.document.id for n in traversal.nodes}
                assert first.document.id in traversed_ids

                # Lifecycle transition through the service layer.
                bulk = await services.lifecycle_service.bulk_set_lifecycle(
                    BulkLifecycleRequest(
                        items=[BulkLifecycleItem(document_id=second.document.id, action="complete")]
                    )
                )
                assert bulk.results[0].status == "success"
                completed = await services.graph_store.get_document(second.document.id)
                assert completed is not None
                assert completed.lifecycle_status == "completed"
            finally:
                await services.ingestion_service.stop_worker()
    finally:
        set_stack_config(None)
        async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{vault_id}" CASCADE')


async def test_reload_vault_in_registry_closes_old_storage_handle(
    minimal_vault_config_dict, tmp_vault_dir
):
    """`reload_vault_in_registry` releases the predecessor services' storage
    handle (the resource backing the stores — the pool, on the Postgres
    binding) when it tears the old services down.

    Hermetic: a stub handle with a closed flag stands in for the pool-owning
    handle. Anti-coincidental-pass: removing the `close_storage()` call at
    the reload teardown site leaves the flag False and fails this test.
    """
    from sage.adapters.stubs import StubGraphStore
    from sage.mcp_init import reload_vault_in_registry

    class FlagHandle:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    config = VaultConfig.model_validate(minimal_vault_config_dict)
    async with initialize_services_for_test(
        config,
        graph_store_factory=lambda _root: StubGraphStore(),
        content_store=StubContentStore(),
        abstraction_provider=StubAbstractionProvider(),
    ) as old:
        handle = FlagHandle()
        old.storage = handle
        registry = {"test_vault": old}

        new_services = await reload_vault_in_registry(registry, "test_vault", config)
        try:
            assert registry["test_vault"] is new_services
            assert handle.closed, "old services' storage handle was not closed on reload"
        finally:
            new_services.close_timing()
            await new_services.close_storage()
