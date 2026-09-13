"""The frontend API interfaces declare the published component schemas.

``app/src/api/types.ts`` hand-maintains TypeScript mirrors of the shapes the
SAGE Core API publishes: the document responses the frontend reads and the
request bodies it sends. The spec-versus-code gates elsewhere in this suite hold
the Pydantic models to ``sage_core_api.openapi.yaml`` and stop at the Python
boundary; nothing else reads the TypeScript file, so a property added to a
schema reaches the frontend only if whoever added it remembered to.

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

Response interfaces are compared by name only: the REST document routes emit
every key, so a response schema's ``required`` list carries nothing the frontend
relies on. Whether a property is nullable or correctly typed on the TypeScript
side is not asserted for either kind.

Invariants
----------

F1  Every property of an enrolled component schema is declared on its interface.
F2  An enrolled interface declares no property its component schema omits.
F3  Every allowlist entry names an enrolled interface and a property that still
    diverges on the side the entry names.
F4  Every enrolled interface and component resolves, the response and request
    enrollments are disjoint, each per-interface gate iterates the whole of its
    enrollment, and enough properties are compared in each to mean something
    (vacuity floors).
F5  The comparison fires on a removed declaration, including one inherited
    through a heritage clause; the optionality check fires on a relaxed required
    member; and the staleness check fires on a stale entry.
F6  Every property a request component requires is non-optional on its interface.

The reader
----------

The interfaces are read by a token-level declaration reader, not by matching
lines: comments, string literals and nested type literals are lexed as what they
are, so reformatting a declaration or annotating a member does not change what
is collected. The reader models plain property members, and heritage clauses
whose bases are bare names of interfaces declared in the same source; inherited
members are collected with the interface's own. It **fails closed** on anything
else it meets in an enrolled interface -- an index signature, a method signature,
a member it cannot delimit, a generic or qualified base, a base it cannot find as
an interface, a cyclic clause, a member declared twice along the heritage chain
-- because collecting a subset of such a declaration would let F1 pass over
members it never saw. The P tests pin its behaviour on synthetic sources.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final, Literal

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
TYPES_TS_PATH = _REPO_ROOT / "app" / "src" / "api" / "types.ts"
SAGE_CORE_SPEC_PATH = _REPO_ROOT / "docs" / "fs" / "sage" / "sage_core_api.openapi.yaml"

# Response interfaces: TypeScript interface name -> SAGE Core component schema name.
ENROLLED: Final[dict[str, str]] = {
    "Document": "Document",
    "DocumentSummary": "DocumentSummary",
}

# Request interfaces, which F6 additionally holds to their components' required lists.
ENROLLED_REQUESTS: Final[dict[str, str]] = {
    "BulkLifecycleItem": "BulkLifecycleItem",
    "BulkLifecycleRequest": "BulkLifecycleRequest",
    "BulkLinkItem": "BulkLinkItem",
    "BulkLinkRequest": "BulkLinkRequest",
    "BulkMetadataItem": "BulkMetadataItem",
    "BulkMetadataRequest": "BulkMetadataRequest",
    "DiscoverRequest": "DiscoverRequest",
    "LinkRequest": "LinkRequest",
    "ListFieldPatch": "ListFieldPatch",
    "ReabstractRequest": "ReabstractRequest",
    "RelocationPointer": "RelocationPointer",
    "Tier3Patch": "Tier3Patch",
    "TraverseRequest": "TraverseRequest",
    "UpdateMetadataRequest": "UpdateMetadataRequest",
}

ALL_ENROLLED: Final[dict[str, str]] = {**ENROLLED, **ENROLLED_REQUESTS}

# Which side of a divergence carries the property: published by the schema only, or
# declared by the interface only.
Side = Literal["schema_only", "interface_only"]

_DOC_ID_ALIAS_REASON: Final[str] = (
    "back-compatible alias for document_id; the component accepts exactly one of the "
    "two per item, so declaring both would type-permit a body the server refuses"
)

# (interface, property, side) -> the reason the divergence is kept rather than closed.
# The side is part of the key so an entry justifying one direction cannot silence
# the other: a frontend-only field that later becomes schema-only is drift again.
# An entry is an admission that the frontend and the published contract disagree
# about a shape, and should be justified in review.
KNOWN_FRONTEND_TYPE_DIVERGENCE: Final[dict[tuple[str, str, Side], str]] = {
    ("BulkLifecycleItem", "doc_id", "schema_only"): _DOC_ID_ALIAS_REASON,
    ("BulkMetadataItem", "doc_id", "schema_only"): _DOC_ID_ALIAS_REASON,
}

# Vacuity floors for F4, each set below today's count so ordinary movement does not
# trip it while a lookup returning nothing does. The response pairs compare
# forty-three schema properties, the request pairs ninety-one, and the request
# components require thirteen.
MIN_PROPERTIES_COMPARED: Final[int] = 35
MIN_REQUEST_PROPERTIES_COMPARED: Final[int] = 70
MIN_REQUIRED_PROPERTIES_COMPARED: Final[int] = 7


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


def read_interface(source: str, name: str) -> dict[str, bool]:
    """Return the property members of interface ``name`` as ``{property: optional}``.

    Members inherited through a heritage clause are included. Raises ``KeyError``
    when no such interface is declared, and ``UnsupportedDeclarationError`` when it
    is declared more than once or uses syntax the reader does not model.
    """
    return _read_interface(_lex(source), name, ())


def _read_interface(
    tokens: list[tuple[str, str]], name: str, seen: tuple[str, ...]
) -> dict[str, bool]:
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

    members: dict[str, bool] = {}
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
    members: dict[str, bool], incoming: dict[str, bool], name: str, origin: str
) -> None:
    """Add ``incoming`` to ``members``, refusing a member already collected along the chain."""
    for member, optional in incoming.items():
        if member in members:
            raise UnsupportedDeclarationError(
                f"interface {name!r}: {origin!r} redeclares member {member!r}"
            )
        members[member] = optional


def _read_members(tokens: list[tuple[str, str]], j: int, name: str) -> dict[str, bool]:
    """Collect depth-one property members of a body whose ``{`` precedes index ``j``."""
    members: dict[str, bool] = {}
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
            members[member] = _is_punct(tokens[colon - 1], "?")
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
        if kind == "punct" and text in _OPENERS:
            depth += 1
        elif kind == "punct" and text in _CLOSERS:
            depth -= 1
            if depth == 0:
                if text != "}":
                    raise UnsupportedDeclarationError(f"interface {name!r}: unbalanced {text!r}")
                return members
        elif depth == 1 and (_is_punct(token, ";") or _is_punct(token, ",")):
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
    allowlist: dict[tuple[str, str, Side], str],
    interface_props: dict[str, set[str]],
    schema_props: dict[str, set[str]],
) -> list[str]:
    """Allowlist entries that name no enrolled interface or no longer diverge on their side."""
    stale: list[str] = []
    for interface, prop, side in sorted(allowlist):
        if interface not in interface_props or interface not in schema_props:
            stale.append(f"{interface}.{prop}: {interface!r} is not enrolled")
            continue
        schema_only, interface_only = divergence(
            interface_props[interface], schema_props[interface]
        )
        sides: dict[Side, set[str]] = {"schema_only": schema_only, "interface_only": interface_only}
        if prop in sides[side]:
            continue
        other: Side = "interface_only" if side == "schema_only" else "schema_only"
        if prop in sides[other]:
            stale.append(
                f"{interface}.{prop} ({side}): now diverges as {other}; re-examine the entry"
            )
        else:
            stale.append(f"{interface}.{prop} ({side}): no longer diverges; remove the entry")
    return stale


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def types_source() -> str:
    return TYPES_TS_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def core_spec() -> dict:
    with SAGE_CORE_SPEC_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def interface_members(types_source: str) -> dict[str, dict[str, bool]]:
    return {interface: read_interface(types_source, interface) for interface in ALL_ENROLLED}


@pytest.fixture(scope="module")
def interface_props(interface_members: dict[str, dict[str, bool]]) -> dict[str, set[str]]:
    return {interface: set(members) for interface, members in interface_members.items()}


@pytest.fixture(scope="module")
def schema_props(core_spec: dict) -> dict[str, set[str]]:
    return {
        interface: component_properties(core_spec, component)
        for interface, component in ALL_ENROLLED.items()
    }


def allowlisted(
    interface: str, side: Side, allowlist: dict[tuple[str, str, Side], str]
) -> set[str]:
    """The properties ``allowlist`` exempts on ``interface`` for one side of a divergence."""
    return {
        prop for (name, prop, entry_side) in allowlist if name == interface and entry_side == side
    }


def unexempted_schema_only(source: str, spec: dict) -> dict[str, set[str]]:
    """F1's findings over ``source``: each enrolled interface with a non-exempt omission."""
    findings: dict[str, set[str]] = {}
    for interface, component in ALL_ENROLLED.items():
        schema_only, _ = divergence(
            set(read_interface(source, interface)), component_properties(spec, component)
        )
        missing = schema_only - allowlisted(
            interface, "schema_only", KNOWN_FRONTEND_TYPE_DIVERGENCE
        )
        if missing:
            findings[interface] = missing
    return findings


def relaxed_request_members(source: str, spec: dict) -> dict[str, set[str]]:
    """F6's findings over ``source``: each enrolled request with a relaxed required member."""
    findings: dict[str, set[str]] = {}
    for interface, component in ENROLLED_REQUESTS.items():
        relaxed = relaxed_required(
            read_interface(source, interface), component_required(spec, component)
        )
        if relaxed:
            findings[interface] = relaxed
    return findings


def rewrite_member(source: str, interface: str, member: str, replacement: str) -> str:
    """Rewrite the one declaration head of ``member`` in the body of exported ``interface``.

    The head is the member's name, optional marker and colon; ``replacement`` is
    substituted for it. An empty ``replacement`` removes a declaration that ends on its
    own line, and refuses a member whose type continues onto further lines.
    """
    body = re.search(
        rf"^export interface {re.escape(interface)}\b[^\n]*\{{\n(?P<body>.*?)^\}}",
        source,
        re.MULTILINE | re.DOTALL,
    )
    assert body is not None, f"interface {interface!r} body not found"
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


@pytest.mark.parametrize("interface", sorted(ALL_ENROLLED))
def test_schema_properties_are_declared_on_the_interface(
    interface: str,
    interface_props: dict[str, set[str]],
    schema_props: dict[str, set[str]],
) -> None:
    """F1: a property the published schema declares is declared on the interface."""
    schema_only, _ = divergence(interface_props[interface], schema_props[interface])
    missing = sorted(
        schema_only - allowlisted(interface, "schema_only", KNOWN_FRONTEND_TYPE_DIVERGENCE)
    )
    assert not missing, (
        f"{ALL_ENROLLED[interface]} publishes properties the TypeScript {interface} interface "
        f"does not declare: {missing}. The published schema is authoritative; "
        f"declare them in {TYPES_TS_PATH.name}."
    )


@pytest.mark.parametrize("interface", sorted(ALL_ENROLLED))
def test_interface_declares_no_property_absent_from_the_schema(
    interface: str,
    interface_props: dict[str, set[str]],
    schema_props: dict[str, set[str]],
) -> None:
    """F2: a property only the interface declares is not supplied by the wire."""
    _, interface_only = divergence(interface_props[interface], schema_props[interface])
    extra = sorted(
        interface_only - allowlisted(interface, "interface_only", KNOWN_FRONTEND_TYPE_DIVERGENCE)
    )
    assert not extra, (
        f"The TypeScript {interface} interface declares properties {ALL_ENROLLED[interface]} "
        f"does not publish: {extra}. Remove them, or add a KNOWN_FRONTEND_TYPE_DIVERGENCE entry "
        "naming why a frontend-only field exists."
    )


def test_divergence_allowlist_has_no_stale_entries(
    interface_props: dict[str, set[str]],
    schema_props: dict[str, set[str]],
) -> None:
    """F3: every allowlist entry still names a live divergence."""
    stale = stale_entries(KNOWN_FRONTEND_TYPE_DIVERGENCE, interface_props, schema_props)
    assert not stale, "stale KNOWN_FRONTEND_TYPE_DIVERGENCE entries:\n" + "\n".join(stale)


@pytest.mark.parametrize("interface", sorted(ENROLLED_REQUESTS))
def test_required_request_properties_are_not_optional_on_the_interface(
    interface: str,
    interface_members: dict[str, dict[str, bool]],
    core_spec: dict,
) -> None:
    """F6: a property the request component requires is non-optional on the interface."""
    relaxed = sorted(
        relaxed_required(
            interface_members[interface],
            component_required(core_spec, ENROLLED_REQUESTS[interface]),
        )
    )
    assert not relaxed, (
        f"{ENROLLED_REQUESTS[interface]} requires properties the TypeScript {interface} interface "
        f"marks optional: {relaxed}. The server refuses a body without them; "
        f"remove the '?' in {TYPES_TS_PATH.name}."
    )


@pytest.mark.parametrize(
    ("gate", "enrollment"),
    [
        (test_schema_properties_are_declared_on_the_interface, ALL_ENROLLED),
        (test_interface_declares_no_property_absent_from_the_schema, ALL_ENROLLED),
        (test_required_request_properties_are_not_optional_on_the_interface, ENROLLED_REQUESTS),
    ],
    ids=["F1", "F2", "F6"],
)
def test_gates_are_parametrized_over_their_enrollment(
    gate: object, enrollment: dict[str, str]
) -> None:
    """F4: each per-interface gate iterates the whole of the enrollment it enforces.

    The F5 probes exercise the comparison helpers, not the parametrized gates, so a
    gate narrowed back to a subset of its enrollment would leave them green.
    """
    marks = [mark for mark in getattr(gate, "pytestmark", []) if mark.name == "parametrize"]
    assert len(marks) == 1, (
        f"{getattr(gate, '__name__', gate)} carries {len(marks)} parametrizations"
    )
    assert marks[0].args == ("interface", sorted(enrollment))


def test_enrollment_resolves_and_is_not_vacuous(types_source: str, core_spec: dict) -> None:
    """F4: every enrolled pair resolves, and each kind compares enough properties."""
    overlap = sorted(set(ENROLLED) & set(ENROLLED_REQUESTS))
    assert not overlap, f"interfaces enrolled as both response and request: {overlap}"
    floors = (
        ("response", ENROLLED, MIN_PROPERTIES_COMPARED),
        ("request", ENROLLED_REQUESTS, MIN_REQUEST_PROPERTIES_COMPARED),
    )
    for kind, enrolled, floor in floors:
        compared = 0
        for interface, component in enrolled.items():
            assert read_interface(types_source, interface), (
                f"interface {interface!r} declares no properties"
            )
            compared += len(component_properties(core_spec, component))
        assert compared >= floor, (
            f"only {compared} {kind} schema properties compared; floor is {floor}"
        )
    required = sum(
        len(component_required(core_spec, component)) for component in ENROLLED_REQUESTS.values()
    )
    assert required >= MIN_REQUIRED_PROPERTIES_COMPARED, (
        f"only {required} required request properties compared; "
        f"floor is {MIN_REQUIRED_PROPERTIES_COMPARED}"
    )


def test_gate_fires_when_a_declared_property_is_removed(types_source: str, core_spec: dict) -> None:
    """F5: removing a declaration from the real file surfaces exactly that property."""
    declaration = re.compile(r"^[ \t]*stored_content_hash\??[ \t]*:[^\n]*\n", re.MULTILINE)
    mutated, removed = declaration.subn("", types_source)
    assert removed == 1, "mutation target is not declared exactly once"
    assert mutated != types_source

    document_only, _ = divergence(
        set(read_interface(mutated, "Document")), component_properties(core_spec, "Document")
    )
    summary_only, _ = divergence(
        set(read_interface(mutated, "DocumentSummary")),
        component_properties(core_spec, "DocumentSummary"),
    )
    assert document_only == {"stored_content_hash"}
    assert summary_only == set()


def test_gate_fires_when_a_request_declaration_is_removed(
    types_source: str, core_spec: dict
) -> None:
    """F5: removing one request member surfaces exactly that member, on that interface only."""
    assert unexempted_schema_only(types_source, core_spec) == {}
    mutated = rewrite_member(types_source, "DiscoverRequest", "facet_value_limit", "")
    assert unexempted_schema_only(mutated, core_spec) == {"DiscoverRequest": {"facet_value_limit"}}


def test_gate_sees_a_base_member_removed_through_heritage(
    types_source: str, core_spec: dict
) -> None:
    """F5: a member removed from a base is missing from the interface that inherits it."""
    assert unexempted_schema_only(types_source, core_spec) == {}
    mutated = rewrite_member(types_source, "BulkLinkItem", "rationale_kind", "")
    assert unexempted_schema_only(mutated, core_spec) == {
        "BulkLinkItem": {"rationale_kind"},
        "LinkRequest": {"rationale_kind"},
    }


def test_optionality_check_fires_when_a_required_member_is_relaxed(
    types_source: str, core_spec: dict
) -> None:
    """F5: marking an inherited required member optional is reported wherever it is inherited."""
    assert relaxed_request_members(types_source, core_spec) == {}
    mutated = rewrite_member(types_source, "BulkLinkItem", "edge_type", "edge_type?:")
    assert relaxed_request_members(mutated, core_spec) == {
        "BulkLinkItem": {"edge_type"},
        "LinkRequest": {"edge_type"},
    }


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
    schema_props = {"Thing": {"shared", "schema_only"}}
    allowlist: dict[tuple[str, str, Side], str] = {
        ("Thing", "frontend_only", "interface_only"): "live",
        ("Thing", "schema_only", "schema_only"): "live",
        ("Thing", "shared", "interface_only"): "stale",
        ("Thing", "schema_only", "interface_only"): "diverges, but on the other side",
        ("Other", "anything", "schema_only"): "unenrolled",
    }
    stale = stale_entries(allowlist, interface_props, schema_props)
    assert stale == [
        "Other.anything: 'Other' is not enrolled",
        "Thing.schema_only (interface_only): now diverges as schema_only; re-examine the entry",
        "Thing.shared (interface_only): no longer diverges; remove the entry",
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
    """F1/F2: an entry exempts its property only on the interface and side it names."""
    allowlist: dict[tuple[str, str, Side], str] = {
        ("Document", "frontend_field", "interface_only"): "reason",
        ("Document", "schema_field", "schema_only"): "reason",
    }
    assert allowlisted("Document", "interface_only", allowlist) == {"frontend_field"}
    assert allowlisted("Document", "schema_only", allowlist) == {"schema_field"}
    assert allowlisted("DocumentSummary", "interface_only", allowlist) == set()


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
