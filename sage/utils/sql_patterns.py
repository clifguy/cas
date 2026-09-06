"""Escaping for SQL ``LIKE``/``ILIKE`` pattern operands.

Shared by both Postgres bindings so a caller's text means the same thing to
each. The two reach the operator for different reasons -- one to hold a heading
prefix, one to hold a search query -- but a value spliced into a pattern
position is a pattern either way, and a rule stated in one binding is a rule the
other does not inherit.
"""


def escape_like(value: str) -> str:
    """Escape ``LIKE`` wildcards so a value never acts as a pattern.

    The backslash goes first: escaping it after introducing escapes of our own
    would double them and change what they mean.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
