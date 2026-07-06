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

import hashlib
import time
from collections.abc import Callable, Iterator

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

# Read size for streamed source hashing, so a large source is never loaded whole
# into memory to compute its digest. Matches the filesystem binding's chunk size.
_HASH_CHUNK_BYTES = 65536

# Chunk size for streamed source delivery, matching the filesystem binding's
# delivery chunk size so both bindings hand equal-bounded chunks upward.
_SOURCE_CHUNK_BYTES = 65536


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

    def close(self) -> None:
        """Close the underlying HTTP client, releasing its connection pool.

        Long-lived server processes keep the client open; short-lived jobs (the
        cloud maintenance entrypoints) should close it at shutdown so its sockets
        are released deterministically rather than on garbage collection.
        """
        self._http.close()

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

    def _drive_content_url(self, *parts: str) -> str:
        """Content URL for a drive-root-relative path, NOT under the vault root.

        Unlike :meth:`_content_url` this does not prepend ``root_path``, so it
        addresses a sibling of the vault tree (e.g. a ``_teardown_archives`` folder)
        that vault discovery -- which enumerates only ``root_path``'s children --
        never sees. Still site/drive-scoped, so the site-scoped grant suffices.
        """
        rel = "/".join(p for p in parts if p)
        return f"{self._drive_root()}:/{rel}:/content"

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

    def delete_tree(self, vault_id: str) -> None:
        """Delete the vault's whole folder tree; a missing folder is tolerated.

        A ``DELETE`` on the vault folder removes it and everything under it -- the
        config and every retained source -- server-side (Graph cascades a folder
        delete), so the out-of-band teardown's source-tree removal is one
        site/drive-scoped request. Idempotent: a 404 (already gone) is tolerated;
        any other failure fails closed.
        """
        resp = self._request("DELETE", self._item_url(vault_id))
        if resp.status_code == 404:
            return
        if resp.status_code >= 400:
            self._fail(resp, "delete tree", vault_id)

    def list_sources(self, vault_id: str) -> list[dict]:
        """Enumerate the vault folder's files recursively (path + size).

        Best-effort snapshot manifest for the out-of-band teardown: walks the vault
        folder, recording each file's vault-relative path and byte size. An absent
        vault folder (or subfolder) yields no entries. Ordered by path so the
        manifest is deterministic.
        """
        entries: list[dict] = []

        def _walk(*parts: str) -> None:
            resp = self._request("GET", self._children_url(vault_id, *parts))
            if resp.status_code == 404:
                return
            if resp.status_code >= 400:
                self._fail(resp, "list sources", "/".join((vault_id, *parts)))
            for child in resp.json().get("value", []):
                child_parts = (*parts, child["name"])
                if "folder" in child:
                    _walk(*child_parts)
                else:
                    entries.append(
                        {"path": "/".join(child_parts), "size": int(child.get("size", 0))}
                    )

        _walk()
        return sorted(entries, key=lambda e: e["path"])

    def write_archive(self, archive_path: str, data: bytes) -> None:
        """Create or replace bytes at a drive-root-relative path (outside root_path).

        Used by the teardown snapshot to write a schema dump + manifest to a
        ``_teardown_archives/...`` folder that is a sibling of the vault tree, so the
        archive survives the vault-folder delete and is invisible to vault
        discovery. A direct create-or-replace upload; fails closed on a Graph error.
        """
        resp = self._request(
            "PUT",
            self._drive_content_url(*archive_path.split("/")),
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        if resp.status_code >= 400:
            self._fail(resp, "write archive", archive_path)

    # -- source-byte operations --------------------------------------------
    #
    # A retained source lives at ``<root>/<vault_id>/<vault-relative path>`` in
    # the same site/drive; the vault-relative path (e.g. ``imports/x.md``) is
    # split into Graph path segments. A direct create-or-replace upload and a
    # streamed read mirror the config surface's mechanics.

    def source_item(self, vault_id: str, source_path: str) -> dict | None:
        """Return a retained source's item metadata, or ``None`` when absent.

        A metadata read, not a content download, so ``source_exists`` and
        ``source_size`` resolve without pulling the bytes.
        """
        resp = self._request("GET", self._item_url(vault_id, *source_path.split("/")))
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            self._fail(resp, "stat source", f"{vault_id}/{source_path}")
        return resp.json()

    def read_source_bytes(self, vault_id: str, source_path: str) -> bytes:
        """Return a retained source's bytes (a content download)."""
        resp = self._request("GET", self._content_url(vault_id, *source_path.split("/")))
        if resp.status_code >= 400:
            self._fail(resp, "read source", f"{vault_id}/{source_path}")
        return resp.content

    def upload_source(self, vault_id: str, source_path: str, data: bytes) -> None:
        """Create or replace a retained source's bytes (a direct upload).

        The Graph item is replaced atomically server-side, so no temp-and-rename
        emulation is needed (SAGE is the sole writer). Large-source chunked upload
        sessions are deferred (CAS-ADR-043's thin-then-extended mechanics).
        """
        resp = self._request(
            "PUT",
            self._content_url(vault_id, *source_path.split("/")),
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        if resp.status_code >= 400:
            self._fail(resp, "write source", f"{vault_id}/{source_path}")

    def hash_source_bytes(self, vault_id: str, source_path: str) -> str:
        """Stream a retained source and return its canonical ``sha256:<hex>``.

        Streams the content so a large source is never loaded whole into memory
        to compute its digest, mirroring the filesystem binding's chunked hash.
        Carries the same single throttle/transient retry as the other ops.
        """
        url = self._content_url(vault_id, *source_path.split("/"))
        target = f"{vault_id}/{source_path}"
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self._token_provider()}"}
            with self._http.stream("GET", url, headers=headers) as resp:
                if resp.status_code in _RETRY_STATUSES and attempt == 0:
                    retry_after = resp.headers.get("Retry-After")
                    self._sleep(float(retry_after) if retry_after else 0.0)
                    continue
                if resp.status_code >= 400:
                    resp.read()
                    self._fail(resp, "hash source", target)
                digest = hashlib.sha256()
                for chunk in resp.iter_bytes(_HASH_CHUNK_BYTES):
                    digest.update(chunk)
                return f"sha256:{digest.hexdigest()}"
        # ``range(2)`` is non-empty, so the loop always returns on the second
        # attempt; this line is unreachable and only satisfies the type checker.
        raise RuntimeError("vault-source Graph hash retry loop produced no response")

    def stream_source_bytes(self, vault_id: str, source_path: str) -> Iterator[bytes]:
        """Yield a retained source's bytes in bounded chunks (a streamed download).

        The delivery counterpart of ``hash_source_bytes``'s streamed read: the
        content is never loaded whole into memory. Carries the same single
        throttle/transient retry, applied only before the first chunk has
        flowed -- once bytes have been yielded a retry would corrupt the
        delivery. Closing the iterator early unwinds the streaming context and
        releases the response.
        """
        url = self._content_url(vault_id, *source_path.split("/"))
        target = f"{vault_id}/{source_path}"
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self._token_provider()}"}
            with self._http.stream("GET", url, headers=headers) as resp:
                if resp.status_code in _RETRY_STATUSES and attempt == 0:
                    retry_after = resp.headers.get("Retry-After")
                    self._sleep(float(retry_after) if retry_after else 0.0)
                    continue
                if resp.status_code >= 400:
                    resp.read()
                    self._fail(resp, "stream source", target)
                yield from resp.iter_bytes(_SOURCE_CHUNK_BYTES)
                return

    def source_download_url(self, vault_id: str, source_path: str) -> str | None:
        """Return a retained source's short-lived pre-authenticated download URL.

        Reads the driveItem's ``@microsoft.graph.downloadUrl`` annotation -- a
        pre-authenticated, time-limited URL Graph returns on an item metadata GET,
        fetchable without an ``Authorization`` header. Returns ``None`` when the
        source is absent (no item), so a caller can distinguish "not retained" from
        a real URL. A metadata read, not a content download: the bytes are never
        pulled through this process.
        """
        item = self.source_item(vault_id, source_path)
        if item is None:
            return None
        return item.get("@microsoft.graph.downloadUrl")


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

    # follow_redirects is required, not cosmetic: Graph answers a content GET on a
    # '.../:/content' endpoint with a 302 to a short-lived download URL, so a client
    # that did not follow redirects would read the empty-bodied 302 for every config
    # and source download.
    return SharePointGraphClient(
        site_id=config.site_id,
        drive_id=config.drive_id,
        root_path=config.root_path,
        token_provider=token_provider,
        http_client=httpx.Client(timeout=30.0, follow_redirects=True),
    )
