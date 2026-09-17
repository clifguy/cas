"""The frontend API interfaces declare the published component schemas.

``app/src/api/types.ts`` hand-maintains TypeScript mirrors of the shapes two
specifications publish: the SAGE Core API (``sage_core_api.openapi.yaml``), whose
document responses the frontend reads and whose request bodies it sends, and the
CAS App API (``cas_app_api.openapi.yaml``), the backend-for-frontend's scan,
ingest and sign-in surface. The spec-versus-code gates elsewhere in this suite
hold the Pydantic models to those specifications and stop at the Python boundary;
nothing else reads the TypeScript file, so a property added to a schema reaches
the frontend only if whoever added it remembered to.

This module reads the component schemas, not the Pydantic models, so it sits on
the same authority those gates do and needs no opinion about which Python class
backs which interface.

Authority
---------

The formal substrate is authoritative (CAS-ADR-008), in the same direction for
responses and requests.

A property the schema declares and the interface omits is drift, and the fix is
to declare it. On a response it is a field the wire supplies that the frontend
cannot see. On a request it is a field the server accepts that the frontend
cannot send without first widening its own type; declaring an optional member
obliges no caller to send it, so omission is never the way to say "not sent".
The settled omission is a property the frontend must not send, such as a
back-compatible alias a request component accepts in place of another property:
declaring both would type-permit a body the server refuses. Such a property needs
an allowlist entry naming why it is withheld.

A property the interface declares and the schema omits is a frontend-only field:
nothing on the wire supplies it, or the server refuses it, so it needs an
allowlist entry naming why it exists, or it should not exist.

Optionality
-----------

A request component's ``required`` list is part of the contract the frontend
sends against, so for request interfaces one direction is compared: a property
the component requires is non-optional on the interface. The other direction is
permitted, since an interface requiring what the schema leaves optional only
means the frontend always sends it. F6 has no allowlist. A required property the
interface marks optional type-permits a body the server refuses, which is never a
settled divergence.

A REST response renders through the same wire rule as a stream (``docs/fs/CLAUDE.md``,
"Null fields on the wire"): a key is omitted when its property is optional and its
value is null. So a response property the component leaves optional and declares
nullable can be absent, and its member is optional on the interface (F11); typed
``T | null`` without ``?``, it tells the frontend it will find a null that is not
there, and a strict ``=== null`` test reads the absent key as a value. The other
optional properties are never null and so always arrive, and a response
interface may declare them required. F11 has no allowlist, for the reason F6 has
none: a member the wire can omit, typed as always present, is never a settled
divergence. Whether a property is otherwise correctly typed on the TypeScript
side is not asserted for any kind.

Stream events
-------------

A shape delivered over a server-sent event stream is held to a stricter
reading than a REST response. The stream renders each event through the wire
rule in ``docs/fs/CLAUDE.md`` ("Null fields on the wire"): a property the
component requires carries its key on every event, and an optional one is
omitted when it is null. On a stream the ``required`` list therefore decides which keys arrive,
and a member typed ``T | null`` where the key can be absent tells the frontend
it will find a null that is not there. Stream interfaces are held to the list in
both directions: a member is optional exactly when its component leaves it
optional (F7).

Both specifications publish an event stream, and both render through that rule:
the App API's ingest stream is produced by the same generator as the Core API's
upload stream, and that generator serializes every event through the wire model
(``sage.models.wire``) rather than dumping it. The classification is a fact about
the emitter, so a stream that stopped rendering through the wire model would
take a rule of its own rather than inherit this one.

Which shapes those are is derived, not chosen. Each ``text/event-stream``
response in a specification names its event components, and every component
reachable from them by reference renders through the same rule, since a model
has one wire shape wherever it is nested. F4 holds each specification's stream
enrollment to exactly the enrolled components its own event streams reach. A
shape that also arrives over REST takes the stream rule, which REST satisfies
too: an optional member reads a present key as readily as an absent one.

Specifications
--------------

Each specification is its own namespace of component names, and the two overlap:
both publish a ``ProgressEvent`` and a ``SummaryEvent``, for instance. Enrollment
is therefore keyed by specification as well as by interface, and every check
that relates components to one another -- one interface per component (F4), the
component-to-interface map F8 reads, the reference closure (F9), the event-stream
derivation (F4) -- is evaluated within one specification. An interface may mirror
one component in each, and is then compared against both; the allowlist and the
reference pins name the specification their entry applies to.

References between components
-----------------------------

Properties are compared one level deep. A member whose type is a nested type
literal is compared by its name only, and a literal that mirrors a published
component is promoted to a named interface and enrolled rather than compared in
place. Depth comes from enrollment instead: a property whose schema references
another component is gated through that component's own interface, which F8
requires the member to name and F9 requires to be enrolled. A referenced
component with properties of its own that has no enrolled mirror is pinned in
``UNGATED_REFERENCED_COMPONENTS`` with the reason, so the gate's reach stops
where it says it stops. F8 checks that the interface name appears in the
member's type, not that the type is otherwise what the schema declares.

Reach
-----

The gate reads the exported interfaces of ``types.ts`` and nothing else, so the
mirrors of either specification are declared there rather than beside the client
functions that use them. Every exported interface is either enrolled under some
specification or named in ``UNENROLLED_INTERFACES`` with the reason it mirrors no
published component (F10), so an interface left out of the enrollment is a
recorded decision rather than an omission the file cannot tell apart from one.
Interfaces declared in other frontend modules, and route bodies typed inline
rather than through an exported interface, are outside the scan.

Invariants
----------

F1  Every property of an enrolled component schema is declared on its interface.
F2  An enrolled interface declares no property its component schema omits.
F3  Every allowlist entry names an interface enrolled under its specification and
    a property that still diverges on the side the entry names, and every pinned
    reference is still an unenrolled component referenced by the property the pin
    names.
F4  Every enrolled interface and component resolves; within each specification
    the response, request and stream enrollments are disjoint and map one
    interface to each component, and the stream enrollment is what that
    specification's event-stream contract reaches; each per-interface gate
    iterates the whole of its enrollment across both specifications; and enough
    is compared in each to mean something (vacuity floors, per specification).
F5  The comparison fires on a removed declaration, including one inherited
    through a heritage clause, one on an interface other interfaces reference, and
    one on an interface enrolled under both specifications;
    the optionality checks fire on a relaxed required member and, for streams, on
    either direction; the reference check fires on a member that stops naming
    its component; the export check fires on an interface neither enrolled nor
    excluded; and the staleness checks fire on a stale entry.
F6  Every property a request component requires is non-optional on its interface.
F7  A stream interface marks a member optional exactly when its component does.
F8  A member whose schema references an enrolled component names the interface
    enrolled for that component in the same specification.
F9  Every component with properties that an enrolled component references is
    enrolled under the same specification, or pinned with a reason.
F10 Every exported interface in the source is enrolled or excluded with a reason,
    never both, and every exclusion names an interface the source still exports.
F11 A response interface marks optional every member whose component leaves it
    optional and declares it nullable.

The reader
----------

The interfaces are read by a token-level declaration reader, not by matching
lines: comments, string literals and nested type literals are lexed as what they
are, so reformatting a declaration or annotating a member does not change what
is collected. The reader models plain property members, and heritage clauses
whose bases are bare names of interfaces declared in the same source; inherited
members are collected with the interface's own. For each member it also collects
the identifier tokens of its type, which F8 reads, so a string literal or a
longer name containing an interface's name does not count as naming it. It
**fails closed** on anything else it meets in an enrolled interface -- an index
signature, a method signature, a member it cannot delimit, a generic or
qualified base, a base it cannot find as an interface, a cyclic clause, a member
declared twice along the heritage chain -- because collecting a subset of such a
declaration would let F1 pass over members it never saw. The P tests pin its
behaviour on synthetic sources.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from pathlib import Path
from typing import Final, Literal

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
TYPES_TS_PATH = _REPO_ROOT / "app" / "src" / "api" / "types.ts"
SAGE_CORE_SPEC_PATH = _REPO_ROOT / "docs" / "fs" / "sage" / "sage_core_api.openapi.yaml"
CAS_APP_SPEC_PATH = _REPO_ROOT / "docs" / "fs" / "cas_app_api.openapi.yaml"

# The published specifications, each a separate namespace of component names.
Spec = Literal["core", "app"]
SPEC_PATHS: Final[dict[Spec, Path]] = {"core": SAGE_CORE_SPEC_PATH, "app": CAS_APP_SPEC_PATH}

# An enrollment: specification -> {TypeScript interface name -> component schema name}.
Enrollment = dict[Spec, dict[str, str]]

# Response interfaces.
ENROLLED: Final[Enrollment] = {
    "core": {
        "AdapterInfo": "VaultAdapterInfo",
        "BulkLifecycleItemResult": "BulkLifecycleItemResult",
        "BulkLifecycleResponse": "BulkLifecycleResponse",
        "BulkLinkItemResult": "BulkLinkItemResult",
        "BulkLinkResponse": "BulkLinkResponse",
        "BulkMetadataItemResult": "BulkMetadataItemResult",
        "BulkMetadataResponse": "BulkMetadataResponse",
        "ChainEntry": "ChainEntry",
        "ChainResponse": "ChainResponse",
        "DiscoverHit": "DiscoverHit",
        "DiscoverResponse": "DiscoverResponse",
        "DocTypeEntry": "VaultDocTypeEntry",
        "Document": "Document",
        "DocumentDownloadUrlResponse": "DocumentDownloadUrlResponse",
        "DocumentSummary": "DocumentSummary",
        "Edge": "Edge",
        "ExtractedField": "ExtractedField",
        "FieldChange": "FieldChange",
        "HealthIndicators": "HealthIndicators",
        "LastOptimizeSummary": "LastOptimizeSummary",
        "LifecycleState": "VaultLifecycleState",
        "OpenDocumentResponse": "OpenDocumentResponse",
        "OptimizeContentStoreReport": "OptimizeContentStoreReport",
        "PendingMetadata": "PendingMetadataItem",
        "ReabstractReport": "ReabstractReport",
        "ReabstractStartedResponse": "ReabstractStartedResponse",
        "ReadMeta": "ReadMeta",
        "ResolutionPathEntry": "ResolutionPathEntry",
        "StagingEdge": "StagingEdge",
        "StagingEdgeConfirmResponse": "StagingEdgeConfirmResponse",
        "StagingEdgeDismissResponse": "StagingEdgeDismissResponse",
        "TraversalNode": "TraversalNode",
        "TraverseResponse": "TraverseResponse",
        "UpdateConfigResponse": "UpdateVaultConfigResponse",
        "VaultConfigPreview": "VaultConfigPreview",
        "VaultStats": "VaultStatsResponse",
        "VaultSummary": "VaultSummary",
    },
    "app": {
        "LoginChallenge": "LoginChallengeResponse",
        "ScanResponse": "ScanResponse",
        "ScanResultItem": "ScanResultResponse",
        "SessionInfo": "SessionInfoResponse",
        "UserClaims": "UserClaims",
    },
}

# Request interfaces, which F6 additionally holds to their components' required lists.
ENROLLED_REQUESTS: Final[Enrollment] = {
    "core": {
        "BatchIngestFileMetadata": "BatchIngestFileMetadata",
        "BatchIngestParsedMetadata": "BatchIngestParsedMetadata",
        "BatchIngestUploadMetadata": "BatchIngestUploadMetadata",
        "BulkLifecycleItem": "BulkLifecycleItem",
        "BulkLifecycleRequest": "BulkLifecycleRequest",
        "BulkLinkItem": "BulkLinkItem",
        "BulkLinkRequest": "BulkLinkRequest",
        "BulkMetadataItem": "BulkMetadataItem",
        "BulkMetadataRequest": "BulkMetadataRequest",
        "CreateVaultRequest": "CreateVaultRequest",
        "DiscoverRequest": "DiscoverRequest",
        "LinkRequest": "LinkRequest",
        "ListFieldPatch": "ListFieldPatch",
        "ReabstractRequest": "ReabstractRequest",
        "RelocationPointer": "RelocationPointer",
        "RetrievalFilters": "RetrievalFilters",
        "Tier3Patch": "Tier3Patch",
        "TraverseRequest": "TraverseRequest",
        "UpdateMetadataRequest": "UpdateMetadataRequest",
        "UpdateVaultConfigRequest": "UpdateVaultConfigRequest",
    },
    "app": {
        "IngestFileItem": "IngestFileItem",
        "ParsedMetadataItem": "ParsedMetadata",
    },
}

# Stream interfaces, which F7 holds to their components' required lists in both
# directions. F4 holds each specification's set to what its event-stream contract reaches.
ENROLLED_STREAMS: Final[Enrollment] = {
    "core": {
        "BatchDocumentsCreated": "DocumentsCreated",
        "BatchIngestFileError": "BatchIngestFileError",
        "BatchProgressEvent": "ProgressEvent",
        "BatchSummaryEvent": "SummaryEvent",
        "DocTypeRequirements": "DocTypeRequirements",
        "EdgeWarning": "EdgeWarning",
        "IngestPreview": "IngestPreview",
        "ReabstractProgressEvent": "ReabstractProgressEvent",
        "ReabstractReportEntry": "ReabstractReportEntry",
        "ReabstractSummaryEvent": "ReabstractSummaryEvent",
    },
    "app": {
        "BatchIngestFileError": "BatchIngestFileError",
        "DocTypeRequirements": "DocTypeRequirements",
        "EdgeWarning": "EdgeWarning",
        "IngestPreview": "IngestPreview",
        "IngestProgressEvent": "ProgressEvent",
        "IngestSummaryEvent": "SummaryEvent",
    },
}


def merge_enrollments(enrollments: Iterable[Enrollment]) -> Enrollment:
    """Combine enrollments specification by specification."""
    merged: Enrollment = {}
    for enrollment in enrollments:
        for spec, enrolled in enrollment.items():
            merged.setdefault(spec, {}).update(enrolled)
    return merged


def pairs(enrollment: Enrollment) -> list[tuple[Spec, str]]:
    """The ``(specification, interface)`` pairs of an enrollment, in a stable order."""
    return sorted(
        (spec, interface) for spec, enrolled in enrollment.items() for interface in enrolled
    )


def pair_ids(enrollment: Enrollment) -> list[str]:
    return [f"{spec}-{interface}" for spec, interface in pairs(enrollment)]


ALL_ENROLLED: Final[Enrollment] = merge_enrollments((ENROLLED, ENROLLED_REQUESTS, ENROLLED_STREAMS))

# Which side of a divergence carries the property: published by the schema only, or
# declared by the interface only.
Side = Literal["schema_only", "interface_only"]

_DOC_ID_ALIAS_REASON: Final[str] = (
    "back-compatible alias for document_id; the component accepts exactly one of the "
    "two per item, so declaring both would type-permit a body the server refuses"
)

# (specification, interface, property, side) -> the reason the divergence is kept rather
# than closed. The side is part of the key so an entry justifying one direction cannot
# silence the other: a frontend-only field that later becomes schema-only is drift
# again. The specification is part of it because an interface may mirror a component in
# each, and a divergence from one is not a divergence from the other. An entry is an
# admission that the frontend and the published contract disagree about a shape, and
# should be justified in review.
KNOWN_FRONTEND_TYPE_DIVERGENCE: Final[dict[tuple[Spec, str, str, Side], str]] = {
    ("core", "BulkLifecycleItem", "doc_id", "schema_only"): _DOC_ID_ALIAS_REASON,
    ("core", "BulkMetadataItem", "doc_id", "schema_only"): _DOC_ID_ALIAS_REASON,
}

_VAULT_CONFIG_FILE_REASON: Final[str] = (
    "a section of the vault configuration file; the config read route publishes it as an "
    "open object governed by vault_config.schema.json, not as a component"
)

# Exported interfaces that mirror no published component, with the reason. F10 holds
# every exported interface to being enrolled under some specification or named here.
UNENROLLED_INTERFACES: Final[dict[str, str]] = {
    "BulkItemErrorEnvelope": (
        "the per-item error of a bulk result, which the result components publish as an "
        "inline object rather than a component"
    ),
    "DocTypeConfig": _VAULT_CONFIG_FILE_REASON,
    "LifecycleConfig": _VAULT_CONFIG_FILE_REASON,
    "LifecycleStateConfig": _VAULT_CONFIG_FILE_REASON,
    "LifecycleTransitionConfig": _VAULT_CONFIG_FILE_REASON,
    "VaultAbstractionConfig": _VAULT_CONFIG_FILE_REASON,
    "VaultConfig": _VAULT_CONFIG_FILE_REASON,
    "VaultIdentityConfig": _VAULT_CONFIG_FILE_REASON,
}

_DOCUMENTS_TARGET_ONLY_REASON: Final[str] = (
    "the frontend never sets a discover target, so every response it reads is the "
    "documents target, whose rows are DiscoverHit"
)

# (specification, interface, property, component) -> the reason a component the property
# references has no enrolled mirror in that specification. Each entry marks where F8 and
# F9 stop reaching.
UNGATED_REFERENCED_COMPONENTS: Final[dict[tuple[Spec, str, str, str], str]] = {
    ("core", "DiscoverHit", "document", "DocumentSummaryLight"): (
        "the light projection is returned only to a request that leaves response_mode "
        "unset or asks for light, and every discover request whose rows the frontend "
        "reads asks for full"
    ),
    ("core", "DiscoverResponse", "results", "EdgeHit"): _DOCUMENTS_TARGET_ONLY_REASON,
    ("core", "DiscoverResponse", "results", "FacetHit"): _DOCUMENTS_TARGET_ONLY_REASON,
}

# Vacuity floors for F4, per specification, each set below today's count so ordinary
# movement does not trip it while a lookup returning nothing does. In the Core API the
# response pairs compare 227 schema properties, the request pairs 120, and the stream
# pairs 71; the request components require 16; F8 checks 46 references to enrolled
# components; and the event-stream descriptions name 4 event components. In the App API
# those counts are 16, 9, 50, 3, 8 and 2.
MIN_PROPERTIES_COMPARED: Final[dict[Spec, int]] = {"core": 170, "app": 12}
MIN_REQUEST_PROPERTIES_COMPARED: Final[dict[Spec, int]] = {"core": 85, "app": 7}
MIN_STREAM_PROPERTIES_COMPARED: Final[dict[Spec, int]] = {"core": 55, "app": 40}
MIN_REQUIRED_PROPERTIES_COMPARED: Final[dict[Spec, int]] = {"core": 12, "app": 2}
MIN_REFERENCES_CHECKED: Final[dict[Spec, int]] = {"core": 36, "app": 6}
MIN_EVENT_STREAM_ROOTS: Final[dict[Spec, int]] = {"core": 3, "app": 1}
MIN_NULLABLE_OPTIONAL_RESPONSE_PROPERTIES: Final[dict[Spec, int]] = {"core": 40, "app": 3}


# ---------------------------------------------------------------------------
# Declaration reader
# ---------------------------------------------------------------------------


class UnsupportedDeclarationError(ValueError):
    """The declaration uses syntax the reader does not model."""


_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
    | (?P<comment>//[^\n]*|/\*.*?\*/)
    | (?P<string>'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*"|`(?:[^`\\]|\\.)*`)
    | (?P<arrow>=>)
    | (?P<ident>[A-Za-z_$][A-Za-z0-9_$]*)
    | (?P<number>\d+(?:\.\d+)?)
    | (?P<punct>[{}()\[\]<>:;,?|&=.!*+\-/@#%^~])
    """,
    re.VERBOSE | re.DOTALL,
)

_OPENERS: Final[frozenset[str]] = frozenset("{([<")
_CLOSERS: Final[frozenset[str]] = frozenset("})]>")


def _lex(source: str) -> list[tuple[str, str]]:
    """Tokenize ``source`` into ``(kind, text)`` pairs, dropping whitespace and comments."""
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(source):
        match = _TOKEN_RE.match(source, pos)
        if match is None:
            raise UnsupportedDeclarationError(
                f"cannot lex source at offset {pos}: {source[pos : pos + 20]!r}"
            )
        kind = match.lastgroup
        assert kind is not None
        if kind not in ("ws", "comment"):
            tokens.append((kind, match.group()))
        pos = match.end()
    return tokens


def _is_punct(token: tuple[str, str], text: str) -> bool:
    return token == ("punct", text)


# A property member as the reader collects it: whether it is optional, and the
# identifier tokens of its type.
_Member = tuple[bool, frozenset[str]]


def read_interface(source: str, name: str) -> dict[str, bool]:
    """Return the property members of interface ``name`` as ``{property: optional}``.

    Members inherited through a heritage clause are included. Raises ``KeyError``
    when no such interface is declared, and ``UnsupportedDeclarationError`` when it
    is declared more than once or uses syntax the reader does not model.
    """
    return {member: optional for member, (optional, _) in _read_source(source, name).items()}


def read_interface_types(source: str, name: str) -> dict[str, frozenset[str]]:
    """Return the identifier tokens of each property member's type in interface ``name``.

    Reads the declaration exactly as ``read_interface`` does and fails on the same terms.
    """
    return {member: idents for member, (_, idents) in _read_source(source, name).items()}


def exported_interfaces(source: str) -> set[str]:
    """The names of the interfaces ``source`` declares with ``export``."""
    tokens = _lex(source)
    return {
        tokens[i + 2][1]
        for i in range(len(tokens) - 2)
        if tokens[i] == ("ident", "export")
        and tokens[i + 1] == ("ident", "interface")
        and tokens[i + 2][0] == "ident"
    }


def unaccounted_interfaces(
    source: str, enrolled: Collection[str], excluded: dict[str, str]
) -> tuple[set[str], set[str], set[str]]:
    """Return ``(unaccounted, both, stale)`` for the exports of ``source``.

    ``unaccounted`` are exported and neither enrolled nor excluded; ``both`` are
    enrolled and excluded at once; ``stale`` are excluded but no longer exported.
    """
    exported = exported_interfaces(source)
    return (
        exported - set(enrolled) - set(excluded),
        set(enrolled) & set(excluded),
        set(excluded) - exported,
    )


def _read_source(source: str, name: str) -> dict[str, _Member]:
    return _read_interface(_lex(source), name, ())


def _read_interface(
    tokens: list[tuple[str, str]], name: str, seen: tuple[str, ...]
) -> dict[str, _Member]:
    """Read interface ``name`` from ``tokens``; ``seen`` is the heritage chain that reached it."""
    if name in seen:
        chain = " -> ".join((*seen, name))
        raise UnsupportedDeclarationError(f"interface {seen[0]!r}: cyclic heritage {chain}")
    starts = [
        i
        for i in range(len(tokens) - 1)
        if tokens[i] == ("ident", "interface")
        and tokens[i + 1] == ("ident", name)
        and not (i > 0 and _is_punct(tokens[i - 1], "."))
    ]
    if not starts:
        raise KeyError(f"interface {name!r} is not declared")
    if len(starts) > 1:
        raise UnsupportedDeclarationError(f"interface {name!r} is declared {len(starts)} times")

    j = starts[0] + 2
    if j < len(tokens) and _is_punct(tokens[j], "<"):
        depth = 0
        while j < len(tokens):
            if _is_punct(tokens[j], "<"):
                depth += 1
            elif _is_punct(tokens[j], ">"):
                depth -= 1
                if depth == 0:
                    j += 1
                    break
            j += 1
    bases: list[str] = []
    if j < len(tokens) and tokens[j] == ("ident", "extends"):
        j, bases = _read_heritage(tokens, j + 1, name)
    if j >= len(tokens) or not _is_punct(tokens[j], "{"):
        found = tokens[j][1] if j < len(tokens) else "end of source"
        raise UnsupportedDeclarationError(f"interface {name!r}: expected '{{', found {found!r}")
    own = _read_members(tokens, j + 1, name)

    members: dict[str, _Member] = {}
    for base in bases:
        try:
            inherited = _read_interface(tokens, base, (*seen, name))
        except KeyError:
            raise UnsupportedDeclarationError(
                f"interface {name!r}: base {base!r} is not an interface declared in this source"
            ) from None
        _merge_members(members, inherited, name, base)
    _merge_members(members, own, name, name)
    return members


def _read_heritage(tokens: list[tuple[str, str]], j: int, name: str) -> tuple[int, list[str]]:
    """Read the base names of a heritage clause starting at ``j``.

    Returns the index of the body's ``{`` with the bases in declaration order.
    Only bare identifiers are modelled: a type argument or a qualified name would
    make the base something other than the interface its identifier names.
    """
    bases: list[str] = []
    while True:
        if j >= len(tokens) or tokens[j][0] != "ident":
            found = tokens[j][1] if j < len(tokens) else "end of source"
            raise UnsupportedDeclarationError(
                f"interface {name!r}: heritage clause names {found!r}, not a bare interface name"
            )
        base = tokens[j][1]
        j += 1
        if j < len(tokens) and _is_punct(tokens[j], ","):
            bases.append(base)
            j += 1
            continue
        if j < len(tokens) and _is_punct(tokens[j], "{"):
            bases.append(base)
            return j, bases
        found = tokens[j][1] if j < len(tokens) else "end of source"
        raise UnsupportedDeclarationError(
            f"interface {name!r}: base {base!r} is followed by {found!r}, not a bare interface name"
        )


def _merge_members(
    members: dict[str, _Member], incoming: dict[str, _Member], name: str, origin: str
) -> None:
    """Add ``incoming`` to ``members``, refusing a member already collected along the chain."""
    for member, collected in incoming.items():
        if member in members:
            raise UnsupportedDeclarationError(
                f"interface {name!r}: {origin!r} redeclares member {member!r}"
            )
        members[member] = collected


def _read_members(tokens: list[tuple[str, str]], j: int, name: str) -> dict[str, _Member]:
    """Collect depth-one property members of a body whose ``{`` precedes index ``j``."""
    members: dict[str, _Member] = {}
    current: tuple[str, bool] | None = None
    idents: set[str] = set()
    depth = 1
    expecting_member = True
    while j < len(tokens):
        token = tokens[j]
        kind, text = token
        if depth == 1 and expecting_member:
            if _is_punct(token, "}"):
                return members
            if _is_punct(token, ";") or _is_punct(token, ","):
                j += 1
                continue
            if _is_punct(token, "["):
                raise UnsupportedDeclarationError(f"interface {name!r}: index signature")
            if (
                token == ("ident", "readonly")
                and j + 1 < len(tokens)
                and tokens[j + 1][0] in ("ident", "string")
            ):
                j += 1
                continue
            member, colon = _member_head(tokens, j)
            if member is None:
                raise UnsupportedDeclarationError(
                    f"interface {name!r}: unexpected {text!r} where a member starts"
                )
            if colon is None:
                raise UnsupportedDeclarationError(
                    f"interface {name!r}: member {member!r} is not a property signature"
                )
            current = (member, _is_punct(tokens[colon - 1], "?"))
            idents = set()
            expecting_member = False
            j = colon + 1
            continue

        if depth == 1 and _is_punct(token, "?"):
            raise UnsupportedDeclarationError(
                f"interface {name!r}: conditional type or other unmodelled syntax in a member type"
            )
        if depth == 1:
            member, colon = _member_head(tokens, j)
            if member is not None and colon is not None:
                raise UnsupportedDeclarationError(
                    f"interface {name!r}: member {member!r} is not delimited from the one before it"
                )
        if kind == "ident":
            idents.add(text)
        elif kind == "punct" and text in _OPENERS:
            depth += 1
        elif kind == "punct" and text in _CLOSERS:
            depth -= 1
            if depth == 0:
                if text != "}":
                    raise UnsupportedDeclarationError(f"interface {name!r}: unbalanced {text!r}")
                if current is not None:
                    members[current[0]] = (current[1], frozenset(idents))
                return members
        elif depth == 1 and (_is_punct(token, ";") or _is_punct(token, ",")):
            if current is not None:
                members[current[0]] = (current[1], frozenset(idents))
                current = None
            expecting_member = True
        j += 1
    raise UnsupportedDeclarationError(f"interface {name!r}: body is not closed")


def _member_head(tokens: list[tuple[str, str]], j: int) -> tuple[str | None, int | None]:
    """Read a member name at ``j``; return it with the index of its ``:``, if it has one."""
    kind, text = tokens[j]
    if kind == "ident":
        member = text
    elif kind == "string" and not text.startswith("`"):
        member = text[1:-1]
    else:
        return None, None
    k = j + 1
    if k < len(tokens) and _is_punct(tokens[k], "?"):
        k += 1
    if k < len(tokens) and _is_punct(tokens[k], ":"):
        return member, k
    return member, None


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def component_properties(spec: dict, component: str) -> set[str]:
    """The property names a component schema declares inline.

    Raises ``ValueError`` when the component is absent or composes its
    properties from elsewhere, since collecting only the inline part would
    under-report what the contract publishes.
    """
    schemas = (spec.get("components") or {}).get("schemas") or {}
    schema = schemas.get(component)
    if not isinstance(schema, dict):
        raise ValueError(f"component schema {component!r} is not published")
    composed = sorted(key for key in ("$ref", "allOf", "anyOf", "oneOf") if key in schema)
    if composed:
        raise ValueError(f"component schema {component!r} composes its shape via {composed}")
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        raise ValueError(f"component schema {component!r} declares no inline properties")
    return set(properties)


def component_required(spec: dict, component: str) -> set[str]:
    """The property names a component schema lists as required.

    Refuses an absent or composed component on the same terms as
    ``component_properties``.
    """
    component_properties(spec, component)
    return set(spec["components"]["schemas"][component].get("required") or [])


def divergence(interface_props: set[str], schema_props: set[str]) -> tuple[set[str], set[str]]:
    """Return ``(schema_only, interface_only)`` property names."""
    return schema_props - interface_props, interface_props - schema_props


def relaxed_required(members: dict[str, bool], required: set[str]) -> set[str]:
    """Required property names the interface declares optional.

    A required property the interface does not declare at all is F1's to report.
    """
    return {prop for prop in required if members.get(prop, False)}


def stale_entries(
    allowlist: dict[tuple[Spec, str, str, Side], str],
    interface_props: dict[str, set[str]],
    schema_props: dict[tuple[Spec, str], set[str]],
) -> list[str]:
    """Allowlist entries that name no enrolled pair or no longer diverge on their side.

    ``schema_props`` is keyed by ``(specification, interface)``, so an entry names an
    enrolled pair only when the interface is enrolled under the specification it names.
    """
    stale: list[str] = []
    for spec, interface, prop, side in sorted(allowlist):
        label = f"{spec}:{interface}.{prop}"
        if interface not in interface_props or (spec, interface) not in schema_props:
            stale.append(f"{label}: {interface!r} is not enrolled under {spec!r}")
            continue
        schema_only, interface_only = divergence(
            interface_props[interface], schema_props[(spec, interface)]
        )
        sides: dict[Side, set[str]] = {"schema_only": schema_only, "interface_only": interface_only}
        if prop in sides[side]:
            continue
        other: Side = "interface_only" if side == "schema_only" else "schema_only"
        if prop in sides[other]:
            stale.append(f"{label} ({side}): now diverges as {other}; re-examine the entry")
        else:
            stale.append(f"{label} ({side}): no longer diverges; remove the entry")
    return stale


def optionality_mismatch(
    members: dict[str, bool], schema_props: set[str], required: set[str]
) -> tuple[set[str], set[str]]:
    """Return ``(required_but_optional, optional_but_required)`` over the shared properties.

    The first names members the interface marks optional though the component
    requires them; the second names members it marks required though the component
    leaves them optional. A property only one side declares is F1's or F2's to report.
    """
    shared = set(members) & schema_props
    return (
        {prop for prop in shared if members[prop] and prop in required},
        {prop for prop in shared if not members[prop] and prop not in required},
    )


_COMPONENT_REF_PREFIX: Final[str] = "#/components/schemas/"


def schema_references(node: object) -> set[str]:
    """Component names a schema node references, without following into those components."""
    found: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(_COMPONENT_REF_PREFIX):
            found.add(ref[len(_COMPONENT_REF_PREFIX) :])
        for key, value in node.items():
            if key != "$ref":
                found |= schema_references(value)
    elif isinstance(node, list):
        for value in node:
            found |= schema_references(value)
    return found


def component_references(spec: dict, component: str) -> dict[str, set[str]]:
    """Each property of ``component`` that references other components, with their names."""
    component_properties(spec, component)
    properties = spec["components"]["schemas"][component]["properties"]
    references = {prop: schema_references(schema) for prop, schema in properties.items()}
    return {prop: names for prop, names in references.items() if names}


def carries_properties(spec: dict, component: str) -> bool:
    """Whether a component describes an object shape, rather than a scalar or an enumeration."""
    schema = ((spec.get("components") or {}).get("schemas") or {}).get(component)
    if not isinstance(schema, dict):
        raise ValueError(f"component schema {component!r} is not published")
    return any(key in schema for key in ("properties", "allOf", "anyOf", "oneOf", "$ref"))


_STREAM_MEDIA_TYPE: Final[str] = "text/event-stream"
_NAMED_COMPONENT_RE = re.compile(r"components\.schemas\.([A-Za-z_][A-Za-z0-9_]*)")


def event_stream_roots(spec: dict) -> set[str]:
    """The event components each ``text/event-stream`` response names in its description.

    The stream body is published as a string, so its event shapes are named in prose
    rather than referenced; a name that is not a published component is refused.
    """
    schemas = (spec.get("components") or {}).get("schemas") or {}
    roots: set[str] = set()
    for operations in (spec.get("paths") or {}).values():
        for operation in operations.values():
            if not isinstance(operation, dict):
                continue
            for response in (operation.get("responses") or {}).values():
                for media_type, body in ((response or {}).get("content") or {}).items():
                    if media_type != _STREAM_MEDIA_TYPE:
                        continue
                    text = " ".join(
                        str(part.get("description", ""))
                        for part in (response, body, (body or {}).get("schema") or {})
                    )
                    roots |= set(_NAMED_COMPONENT_RE.findall(text))
    unpublished = sorted(root for root in roots if root not in schemas)
    if unpublished:
        raise ValueError(f"event-stream descriptions name unpublished components {unpublished}")
    return roots


def stream_components(spec: dict) -> set[str]:
    """Every component reachable by reference from the event-stream roots, roots included."""
    schemas = spec["components"]["schemas"]
    reached: set[str] = set()
    pending = sorted(event_stream_roots(spec))
    while pending:
        component = pending.pop()
        if component in reached:
            continue
        reached.add(component)
        pending.extend(schema_references(schemas[component]) - reached)
    return reached


def unnamed_references(
    types: dict[str, frozenset[str]],
    references: dict[str, set[str]],
    interface_for: dict[str, str],
) -> dict[str, set[str]]:
    """Members whose type does not name the interface of an enrolled component they reference.

    Returns each such member with the interface names it omits. A member the
    interface does not declare is F1's to report, and a referenced component with no
    enrolled interface is F9's.
    """
    findings: dict[str, set[str]] = {}
    for prop, components in references.items():
        if prop not in types:
            continue
        expected = {interface_for[c] for c in components if c in interface_for}
        missing = expected - types[prop]
        if missing:
            findings[prop] = missing
    return findings


def unpinned_references(
    specs: dict[Spec, dict],
    enrollment: Enrollment,
    pins: dict[tuple[Spec, str, str, str], str],
) -> set[tuple[Spec, str, str, str]]:
    """``(specification, interface, property, component)`` references with no mirror.

    A reference is closed only by a mirror enrolled under the specification that
    publishes both components: a same-named component in another specification is a
    different component.
    """
    findings: set[tuple[Spec, str, str, str]] = set()
    for spec, enrolled in enrollment.items():
        enrolled_components = set(enrolled.values())
        for interface, component in enrolled.items():
            for prop, referenced in component_references(specs[spec], component).items():
                for target in referenced:
                    key = (spec, interface, prop, target)
                    if (
                        target not in enrolled_components
                        and carries_properties(specs[spec], target)
                        and key not in pins
                    ):
                        findings.add(key)
    return findings


def stale_reference_pins(
    specs: dict[Spec, dict],
    enrollment: Enrollment,
    pins: dict[tuple[Spec, str, str, str], str],
) -> list[str]:
    """Pins that name no enrolled pair, no live reference, or a component now enrolled."""
    stale: list[str] = []
    for spec, interface, prop, target in sorted(pins):
        label = f"{spec}:{interface}.{prop} -> {target}"
        enrolled = enrollment.get(spec, {})
        if interface not in enrolled:
            stale.append(f"{label}: {interface!r} is not enrolled under {spec!r}")
        elif target not in component_references(specs[spec], enrolled[interface]).get(prop, set()):
            stale.append(f"{label}: the property no longer references it; remove the pin")
        elif target in enrolled.values():
            stale.append(f"{label}: the component is enrolled; remove the pin")
    return stale


def enrollment_conflicts(enrollments: Iterable[Enrollment]) -> list[str]:
    """Within each specification: interfaces enrolled under more than one rule, and
    components mirrored by more than one interface.

    Each specification is its own namespace, so an interface may mirror one component in
    each, and two specifications may publish components of the same name.
    """
    kinds = list(enrollments)
    conflicts: list[str] = []
    for spec in sorted({spec for enrollment in kinds for spec in enrollment}):
        interfaces = [i for enrollment in kinds for i in enrollment.get(spec, {})]
        repeated = sorted({i for i in interfaces if interfaces.count(i) > 1})
        if repeated:
            conflicts.append(f"{spec}: interfaces enrolled under more than one rule: {repeated}")
        components = [c for enrollment in kinds for c in enrollment.get(spec, {}).values()]
        shared = sorted({c for c in components if components.count(c) > 1})
        if shared:
            conflicts.append(f"{spec}: components mirrored by more than one interface: {shared}")
    return conflicts


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def types_source() -> str:
    return TYPES_TS_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def specs() -> dict[Spec, dict]:
    loaded: dict[Spec, dict] = {}
    for spec, path in SPEC_PATHS.items():
        with path.open(encoding="utf-8") as f:
            loaded[spec] = yaml.safe_load(f)
    return loaded


def enrolled_interfaces(enrollment: Enrollment) -> set[str]:
    """Every interface an enrollment names, under any specification."""
    return {interface for enrolled in enrollment.values() for interface in enrolled}


@pytest.fixture(scope="module")
def interface_members(types_source: str) -> dict[str, dict[str, bool]]:
    return {
        interface: read_interface(types_source, interface)
        for interface in enrolled_interfaces(ALL_ENROLLED)
    }


@pytest.fixture(scope="module")
def interface_types(types_source: str) -> dict[str, dict[str, frozenset[str]]]:
    return {
        interface: read_interface_types(types_source, interface)
        for interface in enrolled_interfaces(ALL_ENROLLED)
    }


@pytest.fixture(scope="module")
def interface_props(interface_members: dict[str, dict[str, bool]]) -> dict[str, set[str]]:
    return {interface: set(members) for interface, members in interface_members.items()}


@pytest.fixture(scope="module")
def schema_props(specs: dict[Spec, dict]) -> dict[tuple[Spec, str], set[str]]:
    return {
        (spec, interface): component_properties(specs[spec], ALL_ENROLLED[spec][interface])
        for spec, interface in pairs(ALL_ENROLLED)
    }


def allowlisted(
    spec: Spec,
    interface: str,
    side: Side,
    allowlist: dict[tuple[Spec, str, str, Side], str],
) -> set[str]:
    """The properties ``allowlist`` exempts on one enrolled pair for one side of a divergence."""
    return {
        prop
        for (entry_spec, name, prop, entry_side) in allowlist
        if entry_spec == spec and name == interface and entry_side == side
    }


def unexempted_schema_only(
    source: str,
    specs: dict[Spec, dict],
    enrollment: Enrollment = ALL_ENROLLED,
    allowlist: dict[tuple[Spec, str, str, Side], str] = KNOWN_FRONTEND_TYPE_DIVERGENCE,
) -> dict[tuple[Spec, str], set[str]]:
    """F1's findings over ``source``: each enrolled pair with a non-exempt omission."""
    findings: dict[tuple[Spec, str], set[str]] = {}
    for spec, interface in pairs(enrollment):
        schema_only, _ = divergence(
            set(read_interface(source, interface)),
            component_properties(specs[spec], enrollment[spec][interface]),
        )
        missing = schema_only - allowlisted(spec, interface, "schema_only", allowlist)
        if missing:
            findings[(spec, interface)] = missing
    return findings


def relaxed_request_members(
    source: str, specs: dict[Spec, dict], enrollment: Enrollment = ENROLLED_REQUESTS
) -> dict[tuple[Spec, str], set[str]]:
    """F6's findings over ``source``: each enrolled request with a relaxed required member."""
    findings: dict[tuple[Spec, str], set[str]] = {}
    for spec, interface in pairs(enrollment):
        relaxed = relaxed_required(
            read_interface(source, interface),
            component_required(specs[spec], enrollment[spec][interface]),
        )
        if relaxed:
            findings[(spec, interface)] = relaxed
    return findings


def interface_for_component(enrolled: dict[str, str]) -> dict[str, str]:
    """Invert one specification's enrollment to ``{component: interface}``.

    F4 holds each specification's enrollment to one-to-one, so the inversion is taken per
    specification: across two, one component name can name two components.
    """
    return {component: interface for interface, component in enrolled.items()}


def stream_optionality_findings(
    source: str, specs: dict[Spec, dict], enrollment: Enrollment = ENROLLED_STREAMS
) -> dict[tuple[Spec, str], set[str]]:
    """F7's findings over ``source``: each stream pair with a mismatched member."""
    findings: dict[tuple[Spec, str], set[str]] = {}
    for spec, interface in pairs(enrollment):
        component = enrollment[spec][interface]
        relaxed, tightened = optionality_mismatch(
            read_interface(source, interface),
            component_properties(specs[spec], component),
            component_required(specs[spec], component),
        )
        if relaxed | tightened:
            findings[(spec, interface)] = relaxed | tightened
    return findings


def nullable_optional_properties(spec: dict, component: str) -> set[str]:
    """Properties the component leaves optional and whose declaration admits null.

    These are the keys the wire omits when their value is null. Nullability is read
    as a 3.1 client reads it, by the same reader the serialized-shape gate uses.
    """
    from tests.sage.test_wire_shape_conformance import _spec_admits_null

    required = component_required(spec, component)
    properties = spec["components"]["schemas"][component]["properties"]
    return {
        prop
        for prop, declaration in properties.items()
        if prop not in required and _spec_admits_null(declaration, spec)
    }


def omissible_members_typed_present(members: dict[str, bool], omissible: set[str]) -> set[str]:
    """Members the interface declares required though the wire can omit their key.

    A property the interface does not declare at all is F1's to report.
    """
    return {prop for prop in omissible if prop in members and not members[prop]}


def response_omission_findings(
    source: str, specs: dict[Spec, dict], enrollment: Enrollment = ENROLLED
) -> dict[tuple[Spec, str], set[str]]:
    """F11's findings over ``source``: each response pair typing an omissible member present."""
    findings: dict[tuple[Spec, str], set[str]] = {}
    for spec, interface in pairs(enrollment):
        present = omissible_members_typed_present(
            read_interface(source, interface),
            nullable_optional_properties(specs[spec], enrollment[spec][interface]),
        )
        if present:
            findings[(spec, interface)] = present
    return findings


def unnamed_reference_findings(
    source: str, specs: dict[Spec, dict], enrollment: Enrollment = ALL_ENROLLED
) -> dict[tuple[Spec, str], set[str]]:
    """F8's findings over ``source``: each enrolled pair with an unnamed reference."""
    findings: dict[tuple[Spec, str], set[str]] = {}
    for spec, interface in pairs(enrollment):
        unnamed = unnamed_references(
            read_interface_types(source, interface),
            component_references(specs[spec], enrollment[spec][interface]),
            interface_for_component(enrollment[spec]),
        )
        if unnamed:
            findings[(spec, interface)] = set(unnamed)
    return findings


def _interface_body(source: str, interface: str) -> re.Match[str]:
    body = re.search(
        rf"^export interface {re.escape(interface)}\b[^\n]*\{{\n(?P<body>.*?)^\}}",
        source,
        re.MULTILINE | re.DOTALL,
    )
    assert body is not None, f"interface {interface!r} body not found"
    return body


def rewrite_member_type(source: str, interface: str, member: str, new_type: str) -> str:
    """Replace the one-line type of ``member`` in the body of exported ``interface``."""
    body = _interface_body(source, interface)
    mutated, count = re.subn(
        rf"^([ \t]*{re.escape(member)}\??[ \t]*:)[^\n;]*;",
        lambda match: f"{match.group(1)} {new_type};",
        body.group("body"),
        flags=re.MULTILINE,
    )
    assert count == 1, f"{interface}.{member} is not declared on one line exactly once in its body"
    return source[: body.start("body")] + mutated + source[body.end("body") :]


def rewrite_member(source: str, interface: str, member: str, replacement: str) -> str:
    """Rewrite the one declaration head of ``member`` in the body of exported ``interface``.

    The head is the member's name, optional marker and colon; ``replacement`` is
    substituted for it. An empty ``replacement`` removes a declaration that ends on its
    own line, and refuses a member whose type continues onto further lines.
    """
    body = _interface_body(source, interface)
    if replacement:
        pattern, repl = rf"^([ \t]*){re.escape(member)}\??[ \t]*:", rf"\g<1>{replacement}"
    else:
        pattern, repl = rf"^[ \t]*{re.escape(member)}\??[ \t]*:[^\n]*;[ \t]*\n", ""
    mutated, count = re.subn(pattern, repl, body.group("body"), flags=re.MULTILINE)
    assert count == 1, f"{interface}.{member} is not declared exactly once in its body"
    return source[: body.start("body")] + mutated + source[body.end("body") :]


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("spec", "interface"), pairs(ALL_ENROLLED), ids=pair_ids(ALL_ENROLLED))
def test_schema_properties_are_declared_on_the_interface(
    spec: Spec,
    interface: str,
    interface_props: dict[str, set[str]],
    schema_props: dict[tuple[Spec, str], set[str]],
) -> None:
    """F1: a property the published schema declares is declared on the interface."""
    schema_only, _ = divergence(interface_props[interface], schema_props[(spec, interface)])
    missing = sorted(
        schema_only - allowlisted(spec, interface, "schema_only", KNOWN_FRONTEND_TYPE_DIVERGENCE)
    )
    assert not missing, (
        f"{ALL_ENROLLED[spec][interface]} in {SPEC_PATHS[spec].name} publishes properties the "
        f"TypeScript {interface} interface does not declare: {missing}. The published schema "
        f"is authoritative; declare them in {TYPES_TS_PATH.name}."
    )


@pytest.mark.parametrize(("spec", "interface"), pairs(ALL_ENROLLED), ids=pair_ids(ALL_ENROLLED))
def test_interface_declares_no_property_absent_from_the_schema(
    spec: Spec,
    interface: str,
    interface_props: dict[str, set[str]],
    schema_props: dict[tuple[Spec, str], set[str]],
) -> None:
    """F2: a property only the interface declares is not supplied by the wire."""
    _, interface_only = divergence(interface_props[interface], schema_props[(spec, interface)])
    extra = sorted(
        interface_only
        - allowlisted(spec, interface, "interface_only", KNOWN_FRONTEND_TYPE_DIVERGENCE)
    )
    assert not extra, (
        f"The TypeScript {interface} interface declares properties "
        f"{ALL_ENROLLED[spec][interface]} in {SPEC_PATHS[spec].name} does not publish: {extra}. "
        "Remove them, or add a KNOWN_FRONTEND_TYPE_DIVERGENCE entry naming why a frontend-only "
        "field exists."
    )


def test_divergence_allowlist_has_no_stale_entries(
    interface_props: dict[str, set[str]],
    schema_props: dict[tuple[Spec, str], set[str]],
) -> None:
    """F3: every allowlist entry still names a live divergence."""
    stale = stale_entries(KNOWN_FRONTEND_TYPE_DIVERGENCE, interface_props, schema_props)
    assert not stale, "stale KNOWN_FRONTEND_TYPE_DIVERGENCE entries:\n" + "\n".join(stale)


@pytest.mark.parametrize(
    ("spec", "interface"), pairs(ENROLLED_REQUESTS), ids=pair_ids(ENROLLED_REQUESTS)
)
def test_required_request_properties_are_not_optional_on_the_interface(
    spec: Spec,
    interface: str,
    interface_members: dict[str, dict[str, bool]],
    specs: dict[Spec, dict],
) -> None:
    """F6: a property the request component requires is non-optional on the interface."""
    component = ENROLLED_REQUESTS[spec][interface]
    relaxed = sorted(
        relaxed_required(interface_members[interface], component_required(specs[spec], component))
    )
    assert not relaxed, (
        f"{component} in {SPEC_PATHS[spec].name} requires properties the TypeScript {interface} "
        f"interface marks optional: {relaxed}. The server refuses a body without them; "
        f"remove the '?' in {TYPES_TS_PATH.name}."
    )


@pytest.mark.parametrize(
    ("spec", "interface"), pairs(ENROLLED_STREAMS), ids=pair_ids(ENROLLED_STREAMS)
)
def test_stream_member_optionality_matches_the_required_list(
    spec: Spec,
    interface: str,
    interface_members: dict[str, dict[str, bool]],
    specs: dict[Spec, dict],
) -> None:
    """F7: a stream interface marks a member optional exactly when its component does."""
    component = ENROLLED_STREAMS[spec][interface]
    relaxed, tightened = optionality_mismatch(
        interface_members[interface],
        component_properties(specs[spec], component),
        component_required(specs[spec], component),
    )
    assert not relaxed and not tightened, (
        f"{component} in {SPEC_PATHS[spec].name} is delivered over an event stream, where a "
        "required key is always present and an optional null key is omitted. The TypeScript "
        f"{interface} interface marks required members optional: {sorted(relaxed)}; and marks "
        f"optional members required, which the stream may omit: {sorted(tightened)}. "
        f"Match the '?' to the component's required list in {TYPES_TS_PATH.name}."
    )


@pytest.mark.parametrize(("spec", "interface"), pairs(ENROLLED), ids=pair_ids(ENROLLED))
def test_response_member_the_wire_can_omit_is_optional(
    spec: Spec,
    interface: str,
    interface_members: dict[str, dict[str, bool]],
    specs: dict[Spec, dict],
) -> None:
    """F11: a response member whose optional property admits null is optional on the interface."""
    component = ENROLLED[spec][interface]
    present = sorted(
        omissible_members_typed_present(
            interface_members[interface], nullable_optional_properties(specs[spec], component)
        )
    )
    assert not present, (
        f"{component} in {SPEC_PATHS[spec].name} leaves these properties optional and "
        "nullable, so a REST body omits the key when the value is null. The TypeScript "
        f"{interface} interface declares them always present: {present}. Mark them optional "
        f"('?: T | null') in {TYPES_TS_PATH.name}, and read them with '!= null' or '??'."
    )


@pytest.mark.parametrize(("spec", "interface"), pairs(ALL_ENROLLED), ids=pair_ids(ALL_ENROLLED))
def test_member_names_the_interface_of_each_enrolled_component_it_references(
    spec: Spec,
    interface: str,
    interface_types: dict[str, dict[str, frozenset[str]]],
    specs: dict[Spec, dict],
) -> None:
    """F8: a member referencing an enrolled component names that component's interface."""
    unnamed = unnamed_references(
        interface_types[interface],
        component_references(specs[spec], ALL_ENROLLED[spec][interface]),
        interface_for_component(ALL_ENROLLED[spec]),
    )
    assert not unnamed, (
        f"Members of the TypeScript {interface} interface reference enrolled components in "
        f"{ALL_ENROLLED[spec][interface]} ({SPEC_PATHS[spec].name}) but their types do not "
        "name the mirroring interfaces: "
        f"{ {prop: sorted(names) for prop, names in sorted(unnamed.items())} }. "
        f"Type each member with its component's interface in {TYPES_TS_PATH.name}."
    )


def test_referenced_object_components_are_enrolled_or_pinned(specs: dict[Spec, dict]) -> None:
    """F9: every object component an enrolled component references is enrolled or pinned."""
    unpinned = sorted(unpinned_references(specs, ALL_ENROLLED, UNGATED_REFERENCED_COMPONENTS))
    assert not unpinned, (
        "Enrolled components reference object components with no interface enrolled under "
        f"the same specification: {[f'{s}:{i}.{p} -> {c}' for s, i, p, c in unpinned]}. "
        "Enroll a mirroring interface, or pin the reference in UNGATED_REFERENCED_COMPONENTS "
        "naming why it is not mirrored."
    )


def test_ungated_reference_pins_have_no_stale_entries(specs: dict[Spec, dict]) -> None:
    """F3: every pin still names an unenrolled component its property references."""
    stale = stale_reference_pins(specs, ALL_ENROLLED, UNGATED_REFERENCED_COMPONENTS)
    assert not stale, "stale UNGATED_REFERENCED_COMPONENTS entries:\n" + "\n".join(stale)


def test_every_exported_interface_is_enrolled_or_excluded(types_source: str) -> None:
    """F10: an exported interface is enrolled or excluded with a reason, never both."""
    unaccounted, both, stale = unaccounted_interfaces(
        types_source, enrolled_interfaces(ALL_ENROLLED), UNENROLLED_INTERFACES
    )
    assert not unaccounted and not both and not stale, (
        f"Exported interfaces in {TYPES_TS_PATH.name} neither enrolled nor excluded: "
        f"{sorted(unaccounted)}; both enrolled and excluded: {sorted(both)}; excluded but no "
        f"longer exported: {sorted(stale)}. Enroll each mirror of a published component, and "
        "name every other interface in UNENROLLED_INTERFACES with the reason it mirrors none."
    )


@pytest.mark.parametrize("spec", sorted(SPEC_PATHS))
def test_stream_enrollment_matches_the_event_stream_contract(
    spec: Spec, specs: dict[Spec, dict]
) -> None:
    """F4: the stream enrollment is exactly the enrolled components an event stream reaches."""
    roots = event_stream_roots(specs[spec])
    assert len(roots) >= MIN_EVENT_STREAM_ROOTS[spec], (
        f"only {sorted(roots)} named as event-stream components in {SPEC_PATHS[spec].name}; "
        f"floor is {MIN_EVENT_STREAM_ROOTS[spec]}"
    )
    reached = stream_components(specs[spec]) & set(ALL_ENROLLED[spec].values())
    enrolled = set(ENROLLED_STREAMS[spec].values())
    assert reached == enrolled, (
        f"Components {SPEC_PATHS[spec].name} delivers over an event stream and enrolled under "
        f"another rule: {sorted(reached - enrolled)}; enrolled as stream shapes but not "
        f"reachable from an event stream: {sorted(enrolled - reached)}. Move them into or out "
        f"of ENROLLED_STREAMS[{spec!r}]."
    )


@pytest.mark.parametrize(
    ("gate", "enrollment"),
    [
        (test_schema_properties_are_declared_on_the_interface, ALL_ENROLLED),
        (test_interface_declares_no_property_absent_from_the_schema, ALL_ENROLLED),
        (test_required_request_properties_are_not_optional_on_the_interface, ENROLLED_REQUESTS),
        (test_stream_member_optionality_matches_the_required_list, ENROLLED_STREAMS),
        (test_response_member_the_wire_can_omit_is_optional, ENROLLED),
        (
            test_member_names_the_interface_of_each_enrolled_component_it_references,
            ALL_ENROLLED,
        ),
    ],
    ids=["F1", "F2", "F6", "F7", "F11", "F8"],
)
def test_gates_are_parametrized_over_their_enrollment(gate: object, enrollment: Enrollment) -> None:
    """F4: each per-interface gate iterates the whole of the enrollment it enforces.

    The F5 probes exercise the comparison helpers, not the parametrized gates, so a
    gate narrowed back to a subset of its enrollment, or to one specification, would
    leave them green.
    """
    marks = [mark for mark in getattr(gate, "pytestmark", []) if mark.name == "parametrize"]
    assert len(marks) == 1, (
        f"{getattr(gate, '__name__', gate)} carries {len(marks)} parametrizations"
    )
    assert marks[0].args == (("spec", "interface"), pairs(enrollment))
    assert set(enrollment) == set(SPEC_PATHS)


def test_enrollment_resolves_and_is_not_vacuous(types_source: str, specs: dict[Spec, dict]) -> None:
    """F4: every enrolled pair resolves, and each kind compares enough properties."""
    conflicts = enrollment_conflicts((ENROLLED, ENROLLED_REQUESTS, ENROLLED_STREAMS))
    assert not conflicts, "\n".join(conflicts)
    for spec in SPEC_PATHS:
        floors = (
            ("response", ENROLLED, MIN_PROPERTIES_COMPARED),
            ("request", ENROLLED_REQUESTS, MIN_REQUEST_PROPERTIES_COMPARED),
            ("stream", ENROLLED_STREAMS, MIN_STREAM_PROPERTIES_COMPARED),
        )
        for kind, enrollment, floor in floors:
            compared = 0
            for interface, component in enrollment[spec].items():
                assert read_interface(types_source, interface), (
                    f"interface {interface!r} declares no properties"
                )
                compared += len(component_properties(specs[spec], component))
            assert compared >= floor[spec], (
                f"only {compared} {kind} schema properties compared in {spec}; "
                f"floor is {floor[spec]}"
            )
        required = sum(
            len(component_required(specs[spec], component))
            for component in ENROLLED_REQUESTS[spec].values()
        )
        assert required >= MIN_REQUIRED_PROPERTIES_COMPARED[spec], (
            f"only {required} required request properties compared in {spec}; "
            f"floor is {MIN_REQUIRED_PROPERTIES_COMPARED[spec]}"
        )
        omissible = sum(
            len(nullable_optional_properties(specs[spec], component))
            for component in ENROLLED[spec].values()
        )
        assert omissible >= MIN_NULLABLE_OPTIONAL_RESPONSE_PROPERTIES[spec], (
            f"only {omissible} optional nullable response properties compared in {spec}; "
            f"floor is {MIN_NULLABLE_OPTIONAL_RESPONSE_PROPERTIES[spec]}"
        )
        interface_for = interface_for_component(ALL_ENROLLED[spec])
        references = sum(
            len(referenced & set(interface_for))
            for component in ALL_ENROLLED[spec].values()
            for referenced in component_references(specs[spec], component).values()
        )
        assert references >= MIN_REFERENCES_CHECKED[spec], (
            f"only {references} references to enrolled components checked in {spec}; "
            f"floor is {MIN_REFERENCES_CHECKED[spec]}"
        )


def test_gate_fires_when_a_declared_property_is_removed(
    types_source: str, specs: dict[Spec, dict]
) -> None:
    """F5: removing a declaration from the real file surfaces exactly that property."""
    declaration = re.compile(r"^[ \t]*stored_content_hash\??[ \t]*:[^\n]*\n", re.MULTILINE)
    mutated, removed = declaration.subn("", types_source)
    assert removed == 1, "mutation target is not declared exactly once"
    assert mutated != types_source

    document_only, _ = divergence(
        set(read_interface(mutated, "Document")), component_properties(specs["core"], "Document")
    )
    summary_only, _ = divergence(
        set(read_interface(mutated, "DocumentSummary")),
        component_properties(specs["core"], "DocumentSummary"),
    )
    assert document_only == {"stored_content_hash"}
    assert summary_only == set()


def test_gate_fires_when_a_request_declaration_is_removed(
    types_source: str, specs: dict[Spec, dict]
) -> None:
    """F5: removing one request member surfaces exactly that member, on that interface only."""
    assert unexempted_schema_only(types_source, specs) == {}
    mutated = rewrite_member(types_source, "DiscoverRequest", "facet_value_limit", "")
    assert unexempted_schema_only(mutated, specs) == {
        ("core", "DiscoverRequest"): {"facet_value_limit"}
    }


def test_gate_sees_a_base_member_removed_through_heritage(
    types_source: str, specs: dict[Spec, dict]
) -> None:
    """F5: a member removed from a base is missing from the interface that inherits it."""
    assert unexempted_schema_only(types_source, specs) == {}
    mutated = rewrite_member(types_source, "BulkLinkItem", "rationale_kind", "")
    assert unexempted_schema_only(mutated, specs) == {
        ("core", "BulkLinkItem"): {"rationale_kind"},
        ("core", "LinkRequest"): {"rationale_kind"},
    }


def test_optionality_check_fires_when_a_required_member_is_relaxed(
    types_source: str, specs: dict[Spec, dict]
) -> None:
    """F5: marking an inherited required member optional is reported wherever it is inherited."""
    assert relaxed_request_members(types_source, specs) == {}
    mutated = rewrite_member(types_source, "BulkLinkItem", "edge_type", "edge_type?:")
    assert relaxed_request_members(mutated, specs) == {
        ("core", "BulkLinkItem"): {"edge_type"},
        ("core", "LinkRequest"): {"edge_type"},
    }


def test_gate_fires_on_a_newly_enrolled_interface(
    types_source: str, specs: dict[Spec, dict]
) -> None:
    """F5: a member removed from a referenced interface is reported on that interface alone."""
    assert unexempted_schema_only(types_source, specs) == {}
    mutated = rewrite_member(types_source, "Edge", "rationale", "")
    assert unexempted_schema_only(mutated, specs) == {("core", "Edge"): {"rationale"}}


def test_stream_optionality_check_fires_in_both_directions(
    types_source: str, specs: dict[Spec, dict]
) -> None:
    """F5: F7 reports a stream member made required and one made optional, each where it is."""
    assert stream_optionality_findings(types_source, specs) == {}
    tightened = rewrite_member(types_source, "ReabstractProgressEvent", "outcome", "outcome:")
    assert stream_optionality_findings(tightened, specs) == {
        ("core", "ReabstractProgressEvent"): {"outcome"}
    }
    relaxed = rewrite_member(types_source, "IngestPreview", "would_create", "would_create?:")
    assert stream_optionality_findings(relaxed, specs) == {
        ("core", "IngestPreview"): {"would_create"},
        ("app", "IngestPreview"): {"would_create"},
    }


def test_response_omission_check_fires_on_a_member_typed_present(
    types_source: str, specs: dict[Spec, dict]
) -> None:
    """F5: F11 reports an omissible response member typed present, on that interface alone."""
    assert response_omission_findings(types_source, specs) == {}
    mutated = rewrite_member(types_source, "DiscoverHit", "relevance_score", "relevance_score:")
    assert response_omission_findings(mutated, specs) == {
        ("core", "DiscoverHit"): {"relevance_score"}
    }


def test_nullable_optional_properties_reads_both_conditions() -> None:
    """F11: only a property that is both optional and nullable can be omitted."""
    spec = {
        "components": {
            "schemas": {
                "Shape": {
                    "type": "object",
                    "required": ["req_null"],
                    "properties": {
                        "req_null": {"type": ["string", "null"]},
                        "opt_null": {"type": ["string", "null"]},
                        "opt_ref_null": {
                            "anyOf": [{"$ref": "#/components/schemas/Other"}, {"type": "null"}]
                        },
                        "opt_value": {"type": "string"},
                        "opt_legacy": {"type": "string", "nullable": True},
                    },
                },
                "Other": {"type": "object", "properties": {"x": {"type": "string"}}},
            }
        }
    }
    assert nullable_optional_properties(spec, "Shape") == {"opt_null", "opt_ref_null"}
    members = {"opt_null": False, "opt_ref_null": True, "opt_value": False}
    assert omissible_members_typed_present(members, {"opt_null", "opt_ref_null", "absent"}) == {
        "opt_null"
    }


def test_reference_check_fires_when_a_member_stops_naming_its_component(
    types_source: str, specs: dict[Spec, dict]
) -> None:
    """F5: F8 reports a member retyped away from the interface of the component it references."""
    assert unnamed_reference_findings(types_source, specs) == {}
    mutated = rewrite_member_type(types_source, "TraversalNode", "edge", "StagingEdge")
    assert unnamed_reference_findings(mutated, specs) == {("core", "TraversalNode"): {"edge"}}


def test_gate_fires_on_a_newly_enrolled_app_interface(
    types_source: str, specs: dict[Spec, dict]
) -> None:
    """F5: a member removed from an App API mirror is reported under that specification alone."""
    assert unexempted_schema_only(types_source, specs) == {}
    mutated = rewrite_member(types_source, "IngestSummaryEvent", "edges_removed", "")
    assert unexempted_schema_only(mutated, specs) == {
        ("app", "IngestSummaryEvent"): {"edges_removed"}
    }


def test_gate_reports_a_doubly_enrolled_interface_under_each_specification(
    types_source: str, specs: dict[Spec, dict]
) -> None:
    """F5: an interface mirroring a component in each specification is compared against both."""
    assert unexempted_schema_only(types_source, specs) == {}
    mutated = rewrite_member(types_source, "IngestPreview", "would_supersede", "")
    assert unexempted_schema_only(mutated, specs) == {
        ("core", "IngestPreview"): {"would_supersede"},
        ("app", "IngestPreview"): {"would_supersede"},
    }


def test_reference_check_fires_on_an_app_interface(
    types_source: str, specs: dict[Spec, dict]
) -> None:
    """F5: F8 reports an App API mirror retyped away from the interface its component references."""
    assert unnamed_reference_findings(types_source, specs) == {}
    mutated = rewrite_member_type(types_source, "SessionInfo", "user", "string | null")
    assert unnamed_reference_findings(mutated, specs) == {("app", "SessionInfo"): {"user"}}


def test_enrollment_checks_are_evaluated_within_one_specification() -> None:
    """F4/F1/F8: one component name published by two specifications is two components."""
    specs: dict[Spec, dict] = {
        "core": {
            "components": {
                "schemas": {
                    "E": {"properties": {"a": {}}},
                    "R": {"properties": {"e": {"$ref": "#/components/schemas/E"}}},
                }
            }
        },
        "app": {
            "components": {
                "schemas": {
                    "E": {"properties": {"b": {}}},
                    "R": {"properties": {"e": {"$ref": "#/components/schemas/E"}}},
                }
            }
        },
    }
    source = """
    interface A { a: string }
    interface B { b: string }
    interface CoreR { e: A }
    interface AppR { e: B }
    """
    enrolled: Enrollment = {"core": {"A": "E", "CoreR": "R"}, "app": {"B": "E", "AppR": "R"}}
    assert enrollment_conflicts([enrolled]) == []
    assert unexempted_schema_only(source, specs, enrolled, {}) == {}
    assert unnamed_reference_findings(source, specs, enrolled) == {}

    swapped: Enrollment = {"core": {"B": "E", "CoreR": "R"}, "app": {"A": "E", "AppR": "R"}}
    assert unexempted_schema_only(source, specs, swapped, {}) == {
        ("core", "B"): {"a"},
        ("app", "A"): {"b"},
    }
    assert unnamed_reference_findings(source, specs, swapped) == {
        ("core", "CoreR"): {"e"},
        ("app", "AppR"): {"e"},
    }
    assert enrollment_conflicts([{"core": {"A": "E", "B": "E"}}]) == [
        "core: components mirrored by more than one interface: ['E']"
    ]
    assert enrollment_conflicts([{"core": {"A": "E"}}, {"core": {"A": "R"}}]) == [
        "core: interfaces enrolled under more than one rule: ['A']"
    ]


def test_stream_optionality_is_evaluated_within_one_specification() -> None:
    """F7: a stream member is held to the required list of its own specification's component.

    The two specifications' stream components are shape-identical today, so no real-file
    assertion separates a check reading the pair's specification from one reading the other.
    """
    specs: dict[Spec, dict] = {
        "core": {
            "components": {
                "schemas": {"S": {"properties": {"a": {}, "b": {}}, "required": ["a", "b"]}}
            }
        },
        "app": {
            "components": {"schemas": {"S": {"properties": {"a": {}, "b": {}}, "required": ["a"]}}}
        },
    }
    source = "interface I { a: string; b: string }"
    enrolled: Enrollment = {"core": {"I": "S"}, "app": {"I": "S"}}
    assert stream_optionality_findings(source, specs, enrolled) == {("app", "I"): {"b"}}


def test_reference_closure_is_evaluated_within_one_specification() -> None:
    """F9: a component enrolled under one specification does not close a reference in another."""
    schemas = {
        "X": {"properties": {"y": {"$ref": "#/components/schemas/Y"}}},
        "Y": {"properties": {"a": {}}},
    }
    specs: dict[Spec, dict] = {
        "core": {"components": {"schemas": schemas}},
        "app": {"components": {"schemas": schemas}},
    }
    enrolled: Enrollment = {"core": {"X": "X", "Y": "Y"}, "app": {"X": "X"}}
    assert unpinned_references(specs, enrolled, {}) == {("app", "X", "y", "Y")}
    assert unpinned_references(specs, enrolled, {("app", "X", "y", "Y"): "reason"}) == set()
    assert stale_reference_pins(specs, enrolled, {("app", "X", "y", "Y"): "reason"}) == []
    assert stale_reference_pins(specs, enrolled, {("core", "X", "y", "Y"): "reason"}) == [
        "core:X.y -> Y: the component is enrolled; remove the pin"
    ]


def test_reference_closure_flags_an_unpinned_component() -> None:
    """F9: an object reference with no mirror is reported unless pinned; a scalar one is not."""
    spec = {
        "components": {
            "schemas": {
                "X": {
                    "properties": {
                        "ys": {"type": "array", "items": {"$ref": "#/components/schemas/Y"}},
                        "kind": {"$ref": "#/components/schemas/Kind"},
                        "z": {"anyOf": [{"$ref": "#/components/schemas/Z"}, {"type": "null"}]},
                    }
                },
                "Y": {"properties": {"a": {}}},
                "Z": {"properties": {"b": {}}},
                "Kind": {"type": "string", "enum": ["a", "b"]},
            }
        }
    }
    specs: dict[Spec, dict] = {"core": spec}
    enrolled: Enrollment = {"core": {"X": "X", "ZMirror": "Z"}}
    assert unpinned_references(specs, enrolled, {}) == {("core", "X", "ys", "Y")}
    assert unpinned_references(specs, enrolled, {("core", "X", "ys", "Y"): "reason"}) == set()
    assert stale_reference_pins(
        specs,
        enrolled,
        {
            ("core", "X", "ys", "Y"): "live",
            ("core", "X", "kind", "Y"): "not referenced by that property",
            ("core", "X", "z", "Z"): "enrolled",
            ("core", "Other", "ys", "Y"): "unenrolled interface",
        },
    ) == [
        "core:Other.ys -> Y: 'Other' is not enrolled under 'core'",
        "core:X.kind -> Y: the property no longer references it; remove the pin",
        "core:X.z -> Z: the component is enrolled; remove the pin",
    ]


def test_reference_check_matches_interface_names_as_tokens() -> None:
    """F8: a member names an interface only through an identifier token of its type."""
    source = """
    interface X {
      direct: Edge;
      nullable: Edge | null;
      listed: Array<Edge>;
      quoted: 'Edge';
      longer: EdgeWarning;
      commented: string /* Edge */;
    }
    """
    references = {prop: {"EdgeComponent"} for prop in read_interface(source, "X")}
    references["unrelated"] = {"EdgeComponent"}
    unnamed = unnamed_references(
        read_interface_types(source, "X"), references, {"EdgeComponent": "Edge"}
    )
    assert unnamed == {prop: {"Edge"} for prop in ("quoted", "longer", "commented")}
    assert unnamed_references(read_interface_types(source, "X"), references, {}) == {}


def test_event_stream_roots_are_read_from_stream_descriptions_only() -> None:
    """F4: roots come from event-stream bodies, and reachability follows references from them."""
    spec = {
        "paths": {
            "/stream": {
                "post": {
                    "responses": {
                        "200": {
                            "content": {
                                "text/event-stream": {
                                    "schema": {
                                        "type": "string",
                                        "description": "Events conform to "
                                        "components.schemas.Event.",
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "/json": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"description": "components.schemas.Other"}
                                }
                            }
                        }
                    }
                }
            },
        },
        "components": {
            "schemas": {
                "Event": {"properties": {"entry": {"$ref": "#/components/schemas/Entry"}}},
                "Entry": {"properties": {"a": {}}},
                "Other": {"properties": {"b": {}}},
            }
        },
    }
    assert event_stream_roots(spec) == {"Event"}
    assert stream_components(spec) == {"Event", "Entry"}
    spec["components"]["schemas"].pop("Event")
    with pytest.raises(ValueError, match="unpublished"):
        event_stream_roots(spec)


def test_optionality_mismatch_reports_both_directions() -> None:
    """F7: required members marked optional and optional members marked required are both found."""
    members = {"req_opt": True, "req_req": False, "opt_opt": True, "opt_req": False, "extra": False}
    schema_props = {"req_opt", "req_req", "opt_opt", "opt_req", "absent"}
    required = {"req_opt", "req_req", "absent"}
    assert optionality_mismatch(members, schema_props, required) == ({"req_opt"}, {"opt_req"})


def test_export_check_fires_on_an_unaccounted_interface(types_source: str) -> None:
    """F5: F10 reports a new unenrolled export and a dropped exclusion, each by name."""
    enrolled = enrolled_interfaces(ALL_ENROLLED)
    assert unaccounted_interfaces(types_source, enrolled, UNENROLLED_INTERFACES) == (
        set(),
        set(),
        set(),
    )
    added = types_source + "\nexport interface Unmirrored {\n  a: string;\n}\n"
    assert unaccounted_interfaces(added, enrolled, UNENROLLED_INTERFACES)[0] == {"Unmirrored"}
    dropped = {name: r for name, r in UNENROLLED_INTERFACES.items() if name != "VaultConfig"}
    assert unaccounted_interfaces(types_source, enrolled, dropped)[0] == {"VaultConfig"}


def test_export_check_reports_overlap_and_stale_exclusions() -> None:
    """F10: an interface enrolled and excluded at once, and a stale exclusion, are reported."""
    source = "export interface A { a: string }\nexport interface B { b: string }"
    assert unaccounted_interfaces(source, {"A": "A"}, {"A": "r", "B": "r", "Gone": "r"}) == (
        set(),
        {"A"},
        {"Gone"},
    )


def test_component_required_reads_the_required_list() -> None:
    """F6: the required list is read as declared, and its absence means nothing is required."""
    spec = {
        "components": {
            "schemas": {
                "X": {"properties": {"a": {}, "b": {}}, "required": ["a"]},
                "Y": {"properties": {"a": {}}},
            }
        }
    }
    assert component_required(spec, "X") == {"a"}
    assert component_required(spec, "Y") == set()
    assert relaxed_required({"a": True, "b": True}, {"a"}) == {"a"}
    assert relaxed_required({"b": False}, {"a"}) == set()


def test_stale_check_flags_an_entry_that_no_longer_diverges() -> None:
    """F5: the staleness check keeps live entries and flags stale, flipped and unenrolled ones."""
    interface_props = {"Thing": {"shared", "frontend_only"}}
    schema_props: dict[tuple[Spec, str], set[str]] = {("core", "Thing"): {"shared", "schema_only"}}
    allowlist: dict[tuple[Spec, str, str, Side], str] = {
        ("core", "Thing", "frontend_only", "interface_only"): "live",
        ("core", "Thing", "schema_only", "schema_only"): "live",
        ("core", "Thing", "shared", "interface_only"): "stale",
        ("core", "Thing", "schema_only", "interface_only"): "diverges, but on the other side",
        ("core", "Other", "anything", "schema_only"): "unenrolled",
        ("app", "Thing", "schema_only", "schema_only"): "enrolled, but not under this spec",
    }
    stale = stale_entries(allowlist, interface_props, schema_props)
    assert stale == [
        "app:Thing.schema_only: 'Thing' is not enrolled under 'app'",
        "core:Other.anything: 'Other' is not enrolled under 'core'",
        "core:Thing.schema_only (interface_only): now diverges as schema_only; "
        "re-examine the entry",
        "core:Thing.shared (interface_only): no longer diverges; remove the entry",
    ]


@pytest.mark.parametrize("keyword", ["$ref", "allOf", "anyOf", "oneOf"])
def test_component_with_composed_shape_is_refused(keyword: str) -> None:
    """F4: a composed component is refused rather than read as its inline part."""
    composition = "#/components/schemas/Y" if keyword == "$ref" else [{"type": "object"}]
    spec = {"components": {"schemas": {"X": {keyword: composition, "properties": {"a": {}}}}}}
    with pytest.raises(ValueError, match="composes"):
        component_properties(spec, "X")
    with pytest.raises(ValueError, match="not published"):
        component_properties(spec, "Missing")


def test_allowlist_exempts_a_property_only_on_the_interface_it_names() -> None:
    """F1/F2: an entry exempts its property only on the pair and side it names."""
    allowlist: dict[tuple[Spec, str, str, Side], str] = {
        ("core", "Document", "frontend_field", "interface_only"): "reason",
        ("core", "Document", "schema_field", "schema_only"): "reason",
    }
    assert allowlisted("core", "Document", "interface_only", allowlist) == {"frontend_field"}
    assert allowlisted("core", "Document", "schema_only", allowlist) == {"schema_field"}
    assert allowlisted("core", "DocumentSummary", "interface_only", allowlist) == set()
    assert allowlisted("app", "Document", "schema_only", allowlist) == set()


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


def test_reader_ignores_comments_inside_a_body() -> None:
    """P1."""
    source = """
    export interface X {
      // bogus: string;
      a: string; /* hidden: number; */
      b: number;
    }
    """
    assert set(read_interface(source, "X")) == {"a", "b"}


def test_reader_is_indifferent_to_layout() -> None:
    """P2."""
    assert read_interface("export interface X { a: string; b?: number }", "X") == {
        "a": False,
        "b": True,
    }


def test_reader_does_not_collect_members_of_nested_type_literals() -> None:
    """P3."""
    source = "interface X { meta: { inner: string; deeper: { x: number } }; after: string; }"
    assert set(read_interface(source, "X")) == {"meta", "after"}


def test_reader_tracks_depth_through_arrows_generics_and_unions() -> None:
    """P4."""
    source = """
    interface X {
      cb: (x: number) => void;
      r: Record<string, Array<Map<string, unknown>>>;
      u:
        | 'a'
        | 'b';
      last: string;
    }
    """
    assert set(read_interface(source, "X")) == {"cb", "r", "u", "last"}


def test_reader_collects_quoted_and_readonly_members() -> None:
    """P5."""
    source = "interface X { 'weird-key': string; readonly z: string; readonly: boolean }"
    assert set(read_interface(source, "X")) == {"weird-key", "z", "readonly"}


def test_reader_resolves_a_heritage_clause() -> None:
    """P6: inherited members are collected with the interface's own, optional flags intact."""
    source = """
    interface B { b?: string }
    interface C { c: boolean }
    interface A extends B { a: number }
    export interface Multi extends B, C { m: string }
    interface Generic<T> extends C { g: T }
    """
    assert read_interface(source, "A") == {"b": True, "a": False}
    assert read_interface(source, "Multi") == {"b": True, "c": False, "m": False}
    assert read_interface(source, "Generic") == {"c": False, "g": False}


def test_reader_collects_member_type_identifiers() -> None:
    """P11: identifiers of each member's type are collected, and nothing else is."""
    source = """
    interface X {
      a: Array<Edge> | null;
      b: 'Edge';
      c: EdgeWarning /* Edge */ // Edge
        ,
      d: { inner: Edge };
      last: string
    }
    """
    assert read_interface_types(source, "X") == {
        "a": frozenset({"Array", "Edge", "null"}),
        "b": frozenset(),
        "c": frozenset({"EdgeWarning"}),
        "d": frozenset({"inner", "Edge"}),
        "last": frozenset({"string"}),
    }


def test_reader_lists_only_exported_interface_declarations() -> None:
    """P13: exports are read as tokens; comments, strings and private interfaces do not count."""
    source = """
    // export interface Commented { a: string }
    const s = "export interface Quoted { a: string }";
    interface Private { a: string }
    export interface Real { a: string }
    export type Alias = { a: string };
    """
    assert exported_interfaces(source) == {"Real"}


def test_reader_carries_type_identifiers_through_heritage() -> None:
    """P12: an inherited member keeps the identifiers of its declared type."""
    source = "interface B { e: Edge | null }\ninterface A extends B { own?: string }"
    assert read_interface_types(source, "A") == {
        "e": frozenset({"Edge", "null"}),
        "own": frozenset({"string"}),
    }
    assert read_interface(source, "A") == {"e": False, "own": True}


@pytest.mark.parametrize(
    ("source", "diagnostic"),
    [
        ("interface B<T> { b: T }\ninterface X extends B<string> { a: string }", "not a bare"),
        ("interface X extends ns.B { a: string }", "not a bare"),
        ("interface X extends { a: string }", "not a bare"),
        ("interface X extends Absent { a: string }", "not an interface declared"),
        (
            "type B = { b: string }\ninterface X extends B { a: string }",
            "not an interface declared",
        ),
        ("interface X extends B { a: string }\ninterface B extends X { b: string }", "cyclic"),
        ("interface B { a: string }\ninterface X extends B { a: string }", "redeclares"),
        (
            "interface B { a: string }\ninterface C { a: string }\ninterface X extends B, C {}",
            "redeclares",
        ),
    ],
    ids=[
        "generic-base",
        "qualified-base",
        "empty-clause",
        "absent-base",
        "type-alias-base",
        "cycle",
        "redeclared-in-derived",
        "redeclared-across-bases",
    ],
)
def test_reader_refuses_heritage_it_does_not_model(source: str, diagnostic: str) -> None:
    """P10: each refusal names its own cause."""
    with pytest.raises(UnsupportedDeclarationError, match=diagnostic):
        read_interface(source, "X")


@pytest.mark.parametrize(
    ("body", "diagnostic"),
    [
        ("[k: string]: unknown;", "index signature"),
        ("f(): void;", "not a property signature"),
        ("g<T>(x: T): T;", "not a property signature"),
        ("a: string\n  b: number", "not delimited"),
        ("a: T extends U ? A : B;\n  b: string;", "conditional type"),
    ],
    ids=["index-signature", "method", "generic-method", "undelimited-member", "conditional-type"],
)
def test_reader_refuses_members_it_does_not_model(body: str, diagnostic: str) -> None:
    """P7: each refusal names its own cause."""
    with pytest.raises(UnsupportedDeclarationError, match=diagnostic):
        read_interface(f"interface X {{\n  {body}\n}}", "X")


def test_reader_finds_declarations_only_outside_comments_and_strings() -> None:
    """P8."""
    source = """
    // interface X { commented: string }
    const s = "interface X { quoted: string }";
    interface X { real: string }
    """
    assert set(read_interface(source, "X")) == {"real"}


def test_reader_refuses_duplicates_and_names_an_absent_interface() -> None:
    """P9."""
    with pytest.raises(UnsupportedDeclarationError, match="2 times"):
        read_interface("interface X { a: string }\ninterface X { b: string }", "X")
    with pytest.raises(KeyError, match="Absent"):
        read_interface("interface X { a: string }", "Absent")
