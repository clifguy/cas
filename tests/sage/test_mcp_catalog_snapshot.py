"""The committed MCP tool catalog matches what the servers publish (CAS-ADR-008).

``docs/fs/sage/sage_mcp_tools.catalog.json`` is the MCP surfaces' published
contract in committed form: the substrate manifest lists it, the contract-diff
detector compares it across revisions, and the change-record gate treats an edit
to it as a contract change. All three are only as good as the file's agreement
with the servers, which generate their ``tools/list`` from code. This gate holds
that agreement, so a change to a tool's signature or docstring cannot land
without the catalog moving with it.
"""

from __future__ import annotations

import json

from scripts.dump_mcp_catalog import CATALOG_PATH, build_catalog, render_catalog


def test_committed_catalog_matches_both_surfaces() -> None:
    assert CATALOG_PATH.exists(), (
        f"{CATALOG_PATH} is missing; run python -m scripts.dump_mcp_catalog --write"
    )
    expected = render_catalog(build_catalog())

    assert CATALOG_PATH.read_text(encoding="utf-8") == expected, (
        "the committed MCP tool catalog no longer matches the servers; run "
        "python -m scripts.dump_mcp_catalog --write, then add a change record "
        "under docs/fs/changes/unreleased/"
    )


def test_catalog_covers_both_surfaces_with_real_tools() -> None:
    """A dump that silently emitted no tools would match an empty committed file."""
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))

    assert set(catalog["surfaces"]) == {"sage", "sage_maint"}
    names = {tool["name"] for tool in catalog["surfaces"]["sage"]}
    maint_names = {tool["name"] for tool in catalog["surfaces"]["sage_maint"]}
    assert {"search", "get_document", "ingest_document"} <= names
    assert "reload_vault" in maint_names
    assert not names & maint_names, "a tool is listed on both surfaces"
    for tools in catalog["surfaces"].values():
        for tool in tools:
            assert tool["inputSchema"].get("type") == "object", tool["name"]
