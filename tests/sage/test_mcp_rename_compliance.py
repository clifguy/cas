"""Catalog-level conformance tests for the SAGE MCP tool naming scheme.

Gates over the live FastMCP catalog and the naming taxonomy:

- ``test_no_legacy_prefix`` — every registered tool name omits the
  pre-rename ``sage_``, ``sage_admin_``, and ``app_`` inner prefixes
  that the two-server design in CAS-ADR-034 made vestigial.
- ``test_verb_category_compliance`` — every registered tool name
  begins with a verb in ``CANONICAL_VERBS`` (after stripping any
  member of ``COMPOUND_PREFIXES``). Anchored to CAS-ADR-033.
- ``test_live_catalog_matches_expected_set`` — the live catalog equals
  the roster transcribed in ``SERVER_ASSIGNMENT``.
- ``test_rename_target_invocable`` — a representative tool per class
  dispatches end-to-end (a wrong registration key surfaces as
  tool-not-found).
- ``test_settings_local_permissions_match_live_catalog`` — every
  ``mcp__sage__<name>`` permission entry names a registered tool.

The tests enumerate the live catalog (``mcp.list_tools()``); they do
not parametrize over a static expected list. A stale expected list
would let the gate pass while the registration drifts; live
enumeration surfaces drift on the next test run.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import sage.mcp_server as _mcp
from sage._tool_naming import (
    CANONICAL_VERBS,
    COMPOUND_PREFIXES,
    SERVER_ASSIGNMENT,
    extract_verb,
)
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.config import VaultConfig
from tests.sage.conftest import initialize_services_for_test

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _live_tool_names() -> set[str]:
    """Return the set of tool names currently registered on the FastMCP server."""
    tools = await _mcp.mcp.list_tools()
    return {tool.name for tool in tools}


# ---------------------------------------------------------------------------
# Test 1 — no legacy prefix on any registered tool name
# ---------------------------------------------------------------------------


async def test_no_legacy_prefix() -> None:
    """No live tool name carries the pre-rename ``sage_`` / ``sage_admin_`` / ``app_`` inner prefix.

    Anti-coincidental check: enumerate the **live** registered names via
    ``mcp.list_tools()`` rather than asserting against a static expected
    list. A static list lets a stale registration pass coincidentally;
    live enumeration surfaces any reintroduced legacy prefix.
    """
    legacy_prefixes = ("sage_", "sage_admin_", "app_")
    names = await _live_tool_names()
    offending = sorted(n for n in names if n.startswith(legacy_prefixes))
    assert not offending, (
        f"Tools still carry a legacy inner prefix (sage_, sage_admin_, or app_): {offending}. "
        "The two-server design in CAS-ADR-034 makes these prefixes vestigial; "
        "the MCP client identifier (mcp__<server>__<tool>) carries the disambiguation."
    )


# ---------------------------------------------------------------------------
# Invocation probes — a representative tool per class dispatches end-to-end
# ---------------------------------------------------------------------------


@pytest.fixture
async def vault_services(minimal_vault_config_dict, tmp_vault_dir):
    """Initialize SAGE services and register them in the MCP vault registry.

    Mirrors the fixture in ``test_mcp_server.py`` so invocation probes
    can dispatch through the live MCP layer against a stub-backed vault.
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    async with initialize_services_for_test(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        _mcp._vaults["test_vault"] = services
        try:
            yield services
        finally:
            await asyncio.sleep(0.5)
            _mcp._vaults.pop("test_vault", None)


def _parse_result(result) -> dict | list:
    """Parse the FastMCP TextContent envelope into the inner payload."""
    if isinstance(result, list):
        # FastMCP returns [TextContent(text=<json>), ...]; take the first.
        first = result[0]
        text = getattr(first, "text", first)
        return json.loads(text)
    if isinstance(result, dict):
        return result
    return json.loads(result)


# Invocation probes -- one tool per representative class. The probe
# argument shapes are minimal-valid for each tool. Per CAS-ADR-029 v4
# plural-noun convention, the write-spine probes exercise the
# collection shape (length-1 ``items`` lists) on the consolidated
# tools (create_edges, update_lifecycles, update_metadata).
_INVOCATION_PROBES: tuple[tuple[str, dict], ...] = (
    # Read spine
    ("maint_list_vaults", {}),
    ("get_document", {"vault_id": "test_vault", "document_id": "does-not-exist"}),
    (
        "search",
        {"vault_id": "test_vault", "mode": "catalog", "filters": {}, "limit": 1},
    ),
    # Write spine -- length-1 items collection exercises the pair-collapse
    # tools' minimum-valid input shape.
    (
        "update_metadata",
        {
            "vault_id": "test_vault",
            "items": [{"document_id": "does-not-exist"}],
        },
    ),
    (
        "update_lifecycles",
        {
            "vault_id": "test_vault",
            "items": [{"document_id": "does-not-exist", "action": "archive"}],
        },
    ),
    (
        "create_edges",
        {
            "vault_id": "test_vault",
            "items": [
                {
                    "source_id": "does-not-exist-a",
                    "target_id": "does-not-exist-b",
                    "edge_type": "references",
                    "source_valid_from_version": "does-not-exist-a",
                    "target_valid_from_version": "does-not-exist-b",
                }
            ],
        },
    ),
    # Maintenance
    ("verify_hashes", {"vault_id": "test_vault", "hashes": []}),
)


@pytest.mark.parametrize(("tool_name", "arguments"), _INVOCATION_PROBES)
async def test_rename_target_invocable(vault_services, tool_name: str, arguments: dict) -> None:
    """A representative subset of tools is invokable end-to-end.

    Anti-coincidental check: a typo in a tool's MCP-registered name
    could let registration succeed under the wrong key; a pure-presence
    assertion (see ``test_live_catalog_matches_expected_set``) would
    still pass. The invocation probe forces FastMCP to dispatch to the
    underlying handler; a wrong key would surface as a tool-not-found
    error here.

    The probes target the read, write, and maintenance classes so a
    registration mistake confined to one class still trips at least one
    probe.
    """
    result = await _mcp.mcp.call_tool(tool_name, arguments)
    payload = _parse_result(result)
    # We do not assert on payload correctness here -- some probes
    # legitimately return error envelopes (e.g. document_id="does-not-exist").
    # The point is that dispatch reached a handler that returned a
    # well-formed envelope rather than tool-not-found.
    assert isinstance(payload, (dict, list)), (
        f"{tool_name}: dispatch returned non-envelope payload {payload!r}"
    )


# ---------------------------------------------------------------------------
# Verb-category compliance across the live catalog
# ---------------------------------------------------------------------------


async def test_verb_category_compliance() -> None:
    """Every registered tool name's verb is a member of ``CANONICAL_VERBS``.

    Anti-coincidental check: enumerate live tools (not a static list);
    use the same ``extract_verb`` helper the gate documents (so a
    helper bug surfaces alongside a name bug, and synthetic-input
    tests below pin the helper itself).
    """
    names = await _live_tool_names()
    offending = sorted(n for n in names if extract_verb(n) not in CANONICAL_VERBS)
    assert not offending, (
        f"Tools whose leading verb is not in CANONICAL_VERBS per CAS-ADR-033: "
        f"{offending}. Allowed verbs: {sorted(CANONICAL_VERBS)}; compound "
        f"prefixes: {sorted(COMPOUND_PREFIXES)}."
    )


@pytest.mark.parametrize(
    ("tool_name", "expected_verb"),
    [
        # Plain verb-noun cases
        ("get_document", "get"),
        ("list_vaults", "list"),
        ("search", "search"),
        ("read_section", "read"),
        ("traverse", "traverse"),
        ("chain", "chain"),
        ("create_edge", "create"),
        ("update_metadata", "update"),
        ("delete_edge", "delete"),
        ("ingest_document", "ingest"),
        ("recompute_views", "recompute"),
        ("verify_hash", "verify"),
        ("migrate_vault", "migrate"),
        ("reload_vault", "reload"),
        # Compound-prefix unwrap
        ("bulk_create_edge", "create"),
        ("bulk_update_metadata", "update"),
        ("bulk_ingest_document", "ingest"),
        # Multi-segment tail
        ("recompute_deferred_vault_abstracts", "recompute"),
        ("get_filename_metadata", "get"),
        ("list_pending_metadata", "list"),
        # Edge cases
        ("", ""),
        ("bulk", "bulk"),  # Lone compound prefix with no tail returns the prefix itself.
        ("bulk_", "bulk"),  # Trailing underscore with empty tail behaves the same.
    ],
)
def test_extract_verb_synthetic_inputs(tool_name: str, expected_verb: str) -> None:
    """The ``extract_verb`` helper itself is correct across the compound-prefix and edge cases.

    Anti-coincidental check: if the helper has a bug (e.g. it strips
    ``bulk_`` but then returns the wrong tail segment, or it strips a
    non-compound prefix coincidentally), the live-catalog gate above
    would still pass on coincidence. These synthetic-input cases pin
    the helper independent of the live catalog.
    """
    assert extract_verb(tool_name) == expected_verb


# ---------------------------------------------------------------------------
# settings.local.json permission entries align with live catalog
# ---------------------------------------------------------------------------


async def test_settings_local_permissions_match_live_catalog() -> None:
    """Every SAGE MCP permission entry in the project settings names a
    registered tool on the surface the entry's server segment claims.

    Reads ``.claude/settings.local.json`` (project root) and asserts
    every ``mcp__sage__<X>`` and ``mcp__sage_maint__<X>`` permission
    entry has ``X`` registered in the live MCP catalog **and** assigned
    to that server in ``SERVER_ASSIGNMENT``. Catches drift in three
    directions: entries for renamed-away tools, unauthorized new tools,
    and entries left on a surface a tool has since moved off -- a
    catalog-only check is surface-blind and leaves that last case green.
    Alias names are deliberately not admitted: a permission entry should
    name the canonical tool.

    Anti-coincidental check: the assertion compares against the live
    catalog and the registration table rather than a static expected
    list; if either drifts or a permission goes stale, the next test run
    surfaces it.
    """
    import json as _json
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    settings_path = repo_root / ".claude" / "settings.local.json"
    if not settings_path.exists():
        pytest.skip("project .claude/settings.local.json absent (CI worktree)")

    data = _json.loads(settings_path.read_text())
    allow = data.get("permissions", {}).get("allow", [])

    live_names = await _live_tool_names()
    stale: list[str] = []
    wrong_surface: list[str] = []
    for server in ("sage", "sage_maint"):
        prefix = f"mcp__{server}__"
        for entry in (e for e in allow if e.startswith(prefix)):
            tool = entry[len(prefix) :]
            if tool not in live_names:
                stale.append(entry)
            elif SERVER_ASSIGNMENT.get(tool) != server:
                wrong_surface.append(entry)
    stale.sort()
    wrong_surface.sort()
    assert not stale, (
        f"Stale settings.local.json permission entries (no live MCP tool): "
        f"{stale}. Either rename the entries to the current tool name "
        "or remove them."
    )
    assert not wrong_surface, (
        f"settings.local.json permission entries naming a tool on a server it "
        f"is not registered on: {wrong_surface}. Move each entry to the server "
        "SERVER_ASSIGNMENT assigns the tool to."
    )


# ---------------------------------------------------------------------------
# Live catalog matches the SERVER_ASSIGNMENT roster
# ---------------------------------------------------------------------------


# Derived from SERVER_ASSIGNMENT in sage/_tool_naming.py — the target
# catalog after CAS-ADR-029. Sourcing from SERVER_ASSIGNMENT (rather than
# a hand-typed literal) closes the misclassification re-entry trap: a
# v6 → v7 amendment that updates SERVER_ASSIGNMENT automatically
# propagates here, and any drift between the table and the live catalog
# surfaces on either side as the same diff.

_EXPECTED_CATALOG: frozenset[str] = frozenset(SERVER_ASSIGNMENT.keys())


async def test_live_catalog_matches_expected_set() -> None:
    """The live MCP catalog equals the target set defined in
    SERVER_ASSIGNMENT after CAS-ADR-029's plural-noun + surface-prefix pass.

    Anti-coincidental check: the expected set is sourced from the
    SERVER_ASSIGNMENT table in ``sage/_tool_naming.py`` rather than from
    a hand-typed literal. A v6 → v7 misclassification re-entry (e.g.,
    re-adding `list_vaults` as unprefixed) would surface as the SAME diff
    on both sides — the live catalog and the table — which is the desired
    auditable failure mode.
    """
    live = await _live_tool_names()
    expected = set(_EXPECTED_CATALOG)
    missing = expected - live
    extra = live - expected
    assert not missing and not extra, (
        f"Live MCP catalog differs from the CAS-ADR-029 target set. "
        f"Missing from live: {sorted(missing)}; "
        f"Extra in live (not in SERVER_ASSIGNMENT): {sorted(extra)}."
    )
