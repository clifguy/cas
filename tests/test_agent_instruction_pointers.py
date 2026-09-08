"""Keep agent entry points connected to their canonical instructions."""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_SKILL = Path(".claude/skills/cas-code-review/SKILL.md")
SKILL_POINTER = Path(".agents/skills/cas-code-review/SKILL.md")


def _frontmatter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"Missing frontmatter: {path}"
    metadata = yaml.safe_load(text.split("---", 2)[1])
    assert isinstance(metadata, dict), f"Invalid frontmatter: {path}"
    return metadata


@pytest.mark.parametrize("field", ["name", "description"])
def test_skill_pointer_metadata_matches_canonical(field: str) -> None:
    canonical = _frontmatter(REPO_ROOT / CANONICAL_SKILL)
    pointer = _frontmatter(REPO_ROOT / SKILL_POINTER)
    assert field in canonical, f"Canonical skill is missing {field}"
    assert pointer.get(field) == canonical[field], f"Skill pointer {field} drifted"


@pytest.mark.parametrize(
    ("pointer", "canonical"),
    [(Path("AGENTS.md"), Path("CLAUDE.md")), (SKILL_POINTER, CANONICAL_SKILL)],
)
def test_instruction_pointer_targets_canonical(pointer: Path, canonical: Path) -> None:
    source = REPO_ROOT / pointer
    links = re.findall(r"\[[^\]]+\]\(([^)]+)\)", source.read_text(encoding="utf-8"))
    assert len(links) == 1, f"Expected one canonical instruction link in {pointer}"
    target = (source.parent / links[0]).resolve()
    assert target == (REPO_ROOT / canonical).resolve(), f"Wrong target in {pointer}"
    assert target.is_file(), f"Missing canonical instructions: {canonical}"
