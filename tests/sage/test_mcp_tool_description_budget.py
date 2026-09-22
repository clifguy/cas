"""A tool's caller-facing contract fits what a client will show the model.

MCP clients do not forward a tool's description whole. Claude Code truncates it
at ``DESCRIPTION_BUDGET`` characters, so anything past that point -- in
practice the ``Args:`` block, which sits last -- never reaches an agent, while
a client that forwards descriptions whole pays for every character of every
tool before the first call. Parameter documentation therefore belongs in the
input schema, where it travels with the parameter and no description cap
reaches it, and the description keeps a prose body and a compact error list.

Three rules, held for every registered tool on every surface:

- the description is at most ``DESCRIPTION_BUDGET`` characters;
- it carries no parameter block (``Args:`` and its aliases);
- every top-level parameter carries a schema ``description``.

A tool added to the roster is held to the rules from registration.

A compact error-mode list is a curated subset of the tool's published error
table, so every tool, converted or not, is also held to naming only codes that
table carries. That check is only as accurate as the table.
"""

from __future__ import annotations

import re
from typing import Final

import pytest

from tests.helpers.published_tool import parameter_descriptions, published_tools
from tests.sage.mcp_surface_pin import EXPECTED_SURFACE

#: The tightest description cap observed in a client: Claude Code keeps the
#: first 2,048 characters of a tool description and discards the rest.
DESCRIPTION_BUDGET: Final[int] = 2048

#: A parameter block by any of the names the common docstring styles give it.
#: Keying on ``Args:`` alone would let the same block back in under another
#: heading, which is the rule's purpose rather than its spelling.
_ARGS_HEADER_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t]*(?:Args|Arguments|Parameters|Params|Keyword Args|Kwargs):?[ \t]*$",
    re.MULTILINE,
)


def _violations(tool: object) -> list[str]:
    description = tool.description or ""  # type: ignore[attr-defined]
    found: list[str] = []
    if len(description) > DESCRIPTION_BUDGET:
        found.append(f"description is {len(description)} chars (budget {DESCRIPTION_BUDGET})")
    if _ARGS_HEADER_RE.search(description):
        found.append("description carries a parameter block")
    schema = tool.parameters  # type: ignore[attr-defined]
    described = parameter_descriptions(schema)
    undescribed = sorted(
        p for p in schema.get("properties") or {} if not described.get(p, "").strip()
    )
    if undescribed:
        found.append(f"parameters without a schema description: {undescribed}")
    return found


def test_roster_is_the_whole_surface() -> None:
    """The rules enumerate every registered tool, not an empty or partial set."""
    assert set(published_tools()) == set(EXPECTED_SURFACE)


@pytest.mark.parametrize("name", sorted(EXPECTED_SURFACE))
def test_tool_fits_the_description_budget(name: str) -> None:
    violations = _violations(published_tools()[name])
    assert not violations, f"{name}: " + "; ".join(violations)


#: A listed error-mode entry: a bullet opening with a code in double backticks,
#: or a bullet grouping several codes under one status (``- 400: ``a``, ``b````).
_ERROR_ENTRY_RE: Final[re.Pattern[str]] = re.compile(r"^\s*- ``([a-z0-9_]+)``", re.MULTILINE)
_GROUPED_ENTRY_RE: Final[re.Pattern[str]] = re.compile(r"^\s*- \d{3}: (.*)$", re.MULTILINE)
_CODE_RE: Final[re.Pattern[str]] = re.compile(r"``([a-z0-9_]+)``")

#: Codes an error-mode list may name that the tool never refuses with, by tool.
#: Each names a condition the tool reports in its successful response.
REPORTED_NOT_REFUSED: Final[dict[str, frozenset[str]]] = {
    # A forked supersedes chain is a drift finding row, not a refusal.
    "verify_vault_drift": frozenset({"chain_nonlinear"}),
}


def _listed_error_codes(description: str) -> set[str]:
    _, sep, block = description.partition("Error modes")
    if not sep:
        return set()
    grouped = {c for line in _GROUPED_ENTRY_RE.findall(block) for c in _CODE_RE.findall(line)}
    return set(_ERROR_ENTRY_RE.findall(block)) | grouped


@pytest.mark.parametrize("name", sorted(EXPECTED_SURFACE))
def test_listed_error_codes_are_ones_the_tool_can_refuse(name: str) -> None:
    """Every code a description lists is in the tool's published error table.

    A compact list is a curated subset of the table, which the typed error
    schema publishes whole; a listed code the tool cannot return sends a caller
    branching on it after a refusal that never comes.
    """
    from sage.models.error_contract import SCHEMAS

    table = set(SCHEMAS["ErrorResponse"]["x-mcp-tool-errors"].get(name, ()))
    listed = _listed_error_codes(published_tools()[name].description or "")
    phantom = sorted(listed - table - REPORTED_NOT_REFUSED.get(name, frozenset()))
    assert not phantom, f"{name} lists codes it cannot refuse with: {phantom}"


def test_reported_not_refused_is_not_stale() -> None:
    """Each exemption is still listed by its tool and still absent from its table."""
    from sage.models.error_contract import SCHEMAS

    table = SCHEMAS["ErrorResponse"]["x-mcp-tool-errors"]
    tools = published_tools()
    for name, codes in REPORTED_NOT_REFUSED.items():
        listed = _listed_error_codes(tools[name].description or "")
        assert codes <= listed, f"{name}: exemption names unlisted codes {sorted(codes - listed)}"
        assert not codes & set(table.get(name, ())), f"{name}: exempted code is now refusable"
