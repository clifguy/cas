"""Event-loop offload and scope-bound tests for the directory scan service.

Asserts that ``scan_directory`` runs its blocking walk-and-hash phase on a
dedicated single-thread executor rather than on the asyncio event loop, and
that the scan is bounded: a default depth ceiling replaces the previously
unlimited walk, and file-count / byte ceilings truncate oversized scans with
an explicit warning instead of a silent partial result. An unbounded on-loop
scan freezes every concurrent request for the full duration of the walk.

The tests drive ``scan_directory`` directly against tmp-path trees with a
stub graph store; no vault services are initialized.
"""

import asyncio
import hashlib
import threading
from collections.abc import Callable

import sage.services.scan as scan_module
from sage.services.scan import scan_directory

EXT_MAP = {".md": "markdown"}


class _StubGraphStore:
    """Graph store exposing only the two lookups the scan performs."""

    async def find_documents_by_hashes(self, hashes):
        return {}

    async def find_by_source_path(self, source_path):
        return []


async def _wait_until(predicate: Callable[[], bool]) -> None:
    while not predicate():
        await asyncio.sleep(0.005)


def _make_tree(root, count: int, content: str = "# Doc") -> None:
    for i in range(count):
        (root / f"doc_{i:02d}.md").write_text(content)


async def test_scan_walk_hash_offloaded_to_worker_thread(minimal_config, tmp_path, monkeypatch):
    """The walk-and-hash unit runs on the dedicated ``sage-scan`` thread,
    never the main/event-loop thread, and the offload is output-transparent.

    Regression guard: an implementation that calls the sync unit inline
    would report the main thread here.
    """
    scan_dir = tmp_path / "offload_tree"
    scan_dir.mkdir()
    (scan_dir / "doc.md").write_text("# Offload")

    thread_names: list[str] = []
    real_sync = scan_module._scan_files_sync

    def _recording_sync(*args, **kwargs):
        thread_names.append(threading.current_thread().name)
        return real_sync(*args, **kwargs)

    monkeypatch.setattr(scan_module, "_scan_files_sync", _recording_sync)

    results, warnings, truncated = await scan_directory(
        directory=scan_dir,
        vault_config=minimal_config,
        graph_store=_StubGraphStore(),
        extension_map=EXT_MAP,
    )

    assert len(thread_names) == 1
    assert thread_names[0].startswith("sage-scan")
    assert thread_names[0] != threading.main_thread().name

    expected_hash = "sha256:" + hashlib.sha256(b"# Offload").hexdigest()
    assert len(results) == 1
    assert results[0].file_hash == expected_hash
    assert results[0].sage_status == "new"
    assert warnings == []
    assert truncated is False


async def test_scan_event_loop_responsive_during_scan(minimal_config, tmp_path, monkeypatch):
    """While the walk-and-hash unit blocks in its worker thread, the event
    loop stays responsive (an independent coroutine completes before the
    scan is released), and a second scan queues behind the first rather
    than running alongside it.

    Regression guards: if the sync unit ran on the loop, the
    ``_wait_until(started)`` probe would time out; if the executor had
    more than one worker, the second scan's sync unit would start while
    the first still blocks (call count 2 instead of 1).
    """
    scan_dir = tmp_path / "blocking_tree"
    scan_dir.mkdir()

    started = threading.Event()
    release = threading.Event()
    in_progress = threading.Event()
    call_count = 0

    def _blocking_sync(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        in_progress.set()
        started.set()
        release.wait(timeout=2.0)
        in_progress.clear()
        return [], [], False

    monkeypatch.setattr(scan_module, "_scan_files_sync", _blocking_sync)

    def _scan_task():
        return asyncio.create_task(
            scan_directory(
                directory=scan_dir,
                vault_config=minimal_config,
                graph_store=_StubGraphStore(),
                extension_map=EXT_MAP,
            )
        )

    first = _scan_task()
    try:
        await asyncio.wait_for(_wait_until(started.is_set), timeout=1.0)
        assert in_progress.is_set()

        second = _scan_task()
        probe = await asyncio.wait_for(asyncio.sleep(0, result="alive"), timeout=1.0)
        assert probe == "alive"
        assert in_progress.is_set(), "scan finished before the probe; no concurrency shown"
        assert call_count == 1, "second scan ran concurrently; executor is not single-worker"
    finally:
        release.set()

    for task in (first, second):
        results, warnings, truncated = await asyncio.wait_for(task, timeout=2.0)
        assert results == []
        assert truncated is False
    assert call_count == 2


async def test_scan_truncates_at_file_cap(minimal_config, tmp_path, monkeypatch):
    """A tree larger than the file ceiling is cut at the ceiling, flagged
    ``truncated``, the cut is named in a warning, and — the load-bearing
    half — no more than the ceiling's worth of files is ever hashed.

    Regression guards: without cap enforcement in the walk, all seven
    files come back; a post-hoc slicer that hashes everything and trims
    the result list afterward passes every length assertion but fails the
    hash-call count.
    """
    scan_dir = tmp_path / "file_cap_tree"
    scan_dir.mkdir()
    _make_tree(scan_dir, 7)

    hash_calls = []
    real_hash = scan_module._compute_file_hash
    monkeypatch.setattr(
        scan_module,
        "_compute_file_hash",
        lambda path: (hash_calls.append(path), real_hash(path))[1],
    )

    results, warnings, truncated = await scan_directory(
        directory=scan_dir,
        vault_config=minimal_config,
        graph_store=_StubGraphStore(),
        extension_map=EXT_MAP,
        max_files=5,
    )

    assert truncated is True
    assert len(results) == 5
    assert len(hash_calls) == 5
    assert any("file" in w and "5" in w for w in warnings)


async def test_scan_truncates_at_byte_cap(minimal_config, tmp_path, monkeypatch):
    """Hashing stops once the cumulative byte ceiling is reached; remaining
    files are dropped (never hashed), the response is flagged ``truncated``,
    and the cut is named in a warning.

    Regression guards: without cap enforcement in the hash loop, all three
    files come back hashed and no warning is emitted; a post-hoc slicer
    that hashes everything and trims afterward fails the hash-call count.
    """
    scan_dir = tmp_path / "byte_cap_tree"
    scan_dir.mkdir()
    _make_tree(scan_dir, 3, content="x" * 100)

    hash_calls = []
    real_hash = scan_module._compute_file_hash
    monkeypatch.setattr(
        scan_module,
        "_compute_file_hash",
        lambda path: (hash_calls.append(path), real_hash(path))[1],
    )

    results, warnings, truncated = await scan_directory(
        directory=scan_dir,
        vault_config=minimal_config,
        graph_store=_StubGraphStore(),
        extension_map=EXT_MAP,
        max_bytes=150,
    )

    assert truncated is True
    assert len(results) == 2
    assert len(hash_calls) == 2
    assert any("byte" in w for w in warnings)


async def test_default_depth_cap_warns_when_pruning(minimal_config, tmp_path):
    """With no caller-supplied depth, the walk stops at the default depth
    ceiling and says so: deeper files are absent and a depth warning is
    emitted. A partial result from the implicit default must never look
    like a complete scan.

    Regression guard: treating an omitted depth as unlimited restores the
    unbounded walk (the deep file comes back, no warning).
    """
    scan_dir = tmp_path / "deep_tree"
    current = scan_dir
    for i in range(scan_module.DEFAULT_MAX_DEPTH + 2):
        current = current / f"level_{i:02d}"
    current.mkdir(parents=True)
    (current / "deep.md").write_text("# Deep")
    (scan_dir / "shallow.md").write_text("# Shallow")

    results, warnings, truncated = await scan_directory(
        directory=scan_dir,
        vault_config=minimal_config,
        graph_store=_StubGraphStore(),
        extension_map=EXT_MAP,
        max_depth=None,
    )

    paths = [r.file_path for r in results]
    assert any(p.endswith("shallow.md") for p in paths)
    assert not any(p.endswith("deep.md") for p in paths)
    assert any("depth" in w for w in warnings)
    assert truncated is True


async def test_explicit_max_depth_prunes_silently(minimal_config, tmp_path):
    """A caller-supplied depth ceiling is intentional pruning: deeper files
    are absent and no depth warning is emitted (existing caller semantics).

    Regression guard: emitting the depth warning unconditionally would
    misreport every deliberate shallow scan as truncated.
    """
    scan_dir = tmp_path / "shallow_scan_tree"
    scan_dir.mkdir()
    (scan_dir / "top.md").write_text("# Top")
    sub = scan_dir / "sub"
    sub.mkdir()
    (sub / "nested.md").write_text("# Nested")

    results, warnings, truncated = await scan_directory(
        directory=scan_dir,
        vault_config=minimal_config,
        graph_store=_StubGraphStore(),
        extension_map=EXT_MAP,
        max_depth=0,
    )

    paths = [r.file_path for r in results]
    assert any(p.endswith("top.md") for p in paths)
    assert not any(p.endswith("nested.md") for p in paths)
    assert not any("depth" in w for w in warnings)
    assert truncated is False
