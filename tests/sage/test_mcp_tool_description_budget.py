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
- it carries no ``Args:`` block;
- every top-level parameter carries a schema ``description``.

``KNOWN_UNCONVERTED`` names the tools not yet brought under the rules. It only
shrinks: a tool that now satisfies all three must leave it, and a tool added to
the roster is held to the rules from registration.
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

_ARGS_HEADER_RE: Final[re.Pattern[str]] = re.compile(r"^[ \t]*Args:[ \t]*$", re.MULTILINE)

#: Tools that do not yet satisfy the three rules. Remove a tool when it does.
KNOWN_UNCONVERTED: Final[frozenset[str]] = frozenset(
    {
        "bulk_ingest_document",
        "create_edges",
        "create_vault",
        "delete_edge",
        "export_projection",
        "get_default_vault_config",
        "get_document",
        "get_filename_metadata",
        "get_vault_config",
        "get_vault_stats",
        "list_directory",
        "list_headings",
        "list_pending_metadata",
        "list_staging_edges",
        "migrate_vault",
        "optimize_vault_content_store",
        "read_projection",
        "read_section",
        "recompute_abstract",
        "recompute_deferred_vault_abstracts",
        "recompute_pipeline",
        "recompute_views",
        "reload_vault",
        "restore_vault_source_file",
        "traverse",
        "update_lifecycles",
        "update_metadata",
        "update_staging_edge",
        "update_vault_config",
        "verify_hashes",
        "verify_preconditions",
        "verify_vault_drift",
        "verify_vault_retrieval",
        "verify_vault_source_files",
    }
)


def _violations(tool: object) -> list[str]:
    description = tool.description or ""  # type: ignore[attr-defined]
    found: list[str] = []
    if len(description) > DESCRIPTION_BUDGET:
        found.append(f"description is {len(description)} chars (budget {DESCRIPTION_BUDGET})")
    if _ARGS_HEADER_RE.search(description):
        found.append("description carries an Args: block")
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
    if name in KNOWN_UNCONVERTED:
        pytest.skip("not yet converted; listed in KNOWN_UNCONVERTED")
    violations = _violations(published_tools()[name])
    assert not violations, f"{name}: " + "; ".join(violations)


def test_known_unconverted_is_not_stale() -> None:
    """A listed tool still breaks a rule, and names a registered tool."""
    tools = published_tools()
    unknown = sorted(KNOWN_UNCONVERTED - set(tools))
    assert not unknown, f"KNOWN_UNCONVERTED names unregistered tools: {unknown}"
    conforming = sorted(n for n in KNOWN_UNCONVERTED if not _violations(tools[n]))
    assert not conforming, f"now within budget; remove from KNOWN_UNCONVERTED: {conforming}"
