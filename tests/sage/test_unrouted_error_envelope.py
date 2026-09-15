"""Errors raised outside any operation reach the caller in the application envelope.

The Core API contract promises application errors as ``{code, message, detail}``
and tells a caller to branch on ``code``. Three failures are raised by the
framework rather than by an operation, and each carried the framework's own body
instead: a path no operation serves (404), a method the path does not accept
(405), and a response the server built that its declared model refuses (500).
A client that branches on ``code`` meets those bodies as unparseable.

Invariants
----------

U1  A path no operation serves answers 404 with ``code`` ``route_not_found``.
U2  A method the path does not accept answers 405 with ``code``
    ``method_not_allowed``, and keeps the ``Allow`` header naming what it does.
U3  A response its declared model refuses answers 500 with ``code``
    ``internal_error``, and the body carries nothing of the refused value.
U4  Both applications register the same handlers, so the backend-for-frontend
    answers an unserved path the same way.

Each body is validated against the published ``ErrorResponse`` component, so a
handler that answered with the right status and a body of its own shape fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from sage.api.errors import register_exception_handlers
from sage.api.wire_route import WireRoute
from tests.sage.test_wire_shape_conformance import SAGE_CORE_SPEC_PATH, _validator_for


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _assert_envelope(body: dict, code: str) -> None:
    spec = yaml.safe_load(SAGE_CORE_SPEC_PATH.read_text())
    _validator_for(spec, "ErrorResponse").validate(body)
    assert body["code"] == code, body


@pytest.fixture
def core_app() -> FastAPI:
    from sage.app import create_app

    return create_app()


async def test_unserved_path_answers_in_the_envelope(core_app: FastAPI):
    """U1 -- a 404 for a path no operation serves carries a code to branch on."""
    async with _client(core_app) as client:
        response = await client.get("/sage_vaults/no_such_vault/no-such-operation")

    assert response.status_code == 404
    _assert_envelope(response.json(), "route_not_found")


async def test_unaccepted_method_answers_in_the_envelope_and_names_the_allowed_ones(
    core_app: FastAPI,
):
    """U2 -- a 405 keeps the ``Allow`` header a handler rendering its own body can drop."""
    async with _client(core_app) as client:
        response = await client.delete("/sage_vaults")

    assert response.status_code == 405
    _assert_envelope(response.json(), "method_not_allowed")
    # The framework names the methods of the first route matching the path.
    allowed = {m.strip() for m in response.headers["allow"].split(",")}
    assert "GET" in allowed, response.headers["allow"]


async def test_refused_response_answers_internal_error_without_its_value():
    """U3 -- a response the declared model refuses is a server error in the envelope.

    The refused value is the server's own data, so none of it reaches the body:
    the sentinel below is checked absent from the raw text.
    """

    class Declared(BaseModel):
        count: int

    router = APIRouter(route_class=WireRoute)

    @router.get("/broken", response_model=Declared)
    async def broken():
        return {"count": "not-a-number-sentinel-7f3a"}

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)

    async with _client(app) as client:
        response = await client.get("/broken")

    assert response.status_code == 500
    _assert_envelope(response.json(), "internal_error")
    assert "sentinel-7f3a" not in response.text


async def test_backend_for_frontend_answers_an_unserved_path_in_the_envelope(tmp_path: Path):
    """U4 -- the standalone backend registers the same handlers as the Core API."""
    from app.backend.asgi import create_bff_app
    from sage.config import SageCoreConfig

    app = create_bff_app(spa_dir=tmp_path / "absent", stack_config=SageCoreConfig(profile="cloud"))
    async with _client(app) as client:
        response = await client.get("/app/no-such-operation")

    assert response.status_code == 404
    _assert_envelope(response.json(), "route_not_found")
