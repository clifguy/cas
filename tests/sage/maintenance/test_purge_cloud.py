"""Cloud-profile purge entrypoint (``sage.maintenance.purge_cloud``).

Exercises the env-driven entrypoint against fakes: the env-config builder, the
binding factories, and the vault resolution are patched, and the purge cores
are either captured (wiring tests) or run for real over stubs (envelope
tests). No Azure SDK, no live tenant, no Postgres — the cloud wiring is
verified structurally; the real in-cloud run is the out-of-band post-deploy
smoke (CAS-ADR-043).
"""

import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace

import sage.maintenance.purge_cloud as pc
from sage.adapters.stubs import StubContentStore, StubGraphStore
from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import Document

_FAKE_STACK = SimpleNamespace()

_PURGE_ENV = (
    "SAGE_MAINTENANCE_COMMAND",
    "SAGE_PURGE_VAULT_ID",
    "SAGE_PURGE_REASON",
    "SAGE_PURGE_APPLY",
    "SAGE_PURGE_CONFIRM",
    "SAGE_PURGE_DOCUMENT_ID",
    "SAGE_PURGE_HEAD_ID",
    "SAGE_PURGE_EDGE_TYPE",
    "SAGE_PURGE_ALLOW_BRANCHED",
    "SAGE_PURGE_CONFIRM_LENGTH",
    "SAGE_PURGE_INGESTED_SINCE",
    "SAGE_PURGE_INGESTED_UNTIL",
)


def _did(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()[:8] + "_" + name


def _doc(name: str) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=_did(name),
        title=f"Doc {name}",
        source_type=SourceType.MARKDOWN,
        source_path=f"/x/{name}.md",
        lifecycle_status="active",
        source_content_hash="sha256:" + hashlib.sha256(name.encode()).hexdigest(),
        adapter_version="1",
        created_by="t",
        created_at=now,
        last_modified_by="t",
        updated_at=now,
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
        tags=["a"],
        doc_type="ticket",
    )


class _StubSink:
    def __init__(self):
        self.records = []

    async def append(self, record):
        self.records.append(dict(record))


class _StubHandle:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _FakeSourceStore:
    """A source store stand-in that records its (synchronous) close."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _CredentialCloseRecorder:
    """Async stand-in for ``close_postgres_credential`` that counts its calls."""

    def __init__(self):
        self.calls = 0

    async def __call__(self):
        self.calls += 1


def _clear_env(monkeypatch):
    for name in _PURGE_ENV:
        monkeypatch.delenv(name, raising=False)


def _patch_resolution(
    monkeypatch, *, graph=None, content=None, sink=None, handle=None, seen=None, source_store=None
):
    """Patch the env-config builder, binding factories, and vault resolution."""
    graph = graph if graph is not None else StubGraphStore()
    content = content if content is not None else StubContentStore()
    sink = sink if sink is not None else _StubSink()
    handle = handle if handle is not None else _StubHandle()
    source_store = source_store if source_store is not None else _FakeSourceStore()

    monkeypatch.setattr(pc, "_config_from_env", lambda env: _FAKE_STACK)

    def _rec_source(cfg, **kw):
        if seen is not None:
            seen["source_mi"] = kw.get("managed_identity")
        return source_store

    def _rec_prov(cfg, **kw):
        if seen is not None:
            seen["prov_mi"] = kw.get("managed_identity")
        return object()

    monkeypatch.setattr("sage.vault_source_binding.build_stack_vault_source_store", _rec_source)
    monkeypatch.setattr("sage.storage_binding.build_stack_storage_provisioner", _rec_prov)

    async def _fake_open(vault_id, *, source_store=None, provisioner=None):
        return (graph, content, sink, handle)

    monkeypatch.setattr(pc, "open_vault_stores", _fake_open)
    return graph, content, sink, handle


# ---------------------------------------------------------------------------
# Env parsing -> purge-core call args
# ---------------------------------------------------------------------------


def test_cloud_maps_env_onto_the_document_purge_core(monkeypatch):
    """The per-invocation env vars map onto the ``purge_document`` call: target
    id, reason, apply, and the typed-confirm value threaded as the prompt."""
    _clear_env(monkeypatch)
    _, _, sink, handle = _patch_resolution(monkeypatch)

    captured = {}

    async def _fake_core(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(pc, "purge_document", _fake_core)

    monkeypatch.setenv("SAGE_MAINTENANCE_COMMAND", "purge_document")
    monkeypatch.setenv("SAGE_PURGE_VAULT_ID", "cas_smoke")
    monkeypatch.setenv("SAGE_PURGE_DOCUMENT_ID", "deadbeef_doc")
    monkeypatch.setenv("SAGE_PURGE_CONFIRM", "deadbeef_doc")
    monkeypatch.setenv("SAGE_PURGE_APPLY", "1")
    monkeypatch.setenv("SAGE_PURGE_REASON", "wrong-vault ingest")

    rc = pc.main()

    assert rc == 0
    assert captured["document_id"] == "deadbeef_doc"
    assert captured["reason"] == "wrong-vault ingest"
    assert captured["apply"] is True
    assert captured["audit_sink"] is sink
    assert captured["input_fn"]("prompt") == "deadbeef_doc"  # typed-confirm threaded
    assert handle.closed


def test_cloud_chain_mode_threads_both_confirmations(monkeypatch):
    """Chain mode answers the head-id prompt then the chain-length prompt from
    the two confirmation env vars, in order."""
    _clear_env(monkeypatch)
    _patch_resolution(monkeypatch)

    captured = {}

    async def _fake_core(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(pc, "purge_chain", _fake_core)

    monkeypatch.setenv("SAGE_MAINTENANCE_COMMAND", "purge_chain")
    monkeypatch.setenv("SAGE_PURGE_VAULT_ID", "cas_smoke")
    monkeypatch.setenv("SAGE_PURGE_HEAD_ID", "deadbeef_head")
    monkeypatch.setenv("SAGE_PURGE_CONFIRM", "deadbeef_head")
    monkeypatch.setenv("SAGE_PURGE_CONFIRM_LENGTH", "3")

    rc = pc.main()

    assert rc == 0
    assert captured["head_id"] == "deadbeef_head"
    assert captured["edge_type"] == "supersedes"
    assert captured["allow_branched"] is False
    input_fn = captured["input_fn"]
    assert input_fn("head?") == "deadbeef_head"
    assert input_fn("length?") == "3"


def test_cloud_batch_mode_parses_the_window(monkeypatch):
    """Batch mode parses the half-open window; a naive timestamp is UTC and an
    absent upper bound stays ``None`` (script-start semantics live in the core)."""
    _clear_env(monkeypatch)
    _patch_resolution(monkeypatch)

    captured = {}

    async def _fake_core(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(pc, "purge_batch", _fake_core)

    monkeypatch.setenv("SAGE_MAINTENANCE_COMMAND", "purge_batch")
    monkeypatch.setenv("SAGE_PURGE_VAULT_ID", "cas_smoke")
    monkeypatch.setenv("SAGE_PURGE_INGESTED_SINCE", "2026-07-01T00:00:00")
    monkeypatch.setenv("SAGE_PURGE_CONFIRM", "2")

    rc = pc.main()

    assert rc == 0
    assert captured["since"] == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert captured["until"] is None
    assert captured["input_fn"]("count?") == "2"


# ---------------------------------------------------------------------------
# Cloud-binding wiring
# ---------------------------------------------------------------------------


def test_cloud_builds_bindings_with_managed_identity(monkeypatch):
    """Both binding factories receive ``managed_identity=True`` — a call that
    dropped the flag would resolve the on-box bindings and never reach the
    Entra-only Postgres or SharePoint."""
    _clear_env(monkeypatch)
    seen = {}
    _patch_resolution(monkeypatch, seen=seen)

    async def _fake_core(**kwargs):
        return 0

    monkeypatch.setattr(pc, "purge_document", _fake_core)

    monkeypatch.setenv("SAGE_MAINTENANCE_COMMAND", "purge_document")
    monkeypatch.setenv("SAGE_PURGE_VAULT_ID", "cas_smoke")
    monkeypatch.setenv("SAGE_PURGE_DOCUMENT_ID", "deadbeef_doc")

    assert pc.main() == 0
    assert seen["source_mi"] is True
    assert seen["prov_mi"] is True


# ---------------------------------------------------------------------------
# Usage errors refuse before any resolution
# ---------------------------------------------------------------------------


def test_missing_or_unknown_command_refuses(monkeypatch):
    _clear_env(monkeypatch)
    assert pc.main() == 2
    monkeypatch.setenv("SAGE_MAINTENANCE_COMMAND", "purge_everything")
    assert pc.main() == 2


def test_missing_vault_id_refuses(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SAGE_MAINTENANCE_COMMAND", "purge_document")
    assert pc.main() == 2


def test_missing_mode_target_refuses(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SAGE_MAINTENANCE_COMMAND", "purge_document")
    monkeypatch.setenv("SAGE_PURGE_VAULT_ID", "cas_smoke")
    assert pc.main() == 2


def test_malformed_batch_timestamp_refuses(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SAGE_MAINTENANCE_COMMAND", "purge_batch")
    monkeypatch.setenv("SAGE_PURGE_VAULT_ID", "cas_smoke")
    monkeypatch.setenv("SAGE_PURGE_INGESTED_SINCE", "not-a-timestamp")
    assert pc.main() == 2


# ---------------------------------------------------------------------------
# The safety envelope survives the CI adaptation (real core, fakes injected)
# ---------------------------------------------------------------------------


def test_cloud_dry_run_is_default(monkeypatch):
    """Without ``SAGE_PURGE_APPLY`` the run is a dry-run: it prints the plan,
    returns 0, and removes nothing."""
    _clear_env(monkeypatch)
    graph = StubGraphStore()
    doc = _doc("keep")
    _patch_resolution(monkeypatch, graph=graph)

    import asyncio

    asyncio.run(graph.insert_document(doc))

    monkeypatch.setenv("SAGE_MAINTENANCE_COMMAND", "purge_document")
    monkeypatch.setenv("SAGE_PURGE_VAULT_ID", "cas_smoke")
    monkeypatch.setenv("SAGE_PURGE_DOCUMENT_ID", doc.id)
    monkeypatch.setenv("SAGE_PURGE_CONFIRM", doc.id)

    rc = pc.main()

    assert rc == 0
    assert asyncio.run(graph.get_document(doc.id)) is not None


def test_cloud_typed_confirm_mismatch_refuses(monkeypatch):
    """A confirmation that does not equal the document id refuses (exit 3) and
    removes nothing — the typed-confirm envelope is live without interactive
    stdin."""
    _clear_env(monkeypatch)
    graph = StubGraphStore()
    sink = _StubSink()
    doc = _doc("keep")
    _patch_resolution(monkeypatch, graph=graph, sink=sink)

    import asyncio

    asyncio.run(graph.insert_document(doc))

    monkeypatch.setenv("SAGE_MAINTENANCE_COMMAND", "purge_document")
    monkeypatch.setenv("SAGE_PURGE_VAULT_ID", "cas_smoke")
    monkeypatch.setenv("SAGE_PURGE_DOCUMENT_ID", doc.id)
    monkeypatch.setenv("SAGE_PURGE_CONFIRM", "WRONG")
    monkeypatch.setenv("SAGE_PURGE_APPLY", "1")

    rc = pc.main()

    assert rc == 3
    assert asyncio.run(graph.get_document(doc.id)) is not None
    assert sink.records == []


# ---------------------------------------------------------------------------
# Shutdown hygiene: the short-lived job releases its HTTP/aiohttp clients
# ---------------------------------------------------------------------------


def test_cloud_shutdown_closes_source_store_and_credential(monkeypatch):
    """A purge run closes, at shutdown, the vault-store handle (existing), the
    source store's Graph client, and the cached Entra credential's aiohttp session.

    Anti-coincidental-pass: assert the source-store and credential recorders fired
    in addition to ``handle.closed`` -- an entrypoint whose ``finally`` closed only
    the handle would leave the source store and credential open.
    """
    _clear_env(monkeypatch)
    store = _FakeSourceStore()
    _, _, _, handle = _patch_resolution(monkeypatch, source_store=store)

    async def _fake_core(**kwargs):
        return 0

    monkeypatch.setattr(pc, "purge_document", _fake_core)
    cred = _CredentialCloseRecorder()
    monkeypatch.setattr("sage.storage.postgres.managed_identity.close_postgres_credential", cred)

    monkeypatch.setenv("SAGE_MAINTENANCE_COMMAND", "purge_document")
    monkeypatch.setenv("SAGE_PURGE_VAULT_ID", "cas_smoke")
    monkeypatch.setenv("SAGE_PURGE_DOCUMENT_ID", "deadbeef_doc")

    assert pc.main() == 0
    assert handle.closed is True
    assert store.closed is True
    assert cred.calls == 1
