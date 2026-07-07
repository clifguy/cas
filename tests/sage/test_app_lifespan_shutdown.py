"""Shutdown-hygiene tests for the SAGE Core API lifespan.

The long-lived FastAPI server builds two clients whose transports must be
released at shutdown, or each surfaces a benign resource warning on process
exit: the process-wide cached async Entra credential (an ``aiohttp`` session,
via ``close_postgres_credential``) and the vault-source store's SharePoint
``httpx`` client (via ``DocumentStoreVaultSourceStore.close``). The close
primitives already exist; these tests pin that the lifespan ``finally`` wires
them in, closes the credential last (after every Postgres consumer is drained),
runs on the raising path, and is a safe no-op when no vault-source store was
retained (the ``config=``/``configs=``/no-vault-root branches, and the local
filesystem binding whose ``close()`` is an inherited no-op).

Driven hermetically -- no real Postgres -- by entering the lifespan directly
(``app.router.lifespan_context``) with an empty ``discover()`` so no vault is
initialized, and monkeypatching the two resolvers the lifespan reaches for at
call time. Fakes follow the recorder style of
``tests/sage/maintenance/test_delete_vault_cloud.py`` and the service stand-ins
of ``tests/sage/test_vault_registry.py``.
"""

from __future__ import annotations

import pytest

from sage.app import create_app


class _RecordingSourceStore:
    """Vault-source store stand-in: ``discover()`` yields nothing (so the
    lifespan initializes no vault and needs no real storage) and ``close()``
    records that it ran."""

    def __init__(self, order: list[str] | None = None) -> None:
        self.closed = False
        self._order = order

    def discover(self) -> list:
        return []

    def close(self) -> None:
        self.closed = True
        if self._order is not None:
            self._order.append("source")


class _CredentialCloseRecorder:
    """Async stand-in for ``close_postgres_credential`` that counts its calls."""

    def __init__(self, order: list[str] | None = None) -> None:
        self.calls = 0
        self._order = order

    async def __call__(self) -> None:
        self.calls += 1
        if self._order is not None:
            self._order.append("credential")


class _FakeIngestionService:
    async def stop_worker(self) -> None:
        pass


class _FakeServices:
    """Minimal ``SAGEServices`` stand-in covering exactly what the lifespan
    teardown loop touches: ``ingestion_service.stop_worker``, ``close_timing``,
    and ``close_storage``. ``close_storage`` records so the ordering test can
    assert the credential closes after the storage pool is drained."""

    def __init__(self, order: list[str] | None = None) -> None:
        self.ingestion_service = _FakeIngestionService()
        self._order = order
        self.close_timing_called = False
        self.close_storage_called = False

    def close_timing(self) -> None:
        self.close_timing_called = True

    async def close_storage(self) -> None:
        self.close_storage_called = True
        if self._order is not None:
            self._order.append("storage")


def _patch_shutdown_probes(
    monkeypatch,
    *,
    credential: _CredentialCloseRecorder,
    source_store: _RecordingSourceStore | None = None,
) -> None:
    """Route the lifespan's two teardown resolvers to recording fakes.

    The lifespan late-imports ``resolve_stack_vault_source_store`` from
    ``sage.mcp_init`` at startup and ``close_postgres_credential`` from
    ``sage.storage.postgres.managed_identity`` inside the ``finally``; patching
    the source-module attributes is picked up at call time.
    """
    if source_store is not None:
        monkeypatch.setattr(
            "sage.mcp_init.resolve_stack_vault_source_store",
            lambda *a, **k: source_store,
        )
    monkeypatch.setattr(
        "sage.storage.postgres.managed_identity.close_postgres_credential",
        credential,
    )


async def test_app_shutdown_closes_source_store_and_credential(monkeypatch, tmp_path):
    """A clean lifespan shutdown closes the retained vault-source store and the
    cached Entra credential's aiohttp session.

    Anti-coincidental-pass: the unmodified ``finally`` closes neither, so
    ``store.closed`` stays False and ``cred.calls`` stays 0 -- both assertions
    fail against a lifespan that does not wire in the two close primitives.
    """
    store = _RecordingSourceStore()
    cred = _CredentialCloseRecorder()
    _patch_shutdown_probes(monkeypatch, source_store=store, credential=cred)
    app = create_app(vault_root=tmp_path)

    async with app.router.lifespan_context(app):
        pass

    assert store.closed is True
    assert cred.calls == 1


async def test_app_shutdown_closes_credential_after_storage_drained(monkeypatch, tmp_path):
    """The credential closes LAST -- after every per-vault storage pool is
    drained -- so no in-flight connection needs it when it is released.

    Anti-coincidental-pass: a ``close_postgres_credential`` call placed anywhere
    but last (e.g. at the top of the ``finally``, before the vault loop) would
    make "credential" precede "storage" in the recorded order; the index
    assertions fail. Guards ordering, not merely that the call happened.
    """
    order: list[str] = []
    store = _RecordingSourceStore(order=order)
    cred = _CredentialCloseRecorder(order=order)
    _patch_shutdown_probes(monkeypatch, source_store=store, credential=cred)
    app = create_app(vault_root=tmp_path)

    async with app.router.lifespan_context(app):
        # Insert after startup: the startup discover() yields nothing, so a
        # storage-bearing service only exists in the teardown loop if we add it
        # here. The finally's registry.clear() (and the autouse registry-isolation
        # fixture) drop it again.
        app.state.vault_registry["ordering-probe"] = _FakeServices(order=order)

    assert order[-1] == "credential"
    assert order.index("storage") < order.index("credential")


async def test_app_shutdown_teardown_runs_when_run_body_raises(monkeypatch, tmp_path):
    """The two closes are ``finally``-guaranteed: an exception raised during the
    server's run still closes the store and the credential before it propagates.

    Anti-coincidental-pass: a ``try``/``return`` (or a close done at the end of
    startup rather than in the ``finally``) would skip both closes on the raising
    path, leaving the recorders unset.
    """
    store = _RecordingSourceStore()
    cred = _CredentialCloseRecorder()
    _patch_shutdown_probes(monkeypatch, source_store=store, credential=cred)
    app = create_app(vault_root=tmp_path)

    with pytest.raises(RuntimeError, match="boom"):
        async with app.router.lifespan_context(app):
            raise RuntimeError("boom")

    assert store.closed is True
    assert cred.calls == 1


async def test_app_shutdown_without_vault_source_store_still_closes_credential(monkeypatch):
    """With no vault_root, no vault-source store is built or retained; shutdown
    must not blow up on the missing reference and must still close the credential
    (the local-profile shape: the credential close is unconditional and no-ops
    when none was ever built).

    Anti-coincidental-pass: an UNGUARDED store close in the ``finally`` (touching
    ``vault_source_store`` directly rather than ``getattr(app.state, ..., None)``)
    raises at shutdown when nothing was retained; the clean-exit path fails. And
    ``cred.calls == 1`` proves the credential close is not gated behind a store
    having been built.
    """
    cred = _CredentialCloseRecorder()
    _patch_shutdown_probes(monkeypatch, credential=cred)
    app = create_app()  # no vault_root -> the store branch is skipped

    async with app.router.lifespan_context(app):
        pass

    assert cred.calls == 1
