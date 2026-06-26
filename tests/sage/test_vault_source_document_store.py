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
import subprocess
import sys
import textwrap

import httpx
import pytest

from sage.config import StackDocumentStoreConfig, VaultConfig
from sage.vault_source_binding import DiscoveredVault, DocumentStoreVaultSourceStore
from sage.vault_source_document_store import SharePointGraphClient

_SITE = "contoso.sharepoint.com,site-guid,web-guid"
_DRIVE = "b!drive-id"


# --------------------------------------------------------------------------
# Binding tests, against an in-memory fake Graph client
# --------------------------------------------------------------------------


class _FakeGraphClient:
    """In-memory stand-in for SharePointGraphClient: vault_id -> config bytes."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.uploads = 0
        self.deletes = 0

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
