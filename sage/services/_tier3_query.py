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

from typing import Literal

from referencing.exceptions import Unresolvable

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


def _resolve(schema: object, root: dict, hops: int = 0) -> dict:
    """Follow local ``$ref`` pointers and flatten compositions for classification.

    A ``$ref`` into the doc_type's own schema is followed. An ``allOf`` is
    merged into the schema carrying it. An ``anyOf`` or ``oneOf`` whose
    branches are one schema and ``null`` resolves to that schema, so
    ``anyOf: [{type: array, ...}, {type: null}]`` declares an array; any
    other ``anyOf`` or ``oneOf`` keeps its resolved branches for
    ``field_kind`` to classify.
    """
    if not isinstance(schema, dict) or hops >= _MAX_REF_HOPS:
        return schema if isinstance(schema, dict) else {}
    if "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return schema
        target: object = root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            target = target.get(part) if isinstance(target, dict) else None
        if not isinstance(target, dict):
            return schema
        siblings = {key: value for key, value in schema.items() if key != "$ref"}
        return _resolve({**target, **siblings}, root, hops + 1)
    branches = schema.get("allOf")
    if isinstance(branches, list):
        merged = {key: value for key, value in schema.items() if key != "allOf"}
        for branch in branches:
            for key, value in _resolve(branch, root, hops + 1).items():
                merged.setdefault(key, value)
        return _resolve(merged, root, hops + 1)
    for key in ("anyOf", "oneOf"):
        branches = schema.get(key)
        if isinstance(branches, list):
            resolved = [_resolve(branch, root, hops + 1) for branch in branches]
            non_null = [branch for branch in resolved if not _is_null_only(branch)]
            if len(non_null) == 1:
                return non_null[0]
            return {**schema, key: non_null}
    return schema


def field_kind(schema: dict) -> FieldKind:
    """Classify a resolved property schema.

    ``array`` is a list of scalars, the only list the operators and list
    facets read. ``object`` is an object, or a composition admitting one.
    ``composite`` is a list of objects or lists, or a composition admitting
    a list but no object: neither an operator nor a facet reads it. Anything
    else -- including a property declaring no type -- is a ``scalar``.
    """
    for key in ("anyOf", "oneOf"):
        branches = schema.get(key)
        if isinstance(branches, list):
            kinds = {field_kind(branch) for branch in branches}
            if "object" in kinds:
                return "object"
            if kinds - {"scalar"}:
                return "composite"
            return "scalar"
    types = _declared_types(schema)
    if types == {"array"}:
        item_types = _declared_types(schema.get("items") or {})
        return "composite" if item_types & {"object", "array"} else "array"
    if "object" in types:
        return "object"
    if "array" in types:
        return "composite"
    return "scalar"


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

    Each property is held resolved (see ``_resolve``), its ``items`` too.
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
            resolved: dict[str, dict] = {}
            for field, schema in properties.items():
                prop = _resolve(schema, root)
                if "items" in prop:
                    prop = {**prop, "items": _resolve(prop["items"], root)}
                resolved[field] = prop
            self._properties[name] = resolved
            self._validators[name] = validator
            self._roots[name] = root

    def declaring(self, field: str) -> dict[str, dict]:
        """``{doc_type: resolved property}`` for each in-scope doc_type declaring ``field``."""
        return {
            doc_type: props[field] for doc_type, props in self._properties.items() if field in props
        }

    def item_validators(self, doc_types: dict[str, dict]) -> list:
        """A validator per doc_type for one element of its declared list.

        The items schema keeps the doc_type's reference containers beside
        it -- ``$defs``, ``definitions`` and ``properties`` -- so a reference
        inside the items resolves as it does on write. ``properties`` does
        not constrain the scalar operands validated here.
        """
        validators = []
        for doc_type, schema in doc_types.items():
            root = self._roots[doc_type]
            items = dict(schema.get("items") or {})
            for key in ("$defs", "definitions", "properties"):
                if key in root and key not in items:
                    items[key] = root[key]
            validators.append(type(self._validators[doc_type])(items))
        return validators


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


def _admits(validator: object, value: object) -> bool:
    """Whether ``value`` is valid under ``validator``.

    A reference the items schema cannot resolve admits nothing, so the
    operand is refused rather than raising.
    """
    try:
        return bool(validator.is_valid(value))  # type: ignore[attr-defined]
    except Unresolvable:
        return False


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

    validators = declarations.item_validators(arrays)
    item_types: set[str] = set()
    enums: list[object] = []
    for schema in arrays.values():
        items = schema.get("items") or {}
        item_types |= _declared_types(items)
        enums.extend(items.get("enum") or [])
    for value in values:
        if any(_admits(validator, value) for validator in validators):
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
        if enums and value not in enums:
            raise InvalidFilterValueError(
                field=field,
                value=value,
                valid_values=sorted({str(item) for item in enums}),
            )
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
