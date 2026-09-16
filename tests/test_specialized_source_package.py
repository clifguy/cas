"""Validate staged source wiring, not live workflow behavior or installation."""

import json
import re
import shutil
from pathlib import Path

import pytest

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
        source = (ROOT / entry["entrypoint"]).read_text()
        assert entry["procedure"] in source
        assert entry["responsibility"] in source
        assert entry["operation"] in source
        assert "INACTIVE" in source
    assert declaration["external_dependencies"]
    assert (ROOT / ".development-skills/project.json").is_file()


def test_canonical_repository_references_remain_resolvable() -> None:
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
            # Canonical procedures ship with the repository, not the skill-folder installer.
            assert (ROOT / referenced).is_file(), f"missing canonical reference: {source}: {href}"
            if referenced in mapping:
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


def test_installable_composition_uses_one_shared_smoke_and_unversioned_batch() -> None:
    declaration = json.loads((ROOT / "docs/development/distribution/composition.json").read_text())
    components = {row["name"]: row for row in declaration["components"]}
    assert set(components) == {"next", "smoke-test", "deploy", "azure-deploy-review", "batch"}
    assert components["smoke-test"]["root"] == "shared"
    assert components["batch"]["version"] is None
    assert components["batch"]["version_exception"] == "batch"
    for name in ("deploy", "azure-deploy-review", "batch"):
        component = components[name]
        assert component["kind"] == "project-skill"
        source = (ROOT / component["path"] / "SKILL.md").read_text()
        assert "coordinated activation receipt" in source
        assert "project-policy" in source
        assert "docs/development/operations/" in source
    assert not (ROOT / "docs/development/distribution/skills/smoke-test").exists()


@pytest.mark.parametrize("receipt_present", [False, True])
@pytest.mark.parametrize("targets_present", [False, True])
def test_historical_declaration_conformance_is_independent_of_installation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    receipt_present: bool,
    targets_present: bool,
) -> None:
    source_root = ROOT
    shutil.copytree(source_root / "docs", tmp_path / "docs")
    shutil.copytree(source_root / ".development-skills", tmp_path / ".development-skills")
    receipt = tmp_path / ".development-skills/activation-receipt.md"
    receipt.unlink(missing_ok=True)
    if receipt_present:
        receipt.write_text("Synthetic fixture only; no live authority evidence.\n")
    if targets_present:
        for name in EXPECTED:
            # Representative target presence, not package-integrity acceptance.
            target = tmp_path / f".agents/skills/{name}/SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_text("Synthetic installed entry point.\n")
    assert receipt.exists() is receipt_present
    assert all(
        (tmp_path / f".agents/skills/{name}/SKILL.md").exists() is targets_present
        for name in EXPECTED
    )
    module = __import__(__name__, fromlist=["ROOT"])
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "PROPOSED", tmp_path / "docs/development/proposed")
    test_staged_declaration_is_complete_and_inactive()
