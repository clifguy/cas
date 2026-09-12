"""Shared response documentation for the typed-alias boundary refusals.

Every value a route binds through a typed alias is validated at request
binding, and a malformed one is translated into a structured per-field 400
rather than a framework-native 422 (CAS-ADR-040). The refusal is therefore a
branch the operation has always had, and one a generated client can only
handle if the published contract declares it.

The sentence for each code lives here rather than at each route because the
same refusal is reachable from every vault-scoped operation: a per-route copy
is a text that drifts one route at a time, and the OpenAPI document has to
match it word for word. One definition, composed at each route, keeps the
published contract and the router in step by construction.

Two wordings per code, chosen by where the value arrives. A path parameter has
one spelling and the sentence names it, which is what a caller reading a URL
template needs. A request body or query string may carry the shape at several
names, some of them nested in a list of items, so that sentence names the shape
instead -- naming one of the carriers would be false for the others.
"""

from sage.models.schemas import ErrorResponse

#: Sentence per code for a value bound from the path. Iteration order is the
#: order the sentences are composed in, and it follows the path template,
#: outermost segment first, so an operation declaring two of them reads in the
#: order a caller reads the URL.
PATH_400_SENTENCES: dict[str, str] = {
    "invalid_vault_id": "`invalid_vault_id`: `vault_id` is not a well-formed vault id.",
    "invalid_document_id": (
        "`invalid_document_id`: `document_id` is not a well-formed document id."
    ),
    "invalid_edge_id": "`invalid_edge_id`: `edge_id` is not a well-formed edge id.",
}

#: Sentence per code for a value bound from the request body or query string.
REQUEST_400_SENTENCES: dict[str, str] = {
    "invalid_vault_id": "`invalid_vault_id`: a vault id in the request is not well-formed.",
    "invalid_document_id": (
        "`invalid_document_id`: a document id in the request is not well-formed."
    ),
    "invalid_edge_id": "`invalid_edge_id`: an edge id in the request is not well-formed.",
    "invalid_sha256": (
        "`invalid_sha256`: a content hash in the request is not a well-formed sha256 digest."
    ),
    "invalid_document_date": (
        "`invalid_document_date`: a document date in the request is not a "
        "well-formed calendar date (`YYYY-MM-DD`)."
    ),
    "invalid_user_id": "`invalid_user_id`: a user id in the request is not well-formed.",
}


def boundary_400(
    *,
    path: tuple[str, ...] = (),
    request: tuple[str, ...] = (),
    extra: str | None = None,
) -> dict[str, object]:
    """Build the 400 ``responses`` entry for a route's boundary refusals.

    ``path`` and ``request`` name the typed-alias refusals reachable from each
    side of the request. ``extra`` carries the operation's own 400 prose, which
    is placed first: a caller reading the entry meets what is particular to
    this operation before the refusals every sibling shares. A status may be
    declared only once per operation, so an operation with both kinds of 400
    reconciles them into this single description rather than a second entry.
    """
    unknown = sorted(
        {code for code in path if code not in PATH_400_SENTENCES}
        | {code for code in request if code not in REQUEST_400_SENTENCES}
    )
    if unknown:
        raise ValueError(f"no boundary sentence declared for {unknown}")

    paragraphs = [] if extra is None else [extra]
    paragraphs.extend(s for code, s in PATH_400_SENTENCES.items() if code in path)
    paragraphs.extend(s for code, s in REQUEST_400_SENTENCES.items() if code in request)
    return {"model": ErrorResponse, "description": "\n\n".join(paragraphs)}
