"""Catalog-level conformance tests for the SAGE MCP tool naming scheme.

Three gates over the live FastMCP catalog:

- ``test_no_legacy_prefix`` — every registered tool name omits the
  pre-rename ``sage_``, ``sage_admin_``, and ``app_`` inner prefixes
  that the two-server design in CAS-ADR-034 made vestigial.
- ``test_rename_mapping_complete`` — every entry on the value side of
  ``RENAME_MAPPING`` is present in the live catalog. Five invocation
  probes across the read / write / admin classes pin handler binding
  so a typo'd registration cannot pass on presence alone.
- ``test_verb_category_compliance`` — every registered tool name
  begins with a verb in ``CANONICAL_VERBS`` (after stripping any
  member of ``COMPOUND_PREFIXES``). Anchored to CAS-ADR-033.

The tests enumerate the live catalog (``mcp.list_tools()``); they do
not parametrize over a static expected list. A stale expected list
would let the gate pass while the registration drifts; live
enumeration surfaces drift on the next test run.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest

import sage.mcp_server as _mcp
from sage._tool_rename_mapping import (
    CANONICAL_VERBS,
    COMPOUND_PREFIXES,
    RENAME_MAPPING,
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
# Test 2 — every rename target is registered (presence + invocation probes)
# ---------------------------------------------------------------------------


async def test_rename_mapping_targets_registered() -> None:
    """Every value on the right side of ``RENAME_MAPPING`` is registered.

    Anti-coincidental check: the assertion compares the full target set
    to the live catalog (with the legacy prefix asserted absent in
    Test 1, the live catalog must equal the target set). A typo in a
    rename target would surface as a missing registration here, not
    just as a silent gap in the conformance allowlist.
    """
    targets = set(RENAME_MAPPING.values())
    names = await _live_tool_names()
    missing = sorted(targets - names)
    extras = sorted(names - targets)
    assert not missing, (
        f"Rename targets missing from the live MCP catalog: {missing}. "
        "Every value side of RENAME_MAPPING in sage/_tool_rename_mapping.py "
        "must correspond to a registered FastMCP tool."
    )
    assert not extras, (
        f"Live catalog contains tools not in RENAME_MAPPING targets: {extras}. "
        "Add the new tool to RENAME_MAPPING and SERVER_ASSIGNMENT, or remove "
        "the registration."
    )


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
    ("admin_list_vaults", {}),
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
    """A representative subset of rename targets is invokable end-to-end.

    Anti-coincidental check: a typo in a tool's MCP-registered name
    could let registration succeed under the wrong key; pure-presence
    assertion in ``test_rename_mapping_targets_registered`` would still
    pass. The invocation probe forces FastMCP to dispatch to the
    underlying handler; a wrong key would surface as a tool-not-found
    error here.

    The probes target the read, write, and maintenance classes so a
    rename mistake confined to one class still trips at least one
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
# Test 5 — verb-category compliance across the live catalog
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
# Module-level invariants over the mapping module itself
# ---------------------------------------------------------------------------


def test_server_assignment_covers_every_rename_target() -> None:
    """Every value side of ``RENAME_MAPPING`` is in ``SERVER_ASSIGNMENT``.

    The mapping module's import-time assert pins this too, but we
    re-test at the pytest layer to keep the failure mode legible if
    someone adds a rename target without a server assignment.
    """
    rename_targets = set(RENAME_MAPPING.values())
    assigned = set(SERVER_ASSIGNMENT.keys())
    missing = sorted(rename_targets - assigned)
    assert not missing, f"Rename targets without a SERVER_ASSIGNMENT entry: {missing}."


def test_rename_mapping_targets_use_canonical_verbs() -> None:
    """Every rename target itself begins with a canonical verb.

    Anti-coincidental check: the live-catalog gate above can only fail
    after the rename lands. This test catches the *mapping table*
    being wrong (e.g. a target with a non-canonical verb) as soon as
    the mapping module is imported, independent of the live catalog.
    """
    bad = sorted(t for t in RENAME_MAPPING.values() if extract_verb(t) not in CANONICAL_VERBS)
    assert not bad, f"RENAME_MAPPING contains targets whose verb is not in CANONICAL_VERBS: {bad}."


# ---------------------------------------------------------------------------
# Test 3 — alias layer rewrites old names to new before dispatch
# ---------------------------------------------------------------------------


from sage._tool_rename_mapping import REMOVED_TOOLS  # noqa: E402
from sage.mcp_server import _LoggingFastMCP  # noqa: E402


@pytest.mark.parametrize(
    ("old_name", "new_name"),
    sorted(RENAME_MAPPING.items()),
)
async def test_alias_layer_rewrites_old_name_to_new(
    old_name: str, new_name: str, monkeypatch
) -> None:
    """``_LoggingFastMCP.call_tool`` rewrites every old name to its new target before dispatch.

    Anti-coincidental check: parametrize over the full ``RENAME_MAPPING``
    table; record the name actually passed to the parent FastMCP
    ``call_tool`` (rather than asserting against a static expected).
    A mis-wired alias (e.g. all old names mapping to the same target)
    would surface as a mismatch on every parametrization that pointed
    to a different new name.
    """
    captured: list[str] = []

    async def fake_super_call(self, name, arguments):
        captured.append(name)
        return {"echoed": name, "args": arguments}

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.call_tool", fake_super_call)
    mcp = _LoggingFastMCP("test")
    result = await mcp.call_tool(old_name, {"probe": True})

    assert captured == [new_name], (
        f"alias rewrite for {old_name!r} dispatched to {captured!r}; "
        f"expected exactly [{new_name!r}]"
    )
    assert result == {"echoed": new_name, "args": {"probe": True}}


async def test_alias_layer_passes_new_names_through_unchanged(monkeypatch) -> None:
    """New names dispatch directly without rewrite.

    Anti-coincidental check: a buggy alias layer might rewrite *every*
    incoming name. Calling with the new name and asserting no rewrite
    catches that.
    """
    captured: list[str] = []

    async def fake_super_call(self, name, arguments):
        captured.append(name)
        return {"ok": True}

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.call_tool", fake_super_call)
    mcp = _LoggingFastMCP("test")

    # Pick a few representative new names from each class. Per CAS-ADR-029
    # (CAS-ADR-029 v4), the read/write/maintenance probes use the post-v7
    # canonical names (verify_hashes plural-noun, admin_migrate_vault
    # admin-prefixed) rather than their legacy pre-v7 forms.
    for new_name in ("search", "get_document", "verify_hashes", "admin_migrate_vault"):
        await mcp.call_tool(new_name, {})

    assert captured == ["search", "get_document", "verify_hashes", "admin_migrate_vault"]


# ---------------------------------------------------------------------------
# Test 4 — deprecation warning emitted for old-name invocations
# ---------------------------------------------------------------------------


import logging  # noqa: E402


async def test_alias_emits_deprecation_warning_with_both_names(caplog, monkeypatch) -> None:
    """An old-name call emits a WARNING naming both the old and new names.

    Anti-coincidental check: the assertion verifies *both* names
    appear in the warning message. A hard-coded message that names
    only the old or only the new name would fail this assertion.
    Parametrize across a representative sample to ensure the message
    formatting is per-call, not a literal constant.
    """

    async def fake_super_call(self, name, arguments):
        return {"ok": True}

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.call_tool", fake_super_call)
    mcp = _LoggingFastMCP("test")

    # NOTE: these pairs intentionally use the *old* names on the left;
    # the test sweep that mass-renames legacy references must not touch
    # them. Keep this list synchronized with RENAME_MAPPING by hand —
    # add a representative across read / write / admin classes.
    for old_name, new_name in [
        ("sage_discover", "search"),
        ("sage_get_document", "get_document"),
        ("sage_admin_migrate_vault", "admin_migrate_vault"),
        ("app_batch_ingest", "bulk_ingest_document"),
    ]:
        with caplog.at_level(logging.WARNING, logger="sage.mcp_server"):
            caplog.clear()
            await mcp.call_tool(old_name, {})

        warning_messages = [
            rec.getMessage()
            for rec in caplog.records
            if rec.name == "sage.mcp_server" and rec.levelno == logging.WARNING
        ]
        assert any(old_name in msg and new_name in msg for msg in warning_messages), (
            f"Deprecation warning for {old_name!r} → {new_name!r} did not name "
            f"both old and new. WARNING records: {warning_messages}"
        )


async def test_new_name_call_emits_no_deprecation_warning(caplog, monkeypatch) -> None:
    """A new-name call does NOT emit the deprecation WARNING.

    Anti-coincidental check: pairs with the previous test — if the
    warning emission is unconditional (a wiring bug), this test would
    fail.
    """

    async def fake_super_call(self, name, arguments):
        return {"ok": True}

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.call_tool", fake_super_call)
    mcp = _LoggingFastMCP("test")

    with caplog.at_level(logging.WARNING, logger="sage.mcp_server"):
        await mcp.call_tool("search", {})
        await mcp.call_tool("get_document", {})

    deprecation_records = [
        rec
        for rec in caplog.records
        if rec.name == "sage.mcp_server"
        and rec.levelno == logging.WARNING
        and "deprecated" in rec.getMessage()
    ]
    assert not deprecation_records, (
        f"new-name calls emitted deprecation warnings: {deprecation_records}"
    )


# ---------------------------------------------------------------------------
# REMOVED_TOOLS handling
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test 7 — no docstring cross-references the renamed tools by their old name
# ---------------------------------------------------------------------------


async def test_no_docstring_references_legacy_tool_name() -> None:
    """No tool's docstring (live ``description``) names another tool by its old name.

    Iterates the live MCP catalog's tool descriptions and asserts each
    description avoids every member of ``RENAME_MAPPING``'s key set.

    Anti-coincidental check: the assertion only fires when a docstring
    contains an old name as a *word* (regex boundary). Legitimate
    substrings — ``sage_core`` (a conformance-surface name), ``sage_admin``
    (an MCP server name), ``sage_vault`` (a path prefix) — do not match
    because they are not in ``RENAME_MAPPING`` keys. The check is
    therefore precise: only deprecated cross-references trip it.
    """
    old_names = set(RENAME_MAPPING.keys())
    tools = await _mcp.mcp.list_tools()
    offending: list[tuple[str, list[str]]] = []
    for tool in tools:
        desc = tool.description or ""
        # Find tokens; each token is a maximal word that includes
        # underscores. A docstring of the form "use `sage_traverse` to
        # walk further" tokenizes to "use", "sage_traverse", "to",
        # "walk", "further" — the token check catches sage_traverse
        # while leaving sage_admin (with a trailing word boundary)
        # alone unless it appears as a standalone token.
        tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", desc))
        old_in_doc = sorted(old_names & tokens)
        if old_in_doc:
            offending.append((tool.name, old_in_doc))
    assert not offending, (
        "Tool descriptions still cross-reference renamed tools by their old name:\n"
        + "\n".join(f"  {name}: {refs}" for name, refs in offending)
    )


# ---------------------------------------------------------------------------
# REMOVED_TOOLS handling
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test 9 — settings.local.json permission entries align with live catalog
# ---------------------------------------------------------------------------


async def test_settings_local_permissions_match_live_catalog() -> None:
    """Every ``mcp__sage__<name>`` entry in the project settings names a registered tool.

    Reads ``.claude/settings.local.json`` (project root) and asserts
    every ``mcp__sage__<X>`` permission entry has ``X`` registered in
    the live MCP catalog. Catches drift in either direction:
    permission entries for renamed-away tools, or unauthorized new
    tools.

    Anti-coincidental check: the assertion compares against the live
    catalog rather than a static expected list; if the catalog drifts
    or a permission goes stale, the next test run surfaces it.
    """
    import json as _json
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    settings_path = repo_root / ".claude" / "settings.local.json"
    if not settings_path.exists():
        pytest.skip("project .claude/settings.local.json absent (CI worktree)")

    data = _json.loads(settings_path.read_text())
    allow = data.get("permissions", {}).get("allow", [])
    sage_entries = [e for e in allow if e.startswith("mcp__sage__")]

    live_names = await _live_tool_names()
    stale = sorted(e for e in sage_entries if e[len("mcp__sage__") :] not in live_names)
    assert not stale, (
        f"Stale settings.local.json permission entries (no live MCP tool): "
        f"{stale}. Either rename the entries to the current tool name "
        "or remove them."
    )


# ---------------------------------------------------------------------------
# REMOVED_TOOLS handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("removed_name", sorted(REMOVED_TOOLS))
async def test_removed_tool_returns_envelope_error(removed_name: str, caplog, monkeypatch) -> None:
    """Calling a removed tool returns a ``tool_removed`` envelope without dispatch.

    Anti-coincidental check: monkeypatch the parent ``call_tool`` so
    the test would FAIL if the removed-name path accidentally falls
    through to dispatch.
    """
    dispatched: list[str] = []

    async def fake_super_call(self, name, arguments):
        dispatched.append(name)
        return {"unexpected": True}

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.call_tool", fake_super_call)
    mcp = _LoggingFastMCP("test")

    with caplog.at_level(logging.WARNING, logger="sage.mcp_server"):
        result = await mcp.call_tool(removed_name, {})

    assert not dispatched, f"removed tool {removed_name} reached dispatch: {dispatched}"
    # Result should be the production-shape envelope; decode and check error code.
    assert isinstance(result, list) and len(result) == 1
    envelope = json.loads(result[0].text)
    assert envelope.get("error") == "tool_removed"
    assert removed_name in envelope.get("message", "")


# ---------------------------------------------------------------------------
# Post-CAS-ADR-029 catalog conformance (CAS-ADR-029 v4)
# ---------------------------------------------------------------------------


# Derived from SERVER_ASSIGNMENT in sage/_tool_rename_mapping.py — the
# 34-tool target catalog after CAS-ADR-029. Sourcing from SERVER_ASSIGNMENT
# (rather than a hand-typed literal) closes the misclassification
# re-entry trap: a v6 → v7 amendment that updates SERVER_ASSIGNMENT
# automatically propagates here, and any drift between the table and
# the live catalog surfaces on either side as the same diff.

_EXPECTED_T0240_CATALOG: frozenset[str] = frozenset(SERVER_ASSIGNMENT.keys())


async def test_post_t0240_catalog_matches_expected_set() -> None:
    """The live MCP catalog equals the 34-tool target set defined in
    SERVER_ASSIGNMENT after CAS-ADR-029's plural-noun + admin-prefix pass.

    Anti-coincidental check: the expected set is sourced from the
    SERVER_ASSIGNMENT table in ``sage/_tool_rename_mapping.py`` rather
    than from a hand-typed literal. A v6 → v7 misclassification re-entry
    (e.g., re-adding `list_vaults` as unprefixed) would surface as the
    SAME diff on both sides — the live catalog and the table — which is
    the desired auditable failure mode.
    """
    live = await _live_tool_names()
    expected = set(_EXPECTED_T0240_CATALOG)
    missing = expected - live
    extra = live - expected
    assert not missing and not extra, (
        f"Live MCP catalog differs from the T-0240 target set. "
        f"Missing from live: {sorted(missing)}; "
        f"Extra in live (not in SERVER_ASSIGNMENT): {sorted(extra)}."
    )


async def test_old_names_absent_from_canonical_catalog() -> None:
    """Every key in RENAME_MAPPING is absent from the live MCP catalog.

    Anti-coincidental check: pairs with
    ``test_rename_mapping_targets_registered`` (which asserts targets
    are present). A registration that accidentally exposes BOTH the
    old and the new name would pass that test but break the
    surface-reduction goal of CAS-ADR-029 v4. This test catches that
    case by asserting old names resolve only via the alias middleware,
    not as first-class catalog entries.
    """
    live = await _live_tool_names()
    leaked = sorted(old for old in RENAME_MAPPING if old in live)
    assert not leaked, (
        f"Old names appear in the live canonical catalog (should resolve "
        f"only via the alias middleware): {leaked}."
    )
