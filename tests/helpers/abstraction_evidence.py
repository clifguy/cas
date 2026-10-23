"""Known authored fixture evidence for tests isolating scheduling/provider behavior."""

from sage.models.discovery import EvidencePackage
from sage.services.discovery_selection import select_discovery
from sage.source_adapters.structure import SourceFact


def authored_package(source_hash: str, text: str) -> EvidencePackage:
    return select_discovery(
        [SourceFact("document", "body", "fixture:0", 0, "string", text, text)],
        source_hash=source_hash,
        adapter_version="fixture-authored:1",
        tokenizer=lambda value: len(value.encode("utf-8")),
        tokenizer_identity="utf8-byte-tokens:1",
    )


async def seed_authored_evidence(content_store, doc, text: str) -> None:
    """Supply the same explicit provenance captured by a normal fixture ingest."""
    current = (
        await content_store._processing_store.get_document(doc.id)
        if hasattr(content_store, "_processing_store")
        else doc
    )
    attempt = current.processing_coverage.attempt
    if attempt < 1:
        attempt = await content_store.begin_processing_attempt(doc.id)
    assert await content_store.save_abstraction_evidence(
        doc.id, attempt, authored_package(doc.source_content_hash, text)
    )
