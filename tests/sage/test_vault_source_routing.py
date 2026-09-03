"""The three hot paths route their source-byte work through the vault-source
store, not direct filesystem access (CAS-ADR-043).

Each test injects a fake store by monkeypatching
``sage.mcp_init.resolve_stack_vault_source_store`` and has that fake return a
sentinel the real filesystem binding could never produce. A test passes only if
the service actually delegates to the store; were the service still constructing
``storage_root / source_path`` itself, the real file's bytes (or a real path)
would surface and the sentinel assertion would fail. Each fake subclasses the
real filesystem binding so any method it does not override behaves normally.

Test IDs follow VSBB-NNN (Vault-Source Binding Bytes), routing slice.
"""

import base64
import hashlib
import shutil
from pathlib import Path

import pytest

from sage.api.errors import (
    ContentFileMissingError,
    DuplicateContentError,
    ForceReingestPathMismatchError,
    SourceFileNotFoundError,
    VaultSourcePathRefusedError,
)
from sage.models.enums import SourceType
from sage.models.schemas import IngestRequest
from sage.services.documents import DocumentsService
from sage.services.maintenance import MaintenanceService
from sage.vault_source_binding import FilesystemVaultSourceStore, hash_file

_UNUSED_ROOT = Path("/unused/vault_root")


def _patch_store(monkeypatch, store) -> None:
    """Make the lazily-resolved stack vault-source store be ``store``."""
    monkeypatch.setattr(
        "sage.mcp_init.resolve_stack_vault_source_store",
        lambda *args, **kwargs: store,
    )


async def _ingest_internal(ingestion_service, tmp_vault_dir, rel: str, body: str):
    """Seed an internal source file and ingest it; return the document."""
    full = tmp_vault_dir / "sources" / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body)
    result = await ingestion_service.ingest(
        IngestRequest(source=rel, source_type=SourceType.MARKDOWN)
    )
    return result.document


# --------------------------------------------------------------------------- #
# VSBB-013: ingest retention routes through retain_source
# --------------------------------------------------------------------------- #


class _SentinelRetainStore(FilesystemVaultSourceStore):
    SENTINEL = "imports/SENTINEL.md"

    def retain_source(self, vault_id, storage_root, source_path, delivered_hash=None):
        # Materialize the retained file so the post-retain projection can read
        # it, but return a sentinel relative path the real binding never would.
        dest = storage_root / "imports" / "SENTINEL.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, dest)
        return self.SENTINEL


async def test_vsbb_013_ingest_uses_port_retain(
    ingestion_service, tmp_vault_dir, tmp_path, monkeypatch
):
    """The ingested document's ``source_path`` is the value ``retain_source``
    returned, proving retention routes through the port. Anti-coincidental: the
    real binding would return ``imports/external.md``, not the sentinel."""
    _patch_store(monkeypatch, _SentinelRetainStore(_UNUSED_ROOT))

    external = tmp_path / "external.md"
    external.write_text("# External\n\nBody.")

    result = await ingestion_service.ingest(
        IngestRequest(source=str(external), source_type=SourceType.MARKDOWN)
    )

    assert result.document.source_path == _SentinelRetainStore.SENTINEL


# --------------------------------------------------------------------------- #
# VSBB-030 / VSBB-031: one digest of the delivered file per ingest
# --------------------------------------------------------------------------- #


def _count_hashed_paths(monkeypatch) -> list[Path]:
    """Record every path ``hash_file`` is asked to digest during an ingest.

    Per-path rather than a bare call count: an ingest legitimately hashes more
    than one file (a name collision also digests the copy already retained), and
    the claim under test is about the *delivered* file specifically.
    """
    import sage.vault_source_binding as binding

    real = binding.hash_file
    seen: list[Path] = []

    def counting(path: Path) -> str:
        seen.append(Path(path))
        return real(path)

    monkeypatch.setattr(binding, "hash_file", counting)
    return seen


def _count_whole_file_reads(monkeypatch) -> list[Path]:
    """Record every path loaded whole into memory via ``Path.read_bytes``.

    Distinct from :func:`_count_hashed_paths`, and not redundant with it. A
    digest taken through ``hash_file`` streams; one taken by hashing
    ``read_bytes()`` output does not, and does not pass through ``hash_file``
    either -- so an implementation that recomputes the delivered digest inline
    is invisible to the ``hash_file`` counter while still paying the whole-file
    read the threaded digest exists to remove. ``shutil.copy2`` does not route
    through ``read_bytes``, so the retain's own copy is not counted.
    """
    real = Path.read_bytes
    seen: list[Path] = []

    def counting(self: Path) -> bytes:
        seen.append(Path(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    return seen


async def test_vsbb_030_ingest_digests_the_delivered_file_once(
    ingestion_service, tmp_vault_dir, tmp_path, monkeypatch
):
    """An external ingest takes exactly one digest of the caller's file.

    The service must hash before retaining -- content identity decides whether
    these bytes are already known -- so the digest it already holds is threaded
    into ``retain_source`` instead of being taken again there.

    A **regression guard, not a discriminator**, and labelled as one because the
    difference matters: on the uncontested filesystem path there was never a
    second digest to remove, so this test passes against the implementation both
    before and after the change (verified by probe). What it pins is that the
    property stays true as retention grows. The tests that actually fail when the
    threaded digest is ignored are VSBB-031 (the collision branch, the one place
    the filesystem binding did re-hash) and VSB-DS-067 (the document-store
    binding, which re-hashed unconditionally).

    The count is asserted ``== 1`` rather than ``<= 1`` so an implementation that
    bypassed ``hash_file`` entirely fails too, and the recorded provenance digest
    is asserted so the single call is confirmed to be the one whose value the
    document carries.
    """
    seen = _count_hashed_paths(monkeypatch)
    external = tmp_path / "external.md"
    external.write_text("# External\n\nBody.")

    result = await ingestion_service.ingest(
        IngestRequest(source=str(external), source_type=SourceType.MARKDOWN)
    )

    assert seen.count(external.resolve()) == 1
    assert result.document.source_content_hash == hash_file(external)


async def test_vsbb_031_collision_retain_still_digests_the_delivered_file_once(
    ingestion_service, tmp_vault_dir, tmp_path, monkeypatch
):
    """The name-collision branch of retention consumes the threaded digest rather
    than taking its own of the delivered file.

    Anti-coincidental-pass: this is the branch that actually re-hashed before the
    change, so it is the one that discriminates. The retained path is asserted to
    carry the disambiguation suffix, which proves the collision branch ran at all
    -- without it, a count of 1 would be satisfied by an ingest that never
    reached the branch. The copy already sitting at the target *is* expected to
    be hashed (it is a different file, and the comparison needs its digest), so
    the digest assertion is on the delivered path alone.

    The whole-file-read assertion closes a rival the digest counter alone cannot
    see: an implementation that consumes the threaded digest for the
    disambiguation *suffix* but recomputes one inline for the *comparison*. It
    produces the right suffix and never calls ``hash_file`` a second time, so it
    passes every other assertion here and in VSBB-029 -- while still loading the
    delivered file whole into memory, which is the cost the threading exists to
    remove.
    """
    storage_root = tmp_vault_dir / "sources"
    squatter = storage_root / "imports" / "external.md"
    squatter.parent.mkdir(parents=True, exist_ok=True)
    squatter.write_text("# Someone else's external.md\n")

    seen = _count_hashed_paths(monkeypatch)
    reads = _count_whole_file_reads(monkeypatch)
    external = tmp_path / "external.md"
    external.write_text("# External\n\nBody.")

    result = await ingestion_service.ingest(
        IngestRequest(source=str(external), source_type=SourceType.MARKDOWN)
    )

    assert result.document.source_path.startswith("imports/external_")
    assert seen.count(external.resolve()) == 1
    assert squatter.resolve() in seen
    assert external.resolve() not in reads, (
        "the delivered file must never be loaded whole to take a digest the caller already computed"
    )


async def test_vsbb_041_ingest_surfaces_a_refused_write_path_as_a_typed_error(
    ingestion_service, tmp_vault_dir, tmp_path
):
    """A destination the binding refuses surfaces as a typed API error on the
    ingest path, not as a bare ``ValueError``.

    Anti-coincidental-pass: the assertion is on the exception *class*, because
    that is the whole claim. The binding raises its own ``ValueError`` subclass —
    it sits below the API layer and may not import it — and an untranslated
    refusal reaches an MCP caller as a generic internal error and an HTTP caller
    as a bare 500, neither of which the spec declares. ``pytest.raises(Exception)``
    would pass against exactly the defect this guards.

    The restore path was given this translation in an earlier pass; the guards
    that made retention able to refuse then created the same exposure on the far
    more reachable ingest path.
    """
    storage_root = tmp_vault_dir / "sources"
    (storage_root / "imports").mkdir(parents=True, exist_ok=True)
    (storage_root / "imports" / "refused.md").symlink_to(tmp_path / "nowhere.md")

    external = tmp_path / "refused.md"
    external.write_text("# body\n")

    with pytest.raises(VaultSourcePathRefusedError) as excinfo:
        await ingestion_service.ingest(
            IngestRequest(source=str(external), source_type=SourceType.MARKDOWN)
        )

    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "vault_source_path_refused"


async def test_vsbb_060_reuse_does_not_treat_a_symlinked_path_as_a_retained_copy(
    ingestion_service, tmp_vault_dir, tmp_path
):
    """A re-delivery does not reuse a recorded path that has become a link.

    The third surface of the same blind spot: ``source_exists`` resolves
    through a link, so the reuse short-circuit saw a present retained copy and
    handed the path back untouched. The record went on naming a location the
    write side refuses, and the re-delivery that could have surfaced it instead
    confirmed it.

    Anti-coincidental-pass: the bytes behind the link are byte-identical to the
    ones re-delivered, which closes the short-circuit's other two fall-throughs
    -- no document carries these bytes, and the copy is gone -- so nothing but
    the link question can change the outcome. The ingest is forced, because an
    unforced one is declined by duplicate detection downstream and the reuse
    would never be the discriminator. Before the check, this call succeeds and
    returns the linked path; asserting the refusal *type* is what distinguishes
    that from any other decline.
    """
    storage_root = tmp_vault_dir / "sources"
    (storage_root / "imports").mkdir(parents=True, exist_ok=True)

    external = tmp_path / "reused.md"
    external.write_text("# body\n")
    first = await ingestion_service.ingest(
        IngestRequest(source=str(external), source_type=SourceType.MARKDOWN)
    )
    retained = storage_root / first.document.source_path

    # Something other than SAGE swaps the retained copy for a link to a file
    # holding the very same bytes -- the state the write-side guards refuse to
    # create and the read side could not see.
    outside = tmp_path / "outside" / "reused.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(retained.read_bytes())
    retained.unlink()
    retained.symlink_to(outside)

    with pytest.raises(VaultSourcePathRefusedError):
        await ingestion_service.ingest(
            IngestRequest(source=str(external), source_type=SourceType.MARKDOWN, force=True)
        )

    assert retained.is_symlink(), "a refusal must not disturb the tree"
    assert outside.read_bytes() == external.read_bytes()


async def test_vsbb_067_reuse_does_not_treat_an_out_of_root_path_as_a_retained_copy(
    ingestion_service, tmp_vault_dir, tmp_path
):
    """A re-delivery does not reuse a recorded path that has left the source tree.

    The containment counterpart of the case above, and the same blind spot in
    the same place: ``source_exists`` resolves through an ancestor as readily as
    through a leaf link, so the reuse short-circuit saw a present retained copy
    and handed back a path the write side refuses.

    Anti-coincidental-pass: the recorded path's own final component is an
    ordinary file, asserted so, which means the link question answers False here
    and cannot be what produces the refusal. The bytes behind the ancestor are
    byte-identical to the ones re-delivered, which closes the short-circuit's
    other fall-throughs -- no document carries these bytes, and the copy is gone
    -- so nothing but the containment question can change the outcome. The
    ingest is forced, because an unforced one is declined by duplicate detection
    downstream and the reuse would never be the discriminator.
    """
    storage_root = tmp_vault_dir / "sources"
    (storage_root / "imports").mkdir(parents=True, exist_ok=True)

    external = tmp_path / "escaped.md"
    external.write_text("# body\n")
    first = await ingestion_service.ingest(
        IngestRequest(source=str(external), source_type=SourceType.MARKDOWN)
    )
    retained = storage_root / first.document.source_path

    # An operator re-points ``imports/`` at a directory outside the vault's
    # source tree, carrying the retained copy with it. The leaf stays an
    # ordinary file; only the way to it now leaves the root.
    outside = tmp_path / "outside" / "imports"
    outside.mkdir(parents=True, exist_ok=True)
    (outside / retained.name).write_bytes(retained.read_bytes())
    shutil.rmtree(storage_root / "imports")
    (storage_root / "imports").symlink_to(outside)
    moved = outside / retained.name
    assert moved.is_file() and not moved.is_symlink(), "the leaf must stay an ordinary file"

    with pytest.raises(VaultSourcePathRefusedError):
        await ingestion_service.ingest(
            IngestRequest(source=str(external), source_type=SourceType.MARKDOWN, force=True)
        )

    assert moved.read_bytes() == external.read_bytes(), "a refusal must not disturb the tree"


async def test_vsbb_046_refusal_detail_carries_the_external_path_as_supplied(
    ingestion_service, tmp_vault_dir, tmp_path
):
    """The refusal's ``source_path`` detail is the caller's spelling, not the
    realpath the service resolved it to.

    The detail exists so a caller can tell *which* of the paths it named was
    refused. Reporting a resolved form answers a question nobody asked: the
    caller cannot match it against anything it sent, and it discloses a
    filesystem layout the caller may not otherwise see.

    Anti-coincidental-pass: the fixture reaches the file through a symlinked
    *parent directory*, so the supplied and resolved spellings genuinely differ.
    A file named directly under ``tmp_path`` would not discriminate — pytest
    hands out an already-realpath'd ``tmp_path`` on macOS, so supplied and
    resolved are the same string there and the assertion would pass against the
    defect. Equality rather than a suffix match, since both spellings end in the
    same basename.

    VSBB-041's sibling: that test pins the exception class, this one its payload.
    """
    storage_root = tmp_vault_dir / "sources"
    (storage_root / "imports").mkdir(parents=True, exist_ok=True)
    # Dangling, so retention falls through to the write exit and refuses there.
    (storage_root / "imports" / "note.md").symlink_to(tmp_path / "nowhere.md")

    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "note.md").write_text("# body\n")
    (tmp_path / "alias").symlink_to(real_dir)
    supplied = str(tmp_path / "alias" / "note.md")
    assert supplied != str(Path(supplied).resolve()), "the fixture must separate the two spellings"

    with pytest.raises(VaultSourcePathRefusedError) as excinfo:
        await ingestion_service.ingest(
            IngestRequest(source=supplied, source_type=SourceType.MARKDOWN)
        )

    assert excinfo.value.detail == {"source_path": supplied}


async def test_vsbb_045_refusal_detail_carries_a_relative_source_as_supplied(
    ingestion_service, tmp_vault_dir, tmp_path
):
    """A refused *relative* source reports the vault-relative string the caller
    sent, not the storage-root-absolute path the service built from it.

    The relative branch joins the caller's string onto the storage root and
    resolves it, so what came back named a server-side location. See
    ``IngestionService.ingest``'s ``caller_source`` parameter for why that is
    the wrong answer.

    Anti-coincidental-pass: two rivals, and the fixture must exclude both.

    The first is reporting the resolved path — the *outside* link target's
    absolute form, which ends in the same basename, so an ``endswith`` or
    ``in`` assertion would pass against it. Equality is what excludes it.

    The second is any implementation that round-trips the caller's string
    through ``Path`` — ``str(Path(request.source))`` — which is why the source
    here is spelled ``./imports/link.md`` rather than the plain form. ``pathlib``
    drops a leading ``.`` on construction, so that rival silently hands back a
    spelling the caller did not send, and every fixture using a spelling
    ``Path()`` leaves unchanged passes against it. The string survives only
    because the service carries ``request.source`` as a string and never
    re-derives it, which is the property under test. VSBB-048 uses the same
    dotted spelling for the provenance-lookup leg.

    The fixture also carries retention to its reuse exit through a real ingest:
    the caller's relative path resolves through the link to a file outside the
    root, so retention treats it as external, plans ``imports/link.md`` for it,
    finds that path occupied by bytes that hash equal (they are the same file),
    and refuses to hand a symlink back as a record's ``source_path``.
    """
    storage_root = tmp_vault_dir / "sources"
    (storage_root / "imports").mkdir(parents=True, exist_ok=True)

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside = outside_dir / "link.md"
    outside.write_text("# body\n")
    (storage_root / "imports" / "link.md").symlink_to(outside)

    with pytest.raises(VaultSourcePathRefusedError) as excinfo:
        await ingestion_service.ingest(
            IngestRequest(source="./imports/link.md", source_type=SourceType.MARKDOWN)
        )

    assert excinfo.value.detail == {"source_path": "./imports/link.md"}


async def test_vsbb_049_refusal_message_names_the_vault_relative_destination(
    ingestion_service, tmp_vault_dir, tmp_path
):
    """The refusal's ``message`` describes the refused destination as the
    vault-relative path, not the server's absolute one.

    VSBB-046 pinned the ``detail`` half; this is the ``message`` half, which is
    the binding's own text forwarded verbatim. Under a hosted profile an
    absolute path in it discloses the container's filesystem layout to any
    caller who trips a refusal, and the same string reaches bulk callers
    through the per-item error summary.

    Anti-coincidental-pass: both halves of the message assertion are needed.
    The absolute form ends in ``imports/refused.md`` too, so the positive
    assertion alone passes against the disclosing message; the negative one --
    the vault directory's absolute string is absent -- is the discriminator.
    The ``detail`` assertion pins that the other half did not regress.
    """
    storage_root = tmp_vault_dir / "sources"
    (storage_root / "imports").mkdir(parents=True, exist_ok=True)
    (storage_root / "imports" / "refused.md").symlink_to(tmp_path / "nowhere.md")

    external = tmp_path / "refused.md"
    external.write_text("# body\n")

    with pytest.raises(VaultSourcePathRefusedError) as excinfo:
        await ingestion_service.ingest(
            IngestRequest(source=str(external), source_type=SourceType.MARKDOWN)
        )

    assert "imports/refused.md" in excinfo.value.message
    assert str(tmp_vault_dir) not in excinfo.value.message
    assert excinfo.value.detail == {"source_path": str(external)}


async def test_vsbb_050_directory_at_the_planned_destination_is_a_typed_refusal(
    ingestion_service, tmp_vault_dir, tmp_path
):
    """A directory at the path retention planned surfaces as the typed
    ``vault_source_path_refused`` error, not as an unhandled exception.

    The planned path is hashed to decide between reuse and disambiguation
    before any guard runs, so a directory there raised ``IsADirectoryError``
    past every translation -- a bare 500 against a spec that declares neither.

    Anti-coincidental-pass: the class assertion is the claim;
    ``pytest.raises(Exception)`` passes against the defect. The tree
    assertions exclude a rival that swallowed the error and disambiguated past
    the directory: it stays empty and no ``note_<token>.md`` appears.
    """
    storage_root = tmp_vault_dir / "sources"
    squatter = storage_root / "imports" / "note.md"
    squatter.mkdir(parents=True)

    body = b"# body\n"
    external = tmp_path / "note.md"
    external.write_bytes(body)

    with pytest.raises(VaultSourcePathRefusedError) as excinfo:
        await ingestion_service.ingest(
            IngestRequest(source=str(external), source_type=SourceType.MARKDOWN)
        )

    assert excinfo.value.status_code == 400
    assert squatter.is_dir()
    assert list(squatter.iterdir()) == []
    token = hashlib.sha256(body).hexdigest()[:8]
    assert not (storage_root / "imports" / f"note_{token}.md").exists()


async def test_vsbb_051_dangling_imports_link_is_a_typed_refusal(
    ingestion_service, tmp_vault_dir, tmp_path
):
    """A dangling symlink where ``imports/`` should be surfaces as the typed
    refusal, not as the bare ``FileExistsError`` its ``mkdir`` raises.

    Anti-coincidental-pass: the link must dangle -- a live link to a directory
    passes the ``mkdir`` and is refused later by containment (VSBB-039), which
    would leave the new branch unexercised. The target's continued absence
    excludes an implementation that created the directory through the link.
    """
    storage_root = tmp_vault_dir / "sources"
    gone = tmp_path / "gone"
    (storage_root / "imports").symlink_to(gone)

    external = tmp_path / "n.md"
    external.write_text("# body\n")

    with pytest.raises(VaultSourcePathRefusedError) as excinfo:
        await ingestion_service.ingest(
            IngestRequest(source=str(external), source_type=SourceType.MARKDOWN)
        )

    assert excinfo.value.status_code == 400
    assert not gone.exists()
    assert (storage_root / "imports").is_symlink()


# --------------------------------------------------------------------------- #
# VSBB-014 / VSBB-015: delivery routes through the port
# --------------------------------------------------------------------------- #


class _SentinelReadStore(FilesystemVaultSourceStore):
    SENTINEL_BYTES = b"SENTINEL-BYTES"

    def source_exists(self, vault_id, storage_root, source_path):
        return True

    def source_size(self, vault_id, storage_root, source_path):
        return len(self.SENTINEL_BYTES)

    def read_source(self, vault_id, storage_root, source_path):
        return self.SENTINEL_BYTES


class _MissingStore(FilesystemVaultSourceStore):
    def source_exists(self, vault_id, storage_root, source_path):
        return False


async def test_vsbb_014_delivery_reads_via_port(
    ingestion_service, graph_store, minimal_config, tmp_vault_dir, monkeypatch
):
    """Inline delivery returns the bytes ``read_source`` produced, not the real
    file's. Anti-coincidental: the real file holds ``# Real…``, so a direct read
    would not base64-encode the sentinel bytes."""
    doc = await _ingest_internal(
        ingestion_service, tmp_vault_dir, "reports/real.md", "# Real\n\nX."
    )

    _patch_store(monkeypatch, _SentinelReadStore(_UNUSED_ROOT))
    documents = DocumentsService(graph_store, minimal_config)

    response = await documents.get_document_with_content(
        doc.id, include_content=True, write_to_path=None
    )

    assert response.content == base64.b64encode(_SentinelReadStore.SENTINEL_BYTES).decode("ascii")


async def test_vsbb_015_delivery_missing_via_port(
    ingestion_service, graph_store, minimal_config, tmp_vault_dir, monkeypatch
):
    """When the store reports the source absent, delivery raises
    ``ContentFileMissingError`` even though the real file is present.
    Anti-coincidental: a direct ``Path.exists()`` on the present file would not
    raise."""
    doc = await _ingest_internal(
        ingestion_service, tmp_vault_dir, "reports/present.md", "# Present\n\nX."
    )
    # The real file is deliberately present.
    assert (tmp_vault_dir / "sources" / doc.source_path).exists()

    _patch_store(monkeypatch, _MissingStore(_UNUSED_ROOT))
    documents = DocumentsService(graph_store, minimal_config)

    with pytest.raises(ContentFileMissingError):
        await documents.get_document_with_content(doc.id, include_content=True, write_to_path=None)


# --------------------------------------------------------------------------- #
# VSBB-024: streaming content delivery routes through the port
# --------------------------------------------------------------------------- #


class _SentinelStreamStore(FilesystemVaultSourceStore):
    SENTINEL_CHUNKS = [b"SENTINEL-", b"CHUNKS"]

    def source_exists(self, vault_id, storage_root, source_path):
        return True

    def source_size(self, vault_id, storage_root, source_path):
        return sum(len(c) for c in self.SENTINEL_CHUNKS)

    def iter_source(self, vault_id, storage_root, source_path):
        yield from self.SENTINEL_CHUNKS

    def read_source(self, vault_id, storage_root, source_path):
        raise AssertionError("buffered read on the streaming path")


async def test_vsbb_024_content_delivery_via_port(
    ingestion_service, graph_store, minimal_config, tmp_vault_dir, monkeypatch
):
    """Streaming content delivery yields exactly the chunks ``iter_source``
    produced, and never touches the buffered ``read_source``. Anti-coincidental:
    the real file holds ``# Real…`` (a direct read would surface it), and the
    sentinel store's ``read_source`` raises, so any whole-bytes fallback fails
    loudly."""
    doc = await _ingest_internal(
        ingestion_service, tmp_vault_dir, "reports/stream.md", "# Real\n\nX."
    )

    _patch_store(monkeypatch, _SentinelStreamStore(_UNUSED_ROOT))
    documents = DocumentsService(graph_store, minimal_config)

    delivery = await documents.get_document_content(doc.id)

    assert list(delivery.chunks) == _SentinelStreamStore.SENTINEL_CHUNKS
    assert delivery.size == sum(len(c) for c in _SentinelStreamStore.SENTINEL_CHUNKS)
    assert delivery.filename == "stream.md"
    assert delivery.media_type == "text/markdown"


# --------------------------------------------------------------------------- #
# VSBB-016 / VSBB-017: integrity audit routes through the port
# --------------------------------------------------------------------------- #


class _SentinelHashStore(FilesystemVaultSourceStore):
    SENTINEL_HASH = "sha256:" + "0" * 64

    def source_exists(self, vault_id, storage_root, source_path):
        return True

    def hash_source(self, vault_id, storage_root, source_path):
        return self.SENTINEL_HASH


def _maintenance(vault_id, graph_store, config, content_store):
    return MaintenanceService(
        vault_id=vault_id,
        graph_store=graph_store,
        config=config,
        registry_service=None,
        content_store=content_store,
    )


async def test_vsbb_016_audit_hash_via_port(
    ingestion_service, graph_store, minimal_config, tmp_vault_dir, stub_content_store, monkeypatch
):
    """``verify_vault_source_files(check_hashes=True)`` reports a hash mismatch
    carrying the value ``hash_source`` returned. Anti-coincidental: hashing the
    real (matching) file would yield no entry."""
    await _ingest_internal(ingestion_service, tmp_vault_dir, "reports/h.md", "# H\n\nX.")

    _patch_store(monkeypatch, _SentinelHashStore(_UNUSED_ROOT))
    maintenance = _maintenance(
        minimal_config.vault.id, graph_store, minimal_config, stub_content_store
    )

    report = await maintenance.verify_vault_source_files(check_hashes=True)

    assert report.summary["hash_mismatch"] == 1
    entry = next(e for e in report.entries if e.integrity_status == "hash_mismatch")
    assert entry.observed_content_hash == _SentinelHashStore.SENTINEL_HASH


async def test_vsbb_017_audit_missing_via_port(
    ingestion_service, graph_store, minimal_config, tmp_vault_dir, stub_content_store, monkeypatch
):
    """When the store reports a source absent, the audit classifies it
    ``missing`` even though the real file exists. Anti-coincidental: a direct
    ``Path.exists()`` on the present file would report it healthy."""
    doc = await _ingest_internal(ingestion_service, tmp_vault_dir, "reports/m.md", "# M\n\nX.")
    assert (tmp_vault_dir / "sources" / doc.source_path).exists()

    _patch_store(monkeypatch, _MissingStore(_UNUSED_ROOT))
    maintenance = _maintenance(
        minimal_config.vault.id, graph_store, minimal_config, stub_content_store
    )

    report = await maintenance.verify_vault_source_files(check_hashes=False)

    assert report.summary["missing"] == 1
    assert any(e.integrity_status == "missing" for e in report.entries)


# --------------------------------------------------------------------------- #
# VSBB-018 / 019 / 020 / 021: projection & repair route through the port
# --------------------------------------------------------------------------- #


class _SentinelProjectStore(FilesystemVaultSourceStore):
    """Reports every source present and yields sentinel markdown the local file
    could never produce, so a projection reflecting the sentinel proves the read
    routed through ``read_source``. ``retain_source`` deliberately materializes no
    local copy, forcing the post-retain projection down the port path."""

    SENTINEL_TITLE = "PROJECT-SENTINEL"
    SENTINEL_BYTES = b"# PROJECT-SENTINEL\n\nRouted through the port.\n"
    RETAINED = "imports/sentinel.md"

    def retain_source(self, vault_id, storage_root, source_path, delivered_hash=None):
        return self.RETAINED

    def source_exists(self, vault_id, storage_root, source_path):
        return True

    def read_source(self, vault_id, storage_root, source_path):
        return self.SENTINEL_BYTES


class _RaisingReadStore(FilesystemVaultSourceStore):
    """``read_source`` raises: proves the local-copy fast path never calls the
    port when a local source file is present."""

    def source_exists(self, vault_id, storage_root, source_path):
        return True

    def read_source(self, vault_id, storage_root, source_path):
        raise AssertionError("read_source must not be called when a local copy exists")


async def test_vsbb_018_ingest_projection_via_port(
    ingestion_service, tmp_vault_dir, tmp_path, monkeypatch
):
    """The ingest-time projection reads through ``read_source`` when no local copy
    exists: the document's title is the sentinel markdown's heading, not the
    external file's. Anti-coincidental: the store materializes no local file, so a
    direct ``adapter.project(storage_root / vault_relative)`` would hit a missing
    path and fail."""
    _patch_store(monkeypatch, _SentinelProjectStore(_UNUSED_ROOT))
    external = tmp_path / "external.md"
    external.write_text("# External Heading\n\nlocal body")

    result = await ingestion_service.ingest(
        IngestRequest(source=str(external), source_type=SourceType.MARKDOWN)
    )

    assert result.document.title == _SentinelProjectStore.SENTINEL_TITLE


async def test_vsbb_019_recompute_projection_via_port(
    ingestion_service, tmp_vault_dir, monkeypatch
):
    """``recompute_pipeline`` re-projects through the port after the local source
    copy is gone (the post-restart cloud condition). Anti-coincidental: with the
    local file deleted, the pre-port code raised ``SourceFileNotFoundError``;
    routing through ``read_source`` lets the re-projection succeed."""
    doc = await _ingest_internal(ingestion_service, tmp_vault_dir, "reports/r.md", "# Real\n\nX.")
    (tmp_vault_dir / "sources" / doc.source_path).unlink()

    _patch_store(monkeypatch, _SentinelProjectStore(_UNUSED_ROOT))
    result = await ingestion_service.recompute_pipeline(doc.id)

    assert result["status"] == "recompute_pipeline_started"


async def test_vsbb_020_reproject_from_source_via_port(
    ingestion_service, tmp_vault_dir, monkeypatch
):
    """``_reproject_from_source`` re-projects through the port after the local
    copy is gone, returning a projection built from the sentinel bytes.
    Anti-coincidental: the returned projection's title is the sentinel heading,
    which only ``read_source`` could supply once the local file is deleted."""
    doc = await _ingest_internal(ingestion_service, tmp_vault_dir, "reports/p.md", "# Real\n\nX.")
    (tmp_vault_dir / "sources" / doc.source_path).unlink()

    _patch_store(monkeypatch, _SentinelProjectStore(_UNUSED_ROOT))
    projection = await ingestion_service._reproject_from_source(doc.id)

    assert projection.title == _SentinelProjectStore.SENTINEL_TITLE


async def test_vsbb_021_local_copy_short_circuits_the_port(
    ingestion_service, tmp_vault_dir, monkeypatch
):
    """When a local source copy is present, projection reads it directly and never
    calls the port. Anti-coincidental: the store's ``read_source`` raises, so a
    re-projection that always staged through the port would surface that error;
    success proves the ``exists()`` short-circuit holds (and the filesystem
    binding's direct read is not regressed)."""
    doc = await _ingest_internal(
        ingestion_service, tmp_vault_dir, "reports/keep.md", "# Keep\n\nX."
    )
    assert (tmp_vault_dir / "sources" / doc.source_path).exists()

    _patch_store(monkeypatch, _RaisingReadStore(_UNUSED_ROOT))
    projection = await ingestion_service._reproject_from_source(doc.id)

    assert projection.title == "Keep"


# --------------------------------------------------------------------------- #
# VSBB-022 / VSBB-023: ingest resolves a relative source through the port
# --------------------------------------------------------------------------- #


class _BackendOnlyStore(FilesystemVaultSourceStore):
    """A relative source present in the backing store with no local mirror --
    the post-restart document-store-binding condition (CAS-ADR-043). Reports the
    source present, yields sentinel bytes, and fails loudly if the service tries
    to re-retain (re-upload) an already-retained relative source."""

    SENTINEL_TITLE = "BACKEND-ONLY-SENTINEL"
    SENTINEL_BYTES = b"# BACKEND-ONLY-SENTINEL\n\nFetched from the backend.\n"

    def source_exists(self, vault_id, storage_root, source_path):
        return True

    def read_source(self, vault_id, storage_root, source_path):
        return self.SENTINEL_BYTES

    def retain_source(self, vault_id, storage_root, source_path, delivered_hash=None):
        raise AssertionError("retain_source must not run for an already-retained relative source")


async def test_vsbb_042_backend_resident_source_path_is_normalized(
    ingestion_service, tmp_vault_dir, monkeypatch
):
    """The one branch that records the caller's relative string stores a plain
    path, and refuses one that walks out of the tree.

    This is the only ingest branch that does not derive its recorded
    ``source_path`` from a resolved location, so a `.` or `..` segment reaches
    the record verbatim. The two sides then disagree: existence, hashing, and
    the integrity audit all resolve such a path happily, while the write-time
    guard refuses it -- leaving a record naming a location its own bytes could
    never be restored to.

    Anti-coincidental-pass: both halves are asserted, because either alone is
    satisfiable by the wrong implementation. Normalizing without refusing lets
    ``imports/../../escape.md`` through as ``../escape.md``; refusing without
    normalizing leaves ``./imports/x.md`` on the record. The accepted case
    asserts the *stored* value rather than merely that the call succeeded, so an
    implementation that validated and then recorded the raw string still fails.
    """
    _patch_store(monkeypatch, _BackendOnlyStore(_UNUSED_ROOT))

    result = await ingestion_service.ingest(
        IngestRequest(source="./imports/resident.md", source_type=SourceType.MARKDOWN)
    )
    assert result.document.source_path == "imports/resident.md"

    with pytest.raises(VaultSourcePathRefusedError):
        await ingestion_service.ingest(
            IngestRequest(source="imports/../../escape.md", source_type=SourceType.MARKDOWN)
        )


async def test_vsbb_044_legacy_dotted_record_is_found_not_duplicated(
    ingestion_service, graph_store, tmp_vault_dir, monkeypatch
):
    """A record whose stored source_path predates normalization is still found,
    so re-projecting it fails loudly instead of landing a second document.

    Normalizing the recorded path made it diverge from what pre-normalization
    records hold, and the provenance lookup runs on the normalized form. Looking
    up only that spelling misses such a record, and a miss here raises nothing:
    provenance falls back to the stored digest, the re-projection acquires a
    different identity from the document it is re-projecting, and duplicate
    detection lands a *second* document on the same stored file.

    Anti-coincidental-pass: the assertion is the document count together with the
    error type, because the defective path succeeds. A `pytest.raises` alone
    would pass against neither implementation cleanly and a success assertion
    would pass against the defect, which silently returns a new document. The
    raised error is the cross-document path-mismatch guard doing its job on a
    record whose stored path genuinely no longer matches the computed one --
    which is as far as a lookup fix can get. Making such a record
    *re-projectable* needs the stored paths normalized once, which is tracked
    separately.
    """
    first = await _ingest_internal(
        ingestion_service, tmp_vault_dir, "imports/legacy.md", "# Legacy\n\nBody."
    )
    # The pre-normalization spelling of the record's own path.
    await graph_store.update_document(first.id, {"source_path": "./imports/legacy.md"})
    (tmp_vault_dir / "sources" / "imports" / "legacy.md").unlink()
    _patch_store(monkeypatch, _BackendOnlyStore(_UNUSED_ROOT))

    with pytest.raises(ForceReingestPathMismatchError):
        await ingestion_service.ingest(
            IngestRequest(source="./imports/legacy.md", source_type=SourceType.MARKDOWN, force=True)
        )

    assert len(await graph_store.list_all_documents()) == 1, (
        "a missed provenance lookup inserts a second document on the same stored "
        "file, which is the outcome the raw-string fallback exists to prevent"
    )


async def test_vsbb_048_normalized_record_is_found_under_a_dotted_caller_string(
    ingestion_service, graph_store, tmp_vault_dir, monkeypatch
):
    """The mirror of VSBB-044: a record stored in the normalized spelling is
    still found when the caller names it with a dotted one.

    The provenance lookup consults two spellings because either side may carry
    the ``.``. VSBB-044 covers the case where the *record* holds the dotted form
    and the caller matches it, which the raw-string leg answers. This is the
    other half -- an ordinary, post-normalization record re-projected by a
    caller who typed ``./`` -- which only the normalized leg answers.

    Anti-coincidental-pass: the two tests pin opposite legs, and neither is
    redundant. A lookup narrowed to the raw caller string passes VSBB-044 and
    fails here; one narrowed to the normalized form does the reverse. The
    assertion is the error type *together with* the document count, because the
    defective path succeeds rather than raising: the lookup misses, provenance
    falls back to the digest of the sentinel bytes the backend yields, no hash
    matches, and a second document lands on the same stored file. A
    ``pytest.raises`` alone would not show that, and a success assertion would
    pass against the defect outright.
    """
    first = await _ingest_internal(
        ingestion_service, tmp_vault_dir, "imports/plain.md", "# Plain\n\nBody."
    )
    assert first.source_path == "imports/plain.md", "the record must hold the normalized spelling"
    (tmp_vault_dir / "sources" / "imports" / "plain.md").unlink()
    _patch_store(monkeypatch, _BackendOnlyStore(_UNUSED_ROOT))

    with pytest.raises(DuplicateContentError):
        await ingestion_service.ingest(
            IngestRequest(source="./imports/plain.md", source_type=SourceType.MARKDOWN)
        )

    assert len(await graph_store.list_all_documents()) == 1, (
        "a missed provenance lookup lands a second document on the same stored file"
    )


async def test_vsbb_049_migration_makes_a_legacy_dotted_record_reprojectable(
    ingestion_service, graph_store, minimal_config, stub_content_store, tmp_vault_dir, monkeypatch
):
    """VSBB-044's record, once the migration has run, re-projects instead of
    raising -- and re-projects as itself.

    The lookup half made the legacy record *found*, which turned silent
    duplication into a loud refusal but left the refusal with no remedy: the
    stored path genuinely differs from the computed one, which is the exact
    condition the cross-document guard exists to reject. Only rewriting the
    stored path can clear it, and this is the claim that the rewrite does.

    Anti-coincidental-pass: the ``pytest.raises`` before the migration is the
    control -- without it, a test asserting only the success afterwards would
    pass against a build where the guard never fired at all, proving nothing
    about the repair. Afterwards the assertion is the *identity* of the returned
    document together with the vault's document count, not merely that no error
    was raised: a re-projection landing a second document on the same stored
    file also raises nothing, and that silent duplication is the outcome the
    guard was installed to refuse in the first place.
    """
    first = await _ingest_internal(
        ingestion_service, tmp_vault_dir, "imports/legacy2.md", "# Legacy2\n\nBody."
    )
    # The pre-normalization spelling of the record's own path.
    await graph_store.update_document(first.id, {"source_path": "./imports/legacy2.md"})
    (tmp_vault_dir / "sources" / "imports" / "legacy2.md").unlink()
    _patch_store(monkeypatch, _BackendOnlyStore(_UNUSED_ROOT))

    with pytest.raises(ForceReingestPathMismatchError):
        await ingestion_service.ingest(
            IngestRequest(
                source="./imports/legacy2.md", source_type=SourceType.MARKDOWN, force=True
            )
        )

    report = await _maintenance(
        minimal_config.vault.id, graph_store, minimal_config, stub_content_store
    ).migrate_vault()
    assert [e.document_id for e in report.source_paths_normalized] == [first.id]

    result = await ingestion_service.ingest(
        IngestRequest(source="./imports/legacy2.md", source_type=SourceType.MARKDOWN, force=True)
    )

    assert result.document.id == first.id
    assert result.document.source_path == "imports/legacy2.md"
    assert len(await graph_store.list_all_documents()) == 1


class _AbsentBackendStore(FilesystemVaultSourceStore):
    """Reports every relative source absent from the store."""

    def source_exists(self, vault_id, storage_root, source_path):
        return False


async def test_vsbb_022_ingest_relative_backend_source_resolves(
    ingestion_service, tmp_vault_dir, monkeypatch
):
    """A relative ``source`` present only in the backing store resolves through
    the port instead of 404'ing on a raw local ``Path.exists()`` gate
    (CAS-ADR-043). The document's title is the sentinel backend markdown's
    heading, proving the bytes were read via ``read_source``; its
    ``source_path`` is the relative input recorded verbatim. Anti-coincidental:
    with no local mirror the pre-fix code raised ``SourceFileNotFoundError`` at
    the local-disk gate, and the fake's ``retain_source`` raises if the service
    mistreats the relative source as an external file to re-upload."""
    _patch_store(monkeypatch, _BackendOnlyStore(_UNUSED_ROOT))

    result = await ingestion_service.ingest(
        IngestRequest(source="imports/backend_only.md", source_type=SourceType.MARKDOWN)
    )

    assert result.document.title == _BackendOnlyStore.SENTINEL_TITLE
    assert result.document.source_path == "imports/backend_only.md"


async def test_vsbb_023_ingest_relative_absent_source_still_raises(
    ingestion_service, tmp_vault_dir, monkeypatch
):
    """Relocating the existence gate behind the port does not remove it: a
    relative source absent from both the local tree and the store still raises
    ``SourceFileNotFoundError``. Anti-coincidental: were the gate dropped, the
    call would fall through to projection and surface a different failure (or
    none)."""
    _patch_store(monkeypatch, _AbsentBackendStore(_UNUSED_ROOT))

    with pytest.raises(SourceFileNotFoundError):
        await ingestion_service.ingest(
            IngestRequest(source="imports/nope.md", source_type=SourceType.MARKDOWN)
        )
