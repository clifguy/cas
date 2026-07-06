"""In-memory stand-in for ``SharePointGraphClient``.

Holds config bytes per vault_id and retained source bytes per vault-relative
path, with per-operation counters so a test can prove a cheap stat did not
pull content, a reuse did not re-upload, and a streamed delivery did not fall
back to a whole-file read. Shared across the document-store binding tests and
the MCP profile-invariance suite; one instance carries state across the
per-call store constructions the stack resolver performs.
"""

import hashlib
from collections.abc import Iterator

# Chunk size for the fake's streamed reads. Small enough that modest test
# payloads exercise multi-chunk delivery.
STREAM_CHUNK_BYTES = 8192


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
        self.source_streams: list[tuple[str, str]] = []

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

    def stream_source_bytes(self, vault_id: str, source_path: str) -> Iterator[bytes]:
        # Yields the *real* stored bytes in bounded chunks (unlike
        # read_source_bytes, which hands back the whole payload) while
        # recording the call, so a consumer test can assert both the
        # delivered content and that delivery went through the streaming
        # channel rather than a whole-file read.
        self.source_streams.append((vault_id, source_path))
        data = self.sources[source_path]
        for offset in range(0, len(data), STREAM_CHUNK_BYTES):
            yield data[offset : offset + STREAM_CHUNK_BYTES]

    def source_download_url(self, vault_id: str, source_path: str) -> str | None:
        if source_path not in self.sources:
            return None
        return f"https://sp.example/download/{source_path}?t=fake"
