"""How a response model becomes a wire body.

The published API contracts decide whether a key appears in a serialized
response. A field the contract declares **required** carries its key on every
response, null or not. A field it declares **optional** may be omitted when its
value is null, and is: an absent key reads as null, and the saving is
substantial on a surface where roughly a third of all response fields are
nullable and most are null on any given call.

Both halves matter, and they are easy to confuse. Dropping every null field is
economical but deletes keys the contract promises are always there, and a null
is often a reported verdict rather than an absence -- "no document holds these
bytes" is information a caller cannot read off a key that is not present.
Keeping every null field honours the contract but inflates every response with
keys that carry nothing.

Serializing with a blanket ``exclude_none`` chooses the first of those for
every field at once, which is why it cannot be right: the choice is per field,
and each field has already been declared. This module applies the declaration.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

__all__ = ["prune_optional_nulls", "to_wire"]


def prune_optional_nulls(model: object, dumped: Any) -> Any:
    """Drop null values whose field is optional, keeping those that are not.

    Walks the model instance and its dumped form together, so nesting is
    followed by the objects themselves rather than inferred from the dict: a
    model inside a model, a list of models, and a mapping of them all keep
    their declarations. Following the instance is what makes a union-typed
    field work, where the dict alone cannot say which member produced it.

    Keys in ``dumped`` with no backing field -- a serializer alias, an extra
    permitted by the model's configuration -- are passed through untouched.
    Nothing has declared them, so nothing here is entitled to remove them.

    Args:
        model: The object that produced ``dumped``.
        dumped: The result of dumping it.

    Returns:
        The dumped structure with optional nulls removed.
    """
    if isinstance(model, BaseModel) and isinstance(dumped, dict):
        pruned: dict[str, Any] = {}
        consumed: set[str] = set()
        for name, field in type(model).model_fields.items():
            key = field.serialization_alias or field.alias or name
            if key not in dumped:
                continue
            consumed.add(key)
            value = dumped[key]
            if value is None and not field.is_required():
                continue
            pruned[key] = prune_optional_nulls(getattr(model, name, None), value)
        for key, value in dumped.items():
            if key not in consumed:
                pruned[key] = value
        return pruned

    if isinstance(model, (list, tuple)) and isinstance(dumped, list):
        if len(model) != len(dumped):
            return dumped
        return [prune_optional_nulls(item, entry) for item, entry in zip(model, dumped)]

    if isinstance(model, dict) and isinstance(dumped, dict):
        return {key: prune_optional_nulls(model.get(key), value) for key, value in dumped.items()}

    return dumped


def to_wire(model: BaseModel) -> dict:
    """Serialize a response model to the body the surface sends.

    JSON mode, so enumerations, timestamps and paths arrive as the JSON types
    the contract declares rather than as Python objects a later encoder has to
    guess at, and optional nulls removed per the rule above.
    """
    return prune_optional_nulls(model, model.model_dump(mode="json"))
