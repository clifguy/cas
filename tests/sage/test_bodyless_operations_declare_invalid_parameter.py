"""No operation publishes FastAPI's default 422, and every reachable 422 is declared.

FastAPI attaches its own ``422 -> HTTPValidationError`` response to every
operation that binds a parameter and declares no 422 of its own. The server
never sends that body: a request-validation failure is translated into the
SAGE envelope, as ``invalid_parameter`` at 422 when no more specific code
reports it. The sibling ``test_body_operations_declare_invalid_parameter.py``
holds operations that bind a body to the declared refusal. This module covers
the rest, which fall into two classes:

* **Fallible.** A query, header or cookie value can fail validation -- it is
  required, or it is typed as something other than a string -- so the
  operation can answer ``invalid_parameter`` at 422 and must declare it.
* **Phantom.** Every bound value is a path segment, refused at 400 by its
  typed alias, or a string with a default. No 422 is reachable, and the
  published document must not claim one.

The classifier below decides which class a route is in. Its reach is pinned
both ways: against the live route set, and against a synthetic app it must
flag. Wire probes show the fallible class answering 422, with a phantom
operation answering 400 as the paired control.
"""

from __future__ import annotations

import asyncio
import re
import types
import typing
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import yaml
from fastapi import Cookie, FastAPI, Header
from fastapi.dependencies.utils import get_flat_dependant
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from sage.api.response_docs import INVALID_PARAMETER_422_SENTENCE
from sage.app import create_app

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CORE_SPEC = _REPO_ROOT / "docs" / "fs" / "sage" / "sage_core_api.openapi.yaml"
_ERROR_RESPONSE_REF = "#/components/schemas/ErrorResponse"
_FRAMEWORK_SCHEMAS = ("HTTPValidationError", "ValidationError")

#: The body-less operations with a value that can fail validation. A route
#: added later with such a value joins this set knowingly.
FALLIBLE_OPERATIONS = frozenset(
    {"get_default_vault_config", "get_document", "delete_edge", "list_pending_metadata"}
)

VS = "/sage_vaults/test_vault"
DOC_ID = "00000000_absent_document"
EDGE_ID = "00000000-0000-0000-0000-000000000000"


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


_SENTENCE = _normalize(INVALID_PARAMETER_422_SENTENCE)


def _base_annotation(annotation: object) -> object:
    """Strip ``Annotated`` metadata and a single ``None`` arm from an annotation."""
    if typing.get_origin(annotation) is typing.Annotated:
        return _base_annotation(typing.get_args(annotation)[0])
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        arms = [arm for arm in typing.get_args(annotation) if arm is not type(None)]
        if len(arms) == 1:
            return _base_annotation(arms[0])
    return annotation


def _fallible_parameters(route: APIRoute) -> list[str]:
    """Names of the non-path values on ``route`` that can fail validation."""
    dependant = get_flat_dependant(route.dependant)
    return sorted(
        param.name
        for param in (dependant.query_params + dependant.header_params + dependant.cookie_params)
        if param.field_info.is_required()
        or _base_annotation(param.field_info.annotation) is not str
    )


def _bodyless_routes(app: FastAPI) -> list[APIRoute]:
    return [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.include_in_schema and route.body_field is None
    ]


def _declares_invalid_parameter(operation: dict) -> bool:
    response = operation.get("responses", {}).get("422", {})
    schema_ref = (
        response.get("content", {}).get("application/json", {}).get("schema", {}).get("$ref")
    )
    return schema_ref == _ERROR_RESPONSE_REF and _SENTENCE in _normalize(
        response.get("description", "")
    )


def _undeclared_fallible(app: FastAPI) -> list[str]:
    paths = app.openapi()["paths"]
    undeclared = []
    for route in _bodyless_routes(app):
        if not _fallible_parameters(route):
            continue
        for method in route.methods:
            operation = paths[route.path_format][method.lower()]
            if not _declares_invalid_parameter(operation):
                undeclared.append(operation["operationId"])
    return sorted(undeclared)


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return create_app()


def test_no_live_operation_publishes_default_422(app):
    document = app.openapi()
    published = sorted(
        operation["operationId"]
        for item in document["paths"].values()
        for operation in item.values()
        if isinstance(operation, dict)
        and "HTTPValidationError"
        in str(operation.get("responses", {}).get("422", {}).get("content", {}))
    )
    assert published == []
    schemas = document.get("components", {}).get("schemas", {})
    assert [name for name in _FRAMEWORK_SCHEMAS if name in schemas] == []


def test_every_fallible_route_declares_invalid_parameter(app):
    assert _undeclared_fallible(app) == []


def test_fallible_walk_reaches_known_operations(app):
    """The walk above would pass on an empty set; this pins its reach."""
    paths = app.openapi()["paths"]
    fallible = {
        paths[route.path_format][method.lower()]["operationId"]
        for route in _bodyless_routes(app)
        if _fallible_parameters(route)
        for method in route.methods
    }
    assert fallible == FALLIBLE_OPERATIONS


def test_classifier_flags_a_synthetic_undeclared_route():
    synthetic = FastAPI()

    @synthetic.get("/flagged", operation_id="flagged")
    async def flagged(flag: bool = False) -> dict:
        return {}

    @synthetic.get("/named", operation_id="named")
    async def named(name: str = "") -> dict:
        return {}

    @synthetic.get("/required", operation_id="required")
    async def required(name: str) -> dict:
        return {}

    # No live route binds a fallible header or cookie, so these two are the
    # only evidence that the walk reads beyond the query string.
    @synthetic.get("/header", operation_id="header")
    async def header(x_flag: bool = Header(default=False)) -> dict:
        return {}

    @synthetic.get("/cookie", operation_id="cookie")
    async def cookie(session: int = Cookie(default=0)) -> dict:
        return {}

    @synthetic.get("/token", operation_id="token")
    async def token(x_token: str = Header(default="")) -> dict:
        return {}

    assert _undeclared_fallible(synthetic) == ["cookie", "flagged", "header", "required"]


def test_spec_declares_invalid_parameter_on_fallible_operations():
    paths = yaml.safe_load(_CORE_SPEC.read_text())["paths"]
    declared = {
        operation["operationId"]
        for item in paths.values()
        for operation in item.values()
        if isinstance(operation, dict) and "requestBody" not in operation
        if _declares_invalid_parameter(operation)
    }
    assert FALLIBLE_OPERATIONS <= declared, sorted(FALLIBLE_OPERATIONS - declared)


@pytest.fixture
async def client(app_with_one_vault) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app_with_one_vault)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await asyncio.sleep(0.2)


@pytest.mark.parametrize(
    ("method", "url", "parameter"),
    [
        ("get", "/sage_vaults/default-config", "vault_id"),
        ("get", f"{VS}/documents/{DOC_ID}?include_content=maybe", "include_content"),
        ("delete", f"{VS}/edges/{EDGE_ID}?dry_run=maybe", "dry_run"),
        ("get", f"{VS}/pending-metadata?limit=1000", "limit"),
    ],
)
async def test_fallible_operation_answers_invalid_parameter(client, method, url, parameter):
    resp = await client.request(method.upper(), url)
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "invalid_parameter"
    assert body["detail"]["parameter"] == parameter


async def test_phantom_operation_answers_400_not_422(client):
    resp = await client.get(f"{VS}/preconditions/not!valid")
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "invalid_document_id"
