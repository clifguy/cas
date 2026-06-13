"""Tests for _initialize_bff_auth profile dispatch.

Verifies that the cloud profile wires a managed-identity connection_class into
PostgresSessionStore and the local profile does not, without touching real
Postgres or Azure credentials.

All imports inside _initialize_bff_auth are deferred (inside the function
body), so patches must target the source modules, not sage.app.*.
"""

from __future__ import annotations

import types

from fastapi import FastAPI

from sage.app import _initialize_bff_auth

# Sentinel class representing the token-auth connection class produced by
# make_token_auth_connection_class in the cloud branch.
_SENTINEL_CLASS = object()


def _fake_stack_cfg(profile: str):
    pg = types.SimpleNamespace(
        host="pg.example.com",
        port=5432,
        database="cas",
        user="bff",
        sslmode="require",
    )
    return types.SimpleNamespace(profile=profile, postgres=pg)


def _stub_settings():
    return types.SimpleNamespace(
        tenant_id="t",
        client_id="c",
        client_secret="s",
        sage_app_id_uri="u",
        redirect_uri="r",
        session_cookie_name="sid",
        session_ttl_seconds=3600,
    )


class _StubOidc:
    pass


class _RecordingStore:
    """Captures constructor kwargs; open/close are no-ops."""

    captured_kwargs: dict | None = None

    def __init__(self, conninfo: str, **kwargs):
        _RecordingStore.captured_kwargs = kwargs

    async def open(self):
        pass

    async def close(self):
        pass


class TestBffInitProfileDispatch:
    def _patch_common(self, monkeypatch):
        """Patch all deferred imports shared between cloud and local paths."""
        monkeypatch.setattr(
            "app.backend.auth.config.load_bff_auth_settings",
            lambda env: _stub_settings(),
        )
        monkeypatch.setattr(
            "app.backend.auth.config.BffAuthContext",
            lambda **kwargs: types.SimpleNamespace(**kwargs),
        )
        monkeypatch.setattr(
            "app.backend.auth.oidc.MsalOidcService",
            lambda settings: _StubOidc(),
        )
        monkeypatch.setattr(
            "app.backend.auth.session_store.PostgresSessionStore",
            _RecordingStore,
        )

    async def test_bff_mi_004_cloud_profile_injects_managed_identity(self, monkeypatch):
        """BFF-MI-004: cloud profile → PostgresSessionStore receives a non-None connection_class."""
        _RecordingStore.captured_kwargs = None
        self._patch_common(monkeypatch)
        monkeypatch.setattr(
            "sage.storage.postgres.managed_identity.get_postgres_credential",
            lambda: object(),
        )
        monkeypatch.setattr(
            "sage.storage.postgres.managed_identity.make_token_auth_connection_class",
            lambda cred: _SENTINEL_CLASS,
        )

        app = FastAPI()
        await _initialize_bff_auth(app, _fake_stack_cfg("cloud"))

        assert _RecordingStore.captured_kwargs is not None
        assert _RecordingStore.captured_kwargs.get("connection_class") is _SENTINEL_CLASS

    async def test_bff_mi_005_local_profile_omits_connection_class(self, monkeypatch):
        """BFF-MI-005: local profile → PostgresSessionStore receives connection_class=None."""
        _RecordingStore.captured_kwargs = None
        self._patch_common(monkeypatch)

        app = FastAPI()
        await _initialize_bff_auth(app, _fake_stack_cfg("local"))

        assert _RecordingStore.captured_kwargs is not None
        assert _RecordingStore.captured_kwargs.get("connection_class") is None
