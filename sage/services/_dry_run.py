"""Shared vocabulary for dry-run mode across mutation services.

Centralized so that ``LifecycleService`` (would-be ``supersedes`` edge),
``GraphOpsService`` (would-be ``_create_edge_strict`` edge), the MCP-tool wrappers,
and the dry-run test suite all agree on the same value.

Also home to the document-type requirement set, which two services read
off the same vault configuration and must report identically: the ingest
preview reports it on every verdict, and the typed-metadata refusal
carries it on every raise, from either service.
"""

from __future__ import annotations

from sage.config import VaultConfig
from sage.models.schemas import DocTypeRequirements

# Sentinel ``id`` populated on edges returned from a dry-run mutation
# that would have created the edge on a real run. We use the **nil UUID**
# (RFC 9562 §5.9: ``00000000-0000-0000-0000-000000000000``) because the
# ``EdgeIdStr`` schema validator requires a UUID — a literal like
# ``"<dry-run>"`` would fail Pydantic validation. The nil UUID is the
# documented "absent identifier" convention; storage will never mint
# this value (uuid4 cannot produce it), so a caller that mistakes it
# for a real id and uses it in a follow-up call will fail loudly on
# the lookup. The response wrapper's ``dry_run=True`` echo is the
# primary signal; this constant is the per-edge belt-and-braces marker.
DRY_RUN_SENTINEL_EDGE_ID = "00000000-0000-0000-0000-000000000000"


def doc_type_requirements(config: VaultConfig, doc_type: str) -> DocTypeRequirements:
    """Read what a vault declares a document type requires of its typed metadata.

    Every field is read off the declaration rather than inferred from
    another, because the interesting cases are the ones where they
    disagree. A document type declaring no ``metadata_schema`` and one
    declaring a schema with no properties both report an empty
    ``declared_tier3_fields`` and both refuse a payload, but only the
    first refuses every payload for the same reason whatever the keys
    are -- so the two are separated by ``has_metadata_schema`` and not
    by the field list. A type absent from the vocabulary altogether
    reports ``is_declared=False`` and empty sets, which is what a
    ``misc`` fallback or a typo'd caller value resolves to.

    ``required`` is read only from the schema's top level. A schema
    expressing conditional requirements through ``allOf`` or
    ``if``/``then`` still validates as declared; what this reports is
    what the declaration states unconditionally.
    """
    entry = next(
        (dt for dt in config.document_types.doc_types if dt.value == doc_type),
        None,
    )
    if entry is None:
        return DocTypeRequirements(
            doc_type=doc_type,
            is_declared=False,
            has_metadata_schema=False,
            declared_tier3_fields=[],
            required_tier3_fields=[],
            unique_tier3_fields=[],
            permitted_source_types=None,
        )

    schema = entry.metadata_schema
    properties = (schema or {}).get("properties") or {}
    required = (schema or {}).get("required") or []
    return DocTypeRequirements(
        doc_type=doc_type,
        is_declared=True,
        has_metadata_schema=schema is not None,
        declared_tier3_fields=sorted(properties),
        required_tier3_fields=sorted(required),
        unique_tier3_fields=sorted(entry.unique_keys or []),
        permitted_source_types=(
            sorted(entry.source_types) if entry.source_types is not None else None
        ),
    )
