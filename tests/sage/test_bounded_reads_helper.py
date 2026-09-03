"""The whole-file-read trap the streaming tests rest on.

A streaming assertion is only as strong as the trap behind it: a helper that
refuses ``Path.read_bytes`` and nothing else lets an implementation read the
file whole through ``open()`` and pass. These tests enumerate the ways a file
can be read whole and require the helper to refuse each, so the streaming
tests' claim ("this code never held the file whole") does not rest on the
production code happening to pick the one API the trap watches.
"""

from __future__ import annotations

import builtins
import io
import os
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.helpers.bounded_reads import WholeFileReadError, refuse_whole_reads

MAX_READ = 1024
PAYLOAD = bytes(range(256)) * 20  # 5 KiB: several bounded reads, one unbounded one


@pytest.fixture
def watched(tmp_path: Path) -> Path:
    p = tmp_path / "watched.bin"
    p.write_bytes(PAYLOAD)
    return p


def _path_read_bytes(p: Path) -> None:
    p.read_bytes()


def _path_open_unsized_read(p: Path) -> None:
    with p.open("rb") as f:
        f.read()


def _path_open_oversized_read(p: Path) -> None:
    with p.open("rb") as f:
        f.read(MAX_READ + 1)


def _builtins_open_read(p: Path) -> None:
    with builtins.open(str(p), "rb") as f:
        f.read()


def _io_open_read(p: Path) -> None:
    with io.open(p, "rb") as f:
        f.read()


def _text_mode_read(p: Path) -> None:
    with open(p, encoding="latin-1") as f:
        f.read()


def _readlines(p: Path) -> None:
    with p.open("rb") as f:
        f.readlines()


def _readline(p: Path) -> None:
    with p.open("rb") as f:
        f.readline()


def _iteration(p: Path) -> None:
    with p.open("rb") as f:
        list(f)


def _readinto(p: Path) -> None:
    with p.open("rb") as f:
        f.readinto(bytearray(len(PAYLOAD) * 2))


def _read1(p: Path) -> None:
    with p.open("rb") as f:
        f.read1()


def _os_open_read(p: Path) -> None:
    fd = os.open(p, os.O_RDONLY)
    try:
        os.read(fd, len(PAYLOAD) * 2)
    finally:
        os.close(fd)


def _shutil_copyfile(p: Path) -> None:
    shutil.copyfile(p, p.with_name("copy.bin"))


def _shutil_copy2(p: Path) -> None:
    shutil.copy2(p, p.with_name("copy2.bin"))


BYPASSES: dict[str, Callable[[Path], None]] = {
    "Path.read_bytes": _path_read_bytes,
    "Path.open().read()": _path_open_unsized_read,
    "Path.open().read(oversized)": _path_open_oversized_read,
    "builtins.open().read()": _builtins_open_read,
    "io.open().read()": _io_open_read,
    "open(text).read()": _text_mode_read,
    "readlines": _readlines,
    "readline": _readline,
    "iteration": _iteration,
    "readinto": _readinto,
    "read1": _read1,
    "os.open+os.read": _os_open_read,
    "shutil.copyfile": _shutil_copyfile,
    "shutil.copy2": _shutil_copy2,
}


@pytest.mark.parametrize("route", sorted(BYPASSES))
def test_every_whole_file_route_is_refused(watched, monkeypatch, route):
    """Each way of reading the watched file whole raises the trap's error.

    Anti-coincidental-pass: every route is first shown to read the file whole
    *without* the trap (the positive control below), so a route that raised
    for an unrelated reason -- a wrong signature, say -- would fail that
    control rather than pass this one by accident.
    """
    BYPASSES[route](watched)  # positive control: legal before the trap

    refuse_whole_reads(monkeypatch, watched, MAX_READ)
    with pytest.raises(WholeFileReadError):
        BYPASSES[route](watched)


def test_bounded_reads_reassemble_the_file(watched, monkeypatch):
    """Reads bounded by ``max_read`` are the one route left open, and they
    still deliver the file intact through both ``Path.open`` and ``open``."""
    refuse_whole_reads(monkeypatch, watched, MAX_READ)

    with watched.open("rb") as f:
        chunks = list(iter(lambda: f.read(MAX_READ), b""))
    assert b"".join(chunks) == PAYLOAD
    assert max(len(c) for c in chunks) <= MAX_READ

    with open(watched, "rb") as f:
        assert b"".join(iter(lambda: f.read(MAX_READ), b"")) == PAYLOAD


def test_unwatched_paths_are_untouched(watched, tmp_path, monkeypatch):
    """Only the watched path is bounded: a sibling reads whole by every route,
    so a call under test that legitimately loads a fixture is not tripped."""
    sibling = tmp_path / "sibling.bin"
    sibling.write_bytes(PAYLOAD)
    refuse_whole_reads(monkeypatch, watched, MAX_READ)

    assert sibling.read_bytes() == PAYLOAD
    with open(sibling, "rb") as f:
        assert f.read() == PAYLOAD
    shutil.copyfile(sibling, tmp_path / "sibling_copy.bin")
    fd = os.open(sibling, os.O_RDONLY)
    try:
        assert os.read(fd, len(PAYLOAD)) == PAYLOAD
    finally:
        os.close(fd)


def test_the_trap_lifts_with_the_monkeypatch(watched):
    """The refusals are scoped to the monkeypatch and vanish with it."""
    mp = pytest.MonkeyPatch()
    refuse_whole_reads(mp, watched, MAX_READ)
    with pytest.raises(WholeFileReadError):
        watched.read_bytes()
    mp.undo()
    assert watched.read_bytes() == PAYLOAD
