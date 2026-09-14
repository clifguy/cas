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

The CAS Application API (``/app/*``) is a separate published contract and is
not walked here.
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


def test_request_name_refusal_flattens_each_route_once():
    """The refusal keeps a route's flattened parameters on the route it walked.

    Driven through a real request, so a refusal that walked the route afresh
    each time -- leaving the helper unused -- would leave nothing on the route.
    """
    from fastapi.testclient import TestClient

    from sage.api.dependencies import _FLAT_DEPENDANT_ATTR, _flat_dependant

    app = create_app()
    route = next(r for r in app.routes if isinstance(r, APIRoute) and r.path == "/sage_vaults")
    assert getattr(route, _FLAT_DEPENDANT_ATTR, None) is None

    resp = TestClient(app).get("/sage_vaults", params={"bogus_q": "1"})

    assert resp.status_code == 400, resp.text
    cached = getattr(route, _FLAT_DEPENDANT_ATTR, None)
    assert cached is not None
    assert _flat_dependant(route) is cached


def test_every_request_body_model_forbids_undeclared_fields():
    models = _body_models()
    names = {model.__name__ for model in models}

    assert {"IngestRequest", "BulkLifecycleItem", "BatchIngestFileMetadata"} <= names
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
