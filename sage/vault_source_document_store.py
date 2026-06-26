"""Microsoft Graph / SharePoint adapter for the document-store vault-source binding (CAS-ADR-043).

The cloud vault-source binding persists each vault's configuration declaration to
a Microsoft 365 SharePoint document library, reached over the Microsoft Graph API
under the workload's managed identity. This module owns the two cloud-only
dependencies that adapter needs -- the ``azure-identity`` credential and the raw
Graph REST calls over ``httpx`` -- so the port module (``sage.vault_source_binding``)
stays free of any Azure import and the import-boundary guardrails can confine the
Azure SDK to this module and the other cloud-owner leaves.

The access model is least-privilege: every request addresses the single
configured site and drive (``.../sites/{site}/drives/{drive}/root:/...``), never a
tenant-wide path, matching the site-scoped application permission CAS-ADR-043
mandates. Adapter mechanics land thin per that ADR's "established thin and extended
as the binding lands" clause: a direct create-or-replace upload (SAGE is the sole
writer of the tree, so write contention is not a concern), a single retry on a
throttling / transient response, and a flat folder enumeration; richer mechanics
(upload-session atomic-write emulation, change feeds, locks) are deferred to the
binding's later slices.

The ``azure-identity`` import is deferred to credential-build time so an on-box
local-profile process that never selects the document store never loads it.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from sage.config import StackDocumentStoreConfig

# Microsoft Graph v1.0 service root. Not an environment URL: it is the fixed,
# worldwide Graph endpoint, identical across tenants.
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# The throttle/transient statuses that earn the one retry this slice lands. 429
# is Graph's explicit throttle; 503 is a transient backend signal. A single
# retry is the thin mechanic; a full backoff curve is deferred (CAS-ADR-043).
_RETRY_STATUSES = frozenset({429, 503})

# The fixed name of every vault's configuration declaration within its folder,
# identical to the filesystem binding's on-disk name.
_CONFIG_FILENAME = "vault_config.yaml"


class SharePointGraphClient:
    """Thin Microsoft Graph client for one SharePoint drive (CAS-ADR-043).

    Addresses a single site/drive under a managed-identity bearer token and
    carries only the four operations the vault-source binding's config surface
    needs: enumerate vault folders, read a vault's config bytes, create-or-replace
    them, and delete them. Every path is rooted at the configured site, drive, and
    root folder, so the site-scoped application permission is sufficient and no
    tenant-wide path is ever addressed.
    """

    def __init__(
        self,
        *,
        site_id: str | None,
        drive_id: str | None,
        root_path: str,
        token_provider: Callable[[], str],
        http_client: httpx.Client,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._site_id = site_id
        self._drive_id = drive_id
        self._root_path = root_path.strip("/")
        self._token_provider = token_provider
        self._http = http_client
        self._sleep = sleep

    # -- path construction (always site/drive-scoped) ----------------------

    def _drive_root(self) -> str:
        if not self._site_id or not self._drive_id:
            raise RuntimeError(
                "the document-store vault-source binding requires document_store.site_id "
                "and document_store.drive_id to be configured; one or both are unset."
            )
        return f"{_GRAPH_BASE}/sites/{self._site_id}/drives/{self._drive_id}/root"

    def _rel(self, *parts: str) -> str:
        """Join the configured root with ``parts`` into a drive-relative path."""
        return "/".join(p for p in (self._root_path, *parts) if p)

    def _children_url(self, *parts: str) -> str:
        rel = self._rel(*parts)
        return f"{self._drive_root()}:/{rel}:/children" if rel else f"{self._drive_root()}/children"

    def _item_url(self, *parts: str) -> str:
        return f"{self._drive_root()}:/{self._rel(*parts)}"

    def _content_url(self, *parts: str) -> str:
        return f"{self._item_url(*parts)}:/content"

    # -- transport ---------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        """Issue a Graph request with a fresh bearer token and one throttle retry."""
        extra_headers = kwargs.pop("headers", {})
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self._token_provider()}", **extra_headers}  # type: ignore[dict-item]
            resp = self._http.request(method, url, headers=headers, **kwargs)  # type: ignore[arg-type]
            if resp.status_code in _RETRY_STATUSES and attempt == 0:
                retry_after = resp.headers.get("Retry-After")
                self._sleep(float(retry_after) if retry_after else 0.0)
                continue
            return resp
        # ``range(2)`` is non-empty, so the loop always returns on the second
        # attempt; this line is unreachable and only satisfies the type checker.
        raise RuntimeError("vault-source Graph retry loop produced no response")

    def _fail(self, resp: httpx.Response, op: str, target: str) -> None:
        raise RuntimeError(
            f"Microsoft Graph {op} for {target!r} on site "
            f"{self._site_id}/{self._drive_id} failed: {resp.status_code} {resp.text}"
        )

    # -- operations --------------------------------------------------------

    def list_vault_ids(self) -> list[str]:
        """Return the sorted ids of vault folders under the root that hold a config.

        A folder under the root is a vault only if it carries a
        ``vault_config.yaml``; a stray folder is skipped. Returns an empty list
        when the root folder itself is absent (a freshly provisioned, empty store).
        """
        resp = self._request("GET", self._children_url())
        if resp.status_code == 404:
            return []
        if resp.status_code >= 400:
            self._fail(resp, "list children", self._rel() or "<root>")
        names = [c["name"] for c in resp.json().get("value", []) if "folder" in c]
        return sorted(name for name in names if self._has_config(name))

    def _has_config(self, vault_id: str) -> bool:
        resp = self._request("GET", self._item_url(vault_id, _CONFIG_FILENAME))
        if resp.status_code == 404:
            return False
        if resp.status_code >= 400:
            self._fail(resp, "stat config", f"{vault_id}/{_CONFIG_FILENAME}")
        return True

    def read_config_bytes(self, vault_id: str) -> bytes | None:
        """Return the vault's config bytes, or ``None`` when it is absent."""
        resp = self._request("GET", self._content_url(vault_id, _CONFIG_FILENAME))
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            self._fail(resp, "read config", f"{vault_id}/{_CONFIG_FILENAME}")
        return resp.content

    def write_config_bytes(self, vault_id: str, data: bytes) -> None:
        """Create or replace the vault's config bytes (a direct upload)."""
        resp = self._request(
            "PUT",
            self._content_url(vault_id, _CONFIG_FILENAME),
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        if resp.status_code >= 400:
            self._fail(resp, "write config", f"{vault_id}/{_CONFIG_FILENAME}")

    def delete_config(self, vault_id: str) -> None:
        """Delete the vault's config item; a missing item is tolerated (idempotent)."""
        resp = self._request("DELETE", self._item_url(vault_id, _CONFIG_FILENAME))
        if resp.status_code == 404:
            return
        if resp.status_code >= 400:
            self._fail(resp, "delete config", f"{vault_id}/{_CONFIG_FILENAME}")


def build_sharepoint_graph_client(
    config: StackDocumentStoreConfig,
    *,
    managed_identity: bool = True,
) -> SharePointGraphClient:
    """Build the Graph client over the workload's managed-identity credential.

    Defers the ``azure-identity`` import to call time so a process that never
    selects the document store never loads the Azure SDK. ``DefaultAzureCredential``
    resolves the user-assigned managed identity from ``AZURE_CLIENT_ID`` in the
    container environment; the same credential resolves a developer identity (az
    login / environment) when the document store is exercised from the local
    profile (CAS-ADR-043's co-variation test). ``managed_identity`` is accepted
    for parity with the storage binding's selector; the credential resolution is
    identical either way, so it does not branch behavior.

    Construction performs no network call -- the bearer token is minted lazily on
    the first Graph request -- so the client builds offline; only an actual
    operation against an unreachable tenant fails.
    """
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential()
    scope = config.graph_scope

    def token_provider() -> str:
        return credential.get_token(scope).token

    return SharePointGraphClient(
        site_id=config.site_id,
        drive_id=config.drive_id,
        root_path=config.root_path,
        token_provider=token_provider,
        http_client=httpx.Client(timeout=30.0),
    )
