"""Access Control tests: BH-009 through BH-011.

Covers vault owner auto-registration, ROOT Harness agent registration
(boundary test stub), and direct SAGE register_user with user_type=agent.
"""

from sage.models.enums import UserType
from sage.models.schemas import RegisterUserRequest

# ---------------------------------------------------------------------------
# BH-009: Vault owner auto-registered at initialization
# ---------------------------------------------------------------------------


async def test_bh_009_vault_owner_auto_registered(graph_store, user_service):
    """Vault init reads owner from config and auto-creates user record."""
    owner = await user_service.bootstrap_owner()

    assert owner.display_name == "testuser"
    assert owner.user_type == UserType.HUMAN
    assert owner.id  # non-empty

    # Verify the user exists in the graph store
    users = await graph_store.list_users()
    assert len(users) == 1
    assert users[0].display_name == "testuser"
    assert users[0].user_type == UserType.HUMAN

    # Idempotent: second call returns the same user
    owner2 = await user_service.bootstrap_owner()
    assert owner2.id == owner.id


# ---------------------------------------------------------------------------
# BH-010: ROOT Harness register_agent creates SAGE user (boundary test)
#
# This is a cross-cutting boundary test. For this slice, we verify that
# SAGE's register_user endpoint accepts user_type=agent correctly (BH-011
# covers the implementation; this test documents the boundary contract).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# BH-011: Direct SAGE register_user with user_type=agent succeeds
# ---------------------------------------------------------------------------


async def test_bh_011_register_user_agent(user_service):
    """SAGE register_user accepts user_type=agent."""
    request = RegisterUserRequest(display_name="test_agent", user_type=UserType.AGENT)
    user = await user_service.register_user(request)

    assert user.display_name == "test_agent"
    assert user.user_type == UserType.AGENT
    assert user.id  # non-empty
    assert user.created_at is not None
