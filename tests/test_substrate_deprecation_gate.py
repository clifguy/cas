"""A caller adaptation that withdraws something follows a deprecation (CAS-ADR-008 clause 8).

The change-record gate in ``scripts/substrate_changes.py check`` refuses a
removal, a narrowed input, or a changed default the contract comparison finds,
unless the change's record either follows a deprecation of its target or claims
an exemption. A deprecation counts only when a release after 3.0 published it, at
least 30 days before the check and in an earlier release than the adaptation.
The deprecation's date is its release's date in the manifest's revision history.

Every red arm below carries an otherwise valid minor ``caller-adaptation``
record, so the gate cannot pass the test by failing for an unrelated reason,
and each asserts the error that names the arm.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from scripts.substrate_changes import main, run_check
from tests.helpers.substrate_repo import (
    CATALOG,
    CORE_SPEC,
    MANIFEST,
    REAL_SUBSTRATE,
    SubstrateRepo,
    make_substrate_repo,
)

TODAY = dt.date(2026, 12, 1)
LIMIT = "paths//things/get/parameters/query:limit"
OPERATION = "paths//things/get"

DEPRECATION: dict[str, Any] = {
    "classification": "minor",
    "category": ["caller-adaptation"],
    "summary": "Deprecates the limit parameter of the things listing.",
    "for_callers": "limit is deprecated; use page_size. It may be removed from 2027-01-01.",
    "deprecates": [
        {
            "surface": "sage_core_api",
            "pointer": LIMIT,
            "replacement": "page_size",
            "earliest_adaptation": "2026-11-01",
        }
    ],
}


def _adaptation(**extra: Any) -> dict[str, Any]:
    return {
        "classification": "minor",
        "category": ["caller-adaptation"],
        "summary": "Removes the limit parameter deprecated in 3.1.",
        "for_callers": "limit is gone; use page_size.",
        **extra,
    }


def _follows(release: str = "3.1", pointer: str = LIMIT) -> list[dict[str, str]]:
    return [{"release": release, "surface": "sage_core_api", "pointer": pointer}]


def _mark_limit(spec: dict[str, Any]) -> None:
    spec["paths"]["/things"]["get"]["parameters"][0]["deprecated"] = True


def _remove_limit(spec: dict[str, Any]) -> None:
    spec["paths"]["/things"]["get"]["parameters"] = []


def _publish(
    repo: SubstrateRepo, release: str, date: dt.date, changes: list[dict[str, Any]]
) -> None:
    """Record a release entry on ``main``, as the release step would have folded it."""
    repo.edit_json(
        MANIFEST,
        lambda m: m["revision_history"].insert(
            0, {"release": release, "date": date.isoformat(), "changes": changes}
        ),
    )


def _deprecated_repo(
    tmp_path: Path,
    *,
    release: str = "3.1",
    age: int = 30,
    deprecation: dict[str, Any] | None = None,
) -> SubstrateRepo:
    """``limit`` marked deprecated and published ``age`` days before TODAY; branch ``feature``."""
    repo = make_substrate_repo(tmp_path / "repo")
    repo.edit_yaml(CORE_SPEC, _mark_limit)
    _publish(repo, release, TODAY - dt.timedelta(days=age), [deprecation or DEPRECATION])
    repo.commit(f"release {release} deprecates limit")
    repo.checkout("feature", create=True)
    return repo


def _check(repo: SubstrateRepo) -> Any:
    return run_check(repo.root, base="main", today=TODAY)


def _errors(result: Any) -> str:
    return "\n".join(result.errors)


# ---------------------------------------------------------------------------
# The record schema
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def validator() -> jsonschema.Draft202012Validator:
    schema = json.loads((REAL_SUBSTRATE / "changes" / "change_record.schema.json").read_text())
    return jsonschema.Draft202012Validator(schema)


@pytest.mark.parametrize(
    "record",
    [
        DEPRECATION,
        _adaptation(follows_deprecation=_follows()),
        _adaptation(exemption={"kind": "never-served", "reason": "Never implemented."}),
        {
            **DEPRECATION,
            "deprecates": [
                {**DEPRECATION["deprecates"][0], "pointer": f"{LIMIT}/schema/default", "value": 10}
            ],
        },
    ],
    ids=["deprecates", "follows", "exemption", "deprecated-default"],
)
def test_schema_accepts_the_deprecation_fields(
    validator: jsonschema.Draft202012Validator, record: dict[str, Any]
) -> None:
    assert list(validator.iter_errors(record)) == []


@pytest.mark.parametrize(
    "record",
    [
        {
            "classification": "patch",
            "summary": "S.",
            "exemption": {"kind": "never-served", "reason": "R."},
        },
        {**DEPRECATION, "category": ["capability"]},
        _adaptation(exemption={"kind": "convenience", "reason": "R."}),
        _adaptation(exemption={"kind": "data-integrity", "reason": "  "}),
        {
            **DEPRECATION,
            "deprecates": [{**DEPRECATION["deprecates"][0], "earliest_adaptation": "soon"}],
        },
        {
            **DEPRECATION,
            "deprecates": [{**DEPRECATION["deprecates"][0], "replacement": ""}],
        },
        _adaptation(follows_deprecation=[{"release": "3.1.0", "surface": "mcp", "pointer": "x"}]),
        _adaptation(follows_deprecation=[{"release": "3.1", "surface": "rest", "pointer": "x"}]),
    ],
    ids=[
        "on-a-patch",
        "without-caller-adaptation",
        "unknown-exemption",
        "blank-reason",
        "undated",
        "no-replacement",
        "release-not-major-minor",
        "unknown-surface",
    ],
)
def test_schema_refuses_malformed_deprecation_fields(
    validator: jsonschema.Draft202012Validator, record: dict[str, Any]
) -> None:
    assert list(validator.iter_errors(record)), "the schema accepted a malformed record"


# ---------------------------------------------------------------------------
# The adaptation arms
# ---------------------------------------------------------------------------


def test_a_removal_after_a_ripe_deprecation_passes(tmp_path: Path) -> None:
    repo = _deprecated_repo(tmp_path, age=30)
    repo.edit_yaml(CORE_SPEC, _remove_limit)
    repo.add_record("remove-limit", **_adaptation(follows_deprecation=_follows()))

    result = _check(repo)

    assert result.errors == []
    assert [f.kind for f in result.findings] == ["parameter-removed"]


def test_a_removal_never_deprecated_fails(tmp_path: Path) -> None:
    repo = make_substrate_repo(tmp_path / "repo")
    repo.checkout("feature", create=True)
    repo.edit_yaml(CORE_SPEC, _remove_limit)
    repo.add_record("remove-limit", **_adaptation())

    message = _errors(_check(repo))

    assert "no added record follows a deprecation of it or claims an exemption" in message
    assert f"parameter-removed at sage_core_api:{LIMIT}" in message


def test_a_deprecation_younger_than_30_days_fails(tmp_path: Path) -> None:
    repo = _deprecated_repo(tmp_path, age=29)
    repo.edit_yaml(CORE_SPEC, _remove_limit)
    repo.add_record("remove-limit", **_adaptation(follows_deprecation=_follows()))

    message = _errors(_check(repo))

    assert "may ship no sooner than 2026-12-02" in message
    assert LIMIT in message


def test_a_deprecation_in_the_same_unreleased_window_fails(tmp_path: Path) -> None:
    repo = make_substrate_repo(tmp_path / "repo")
    repo.edit_yaml(CORE_SPEC, _mark_limit)
    repo.add_record("deprecate-limit", **DEPRECATION)
    repo.commit("deprecation lands, not yet released")
    repo.checkout("feature", create=True)
    repo.edit_yaml(CORE_SPEC, _remove_limit)
    repo.add_record("remove-limit", **_adaptation(follows_deprecation=_follows()))

    message = _errors(_check(repo))

    assert "which no release after 3.0 deprecates" in message


@pytest.mark.parametrize(
    "follows",
    [_follows(release="3.2"), _follows(pointer=OPERATION)],
    ids=["wrong-release", "wrong-pointer"],
)
def test_following_a_deprecation_that_does_not_exist_fails(
    tmp_path: Path, follows: list[dict[str, str]]
) -> None:
    repo = _deprecated_repo(tmp_path, age=60)
    repo.edit_yaml(CORE_SPEC, _remove_limit)
    repo.add_record("remove-limit", **_adaptation(follows_deprecation=follows))

    message = _errors(_check(repo))

    assert "which no release after 3.0 deprecates" in message


def test_a_deprecated_operation_covers_the_removal_of_its_parameter(tmp_path: Path) -> None:
    deprecation = {
        **DEPRECATION,
        "deprecates": [{**DEPRECATION["deprecates"][0], "pointer": OPERATION}],
    }
    repo = _deprecated_repo(tmp_path, age=45, deprecation=deprecation)
    repo.edit_yaml(CORE_SPEC, _remove_limit)
    repo.add_record("remove-limit", **_adaptation(follows_deprecation=_follows(pointer=OPERATION)))

    assert _check(repo).errors == []


def test_a_sibling_pointer_is_not_an_ancestor(tmp_path: Path) -> None:
    """``.../query:limit`` must not cover ``.../query:limit_old`` by string prefix."""
    repo = make_substrate_repo(tmp_path / "repo")
    repo.edit_yaml(
        CORE_SPEC,
        lambda s: s["paths"]["/things"]["get"]["parameters"][0].__setitem__("name", "limit_old"),
    )
    _publish(repo, "3.1", TODAY - dt.timedelta(days=60), [DEPRECATION])
    repo.commit("release 3.1 deprecates limit, beside limit_old")
    repo.checkout("feature", create=True)
    repo.edit_yaml(CORE_SPEC, _remove_limit)
    repo.add_record("remove-limit-old", **_adaptation(follows_deprecation=_follows()))

    message = _errors(_check(repo))

    assert f"parameter-removed at sage_core_api:{LIMIT}_old" in message


@pytest.mark.parametrize("kind", ["security-exposure", "data-integrity", "never-served"])
def test_an_exemption_covers_an_undeprecated_removal(tmp_path: Path, kind: str) -> None:
    repo = make_substrate_repo(tmp_path / "repo")
    repo.checkout("feature", create=True)
    repo.edit_yaml(CORE_SPEC, _remove_limit)
    repo.add_record(
        "remove-limit",
        **_adaptation(exemption={"kind": kind, "reason": "Stated for the reviewer."}),
    )

    assert _check(repo).errors == []


@pytest.mark.parametrize("release", ["3.0", "2.7"])
def test_a_deprecation_at_or_before_3_0_does_not_count(tmp_path: Path, release: str) -> None:
    repo = _deprecated_repo(tmp_path, release=release, age=90)
    repo.edit_yaml(CORE_SPEC, _remove_limit)
    repo.add_record("remove-limit", **_adaptation(follows_deprecation=_follows(release=release)))

    message = _errors(_check(repo))

    assert "which no release after 3.0 deprecates" in message


def test_the_recorded_earliest_date_holds_when_later_than_the_window(tmp_path: Path) -> None:
    deprecation = {
        **DEPRECATION,
        "deprecates": [{**DEPRECATION["deprecates"][0], "earliest_adaptation": "2026-12-15"}],
    }
    repo = _deprecated_repo(tmp_path, age=40, deprecation=deprecation)
    repo.edit_yaml(CORE_SPEC, _remove_limit)
    repo.add_record("remove-limit", **_adaptation(follows_deprecation=_follows()))

    message = _errors(_check(repo))

    assert "may ship no sooner than 2026-12-15" in message


def test_an_adaptation_that_withdraws_nothing_needs_no_deprecation(tmp_path: Path) -> None:
    repo = make_substrate_repo(tmp_path / "repo")
    repo.checkout("feature", create=True)
    repo.edit_yaml(
        CORE_SPEC,
        lambda s: s["paths"]["/things"]["get"]["responses"].__setitem__(
            "409", {"description": "Conflict."}
        ),
    )
    repo.add_record("things-may-conflict", **_adaptation())

    result = _check(repo)

    assert [f.kind for f in result.findings] == ["response-added"]
    assert result.errors == []


def test_an_override_to_patch_is_not_also_held_to_deprecation(tmp_path: Path) -> None:
    repo = make_substrate_repo(tmp_path / "repo")
    repo.checkout("feature", create=True)
    repo.edit_yaml(CORE_SPEC, _remove_limit)
    repo.add_record(
        "limit-was-never-honoured",
        classification="patch",
        summary="The spec listed a parameter the server never read.",
        detector_override={"reason": "Declares behaviour the server already had."},
    )

    assert _check(repo).errors == []


# ---------------------------------------------------------------------------
# The contract and the records agree about deprecations
# ---------------------------------------------------------------------------


def test_marking_a_deprecation_needs_a_record_declaring_it(tmp_path: Path) -> None:
    repo = make_substrate_repo(tmp_path / "repo")
    repo.checkout("feature", create=True)
    repo.edit_yaml(CORE_SPEC, _mark_limit)
    repo.add_record("deprecate-limit", **_adaptation())

    message = _errors(_check(repo))

    assert "no added record's deprecates names it" in message
    assert f"deprecation-added at sage_core_api:{LIMIT}" in message


def test_a_deprecation_marked_and_declared_passes(tmp_path: Path) -> None:
    repo = make_substrate_repo(tmp_path / "repo")
    repo.checkout("feature", create=True)
    repo.edit_yaml(CORE_SPEC, _mark_limit)
    repo.add_record("deprecate-limit", **DEPRECATION)

    result = _check(repo)

    assert result.errors == []
    assert result.categories == ["caller-adaptation"]


def test_an_mcp_deprecation_marked_and_declared_passes(tmp_path: Path) -> None:
    repo = make_substrate_repo(tmp_path / "repo")
    repo.checkout("feature", create=True)
    repo.edit_json(
        CATALOG,
        lambda c: c["surfaces"]["sage"][0].__setitem__("description", "Deprecated: use find."),
    )
    repo.add_record(
        "deprecate-search",
        **{
            **DEPRECATION,
            "deprecates": [
                {
                    "surface": "mcp",
                    "pointer": "sage/search",
                    "replacement": "find",
                    "earliest_adaptation": "2027-01-01",
                }
            ],
        },
    )

    assert _check(repo).errors == []


def test_declaring_a_deprecation_the_contract_does_not_mark_fails(tmp_path: Path) -> None:
    repo = make_substrate_repo(tmp_path / "repo")
    repo.checkout("feature", create=True)
    repo.edit_yaml(
        CORE_SPEC, lambda s: s["paths"]["/things"]["get"].__setitem__("summary", "Reworded.")
    )
    repo.add_record("deprecate-limit", **DEPRECATION)

    message = _errors(_check(repo))

    assert "does not mark it deprecated" in message
    assert LIMIT in message


def test_a_deprecated_default_needs_no_contract_mark(tmp_path: Path) -> None:
    repo = make_substrate_repo(tmp_path / "repo")
    repo.checkout("feature", create=True)
    repo.edit_yaml(
        CORE_SPEC,
        lambda s: s["paths"]["/things"]["get"]["parameters"][0].__setitem__(
            "description", "Page size. The default of 10 is deprecated."
        ),
    )
    repo.add_record(
        "deprecate-limit-default",
        **{
            **DEPRECATION,
            "deprecates": [
                {**DEPRECATION["deprecates"][0], "pointer": f"{LIMIT}/schema/default", "value": 10}
            ],
        },
    )

    assert _check(repo).errors == []


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_the_json_report_and_exit_status_carry_the_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_substrate_repo(tmp_path / "repo")
    repo.checkout("feature", create=True)
    repo.edit_yaml(CORE_SPEC, _remove_limit)
    repo.add_record("remove-limit", **_adaptation())

    code = main(["check", "--repo-root", str(repo.root), "--base", "main", "--format", "json"])
    report = json.loads(capsys.readouterr().out)

    assert code == 1
    assert any("claims an exemption" in error for error in report["errors"])


def _require_kind(spec: dict[str, Any]) -> None:
    thing = spec["components"]["schemas"]["Thing"]
    thing["properties"]["kind"] = {"type": "string", "description": "Kind."}
    thing["required"].append("kind")


def _default_limit(spec: dict[str, Any]) -> None:
    spec["paths"]["/things"]["get"]["parameters"][0]["schema"]["default"] = 20


def _narrow_limit(spec: dict[str, Any]) -> None:
    spec["paths"]["/things"]["get"]["parameters"][0]["schema"]["maximum"] = 50


@pytest.mark.parametrize(
    ("mutate", "kind"),
    [
        (_require_kind, "required-added"),
        (_default_limit, "default-changed"),
        (_narrow_limit, "constraint-tightened"),
    ],
    ids=["new-requirement", "changed-default", "narrowed-input"],
)
def test_a_narrowing_or_changed_default_never_deprecated_fails(
    tmp_path: Path, mutate: Any, kind: str
) -> None:
    repo = make_substrate_repo(tmp_path / "repo")
    repo.checkout("feature", create=True)
    repo.edit_yaml(CORE_SPEC, mutate)
    repo.add_record("adapt", **_adaptation())

    result = _check(repo)

    assert kind in [f.kind for f in result.findings]
    assert f"{kind} at sage_core_api:" in _errors(result)


@pytest.mark.parametrize(
    "malformed",
    [
        {"follows_deprecation": "3.1"},
        {"follows_deprecation": ["3.1"]},
        {"deprecates": {"pointer": LIMIT}},
        {"exemption": "never-served"},
    ],
    ids=[
        "follows-a-string",
        "follows-list-of-strings",
        "deprecates-a-mapping",
        "exemption-a-string",
    ],
)
def test_a_malformed_record_is_refused_not_raised_on(
    tmp_path: Path, malformed: dict[str, Any]
) -> None:
    repo = make_substrate_repo(tmp_path / "repo")
    repo.checkout("feature", create=True)
    repo.edit_yaml(CORE_SPEC, _remove_limit)
    repo.add_record("remove-limit", **_adaptation(**malformed))

    message = _errors(_check(repo))

    assert "remove-limit.yaml" in message, "the schema refusal is not reported"
    assert "claims an exemption" in message, "a malformed field was read as coverage"


def _post_things(schema: dict[str, Any]) -> Any:
    def mutate(spec: dict[str, Any]) -> None:
        spec["paths"]["/things"]["post"] = {
            "summary": "Create a thing.",
            "operationId": "create_thing",
            "requestBody": {
                "required": False,
                "content": {"application/json": {"schema": schema}},
            },
            "responses": {"201": {"description": "Created."}},
        }

    return mutate


_UNION = {"oneOf": [{"type": "string"}, {"type": "integer"}]}


@pytest.mark.parametrize(
    ("base", "mutate", "kind"),
    [
        (
            _post_things({"type": "object"}),
            lambda s: s["paths"]["/things"]["post"].pop("requestBody"),
            "request-body-removed",
        ),
        (
            None,
            lambda s: s["paths"]["/things"]["get"]["responses"].pop("200"),
            "response-removed",
        ),
        (
            _post_things(_UNION),
            lambda s: s["paths"]["/things"]["post"]["requestBody"]["content"]["application/json"][
                "schema"
            ]["oneOf"].pop(),
            "branch-removed",
        ),
    ],
    ids=["request-body-removed", "success-response-removed", "request-branch-removed"],
)
def test_every_withdrawal_shape_needs_a_deprecation(
    tmp_path: Path, base: Any, mutate: Any, kind: str
) -> None:
    repo = make_substrate_repo(tmp_path / "repo")
    if base is not None:
        repo.edit_yaml(CORE_SPEC, base)
        repo.commit("the element exists on main")
    repo.checkout("feature", create=True)
    repo.edit_yaml(CORE_SPEC, mutate)
    repo.add_record("adapt", **_adaptation())

    result = _check(repo)

    assert kind in [f.kind for f in result.findings]
    assert f"{kind} at sage_core_api:" in _errors(result)


def test_a_declared_earliest_date_that_is_not_a_date_fails(tmp_path: Path) -> None:
    repo = make_substrate_repo(tmp_path / "repo")
    repo.checkout("feature", create=True)
    repo.edit_yaml(CORE_SPEC, _mark_limit)
    repo.add_record(
        "deprecate-limit",
        **{
            **DEPRECATION,
            "deprecates": [{**DEPRECATION["deprecates"][0], "earliest_adaptation": "2026-02-30"}],
        },
    )

    message = _errors(_check(repo))

    assert "'2026-02-30', which is not a date" in message


def test_an_mcp_flag_and_prefix_together_are_one_undeclared_deprecation(tmp_path: Path) -> None:
    repo = make_substrate_repo(tmp_path / "repo")
    repo.checkout("feature", create=True)

    def mark(catalog: dict[str, Any]) -> None:
        query = catalog["surfaces"]["sage"][0]["inputSchema"]["properties"]["query"]
        query["deprecated"] = True
        query["description"] = "Deprecated: use text."

    repo.edit_json(CATALOG, mark)
    repo.add_record("deprecate-query", **_adaptation())

    result = _check(repo)

    undeclared = [e for e in result.errors if "no added record's deprecates names it" in e]
    assert len(undeclared) == 1, result.errors


def _mode_enum(spec: dict[str, Any]) -> None:
    spec["paths"]["/things"]["get"]["parameters"][0]["schema"] = {
        "type": "string",
        "enum": ["keyword", "semantic"],
    }


MODE = "paths//things/get/parameters/query:limit/schema/enum"


@pytest.mark.parametrize(
    ("removed", "passes"),
    [("keyword", True), ("semantic", False)],
    ids=["the-deprecated-value", "another-value"],
)
def test_a_value_deprecation_covers_that_value_only(
    tmp_path: Path, removed: str, passes: bool
) -> None:
    deprecation = {
        **DEPRECATION,
        "deprecates": [{**DEPRECATION["deprecates"][0], "pointer": f"{MODE}/keyword"}],
    }
    repo = make_substrate_repo(tmp_path / "repo")
    repo.edit_yaml(CORE_SPEC, _mode_enum)
    _publish(repo, "3.1", TODAY - dt.timedelta(days=60), [deprecation])
    repo.commit("release 3.1 deprecates the keyword value")
    repo.checkout("feature", create=True)

    def remove(spec: dict[str, Any]) -> None:
        spec["paths"]["/things"]["get"]["parameters"][0]["schema"]["enum"].remove(removed)

    repo.edit_yaml(CORE_SPEC, remove)
    repo.add_record(
        "remove-value",
        **_adaptation(follows_deprecation=_follows(pointer=f"{MODE}/keyword")),
    )

    result = _check(repo)

    assert [f.pointer for f in result.findings] == [f"{MODE}/{removed}"]
    assert (result.errors == []) is passes, result.errors


def _post_without_body(spec: dict[str, Any]) -> None:
    _post_things({"type": "object"})(spec)
    spec["paths"]["/things"]["post"].pop("requestBody")


def _add_body(required: bool) -> Any:
    def mutate(spec: dict[str, Any]) -> None:
        spec["paths"]["/things"]["post"]["requestBody"] = {
            "required": required,
            "content": {"application/json": {"schema": {"type": "object"}}},
        }

    return mutate


@pytest.mark.parametrize(
    ("required", "category", "held"),
    [(False, "capability", False), (True, "caller-adaptation", True)],
    ids=["optional-body-is-a-capability", "required-body-is-held"],
)
def test_an_added_request_body_is_held_only_when_required(
    tmp_path: Path, required: bool, category: str, held: bool
) -> None:
    repo = make_substrate_repo(tmp_path / "repo")
    repo.edit_yaml(CORE_SPEC, _post_without_body)
    repo.commit("a bodiless create exists on main")
    repo.checkout("feature", create=True)
    repo.edit_yaml(CORE_SPEC, _add_body(required))
    repo.add_record("body", **_adaptation(category=["capability", "caller-adaptation"]))

    result = _check(repo)

    assert [(f.kind, f.category) for f in result.findings] == [("request-body-added", category)]
    assert ("request-body-added at sage_core_api:" in _errors(result)) is held, result.errors


def test_a_value_less_enum_deprecation_is_refused(tmp_path: Path) -> None:
    """A bare ``.../enum`` would cover every value's removal, so it must name one."""
    repo = make_substrate_repo(tmp_path / "repo")
    repo.edit_yaml(CORE_SPEC, _mode_enum)
    repo.commit("the enum exists on main")
    repo.checkout("feature", create=True)
    repo.edit_yaml(
        CORE_SPEC,
        lambda s: s["paths"]["/things"]["get"]["parameters"][0].__setitem__(
            "description", "Page size. Some values are deprecated."
        ),
    )
    repo.add_record(
        "deprecate-enum",
        **{**DEPRECATION, "deprecates": [{**DEPRECATION["deprecates"][0], "pointer": MODE}]},
    )

    message = _errors(_check(repo))

    assert f"deprecates names sage_core_api:{MODE}, but" in message


def test_a_deprecated_value_needs_no_contract_mark(tmp_path: Path) -> None:
    repo = make_substrate_repo(tmp_path / "repo")
    repo.edit_yaml(CORE_SPEC, _mode_enum)
    repo.commit("the enum exists on main")
    repo.checkout("feature", create=True)
    repo.edit_yaml(
        CORE_SPEC,
        lambda s: s["paths"]["/things"]["get"]["parameters"][0].__setitem__(
            "description", "Page size. The keyword value is deprecated."
        ),
    )
    repo.add_record(
        "deprecate-keyword",
        **{
            **DEPRECATION,
            "deprecates": [
                {**DEPRECATION["deprecates"][0], "pointer": f"{MODE}/keyword", "value": "keyword"}
            ],
        },
    )

    assert _check(repo).errors == []


def test_a_deprecated_value_format_needs_no_contract_mark(tmp_path: Path) -> None:
    """A format is a class of values, stated only in its parameter's description."""
    repo = make_substrate_repo(tmp_path / "repo")
    repo.checkout("feature", create=True)
    repo.edit_yaml(
        CORE_SPEC,
        lambda s: s["paths"]["/things"]["get"]["parameters"][0].__setitem__(
            "description", "Page size. A timestamp value is deprecated."
        ),
    )
    repo.add_record(
        "deprecate-limit-timestamps",
        **{
            **DEPRECATION,
            "deprecates": [
                {**DEPRECATION["deprecates"][0], "pointer": f"{LIMIT}/schema/format/timestamp"}
            ],
        },
    )

    assert _check(repo).errors == []


def test_a_format_less_deprecation_is_refused(tmp_path: Path) -> None:
    """A bare ``.../format`` names no class of values, so it is held to a mark."""
    repo = make_substrate_repo(tmp_path / "repo")
    repo.checkout("feature", create=True)
    repo.edit_yaml(
        CORE_SPEC,
        lambda s: s["paths"]["/things"]["get"]["parameters"][0].__setitem__(
            "description", "Page size. Some values are deprecated."
        ),
    )
    pointer = f"{LIMIT}/schema/format"
    repo.add_record(
        "deprecate-limit-format",
        **{**DEPRECATION, "deprecates": [{**DEPRECATION["deprecates"][0], "pointer": pointer}]},
    )

    message = _errors(_check(repo))

    assert f"deprecates names sage_core_api:{pointer}, but" in message
