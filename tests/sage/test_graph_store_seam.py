"""GraphStore adapter-seam contract: ABC, concrete impl, stub, and injection.

Proves the graph store has a real swappable seam: a ``GraphStore`` ABC port, a
concrete ``PostgresGraphStore`` implementing it, a hermetic ``StubGraphStore``,
and ``initialize_services`` injection mirroring the content-store seam. The
structural tests (T1-T5) guard the port surface -- T5 compares every port
method's signature against both bindings, so neither can drift alone; the
substitutability tests (T6-T7) prove a stub can stand in for the concrete store
end to end.
"""

from __future__ import annotations

import hashlib
import inspect
from datetime import datetime, timezone

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
from sage.models.schemas import Document, PipelineStatus, SourceType
from sage.storage.postgres.graph_store import PostgresGraphStore
from tests.helpers.seam_signatures import (
    assert_signature_conforms,
    parametrized_values,
    port_surface,
    public_members,
)

# Public methods on the concrete store that are intentionally NOT part of the
# port. Deliberately empty: the Postgres store's public surface is exactly the
# port. Any public concrete method that drifts out of the ABC is a seam
# regression (see T4); a backend-specific helper belongs under a leading
# underscore or, if consumers need it, on the port itself.
POSTGRES_ONLY_METHODS: frozenset[str] = frozenset()

# Public methods on the stub that are intentionally NOT part of the port.
# Deliberately empty, like POSTGRES_ONLY_METHODS: a test-inspection helper
# belongs under a leading underscore or in the test that needs it (see T4b).
STUB_ONLY_METHODS: frozenset[str] = frozenset()

# (binding, method) pairs whose signature is known to differ from the port's.
# Deliberately empty: both bindings carry the port's signatures exactly. A
# drift belongs fixed in the binding; pinning one here needs a reason beside
# it, and a pin whose drift is later fixed fails T5, so the set can only
# shrink.
KNOWN_SIGNATURE_DIVERGENCES: frozenset[tuple[type, str]] = frozenset()


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


def test_postgres_graph_store_is_concrete_graphstore():
    """T2: PostgresGraphStore implements the full port (no abstract leftovers).

    Trap: a single un-implemented abstract method flips isabstract back to
    True and makes the class un-instantiable — exactly the failure mode of a
    50-method port that is easy to under-fill.
    """
    assert issubclass(PostgresGraphStore, GraphStore)
    assert not inspect.isabstract(PostgresGraphStore)


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

    ``public(PostgresGraphStore) - POSTGRES_ONLY_METHODS`` must equal the ABC's
    port surface. Trap: if a service-consumed method were dropped from the ABC,
    it would appear in this difference (and not in POSTGRES_ONLY_METHODS),
    failing the test. Without this guard, the stub could "pass" merely by also
    omitting the method. This is the strongest structural guard on the seam,
    and with an empty divergence set it also proves the concrete store exposes
    nothing backend-specific.
    """
    concrete_public = public_members(PostgresGraphStore)
    abc_methods = set(GraphStore.__abstractmethods__)
    surface = port_surface(GraphStore)

    # Every abstract method is implemented as a public concrete method.
    assert abc_methods <= concrete_public
    # No public concrete method exists outside the port.
    assert concrete_public - surface == POSTGRES_ONLY_METHODS
    # And nothing in the divergence list leaked into the port.
    assert surface.isdisjoint(POSTGRES_ONLY_METHODS)


def test_stub_surface_matches_port():
    """T4b: the stub exposes exactly the port, plus STUB_ONLY_METHODS.

    Trap: T4 bounds the concrete store's public surface and nothing bounds the
    stub's, so a convenience method added to the stub alone -- one a service
    could come to call, and the durable store would not answer -- passes T3 and
    every signature check, which read only port methods.
    """
    stub_public = public_members(StubGraphStore)
    surface = port_surface(GraphStore)
    assert set(GraphStore.__abstractmethods__) <= stub_public
    assert stub_public - surface == STUB_ONLY_METHODS
    assert surface.isdisjoint(STUB_ONLY_METHODS)


@pytest.mark.parametrize("method_name", sorted(port_surface(GraphStore)))
def test_concrete_signature_matches_port(method_name):
    """T5: each concrete method's signature matches the port's exactly.

    Trap: a parameter rename, default change, or return-type drift between the
    port and the concrete would break substitutability silently while T2-T4 stay
    green. Strict signature equality surfaces it per method. Defaulted port
    methods are included so an override cannot drift from the port shape; one
    the concrete store inherits unchanged conforms by identity rather than by
    comparing the port's function to itself.
    """
    assert_signature_conforms(
        GraphStore, PostgresGraphStore, method_name, divergences=KNOWN_SIGNATURE_DIVERGENCES
    )


@pytest.mark.parametrize("method_name", sorted(port_surface(GraphStore)))
def test_stub_signature_matches_port(method_name):
    """T5 (stub): each stub method's signature matches the port's exactly.

    The stub is what the substitutability tests stand a service on, so a stub
    signature that admits a call the port forbids lets a test exercise a shape
    the durable store would reject -- a keyword-only preference left positional,
    or a required parameter given a default, quietly lets a caller omit the
    rule. T3 is name-based and cannot see either. The stub module does not
    stringize its annotations where the concrete one does, which is why the
    comparison resolves them first.
    """
    assert_signature_conforms(
        GraphStore, StubGraphStore, method_name, divergences=KNOWN_SIGNATURE_DIVERGENCES
    )


def test_signature_gate_covers_every_port_method_on_both_bindings():
    """T5b: both T5 arms are parametrized over the whole port surface.

    Trap: a parametrization narrowed to the abstract set, or a stub arm that
    lost its cases, keeps T5 green while half the seam goes unchecked. The
    surface is read back from the collected parametrization, not restated.
    """
    surface = sorted(port_surface(GraphStore))
    assert set(GraphStore.__abstractmethods__) < set(surface)  # defaults included
    for arm in (test_concrete_signature_matches_port, test_stub_signature_matches_port):
        assert parametrized_values(arm, "method_name") == surface


def test_stub_signature_mutation_turns_the_gate_red(monkeypatch):
    """T5c: a deliberate drift on the real stub fails the gate it runs through.

    Without this probe a gate that silently compared a method to itself would
    pass for the wrong reason. Two shapes, on the method whose preference must
    stay keyword-only and required: made positional, and given a default.
    """

    async def positional(
        self, hashes: list[str], prefer_lifecycle_statuses: frozenset[str]
    ) -> dict[str, str]:
        return {}

    async def defaulted(
        self, hashes: list[str], *, prefer_lifecycle_statuses: frozenset[str] = frozenset()
    ) -> dict[str, str]:
        return {}

    for mutant in (positional, defaulted):
        monkeypatch.setattr(StubGraphStore, "find_documents_by_hashes", mutant)
        with pytest.raises(AssertionError, match="StubGraphStore.find_documents_by_hashes"):
            test_stub_signature_matches_port("find_documents_by_hashes")


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
    """T6: an injected StubGraphStore stands in for the concrete store end to end.

    The services must hold the exact injected instance (not a freshly-built
    concrete store), and the write performed during init (bootstrap_owner)
    must land in the stub.

    Trap: if initialize_services ignored the injection and built its own
    default store, ``services.graph_store is stub`` fails. If it stored the
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

    Trap: if the factory result were discarded (default store built instead),
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


# --------------------------------------------------------------------------- #
# Substitutability of the answer, not just the surface (T8)
# --------------------------------------------------------------------------- #

# The vocabulary the services pass is the vault's own; here it is a literal, so
# the two bindings are compared against a stated rule rather than against
# whatever a lifecycle table currently declares.
_SEAM_SURVIVING_STATES = frozenset({"active", "completed"})


def _seam_doc(doc_id: str, content_hash: str, lifecycle_status: str = "active") -> Document:
    """A minimal document pinned to an explicit id, hash, and lifecycle state."""
    now = datetime.now(timezone.utc)
    return Document(
        id=doc_id,
        title=f"Doc {doc_id}",
        source_type=SourceType.MARKDOWN,
        source_path=f"test/seam/{doc_id}.md",
        lifecycle_status=lifecycle_status,
        source_content_hash=f"sha256:{hashlib.sha256(content_hash.encode()).hexdigest()}",
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
    )


async def test_hash_lookup_agrees_between_stub_and_concrete(graph_store):
    """T8: both bindings resolve a shared hash to the same document.

    The bulk hash lookup is the one port method where the two bindings could
    each be internally consistent and still disagree: the durable store
    answered in scan order and the stub in insertion order, so neither stated
    a rule and a service swapping one for the other changed its answer.

    Two assertions, and the second is what makes the first mean anything.
    Agreement alone passes when both sides are broken the same way, and
    passes trivially on ``{} == {}``; pinning the expected ids as well says
    the shared answer is also the right one. Documents are inserted in an
    order that is neither id order nor answer order, so "first inserted wins"
    cannot masquerade as either rule.
    """
    retired_low = _seam_doc("00000001_seam_retired", "seam_h1", lifecycle_status="archived")
    surviving_high = _seam_doc("00000002_seam_surviving", "seam_h1")
    active_low = _seam_doc("00000003_seam_active_low", "seam_h2")
    active_high = _seam_doc("00000004_seam_active_high", "seam_h2")
    corpus = [active_high, retired_low, active_low, surviving_high]

    stub = StubGraphStore()
    for doc in corpus:
        await stub.insert_document(doc)
        await graph_store.insert_document(doc)

    hashes = [retired_low.source_content_hash, active_low.source_content_hash]
    from_stub = await stub.find_documents_by_hashes(
        hashes, prefer_lifecycle_statuses=_SEAM_SURVIVING_STATES
    )
    from_concrete = await graph_store.find_documents_by_hashes(
        hashes, prefer_lifecycle_statuses=_SEAM_SURVIVING_STATES
    )

    assert from_stub == from_concrete
    assert from_concrete == {
        retired_low.source_content_hash: surviving_high.id,
        active_low.source_content_hash: active_low.id,
    }


async def test_hash_lookup_tie_break_agrees_on_ids_the_two_comparators_order_differently(
    graph_store,
):
    """T8c: the id tie-break is byte order in both bindings, not the locale's.

    Every other fixture in this file and in the store's own group pins ids
    whose eight-hex prefixes differ, so the tie-break arm never runs and no
    assertion anywhere reaches the comparator. These two ids tie on the prefix
    and differ only at a character the two comparators order oppositely: the
    server's default collation ranks ``_`` before ``2`` here, while byte order
    and Python both rank it after. Reproduced against the test server before
    this test was written.

    Trap, and it is the one that matters: remove ``COLLATE "C"`` from the
    store's ORDER BY and this goes red while the entire rest of the suite
    stays green -- the divergence needs a prefix tie to become visible at all.
    A store left on the locale also answers differently on a macOS server and
    on a Linux one, so a green suite on one would say nothing about the other.
    """
    shared = "seam_collation"
    under = _seam_doc("0a1b2c3d_v_2", shared)
    digit = _seam_doc("0a1b2c3d_v2a", shared)

    stub = StubGraphStore()
    for doc in (under, digit):
        await stub.insert_document(doc)
        await graph_store.insert_document(doc)

    hashes = [under.source_content_hash]
    from_stub = await stub.find_documents_by_hashes(
        hashes, prefer_lifecycle_statuses=_SEAM_SURVIVING_STATES
    )
    from_concrete = await graph_store.find_documents_by_hashes(
        hashes, prefer_lifecycle_statuses=_SEAM_SURVIVING_STATES
    )

    assert from_stub == from_concrete
    # "2" (0x32) sorts below "_" (0x5f), so the digit-bearing id is lowest.
    assert from_concrete == {under.source_content_hash: digit.id}
