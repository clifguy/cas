"""In-memory stand-in for ``SharePointGraphClient``.

Holds config bytes per vault_id and retained source bytes per vault-relative
path, with per-operation counters so a test can prove a cheap stat did not
pull content, a reuse did not re-upload, and a streamed delivery did not fall
back to a whole-file read. Shared across the document-store binding tests and
the MCP profile-invariance suite; one instance carries state across the
per-call store constructions the stack resolver performs.
"""

import hashlib
import io
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

# Chunk size for the fake's streamed reads. Small enough that modest test
# payloads exercise multi-chunk delivery.
STREAM_CHUNK_BYTES = 8192

# Office package extensions the backing document store rewrites at rest. An
# upload of one of these is stored as a *stamped* copy, not verbatim: the
# service mints fresh per-upload identifiers into added ``customXml`` parts, so
# the same bytes uploaded twice yield two different stored digests. Reproducing
# that here is what gives the provenance assertions teeth -- a fake that stored
# every upload verbatim would report the delivered digest by accident.
STAMPED_SUFFIXES = (".docx", ".dotx", ".xlsx", ".pptx")


class FakeGraphClient:
    """In-memory fake of the Graph client's config and source-byte surface."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.uploads = 0
        self.deletes = 0
        self.tree_deletes = 0
        self.deleted_trees: list[str] = []
        self.archives: dict[str, bytes] = {}
        # Source-byte half: vault-relative path -> bytes, with op counters so a
        # test can prove a cheap stat did not pull content and a reuse did not
        # re-upload.
        self.sources: dict[str, bytes] = {}
        self.source_uploads = 0
        self.source_reads = 0
        self.source_stats = 0
        self.source_hashes = 0
        self.source_streams: list[tuple[str, str]] = []
        # Count of uploads the store rewrote on the way in (see
        # STAMPED_SUFFIXES), so a test can prove the divergence it asserts
        # against was actually produced rather than assumed.
        self.stamped_uploads = 0
        # Refusals a test installs to drive the store-refusal paths: an
        # exception raised in place of the operation, the way the real client
        # raises when the store declines. Left None, every operation behaves.
        #
        # The three source-read slots also accept a callable taking
        # ``(vault_id, source_path)`` and returning an exception or None, so a
        # test can refuse one path, or the Nth call, while the rest of a walk
        # succeeds -- which is what lets an assertion distinguish a run that
        # aborted from one that never got started.
        self.refuse_upload: Exception | None = None
        self.refuse_hash: Exception | None = None
        self.refuse_stat: Exception | Callable | None = None
        self.refuse_read: Exception | Callable | None = None
        self.refuse_stream: Exception | Callable | None = None
        self.refuse_download_url: Exception | Callable | None = None

    @staticmethod
    def _refusal_for(slot, vault_id: str, source_path: str) -> Exception | None:
        """The exception ``slot`` calls for on this operation, if any."""
        if slot is None or isinstance(slot, Exception):
            return slot
        return slot(vault_id, source_path)

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

    def delete_tree(self, vault_id: str) -> None:
        # A folder delete removes the whole vault folder: its config and every
        # retained source. Idempotent -- an absent vault is tolerated.
        self.tree_deletes += 1
        self.deleted_trees.append(vault_id)
        self.store.pop(vault_id, None)

    def list_sources(self, vault_id: str) -> list[dict]:
        return [{"path": p, "size": len(d)} for p, d in sorted(self.sources.items())]

    def write_archive(self, archive_path: str, data: bytes) -> None:
        self.archives[archive_path] = data

    # -- source-byte half --------------------------------------------------

    def source_item(self, vault_id: str, source_path: str) -> dict | None:
        # Refused before the counter moves, so a test can prove the refusal
        # fired in place of the stat rather than after it -- and can count the
        # stats that did succeed before it.
        refusal = self._refusal_for(self.refuse_stat, vault_id, source_path)
        if refusal is not None:
            raise refusal
        self.source_stats += 1
        data = self.sources.get(source_path)
        if data is None:
            return None
        return {"name": source_path.rsplit("/", 1)[-1], "size": len(data)}

    def read_source_bytes(self, vault_id: str, source_path: str) -> bytes:
        refusal = self._refusal_for(self.refuse_read, vault_id, source_path)
        if refusal is not None:
            raise refusal
        self.source_reads += 1
        return self.sources[source_path]

    def upload_source(self, vault_id: str, source_path: str, source_file: Path) -> None:
        # Consumed in bounded chunks, as the real client streams a file up:
        # mirroring the path-taking signature is what keeps a binding test from
        # passing against a bytes-taking stand-in the production client no
        # longer has.
        if self.refuse_upload is not None:
            raise self.refuse_upload
        self.source_uploads += 1
        with source_file.open("rb") as f:
            chunks = list(iter(lambda: f.read(STREAM_CHUNK_BYTES), b""))
        self.sources[source_path] = self._store_form(source_path, b"".join(chunks))

    def _store_form(self, source_path: str, data: bytes) -> bytes:
        """The bytes the store retains for an upload of ``data``.

        Verbatim for every format the store leaves alone. For an Office package
        (STAMPED_SUFFIXES) the package is reopened and a fresh
        ``customXml/itemProps<N>.xml`` part is added carrying a per-upload
        identifier, mirroring the item GUIDs the real service mints on each
        upload event: the added part is structurally inert (nothing references
        it, so the package still opens), the authored parts are copied through
        byte-for-byte, and the resulting digest differs from the delivered one
        *and* from the digest of any previous upload of the same bytes.
        """
        lowered = source_path.lower()
        if not lowered.endswith(STAMPED_SUFFIXES):
            return data
        self.stamped_uploads += 1
        marker = f"fake-item-{self.stamped_uploads:08d}"
        buffer = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as incoming:
            taken = set(incoming.namelist())
            index = 1
            while f"customXml/itemProps{index}.xml" in taken:
                index += 1
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as stamped:
                for item in incoming.infolist():
                    stamped.writestr(item, incoming.read(item.filename))
                stamped.writestr(
                    f"customXml/itemProps{index}.xml",
                    f'<ds:datastoreItem ds:itemID="{{{marker}}}"/>',
                )
        return buffer.getvalue()

    def hash_source_bytes(self, vault_id: str, source_path: str) -> str:
        # Counted like every other source-byte operation: a caller that swaps a
        # whole-file read for a streamed digest is only proved to have done so by
        # a test that can see this call happen while source_reads stays at zero.
        if self.refuse_hash is not None:
            raise self.refuse_hash
        self.source_hashes += 1
        return "sha256:" + hashlib.sha256(self.sources[source_path]).hexdigest()

    def stream_source_bytes(self, vault_id: str, source_path: str) -> Iterator[bytes]:
        # Yields the *real* stored bytes in bounded chunks (unlike
        # read_source_bytes, which hands back the whole payload) while
        # recording the call, so a consumer test can assert both the
        # delivered content and that delivery went through the streaming
        # channel rather than a whole-file read.
        refusal = self._refusal_for(self.refuse_stream, vault_id, source_path)
        if refusal is not None:
            raise refusal
        self.source_streams.append((vault_id, source_path))
        data = self.sources[source_path]
        for offset in range(0, len(data), STREAM_CHUNK_BYTES):
            yield data[offset : offset + STREAM_CHUNK_BYTES]

    def source_download_url(self, vault_id: str, source_path: str) -> str | None:
        # A URL mint is a store read like any other and can be declined; a fake
        # that could only ever answer it cannot express the refusal at all.
        refusal = self._refusal_for(self.refuse_download_url, vault_id, source_path)
        if refusal is not None:
            raise refusal
        if source_path not in self.sources:
            return None
        return f"https://sp.example/download/{source_path}?t=fake"
