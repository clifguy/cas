"""ContentStore adapter-seam contract: port, both bindings, and their signatures.

The content store has the same two-binding shape as the graph store: a
``ContentStore`` ABC port, a durable ``PostgresContentStore``, and a hermetic
``StubContentStore`` that services are stood on in tests. A binding whose
signature admits a call the port forbids lets a test exercise a shape the
other binding would reject, so every port method's signature is compared
against each binding, and neither can drift alone.

The structural tests (CS1-CS4) guard that each binding implements the whole
port and exposes nothing outside it but a named allowance; CS5 compares
signatures. This module is the structural mirror of
tests/sage/test_graph_store_seam.py.
"""

from __future__ import annotations

import inspect

import pytest

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.adapters.interfaces import ContentStore, SearchResult
from sage.adapters.stubs import StubContentStore
from tests.helpers.seam_signatures import (
    assert_signature_conforms,
    parametrized_values,
    port_surface,
    public_members,
)

_BINDINGS = [PostgresContentStore, StubContentStore]

# Public methods a binding exposes that are intentionally NOT part of the port,
# keyed by binding, each with its reason. The Postgres binding's public surface
# is exactly the port. The stub carries one read-back accessor so tests can
# assert what a service wrote to the document-level surface, which the port
# deliberately offers no way to read row by row. Any other public method that
# drifts out of the port is a seam regression (CS4); the sets can only shrink.
BINDING_ONLY_METHODS: dict[type, frozenset[str]] = {
    PostgresContentStore: frozenset(),
    StubContentStore: frozenset({"stored_document_surface"}),
}

# (binding, method) pairs whose signature is known to differ from the port's.
# Deliberately empty: both bindings carry the port's signatures exactly. A
# drift belongs fixed in the binding; pinning one here needs a reason beside
# it, and a pin whose drift is later fixed fails CS5, so the set can only
# shrink.
KNOWN_SIGNATURE_DIVERGENCES: frozenset[tuple[type, str]] = frozenset()


# --------------------------------------------------------------------------- #
# Structural contract (CS1-CS4)
# --------------------------------------------------------------------------- #


def test_cs1_port_is_abstract():
    """CS1: ContentStore is a genuine ABC and cannot be instantiated.

    Trap: were it a plain class with no @abstractmethod members, the seam would
    be a port in name only and CS4's completeness half would assert nothing.
    """
    assert inspect.isabstract(ContentStore)
    assert len(ContentStore.__abstractmethods__) > 0
    with pytest.raises(TypeError):
        ContentStore()  # type: ignore[abstract]


@pytest.mark.parametrize("binding", _BINDINGS, ids=lambda b: b.__name__)
def test_cs2_binding_is_concrete(binding):
    """CS2: each binding implements the full port (no abstract leftovers).

    Trap: a single un-implemented abstract method flips isabstract back to True.
    The stub is also instantiated, since it is what services are stood on.
    """
    assert issubclass(binding, ContentStore)
    assert not inspect.isabstract(binding)
    if binding is StubContentStore:
        StubContentStore()  # must not raise


def test_cs3_every_binding_carries_a_named_allowance():
    """CS3: the allowance table names exactly the bindings under test.

    Trap: a binding added to ``_BINDINGS`` without an entry would be checked
    against an implicit empty allowance, and an entry left for a removed
    binding would be dead weight nobody prunes.
    """
    assert set(BINDING_ONLY_METHODS) == set(_BINDINGS)


@pytest.mark.parametrize("binding", _BINDINGS, ids=lambda b: b.__name__)
def test_cs4_binding_surface_matches_port(binding):
    """CS4: each binding's public surface is the port plus its named allowance.

    Trap: a public method added to a binding and never to the port -- the way a
    stub grows a convenience that services then start calling -- falls outside
    the port and outside the allowance. An allowance entry the binding no
    longer defines fails too, so the allowance can only shrink. And every
    abstract port method must be defined directly on the binding.
    """
    surface = public_members(binding)
    extras = surface - port_surface(ContentStore)
    assert extras == BINDING_ONLY_METHODS[binding], (
        f"{binding.__name__}: public methods outside the port {sorted(extras)} "
        f"!= allowance {sorted(BINDING_ONLY_METHODS[binding])}"
    )
    assert set(ContentStore.__abstractmethods__) <= surface
    assert port_surface(ContentStore).isdisjoint(BINDING_ONLY_METHODS[binding])


# --------------------------------------------------------------------------- #
# Signature contract (CS5)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("binding", _BINDINGS, ids=lambda b: b.__name__)
@pytest.mark.parametrize("method_name", sorted(port_surface(ContentStore)))
def test_cs5_binding_signature_matches_port(binding, method_name):
    """CS5: each binding method's signature matches the port's exactly.

    Trap: a parameter rename, default change, or return-type drift between the
    port and either binding breaks substitutability silently -- nothing else
    compares them. The Postgres binding stringizes its annotations and the stub
    does not, so the comparison resolves them first.
    """
    assert_signature_conforms(
        ContentStore, binding, method_name, divergences=KNOWN_SIGNATURE_DIVERGENCES
    )


def test_cs5b_signature_gate_covers_every_port_method_on_both_bindings():
    """CS5b: CS5 is parametrized over the whole port surface and both bindings.

    Trap: a bindings list that lost the stub, or a method list narrowed below
    the port, keeps CS5 green while part of the seam goes unchecked. Both are
    read back from the collected parametrization, not restated.
    """
    assert parametrized_values(test_cs5_binding_signature_matches_port, "binding") == [
        PostgresContentStore,
        StubContentStore,
    ]
    assert parametrized_values(test_cs5_binding_signature_matches_port, "method_name") == sorted(
        port_surface(ContentStore)
    )


def test_cs5c_stub_signature_mutation_turns_the_gate_red(monkeypatch):
    """CS5c: a deliberate drift on the real stub fails the gate it runs through.

    Without this probe a gate that silently compared a method to itself would
    pass for the wrong reason. The port carries no keyword-only parameter, so
    the drift is the other shape a stub can take: a required parameter given a
    default.
    """

    async def defaulted(
        self,
        query: str = "",
        limit: int = 10,
        filters: dict[str, str | list[str]] | None = None,
    ) -> list[SearchResult]:
        return []

    monkeypatch.setattr(StubContentStore, "search_bm25", defaulted)
    with pytest.raises(AssertionError, match="StubContentStore.search_bm25"):
        test_cs5_binding_signature_matches_port(StubContentStore, "search_bm25")
