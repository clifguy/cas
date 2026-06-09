"""Diagnostics for self-describing read failures (CAS-ADR-039).

On a read miss, SAGE returns ``document_not_found``. The bare code does
not tell a caller whether the identifier was a typo, an identifier from
another vault, or a real document whose current version now lives under a
different id. This module builds the discriminating ``detail`` dict that
rides the existing ``document_not_found`` envelope so the caller can
choose its next action without a second probing round-trip.

The three signals are deliberately orthogonal:

- ``id_well_formed`` -- purely lexical: could this string ever have been
  minted by this vault's id generator? Separates a malformed or
  cross-shape identifier from a plausibly-real one.
- ``ever_existed`` -- does any record with this exact id exist in the
  catalog, including superseded versions? Supersession updates the
  predecessor row in place rather than deleting it, so a catalog lookup
  already spans supersedes history. Distinguishes a never-existed id
  from one that resolves (the latter cannot reach a not-found site, so
  in practice this is ``False`` there, but the signal is reported
  honestly for any id).
- ``slug_matches_catalog`` -- does the id's title-slug match any catalog
  entry's slug? Two versions minted from the same title share a slug even
  when their hash prefixes differ, so a match is the "same document,
  different version/hash -- re-resolve by title" nudge.

The recovery these signals point at (re-resolve by title via ``search``)
is the same regardless of which discriminator fires; the value is
diagnostic, telling the caller whether re-resolution is worth attempting
at all.
"""

from sage.adapters.interfaces import GraphStore
from sage.services.identity import document_id_slug, is_well_formed_document_id


async def build_not_found_detail(store: GraphStore, document_id: str) -> dict:
    """Assemble the ``document_not_found`` detail dict for a read miss.

    Computes the three CAS-ADR-039 discriminators against the graph store
    and catalog. Callers pass the result into ``DocumentNotFoundError`` so
    the error code stays ``document_not_found`` while the detail dict
    differentiates the root cause.
    """
    id_well_formed = is_well_formed_document_id(document_id)

    # Superseded predecessor rows persist in the documents table (a
    # supersede updates the row, it does not delete it), and the edges
    # table's foreign keys forbid an edge endpoint without a backing row.
    # So a single catalog lookup already spans supersedes history.
    # Computed self-containedly so the helper is correct in isolation and
    # unit-testable without a prior get_document call.
    ever_existed = await store.get_document(document_id) is not None

    slug = document_id_slug(document_id)
    slug_matches_catalog = False
    if slug is not None:
        for doc in await store.list_all_documents():
            if document_id_slug(doc.id) == slug:
                slug_matches_catalog = True
                break

    return {
        "document_id": document_id,
        "id_well_formed": id_well_formed,
        "ever_existed": ever_existed,
        "slug_matches_catalog": slug_matches_catalog,
    }
