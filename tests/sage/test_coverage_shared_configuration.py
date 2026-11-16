"""Each effective configuration is served once per coverage object (CAS-ADR-059).

The shared block and the per-holder references are derived from the
identities whenever coverage is serialized for a caller. They are never read
from input and never stored, so stored coverage keeps its existing shape and
the stored hashes that key on a whole identity do not move.
"""

import json
from pathlib import Path

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.adapters.interfaces import Chunk
from sage.models.coverage import (
    DIMENSIONS,
    AbstractionQuality,
    CoverageDimension,
    ProcessingCoverage,
    ProcessingIdentity,
    canonical_configuration_hash,
)
from tests.sage.test_postgres_graph_store import _doc

FIXTURE = Path(__file__).parents[1] / "fixtures" / "coverage" / "coverage_3_8_markdown.json"


def _stored() -> dict:
    return json.loads(FIXTURE.read_text())


def _holders(wire: dict) -> dict[str, dict]:
    holders = {name: wire[name] for name in DIMENSIONS if "identity" in wire.get(name, {})}
    if "identity" in wire.get("abstraction_quality", {}):
        holders["abstraction_quality"] = wire["abstraction_quality"]
    return holders


def _quality(identity: ProcessingIdentity) -> AbstractionQuality:
    return AbstractionQuality(
        mode="recording_generated", policy="p1", generation=1, identity=identity
    )


def test_identical_configurations_are_served_once():
    stored = _stored()
    configured = {
        name: stored[name]["identity"]["configuration"]
        for name in DIMENSIONS
        if stored[name].get("identity")
    }
    assert len(configured) == 6
    distinct = {canonical_configuration_hash(c) for c in configured.values()}
    assert len(distinct) == 3, "fixture must hold two shared configurations and an empty one"

    wire = ProcessingCoverage.model_validate(stored).model_dump(mode="json")

    assert set(wire["configurations"]) == distinct
    for name, holder in _holders(wire).items():
        ref = holder["configuration_ref"]
        assert wire["configurations"][ref] == holder["identity"]["configuration"], name
        assert ref == canonical_configuration_hash(configured[name])


def test_every_holder_recovers_its_exact_configuration_from_block_and_ref():
    stored = _stored()
    distinct = dict(stored["inspection"]["identity"]["configuration"], extra_setting=7)
    stored["inspection"]["identity"]["configuration"] = distinct
    coverage = ProcessingCoverage.model_validate(stored)
    identity = coverage.projection.identity
    assert identity is not None
    coverage.abstraction_quality = _quality(identity)

    wire = coverage.model_dump(mode="json")

    holders = _holders(wire)
    assert "abstraction_quality" in holders
    assert wire["configurations"][holders["inspection"]["configuration_ref"]] == distinct
    for name, holder in holders.items():
        assert (
            wire["configurations"][holder["configuration_ref"]]
            == (holder["identity"]["configuration"])
        ), name
    referenced = {holder["configuration_ref"] for holder in holders.values()}
    assert referenced == set(wire["configurations"]), "block holds only referenced entries"


def test_legacy_stored_coverage_reads_and_derives_the_shared_form():
    stored = _stored()
    assert "configurations" not in stored
    assert all("configuration_ref" not in stored[name] for name in DIMENSIONS)

    wire = ProcessingCoverage.model_validate(stored).model_dump(mode="json")

    assert wire["configurations"]
    assert all("configuration_ref" in holder for holder in _holders(wire).values())


def test_supplied_block_and_refs_are_recomputed_not_trusted():
    stored = _stored()
    forged_ref = "sha256:" + "0" * 64
    stored["configurations"] = {forged_ref: {"forged": True}}
    for name in DIMENSIONS:
        stored[name]["configuration_ref"] = forged_ref

    wire = ProcessingCoverage.model_validate(stored).model_dump(mode="json")

    assert forged_ref not in wire["configurations"]
    for name, holder in _holders(wire).items():
        assert holder["configuration_ref"] != forged_ref, name


def test_copies_that_skip_validation_still_serve_a_current_block():
    coverage = ProcessingCoverage.model_validate(_stored())
    changed = coverage.projection.model_copy(
        update={
            "identity": coverage.projection.identity.model_copy(
                update={"configuration": {"changed": 1}}
            )
        }
    )
    copied = coverage.model_copy(update={"projection": changed})

    wire = copied.model_dump(mode="json")

    ref = wire["projection"]["configuration_ref"]
    assert wire["configurations"][ref] == {"changed": 1}


def test_unknown_coverage_carries_no_block_or_refs():
    wire = ProcessingCoverage().model_dump(mode="json")
    assert "configurations" not in wire
    assert all("configuration_ref" not in wire[name] for name in DIMENSIONS)


def test_storage_form_omits_the_derived_fields():
    coverage = ProcessingCoverage.model_validate(_stored())
    stored = coverage.storage_dump()
    assert stored == ProcessingCoverage.model_validate(_stored()).storage_dump()
    assert "configurations" not in stored
    assert all("configuration_ref" not in stored[name] for name in DIMENSIONS)
    assert stored["inspection"] == _stored()["inspection"]


def test_identity_dump_and_the_ledger_keys_on_it_are_unchanged():
    stored = _stored()
    coverage = ProcessingCoverage.model_validate(stored)
    for name in DIMENSIONS:
        identity = getattr(coverage, name).identity
        if identity is None:
            continue
        assert identity.model_dump(mode="json") == stored[name]["identity"], name
        # Embedding accounting and the abstraction allowance ledger both key on
        # this hash of the whole identity dump.
        assert canonical_configuration_hash(identity.model_dump(mode="json")) == (
            canonical_configuration_hash(stored[name]["identity"])
        ), name


def test_the_whole_object_bound_is_measured_on_the_stored_form():
    stored = _stored()
    for name in DIMENSIONS:
        identity = stored[name].get("identity")
        if identity:
            identity["configuration"] = {
                **{f"k{i}": "x" * 100 for i in range(30)},
                "which": name,
            }
            stored[name]["limitations"] = [f"{name}-{i}-" + "y" * 400 for i in range(16)]
    coverage = ProcessingCoverage.model_validate(stored)
    stored_size = len(json.dumps(coverage.storage_dump()).encode())
    served_size = len(coverage.model_dump_json().encode())
    assert stored_size < 65536 < served_size


async def test_published_coverage_is_stored_without_the_derived_fields(
    pg_pool, postgres_graph_store
):
    await postgres_graph_store.insert_document(_doc(1))
    store = PostgresContentStore(pg_pool)
    identity = ProcessingIdentity(
        source_hash=_doc(1).source_content_hash,
        adapter="markdown:1",
        policy="coverage-v1",
        configuration={"shared": True},
        tokenization="unknown",
    )
    attempt = await store.begin_processing_attempt("00000001_doc_1")
    dimension = CoverageDimension(
        scope="limited",
        identity=identity,
        counts=[{"unit": "characters", "observed": 3, "represented": 3, "total": None}],
        limitations=["inspection_unknown"],
    )
    await store.publish_processing(
        "00000001_doc_1",
        attempt,
        {"projection": dimension, "keyword_indexing": dimension, "passage_embeddings": dimension},
        chunks=[
            Chunk(document_id="00000001_doc_1", heading_path="A", content="text", chunk_index=0)
        ],
    )
    async with pg_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT processing_coverage FROM documents WHERE id = %s", ("00000001_doc_1",)
        )
        (raw,) = await cur.fetchone()
    assert "configurations" not in raw
    assert "configuration_ref" not in raw["projection"]

    served = (await postgres_graph_store.get_document("00000001_doc_1")).processing_coverage
    wire = served.model_dump(mode="json")
    ref = canonical_configuration_hash({"shared": True})
    assert wire["configurations"] == {ref: {"shared": True}}
    assert wire["projection"]["configuration_ref"] == ref
    assert wire["keyword_indexing"]["configuration_ref"] == ref


def test_assignment_keeps_the_derived_fields_and_their_values():
    coverage = ProcessingCoverage.model_validate(_stored())
    identity = coverage.projection.identity
    assert identity is not None

    coverage.abstraction_quality = _quality(identity)
    coverage.projection = coverage.projection.model_copy(update={"limitations": []})

    assert coverage.configurations is None
    assert coverage.projection.configuration_ref is None
    wire = coverage.model_dump(mode="json")
    ref = wire["abstraction_quality"]["configuration_ref"]
    assert wire["configurations"][ref] == identity.configuration


def test_the_served_and_published_schemas_describe_the_shared_form():
    """The served OpenAPI keeps the full coverage schema and both mark the deprecation.

    A return annotation on the coverage serializer would replace the served
    schema with that annotation's type, orphaning the dimension schemas.
    """
    import yaml

    from sage.app import create_app

    served = create_app().openapi()["components"]["schemas"]
    published = yaml.safe_load(
        (Path(__file__).parents[2] / "docs/fs/sage/sage_core_api.openapi.yaml").read_text()
    )["components"]["schemas"]
    for schemas in (served, published):
        coverage = schemas["ProcessingCoverage"]["properties"]
        assert {*DIMENSIONS, "abstraction_quality", "configurations"} <= set(coverage)
        assert "configuration_ref" in schemas["CoverageDimension"]["properties"]
        assert "configuration_ref" in schemas["AbstractionQuality"]["properties"]
        configuration = schemas["ProcessingIdentity"]["properties"]["configuration"]
        assert configuration.get("deprecated") is True


def test_supplied_derived_values_never_reach_the_stored_form():
    """Every holder's supplied reference and a supplied block are discarded on input.

    A merge that re-validates a served dump (as the store's publication paths
    do) would otherwise write the derived fields into storage.
    """
    stored = _stored()
    forged_ref = "sha256:" + "1" * 64
    stored["configurations"] = {forged_ref: {"forged": True}}
    for name in DIMENSIONS:
        stored[name]["configuration_ref"] = forged_ref
    identity = stored["projection"]["identity"]
    stored["abstraction_quality"] = {
        **_quality(ProcessingIdentity.model_validate(identity)).model_dump(mode="json"),
        "configuration_ref": forged_ref,
    }

    coverage = ProcessingCoverage.model_validate(stored)

    assert coverage.configurations is None
    assert coverage.abstraction_quality.configuration_ref is None
    assert all(getattr(coverage, name).configuration_ref is None for name in DIMENSIONS)
    kept = coverage.storage_dump()
    assert "configurations" not in kept
    assert "configuration_ref" not in kept["abstraction_quality"]
    assert all("configuration_ref" not in kept[name] for name in DIMENSIONS)


async def _raw_coverage(pg_pool, document_id: str) -> dict:
    async with pg_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT processing_coverage FROM documents WHERE id = %s", (document_id,)
        )
        (raw,) = await cur.fetchone()
    return raw


def _no_derived_fields(raw: dict) -> bool:
    return "configurations" not in raw and not any(
        "configuration_ref" in value for value in raw.values() if isinstance(value, dict)
    )


async def test_embedding_increments_and_rewrite_invalidation_store_no_derived_fields(
    pg_pool, postgres_graph_store
):
    """The increment and invalidation writers each store the stored form.

    Both read stored coverage, change it and write it back; each is checked
    on its own raw row after a publication whose identities are configured.
    """
    from sage.storage.postgres.schema import EMBEDDING_DIM

    doc = _doc(1)
    await postgres_graph_store.insert_document(doc)
    store = PostgresContentStore(pg_pool)
    identity = ProcessingIdentity(
        source_hash=doc.source_content_hash,
        adapter="markdown:1",
        policy="coverage-v1",
        configuration={"shared": True},
        tokenization="unknown",
    )
    dimension = CoverageDimension(
        scope="limited",
        identity=identity,
        counts=[{"unit": "characters", "observed": 3, "represented": 3, "total": None}],
        limitations=["inspection_unknown"],
    )
    attempt = await store.begin_processing_attempt(doc.id)
    chunks = [Chunk(document_id=doc.id, heading_path="A", content="text", chunk_index=0)]
    assert await store.publish_processing(
        doc.id,
        attempt,
        {
            "inspection": dimension,
            "projection": dimension,
            "keyword_indexing": dimension,
            "passage_embeddings": dimension,
        },
        chunks=chunks,
    )

    vector = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
    assert await store.publish_embedding_increment(doc.id, attempt, {0: vector}, dimension)
    raw = await _raw_coverage(pg_pool, doc.id)
    assert raw["passage_embeddings"]["identity"]["configuration"] == {"shared": True}
    assert _no_derived_fields(raw)

    await store.index_chunks(doc.id, chunks)
    raw = await _raw_coverage(pg_pool, doc.id)
    assert raw["projection"]["limitations"] == ["artifact_rewritten_without_coverage_evidence"]
    assert raw["projection"].get("identity") is None
    # An identity survives the rewrite, so a served dump would carry the block.
    assert raw["inspection"]["identity"]["configuration"] == {"shared": True}
    assert _no_derived_fields(raw)
