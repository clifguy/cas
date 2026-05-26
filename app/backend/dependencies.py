"""FastAPI dependency factories for the CAS App backend.

These factories produce service instances for the ``/app/scan`` and
``/app/ingest`` route handlers. Both routes carry ``vault_id`` in the
request body rather than the URL path, so the body-scoped variant of
the SAGE ``Depends(get_vault_id)`` pattern applies here: each factory
declares the request body as a parameter, FastAPI shares the single
parsed body with the route handler, and the factory looks the vault
up in the registry before constructing the service.

The body-scoped pattern is slightly less elegant than SAGE's
path-scoped chain (e.g. ``sage/api/routers/graph_ops.py:link``), but
it satisfies the same one-line-handler invariant.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

from app.backend.ingest_streaming_service import IngestStreamingService
from app.backend.models import IngestRequest, ScanRequest
from app.backend.scan_service import ScanService
from sage.api.errors import VaultNotFoundError

if TYPE_CHECKING:
    from sage.mcp_init import SAGEServices


def _get_services(request: Request, vault_id: str) -> SAGEServices:
    """Look up ``SAGEServices`` for a vault_id, raising
    ``VaultNotFoundError`` on miss. Mirrors
    ``sage.api.dependencies._get_services`` and is kept private to this
    module per ``out of scope: changing the function itself``."""

    registry: dict[str, SAGEServices] = request.app.state.vault_registry
    if vault_id not in registry:
        raise VaultNotFoundError(vault_id)
    return registry[vault_id]


async def get_scan_service(
    body: ScanRequest,
    request: Request,
) -> ScanService:
    services = _get_services(request, body.vault_id)
    return ScanService(
        vault_config=services.config,
        graph_store=services.graph_store,
        ingestion_service=services.ingestion_service,
    )


async def get_ingest_streaming_service(
    body: IngestRequest,
    request: Request,
) -> IngestStreamingService:
    services = _get_services(request, body.vault_id)
    return IngestStreamingService(vault_services=services)
