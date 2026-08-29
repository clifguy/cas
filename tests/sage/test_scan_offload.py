"""Event-loop offload, scope-bound, and round-trip tests for the scan service.

Asserts that ``scan_directory`` runs its blocking walk-and-hash phase on a
dedicated single-thread executor rather than on the asyncio event loop, and
that the scan is bounded: a default depth ceiling replaces the previously
unlimited walk, and file-count / byte ceilings truncate oversized scans with
an explicit warning instead of a silent partial result. An unbounded on-loop
scan freezes every concurrent request for the full duration of the walk.

Also asserts that the vault-lookup phase costs a constant number of graph-store
round-trips regardless of file count, and that the resulting ``sage_status``
classification is unchanged by that batching.

The tests drive ``scan_directory`` directly against tmp-path trees with a
stub graph store; no vault services are initialized.
"""

import asyncio
import hashlib
import threading
from collections.abc import Callable
from pathlib import Path

import sage.services.scan as scan_module
from sage.services.scan import scan_directory

EXT_MAP = {".md": "markdown"}


class _StubGraphStore:
    """Graph store exposing only the two lookups the scan performs.

    Deliberately narrow: a scan that reached for any other store method
    would fail with AttributeError here rather than silently working.

    ``path_matches`` is keyed the way the durable store keys it: ingest
    records ``source_path`` vault-relative (``imports/alpha.md``), so a
    seed keyed by absolute path describes a row no real store holds.
    Seeding it that way is what let the ``modified`` verdict stay dead
    while the tests covering it stayed green.
    """

    def __init__(self, hash_matches=None, path_matches=None):
        self._hash_matches: set[str] = set(hash_matches or ())
        self._path_matches: dict[str, str] = dict(path_matches or {})
        self.hash_calls: list[list[str]] = []
        self.path_calls: list[list[str]] = []

    async def find_documents_by_hashes(self, hashes):
        self.hash_calls.append(list(hashes))
        return {h: f"doc_for_{h}" for h in hashes if h in self._hash_matches}

    async def find_documents_by_source_paths(self, source_paths):
        self.path_calls.append(list(source_paths))
        return {p: self._path_matches[p] for p in source_paths if p in self._path_matches}


async def _wait_until(predicate: Callable[[], bool]) -> None:
    while not predicate():
        await asyncio.sleep(0.005)


def _make_tree(root, count: int, content: str = "# Doc") -> None:
    for i in range(count):
        (root / f"doc_{i:02d}.md").write_text(content)


def _vault_imports(config) -> Path:
    """The vault's own ``imports/`` directory, created on first use.

    Where retained sources live, and so the tree a scan of
    already-ingested files walks. A file here has a vault-relative path
    (``imports/<name>``) that a stored ``source_path`` can equal; a file
    anywhere else does not.
    """
    imports = Path(config.vault.storage_root) / "imports"
    imports.mkdir(parents=True, exist_ok=True)
    return imports


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


def _hash_of(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


async def test_scan_issues_constant_graph_store_round_trips(minimal_config):
    """The vault-lookup phase costs two round-trips no matter how many files
    are matched, and the path lookup is asked only about adapter-matched
    files.

    Regression guards: the per-file form issued one path lookup per matched
    file, so the path-call count would be 6 instead of 1 -- and a bulk method
    that internally loops over paths would satisfy every classification
    assertion in this module while leaving that cost in place, which is why
    this test counts calls rather than inspecting results. Passing the whole
    walked file list (rather than the adapter-matched subset) would widen the
    single call's argument list to include the extensionless file. Asking for
    both path forms in one call is what keeps the round-trip count at one
    while the lookup covers vault-relative and absolute stored paths alike.
    """
    scan_dir = _vault_imports(minimal_config)
    _make_tree(scan_dir, 6)
    (scan_dir / "thing.xyz").write_text("not ingestable")

    store = _StubGraphStore()
    results, _warnings, truncated = await scan_directory(
        directory=scan_dir,
        vault_config=minimal_config,
        graph_store=store,
        extension_map=EXT_MAP,
    )

    assert truncated is False
    assert len(results) == 7
    assert len(store.hash_calls) == 1
    assert len(store.path_calls) == 1
    assert sorted(store.path_calls[0]) == sorted(
        [str(scan_dir / f"doc_{i:02d}.md") for i in range(6)]
        + [f"imports/doc_{i:02d}.md" for i in range(6)]
    )


async def test_scan_classifies_new_modified_unchanged_and_no_adapter(minimal_config):
    """Batching the path lookup leaves all four sage_status verdicts intact.

    Regression guard: reading the batched result with the wrong key (a
    document id rather than the file's own path, say) collapses every
    matched file to ``new`` -- which no round-trip count would catch.
    The path seed is vault-relative, matching what ingest actually
    stores; an absolute seed would describe a row no real store holds.
    """
    scan_dir = _vault_imports(minimal_config)
    (scan_dir / "unchanged.md").write_text("# Same")
    (scan_dir / "modified.md").write_text("# Changed")
    (scan_dir / "new.md").write_text("# Fresh")
    (scan_dir / "thing.xyz").write_text("not ingestable")

    store = _StubGraphStore(
        hash_matches={_hash_of("# Same")},
        path_matches={"imports/modified.md": _hash_of("# Was")},
    )
    results, _warnings, _truncated = await scan_directory(
        directory=scan_dir,
        vault_config=minimal_config,
        graph_store=store,
        extension_map=EXT_MAP,
    )

    status_by_name = {r.file_path.rsplit("/", 1)[-1]: r.sage_status for r in results}
    assert status_by_name == {
        "unchanged.md": "unchanged",
        "modified.md": "modified",
        "new.md": "new",
        "thing.xyz": "no_adapter",
    }


async def test_scan_hash_match_wins_over_path_match(minimal_config):
    """A file matching on both hash and path is ``unchanged``, not
    ``modified``.

    Regression guard: the two lookups are consulted in a fixed order, and the
    other tests here never let a single file hit both, so a reordering that
    checked the path first would leave them green. Re-ingesting an unmodified
    file already in the vault is exactly this case.
    """
    scan_dir = _vault_imports(minimal_config)
    (scan_dir / "both.md").write_text("# Both")

    store = _StubGraphStore(
        hash_matches={_hash_of("# Both")},
        path_matches={"imports/both.md": _hash_of("# Both")},
    )
    results, _warnings, _truncated = await scan_directory(
        directory=scan_dir,
        vault_config=minimal_config,
        graph_store=store,
        extension_map=EXT_MAP,
    )

    assert len(results) == 1
    assert results[0].sage_status == "unchanged"


async def test_scan_reports_modified_for_edited_file_in_vault_imports(minimal_config):
    """An already-ingested file edited in place under the vault's own
    ``imports/`` is ``modified``, and its reported path stays absolute.

    Regression guard: this is the verdict that was unreachable. Ingest
    stores ``source_path`` vault-relative, so a lookup keyed only by the
    absolute path can never match and the file comes back ``new``. The
    stored-path seed here is the relative form a real store holds.

    The ``file_path`` assertion pins the other half of the contract: the
    relative form is a lookup key, not an output change. Callers feed
    ``file_path`` straight back into a subsequent ingest, so it must stay
    the absolute path the caller asked about.
    """
    scan_dir = _vault_imports(minimal_config)
    (scan_dir / "alpha.md").write_text("# Edited")

    store = _StubGraphStore(path_matches={"imports/alpha.md": _hash_of("# Original")})
    results, _warnings, _truncated = await scan_directory(
        directory=scan_dir,
        vault_config=minimal_config,
        graph_store=store,
        extension_map=EXT_MAP,
    )

    assert len(results) == 1
    assert results[0].sage_status == "modified"
    assert results[0].file_path == str(scan_dir / "alpha.md")


async def test_scan_asks_the_store_by_vault_relative_path_for_in_vault_files(minimal_config):
    """The lookup the scan issues carries the vault-relative form for a
    file inside the vault tree.

    Regression guard: separate from the verdict test because a verdict can
    be reached without asking the store the right question -- resolving
    every stored path client-side, say, or matching on basename after the
    fact. Only inspecting the call argument pins the key, and only a
    pinned key keeps the lookup a single indexed query.
    """
    scan_dir = _vault_imports(minimal_config)
    (scan_dir / "alpha.md").write_text("# Edited")

    store = _StubGraphStore()
    await scan_directory(
        directory=scan_dir,
        vault_config=minimal_config,
        graph_store=store,
        extension_map=EXT_MAP,
    )

    assert len(store.path_calls) == 1
    assert "imports/alpha.md" in store.path_calls[0]


async def test_scan_outside_the_vault_classifies_edited_file_as_new(minimal_config, tmp_path):
    """A file scanned from a staging directory outside the vault is
    ``new``, even when the vault holds a document retained from it.

    The scope decision, made executable: path equality is the predicate,
    and a staged file bears no relation to the vault's storage root. It is
    also the truthful verdict -- retaining a same-named source whose
    content differs disambiguates to ``imports/<stem>_<hash>``, so
    ingesting this file produces a new document rather than updating the
    existing one.

    Regression guard: a basename fallback added later to make the staging
    case report ``modified`` would turn this red, which is the point. Such
    a fallback would also fire on an unrelated same-named file that was
    never ingested at all.
    """
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "alpha.md").write_text("# Edited")

    store = _StubGraphStore(path_matches={"imports/alpha.md": _hash_of("# Original")})
    results, _warnings, _truncated = await scan_directory(
        directory=staging,
        vault_config=minimal_config,
        graph_store=store,
        extension_map=EXT_MAP,
    )

    assert len(results) == 1
    assert results[0].sage_status == "new"
    assert store.path_calls[0] == [str(staging / "alpha.md")]


async def test_scan_still_matches_a_stored_absolute_source_path(minimal_config, tmp_path):
    """A document whose stored ``source_path`` is itself absolute still
    matches, wherever the file sits.

    Regression guard: normalizing the scanned path to the vault-relative
    form is an addition, not a replacement, and this is the out-of-vault
    half of that property -- asserted by verdict, where the sibling
    round-trip test asserts it by inspecting the lookup keys.

    An implementation that dropped the absolute key outright would fail
    this test, but not only this one: the round-trip and outside-the-vault
    tests pin the key list directly and would fail alongside it. The case
    no other test in this module reaches is the narrower one -- dropping
    the absolute key just for files that also have a relative form -- and
    that is the companion in-vault test's job, not this one's.
    """
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "alpha.md").write_text("# Edited")

    store = _StubGraphStore(path_matches={str(staging / "alpha.md"): _hash_of("# Original")})
    results, _warnings, _truncated = await scan_directory(
        directory=staging,
        vault_config=minimal_config,
        graph_store=store,
        extension_map=EXT_MAP,
    )

    assert len(results) == 1
    assert results[0].sage_status == "modified"


async def test_scan_matches_an_absolute_stored_path_for_a_file_inside_the_vault(minimal_config):
    """A file inside the vault whose stored source path is absolute still
    matches, even though the vault-relative form is also available.

    Regression guard: the in-vault half of the strict-superset property,
    and the one the out-of-vault case cannot cover -- there the absolute
    path is the only key either way, so dropping the absolute key for
    files that have a relative form leaves that test green. Only a file
    carrying both forms distinguishes "ask by both" from "ask by the
    relative form instead".
    """
    scan_dir = _vault_imports(minimal_config)
    (scan_dir / "alpha.md").write_text("# Edited")

    store = _StubGraphStore(path_matches={str(scan_dir / "alpha.md"): _hash_of("# Original")})
    results, _warnings, _truncated = await scan_directory(
        directory=scan_dir,
        vault_config=minimal_config,
        graph_store=store,
        extension_map=EXT_MAP,
    )

    assert len(results) == 1
    assert results[0].sage_status == "modified"
