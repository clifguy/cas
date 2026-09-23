"""Every tool, and every surface, fits the size its clients present to a model.

A client forwards a tool's description and input schema to its model, and pays
for both before the first call. Clients differ in what they forward: some pass
the published schema whole, others simplify it first, so a tool's presented
size is measured in each form ``scripts.measure_mcp_surface`` renders and held
at the larger. The description-only budget in
``test_mcp_tool_description_budget`` bounds one client's truncation point; this
gate bounds what every client pays.

Ceilings are set from the measurement, not guessed: each named ceiling sits a
few percent above the tool's current size, and one more than
``SLACK_TOLERANCE`` above it fails as slack, so a cut lowers its ceiling in the
same change. A tool without a named ceiling is held to
``DEFAULT_TOOL_CEILING`` from registration, and its surface's ceiling is
raised by its measured size in the same change that adds it. The surface
totals bind growth spread thinly across many tools, which no per-tool ceiling
sees. A surface total sums each tool's larger form, so it is an upper bound on
what any one client presents, not a figure a particular client pays.

Text earns a place in a published description only if it changes how a caller
builds a call or reads its result; the governing steering document or ADR holds
the rest. A tool that outgrows its ceiling is cut against that rule before its
ceiling is raised.
"""

from __future__ import annotations

from typing import Any, Final

import pytest

from scripts.dump_mcp_catalog import build_catalog
from scripts.measure_mcp_surface import FORMS, measure
from tests.sage.mcp_surface_pin import EXPECTED_SURFACE

#: Ceiling, in characters of presented size, for every tool not named below.
DEFAULT_TOOL_CEILING: Final[int] = 4_000

#: Ceilings for the tools whose presented size exceeds the default.
TOOL_CEILINGS: Final[dict[str, int]] = {
    "ingest_document": 16_250,
    "search": 14_750,
    "bulk_ingest_document": 11_250,
    "create_edges": 9_000,
    "update_lifecycles": 8_750,
    "update_metadata": 7_750,
    "restore_vault_source_file": 4_750,
    "update_vault_config": 4_750,
}

#: Ceiling on the summed presented size of every tool a surface registers.
SURFACE_CEILINGS: Final[dict[str, int]] = {
    "sage": 104_000,
    "sage_maint": 34_500,
}

#: A named or surface ceiling further than this above its measurement is slack.
SLACK_TOLERANCE: Final[float] = 1.10


def _presented(row: dict[str, int]) -> int:
    """A tool's presented size: the largest of its per-form sizes."""
    return max(row[form] for form in FORMS)


@pytest.fixture(scope="module")
def report() -> dict[str, dict[str, Any]]:
    return measure(build_catalog())


def _tool_rows(report: dict[str, dict[str, Any]]) -> dict[str, dict[str, int]]:
    return {name: row for data in report.values() for name, row in data["tools"].items()}


def _surface_total(report: dict[str, dict[str, Any]], surface: str) -> int:
    """The sum of each tool's larger form: a bound on what any client presents."""
    return sum(_presented(row) for row in report[surface]["tools"].values())


def test_measured_roster_is_the_surface_assignment(report: dict[str, dict[str, Any]]) -> None:
    """The gate measures every registered tool, on the surface that registers it."""
    measured = {(name, surface) for surface, data in report.items() for name in data["tools"]}
    assert measured == set(EXPECTED_SURFACE.items())


@pytest.mark.parametrize("name", sorted(EXPECTED_SURFACE))
def test_tool_within_presented_size_ceiling(name: str, report: dict[str, dict[str, Any]]) -> None:
    row = _tool_rows(report)[name]
    ceiling = TOOL_CEILINGS.get(name, DEFAULT_TOOL_CEILING)
    assert _presented(row) <= ceiling, (
        f"{name} presents {_presented(row)} chars ({row}); ceiling {ceiling}. Cut text that "
        f"does not change how a caller builds the call or reads the result before raising it."
    )


@pytest.mark.parametrize("surface", sorted(SURFACE_CEILINGS))
def test_surface_within_total_ceiling(surface: str, report: dict[str, dict[str, Any]]) -> None:
    total = _surface_total(report, surface)
    assert total <= SURFACE_CEILINGS[surface], (
        f"{surface} presents {total} chars in total; ceiling {SURFACE_CEILINGS[surface]}. "
        f"Cut first; a new tool raises this ceiling by its measured size in the same "
        f"change, and a cut lowers it."
    )


def test_named_ceilings_name_registered_tools_above_the_default(
    report: dict[str, dict[str, Any]],
) -> None:
    """A named ceiling names a registered tool and is not redundant with the default.

    A renamed tool would otherwise leave its ceiling checking nothing, and an
    entry at or below the default adds nothing the default does not hold.
    """
    unregistered = sorted(set(TOOL_CEILINGS) - set(EXPECTED_SURFACE))
    assert not unregistered, f"ceilings naming no registered tool: {unregistered}"
    redundant = sorted(n for n, c in TOOL_CEILINGS.items() if c <= DEFAULT_TOOL_CEILING)
    assert not redundant, f"ceilings at or below the default: {redundant}"
    assert set(SURFACE_CEILINGS) == set(report)


def test_ceilings_are_not_slack(report: dict[str, dict[str, Any]]) -> None:
    """Every named and surface ceiling stays close to what it measures.

    A ceiling far above its tool stops being a measurement and admits growth no
    one decided on, so a cut lowers the ceiling it made slack.
    """
    rows = _tool_rows(report)
    slack = [
        f"{name}: ceiling {ceiling}, presents {_presented(rows[name])}"
        for name, ceiling in TOOL_CEILINGS.items()
        if ceiling > _presented(rows[name]) * SLACK_TOLERANCE
    ]
    slack += [
        f"surface {surface}: ceiling {ceiling}, presents {_surface_total(report, surface)}"
        for surface, ceiling in SURFACE_CEILINGS.items()
        if ceiling > _surface_total(report, surface) * SLACK_TOLERANCE
    ]
    assert not slack, "ceilings more than 10% above their measurement: " + "; ".join(slack)


@pytest.mark.parametrize(
    ("schema", "larger"),
    [
        (
            {
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "$defs": {"Unused": {"description": "d" * 400, "type": "string"}},
            },
            "raw",
        ),
        ({"type": "object", "properties": {"x": {"type": "string"}}}, "claude_code"),
    ],
    ids=["raw-larger", "claude-code-larger"],
)
def test_presented_size_takes_the_larger_form(schema: dict[str, Any], larger: str) -> None:
    """The presented size is the larger form, whichever that is.

    The raw form carries definitions another client drops, and the simplified
    form adds a schema declaration the raw form lacks, so either can be the
    larger. A gate reading one form would under-count the other client.
    """
    catalog = {"surfaces": {"s": [{"name": "t", "description": "d", "inputSchema": schema}]}}
    row = measure(catalog)["s"]["tools"]["t"]
    assert row[larger] == max(row.values())
    assert row[larger] > min(row.values())
    assert _presented(row) == row[larger]
