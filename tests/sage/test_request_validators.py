"""Boundary validators on caller-supplied request models.

Pins shape contracts for fields that the substrate stores verbatim from
caller input. Today's gap: ``document_date`` is typed ``str | None`` and
the schema/OpenAPI specifies the YYYY-MM-DD shape, but no validator
enforces it. As a result, callers passing datetime-ISO strings such as
``2026-05-05T00:00:00Z`` poisoned the live PIM Health vault and made
``sage_traverse`` raise ``ValueError`` from ``strptime``.

These tests pin the strict-reject contract on the two boundary models
that accept ``document_date`` from callers: ``UpdateMetadataRequest``
(direct field) and ``IngestRequest`` (via the generic ``metadata`` dict
under either the ``document_date`` or filename-parser-keyed ``date``
slot).
"""

import uuid

import pytest
from pydantic import ValidationError

from sage.models.enums import EdgeType, SourceType
from sage.models.schemas import (
    ChainRequest,
    HashCheckRequest,
    IngestRequest,
    LinkRequest,
    RetrievalFilters,
    SetLifecycleRequest,
    TagsPatch,
    Tier3Patch,
    TraverseRequest,
    UpdateMetadataRequest,
)

# Shaped IDs used throughout these tests. The 8-hex prefix is required by
# the document-ID regex in sage/services/identity.py.
_DOC_A = "deadbeef_doc_a"
_DOC_B = "cafebabe_doc_b"


# ---------------------------------------------------------------------------
# UpdateMetadataRequest.document_date
# ---------------------------------------------------------------------------


def test_update_metadata_accepts_yyyy_mm_dd():
    """The contract shape constructs without error and is preserved verbatim."""
    req = UpdateMetadataRequest(document_date="2026-05-05")
    assert req.document_date == "2026-05-05"


def test_update_metadata_rejects_iso_with_z():
    """Datetime-ISO with a trailing Z is rejected (this is the live-vault bug)."""
    with pytest.raises(ValidationError) as excinfo:
        UpdateMetadataRequest(document_date="2026-05-05T00:00:00Z")
    msg = str(excinfo.value)
    assert "document_date" in msg


def test_update_metadata_rejects_garbage():
    """Any string outside YYYY-MM-DD is rejected."""
    with pytest.raises(ValidationError):
        UpdateMetadataRequest(document_date="not a date")


# ---------------------------------------------------------------------------
# IngestRequest.metadata document-date keys
# ---------------------------------------------------------------------------


def test_ingest_metadata_document_date_yyyy_mm_dd_accepted():
    req = IngestRequest(
        source="x",
        adapter=SourceType.MARKDOWN,
        metadata={"document_date": "2026-05-05"},
    )
    assert req.metadata["document_date"] == "2026-05-05"


def test_ingest_metadata_document_date_iso_with_z_rejected():
    with pytest.raises(ValidationError) as excinfo:
        IngestRequest(
            source="x",
            adapter=SourceType.MARKDOWN,
            metadata={"document_date": "2026-05-05T00:00:00Z"},
        )
    assert "document_date" in str(excinfo.value)


def test_ingest_metadata_date_iso_with_z_rejected():
    """The filename-parser-keyed ``date`` slot maps to document_date too."""
    with pytest.raises(ValidationError) as excinfo:
        IngestRequest(
            source="x",
            adapter=SourceType.MARKDOWN,
            metadata={"date": "2026-05-05T00:00:00Z"},
        )
    assert "date" in str(excinfo.value)


# ---------------------------------------------------------------------------
# DocumentIdStr — applied to LinkRequest source/target/anchor fields,
# TraverseRequest.start_id, ChainRequest.document_id,
# IngestRequest.supersedes_document_id, SetLifecycleRequest.new_version_id.
# ---------------------------------------------------------------------------


def test_link_request_accepts_shaped_document_ids():
    """Valid document IDs (8 hex + underscore + slug) construct without error."""
    req = LinkRequest(
        source_id=_DOC_A,
        target_id=_DOC_B,
        edge_type=EdgeType.REFERENCES,
    )
    assert req.source_id == _DOC_A
    assert req.target_id == _DOC_B


def test_link_request_rejects_version_label_in_anchor():
    """The MEMORY.md-captured failure mode: caller passes a version_label
    string ('v8.2.0') into an anchor slot expecting a document ID."""
    with pytest.raises(ValidationError) as excinfo:
        LinkRequest(
            source_id=_DOC_A,
            target_id=_DOC_B,
            edge_type=EdgeType.REFERENCES,
            source_valid_from_version="v8.2.0",
            target_valid_from_version=_DOC_B,
        )
    assert "source_valid_from_version" in str(excinfo.value)


def test_traverse_request_rejects_malformed_start_id():
    with pytest.raises(ValidationError) as excinfo:
        TraverseRequest(start_id="bad")
    assert "start_id" in str(excinfo.value)


def test_chain_request_rejects_malformed_document_id():
    with pytest.raises(ValidationError) as excinfo:
        ChainRequest(document_id="bad", edge_type=EdgeType.SUPERSEDES)
    assert "document_id" in str(excinfo.value)


def test_ingest_request_rejects_malformed_supersedes_document_id():
    with pytest.raises(ValidationError) as excinfo:
        IngestRequest(
            source="x",
            adapter=SourceType.MARKDOWN,
            supersedes_document_id="bad",
        )
    assert "supersedes_document_id" in str(excinfo.value)


def test_set_lifecycle_request_rejects_malformed_new_version_id():
    with pytest.raises(ValidationError) as excinfo:
        SetLifecycleRequest(action="supersede", new_version_id="bad")
    assert "new_version_id" in str(excinfo.value)


# ---------------------------------------------------------------------------
# EdgeIdStr — applied to LinkRequest.retracted_edge_id (UUID4).
# ---------------------------------------------------------------------------


def test_link_request_accepts_uuid_retracted_edge_id():
    edge_uuid = str(uuid.uuid4())
    req = LinkRequest(
        source_id=_DOC_A,
        edge_type=EdgeType.RETRACTS,
        retracted_edge_id=edge_uuid,
    )
    assert req.retracted_edge_id == edge_uuid


def test_link_request_rejects_non_uuid_retracted_edge_id():
    with pytest.raises(ValidationError) as excinfo:
        LinkRequest(
            source_id=_DOC_A,
            edge_type=EdgeType.RETRACTS,
            retracted_edge_id="not-a-uuid",
        )
    assert "retracted_edge_id" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Sha256Str — applied to HashCheckRequest.hashes.
# ---------------------------------------------------------------------------


def test_hash_check_request_accepts_valid_sha256():
    valid_hash = "sha256:" + "a" * 64
    req = HashCheckRequest(hashes=[valid_hash])
    assert req.hashes == [valid_hash]


def test_hash_check_request_rejects_missing_prefix():
    with pytest.raises(ValidationError):
        HashCheckRequest(hashes=["a" * 64])


def test_hash_check_request_rejects_wrong_length():
    with pytest.raises(ValidationError):
        HashCheckRequest(hashes=["sha256:" + "a" * 32])


# ---------------------------------------------------------------------------
# tier3_metadata — applied to IngestRequest, UpdateMetadataRequest, and
# RetrievalFilters (T-0004 Phase 1, plumbing only — validator-cache
# integration tests live in test_tier3_metadata.py).
# ---------------------------------------------------------------------------


def test_ingest_request_accepts_tier3_metadata_dict():
    req = IngestRequest(
        source="foo.md",
        adapter=SourceType.MARKDOWN,
        tier3_metadata={"severity": "high"},
    )
    assert req.tier3_metadata == {"severity": "high"}


def test_ingest_request_tier3_metadata_defaults_none():
    req = IngestRequest(source="foo.md", adapter=SourceType.MARKDOWN)
    assert req.tier3_metadata is None


def test_update_metadata_request_accepts_tier3_patch():
    """UpdateMetadataRequest accepts a Tier3Patch ops object (post-CAS-ADR-028)."""
    req = UpdateMetadataRequest(tier3_metadata=Tier3Patch(set={"ticket_priority": "high"}))
    assert isinstance(req.tier3_metadata, Tier3Patch)
    assert req.tier3_metadata.set == {"ticket_priority": "high"}


def test_update_metadata_request_tier3_metadata_defaults_none():
    req = UpdateMetadataRequest()
    assert req.tier3_metadata is None


def test_retrieval_filters_accepts_tier3_dict():
    filt = RetrievalFilters(tier3={"severity": "high", "fix_commit": None})
    assert filt.tier3 == {"severity": "high", "fix_commit": None}


def test_retrieval_filters_tier3_defaults_none():
    filt = RetrievalFilters()
    assert filt.tier3 is None


def test_retrieval_filters_accepts_empty_tier3_dict():
    # The semantics of an empty tier3 dict are "no filter" (handled at the
    # service layer). The request model must still construct.
    filt = RetrievalFilters(tier3={})
    assert filt.tier3 == {}


# ---------------------------------------------------------------------------
# TagsPatch / Tier3Patch validators (CAS-ADR-028 update revision)
# ---------------------------------------------------------------------------


def test_tags_patch_requires_at_least_one_op():
    with pytest.raises(ValidationError) as excinfo:
        TagsPatch()
    assert "actionable operation" in str(excinfo.value)


def test_tags_patch_rejects_two_empty_lists():
    """{add: [], remove: []} is degenerate -- equivalent to no operation."""
    with pytest.raises(ValidationError):
        TagsPatch(add=[], remove=[])


def test_tags_patch_accepts_add_only():
    patch = TagsPatch(add=["urgent"])
    assert patch.add == ["urgent"]
    assert patch.remove is None


def test_tags_patch_accepts_remove_only():
    patch = TagsPatch(remove=["stale"])
    assert patch.remove == ["stale"]
    assert patch.add is None


def test_tags_patch_accepts_both_disjoint():
    patch = TagsPatch(add=["new"], remove=["old"])
    assert patch.add == ["new"]
    assert patch.remove == ["old"]


def test_tags_patch_rejects_add_remove_overlap():
    with pytest.raises(ValidationError) as excinfo:
        TagsPatch(add=["x"], remove=["x"])
    assert "disjoint" in str(excinfo.value)


def test_tags_patch_rejects_duplicates_in_add():
    with pytest.raises(ValidationError) as excinfo:
        TagsPatch(add=["x", "x"])
    assert "duplicates" in str(excinfo.value)


def test_tags_patch_rejects_duplicates_in_remove():
    with pytest.raises(ValidationError):
        TagsPatch(remove=["y", "y"])


def test_tags_patch_rejects_extra_keys():
    """extra='forbid' rejects unknown ops to keep the verb vocabulary tight."""
    with pytest.raises(ValidationError):
        TagsPatch(add=["x"], replace=["y"])  # type: ignore[call-arg]


def test_tier3_patch_requires_at_least_one_op():
    with pytest.raises(ValidationError):
        Tier3Patch()


def test_tier3_patch_rejects_two_empty_ops():
    with pytest.raises(ValidationError):
        Tier3Patch(set={}, unset=[])


def test_tier3_patch_accepts_set_only():
    patch = Tier3Patch(set={"k": "v"})
    assert patch.set == {"k": "v"}
    assert patch.unset is None


def test_tier3_patch_accepts_unset_only():
    patch = Tier3Patch(unset=["stale_key"])
    assert patch.unset == ["stale_key"]


def test_tier3_patch_rejects_set_unset_overlap():
    with pytest.raises(ValidationError) as excinfo:
        Tier3Patch(set={"k": "v"}, unset=["k"])
    assert "disjoint" in str(excinfo.value)


def test_tier3_patch_rejects_unset_duplicates():
    with pytest.raises(ValidationError):
        Tier3Patch(unset=["k", "k"])


def test_tier3_patch_rejects_extra_keys():
    with pytest.raises(ValidationError):
        Tier3Patch(set={"k": "v"}, replace={"k": "x"})  # type: ignore[call-arg]


def test_update_metadata_request_rejects_legacy_bare_list_tags():
    """The pre-patch bare-list form for tags is rejected at request validation."""
    with pytest.raises(ValidationError):
        UpdateMetadataRequest(tags=["a", "b"])  # type: ignore[arg-type]


def test_update_metadata_request_rejects_legacy_bare_dict_tier3():
    """The pre-patch bare-dict form for tier3_metadata is rejected at request validation."""
    with pytest.raises(ValidationError):
        UpdateMetadataRequest(tier3_metadata={"ticket_priority": "high"})  # type: ignore[arg-type]


def test_update_metadata_request_rejects_extra_field():
    """model_config={'extra': 'forbid'} catches typos in field names."""
    with pytest.raises(ValidationError):
        UpdateMetadataRequest(titel="oops")  # type: ignore[call-arg]
