"""Whole-vault teardown orchestration (Tier A: real filesystem + spy provisioner).

Exercises the ``delete_vault`` core against a real on-disk vault tree (via the
``FilesystemVaultSourceStore``) and a spy provisioner that records
``drop_vault_schema`` calls without a database, plus an injected ``dump_runner``
that never shells out to ``pg_dump``. The real Postgres schema drop is proven in
``tests/sage/test_storage_binding.py::test_sto_022`` and the DDL in
``tests/sage/test_postgres_schema.py``.
"""

import copy
import errno
import json
from types import SimpleNamespace

import pytest

import sage.vault_source_binding as vsb
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
    """Records drop_vault_schema calls; optionally raises to simulate a failure.

    ``schema_present`` backs the ``schema_exists`` predicate the snapshot step
    consults: the default ``True`` reflects a live schema (dump attempted), and
    ``False`` reflects a resume after the schema was already dropped (dump skipped).
    """

    def __init__(self, *, raises=None, log=None, schema_present=True):
        self.dropped = []
        self._raises = raises
        self._log = log
        self._schema_present = schema_present

    async def drop_vault_schema(self, vault_id):
        if self._log is not None:
            self._log.append("drop")
        self.dropped.append(vault_id)
        if self._raises is not None:
            raise self._raises

    async def schema_exists(self, vault_id):
        return self._schema_present


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


# ---------------------------------------------------------------------------
# D.23 -- every teardown removal tolerates a concurrent writer repopulating a
# directory mid-removal (storage_root, brain_root, and the enclosing vault dir)
# ---------------------------------------------------------------------------


async def test_teardown_tolerates_concurrent_writer_on_every_tree(built_vault, monkeypatch):
    """Each recursive removal in the teardown -- storage_root (via the source-store
    port), brain_root, and the enclosing vault dir -- tolerates a directory
    repopulated between rmtree's scan and its final rmdir (ENOTEMPTY).

    Anti-coincidental-pass: the FIRST removal of each target is sabotaged once with
    ENOTEMPTY and its path recorded. A target whose removal did not route through
    the resilient helper would never be sabotaged (the subset assertion catches it),
    and a removal that aborted on the transient error would leave the tree behind.
    """
    real_rmtree = vsb.shutil.rmtree
    sabotaged = set()

    def flaky(path, *args, **kwargs):
        key = str(path)
        if key not in sabotaged:
            sabotaged.add(key)
            raise OSError(errno.ENOTEMPTY, "Directory not empty", key)
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(vsb.shutil, "rmtree", flaky)
    monkeypatch.setattr(vsb, "_RMTREE_RETRY_BACKOFF_SECONDS", 0)

    rc = await delete_vault(**_common(built_vault, provisioner=_SpyProvisioner()))

    assert rc == 0
    assert not built_vault.storage_root.exists()
    assert not built_vault.brain_root.exists()
    assert not built_vault.vault_dir.exists()
    expected = {
        str(built_vault.storage_root.resolve()),
        str(built_vault.brain_root.resolve()),
        str(built_vault.vault_dir.resolve()),
    }
    assert expected <= sabotaged


# ---------------------------------------------------------------------------
# D.24 / D.25 -- the snapshot tolerates an already-dropped schema on resume: the
# schema dump is skipped rather than erroring on pg_dump against an absent schema
# ---------------------------------------------------------------------------


async def test_resume_with_snapshot_tolerates_already_dropped_schema(built_vault):
    """A resume after a partial teardown -- schema already dropped -- with the
    default snapshot ON skips the schema dump instead of erroring on pg_dump against
    the absent schema, and the teardown completes.

    Anti-coincidental-pass: the injected dump runner RAISES if called, so a snapshot
    that still attempted the dump would return 4; rc == 0 with no dump call proves
    the schema-exists gate skipped it.
    """
    prov = _SpyProvisioner(schema_present=False)
    dump = _SpyDump(raises=AssertionError("pg_dump must be skipped when the schema is absent"))
    rc = await delete_vault(**_common(built_vault, provisioner=prov, dump_runner=dump))

    assert rc == 0
    assert dump.calls == []
    assert not built_vault.config_path.exists()
    snapshot_dir = _deletions_dir(built_vault)
    receipts = list(snapshot_dir.glob(f"{built_vault.vault_id}-*/receipt.json"))
    assert len(receipts) == 1


async def test_snapshot_runs_when_the_schema_is_present(built_vault):
    """The AC-5 gate does not skip a needed snapshot: with the schema present
    (default), the dump still runs.

    Anti-coincidental-pass: asserting dump.calls is non-empty catches an inverted
    gate that skipped the dump whenever schema_exists was consulted.
    """
    prov = _SpyProvisioner(schema_present=True)
    dump = _SpyDump()
    rc = await delete_vault(**_common(built_vault, provisioner=prov, dump_runner=dump))

    assert rc == 0
    assert dump.calls  # snapshot attempted


# ---------------------------------------------------------------------------
# D.26 -- document-store binding: the retained-source tree is removed as a store
# op even though config_locator returns None (no filesystem locator, so nothing
# enters the per-target guard). Guards the cloud teardown path (CAS-ADR-043).
# ---------------------------------------------------------------------------


class _FakeDocStoreSourceStore:
    """A document-store-like source store: no filesystem locator (``config_locator``
    returns ``None``), recording the teardown store ops the core issues."""

    def __init__(self):
        self.deleted_trees = []
        self.deleted_configs = []

    def config_locator(self, vault_id):
        return None

    def delete_source_tree(self, vault_id, storage_root):
        self.deleted_trees.append((vault_id, storage_root))

    def delete_config(self, vault_id):
        self.deleted_configs.append(vault_id)


async def test_delete_source_tree_called_when_config_locator_returns_none(tmp_path):
    """A binding with no filesystem locator (``config_locator`` -> ``None``, the
    cloud document store) still has its retained-source tree removed via the port,
    like ``delete_config`` -- otherwise the external source tree is orphaned while
    the schema and config are removed.

    Anti-coincidental-pass: fails under the per-target guard alone, where the
    source-tree removal is gated on ``"storage_root" in guarded``; with
    ``config_locator`` returning ``None`` that key is never present, so
    ``delete_source_tree`` is skipped.
    """
    store = _FakeDocStoreSourceStore()
    prov = _SpyProvisioner()
    rc = await delete_vault(
        vault_id="cas_smoke",
        source_store=store,
        provisioner=prov,
        vault_root=tmp_path / "vaults",
        snapshot_dir=tmp_path / "deletions",
        reason="retire",
        apply=True,
        snapshot=False,
        dump_runner=_SpyDump(),
        input_fn=lambda _p: "cas_smoke",
    )

    assert rc == 0
    assert store.deleted_trees == [("cas_smoke", None)]
    assert store.deleted_configs == ["cas_smoke"]
    assert prov.dropped == ["cas_smoke"]


# ---------------------------------------------------------------------------
# D.27 -- injectable snapshot sink: runs inside the snapshot's fail-closed block,
# after the local dump/manifest write and before any destruction. The default is
# a no-op (the local snapshot files are already durable); a profile that needs a
# durable off-box sink (e.g. the cloud archive) injects one.
# ---------------------------------------------------------------------------


async def test_snapshot_sink_invoked_after_local_write_before_destroy(built_vault):
    """The sink runs after the local dump was attempted and before the schema drop.

    Anti-coincidental-pass: the sink captures ``len(dump.calls)`` at call time (must
    be 1 -- the dump already ran) and appends to a shared log the provisioner also
    writes to (``["sink", "drop"]`` -- the sink precedes destruction). A sink wired
    before the dump would see 0 calls; one wired after the drop would log
    ``["drop", "sink"]``.
    """
    log = []
    dump = _SpyDump()
    captured = {}

    def sink(run_dir):
        log.append("sink")
        captured["run_dir"] = run_dir
        captured["dump_calls_at_sink"] = len(dump.calls)

    prov = _SpyProvisioner(log=log)
    rc = await delete_vault(
        **_common(built_vault, provisioner=prov, dump_runner=dump, snapshot_sink=sink)
    )

    assert rc == 0
    assert captured["dump_calls_at_sink"] == 1
    assert log == ["sink", "drop"]
    assert captured["run_dir"].exists()


async def test_snapshot_sink_failure_halts_before_destroy(built_vault):
    """A raising sink returns 4 and performs no destruction (fail-closed, like a
    failed pg_dump).

    Anti-coincidental-pass: a sink wired after the destruction (or outside the
    fail-closed block) would leave ``prov.dropped`` non-empty and the trees gone.
    """
    prov = _SpyProvisioner()

    def sink(_run_dir):
        raise RuntimeError("archive upload failed")

    rc = await delete_vault(**_common(built_vault, provisioner=prov, snapshot_sink=sink))

    assert rc == 4
    assert prov.dropped == []
    assert built_vault.storage_root.exists()
    assert built_vault.config_path.exists()


async def test_default_snapshot_sink_is_noop(built_vault):
    """Not passing ``snapshot_sink`` uses the default no-op; the local teardown
    behaves exactly as before (snapshot ON, full cascade, receipt written)."""
    prov = _SpyProvisioner()
    rc = await delete_vault(**_common(built_vault, provisioner=prov))

    assert rc == 0
    assert prov.dropped == [built_vault.vault_id]
    assert not built_vault.config_path.exists()
    assert not built_vault.vault_dir.exists()
