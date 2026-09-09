"""Migration orchestration is fail-closed and resume requires matching evidence."""

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from sage.maintenance.postgres_migration import run_migration


class Store:
    """Boundary fake: the runner itself, ordering and reconciliation are real."""

    def __init__(self, identity: str, major: int, state: dict) -> None:
        self.identity, self.major, self.state = identity, major, state
        self.checkpoint = None
        self.writers = False
        self.events = []
        self.fail = ""

    def snapshot(self) -> dict:
        self.events.append("snapshot")
        return deepcopy(self.state)

    def fence(self) -> None:
        self.events.append("fence")
        self.assert_quiescent()

    def assert_quiescent(self) -> None:
        if self.writers:
            raise ValueError("writers remain")

    def dump(self, path: Path) -> None:
        self.events.append("dump")
        if self.fail == "dump":
            raise RuntimeError("dump failed")
        path.write_bytes(b"archive")

    def restore(self, path: Path) -> None:
        self.events.append("restore")
        if self.fail == "restore":
            raise RuntimeError("restore failed")
        assert path.read_bytes() == b"archive"
        self.state = deepcopy(SOURCE)
        if self.fail == "corrupt":
            self.state["tables"]["vault.edges"]["hash"] = "different"

    def read_checkpoint(self) -> dict | None:
        return self.checkpoint

    def save_checkpoint(self, value: dict) -> None:
        self.events.append("checkpoint")
        self.checkpoint = deepcopy(value)


SOURCE = {
    "tables": {
        "vault.edges": {"count": 2, "hash": "curated-rationale"},
    },
    "sequences": {},
}
EMPTY = {"tables": {}, "sequences": {}}


@pytest.fixture
def stores() -> tuple[Store, Store]:
    return Store("source", 16, SOURCE), Store("target", 17, EMPTY)


def test_restore_and_resume_require_matching_evidence(
    stores: tuple[Store, Store], tmp_path: Path
) -> None:
    source, target = stores
    report = run_migration(source, target, tmp_path / "copy.dump", "run-one", 16, 17)
    assert report["status"] == "verified"
    assert target.snapshot() == source.snapshot()
    assert target.events.index("restore") < target.events.index("checkpoint")
    assert source.events.index("fence") < source.events.index("dump")
    run_migration(source, target, tmp_path / "retry.dump", "run-one", 16, 17)
    assert target.events.count("restore") == 1


@pytest.mark.parametrize("defect", ["same", "major", "nonempty", "stale"])
def test_migration_refuses_unsafe_target(
    stores: tuple[Store, Store], tmp_path: Path, defect: str
) -> None:
    source, target = stores
    if defect == "same":
        target.identity = source.identity
    elif defect == "major":
        target.major = 16
    elif defect == "nonempty":
        target.state = deepcopy(SOURCE)
    else:
        target.checkpoint = {"run_id": "someone-else"}
    with pytest.raises(
        ValueError,
        match={
            "same": "distinct",
            "major": "major",
            "nonempty": "existing workload",
            "stale": "stale",
        }[defect],
    ):
        run_migration(source, target, tmp_path / "copy.dump", "run-one", 16, 17)
    assert "restore" not in target.events
    assert "dump" not in source.events


def test_migration_requires_quiescence(stores: tuple[Store, Store], tmp_path: Path) -> None:
    source, target = stores
    source.writers = True
    with pytest.raises(ValueError, match="writers"):
        run_migration(source, target, tmp_path / "copy.dump", "run-one", 16, 17)
    assert "dump" not in source.events
    assert not target.checkpoint


@pytest.mark.parametrize("phase", ["dump", "restore", "corrupt"])
def test_failed_phase_cannot_advance(
    stores: tuple[Store, Store], tmp_path: Path, phase: str
) -> None:
    source, target = stores
    (source if phase == "dump" else target).fail = phase
    with pytest.raises(
        (ValueError, RuntimeError),
        match={"dump": "dump failed", "restore": "restore failed", "corrupt": "reconciliation"}[
            phase
        ],
    ):
        run_migration(source, target, tmp_path / "copy.dump", "run-one", 16, 17)
    assert not target.checkpoint
    if phase == "dump":
        assert "restore" not in target.events


def test_reconciliation_rejects_equal_count_corruption(
    stores: tuple[Store, Store], tmp_path: Path
) -> None:
    source, target = stores
    target.fail = "corrupt"
    with pytest.raises(ValueError, match="reconciliation"):
        run_migration(source, target, tmp_path / "copy.dump", "run-one", 16, 17)
    assert (
        target.state["tables"]["vault.edges"]["count"]
        == source.state["tables"]["vault.edges"]["count"]
    )
    assert not target.checkpoint


@pytest.mark.parametrize(
    "mode",
    ["preflight", "migrate"],
)
@pytest.mark.parametrize("failure", [None, "permissions", "extensions", "fence"])
def test_main_checks_permissions_before_migration(
    monkeypatch: pytest.MonkeyPatch, mode: str, failure: str | None
) -> None:
    import asyncio
    from types import SimpleNamespace

    from sage.maintenance import postgres_migration as migration
    from sage.storage.postgres import cloud_bootstrap, managed_identity

    events = []
    loops = []

    class Store:
        def __init__(self, conninfo: str, *args: Any, **kwargs: Any) -> None:
            self.identity = conninfo
            self.major = 16 if conninfo == "source" else 17

        def snapshot(self) -> Any:
            return {"tables": {}, "sequences": {}}

        def read_checkpoint(self) -> Any:
            return None

        def restore_owners(self) -> Any:
            events.append("owners")
            return ["workload"]

        def assert_restore_privileges(self, owners: list[str]) -> None:
            assert owners == ["workload"]
            events.append("permissions")
            if failure == "permissions":
                raise ValueError("restore ownership privileges missing")

        def assert_extensions_match(self, target: Any) -> None:
            assert self.identity == "source" and target.identity == "target"
            events.append("extensions")
            if failure == "extensions":
                raise ValueError("extension parity failed")

        def assert_fence_privileges(self) -> None:
            assert self.identity == "source"
            events.append("fence")
            if failure == "fence":
                raise ValueError("source fence privileges missing")

        def capacity_report(self, directory: Path) -> Any:
            assert self.identity == "source" and directory.is_dir()
            events.append("capacity")
            return {"database_bytes": 10, "scratch_free_bytes": 20}

        def close(self) -> None:
            events.append("close-" + self.identity)

    async def token(*args: Any) -> Any:
        loops.append(asyncio.get_running_loop())
        return SimpleNamespace(token="disposable-token")

    async def close() -> None:
        loops.append(asyncio.get_running_loop())
        events.append("credential-close")

    async def bootstrap(env: dict[str, str]) -> None:
        loops.append(asyncio.get_running_loop())
        assert env["PG_FQDN"] == "target"
        events.append("bootstrap-target")

    def copy(*args: Any) -> Any:
        events.append("migration")
        return {"status": "verified"}

    for key, value in {
        "PG_SOURCE_FQDN": "source",
        "PG_TARGET_FQDN": "target",
        "PG_DATABASE": "test",
        "PG_ADMIN_USER": "administrator",
        "SAGE_DB_ROLE": "sage",
        "BFF_DB_ROLE": "bff",
        "PG_MIGRATION_RUN_ID": "run",
        "PG_SOURCE_MAJOR": "16",
        "PG_TARGET_MAJOR": "17",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(migration, "PostgresStore", Store)
    monkeypatch.setattr(migration, "make_conninfo", lambda **kw: kw["host"])
    monkeypatch.setattr(migration, "run_migration", copy)
    monkeypatch.setattr(cloud_bootstrap, "_run", bootstrap)
    monkeypatch.setattr(
        managed_identity, "get_postgres_credential", lambda: SimpleNamespace(get_token=token)
    )
    monkeypatch.setattr(managed_identity, "close_postgres_credential", close)
    if failure is None:
        assert migration.main([mode]) == 0
    else:
        message = {
            "permissions": "restore ownership privileges",
            "extensions": "extension parity",
            "fence": "source fence privileges",
        }[failure]
        with pytest.raises(ValueError, match=message):
            migration.main([mode])
    expected = ["bootstrap-target", "owners", "permissions", "extensions", "fence", "capacity"]
    if failure is not None:
        expected = expected[: expected.index(failure) + 1]
    elif mode == "migrate":
        expected.append("migration")
    assert events == expected + ["close-source", "close-target", "credential-close"]
    assert len(set(loops)) == 1, "credential and bootstrap must share one event loop"
