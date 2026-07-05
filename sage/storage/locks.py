"""Per-document asyncio lock manager for concurrency control (BH-005, BH-006).

Application-level locking: different documents can be written concurrently;
writes to the same document are serialized. Prevents callers from seeing
raw store-level lock-contention errors.
"""

import asyncio
from collections import defaultdict


class DocumentLockManager:
    """Per-document asyncio locks using lazy initialization."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def lock(self, document_id: str) -> asyncio.Lock:
        """Return the lock for use as an async context manager."""
        return self._locks[document_id]
