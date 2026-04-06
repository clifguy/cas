"""User registration and vault owner bootstrap (BH-009, BH-011)."""

import uuid
from datetime import datetime, timezone

from sage.config import VaultConfig
from sage.models.enums import UserType
from sage.models.schemas import RegisterUserRequest, User
from sage.storage.graph_store import GraphStore


class UserService:
    def __init__(self, graph_store: GraphStore, config: VaultConfig) -> None:
        self._store = graph_store
        self._config = config

    async def bootstrap_owner(self) -> User:
        """Auto-register vault owner from config (BH-009).

        Reads vault.owner from config and creates a user record with
        type=human. Idempotent: returns existing user if already present.
        """
        owner_name = self._config.vault.owner
        existing = await self._store.get_user_by_display_name(owner_name)
        if existing is not None:
            return existing

        user = User(
            id=str(uuid.uuid4()),
            display_name=owner_name,
            type=UserType.HUMAN,
            created_at=datetime.now(timezone.utc),
        )
        await self._store.insert_user(user)
        return user

    async def register_user(self, request: RegisterUserRequest) -> User:
        """Register a new user (human or agent) (BH-011)."""
        user = User(
            id=str(uuid.uuid4()),
            display_name=request.display_name,
            type=request.type,
            created_at=datetime.now(timezone.utc),
        )
        await self._store.insert_user(user)
        return user
