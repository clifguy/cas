"""Every operation that takes a request body declares its 422 ``invalid_parameter``.

A value in a request body that fails validation, with no more specific code to
report it, is refused with ``invalid_parameter`` at 422 on every operation that
binds a body. The refusal is a branch each such operation has always had, so a
generated client can handle it only if the published contract declares it. The
live probes in ``test_validation_envelope_parity.py`` (Core API) and
``tests/app/test_app_backend.py`` (CAS Application API) show the refusal on the
wire; this module makes "every" true, for the routes the app serves and for both
published specifications, so an operation added later cannot omit it.

FastAPI's own ``HTTPValidationError`` 422, which names a body shape the server
never sends, is dropped from the served document, so an operation that omits
the declaration publishes no 422 at all. The walks below assert the declared
schema and sentence, not merely the status.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from fastapi.routing import APIRoute

from sage.api.response_docs import INVALID_PARAMETER_422_SENTENCE
from sage.app import create_app

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SPECS = {
    "sage_core": _REPO_ROOT / "docs" / "fs" / "sage" / "sage_core_api.openapi.yaml",
    "cas_app": _REPO_ROOT / "docs" / "fs" / "cas_app_api.openapi.yaml",
}
_ERROR_RESPONSE_REF = "#/components/schemas/ErrorResponse"

#: Operations whose body is read as a raw byte stream rather than bound to a
#: model, so no body value is validated and ``invalid_parameter`` is
#: unreachable. Each carries the reason; ``test_raw_stream_exemption_is_live``
#: fails the entry once the operation binds a body.
RAW_STREAM_BODY_OPERATIONS: dict[tuple[str, str], str] = {
    ("/upload", "put"): (
        "transfer_upload streams the request body to storage unparsed; its only "
        "bound input is the transfer token header."
    ),
}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


_SENTENCE = _normalize(INVALID_PARAMETER_422_SENTENCE)


@pytest.fixture(scope="module")
def app():
    return create_app()


@pytest.fixture(scope="module")
def live_paths(app) -> dict:
    return app.openapi()["paths"]


def _body_routes(app, *, application: bool) -> list[APIRoute]:
    routes = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        if route.path.startswith("/app") != application:
            continue
        if route.body_field is None:
            continue
        routes.append(route)
    return routes


def _undeclared(routes: list[APIRoute], paths: dict) -> list[str]:
    undeclared = []
    for route in routes:
        for method in route.methods:
            operation = paths[route.path_format][method.lower()]
            response = operation.get("responses", {}).get("422", {})
            schema_ref = (
                response.get("content", {})
                .get("application/json", {})
                .get("schema", {})
                .get("$ref")
            )
            if schema_ref != _ERROR_RESPONSE_REF or _SENTENCE not in _normalize(
                response.get("description", "")
            ):
                undeclared.append(operation["operationId"])
    return sorted(undeclared)


def _operation_ids(routes: list[APIRoute], paths: dict) -> set[str]:
    return {
        paths[route.path_format][method.lower()]["operationId"]
        for route in routes
        for method in route.methods
    }


def test_body_walks_are_not_empty(app, live_paths):
    """The walks below would pass on an empty route set; this pins their reach."""
    core = _operation_ids(_body_routes(app, application=False), live_paths)
    application = {route.path for route in _body_routes(app, application=True)}

    assert len(core) >= 18, sorted(core)
    assert {"search", "update_lifecycles", "export_projection", "batch_ingest_documents"} <= core
    assert application == {"/app/scan", "/app/ingest"}


def test_every_core_body_route_declares_invalid_parameter(app, live_paths):
    assert _undeclared(_body_routes(app, application=False), live_paths) == []


def test_every_app_body_route_declares_invalid_parameter(app, live_paths):
    assert _undeclared(_body_routes(app, application=True), live_paths) == []


@pytest.mark.parametrize("spec_name", sorted(_SPECS))
def test_every_spec_body_operation_declares_invalid_parameter(spec_name):
    paths = yaml.safe_load(_SPECS[spec_name].read_text())["paths"]
    undeclared = []
    for path, item in paths.items():
        for method, operation in item.items():
            if not isinstance(operation, dict) or "requestBody" not in operation:
                continue
            if (path, method) in RAW_STREAM_BODY_OPERATIONS:
                continue
            response = operation.get("responses", {}).get("422", {})
            schema_ref = (
                response.get("content", {})
                .get("application/json", {})
                .get("schema", {})
                .get("$ref")
            )
            if schema_ref != _ERROR_RESPONSE_REF or _SENTENCE not in _normalize(
                response.get("description", "")
            ):
                undeclared.append(operation["operationId"])

    assert sorted(undeclared) == []


def test_raw_stream_exemption_is_live(app):
    """An exempt operation still publishes a body and still binds none."""
    spec_paths = yaml.safe_load(_SPECS["sage_core"].read_text())["paths"]
    routes = {
        (route.path_format, method.lower()): route
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    for (path, method), reason in RAW_STREAM_BODY_OPERATIONS.items():
        assert reason.strip(), (path, method)
        assert "requestBody" in spec_paths[path][method], f"{method} {path} publishes no body"
        route = routes[(path, method)]
        assert route.body_field is None, f"{method} {path} now binds a body; drop its exemption"
