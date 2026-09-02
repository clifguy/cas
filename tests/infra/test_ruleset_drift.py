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
``SAGE_TEST_LIVE_RULESET=1``; the default test job has neither network access
nor a token. Each detector is driven against mutated input so a comparison that
silently compares nothing cannot pass vacuously.

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
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from scripts.check_ruleset_drift import (
    MISSING,
    RULESET_NAME,
    VOLATILE_BYPASS_ACTOR_KEYS,
    VOLATILE_TOP_LEVEL_KEYS,
    diff_rulesets,
    extract_captured_ruleset,
    fetch_live_ruleset,
    format_report,
    main,
    normalize_ruleset,
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
# The gate: the command-line contract
# ---------------------------------------------------------------------------


def test_missing_token_skips_with_exit_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """S1: with no token configured the check logs and exits 0 rather than
    failing, so the surrounding automation stays dormant until the secret is
    provisioned -- and it does not reach the forge on the way there."""
    monkeypatch.delenv("GH_RULESET_TOKEN", raising=False)

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the check must not shell out with no token configured")

    monkeypatch.setattr("scripts.check_ruleset_drift._run", _explode)

    assert main(["--repo", "owner/repo"]) == 0


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


def test_workflow_references_the_token_secret_and_the_script() -> None:
    """W3: the ruleset read is wired to the fine-grained token secret and to
    the check module.

    The check exits 0 when that secret is absent, so nothing at run time
    complains about a workflow that was never wired to it. This assertion is
    what keeps the dormant path honest -- which is why it reads the parsed step
    rather than the file's text: the secret is named in this workflow's own
    header comment, so a text search would go on passing after the ``env``
    binding that actually supplies it was deleted.
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
        "secrets.RULESET_READ_TOKEN" in (step.get("env") or {}).get("GH_RULESET_TOKEN", "")
        for step in steps
    ), "the ruleset read needs the fine-grained token in its step env"


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

    Opt-in because the default test job has neither network access nor a
    token; this is the same comparison the scheduled workflow makes, available
    as a faster signal while editing the document.
    """
    repo = os.environ.get("GH_REPO") or ""
    live = fetch_live_ruleset(repo or None, RULESET_NAME, os.environ.get("GH_RULESET_TOKEN"))

    divergences = diff_rulesets(_captured(), normalize_ruleset(live))

    assert divergences == [], format_report(divergences)
