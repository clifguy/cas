"""GraphStore adapter-seam contract: ABC, concrete impl, stub, and injection.

Proves the graph store has a real swappable seam: a ``GraphStore`` ABC port, a
concrete ``SqliteGraphStore`` implementing it, a hermetic ``StubGraphStore``, and
``initialize_services`` injection mirroring the content-store seam. The
structural tests (T1-T5) guard the port surface; the substitutability tests
(T6-T7) prove a stub can stand in for the SQLite store end to end.
"""

from __future__ import annotations

import inspect

import pytest

from sage.adapters.interfaces import GraphStore
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
    StubGraphStore,
)
from sage.config import VaultConfig
from sage.mcp_init import initialize_services
from sage.storage.graph_store import SqliteGraphStore

# The only public methods on the concrete store that are intentionally NOT part
# of the port: SQLite PRAGMA introspection with no cross-backend meaning. Any
# OTHER public concrete method that drifts out of the ABC is a seam regression
# (see T4). This is the one documented, intentional ABC-vs-concrete divergence.
SQLITE_ONLY_METHODS = {"get_journal_mode", "get_busy_timeout", "get_synchronous"}


def _concrete_public_methods() -> set[str]:
    """Public methods defined directly on SqliteGraphStore (not inherited)."""
    return {
        name
        for name, val in vars(SqliteGraphStore).items()
        if not name.startswith("_") and inspect.isfunction(val)
    }


def _port_default_methods() -> set[str]:
    """Public concrete (defaulted) methods defined directly on the GraphStore ABC.

    A defaulted port method is port surface even though it is not abstract:
    implementations may override it (the SQLite store overrides the lenient
    edge enumeration for per-row failure containment), and T4/T5 must treat
    such an override as port-conformant rather than as seam drift.
    """
    return {
        name
        for name, val in vars(GraphStore).items()
        if not name.startswith("_") and inspect.isfunction(val)
    }


# --------------------------------------------------------------------------- #
# Structural contract (T1-T5)
# --------------------------------------------------------------------------- #


def test_graphstore_abc_is_abstract():
    """T1: GraphStore is a genuine ABC and cannot be instantiated.

    Trap (anti-coincidental): if GraphStore were a plain class with no
    @abstractmethod members, ``GraphStore()`` would succeed and the seam would
    be a port in name only. The isabstract / __abstractmethods__ assertions
    close that loophole.
    """
    assert inspect.isabstract(GraphStore)
    assert len(GraphStore.__abstractmethods__) > 0
    with pytest.raises(TypeError):
        GraphStore()  # type: ignore[abstract]


def test_sqlite_graph_store_is_concrete_graphstore():
    """T2: SqliteGraphStore implements the full port (no abstract leftovers).

    Trap: a single un-implemented abstract method flips isabstract back to
    True and makes the class un-instantiable — exactly the failure mode of a
    50-method port that is easy to under-fill.
    """
    assert issubclass(SqliteGraphStore, GraphStore)
    assert not inspect.isabstract(SqliteGraphStore)


def test_stub_graph_store_is_concrete_graphstore():
    """T3: StubGraphStore implements the full port and instantiates.

    Trap: same as T2 for the stub. A stub that silently omits a method would
    stay abstract; the instantiation below would raise.
    """
    assert issubclass(StubGraphStore, GraphStore)
    assert not inspect.isabstract(StubGraphStore)
    StubGraphStore()  # must not raise


def test_abc_surface_matches_consumed_concrete_surface():
    """T4: the port captures exactly the consumed concrete surface.

    ``public(SqliteGraphStore) - SQLITE_ONLY_METHODS`` must equal the ABC's
    abstract-method set. Trap: if a service-consumed method were dropped from
    the ABC, it would appear in this difference (and not in SQLITE_ONLY_METHODS),
    failing the test. Without this guard, the stub could "pass" merely by also
    omitting the method. This is the strongest structural guard on the seam.
    """
    concrete_public = _concrete_public_methods()
    abc_methods = set(GraphStore.__abstractmethods__)
    port_surface = abc_methods | _port_default_methods()

    # Every abstract method is implemented as a public concrete method.
    assert abc_methods <= concrete_public
    # The only public concrete methods outside the port are the SQLite-only trio.
    assert concrete_public - port_surface == SQLITE_ONLY_METHODS
    # And nothing in the divergence list leaked into the port.
    assert port_surface.isdisjoint(SQLITE_ONLY_METHODS)


@pytest.mark.parametrize(
    "method_name", sorted(set(GraphStore.__abstractmethods__) | _port_default_methods())
)
def test_concrete_signature_matches_port(method_name):
    """T5: each concrete method's signature matches the port's exactly.

    Trap: a parameter rename, default change, or return-type drift between the
    port and the concrete would break substitutability silently while T2-T4 stay
    green. Strict signature equality surfaces it per method. Defaulted port
    methods are included so an override cannot drift from the port shape.
    """
    abc_sig = inspect.signature(getattr(GraphStore, method_name))
    concrete_sig = inspect.signature(getattr(SqliteGraphStore, method_name))
    assert concrete_sig == abc_sig, f"{method_name}: concrete {concrete_sig} != port {abc_sig}"


# --------------------------------------------------------------------------- #
# Substitutability + injection (T6-T7)
# --------------------------------------------------------------------------- #


async def _init_with_stubs(config: VaultConfig, **graph_kwargs):
    """Build services with hermetic content/embedding/abstraction stubs.

    Only the graph-store binding varies (via ``graph_kwargs``), so each test
    isolates the seam under test.
    """
    return await initialize_services(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
        **graph_kwargs,
    )


async def _teardown(services) -> None:
    services.close_timing()
    await services.graph_store.close()


async def test_substitutability_instance_injection(minimal_vault_config_dict):
    """T6: an injected StubGraphStore stands in for SQLite end to end.

    The services must hold the exact injected instance (not a freshly-built
    SqliteGraphStore), and the write performed during init (bootstrap_owner)
    must land in the stub.

    Trap: if initialize_services ignored the injection and built its own
    SqliteGraphStore, ``services.graph_store is stub`` fails. If it stored the
    stub but wired services to a different store, the bootstrapped-owner read
    returns None.
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    stub = StubGraphStore()
    services = await _init_with_stubs(config, graph_store=stub)
    try:
        assert services.graph_store is stub
        assert isinstance(services.graph_store, StubGraphStore)
        # A real service was threaded to the injected store, not a sibling.
        assert services.user_service._store is stub
        # bootstrap_owner wrote the vault owner THROUGH the service INTO the stub.
        owner = await services.graph_store.get_user_by_display_name(config.vault.owner)
        assert owner is not None
        assert owner.display_name == config.vault.owner
    finally:
        await _teardown(services)


async def test_substitutability_factory_injection_persists_factory(minimal_vault_config_dict):
    """T6 (factory variant): graph_store_factory builds the store and is retained.

    Mirrors content_store_factory: the factory is invoked with brain_root, its
    product becomes services.graph_store, and the factory itself is stored on
    SAGEServices so reload paths can reuse it.

    Trap: if the factory result were discarded (default SQLite built instead),
    the identity assertion fails; if the factory were not persisted, the
    graph_store_factory assertion fails.
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    built: list[StubGraphStore] = []

    def factory(_brain_root):
        store = StubGraphStore()
        built.append(store)
        return store

    services = await _init_with_stubs(config, graph_store_factory=factory)
    try:
        assert len(built) == 1
        assert services.graph_store is built[0]
        assert services.graph_store_factory is factory
    finally:
        await _teardown(services)


async def test_injection_precedence_instance_over_factory(minimal_vault_config_dict):
    """T7a: an explicit instance wins over a factory (mirrors content store)."""
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    instance = StubGraphStore()
    factory_built: list[StubGraphStore] = []

    def factory(_brain_root):
        store = StubGraphStore()
        factory_built.append(store)
        return store

    services = await _init_with_stubs(config, graph_store=instance, graph_store_factory=factory)
    try:
        assert services.graph_store is instance
        assert factory_built == []  # factory never consulted
    finally:
        await _teardown(services)


async def test_default_path_builds_sqlite_graph_store(minimal_vault_config_dict):
    """T7b: with no injection, the default concrete SqliteGraphStore is built.

    Trap: if the default branch were wired to the wrong class (e.g. left as the
    ABC, or a stub), this isinstance check fails. This is the no-local-behavior-
    change guarantee.
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    services = await _init_with_stubs(config)
    try:
        assert isinstance(services.graph_store, SqliteGraphStore)
        assert services.graph_store_factory is None
    finally:
        await _teardown(services)


async def test_injected_store_not_closed_on_failure(minimal_vault_config_dict, monkeypatch):
    """T7c: a caller-supplied store is NOT closed by failure cleanup.

    initialize_services only closes the store it constructs itself
    (graph_store_owned_here). An injected store is the caller's to close.

    Trap: if cleanup closed the local graph_store regardless of ownership, the
    stub's close_calls would be > 0. This guards the ownership split added
    alongside the injection. (The complement — the OWNED default store IS closed
    on failure — is covered by test_initialize_services_cleanup.py::N6.)
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    stub = StubGraphStore()

    from sage.api.errors import SAGEError
    from sage.services.user_service import UserService

    async def raising_bootstrap(self):
        raise SAGEError(
            code="schema_migration_required",
            message="T7c failure injection",
            status_code=409,
        )

    monkeypatch.setattr(UserService, "bootstrap_owner", raising_bootstrap)

    with pytest.raises(SAGEError, match="T7c failure injection"):
        await _init_with_stubs(config, graph_store=stub)

    assert stub.close_calls == 0
