"""Migration orchestration is fail-closed and resume requires matching evidence."""

from copy import deepcopy
from pathlib import Path

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


def test_restore_and_resume_require_matching_evidence(stores, tmp_path: Path) -> None:
    source, target = stores
    report = run_migration(source, target, tmp_path / "copy.dump", "run-one", 16, 17)
    assert report["status"] == "verified"
    assert target.snapshot() == source.snapshot()
    assert target.events.index("restore") < target.events.index("checkpoint")
    assert source.events.index("fence") < source.events.index("dump")
    run_migration(source, target, tmp_path / "retry.dump", "run-one", 16, 17)
    assert target.events.count("restore") == 1


@pytest.mark.parametrize("defect", ["same", "major", "nonempty", "stale"])
def test_migration_refuses_unsafe_target(stores, tmp_path: Path, defect: str) -> None:
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


def test_migration_requires_quiescence(stores, tmp_path: Path) -> None:
    source, target = stores
    source.writers = True
    with pytest.raises(ValueError, match="writers"):
        run_migration(source, target, tmp_path / "copy.dump", "run-one", 16, 17)
    assert "dump" not in source.events
    assert not target.checkpoint


@pytest.mark.parametrize("phase", ["dump", "restore", "corrupt"])
def test_failed_phase_cannot_advance(stores, tmp_path: Path, phase: str) -> None:
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


def test_reconciliation_rejects_equal_count_corruption(stores, tmp_path: Path) -> None:
    source, target = stores
    target.fail = "corrupt"
    with pytest.raises(ValueError, match="reconciliation"):
        run_migration(source, target, tmp_path / "copy.dump", "run-one", 16, 17)
    assert (
        target.state["tables"]["vault.edges"]["count"]
        == source.state["tables"]["vault.edges"]["count"]
    )
    assert not target.checkpoint
