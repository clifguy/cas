"""SAGE error hierarchy and FastAPI exception handlers.

Exception classes carry structured detail dicts matching the OpenAPI
ErrorResponse schema. The exception handler converts them to JSON responses.
"""

import logging
import types
import typing
from collections.abc import Collection, Iterable
from datetime import datetime
from enum import StrEnum
from typing import Final

from fastapi import FastAPI, Request
from fastapi.dependencies.utils import get_flat_dependant
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from sage.config import render_state_set
from sage.models.enums import EdgeType, SourceType
from sage.models.schemas import ErrorResponse
from sage.models.wire import to_wire


class SAGEError(Exception):
    """Base exception for SAGE API errors."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        detail: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        # An empty detail is an absent one, as the published envelope states:
        # its keys vary by code, so an empty dict says nothing the code has not.
        # Normalized here, once, so every renderer applies the plain null rule.
        self.detail = detail or None


class DocumentNotFoundError(SAGEError):
    """404: no document resolves to the supplied id.

    The error ``code`` is always ``document_not_found``; the ``detail``
    dict differentiates the root cause (CAS-ADR-039). Read-path callers
    pass a pre-built ``detail`` carrying the discriminators
    ``id_well_formed``, ``ever_existed``, and ``slug_matches_catalog`` so
    a caller can tell a malformed id from a never-existed id from a
    real-but-renamed one without a second probing round-trip. Write-path
    and graph callers omit ``detail`` and get the bare
    ``{"document_id": ...}`` form.
    """

    def __init__(self, document_id: str, detail: dict | None = None) -> None:
        super().__init__(
            "document_not_found",
            f"Document {document_id} not found",
            404,
            detail if detail is not None else {"document_id": document_id},
        )


class InvalidDocumentIdError(SAGEError):
    """400: the supplied document_id is not a well-formed id.

    A syntactically-malformed id is rejected at the request boundary and
    surfaces as this structured 400 — distinct from the 404
    ``document_not_found`` discriminator, which fires only for a
    well-formed id that resolves to no document. The boundary precedes
    the discriminator by design: malformed *syntax* is a client error,
    not a miss. The ``detail`` carries the offending value so a caller
    can correct its call without parsing the message.
    """

    def __init__(self, document_id: str) -> None:
        super().__init__(
            "invalid_document_id",
            f"document_id {document_id!r} is not a well-formed document id "
            "(expected 8 hex characters, an underscore, then a lowercase "
            "alphanumeric/underscore slug)",
            400,
            {"document_id": document_id},
        )


#: Single source of truth for the typed-alias boundary-error family. Shared by
#: ``translate_validation_error`` (which rebuilds the structured 400 from the
#: validator's ctx) and the MCP ``_error_response`` choke point (which envelopes
#: these codes instead of the generic ``internal_error``). ``invalid_document_id``
#: keeps its own ``InvalidDocumentIdError`` but joins the set so both dispatch
#: points recognize the whole family from one place. Adding a future alias means
#: adding its code here plus raising the shared-shape ``PydanticCustomError`` in
#: its validator -- no new error class or dispatch branch.
_TYPED_ALIAS_CODES: frozenset[str] = frozenset(
    {
        "invalid_document_id",
        "invalid_vault_id",
        "invalid_edge_id",
        "invalid_sha256",
        "invalid_document_date",
        "invalid_user_id",
    }
)


class InvalidTypedAliasError(SAGEError):
    """400: a typed-alias boundary value failed its shape validator.

    One parameterized error for the typed-alias family (vault_id, edge_id,
    sha256, document_date, user_id). The leaf-layer validator in
    ``sage/models/schemas.py`` raises a ``PydanticCustomError`` carrying a
    uniform ``{argument, value, expected}`` ctx; the request-boundary translator
    rebuilds this structured 400 from that ctx, so a malformed value surfaces as
    a caller-actionable ``invalid_<argument>`` code -- with the offending value
    and the expected shape in ``detail`` -- instead of the generic
    ``internal_error`` (MCP) / native 422 (HTTP). ``invalid_document_id`` keeps
    its own ``InvalidDocumentIdError``.
    """

    def __init__(self, code: str, argument: str, value: object, expected: str) -> None:
        super().__init__(
            code,
            f"{argument} {value!r} is not a well-formed {argument} (expected {expected})",
            400,
            {argument: value, "expected": expected},
        )


class InvalidLifecycleTransitionError(SAGEError):
    """409: action is known but invalid from current state (BH-012)."""

    def __init__(
        self,
        current_state: str,
        attempted_action: str,
        valid_actions: list[str],
        pipeline_status: str | None = None,
    ) -> None:
        detail: dict = {
            "current_state": current_state,
            "attempted_action": attempted_action,
            "valid_actions": valid_actions,
        }
        if pipeline_status is not None:
            detail["pipeline_status"] = pipeline_status
        super().__init__(
            "invalid_lifecycle_transition",
            f"Cannot {attempted_action} from {current_state}",
            409,
            detail,
        )


class InvalidActionError(SAGEError):
    """400: an action value the operation does not know.

    ``known_actions`` names every action the operation accepts. For a
    staging-edge resolution that is ``confirm`` and ``dismiss``. For a
    lifecycle transition it is the vault's whole caller-invocable action
    vocabulary, which is vault-config-defined rather than a fixed set.
    The pipeline's own landing transition is not in it, because a caller
    invoking that one would be refused whatever state the document is
    in. This is the first refusal a caller who does not know the
    vocabulary meets -- the
    sibling ``invalid_lifecycle_transition``, which names the actions
    valid from the current state, is reached only by an action the table
    already knows. Naming the vocabulary here is what lets a caller
    recover from a guess without reading the vault configuration.
    """

    def __init__(self, action: str, known_actions: list[str] | None = None) -> None:
        detail: dict = {"attempted_action": action}
        if known_actions is not None:
            detail["known_actions"] = sorted(known_actions)
        super().__init__(
            "invalid_action",
            f"Unknown action: {action}",
            400,
            detail,
        )


class DuplicateContentError(SAGEError):
    """409: a document already carries this content hash (BH-018, BH-066).

    Keyed on the hash alone, so identical bytes at a path the vault has
    never seen refuse too. ``existing_document_id`` names one holder:
    where several carry the hash, a version a supersession has not
    retired outranks one it has, and the lowest document id decides
    among equals.
    """

    def __init__(self, existing_document_id: str, source_content_hash: str) -> None:
        super().__init__(
            "duplicate_content",
            "Duplicate content detected",
            409,
            {
                "existing_document_id": existing_document_id,
                "source_content_hash": source_content_hash,
            },
        )


class ForceReingestPathMismatchError(SAGEError):
    """409: a force-reingest resolved a content-hash match to a record stored
    at a different source_path than the incoming file, and the caller did not
    confirm the target.

    Force-reingest keys its target by content hash alone. When two files are
    byte-identical but live at different paths, the hash match may be an
    unrelated document rather than the one the caller meant to re-ingest.
    Overwriting it would silently discard that document's identity, so the
    substrate refuses until the caller confirms the intended record via
    ``document_id``. Same-path force-reingest (BH-019) never trips this.
    """

    def __init__(
        self,
        resolved_id: str,
        resolved_source_path: str,
        incoming_source_path: str,
        content_hash: str,
    ) -> None:
        super().__init__(
            "force_reingest_path_mismatch",
            (
                "Force re-ingest matched an existing document at a different "
                "source_path by content hash alone; pass document_id to "
                "confirm the record to overwrite."
            ),
            409,
            {
                "existing_document_id": resolved_id,
                "existing_source_path": resolved_source_path,
                "new_source_path": incoming_source_path,
                "source_content_hash": content_hash,
            },
        )


class ForceReingestPinMismatchError(SAGEError):
    """409: a force-reingest pinned a ``document_id`` that does not carry the
    delivered content hash.

    The pin names which of the documents holding the delivered bytes to
    reuse, so it can only name one of them. A pin naming a document with
    other bytes, or no document at all, cannot be honored, and falling back
    to whichever holder the hash lookup prefers would overwrite a record the
    caller did not name. The detail carries both halves of the disagreement:
    the pinned document's own hash (null when no document has that id) and
    the document the delivered hash resolves to (null when none holds it).
    """

    def __init__(
        self,
        pinned_id: str,
        pinned_content_hash: str | None,
        content_hash: str,
        resolved_id: str | None,
    ) -> None:
        super().__init__(
            "force_reingest_pin_mismatch",
            (
                "Force re-ingest was pinned to a document_id that does not "
                "carry the delivered content hash; the pin must name a "
                "document holding these bytes."
            ),
            409,
            {
                "document_id": pinned_id,
                "pinned_source_content_hash": pinned_content_hash,
                "source_content_hash": content_hash,
                "existing_document_id": resolved_id,
            },
        )


class MissingFieldError(SAGEError):
    """400: required field missing from request."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(
            f"missing_{field}",
            message,
            400,
        )


class UnexpectedFieldError(SAGEError):
    """400: request field supplied alongside an action that does not take it.

    The mirror of :class:`MissingFieldError`, and the reason both exist:
    a field that qualifies one action qualifies no other, so supplying it
    elsewhere is a caller who believes something about the call that is
    not true. Ignoring it silently lets that belief stand -- the caller
    sees a success and reads it as confirmation -- which is worse than a
    refusal naming the action the field belongs to.
    """

    def __init__(self, field: str, attempted_action: str, required_action: str) -> None:
        super().__init__(
            f"unexpected_{field}",
            f"{field} is accepted only with action '{required_action}', not '{attempted_action}'",
            400,
            {
                "field": field,
                "attempted_action": attempted_action,
                "required_action": required_action,
            },
        )


class ReservedTransitionError(SAGEError):
    """409: the vault declares a transition the engine reserves to itself.

    Almost every lifecycle transition means whatever its vault says it
    means. One does not: the engine requires a relocation pointer on the
    transition that lands a document in the relocated state, so that
    action and that state are reserved to each other. The configuration
    validator refuses a table that breaks the reservation, but an
    already-on-disk configuration loads leniently -- a rejected file
    would drop its vault from the registry, unreachable by the surfaces
    that could repair it -- so a vault can be serving such a table right
    now. Refusing here is what stops the transition from running against
    one, and the message names the row to repair rather than the call.
    """

    def __init__(self, from_state: str, action: str, to_state: str, reason: str) -> None:
        super().__init__(
            "reserved_transition",
            f"the vault's transition '{from_state} -> {action} -> {to_state}' is refused: {reason}",
            409,
            {
                "from_state": from_state,
                "attempted_action": action,
                "to_state": to_state,
                "reason": reason,
            },
        )


class RelocationProvenanceMismatchError(SAGEError):
    """400: a relocation pointer names a digest the document does not carry.

    A relocation moves a document between vaults; it does not modify one,
    so the pointer's ``source_content_hash`` names the bytes that
    travelled between the two, and a pointer naming a digest neither side
    can account for is not describing a relocation (CAS-ADR-050). It would
    be one more asserted coordinate, which is what the decision prefers a
    checkable digest to.

    Neither half follows its pointer or reaches the other side to raise
    this. Each compares a caller-supplied value against digests already
    recorded locally, so the refusal is available under every deployment
    and to a vault whose store rewrites its copy at rest.

    What each half accounts for differs, which is why the code names the
    pointer rather than being shared. The **destination** knows exactly
    what it received, so it admits one value: the digest of the bytes the
    call delivered. The **origin** admits either digest it records for the
    document -- its source provenance digest or its as-stored digest --
    because it cannot know which of its two byte-sets the caller took. A
    caller still holding the file it originally ingested relocates that; a
    caller that does not fetches what the vault serves, which is the
    retained copy. Under a binding that rewrites at rest (CAS-ADR-043)
    the second is the ordinary case, and admitting only the first would
    refuse every relocation out of such a vault.

    The two halves are also recovered from differently: on the
    destination the caller has built nothing yet and can simply retry,
    while on the origin the destination half has already been written
    (CAS-ADR-050 writes it first), so a live document in another vault is
    now waiting on a decision.

    ``also_accounted`` carries the origin's second admissible digest when
    it has one, so a caller refused here can see both values rather than
    inferring the other. The destination half accounts for exactly one, so
    its family declares no second digest and the constructor refuses one
    rather than emit a key that family forbids.
    """

    def __init__(
        self,
        field: str,
        pointer_hash: str,
        document_hash: str,
        also_accounted: str | None = None,
    ) -> None:
        if also_accounted is not None and field == "relocated_from":
            raise ValueError("the relocated_from half accounts for exactly one digest")
        accounted = f"{document_hash} or {also_accounted}" if also_accounted else document_hash
        detail = {
            "field": field,
            "pointer_content_hash": pointer_hash,
            "document_content_hash": document_hash,
        }
        if also_accounted is not None:
            detail["also_accounted_content_hash"] = also_accounted
        super().__init__(
            f"{field}_provenance_mismatch",
            (
                f"{field} names source content hash {pointer_hash}, which this document "
                f"does not account for: its source is {accounted}. A relocation names the "
                f"bytes that travelled between the two vaults. Correct the pointer, or "
                f"relocate the document whose source it names."
            ),
            400,
            detail,
        )


class RelocationSourceUndeliveredError(SAGEError):
    """400: a relocation named a source this vault records no digest for.

    The destination half checks the pointer against the digest this vault
    will record for the source (CAS-ADR-050). Ordinarily that digest comes
    from hashing the bytes the call delivered. A source already resident
    in the store is re-projected rather than re-delivered, and there the
    digest is inherited from the document that established the path --
    which is still a digest this vault recorded for those exact bytes, so
    such a call is checked rather than refused.

    This error is the remaining case: a resident source with no such
    document, so there is nothing to inherit. What is left is the retained
    copy's own digest, which a binding that rewrites at rest is permitted
    to make different from what produced it (CAS-ADR-043), so comparing
    the pointer against it would decide the relocation on evidence about
    the wrong bytes. Refused rather than guessed at.

    Note what the ground is, because a broader one would prove too much.
    It is not that nothing was delivered -- the inherited-record branch
    delivers nothing either and is admitted. It is that nothing here
    accounts for the bytes. Deliver the document's source, or ingest
    without the pointer and record the relocation separately.

    The refused source is named by the caller's own spelling. Every other
    spelling this error could reach for is one the service resolved and
    the caller would not recognize.
    """

    def __init__(self, source: str) -> None:
        super().__init__(
            "relocation_source_undelivered",
            (
                f"relocated_from was supplied for '{source}', whose bytes are already "
                f"resident in this vault and belong to no document here, so there is no "
                f"recorded digest to check the pointer against. Deliver the document's "
                f"source, or ingest without relocated_from."
            ),
            400,
            {"source": source},
        )


class InvalidDocTypeError(SAGEError):
    """400: doc_type not in vault's document_types config."""

    def __init__(self, doc_type: str, valid_types: set[str]) -> None:
        super().__init__(
            "invalid_doc_type",
            f"Unknown doc_type: {doc_type}",
            400,
            {"doc_type": doc_type, "valid_types": sorted(valid_types)},
        )


class LifecycleStateNotApplicableError(SAGEError):
    """409: a doc_type change would leave the document in a state its new type lacks.

    A lifecycle state may be scoped to named doc_types (CAS-ADR-054), and no
    transition moves a document into a state its doc_type cannot hold. A
    doc_type change is the one other way a document could come to hold one,
    so it is refused while the document sits in a state scoped away from the
    requested type. ``state_doc_types`` names the types the state admits; the
    remedy is to transition the document to a state the new type carries,
    then change its doc_type.
    """

    def __init__(self, current_state: str, doc_type: str, state_doc_types: list[str]) -> None:
        super().__init__(
            "lifecycle_state_not_applicable",
            f"Cannot change doc_type to {doc_type} while the document is in "
            f"{current_state}, a state scoped to {', '.join(sorted(state_doc_types))}",
            409,
            {
                "current_state": current_state,
                "doc_type": doc_type,
                "state_doc_types": sorted(state_doc_types),
            },
        )


class Tier3SchemaViolationError(SAGEError):
    """400: tier3_metadata payload failed validation.

    Two failure modes share this error code:

      (1) The resolved doc_type has no metadata_schema declared in vault
          config (strict no-loose-mode). ``path`` is empty; ``message``
          says so explicitly.
      (2) The payload was validated against the declared metadata_schema
          and failed. ``path`` is the JSON Pointer to the offending field
          (from ``jsonschema.ValidationError.json_path``); ``message`` is
          the validator's own error message.

    ``requirements`` carries the doc_type's complete declared requirement
    set, so a caller learns what would satisfy the refusal from the
    refusal itself rather than from a second call. ``path`` and
    ``message`` name one failure; they do not describe the target, and on
    mode (1) there is no schema for them to point into at all. Supplied
    wherever the vault configuration is in hand at the raise site.
    """

    def __init__(
        self,
        doc_type: str,
        path: str,
        message: str,
        instance: object | None = None,
        requirements: dict | None = None,
    ) -> None:
        detail: dict = {
            "doc_type": doc_type,
            "path": path,
            "message": message,
        }
        if instance is not None:
            detail["instance"] = instance
        if requirements is not None:
            detail["requirements"] = requirements
        super().__init__(
            "tier3_schema_violation",
            f"tier3_metadata violates schema for doc_type '{doc_type}': {message}",
            400,
            detail,
        )


class Tier3DocTypeChangeStaleKeysError(SAGEError):
    """400: doc_type is being changed in the same call as a tier3_metadata
    ops object, and the merged tier3 dict carries keys that are not in the
    new doc_type's metadata_schema properties.

    The post-merge `_validate_tier3` call would catch this as a generic
    `tier3_schema_violation` (additionalProperties: false fires on the first
    stale key), but the caller cannot tell from that error whether their
    patch is wrong for the new schema or whether they merely forgot to
    `unset` the legacy keys. This error is raised before validation runs
    and names the exact list of keys the caller must add to `unset` to
    satisfy the new schema. When the new doc_type has no metadata_schema,
    every merged key is stale by definition.
    """

    def __init__(
        self,
        document_id: str,
        previous_doc_type: str,
        new_doc_type: str,
        stale_keys: list[str],
        merged_tier3_keys: list[str],
    ) -> None:
        super().__init__(
            "tier3_doc_type_change_stale_keys",
            (
                f"Cannot change doc_type from {previous_doc_type!r} to "
                f"{new_doc_type!r} without also unsetting stale tier3_metadata "
                f"keys: {sorted(stale_keys)!r}"
            ),
            400,
            {
                "document_id": document_id,
                "previous_doc_type": previous_doc_type,
                "new_doc_type": new_doc_type,
                "stale_keys": sorted(stale_keys),
                "merged_tier3_keys": sorted(merged_tier3_keys),
            },
        )


class StaleReadError(SAGEError):
    """409: caller's `expected_version` does not match the document's
    current version at write time (CAS-ADR-038 Primitive B).

    Raised when a scalar-metadata write supplies an `expected_version`
    that does not equal the document's current `updated_at`. The detail
    envelope carries the current version so the caller can refetch
    and retry without an extra round-trip. Mirror of
    `Tier3UniqueConstraintViolation` shape per CAS-ADR-031.
    """

    def __init__(
        self,
        document_id: str,
        expected_version: str,
        current_version: str,
    ) -> None:
        super().__init__(
            "stale_read",
            (
                f"Document {document_id!r} version mismatch: "
                f"expected {expected_version!r}, current {current_version!r}"
            ),
            409,
            {
                "document_id": document_id,
                "expected_version": expected_version,
                "current_version": current_version,
            },
        )


class StaleChainHeadError(SAGEError):
    """409: caller's `expected_head_version` does not match the chain
    head's current version at supersede time (CAS-ADR-038 Primitive C).

    Raised when an ingest with `predecessor_id` supplies an
    `expected_head_version` that does not equal the predecessor's
    current `updated_at` at the moment the supersede is about to run.
    The detail envelope carries the current head id and version so the
    caller can pivot through the chain (use `current_head_id` as the
    next supersede's `predecessor_id`) and retry without an extra
    round-trip. Preserves the linear-chain invariant in CAS-ADR-023 by
    surfacing the conflict instead of letting two concurrent supersedes
    each create their own version, forking the chain into a tree.
    """

    def __init__(
        self,
        predecessor_id: str,
        expected_head_version: str,
        current_head_id: str,
        current_head_version: str,
    ) -> None:
        super().__init__(
            "stale_chain_head",
            (
                f"Chain head version mismatch for predecessor {predecessor_id!r}: "
                f"expected {expected_head_version!r}, current head "
                f"{current_head_id!r} at {current_head_version!r}"
            ),
            409,
            {
                "predecessor_id": predecessor_id,
                "expected_head_version": expected_head_version,
                "current_head_id": current_head_id,
                "current_head_version": current_head_version,
            },
        )


class ExpectedHeadVersionRequiresPredecessorError(SAGEError):
    """400: `expected_head_version` was supplied without a `predecessor_id`.

    The two parameters anchor each other: `expected_head_version` is a
    compare-and-swap token bound to the chain head identified by
    `predecessor_id`. Without a predecessor there is no chain head to
    compare against, so the parameter has no defined meaning. Surface
    the conflict explicitly rather than silently ignoring the token.
    """

    def __init__(self) -> None:
        super().__init__(
            "expected_head_version_requires_predecessor",
            (
                "`expected_head_version` requires `predecessor_id`; "
                "the version token is bound to the chain head identified "
                "by the predecessor."
            ),
            400,
            None,
        )


class Tier3UniqueConstraintViolation(SAGEError):
    """409: tier3_metadata field value collides with the declared uniqueness
    constraint (CAS-ADR-031).

    Raised by the storage substrate when an insert or supersession-insert
    would violate a `unique_keys` declaration on the resolved doc_type.
    The supersession chain is the explicit exception: a successor inherits
    its predecessor's identifier without collision because the substrate
    marks the predecessor superseded before inserting the successor, and
    the partial UNIQUE index excludes superseded rows.

    Callers detect this error to drive a retry path (e.g., the
    cas-ticket-management skill's W1.1 allocator re-runs its existence
    check and fallback scan on this 409, then propagates the error if a
    single retry does not converge).
    """

    def __init__(
        self,
        doc_type: str,
        field: str,
        colliding_value: object,
        existing_document_id: str,
    ) -> None:
        super().__init__(
            "tier3_unique_constraint_violation",
            (
                f"tier3_metadata field {field!r} value {colliding_value!r} "
                f"is already held by document {existing_document_id!r} "
                f"in doc_type {doc_type!r}"
            ),
            409,
            {
                "doc_type": doc_type,
                "field": field,
                "colliding_value": colliding_value,
                "existing_document_id": existing_document_id,
            },
        )


class ListFieldAddConflictError(SAGEError):
    """400: a ListFieldPatch.add carries one or more values already present
    on the named list-valued field (CAS-ADR-038 Primitive A).

    The error code derives from the field name: ``{field}_add_conflict``.
    The detail envelope carries ``document_id``, the conflicting subset
    keyed by the field name, and the stored list keyed
    ``current_{field}``.
    """

    def __init__(
        self,
        field: str,
        document_id: str,
        values: list[str],
        current: list[str],
    ) -> None:
        super().__init__(
            f"{field}_add_conflict",
            (f"Cannot add {field} already present on document {document_id}: {sorted(values)!r}"),
            400,
            {
                "document_id": document_id,
                field: sorted(values),
                f"current_{field}": current,
            },
        )


class ListFieldRemoveConflictError(SAGEError):
    """400: a ListFieldPatch.remove carries one or more values absent from
    the named list-valued field (CAS-ADR-038 Primitive A).

    Code: ``{field}_remove_conflict``. Detail envelope mirrors
    ``ListFieldAddConflictError``.
    """

    def __init__(
        self,
        field: str,
        document_id: str,
        values: list[str],
        current: list[str],
    ) -> None:
        super().__init__(
            f"{field}_remove_conflict",
            (f"Cannot remove {field} absent from document {document_id}: {sorted(values)!r}"),
            400,
            {
                "document_id": document_id,
                field: sorted(values),
                f"current_{field}": current,
            },
        )


class TagPatchOverlapError(SAGEError):
    """400: ListFieldPatch.add and remove lists share entries, or one list contains duplicates."""

    def __init__(self, violation: str, tags: list[str]) -> None:
        super().__init__(
            "tag_patch_overlap",
            f"ListFieldPatch invalid: {violation}",
            400,
            {"violation": violation, "tags": sorted(tags)},
        )


class Tier3UnsetConflictError(SAGEError):
    """400: Tier3Patch.unset includes one or more keys absent from the stored tier3_metadata."""

    def __init__(
        self,
        document_id: str,
        doc_type: str | None,
        keys: list[str],
        current_tier3_keys: list[str],
    ) -> None:
        super().__init__(
            "tier3_unset_conflict",
            (
                f"Cannot unset tier3_metadata keys absent on document "
                f"{document_id}: {sorted(keys)!r}"
            ),
            400,
            {
                "document_id": document_id,
                "doc_type": doc_type,
                "keys": sorted(keys),
                "current_tier3_keys": sorted(current_tier3_keys),
            },
        )


class Tier3PatchOverlapError(SAGEError):
    """400: Tier3Patch.set and unset share keys."""

    def __init__(self, keys: list[str]) -> None:
        super().__init__(
            "tier3_patch_overlap",
            f"Tier3Patch set and unset must be disjoint; overlap: {sorted(keys)!r}",
            400,
            {"keys": sorted(keys)},
        )


class PatchEmptyError(SAGEError):
    """400: a patch object was supplied but carries no actionable operation.

    Examples: ``tags={}``, ``tags={"add": []}``, ``tier3_metadata={"set": {}}``.
    Caught at Pydantic request validation; the empty-shape forms are almost
    always serialization-stripped bugs rather than intentional no-ops.
    """

    def __init__(self, field: str) -> None:
        super().__init__(
            "patch_empty",
            (
                f"{field} patch carries no actionable operation; supply a non-empty "
                "operation key or omit the field entirely"
            ),
            400,
            {"field": field},
        )


class LegacyFormError(SAGEError):
    """400: caller passed the deprecated bare-list / bare-dict shape on a patch field.

    Surfaced at the MCP boundary so callers familiar with the pre-patch
    contract receive a structured error naming the new ops-object shape
    rather than a generic Pydantic validation error.
    """

    def __init__(self, field: str, received_type: str, example: str) -> None:
        super().__init__(
            "legacy_form",
            (f"{field} no longer accepts the {received_type} form. Use the ops object: {example}"),
            400,
            {"field": field, "received_type": received_type, "example": example},
        )


class MisplacedMetadataError(SAGEError):
    """400: caller spelled nested metadata fields as top-level ingest arguments.

    ``ingest_document`` takes caller metadata nested under ``metadata``.
    The recognized keys are also published as top-level parameters, but
    only as tripwires: an unpublished parameter is stripped by MCP
    clients that coerce arguments to the published schema, so a
    misplaced field would be discarded before the server could object.
    Publishing the spellings makes the mistake reachable; this error
    makes it loud.

    Sibling of ``LegacyFormError``: both turn a wrong-shape call that
    would otherwise be silently partially applied into a structured
    rejection naming the accepted shape.
    """

    def __init__(self, fields: list[str], recognized: list[str], example: str) -> None:
        joined = ", ".join(fields)
        super().__init__(
            "misplaced_metadata",
            (
                f"{joined} must be nested under `metadata`, not passed as a "
                f"top-level argument. Use: {example}"
            ),
            400,
            {"fields": fields, "recognized": recognized, "example": example},
        )


class MisplacedTopLevelFieldError(SAGEError):
    """400: a name the operation declares was nested inside ``metadata``.

    The inverse of ``misplaced_metadata``, and a sibling rather than a
    direction on it: that error's sentence, and the meaning of its
    ``recognized``, both run the other way -- the keys that belong *inside*
    ``metadata``. Here ``recognized`` names the arguments that belong at the
    top level, which is the set the caller needs to place the one they got
    wrong.

    The confusion this answers is a real one rather than a typo:
    ``tier3_metadata`` is a top-level argument on ingest and a nested one on
    retrieval, so the same name has opposite rules on the two surfaces.
    """

    def __init__(self, fields: list[str], recognized: list[str], example: str) -> None:
        joined = ", ".join(fields)
        super().__init__(
            "misplaced_top_level_field",
            (
                f"{joined} must be passed as a top-level argument, not nested "
                f"under `metadata`. Use: {example}"
            ),
            400,
            {"fields": fields, "recognized": sorted(recognized), "example": example},
        )


class MisplacedFilterError(SAGEError):
    """400: caller spelled nested filter keys as top-level search arguments.

    ``search`` takes its scope constraints nested under ``filters``. The
    recognized keys are also published as top-level parameters, but only
    as tripwires: an unpublished parameter is stripped by MCP clients
    that coerce arguments to the published schema, so a misplaced key
    would be discarded before the server could object. Publishing the
    spellings makes the mistake reachable; this error makes it loud.

    Read-side sibling of ``MisplacedMetadataError``. The write-side case
    costs a mis-titled document, which is visible; this one costs an
    unfiltered result set, which is not -- the caller receives plausible
    rows carrying no indication that the constraint was dropped.
    """

    def __init__(self, fields: list[str], recognized: list[str], example: str) -> None:
        joined = ", ".join(fields)
        super().__init__(
            "misplaced_filters",
            (
                f"{joined} must be nested under `filters`, not passed as a "
                f"top-level argument. Use: {example}"
            ),
            400,
            {"fields": fields, "recognized": recognized, "example": example},
        )


class InvalidModeError(SAGEError):
    """400: discover mode value is not in the RetrievalMode enum."""

    def __init__(self, mode: str, valid_modes: list[str]) -> None:
        super().__init__(
            "invalid_mode",
            f"Unknown discover mode: {mode!r}. Valid modes: {sorted(valid_modes)!r}",
            400,
            {"mode": mode, "valid_modes": sorted(valid_modes)},
        )


class InvalidFilterValueError(SAGEError):
    """400: a filter value falls outside a closed vocabulary.

    Applies to filter fields typed against a Python enum, where the
    accepted set is fixed in code rather than per-vault configuration.
    Such a value can never match a stored row, so refusing it is more
    useful than returning an empty result the caller cannot distinguish
    from a genuine zero-match. The valid set travels with the error so
    the caller can self-correct without a probe round-trip.

    Vault-configured vocabularies (doc_type, lifecycle_status) cannot be
    checked here -- the accepted set is not known until the request is
    resolved against a vault -- and surface as a non-fatal hint on the
    response instead.
    """

    def __init__(self, field: str, value: object, valid_values: list[str]) -> None:
        super().__init__(
            "invalid_filter_value",
            (
                f"Invalid value {value!r} for filter {field!r}. "
                f"Valid values: {sorted(valid_values)!r}."
            ),
            400,
            {"field": field, "value": value, "valid_values": sorted(valid_values)},
        )


class StorageQueryFailedError(SAGEError):
    """500: the storage backend refused a filtered document query.

    The driver's own rejection quotes the failing statement and any
    backend hint. Returned verbatim that becomes an unstructured leak of
    internal query shape through what is otherwise a typed envelope, and
    a caller cannot act on it either way. This carries the operation that
    failed and nothing more; the driver text is logged where an operator
    can read it.

    A caller reaching this has found a defect rather than a usable
    correction, which is why it is a 500 and carries no remediation
    hint -- unlike the 400-class filter errors above, where the request
    itself is the thing to fix.
    """

    def __init__(self, operation: str) -> None:
        super().__init__(
            "storage_query_failed",
            (
                "The storage backend refused the query. This is a defect, not "
                "a correctable request; the failure has been logged for the "
                "vault operator."
            ),
            500,
            {"operation": operation},
        )


class UnknownFilterKeyError(SAGEError):
    """400: a key in `filters` is not in RetrievalFilters.

    Previously, unknown filter keys were silently dropped by Pydantic's
    default extra="ignore" behavior — a typo footgun where a misspelled
    `tickett_id` matched every row. ``extra="forbid"`` on RetrievalFilters
    now surfaces these typos as a typed error.
    """

    def __init__(self, key: str, valid_keys: list[str], example: str) -> None:
        super().__init__(
            "unknown_filter_key",
            (f"Unknown filter key {key!r}. Valid keys: {sorted(valid_keys)!r}. Example: {example}"),
            400,
            {"key": key, "valid_keys": sorted(valid_keys), "example": example},
        )


def _undeclared_key_example(parameter: str, recognized: list[str]) -> str:
    """Render a corrected call fragment for an undeclared nested key.

    The accepted names alone leave a caller to work out where the object they
    belong to sits, which for a key several segments down is the part that was
    unclear. The fragment carries both, and it is built from the model's own
    declared names, so it cannot name a key the refusal does not accept.

    Two names, not all of them: the whole set is already in ``recognized``, and
    a fragment long enough to restate it stops reading as an example.
    """
    shown = ", ".join(f'"{name}": <value>' for name in recognized[:2])
    obj = "{" + shown + (", ..." if len(recognized) > 2 else "") + "}"
    return f"{parameter}={obj}" if parameter else obj


class UndeclaredKeyError(SAGEError):
    """400: a key nested inside a request parameter is not one the model declares.

    The nested counterpart of ``unknown_parameter``, which answers for a name
    at the top of a call. Both say the caller named something that does not
    exist, which is why both are 400 rather than the 422 a malformed *value*
    gets, and both carry the accepted set so the call is repairable on first
    read: naming only the offending key costs a round trip, and the operating
    rule forbids retrying a refused call with different phrasing, so guessing
    is not a recovery.

    They stay distinct because their details are. ``unknown_parameter`` names
    the operation and the parameters it declares; a key nested inside one has
    no operation-level analogue for either, and ``recognized`` belongs to the
    model that refused. ``parameter`` locates that model -- empty when the
    refusing model is the one the call was validated against, as it is where a
    surface validates each item of a batch on its own.

    Every undeclared key in that one object is named, sorted, in ``keys``:
    naming them one per refusal would cost a round trip per key for a single
    mistake. ``key`` is the first of them. Undeclared keys in *other* objects
    are left to a later refusal rather than folded in, because ``parameter``
    and ``recognized`` describe one object; ``elsewhere`` says so in the
    message, so a caller who repairs this object is not surprised by the next.
    """

    def __init__(
        self,
        parameter: str,
        keys: Iterable[str],
        recognized: list[str],
        example: str | None = None,
        elsewhere: bool = False,
    ) -> None:
        named = sorted(set(keys))
        if not named:
            raise ValueError("an undeclared_key refusal names at least one undeclared key")
        self.elsewhere = elsewhere
        located = [f"{parameter}.{key}" if parameter else key for key in named]
        accepted = sorted(recognized)
        if example is None:
            example = _undeclared_key_example(parameter, accepted)
        subject = (
            f"{located[0]!r} is not a declared key."
            if len(located) == 1
            else f"{', '.join(repr(name) for name in located)} are not declared keys."
        )
        message = f"{subject} Accepted: {accepted!r}. Example: {example}"
        if elsewhere:
            message += (
                " Undeclared keys at other locations are reported once this object is repaired."
            )
        super().__init__(
            "undeclared_key",
            message,
            400,
            {
                "parameter": parameter,
                "key": named[0],
                "keys": named,
                "recognized": accepted,
                "example": example,
            },
        )


class InvalidFilterShapeError(SAGEError):
    """400: a value in `filters` has the wrong type for its field.

    e.g. ``filters={"tags": 42}`` passed an int where ``list[str] | None``
    was expected.
    """

    def __init__(self, field: str, expected_type: str, received_type: str) -> None:
        super().__init__(
            "invalid_filter_shape",
            (
                f"filters[{field!r}] has wrong type: expected {expected_type}, "
                f"received {received_type}"
            ),
            400,
            {
                "field": field,
                "expected_type": expected_type,
                "received_type": received_type,
            },
        )


#: Keys of the request validator's context that belong in this error's
#: published ``detail``. A whitelist rather than a blocklist, so context a
#: branch carries for its own message template -- ``key`` on the filter-key
#: branches today, whatever a future branch adds -- stays out of the
#: caller-facing envelope by default rather than by remembering to exclude it.
_MODE_MISMATCH_DETAIL_KEYS: frozenset[str] = frozenset(
    {"mode", "target", "forbidden_param", "allowed_modes", "allowed_targets"}
)

#: The two axis-scoped allowed-set keys, exactly one of which a rejection
#: carries. Sorted on the way out so ordering is a property of the envelope
#: rather than of the order a branch happened to list its members in.
_MODE_MISMATCH_ALLOWED_KEYS: tuple[str, ...] = ("allowed_modes", "allowed_targets")


class ModeParameterMismatchError(SAGEError):
    """400: a parameter is set that the chosen mode or target forbids.

    Distinct from `missing_*` codes which fire on the inverse case
    (parameter required for the chosen mode but absent — e.g.,
    `missing_query` for semantic mode). This error fires when the parameter
    IS present but is not valid — `heading_path` outside deterministic mode,
    `facet_fields` off the facets target.

    A typed envelope over the request validator's own message, not a second
    account of the rejection. The constraint is enforced in
    `DiscoverRequest._reject_mode_parameter_mismatch`, which the leaf-layer
    import contract keeps from raising this error itself; composing a
    message here from the context fields alone would lose the axis the
    branch actually constrains, since most branches constrain `target`
    while the fields available to compose from describe `mode`.

    `detail` reports both axes' current values and the allowed set for the
    constrained one — `allowed_modes` or `allowed_targets`, never both.
    """

    def __init__(self, message: str, detail: dict) -> None:
        detail = {
            key: sorted(value) if key in _MODE_MISMATCH_ALLOWED_KEYS else value
            for key, value in detail.items()
        }
        super().__init__("mode_parameter_mismatch", message, 400, detail)


class InvalidParameterError(SAGEError):
    """422: a request parameter failed validation with no more specific code.

    The general case behind the specific ones. `invalid_filter_value`,
    `invalid_filter_shape`, `invalid_mode` and the typed-alias family each
    report something this cannot -- an accepted value set, an expected type,
    an enum's members -- and are preferred wherever they apply. This code
    covers what is left: bound violations and type-coercion failures on
    ordinary request parameters, which would otherwise reach the caller with
    no envelope at all.

    Built from the structured fields of the underlying validation error
    rather than from its rendered text, so the model class name and the
    validator's documentation URL -- both present only in the rendering --
    cannot reach a caller. See `validation_error_envelope`.

    The 422 status matches what request validation already returns on the
    HTTP surface, so adopting this envelope changes the response body
    without moving any endpoint's status code.
    """

    def __init__(
        self,
        parameter: str,
        value: object,
        constraint: str,
        hint: str | None = None,
    ) -> None:
        message = f"Invalid value for parameter {parameter!r}: {constraint}."
        detail: dict = {
            "parameter": parameter,
            "value": value if isinstance(value, _JSON_NATIVE_TYPES) else str(value),
            "constraint": constraint,
        }
        if hint is not None:
            message = f"{message} {hint}"
            detail["hint"] = hint
        super().__init__("invalid_parameter", message, 422, detail)


class UnknownParameterError(SAGEError):
    """400: a call carried a parameter name the operation does not declare.

    Raised at each request surface's framework boundary rather than in any
    operation, so an undeclared name is refused before the operation runs
    instead of being discarded (CAS-ADR-037). Both surfaces build it here, so
    a caller meets one refusal shape whichever surface it reached
    (CAS-ADR-052).

    ``tool`` is the operation's name -- the tool name on the MCP surface, the
    published operation id on the HTTP surface, which are the same name.
    ``valid_params`` lists what the operation accepts where the rejected names
    were sent, so the caller can correct the call in one round-trip.
    """

    def __init__(self, tool: str, rejected_params: list[str], valid_params: list[str]) -> None:
        super().__init__(
            "unknown_parameter",
            f"Tool {tool!r} received unknown parameter(s): {rejected_params}.",
            400,
            {
                "tool": tool,
                "rejected_params": rejected_params,
                "valid_params": valid_params,
            },
        )


class PipelineIncompleteError(SAGEError):
    """422: document has incomplete/failed pipeline."""

    def __init__(self, document_id: str) -> None:
        super().__init__(
            "pipeline_incomplete",
            f"Document {document_id} has incomplete pipeline",
            422,
            {"document_id": document_id},
        )


class HeadingNotFoundError(SAGEError):
    """404: heading path not found in document (BH-030).

    ``available_headings`` is the full enumeration. ``candidate_matches``
    is a substring-match shortlist (case-insensitive) computed by the
    caller — useful when the query is the *tail* of a stored path (e.g.
    "CLAIMS" against a stored "CLAIMS -- Remove Before Filing") so the
    caller can retry with the exact path in one extra round-trip.
    """

    def __init__(
        self,
        heading_path: str,
        document_id: str,
        available_headings: list[str] | None = None,
        candidate_matches: list[str] | None = None,
    ) -> None:
        detail: dict = {"heading_path": heading_path, "document_id": document_id}
        if candidate_matches:
            detail["candidate_matches"] = candidate_matches
        if available_headings is not None:
            detail["available_headings"] = available_headings
        super().__init__(
            "heading_not_found",
            f"Heading '{heading_path}' not found in document {document_id}",
            404,
            detail,
        )


class SelfReferentialEdgeError(SAGEError):
    """400: source_id and target_id are the same document."""

    def __init__(self, document_id: str) -> None:
        super().__init__(
            "self_referential_edge",
            f"Cannot create edge from document {document_id} to itself",
            400,
            {"document_id": document_id},
        )


class AdapterNotFoundError(SAGEError):
    """400: no adapter registered for source type."""

    def __init__(self, adapter: str) -> None:
        super().__init__(
            "adapter_not_found",
            f"No adapter registered for source type: {adapter}",
            400,
        )


class SourceTypeUnresolvedError(SAGEError):
    """400: no source type was supplied and none could be inferred.

    Inference reads the extension of the source being ingested against the
    registered adapters' declared extensions, and refuses rather than guesses
    when none claims it, because routing bytes to the wrong adapter is worse
    than an explicit failure. The detail names only the extension -- never the
    path, which for a two-phase delivery is a server-side staging location --
    and the source types the caller may supply instead.
    """

    def __init__(self, extension: str | None, registered_source_types: list[str]) -> None:
        subject = (
            f"extension {extension!r} is not claimed by any registered adapter"
            if extension
            else "the source has no extension"
        )
        super().__init__(
            "source_type_unresolved",
            f"source_type was not supplied and could not be inferred: {subject}. "
            f"Supply source_type as one of: {', '.join(registered_source_types)}.",
            400,
            {"extension": extension, "registered_source_types": registered_source_types},
        )


class AdapterConfigInvalidError(SAGEError):
    """400: the source adapter refused a config value it cannot use.

    The value reaches the adapter from the vault's ``adapter_defaults`` merged
    with any per-request ``config``, so the detail names the source type, the
    key and the value as supplied, and the message carries the adapter's own
    statement of what it accepts.

    Exists because the adapter raises a plain ``ValueError`` subclass: it sits
    below the API layer and may not import it, so without a translation at the
    service boundary the refusal reaches an MCP caller as a generic internal
    error and an HTTP caller as a bare 500 against a spec that declares neither.
    """

    def __init__(self, source_type: str, key: str, value: object, reason: str) -> None:
        super().__init__(
            "adapter_config_invalid",
            reason,
            400,
            {"source_type": source_type, "key": key, "value": value},
        )


class SourceUnreadableError(SAGEError):
    """400: the source adapter could not read the source.

    The file is malformed, truncated, encrypted, or not in the format its source
    type names, which is the caller's to correct. The detail names the source type
    and the source in the spelling the caller used, and the message carries the
    adapter's own statement of what failed. A failure the adapter does not report
    as a read failure is not this error, so a server fault is never presented as a
    fault in the caller's file.
    """

    def __init__(self, source_type: str, source_path: str, reason: str) -> None:
        super().__init__(
            "source_unreadable",
            reason,
            400,
            {"source_type": source_type, "source_path": source_path},
        )


class SourceFileNotFoundError(SAGEError):
    """404: source file does not exist at the resolved path."""

    def __init__(self, source: str) -> None:
        super().__init__(
            "source_file_not_found",
            f"Source file not found: {source}",
            404,
            {"source": source},
        )


class DocumentScopeUnmatchedError(SAGEError):
    """404: a document scope names ids with no document in the vault.

    Refused rather than narrowed: auditing only the ids that matched would
    return a report the caller reads as covering everything it asked about,
    and a misspelled id would yield a clean report over nothing. ``detail``
    carries the unmatched ids, sorted, so the caller can correct exactly those.
    """

    def __init__(self, unmatched_ids: list[str]) -> None:
        ordered = sorted(unmatched_ids)
        super().__init__(
            "document_scope_unmatched",
            f"No document in the vault matches {len(ordered)} of the scoped ids: "
            f"{', '.join(ordered)}",
            404,
            {"unmatched_ids": ordered},
        )


class RestoreTargetUnresolvedError(SAGEError):
    """404: no single document claims the delivered bytes as its provenance.

    A restore names its target by content: the bytes handed over must be the
    ones some document was ingested from. Nothing matched, or more than one
    document did, so there is no unambiguous copy to write over -- and writing
    to a guessed path would corrupt a document that was intact. The caller
    resolves it by supplying ``document_id``.
    """

    def __init__(self, content_hash: str, candidate_ids: list[str]) -> None:
        if candidate_ids:
            message = (
                f"{len(candidate_ids)} documents share the delivered bytes' digest "
                f"{content_hash}; supply document_id to name the one to restore."
            )
        else:
            message = (
                f"No document was ingested from the delivered bytes (digest "
                f"{content_hash}). Deliver the original source bytes, or supply "
                f"document_id to name the target explicitly."
            )
        super().__init__(
            "restore_target_unresolved",
            message,
            404,
            {"content_hash": content_hash, "candidate_ids": candidate_ids},
        )


class RestoreProvenanceMismatchError(SAGEError):
    """400: the pinned document was not ingested from the delivered bytes.

    ``document_id`` names which copy to write over; it does not license writing
    *arbitrary* bytes there. Without this check a pin turns the repair into its
    opposite: the delivered file overwrites the retained copy and the record is
    refreshed to describe it, so the integrity audit goes green over a document
    whose stored bytes are now something else entirely -- erasing the very
    evidence the audit exists to preserve.

    Not raised for a record whose ``stored_content_hash`` is null. Such a record
    predates the delivered/stored digest split and its provenance hash describes
    the stored copy rather than the delivered bytes, so a caller re-delivering
    the original cannot match it -- which is the case the pin exists to serve.
    The refresh rule in ``restore_vault_source_file`` is what keeps that
    exemption from laundering: a store that returns what it was handed licenses
    no digest update.
    """

    def __init__(self, document_id: str, delivered_hash: str, recorded_hash: str) -> None:
        super().__init__(
            "restore_provenance_mismatch",
            (
                f"Document {document_id} was not ingested from the delivered bytes "
                f"(delivered {delivered_hash}, recorded {recorded_hash}). Deliver "
                f"that document's original source bytes, or drop document_id to "
                f"resolve the target from the bytes themselves."
            ),
            400,
            {
                "document_id": document_id,
                "delivered_content_hash": delivered_hash,
                "recorded_content_hash": recorded_hash,
            },
        )


class VaultSourcePathRefusedError(SAGEError):
    """400: the vault-source store refused to write at the path it was given.

    Carries the binding's own reason rather than restating one. A binding
    refuses a write target for several distinct causes -- the path is absolute,
    it walks out of the vault's source tree, a symlink or a directory sits at
    the destination, it resolves outside the source root through an ancestor,
    or something that is not a directory sits where its parent belongs -- and
    a fixed message can only describe one of them, leaving the others reported
    as something that did not happen. The reason names the destination by its
    vault-relative spelling, never by a server-side absolute path.

    Exists because the binding raises a plain ``ValueError`` subclass: it sits
    below the API layer and may not import it, so without a translation at the
    service boundary the refusal reaches an MCP caller as a generic internal
    error and an HTTP caller as a bare 500 against a spec that declares neither.
    """

    def __init__(self, source_path: str, reason: str) -> None:
        super().__init__(
            "vault_source_path_refused",
            reason,
            400,
            {"source_path": source_path},
        )


class VaultSourceStoreRefusedError(SAGEError):
    """502: the vault-source store refused the operation on its merits.

    The store was reachable and answered; it declined. A quota it will not
    exceed, a permission it no longer grants, a reply that accepted an upload
    session and named no URL to write to, a session it committed at the wrong
    fragment. Repeating the request reproduces the answer, so the operator has
    to act on the store before the operation can succeed --
    :class:`VaultSourceStoreUnavailableError` is the counterpart for the
    refusals where waiting is the whole remedy.

    502 rather than a 4xx: the request that reached SAGE was well formed, and
    the fault is an upstream one SAGE is reporting rather than committing.

    The message is composed here rather than forwarded from the binding. The
    store's own response body names its cause precisely and is written to the
    log for that reason, but it is the store's text, can carry tenant
    coordinates, and would become a declared part of this API's surface if it
    travelled on the error.
    """

    def __init__(self, source_path: str, operation: str, store_status: int | None = None) -> None:
        detail: dict = {"source_path": source_path, "operation": operation}
        if store_status is not None:
            detail["store_status"] = store_status
        super().__init__(
            "vault_source_store_refused",
            (
                f"The vault-source store refused to {operation} for "
                f"{source_path!r}. The refusal is not transient: resolve it at "
                f"the store before retrying."
            ),
            502,
            detail,
        )


class VaultSourceStoreUnavailableError(SAGEError):
    """503: the vault-source store declined to serve the operation just now.

    Throttling that outlasted the binding's one retry, a transient backend
    signal, an upload session the store expired or that was interrupted. The
    request was never judged on its merits, so the same one can succeed later
    unchanged -- which is the whole difference from
    :class:`VaultSourceStoreRefusedError`, and the reason the two are separate
    codes rather than one code with a flag: a caller reads the code to decide
    whether to retry or to escalate, and a flag inside a detail dict is easy to
    miss and easy to leave unread.

    Carries the same curated message discipline as its non-transient
    counterpart: the store's own body goes to the log, not to the caller.
    """

    def __init__(self, source_path: str, operation: str, store_status: int | None = None) -> None:
        detail: dict = {"source_path": source_path, "operation": operation}
        if store_status is not None:
            detail["store_status"] = store_status
        super().__init__(
            "vault_source_store_unavailable",
            (
                f"The vault-source store could not {operation} for "
                f"{source_path!r} just now. The same request may succeed on a "
                f"later attempt."
            ),
            503,
            detail,
        )


class RestoreSourceNotAbsoluteError(SAGEError):
    """400: the restore source path is not absolute.

    A restore reads bytes from the *caller's* filesystem, so the path names a
    file there and must say so unambiguously. A relative path has no defined
    meaning on this operation -- unlike an ingest, where it addresses a source
    already inside the vault -- and would otherwise resolve against the server
    process's working directory, which on a deployed profile is the container's,
    not the caller's. Refusing it also keeps the caller-local delivery gate
    honest: that gate triggers on an absolute path, so a relative one would slip
    past it and be read server-side instead of prompting an upload.
    """

    def __init__(self, source: str) -> None:
        super().__init__(
            "restore_source_not_absolute",
            f"Restore source must be an absolute path: {source}",
            400,
            {"source": source},
        )


class PathTraversalDeniedError(SAGEError):
    """400: output_path resolves outside vault storage_root (BH-038, BH-040)."""

    def __init__(self, output_path: str) -> None:
        super().__init__(
            "path_traversal_denied",
            f"Path resolves outside vault storage root: {output_path}",
            400,
            {"output_path": output_path},
        )


class OutputPathInvalidError(SAGEError):
    """400: output_path lies inside the storage root but is not a file location.

    An export writes one file, so a directory already at the target, or a file
    where one of its parent directories is needed, makes the path unusable.
    The path is refused before anything is read or written, as a path outside
    the root is, and ``reason`` says which.
    """

    def __init__(self, output_path: str, reason: str) -> None:
        super().__init__(
            "output_path_invalid",
            f"Cannot export to {output_path}: {reason}",
            400,
            {"output_path": output_path, "reason": reason},
        )


class NoProjectionError(SAGEError):
    """404: document has no stored projection."""

    def __init__(self, document_id: str) -> None:
        super().__init__(
            "no_projection",
            f"No projection stored for document {document_id}",
            404,
            {"document_id": document_id},
        )


class AssertionsFileNotFoundError(SAGEError):
    """404: retrieval health assertions file not found (BH-042).

    The vault config references an ``assertions_file`` that does not exist at
    its path in the vault-source store -- a missing resource, so 404, distinct
    from ``AssertionsNotConfiguredError`` (no file configured at all, a 400
    precondition).
    """

    def __init__(self, path: str) -> None:
        super().__init__(
            "assertions_file_not_found",
            f"Assertions file not found: {path}",
            404,
            {"assertions_file": path},
        )


class AssertionsFileInvalidError(SAGEError):
    """400: retrieval health assertions file is malformed (BH-042)."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(
            "assertions_file_invalid",
            f"Assertions file invalid: {reason}",
            400,
            {"assertions_file": path, "reason": reason},
        )


class AssertionsNotConfiguredError(SAGEError):
    """400: no assertions_file configured in vault config."""

    def __init__(self) -> None:
        super().__init__(
            "assertions_not_configured",
            "No retrieval_health.assertions_file configured for this vault",
            400,
        )


class VaultNotFoundError(SAGEError):
    """404: vault_id names no registered vault.

    The one refusal for an unregistered vault on every request surface
    (CAS-ADR-052). It names the vaults that are registered, so a caller can
    correct the id without a second call.
    """

    def __init__(self, vault_id: str, *, available_vaults: Iterable[str]) -> None:
        available = sorted(available_vaults)
        super().__init__(
            "vault_not_found",
            f"Vault '{vault_id}' not found. Available vaults: {', '.join(available) or '(none)'}",
            404,
            {"vault_id": vault_id, "available_vaults": available},
        )


class VaultConfigValidationError(SAGEError):
    """400: vault config failed Pydantic validation."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__(
            "vault_config_validation_error",
            "Vault configuration is invalid",
            400,
            {"errors": errors},
        )


class VaultAlreadyExistsError(SAGEError):
    """409: vault_id already registered."""

    def __init__(self, vault_id: str) -> None:
        super().__init__(
            "vault_already_exists",
            f"Vault '{vault_id}' already exists",
            409,
            {"vault_id": vault_id},
        )


class ReabstractAlreadyInFlightError(SAGEError):
    """409: a reabstract_deferred operation is already running on the vault.

    Single-flight per vault: a second concurrent caller receives this
    structured error with the start_time of the running operation rather
    than queueing. The non-blocking rejection (vs. await lock.acquire())
    is intentional -- reabstract passes can run for minutes against the
    in-process Qwen3 provider, and silently queuing a second long-running
    caller would mask client-side coordination bugs.
    """

    def __init__(self, vault_id: str, start_time: datetime) -> None:
        super().__init__(
            "reabstract_already_in_flight",
            (
                f"A reabstract_deferred operation is already running on vault "
                f"{vault_id!r}; started at {start_time.isoformat()}."
            ),
            409,
            {"vault_id": vault_id, "start_time": start_time.isoformat()},
        )


class ReabstractDocumentAlreadyInFlightError(SAGEError):
    """409: a per-document reabstract is already running for this document_id.

    Single-flight per document: a second concurrent caller against the same
    document receives this structured error with the start_time of the
    running operation rather than dispatching a parallel background task.
    Concurrent reabstract calls against different document_ids in the
    same vault continue to run in parallel.
    """

    def __init__(self, document_id: str, start_time: datetime) -> None:
        super().__init__(
            "reabstract_document_already_in_flight",
            (
                f"A reabstract is already running on document "
                f"{document_id!r}; started at {start_time.isoformat()}."
            ),
            409,
            {"document_id": document_id, "start_time": start_time.isoformat()},
        )


class RecomputePipelineAlreadyInFlightError(SAGEError):
    """409: a per-document recompute_pipeline is already running for this document_id.

    Single-flight per document: a second concurrent caller against the same
    document receives this structured error with the start_time of the
    running operation rather than dispatching a parallel background task.
    Concurrent calls against different document_ids in the same vault
    continue to run in parallel.
    """

    def __init__(self, document_id: str, start_time: datetime) -> None:
        super().__init__(
            "recompute_pipeline_already_in_flight",
            (
                f"A recompute_pipeline is already running on document "
                f"{document_id!r}; started at {start_time.isoformat()}."
            ),
            409,
            {"document_id": document_id, "start_time": start_time.isoformat()},
        )


class PipelineWorkInFlightError(SAGEError):
    """409: migrate_vault refused because pipeline work is in flight on the vault."""

    def __init__(self, vault_id: str) -> None:
        message = f"Pipeline work is queued or running on vault {vault_id!r}."
        super().__init__("pipeline_work_in_flight", message, 409, {"vault_id": vault_id})


class VaultMigrationInFlightError(SAGEError):
    """409: refused because migrate_vault is running on the vault."""

    def __init__(self, vault_id: str, start_time: datetime) -> None:
        detail = {"vault_id": vault_id, "start_time": start_time.isoformat()}
        message = f"migrate_vault is running on vault {vault_id!r}, since {detail['start_time']}."
        super().__init__("vault_migration_in_flight", message, 409, detail)


class DestructiveConfigChangeError(SAGEError):
    """409: vault config update would orphan existing documents.

    Raised when the merged config removes a doc_type or lifecycle state
    that still has documents attached, and the caller has not passed
    force=True (MCP) or ?force=true (REST).
    """

    def __init__(self, warnings: list[str]) -> None:
        super().__init__(
            "destructive_config_change",
            (
                "Vault configuration update would orphan existing documents. "
                "Pass force=true to proceed."
            ),
            409,
            {"warnings": warnings},
        )


class EdgeNotFoundError(SAGEError):
    """404: production edge not found."""

    def __init__(self, edge_id: str) -> None:
        super().__init__(
            "edge_not_found",
            f"Edge '{edge_id}' not found",
            404,
            {"edge_id": edge_id},
        )


class StagingEdgeNotFoundError(SAGEError):
    """404: staging edge not found (already confirmed/dismissed or never existed)."""

    def __init__(self, edge_id: str) -> None:
        super().__init__(
            "staging_edge_not_found",
            f"Staging edge '{edge_id}' not found",
            404,
            {"edge_id": edge_id},
        )


class ContentTooLargeError(SAGEError):
    """413: document file exceeds the inline content size ceiling (BH-118)."""

    def __init__(self, document_id: str, size_bytes: int, max_bytes: int) -> None:
        super().__init__(
            "content_too_large",
            (
                f"Document {document_id} file size {size_bytes} bytes exceeds "
                f"the inline content ceiling of {max_bytes} bytes"
            ),
            413,
            {
                "document_id": document_id,
                "size_bytes": size_bytes,
                "max_bytes": max_bytes,
            },
        )


class ContentFileMissingError(SAGEError):
    """404: document record exists but the vault-local file is absent (BH-119)."""

    def __init__(self, document_id: str, source_path: str) -> None:
        super().__init__(
            "content_file_missing",
            (f"Document {document_id} file is missing at vault-relative path {source_path}"),
            404,
            {"document_id": document_id, "source_path": source_path},
        )


class SupersedeTargetNotActiveError(SAGEError):
    """409: supersede is not legal from the target's state (BH-122).

    `allowed_states` comes from the vault's lifecycle transition table --
    the states that declare a `supersede` transition -- so the reported
    precondition tracks the vault's configuration instead of restating
    it. The error's name reflects a single-state rule that no table is
    obliged to hold: the create-vault scaffold declares `supersede` from
    `active` and from `completed`, so a vault built from it reports both
    rather than being rejected against a rule it does not hold. Read
    `allowed_states`, never the name. A vault whose table permits
    `supersede` from
    no state at all reports the empty set as such: substituting a state
    the vault does not permit would misreport the precondition.

    `required_state` renders the same set for humans through the shared
    state-set renderer, so it reads identically to every other
    config-derived precondition, and remains in the detail payload for
    callers that key remediation prose off it.
    """

    def __init__(
        self,
        predecessor_id: str,
        current_state: str,
        allowed_states: list[str] | None = None,
    ) -> None:
        states = sorted(allowed_states or [])
        required_state = render_state_set(states)
        super().__init__(
            "supersede_target_not_active",
            (
                f"Cannot supersede document {predecessor_id}: current state "
                f"'{current_state}', required '{required_state}'"
            ),
            409,
            {
                "predecessor_id": predecessor_id,
                "current_state": current_state,
                "required_state": required_state,
                "allowed_states": states,
            },
        )


class WritePathExistsError(SAGEError):
    """409: write_to_path target already exists (BH-126)."""

    def __init__(self, write_to_path: str) -> None:
        super().__init__(
            "write_path_exists",
            f"Target path already exists: {write_to_path}",
            409,
            {"write_to_path": write_to_path},
        )


class WritePathInvalidError(SAGEError):
    """400: invalid write_to_path or a failed exclusive open (BH-127).

    Covers a non-absolute path, a missing or unwritable parent, and a target
    that cannot be opened for exclusive creation after validation. An existing
    target instead reports write_path_exists (409); failures after opening
    are not translated into this error.
    """

    def __init__(self, write_to_path: str, reason: str) -> None:
        super().__init__(
            "write_path_invalid",
            f"Cannot write to {write_to_path}: {reason}",
            400,
            {"write_to_path": write_to_path, "reason": reason},
        )


class ContentDeliveryConflictError(SAGEError):
    """400: caller set both include_content and write_to_path (BH-128)."""

    def __init__(self) -> None:
        super().__init__(
            "content_delivery_conflict",
            "include_content and write_to_path are mutually exclusive",
            400,
        )


class BinaryContentRefusedError(SAGEError):
    """400: include_content was requested against a binary-container source.

    A document whose source adapter is a binary container (``.docx``,
    ``.pptx``, ``.pdf``, ``.xlsx``) holds raw container bytes, not scannable text. The
    read path declines to inline those bytes so a caller cannot scan a
    binary container as though it were text and read a confident false
    negative. The readable content lives in the extracted-text projection;
    the detail directs the caller to ``read_projection`` (CAS-ADR-039).
    Distinct from ``document_not_found``: the document exists and is
    readable, only not via raw-byte delivery.
    """

    def __init__(self, document_id: str, source_type: str) -> None:
        super().__init__(
            "binary_content_refused",
            (
                f"Refusing to inline raw bytes for document {document_id}: "
                f"source type {source_type!r} is a binary container, not "
                f"scannable text. Use read_projection for the extracted text."
            ),
            400,
            {
                "document_id": document_id,
                "source_type": source_type,
                "use_instead": "read_projection",
            },
        )


class LocalOpenNotAvailableError(SAGEError):
    """501: the host OS opener is a local-profile affordance, gated off under cloud.

    ``POST /documents/{id}/open`` shells out the host OS opener
    (``open``/``xdg-open``/``startfile``), which is meaningful only when the
    browser and SAGE share a machine (the local profile). Under the cloud profile
    SAGE is a headless container with no desktop, so the opener is gated off and a
    caller delivers a document to the browser through a store-issued download URL
    (``GET /documents/{id}/download-url``) instead. Mirrors the
    "capability unavailable in this deployment" shape the co-located-only surfaces
    use.
    """

    def __init__(self) -> None:
        super().__init__(
            "local_open_only",
            (
                "The host OS opener is a local-profile affordance and is not "
                "available under the cloud profile; request a download URL instead."
            ),
            501,
        )


class DownloadUrlNotAvailableError(SAGEError):
    """501: the active vault-source binding cannot issue a source download URL.

    A store-issued download URL is a richer-binding capability (CAS-ADR-043): the
    document-store binding backs it with a short-lived pre-authenticated URL, while
    the filesystem binding has no equivalent. When the active binding lacks the
    capability the request is refused with this structured 501 rather than a bare
    failure, so a caller learns the deployment does not offer browser delivery for
    this document.
    """

    def __init__(self, document_id: str) -> None:
        super().__init__(
            "download_url_unavailable",
            (
                f"The active vault-source binding cannot issue a download URL for "
                f"document {document_id}."
            ),
            501,
            {"document_id": document_id},
        )


class CallerFilesystemUnavailableError(SAGEError):
    """501: an operation needs a filesystem the caller and server share.

    Under the cloud profile SAGE runs as a remote container, and the two sides
    see disjoint filesystems. That defeats an operation in either direction:
    one taking a caller-supplied local path, which the server cannot resolve,
    and one whose output lands in the server's own vault tree, which the caller
    cannot reach (a projection export, the browsable symlink views). The
    byte-moving path tools answer with a transfer recipe instead, but
    ``list_directory`` has no byte leg to hand off -- walking and
    content-hashing a directory only makes sense against the caller's own tree
    -- and an output written into the container has no one to read it, so each
    is refused with this structured error naming the in-request alternative.
    Mirrors the "capability unavailable in this deployment" shape of
    :class:`LocalOpenNotAvailableError` and
    :class:`DownloadUrlNotAvailableError` (CAS-ADR-042 constraint 1: the
    caller-visible surface stays profile-invariant; the per-profile byte
    transport below it is a binding detail).
    """

    def __init__(self, operation: str, remedy: str) -> None:
        super().__init__(
            "caller_filesystem_unavailable",
            (
                f"{operation} requires a filesystem shared between the caller and "
                f"the SAGE server, which the cloud profile does not provide; {remedy}."
            ),
            501,
            {"operation": operation, "remedy": remedy},
        )


class TransferTokenInvalidError(SAGEError):
    """410: a transfer token does not name a redeemable pending transfer.

    Transfer tokens are short-lived, one-time, direction-scoped credentials
    minted by an authenticated call and redeemed against the transfer
    endpoints. One code covers every unredeemable state -- unknown, expired,
    already consumed, wrong direction, or wrong vault -- so the error surface
    is not an oracle distinguishing a token that never existed from one that
    just expired. The remedy is always the same: re-issue the originating
    call to mint a fresh token.

    That remedy is the only one on offer for an expired token, deliberately
    and per CAS-ADR-045, which re-mints a lapsed token by re-issuing the
    originating call and leaves resumable sessions unspecified until a
    payload class demands them.
    A lapsed handshake is not resumable: reclamation destroys the staging
    directory along with the entry, so bytes already delivered survive no
    better than bytes never sent, and there is nothing for a second
    redemption to redeem. Offering one would mean retaining expired entries
    behind a grace state and answering "expired, try again" where this code
    answers only "not redeemable" -- reintroducing exactly the oracle the
    single code exists to avoid. Retries *within* the window are a different
    matter and are already supported: a byte leg that fails reopens its
    entry, up to the token's refusal limit, and a completion that fails after
    redemption hands the token back with its staged bytes intact.
    """

    def __init__(self) -> None:
        super().__init__(
            "transfer_token_invalid",
            (
                "The transfer token does not name a redeemable pending "
                "transfer (unknown, expired, already used, or scoped to a "
                "different direction or vault); re-issue the originating "
                "call to mint a fresh token."
            ),
            410,
        )


class TransferNotStagedError(SAGEError):
    """409: an upload's completion call arrived before its bytes.

    Completing a caller-local ingest is a two-step exchange: the caller's
    environment first delivers the bytes to the upload endpoint, then the
    completion call redeems the token against the staged bytes. Arriving
    here without a successful upload leg is an ordering error, not a token
    error -- the token stays valid, and the remedy is to run the upload leg.
    """

    def __init__(self, transfer_id: str) -> None:
        super().__init__(
            "transfer_not_staged",
            (
                f"Transfer {transfer_id} has no staged bytes yet; deliver the "
                f"file to the upload endpoint with the transfer token, then "
                f"repeat this call."
            ),
            409,
            {"transfer_id": transfer_id},
        )


class TransferAlreadyStagedError(SAGEError):
    """409: a second upload attempted against an already-staged transfer.

    Each upload token admits exactly one successful byte delivery; the staged
    bytes then wait for the completion call. A repeat delivery is refused
    rather than silently overwriting the staged bytes, so a duplicated curl
    cannot race the completion.
    """

    def __init__(self, transfer_id: str) -> None:
        super().__init__(
            "transfer_token_already_used",
            (
                f"Transfer {transfer_id} already holds staged bytes; complete "
                f"the originating call, or mint a fresh token to re-send."
            ),
            409,
            {"transfer_id": transfer_id},
        )


class TransferContentTooLargeError(SAGEError):
    """413: an upload exceeded the transfer byte ceiling mid-stream.

    The upload endpoint bounds the body while streaming it to staging, so an
    oversize payload is aborted at the ceiling instead of filling the
    container's disk; the partial staging file is removed and the token
    reverts to retryable, until its refusal limit is reached. The ceiling is
    ``SAGE_MAX_TRANSFER_BYTES`` (default 100 MB).
    """

    def __init__(self, max_bytes: int) -> None:
        super().__init__(
            "transfer_content_too_large",
            (
                f"Upload exceeded the transfer ceiling of {max_bytes} bytes "
                f"and was aborted; send a smaller file or raise "
                f"SAGE_MAX_TRANSFER_BYTES."
            ),
            413,
            {"max_bytes": max_bytes},
        )


class TransferRefusalLimitError(SAGEError):
    """410: a refused byte delivery exhausted its upload token.

    A delivery the upload endpoint refuses -- over the ceiling, not the bound
    digest, or abandoned mid-body -- stages nothing and leaves the token
    retryable, but only up to the token's refusal limit
    (``transfer.max_refused_deliveries``). The refusal that reaches it
    reclaims the transfer, so a disclosed token cannot be used to stream up
    to the ceiling for as long as it lives. The remedy is the one a lapsed
    token has: re-issue the originating call for a fresh recipe.

    The byte leg is authenticated by the token alone, so the refusal names
    only what the presenter already knows -- the transfer it named and the
    limit its own deliveries reached. Any later presentation of the token
    meets ``transfer_token_invalid``, like any other unredeemable token.
    """

    def __init__(self, transfer_id: str, max_refused_deliveries: int) -> None:
        super().__init__(
            "transfer_refusal_limit_reached",
            (
                f"Transfer {transfer_id} was refused {max_refused_deliveries} "
                f"deliveries and has been reclaimed; re-issue the originating "
                f"call to mint a fresh token."
            ),
            410,
            {"transfer_id": transfer_id, "max_refused_deliveries": max_refused_deliveries},
        )


class SourceDigestMismatchError(SAGEError):
    """400: the bytes delivered are not the bytes the caller declared.

    A caller may declare the ``sha256`` of the file it means to ingest. The
    declaration is held wherever the bytes are first seen: on the upload leg,
    against the digest an upload token was bound to at mint, and on the
    ingest itself, against the digest of the source it reads. Both refuse
    before anything is staged or retained, so the same call with the right
    bytes succeeds, and on the upload leg the token stays unspent, short of
    its refusal limit (see ``TransferRefusalLimitError``).

    The two arms disclose differently. The upload leg is authenticated by the
    token alone, so its refusal names only the transfer and the digest of the
    bytes just presented -- never the bound digest, which would tell the
    holder of a leaked token which file to send. The ingest arm is an
    authenticated call reporting on the caller's own declaration, and names
    the source and both digests.
    """

    def __init__(
        self,
        delivered_sha256: str | None,
        *,
        transfer_id: str | None = None,
        source: str | None = None,
        declared_sha256: str | None = None,
    ) -> None:
        detail: dict[str, str | None]
        if transfer_id is not None:
            message = (
                f"The bytes delivered to transfer {transfer_id} ({delivered_sha256}) "
                f"are not the file its token was minted for; nothing was staged. "
                f"Deliver that file with the same token."
            )
            detail = {"transfer_id": transfer_id, "delivered_sha256": delivered_sha256}
        elif delivered_sha256 is None:
            message = (
                f"{source} is resident in the vault's store with no recorded digest, so "
                f"the declared sha256 {declared_sha256} cannot be checked; nothing was "
                f"ingested. Deliver the file's bytes instead."
            )
            detail = {
                "source": source,
                "declared_sha256": declared_sha256,
                "delivered_sha256": None,
            }
        else:
            message = (
                f"{source} has content hash {delivered_sha256}, not the declared "
                f"sha256 {declared_sha256}; nothing was ingested. Declare the digest "
                f"of the file being ingested, or ingest the file it names."
            )
            detail = {
                "source": source,
                "declared_sha256": declared_sha256,
                "delivered_sha256": delivered_sha256,
            }
        super().__init__("source_digest_mismatch", message, 400, detail)


class TransferEndpointNotConfiguredError(SAGEError):
    """500: this deployment cannot mint transfer recipes.

    Minting a recipe requires the stack config to declare the public base URL
    the caller's environment can reach (``transfer.public_base_url``). A
    deployment that needs the caller-local byte channel but does not declare
    it fails loud at mint time with this structured error, rather than
    emitting a recipe whose URL cannot work.
    """

    def __init__(self) -> None:
        super().__init__(
            "transfer_endpoint_not_configured",
            (
                "This deployment does not declare transfer.public_base_url in "
                "its stack config, so no transfer recipe can be minted."
            ),
            500,
        )


class AmbiguousIngestSourceError(SAGEError):
    """400: both a path ``source`` and a ``transfer_token`` were supplied.

    The two are mutually exclusive delivery shapes for the same logical source:
    a ``source`` path, or a ``transfer_token`` redeeming bytes already
    delivered to the upload endpoint. Supplying both is refused so the caller
    learns which to drop, mirroring the exactly-one-of contract of
    :class:`AmbiguousDocumentIdentifierError`.

    ``source_parameter`` is the name the calling tool gives the path, so the
    message names the parameter that caller can actually correct.
    """

    def __init__(self, *, source_parameter: str) -> None:
        super().__init__(
            "ambiguous_ingest_source",
            (
                f"Supply exactly one of `{source_parameter}` (a source file path) or "
                "`transfer_token` (redeeming an already-delivered upload); "
                "both were provided."
            ),
            400,
        )


class MissingIngestSourceError(SAGEError):
    """400: neither a path ``source`` nor a ``transfer_token`` was supplied.

    Companion to :class:`AmbiguousIngestSourceError`: a document to ingest must
    arrive by exactly one of the two delivery shapes, and neither was provided.
    ``source_parameter`` names the path as its sibling's does.
    """

    def __init__(self, *, source_parameter: str) -> None:
        super().__init__(
            "missing_ingest_source",
            (
                f"Supply exactly one of `{source_parameter}` (a source file path) or "
                "`transfer_token` (redeeming an already-delivered upload); "
                "neither was provided."
            ),
            400,
        )


class DeliveryParameterConflictError(SAGEError):
    """400: an explicit ``delivery`` mode contradicts the write_to_path argument.

    Raised by content read tools that accept a ``delivery`` selector
    (``inline | spill | auto``) when the requested mode cannot be honored:
    ``delivery="inline"`` was combined with a ``write_to_path`` target, or
    ``delivery="spill"`` was requested without one. The contradiction is
    refused rather than silently resolved so the caller learns which of the
    two arguments to drop.
    """

    def __init__(self, delivery: str, reason: str) -> None:
        super().__init__(
            "delivery_conflict",
            f"delivery={delivery!r} conflicts with write_to_path: {reason}",
            400,
            {"delivery": delivery, "reason": reason},
        )


class EdgeAnchorPolicyViolationError(SAGEError):
    """400: edge violates the resolution-policy write-time invariant (CAS-ADR-017).

    The invariant matrix is policy-keyed:
      - none (non-retracts): all anchor fields null, target_id required.
      - retracts: target_id null, retracted_edge_id required, source-side anchor only.
      - transitive_source: source-side anchor required; no target-side anchor.
      - transitive_both: both anchors required.
    """

    def __init__(
        self,
        edge_type: str,
        resolution_policy: str,
        violation: str,
        offending_fields: list[str] | None = None,
    ) -> None:
        detail: dict = {
            "edge_type": edge_type,
            "resolution_policy": resolution_policy,
            "violation": violation,
        }
        if offending_fields is not None:
            detail["offending_fields"] = offending_fields
        super().__init__(
            "edge_anchor_policy_violation",
            f"Edge violates resolution_policy '{resolution_policy}' invariant: {violation}",
            400,
            detail,
        )


class TBDPolicyEdgeError(SAGEError):
    """400: attempted to create an edge whose registry policy is TBD (CAS-ADR-017)."""

    def __init__(self, edge_type: str) -> None:
        super().__init__(
            "tbd_policy_edge",
            (
                f"Cannot create edge of type '{edge_type}': its resolution_policy "
                "is TBD. Freeze the policy in the edge_type_registry before use."
            ),
            400,
            {"edge_type": edge_type},
        )


class RetractTargetNotEdgeError(SAGEError):
    """400: retracts edge references an unknown edge id (CAS-ADR-017, Chunk 5)."""

    def __init__(self, retracted_edge_id: str) -> None:
        super().__init__(
            "retract_target_not_edge",
            (
                f"retracts edge references edge id '{retracted_edge_id}' that "
                "does not exist in the edges table"
            ),
            400,
            {"retracted_edge_id": retracted_edge_id},
        )


class MergedFromValidationError(SAGEError):
    """400: merged_from edge violates chain-position invariants (CAS-ADR-017, Chunk 6).

    The source (successor) must be the first version of its chain (no
    outbound supersedes edges from it) and the target (predecessor) must
    be the chain head (no supersedes edge points at it).
    """

    def __init__(
        self,
        violation: str,
        source_id: str | None = None,
        target_id: str | None = None,
    ) -> None:
        detail: dict = {"violation": violation}
        if source_id is not None:
            detail["source_id"] = source_id
        if target_id is not None:
            detail["target_id"] = target_id
        super().__init__(
            "merged_from_validation",
            f"merged_from edge invalid: {violation}",
            400,
            detail,
        )


class IdenticalContentSupersedeError(SAGEError):
    """409: attempted supersede whose content matches the predecessor (BH-123)."""

    def __init__(self, predecessor_id: str, source_content_hash: str) -> None:
        super().__init__(
            "identical_content_supersede",
            (
                f"Cannot supersede document {predecessor_id}: new content is "
                "identical to the predecessor (no-op edit)"
            ),
            409,
            {
                "predecessor_id": predecessor_id,
                "source_content_hash": source_content_hash,
            },
        )


class SyncedFromInapplicableEdgeType(SAGEError):
    """400: synced_from_* fields set on an edge_type other than sync_target /
    derived_from.

    The `synced_from_version` and `synced_from_content_hash` columns are
    semantically meaningful only on `sync_target` (Tier 1) and
    `derived_from` (Tier 3) edges. Setting them on any other edge type
    creates orphaned provenance the drift detector would never inspect.
    """

    def __init__(self, edge_type: str, fields_set: list[str]) -> None:
        super().__init__(
            "synced_from_inapplicable_edge_type",
            (
                f"synced_from_* fields {sorted(fields_set)!r} are not "
                f"applicable to edge_type '{edge_type}'; only 'sync_target' "
                "and 'derived_from' carry synced-from provenance."
            ),
            400,
            {"edge_type": edge_type, "fields_set": sorted(fields_set)},
        )


class SyncedFromVersionNotInSourceChain(SAGEError):
    """400: synced_from_version doc id is not a member of the target's
    supersedes chain.

    Raised when `create_edge` is called with a `synced_from_version` that
    either references a document outside the chain rooted at `target_id`
    or references a document id that does not resolve at all. Surfaced
    as this dedicated code (not `document_not_found`) so operators can
    distinguish "wrong document" from "missing document" — the
    remediation differs.
    """

    def __init__(self, target_id: str, synced_from_version: str) -> None:
        super().__init__(
            "synced_from_version_not_in_source_chain",
            (
                f"synced_from_version {synced_from_version!r} is not a member "
                f"of the supersedes chain rooted at target_id {target_id!r}."
            ),
            400,
            {
                "target_id": target_id,
                "synced_from_version": synced_from_version,
            },
        )


class AmbiguousDocumentIdentifierError(SAGEError):
    """400: caller supplied both the canonical parameter and an alias
    for the same logical document identifier.

    Some MCP tools accept the canonical name ``document_id`` as an alias
    for a tool-specific name (e.g., ``traverse`` accepts both
    ``start_id`` and ``document_id``). Supplying both is treated as
    ambiguous — even when the values are equal — to keep the call-shape
    contract simple: exactly one must be supplied.
    """

    def __init__(self, tool: str, canonical: str, alias: str) -> None:
        super().__init__(
            "ambiguous_document_identifier",
            (f"{tool}: supply exactly one of {canonical!r} or {alias!r}; both were provided."),
            400,
            {
                "tool": tool,
                "supplied": [canonical, alias],
            },
        )


class MissingDocumentIdentifierError(SAGEError):
    """400: caller supplied neither the canonical parameter nor any
    accepted alias for the document identifier.

    Companion to :class:`AmbiguousDocumentIdentifierError`. Distinct
    from ``document_not_found`` (404) and from a downstream Pydantic
    ``ValidationError``: this code fires before the service layer is
    invoked, when no document identifier was supplied at all. The
    ``accepted`` detail enumerates every parameter name the tool will
    take so the caller learns the alias without trial-and-error.
    """

    def __init__(self, tool: str, accepted: list[str]) -> None:
        super().__init__(
            "missing_document_identifier",
            (f"{tool}: a document identifier is required. Supply exactly one of: {accepted!r}."),
            400,
            {
                "tool": tool,
                "accepted": list(accepted),
            },
        )


# Filter keys whose accepted values are a closed Python enum. Drives the
# ``invalid_filter_value`` envelope, which reports the enum's members back
# to the caller. Vault-configured vocabularies are deliberately absent --
# their accepted set is not knowable at validation time.
_ENUM_TYPED_FILTER_FIELDS: dict[str, type[StrEnum]] = {
    "source_type": SourceType,
    "edge_type": EdgeType,
}

# Field-annotation strings used in InvalidFilterShapeError detail.
# Kept as a small lookup rather than introspected from RetrievalFilters because
# Pydantic v2's stringified annotations for ``str | None`` shapes are noisy
# (``typing.Optional[str]`` or ``Union[str, None]`` depending on Python form).
# Enum-typed keys are absent: a bad value on those raises ``enum`` rather
# than a ``*_type`` shape error, so it never reaches this table.
#
# A key whose field carries a typed alias names the alias, not the bare type
# it refines. Naming the bare type would make the envelope's own remedy
# unreachable: a caller told to supply ``list[str]`` who then supplies one
# with an off-shape entry gets a second, different 400 from the alias.
_FILTER_FIELD_TYPE_NAMES: dict[str, str] = {
    "doc_type": "str",
    "project": "str",
    "lifecycle_status": "str",
    "exclude_terminal_lifecycle": "bool",
    "tags": "list[str]",
    "document_ids": "list[DocumentIdStr]",
    "pipeline_status": "str",
    "source_id": "DocumentIdStr",
    "target_id": "DocumentIdStr",
    "tier3_metadata": "dict",
}

# Remedies attached to ``invalid_parameter`` envelopes, keyed by the final
# segment of the failing location and the Pydantic error type. Deliberately
# sparse: a hint earns its place by naming a way forward the constraint alone
# does not imply, and an absent entry yields an envelope with no ``hint`` key
# rather than filler. Keying on the error type keeps a remedy to the violation
# it answers -- paging past a cap says nothing to a value below the floor.
# The bound itself is never restated here -- it comes from the validator's
# own message, so a changed cap cannot leave a stale number behind.
_PARAMETER_HINTS: dict[tuple[str, str], str] = {
    ("limit", "less_than_equal"): "Page through larger result sets with `offset`.",
}

# Request components FastAPI prepends to a validation error's location to
# name where the value came from. They are part of the framing, not part of
# the parameter path, so they are stripped before any location is matched
# against a rule or reported back to a caller. Only a RequestValidationError
# carries one: a model's own ValidationError starts at the parameter, and a
# parameter may be named ``query`` or ``path``.
_TRANSPORT_LOC_SEGMENTS = ("body", "query", "path", "header", "cookie")

# Types whose ``input`` a caller can be shown verbatim. Anything else is
# rendered with ``str`` so the envelope stays JSON-serializable on both
# transports regardless of what the caller supplied.
_JSON_NATIVE_TYPES = (str, int, float, bool, type(None))


def _strip_transport_segment(loc: tuple, exc: ValidationError | RequestValidationError) -> tuple:
    """Drop the request-component segment FastAPI prepends to a location."""
    if isinstance(exc, RequestValidationError) and loc and loc[0] in _TRANSPORT_LOC_SEGMENTS:
        return loc[1:]
    return loc


def _model_for_loc(root_model: type[BaseModel], loc: tuple) -> type[BaseModel] | None:
    """Return the model that refused a key at ``loc``, or ``None``.

    ``loc`` is a validation-error location whose last segment is the key that
    was refused; the segments before it name the path from ``root_model`` down
    to the model carrying it. A location one segment long names a key on the
    root itself, which is how a surface that validates each item of a batch
    against the item model reports one.

    Three shapes are followed, and they are the ones the request models have:
    a model behind an ``Annotated`` wrapper, a model optional against
    ``None``, and a model as the element of a sequence, whose position segment
    is an integer and names no field. A union carrying more than one model is
    not followed: the validator tried every arm, so the location alone does
    not say which one refused, and naming a field set the caller was not
    refused against is worse than naming none. ``None`` returns the caller to
    the refusal it would have given anyway, so an unfollowable shape costs
    nothing that is not already lost -- which is the whole reason this stays
    narrow. What keeps it honest is the conformance walk over every nested
    request model, which fails on a shape this cannot follow.
    """

    def unwrap(annotation: object) -> type[BaseModel] | None:
        """The single model an annotation carries, or ``None`` if not exactly one."""
        models: list[type[BaseModel]] = []
        pending = [annotation]
        while pending:
            current = pending.pop()
            if isinstance(current, type) and issubclass(current, BaseModel):
                if current not in models:
                    models.append(current)
                continue
            if isinstance(current, types.UnionType) or typing.get_origin(current) is not None:
                pending.extend(arg for arg in typing.get_args(current) if arg is not type(None))
        return models[0] if len(models) == 1 else None

    model: type[BaseModel] | None = root_model
    for segment in loc[:-1]:
        if isinstance(segment, int):
            # A position in a sequence, not a field name.
            continue
        if model is None:
            return None
        field = model.model_fields.get(str(segment))
        model = None if field is None else unwrap(field.annotation)
    return model


def unknown_parameter_names(exc: ValidationError | RequestValidationError) -> list[str]:
    """Return the undeclared top-level names a validation error rejected.

    Only a name at the top of the call counts: a parameter the operation does
    not declare. An undeclared key nested inside a declared parameter -- a
    batch item's field, a filter key -- is a malformed value of that
    parameter, and keeps the code its own rule selects. The names come back
    sorted and once each, so the refusal does not depend on the order the
    validator reported them in.
    """
    names = {
        str(loc[0])
        for err in exc.errors()
        if err.get("type") == "extra_forbidden"
        and len(loc := _strip_transport_segment(tuple(err.get("loc") or ()), exc)) == 1
    }
    return sorted(names)


def undeclared_entry_keys(
    files: Iterable[object], declared_by_depth: dict[int, Collection[str]]
) -> list[tuple[int, int, str, object]]:
    """Walk batch file entries for names their declared sets do not include.

    Returns ``(file index, depth, key, value)`` candidates for
    ``undeclared_entry_key_error``: depth 0 is a key on the entry itself and
    depth 1 a key in the ``parsed_metadata`` mapping it carries. An entry or a
    ``parsed_metadata`` that is not a mapping is passed over, because it
    declares no names to check -- its shape is the model's to refuse.

    Surfaces whose entries arrive as plain mappings walk them here, before any
    value is checked, so an undeclared name is reported ahead of a malformed
    value on every batch surface alike.
    """
    candidates: list[tuple[int, int, str, object]] = []
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            continue
        locations = [(0, entry)]
        parsed = entry.get("parsed_metadata")
        if isinstance(parsed, dict):
            locations.append((1, parsed))
        for depth, mapping in locations:
            declared = declared_by_depth[depth]
            candidates.extend(
                (index, depth, name, mapping[name]) for name in mapping if name not in declared
            )
    return candidates


def undeclared_entry_key_error(
    candidates: Iterable[tuple[int, int, str, object]],
    *,
    recognized_by_depth: dict[int, Iterable[str]],
) -> UndeclaredKeyError | None:
    """Return the refusal for a batch's undeclared file-entry keys.

    Each candidate is ``(file index, depth, key, value)``, where depth 0 is a
    key on the file entry itself and depth 1 a key in the ``parsed_metadata`` it
    carries. The object reported is the one at the lowest file index, then the
    lowest depth, located as ``files.<n>`` or ``files.<n>.parsed_metadata``,
    and every undeclared key in it is named. Keys in other objects are left to
    a later refusal, and the message says they exist. ``None`` when there is no
    candidate.

    Every batch ingest surface -- the Core API upload, the MCP bulk ingest tool
    and the application's ingest route -- reports through this rule, so the
    same entries are refused at the same location, under the same code, naming
    the same keys. What they do *not* share is the accepted set at depth 0: an
    upload's bytes arrive as file parts, so its entries declare no name for
    them, while the other surfaces' entries name a path or a transfer token.
    ``recognized_by_depth`` is therefore the caller's to supply rather than
    derived here -- a shared answer would be wrong for one of the surfaces.
    """
    by_location: dict[tuple[int, int], list[str]] = {}
    for index, depth, key, _value in candidates:
        by_location.setdefault((index, depth), []).append(key)
    if not by_location:
        return None
    index, depth = min(by_location)
    prefix = f"files.{index}" if depth == 0 else f"files.{index}.parsed_metadata"
    return UndeclaredKeyError(
        parameter=prefix,
        keys=by_location[(index, depth)],
        recognized=list(recognized_by_depth[depth]),
        elsewhere=len(by_location) > 1,
    )


#: Why an entry may not supply both. One sentence, shared by every surface that
#: refuses the pair, so a caller meets the same explanation on each: the batch
#: boundaries locate it at ``files.<n>.parsed_metadata.tags``, the
#: single-document boundary at ``metadata.tags``.
CODES_AND_TAGS_CONSTRAINT: Final[str] = (
    "codes and tags both set the document's tags; supply one, not both"
)


def codes_and_tags_conflict_error(
    candidates: Iterable[tuple[int, object]],
) -> InvalidParameterError | None:
    """Return the refusal for a batch entry carrying both ``codes`` and ``tags``.

    Each candidate is ``(file index, tags value)`` for one entry whose parsed
    metadata supplies both. The two write the same field -- a document's tags --
    so an entry supplying both leaves which one stands to the order a mapping
    happens to be walked in. That is a defect in the request's shape rather than
    in one file's content, so it refuses the whole call before anything is
    delivered, the way an undeclared key does, instead of becoming that file's
    own error.

    The entry at the lowest file index is reported, located at
    ``files.<n>.parsed_metadata.tags``: the refusal names the key a caller adds
    to an entry that already parses, not the one that was there first. The Core
    API batch upload and the MCP bulk ingest tool both report through this rule.

    They agree on the location for a batch whose *only* boundary defect is the
    conflict. Where another entry also carries a malformed value, the two
    surfaces differ on which defect is reported first: the Core API validates
    the whole envelope before this is asked, so a sibling's wrong-typed value
    refuses the call as ``invalid_batch_metadata`` with no location, while the
    tool checks entry types after this and reports the conflict. That
    precedence difference is the Core API's pre-existing envelope-first
    ordering rather than anything this rule decides. ``None`` when there is no
    candidate.
    """
    chosen = min(candidates, key=lambda candidate: candidate[0], default=None)
    if chosen is None:
        return None
    index, value = chosen
    return InvalidParameterError(
        parameter=f"files.{index}.parsed_metadata.tags",
        value=value,
        constraint=CODES_AND_TAGS_CONSTRAINT,
    )


def translate_validation_error(
    exc: ValidationError | RequestValidationError,
    *,
    root_model: type[BaseModel] | None = None,
) -> SAGEError | None:
    """Map a Pydantic validation failure to a typed ADR-028 SAGEError.

    Walks ``exc.errors()`` and returns the first matching SAGEError. Returns
    ``None`` when no rule matches, signaling the caller to fall back to the
    default validation-error path (FastAPI's 422 on HTTP, ``internal_error``
    on MCP). Two kinds of rule apply. Custom error types a leaf validator
    raises with a structured ``ctx`` -- ``mode_parameter_mismatch``,
    ``legacy_form``, and the typed-alias family (``invalid_document_id``,
    ``invalid_vault_id``, ``invalid_document_date`` and the rest) -- are
    rebuilt wherever they occur, on any request model. The remaining rules are
    scoped to the discover request: they fire only on ``mode``- or
    ``filters``-rooted errors, so other models' built-in validation failures
    are left to the fallback.

    ``root_model`` is the model the request was validated against, supplied by
    a surface that knows it. It drives one rule: an undeclared key nested
    inside a parameter is answered with the field set of the model that
    refused it, which is knowable only by walking the error's location back
    through that model. Omitted, the rule does not fire and the failure keeps
    the envelope it had, so a surface that cannot name its root model loses
    nothing it already has.

    Both ``pydantic.ValidationError`` and ``fastapi.exceptions.RequestValidationError``
    expose ``.errors()`` with the same dict shape, so one function serves
    both transports.
    """
    # Local import sidesteps any future circular-import risk: errors.py is
    # imported by routers/services that themselves import models/schemas.
    from sage.models.enums import RetrievalMode
    from sage.models.schemas import RetrievalFilters

    for err in exc.errors():
        loc = tuple(err.get("loc") or ())
        # FastAPI's RequestValidationError prepends a "body" / "query" /
        # "path" location segment naming the request component the value
        # came from. Pydantic's ValidationError does not. Strip that segment
        # so one set of loc rules works for both call sites.
        loc = _strip_transport_segment(loc, exc)
        err_type = err.get("type", "")
        input_value = err.get("input")
        ctx = err.get("ctx") or {}

        # 0) Custom ``mode_parameter_mismatch`` raised from the
        # DiscoverRequest model_validator via PydanticCustomError. The
        # validator lives in sage.models.schemas which cannot import
        # sage.api.errors (import-linter "Models are a leaf layer"
        # contract), so the public-facing SAGEError is built here.
        #
        # The message is carried across rather than composed. Pydantic has
        # already rendered the validator's template into ``msg`` with the
        # ctx placeholders substituted, and that sentence names the axis
        # the branch constrains -- a target, a filter-key set -- which the
        # ctx fields on their own cannot be composed back into.
        if err_type == "mode_parameter_mismatch":
            return ModeParameterMismatchError(
                message=str(err.get("msg", "")),
                detail={
                    key: value for key, value in ctx.items() if key in _MODE_MISMATCH_DETAIL_KEYS
                },
            )

        # 0a) Custom ``legacy_form`` raised from the UpdateMetadataRequest
        # and BulkMetadataItem model_validators via PydanticCustomError.
        # Same leaf-layer-contract reasoning as ``mode_parameter_mismatch``:
        # the validator can't construct ``LegacyFormError`` itself, so it
        # embeds the envelope fields in ``ctx`` and we rebuild here. Drives
        # the structured ``legacy_form`` 400 envelope on the FastAPI surface
        # (CAS-ADR-028 ops-object patch grammar).
        # 0b) Custom ``codes_and_tags_conflict`` raised from the IngestRequest
        # model_validator via PydanticCustomError. Same leaf-layer-contract
        # reasoning as the two rules above: the models layer cannot import
        # this module, so the validator embeds the location in ``ctx`` and the
        # envelope is rebuilt here. The batch boundaries refuse the same pair
        # without reaching a request model, so they call
        # ``codes_and_tags_conflict_error`` directly; both report the same
        # code and the same constraint, located at their own spelling.
        if err_type == "codes_and_tags_conflict":
            return InvalidParameterError(
                parameter=str(ctx.get("parameter", "metadata.tags")),
                value=ctx.get("value"),
                constraint=CODES_AND_TAGS_CONSTRAINT,
            )

        if err_type == "legacy_form":
            return LegacyFormError(
                field=str(ctx.get("field", "")),
                received_type=str(ctx.get("received_type", "")),
                example=str(ctx.get("example", "")),
            )

        # 0a-bis) Custom ``misplaced_top_level_field`` raised from the
        # IngestRequest model_validator. Same leaf-layer-contract reasoning as
        # ``legacy_form`` above. On the request model rather than at either
        # tool body, because both surfaces bind the same model and the hole is
        # the same on each.
        if err_type == "misplaced_top_level_field":
            return MisplacedTopLevelFieldError(
                fields=list(ctx.get("fields", ())),
                recognized=list(ctx.get("recognized", ())),
                example=str(ctx.get("example", "")),
            )

        # 0b) Custom ``invalid_document_id`` raised from the DocumentIdStr
        # AfterValidator via PydanticCustomError. Same leaf-layer-contract
        # reasoning as above: the validator embeds the offending value in
        # ``ctx`` and we rebuild the public 400 envelope here. Reconciles the
        # reject-at-boundary rule (a malformed id is a client error caught
        # before lookup) with the self-describing not-found discriminator (a
        # well-formed-but-absent id is a 404): malformed syntax surfaces as
        # ``invalid_document_id`` (400), never as the generic internal_error.
        if err_type == "invalid_document_id":
            return InvalidDocumentIdError(str(ctx.get("document_id", input_value)))

        # 0c) The rest of the typed-alias family: vault_id, edge_id, sha256,
        # document_date, user_id. Same leaf-layer-contract
        # reasoning as ``invalid_document_id`` above -- the validator embeds a
        # uniform ``{argument, value, expected}`` ctx and we rebuild the public
        # 400 here via the single parameterized ``InvalidTypedAliasError``.
        # Placed AFTER the ``invalid_document_id`` branch, which keeps its own
        # distinct error and three-key ctx; the ordering matters because
        # ``invalid_document_id`` is itself a member of the family set.
        if err_type in _TYPED_ALIAS_CODES:
            return InvalidTypedAliasError(
                code=err_type,
                argument=(
                    ".".join(str(part) for part in loc)
                    if err_type == "invalid_sha256"
                    and len(loc) == 3
                    and loc[0] == "files"
                    and isinstance(loc[1], int)
                    and loc[2] == "sha256"
                    else str(ctx.get("argument", ""))
                ),
                value=ctx.get("value", input_value),
                expected=str(ctx.get("expected", "")),
            )

        # 1) Invalid `mode` enum value: caller passed a string not in RetrievalMode.
        if loc and loc[0] == "mode" and err_type in ("enum", "literal_error"):
            valid_modes = [m.value for m in RetrievalMode]
            return InvalidModeError(mode=str(input_value), valid_modes=valid_modes)

        # 2) Unknown filter key: extra_forbidden under `filters`.
        if len(loc) >= 2 and loc[0] == "filters" and err_type == "extra_forbidden":
            key = str(loc[1])
            valid_keys = list(RetrievalFilters.model_fields.keys())
            example = (
                '{"tier3_metadata": {"ticket_id": "<id>"}} for typed '
                'metadata, or {"doc_type": "ticket"} for built-in fields'
            )
            return UnknownFilterKeyError(key=key, valid_keys=valid_keys, example=example)

        # 2a) Out-of-vocabulary value for an enum-typed filter key.
        # Pydantic reports these as `enum` (StrEnum members) or
        # `literal_error`, neither of which the shape branch below
        # catches -- it keys on the `*_type` suffix. Without this branch
        # such a value falls through untranslated to the generic 422/
        # internal_error path, losing the valid set the caller needs.
        # Scoped to the field->enum map rather than to any one field, so
        # every enum-typed filter key gets the same envelope.
        if len(loc) >= 2 and loc[0] == "filters" and err_type in ("enum", "literal_error"):
            field = str(loc[1])
            enum_cls = _ENUM_TYPED_FILTER_FIELDS.get(field)
            if enum_cls is not None:
                return InvalidFilterValueError(
                    field=field,
                    value=input_value,
                    valid_values=[member.value for member in enum_cls],
                )

        # 3) Wrong value type for a known filter key.
        # Pydantic emits types like `list_type`, `int_type`, `string_type`,
        # `dict_type` for primitive-shape failures.
        if len(loc) >= 2 and loc[0] == "filters" and err_type.endswith("_type"):
            field = str(loc[1])
            expected_type = _FILTER_FIELD_TYPE_NAMES.get(field, "unknown")
            received_type = type(input_value).__name__
            return InvalidFilterShapeError(
                field=field,
                expected_type=expected_type,
                received_type=received_type,
            )

        # 4) A key a nested model does not declare. Last of the branches, and
        # deliberately so: ``filters`` resolves through the walk like any
        # other nested model, so this rule would answer for it too and retire
        # ``unknown_filter_key`` without a word. Placement is the only thing
        # that prevents it, which is why a test pins the order rather than
        # trusting this comment.
        #
        # Keyed on the location resolving to a model, not on how deep it is: a
        # surface that validates each item of a batch against the item model
        # reports the key one segment deep, and the same rule has to answer
        # there so the two surfaces do not diverge on the same mistake.
        #
        # Every undeclared key the validator reported in the same object is
        # named in the one refusal; keys in other objects are left to a later
        # one, and the refusal says they exist.
        if err_type == "extra_forbidden" and root_model is not None:
            refusing = _model_for_loc(root_model, loc)
            if refusing is not None and loc:
                by_object = _undeclared_keys_by_object(exc, root_model)
                return UndeclaredKeyError(
                    parameter=".".join(str(segment) for segment in loc[:-1]),
                    keys=by_object[loc[:-1]],
                    recognized=list(refusing.model_fields),
                    elsewhere=len(by_object) > 1,
                )

    return None


def _undeclared_keys_by_object(
    exc: ValidationError | RequestValidationError, root_model: type[BaseModel]
) -> dict[tuple, list[str]]:
    """Group the undeclared nested keys a validation error reports by the object holding them.

    Only keys whose location resolves to a model count, which is the same
    condition the nested rule fires on, so every group is one a refusal could
    name. Keyed by the object's location, in the order the validator reported.
    """
    grouped: dict[tuple, list[str]] = {}
    for err in exc.errors():
        if err.get("type") != "extra_forbidden":
            continue
        loc = _strip_transport_segment(tuple(err.get("loc") or ()), exc)
        if loc and _model_for_loc(root_model, loc) is not None:
            grouped.setdefault(loc[:-1], []).append(str(loc[-1]))
    return grouped


def _generic_parameter_error(
    exc: ValidationError | RequestValidationError,
) -> InvalidParameterError:
    """Build an `invalid_parameter` envelope from a validation error.

    Reads only the structured fields Pydantic exposes per error --
    ``loc``, ``input`` and ``msg``. The rendered form of a validation
    error additionally carries the model class name and a link to the
    validator's documentation site; neither is meaningful to a caller of
    this API, so neither is read here. That is a property of the
    construction, not of any filtering applied afterwards.

    The first error is reported. Callers who need a specific one of
    several failures to win -- as the argument-model boundary does for
    unknown parameters -- select it before reaching this function.
    """
    errors = exc.errors()
    if not errors:  # pragma: no cover -- pydantic always reports at least one
        return InvalidParameterError(
            parameter="request",
            value=None,
            constraint="Request failed validation",
        )

    err = errors[0]
    loc = _strip_transport_segment(tuple(err.get("loc") or ()), exc)
    parameter = ".".join(str(segment) for segment in loc) or "request"
    return InvalidParameterError(
        parameter=parameter,
        value=err.get("input"),
        constraint=str(err.get("msg", "Invalid value")),
        hint=_PARAMETER_HINTS.get((str(loc[-1]) if loc else "", str(err.get("type", "")))),
    )


def validation_error_envelope(
    exc: ValidationError | RequestValidationError,
    *,
    root_model: type[BaseModel] | None = None,
) -> SAGEError:
    """Map any validation error to a structured envelope (CAS-ADR-028).

    `translate_validation_error` handles the cases with a more specific
    code and returns ``None`` for the rest; this wrapper supplies the
    general `invalid_parameter` envelope for that remainder, so no
    validation failure reaches a caller as a raw Pydantic rendering.

    The division of labour is deliberate. The translator stays scoped to
    the rules it can state precisely, and remains usable by callers that
    need to know whether a specific rule matched. Uniformity is a property
    of this wrapper: every failure reaches *an* envelope, not the same one.
    """
    return translate_validation_error(exc, root_model=root_model) or _generic_parameter_error(exc)


def request_operation_name(request: Request) -> str:
    """Return the published operation id of the operation a request reached.

    The id is read from the document the app serves, which carries the
    authored operation ids rather than the handler names the framework would
    generate -- the names the MCP surface calls its tools. A request that
    reached no documented operation is named by its path.
    """
    route = request.scope.get("route")
    if isinstance(route, APIRoute):
        operation = (
            request.app.openapi()
            .get("paths", {})
            .get(route.path_format, {})
            .get(request.method.lower(), {})
        )
        return str(operation.get("operationId") or route.name)
    return request.url.path


def _body_model(request: Request) -> type[BaseModel] | None:
    """Return the model an operation validated a request's whole body against.

    ``None`` unless the operation binds exactly one unembedded body parameter
    whose annotation is a model -- the shape that makes the model the root of
    every location the validator reports. A dependency that binds the same
    body the handler does contributes it a second time under the same name,
    and the framework treats the repeats as one body, so the decision is made
    on the distinct names.
    """
    route = request.scope.get("route")
    if not isinstance(route, APIRoute):
        return None
    body_params = get_flat_dependant(route.dependant).body_params
    if len({param.alias for param in body_params}) != 1:
        return None
    if getattr(body_params[0].field_info, "embed", False):
        return None
    annotation = body_params[0].field_info.annotation
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def _declared_body_names(request: Request) -> list[str]:
    """Return the top-level body names the operation a request reached declares.

    The fields of the body model where there is one, and the bound parameter
    names otherwise.
    """
    route = request.scope.get("route")
    if not isinstance(route, APIRoute):
        return []
    model = _body_model(request)
    if model is not None:
        return sorted(model.model_fields)
    return sorted({param.alias for param in get_flat_dependant(route.dependant).body_params})


_logger = logging.getLogger(__name__)

# Codes for the errors the framework raises before any operation runs. Every
# other status reaching the handler keeps the generic code.
_FRAMEWORK_ERROR_CODES: dict[int, str] = {
    404: "route_not_found",
    405: "method_not_allowed",
}


def register_exception_handlers(app: FastAPI) -> None:
    """Register SAGE exception handlers on the FastAPI app.

    Every error a caller receives from the application carries the
    ``ErrorResponse`` envelope, including the few the framework raises itself:
    a path no operation serves, a method a path does not accept, and a response
    the server built that its declared model refuses.
    """

    @app.exception_handler(StarletteHTTPException)
    async def framework_http_error_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """Render a routing failure in the envelope, keeping its headers.

        A 405 carries ``Allow``, naming the methods the path does accept.
        """
        return JSONResponse(
            status_code=exc.status_code,
            content=to_wire(
                ErrorResponse(
                    code=_FRAMEWORK_ERROR_CODES.get(exc.status_code, "http_error"),
                    message=str(exc.detail),
                )
            ),
            headers=exc.headers,
        )

    @app.exception_handler(ResponseValidationError)
    async def response_validation_handler(
        request: Request, exc: ResponseValidationError
    ) -> JSONResponse:
        """Report a response its declared model refuses as a server error.

        The refused value is the server's own data, so it is logged and never
        sent: the caller learns only that the server failed.
        """
        _logger.error(
            "response for %s %s failed its declared model: %s",
            request.method,
            request.url.path,
            exc.errors(),
        )
        return JSONResponse(
            status_code=500,
            content=to_wire(
                ErrorResponse(
                    code="internal_error",
                    message="The server built a response its published contract does not admit.",
                )
            ),
        )

    @app.exception_handler(SAGEError)
    async def sage_error_handler(request: Request, exc: SAGEError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=to_wire(
                ErrorResponse(
                    code=exc.code,
                    message=exc.message,
                    detail=exc.detail,
                )
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Translate request-validation failures into the SAGE envelope.

        Every failure reaches a structured envelope: the most specific
        code that applies, or the general `invalid_parameter` for the
        remainder. The generic envelope carries 422 -- the status request
        validation already returned -- so what changes on this path is the
        response body, not any endpoint's status code. Endpoints whose
        failures translate to a more specific code keep the 400 that code
        declares.

        An undeclared top-level body field wins outright, as an unknown
        argument does on the MCP surface: naming the fields the operation
        accepts is the more useful answer, whatever else failed alongside it.
        """
        rejected = unknown_parameter_names(exc)
        if rejected:
            sage_err: SAGEError = UnknownParameterError(
                request_operation_name(request), rejected, _declared_body_names(request)
            )
        else:
            sage_err = validation_error_envelope(exc, root_model=_body_model(request))
        return JSONResponse(
            status_code=sage_err.status_code,
            content=to_wire(
                ErrorResponse(
                    code=sage_err.code,
                    message=sage_err.message,
                    detail=sage_err.detail,
                )
            ),
        )
