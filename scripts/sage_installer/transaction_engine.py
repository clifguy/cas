"""Reusable POSIX installation transactions with explicit verifier and layout adapters.

Only the adapters know package schemas, host locations, and ownership vocabulary.
This module and package_safety use only the Python standard library.
"""

import fcntl
import os
import re
import shutil
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

sys.dont_write_bytecode = True
from package_safety import (  # noqa: E402 - suppress bytecode before sibling imports
    atomic_json,
    digest,
    load_json,
    name,
    regular_path,
    relative,
    rename_exclusive,
    sync_dir,
    tree,
)


class TransactionEngine:
    def __init__(self, verifier, layouts, checkpoint=None):
        self.verifier = verifier
        self.layouts = layouts
        if checkpoint is not None:
            self.checkpoint = checkpoint

    def checkpoint(self, event):
        """Boundary for deterministic fault/crash trials; production does nothing."""

    def root_names(self, manifest, target_kind):
        slots = self.verifier.root_names(manifest, target_kind)
        if (
            not isinstance(slots, list)
            or len(slots) != len(set(slots))
            or "manifest.json" not in slots
        ):
            raise ValueError("invalid package ownership slots")
        for slot in slots:
            if slot != "manifest.json":
                name(slot)
        return slots

    def sealed(self, value):
        return {**value, "checksum": digest(value)}

    def unseal(self, value):
        if not isinstance(value, dict) or value.get("checksum") != digest(
            {k: v for k, v in value.items() if k != "checksum"}
        ):
            raise ValueError("record checksum mismatch")
        return {k: v for k, v in value.items() if k != "checksum"}

    def snapshot(self, target, names):
        return {n: tree(target / n) for n in names}

    def validate_snapshot(self, value):
        if not isinstance(value, dict):
            raise ValueError("invalid ownership snapshot")
        for n, entries in value.items():
            if n != "manifest.json":
                name(n)
            if entries is not None:
                if not isinstance(entries, dict) or "." not in entries:
                    raise ValueError("invalid snapshot entries")
                for path, e in entries.items():
                    if path != ".":
                        relative(path)
                    if (
                        not isinstance(e, dict)
                        or e.get("type") not in {"file", "directory"}
                        or type(e.get("mode")) is not int
                    ):
                        raise ValueError("invalid snapshot entry")
                    if e["type"] == "file" and not re.fullmatch(
                        "[0-9a-f]{64}", e.get("sha256", "")
                    ):
                        raise ValueError("invalid snapshot digest")

    def receipt_at(self, state):
        path = regular_path(state / "current.json")
        if not path.exists():
            return None
        receipt = self.unseal(load_json(path))
        if (
            set(receipt) != {"schema_version", "transaction", "owned", "manifest_digest"}
            or receipt["schema_version"] != 1
        ):
            raise ValueError("invalid installation receipt")
        self.transaction_id(receipt["transaction"])
        self.validate_snapshot(receipt["owned"])
        return receipt

    def inventory(self, repo, target_kind="codex-project"):
        repo, target, state = self.layouts.layout(repo, target_kind)
        pending = regular_path(state / "pending.json")
        if pending.exists():
            raise ValueError("unfinished installation; run recover before using affected skills")
        receipt = self.receipt_at(state)
        manifest_path = regular_path(target / "manifest.json")

        def verify_target():
            return self.verifier.verify(
                self.layouts.payload_root(target), installed=True, manifest_path=manifest_path
            )

        if receipt:
            if self.snapshot(target, receipt["owned"]) != receipt["owned"]:
                raise ValueError("owned installation modified, missing or incomplete")
            manifest = verify_target()
            if digest(manifest) != receipt["manifest_digest"] or set(
                self.root_names(manifest, target_kind)
            ) != set(receipt["owned"]):
                raise ValueError("receipt/manifest ownership mismatch")
            return {
                "status": "verified",
                "manifest": manifest,
                "receipt": receipt,
                "owned": receipt["owned"],
            }
        if manifest_path.exists():
            if not self.layouts.allow_legacy(target_kind):
                raise ValueError("manifest requires managed receipt")
            manifest = verify_target()
            return {
                "status": "legacy-verified",
                "manifest": manifest,
                "receipt": None,
                "owned": self.snapshot(target, self.root_names(manifest, target_kind)),
            }
        return {"status": "unmanaged", "manifest": None, "receipt": None, "owned": {}}

    def plan(self, repo, bundle, target_kind="codex-project", adopt=None):
        repo, target, state = self.layouts.layout(repo, target_kind)
        adopt = [] if adopt is None else adopt
        adopted = self.layouts.adoption_slots(adopt, target_kind)
        if not Path(bundle).is_absolute():
            raise ValueError("explicit absolute package required")
        bundle = regular_path(bundle)
        if not bundle.is_dir():
            raise ValueError("explicit package directory required")
        if bundle.is_relative_to(repo) or repo.is_relative_to(bundle):
            raise ValueError("package and target repository must be disjoint")
        manifest = self.verifier.verify(bundle)
        self.verifier.validate_target(manifest, target_kind)
        current = self.inventory(repo, target_kind)
        new_names = self.root_names(manifest, target_kind)
        source = self.layouts.source(bundle, target_kind)
        collisions = {n for n in new_names if n not in current["owned"] and (target / n).exists()}
        if adopted != collisions:
            if collisions:
                raise ValueError("unowned destination collision: " + ", ".join(sorted(collisions)))
            if adopted:
                raise ValueError("adoption names must identify actual unowned collisions")
        for n in new_names:
            if n not in current["owned"] and n not in adopted and (target / n).exists():
                raise ValueError("unowned destination collision: " + n)
            regular_path(target / n)
        action = "install"
        if (
            current["receipt"]
            and current["manifest"] == manifest
            and self.snapshot(target, new_names) == self.snapshot(source, new_names)
        ):
            action = "already-current"
        elif adopted:
            action = "adopt-explicit-snapshot"
        elif current["owned"]:
            action = "upgrade" if current["receipt"] else "adopt-verified-legacy"
        return self.sealed(
            {
                "schema_version": 1,
                "repo": str(repo),
                "bundle": str(bundle),
                "destination": str(target),
                **(
                    {"target_kind": target_kind, "adopt": adopt}
                    if target_kind != "codex-project"
                    else {}
                ),
                "action": action,
                "manifest_digest": digest(manifest),
                "package_tree": tree(bundle),
                "target_tree": self.layouts.observation(target),
                "prior_receipt": current["receipt"],
                "before": self.snapshot(target, sorted(set(current["owned"]) | set(new_names))),
                "after": {
                    n: tree(source / n) if n in new_names else None
                    for n in sorted(set(current["owned"]) | set(new_names))
                },
            }
        )

    @contextmanager
    def lock(self, repo, target_kind="codex-project"):
        _, _, state = self.layouts.layout(repo, target_kind)
        state.mkdir(parents=True, exist_ok=True)
        path = regular_path(state / "lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("another installer holds the target lock") from exc
            yield state
        finally:
            os.close(fd)

    def copy_entry(self, source, dest):
        regular_path(source)
        regular_path(dest)
        if dest.exists():
            raise ValueError("transaction destination collision")
        if source.is_dir():
            shutil.copytree(source, dest)
        else:
            shutil.copy2(source, dest)
        files = [p for p in dest.rglob("*") if p.is_file()] if dest.is_dir() else [dest]
        for path in files:
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        if dest.is_dir():
            for path in sorted((p for p in dest.rglob("*") if p.is_dir()), reverse=True):
                sync_dir(path)
            sync_dir(dest)
        sync_dir(dest.parent)

    def transaction_id(self, value):
        if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{32}", value):
            raise ValueError("invalid transaction id")
        return value

    def record(self, state, tx):
        tx = self.transaction_id(tx)
        folder = regular_path(state / "transactions" / tx)
        data = self.unseal(load_json(folder / "record.json"))
        if (
            set(data) - {"target_kind"}
            != {"schema_version", "repo", "before", "after", "before_receipt", "after_receipt"}
            or data["schema_version"] != 1
        ):
            raise ValueError("invalid transaction record")
        self.validate_snapshot(data["before"])
        self.validate_snapshot(data["after"])
        if set(data["before"]) != set(data["after"]):
            raise ValueError("transaction ownership mismatch")
        return folder, data

    def check_copies(self, folder, data):
        for side in ["before", "after"]:
            if self.snapshot(folder / side, data[side]) != data[side]:
                raise ValueError("transaction backup/staging integrity mismatch: " + side)

    def set_receipt(self, state, receipt):
        if receipt is None:
            path = regular_path(state / "current.json")
            if path.exists():
                path.unlink()
                sync_dir(state)
        else:
            atomic_json(state / "current.json", self.sealed(receipt))

    def restore(self, repo, state, tx, target_kind="codex-project"):
        """Recover to the prior state, accepting only known transaction states."""
        _, target, _ = self.layouts.layout(repo, target_kind)
        folder, data = self.record(state, tx)
        if data.get("target_kind", "codex-project") != target_kind:
            raise ValueError("transaction target kind mismatch")
        if data["repo"] != str(repo):
            raise ValueError("transaction belongs to another repository")
        self.check_copies(folder, data)
        current = self.receipt_at(state)
        if current not in [data["before_receipt"], data["after_receipt"]]:
            raise ValueError("concurrent receipt change; recovery refused")
        recovery_path = regular_path(folder / "recovery.json")
        if not recovery_path.exists():
            initial = self.snapshot(target, data["before"])
            # A missing destination is ours only while its publication copy remains
            # and any previous root has actually been retired by this transaction.
            for n, actual in initial.items():
                before, after = data["before"][n], data["after"][n]
                if actual is None:
                    if (
                        before is not None
                        and tree(folder / ("retired-" + n)) != before
                        or after is not None
                        and tree(folder / ("publish-" + n)) != after
                    ):
                        raise ValueError("concurrent owned deletion; recovery refused: " + n)
                elif actual not in [before, after]:
                    raise ValueError("concurrent owned edit; recovery refused: " + n)
            recovery_id = uuid.uuid4().hex
            recovery = folder / ("recovery-" + recovery_id)
            recovery.mkdir()
            (recovery / "stage").mkdir()
            (recovery / "retired").mkdir()
            for n, before in data["before"].items():
                if before is not None:
                    self.copy_entry(folder / "before" / n, recovery / "stage" / n)
            if self.snapshot(recovery / "stage", data["before"]) != data["before"]:
                raise ValueError("recovery staging integrity mismatch")
            if self.snapshot(target, initial) != initial:
                raise ValueError("concurrent change before recovery")
            atomic_json(recovery_path, self.sealed({"id": recovery_id, "initial": initial}))
        recovery_data = self.unseal(load_json(recovery_path))
        if set(recovery_data) != {"id", "initial"}:
            raise ValueError("invalid recovery record")
        initial = recovery_data["initial"]
        self.validate_snapshot(initial)
        if set(initial) != set(data["before"]):
            raise ValueError("invalid recovery ownership")
        recovery = regular_path(folder / ("recovery-" + self.transaction_id(recovery_data["id"])))

        def checked_actual(n):
            actual = tree(target / n)
            before = data["before"][n]
            start = initial[n]
            stage = tree(recovery / "stage" / n)
            retired = tree(recovery / "retired" / n)
            if retired is not None and retired != start:
                raise ValueError("recovery retired integrity mismatch: " + n)
            if stage is not None and stage != before:
                raise ValueError("recovery staging integrity mismatch: " + n)
            if actual is None:
                # Consumed restoration copies prove restoration already happened;
                # absence after that is a later deletion, never a resumable gap.
                if before is not None and stage != before or start is not None and retired != start:
                    raise ValueError("concurrent owned deletion during recovery: " + n)
            elif actual not in [before, start]:
                raise ValueError("concurrent owned edit during recovery: " + n)
            elif actual != before and (retired is not None or stage != before):
                raise ValueError("concurrent owned change after recovery: " + n)
            return actual

        # Preflight the complete population before changing any target root.
        for n in initial:
            checked_actual(n)
        for n, before in data["before"].items():
            dest = target / n
            actual = checked_actual(n)
            if actual == before:
                continue
            if actual is not None:
                rename_exclusive(dest, recovery / "retired" / n)
                sync_dir(dest.parent)
                sync_dir(recovery / "retired")
            self.checkpoint("recovery-old-moved")
            if before is not None:
                rename_exclusive(recovery / "stage" / n, dest)
                sync_dir(dest.parent)
                sync_dir(recovery / "stage")
            self.checkpoint("recovery-new-moved")
        self.set_receipt(state, data["before_receipt"])
        if self.snapshot(target, data["before"]) != data["before"]:
            raise ValueError("recovery verification failed")
        atomic_json(folder / "outcome.json", {"status": "restored"})
        (state / "pending.json").unlink()
        sync_dir(state)
        return {"status": "restored", "transaction": tx}

    def recover(self, repo, target_kind="codex-project"):
        repo, _, state = self.layouts.layout(repo, target_kind)
        with self.lock(repo, target_kind):
            pending = regular_path(state / "pending.json")
            if not pending.exists():
                return {"status": "no-pending-transaction"}
            data = self.unseal(load_json(pending))
            if set(data) != {"transaction"}:
                raise ValueError("invalid pending record")
            return self.restore(repo, state, self.transaction_id(data["transaction"]), target_kind)

    def perform(
        self,
        repo,
        target,
        state,
        before,
        after,
        before_receipt,
        source,
        target_kind="codex-project",
    ):
        tx = uuid.uuid4().hex
        folder = state / "transactions" / tx
        regular_path(folder).mkdir(parents=True, exist_ok=False)
        (folder / "before").mkdir()
        (folder / "after").mkdir()
        for n, expected in after.items():
            if expected is not None:
                self.copy_entry(source / n, folder / "after" / n)
        if self.snapshot(folder / "after", after) != after:
            raise ValueError("package changed while staging")
        for n, expected in after.items():
            if expected is not None:
                self.copy_entry(folder / "after" / n, folder / ("publish-" + n))
        self.checkpoint("staged")
        for n, expected in before.items():
            if tree(target / n) != expected:
                raise ValueError("target changed before backup")
            if expected is not None:
                self.copy_entry(target / n, folder / "before" / n)
        if self.snapshot(folder / "before", before) != before:
            raise ValueError("target changed while backing up")
        manifest = load_json(folder / "after/manifest.json") if after.get("manifest.json") else None
        after_receipt = (
            {
                "schema_version": 1,
                "transaction": tx,
                "owned": {n: v for n, v in after.items() if v is not None},
                "manifest_digest": digest(manifest),
            }
            if manifest
            else None
        )
        data = {
            "schema_version": 1,
            "repo": str(repo),
            "before": before,
            "after": after,
            **({"target_kind": target_kind} if target_kind != "codex-project" else {}),
            "before_receipt": before_receipt,
            "after_receipt": after_receipt,
        }
        atomic_json(folder / "record.json", self.sealed(data))
        self.checkpoint("backed-up")
        if self.snapshot(target, before) != before:
            raise ValueError("target changed before replacement")
        atomic_json(state / "pending.json", self.sealed({"transaction": tx}))
        try:
            target.mkdir(parents=True, exist_ok=True)
            for n, expected in after.items():
                dest = regular_path(target / n)
                if tree(dest) != before[n]:
                    raise ValueError("target changed during replacement: " + n)
                if before[n] is not None:
                    rename_exclusive(dest, folder / ("retired-" + n))
                    sync_dir(dest.parent)
                self.checkpoint("old-moved")
                if expected is not None:
                    stage = folder / ("publish-" + n)
                    if dest.exists() or dest.is_symlink():
                        raise ValueError("concurrent destination collision")
                    rename_exclusive(stage, dest)
                    sync_dir(dest.parent)
                self.checkpoint("new-moved")
            if self.snapshot(target, after) != after:
                raise ValueError("post-install integrity mismatch")
            self.set_receipt(state, after_receipt)
            self.checkpoint("receipt-written")
            atomic_json(folder / "outcome.json", {"status": "installed"})
            (state / "pending.json").unlink()
            sync_dir(state)
            return {"status": "installed", "transaction": tx, "manifest_digest": digest(manifest)}
        except Exception:
            # If concurrent edits prevent safe restoration, leave the pending record
            # and preserved copies; explicit recover will report the conflict.
            self.restore(repo, state, tx, target_kind)
            raise

    def apply(self, prepared):
        data = self.unseal(prepared)
        if data.get("schema_version") != 1:
            raise ValueError("unsupported plan version")
        target_kind = data.get("target_kind", "codex-project")
        repo, target, state = self.layouts.layout(data["repo"], target_kind)
        adopted = data.get("adopt", [])
        fresh = self.plan(repo, data["bundle"], target_kind, adopted)
        if fresh != prepared:
            raise ValueError("stale or altered install plan")
        if data["action"] == "already-current":
            return {"status": "already-current"}
        with self.lock(repo, target_kind):
            if self.plan(repo, data["bundle"], target_kind, adopted) != prepared:
                raise ValueError("stale install plan under lock")
            return self.perform(
                repo,
                target,
                state,
                data["before"],
                data["after"],
                data["prior_receipt"],
                self.layouts.source(regular_path(data["bundle"]), target_kind),
                target_kind,
            )

    def rollback(self, repo, tx, target_kind="codex-project"):
        repo, target, state = self.layouts.layout(repo, target_kind)
        # Read-only preflight: conflicts must not even alter state files.
        current = self.inventory(repo, target_kind)
        folder, data = self.record(state, tx)
        if data.get("target_kind", "codex-project") != target_kind:
            raise ValueError("transaction target kind mismatch")
        if (
            data["repo"] != str(repo)
            or not current["receipt"]
            or current["receipt"]["transaction"] != tx
        ):
            raise ValueError("rollback requires the current transaction of this repository")
        self.check_copies(folder, data)
        if self.snapshot(target, data["after"]) != data["after"]:
            raise ValueError("owned rollback conflict")
        with self.lock(repo, target_kind):
            if (
                self.inventory(repo, target_kind) != current
                or self.snapshot(target, data["after"]) != data["after"]
            ):
                raise ValueError("concurrent change before rollback")
            # Rollback is itself a recoverable transaction; its backup is the current
            # installation, and its replacement source is the original before copy.
            return self.perform(
                repo,
                target,
                state,
                data["after"],
                data["before"],
                current["receipt"],
                folder / "before",
                target_kind,
            )
