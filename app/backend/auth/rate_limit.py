"""Per-client rate limit for starting interactive sign-in.

Starting sign-in is unauthenticated and each start costs an identity-provider
round-trip and a pre-login record, so it is limited per client: a sliding
window of recent starts per key, refused once the window holds its limit. The
limiter is process-local, so each replica enforces it independently; the
effective ceiling for a client is the limit times the number of replicas that
client reaches.

The client key is the address the container ingress appends to
``X-Forwarded-For``: the rightmost entry, which a client cannot forge because
the ingress writes it. Entries to its left are client-supplied and ignored.
Without the header (a direct, unproxied request) the peer address is the key.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable

from starlette.requests import Request

DEFAULT_LOGIN_LIMIT = 10
DEFAULT_LOGIN_WINDOW_SECONDS = 60.0
# Upper bound on tracked clients, so a flood of distinct addresses cannot grow
# the limiter without bound; the stalest client is forgotten first.
DEFAULT_MAX_TRACKED_CLIENTS = 10_000


def client_key(request: Request) -> str:
    """Return the rate-limit key for ``request``: the ingress-appended address."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        rightmost = forwarded.rsplit(",", 1)[-1].strip()
        if rightmost:
            return rightmost
    return request.client.host if request.client else "unknown"


class LoginRateLimiter:
    """Sliding-window limiter keyed by client."""

    def __init__(
        self,
        *,
        limit: int = DEFAULT_LOGIN_LIMIT,
        window_seconds: float = DEFAULT_LOGIN_WINDOW_SECONDS,
        max_clients: int = DEFAULT_MAX_TRACKED_CLIENTS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._max_clients = max_clients
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> int | None:
        """Record a start for ``key``, or refuse it.

        Returns ``None`` when the start is admitted, else the whole number of
        seconds until the oldest start in the window ages out.
        """
        now = self._clock()
        with self._lock:
            hits = self._hits.pop(key, None) or deque()
            while hits and hits[0] <= now - self._window:
                hits.popleft()
            # Re-inserting moves the key to the newest end of the dict's order.
            self._hits[key] = hits
            if len(hits) >= self._limit:
                return max(1, math.ceil(hits[0] + self._window - now))
            hits.append(now)
            while len(self._hits) > self._max_clients:
                del self._hits[next(iter(self._hits))]
            return None
