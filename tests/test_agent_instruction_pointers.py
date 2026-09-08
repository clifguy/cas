"""Check pointer structure and metadata, not an agent's compliance with prose."""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_SKILL = Path(".agents/skills/cas-code-review/SKILL.md")


def _frontmatter(path: Path) -> dict[str, object]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0] == "---", f"Missing frontmatter: {path}"
    assert "---" in lines[1:], f"Missing closing frontmatter delimiter: {path}"
    end = lines.index("---", 1)
    metadata = yaml.safe_load("\n".join(lines[1:end]))
    assert isinstance(metadata, dict), f"Invalid frontmatter: {path}"
    return metadata


def _skill_pairs(root: Path) -> list[tuple[Path, Path]]:
    pointers = sorted((root / ".agents").rglob("SKILL.md"))
    assert pointers, "No agent skill pointers discovered"
    assert root / REQUIRED_SKILL in pointers, "Required review skill pointer is missing"
    return [(p, root / ".claude" / p.relative_to(root / ".agents")) for p in pointers]


def _check_metadata(pointer: Path, canonical: Path) -> None:
    assert canonical.is_file(), f"Missing canonical skill: {canonical}"
    metadata = _frontmatter(pointer)
    expected = _frontmatter(canonical)
    assert set(metadata) == {"name", "description"}, f"Unexpected pointer metadata: {pointer}"
    for field in ("name", "description"):
        assert isinstance(expected.get(field), str) and expected[field], (
            f"Missing canonical {field}: {canonical}"
        )
        assert metadata[field] == expected[field], f"Pointer {field} drifted: {pointer}"


def _check_instruction(pointer: Path, canonical: Path) -> None:
    text = pointer.read_text(encoding="utf-8")
    lines = text.splitlines()
    if lines and lines[0] == "---":
        assert "---" in lines[1:], f"Missing closing frontmatter delimiter: {pointer}"
        lines = lines[lines.index("---", 1) + 1 :]
    body = "\n".join(lines)
    assert len(body.encode("utf-8")) <= 600, f"Pointer body exceeds 600 bytes: {pointer}"
    assert not re.search(r"^\s*(?:`{3,}|~{3,})", body, re.MULTILINE), (
        f"Fenced code is not allowed in pointer body: {pointer}"
    )
    headings = [line for line in lines if re.match(r"^\s*#{1,6}\s", line)]
    nonempty = [line for line in lines if line.strip()]
    assert not headings or (
        len(headings) == 1 and headings[0].startswith("# ") and headings[0] == nonempty[0]
    ), f"Section headings are not allowed in pointer body: {pointer}"
    # This fixed instruction form is a structural guard, not a semantic prose checker.
    targets = re.findall(
        r"^Read and follow (?:the complete )?\[[^\]\n]+\]\(([^)\n]+)\)", text, re.MULTILINE
    )
    assert len(targets) == 1, f"Expected one canonical read-and-follow instruction: {pointer}"
    target = (pointer.parent / targets[0]).resolve()
    assert target == canonical.resolve(), f"Wrong instruction target: {pointer}"
    assert target.is_file(), f"Missing canonical instructions: {canonical}"


def _check_skill_tree(root: Path) -> None:
    for pointer, canonical in _skill_pairs(root):
        _check_metadata(pointer, canonical)
        _check_instruction(pointer, canonical)


def test_skill_pointers_follow_canonical_contract() -> None:
    _check_skill_tree(REPO_ROOT)


def test_repository_instruction_pointer() -> None:
    _check_instruction(REPO_ROOT / "AGENTS.md", REPO_ROOT / "CLAUDE.md")


def _write_skill_pair(root: Path, name: str) -> tuple[Path, Path]:
    pointer = root / ".agents" / "skills" / name / "SKILL.md"
    canonical = root / ".claude" / "skills" / name / "SKILL.md"
    metadata = f"---\nname: {name}\ndescription: Review --- source changes\n"
    for path in (pointer, canonical):
        path.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text(metadata + "version: 1\n---\nCanonical procedure.\n", encoding="utf-8")
    pointer.write_text(
        metadata + f"---\nRead and follow [canonical](../../../.claude/skills/{name}/SKILL.md).\n",
        encoding="utf-8",
    )
    return pointer, canonical


@pytest.mark.parametrize(
    ("defect", "expected_error"),
    [
        ("negation", "Expected one canonical read-and-follow instruction"),
        ("bare-reference", "Expected one canonical read-and-follow instruction"),
        ("version", "Unexpected pointer metadata"),
        ("name", "Pointer name drifted"),
        ("description", "Pointer description drifted"),
        ("suffix-drift", "Pointer description drifted"),
        ("missing-target", "Missing canonical skill"),
        ("wrong-target", "Wrong instruction target"),
        ("missing-link", "Expected one canonical read-and-follow instruction"),
        ("inline-skill", "Section headings are not allowed"),
        ("long-body", "Pointer body exceeds 600 bytes"),
        ("fenced-instruction", "Fenced code is not allowed"),
        ("empty-tree", "No agent skill pointers discovered"),
    ],
)
def test_skill_tree_rejects_defective_pointers(
    tmp_path: Path, defect: str, expected_error: str
) -> None:
    required, _ = _write_skill_pair(tmp_path, "cas-code-review")
    pointer, canonical = _write_skill_pair(tmp_path, "another-skill")
    text = pointer.read_text(encoding="utf-8")
    if defect == "negation":
        pointer.write_text(text.replace("Read and follow", "Do NOT read"), encoding="utf-8")
    elif defect == "bare-reference":
        pointer.write_text(text.replace("Read and follow", "See"), encoding="utf-8")
    elif defect == "version":
        pointer.write_text(text.replace("name:", "version: stale\nname:"), encoding="utf-8")
    elif defect in {"name", "description"}:
        pointer.write_text(
            re.sub(rf"^{defect}:.*$", f"{defect}: wrong", text, flags=re.MULTILINE),
            encoding="utf-8",
        )
    elif defect == "suffix-drift":
        pointer.write_text(
            text.replace("--- source changes", "--- unrelated changes"), encoding="utf-8"
        )
    elif defect == "missing-target":
        canonical.unlink()
    elif defect == "wrong-target":
        pointer.write_text(
            text.replace("another-skill/SKILL.md", "cas-code-review/SKILL.md"), encoding="utf-8"
        )
    elif defect == "missing-link":
        pointer.write_text(text.split("Read and follow")[0], encoding="utf-8")
    elif defect == "inline-skill":
        pointer.write_text(
            text + "\n## Copied procedure\nExecute an inlined procedure.\n", encoding="utf-8"
        )
    elif defect == "long-body":
        pointer.write_text(text + "Copied procedure. " * 50, encoding="utf-8")
    elif defect == "fenced-instruction":
        pointer.write_text(
            text.replace("Read and follow", "```\nRead and follow") + "```\n", encoding="utf-8"
        )
    else:
        pointer.unlink()
        required.unlink()
    with pytest.raises(AssertionError, match=re.escape(expected_error)):
        _check_skill_tree(tmp_path)


def test_skill_tree_accepts_additional_pointer_and_supplementary_link(tmp_path: Path) -> None:
    _write_skill_pair(tmp_path, "cas-code-review")
    pointer, _ = _write_skill_pair(tmp_path, "another-skill")
    with pointer.open("a", encoding="utf-8") as stream:
        stream.write("Also see [README](../../../README.md).\n")
    _check_skill_tree(tmp_path)


def test_repository_pointer_accepts_supplementary_link(tmp_path: Path) -> None:
    canonical = tmp_path / "CLAUDE.md"
    canonical.write_text("Repository guide.\n", encoding="utf-8")
    pointer = tmp_path / "AGENTS.md"
    pointer.write_text(
        "Read and follow [guide](CLAUDE.md).\nAlso see [README](README.md).\n", encoding="utf-8"
    )
    _check_instruction(pointer, canonical)


@pytest.mark.parametrize("body_bytes", [600, 601])
def test_pointer_body_byte_limit(tmp_path: Path, body_bytes: int) -> None:
    canonical = tmp_path / "CLAUDE.md"
    canonical.write_text("Guide.\n", encoding="utf-8")
    pointer = tmp_path / "AGENTS.md"
    body = "Read and follow [guide](CLAUDE.md).\n"
    remaining = body_bytes - len(body.encode("utf-8"))
    body += "é" * (remaining // 2) + "x" * (remaining % 2)
    pointer.write_text(body, encoding="utf-8")
    if body_bytes == 600:
        _check_instruction(pointer, canonical)
    else:
        with pytest.raises(AssertionError, match="Pointer body exceeds 600 bytes"):
            _check_instruction(pointer, canonical)
