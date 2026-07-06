"""Cloud-profile teardown entrypoint (``sage.maintenance.delete_vault_cloud``).

Exercises the env-driven entrypoint against fakes: the deferred resolvers
(``get_stack_config`` and the two ``build_stack_*`` factories) are patched on their
source modules, and the SharePoint snapshot sink is tested directly. No Azure SDK,
no live tenant, no Postgres -- the cloud wiring is verified structurally; the real
in-cloud run is the out-of-band post-deploy smoke (CAS-ADR-043).
"""

import json
from types import SimpleNamespace

import pytest

import sage.maintenance.delete_vault_cloud as dvc
from sage.maintenance.delete_vault_cloud import main, upload_teardown_archive

_FAKE_STACK = SimpleNamespace(
    postgres=SimpleNamespace(
        host="pg.example", port=5432, database="sage", user="id-sage-prod", sslmode="require"
    ),
    document_store=SimpleNamespace(
        site_id="site", drive_id="drive", root_path="vaults", graph_scope="scope"
    ),
)


class _FakeSourceStore:
    """A document-store-like source store: no filesystem locator, recording ops."""

    def __init__(self):
        self.deleted_trees = []
        self.deleted_configs = []
        self.closed = False

    def config_locator(self, vault_id):
        return None

    def delete_source_tree(self, vault_id, storage_root):
        self.deleted_trees.append((vault_id, storage_root))

    def delete_config(self, vault_id):
        self.deleted_configs.append(vault_id)

    def close(self):
        self.closed = True


class _RecordingGraphClient:
    """A SharePoint archive client stand-in that records its close (the snapshot
    sink upload is exercised separately by ``_FakeArchiveClient``)."""

    def __init__(self):
        self.closed = False

    def list_sources(self, vault_id):
        return []

    def write_archive(self, archive_path, data):
        pass

    def close(self):
        self.closed = True


class _FakeProvisioner:
    def __init__(self):
        self.dropped = []

    async def schema_exists(self, vault_id):
        return True

    async def drop_vault_schema(self, vault_id):
        self.dropped.append(vault_id)


def _patch_resolvers(monkeypatch, *, source_store=None, provisioner=None):
    """Patch the env-config builder and the deferred store factories."""
    monkeypatch.setattr(dvc, "_config_from_env", lambda env: _FAKE_STACK)
    monkeypatch.setattr(
        "sage.vault_source_binding.build_stack_vault_source_store",
        lambda cfg, **kw: source_store if source_store is not None else _FakeSourceStore(),
    )
    monkeypatch.setattr(
        "sage.storage_binding.build_stack_storage_provisioner",
        lambda cfg, **kw: provisioner if provisioner is not None else _FakeProvisioner(),
    )


def _clear_env(monkeypatch):
    for name in (
        "SAGE_DELETE_VAULT_ID",
        "SAGE_DELETE_CONFIRM",
        "SAGE_DELETE_APPLY",
        "SAGE_DELETE_SNAPSHOT",
        "SAGE_DELETE_REASON",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Env parsing -> delete_vault call args
# ---------------------------------------------------------------------------


def test_cloud_reads_env_params(monkeypatch):
    """The per-invocation env vars map onto the ``delete_vault`` call: vault id,
    reason, apply, snapshot, and the typed-confirm value threaded as the prompt."""
    _clear_env(monkeypatch)
    _patch_resolvers(monkeypatch)
    monkeypatch.setattr(
        "sage.vault_source_document_store.build_sharepoint_graph_client",
        lambda cfg, **kw: _RecordingGraphClient(),
    )

    async def _fake_pw():
        return "tok"

    monkeypatch.setattr(dvc, "_cloud_dump_password", _fake_pw)

    captured = {}

    async def _fake_delete_vault(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(dvc, "delete_vault", _fake_delete_vault)

    monkeypatch.setenv("SAGE_DELETE_VAULT_ID", "cas_smoke")
    monkeypatch.setenv("SAGE_DELETE_CONFIRM", "cas_smoke")
    monkeypatch.setenv("SAGE_DELETE_APPLY", "1")
    monkeypatch.setenv("SAGE_DELETE_SNAPSHOT", "true")
    monkeypatch.setenv("SAGE_DELETE_REASON", "retire the smoke vault")
    # config coordinates unused here (the builder is patched), but set so nothing
    # falls through to a real read.

    rc = main()

    assert rc == 0
    assert captured["vault_id"] == "cas_smoke"
    assert captured["reason"] == "retire the smoke vault"
    assert captured["apply"] is True
    assert captured["snapshot"] is True
    assert captured["input_fn"]("prompt") == "cas_smoke"  # typed-confirm threaded


def test_cloud_missing_vault_id_refuses(monkeypatch):
    """No ``SAGE_DELETE_VAULT_ID`` is a usage error (exit 2), before any resolution."""
    _clear_env(monkeypatch)
    assert main() == 2


def test_config_from_env_builds_cloud_document_store_config():
    """The env-driven config builder yields a cloud, document-store config carrying
    the Postgres and SharePoint coordinates -- the job has no lifespan to populate a
    stack-config singleton, so it reads the coordinates straight from its env.

    Anti-coincidental-pass: assert the fixed cloud/document-store selectors *and* the
    threaded Postgres + SharePoint coordinates -- a builder that returned an empty
    default (the singleton-less ``get_stack_config`` failure mode) would carry the
    filesystem backend and no host/site.
    """
    cfg = dvc._config_from_env(
        {
            "PG_FQDN": "pg.example",
            "PG_DATABASE": "sage",
            "PG_USER": "id-sage-prod",
            "SHAREPOINT_SITE_ID": "site",
            "SHAREPOINT_DRIVE_ID": "drive",
            "SHAREPOINT_ROOT_PATH": "vaults",
        }
    )

    assert cfg.profile == "cloud"
    assert cfg.vault_source_backend == "document_store"
    assert cfg.postgres.host == "pg.example"
    assert cfg.postgres.database == "sage"
    assert cfg.postgres.user == "id-sage-prod"
    assert cfg.postgres.sslmode == "require"
    assert cfg.document_store.site_id == "site"
    assert cfg.document_store.drive_id == "drive"
    assert cfg.document_store.root_path == "vaults"


def test_config_from_env_requires_coordinates():
    """A missing required coordinate fails loud rather than silently defaulting."""
    with pytest.raises(ValueError, match="PG_FQDN"):
        dvc._config_from_env({})


# ---------------------------------------------------------------------------
# Cloud-binding wiring
# ---------------------------------------------------------------------------


def test_cloud_binds_document_store_and_cloud_provisioner(monkeypatch):
    """The entrypoint builds the source store and provisioner with
    ``managed_identity=True`` and hands them (plus a snapshot sink) to the core.

    Anti-coincidental-pass: assert the exact ``managed_identity=True`` kwarg on both
    factories -- a call that dropped the flag would resolve the filesystem/on-box
    bindings and never reach SharePoint or the Entra-only Postgres.
    """
    _clear_env(monkeypatch)
    seen = {}

    def _rec_source(cfg, **kw):
        seen["source_mi"] = kw.get("managed_identity")
        return _FakeSourceStore()

    def _rec_prov(cfg, **kw):
        seen["prov_mi"] = kw.get("managed_identity")
        return _FakeProvisioner()

    monkeypatch.setattr(dvc, "_config_from_env", lambda env: _FAKE_STACK)
    monkeypatch.setattr("sage.vault_source_binding.build_stack_vault_source_store", _rec_source)
    monkeypatch.setattr("sage.storage_binding.build_stack_storage_provisioner", _rec_prov)
    monkeypatch.setattr(
        "sage.vault_source_document_store.build_sharepoint_graph_client",
        lambda cfg, **kw: _RecordingGraphClient(),
    )

    async def _fake_pw():
        return "tok"

    monkeypatch.setattr(dvc, "_cloud_dump_password", _fake_pw)

    captured = {}

    async def _fake_delete_vault(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(dvc, "delete_vault", _fake_delete_vault)

    monkeypatch.setenv("SAGE_DELETE_VAULT_ID", "cas_smoke")
    monkeypatch.setenv("SAGE_DELETE_CONFIRM", "cas_smoke")
    monkeypatch.setenv("SAGE_DELETE_APPLY", "1")

    rc = main()

    assert rc == 0
    assert seen["source_mi"] is True
    assert seen["prov_mi"] is True
    assert isinstance(captured["source_store"], _FakeSourceStore)
    assert isinstance(captured["provisioner"], _FakeProvisioner)
    assert callable(captured["snapshot_sink"])


# ---------------------------------------------------------------------------
# The safety envelope survives the CI adaptation (real core, fakes injected)
# ---------------------------------------------------------------------------


def test_cloud_typed_confirm_mismatch_refuses(monkeypatch):
    """A confirmation that does not equal the vault id refuses (exit 3) and destroys
    nothing -- the typed-confirm envelope is live even without interactive stdin.

    Anti-coincidental-pass: runs the *real* core; a wiring that bypassed the prompt
    (e.g. auto-confirming) would drop the schema and delete the tree.
    """
    _clear_env(monkeypatch)
    store = _FakeSourceStore()
    prov = _FakeProvisioner()
    _patch_resolvers(monkeypatch, source_store=store, provisioner=prov)

    monkeypatch.setenv("SAGE_DELETE_VAULT_ID", "cas_smoke")
    monkeypatch.setenv("SAGE_DELETE_CONFIRM", "WRONG")
    monkeypatch.setenv("SAGE_DELETE_APPLY", "1")
    monkeypatch.setenv("SAGE_DELETE_SNAPSHOT", "false")  # no dump/sink -> no Azure

    rc = main()

    assert rc == 3
    assert prov.dropped == []
    assert store.deleted_trees == []


def test_cloud_dry_run_is_default(monkeypatch):
    """Without ``SAGE_DELETE_APPLY`` the run is a dry-run: it prints the plan, returns
    0, and destroys nothing."""
    _clear_env(monkeypatch)
    store = _FakeSourceStore()
    prov = _FakeProvisioner()
    _patch_resolvers(monkeypatch, source_store=store, provisioner=prov)

    monkeypatch.setenv("SAGE_DELETE_VAULT_ID", "cas_smoke")
    monkeypatch.setenv("SAGE_DELETE_CONFIRM", "cas_smoke")
    monkeypatch.setenv("SAGE_DELETE_SNAPSHOT", "false")

    rc = main()

    assert rc == 0
    assert prov.dropped == []
    assert store.deleted_trees == []


def test_cloud_apply_runs_full_teardown_via_core(monkeypatch):
    """Apply + matching confirm + snapshot off runs the core end to end against the
    fakes: schema dropped, source tree removed via the port."""
    _clear_env(monkeypatch)
    store = _FakeSourceStore()
    prov = _FakeProvisioner()
    _patch_resolvers(monkeypatch, source_store=store, provisioner=prov)

    monkeypatch.setenv("SAGE_DELETE_VAULT_ID", "cas_smoke")
    monkeypatch.setenv("SAGE_DELETE_CONFIRM", "cas_smoke")
    monkeypatch.setenv("SAGE_DELETE_APPLY", "yes")
    monkeypatch.setenv("SAGE_DELETE_SNAPSHOT", "false")

    rc = main()

    assert rc == 0
    assert prov.dropped == ["cas_smoke"]
    assert store.deleted_trees == [("cas_smoke", None)]
    assert store.deleted_configs == ["cas_smoke"]


# ---------------------------------------------------------------------------
# The snapshot sink: manifest + archive upload to the drive-root sibling folder
# ---------------------------------------------------------------------------


class _FakeArchiveClient:
    def __init__(self, sources):
        self._sources = sources
        self.archives = {}

    def list_sources(self, vault_id):
        return list(self._sources)

    def write_archive(self, archive_path, data):
        self.archives[archive_path] = data


def test_cloud_snapshot_sink_uploads_dump_and_manifest_to_archive(tmp_path):
    """The sink writes a manifest from the live SharePoint sources, then uploads the
    schema dump and the manifest to ``_teardown_archives/<run>/`` -- a drive-root
    sibling of the vault tree.

    Anti-coincidental-pass: every archived key is under ``_teardown_archives/`` and
    none under the vault ``vaults/`` tree -- an archive written inside the vault
    folder would be lost to the subsequent folder delete; the manifest content
    reflects the enumerated sources, so a sink that skipped enumeration would fail.
    """
    run_dir = tmp_path / "cas_smoke-20260101T000000Z"
    run_dir.mkdir()
    (run_dir / "schema.dump").write_bytes(b"DUMP")
    client = _FakeArchiveClient([{"path": "imports/a.md", "size": 3}])

    upload_teardown_archive(client, "cas_smoke", run_dir)

    prefix = "_teardown_archives/cas_smoke-20260101T000000Z"
    assert client.archives[f"{prefix}/schema.dump"] == b"DUMP"
    manifest = json.loads(client.archives[f"{prefix}/sources_manifest.json"].decode())
    assert manifest == [{"path": "imports/a.md", "size": 3}]
    assert all(key.startswith("_teardown_archives/") for key in client.archives)
    assert all("vaults/" not in key for key in client.archives)


# ---------------------------------------------------------------------------
# Shutdown hygiene: the short-lived job releases its HTTP/aiohttp clients
# ---------------------------------------------------------------------------


class _CredentialCloseRecorder:
    """Async stand-in for ``close_postgres_credential`` that counts its calls."""

    def __init__(self):
        self.calls = 0

    async def __call__(self):
        self.calls += 1


def _patch_cleanup_probes(monkeypatch, *, source_store, archive_client, delete_vault_impl):
    """Wire the entrypoint to recording fakes and return the credential-close recorder."""
    _patch_resolvers(monkeypatch, source_store=source_store)
    monkeypatch.setattr(
        "sage.vault_source_document_store.build_sharepoint_graph_client",
        lambda cfg, **kw: archive_client,
    )

    async def _fake_pw():
        return "tok"

    monkeypatch.setattr(dvc, "_cloud_dump_password", _fake_pw)
    monkeypatch.setattr(dvc, "delete_vault", delete_vault_impl)
    cred = _CredentialCloseRecorder()
    monkeypatch.setattr("sage.storage.postgres.managed_identity.close_postgres_credential", cred)
    return cred


def test_cloud_shutdown_closes_clients_and_credential(monkeypatch):
    """A snapshot-ON run closes, at shutdown, the archive Graph client, the source
    store's client, and the cached Entra credential's aiohttp session.

    Anti-coincidental-pass: assert all three recorders fired -- an entrypoint that
    returned without a cleanup ``finally`` would leave the archive client, the
    source store, and the credential all open.
    """
    _clear_env(monkeypatch)
    store = _FakeSourceStore()
    archive = _RecordingGraphClient()

    async def _ok_delete(**kwargs):
        return 0

    cred = _patch_cleanup_probes(
        monkeypatch, source_store=store, archive_client=archive, delete_vault_impl=_ok_delete
    )

    monkeypatch.setenv("SAGE_DELETE_VAULT_ID", "cas_smoke")
    monkeypatch.setenv("SAGE_DELETE_CONFIRM", "cas_smoke")
    monkeypatch.setenv("SAGE_DELETE_APPLY", "1")
    monkeypatch.setenv("SAGE_DELETE_SNAPSHOT", "true")

    rc = main()

    assert rc == 0
    assert archive.closed is True
    assert store.closed is True
    assert cred.calls == 1


def test_cloud_shutdown_cleanup_runs_when_teardown_raises(monkeypatch):
    """Cleanup is ``finally``-guaranteed: an exception mid-teardown still closes all
    three clients before it propagates.

    Anti-coincidental-pass: a ``try``/``return`` without ``finally`` would skip the
    closes on the raising path, leaving the recorders unset.
    """
    _clear_env(monkeypatch)
    store = _FakeSourceStore()
    archive = _RecordingGraphClient()

    async def _boom_delete(**kwargs):
        raise RuntimeError("teardown blew up")

    cred = _patch_cleanup_probes(
        monkeypatch, source_store=store, archive_client=archive, delete_vault_impl=_boom_delete
    )

    monkeypatch.setenv("SAGE_DELETE_VAULT_ID", "cas_smoke")
    monkeypatch.setenv("SAGE_DELETE_CONFIRM", "cas_smoke")
    monkeypatch.setenv("SAGE_DELETE_APPLY", "1")
    monkeypatch.setenv("SAGE_DELETE_SNAPSHOT", "true")

    with pytest.raises(RuntimeError, match="teardown blew up"):
        main()

    assert archive.closed is True
    assert store.closed is True
    assert cred.calls == 1


def test_cloud_shutdown_snapshot_off_closes_source_and_credential(monkeypatch):
    """With snapshot OFF no archive client is built, so cleanup closes only the
    source store and the credential -- and never tries to close a nonexistent
    archive client.

    Anti-coincidental-pass: the ``build_sharepoint_graph_client`` sentinel raises if
    called, proving the snapshot-off path builds no archive client; the source store
    and credential still close.
    """
    _clear_env(monkeypatch)
    store = _FakeSourceStore()

    def _must_not_build(cfg, **kw):
        raise AssertionError("archive client built with snapshot off")

    async def _ok_delete(**kwargs):
        return 0

    _patch_resolvers(monkeypatch, source_store=store)
    monkeypatch.setattr(
        "sage.vault_source_document_store.build_sharepoint_graph_client", _must_not_build
    )
    monkeypatch.setattr(dvc, "delete_vault", _ok_delete)
    cred = _CredentialCloseRecorder()
    monkeypatch.setattr("sage.storage.postgres.managed_identity.close_postgres_credential", cred)

    monkeypatch.setenv("SAGE_DELETE_VAULT_ID", "cas_smoke")
    monkeypatch.setenv("SAGE_DELETE_CONFIRM", "cas_smoke")
    monkeypatch.setenv("SAGE_DELETE_APPLY", "1")
    monkeypatch.setenv("SAGE_DELETE_SNAPSHOT", "false")

    rc = main()

    assert rc == 0
    assert store.closed is True
    assert cred.calls == 1
