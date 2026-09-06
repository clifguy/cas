"""Boundary validators on caller-supplied request models.

Pins shape contracts for fields that the substrate stores verbatim from
caller input. Today's gap: ``document_date`` is typed ``str | None`` and
the schema/OpenAPI specifies the YYYY-MM-DD shape, but no validator
enforces it. As a result, callers passing datetime-ISO strings such as
``2026-05-05T00:00:00Z`` poisoned the live Example Portfolio vault and made
``traverse`` raise ``ValueError`` from ``strptime``.

These tests pin the strict-reject contract on the two boundary models
that accept ``document_date`` from callers: ``UpdateMetadataRequest``
(direct field) and ``IngestRequest`` (via the generic ``metadata`` dict
under either the ``document_date`` or filename-parser-keyed ``date``
slot).
"""

import typing
import uuid

import pytest
from pydantic import ValidationError

from sage.models.enums import EdgeType, SourceType
from sage.models.schemas import (
    ChainRequest,
    HashCheckRequest,
    IngestRequest,
    LinkRequest,
    ListFieldPatch,
    RetrievalFilters,
    SetLifecycleRequest,
    Tier3Patch,
    Tier3UniquenessCollision,
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
        source_type=SourceType.MARKDOWN,
        metadata={"document_date": "2026-05-05"},
    )
    assert req.metadata["document_date"] == "2026-05-05"


def test_ingest_metadata_document_date_iso_with_z_rejected():
    with pytest.raises(ValidationError) as excinfo:
        IngestRequest(
            source="x",
            source_type=SourceType.MARKDOWN,
            metadata={"document_date": "2026-05-05T00:00:00Z"},
        )
    assert "document_date" in str(excinfo.value)


def test_ingest_metadata_date_iso_with_z_rejected():
    """The filename-parser-keyed ``date`` slot maps to document_date too."""
    with pytest.raises(ValidationError) as excinfo:
        IngestRequest(
            source="x",
            source_type=SourceType.MARKDOWN,
            metadata={"date": "2026-05-05T00:00:00Z"},
        )
    assert "date" in str(excinfo.value)


# ---------------------------------------------------------------------------
# DocumentIdStr — applied to LinkRequest source/target/anchor fields,
# TraverseRequest.start_id, ChainRequest.document_id,
# IngestRequest.predecessor_id, SetLifecycleRequest.successor_id.
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


def test_ingest_request_rejects_malformed_predecessor_id():
    with pytest.raises(ValidationError) as excinfo:
        IngestRequest(
            source="x",
            source_type=SourceType.MARKDOWN,
            predecessor_id="bad",
        )
    assert "predecessor_id" in str(excinfo.value)


def test_set_lifecycle_request_rejects_malformed_successor_id():
    with pytest.raises(ValidationError) as excinfo:
        SetLifecycleRequest(action="supersede", successor_id="bad")
    assert "successor_id" in str(excinfo.value)


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


def test_hash_check_request_normalizes_missing_prefix():
    """Bare hex is accepted and canonicalized (Sha256Str is normalize-flavor)."""
    req = HashCheckRequest(hashes=["a" * 64])
    assert req.hashes == ["sha256:" + "a" * 64]


def test_hash_check_request_normalizes_uppercase_hex():
    """Uppercase digests canonicalize to lowercase, bare or prefixed."""
    req = HashCheckRequest(hashes=["A" * 64, "sha256:" + "B" * 64])
    assert req.hashes == ["sha256:" + "a" * 64, "sha256:" + "b" * 64]


def test_hash_check_request_rejects_uppercase_prefix():
    """Only the digest is lowercased; the algorithm prefix stays strict."""
    with pytest.raises(ValidationError):
        HashCheckRequest(hashes=["SHA256:" + "a" * 64])


def test_hash_check_request_rejects_whitespace_padding():
    """Normalization does not strip; a padded digest is still malformed."""
    with pytest.raises(ValidationError):
        HashCheckRequest(hashes=["sha256:" + "a" * 64 + "\n"])


def test_hash_check_request_rejects_wrong_length():
    with pytest.raises(ValidationError):
        HashCheckRequest(hashes=["sha256:" + "a" * 32])


# ---------------------------------------------------------------------------
# Sha256Str — applied to LinkRequest.synced_from_content_hash and
# Edge.synced_from_content_hash — a tightening of fields that were
# previously schema-only.
#
# Each rejection case populates all other required LinkRequest fields with
# valid values so the only validator that can fire is the synced_from
# hash check — guards against the coincidental-pass scenario where some
# unrelated required-field error happens to raise ValidationError.
# ---------------------------------------------------------------------------


def test_link_request_synced_from_content_hash_accepts_valid_sha256():
    valid = "sha256:" + "a" * 64
    req = LinkRequest(
        source_id=_DOC_A,
        target_id=_DOC_B,
        edge_type=EdgeType.DERIVED_FROM,
        synced_from_content_hash=valid,
    )
    assert req.synced_from_content_hash == valid


def test_link_request_synced_from_content_hash_accepts_none():
    req = LinkRequest(
        source_id=_DOC_A,
        target_id=_DOC_B,
        edge_type=EdgeType.DERIVED_FROM,
        synced_from_content_hash=None,
    )
    assert req.synced_from_content_hash is None


def test_link_request_synced_from_content_hash_rejects_short_digest():
    with pytest.raises(ValidationError) as excinfo:
        LinkRequest(
            source_id=_DOC_A,
            target_id=_DOC_B,
            edge_type=EdgeType.DERIVED_FROM,
            synced_from_content_hash="sha256:" + "a" * 63,
        )
    assert "synced_from_content_hash" in str(excinfo.value)


def test_link_request_synced_from_content_hash_normalizes_uppercase_hex():
    """Normalize flavor reaches every Sha256Str site, not just hash-check."""
    req = LinkRequest(
        source_id=_DOC_A,
        target_id=_DOC_B,
        edge_type=EdgeType.DERIVED_FROM,
        synced_from_content_hash="sha256:" + "A" * 64,
    )
    assert req.synced_from_content_hash == "sha256:" + "a" * 64


def test_link_request_synced_from_content_hash_normalizes_missing_prefix():
    """Bare hex canonicalizes here too; this is the global-scope discriminator."""
    req = LinkRequest(
        source_id=_DOC_A,
        target_id=_DOC_B,
        edge_type=EdgeType.DERIVED_FROM,
        synced_from_content_hash="a" * 64,
    )
    assert req.synced_from_content_hash == "sha256:" + "a" * 64


def test_link_request_synced_from_content_hash_rejects_empty_string():
    with pytest.raises(ValidationError) as excinfo:
        LinkRequest(
            source_id=_DOC_A,
            target_id=_DOC_B,
            edge_type=EdgeType.DERIVED_FROM,
            synced_from_content_hash="",
        )
    assert "synced_from_content_hash" in str(excinfo.value)


# ---------------------------------------------------------------------------
# tier3_metadata — applied to IngestRequest, UpdateMetadataRequest, and
# RetrievalFilters (Phase 1, plumbing only — validator-cache
# integration tests live in test_tier3_metadata.py).
# ---------------------------------------------------------------------------


def test_ingest_request_accepts_tier3_metadata_dict():
    req = IngestRequest(
        source="foo.md",
        source_type=SourceType.MARKDOWN,
        tier3_metadata={"severity": "high"},
    )
    assert req.tier3_metadata == {"severity": "high"}


def test_ingest_request_tier3_metadata_defaults_none():
    req = IngestRequest(source="foo.md", source_type=SourceType.MARKDOWN)
    assert req.tier3_metadata is None


def test_update_metadata_request_accepts_tier3_patch():
    """UpdateMetadataRequest accepts a Tier3Patch ops object (post-CAS-ADR-028)."""
    req = UpdateMetadataRequest(tier3_metadata=Tier3Patch(set={"ticket_priority": "high"}))
    assert isinstance(req.tier3_metadata, Tier3Patch)
    assert req.tier3_metadata.set == {"ticket_priority": "high"}


def test_update_metadata_request_tier3_metadata_defaults_none():
    req = UpdateMetadataRequest()
    assert req.tier3_metadata is None


def test_retrieval_filters_accepts_tier3_metadata_dict():
    filt = RetrievalFilters(tier3_metadata={"severity": "high", "fix_commit": None})
    assert filt.tier3_metadata == {"severity": "high", "fix_commit": None}


def test_retrieval_filters_tier3_metadata_defaults_none():
    filt = RetrievalFilters()
    assert filt.tier3_metadata is None


def test_retrieval_filters_accepts_empty_tier3_metadata_dict():
    # The semantics of an empty tier3_metadata dict are "no filter"
    # (handled at the service layer). The request model must still
    # construct.
    filt = RetrievalFilters(tier3_metadata={})
    assert filt.tier3_metadata == {}


# ---------------------------------------------------------------------------
# ListFieldPatch / Tier3Patch validators (CAS-ADR-028 update revision)
# ---------------------------------------------------------------------------


def test_tags_patch_requires_at_least_one_op():
    with pytest.raises(ValidationError) as excinfo:
        ListFieldPatch()
    assert "actionable operation" in str(excinfo.value)


def test_tags_patch_rejects_two_empty_lists():
    """{add: [], remove: []} is degenerate -- equivalent to no operation."""
    with pytest.raises(ValidationError):
        ListFieldPatch(add=[], remove=[])


def test_tags_patch_accepts_add_only():
    patch = ListFieldPatch(add=["urgent"])
    assert patch.add == ["urgent"]
    assert patch.remove is None


def test_tags_patch_accepts_remove_only():
    patch = ListFieldPatch(remove=["stale"])
    assert patch.remove == ["stale"]
    assert patch.add is None


def test_tags_patch_accepts_both_disjoint():
    patch = ListFieldPatch(add=["new"], remove=["old"])
    assert patch.add == ["new"]
    assert patch.remove == ["old"]


def test_tags_patch_rejects_add_remove_overlap():
    with pytest.raises(ValidationError) as excinfo:
        ListFieldPatch(add=["x"], remove=["x"])
    assert "disjoint" in str(excinfo.value)


def test_tags_patch_rejects_duplicates_in_add():
    with pytest.raises(ValidationError) as excinfo:
        ListFieldPatch(add=["x", "x"])
    assert "duplicates" in str(excinfo.value)


def test_tags_patch_rejects_duplicates_in_remove():
    with pytest.raises(ValidationError):
        ListFieldPatch(remove=["y", "y"])


def test_tags_patch_rejects_extra_keys():
    """extra='forbid' rejects unknown ops to keep the verb vocabulary tight."""
    with pytest.raises(ValidationError):
        ListFieldPatch(add=["x"], replace=["y"])  # type: ignore[call-arg]


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


# ---------------------------------------------------------------------------
# RetrievalFilters.document_ids — collection-typed boundary
#
# The filter is resolved by SQL equality against stored ids, so a malformed
# entry matched nothing and returned a successful empty result: a caller
# could not tell it apart from a well-formed id with no matches. The alias
# on the element type turns that silent miss into a structured rejection.
# ---------------------------------------------------------------------------


def test_retrieval_filters_rejects_malformed_document_id():
    """A malformed entry rejects the filter rather than resolving to no match."""
    with pytest.raises(ValidationError) as excinfo:
        RetrievalFilters(document_ids=["not-a-doc-id"])
    err = excinfo.value.errors()[0]
    assert err["type"] == "invalid_document_id", err
    # The index is load-bearing: it names which entry failed.
    assert err["loc"] == ("document_ids", 0), err
    assert err["ctx"]["document_id"] == "not-a-doc-id", err


def test_retrieval_filters_reports_the_offending_entry_not_the_first():
    """A well-formed entry ahead of a malformed one does not mask the failure."""
    with pytest.raises(ValidationError) as excinfo:
        RetrievalFilters(document_ids=[_DOC_A, "BAD-ID"])
    err = excinfo.value.errors()[0]
    assert err["loc"] == ("document_ids", 1), err
    assert err["ctx"]["document_id"] == "BAD-ID", err


def test_retrieval_filters_accepts_well_formed_document_ids():
    """Legitimate filtering is untouched: shaped ids pass through verbatim."""
    filt = RetrievalFilters(document_ids=[_DOC_A, _DOC_B])
    assert filt.document_ids == [_DOC_A, _DOC_B]


def test_retrieval_filters_document_ids_agrees_with_tier3_collision():
    """Both models spell the same field name with the same shape contract.

    The two carried contradictory annotations -- one bare ``list[str]``, one
    ``list[DocumentIdStr]`` -- for a field of identical meaning. Comparing the
    resolved validators pins the agreement as a property rather than as a
    coincidence of two independent edits.
    """
    from pydantic import AfterValidator

    from sage.models import schemas as schemas_mod

    def _element_validators(cls, name):
        """AfterValidator funcs reachable anywhere in the field annotation."""
        found = set()
        stack = [cls.model_fields[name].annotation]
        while stack:
            node = stack.pop()
            if hasattr(node, "__metadata__"):
                for m in node.__metadata__:
                    if isinstance(m, AfterValidator):
                        found.add(m.func)
                stack.append(node.__origin__)
                continue
            stack.extend(typing.get_args(node))
        return found

    filters_validators = _element_validators(RetrievalFilters, "document_ids")
    collision_validators = _element_validators(Tier3UniquenessCollision, "document_ids")
    assert schemas_mod._validate_document_id in filters_validators, filters_validators
    assert schemas_mod._validate_document_id in collision_validators, collision_validators
