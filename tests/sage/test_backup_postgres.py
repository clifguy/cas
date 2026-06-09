"""Unit tests for the Postgres backup runner's pure helpers.

These exercise the command-construction, filename, connection-resolution, and
retention logic of ``scripts/backup_postgres.py`` without a live database or any
subprocess. The end-to-end dump/restore proof lives in
``test_backup_postgres_roundtrip.py``; the plist structure in
``test_backup_postgres_plist.py``.

Why each case matters is stated inline; collectively they guard the two
data-loss inversions a backup tool can hide -- a non-restorable dump format and
a retention sweep that prunes the wrong files.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

from scripts.backup_postgres import (
    build_dump_argv,
    build_globals_argv,
    default_backup_dir,
    dump_filenames,
    resolve_conninfo,
    select_prunable,
)

# Repo root: <repo>/tests/sage/test_backup_postgres.py -> parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]

_CONNINFO = "dbname=sage port=5432"


def test_build_dump_argv_uses_custom_format() -> None:
    """``pg_dump -Fc`` against the conninfo, writing the named file.

    ``-Fc`` (custom format) is what makes the dump selective and
    ``pg_restore``-able; a regression to plain format would silently produce an
    archive ``pg_restore`` cannot consume.
    """
    out = Path("/tmp/sage-20260608T020000Z.dump")
    assert build_dump_argv(_CONNINFO, out) == [
        "pg_dump",
        "-Fc",
        "-d",
        _CONNINFO,
        "-f",
        str(out),
    ]


def test_build_globals_argv_is_globals_only() -> None:
    """``pg_dumpall --globals-only`` -- roles/tablespaces, not the cluster.

    Without ``--globals-only`` this dumps every database in the cluster: wrong
    content and potentially enormous.
    """
    out = Path("/tmp/globals-20260608T020000Z.sql")
    assert build_globals_argv(_CONNINFO, out) == [
        "pg_dumpall",
        "--globals-only",
        "-d",
        _CONNINFO,
        "-f",
        str(out),
    ]


def test_dump_filenames_timestamped_distinct_sortable() -> None:
    """Distinct timestamps -> distinct, chronologically-sortable names.

    A fixed or colliding filename would silently overwrite a prior backup; the
    ``YYYYMMDDTHHMMSSZ`` stamp must also sort lexically == chronologically so the
    retention sweep can order runs by name.
    """
    earlier = datetime(2026, 6, 8, 2, 0, 0, tzinfo=timezone.utc)
    later = datetime(2026, 6, 8, 18, 0, 0, tzinfo=timezone.utc)

    db_e, glob_e = dump_filenames(earlier)
    db_l, glob_l = dump_filenames(later)

    # The stamp is embedded and the two runs do not collide.
    assert db_e == "sage-20260608T020000Z.dump"
    assert glob_e == "globals-20260608T020000Z.sql"
    assert db_e != db_l and glob_e != glob_l
    # Lexical order tracks chronological order.
    assert db_e < db_l
    assert glob_e < glob_l


def test_resolve_conninfo_from_config_targets_sage_db() -> None:
    """With no override, the conninfo is composed from the config postgres block.

    The committed ``sage/config.yaml`` postgres block has ``database: sage`` and
    ``host: null`` (local socket), so the resolved conninfo names the ``sage``
    database and carries no host. A regression hardcoding ``localhost`` or the
    ``postgres`` maintenance DB would back up the wrong database.
    """
    parsed = conninfo_to_dict(resolve_conninfo(None))
    assert parsed["dbname"] == "sage"
    assert "host" not in parsed  # socket default -- host omitted


def test_resolve_conninfo_dsn_override_passthrough() -> None:
    """An explicit ``--dsn`` passes through verbatim (test harness / hosted)."""
    dsn = "postgresql://postgres:postgres@localhost:5432/postgres"
    assert resolve_conninfo(dsn) == dsn


def test_default_backup_dir_under_home_not_repo() -> None:
    """The default backup dir sits under ``$HOME`` and outside the repo.

    A default inside the repo tree would get committed and double-covered by
    Time Machine; one outside ``$HOME`` would not be Time-Machine-covered.
    """
    result = default_backup_dir()
    assert result.is_relative_to(Path.home())
    assert not result.is_relative_to(_REPO_ROOT)


def _touch(directory: Path, ts: str) -> tuple[Path, Path]:
    """Create the sage+globals file pair for one timestamp; return the pair."""
    db = directory / f"sage-{ts}.dump"
    glob = directory / f"globals-{ts}.sql"
    db.write_bytes(b"x")
    glob.write_text("x")
    return db, glob


def test_select_prunable_keeps_n_recent_runs(tmp_path: Path) -> None:
    """``retain=2`` keeps the two newest runs; older runs are returned to prune.

    The dangerous inversion -- pruning the *newest* runs -- is caught because
    this asserts the exact pruned set is the older runs' files and the newest two
    pairs are absent from it.
    """
    stamps = [
        "20260601T020000Z",
        "20260602T020000Z",
        "20260603T020000Z",
        "20260604T020000Z",
    ]
    pairs = {ts: _touch(tmp_path, ts) for ts in stamps}

    prunable = set(select_prunable(sorted(tmp_path.iterdir()), retain=2))

    # The two newest runs survive; the two oldest are pruned (both files each).
    kept = set(pairs["20260603T020000Z"]) | set(pairs["20260604T020000Z"])
    pruned = set(pairs["20260601T020000Z"]) | set(pairs["20260602T020000Z"])
    assert prunable == pruned
    assert prunable.isdisjoint(kept)


def test_select_prunable_ignores_unrelated_files(tmp_path: Path) -> None:
    """Files that do not match the backup naming are never pruned.

    A loose glob that deleted unrelated files in the backup directory would be
    catastrophic; an unrelated ``notes.txt`` must survive even with ``retain=0``.
    """
    _touch(tmp_path, "20260601T020000Z")
    unrelated = tmp_path / "notes.txt"
    unrelated.write_text("keep me")

    prunable = select_prunable(sorted(tmp_path.iterdir()), retain=0)

    assert unrelated not in prunable
