"""Cloud-profile bulk-reabstract entrypoint (``sage.maintenance.reabstract_cloud``).

Exercises the env-driven entrypoint against fakes: the env-config builder, the
binding factories, vault resolution, service construction, and the sweep core
are patched. No Azure SDK, no live tenant, no Postgres -- the cloud wiring is
verified structurally; the real in-cloud run is the out-of-band post-deploy
smoke (CAS-ADR-043).

Unlike the purge entrypoints this one needs a live abstraction provider, so the
tests pin the two things that distinguish it: the request is refused before any
store is opened when the abstraction model is unconfigured, and the services are
torn down even when the sweep raises.
"""

from types import SimpleNamespace

import sage.maintenance.reabstract_cloud as rc
from sage.models.enums import PipelineStatus

_REABSTRACT_ENV = (
    "SAGE_MAINTENANCE_COMMAND",
    "SAGE_REABSTRACT_VAULT_ID",
    "SAGE_REABSTRACT_STATUSES",
    "SAGE_REABSTRACT_LIMIT",
    "SAGE_REABSTRACT_CONFIRM",
    "SAGE_REABSTRACT_APPLY",
    "SAGE_REABSTRACT_REASON",
    "ABSTRACTION_MODEL",
    "ABSTRACTION_PROVIDER",
)

_FAKE_STACK = SimpleNamespace(abstraction=SimpleNamespace(provider="anthropic", model="a-model"))


class _FakeSourceStore:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _CredentialCloseRecorder:
    def __init__(self):
        self.calls = 0

    async def __call__(self):
        self.calls += 1


class _FakeIngestion:
    def __init__(self):
        self.stopped = False
        self.restamp_arg = None

    async def stop_worker(self, *, restamp: bool = True):
        self.stopped = True
        self.restamp_arg = restamp


class _FakeServices:
    def __init__(self):
        self.ingestion_service = _FakeIngestion()
        self.graph_store = object()
        self.storage_closed = False
        self.timing_closed = False

    def close_timing(self):
        self.timing_closed = True

    async def close_storage(self):
        self.storage_closed = True


def _torn_down(fakes) -> bool:
    """Whether every short-lived resource the job allocates was released."""
    return (
        fakes.services.ingestion_service.stopped
        and fakes.services.storage_closed
        and fakes.services.timing_closed
        and fakes.source_store.closed
        and fakes.credential_close.calls == 1
    )


def _clear_env(monkeypatch):
    for name in _REABSTRACT_ENV:
        monkeypatch.delenv(name, raising=False)


def _base_env(monkeypatch, **overrides):
    values = {
        "SAGE_MAINTENANCE_COMMAND": "reabstract",
        "SAGE_REABSTRACT_VAULT_ID": "cas_smoke",
        "SAGE_REABSTRACT_CONFIRM": "cas_smoke",
        "SAGE_REABSTRACT_REASON": "provider outage recovery",
        "ABSTRACTION_MODEL": "a-model",
    }
    values.update(overrides)
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def _patch_resolution(monkeypatch, *, services=None, vault_config=object(), source_store=None):
    """Patch the env-config builder, bindings, resolution, and service build."""
    services = services if services is not None else _FakeServices()
    source_store = source_store if source_store is not None else _FakeSourceStore()
    opened: list[str] = []

    monkeypatch.setattr(rc, "_config_from_env", lambda env: _FAKE_STACK)
    monkeypatch.setattr("sage.mcp_init.set_stack_config", lambda cfg: None)
    monkeypatch.setattr(
        "sage.mcp_init.resolve_stack_vault_source_store", lambda cfg, **kw: source_store
    )
    monkeypatch.setattr("sage.mcp_init.resolve_stack_abstraction_provider", lambda cfg: object())

    def _resolve(source, vault_id):
        opened.append(vault_id)
        return vault_config

    monkeypatch.setattr(rc, "resolve_vault_config", _resolve)

    async def _init(config, **kwargs):
        return services

    monkeypatch.setattr("sage.mcp_init.initialize_services", _init)
    credential_close = _CredentialCloseRecorder()
    monkeypatch.setattr(
        "sage.storage.postgres.managed_identity.close_postgres_credential", credential_close
    )
    return SimpleNamespace(
        services=services,
        source_store=source_store,
        opened=opened,
        credential_close=credential_close,
    )


def _capture_core(monkeypatch, *, rc_value=0, raises=False):
    captured: dict = {}

    async def _fake(**kwargs):
        captured.update(kwargs)
        if raises:
            raise RuntimeError("sweep exploded")
        return rc_value

    monkeypatch.setattr(rc, "reabstract_bulk", _fake)
    return captured


def test_env_maps_onto_the_sweep_core(monkeypatch):
    _clear_env(monkeypatch)
    fakes = _patch_resolution(monkeypatch)
    captured = _capture_core(monkeypatch)
    _base_env(
        monkeypatch,
        SAGE_REABSTRACT_STATUSES="failed",
        SAGE_REABSTRACT_LIMIT="25",
        SAGE_REABSTRACT_APPLY="1",
    )

    assert rc.main() == 0

    assert captured["vault_id"] == "cas_smoke"
    assert captured["statuses"] == frozenset({PipelineStatus.FAILED.value})
    assert captured["limit"] == 25
    assert captured["apply"] is True
    assert captured["reason"] == "provider outage recovery"
    assert fakes.opened == ["cas_smoke"]
    assert _torn_down(fakes)


def test_missing_vault_id_refuses(monkeypatch):
    _clear_env(monkeypatch)
    _patch_resolution(monkeypatch)
    captured = _capture_core(monkeypatch)
    _base_env(monkeypatch, SAGE_REABSTRACT_VAULT_ID=None)

    assert rc.main() == 2
    assert captured == {}


def test_confirm_must_match_the_vault_id(monkeypatch):
    _clear_env(monkeypatch)
    fakes = _patch_resolution(monkeypatch)
    captured = _capture_core(monkeypatch)
    _base_env(monkeypatch, SAGE_REABSTRACT_CONFIRM="wrong")

    assert rc.main() == 2
    assert captured == {}
    assert fakes.opened == [], "a mismatched confirmation must refuse before opening the vault"


def test_dry_run_is_the_default_when_apply_is_unset(monkeypatch):
    _clear_env(monkeypatch)
    _patch_resolution(monkeypatch)
    captured = _capture_core(monkeypatch)
    _base_env(monkeypatch, SAGE_REABSTRACT_APPLY=None)

    assert rc.main() == 0
    assert captured["apply"] is False


def test_apply_flag_is_honored(monkeypatch):
    """Positive control for the dry-run default: the flag genuinely reaches the core."""
    _clear_env(monkeypatch)
    _patch_resolution(monkeypatch)
    captured = _capture_core(monkeypatch)
    _base_env(monkeypatch, SAGE_REABSTRACT_APPLY="true")

    assert rc.main() == 0
    assert captured["apply"] is True


def test_absent_selector_defaults_to_the_recovery_statuses(monkeypatch):
    _clear_env(monkeypatch)
    _patch_resolution(monkeypatch)
    captured = _capture_core(monkeypatch)
    _base_env(monkeypatch, SAGE_REABSTRACT_STATUSES=None)

    assert rc.main() == 0
    assert captured["statuses"] == frozenset(
        {
            PipelineStatus.FAILED.value,
            PipelineStatus.ABSTRACTION_SKIPPED.value,
            PipelineStatus.ABSTRACTION_INTERRUPTED.value,
        }
    )


def test_unknown_status_token_refuses_before_any_work(monkeypatch):
    _clear_env(monkeypatch)
    fakes = _patch_resolution(monkeypatch)
    captured = _capture_core(monkeypatch)
    _base_env(monkeypatch, SAGE_REABSTRACT_STATUSES="failed,not_a_status")

    assert rc.main() == 2
    assert captured == {}
    assert fakes.opened == []


def test_unknown_vault_refuses(monkeypatch):
    _clear_env(monkeypatch)
    _patch_resolution(monkeypatch, vault_config=None)
    captured = _capture_core(monkeypatch)
    _base_env(monkeypatch)

    assert rc.main() == 2
    assert captured == {}


def test_missing_abstraction_model_refuses_before_any_work(monkeypatch, capsys):
    _clear_env(monkeypatch)
    fakes = _patch_resolution(monkeypatch)
    captured = _capture_core(monkeypatch)
    _base_env(monkeypatch, ABSTRACTION_MODEL=None)

    assert rc.main() == 2
    assert captured == {}
    assert fakes.opened == [], "no vault may be opened when abstraction is unconfigured"
    assert "ABSTRACTION_MODEL" in capsys.readouterr().err


def test_services_are_torn_down_on_a_core_failure(monkeypatch):
    _clear_env(monkeypatch)
    fakes = _patch_resolution(monkeypatch)
    _capture_core(monkeypatch, raises=True)
    _base_env(monkeypatch, SAGE_REABSTRACT_APPLY="1")

    try:
        rc.main()
    except RuntimeError:
        pass

    assert _torn_down(fakes)


def test_teardown_settles_work_the_sweep_abandoned(monkeypatch):
    """This job's teardown keeps the interruption stamp; only delete_vault opts
    out of it.

    A short-lived job, so nothing runs behind it: the sweep abandons a document
    at its wait ceiling with the job for it still queued, and the process then
    exits. Without the stamp that document waits for the next server start.
    """
    _clear_env(monkeypatch)
    fakes = _patch_resolution(monkeypatch)
    _capture_core(monkeypatch, raises=True)
    _base_env(monkeypatch, SAGE_REABSTRACT_APPLY="1")

    try:
        rc.main()
    except RuntimeError:
        pass

    assert fakes.services.ingestion_service.restamp_arg is True


def test_sweep_exit_code_passes_through(monkeypatch):
    _clear_env(monkeypatch)
    _patch_resolution(monkeypatch)
    _capture_core(monkeypatch, rc_value=1)
    _base_env(monkeypatch, SAGE_REABSTRACT_APPLY="1")

    assert rc.main() == 1
