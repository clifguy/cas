"""Delivered provenance and inspected retained bytes remain independently identified."""

import hashlib
import json

import pytest

from sage.services.discovery_selection import SelectionConfig, selection_identity


@pytest.mark.parametrize("large", [False, True])
def test_retained_byte_hash_is_frozen_and_preserved_under_summary_bounds(large):
    delivered = "sha256:" + hashlib.sha256(b"delivered office bytes").hexdigest()
    retained = "sha256:" + hashlib.sha256(b"retained rewritten office bytes").hexdigest()
    assert delivered != retained
    adapter = {"heading_style_map": {"authored": "x" * 5000}} if large else {}
    identity = selection_identity(
        source_hash=delivered,
        inspected_source_hash=retained,
        adapter_version="adapter:1",
        tokenizer_identity="utf8-byte-tokens:1",
        config=SelectionConfig(),
        adapter_configuration=adapter,
    )
    frozen = json.loads(identity.configuration_json)
    assert frozen["inspected_source_hash"] == retained
    published = identity.processing_identity()
    assert published.source_hash == delivered
    assert published.configuration["inspected_source_hash"] == retained
    assert len(json.dumps(published.configuration).encode()) <= 4096
    if large:
        assert published.configuration["effective_configuration_omitted"] is True
        assert published.configuration["effective_configuration_sha256"] == (
            "sha256:" + hashlib.sha256(identity.configuration_json.encode()).hexdigest()
        )
    changed = selection_identity(
        source_hash=delivered,
        inspected_source_hash="sha256:" + "f" * 64,
        adapter_version="adapter:1",
        tokenizer_identity="utf8-byte-tokens:1",
        config=SelectionConfig(),
        adapter_configuration=adapter,
    )
    assert identity != changed
    assert identity.processing_identity() != changed.processing_identity()


def test_same_byte_direct_selection_still_identifies_inspected_bytes():
    digest = "sha256:" + hashlib.sha256(b"authored bytes").hexdigest()
    identity = selection_identity(
        source_hash=digest,
        adapter_version="adapter:1",
        tokenizer_identity="utf8-byte-tokens:1",
        config=SelectionConfig(),
    )
    assert (
        identity.source_hash
        == "sha256:2dc25e19e7e5138e7b0cab63d7549c5b3b3e939c70cae3258521fd01545a04a2"
    )
    assert json.loads(identity.configuration_json)["inspected_source_hash"] == digest
    assert identity.processing_identity().configuration["inspected_source_hash"] == digest


@pytest.mark.parametrize("digest", ["", "sha256:not-a-digest"])
def test_inspected_hash_refuses_unidentified_retained_bytes(digest):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        selection_identity(
            source_hash="sha256:" + "a" * 64,
            inspected_source_hash=digest,
            adapter_version="adapter:1",
            tokenizer_identity="utf8-byte-tokens:1",
            config=SelectionConfig(),
        )


async def test_retained_hash_never_substitutes_for_the_delivered_publication_fence(
    pg_pool, postgres_graph_store
):
    from sage.adapters.content_store_postgres import PostgresContentStore
    from sage.models.coverage import CoverageDimension
    from tests.sage.test_processing_coverage import _doc

    doc = _doc(1)
    await postgres_graph_store.insert_document(doc)
    retained = "sha256:" + hashlib.sha256(b"rewritten retained bytes").hexdigest()
    assert retained != doc.source_content_hash
    store = PostgresContentStore(pg_pool)
    attempt = await store.begin_processing_attempt(doc.id)
    before = await store.read_processing_artifacts(doc.id)

    def evidence(delivered):
        identity = selection_identity(
            source_hash=delivered,
            inspected_source_hash=retained,
            adapter_version="adapter:1",
            tokenizer_identity="utf8-byte-tokens:1",
            config=SelectionConfig(),
        ).processing_identity()
        return CoverageDimension(scope="complete", identity=identity, unknown_remainder=False)

    with pytest.raises(ValueError, match="coverage source identity mismatch"):
        await store.publish_processing(doc.id, attempt, {"inspection": evidence(retained)})
    assert await store.read_processing_artifacts(doc.id) == before
    assert await store.publish_processing(
        doc.id, attempt, {"inspection": evidence(doc.source_content_hash)}
    )
    coverage, _, _ = await store.read_processing_artifacts(doc.id)
    assert coverage.inspection.generation == attempt
    assert coverage.inspection.identity.source_hash == doc.source_content_hash
    assert coverage.inspection.identity.configuration["inspected_source_hash"] == retained
