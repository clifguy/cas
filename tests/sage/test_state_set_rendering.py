"""One renderer serves every lifecycle state set a caller is shown.

Two API surfaces report a set of lifecycle states as a precondition --
the `check_preconditions` payload's `required` field and the
`supersede_target_not_active` error's `required_state` -- and both
promise the set is derived from the vault's configuration. Rendered
independently they drift: ordering, separator, and above all the
empty-set policy, which one of them once answered with a hardcoded
`active` the vault may not permit.

These tests pin the shared renderer's contract (sorted, `" or "`-joined,
one documented placeholder for the empty set), the removal of the
`active` fallback, and the parity of the two surfaces on the same set.
"""

from types import SimpleNamespace

from sage.api.errors import SupersedeTargetNotActiveError
from sage.config import (
    EMPTY_STATE_SET_RENDERING,
    LifecycleTransition,
    TransitionTable,
    render_state_set,
)
from sage.services.graph_ops import GraphOpsService


def test_render_state_set_sorts_and_joins():
    """Declaration order does not reach the caller; the rendering is stable."""
    assert render_state_set(["completed", "active"]) == "active or completed"
    assert render_state_set(frozenset({"filed", "active"})) == "active or filed"
    assert render_state_set(["active"]) == "active"


def test_render_state_set_empty_returns_documented_sentinel():
    """The empty set renders as the one documented placeholder, not as ''."""
    assert render_state_set([]) == EMPTY_STATE_SET_RENDERING
    assert render_state_set(frozenset()) == EMPTY_STATE_SET_RENDERING


def test_supersede_error_reports_no_state_when_table_permits_none():
    """A table with no `supersede` row reports no state, not `active`.

    The fallback this replaces answered the empty set with a state the
    vault may never permit supersede from, which is the hardcoded-`active`
    assumption the config-derived precondition exists to remove.
    """
    table = TransitionTable(
        [LifecycleTransition(from_state="active", action="archive", to_state="archived")]
    )
    error = SupersedeTargetNotActiveError("doc_1", "archived", table.states_allowing("supersede"))
    assert error.detail["allowed_states"] == []
    assert error.detail["required_state"] == EMPTY_STATE_SET_RENDERING
    assert EMPTY_STATE_SET_RENDERING in error.message


async def test_precondition_payload_and_supersede_error_render_identically(minimal_config):
    """The same state set reads the same on both surfaces.

    The regression guard for the divergence: either call site growing its
    own join, ordering, or empty-set answer turns this red.
    """
    function_id = "a1b2c3d4_doc_function"

    async def get_document(doc_id):
        return SimpleNamespace(id=doc_id) if doc_id == function_id else None

    async def get_edges_by_source(source_id, edge_type):
        return [SimpleNamespace(target_id="e5f6a7b8_doc_dependency")]

    store = SimpleNamespace(get_document=get_document, get_edges_by_source=get_edges_by_source)
    service = GraphOpsService(store, minimal_config)

    result = await service.check_preconditions(function_id)
    satisfying = minimal_config.lifecycle.dependency_satisfying_states()
    error = SupersedeTargetNotActiveError("doc_1", "archived", sorted(satisfying, reverse=True))

    assert result.checks[0].required == error.detail["required_state"] == "active or completed"
