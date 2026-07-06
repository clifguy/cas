"""The cloud maintenance-job dispatcher (``sage.maintenance.cloud_job``).

One generalized in-VNet job routes every out-of-band cloud maintenance
operation by the ``SAGE_MAINTENANCE_COMMAND`` override. The dispatcher only
routes — each entrypoint owns its own env contract and envelope — so these
tests pin exact command→entrypoint routing and the unknown-command refusal.
"""

from sage.maintenance.cloud_job import main


def test_routes_delete_vault_to_the_teardown_entrypoint(monkeypatch):
    calls = []
    monkeypatch.setattr("sage.maintenance.delete_vault_cloud.main", lambda: calls.append("d") or 0)
    monkeypatch.setattr(
        "sage.maintenance.purge_cloud.main",
        lambda: (_ for _ in ()).throw(AssertionError("purge must not run")),
    )
    monkeypatch.setenv("SAGE_MAINTENANCE_COMMAND", "delete_vault")

    assert main() == 0
    assert calls == ["d"]


def test_routes_each_purge_mode_to_the_purge_entrypoint(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "sage.maintenance.delete_vault_cloud.main",
        lambda: (_ for _ in ()).throw(AssertionError("delete must not run")),
    )
    monkeypatch.setattr("sage.maintenance.purge_cloud.main", lambda: calls.append("p") or 0)

    for command in ("purge_document", "purge_chain", "purge_batch"):
        monkeypatch.setenv("SAGE_MAINTENANCE_COMMAND", command)
        assert main() == 0
    assert calls == ["p", "p", "p"]


def test_unknown_or_missing_command_refuses(monkeypatch):
    monkeypatch.delenv("SAGE_MAINTENANCE_COMMAND", raising=False)
    assert main() == 2
    monkeypatch.setenv("SAGE_MAINTENANCE_COMMAND", "optimize_everything")
    assert main() == 2


def test_exit_code_passes_through(monkeypatch):
    """The dispatcher surfaces the routed entrypoint's exit code unchanged."""
    monkeypatch.setattr("sage.maintenance.purge_cloud.main", lambda: 3)
    monkeypatch.setenv("SAGE_MAINTENANCE_COMMAND", "purge_document")
    assert main() == 3
