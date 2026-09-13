"""The frontend document interfaces declare the published component schemas.

``app/src/api/types.ts`` hand-maintains TypeScript mirrors of the response
shapes the SAGE Core API publishes. The spec-versus-code gates elsewhere in this
suite hold the Pydantic models to ``sage_core_api.openapi.yaml`` and stop at the
Python boundary; nothing else reads the TypeScript file, so a property added to
a schema reaches the frontend only if whoever added it remembered to.

This module reads the component schemas, not the Pydantic models, so it sits on
the same authority those gates do and needs no opinion about which Python class
backs which interface.

Authority
---------

The formal substrate is authoritative (CAS-ADR-008). A property the schema
declares and the interface omits is drift, and the fix is to declare it. A
property the interface declares and the schema omits is a frontend-only field:
nothing on the wire supplies it, so it needs an allowlist entry naming why it
exists, or it should not exist.

Invariants
----------

F1  Every property of an enrolled component schema is declared on its interface.
F2  An enrolled interface declares no property its component schema omits.
F3  Every allowlist entry names an enrolled interface and a property that still
    diverges on the side the entry names.
F4  Every enrolled interface and component resolves, and enough properties are
    compared to mean something (vacuity floor).
F5  The comparison fires on a removed declaration, and the staleness check
    fires on a stale entry.

Only property names are compared. Whether a property is optional, nullable, or
correctly typed on the TypeScript side is not asserted here.

F3 is inert by construction while the allowlist is empty: its loop runs zero
times and passes against any implementation. The staleness function it calls is
exercised with a non-empty allowlist by F5, which is where its teeth are.

The reader
----------

The interfaces are read by a token-level declaration reader, not by matching
lines: comments, string literals and nested type literals are lexed as what they
are, so reformatting a declaration or annotating a member does not change what
is collected. The reader models plain property members only and **fails closed**
on anything else it meets inside an enrolled interface -- a heritage clause, an
index signature, a method signature, a member it cannot delimit -- because
collecting a subset of such a declaration would let F1 pass over members it never
saw. The P tests pin its behaviour on synthetic sources.
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

# TypeScript interface name -> SAGE Core component schema name.
ENROLLED: Final[dict[str, str]] = {
    "Document": "Document",
    "DocumentSummary": "DocumentSummary",
}

# Which side of a divergence carries the property: published by the schema only, or
# declared by the interface only.
Side = Literal["schema_only", "interface_only"]

# (interface, property, side) -> the reason the divergence is kept rather than closed.
# The side is part of the key so an entry justifying one direction cannot silence
# the other: a frontend-only field that later becomes schema-only is drift again.
# Empty by intent: an entry is an admission that the frontend and the published
# contract disagree about a document's shape, and should be justified in review.
KNOWN_FRONTEND_TYPE_DIVERGENCE: Final[dict[tuple[str, str, Side], str]] = {}

# Vacuity floor for F4. The enrolled pairs compare forty-three schema properties
# today; the floor sits below that so ordinary movement does not trip it, while a
# lookup returning nothing does.
MIN_PROPERTIES_COMPARED: Final[int] = 35


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

    The gate compares property names only; the optional flag is reported, not
    compared. Raises ``KeyError`` when no such interface is declared, and
    ``UnsupportedDeclarationError`` when it is declared more than once or uses
    syntax the reader does not model.
    """
    tokens = _lex(source)
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
    if j >= len(tokens) or not _is_punct(tokens[j], "{"):
        found = tokens[j][1] if j < len(tokens) else "end of source"
        raise UnsupportedDeclarationError(f"interface {name!r}: expected '{{', found {found!r}")
    return _read_members(tokens, j + 1, name)


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


def divergence(interface_props: set[str], schema_props: set[str]) -> tuple[set[str], set[str]]:
    """Return ``(schema_only, interface_only)`` property names."""
    return schema_props - interface_props, interface_props - schema_props


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
def interface_props(types_source: str) -> dict[str, set[str]]:
    return {interface: set(read_interface(types_source, interface)) for interface in ENROLLED}


@pytest.fixture(scope="module")
def schema_props(core_spec: dict) -> dict[str, set[str]]:
    return {
        interface: component_properties(core_spec, component)
        for interface, component in ENROLLED.items()
    }


def allowlisted(
    interface: str, side: Side, allowlist: dict[tuple[str, str, Side], str]
) -> set[str]:
    """The properties ``allowlist`` exempts on ``interface`` for one side of a divergence."""
    return {
        prop for (name, prop, entry_side) in allowlist if name == interface and entry_side == side
    }


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("interface", sorted(ENROLLED))
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
        f"{ENROLLED[interface]} publishes properties the TypeScript {interface} interface does not "
        f"declare: {missing}. The published schema is authoritative; "
        f"declare them in {TYPES_TS_PATH.name}."
    )


@pytest.mark.parametrize("interface", sorted(ENROLLED))
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
        f"The TypeScript {interface} interface declares properties {ENROLLED[interface]} does not "
        f"publish: {extra}. Remove them, or add a KNOWN_FRONTEND_TYPE_DIVERGENCE entry naming why "
        "a frontend-only field exists."
    )


def test_divergence_allowlist_has_no_stale_entries(
    interface_props: dict[str, set[str]],
    schema_props: dict[str, set[str]],
) -> None:
    """F3: every allowlist entry still names a live divergence."""
    stale = stale_entries(KNOWN_FRONTEND_TYPE_DIVERGENCE, interface_props, schema_props)
    assert not stale, "stale KNOWN_FRONTEND_TYPE_DIVERGENCE entries:\n" + "\n".join(stale)


def test_enrollment_resolves_and_is_not_vacuous(types_source: str, core_spec: dict) -> None:
    """F4: every enrolled pair resolves, and the comparison covers enough properties."""
    compared = 0
    for interface, component in ENROLLED.items():
        assert read_interface(types_source, interface), (
            f"interface {interface!r} declares no properties"
        )
        compared += len(component_properties(core_spec, component))
    assert compared >= MIN_PROPERTIES_COMPARED, (
        f"only {compared} schema properties compared; floor is {MIN_PROPERTIES_COMPARED}"
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


def test_reader_refuses_a_heritage_clause() -> None:
    """P6."""
    with pytest.raises(UnsupportedDeclarationError, match="expected"):
        read_interface("interface X extends Y { a: string }", "X")


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
