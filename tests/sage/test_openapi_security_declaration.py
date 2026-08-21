"""The served schema document declares how a caller authenticates (CAS-ADR-042).

SAGE enforces bearer auth in a pure-ASGI middleware, which FastAPI's schema
generator cannot see: left alone, the generated document describes every
operation as unauthenticated. The document is published without a token so
each deployment describes itself, which makes that silence the one thing a
caller most needs filled in.

``build_openapi_document`` closes the gap, deriving the declaration from the
deployment's own auth configuration rather than from a fixed string, so a
deployment that accepts a different scope or role publishes that fact.
"""

from __future__ import annotations

import pytest

from sage.app import build_openapi_document, create_app
from sage.config import SageCoreConfig, StackAuthConfig

_AUTHORITY_ENDPOINTS = (
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-authorization-server",
)


def _enabled(**overrides) -> StackAuthConfig:
    base = {"enabled": True, "tenant_id": "tid", "audience": "api://sage"}
    return StackAuthConfig(**{**base, **overrides})


def _base_document() -> dict:
    """A minimal generated document, standing in for FastAPI's own output."""
    return {
        "openapi": "3.1.0",
        "info": {"title": "SAGE Core API", "version": "9.9.9"},
        "paths": {"/sage_vaults": {"get": {"operationId": "list_vaults"}}},
        "components": {"schemas": {"VaultSummary": {"type": "object"}}},
    }


def _scheme(document: dict) -> dict:
    return document["components"]["securitySchemes"]["entraBearer"]


# ---------------------------------------------------------------------------
# The declaration is present exactly when the deployment authenticates
# ---------------------------------------------------------------------------


def test_declares_bearer_scheme_when_auth_enabled() -> None:
    """An authenticating deployment publishes the bearer scheme it enforces."""
    document = build_openapi_document(_base_document(), _enabled())

    scheme = _scheme(document)
    assert scheme["type"] == "http"
    assert scheme["scheme"] == "bearer"
    assert scheme["bearerFormat"] == "JWT"
    assert document["security"] == [{"entraBearer": []}]


@pytest.mark.parametrize(
    "auth",
    [None, StackAuthConfig(enabled=False)],
    ids=["absent", "disabled"],
)
def test_declares_no_security_when_auth_disabled(auth: StackAuthConfig | None) -> None:
    """A deployment that authenticates no one must not claim otherwise.

    The anti-coincidental control on the test above: an unconditional
    injection would satisfy that one and fail this.
    """
    document = build_openapi_document(_base_document(), auth)

    assert "security" not in document
    assert "securitySchemes" not in document.get("components", {})


# ---------------------------------------------------------------------------
# The declaration is derived from the deployment, not hardcoded
# ---------------------------------------------------------------------------


def test_bearer_description_names_the_deployment_scope_and_role() -> None:
    """The accepted scope and role are read off the live configuration.

    Deliberately non-default values: a description that hardcodes the shipped
    defaults would pass a default-configuration assertion and fail here.
    """
    document = build_openapi_document(
        _base_document(),
        _enabled(required_scopes=["Custom.Scope"], required_roles=["Custom.Role"]),
    )

    description = _scheme(document)["description"]
    assert "Custom.Scope" in description
    assert "Custom.Role" in description
    assert "Sage.Access" not in description
    assert "Sage.Reader" not in description


def test_bearer_description_points_at_the_discovery_documents() -> None:
    """The description routes a caller to the unauthenticated discovery docs.

    The tenant, token endpoint, and scope prefix all vary per deployment, so
    the document names where to resolve them rather than pinning them.
    """
    description = _scheme(build_openapi_document(_base_document(), _enabled()))["description"]

    for endpoint in _AUTHORITY_ENDPOINTS:
        assert endpoint in description, f"the scheme description must name {endpoint}"


def test_enrichment_preserves_the_generated_document() -> None:
    """The declaration is added to the generated document, not substituted for it."""
    document = build_openapi_document(_base_document(), _enabled())

    assert document["paths"]["/sage_vaults"]["get"]["operationId"] == "list_vaults"
    assert document["components"]["schemas"]["VaultSummary"] == {"type": "object"}
    assert document["info"]["title"] == "SAGE Core API"


def test_enrichment_does_not_mutate_the_generated_document() -> None:
    """The caller's document is left untouched, so a cached base cannot drift."""
    base = _base_document()
    build_openapi_document(base, _enabled())

    assert "security" not in base
    assert "securitySchemes" not in base["components"]


# ---------------------------------------------------------------------------
# The document a deployment actually serves
# ---------------------------------------------------------------------------


def test_served_document_declares_the_scheme_end_to_end() -> None:
    """create_app wires the enrichment onto the app's own schema document."""
    app = create_app(
        stack_config=SageCoreConfig(
            auth=StackAuthConfig(enabled=True, tenant_id="tid", audience="api://sage")
        )
    )

    document = app.openapi()

    assert document["components"]["securitySchemes"]["entraBearer"]["scheme"] == "bearer"
    assert document["security"] == [{"entraBearer": []}]
    assert document["paths"], "the generated route surface must survive the enrichment"


def test_served_document_pins_no_absolute_server_host() -> None:
    """No server entry hardcodes a host, so paths resolve against the fetch origin.

    This is what lets one image describe every deployment: a caller that
    fetches the document from an edge resolves operations against that same
    edge. An absolute URL here would pin every generated client to whichever
    host happened to be baked in.
    """
    document = create_app(stack_config=SageCoreConfig()).openapi()

    for server in document.get("servers", []):
        url = server.get("url", "")
        assert not url.startswith(("http://", "https://")), (
            f"the served document must not pin an absolute host, found {url!r}"
        )
