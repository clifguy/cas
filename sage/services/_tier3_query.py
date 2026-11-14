"""Resolve typed-metadata (tier3) query terms against the vault's declarations.

A ``tier3_metadata`` filter, a ``tier3:<field>`` facet and a ``tier3:<field>``
sort key all name a field whose meaning lives in a doc_type's
``metadata_schema`` (CAS-ADR-028). This module reads those declarations once
per request term and hands the graph store only resolved, validated terms, so
the MCP and REST surfaces -- which both reach the retrieval service -- refuse
and answer identically (CAS-ADR-052).

The doc_types in scope are the request's filtered doc_type, or every doc_type
declaring a ``metadata_schema`` when none is filtered. A field may be declared
differently by different doc_types; a list operator, a facet or a sort applies
only to the doc_types whose declaration admits it, and documents of any other
doc_type never match the operator or enter the facet. A literal filter value
keeps exact equality over every document, as it always has.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal

from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

from sage.adapters.interfaces import (
    TIER3_FIELD_PREFIX,
    TIER3_LIST_OPERATORS,
    Tier3FacetSpec,
    Tier3ListPredicate,
)
from sage.api.errors import (
    InvalidFilterShapeError,
    InvalidFilterValueError,
    InvalidParameterError,
)
from sage.config import VaultConfig

if TYPE_CHECKING:
    from referencing._core import Resolver

FieldKind = Literal["scalar", "array", "object", "composite"]

_SCALAR_TYPES = (str, int, float, bool)

#: Bound on ``$ref`` hops followed while resolving one property schema.
_MAX_REF_HOPS = 16


def _declared_types(schema: dict) -> set[str]:
    """The non-null JSON types a property schema declares."""
    declared = schema.get("type")
    if isinstance(declared, str):
        types = {declared}
    elif isinstance(declared, list):
        types = set(declared)
    else:
        types = set()
    return types - {"null"}


def _is_null_only(schema: dict) -> bool:
    declared = schema.get("type")
    return declared == "null" or declared == ["null"]


def _merge(outer: dict, inner: dict) -> dict:
    """``outer`` with ``inner``'s keywords added where ``outer`` has none."""
    merged = dict(outer)
    for key, value in inner.items():
        merged.setdefault(key, value)
    return merged


def _as_schema(schema: object) -> dict:
    """A schema as a dict for classification; a boolean schema reads as untyped."""
    return schema if isinstance(schema, dict) else {}


def resolver_for(root: dict) -> Resolver:
    """A reference resolver rooted at a doc_type's ``metadata_schema``."""
    resource = Resource.from_contents(root, default_specification=DRAFT202012)
    base = resource.id() or ""
    return Registry().with_resource(uri=base, resource=resource).resolver(base_uri=base)


def _resolve(schema: object, resolver: Resolver, hops: int = 0) -> dict:
    """Follow references and flatten compositions for classification.

    Applied alike to a property, its ``items`` and every composition
    branch. A ``$ref`` is followed as the validator follows it -- JSON
    pointers, anchors and bundled ``$id`` resources -- its sibling keywords
    kept. An ``allOf`` is merged into the schema carrying it. An ``anyOf``
    or ``oneOf`` whose branches are one schema and ``null`` is merged
    likewise, so ``anyOf: [{type: array, ...}, {type: null}]`` declares an
    array; any other ``anyOf`` or ``oneOf`` keeps its resolved branches for
    ``field_kind``. Keywords the carrying schema declares itself win over a
    merged branch's. A boolean schema reads as untyped. A reference
    that cannot be followed is left in place, so the schema reads as
    untyped. Resolution stops after a bounded number of steps, so a
    reference cycle cannot recurse without end. The result serves
    classification only; operands are validated against the declaration
    itself (see ``Tier3Declarations.admits``).
    """
    schema = _as_schema(schema)
    if hops >= _MAX_REF_HOPS:
        return schema
    if isinstance(schema.get("$ref"), str):
        try:
            resolved = resolver.lookup(schema["$ref"])
        except Unresolvable:
            return schema
        siblings = {key: value for key, value in schema.items() if key != "$ref"}
        return _resolve(
            _merge(siblings, _as_schema(resolved.contents)), resolved.resolver, hops + 1
        )
    branches = schema.get("allOf")
    if isinstance(branches, list):
        merged = {key: value for key, value in schema.items() if key != "allOf"}
        for branch in branches:
            merged = _merge(merged, _resolve(branch, resolver, hops + 1))
        return _resolve(merged, resolver, hops + 1)
    for key in ("anyOf", "oneOf"):
        branches = schema.get(key)
        if isinstance(branches, list):
            outer = {name: value for name, value in schema.items() if name != key}
            resolved_branches = [_resolve(branch, resolver, hops + 1) for branch in branches]
            non_null = [branch for branch in resolved_branches if not _is_null_only(branch)]
            if len(non_null) == 1:
                return _resolve(_merge(outer, non_null[0]), resolver, hops + 1)
            schema = {**outer, key: non_null}
    if "items" in schema:
        schema = {**schema, "items": _resolve(schema["items"], resolver, hops + 1)}
    return schema


def field_kind(schema: object) -> FieldKind:
    """Classify a resolved schema -- a property, its items, or a branch.

    The declared ``type`` decides first. ``object`` is an object.
    ``array`` is a list whose items are scalars, the only list the
    operators and list facets read. ``composite`` is any other list -- of
    objects or lists, or declared beside another type. A schema declaring
    no type is classified by its ``anyOf``/``oneOf`` branches: an object
    branch makes it ``object``, any other non-scalar branch ``composite``.
    Anything else is a ``scalar``. Keywords outside these -- conditionals
    (``if``/``then``/``else``) and ``$dynamicRef`` among them -- are not
    read, so a list declared only through them reads as a scalar and takes
    no operators.
    """
    if not isinstance(schema, dict):
        return "scalar"
    types = _declared_types(schema)
    if "object" in types:
        return "object"
    if "array" in types:
        if types == {"array"} and field_kind(schema.get("items") or {}) == "scalar":
            return "array"
        return "composite"
    if types:
        return "scalar"
    kinds: set[str] = set()
    for key in ("anyOf", "oneOf"):
        branches = schema.get(key)
        if isinstance(branches, list):
            kinds |= {field_kind(branch) for branch in branches}
    if "object" in kinds:
        return "object"
    if kinds - {"scalar"}:
        return "composite"
    return "scalar"


def _item_candidates(items: dict) -> list[object]:
    """Every value an items schema enumerates, through its branches too."""
    values = list(items.get("enum") or [])
    if "const" in items:
        values.append(items["const"])
    for key in ("anyOf", "oneOf"):
        for branch in items.get(key) or []:
            if isinstance(branch, dict):
                values.extend(_item_candidates(branch))
    return values


def _is_closed(items: dict) -> bool:
    """Whether an items schema admits only enumerated values."""
    if "enum" in items or "const" in items:
        return True
    branches = [
        branch
        for key in ("anyOf", "oneOf")
        for branch in items.get(key) or []
        if isinstance(branch, dict) and not _is_null_only(branch)
    ]
    return bool(branches) and all(_is_closed(branch) for branch in branches)


def _render(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def _json_type(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    if isinstance(value, list):
        return "array"
    return "object"


class Tier3Declarations:
    """The ``metadata_schema`` properties declared by the doc_types in scope.

    Each property is held resolved (see ``_resolve``).
    """

    def __init__(self, config: VaultConfig, doc_type: str | None) -> None:
        names = (
            [doc_type] if doc_type else [entry.value for entry in config.document_types.doc_types]
        )
        self._properties: dict[str, dict[str, dict]] = {}
        self._validators: dict[str, object] = {}
        self._roots: dict[str, dict] = {}
        for name in names:
            validator = config.tier3_validator(name)
            if validator is None:
                continue
            root = validator.schema
            properties = root.get("properties", {}) or {}
            resolver = resolver_for(root)
            resolved: dict[str, dict] = {}
            for field, schema in properties.items():
                resolved[field] = _resolve(schema, resolver)
            self._properties[name] = resolved
            self._validators[name] = validator
            self._roots[name] = root

    def declaring(self, field: str) -> dict[str, dict]:
        """``{doc_type: resolved property}`` for each in-scope doc_type declaring ``field``."""
        return {
            doc_type: props[field] for doc_type, props in self._properties.items() if field in props
        }

    def admits(self, doc_type: str, field: str, value: object) -> bool:
        """Whether a list holding ``value`` satisfies the doc_type's declaration.

        The doc_type's own validator checks ``{field: [value]}``, so the
        element is judged exactly as on write, through every reference and
        composition. Only errors bearing on the element count: list-level
        constraints such as ``minItems`` and other fields' requirements say
        nothing about one operand. A union branch that cannot hold a list,
        or whose element fails, rules the branch out; the element is refused
        only when every branch is ruled out. A reference the schema cannot
        resolve admits nothing, so the operand is refused rather than raising.
        """
        validator = self._validators[doc_type]
        target = (field, 0)

        def rules_out(error: object) -> bool:
            path = tuple(error.absolute_path)  # type: ignore[attr-defined]
            if path[:2] == target:
                return True
            if path == (field,) and error.validator == "type":  # type: ignore[attr-defined]
                return True
            if path in ((), (field,)) and error.validator in ("anyOf", "oneOf"):  # type: ignore[attr-defined]
                context = error.context or []  # type: ignore[attr-defined]
                groups: dict[object, list] = {}
                for sub in context:
                    groups.setdefault(sub.schema_path[0], []).append(sub)
                branches = error.validator_value  # type: ignore[attr-defined]
                if not context or len(groups) < len(branches):
                    return False
                return all(any(rules_out(sub) for sub in group) for group in groups.values())
            return False

        try:
            errors = list(validator.iter_errors({field: [value]}))  # type: ignore[attr-defined]
        except Unresolvable:
            return False
        return not any(rules_out(error) for error in errors)


def normalize_tier3_filter(
    config: VaultConfig, tier3: dict, doc_type: str | None
) -> dict[str, object]:
    """Replace each operator object with resolved ``Tier3ListPredicate`` terms.

    A literal value passes through untouched, keeping exact equality. A dict
    is an operator object where its keys are operators and an in-scope
    doc_type declares the field as a list of scalars. Where none declares
    it as an object, any other dict on such a field is refused, and so is
    an operator object on a field declared nowhere as a list of scalars:
    either would otherwise compare as a literal and match nothing -- the
    silent empty result the refusal exists to replace. Where a doc_type
    declares the field as an object, a dict that is not an operator object
    stays a literal.
    """
    declarations = Tier3Declarations(config, doc_type)
    out: dict[str, object] = {}
    for key, value in tier3.items():
        if not isinstance(value, dict):
            out[key] = value
            continue
        declaring = declarations.declaring(key)
        arrays = {dt: s for dt, s in declaring.items() if field_kind(s) == "array"}
        has_objects = any(field_kind(s) == "object" for s in declaring.values())
        operator_shaped = bool(value) and set(value) <= set(TIER3_LIST_OPERATORS)
        if arrays and operator_shaped:
            out[key] = tuple(
                _predicate(declarations, key, op, value[op], arrays)
                for op in TIER3_LIST_OPERATORS
                if op in value
            )
        elif has_objects:
            out[key] = value
        elif arrays:
            raise InvalidFilterShapeError(
                field=f"tier3_metadata.{key}",
                expected_type="operator object with contains, any, all or none",
                received_type=f"object with keys {sorted(value)}",
            )
        elif operator_shaped:
            raise InvalidFilterShapeError(
                field=f"tier3_metadata.{key}",
                expected_type=(
                    "a literal value: no doc_type in scope declares this field as a list of scalars"
                ),
                received_type=f"operator object with keys {sorted(value)}",
            )
        else:
            out[key] = value
    return out


def _predicate(
    declarations: Tier3Declarations,
    key: str,
    operator: str,
    operand: object,
    arrays: dict[str, dict],
) -> Tier3ListPredicate:
    field = f"tier3_metadata.{key}"
    if operator == "contains":
        if not isinstance(operand, _SCALAR_TYPES):
            raise InvalidFilterShapeError(
                field=field, expected_type="a scalar value", received_type=_json_type(operand)
            )
        values: list[object] = [operand]
    else:
        if (
            not isinstance(operand, list)
            or not operand
            or not all(isinstance(item, _SCALAR_TYPES) for item in operand)
        ):
            raise InvalidFilterShapeError(
                field=field,
                expected_type="a non-empty list of scalar values",
                received_type=_json_type(operand),
            )
        values = list(operand)

    item_types: set[str] = set()
    closed = True
    candidates: list[object] = []
    for schema in arrays.values():
        items = schema.get("items") or {}
        item_types |= _declared_types(items)
        closed = closed and _is_closed(items)
        candidates.extend(_item_candidates(items))

    def admitted(value: object) -> bool:
        return any(declarations.admits(doc_type, key, value) for doc_type in arrays)

    for value in values:
        if admitted(value):
            continue
        received = _json_type(value)
        type_ok = (
            not item_types
            or received in item_types
            or (received == "integer" and "number" in item_types)
        )
        if not type_ok:
            raise InvalidFilterShapeError(
                field=field, expected_type=" or ".join(sorted(item_types)), received_type=received
            )
        vocabulary = sorted(
            {_render(item) for item in candidates if item is not None and admitted(item)}
        )
        if closed and vocabulary:
            raise InvalidFilterValueError(field=field, value=value, valid_values=vocabulary)
        raise InvalidFilterShapeError(
            field=field,
            expected_type="a value satisfying the field's declared items schema",
            received_type=f"{received} {value!r}",
        )
    return Tier3ListPredicate(
        operator=operator, values=tuple(values), doc_types=tuple(sorted(arrays))
    )


def _tier3_field_name(name: str) -> str:
    return name[len(TIER3_FIELD_PREFIX) :]


def resolve_tier3_facets(
    config: VaultConfig, names: list[str], doc_type: str | None
) -> list[Tier3FacetSpec]:
    """Resolve the ``tier3:<field>`` entries of ``facet_fields``, in order given."""
    declarations = Tier3Declarations(config, doc_type)
    specs: list[Tier3FacetSpec] = []
    for name in dict.fromkeys(names):
        field = _tier3_field_name(name)
        declaring = declarations.declaring(field)
        usable = {dt: s for dt, s in declaring.items() if field_kind(s) in ("scalar", "array")}
        if not usable:
            raise InvalidParameterError(
                parameter="facet_fields",
                value=name,
                constraint=(
                    "must name a typed-metadata field declared as a scalar or a list "
                    "of scalars by a doc_type in scope"
                ),
            )
        specs.append(
            Tier3FacetSpec(
                name=name,
                field=field,
                doc_types=tuple(sorted(usable)),
            )
        )
    return specs


def validate_tier3_sort(config: VaultConfig, sort_by: str, doc_type: str | None) -> None:
    """Refuse a ``tier3:<field>`` sort key that no in-scope doc_type declares as a scalar."""
    field = _tier3_field_name(sort_by)
    declaring = Tier3Declarations(config, doc_type).declaring(field)
    if not declaring or any(field_kind(s) != "scalar" for s in declaring.values()):
        raise InvalidParameterError(
            parameter="sort_by",
            value=sort_by,
            constraint=(
                "must name a typed-metadata field declared as a scalar by every "
                "doc_type in scope that declares it"
            ),
        )
