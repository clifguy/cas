"""The review skill's section roster agrees wherever it is stated.

The canonical review skill names its sections three times: in the frontmatter
description the skill listing shows, in the tag list its output instructions
emit, and in the section headings themselves. A section added, removed or
renamed in one place and not the others leaves a reviewer walking a roster the
file does not hold, with every other gate green. The description is the one a
pointer mirrors, so its drift would also propagate.
"""

import re
from pathlib import Path

from tests.test_agent_instruction_pointers import REPO_ROOT, _frontmatter

SKILL = REPO_ROOT / ".claude/skills/cas-code-review/SKILL.md"
_ID = r"[A-Z]\d+"


def _expand(text: str) -> set[str]:
    """Collect section ids from prose, expanding ranges such as ``F1-F5``."""
    ids: set[str] = set()
    for start, end in re.findall(rf"\b({_ID})[-–]({_ID})\b", text):
        if start[0] == end[0]:
            ids.update(f"{start[0]}{n}" for n in range(int(start[1:]), int(end[1:]) + 1))
    ids.update(re.findall(rf"\b({_ID})\b", text))
    return ids


def _roster(path: Path) -> tuple[set[str], set[str], set[str]]:
    text = path.read_text(encoding="utf-8")
    description = str(_frontmatter(path)["description"])
    described = _expand(re.search(r"review a CAS commit.*?catalogued", description).group(0))
    tag_line = re.search(r"emit: the section tag \(([^)]*)\)", text)
    assert tag_line, "the output instructions no longer name the section tags"
    tagged = _expand(tag_line.group(1))
    headed = set(re.findall(rf"^## ({_ID}) — ", text, re.MULTILINE))
    return described, tagged, headed


def test_section_roster_agrees_across_description_tags_and_headings() -> None:
    described, tagged, headed = _roster(SKILL)
    assert headed, "no section headings found"
    assert described == headed, f"description {sorted(described)} vs headings {sorted(headed)}"
    assert tagged == headed, f"tag list {sorted(tagged)} vs headings {sorted(headed)}"


def test_roster_detects_a_heading_missing_from_the_description(tmp_path: Path) -> None:
    text = SKILL.read_text(encoding="utf-8")
    probe = tmp_path / "SKILL.md"
    probe.write_text(text + "\n## Z9 — A section the description does not name\n", encoding="utf-8")
    described, _, headed = _roster(probe)
    assert "Z9" in headed
    assert described != headed
