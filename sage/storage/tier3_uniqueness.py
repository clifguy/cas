"""Tier3-metadata uniqueness signals and index-naming convention.

Backend-neutral storage-layer vocabulary for the tier3 uniqueness
constraint (CAS-ADR-031): the exceptions a graph store raises when a
declared ``unique_keys`` constraint fires, and the canonical name scheme
for the partial UNIQUE indexes that enforce it. Concrete stores own the
DDL that creates those indexes; this module owns only what the layers
above the store need to share.
"""

from __future__ import annotations


class Tier3UniqueViolation(Exception):
    """Storage-layer signal that a tier3_metadata uniqueness constraint
    fired on insert or supersession-insert (CAS-ADR-031).

    Raised by a GraphStore implementation when its partial UNIQUE index
    on ``(doc_type, tier3_metadata -> field)`` rejects a write. The
    service layer translates this into the public
    `Tier3UniqueConstraintViolation` SAGEError (sage.api.errors), preserving
    the layering rule that storage does not depend on the api layer.
    """

    def __init__(
        self,
        doc_type: str,
        field: str,
        colliding_value: object,
        existing_document_id: str,
    ) -> None:
        super().__init__(
            f"tier3_metadata.{field}={colliding_value!r} on doc_type "
            f"{doc_type!r} is already held by document {existing_document_id!r}"
        )
        self.doc_type = doc_type
        self.field = field
        self.colliding_value = colliding_value
        self.existing_document_id = existing_document_id


class Tier3UniqueIndexBlockedError(RuntimeError):
    """Raised when an attempt to create a tier3 unique index fails because
    pre-existing rows violate the uniqueness constraint (CAS-ADR-031 §5).

    The activation path surfaces the collision report to the operator and
    refuses to activate the constraint; the substrate does not auto-resolve.
    """

    def __init__(self, doc_type: str, field: str, message: str) -> None:
        super().__init__(message)
        self.doc_type = doc_type
        self.field = field


# Defense-in-depth gate for the doc_type and field tokens
# interpolated into the tier3 partial UNIQUE index DDL. doc_type already
# matches `^[a-z][a-z0-9_]*$` by vault-config schema; the cross-field
# validator in sage.config restricts unique_keys entries to declared
# metadata_schema properties.
TIER3_UNIQUE_INDEX_PREFIX = "idx_tier3_unique_"


def tier3_unique_index_name(doc_type: str, field: str) -> str:
    """Canonical index name for the (doc_type, field) partial UNIQUE index.

    Format: ``idx_tier3_unique_<doc_type>_<field>``. The doc_type/field
    boundary is recovered at error-translation time by stripping the known
    doc_type prefix from the captured tail.
    """
    return f"{TIER3_UNIQUE_INDEX_PREFIX}{doc_type}_{field}"
