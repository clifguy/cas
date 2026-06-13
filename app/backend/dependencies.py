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
from sage.api.errors import SAGEError, VaultNotFoundError

if TYPE_CHECKING:
    from sage.mcp_init import SAGEServices


def _get_services(request: Request, vault_id: str) -> SAGEServices:
    """Look up ``SAGEServices`` for a vault_id, raising
    ``VaultNotFoundError`` on miss. Mirrors
    ``sage.api.dependencies._get_services`` and is kept private to this
    module per ``out of scope: changing the function itself``.

    When no vault registry is present -- the standalone deployment, where SAGE
    runs as a separate process and is reached over HTTP -- directory scan and
    path-based ingest have no in-process pipeline to drive. They walk and read a
    local filesystem the standalone process does not share with SAGE, so the
    factories report this as a co-located-profile capability (``501``) rather
    than failing opaquely on the absent registry. Cloud bulk-ingest is delivered
    by a different route: the SPA uploads file content to the SAGE batch-ingest
    endpoint (reached through the reverse proxy), which runs the pipeline
    server-side where the graph and content handles live (CAS-ADR-042)."""

    registry: dict[str, SAGEServices] | None = getattr(request.app.state, "vault_registry", None)
    if registry is None:
        raise SAGEError(
            "local_profile_only",
            "Directory scan and path-based bulk ingestion are a co-located-profile "
            "capability: they walk a local filesystem this standalone deployment "
            "does not share with SAGE. In the hosted profile, upload file content "
            "to the SAGE batch-ingest endpoint (POST "
            "/sage_vaults/{vault_id}/documents:batch) instead.",
            501,
        )
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
