"""Shared harness for tests that exercise write attribution over real HTTP.

A stub token validator stands in for the identity provider, so a test names a
principal by a fake bearer token; the application runs with stub providers,
and MCP calls go through the mounted transport exactly as a client's would.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from sage.adapters.stubs import StubAbstractionProvider, StubContentStore, StubEmbeddingProvider
from sage.app import _initialize_services, create_app
from sage.auth import AuthenticatedPrincipal, AuthError, NoAuthValidator
from sage.config import SageCoreConfig, VaultConfig
from tests.helpers.pipeline_wait import drain_vaults

VAULT = "test_vault"


class StubValidator:
    """Maps fake bearer tokens to principals; anything else is refused."""

    def __init__(self, principals: Mapping[str, AuthenticatedPrincipal]) -> None:
        self._principals = principals

    async def validate(self, token: str | None) -> AuthenticatedPrincipal:
        if token in self._principals:
            return self._principals[token]
        raise AuthError(401, "invalid_token", "bad or missing token")


def install_stub(monkeypatch, principals: Mapping[str, AuthenticatedPrincipal]) -> None:
    """Resolve the stack's validator to a stub when auth is enabled."""

    def fake(auth_config):
        if auth_config is None or not auth_config.enabled:
            return NoAuthValidator()
        return StubValidator(principals)

    monkeypatch.setattr("sage.mcp_init.build_auth_validator", fake)


@asynccontextmanager
async def running_app(
    config: VaultConfig, stack_config: SageCoreConfig | None
) -> AsyncIterator[object]:
    """An application over stub providers, drained and closed on exit."""
    app = create_app(config=config, stack_config=stack_config)
    await _initialize_services(
        app,
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    )
    try:
        yield app
    finally:
        try:
            await drain_vaults(app.state.vault_registry, [VAULT])
        finally:
            registry = app.state.vault_registry
            if VAULT in registry:
                services = registry.pop(VAULT)
                services.close_timing()
                await services.close_storage()


@asynccontextmanager
async def mcp_running(app) -> AsyncIterator[None]:
    """Run the MCP session managers for the body of one test.

    Entered in the test's own task: a session manager's task group must be
    exited in the task that entered it, which an async fixture's teardown is
    not guaranteed to be.
    """
    async with AsyncExitStack() as stack:
        for server in app.state.mcp_mounts.values():
            await stack.enter_async_context(server.session_manager.run())
        yield


def client(app, token: str | None = None, user_agent: str | None = None) -> AsyncClient:
    """An HTTP client for ``app``, optionally carrying a bearer token and a User-Agent."""
    headers = {"Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if user_agent is not None:
        headers["User-Agent"] = user_agent
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=headers, timeout=30.0
    )


def seed(tmp_vault_dir: Path, name: str, body: str | None = None) -> str:
    """Write a markdown source into the vault's sources tree and return its name."""
    path = tmp_vault_dir / "sources" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body or f"# {name}\n\nBody of {name}.\n")
    return name


def decode_rpc_envelope(resp) -> dict:
    """The JSON-RPC envelope of an MCP response, JSON or event-stream framed."""
    assert resp.status_code == 200, resp.text
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        data = [line[5:].strip() for line in resp.text.splitlines() if line.startswith("data:")]
        return json.loads(data[-1])
    return resp.json()


def decode_rpc(resp) -> dict:
    """The tool result of a successful MCP response."""
    envelope = decode_rpc_envelope(resp)
    assert "error" not in envelope, envelope
    return json.loads(envelope["result"]["content"][0]["text"])


async def mcp_call(
    app,
    token: str | None,
    tool: str,
    arguments: dict,
    *,
    user_agent: str | None = None,
    path: str = "/mcp",
) -> dict:
    """Call ``tool`` over the mounted MCP transport and return its result."""
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    async with client(app, token, user_agent) as c:
        resp = await c.post(path, json=request)
    return decode_rpc(resp)
