"""Every Core API operation refuses names it does not declare, by construction.

``test_rest_unknown_parameter.py`` shows the refusal on a representative
operation per router. This module is what makes "every" true: it walks the
routes the app actually serves and the published specification, so an
operation added later without the refusal fails here rather than silently
tolerating whatever a caller sends.

Four properties, each with no exemption list:

* every Core API route carries the request-name refusal;
* every model a Core API route binds a request body to -- and every model
  nested inside one -- forbids undeclared fields;
* the published request schemas close exactly where the models do, in both
  directions, so the specification neither promises strictness the server
  lacks nor hides strictness it has;
* every Core API operation declares the refusal among its 400 responses.

The CAS Application API (``/app/*``) is a separate published contract, walked
against its own specification and models by the same four properties. Its
sign-in routes under ``/app/auth`` are held to the opposite: an identity
provider's callback carries query parameters no operation declares, so none of
them carries the refusal.
"""

from __future__ import annotations

import re
import types
import typing
from pathlib import Path

import yaml
from fastapi.dependencies.utils import get_flat_dependant
from fastapi.routing import APIRoute
from pydantic import BaseModel

from sage.app import create_app
from sage.models import schemas

_REPO_ROOT = Path(__file__).resolve().parents[2]
SAGE_CORE_SPEC_PATH = _REPO_ROOT / "docs" / "fs" / "sage" / "sage_core_api.openapi.yaml"
CAS_APP_SPEC_PATH = _REPO_ROOT / "docs" / "fs" / "cas_app_api.openapi.yaml"
_APP_AUTH_PREFIX = "/app/auth"

# A multipart operation binds its JSON envelope as a string form field, so the
# model it is parsed into is invisible to the route signature and is named here.
_FORM_ENCODED_BODY_MODELS: dict[tuple[str, str], type[BaseModel]] = {
    ("POST", "/sage_vaults/{vault_id}/documents:batch"): schemas.BatchIngestUploadMetadata,
}


def _spec() -> dict:
    return yaml.safe_load(SAGE_CORE_SPEC_PATH.read_text())


def _core_routes() -> list[APIRoute]:
    """The Core API routes the app serves: documented and not under ``/app``."""
    app = create_app()
    return [
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.include_in_schema
        and not route.path.startswith("/app")
    ]


def _models_in(annotation: object) -> set[type[BaseModel]]:
    """Every BaseModel reachable through an annotation, including nested fields."""
    found: set[type[BaseModel]] = set()
    pending = [annotation]
    while pending:
        current = pending.pop()
        if isinstance(current, type) and issubclass(current, BaseModel):
            if current in found:
                continue
            found.add(current)
            pending.extend(field.annotation for field in current.model_fields.values())
            continue
        if isinstance(current, types.UnionType) or typing.get_origin(current) is not None:
            pending.extend(typing.get_args(current))
    return found


def _body_models() -> set[type[BaseModel]]:
    models: set[type[BaseModel]] = set()
    for route in _core_routes():
        for param in get_flat_dependant(route.dependant).body_params:
            models |= _models_in(param.field_info.annotation)
        for method in route.methods:
            form_model = _FORM_ENCODED_BODY_MODELS.get((method, route.path))
            if form_model is not None:
                models |= _models_in(form_model)
    return models


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def test_core_route_walk_is_not_empty():
    """The walks below would pass on an empty route set; this pins its size."""
    routes = _core_routes()
    paths = {route.path for route in routes}

    assert len(routes) >= 40, sorted(paths)
    assert "/sage_vaults/{vault_id}/discover" in paths
    assert "/upload" in paths
    assert not any(path.startswith("/app") for path in paths)


def test_every_core_route_refuses_undeclared_parameters():
    from sage.api.dependencies import refuse_undeclared_parameters

    missing = [
        f"{sorted(route.methods)} {route.path}"
        for route in _core_routes()
        if refuse_undeclared_parameters
        not in {dep.call for dep in get_flat_dependant(route.dependant).dependencies}
    ]

    assert missing == []


def test_request_name_refusal_flattens_each_route_once(monkeypatch):
    """The refusal walks a route's parameters once, however many requests arrive.

    The walk itself is counted, at the name the refusal calls. Asserting only
    that a cached value exists would pass against a refusal that stores the
    walk and repeats it on every request anyway.
    """
    from fastapi.testclient import TestClient

    import sage.api.dependencies as dependencies

    walks: list[object] = []
    real_walk = dependencies.get_flat_dependant

    def counting_walk(dependant, *args, **kwargs):
        walks.append(dependant)
        return real_walk(dependant, *args, **kwargs)

    monkeypatch.setattr(dependencies, "get_flat_dependant", counting_walk)
    client = TestClient(create_app())

    first = client.get("/sage_vaults", params={"bogus_q": "1"})
    second = client.get("/sage_vaults", params={"bogus_q": "2"})

    assert first.status_code == second.status_code == 400, (first.text, second.text)
    assert len(walks) == 1


def test_every_request_body_model_forbids_undeclared_fields():
    models = _body_models()
    names = {model.__name__ for model in models}

    assert {
        "IngestRequest",
        "BulkLifecycleItem",
        "BatchIngestFileMetadata",
        "BatchIngestParsedMetadata",
    } <= names
    tolerant = sorted(
        model.__name__ for model in models if model.model_config.get("extra") != "forbid"
    )
    assert tolerant == []


def test_request_schemas_close_where_the_models_do():
    components = _spec()["components"]["schemas"]
    models = _body_models()

    open_in_spec = sorted(
        model.__name__
        for model in models
        if components.get(model.__name__, {}).get("additionalProperties") is not False
    )
    assert open_in_spec == [], "spec leaves these request schemas open"

    tolerant_in_code = sorted(
        name
        for name, schema in components.items()
        if isinstance(schema, dict)
        and schema.get("additionalProperties") is False
        and isinstance(cls := getattr(schemas, name, None), type)
        and issubclass(cls, BaseModel)
        and cls.model_config.get("extra") != "forbid"
        and not (getattr(cls, "__pydantic_root_model__", False) and _root_refuses_unknown_keys(cls))
    )
    assert tolerant_in_code == [], "spec closes these schemas but the models accept extras"


def test_every_core_operation_declares_the_refusal():
    from sage.api.response_docs import REQUEST_400_SENTENCES

    sentence = _normalize(REQUEST_400_SENTENCES["unknown_parameter"])
    paths = _spec()["paths"]
    undeclared = []
    for route in _core_routes():
        for method in route.methods:
            operation = paths[route.path_format][method.lower()]
            description = operation.get("responses", {}).get("400", {}).get("description", "")
            if sentence not in _normalize(description):
                undeclared.append(operation["operationId"])

    assert sorted(undeclared) == []


# ---------------------------------------------------------------------------
# CAS Application API
# ---------------------------------------------------------------------------


def _app_spec() -> dict:
    return yaml.safe_load(CAS_APP_SPEC_PATH.read_text())


def _app_routes() -> list[APIRoute]:
    """Every route the app serves under ``/app``, documented or not."""
    return [
        route
        for route in create_app().routes
        if isinstance(route, APIRoute) and route.path.startswith("/app/")
    ]


def _app_backend_routes() -> list[APIRoute]:
    """The documented application operations: ``/app`` outside the sign-in routes."""
    return [
        route
        for route in _app_routes()
        if route.include_in_schema and not route.path.startswith(_APP_AUTH_PREFIX)
    ]


def _app_body_models() -> set[type[BaseModel]]:
    models: set[type[BaseModel]] = set()
    for route in _app_backend_routes():
        for param in get_flat_dependant(route.dependant).body_params:
            models |= _models_in(param.field_info.annotation)
    return models


def _carries_refusal(route: APIRoute) -> bool:
    from sage.api.dependencies import refuse_undeclared_parameters

    return refuse_undeclared_parameters in {
        dep.call for dep in get_flat_dependant(route.dependant).dependencies
    }


def test_app_route_walk_is_not_empty():
    """The walks below would pass on an empty route set; this pins both sets."""
    assert {route.path for route in _app_backend_routes()} == {"/app/scan", "/app/ingest"}
    assert "/app/auth/callback" in {route.path for route in _app_routes()}


def test_every_app_operation_refuses_undeclared_parameters():
    missing = [
        f"{sorted(route.methods)} {route.path}"
        for route in _app_backend_routes()
        if not _carries_refusal(route)
    ]

    assert missing == []


def test_no_sign_in_route_refuses_undeclared_parameters():
    carrying = [
        f"{sorted(route.methods)} {route.path}"
        for route in _app_routes()
        if route.path.startswith(_APP_AUTH_PREFIX) and _carries_refusal(route)
    ]

    assert carrying == []


def test_every_app_request_body_model_forbids_undeclared_fields():
    models = _app_body_models()
    names = {model.__name__ for model in models}

    assert {"ScanRequest", "IngestRequest", "IngestFileItem", "ParsedMetadata"} <= names
    tolerant = sorted(
        model.__name__ for model in models if model.model_config.get("extra") != "forbid"
    )
    assert tolerant == []


def test_app_request_schemas_close_where_the_models_do():
    from app.backend import models as app_models

    components = _app_spec()["components"]["schemas"]
    models = _app_body_models()

    open_in_spec = sorted(
        model.__name__
        for model in models
        if components.get(model.__name__, {}).get("additionalProperties") is not False
    )
    assert open_in_spec == [], "spec leaves these request schemas open"

    tolerant_in_code = sorted(
        name
        for name, schema in components.items()
        if isinstance(schema, dict)
        and schema.get("additionalProperties") is False
        and isinstance(cls := getattr(app_models, name, None), type)
        and issubclass(cls, BaseModel)
        and cls.model_config.get("extra") != "forbid"
        and not (getattr(cls, "__pydantic_root_model__", False) and _root_refuses_unknown_keys(cls))
    )
    assert tolerant_in_code == [], "spec closes these schemas but the models accept extras"


def test_every_app_operation_declares_the_refusal():
    from sage.api.response_docs import REQUEST_400_SENTENCES

    sentence = _normalize(REQUEST_400_SENTENCES["unknown_parameter"])
    paths = _app_spec()["paths"]
    undeclared = []
    for route in _app_backend_routes():
        for method in route.methods:
            operation = paths[route.path_format][method.lower()]
            description = operation.get("responses", {}).get("400", {}).get("description", "")
            if sentence not in _normalize(description):
                undeclared.append(operation["operationId"])

    assert sorted(undeclared) == []


def _root_refuses_unknown_keys(model: type[BaseModel]) -> bool:
    """Reject extras only after accepting each branch's otherwise identical input."""
    from pydantic import ValidationError

    from tests.sage.test_wire_shape_conformance import _error_schema_sentinel

    schema = model.model_json_schema()
    definitions = schema.get("$defs", {})

    def branches(node: dict) -> list[dict]:
        if "$ref" in node:
            return branches(definitions[node["$ref"].rsplit("/", 1)[-1]])
        for keyword in ("oneOf", "anyOf"):
            if keyword in node:
                return [branch for child in node[keyword] for branch in branches(child)]
        return [node]

    specimens = [_error_schema_sentinel(branch, definitions) for branch in branches(schema)]
    if not specimens:
        return False
    for specimen in specimens:
        if not isinstance(specimen, dict):
            return False
        try:
            model.model_validate(specimen)
        except ValidationError:
            return False  # Invalid setup is not evidence of unknown-key refusal.
        try:
            model.model_validate({**specimen, "fabricated_key": 1})
        except ValidationError:
            continue
        return False
    return True
