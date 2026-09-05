"""Caller-facing prose against the contract vocabulary it draws on.

Sibling gates compare a surface against another surface.
``test_mcp_tool_conformance`` compares parameter shape,
``test_openapi_conformance`` compares Pydantic field descriptions against
YAML property descriptions, and ``test_mcp_docstring_disclosure_parity``
compares the two prose surfaces of a mapped pair to each other. None of
them asks whether a name the prose uses exists at all. This module does.

The defect it was built for: both caller-facing narratives of ``chain``
documented a response field ``linearity`` and described it as
three-valued, where the response model and the schema declare
``is_linear``, a boolean. A caller who followed either narrative read
``resp["linearity"]``, got ``None``, and concluded a linear chain was
non-linear -- a wrong answer with no error anywhere. The parity gate
could not see it, and says so in its own header: it compares two
disclosures to each other, so prose that is wrong the same way on both
surfaces passes clean.

**The relation.** Every identifier a narrative names must appear
somewhere in the contract the operation belongs to. The vocabulary is
assembled from the machine-readable definitions rather than
hand-maintained, in two tiers:

- The **operation's own** vocabulary: its parameter names, its request
  schema's properties, its 2xx response schemas' properties, and every
  enum value reachable from those. All ``$ref``s are followed
  transitively. Its ``responses[*].description`` is pointedly *not* read
  here -- see the error-code tier below.
- The **contract-wide** vocabulary: every operationId and MCP tool name
  on either surface, every MCP tool argument name, every property and
  enum value declared anywhere in either spec's ``components``, every
  vault-config property name reachable from ``vault_config.schema.json``,
  every error code the exception hierarchy raises, and the base lifecycle
  states and actions.

A name in neither is unresolved, and must carry a pin in
``UNRESOLVED_IDENTIFIERS`` naming what it is instead.

**What this answers, and what it does not.** The relation is *is this
name part of the contract at all*, not *is this name a response field of
this operation*. The narrower question is the one worth asking and the
one that cannot be asked mechanically: whether an identifier is being
offered as a response field is a fact about the sentence around it, not
about the identifier, and prose says it too many ways to key on --
"returns", "carries", "a `X` flag", "`X` is true when". Every attempt to
infer the frame either misses the plain forms or claims request
parameters and error codes as response fields. So the gate asks the
question it can answer exactly, which is enough: ``linearity`` is in no
vocabulary, and neither is any other invented name.

The same boundary makes the type-and-range half of the audit a human
read. That a narrative calls a field three-valued where the schema
declares a boolean is not recoverable from either surface's tokens.

**Three further limits, all by construction.** A name that resolves in
the contract-wide tier but not the operation's own is accepted, so prose
that names a real field belonging to a *different* operation passes.
Narrowing that would mean rejecting every legitimate cross-reference --
sibling tool names, shared error codes, config keys -- which is most of
what the narratives correctly say. A name is checked for existence, not
for being current: a field that was removed from the contract is caught,
one that was renamed while the old spelling survives elsewhere is not.

**The error-code tier**, closed in a pass of its own after the rest of
this module landed. It had been outside the relation in both directions
at once. The swept surfaces excluded where codes are named:
``_surfaces_for`` takes summary and description only, and ``_prose_body``
truncates the docstring at its first structural header, so the whole
``Error modes:`` block went unread -- on ``chain``, 833 of 2,562
characters, and where ``document_not_found`` and ``invalid_document_id``
live. Meanwhile ``operation_vocabulary`` folded every snake_case token in
``responses[*].description`` *into* the vocabulary rather than checking
it against the raised codes, so a code invented in a response
description validated itself, and one both surfaces agreed on -- which
the parity gate actively drives them toward -- passed every gate in this
repository. The codes were reached only when a narrative body named one
inline.

Both halves are now read as prose: ``unresolved_for`` sweeps four
surfaces rather than two, adding ``_structural_tail`` (the exact
complement of ``_prose_body``, sharing its header pattern) and
``_response_prose``. That is an inversion on the response side, not a
new reader. The defect it was closed for was real and had already
happened: a replacement written into ``migrate_vault``'s ``Error modes:``
block invented ``tier3_unique_index_blocked``, a code no caller can ever
see. It was caught by reading, because nothing mechanical could.
``test_the_fabricated_error_code_is_reported`` pins it.

Closing the tier also reached back into ``_error_codes()`` itself, whose
envelope reader was scoped to a single module and so missed both codes
``sage/app_tools.py`` emits. ``invalid_directory``, named on both
``list_directory`` narratives, had been reading as an invented name for
that reason alone -- the reader's own defect class, one level up. It now
runs four constructions over every package file, and the
``missing_{field}`` family is synthesized from its call sites. One family
stays genuinely out of reach and is pinned rather than swept; that
function's docstring says which and why.

What the pass measured is the residue below: thirty-five names, and
overwhelmingly one category. ``SAGEError.detail`` is an untyped mapping,
so a narrative telling a caller which keys to read names something real
that no contract node declares. Typing the detail envelope per error
family would resolve every one of them at once, which is the finding
those pins record.

**The extractor is not the sibling gate's.** The parity module's
``_IDENTIFIER`` requires an underscore, and ``linearity`` has none.
Extraction here is three tiers -- snake_case, single-word backticked
tokens, and interior-capital names -- which together reach the defect
this module exists for and the shape an invented *schema* name arrives
as. All three are pinned by
``test_extractor_finds_bare_backticked_tokens``. An extractor that cannot
see a shape silently bounds every enumeration built from it, the one
below included -- this module's own defect class, one level up, and the
reason the tiers are pinned by a test rather than left to inspection.

**Pins are a ratchet**, on the rule the parity gate established:
``test_unresolved_identifier_pins_are_not_stale`` fails on a pinned name
that has since become resolvable, so the pin set can only shrink. Each
pin carries the category the name actually belongs to. The categories are
the audit's record: ten mapped pairs resolve completely and appear
nowhere below, and an operation absent from the table is one the sweep
found sound rather than one it skipped.

**Implementation-axis findings, recorded here for the next pass.** The
schema axis above is mechanical; claims about *what the code does* are
not, and were checked by reading the implementing service beside the
prose. The pass was bounded to the operations whose descriptions make a
checkable assertion -- a cost or round-trip claim, or an enumeration of a
closed vocabulary -- which is fifteen of the forty-eight operations
across both specs. Descriptions that only describe behavior in ordinary
terms carry nothing a reader could falsify and were not read against
code. What the pass found, all on ``chain`` and all now corrected:

- The cost claim covered the walk's two statements and omitted the
  existence check preceding them, and the two further edge queries a
  one-document chain makes to fill ``available_edge_types``. Three
  round-trips for a chain of two or more, four for a chain of one --
  which spends the hint's two queries but skips the edge query, the
  storage walk returning early when the CTE yields a single row --
  against a sentence that read as two.
- ``length`` was documented as the chain total on both surfaces and in
  both schemas, where the service assigns it after slicing. A caller
  paging with ``limit`` read the page size as the total, with no error --
  the same failure shape as ``linearity``.
- ``chain`` claimed the head sits at ``position length-1``. Positions are
  assigned over the full ordered chain before slicing, so a page taken at
  an offset keeps absolute positions and the claim is false for it.

One further cost claim was stale rather than wrong when written:
``recompute_deferred_vault_abstracts`` anchored its per-document
wall-clock to a model the committed configuration no longer names. It now
says a local MLX model, which config chooses and prose need not track.

The pass also confirmed the enumerations it read. ``StalenessBasis`` does
carry the four buckets ``verify_vault_drift`` claims; the four
``integrity_status`` values ``verify_vault_source_files`` names are the
four the schema declares; and the three terminal statuses
``recompute_deferred_vault_abstracts`` says it polls for are exactly the
set its poller waits on. Those operations, and the rest of the fifteen,
are sound on this axis.

Seven operations named internal implementation identifiers in prose
whose readers cannot reach them. Five had them reworded as telling the
caller nothing -- nine identifiers, one of which appears on two of those
operations: a service class path, a summary dataclass and a parser class
on ``bulk_ingest_document``, two internal function names on
``batch_ingest_documents``, a service method and a provider class on
``recompute_deferred_vault_abstracts``, that same parser class on
``get_filename_metadata``, and a store class and an exception class on
``migrate_vault``. The remaining two carry pins instead, three names
between them: ``create_vault`` names a Python constructor and its class
for a default config, and no API route returns one, so the reference is
the only affordance a caller has; and ``Sha256Str`` on ``verify_hashes``,
because there the alias *is* the published shape.

Instances remain on surfaces this module does not sweep -- schema
property descriptions carry a few, and one was corrected here only
because this work introduced it. Sweeping that surface is a separate
pass; it is now the only such surface left, the error-code tier having
been closed since.

Widening the sweep to the structural tail surfaced three more, on
surfaces no gate had read: a service class and its dependency in an
``Error modes:`` bullet describing a wiring failure a caller cannot act
on, and a module path plus a threshold constant in an ``Args:`` block.
All three were reworded rather than pinned, on both surfaces together --
the constant is named in the spec as well, and prose that moves on one
surface only diverges under the parity gate.
"""

from __future__ import annotations

import ast
import functools
import inspect
import json
import re
from pathlib import Path
from typing import Any, Final

import pytest

from tests.sage.test_mcp_docstring_disclosure_parity import (
    _STRUCTURAL_HEADER,
    _prose_body,
    _surfaces_for,
)
from tests.sage.test_mcp_tool_conformance import (
    _SURFACES_BY_NAME,
    _all_operation_ids,
    _all_registered_tools,
    _find_operation,
    _load_spec,
    _mapped_tool_pairs,
    _resolve_expected_operation_id,
    _surface_registry,
)

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_SAGE_PACKAGE: Final[Path] = _REPO_ROOT / "sage"
_SUBSTRATE: Final[Path] = _REPO_ROOT / "docs" / "fs" / "sage"
_VAULT_CONFIG_SCHEMA: Final[str] = "vault_config.schema.json"

# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

# An underscore-bearing lowercase token: the shape most contract
# identifiers take, and the shape the sibling parity gate keys on.
_SNAKE_CASE: Final[re.Pattern[str]] = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")

_BACKTICKED: Final[re.Pattern[str]] = re.compile(r"`{1,2}([^`]+)`{1,2}")

# A single lowercase word with no underscore. Extracted only when it is
# backtick-marked, because unmarked it is indistinguishable from prose.
# This is the half that reaches the defect this module was built for:
# ``linearity`` carries no underscore, so a snake_case-only extractor
# never sees it.
_BARE_WORD: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9]*")

# An interior-capital name: the shape a schema, model, or class takes.
# Requires a lower-then-upper transition, so it matches ``ChainResponse``
# and ``Sha256Str`` but not a sentence-initial ordinary word, an
# acronym, or a heading. Without this tier the two lowercase patterns
# above see nothing at all in "Returns a `ChainResult` object", which is
# the invented-name defect in the one shape most likely to carry it.
_CAMEL_CASE: Final[re.Pattern[str]] = re.compile(
    r"\b[A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*(?:[A-Z][A-Za-z0-9]*)+\b"
)


def named_identifiers(text: str) -> frozenset[str]:
    """Every identifier a narrative names.

    Three tiers: snake_case tokens anywhere in the text, single-word
    lowercase tokens that are backtick-marked, and interior-capital
    names anywhere. Marked-up single words are included because that is
    how a narrative points at a field whose name happens to be one word;
    unmarked ones are not, because at that shape they are ordinary
    prose. Interior-capital names need no markup to be unambiguous --
    ordinary prose does not produce them -- so unlike the bare-word tier
    they are read from the running text as well.
    """
    found = set(_SNAKE_CASE.findall(text))
    found |= set(_CAMEL_CASE.findall(text))
    for token in _BACKTICKED.findall(text):
        candidate = token.strip()
        if _BARE_WORD.fullmatch(candidate):
            found.add(candidate)
    return frozenset(found)


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


def _collect(node: Any, key: str, out: set[str]) -> None:
    """Accumulate every ``properties`` name or ``enum`` value under a node."""
    if isinstance(node, dict):
        if key == "properties" and isinstance(node.get("properties"), dict):
            out.update(node["properties"])
        if key == "enum" and isinstance(node.get("enum"), list):
            out.update(v for v in node["enum"] if isinstance(v, str))
        for value in node.values():
            _collect(value, key, out)
    elif isinstance(node, list):
        for value in node:
            _collect(value, key, out)


def _vault_config_names(
    filename: str = _VAULT_CONFIG_SCHEMA, seen: set[str] | None = None
) -> set[str]:
    """Property names reachable from the vault-config JSON Schema.

    Follows sibling-file ``$ref``s, because the sections narratives name
    most often -- ``filename_extraction``, ``tier_assignments`` -- are
    declared one file down rather than in the root schema.
    """
    seen = set() if seen is None else seen
    names: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("properties"), dict):
                names.update(node["properties"])
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.endswith(".json") and ref not in seen:
                seen.add(ref)
                names.update(_vault_config_names(ref, seen))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(json.loads((_SUBSTRATE / filename).read_text()))
    return names


def _error_codes() -> set[str]:
    """Every machine-readable error code the package can emit.

    Read from source rather than by import: a ``SAGEError`` subclass
    passes its code as the first positional argument to
    ``super().__init__``, so the code is bound at instantiation and
    reflecting over the classes yields nothing. Anchoring on that call
    also avoids the over-match a plain text scan makes, where an
    exception's *argument* looks exactly like a code literal.

    Four readers, each anchored on a construction the package actually
    uses: the ``super().__init__`` call above; a direct ``SAGEError`` or
    ``PydanticCustomError``; the ``{"error": "<code>"}`` envelope built at
    a tool boundary without an exception; and the ``missing_{field}``
    family, whose literal appears nowhere because ``MissingFieldError``
    builds the code from its argument. All four run over every package
    file. Scoping the envelope reader to a single module -- which it was,
    and which cost it both codes ``sage/app_tools.py`` emits -- is the
    failure mode a reader like this arrives carrying.

    One family stays out of reach. ``ListFieldAddConflictError`` and its
    remove counterpart build ``{field}_add_conflict`` from a ``field=``
    argument that is a runtime variable at the only call site, in
    ``sage/services/metadata.py``. No literal exists for the scan to
    find, and no call site narrows the field to a closed set, so the
    concrete codes are pinned in ``UNRESOLVED_IDENTIFIERS`` instead --
    the honest record being *not swept*, rather than swept and clean.
    """
    codes: set[str] = set()
    for path in _SAGE_PACKAGE.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        for node in ast.walk(tree):
            # ``{"error": "<code>", ...}`` envelopes built at a tool
            # boundary, which never construct a SAGEError and so are
            # invisible to every call-anchored reader below.
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value in {"error", "code"}
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                    ):
                        codes.add(value.value)
            if not isinstance(node, ast.Call):
                continue
            # ``MissingFieldError("<field>", ...)``, which raises
            # ``missing_{field}``. The code is assembled at the raise
            # site, so the family is reachable only through its callers --
            # every one of which passes a literal.
            if getattr(node.func, "id", "") == "MissingFieldError":
                field = node.args[0] if node.args else None
                if isinstance(field, ast.Constant) and isinstance(field.value, str):
                    codes.add(f"missing_{field.value}")
            # ``super().__init__("<code>", ...)`` inside a SAGEError subclass.
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "__init__"
                and isinstance(node.func.value, ast.Call)
                and getattr(node.func.value.func, "id", "") == "super"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                codes.add(node.args[0].value)
            # ``SAGEError("<code>", ...)`` raised directly, and the
            # leaf-layer ``PydanticCustomError("<code>", ...)`` the models
            # module uses because it may not import sage.api.errors.
            if getattr(node.func, "id", "") in {"SAGEError", "PydanticCustomError"}:
                first = node.args[0] if node.args else None
                keyword = next((k.value for k in node.keywords if k.arg == "code"), None)
                for candidate in (first, keyword):
                    if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
                        codes.add(candidate.value)
    from sage.api.errors import _TYPED_ALIAS_CODES

    return codes | set(_TYPED_ALIAS_CODES)


@functools.lru_cache(maxsize=1)
def contract_vocabulary() -> frozenset[str]:
    """Every name the contract defines, across both surfaces.

    Assembled from the machine-readable definitions so the set cannot go
    stale against them. Deliberately wider than any single operation: a
    narrative legitimately names a sibling tool, a shared error code, or
    a vault-config section, and none of those belongs to the operation's
    own schemas.
    """
    from sage.config import BASE_LIFECYCLE_ACTIONS, BASE_LIFECYCLE_STATES

    names: set[str] = set()
    for surface_name in ("sage_core", "cas_app"):
        surface = _SURFACES_BY_NAME[surface_name]
        spec = _load_spec(surface.spec_path)
        names |= set(_all_operation_ids(spec))
        components = spec.get("components") or {}
        # The component *names*, not only their properties: a narrative
        # that says "returns a ChainResponse" is naming a real contract
        # type, and the CamelCase tier reads it.
        for group in ("schemas", "parameters", "responses"):
            names |= set(components.get(group) or {})
        _collect(components, "properties", names)
        _collect(spec, "enum", names)
        for tool in _surface_registry(surface).values():
            names |= set(inspect.signature(tool).parameters)
    names |= set(_all_registered_tools())
    names |= _vault_config_names()
    names |= _error_codes()
    names |= set(BASE_LIFECYCLE_STATES) | set(BASE_LIFECYCLE_ACTIONS)
    return frozenset(names)


def operation_vocabulary(spec: dict[str, Any], operation: dict[str, Any]) -> frozenset[str]:
    """Every name the operation's own contract nodes declare."""
    names: set[str] = set()
    seen: set[str] = set()

    def resolve(ref: str) -> Any:
        node: Any = spec
        for part in ref[2:].split("/"):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return {}
        return node

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if isinstance(ref, str):
            if ref in seen:
                return
            seen.add(ref)
            walk(resolve(ref))
            return
        if isinstance(node.get("properties"), dict):
            for prop_name, prop in node["properties"].items():
                names.add(prop_name)
                walk(prop)
        for value in node.get("enum") or []:
            if isinstance(value, str):
                names.add(value)
        for key in ("allOf", "anyOf", "oneOf", "items", "additionalProperties"):
            if key in node:
                walk(node[key])

    for parameter in operation.get("parameters") or []:
        node = resolve(str(parameter["$ref"])) if "$ref" in parameter else parameter
        if isinstance(node, dict):
            names.add(str(node.get("name") or ""))
            walk(node.get("schema") or {})
    body = (
        (operation.get("requestBody") or {})
        .get("content", {})
        .get("application/json", {})
        .get("schema")
    )
    if body:
        walk(body)
    for status, response in (operation.get("responses") or {}).items():
        # ``responses[*].description`` is deliberately NOT read here. It
        # is a *narrative* surface, and folding its tokens into the
        # vocabulary let it validate itself: a code invented in a
        # response description resolved against its own prose, and one
        # both surfaces agreed on -- which the disclosure-parity gate
        # actively drives them toward -- passed every gate. It is swept
        # as prose in ``unresolved_for`` instead, which is the direction
        # that can report an invented name.
        schema = (response.get("content") or {}).get("application/json", {}).get("schema")
        if schema:
            walk(schema)
    return frozenset(names - {""})


def _structural_tail(text: object) -> str:
    """The part of a docstring ``_prose_body`` leaves behind.

    Exact complement of the parity module's ``_prose_body``, sharing its
    ``_STRUCTURAL_HEADER`` rather than restating the header list: a
    second copy would drift, and a tail cut at a header the other half
    does not recognize would silently skip a block. Everything from the
    first structural header on -- ``Args:``, ``Returns:``,
    ``Error modes:``, the worked examples -- which is where a tool names
    its error codes and its per-argument vocabulary. A docstring with no
    structural header has no tail, and yields the empty string.
    """
    if not isinstance(text, str):
        return ""
    match = _STRUCTURAL_HEADER.search(text)
    return text[match.start() :] if match else ""


def _response_prose(operation: dict[str, Any]) -> str:
    """Every ``responses[*].description`` on the operation, joined."""
    return "\n".join(
        (response.get("description") or "")
        for response in (operation.get("responses") or {}).values()
    )


def unresolved_for(surface_name: str, tool_name: str) -> list[str]:
    """Identifiers either narrative names that the contract does not define.

    Four surfaces, not two. The docstring's prose body and the
    operation's summary-plus-description are the narrative proper; the
    docstring's structural tail and ``responses[*].description`` are
    where error codes and per-argument vocabulary are named, and both
    were previously unread -- the tail because ``_prose_body`` truncates
    at the first structural header, the response descriptions because
    they fed the vocabulary rather than being checked against it.
    """
    docstring, narrative, _whole = _surfaces_for(surface_name, tool_name)
    surface = _SURFACES_BY_NAME[surface_name]
    spec = _load_spec(surface.spec_path)
    operation = _find_operation(spec, _resolve_expected_operation_id(surface, tool_name))
    assert operation is not None, (
        f"{surface_name}.{tool_name} resolves to an operationId the spec does "
        "not define; the tool-to-operation mapping gate should catch this first."
    )
    mentioned = (
        named_identifiers(_prose_body(docstring))
        | named_identifiers(narrative)
        | named_identifiers(_structural_tail(docstring))
        | named_identifiers(_response_prose(operation))
    )
    known = operation_vocabulary(spec, operation) | contract_vocabulary()
    return sorted(mentioned - known)


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------

# Identifiers a narrative names that the contract does not define, each
# with what it is instead. A pin is not an exemption from the audit: it
# is the audit's finding for that name, and the categories below are the
# vocabulary of things caller-facing prose legitimately points at beyond
# the contract's own nodes.
#
# ``test_unresolved_identifier_pins_are_not_stale`` fails on an entry
# that has since become resolvable, so this table can only shrink.
#
# Ten mapped pairs resolve completely and are absent from this table.
# Absence means the sweep found the pair sound, not that it skipped it --
# ``test_narrative_identifiers_resolve_to_the_contract_vocabulary``
# parametrizes over every mapped pair.
#
# Two categories below are named rather than spelled out per entry,
# because each covers enough names that repeating the sentence would
# obscure how large the class is.

# Overwhelmingly the largest category, and the one this table did not
# carry before the structural tail was swept. A SAGEError's ``detail`` is
# an untyped mapping: no schema declares its keys, so a narrative that
# tells a caller which keys to read names something real that no
# contract node defines. The remedy is not a pin but a typed detail
# envelope per error family, which would resolve every one of these at
# once; until then the pin records that the key was checked and found to
# be a genuine detail field rather than an invented one. Each was
# verified against its construction site in ``sage/api/errors.py``.
ERROR_DETAIL_KEY: Final[str] = "key of the untyped SAGEError detail mapping"

# A Python builtin an ``Error modes:`` block names because the tool
# really does raise it rather than a SAGEError. Naming it is accurate
# disclosure -- the caller sees exactly that class -- and no contract
# node declares builtins, so it can only ever resolve here.
BUILTIN_EXCEPTION: Final[str] = "Python builtin exception the tool raises directly"

UNRESOLVED_IDENTIFIERS: Final[dict[tuple[str, str], dict[str, str]]] = {
    ("cas_app", "bulk_ingest_document"): {
        # The closed set of ``EdgeWarning.reason`` values. It is declared
        # only as prose inside that field's own description, so nothing
        # types it and the vocabulary walk cannot reach it. Typing it as
        # an enum would resolve all six at once.
        "chain_repair_withheld": "edge-warning reason, carried as prose not enum",
        "chunk_lifecycle_sync_failed": "edge-warning reason, carried as prose not enum",
        "edge_creation_failed": "edge-warning reason, carried as prose not enum",
        "lifecycle_transition_failed": "edge-warning reason, carried as prose not enum",
        "supersede_target_missing": "edge-warning reason, carried as prose not enum",
        "supersede_target_not_transitionable": "edge-warning reason, carried as prose not enum",
        "supersede_target_read_failed": "edge-warning reason, carried as prose not enum",
        "staging_edges": "graph-store table holding tier-2 edges awaiting review",
    },
    ("sage_core", "create_edges"): {
        "deliverable_id": "example tier-3 metadata key in a worked example",
        "template_id": "example tier-3 metadata key in a worked example",
    },
    ("sage_core", "create_vault"): {
        "dict": "prose: the Python type of the config argument",
        # Kept rather than reworded. Together these name the only
        # affordance a caller has for a default config -- no route
        # returns one -- so unlike the implementation identifiers removed
        # alongside them, removing these would cost the reader something.
        "get_default_config": "Python constructor for a default config; no API route returns one",
        "VaultRegistryService": "the class holding that constructor; kept for the same reason",
        "sage_vaults": "on-disk vault directory, not a contract node",
        "vault_config": "the per-vault YAML file, not a contract node",
    },
    ("sage_core", "get_document"): {
        "store_status": ERROR_DETAIL_KEY,
    },
    ("sage_core", "get_filename_metadata"): {
        "cas": "example vault id in a worked example",
        "doc_code": "example filename segment field, defined per vault",
        "doc_date": "example filename segment field, defined per vault",
        "sage_vaults": "on-disk vault directory, not a contract node",
    },
    ("sage_core", "get_vault_config"): {
        "vault_config": "the per-vault YAML file, not a contract node",
    },
    ("sage_core", "get_vault_stats"): {
        "vault_config": "the per-vault YAML file, not a contract node",
    },
    ("sage_core", "ingest_document"): {
        "cas": "example vault id in a worked example",
        "false": "prose: a literal boolean value",
        "sage_vaults": "on-disk vault directory, not a contract node",
        "ticket_id": "example tier-3 metadata key in a worked example",
        "unique": "prose: the tier-3 uniqueness property, not a field name",
        "imports": "vault source-tree directory, not a contract node",
        # Keys of the untyped error ``detail``, named in the
        # ``Error modes:`` block. See the category note above the table.
        "allowed_states": ERROR_DETAIL_KEY,
        "colliding_value": ERROR_DETAIL_KEY,
        "current_head_version": ERROR_DETAIL_KEY,
        "current_state": ERROR_DETAIL_KEY,
        "example": ERROR_DETAIL_KEY,
        "existing_document_id": ERROR_DETAIL_KEY,
        "existing_source_path": ERROR_DETAIL_KEY,
        "fields": ERROR_DETAIL_KEY,
        "instance": ERROR_DETAIL_KEY,
        "new_source_path": ERROR_DETAIL_KEY,
        "recognized": ERROR_DETAIL_KEY,
        "required_state": ERROR_DETAIL_KEY,
        "store_status": ERROR_DETAIL_KEY,
    },
    ("sage_core", "optimize_vault_content_store"): {
        "ValueError": BUILTIN_EXCEPTION,
    },
    ("sage_core", "read_section"): {
        "candidate_matches": ERROR_DETAIL_KEY,
    },
    ("sage_core", "restore_vault_source_file"): {
        "candidate_ids": ERROR_DETAIL_KEY,
        "store_status": ERROR_DETAIL_KEY,
    },
    ("sage_core", "search"): {
        "KiB": "prose: the unit the inline budget is stated in",
        "null": "prose: the JSON literal, in a worked filter example",
        "failure_record": "example doc_type value in a worked example",
        "fix_commit": "example tier-3 metadata key in a worked example",
        "allowed_modes": ERROR_DETAIL_KEY,
        "constraint": ERROR_DETAIL_KEY,
        "example": ERROR_DETAIL_KEY,
        "expected_type": ERROR_DETAIL_KEY,
        "fields": ERROR_DETAIL_KEY,
        "forbidden_param": ERROR_DETAIL_KEY,
        "hint": ERROR_DETAIL_KEY,
        "key": ERROR_DETAIL_KEY,
        "operation": ERROR_DETAIL_KEY,
        "parameter": ERROR_DETAIL_KEY,
        "received_type": ERROR_DETAIL_KEY,
        "recognized": ERROR_DETAIL_KEY,
        "recommended_limit": ERROR_DETAIL_KEY,
        "valid_keys": ERROR_DETAIL_KEY,
        "valid_modes": ERROR_DETAIL_KEY,
        "valid_values": ERROR_DETAIL_KEY,
    },
    ("sage_core", "list_headings"): {
        # Carried on the error envelope's untyped ``detail``, which no
        # schema describes.
        "available_headings": "field of the heading_not_found error detail",
    },
    ("sage_core", "list_staging_edges"): {
        "by_source_document": "vault-config value for staging_review_grouping",
        "content_reference": "vault-config value for an edge-inference method",
        "identifier_mention": "vault-config value for an edge-inference method",
        "re_ingestion": "vault-config value for an edge-inference method",
    },
    ("sage_core", "migrate_vault"): {
        "graph_store": "the storage component, not a contract node",
    },
    ("sage_core", "read_projection"): {
        # Values of ``delivery``, an MCP-only argument with no REST
        # counterpart (see KNOWN_ARG_DRIFT); the argument name resolves,
        # its values are not enumerated in either spec.
        "auto": "value of the MCP-only delivery argument",
        "inline": "value of the MCP-only delivery argument",
        "spill": "value of the MCP-only delivery argument",
    },
    ("sage_core", "recompute_abstract"): {
        "start_time": "field of the reabstract_already_in_flight error detail",
    },
    ("sage_core", "recompute_deferred_vault_abstracts"): {
        "reabstract_deferred": "operator fallback script under scripts/",
        "start_time": "field of the reabstract_already_in_flight error detail",
        "RuntimeError": BUILTIN_EXCEPTION,
    },
    ("sage_core", "recompute_views"): {
        # The symlink directory. The response field counting what landed
        # in it is ``by_lifecycle_status``, which resolves.
        "by_lifecycle": "generated view directory under storage_root",
    },
    ("sage_core", "update_lifecycles"): {
        "cas": "example vault id in a worked example",
        "sage_vaults": "on-disk vault directory, not a contract node",
        "KiB": "prose: the unit the inline budget is stated in",
    },
    ("sage_core", "update_metadata"): {
        "other_key": "placeholder in a worked example, not a field name",
        "sage_vaults": "on-disk vault directory, not a contract node",
        "KiB": "prose: the unit the inline budget is stated in",
        "current_version": ERROR_DETAIL_KEY,
        # Codes built as f"{field}_add_conflict" at the raise site, so
        # the literal never appears in source for the AST scan to find.
        "tags_add_conflict": "error code from a parameterized family",
        "tags_remove_conflict": "error code from a parameterized family",
    },
    ("sage_core", "update_vault_config"): {
        "sage_vaults": "on-disk vault directory, not a contract node",
        "vault_config": "the per-vault YAML file, not a contract node",
    },
    ("sage_core", "verify_hashes"): {
        # Names the published shape of the digest a caller sends rather
        # than an implementation detail: here the alias *is* the
        # contract, so naming it is disclosure, not leakage.
        "Sha256Str": "typed alias naming the published digest shape",
    },
    ("sage_core", "verify_vault_source_files"): {
        "store_status": ERROR_DETAIL_KEY,
    },
}


# Anti-vacuity floors. An extractor that silently returns the empty set,
# or a vocabulary that silently swallows everything, makes every
# assertion above pass. These pin both ends against the measured
# baseline -- 32 pairs, 271 distinct identifiers across the docstrings,
# 187 across the descriptions, 621 names in the vocabulary -- set a
# short step below each so ordinary editing does not trip them while a
# collapse does.
MIN_PAIRS_SWEPT: Final[int] = 30
MIN_DOCSTRING_IDENTIFIERS: Final[int] = 240
MIN_DESCRIPTION_IDENTIFIERS: Final[int] = 165
MIN_CONTRACT_VOCABULARY: Final[int] = 550

# How many operation descriptions may name no identifier at all. Two do
# today -- ``read_projection`` and ``search`` -- both stating the call in
# plain terms and leaving the field-level vocabulary to their schemas and
# their tool docstrings. That is legal, so a per-pair floor would assert
# something untrue of them; the ceiling instead catches an extraction
# path that has stopped reaching the spec surface.
MAX_SILENT_DESCRIPTIONS: Final[int] = 2

# The same floors for the two surfaces added when the error-code tier was
# closed. They matter more than the ones above, not less: the tail is
# reached by a slice rather than by a match, and a slice that lands wrong
# yields the empty string rather than an error -- at which point every
# pair resolves clean and the whole tier is silently gone. Measured
# baseline is 242 distinct identifiers across 31 tails (one docstring
# carries no structural block at all) and 63 across the response
# descriptions; each floor sits a short step below.
MIN_TAIL_IDENTIFIERS: Final[int] = 215
MIN_RESPONSE_IDENTIFIERS: Final[int] = 55
MIN_PAIRS_WITH_TAIL: Final[int] = 28


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("surface_name", "tool_name"), sorted(_mapped_tool_pairs()))
def test_narrative_identifiers_resolve_to_the_contract_vocabulary(
    surface_name: str, tool_name: str
) -> None:
    """Every identifier a narrative names is defined by the contract."""
    pinned = set(UNRESOLVED_IDENTIFIERS.get((surface_name, tool_name), {}))
    unexpected = [name for name in unresolved_for(surface_name, tool_name) if name not in pinned]
    assert not unexpected, (
        f"{surface_name}.{tool_name} names {unexpected} in caller-facing prose, "
        "and the contract defines no such name. Either the prose is wrong "
        "(the case this gate exists for -- a response field that does not "
        "exist reads as None to a caller, with no error), or the name is a "
        "real thing outside the contract's own nodes, in which case add it "
        "to UNRESOLVED_IDENTIFIERS saying what it is."
    )


@pytest.mark.parametrize(("surface_name", "tool_name"), sorted(UNRESOLVED_IDENTIFIERS))
def test_unresolved_identifier_pins_are_not_stale(surface_name: str, tool_name: str) -> None:
    """A pinned identifier that has become resolvable fails the pin.

    The ratchet. Without it the table grows quietly and stops meaning
    anything; with it, a name typed into the contract after being pinned
    forces the pin out.
    """
    still_unresolved = set(unresolved_for(surface_name, tool_name))
    stale = sorted(set(UNRESOLVED_IDENTIFIERS[(surface_name, tool_name)]) - still_unresolved)
    assert not stale, (
        f"{surface_name}.{tool_name} pins {stale}, which the contract now "
        "defines. Remove the pin -- it no longer records anything."
    )


def test_extractor_finds_bare_backticked_tokens() -> None:
    """Single-word marked-up identifiers are extracted, not only snake_case.

    The defect this module was built for is a bare word. An extractor
    keyed on snake_case shape alone -- which is what the sibling parity
    gate's ``_IDENTIFIER`` is -- never sees it, so this pins the union
    from the side that matters.
    """
    narrative = "Returns a `linearity` flag beside `results` and an is_linear boolean."
    found = named_identifiers(narrative)

    assert "linearity" in found, "the bare-word half of the extractor is not running"
    assert "results" in found
    assert "is_linear" in found, "the snake_case half of the extractor is not running"
    # The control: an unmarked single word is prose, and stays out.
    assert "flag" not in found
    assert "beside" not in found
    # And the sibling gate's pattern really does miss what this one catches.
    assert not _SNAKE_CASE.findall("a `linearity` flag")

    # The interior-capital tier, which the two lowercase patterns cannot
    # reach, and which an invented schema name arrives as.
    camel = named_identifiers("Returns a ChainResult carrying a Sha256Str digest.")
    assert {"ChainResult", "Sha256Str"} <= camel
    # Its controls: a sentence-initial ordinary word and an all-caps
    # acronym are not interior-capital names.
    assert "Returns" not in camel
    assert "SAGE" not in named_identifiers("SAGE returns the chain.")


def test_the_historical_linearity_divergence_is_reported() -> None:
    """The defect that motivated this module is reported, and its fix is clean.

    Both halves are asserted. The first alone would pass against a gate
    that reports everything; the second is what makes it a measurement.
    """
    surface = _SURFACES_BY_NAME["sage_core"]
    spec = _load_spec(surface.spec_path)
    operation = _find_operation(spec, "chain")
    assert operation is not None
    known = operation_vocabulary(spec, operation) | contract_vocabulary()

    # The four lines the fix removed, quoted rather than paraphrased --
    # two from the operation description, two from the tool docstring.
    # The third is the one that matters here: it names the field
    # unmarked and in running text, so the bare-word tier does not see
    # it and this fixture would still pass with that occurrence missed.
    # The gate catches the historical case on the other three; quoting
    # all four is what makes that a measurement rather than an
    # assumption.
    before = "\n".join(
        (
            "position) and a `linearity` flag.",
            "`linearity` reports whether the chain is strictly linear,",
            "positional metadata (head, tail, query position, linearity).",
            "``linearity`` in the response reports whether the chain is",
        )
    )
    assert "linearity" in named_identifiers(before) - known, (
        "the sweep does not report the divergence it was built for"
    )

    # And the narrative as it stands now names nothing the contract lacks.
    after = f"{operation.get('summary') or ''}\n\n{operation.get('description') or ''}"
    assert not named_identifiers(after) - known, (
        "the reconciled narrative is not reported clean, so the first "
        "assertion above proves nothing about the relation"
    )


def test_the_fabricated_error_code_is_reported() -> None:
    """An invented error code in an ``Error modes:`` bullet is reported.

    The defect that motivated closing this tier. A replacement written
    into ``migrate_vault``'s ``Error modes:`` block named
    ``tier3_unique_index_blocked``, which no caller can ever see:
    ``Tier3UniqueIndexBlockedError`` is a ``RuntimeError`` subclass,
    caught in the maintenance service and converted to a synthetic
    collision entry. It was caught by reading; nothing mechanical could,
    because the block it was written into was the one surface the sweep
    did not read.

    Both halves are asserted, for the reason the linearity fixture states:
    the first alone would pass against a gate that reports everything.
    """
    surface = _SURFACES_BY_NAME["sage_core"]
    spec = _load_spec(surface.spec_path)
    operation = _find_operation(spec, "migrate_vault")
    assert operation is not None
    known = operation_vocabulary(spec, operation) | contract_vocabulary()

    # The bullet as it was written, quoted rather than paraphrased. It
    # sits under an ``Error modes:`` header because that header is what
    # puts the text in the tail -- without it the sweep would have read
    # the line all along, and the fixture would prove nothing.
    fabricated = (
        "Error modes:\n"
        "- ``vault_not_found`` (404): no vault registered with that id.\n"
        "- ``tier3_unique_index_blocked`` (409): a unique_keys declaration\n"
        "  could not be activated because existing data violates it.\n"
    )
    reported = named_identifiers(_structural_tail(fabricated)) - known
    assert "tier3_unique_index_blocked" in reported, (
        "the sweep does not report a fabricated error code in an "
        "Error modes: block -- the tier is not reaching the tail"
    )
    # The control, and the reason the first bullet is there: a real code
    # sits beside the invented one in the same block, and must not be
    # reported. A sweep that reported the whole block would satisfy the
    # assertion above while saying nothing about the relation.
    assert "vault_not_found" not in reported
    # The header really is what routes the text to the tail: the same
    # bullet without one is prose body, which the sweep read before this
    # change and which therefore proves nothing about the new surface.
    assert not _structural_tail(fabricated[len("Error modes:\n") :])

    # And the corrected text, live, names nothing the contract lacks --
    # which is what makes the assertion above a measurement.
    docstring, _narrative, _whole = _surfaces_for("sage_core", "migrate_vault")
    assert not named_identifiers(_structural_tail(docstring)) - known, (
        "migrate_vault's live Error modes: block is not reported clean, so "
        "the first assertion proves nothing about the relation"
    )


def test_error_codes_reaches_every_construction_site() -> None:
    """``_error_codes()`` finds all four constructions, in every module.

    The reader's own defect class. Its envelope scan was scoped to
    ``mcp_server.py``, which cost it both codes ``sage/app_tools.py``
    emits -- and ``invalid_directory``, named on both ``list_directory``
    narratives, read as an invented name for exactly that reason. A
    file-scoped reader looks identical to a working one until a second
    module starts emitting.
    """
    codes = _error_codes()

    # Envelope codes built outside mcp_server.py, the gap that motivated
    # widening the scan.
    assert {"invalid_directory", "empty_file_list"} <= codes
    # Envelope codes inside it, which must not have been lost in the move.
    assert {"unknown_vault", "internal_error"} <= codes
    # The ``missing_{field}`` family, whose literal exists nowhere: the
    # code is assembled at the raise site from a call-site argument.
    assert {"missing_query", "missing_document_id", "missing_heading_path"} <= codes
    # An ordinary SAGEError subclass, pinning the original reader.
    assert "vault_not_found" in codes

    # The control. A scan that swallowed every string it walked past
    # would satisfy every assertion above; this name is written in this
    # module and in a docstring, and is emitted by nothing.
    assert "tier3_unique_index_blocked" not in codes


def test_the_tail_sweep_is_not_vacuous() -> None:
    """Floors on the two surfaces the error-code tier added.

    ``_structural_tail`` reaches its text by slicing at a match rather
    than by matching text. A slice that lands wrong returns the empty
    string, every pair then resolves clean, and the tier disappears with
    every assertion still green -- so it is pinned from below here.
    """
    pairs = sorted(_mapped_tool_pairs())
    tail_names: set[str] = set()
    response_names: set[str] = set()
    with_tail = 0
    beyond_body = 0
    for surface_name, tool_name in pairs:
        surface = _SURFACES_BY_NAME[surface_name]
        spec = _load_spec(surface.spec_path)
        operation = _find_operation(spec, _resolve_expected_operation_id(surface, tool_name))
        assert operation is not None
        docstring, _narrative, _whole = _surfaces_for(surface_name, tool_name)
        tail = named_identifiers(_structural_tail(docstring))
        if tail:
            with_tail += 1
        if tail - named_identifiers(_prose_body(docstring)):
            beyond_body += 1
        tail_names |= tail
        response_names |= named_identifiers(_response_prose(operation))

    # ``_structural_tail`` claims to be the exact complement of
    # ``_prose_body``, and nothing else here checks it: a tail that
    # failed to truncate -- returning the whole docstring -- would
    # satisfy every floor below, because a superset of the body adds
    # nothing new to ``mentioned`` and so reports identically. The
    # partition is the claim, so the partition is what is asserted.
    for surface_name, tool_name in pairs:
        docstring, _narrative, _whole = _surfaces_for(surface_name, tool_name)
        assert _prose_body(docstring) + _structural_tail(docstring) == docstring, (
            f"{surface_name}.{tool_name}: prose body and structural tail do not "
            "partition the docstring, so one of them is reading the other's text"
        )

    assert with_tail >= MIN_PAIRS_WITH_TAIL, f"only {with_tail} docstrings yielded a tail"
    assert len(tail_names) >= MIN_TAIL_IDENTIFIERS, (
        f"the structural-tail sweep collapsed to {len(tail_names)} identifiers"
    )
    assert len(response_names) >= MIN_RESPONSE_IDENTIFIERS, (
        f"the response-description sweep collapsed to {len(response_names)} identifiers"
    )
    # And the tail is genuinely new text rather than a second reading of
    # the prose body: on nearly every pair it names something the body
    # does not. A tail that only ever repeated the body would satisfy the
    # floors above while adding no reach at all.
    assert beyond_body >= MIN_PAIRS_WITH_TAIL, (
        f"only {beyond_body} tails name anything their prose body does not"
    )


def test_error_detail_pins_name_real_detail_keys() -> None:
    """Every ``ERROR_DETAIL_KEY`` pin is a key some error actually sets.

    The category's own claim, gated. A pin asserts that a name the sweep
    reported is a real thing outside the contract's nodes -- so a name
    pinned under this category is being certified as a genuine detail
    field. Certified by hand, that reproduces one level up exactly the
    defect this module exists to catch: an invented name, pinned as
    real, reads identically to a real one and the ratchet cannot tell
    them apart. ``test_unresolved_identifier_pins_are_not_stale`` only
    asks whether a pin is still unresolvable, which an invented name is
    forever.

    A detail mapping is built two ways -- as a dict literal passed to
    ``super().__init__``, and by assigning into one (``detail["k"] =
    v``) where the key is conditional -- so both forms are read. Three of
    the pins (``candidate_matches``, ``hint``, ``instance``) exist only
    in the second, which is why a dict-literal-only scan is not enough.
    """
    keys: set[str] = set()
    for path in _SAGE_PACKAGE.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys |= {
                    key.value
                    for key in node.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                }
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ):
                keys.add(node.slice.value)

    pinned = {
        name
        for entries in UNRESOLVED_IDENTIFIERS.values()
        for name, category in entries.items()
        if category == ERROR_DETAIL_KEY
    }
    assert pinned, "no ERROR_DETAIL_KEY pins found; the category has gone unused"
    assert not pinned - keys, (
        f"pinned as error-detail keys but set by no error: {sorted(pinned - keys)}. "
        "Either the name is invented -- the defect this module catches, pinned "
        "as though it were real -- or it belongs under a different category."
    )

    # The control. A scan that collected every string it walked past
    # would satisfy the assertion above for any pin at all, including an
    # invented one; this name is written in this module and set by no
    # error.
    assert "tier3_unique_index_blocked" not in keys


def test_response_descriptions_are_checked_not_folded() -> None:
    """A response description no longer supplies its own vocabulary.

    The inversion. While ``operation_vocabulary`` folded every snake_case
    token of ``responses[*].description`` into the operation's own names,
    a code invented there resolved against its own prose -- and one both
    surfaces agreed on, which the disclosure-parity gate actively drives
    them toward, passed every gate in this repository.

    ``candidate_matches`` is the probe because it is the one measured
    name the fold was supplying to a surface that was *already* swept:
    with the fold gone it becomes unresolved and is pinned as the error
    detail key it is. Asserting on the vocabulary's contents rather than
    on the gate being green is what distinguishes "fold removed" from
    "fold removed and the name reintroduced by some other route".
    """
    surface = _SURFACES_BY_NAME["sage_core"]
    spec = _load_spec(surface.spec_path)
    operation = _find_operation(spec, "read_section")
    assert operation is not None

    # It is named in the operation's own response prose ...
    assert "candidate_matches" in named_identifiers(_response_prose(operation))
    # ... and that is no longer enough to make it part of the contract.
    assert "candidate_matches" not in operation_vocabulary(spec, operation)
    assert "candidate_matches" not in contract_vocabulary()
    # So it surfaces as a finding, and carries its category.
    assert (
        UNRESOLVED_IDENTIFIERS[("sage_core", "read_section")]["candidate_matches"]
        == ERROR_DETAIL_KEY
    )


def test_every_local_ref_in_the_specs_resolves() -> None:
    """No ``$ref`` in any spec points at a component that does not exist.

    A dangling reference makes the contract unresolvable for every
    consumer that follows it -- a generator, a validator, and this
    module's own vocabulary walk, which would otherwise silently treat
    the missing target's properties as undeclared.

    Runs over every surface the conformance module declares, including
    ``root_harness``. The rest of this module reaches only the two with
    a tool registry, because a narrative can only be compared where
    there are two of them; a ``$ref`` needs no implementation to dangle,
    so scoping this check the same way would leave a whole spec
    unexamined for the defect it was written to catch.
    """
    broken: list[str] = []
    for surface_name in _SURFACES_BY_NAME:
        spec = _load_spec(_SURFACES_BY_NAME[surface_name].spec_path)

        def check(node: Any, trail: str, spec: dict[str, Any] = spec) -> None:
            if isinstance(node, list):
                for index, item in enumerate(node):
                    check(item, f"{trail}[{index}]")
                return
            if not isinstance(node, dict):
                return
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/"):
                target: Any = spec
                for part in ref[2:].split("/"):
                    if isinstance(target, dict) and part in target:
                        target = target[part]
                    else:
                        broken.append(f"{surface_name}: {trail} -> {ref}")
                        break
            for key, value in node.items():
                check(value, f"{trail}.{key}")

        check(spec, surface_name)
    assert not broken, "dangling $ref:\n  " + "\n  ".join(broken)


def test_the_sweep_is_not_vacuous() -> None:
    """Floors on both ends of the relation.

    An empty extractor or an all-swallowing vocabulary makes every
    assertion in this module pass. Breaking either must show up here.
    """
    pairs = sorted(_mapped_tool_pairs())
    assert len(pairs) >= MIN_PAIRS_SWEPT, f"only {len(pairs)} mapped pairs swept"

    vocabulary = contract_vocabulary()
    assert len(vocabulary) >= MIN_CONTRACT_VOCABULARY, (
        f"contract vocabulary collapsed to {len(vocabulary)} names"
    )
    # A vocabulary that swallows everything is the mirror failure, and a
    # word no contract declares is the cheapest probe for it.
    assert "linearity" not in vocabulary

    from_docstrings: set[str] = set()
    from_specs: set[str] = set()
    spec_silent: list[str] = []
    for surface_name, tool_name in pairs:
        docstring, narrative, _whole = _surfaces_for(surface_name, tool_name)
        doc_names = named_identifiers(_prose_body(docstring))
        spec_names = named_identifiers(narrative)
        assert doc_names, f"{surface_name}.{tool_name} docstring yielded no identifiers"
        if not spec_names:
            spec_silent.append(f"{surface_name}.{tool_name}")
        from_docstrings |= doc_names
        from_specs |= spec_names

    assert len(from_docstrings) >= MIN_DOCSTRING_IDENTIFIERS
    assert len(from_specs) >= MIN_DESCRIPTION_IDENTIFIERS
    # An operation description that names nothing is legal -- a short one
    # can describe the call in plain terms -- so this is a ceiling on how
    # many may be silent rather than a per-pair floor. It is the tell for
    # an extraction path that has stopped reaching the spec surface.
    assert len(spec_silent) <= MAX_SILENT_DESCRIPTIONS, (
        f"{len(spec_silent)} operation descriptions name no identifier: {spec_silent}"
    )
