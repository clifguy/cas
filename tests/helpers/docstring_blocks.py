"""Read the caller-facing ``Error modes:`` block out of a tool docstring.

Shared by the gates that hold a tool's declared refusals to what it emits. A
code named in the narrative above the block, or in ``Args:`` prose below it,
is not a declared error mode, so a containment test over the whole docstring
is the rival these gates exist to avoid.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from typing import Final

# A tool's error-mode list is introduced by a line-initial header naming
# "error modes", optionally led by one word and optionally qualified in
# parentheses -- "Error modes:", "Error modes (raised synchronously ...):",
# "Batch-level error modes (...):". The qualifier scopes the list rather than
# renaming it and may wrap across lines, so keying on the bare spelling read
# three enumerations as absent. The colon must follow the phrase or its
# parenthesised qualifier directly: a sentence that mentions the phrase is
# narrative, and reading it through to a later colon would open a block at an
# ``Args:`` header and report argument prose as declared error modes.
#
# The fragment is shared, so every gate that decides where an error-mode list
# begins reads the same grammar.
ERROR_MODES_HEADER: Final[str] = r"(?:[A-Z][\w-]*[ \t]+)?[Ee]rror modes(?:[ \t]*\([^)]*\))?:"
ERROR_MODES_HEADER_RE: Final[re.Pattern[str]] = re.compile(
    rf"^[ \t]*{ERROR_MODES_HEADER}", re.MULTILINE
)


def error_modes_block(fn: Callable) -> str:
    """The tool docstring's ``Error modes:`` block, or the empty string.

    Every such list, not the first: a tool that separates its call-level
    refusals from its per-item ones declares two, and reading one of them
    reports the other's codes as undisclosed. Each is bounded at the next
    structural header or the next list, so a code named in ``Args:`` prose, or
    in the narrative above, does not read as a declared error mode. Which block
    names the code is the whole point: a caller looking for what a call can
    return reads these.
    """
    doc = inspect.getdoc(fn) or ""
    blocks: list[str] = []
    for match in ERROR_MODES_HEADER_RE.finditer(doc):
        rest = doc[match.end() :]
        end = len(rest)
        for header in ("Args:", "Returns:", "Raises:", "Example:", "Examples:", "Note:"):
            found = rest.find(f"\n{header}")
            if found >= 0:
                end = min(end, found)
        next_list = ERROR_MODES_HEADER_RE.search(rest)
        if next_list is not None:
            end = min(end, next_list.start())
        blocks.append(rest[:end])
    return "\n".join(blocks)
