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
as the binding lands" clause: a streamed create-or-replace upload (SAGE is the
sole writer of the tree, so write contention is not a concern) that opens an upload
session for a large source, a single retry on a throttling / transient response,
and a flat folder enumeration; richer mechanics (change feeds, locks) are deferred
to the binding's later slices.

The ``azure-identity`` import is deferred to credential-build time so an on-box
local-profile process that never selects the document store never loads it.
"""

from __future__ import annotations

import contextlib
import hashlib
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NoReturn

import httpx

from sage.config import StackDocumentStoreConfig

# Microsoft Graph v1.0 service root. Not an environment URL: it is the fixed,
# worldwide Graph endpoint, identical across tenants.
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# The throttle/transient statuses that earn the one retry this slice lands. 429
# is Graph's explicit throttle; 503 is a transient backend signal. A single
# retry is the thin mechanic; a full backoff curve is deferred (CAS-ADR-043).
_RETRY_STATUSES = frozenset({429, 503})

# A refusal carrying one of these statuses is worth retrying later: the store
# did not decline the request on its merits, it declined to serve it now. 504
# joins the two retried statuses because it says the same thing one hop out; it
# earns no in-band retry of its own because a gateway timeout has already spent
# the caller's patience once.
_TRANSIENT_STATUSES = frozenset({429, 503, 504})

# The statuses that mean an upload session is no longer there to write to --
# the store expired it, or it was interrupted. Transient only at a fragment
# ``PUT``: the same codes against an item path say the item is missing or
# conflicted, which retrying does not fix.
_SESSION_GONE_STATUSES = frozenset({404, 409, 410})

# The fixed name of every vault's configuration declaration within its folder,
# identical to the filesystem binding's on-disk name.
_CONFIG_FILENAME = "vault_config.yaml"

# Read size for streamed source hashing, so a large source is never loaded whole
# into memory to compute its digest. Matches the filesystem binding's chunk size.
_HASH_CHUNK_BYTES = 65536

# Chunk size for streamed source delivery, matching the filesystem binding's
# delivery chunk size so both bindings hand equal-bounded chunks upward.
_SOURCE_CHUNK_BYTES = 65536

# Graph's simple upload (``PUT ...:/content``) accepts a file of up to 250 MB;
# anything larger needs an upload session. The session is opened well below
# that ceiling, at the 10 MiB from which Graph's documentation recommends a
# resumable transfer, so the single-request path never carries a large body.
# Both paths stream the file from disk; neither holds it whole.
_UPLOAD_SESSION_THRESHOLD_BYTES = 10 * 1024 * 1024

# A session fragment must be a multiple of 320 KiB and under 60 MiB per request
# (Graph's documented rules -- a fragment that is not a multiple fails only when
# the final range is committed, which no offline test can reproduce). 10 MiB is
# 32 quanta, the size the documentation recommends for a stable connection.
_UPLOAD_FRAGMENT_QUANTUM = 327_680
_UPLOAD_FRAGMENT_BYTES = 32 * _UPLOAD_FRAGMENT_QUANTUM


class SourceStoreRefusalError(Exception):
    """The document store refused an operation the binding asked of it.

    Raised in place of a bare ``RuntimeError`` so a refusal reaches the service
    layer as a fact with a shape -- which operation, against what target, under
    which status, and whether retrying is worth the caller's time -- rather
    than as a message to be parsed. Deliberately *not* a ``RuntimeError``
    subclass: this module sits below the API layer and may not import its error
    hierarchy, so the service boundary has to translate, and a base that an
    existing ``except RuntimeError`` would swallow would let that translation
    be skipped without anything noticing.

    ``str(exc)`` carries the store's own response body, which names the cause
    far more precisely than a fixed message here could. That text is for the
    log: it is the store's rather than SAGE's, and can carry tenant
    coordinates, so the translation composes its own public message instead of
    forwarding this one.
    """

    def __init__(
        self,
        message: str,
        *,
        operation: str,
        target: str,
        status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.target = target
        #: The store's HTTP status, or ``None`` where the refusal carried none
        #: -- a reply the store answered 2xx for but did not populate, or a
        #: session it drove outside the protocol.
        self.status = status
        #: Whether the same request, sent later, could succeed.
        self.retryable = retryable


def _file_range(source_file: Path, start: int, length: int) -> Iterator[bytes]:
    """Yield ``length`` bytes of ``source_file`` from ``start`` in bounded chunks.

    The body of every upload request, whether the whole file or one session
    fragment: a fresh generator per request, so a retried request re-reads its
    range from disk rather than replaying an exhausted iterator.
    """
    remaining = length
    with source_file.open("rb") as f:
        f.seek(start)
        while remaining > 0:
            chunk = f.read(min(_SOURCE_CHUNK_BYTES, remaining))
            if not chunk:
                return
            remaining -= len(chunk)
            yield chunk


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

    def _fail(
        self, resp: httpx.Response, op: str, target: str, *, in_session: bool = False
    ) -> NoReturn:
        """Refuse an operation the store answered with an error status.

        The single funnel every Graph call fails through, so the classification
        of a refusal as transient or not is made once rather than per call
        site. ``in_session`` marks a fragment ``PUT``, where the statuses that
        mean "this session is gone" are worth another attempt from a fresh
        session; against an item path the same statuses are not.
        """
        retryable = resp.status_code in _TRANSIENT_STATUSES or (
            in_session and resp.status_code in _SESSION_GONE_STATUSES
        )
        raise SourceStoreRefusalError(
            f"Microsoft Graph {op} for {target!r} on site "
            f"{self._site_id}/{self._drive_id} failed: {resp.status_code} {resp.text}",
            operation=op,
            target=target,
            status=resp.status_code,
            retryable=retryable,
        )

    def _refuse(self, detail: str, op: str, target: str) -> NoReturn:
        """Refuse an operation the store answered without an error status.

        The store returned success and then did not behave like it: a session
        reply carrying no ``uploadUrl``, a session committed at the wrong
        fragment. Never retryable -- repeating a request the store already
        answered 2xx for reproduces the same reply.
        """
        raise SourceStoreRefusalError(
            f"Microsoft Graph {op} for {target!r} on site "
            f"{self._site_id}/{self._drive_id} {detail}",
            operation=op,
            target=target,
            status=None,
            retryable=False,
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

    def upload_source(self, vault_id: str, source_path: str, source_file: Path) -> None:
        """Create or replace a retained source's bytes, streamed from ``source_file``.

        The file is never held whole. At or below the session threshold it is
        one ``PUT`` to the content endpoint with the file as a fixed-length
        streamed body (Graph's simple upload); above it, an upload session: the
        item is replaced through sequential fragment ``PUT``s to the session's
        ``uploadUrl``, each carrying its ``Content-Range`` and no bearer (the
        URL is pre-authenticated, and Graph refuses a bearer on it). A fragment
        the store refuses cancels the session and fails closed, so no
        half-uploaded temporary lingers until the store expires it. Either way
        the Graph item is replaced atomically server-side on completion, so no
        temp-and-rename emulation is needed (SAGE is the sole writer).
        """
        size = source_file.stat().st_size
        target = f"{vault_id}/{source_path}"
        if size > _UPLOAD_SESSION_THRESHOLD_BYTES:
            self._upload_through_session(vault_id, source_path, source_file, size)
            return
        # The retry is local rather than through ``_request`` because the body
        # is a generator: a retry must open the file again, not replay a stream
        # the first attempt already consumed. ``Content-Length`` is set
        # explicitly so the streamed body goes up fixed-length rather than
        # chunked, which the simple upload does not accept.
        url = self._content_url(vault_id, *source_path.split("/"))
        for attempt in range(2):
            headers = {
                "Authorization": f"Bearer {self._token_provider()}",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(size),
            }
            resp = self._http.request(
                "PUT", url, headers=headers, content=_file_range(source_file, 0, size)
            )
            if resp.status_code in _RETRY_STATUSES and attempt == 0:
                retry_after = resp.headers.get("Retry-After")
                self._sleep(float(retry_after) if retry_after else 0.0)
                continue
            if resp.status_code >= 400:
                self._fail(resp, "write source", target)
            return

    def _upload_through_session(
        self, vault_id: str, source_path: str, source_file: Path, size: int
    ) -> None:
        """Replace a retained source through an upload session, fragment by fragment."""
        target = f"{vault_id}/{source_path}"
        resp = self._request(
            "POST",
            f"{self._item_url(vault_id, *source_path.split('/'))}:/createUploadSession",
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
        )
        if resp.status_code >= 400:
            self._fail(resp, "write source", target)
        # A session the store created but did not address is a refusal, not a
        # crash: read the reply defensively rather than indexing it, so a 2xx
        # that carries no ``uploadUrl`` (or no JSON at all) reaches the caller
        # as the store's failure to open a session instead of a ``KeyError``
        # naming a dictionary key the caller has no way to interpret.
        try:
            upload_url = resp.json()["uploadUrl"]
        except (ValueError, KeyError, TypeError):
            self._refuse(
                "accepted the upload session request and returned no uploadUrl.",
                "write source",
                target,
            )
        try:
            for start in range(0, size, _UPLOAD_FRAGMENT_BYTES):
                end = min(start + _UPLOAD_FRAGMENT_BYTES, size)
                resp = self._put_fragment(upload_url, source_file, start, end, size)
                if resp.status_code >= 400:
                    self._fail(resp, "write source", target, in_session=True)
                # 202 means the store is waiting for more; 200/201 means it has
                # committed the item. Each is an error at the other position:
                # a commit before the last fragment means the store now holds
                # something that is not the file.
                completed = resp.status_code in (200, 201)
                if completed and end < size:
                    self._refuse(
                        f"completed the upload session early, after {end} of {size} bytes.",
                        "write source",
                        target,
                    )
                if not completed and end == size:
                    self._refuse(
                        f"did not complete the upload session after the final "
                        f"fragment ({size} bytes): {resp.status_code}.",
                        "write source",
                        target,
                    )
        except BaseException:
            self._cancel_upload_session(upload_url)
            raise

    def _put_fragment(
        self, upload_url: str, source_file: Path, start: int, end: int, size: int
    ) -> httpx.Response:
        """PUT bytes ``[start, end)`` of the file to the session, with one throttle retry.

        No bearer: the session URL is pre-authenticated and Graph answers an
        ``Authorization`` header on it with 401. The retry re-sends the same
        range from a fresh read of the file.
        """
        headers = {
            "Content-Length": str(end - start),
            "Content-Range": f"bytes {start}-{end - 1}/{size}",
        }
        for attempt in range(2):
            resp = self._http.request(
                "PUT",
                upload_url,
                headers=headers,
                content=_file_range(source_file, start, end - start),
            )
            if resp.status_code in _RETRY_STATUSES and attempt == 0:
                retry_after = resp.headers.get("Retry-After")
                self._sleep(float(retry_after) if retry_after else 0.0)
                continue
            return resp
        # ``range(2)`` is non-empty, so the loop always returns on the second
        # attempt; this line is unreachable and only satisfies the type checker.
        raise RuntimeError("vault-source Graph fragment retry loop produced no response")

    def _cancel_upload_session(self, upload_url: str) -> None:
        """Best-effort ``DELETE`` of an abandoned session.

        The store expires an abandoned session on its own, so a cancel that
        fails is not worth reporting -- and must not mask the error that
        prompted it.
        """
        with contextlib.suppress(httpx.HTTPError):
            self._http.request("DELETE", upload_url)

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
