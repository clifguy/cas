"""Tests for the sage.migrate CLI.

The CLI runs schema migrations on discovered vaults outside the serving
binary. Tests monkeypatch ``sage.migrate._migrate_vault`` with a recorder
so they verify the orchestration (which vaults, in what order, with what
exit code) without exercising the real storage layer.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from sage import migrate as migrate_cli

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _materialize_vault(root: Path, vault_id: str, base_config: dict) -> Path:
    """Write a vault directory under root and return its config path."""
    vault_dir = root / vault_id
    vault_dir.mkdir(parents=True, exist_ok=True)
    (vault_dir / "sources").mkdir(exist_ok=True)
    (vault_dir / "brain").mkdir(exist_ok=True)

    cfg = copy.deepcopy(base_config)
    cfg["vault"]["id"] = vault_id
    cfg["vault"]["name"] = vault_id.replace("_", " ").title()
    cfg["vault"]["storage_root"] = str(vault_dir / "sources")
    cfg["vault"]["brain_root"] = str(vault_dir / "brain")

    config_path = vault_dir / "vault_config.yaml"
    config_path.write_text(yaml.safe_dump(cfg))
    return config_path


@pytest.fixture
def vault_root(tmp_path) -> Path:
    root = tmp_path / "vault_root"
    root.mkdir()
    return root


def _patch_migrate_vault(monkeypatch, *, fail_for: set[str] | None = None):
    """Replace migrate._migrate_vault with a recorder.

    Returns (calls_list, fail_for_set). Calls list contains vault_id strings
    in invocation order. If a vault_id is in fail_for, the fake raises.
    """
    calls: list[str] = []
    fail_for = fail_for or set()

    async def fake_migrate(config, config_path):
        vault_id = config.vault.id
        calls.append(vault_id)
        if vault_id in fail_for:
            raise RuntimeError(f"simulated migration failure for {vault_id}")

    monkeypatch.setattr("sage.migrate._migrate_vault", fake_migrate)
    return calls


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_migrate_all_discovered_vaults(vault_root, minimal_vault_config_dict, monkeypatch):
    """#18: With no --vault flag, every discovered vault is migrated."""
    _materialize_vault(vault_root, "alpha", minimal_vault_config_dict)
    _materialize_vault(vault_root, "beta", minimal_vault_config_dict)
    calls = _patch_migrate_vault(monkeypatch)

    exit_code = migrate_cli.main(["--vault-root", str(vault_root)])

    assert exit_code == 0
    assert set(calls) == {"alpha", "beta"}


def test_vault_filter_limits_to_named_vault(vault_root, minimal_vault_config_dict, monkeypatch):
    """#19: --vault VAULT_ID restricts migration to that vault only."""
    _materialize_vault(vault_root, "alpha", minimal_vault_config_dict)
    _materialize_vault(vault_root, "beta", minimal_vault_config_dict)
    calls = _patch_migrate_vault(monkeypatch)

    exit_code = migrate_cli.main(["--vault-root", str(vault_root), "--vault", "alpha"])

    assert exit_code == 0
    assert calls == ["alpha"]


def test_unknown_vault_id_exits_nonzero(vault_root, minimal_vault_config_dict, monkeypatch, caplog):
    """#20: --vault for an id not present is a clear error and migrates nothing."""
    _materialize_vault(vault_root, "alpha", minimal_vault_config_dict)
    calls = _patch_migrate_vault(monkeypatch)

    with caplog.at_level("ERROR"):
        exit_code = migrate_cli.main(["--vault-root", str(vault_root), "--vault", "nonexistent"])

    assert exit_code != 0
    assert calls == []
    # The error must name the unknown id so the operator can see what they typoed.
    log_text = "\n".join(r.message for r in caplog.records).lower()
    assert "nonexistent" in log_text


def test_empty_vault_root_exits_zero(vault_root, monkeypatch, capsys):
    """#21: An empty vault root is not an error; nothing to migrate."""
    calls = _patch_migrate_vault(monkeypatch)

    exit_code = migrate_cli.main(["--vault-root", str(vault_root)])

    assert exit_code == 0
    assert calls == []


def test_migration_failure_halts_remaining_vaults(
    vault_root, minimal_vault_config_dict, monkeypatch
):
    """#22: Fail-fast on the first failure; later vaults are not attempted."""
    # Three vaults in deterministic alphabetical order: alpha, beta, gamma.
    # Beta fails, so gamma must not be attempted.
    _materialize_vault(vault_root, "alpha", minimal_vault_config_dict)
    _materialize_vault(vault_root, "beta", minimal_vault_config_dict)
    _materialize_vault(vault_root, "gamma", minimal_vault_config_dict)
    calls = _patch_migrate_vault(monkeypatch, fail_for={"beta"})

    exit_code = migrate_cli.main(["--vault-root", str(vault_root)])

    assert exit_code != 0
    assert calls == ["alpha", "beta"]
