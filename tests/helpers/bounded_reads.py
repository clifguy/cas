"""Refuse whole-file reads of one path for the duration of a call under test.

The control a streaming test needs. Trapping ``Path.read_bytes`` alone is not
it: ``handle.read()`` with no size, or with a size above the streaming chunk,
holds the file just as whole while never touching ``read_bytes``, so a test
that traps only the obvious call passes against an implementation that
materializes the file through an open handle. This installs both refusals --
``read_bytes`` raises outright, and every read on a handle opened for the
watched path must name a size no larger than ``max_read`` -- through
``monkeypatch``, so they lift with the test's context.

Only the watched path is bounded. Every other open in the call under test
(configuration files, fixtures, the store's own copies) behaves as usual.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


class WholeFileReadError(AssertionError):
    """Raised when the code under test reads the watched file whole."""


class _BoundedHandle:
    """A file object whose reads must be bounded by an explicit size."""

    def __init__(self, handle: Any, path: Path, max_read: int) -> None:
        self._handle = handle
        self._path = path
        self._max_read = max_read

    def read(self, size: int | None = -1) -> bytes:
        if size is None or size < 0 or size > self._max_read:
            raise WholeFileReadError(
                f"read({size}) on {self._path}: reads must be bounded by {self._max_read}"
            )
        return self._handle.read(size)

    def __enter__(self) -> _BoundedHandle:
        self._handle.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self._handle.__exit__(*exc)

    def __iter__(self):
        return iter(self._handle)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handle, name)


def refuse_whole_reads(monkeypatch: pytest.MonkeyPatch, watched: Path, max_read: int) -> None:
    """Refuse ``read_bytes`` on any path and unbounded reads on ``watched``."""
    resolved = watched.resolve()
    real_open = Path.open

    def _refuse_read_bytes(self: Path) -> bytes:
        raise WholeFileReadError(f"whole-file read of {self}")

    def _bounded_open(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        handle = real_open(self, mode, *args, **kwargs)
        if "r" not in mode or self.resolve() != resolved:
            return handle
        return _BoundedHandle(handle, self, max_read)

    monkeypatch.setattr(Path, "read_bytes", _refuse_read_bytes)
    monkeypatch.setattr(Path, "open", _bounded_open)
