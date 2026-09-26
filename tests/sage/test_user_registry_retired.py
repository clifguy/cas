"""SAGE carries one identity model: the request-derived principal (CAS-ADR-056).

The per-vault user registry, and the editor contract declared over it, are
retired. Nothing registers a user, stores one, or accepts a user id; write
attribution reads the validated credential and nothing else.

Anti-coincidental-pass discipline:

* The route check runs against a live application over a real vault, so a
  router still mounted answers 201 or 422 rather than 404.
* The contract checks read the served OpenAPI document and the published spec
  file, not the Python models, so a component left in either fails.
* The port check covers the interface and both implementations, so a method
  removed from one and left on another fails.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
import yaml

from sage.adapters.interfaces import GraphStore
from sage.adapters.stubs import StubGraphStore
from sage.app import create_app
from sage.config import SageCoreConfig
from sage.mcp_init import SAGEServices
from sage.storage.postgres.graph_store import PostgresGraphStore
from tests.helpers.write_attribution import VAULT, client, running_app

_SPEC = Path(__file__).resolve().parents[2] / "docs/fs/sage/sage_core_api.openapi.yaml"

_RETIRED_COMPONENTS = (
    "User",
    "UserType",
    "RegisterUserRequest",
    "SetEditorsRequest",
    "EditorList",
    "InvalidUserIdError",
    "InvalidUserIdErrorDetail",
)
_RETIRED_METHODS = ("insert_user", "get_user", "get_user_by_display_name", "list_users")


async def test_the_registration_route_is_gone(minimal_config) -> None:
    async with running_app(minimal_config, None) as app:
        async with client(app) as c:
            # Positive control: the vault is served, so a 404 below is the route.
            assert (await c.get(f"/sage_vaults/{VAULT}/documents")).status_code != 404
            resp = await c.post(
                f"/sage_vaults/{VAULT}/users",
                json={"display_name": "Alice", "user_type": "human"},
            )
    assert resp.status_code == 404, resp.text


@pytest.mark.parametrize("store", [GraphStore, StubGraphStore, PostgresGraphStore])
def test_the_graph_store_port_stores_no_users(store: type) -> None:
    assert [name for name in _RETIRED_METHODS if hasattr(store, name)] == []


def test_vault_services_carry_no_user_service() -> None:
    assert "user_service" not in {field.name for field in dataclasses.fields(SAGEServices)}


def _contract_documents() -> dict[str, dict]:
    served = create_app(stack_config=SageCoreConfig()).openapi()
    published = yaml.safe_load(_SPEC.read_text())
    return {"served": served, "published": published}


@pytest.mark.parametrize("which", ["served", "published"])
def test_the_contract_declares_no_registry_or_editors(which: str) -> None:
    document = _contract_documents()[which]
    schemas = document["components"]["schemas"]
    # Positive control: the lookup reads real component names.
    assert "Document" in schemas
    assert [name for name in _RETIRED_COMPONENTS if name in schemas] == []
    assert [path for path in document["paths"] if path.endswith(("/users", "/editors"))] == []
    assert "invalid_user_id" not in yaml.safe_dump(document)
