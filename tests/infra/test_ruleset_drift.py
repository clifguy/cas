"""Tests for the captured-versus-live ruleset drift check and its workflow.

``docs/process/branch_protection.md`` reproduces the ``main-protection``
ruleset as a JSON block and declares that block the source of truth, with the
forge's UI as its reflection. The drift check reads the live ruleset and
compares it against that block, naming every diverging *leaf* -- a flipped flag
inside a rule's ``parameters`` names that flag, not the ``rules`` array
containing it, because reporting at the granularity of the top-level keys would
say only that something changed.

Two directions fail differently, and the second is the one a scheduled check
catches. A change made in the document and never applied leaves the repository
under-protected relative to what the document claims. A change made in the UI
and never captured leaves the document asserting something untrue, which is
worse, because the claim to be the source of truth is what the rest of that
document rests on -- and a UI edit carries no commit for an offline gate to
hang off.

Everything here but the final test is offline: the live shape is *derived* from
the tracked document by adding back the forge-internal keys the capture omits,
so the fixtures cannot drift from the document they gate and no forge-internal
identifier enters the tree. The live read itself is an opt-in tier behind
``SAGE_TEST_LIVE_RULESET=1``, which is unset in the default test job -- that,
rather than any network restriction, is why the tier does not run there. Each
detector is driven against mutated input so a comparison that silently compares
nothing cannot pass vacuously.

The two live callers read at different scopes, and the U cases below cover both.
The scheduled run holds only repository read, at which the forge withholds
``bypass_actors`` by omitting the key; a maintainer's own credential carries
administration scope and receives it. So the field must be excluded when absent
and compared when present, and an empty list -- a real ruleset value -- must
never be mistaken for the withholding.

**One gap the offline tier cannot close, recorded rather than solved.** Because
the live shape is derived from the capture, a key the forge starts sending that
this module does not already enumerate is absent from both sides, and every
offline test passes. Only the opt-in tier and the scheduled run read a real
payload. The gap fails safe: an unenumerated key surfaces as a divergence
naming that key, on a ruleset nobody touched, and the fix is to add it to the
volatile set. Tolerating unknown keys wholesale is not the alternative -- that
is precisely how a rule added in the UI would go unseen.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from scripts.check_ruleset_drift import (
    MISSING,
    RULESET_NAME,
    VOLATILE_BYPASS_ACTOR_KEYS,
    VOLATILE_TOP_LEVEL_KEYS,
    _keyed_by,
    _run,
    diff_rulesets,
    extract_captured_ruleset,
    fetch_live_ruleset,
    format_report,
    main,
    normalize_ruleset,
    uncovered_keys,
)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
WORKFLOW: Final[Path] = REPO_ROOT / ".github" / "workflows" / "ruleset-drift.yml"
BRANCH_PROTECTION_DOC: Final[Path] = REPO_ROOT / "docs" / "process" / "branch_protection.md"

LIVE_TIER_ENV: Final[str] = "SAGE_TEST_LIVE_RULESET"

requires_live_ruleset = pytest.mark.skipif(
    os.environ.get(LIVE_TIER_ENV) != "1" or shutil.which("gh") is None,
    reason=(
        f"live-ruleset comparison is opt-in: set {LIVE_TIER_ENV}=1 with an "
        "authenticated gh on PATH (reads the forge API)"
    ),
)

# Paths into the captured ruleset the mutation controls below reach for. Named
# once so a rename in the document surfaces as one edit here rather than as
# eight silently-stale string literals.
_STRICT_POLICY_PATH: Final[str] = (
    "rules[required_status_checks].parameters.strict_required_status_checks_policy"
)
_MERGE_METHODS_PATH: Final[str] = "rules[pull_request].parameters.allowed_merge_methods"
_EXTRA_APPROVAL_PATH: Final[str] = (
    "rules[pull_request].parameters.require_extra_approval_for_unattributed_changes"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _captured() -> dict[str, Any]:
    """The ruleset the tracked document captures."""
    return extract_captured_ruleset(BRANCH_PROTECTION_DOC.read_text(encoding="utf-8"))


def _forge_decorated(captured: dict[str, Any]) -> dict[str, Any]:
    """The captured block in the shape the forge API returns it.

    The API adds identifiers and timestamps the capture deliberately omits.
    Rebuilding the live shape from the document -- rather than committing a
    recorded response -- keeps these tests from drifting away from the document
    they gate, and keeps the repository's real ruleset id and bypass-actor id
    out of the tree, which the public-posture gate forbids the document to
    carry and this file has no better claim to.

    The values are synthetic on purpose: normalization strips these keys
    whatever they hold, so a placeholder proves the stripping exactly as well
    as the real value would.
    """
    live = copy.deepcopy(captured)
    live.update(
        {
            "id": 999999999,
            "node_id": "RRS_synthetic",
            "source": "owner/repo",
            "source_type": "Repository",
            "created_at": "2026-01-01T00:00:00.000-00:00",
            "updated_at": "2026-01-02T00:00:00.000-00:00",
            "current_user_can_bypass": "always",
            "_links": {"self": {"href": "https://example.invalid/rulesets/999999999"}},
        }
    )
    for actor in live.get("bypass_actors") or []:
        actor["actor_id"] = 99
    return live


def _rule(ruleset: dict[str, Any], rule_type: str) -> dict[str, Any]:
    for rule in ruleset.get("rules") or []:
        if isinstance(rule, dict) and rule.get("type") == rule_type:
            return rule
    raise AssertionError(f"captured ruleset has no {rule_type!r} rule")


def _parameters(ruleset: dict[str, Any], rule_type: str) -> dict[str, Any]:
    return _rule(ruleset, rule_type)["parameters"]


def _paths(divergences: list) -> list[str]:
    return [d.path for d in divergences]


def _on_block(workflow: dict) -> dict:
    """The workflow's trigger block.

    PyYAML parses a bare ``on:`` key as the boolean ``True`` under YAML 1.1
    rules, so the trigger block is keyed by ``True`` rather than ``"on"``.
    """
    block = workflow.get(True)
    if block is None:
        block = workflow.get("on")
    return block or {}


# ---------------------------------------------------------------------------
# The gate: normalization
# ---------------------------------------------------------------------------


def test_normalize_drops_forge_internal_top_level_keys() -> None:
    """N1: the identifiers and timestamps the capture omits are stripped, and
    the substantive keys survive."""
    normalized = normalize_ruleset(_forge_decorated(_captured()))

    for key in VOLATILE_TOP_LEVEL_KEYS:
        assert key not in normalized, f"{key!r} is forge-internal and must be normalized away"
    for key in ("name", "target", "enforcement", "conditions", "rules", "bypass_actors"):
        assert key in normalized, f"{key!r} is substantive and must survive normalization"


def test_normalize_drops_bypass_actor_id_only() -> None:
    """N2: inside a bypass actor, the numeric id goes and the role stays."""
    normalized = normalize_ruleset(_forge_decorated(_captured()))

    actors = normalized["bypass_actors"]
    assert actors, "the captured ruleset grants a bypass; normalization must not drop the list"
    for actor in actors:
        for key in VOLATILE_BYPASS_ACTOR_KEYS:
            assert key not in actor, f"{key!r} varies per repository and must be normalized away"
        assert actor["actor_type"]
        assert actor["bypass_mode"]


def test_normalized_live_shape_equals_the_captured_block() -> None:
    """N3: normalization strips exactly the keys the capture omits.

    The round-trip pins the boundary in both directions at once: stripping too
    little leaves an identifier behind, stripping too much takes a substantive
    key with it, and either way the two sides stop being equal.
    """
    captured = _captured()
    assert captured.get("name") == RULESET_NAME, "the document must carry the captured block"

    assert normalize_ruleset(_forge_decorated(captured)) == captured


# ---------------------------------------------------------------------------
# The gate: divergence is reported at leaf granularity
# ---------------------------------------------------------------------------


def test_flipped_strict_policy_names_the_leaf_flag() -> None:
    """D1: the motivating case -- the up-to-date policy flipped on in the UI.

    The flag is named, not the ``rules`` array containing it. Reporting the
    containing array would satisfy 'names the diverging field' while losing
    almost all of its value, so the containing paths are asserted absent.
    """
    captured = _captured()
    live = _forge_decorated(captured)
    _parameters(live, "required_status_checks")["strict_required_status_checks_policy"] = True

    divergences = diff_rulesets(captured, normalize_ruleset(live))

    assert _paths(divergences) == [_STRICT_POLICY_PATH]
    assert divergences[0].captured is False
    assert divergences[0].live is True
    assert "rules" not in _paths(divergences)
    assert "rules[required_status_checks]" not in _paths(divergences)


def test_extra_merge_method_names_the_merge_methods_leaf() -> None:
    """D2: a drift that has actually happened -- the document permitted three
    merge methods where the live ruleset allowed only squash."""
    captured = _captured()
    live = _forge_decorated(captured)
    _parameters(captured, "pull_request")["allowed_merge_methods"] = ["squash", "merge", "rebase"]

    divergences = diff_rulesets(captured, normalize_ruleset(live))

    assert _paths(divergences) == [_MERGE_METHODS_PATH]
    assert divergences[0].captured == ["squash", "merge", "rebase"]
    assert divergences[0].live == ["squash"]


def test_missing_parameter_is_reported_as_a_leaf() -> None:
    """D3: the second drift that happened -- a parameter the document omitted
    entirely. The absent side reports the MISSING sentinel rather than a
    default that would read as agreement."""
    captured = _captured()
    live = _forge_decorated(captured)
    del _parameters(captured, "pull_request")["require_extra_approval_for_unattributed_changes"]

    divergences = diff_rulesets(captured, normalize_ruleset(live))

    assert _paths(divergences) == [_EXTRA_APPROVAL_PATH]
    assert divergences[0].captured is MISSING
    assert divergences[0].live is True


def test_rule_added_live_is_reported_by_rule_type() -> None:
    """D4: a whole rule added in the UI is named by its type.

    This is the direction where the capture omits something new, and it cannot
    be found by projecting the live payload onto the shape the capture already
    has.
    """
    captured = _captured()
    live = _forge_decorated(captured)
    live["rules"].append({"type": "required_signatures"})

    divergences = diff_rulesets(captured, normalize_ruleset(live))

    assert _paths(divergences) == ["rules[required_signatures]"]
    assert divergences[0].captured is MISSING


def test_required_check_context_change_names_the_context() -> None:
    """D5: a renamed required check names both contexts, rather than diffing
    the whole check list as one opaque leaf."""
    captured = _captured()
    live = _forge_decorated(captured)
    for check in _parameters(live, "required_status_checks")["required_status_checks"]:
        if check["context"] == "eslint":
            check["context"] = "eslint2"

    divergences = diff_rulesets(captured, normalize_ruleset(live))

    prefix = "rules[required_status_checks].parameters.required_status_checks"
    assert sorted(_paths(divergences)) == [f"{prefix}[eslint2]", f"{prefix}[eslint]"]


def test_merge_method_order_is_not_divergence() -> None:
    """D6: a reordered scalar list is not drift.

    The forge does not promise an order for these lists. An order-sensitive
    comparison would report drift on a ruleset nobody touched, and a check that
    cries wolf on a schedule is a check that gets muted.
    """
    captured = _captured()
    live = _forge_decorated(captured)
    _parameters(captured, "pull_request")["allowed_merge_methods"] = ["squash", "merge"]
    _parameters(live, "pull_request")["allowed_merge_methods"] = ["merge", "squash"]

    assert diff_rulesets(captured, normalize_ruleset(live)) == []


def test_same_length_different_contents_is_divergence() -> None:
    """D8: two scalar lists of equal length disagree on their contents.

    The pair D2 (different lengths) and D6 (same contents reordered) is
    satisfied by a comparison that reads nothing but ``len``, which would let a
    merge method be swapped for another without a word. This is the only case
    that separates comparing the contents from counting them.
    """
    captured = _captured()
    live = _forge_decorated(captured)
    _parameters(captured, "pull_request")["allowed_merge_methods"] = ["squash", "merge"]
    _parameters(live, "pull_request")["allowed_merge_methods"] = ["squash", "rebase"]

    divergences = diff_rulesets(captured, normalize_ruleset(live))

    assert _paths(divergences) == [_MERGE_METHODS_PATH]
    assert divergences[0].captured == ["squash", "merge"]
    assert divergences[0].live == ["squash", "rebase"]


def test_identical_rulesets_have_no_divergences() -> None:
    """D7: the negative control -- agreement reports nothing.

    Without this, a comparison that always finds something would satisfy every
    mutation case above.
    """
    captured = _captured()

    assert diff_rulesets(captured, normalize_ruleset(_forge_decorated(captured))) == []


def test_report_names_every_diverging_field_and_both_of_its_values() -> None:
    """The rendered report carries each leaf path and the value on each side.

    Asserting only that the words 'captured' and 'live' appear would pass
    against a report that printed the labels and dropped what follows them,
    which is the half a reader actually needs, so each expected value is
    matched against the line that carries its label.
    """
    captured = _captured()
    live = _forge_decorated(captured)
    _parameters(live, "required_status_checks")["strict_required_status_checks_policy"] = True
    _parameters(live, "pull_request")["required_approving_review_count"] = 1

    report = format_report(diff_rulesets(captured, normalize_ruleset(live)))
    lines = [line.strip() for line in report.splitlines()]

    assert _STRICT_POLICY_PATH in report
    assert "rules[pull_request].parameters.required_approving_review_count" in report
    assert "captured: false" in lines and "live:     true" in lines
    assert "captured: 0" in lines and "live:     1" in lines


def test_report_on_agreement_says_so() -> None:
    """An empty divergence list renders as agreement rather than as an empty
    report, so a run that found nothing says what it found nothing of."""
    report = format_report([])

    assert RULESET_NAME in report
    assert "agrees" in report


# ---------------------------------------------------------------------------
# The gate: a field withheld by scope is reported, never quietly compared
# ---------------------------------------------------------------------------


def test_withheld_bypass_actors_are_named_uncovered_not_diverged() -> None:
    """U1: a caller without administration scope receives no ``bypass_actors``
    key, and that must not read as the grant having been removed.

    This is the shape the scheduled run sees. Reporting it as divergence would
    fail every run on a ruleset nobody touched, which is how a check earns being
    ignored.
    """
    captured = _captured()
    live = _forge_decorated(captured)
    del live["bypass_actors"]

    uncovered = uncovered_keys(live)

    assert uncovered == frozenset({"bypass_actors"})
    assert diff_rulesets(captured, normalize_ruleset(live), uncovered=uncovered) == []


def test_empty_bypass_actors_is_covered_and_diverges() -> None:
    """U2: an *empty* list is a value, not a withholding.

    The load-bearing half of the pair. A ruleset that grants no bypass returns
    ``bypass_actors: []``; a caller that may not see the grants gets no key at
    all. Keying on the value rather than on the key's presence would collapse
    the two, and the removal of every bypass -- a real change to who can push
    straight to the default branch -- would be silently excused as a scope
    limit.
    """
    captured = _captured()
    live = _forge_decorated(captured)
    live["bypass_actors"] = []

    uncovered = uncovered_keys(live)

    assert uncovered == frozenset()
    assert _paths(diff_rulesets(captured, normalize_ruleset(live), uncovered=uncovered)) == [
        "bypass_actors[RepositoryRole]"
    ]


def test_visible_bypass_actors_are_compared() -> None:
    """U3: where the field IS visible, it is compared like any other.

    Guards the over-correction: excluding ``bypass_actors`` unconditionally
    passes U1 and U2 alike, and would blind the local and opt-in runs -- which
    do read it -- to a changed bypass mode.
    """
    captured = _captured()
    live = _forge_decorated(captured)
    live["bypass_actors"][0]["bypass_mode"] = "pull_request"

    uncovered = uncovered_keys(live)

    assert uncovered == frozenset()
    divergences = diff_rulesets(captured, normalize_ruleset(live), uncovered=uncovered)
    assert _paths(divergences) == ["bypass_actors[RepositoryRole].bypass_mode"]
    assert divergences[0].captured == "always"
    assert divergences[0].live == "pull_request"


def test_second_bypass_grant_is_divergence() -> None:
    """U5: a bypass grant added alongside an identical one is reported.

    The analogue of D4 for this list, and the case U1--U3 do not reach: they are
    all satisfied by an implementation that keys grants on ``actor_type`` alone,
    under which every ``RepositoryRole`` grant shares one identity and a second
    one is silently folded into the first. Who may push straight to the default
    branch is the last thing this check should lose quietly.
    """
    captured = _captured()
    live = _forge_decorated(captured)
    live["bypass_actors"].append(
        {"actor_id": 98, "actor_type": "RepositoryRole", "bypass_mode": "always"}
    )

    divergences = diff_rulesets(captured, normalize_ruleset(live), uncovered=uncovered_keys(live))

    assert _paths(divergences) == ["bypass_actors"]
    assert len(divergences[0].live) == 2


def test_removed_bypass_grant_is_divergence() -> None:
    """U6: the other direction -- every grant revoked is reported.

    Pairs with U5 so the fix cannot be a rule that only ever notices growth.
    """
    captured = _captured()
    live = _forge_decorated(captured)
    live["bypass_actors"] = []

    divergences = diff_rulesets(captured, normalize_ruleset(live), uncovered=uncovered_keys(live))

    assert _paths(divergences) == ["bypass_actors[RepositoryRole]"]
    assert divergences[0].live is MISSING


def test_colliding_identities_fall_through_to_whole_list_comparison() -> None:
    """U7: the mechanism, driven directly rather than through its one instance.

    Identity keying builds a dict, so a repeated identity drops a member. The
    rule is that keying requires uniqueness as well as presence -- stated here
    against a synthetic list so it is gated as a general property and not only
    where ``bypass_actors`` happens to exercise it.
    """
    assert _keyed_by([{"type": "a"}, {"type": "b"}], "type") is True
    assert _keyed_by([{"type": "a"}, {"type": "a"}], "type") is False
    assert _keyed_by([{"type": "a"}, {"other": 1}], "type") is False


def test_dict_list_member_key_order_is_not_divergence() -> None:
    """U8: two lists of equal mappings agree whatever order the keys came in.

    A dict's ``repr`` follows insertion order, so sorting a list of mappings by
    it can place equal members in different positions and report drift on a
    ruleset nobody touched. Dormant today -- ``required_reviewers`` is the only
    unkeyed dict list and it is empty -- and cheap to hold closed.
    """
    left = {"required_reviewers": [{"b": 1, "a": 2}, {"a": 9}]}
    right = {"required_reviewers": [{"a": 2, "b": 1}, {"a": 9}]}

    assert diff_rulesets(left, right) == []


def test_report_names_the_uncovered_field_even_on_agreement() -> None:
    """U4: a green run states what it did not look at.

    Agreement plus silence reads as broader coverage than the run had, which is
    exactly the misreading this whole check exists to prevent one level up.
    """
    report = format_report([], frozenset({"bypass_actors"}))

    assert "bypass_actors" in report
    assert "NOT COMPARED" in report
    assert "agrees" in report


# ---------------------------------------------------------------------------
# The gate: the command-line contract
# ---------------------------------------------------------------------------


def test_a_failed_read_is_raised_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """S1: a credential the forge rejects fails the run.

    The read carries no stored secret, so the only remaining credential failure
    is an ambient one -- expired, revoked, or absent on the runner. Degrading to
    exit 0 there would turn "I could not look" into "I looked and all is well",
    which is the reading this whole check exists to make impossible one level
    up.
    """
    monkeypatch.setattr(
        "scripts.check_ruleset_drift._run",
        lambda cmd: (_ for _ in ()).throw(
            RuntimeError("command failed (1): gh api\nBad credentials")
        ),
    )

    with pytest.raises(RuntimeError, match="Bad credentials"):
        main(["--repo", "owner/repo"])


def test_a_nonzero_forge_call_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """S1b: the raising happens in the runner, not only in its stand-in.

    S1 above replaces ``_run`` wholesale, so it says nothing about whether
    ``_run`` itself treats a rejected call as an error -- a version that
    returned the failed process unchanged passes S1 and would then parse an
    error body as a ruleset. This is the only test that reaches that decision.
    """
    monkeypatch.setattr(
        "subprocess.run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            args=["gh"], returncode=1, stdout="", stderr="Bad credentials"
        ),
    )

    with pytest.raises(RuntimeError, match="Bad credentials"):
        _run(["gh", "api", "repos/owner/repo/rulesets"])


def test_a_successful_forge_call_returns_its_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """S1c: and a call that succeeds is passed through untouched.

    Pairs with S1b: a ``_run`` that raised unconditionally would satisfy that
    test while making every real read impossible.
    """
    monkeypatch.setattr(
        "subprocess.run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            args=["gh"], returncode=0, stdout="[]", stderr=""
        ),
    )

    assert _run(["gh", "api", "repos/owner/repo/rulesets"]).stdout == "[]"


def test_divergence_exits_nonzero_and_names_the_leaf(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """S2: a diverging live ruleset supplied offline fails the check and names
    the diverging field on the way out."""
    captured = _captured()
    live = _forge_decorated(captured)
    _parameters(live, "required_status_checks")["strict_required_status_checks_policy"] = True
    ruleset_file = tmp_path / "live.json"
    ruleset_file.write_text(json.dumps(live), encoding="utf-8")

    assert main(["--ruleset-file", str(ruleset_file)]) == 1
    assert _STRICT_POLICY_PATH in capsys.readouterr().out


def test_agreement_exits_zero(tmp_path: Path) -> None:
    """S3: the same path with an undiverged ruleset succeeds.

    Paired with S2 this pins the exit code to the comparison; a check that
    always failed would satisfy S2 alone.
    """
    ruleset_file = tmp_path / "live.json"
    ruleset_file.write_text(json.dumps(_forge_decorated(_captured())), encoding="utf-8")

    assert main(["--ruleset-file", str(ruleset_file)]) == 0


# ---------------------------------------------------------------------------
# The gate: the scheduled workflow
# ---------------------------------------------------------------------------


def test_workflow_has_schedule_and_dispatch() -> None:
    """W1: the check runs on a cron schedule and supports manual dispatch.

    The motivating case is a UI edit with no commit attached, so a check that
    fires only when someone invokes it fires only when someone already
    suspects something.
    """
    on = _on_block(yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))

    assert "workflow_dispatch" in on, "workflow must allow manual dispatch"
    schedule = on.get("schedule") or []
    assert any("cron" in entry for entry in schedule), "workflow must declare a cron schedule"


def test_workflow_least_privilege_permissions() -> None:
    """W2: the check reads and reports; it writes nothing.

    Divergence surfaces as a failing run, so no issue-write scope is wanted
    here even though the sibling triage workflow holds one.
    """
    perms = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")).get("permissions") or {}

    assert perms.get("contents") == "read"
    assert "issues" not in perms, "divergence surfaces as a failing run, not as an issue"
    assert "id-token" not in perms, "no OIDC token needed"
    assert "security-events" not in perms


def test_workflow_references_the_token_and_the_script() -> None:
    """W3: the read is wired to a credential and to the check module.

    Read from the parsed step rather than the file's text: the credential is
    discussed in this workflow's own header comment, so a substring search would
    go on passing after the ``env`` binding that actually supplies it was
    deleted -- and an unauthenticated read fails the run rather than reporting
    agreement, so this is the wiring that keeps the run meaningful.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = [
        step
        for job in (workflow.get("jobs") or {}).values()
        for step in (job.get("steps") or [])
        if "scripts.check_ruleset_drift" in (step.get("run") or "")
    ]

    assert steps, "workflow must invoke the drift-check module"
    assert any(
        "secrets.GITHUB_TOKEN" in (step.get("env") or {}).get("GH_TOKEN", "") for step in steps
    ), "the ruleset read needs a credential bound in its step env"


# ---------------------------------------------------------------------------
# The gate: the document describes the check it is gated by
# ---------------------------------------------------------------------------


def test_reconciliation_procedure_names_the_check() -> None:
    """P1: the next person to edit the ruleset can see what will verify them."""
    text = BRANCH_PROTECTION_DOC.read_text(encoding="utf-8")
    _, _, reconciliation = text.partition("## Reconciliation procedure")

    assert reconciliation, "the document must carry a reconciliation procedure"
    assert WORKFLOW.name in reconciliation, (
        "the reconciliation procedure must name the check that verifies it"
    )


def test_document_does_not_deny_the_automatic_comparison() -> None:
    """P2: the superseded claim is replaced, not left standing beside the new
    text. A document that both describes a gate and denies it is the defect
    this check exists to prevent, one level up."""
    text = BRANCH_PROTECTION_DOC.read_text(encoding="utf-8")

    assert "Nothing compares the two automatically" not in text


# ---------------------------------------------------------------------------
# Opt-in tier: the live comparison itself
# ---------------------------------------------------------------------------


@requires_live_ruleset
def test_live_ruleset_matches_the_captured_block() -> None:
    """L1: the live ruleset agrees with the block the document calls the source
    of truth.

    Opt-in so that reaching the forge is always a deliberate choice rather than
    a side effect of running the suite; this is the same comparison the
    scheduled workflow makes, available as a faster signal while editing the
    document.

    Run from a workstation this covers *more* than the scheduled run does: a
    maintainer's credential carries administration scope, so ``bypass_actors``
    comes back and is compared. Hence the assertion on what was uncovered --
    without it, a run whose credential had quietly narrowed would still report
    agreement, having compared less than the reader assumes.
    """
    repo = os.environ.get("GH_REPO") or ""
    live = fetch_live_ruleset(repo or None, RULESET_NAME)
    uncovered = uncovered_keys(live)

    divergences = diff_rulesets(_captured(), normalize_ruleset(live), uncovered=uncovered)

    assert divergences == [], format_report(divergences, uncovered)
    assert uncovered == frozenset(), (
        "this credential could not read "
        f"{', '.join(sorted(uncovered))}; the comparison was narrower than it looks"
    )
