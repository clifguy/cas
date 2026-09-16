"""Validate staged source wiring, not live workflow behavior or installation."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROPOSED = ROOT / "docs/development/proposed"
EXPECTED = {"smoke-test", "deploy", "azure-deploy-review", "batch"}


def test_staged_declaration_is_complete_and_inactive() -> None:
    declaration = json.loads((PROPOSED / "specialized-package.json").read_text())
    assert declaration["schema_version"] == 1
    assert declaration["status"] == "inactive"
    assert declaration["release_version"] is None
    assert set(declaration["skills"]) == EXPECTED
    files = declaration["files"]
    assert len({row["source"] for row in files}) == len(files)
    assert len({row["target"] for row in files}) == len(files)
    mapping = {row["source"]: row["target"] for row in files}
    for name, entry in declaration["skills"].items():
        assert mapping[entry["entrypoint"]] == f".agents/skills/{name}/SKILL.md"
        assert entry["procedure"] in mapping
        assert entry["target"] == mapping[entry["entrypoint"]]
        assert not (ROOT / entry["target"]).exists()
        source = (ROOT / entry["entrypoint"]).read_text()
        assert entry["procedure"] in source
        assert entry["responsibility"] in source
        assert entry["operation"] in source
        assert "INACTIVE" in source
    assert declaration["external_dependencies"]
    assert not (ROOT / ".development-skills/project.json").exists()


def test_declared_local_references_survive_source_and_target_layouts() -> None:
    declaration = json.loads((PROPOSED / "specialized-package.json").read_text())
    mapping = {row["source"]: row["target"] for row in declaration["files"]}
    assert {
        "docs/development/operations/" + name + ".md"
        for name in ("smoke-test", "deploy", "azure-review", "batch")
    } <= set(mapping)
    reference_count = 0
    for source, target in mapping.items():
        path = ROOT / source
        assert path.is_file()
        assert not Path(source).is_absolute() and ".." not in Path(source).parts
        assert not Path(target).is_absolute() and ".." not in Path(target).parts
        for href in re.findall(r"\]\(([^)]+)\)", path.read_text()):
            if "://" in href or href.startswith("#"):
                continue
            reference_count += 1
            relative = href.split("#", 1)[0]
            referenced = (path.parent / relative).resolve().relative_to(ROOT).as_posix()
            assert referenced in mapping, f"unpackaged reference: {source}: {href}"
            actual_target = (ROOT / target).parent / relative
            expected_target = ROOT / mapping[referenced]
            assert actual_target.resolve() == expected_target.resolve()
    assert reference_count >= 8, "runtime links must be exercised, not an empty traversal"


def test_specialized_obligation_roster_covers_procedure_sections() -> None:
    roster = json.loads((PROPOSED / "required-obligations.json").read_text())
    mapping = json.loads((PROPOSED / "obligations.json").read_text())
    required = {row["id"]: row for row in roster["required_obligations"]}
    actual = {row["id"]: row for row in mapping["obligations"]}
    for operation in ("smoke-test", "deploy", "batch"):
        sections = [
            row for row in required.values() if row["source_id"] == "specialized-" + operation
        ]
        assert sections, f"unrostered procedure: {operation}"
        for row in sections:
            migrated = actual[row["id"]]
            assert migrated["source_section"] == row["source_section"]
            assert migrated["binding_ids"] == ["cas-" + operation]
            assert migrated["operations"] == [operation]
            assert migrated["proposed_authority"] == f"docs/development/operations/{operation}.md"
