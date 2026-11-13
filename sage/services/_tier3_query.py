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

FieldKind = Literal["scalar", "array", "object"]

_SCALAR_TYPES = (str, int, float, bool)


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


def field_kind(schema: dict) -> FieldKind:
    """Classify a property: an array of scalars, an object-like value, or a scalar.

    An array whose items are objects or arrays is object-like: membership
    over composite elements is not a supported query. A property declaring
    no type, or only scalar types, is a scalar.
    """
    types = _declared_types(schema)
    if types == {"array"}:
        item_types = _declared_types(schema.get("items") or {})
        return "object" if item_types & {"object", "array"} else "array"
    if types & {"object", "array"}:
        return "object"
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
    """The ``metadata_schema`` properties declared by the doc_types in scope."""

    def __init__(self, config: VaultConfig, doc_type: str | None) -> None:
        self._config = config
        names = (
            [doc_type] if doc_type else [entry.value for entry in config.document_types.doc_types]
        )
        self._properties: dict[str, dict[str, dict]] = {}
        for name in names:
            validator = config.tier3_validator(name)
            if validator is not None:
                self._properties[name] = validator.schema.get("properties", {}) or {}

    def declaring(self, field: str) -> dict[str, dict]:
        """``{doc_type: property schema}`` for each in-scope doc_type declaring ``field``."""
        return {
            doc_type: props[field] for doc_type, props in self._properties.items() if field in props
        }

    def item_validators(self, doc_types: dict[str, dict]) -> list:
        return [
            type(self._config.tier3_validator(doc_type))(schema.get("items") or {})
            for doc_type, schema in doc_types.items()
        ]


def normalize_tier3_filter(
    config: VaultConfig, tier3: dict, doc_type: str | None
) -> dict[str, object]:
    """Replace each operator object with resolved ``Tier3ListPredicate`` terms.

    A literal value passes through untouched, keeping exact equality. A
    value is an operator object only where an in-scope doc_type declares the
    field as an array of scalars; there, any dict must be one. A dict made
    of operator keys on a field declared nowhere as such an array is
    refused, because it would otherwise compare as a literal and match
    nothing -- the silent empty result the refusal exists to replace.
    """
    declarations = Tier3Declarations(config, doc_type)
    out: dict[str, object] = {}
    for key, value in tier3.items():
        declaring = declarations.declaring(key)
        arrays = {dt: s for dt, s in declaring.items() if field_kind(s) == "array"}
        objects = {dt: s for dt, s in declaring.items() if field_kind(s) == "object"}
        if not isinstance(value, dict):
            out[key] = value
            continue
        operator_shaped = bool(value) and set(value) <= set(TIER3_LIST_OPERATORS)
        if arrays:
            if not operator_shaped:
                raise InvalidFilterShapeError(
                    field=f"tier3_metadata.{key}",
                    expected_type="operator object with contains, any, all or none",
                    received_type=f"object with keys {sorted(value)}",
                )
            out[key] = tuple(
                _predicate(declarations, key, op, value[op], arrays)
                for op in TIER3_LIST_OPERATORS
                if op in value
            )
            continue
        if operator_shaped and not objects:
            raise InvalidFilterShapeError(
                field=f"tier3_metadata.{key}",
                expected_type=(
                    "a literal value: no doc_type in scope declares this field as an array"
                ),
                received_type=f"operator object with keys {sorted(value)}",
            )
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

    validators = declarations.item_validators(arrays)
    item_types: set[str] = set()
    enums: list[object] = []
    for schema in arrays.values():
        items = schema.get("items") or {}
        item_types |= _declared_types(items)
        enums.extend(items.get("enum") or [])
    for value in values:
        if any(validator.is_valid(value) for validator in validators):
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
        raise InvalidFilterValueError(
            field=field,
            value=value,
            valid_values=sorted({str(item) for item in enums}),
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
        usable = {dt: s for dt, s in declaring.items() if field_kind(s) != "object"}
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
                is_array=any(field_kind(s) == "array" for s in usable.values()),
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
