"""Every contract change carries a change record, and the record's claim holds.

CAS-ADR-008 retires the hand-assigned substrate counters in favour of per-change
records that carry a patch or minor classification and no number. The gate in
``scripts/substrate_changes.py check`` makes that concrete, and each of its duties
is tested here against a disposable repository:

- a change touching a manifest-listed substrate file, either OpenAPI
  specification, or the MCP tool catalog must add a record;
- a record must be well formed, and a ``patch`` claim must survive the contract
  comparison unless the owner's override says why;
- a change that is not a release may not move a version, rewrite the revision
  history, or delete another change's record, because those are the edits that
  made concurrent changes conflict;
- a release, the one change that does fold records and move versions, is
  recognized as such and held to folding exactly what it deletes.

Most cases come in pairs differing by one file or one field, so a gate that
always passes, or always fails, is caught by one side of the pair.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from scripts.substrate_changes import main, release_prepare, run_check
from tests.helpers.substrate_repo import (
    APP_SPEC,
    CATALOG,
    CORE_SPEC,
    MANIFEST,
    OTHER_SCHEMA,
    REAL_SUBSTRATE,
    RECORDS_DIR,
    SubstrateRepo,
    make_substrate_repo,
)

MINOR_CAPABILITY: dict[str, Any] = {
    "classification": "minor",
    "category": ["capability"],
    "summary": "Things carry a size.",
    "for_callers": "Thing responses now include size.",
}
PATCH: dict[str, Any] = {"classification": "patch", "summary": "Clarify the list description."}


@pytest.fixture
def repo(tmp_path: Path) -> SubstrateRepo:
    repo = make_substrate_repo(tmp_path / "repo")
    repo.checkout("feature", create=True)
    return repo


def _add_property(spec: dict[str, Any]) -> None:
    spec["components"]["schemas"]["Thing"]["properties"]["size"] = {
        "type": "integer",
        "description": "Size.",
    }


def _require_property(spec: dict[str, Any]) -> None:
    spec["components"]["schemas"]["Thing"]["properties"]["kind"] = {
        "type": "string",
        "description": "Kind.",
    }
    spec["components"]["schemas"]["Thing"]["required"].append("kind")


def _reword(spec: dict[str, Any]) -> None:
    spec["paths"]["/things"]["get"]["summary"] = "Enumerate things."


def _check(repo: SubstrateRepo, **kwargs: Any):
    return run_check(repo.root, base="main", **kwargs)


# ---------------------------------------------------------------------------
# The record requirement
# ---------------------------------------------------------------------------


def test_contract_change_without_a_record_fails(repo: SubstrateRepo) -> None:
    repo.edit_yaml(CORE_SPEC, _add_property)

    result = _check(repo)

    assert result.errors, "a spec change with no change record passed the gate"
    message = "\n".join(result.errors)
    assert "change record" in message
    assert CORE_SPEC in message


def test_the_same_change_with_a_record_passes(repo: SubstrateRepo) -> None:
    repo.edit_yaml(CORE_SPEC, _add_property)
    repo.add_record("things-carry-a-size", **MINOR_CAPABILITY)

    result = _check(repo)

    assert result.errors == []
    assert result.classification == "minor"
    assert result.categories == ["capability"]


def test_description_only_change_needs_a_patch_record_and_nothing_more(repo: SubstrateRepo) -> None:
    repo.edit_yaml(CORE_SPEC, _reword)
    assert _check(repo).errors, "a description-only spec edit passed with no record"

    repo.add_record("clarify-list", **PATCH)
    result = _check(repo)

    assert result.errors == []
    assert result.findings == []
    assert result.classification == "patch"


@pytest.mark.parametrize(
    ("rel", "mutate"),
    [
        (
            OTHER_SCHEMA,
            lambda d: d["properties"].__setitem__("x", {"type": "string", "description": "X."}),
        ),
        (APP_SPEC, lambda d: d["info"].__setitem__("description", "Reworded.")),
        (CATALOG, lambda d: d["surfaces"]["sage"][0].__setitem__("description", "Reworded.")),
    ],
    ids=["manifest-listed-schema", "application-spec", "mcp-catalog"],
)
def test_every_contract_artifact_triggers_the_requirement(
    repo: SubstrateRepo, rel: str, mutate: Any
) -> None:
    if rel.endswith(".yaml"):
        repo.edit_yaml(rel, mutate)
    else:
        repo.edit_json(rel, mutate)

    unrecorded = _check(repo)
    repo.add_record("reword", **PATCH)
    recorded = _check(repo)

    assert any(rel in error for error in unrecorded.errors), unrecorded.errors
    assert recorded.errors == []


def test_a_change_outside_the_contract_needs_no_record(repo: SubstrateRepo) -> None:
    repo.write_text("sage/app.py", "VALUE = 2\n")

    result = _check(repo)

    assert result.errors == []
    assert result.classification == "patch"
    assert result.records_added == []


def test_a_record_already_on_the_base_does_not_satisfy_a_new_change(tmp_path: Path) -> None:
    """The requirement is a record this change adds, not any record in the tree."""
    repo = make_substrate_repo(tmp_path / "repo")
    repo.edit_yaml(CORE_SPEC, _reword)
    repo.add_record("landed-earlier", **PATCH)
    repo.commit("an earlier change lands with its record")
    repo.checkout("feature", create=True)
    repo.edit_yaml(CORE_SPEC, _add_property)
    assert repo.path(f"{RECORDS_DIR}/landed-earlier.yaml").exists()

    result = _check(repo)

    assert any("change record" in error for error in result.errors), result.errors


def test_delisting_and_deleting_an_artifact_needs_a_record(repo: SubstrateRepo) -> None:
    """An artifact the change removes is listed only in the base manifest."""
    repo.edit_json(MANIFEST, lambda m: m["schemas"].pop(3))
    repo.delete(OTHER_SCHEMA)

    unrecorded = _check(repo)
    repo.add_record("retire-other-schema", **PATCH)
    recorded = _check(repo)

    assert any(OTHER_SCHEMA in error for error in unrecorded.errors), unrecorded.errors
    assert recorded.errors == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m["schemas"].pop(3),
        lambda m: m["schemas"][3].__setitem__("role", "Reworded role."),
    ],
    ids=["delist-without-deleting", "reword-an-entry"],
)
def test_a_manifest_only_edit_needs_a_record(repo: SubstrateRepo, mutate: Any) -> None:
    """The manifest is published inventory; editing it is a substrate change."""
    repo.edit_json(MANIFEST, mutate)
    assert repo.path(OTHER_SCHEMA).exists(), "control: the artifact file itself is untouched"

    unrecorded = _check(repo)
    repo.add_record("manifest-inventory-edit", **PATCH)
    recorded = _check(repo)

    assert any(MANIFEST in error for error in unrecorded.errors), unrecorded.errors
    assert recorded.errors == []


def test_the_comparison_uses_the_merge_base_when_the_base_branch_moves_on(
    repo: SubstrateRepo,
) -> None:
    """Changes landing on the base after this branch forked are not this change's."""
    repo.edit_yaml(CORE_SPEC, _add_property)
    repo.add_record("things-carry-a-size", **MINOR_CAPABILITY)
    repo.commit("feature work")
    repo.checkout("main")
    repo.edit_yaml(APP_SPEC, lambda s: s["info"].__setitem__("description", "Reworded."))
    repo.add_record("reword-app", **PATCH)
    repo.commit("another change lands on main")
    repo.checkout("feature")

    result = run_check(repo.root, base="main", head="HEAD")

    assert result.errors == []
    assert result.records_added == [f"{RECORDS_DIR}/things-carry-a-size.yaml"]
    assert [f.kind for f in result.findings] == ["property-added"]


# ---------------------------------------------------------------------------
# Record validity and the patch claim
# ---------------------------------------------------------------------------


def test_patch_claim_against_a_minor_finding_fails(repo: SubstrateRepo) -> None:
    repo.edit_yaml(CORE_SPEC, _add_property)
    repo.add_record("things-carry-a-size", **PATCH)

    result = _check(repo)

    message = "\n".join(result.errors)
    assert "property-added" in message
    assert "patch" in message


def test_owner_override_with_a_reason_admits_the_patch_claim(repo: SubstrateRepo) -> None:
    repo.edit_yaml(CORE_SPEC, _add_property)
    repo.add_record(
        "declare-size",
        **PATCH,
        detector_override={"reason": "The server already returned size; this declares it."},
    )

    result = _check(repo)

    assert result.errors == []
    assert result.classification == "patch"
    assert [f.kind for f in result.findings] == ["property-added"]
    assert not [w for w in result.warnings if "detector_override" in w], (
        "an override with a finding to cover was reported stale"
    )


def test_an_override_with_nothing_to_override_warns(repo: SubstrateRepo) -> None:
    """An override that outlived the finding it justified is flagged, not failed."""
    repo.edit_yaml(CORE_SPEC, _reword)
    repo.add_record(
        "clarify-list",
        **PATCH,
        detector_override={"reason": "Written for a finding since removed."},
    )

    result = _check(repo)

    assert result.errors == []
    assert result.findings == []
    assert [w for w in result.warnings if "detector_override" in w], result.warnings


def test_override_without_a_reason_is_refused(repo: SubstrateRepo) -> None:
    repo.edit_yaml(CORE_SPEC, _add_property)
    repo.add_record("declare-size", **PATCH, detector_override={"reason": "  "})

    result = _check(repo)

    assert any("declare-size" in error for error in result.errors), result.errors


@pytest.mark.parametrize(
    "fields",
    [
        {"classification": "minor", "summary": "No category.", "for_callers": "Text."},
        {"classification": "minor", "category": ["capability"], "summary": "S.", "for_callers": ""},
        {"classification": "patch", "category": ["capability"], "summary": "Patch with category."},
        {"classification": "major", "summary": "Not a classification."},
        {"classification": "patch", "summary": "S.", "version": "1.2"},
    ],
    ids=[
        "minor-without-category",
        "minor-without-for-callers",
        "patch-with-category",
        "bad-classification",
        "carries-a-version",
    ],
)
def test_malformed_record_is_refused(repo: SubstrateRepo, fields: dict[str, Any]) -> None:
    repo.edit_yaml(CORE_SPEC, _reword)
    repo.add_record("malformed", **fields)

    result = _check(repo)

    assert any("malformed" in error for error in result.errors), result.errors


@pytest.mark.parametrize("name", ["Upper-Case.yaml", "record.yml", "under_score.yaml"])
def test_record_file_name_is_a_kebab_slug_with_yaml_suffix(repo: SubstrateRepo, name: str) -> None:
    repo.edit_yaml(CORE_SPEC, _reword)
    repo.write_yaml(f"{RECORDS_DIR}/{name}", PATCH)

    result = _check(repo)

    assert any(name in error for error in result.errors), result.errors


def test_category_disagreement_warns_without_failing(repo: SubstrateRepo) -> None:
    repo.edit_yaml(CORE_SPEC, _require_property)
    repo.add_record("things-carry-a-kind", **MINOR_CAPABILITY)

    result = _check(repo)

    assert result.errors == []
    assert any("caller-adaptation" in warning for warning in result.warnings), result.warnings


# ---------------------------------------------------------------------------
# The retired counters
# ---------------------------------------------------------------------------


def _bump_artifact_version(manifest: dict[str, Any]) -> None:
    manifest["schemas"][0]["version"] = "1.1"


def _bump_spec_version(spec: dict[str, Any]) -> None:
    spec["info"]["version"] = "1.1"


def _prepend_history(manifest: dict[str, Any]) -> None:
    manifest["revision_history"].insert(
        0, {"version": "0.2", "date": "2026-01-02", "summary": "Hand-numbered."}
    )


@pytest.mark.parametrize(
    ("rel", "mutate"),
    [
        (MANIFEST, _bump_artifact_version),
        (CORE_SPEC, _bump_spec_version),
        (MANIFEST, _prepend_history),
        (MANIFEST, lambda m: m.__setitem__("substrate_version", "1.1")),
    ],
    ids=["artifact-version", "spec-info-version", "revision-history", "substrate-counter"],
)
def test_a_non_release_change_may_not_move_a_counter(
    repo: SubstrateRepo, rel: str, mutate: Any
) -> None:
    repo.edit_yaml(CORE_SPEC, _reword)
    repo.add_record("clarify-list", **PATCH)
    assert _check(repo).errors == [], "control: the recorded change alone must pass"
    if rel == MANIFEST:
        repo.edit_json(rel, mutate)
    else:
        repo.edit_yaml(rel, mutate)

    result = _check(repo)

    assert any("release" in error for error in result.errors), result.errors


def test_the_migration_from_the_counter_scheme_may_move_versions(tmp_path: Path) -> None:
    """The one change that retires the counters rewrites them; nothing after may."""
    repo = make_substrate_repo(tmp_path / "legacy", legacy=True)
    repo.checkout("migrate", create=True)

    def migrate(manifest: dict[str, Any]) -> None:
        manifest.pop("substrate_version")
        for entry in manifest["schemas"]:
            entry["version"] = "1.0"

    repo.edit_json(MANIFEST, migrate)
    repo.edit_yaml(CORE_SPEC, _reword)
    assert '+      "version": "1.0"' in repo.git("diff", "main", "--", MANIFEST).stdout, (
        "control: the migration must move artifact versions for this case to mean anything"
    )
    unrecorded = _check(repo)
    repo.add_record("retire-counters", **PATCH)
    recorded = _check(repo)

    assert any("change record" in error for error in unrecorded.errors), unrecorded.errors
    assert recorded.errors == []


def test_a_non_release_change_may_not_delete_another_changes_record(tmp_path: Path) -> None:
    repo = make_substrate_repo(tmp_path / "repo")
    repo.edit_yaml(CORE_SPEC, _reword)
    repo.add_record("landed-earlier", **PATCH)
    repo.commit("an earlier change lands")
    repo.checkout("feature", create=True)
    repo.delete(f"{RECORDS_DIR}/landed-earlier.yaml")

    result = _check(repo)

    assert any("landed-earlier" in error for error in result.errors), result.errors


# ---------------------------------------------------------------------------
# Release changes
# ---------------------------------------------------------------------------


@pytest.fixture
def released(tmp_path: Path) -> SubstrateRepo:
    """A repository whose ``release`` branch holds a prepared, uncommitted release."""
    repo = make_substrate_repo(tmp_path / "repo")
    repo.edit_yaml(CORE_SPEC, _add_property)
    repo.add_record("things-carry-a-size", **MINOR_CAPABILITY)
    repo.commit("a minor change lands")
    repo.checkout("release", create=True)
    release_prepare(repo.root, today=dt.date(2026, 2, 1))
    return repo


def test_a_prepared_release_passes_without_a_record(released: SubstrateRepo) -> None:
    result = _check(released)

    assert result.errors == []
    assert result.release == "1.1"
    assert result.classification == "release"


@pytest.mark.parametrize(
    "tamper",
    [
        lambda r: r.add_record("left-behind", **PATCH),
        lambda r: r.edit_json(
            MANIFEST,
            lambda m: m["revision_history"][0]["changes"][0].__setitem__("summary", "Rewritten."),
        ),
        lambda r: r.edit_yaml(CORE_SPEC, _require_property),
    ],
    ids=["record-left-behind", "fold-differs-from-deleted-records", "contract-change-in-release"],
)
def test_a_release_folds_exactly_what_it_deletes_and_changes_nothing_else(
    released: SubstrateRepo, tamper: Any
) -> None:
    assert _check(released).errors == [], "control: the untampered release must pass"
    tamper(released)

    result = _check(released)

    assert result.errors, "a tampered release passed the gate"


# ---------------------------------------------------------------------------
# Working tree, commit range, and the machine-readable report
# ---------------------------------------------------------------------------


def test_working_tree_and_commit_range_agree(repo: SubstrateRepo) -> None:
    repo.edit_yaml(CORE_SPEC, _add_property)
    repo.add_record("things-carry-a-size", **MINOR_CAPABILITY)
    assert repo.git("status", "--porcelain", "--", RECORDS_DIR).stdout.startswith("??"), (
        "control: the record must be untracked for this case to mean anything"
    )

    working_tree = _check(repo)
    repo.commit("record the change")
    committed = run_check(repo.root, base="main", head="HEAD")

    assert working_tree.errors == [] and committed.errors == []
    assert (
        working_tree.records_added
        == committed.records_added
        == [f"{RECORDS_DIR}/things-carry-a-size.yaml"]
    )
    assert working_tree.classification == committed.classification == "minor"


def test_json_report_aggregates_the_added_records(
    repo: SubstrateRepo, capsys: pytest.CaptureFixture[str]
) -> None:
    repo.edit_yaml(CORE_SPEC, _require_property)
    repo.add_record(
        "things-carry-a-kind",
        classification="minor",
        category=["caller-adaptation"],
        summary="Kind is required.",
        for_callers="Send kind.",
    )
    repo.add_record("docs", **PATCH)
    repo.add_record(
        "also-new",
        classification="minor",
        category=["capability", "caller-adaptation"],
        summary="S.",
        for_callers="T.",
    )

    code = main(["check", "--repo-root", str(repo.root), "--base", "main", "--format", "json"])
    report = json.loads(capsys.readouterr().out)

    assert code == 0
    assert report["classification"] == "minor"
    assert report["categories"] == ["capability", "caller-adaptation"]
    assert len(report["records"]) == 3
    assert report["errors"] == []


def test_cli_reports_an_unresolvable_base_without_a_traceback(
    repo: SubstrateRepo, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["check", "--repo-root", str(repo.root), "--base", "no-such-revision"])
    captured = capsys.readouterr()

    assert code == 1
    assert captured.err.startswith("error: ")
    assert "no-such-revision" in captured.err


def test_cli_exit_status_follows_the_verdict(
    repo: SubstrateRepo, capsys: pytest.CaptureFixture[str]
) -> None:
    repo.edit_yaml(CORE_SPEC, _add_property)
    failing = main(["check", "--repo-root", str(repo.root), "--base", "main"])
    repo.add_record("things-carry-a-size", **MINOR_CAPABILITY)
    passing = main(["check", "--repo-root", str(repo.root), "--base", "main"])
    out = capsys.readouterr().out

    assert (failing, passing) == (1, 0)
    assert "Release classification: minor (capability)" in out


# ---------------------------------------------------------------------------
# The real substrate
# ---------------------------------------------------------------------------


def test_every_committed_unreleased_record_is_valid() -> None:
    schema = json.loads((REAL_SUBSTRATE / "changes" / "change_record.schema.json").read_text())
    validator = jsonschema.Draft202012Validator(schema)

    records = sorted((REAL_SUBSTRATE / "changes" / "unreleased").glob("*.yaml"))
    for path in records:
        errors = sorted(validator.iter_errors(yaml.safe_load(path.read_text())), key=str)
        assert not errors, f"{path.name}: {[e.message for e in errors]}"
