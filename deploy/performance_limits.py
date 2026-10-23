"""Carry one caller deadline through asynchronous and blocking controller work."""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar

_DEADLINE = ContextVar("performance_deadline", default=None)


def remaining(maximum):
    deadline = _DEADLINE.get()
    value = maximum if deadline is None else min(maximum, deadline - time.monotonic())
    if value <= 0:
        raise TimeoutError("approved performance operation deadline exceeded")
    return value


@contextmanager
def deadline_scope(seconds):
    prior = _DEADLINE.get()
    deadline = time.monotonic() + seconds
    token = _DEADLINE.set(deadline if prior is None else min(prior, deadline))
    try:
        remaining(seconds)
        yield
    finally:
        _DEADLINE.reset(token)
