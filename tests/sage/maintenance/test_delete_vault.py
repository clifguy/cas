"""Whole-vault teardown orchestration (Tier A: real filesystem + spy provisioner).

Exercises the ``delete_vault`` core against a real on-disk vault tree (via the
``FilesystemVaultSourceStore``) and a spy provisioner that records
``drop_vault_schema`` calls without a database, plus an injected ``dump_runner``
that never shells out to ``pg_dump``. The real Postgres schema drop is proven in
``tests/sage/test_storage_binding.py::test_sto_022`` and the DDL in
``tests/sage/test_postgres_schema.py``.
"""

import copy
import json
from types import SimpleNamespace

import pytest

from sage.maintenance.delete_vault import delete_vault
from sage.vault_source_binding import FilesystemVaultSourceStore

# ---------------------------------------------------------------------------
# Builders and spies
# ---------------------------------------------------------------------------


def _materialize(vault_root, vault_id, base_cfg, *, storage_outside=None, brain_outside=None):
    """Write a real vault tree under ``vault_root`` and return its coordinates.

    ``storage_outside`` / ``brain_outside``, when given, point the respective tree
    at a path outside the vault root (the root-escape case). Either or both can
    escape; an unspecified tree lives under ``<vault_root>/<vault_id>/``.
    """
    vault_dir = vault_root / vault_id
    storage_root = storage_outside if storage_outside is not None else vault_dir / "sources"
    brain_root = brain_outside if brain_outside is not None else vault_dir / "brain"
    (storage_root / "imports").mkdir(parents=True, exist_ok=True)
    (storage_root / "imports" / "doc.md").write_text("body")
    brain_root.mkdir(parents=True, exist_ok=True)
    (brain_root / "timing.log").write_text("t")

    cfg = copy.deepcopy(base_cfg)
    cfg["vault"]["id"] = vault_id
    cfg["vault"]["name"] = vault_id.replace("_", " ").title()
    cfg["vault"]["storage_root"] = str(storage_root)
    cfg["vault"]["brain_root"] = str(brain_root)

    store = FilesystemVaultSourceStore(vault_root)
    store.write_config(vault_id, cfg)
    return SimpleNamespace(
        vault_root=vault_root,
        vault_id=vault_id,
        vault_dir=vault_dir,
        storage_root=storage_root,
        brain_root=brain_root,
        config_path=store.config_locator(vault_id),
        store=store,
    )


class _SpyProvisioner:
    """Records drop_vault_schema calls; optionally raises to simulate a failure."""

    def __init__(self, *, raises=None, log=None):
        self.dropped = []
        self._raises = raises
        self._log = log

    async def drop_vault_schema(self, vault_id):
        if self._log is not None:
            self._log.append("drop")
        self.dropped.append(vault_id)
        if self._raises is not None:
            raise self._raises


class _SpyDump:
    """An injected pg_dump runner: records argv, never shells out."""

    def __init__(self, *, raises=None):
        self.calls = []
        self._raises = raises

    def __call__(self, argv):
        self.calls.append(argv)
        if self._raises is not None:
            raise self._raises


class _SpyIngestion:
    def __init__(self, log):
        self._log = log

    async def stop_worker(self):
        self._log.append("stop_worker")


class _SpyServices:
    """A stand-in for a registered vault's services, recording the teardown order."""

    def __init__(self, log):
        self._log = log
        self.ingestion_service = _SpyIngestion(log)

    def close_timing(self):
        self._log.append("close_timing")

    async def close_storage(self):
        self._log.append("close_storage")


@pytest.fixture
def built_vault(tmp_path, minimal_vault_config_dict):
    return _materialize(tmp_path / "vaults", "victim", minimal_vault_config_dict)


def _deletions_dir(built):
    """A snapshot/receipt dir OUTSIDE the vault root (sibling under tmp_path)."""
    return built.vault_root.parent / "deletions"


def _common(built, **overrides):
    kw = dict(
        vault_id=built.vault_id,
        source_store=built.store,
        provisioner=_SpyProvisioner(),
        vault_root=built.vault_root,
        snapshot_dir=_deletions_dir(built),
        reason="retire",
        apply=True,
        dump_runner=_SpyDump(),
        input_fn=lambda _p: built.vault_id,
    )
    kw.update(overrides)
    return kw


# ---------------------------------------------------------------------------
# D.12 -- dry-run
# ---------------------------------------------------------------------------


async def test_dry_run_makes_no_change(built_vault, capsys):
    prov = _SpyProvisioner()
    dump = _SpyDump()
    rc = await delete_vault(**_common(built_vault, provisioner=prov, dump_runner=dump, apply=False))

    assert rc == 0
    out = capsys.readouterr().out
    assert "(dry-run; pass --apply to execute)" in out
    assert built_vault.vault_id in out
    assert prov.dropped == []
    assert dump.calls == []
    assert built_vault.storage_root.exists()
    assert built_vault.brain_root.exists()
    assert built_vault.config_path.exists()


# ---------------------------------------------------------------------------
# D.13 / D.22 -- apply happy path; receipt lands OUTSIDE the vault
# ---------------------------------------------------------------------------


async def test_apply_full_cascade_and_receipt_outside_vault(built_vault):
    prov = _SpyProvisioner()
    dump = _SpyDump()
    snapshot_dir = _deletions_dir(built_vault)
    rc = await delete_vault(**_common(built_vault, provisioner=prov, dump_runner=dump))

    assert rc == 0
    assert prov.dropped == [built_vault.vault_id]
    assert dump.calls  # snapshot attempted
    assert not built_vault.storage_root.exists()
    assert not built_vault.brain_root.exists()
    assert not built_vault.config_path.exists()
    assert not built_vault.vault_dir.exists()  # enclosing vault dir removed

    receipts = list(snapshot_dir.glob(f"{built_vault.vault_id}-*/receipt.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["status"] == "deleted"
    assert receipt["reason"] == "retire"
    # Fully-in-root pole: every filesystem target is recorded as removed.
    targets = receipt["targets"]
    assert targets["storage_root"]["status"] == "removed"
    assert targets["brain_root"]["status"] == "removed"
    assert targets["vault_dir"]["status"] == "removed"
    # Nothing was written inside the (now-removed) vault root.
    assert list(built_vault.vault_root.rglob("receipt.json")) == []


# ---------------------------------------------------------------------------
# D.14 -- typed-confirm mismatch aborts
# ---------------------------------------------------------------------------


async def test_wrong_confirmation_aborts(built_vault):
    prov = _SpyProvisioner()
    dump = _SpyDump()
    rc = await delete_vault(
        **_common(built_vault, provisioner=prov, dump_runner=dump, input_fn=lambda _p: "nope")
    )

    assert rc == 3
    assert prov.dropped == []
    assert dump.calls == []
    assert built_vault.storage_root.exists()
    assert built_vault.config_path.exists()


# ---------------------------------------------------------------------------
# D.15 -- auto-confirm only for the literal 'test' vault
# ---------------------------------------------------------------------------


async def test_auto_confirm_only_for_literal_test(tmp_path, minimal_vault_config_dict):
    def _boom(_p):
        raise AssertionError("the confirmation prompt must not run for the 'test' vault")

    tv = _materialize(tmp_path / "tvaults", "test", minimal_vault_config_dict)
    rc = await delete_vault(
        vault_id="test",
        source_store=tv.store,
        provisioner=_SpyProvisioner(),
        vault_root=tv.vault_root,
        snapshot_dir=tmp_path / "del",
        reason="r",
        apply=True,
        dump_runner=_SpyDump(),
        input_fn=_boom,
    )
    assert rc == 0
    assert not tv.config_path.exists()

    # A non-'test' vault with a no-op prompt (returns "") is refused, untouched.
    nv = _materialize(tmp_path / "nvaults", "nottest", minimal_vault_config_dict)
    rc = await delete_vault(
        vault_id="nottest",
        source_store=nv.store,
        provisioner=_SpyProvisioner(),
        vault_root=nv.vault_root,
        snapshot_dir=tmp_path / "del2",
        reason="r",
        apply=True,
        dump_runner=_SpyDump(),
        input_fn=lambda _p: "",
    )
    assert rc == 3
    assert nv.config_path.exists()


# ---------------------------------------------------------------------------
# D.16 -- per-target root-escape skip: only the escaping tree is left in place;
# the in-root work still runs
# ---------------------------------------------------------------------------


async def test_root_escape_skips_only_the_escaping_target(tmp_path, minimal_vault_config_dict):
    outside = tmp_path / "outside_sources"
    esc = _materialize(
        tmp_path / "vaults", "escaper", minimal_vault_config_dict, storage_outside=outside
    )
    (outside / "precious").write_text("keep")
    prov = _SpyProvisioner()
    dump = _SpyDump()
    snapshot_dir = _deletions_dir(esc)

    rc = await delete_vault(**_common(esc, provisioner=prov, dump_runner=dump))

    # The escaping storage_root is refused (left in place), but the in-root work
    # is no longer aborted: the schema drops and the in-root config, brain tree,
    # and enclosing dir are removed.
    assert rc == 0
    assert (outside / "precious").exists()  # escaping tree untouched
    assert prov.dropped == [esc.vault_id]  # schema still dropped
    assert not esc.brain_root.exists()  # in-root brain removed
    assert not esc.config_path.exists()  # in-root config removed
    assert not esc.vault_dir.exists()  # enclosing dir removed

    # The receipt distinguishes the skipped (escaping) target from the removed ones.
    receipts = list(snapshot_dir.glob(f"{esc.vault_id}-*/receipt.json"))
    assert len(receipts) == 1
    targets = json.loads(receipts[0].read_text())["targets"]
    assert targets["storage_root"]["status"] == "skipped (outside bound root)"
    assert targets["storage_root"]["path"] == str(outside)
    assert targets["brain_root"]["status"] == "removed"
    assert targets["vault_dir"]["status"] == "removed"


async def test_wholly_outside_roots_still_retire_in_root_work(tmp_path, minimal_vault_config_dict):
    """Both source trees escape the bound root; the in-root work still retires the vault.

    Mirrors the leaked local-profile orphans whose ``storage_root``/``brain_root``
    point at cleaned-up temp dirs outside the bound vault root while the config and
    enclosing directory remain in-root. Both escaping trees are left untouched; the
    schema drops and the in-root config + enclosing dir are removed.
    """
    out_storage = tmp_path / "out_storage"
    out_brain = tmp_path / "out_brain"
    orphan = _materialize(
        tmp_path / "vaults",
        "orphan",
        minimal_vault_config_dict,
        storage_outside=out_storage,
        brain_outside=out_brain,
    )
    (out_storage / "keep_s").write_text("s")
    (out_brain / "keep_b").write_text("b")
    prov = _SpyProvisioner()
    snapshot_dir = _deletions_dir(orphan)

    rc = await delete_vault(**_common(orphan, provisioner=prov))

    assert rc == 0
    # Both escaping trees are untouched.
    assert (out_storage / "keep_s").exists()
    assert (out_brain / "keep_b").exists()
    # In-root work still ran.
    assert prov.dropped == [orphan.vault_id]
    assert not orphan.config_path.exists()
    assert not orphan.vault_dir.exists()

    receipts = list(snapshot_dir.glob(f"{orphan.vault_id}-*/receipt.json"))
    assert len(receipts) == 1
    targets = json.loads(receipts[0].read_text())["targets"]
    assert targets["storage_root"]["status"] == "skipped (outside bound root)"
    assert targets["brain_root"]["status"] == "skipped (outside bound root)"
    assert targets["vault_dir"]["status"] == "removed"


# ---------------------------------------------------------------------------
# D.17 -- snapshot-before-destroy, ON by default, halts on failure; --no-snapshot skips
# ---------------------------------------------------------------------------


async def test_snapshot_failure_halts_before_any_destroy(built_vault):
    prov = _SpyProvisioner()
    dump = _SpyDump(raises=RuntimeError("pg_dump exploded"))
    rc = await delete_vault(**_common(built_vault, provisioner=prov, dump_runner=dump))

    assert rc == 4
    assert prov.dropped == []
    assert built_vault.storage_root.exists()
    assert built_vault.config_path.exists()


async def test_no_snapshot_skips_dump_and_proceeds(built_vault):
    prov = _SpyProvisioner()
    dump = _SpyDump(raises=RuntimeError("must not be called"))
    rc = await delete_vault(
        **_common(built_vault, provisioner=prov, dump_runner=dump, snapshot=False)
    )

    assert rc == 0
    assert dump.calls == []
    assert prov.dropped == [built_vault.vault_id]
    assert not built_vault.config_path.exists()


# ---------------------------------------------------------------------------
# D.18 -- audit-first: the record survives a failed drop
# ---------------------------------------------------------------------------


async def test_audit_written_before_a_failed_drop(built_vault):
    snapshot_dir = _deletions_dir(built_vault)
    prov = _SpyProvisioner(raises=RuntimeError("drop failed"))
    rc = await delete_vault(**_common(built_vault, provisioner=prov))

    assert rc == 4
    audits = list(snapshot_dir.glob(f"{built_vault.vault_id}-*/deletion_audit.jsonl"))
    assert len(audits) == 1
    rec = json.loads(audits[0].read_text().splitlines()[0])
    assert rec["operation"] == "delete_vault"
    assert rec["vault_id"] == built_vault.vault_id
    # No receipt: the teardown did not complete.
    assert list(snapshot_dir.glob(f"{built_vault.vault_id}-*/receipt.json")) == []
    # The graph drop raised before any filesystem removal ran.
    assert built_vault.storage_root.exists()


# ---------------------------------------------------------------------------
# D.19 / D.20 -- in-process eviction sequence, and the registry-less CLI path
# ---------------------------------------------------------------------------


async def test_in_process_eviction_runs_teardown_sequence_before_drop(built_vault):
    log = []
    services = _SpyServices(log)
    registry = {built_vault.vault_id: services}
    prov = _SpyProvisioner(log=log)

    rc = await delete_vault(**_common(built_vault, provisioner=prov, registry=registry))

    assert rc == 0
    # The registry-reload teardown order runs, and the schema drop follows it.
    assert log == ["stop_worker", "close_timing", "close_storage", "drop"]
    assert built_vault.vault_id not in registry  # evicted


async def test_registry_without_the_vault_skips_eviction(built_vault):
    prov = _SpyProvisioner()
    # A live registry that does not hold this vault: no eviction, drop still runs.
    rc = await delete_vault(**_common(built_vault, provisioner=prov, registry={}))

    assert rc == 0
    assert prov.dropped == [built_vault.vault_id]
    assert not built_vault.config_path.exists()


async def test_no_registry_is_the_standalone_cli_path(built_vault):
    prov = _SpyProvisioner()
    rc = await delete_vault(**_common(built_vault, provisioner=prov, registry=None))

    assert rc == 0
    assert prov.dropped == [built_vault.vault_id]
    assert not built_vault.config_path.exists()


# ---------------------------------------------------------------------------
# D.21 -- idempotent re-run
# ---------------------------------------------------------------------------


async def test_idempotent_rerun(built_vault):
    prov = _SpyProvisioner()
    rc1 = await delete_vault(**_common(built_vault, provisioner=prov))
    assert rc1 == 0
    assert not built_vault.config_path.exists()

    # Second run: schema/trees/config are already gone; it tolerates all of them.
    rc2 = await delete_vault(**_common(built_vault, provisioner=prov))
    assert rc2 == 0
