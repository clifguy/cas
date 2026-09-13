"""The release step folds change records into a release and tags it (CAS-ADR-008).

A minor release does four things in one change: it moves the unreleased records
into the manifest's revision history under the new version, sets the version of
every artifact changed since the last release, sets both OpenAPI specifications'
declared contract version, and -- once that change has landed -- creates the
annotated tag the version is derived from.

The last step is the one that needed designing. The contract-version gate holds
both specifications to the version ``git describe`` derives, so a release
change that declares the next version cannot go green before its tag exists,
and its tag cannot be placed on a commit that does not yet exist. The resolution
tested here: the gate accepts specifications declaring the manifest's newest
release while, and only while, that release's tag does not exist. The release
change lands green, the tag is placed on the commit that introduced the release,
and from then on the three-way check is exactly as strict as before.

The conflict property closes the file: two changes landing concurrently, each
touching a specification and recording itself, merge in either order, where the
counter scheme this replaces conflicted.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

import pytest

from scripts.substrate_changes import (
    ReleaseRefused,
    contract_version_ok,
    head_release,
    main,
    release_prepare,
    release_tag,
    run_check,
)
from tests.helpers.substrate_repo import (
    APP_SPEC,
    CATALOG,
    CORE_SPEC,
    MANIFEST,
    OTHER_SCHEMA,
    RECORDS_DIR,
    SubstrateRepo,
    make_real_substrate_repo,
    make_substrate_repo,
    render_manifest,
)

MINOR: dict[str, Any] = {
    "classification": "minor",
    "category": ["capability"],
    "summary": "Things carry a size.",
    "for_callers": "Thing responses now include size.",
}
# Summaries sort opposite to their records' file names, so a fold ordered by
# content rather than by file name produces a different sequence.
PATCH: dict[str, Any] = {
    "classification": "patch",
    "summary": "Update the application API wording.",
}
TODAY = dt.date(2026, 2, 1)


def _add_property(spec: dict[str, Any]) -> None:
    spec["components"]["schemas"]["Thing"]["properties"]["size"] = {
        "type": "integer",
        "description": "Size.",
    }


# ---------------------------------------------------------------------------
# The contract-version rule
# ---------------------------------------------------------------------------

SPECS_AT = lambda version: {"sage_core_api": version, "cas_app_api": version}  # noqa: E731


@pytest.mark.parametrize(
    ("api", "specs", "release", "tag_exists", "ok"),
    [
        ("2.3", SPECS_AT("2.3"), None, False, True),
        ("2.4", SPECS_AT("2.4"), "2.4", True, True),
        ("2.3", SPECS_AT("2.4"), "2.4", False, True),
        ("2.3", SPECS_AT("2.4"), "2.4", True, False),
        ("2.5", SPECS_AT("2.4"), "2.4", True, False),
        ("2.5", SPECS_AT("2.4"), "2.4", False, False),
        ("2.3", {"sage_core_api": "2.4", "cas_app_api": "2.3"}, "2.4", False, False),
        ("2.3", SPECS_AT("2.4"), None, False, False),
        ("2.3", SPECS_AT("3.0"), "3.0", False, True),
    ],
    ids=[
        "between-releases",
        "released-and-tagged",
        "release-landed-tag-pending",
        "tag-exists-but-not-reached",
        "release-behind-the-tag",
        "release-behind-an-untagged-derivation",
        "one-spec-behind",
        "spec-ahead-with-no-release",
        "major-pending",
    ],
)
def test_contract_version_rule(
    api: str, specs: dict[str, str], release: str | None, tag_exists: bool, ok: bool
) -> None:
    errors = contract_version_ok(api, specs, release, tag_exists=tag_exists)

    assert (errors == []) is ok, errors


# ---------------------------------------------------------------------------
# release-prepare
# ---------------------------------------------------------------------------


@pytest.fixture
def ready(tmp_path: Path) -> SubstrateRepo:
    """``main`` carries a minor and a patch change since ``v1.0.0``; ``release`` is checked out."""
    repo = make_substrate_repo(tmp_path / "repo")
    repo.edit_yaml(CORE_SPEC, _add_property)
    repo.add_record("things-carry-a-size", **MINOR)
    repo.commit("a minor change lands")
    repo.edit_yaml(APP_SPEC, lambda s: s["info"].__setitem__("description", "Reworded."))
    repo.add_record("reword-app", **PATCH)
    repo.commit("a patch change lands")
    repo.checkout("release", create=True)
    return repo


def _version_line_neutral(text: str) -> str:
    return re.sub(r'(?m)^(\s+version:\s*)["\']?[0-9.]+["\']?\s*$', r"\1X", text, count=1)


def test_release_prepare_folds_records_and_sets_versions(ready: SubstrateRepo) -> None:
    core_before, app_before = ready.read_text(CORE_SPEC), ready.read_text(APP_SPEC)
    records = {
        name: ready.read_yaml(f"{RECORDS_DIR}/{name}.yaml")
        for name in ("reword-app", "things-carry-a-size")
    }

    release = release_prepare(ready.root, today=TODAY)

    manifest = ready.read_json(MANIFEST)
    assert release == "1.1"
    assert manifest["revision_history"][0] == {
        "release": "1.1",
        "date": "2026-02-01",
        "changes": [records["reword-app"], records["things-carry-a-size"]],
    }
    assert manifest["revision_history"][1:] == [
        {"version": "0.1", "date": "2026-01-01", "summary": "Legacy entry."}
    ]
    assert manifest["manifest_date"] == "2026-02-01"
    assert sorted(p.name for p in ready.path(RECORDS_DIR).iterdir()) == [".gitkeep"]

    versions = {entry["path"]: entry["version"] for entry in manifest["schemas"]}
    assert versions == {
        "sage/sage_core_api.openapi.yaml": "1.1",
        "cas_app_api.openapi.yaml": "1.1",
        "sage/sage_mcp_tools.catalog.json": "1.0",
        "sage/other.schema.json": "1.0",
        "changes/change_record.schema.json": "1.0",
    }

    for rel, before in ((CORE_SPEC, core_before), (APP_SPEC, app_before)):
        after = ready.read_text(rel)
        assert ready.read_yaml(rel)["info"]["version"] == "1.1"
        assert _version_line_neutral(after) == _version_line_neutral(before), (
            f"{rel} changed beyond its info.version line"
        )
    assert ready.read_text(MANIFEST) == render_manifest(manifest), "manifest is not canonical"


def test_release_prepare_major(ready: SubstrateRepo) -> None:
    assert release_prepare(ready.root, today=TODAY, major=True) == "2.0"
    assert head_release(ready.read_json(MANIFEST)) == "2.0"


def test_release_prepare_accepts_no_explicit_version(ready: SubstrateRepo) -> None:
    with pytest.raises(SystemExit):
        main(["release-prepare", "--repo-root", str(ready.root), "--version", "1.5"])


def test_release_prepare_refuses_with_no_minor_due(tmp_path: Path) -> None:
    repo = make_substrate_repo(tmp_path / "repo")
    repo.edit_yaml(APP_SPEC, lambda s: s["info"].__setitem__("description", "Reworded."))
    repo.add_record("reword-app", **PATCH)
    repo.commit("a patch lands")
    repo.checkout("release", create=True)

    with pytest.raises(ReleaseRefused, match="no minor"):
        release_prepare(repo.root, today=TODAY)
    assert repo.git("status", "--porcelain").stdout == "", "a refused release changed files"


def test_release_prepare_refuses_an_uncovered_contract_finding(tmp_path: Path) -> None:
    repo = make_substrate_repo(tmp_path / "repo")
    repo.edit_yaml(CORE_SPEC, _add_property)
    repo.add_record("declare-size", **PATCH, detector_override={"reason": "Already returned."})
    repo.commit("an overridden change lands")
    repo.checkout("release", create=True)

    with pytest.raises(ReleaseRefused, match="property-added"):
        release_prepare(repo.root, today=TODAY)


def test_release_prepare_refuses_on_the_default_branch(ready: SubstrateRepo) -> None:
    ready.checkout("main")

    with pytest.raises(ReleaseRefused, match="branch"):
        release_prepare(ready.root, today=TODAY)


def test_release_prepare_refuses_a_dirty_tree(ready: SubstrateRepo) -> None:
    ready.write_text("sage/app.py", "VALUE = 3\n")

    with pytest.raises(ReleaseRefused, match="uncommitted"):
        release_prepare(ready.root, today=TODAY)


def test_release_prepare_refuses_while_a_release_awaits_its_tag(ready: SubstrateRepo) -> None:
    release_prepare(ready.root, today=TODAY)
    ready.commit("release 1.1")
    ready.edit_yaml(
        CORE_SPEC,
        lambda s: s["components"]["schemas"]["Thing"]["properties"].__setitem__(
            "shape", {"type": "string", "description": "Shape."}
        ),
    )
    ready.add_record("things-carry-a-shape", **MINOR)
    ready.commit("another minor lands before the tag")

    with pytest.raises(ReleaseRefused, match="1.1"):
        release_prepare(ready.root, today=TODAY)


# ---------------------------------------------------------------------------
# release-tag
# ---------------------------------------------------------------------------


def _land_release(repo: SubstrateRepo) -> str:
    release_prepare(repo.root, today=TODAY)
    repo.commit("prepare release")
    release_commit = repo.squash_merge("release", "Release 1.1")
    # A later change that edits the manifest without releasing anything, so the
    # newest manifest-touching commit is not the release commit.
    repo.edit_json(MANIFEST, lambda m: m.__setitem__("description", "Reworded manifest."))
    repo.add_record("reword-manifest", classification="patch", summary="Reword the manifest.")
    repo.commit("a manifest edit lands after the release")
    repo.write_text("sage/app.py", "VALUE = 9\n")
    repo.commit("an unrelated change lands after the release")
    return release_commit


def test_release_tag_tags_the_commit_that_introduced_the_release(ready: SubstrateRepo) -> None:
    release_commit = _land_release(ready)
    assert ready.rev("HEAD") != release_commit, "control: HEAD must not be the release commit"

    tag = release_tag(ready.root)

    assert tag == "v1.1.0"
    assert ready.git("cat-file", "-t", "v1.1.0").stdout.strip() == "tag", "tag is not annotated"
    assert ready.rev("v1.1.0^{commit}") == release_commit
    message = ready.git("tag", "-l", "--format=%(contents)", "v1.1.0").stdout
    assert MINOR["for_callers"] in message
    described = ready.git("describe", "--long", "--tags", "--match", "v*", release_commit).stdout
    assert described.startswith("v1.1.0-0-g")
    assert contract_version_ok("1.1", SPECS_AT("1.1"), "1.1", tag_exists=True) == []


def test_release_tag_refuses_twice(ready: SubstrateRepo) -> None:
    _land_release(ready)
    release_tag(ready.root)

    with pytest.raises(ReleaseRefused, match="v1.1.0"):
        release_tag(ready.root)


def test_release_tag_refuses_with_no_release(tmp_path: Path) -> None:
    repo = make_substrate_repo(tmp_path / "repo")

    with pytest.raises(ReleaseRefused, match="no release"):
        release_tag(repo.root)


# ---------------------------------------------------------------------------
# The conflict property
# ---------------------------------------------------------------------------


def _summary_line_indexes(text: str) -> list[int]:
    """Lines carrying a plain one-line summary, which can be extended in place."""
    plain = re.compile(r"^\s+summary: [A-Za-z][^#]*$")
    return [i for i, line in enumerate(text.splitlines()) if plain.match(line)]


def _reword_summary(repo: SubstrateRepo, which: int, suffix: str) -> None:
    lines = repo.read_text(CORE_SPEC).splitlines(keepends=True)
    index = _summary_line_indexes("".join(lines))[which]
    lines[index] = lines[index].rstrip("\n") + f" {suffix}\n"
    repo.write_text(CORE_SPEC, "".join(lines))


def _merges_cleanly(repo: SubstrateRepo, first: str, second: str) -> tuple[bool, str]:
    repo.checkout(f"merge-{first}-{second}", create=True, start="main")
    out = ""
    for branch in (first, second):
        result = repo.git("merge", "--no-edit", "-q", branch, check=False)
        out += result.stdout + result.stderr
        if result.returncode != 0:
            repo.git("merge", "--abort", check=False)
            return False, out
    return True, out


def test_concurrent_recorded_changes_merge_in_either_order(tmp_path: Path) -> None:
    repo = make_real_substrate_repo(tmp_path / "repo")
    assert len(_summary_line_indexes(repo.read_text(CORE_SPEC))) > 10

    for branch, which in (("a", 0), ("b", -1)):
        repo.checkout(branch, create=True, start="main")
        _reword_summary(repo, which, f"(reworded on {branch})")
        repo.add_record(f"reword-on-{branch}", classification="patch", summary=f"Reword {branch}.")
        repo.commit(f"change {branch}")
        assert CORE_SPEC in repo.git("diff", "--name-only", "main", "HEAD").stdout, (
            "control: each branch must change the specification"
        )
        assert run_check(repo.root, base="main", head="HEAD").errors == []

    assert _merges_cleanly(repo, "a", "b")[0]
    assert _merges_cleanly(repo, "b", "a")[0]


def test_the_counter_scheme_it_replaces_conflicts(tmp_path: Path) -> None:
    """Control: the harness detects a conflict, so the clean merge above means something."""
    repo = make_real_substrate_repo(tmp_path / "repo", legacy=True)

    for branch, which in (("a", 0), ("b", -1)):
        repo.checkout(branch, create=True, start="main")
        _reword_summary(repo, which, f"(reworded on {branch})")

        def bump(manifest: dict[str, Any], branch: str = branch) -> None:
            manifest["substrate_version"] = "1.1"
            core = next(
                e for e in manifest["schemas"] if e["path"] == "sage/sage_core_api.openapi.yaml"
            )
            core["version"] = f"0.9-{branch}"
            manifest["revision_history"].insert(
                0, {"version": "1.1", "date": "2026-01-02", "summary": f"Reword {branch}."}
            )

        repo.edit_json(MANIFEST, bump)
        repo.commit(f"change {branch}")
        assert CORE_SPEC in repo.git("diff", "--name-only", "main", "HEAD").stdout

    clean, output = _merges_cleanly(repo, "a", "b")

    assert not clean
    assert "manifest.json" in output


def test_fixture_constants_name_real_paths() -> None:
    """The fixture's paths are the script's paths; a drift would test a different layout."""
    from scripts import substrate_changes as sc

    assert sc.MANIFEST == MANIFEST
    assert set(sc.SPEC_PATHS) == {CORE_SPEC, APP_SPEC}
    assert sc.CATALOG_PATH == CATALOG
    assert sc.RECORDS_DIR == RECORDS_DIR
    assert OTHER_SCHEMA.startswith("docs/fs/")
