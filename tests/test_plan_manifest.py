"""Keep recorded specification counts equal to active Markdown headings."""

import json
import re
from pathlib import Path

import pytest
from markdown_it import MarkdownIt

REPO_ROOT = Path(__file__).resolve().parent.parent


def _active_test_count(source: str) -> int:
    """Count TEST identifiers in headings at any depth, excluding retired specs."""
    tokens = MarkdownIt().parse(source)
    return sum(
        bool(re.search(r"\bTEST-[A-Za-z0-9_-]+", tokens[i + 1].content))
        and not re.search(r"\bRETIRED\b", tokens[i + 1].content)
        for i, token in enumerate(tokens)
        if token.type == "heading_open"
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("# TEST-A-001\n\n###### TEST-A-002\n", 2),
        ("## RETIRED TEST-A-003\n", 0),
        ("## TEST-A-004 — RETIRED\n", 0),
        ("Prose mentions TEST-A-005.\n", 0),
        ("```markdown\n# TEST-A-006\n```\n", 0),
        ("# Background\n", 0),
        ("", 0),
    ],
)
def test_active_test_heading_count(source: str, expected: int) -> None:
    """Only active specification headings contribute to the inventory."""
    assert _active_test_count(source) == expected


@pytest.mark.parametrize(
    "spec",
    json.loads((REPO_ROOT / "tests/test_plan_manifest.json").read_text())["test_specs"],
    ids=lambda spec: spec["path"],
)
def test_manifest_matches_active_specifications(spec: dict[str, object]) -> None:
    """Each listed file exists and its recorded count is current."""
    path = REPO_ROOT / str(spec["path"])
    assert path.is_file(), f"Missing test specification: {spec['path']}"
    observed = _active_test_count(path.read_text())
    assert spec["test_count"] == observed, (
        f"{spec['path']}: recorded={spec['test_count']}, observed={observed}"
    )
