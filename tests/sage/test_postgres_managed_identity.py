"""Tests for the cloud Postgres managed-identity token connection class (CAS-ADR-042).

The Entra-only cloud endpoint authenticates with a managed-identity access token
presented as the libpq password. These tests mock both the azure credential and
the psycopg base ``connect`` so nothing touches Azure or a real database; the
base ``connect`` is patched on ``psycopg.AsyncConnection`` so the subclass's
``super().connect(...)`` resolves to the recorder.

Test IDs follow PGMI-NNN.
"""

import psycopg
import pytest

from sage.storage.postgres.managed_identity import (
    POSTGRES_AAD_SCOPE,
    make_token_auth_connection_class,
)


class _FakeAccessToken:
    def __init__(self, token: str) -> None:
        self.token = token


class _FakeCredential:
    """Records each get_token call and returns a fixed token."""

    def __init__(self, token: str = "tok-123") -> None:  # noqa: S107 -- test token, not a credential
        self._token = token
        self.calls: list[tuple] = []

    async def get_token(self, *scopes, **kwargs):
        self.calls.append(scopes)
        return _FakeAccessToken(self._token)


def _patch_base_connect(monkeypatch) -> dict:
    """Patch psycopg.AsyncConnection.connect with an async classmethod recorder."""
    captured: dict = {"calls": []}

    async def _fake_connect(cls, conninfo: str = "", **kwargs):
        captured["calls"].append({"conninfo": conninfo, "kwargs": kwargs})
        return "FAKE_CONN"

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", classmethod(_fake_connect))
    return captured


async def test_pgmi_001_token_injected_as_password(monkeypatch):
    """connect acquires a token for the Postgres AAD scope and injects it as the
    libpq password, preserving the conninfo.

    Anti-coincidental-pass: the recorder captures the kwargs handed to the base
    connect. A subclass that skipped the injection (or used a static password)
    would not show ``password == "tok-123"``; one that requested the wrong scope
    would fail the ``calls`` assertion.
    """
    captured = _patch_base_connect(monkeypatch)
    cred = _FakeCredential(token="tok-123")
    conn_cls = make_token_auth_connection_class(cred)

    result = await conn_cls.connect("dbname=sage host=db.example user=svc")

    assert result == "FAKE_CONN"
    call = captured["calls"][-1]
    assert call["conninfo"] == "dbname=sage host=db.example user=svc"
    assert call["kwargs"]["password"] == "tok-123"
    assert cred.calls == [(POSTGRES_AAD_SCOPE,)]


async def test_pgmi_002_token_refetched_each_connect(monkeypatch):
    """A fresh token is acquired on every connect -- the pool opens new
    connections over its lifetime and each must carry a current token.

    Anti-coincidental-pass: a class that fetched once and cached the token in the
    conninfo would show a single get_token call after two connects.
    """
    _patch_base_connect(monkeypatch)
    cred = _FakeCredential(token="tok-xyz")
    conn_cls = make_token_auth_connection_class(cred)

    await conn_cls.connect("dbname=sage")
    await conn_cls.connect("dbname=sage")

    assert len(cred.calls) == 2


async def test_pgmi_003_token_failure_fails_closed(monkeypatch):
    """A token-acquisition error fails closed as a RuntimeError naming the scope,
    chaining the azure error, and never attempting an unauthenticated connect."""
    from azure.core.exceptions import ClientAuthenticationError

    captured = _patch_base_connect(monkeypatch)

    class _FailingCredential:
        async def get_token(self, *scopes, **kwargs):
            raise ClientAuthenticationError("no managed identity available")

    conn_cls = make_token_auth_connection_class(_FailingCredential())

    with pytest.raises(RuntimeError) as excinfo:
        await conn_cls.connect("dbname=sage")

    assert POSTGRES_AAD_SCOPE in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, ClientAuthenticationError)
    assert captured["calls"] == []
