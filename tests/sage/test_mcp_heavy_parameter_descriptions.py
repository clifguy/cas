"""The heaviest parameter descriptions stay within the size they were cut to.

A parameter's description reaches the model in every client, including one that
simplifies the schema around it, so parameter prose is most of what a tool
costs. The parameters below carried the most of it, and each was cut to text
that changes how a caller builds the call or reads its result; maintainer
rationale and implementation detail live in the governing steering document or
ADR instead. Each ceiling holds the cut, so rationale that drifts back in is
refused here rather than noticed in a later measurement.

The ceilings bound one parameter's own ``description``, not its nested item
fields, which are shared with the REST request models.
"""

from __future__ import annotations

from typing import Final

import pytest

from tests.helpers.published_tool import published_tools

#: Ceiling on the published ``description`` of a top-level parameter, in
#: characters, keyed ``"<tool>.<parameter>"``.
HEAVY_PARAMETER_CEILINGS: Final[dict[str, int]] = {
    "update_lifecycles.items": 1600,
    "create_edges.items": 1700,
    "update_metadata.items": 1000,
    "bulk_ingest_document.files": 1100,
    "bulk_ingest_document.infer_edges": 1400,
    "ingest_document.dry_run": 1550,
    "search.query": 1300,
    "search.response_mode": 1100,
}


def _published_description(key: str) -> str | None:
    tool_name, _, param = key.partition(".")
    tool = published_tools().get(tool_name)
    if tool is None:
        return None
    prop = (tool.parameters.get("properties") or {}).get(param)
    if prop is None:
        return None
    return prop.get("description")


def test_heavy_parameter_ceilings_name_published_parameters() -> None:
    """Every ceiling names a described parameter of a registered tool.

    A renamed tool or parameter would otherwise leave its ceiling checking
    nothing.
    """
    unresolved = sorted(k for k in HEAVY_PARAMETER_CEILINGS if not _published_description(k))
    assert not unresolved, f"ceilings naming no described published parameter: {unresolved}"


@pytest.mark.parametrize("key", sorted(HEAVY_PARAMETER_CEILINGS))
def test_heavy_parameter_description_within_ceiling(key: str) -> None:
    description = _published_description(key) or ""
    ceiling = HEAVY_PARAMETER_CEILINGS[key]
    assert len(description) <= ceiling, (
        f"{key} description is {len(description)} chars (ceiling {ceiling})"
    )
