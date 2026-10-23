"""Every actual coverage response teaches the selection qualifiers it carries."""

import pytest

from tests.helpers.published_tool import published_text
from tests.sage.test_mcp_success_qualifier_disclosure import _reachable_schemas
from tests.sage.test_mcp_tool_conformance import (
    _SURFACES_BY_NAME,
    _find_operation,
    _load_spec,
    _mapped_tool_pairs,
    _resolve_expected_operation_id,
)


def coverage_emitters():
    found = set()
    for surface_name, tool_name in _mapped_tool_pairs():
        surface = _SURFACES_BY_NAME[surface_name]
        spec = _load_spec(surface.spec_path)
        operation = _find_operation(spec, _resolve_expected_operation_id(surface, tool_name))
        seen = set()
        for status, response in operation.get("responses", {}).items():
            if str(status).startswith("2"):
                schema = response.get("content", {}).get("application/json", {}).get("schema", {})
                list(_reachable_schemas(spec, schema, seen))
        if "SelectionSummary" in seen:
            found.add(tool_name)
    return found


def test_selection_disclosure_enrollment_follows_actual_response_graph():
    assert coverage_emitters() == {
        "get_document",
        "ingest_document",
        "search",
        "traverse",
        "update_lifecycles",
        "update_metadata",
        "list_pending_metadata",
        "read_projection",
        "read_section",
        "list_headings",
        "export_projection",
    }


@pytest.mark.parametrize("tool_name", sorted(coverage_emitters()))
def test_every_coverage_emitter_explains_selection_counts_and_boundary(tool_name):
    text = " ".join(published_text(tool_name).split())
    for clause in (
        "inspection.selection_summary",
        "complete|limited|interrupted",
        "selected_inputs/selected_tokens",
        "identity.tokenization",
        "established_occurrences",
        "retained occurrences",
        "lower-bound unless associations_complete",
        "boundary=last inspected location",
        "no inference coverage",
    ):
        assert clause in text, f"{tool_name} does not disclose {clause!r}"
