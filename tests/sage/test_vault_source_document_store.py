"""Tests for the cloud document-store vault-source binding (CAS-ADR-043).

Two layers are exercised:

* The binding (:class:`DocumentStoreVaultSourceStore`) against an in-memory fake
  Graph client, proving the config + discovery surface round-trips a vault's
  declaration without a filesystem path.
* The raw Graph client (:class:`SharePointGraphClient`) against an
  ``httpx.MockTransport``, proving it issues site/drive-scoped, authenticated
  requests, fails closed on Graph errors, tolerates a missing item on delete, and
  retries once on a throttling response.

No live tenant is touched: every Graph call is mocked, so the suite runs offline
and unconditionally. Test IDs follow VSB-DS-NNN (Vault-Source Binding, Document
Store).
"""

import copy
import hashlib
import subprocess
import sys
import textwrap
from pathlib import Path

import httpx
import pytest

from sage.config import StackDocumentStoreConfig, VaultConfig
from sage.vault_source_binding import DiscoveredVault, DocumentStoreVaultSourceStore
from sage.vault_source_document_store import (
    SharePointGraphClient,
    build_sharepoint_graph_client,
)

_SITE = "contoso.sharepoint.com,site-guid,web-guid"
_DRIVE = "b!drive-id"


# --------------------------------------------------------------------------
# Binding tests, against an in-memory fake Graph client
# --------------------------------------------------------------------------


class _FakeGraphClient:
    """In-memory stand-in for SharePointGraphClient: config bytes per vault_id
    and retained source bytes per vault-relative path."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.uploads = 0
        self.deletes = 0
        # Source-byte half: vault-relative path -> bytes, with op counters so a
        # test can prove a cheap stat did not pull content and a reuse did not
        # re-upload.
        self.sources: dict[str, bytes] = {}
        self.source_uploads = 0
        self.source_reads = 0
        self.source_stats = 0

    def list_vault_ids(self) -> list[str]:
        return sorted(self.store)

    def read_config_bytes(self, vault_id: str) -> bytes | None:
        return self.store.get(vault_id)

    def write_config_bytes(self, vault_id: str, data: bytes) -> None:
        self.uploads += 1
        self.store[vault_id] = data

    def delete_config(self, vault_id: str) -> None:
        self.deletes += 1
        self.store.pop(vault_id, None)

    # -- source-byte half --------------------------------------------------

    def source_item(self, vault_id: str, source_path: str) -> dict | None:
        self.source_stats += 1
        data = self.sources.get(source_path)
        if data is None:
            return None
        return {"name": source_path.rsplit("/", 1)[-1], "size": len(data)}

    def read_source_bytes(self, vault_id: str, source_path: str) -> bytes:
        self.source_reads += 1
        return self.sources[source_path]

    def upload_source(self, vault_id: str, source_path: str, data: bytes) -> None:
        self.source_uploads += 1
        self.sources[source_path] = data

    def hash_source_bytes(self, vault_id: str, source_path: str) -> str:
        return "sha256:" + hashlib.sha256(self.sources[source_path]).hexdigest()

    def source_download_url(self, vault_id: str, source_path: str) -> str | None:
        if source_path not in self.sources:
            return None
        return f"https://sp.example/download/{source_path}?t=fake"


def _binding(fake: _FakeGraphClient) -> DocumentStoreVaultSourceStore:
    return DocumentStoreVaultSourceStore(StackDocumentStoreConfig(), client=fake)


def test_vsb_ds_001_write_discover_load_round_trip(minimal_vault_config_dict):
    """``write_config`` persists each vault's declaration; ``discover`` returns a
    pathless ``DiscoveredVault`` per vault carrying its id; ``load_config``
    resolves an equal ``VaultConfig`` from that id.

    Anti-coincidental-pass: assert ``config_path is None`` *and* ``vault_id`` is
    populated *and* the loaded ``vault.name`` matches what was written — a binding
    that round-tripped nothing, lost the id, or returned a path would fail. This
    is exactly the startup lifespans' ``discover()`` -> ``load_config(discovered)``
    path, the headline acceptance criterion's real route.
    """
    fake = _FakeGraphClient()
    store = _binding(fake)
    for vid in ("vault_b", "vault_a"):
        cfg = copy.deepcopy(minimal_vault_config_dict)
        cfg["vault"]["id"] = vid
        cfg["vault"]["name"] = vid.replace("_", " ").title()
        store.write_config(vid, cfg)

    discovered = store.discover()
    assert [d.vault_id for d in discovered] == ["vault_a", "vault_b"]  # sorted
    assert all(d.config_path is None for d in discovered)

    loaded = {}
    for d in discovered:
        vc = store.load_config(d)
        assert isinstance(vc, VaultConfig)
        loaded[d.vault_id] = vc.vault.name
    assert loaded == {"vault_a": "Vault A", "vault_b": "Vault B"}


def test_vsb_ds_002_config_locator_returns_none():
    """The document-store binding has no filesystem path, so ``config_locator``
    returns ``None`` exactly.

    Anti-coincidental-pass: assert ``is None`` (not falsy) — a binding that
    returned a ``Path`` would silently break the lifespans' ``config_path``-None
    threading and the ``config_path is not None`` guards in the registry/config
    rollback paths.
    """
    store = _binding(_FakeGraphClient())
    assert store.config_locator("any_vault") is None


def test_vsb_ds_003_delete_removes_and_is_idempotent(minimal_vault_config_dict):
    """``delete_config`` removes the declaration so ``discover`` no longer lists
    it, and a second delete does not raise.

    Anti-coincidental-pass: assert the vault disappears from ``discover`` (not
    just that delete returned) and that the repeated delete is a no-op.
    """
    fake = _FakeGraphClient()
    store = _binding(fake)
    store.write_config("v", minimal_vault_config_dict)
    assert [d.vault_id for d in store.discover()] == ["v"]

    store.delete_config("v")
    assert store.discover() == []
    store.delete_config("v")  # idempotent: a second delete does not raise


def test_vsb_ds_004_load_config_requires_vault_id():
    """``load_config`` on a ``DiscoveredVault`` with no ``vault_id`` raises a
    clear ``ValueError`` rather than mis-resolving.

    Anti-coincidental-pass: match on ``vault_id`` in the message, so a failure for
    an unrelated reason would not pass. The document-store analog of the
    filesystem binding's pathless rejection (VSB-005).
    """
    store = _binding(_FakeGraphClient())
    with pytest.raises(ValueError, match="vault_id"):
        store.load_config(DiscoveredVault(config_path=None, vault_id=None))


def test_vsb_ds_004b_load_config_missing_item_fails_loud():
    """``load_config`` for a vault_id absent from the store raises
    ``FileNotFoundError`` rather than returning a degenerate config.

    Boundary: a vault deleted between ``discover`` and ``load`` (or a caller
    passing an unknown id) surfaces loudly instead of silently mis-resolving.
    """
    store = _binding(_FakeGraphClient())
    with pytest.raises(FileNotFoundError, match="ghost"):
        store.load_config(DiscoveredVault(config_path=None, vault_id="ghost"))


# --------------------------------------------------------------------------
# Binding source-byte tests, against the in-memory fake Graph client
# --------------------------------------------------------------------------


def test_vsb_ds_030_retain_external_uploads_and_returns_vault_relative(tmp_path):
    """``retain_source`` uploads an external file's bytes to ``imports/<name>``
    and returns that vault-relative path.

    Anti-coincidental-pass: assert exactly one upload occurred *and* the stored
    bytes match — a stub returning the path without uploading, or uploading
    elsewhere, would fail.
    """
    fake = _FakeGraphClient()
    store = _binding(fake)
    external = tmp_path / "report.md"
    external.write_bytes(b"# Report\n\nbody")

    rel = store.retain_source("v", tmp_path, external)

    assert rel == "imports/report.md"
    assert fake.source_uploads == 1
    assert fake.sources["imports/report.md"] == b"# Report\n\nbody"


def test_vsb_ds_031_retain_collision_identical_content_reuses(tmp_path):
    """A name collision whose content is identical reuses the existing path and
    does not re-upload.

    Boundary: dedup. Anti-coincidental-pass: assert the upload counter stays at
    zero — a binding that always re-uploaded would still return the right path
    but bump the counter.
    """
    fake = _FakeGraphClient()
    fake.sources["imports/x.md"] = b"same-bytes"
    store = _binding(fake)
    external = tmp_path / "x.md"
    external.write_bytes(b"same-bytes")

    rel = store.retain_source("v", tmp_path, external)

    assert rel == "imports/x.md"
    assert fake.source_uploads == 0  # reused, no upload


def test_vsb_ds_032_retain_collision_different_content_suffixes(tmp_path):
    """A name collision whose content differs uploads under a content-hash
    suffix and leaves the original untouched.

    Boundary: disambiguation. Anti-coincidental-pass: assert the returned path
    carries the 8-char hash suffix, the new bytes land there, and the original
    path's bytes are unchanged.
    """
    fake = _FakeGraphClient()
    fake.sources["imports/x.md"] = b"original"
    store = _binding(fake)
    external = tmp_path / "x.md"
    external.write_bytes(b"different")

    rel = store.retain_source("v", tmp_path, external)

    expected_suffix = hashlib.sha256(b"different").hexdigest()[:8]
    assert rel == f"imports/x_{expected_suffix}.md"
    assert fake.sources[rel] == b"different"
    assert fake.sources["imports/x.md"] == b"original"  # original untouched


def test_vsb_ds_033_source_exists_true_and_false():
    """``source_exists`` reports a retained source present and an absent one
    absent."""
    fake = _FakeGraphClient()
    fake.sources["imports/here.md"] = b"x"
    store = _binding(fake)

    assert store.source_exists("v", Path("/unused"), "imports/here.md") is True
    assert store.source_exists("v", Path("/unused"), "imports/gone.md") is False


def test_vsb_ds_034_source_size_is_a_cheap_stat():
    """``source_size`` returns the byte length via item metadata without pulling
    the content.

    Anti-coincidental-pass: assert no content read occurred — an implementation
    that downloaded the file to measure it would inflate ``source_reads``.
    """
    fake = _FakeGraphClient()
    fake.sources["imports/s.md"] = b"abcde"
    store = _binding(fake)

    assert store.source_size("v", Path("/unused"), "imports/s.md") == 5
    assert fake.source_reads == 0  # metadata only, no content download


def test_vsb_ds_035_read_source_round_trips_bytes():
    """``read_source`` returns the exact retained bytes, binary-safe."""
    fake = _FakeGraphClient()
    payload = b"\x00\x01binary\xffpayload"
    fake.sources["imports/b.bin"] = payload
    store = _binding(fake)

    assert store.read_source("v", Path("/unused"), "imports/b.bin") == payload


def test_vsb_ds_036_hash_source_canonical_form():
    """``hash_source`` returns the canonical ``sha256:<hex>`` of the retained
    bytes."""
    fake = _FakeGraphClient()
    payload = b"hash me"
    fake.sources["imports/h.md"] = payload
    store = _binding(fake)

    assert store.hash_source("v", Path("/unused"), "imports/h.md") == (
        "sha256:" + hashlib.sha256(payload).hexdigest()
    )


def test_vsb_ds_050_download_url_delegates_to_client(tmp_path):
    """``download_url`` returns the URL the Graph client mints for a retained
    source, so the browser fetches the bytes directly from the store.

    Anti-coincidental-pass: assert the exact URL the client would issue is
    returned -- a binding that ignored the client or returned a stub would not
    match. This is the document-store binding's half of the browser-delivery path
    (CAS-ADR-043).
    """
    fake = _FakeGraphClient()
    fake.sources["imports/x.pdf"] = b"%PDF-1.4"
    store = _binding(fake)

    assert store.download_url("v", tmp_path, "imports/x.pdf") == (
        "https://sp.example/download/imports/x.pdf?t=fake"
    )


def test_vsb_ds_051_download_url_none_when_source_absent(tmp_path):
    """``download_url`` returns ``None`` for a source not retained on the store, so
    the service can distinguish "not retained" from a real URL.

    Boundary: a document record whose bytes are missing from the store yields no
    URL rather than a bogus one.
    """
    store = _binding(_FakeGraphClient())  # nothing retained
    assert store.download_url("v", tmp_path, "imports/missing.pdf") is None


# --------------------------------------------------------------------------
# Graph client tests, against httpx.MockTransport
# --------------------------------------------------------------------------


def _client(handler, *, root_path: str = "vaults", sleep=lambda _s: None) -> SharePointGraphClient:
    transport = httpx.MockTransport(handler)
    return SharePointGraphClient(
        site_id=_SITE,
        drive_id=_DRIVE,
        root_path=root_path,
        token_provider=lambda: "tok",
        http_client=httpx.Client(transport=transport),
        sleep=sleep,
    )


def test_vsb_ds_010_client_issues_scoped_authenticated_requests():
    """Each operation issues the expected method against a site/drive-scoped path
    carrying a bearer token.

    Anti-coincidental-pass: assert the path is rooted at
    ``/sites/{site}/drives/{drive}/root`` (least privilege per CAS-ADR-043 §3),
    never a tenant-wide ``/drives`` path, and that every request carries
    ``Authorization: Bearer tok`` — a client that walked a tenant-wide path or
    dropped the token would still satisfy a permissive mock.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        url = str(request.url)
        if request.method == "GET" and url.endswith(":/children"):
            return httpx.Response(200, json={"value": [{"name": "vault_a", "folder": {}}]})
        if request.method == "GET" and url.endswith("vault_config.yaml:/content"):
            return httpx.Response(200, content=b"vault:\n  id: vault_a\n")
        if request.method == "GET" and url.endswith("vault_config.yaml"):
            return httpx.Response(200, json={"name": "vault_config.yaml"})
        if request.method == "PUT":
            return httpx.Response(201, json={})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(500, text="unexpected")

    client = _client(handler)

    assert client.list_vault_ids() == ["vault_a"]
    assert client.read_config_bytes("vault_a") == b"vault:\n  id: vault_a\n"
    client.write_config_bytes("vault_a", b"vault:\n  id: vault_a\n")
    client.delete_config("vault_a")

    scope = f"/sites/{_SITE}/drives/{_DRIVE}/root"
    methods = {r.method for r in seen}
    assert methods == {"GET", "PUT", "DELETE"}
    for r in seen:
        assert r.headers["authorization"] == "Bearer tok"
        assert scope in str(r.url)
        # No tenant-wide drive path leaks in.
        assert "/drives/" + _DRIVE in str(r.url)
        assert str(r.url).split("/drives/")[0].endswith(f"/sites/{_SITE}")

    # The PUT and the content read both target the file content endpoint.
    put = next(r for r in seen if r.method == "PUT")
    assert str(put.url).endswith("/vaults/vault_a/vault_config.yaml:/content")


def test_vsb_ds_005_list_vault_ids_skips_folders_without_config():
    """``list_vault_ids`` returns only folders that carry a ``vault_config.yaml``;
    a stray folder and a non-folder child are skipped.

    Anti-coincidental-pass: include both a config-less folder and a file child, so
    a client that returned every child (or every folder) would fail.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith(":/children"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"name": "real_vault", "folder": {}},
                        {"name": "stray", "folder": {}},
                        {"name": "notes.txt", "file": {}},
                    ]
                },
            )
        if url.endswith("/real_vault/vault_config.yaml"):
            return httpx.Response(200, json={"name": "vault_config.yaml"})
        if url.endswith("/stray/vault_config.yaml"):
            return httpx.Response(404)
        return httpx.Response(500, text="unexpected")

    client = _client(handler)
    assert client.list_vault_ids() == ["real_vault"]


def test_vsb_ds_011_fail_closed_and_404_tolerant():
    """A Graph 4xx/5xx fails closed as a ``RuntimeError`` naming the status; a 404
    on read is the absent case (``None``); a 404 on delete is tolerated.

    Anti-coincidental-pass: a client that swallowed errors would pass a naive
    happy path — assert the raise on 403/500 and the distinct 404 handling per
    operation.
    """
    denied = _client(lambda r: httpx.Response(403, text="denied"))
    with pytest.raises(RuntimeError, match="403"):
        denied.read_config_bytes("v")

    absent = _client(lambda r: httpx.Response(404))
    assert absent.read_config_bytes("v") is None
    absent.delete_config("v")  # 404 on delete is tolerated, no raise

    broken = _client(lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(RuntimeError, match="500"):
        broken.delete_config("v")


def test_vsb_ds_012_single_retry_on_throttle():
    """A single 429 with ``Retry-After`` is retried once and then succeeds; a
    persistent 429 raises after exactly one retry.

    Anti-coincidental-pass: assert the request count (2, not 1 and not infinite)
    and that the sleep honored ``Retry-After`` — a client that never retried, or
    that looped forever, would fail.
    """
    calls = {"n": 0}
    sleeps: list[float] = []

    def transient(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, content=b"ok")

    client = _client(transient, sleep=sleeps.append)
    assert client.read_config_bytes("v") == b"ok"
    assert calls["n"] == 2
    assert sleeps == [0.0]

    persistent = {"n": 0}

    def always_429(request: httpx.Request) -> httpx.Response:
        persistent["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "0"})

    client2 = _client(always_429)
    with pytest.raises(RuntimeError, match="429"):
        client2.read_config_bytes("v")
    assert persistent["n"] == 2  # original attempt + one retry, then give up


def test_vsb_ds_013_client_requires_configured_site_and_drive():
    """With site_id/drive_id unset, an operation fails loud rather than
    addressing a malformed path.

    Boundary: the builder constructs offline with empty coordinates (so the cloud
    boot's positive control can resolve the binding), but an actual Graph call
    must refuse rather than issue a request to ``/sites//drives//root``.
    """
    client = SharePointGraphClient(
        site_id=None,
        drive_id=None,
        root_path="vaults",
        token_provider=lambda: "tok",
        http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
    )
    with pytest.raises(RuntimeError, match="site_id"):
        client.list_vault_ids()


class _StubToken:
    token = "tok"


class _StubCredential:
    """Offline stand-in for ``DefaultAzureCredential``: mints a fixed token."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def get_token(self, *scopes, **kwargs) -> _StubToken:
        return _StubToken()


def _built_client(handler, monkeypatch) -> SharePointGraphClient:
    """Build the client through the production builder, wiring its ``httpx.Client``
    to a ``MockTransport`` and stubbing the managed-identity credential.

    Exercises the builder's *own* client construction -- crucially its
    ``follow_redirects`` setting -- rather than a client hand-built by the test, so
    a builder that does not follow redirects is caught.
    """
    transport = httpx.MockTransport(handler)
    real_client_cls = httpx.Client
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", _StubCredential)
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *a, **kw: real_client_cls(*a, **{**kw, "transport": transport}),
    )
    config = StackDocumentStoreConfig(site_id=_SITE, drive_id=_DRIVE)
    return build_sharepoint_graph_client(config)


def test_vsb_ds_021_content_reads_follow_graph_302_redirect(monkeypatch):
    """Graph's ``.../:/content`` endpoint answers a content GET with a 302 to a
    short-lived download URL; the built client must follow it and return the body.

    Reproduces the live cloud failure: a deployed SAGE *discovered* a seeded vault
    (the children and metadata endpoints answer 200 JSON with no redirect) but every
    content GET -- config and source bytes -- received the empty-bodied 302, whose
    302 status clears both the ``== 404`` and ``>= 400`` guards, so the reads
    returned ``b""`` and config loading parsed an empty document. The happy-path
    mocked-Graph tests missed it because they answer the content endpoint with the
    body directly, never a redirect.

    Anti-coincidental-pass: the client is built through the production builder with
    the redirect served by the transport, so a builder that does not enable
    ``follow_redirects`` yields the empty 302 body and every assertion fails. Drives
    all three content reads -- ``read_config_bytes``, ``read_source_bytes``, and the
    streamed ``hash_source_bytes``.
    """
    body = b"vault:\n  id: vault_a\n  name: Vault A\n"
    download_host = "download.sharepoint.example"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "graph.microsoft.com":
            # The content endpoint redirects to a short-lived, pre-signed download
            # URL on a different host -- exactly Graph's behavior.
            return httpx.Response(
                302, headers={"Location": f"https://{download_host}/blob?sig=abc"}
            )
        # The download URL serves the real bytes. httpx strips Authorization across
        # the host hop, matching the pre-signed URL contract.
        return httpx.Response(200, content=body)

    client = _built_client(handler, monkeypatch)

    assert client.read_config_bytes("vault_a") == body
    assert client.read_source_bytes("vault_a", "imports/x.md") == body
    assert client.hash_source_bytes("vault_a", "imports/x.md") == (
        "sha256:" + hashlib.sha256(body).hexdigest()
    )


def test_vsb_ds_020_graph_client_builder_resolves_in_clean_interpreter():
    """In a fresh interpreter, ``build_sharepoint_graph_client`` imports ``azure``
    and constructs the credential without ``ImportError``.

    Mirrors ``test_cloud_async_transport_resolves_in_a_clean_interpreter``: an
    ``ImportError`` here is a packaging defect (the locked image would omit the
    sync credential's transport and the binding would fail on first use); a
    network/auth failure of an *actual* Graph call is a separate, tolerated
    condition not exercised by mere construction.
    """
    code = textwrap.dedent(
        """
        import sys
        from sage.config import StackDocumentStoreConfig
        from sage.vault_source_document_store import build_sharepoint_graph_client

        client = build_sharepoint_graph_client(StackDocumentStoreConfig())
        assert any(m == "azure" or m.startswith("azure.") for m in sys.modules), (
            "the Graph client builder did not import the azure SDK"
        )
        print("OK")
        """
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "OK" in result.stdout


# --------------------------------------------------------------------------
# Graph client source-byte tests, against httpx.MockTransport
# --------------------------------------------------------------------------


def test_vsb_ds_040_source_ops_hit_scoped_content_and_item_urls():
    """``upload_source`` / ``read_source_bytes`` / ``source_item`` address the
    site/drive-scoped content and item endpoints under a bearer token.

    Anti-coincidental-pass: assert the PUT and content GET end at
    ``imports/x.md:/content`` and the stat GET ends at the bare item path (no
    ``:/content``), every request is site/drive-scoped and bearer-authenticated,
    and no tenant-wide path leaks.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        url = str(request.url)
        if request.method == "PUT":
            return httpx.Response(201, json={})
        if request.method == "GET" and url.endswith(":/content"):
            return httpx.Response(200, content=b"SRC")
        if request.method == "GET":
            return httpx.Response(200, json={"name": "x.md", "size": 3})
        return httpx.Response(500, text="unexpected")

    client = _client(handler)
    client.upload_source("vault_a", "imports/x.md", b"SRC")
    assert client.read_source_bytes("vault_a", "imports/x.md") == b"SRC"
    assert client.source_item("vault_a", "imports/x.md") == {"name": "x.md", "size": 3}

    scope = f"/sites/{_SITE}/drives/{_DRIVE}/root"
    for r in seen:
        assert r.headers["authorization"] == "Bearer tok"
        assert scope in str(r.url)
        assert str(r.url).split("/drives/")[0].endswith(f"/sites/{_SITE}")

    put = next(r for r in seen if r.method == "PUT")
    assert str(put.url).endswith("/vaults/vault_a/imports/x.md:/content")
    content_get = next(r for r in seen if r.method == "GET" and str(r.url).endswith(":/content"))
    assert str(content_get.url).endswith("/vaults/vault_a/imports/x.md:/content")
    stat_get = next(r for r in seen if r.method == "GET" and not str(r.url).endswith(":/content"))
    assert str(stat_get.url).endswith("/vaults/vault_a/imports/x.md")


def test_vsb_ds_041_source_ops_fail_closed_and_404_tolerant():
    """A Graph 4xx/5xx on a source op fails closed as ``RuntimeError``; a 404 on
    ``source_item`` is the absent case (``None``).

    Anti-coincidental-pass: assert the raise on 403/500 *and* the distinct 404
    handling — a client that swallowed errors would pass a naive happy path.
    """
    denied = _client(lambda r: httpx.Response(403, text="denied"))
    with pytest.raises(RuntimeError, match="403"):
        denied.read_source_bytes("v", "imports/x.md")

    absent = _client(lambda r: httpx.Response(404))
    assert absent.source_item("v", "imports/x.md") is None

    broken = _client(lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(RuntimeError, match="500"):
        broken.upload_source("v", "imports/x.md", b"data")


def test_vsb_ds_042_source_read_retries_once_on_throttle():
    """A single 429 on a source read is retried once and then succeeds.

    Anti-coincidental-pass: assert exactly two attempts (one retry, not zero and
    not infinite).
    """
    calls = {"n": 0}

    def transient(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, content=b"SRC")

    client = _client(transient)
    assert client.read_source_bytes("v", "imports/x.md") == b"SRC"
    assert calls["n"] == 2


def test_vsb_ds_043_hash_source_streams_multi_chunk_body():
    """``hash_source_bytes`` streams the content and returns the canonical
    ``sha256:<hex>`` over a body larger than one read chunk.

    Anti-coincidental-pass: drive a multi-chunk body and assert the streaming GET
    path was taken (spy on ``_http.stream``) and the digest matches — an
    implementation that loaded the whole body via ``read_source_bytes`` would not
    exercise ``stream``.
    """
    body = b"abcdefgh" * 40000  # 320 KB: spans several 64 KiB read chunks

    client = _client(lambda r: httpx.Response(200, content=body))
    streamed: list[tuple] = []
    real_stream = client._http.stream

    def spy(*args, **kwargs):
        streamed.append((args, kwargs))
        return real_stream(*args, **kwargs)

    client._http.stream = spy

    digest = client.hash_source_bytes("v", "imports/big.bin")
    assert digest == "sha256:" + hashlib.sha256(body).hexdigest()
    assert streamed, "hash_source_bytes did not use the streaming GET path"


def test_vsb_ds_052_source_download_url_reads_graph_annotation():
    """``source_download_url`` returns the driveItem's
    ``@microsoft.graph.downloadUrl`` -- a pre-authenticated, time-limited URL Graph
    returns on an item metadata GET -- and ``None`` when the annotation is absent
    or the item is missing.

    Anti-coincidental-pass: assert the exact annotated URL is returned (a client
    reading a wrong key, or the bare item URL, would yield ``None``), and that both
    the missing-annotation and 404 paths resolve to ``None``. The metadata GET
    carries no ``:/content`` suffix, so the bytes are never pulled through this
    process.
    """
    dl = "https://contoso.sharepoint.com/_layouts/15/download.aspx?tempauth=abc123"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"name": "x.pdf", "size": 8, "@microsoft.graph.downloadUrl": dl}
        )

    assert _client(handler).source_download_url("vault_a", "imports/x.pdf") == dl
    # An item metadata GET, not a content download.
    assert not str(seen[0].url).endswith(":/content")

    # Item exists but Graph returned no downloadUrl -> None.
    no_anno = _client(lambda r: httpx.Response(200, json={"name": "x.pdf", "size": 8}))
    assert no_anno.source_download_url("v", "imports/x.pdf") is None

    # Item absent (404) -> None.
    absent = _client(lambda r: httpx.Response(404))
    assert absent.source_download_url("v", "imports/x.pdf") is None
