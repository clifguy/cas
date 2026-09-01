"""Regression coverage for the not-found root-cause discriminators (CAS-ADR-039).

A ``document_not_found`` read miss carries a ``detail`` dict with three
orthogonal signals -- ``id_well_formed``, ``ever_existed``, and
``slug_matches_catalog`` -- so a caller can tell a malformed id from a
never-existed id from a real-but-renamed one without a second probing
round-trip. The error ``code`` stays ``document_not_found``; only the
``detail`` differentiates.
"""

import hashlib
from datetime import datetime, timezone

import pytest

from sage.api.errors import DocumentNotFoundError
from sage.models.schemas import Document
from sage.services.documents import DocumentsService
from sage.services.identity import (
    document_id_slug,
    generate_document_id,
    is_well_formed_document_id,
)
from sage.services.read_diagnostics import build_not_found_detail
from sage.services.utilities import UtilitiesService


def _well_formed_id(prefix_seed: str, slug: str) -> str:
    """Build a shape-conformant id with a chosen hash prefix and slug."""
    hash_hex = hashlib.sha256(prefix_seed.encode()).hexdigest()[:8]
    return f"{hash_hex}_{slug}"


def _sha(name: str) -> str:
    return "sha256:" + hashlib.sha256(f"sage-test-hash:{name}".encode()).hexdigest()


def _make_doc(doc_id: str, title: str) -> Document:
    return Document(
        id=doc_id,
        title=title,
        source_type="markdown",
        source_path=f"test/{doc_id}.md",
        lifecycle_status="active",
        source_content_hash=_sha(doc_id),
        adapter_version="1.0",
        created_by="test",
        created_at=datetime.now(timezone.utc),
        last_modified_by="test",
        updated_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def documents_service(graph_store, minimal_config):
    return DocumentsService(graph_store, minimal_config)


@pytest.fixture
def utilities_service(graph_store, stub_content_store, stub_embedding_provider, minimal_config):
    return UtilitiesService(
        graph_store, stub_content_store, stub_embedding_provider, minimal_config
    )


# ---------------------------------------------------------------------------
# Pure helpers (identity.py)
# ---------------------------------------------------------------------------


def test_is_well_formed_document_id_accepts_canonical():
    minted = generate_document_id("a/b.md", "2026-01-01", "Quarterly Report")
    assert is_well_formed_document_id(minted)
    assert is_well_formed_document_id("deadbeef_quarterly_report")


@pytest.mark.parametrize(
    "bad",
    [
        "not a valid id!!",  # spaces and punctuation
        "DEADBEEF_quarterly",  # uppercase hex
        "deadbee_quarterly",  # 7-char prefix
        "deadbeef0_quarterly",  # 9-char prefix
        "deadbeef",  # no slug
        "deadbeef_",  # empty slug
        "deadbeef_Quarterly",  # uppercase slug char
    ],
)
def test_is_well_formed_document_id_rejects_malformed(bad):
    assert not is_well_formed_document_id(bad)


def test_document_id_slug_extracts_suffix():
    assert document_id_slug("deadbeef_quarterly_report") == "quarterly_report"
    # Two ids minted from the same title share a slug despite different prefixes.
    a = _well_formed_id("seed-a", "quarterly_report")
    b = _well_formed_id("seed-b", "quarterly_report")
    assert a != b
    assert document_id_slug(a) == document_id_slug(b) == "quarterly_report"


def test_document_id_slug_returns_none_for_malformed():
    assert document_id_slug("not a valid id!!") is None
    assert document_id_slug("deadbeef") is None


# ---------------------------------------------------------------------------
# build_not_found_detail (read_diagnostics.py) -- the three variants
# ---------------------------------------------------------------------------


async def test_detail_malformed_id(graph_store):
    """(a) A malformed id: not well-formed, never existed, slug undefined."""
    detail = await build_not_found_detail(graph_store, "not a valid id!!")
    assert detail["document_id"] == "not a valid id!!"
    assert detail["id_well_formed"] is False
    assert detail["ever_existed"] is False
    assert detail["slug_matches_catalog"] is False


async def test_detail_never_existed_well_formed(graph_store):
    """A well-formed id whose slug is absent from the catalog: pure typo / wrong vault."""
    detail = await build_not_found_detail(
        graph_store, _well_formed_id("ghost", "completely_unknown_slug")
    )
    assert detail["id_well_formed"] is True
    assert detail["ever_existed"] is False
    assert detail["slug_matches_catalog"] is False


async def test_detail_slug_matches_but_id_differs(graph_store):
    """(c) Real-but-renamed: a catalog doc shares the slug; the queried id's
    hash prefix differs, so the row does not resolve. slug_matches_catalog
    fires -- the 're-resolve by title' nudge."""
    catalog_id = generate_document_id("reports/q3.md", "2026-01-01", "Quarterly Report")
    await graph_store.insert_document(_make_doc(catalog_id, "Quarterly Report"))

    queried_id = _well_formed_id("a-different-version", document_id_slug(catalog_id))
    assert queried_id != catalog_id

    detail = await build_not_found_detail(graph_store, queried_id)
    assert detail["id_well_formed"] is True
    assert detail["ever_existed"] is False
    assert detail["slug_matches_catalog"] is True


async def test_detail_ever_existed_reflects_catalog_presence(graph_store):
    """ever_existed reports honest catalog presence. Supersession updates the
    predecessor row in place (never deletes it) and the edges table's foreign
    keys forbid a dangling endpoint, so a catalog lookup spans supersedes
    history: a present id reports True, an absent one False."""
    present_id = generate_document_id("reports/v2.md", "2026-02-01", "Successor Doc")
    await graph_store.insert_document(_make_doc(present_id, "Successor Doc"))

    present_detail = await build_not_found_detail(graph_store, present_id)
    assert present_detail["ever_existed"] is True

    absent_id = _well_formed_id("never-inserted", "never_inserted_slug")
    absent_detail = await build_not_found_detail(graph_store, absent_id)
    assert absent_detail["ever_existed"] is False


async def test_detail_three_variants_are_distinguishable(graph_store):
    """The three documented root causes yield distinct discriminator tuples."""
    catalog_id = generate_document_id("reports/q3.md", "2026-01-01", "Quarterly Report")
    await graph_store.insert_document(_make_doc(catalog_id, "Quarterly Report"))

    malformed = await build_not_found_detail(graph_store, "wrong-shape id")
    typo = await build_not_found_detail(graph_store, _well_formed_id("x", "unknown_slug"))
    renamed = await build_not_found_detail(
        graph_store, _well_formed_id("y", document_id_slug(catalog_id))
    )

    def tup(d):
        return (d["id_well_formed"], d["ever_existed"], d["slug_matches_catalog"])

    assert tup(malformed) == (False, False, False)
    assert tup(typo) == (True, False, False)
    assert tup(renamed) == (True, False, True)
    assert len({tup(malformed), tup(typo), tup(renamed)}) == 3


# ---------------------------------------------------------------------------
# End-to-end through the read services (error envelope carries the detail)
# ---------------------------------------------------------------------------


async def test_read_projection_carries_discriminators(utilities_service, graph_store):
    catalog_id = generate_document_id("reports/q3.md", "2026-01-01", "Quarterly Report")
    await graph_store.insert_document(_make_doc(catalog_id, "Quarterly Report"))
    queried_id = _well_formed_id("renamed", document_id_slug(catalog_id))

    with pytest.raises(DocumentNotFoundError) as exc_info:
        await utilities_service.read_projection(queried_id)

    err = exc_info.value
    assert err.code == "document_not_found"
    assert err.detail["id_well_formed"] is True
    assert err.detail["ever_existed"] is False
    assert err.detail["slug_matches_catalog"] is True


async def test_get_document_with_content_carries_discriminators(documents_service):
    with pytest.raises(DocumentNotFoundError) as exc_info:
        await documents_service.get_document_with_content(
            "malformed id!!", include_content=False, write_to_path=None
        )

    err = exc_info.value
    assert err.code == "document_not_found"
    assert err.detail["id_well_formed"] is False
    assert err.detail["ever_existed"] is False
    assert err.detail["slug_matches_catalog"] is False


async def test_read_and_content_paths_agree(utilities_service, documents_service):
    """Both read tools emit the same discriminators for the same miss --
    the contract is a read-path property, not a per-tool affordance."""
    missing = _well_formed_id("agree", "never_present_slug")

    with pytest.raises(DocumentNotFoundError) as e1:
        await utilities_service.read_projection(missing)
    with pytest.raises(DocumentNotFoundError) as e2:
        await documents_service.get_document_with_content(
            missing, include_content=False, write_to_path=None
        )

    keys = ("id_well_formed", "ever_existed", "slug_matches_catalog")
    assert {k: e1.value.detail[k] for k in keys} == {k: e2.value.detail[k] for k in keys}
