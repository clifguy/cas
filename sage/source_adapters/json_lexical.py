"""Ordered JSON members and exact number tokens, without map/float materialization.

Strings decode JSON escapes so authored words are searchable. Lone surrogate
code units remain escaped in rendered evidence and valid UTF-8 output. Whitespace
and equivalent string escape spellings are not source identity; retained bytes are.
Callers apply their existing source/depth/node guards before parsing.
"""

import json
from dataclasses import dataclass


class JsonNumber(str):
    """An authored JSON number token, distinct from a decoded string value."""


@dataclass(frozen=True)
class JsonObject:
    members: list[tuple[str, object]]


def loads(source: str) -> object:
    """Parse strict JSON while preserving every ordered member and number token."""

    def refuse(token: str) -> object:
        raise json.JSONDecodeError(f"Non-JSON constant {token}", source, 0)

    return json.loads(
        source,
        object_pairs_hook=JsonObject,
        parse_int=JsonNumber,
        parse_float=JsonNumber,
        parse_constant=refuse,
    )


def string_token(value: str) -> str:
    """Readable JSON string, escaping unpaired surrogates for UTF-8 transport."""
    return json.dumps(value, ensure_ascii=False).encode("utf-8", "backslashreplace").decode("utf-8")


def string_evidence(value: str) -> str:
    """Display text with unpaired surrogates and PostgreSQL-incompatible NUL escaped.

    This is orientation/display text, not the lexical JSON evidence representation.
    """
    return value.encode("utf-8", "backslashreplace").decode("utf-8").replace("\x00", r"\u0000")
