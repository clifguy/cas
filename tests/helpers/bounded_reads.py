"""Refuse whole-file reads of one path for the duration of a call under test.

The control a streaming test needs. Trapping ``Path.read_bytes`` alone is not
it: ``handle.read()`` with no size, ``readlines()``, iteration, a ``builtins.open``
instead of a ``Path.open``, a raw ``os.read``, or a ``shutil.copyfile`` each hold
or move the file whole while never touching ``read_bytes``, so a test that traps
only the obvious call passes against an implementation that materializes the
file by any other route. This installs the whole set through ``monkeypatch``,
so the refusals lift with the test's context:

* ``Path.read_bytes`` on the watched path raises outright.
* Every handle opened on the watched path -- through ``Path.open``, ``open``,
  or ``io.open``, which are one route -- must name a read size no larger than
  ``max_read``; ``readline``, ``readlines``, ``readinto``, ``read1`` and
  iteration are refused, since each is an unbounded read in disguise.
* ``os.open`` on the watched path and ``shutil.copyfile`` (which ``copy`` and
  ``copy2`` route through) with the watched path as source are refused.

Only the watched path is bounded; every other path behaves as usual, so a call
under test that legitimately loads a fixture whole is not tripped. Outside the
trap by construction: a file descriptor obtained before the trap was installed,
and ``mmap``.
"""

from __future__ import annotations

import builtins
import io
import os
import shutil
from pathlib import Path
from typing import Any

import pytest


class WholeFileReadError(AssertionError):
    """Raised when the code under test reads the watched file whole."""


def _refused(path: object, how: str) -> WholeFileReadError:
    return WholeFileReadError(
        f"{how} on {path}: the watched file may only be read in bounded chunks"
    )


class _BoundedHandle:
    """A file object whose reads must be bounded by an explicit size."""

    def __init__(self, handle: Any, path: object, max_read: int) -> None:
        self._handle = handle
        self._path = path
        self._max_read = max_read

    def read(self, size: int | None = -1) -> Any:
        if size is None or size < 0 or size > self._max_read:
            raise _refused(self._path, f"read({size})")
        return self._handle.read(size)

    def read1(self, size: int | None = -1) -> Any:
        raise _refused(self._path, "read1()")

    def readline(self, size: int | None = -1) -> Any:
        raise _refused(self._path, "readline()")

    def readlines(self, hint: int = -1) -> Any:
        raise _refused(self._path, "readlines()")

    def readinto(self, buffer: Any) -> Any:
        raise _refused(self._path, "readinto()")

    def __iter__(self) -> Any:
        raise _refused(self._path, "iteration")

    def __enter__(self) -> _BoundedHandle:
        self._handle.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self._handle.__exit__(*exc)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handle, name)


def _names(target: object, resolved: Path) -> bool:
    """Whether ``target`` (a path-like, not a descriptor) resolves to the watched file."""
    if isinstance(target, int):
        return False
    try:
        return Path(os.fsdecode(target)).resolve() == resolved
    except (TypeError, ValueError, OSError):
        return False


def refuse_whole_reads(monkeypatch: pytest.MonkeyPatch, watched: Path, max_read: int) -> None:
    """Refuse every whole-file read of ``watched``; allow reads bounded by ``max_read``."""
    resolved = watched.resolve()
    real_open = builtins.open
    real_os_open = os.open
    real_copyfile = shutil.copyfile

    def _refuse_read_bytes(self: Path) -> bytes:
        if self.resolve() == resolved:
            raise _refused(self, "read_bytes()")
        with real_open(self, "rb") as f:
            return f.read()

    def _bounded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        handle = real_open(file, mode, *args, **kwargs)
        if "r" not in mode or not _names(file, resolved):
            return handle
        return _BoundedHandle(handle, file, max_read)

    def _guarded_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if _names(path, resolved):
            raise _refused(path, "os.open()")
        return real_os_open(path, flags, *args, **kwargs)

    def _guarded_copyfile(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        if _names(src, resolved):
            raise _refused(src, "shutil.copyfile()")
        return real_copyfile(src, dst, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", _refuse_read_bytes)
    # ``builtins.open`` and ``io.open`` are the same object under two names, and
    # ``Path.open`` calls ``io.open``; patching both names covers all three routes.
    monkeypatch.setattr(builtins, "open", _bounded_open)
    monkeypatch.setattr(io, "open", _bounded_open)
    monkeypatch.setattr(os, "open", _guarded_os_open)
    monkeypatch.setattr(shutil, "copyfile", _guarded_copyfile)
