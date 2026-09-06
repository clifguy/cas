"""The failure-log instrument's own arithmetic, and the two refusals it rests on.

An instrument that miscounts is worse than none, because its output is what a
later reader trusts instead of re-deriving. Two properties carry most of that
risk and are pinned hardest here.

**Mixed-width SHA comparison.** The stored commit fields are seven characters
and git emits forty; Python compares the two as unequal and says nothing, so a
set-membership test over a window reads false for every element and the run
reports zero introductions on a window full of them. The guard must raise, and
the tests that pin it must also pin that it did not become an unconditional
raise -- a guard that refuses everything passes the refusal tests alone.

**Chain-head dedup.** The definitional rule is the supersession graph; the
lifecycle shortcut agrees with it on every ordinary chain, which is exactly why
substituting the shortcut is an easy and invisible mistake. The one fixture
that separates them inverts the lifecycle deliberately.

The storage-touching and git-touching halves are exercised by running the
script; what is pinned here is everything that could go wrong without a vault
or a repository to notice.
"""

from __future__ import annotations

from datetime import date

import pytest

from scripts.failure_metrics import (
    BASELINE_2_CAUGHT,
    BASELINE_2_COMMITS,
    BASELINE_2_INTRODUCED,
    BASELINE_2_OBSERVED,
    CONTAINED,
    DEFAULT_GATE_DATE,
    DEFAULT_SPAN_DAYS,
    DEFAULT_VAULT_ID,
    ESCAPED,
    FIRST_PARENT,
    NULL,
    ON_MAIN,
    OUT_OF_POPULATION,
    PROSE,
    SUBJECT_PREFIX,
    TICKET_ID,
    UNDETERMINED,
    UNRESOLVABLE,
    FailureRow,
    MergedCommit,
    ShaWidthError,
    Verification,
    VerificationRow,
    Window,
    _parse_args,
    chain_heads,
    compute_metrics,
    derive_containment,
    failure_id_sort_key,
    full_sha_set,
    in_window,
    map_to_merged_commit,
    render_report,
    render_verification,
    report_payload,
    same_commit,
)

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40


def _row(
    failure_id: str,
    *,
    doc_id: str | None = None,
    lifecycle: str = "active",
    when: date = date(2026, 9, 1),
    failure_class: str | None = "other",
    caught: bool | None = False,
    introduction: str | None = None,
    fix: str | None = None,
    reached_main: bool | None = None,
    reached_main_present: bool = True,
    tags: tuple[str, ...] = (),
) -> FailureRow:
    return FailureRow(
        doc_id=doc_id or f"doc_{failure_id.lower()}",
        failure_id=failure_id,
        lifecycle_status=lifecycle,
        document_date=when,
        failure_class=failure_class,
        caught_by_gate=caught,
        introduction_commit=introduction,
        fix_commit=fix,
        reached_main=reached_main,
        reached_main_present=reached_main_present,
        tags=tags,
    )


class FakeRepo:
    """A ``GitRepo`` over an in-memory commit graph, so no repository is needed."""

    def __init__(
        self,
        *,
        resolve: dict[str, str] | None = None,
        subjects: dict[str, str] | None = None,
        parents: dict[str, str] | None = None,
    ) -> None:
        self.resolve = resolve or {}
        self.subjects = subjects or {}
        self.parents = parents or {}
        self.rev_parse_calls: list[str] = []

    def rev_parse(self, rev: str) -> str | None:
        self.rev_parse_calls.append(rev)
        return self.resolve.get(rev)

    def subject(self, sha: str) -> str | None:
        return self.subjects.get(sha)

    def first_parent(self, sha: str) -> str | None:
        return self.parents.get(sha)


def _window(
    merged: tuple[MergedCommit, ...],
    *,
    commit_count: int | None = None,
    tip_date: date = date(2026, 9, 3),
    span_days: int = DEFAULT_SPAN_DAYS,
) -> Window:
    return Window(
        base="base",
        tip="tip",
        merged=merged,
        commit_count=commit_count if commit_count is not None else len(merged),
        tip_date=tip_date,
        span_days=span_days,
    )


def _merged(sha: str, subject: str = "Merged work", when: date = date(2026, 9, 1)):
    return MergedCommit(sha=sha, subject=subject, commit_date=when)


# ---------------------------------------------------------------------------
# A. The width the comparison refuses
# ---------------------------------------------------------------------------


def test_comparing_a_short_sha_against_a_full_one_raises():
    """Both spellings name the same commit, and the comparison must still refuse.

    Returning ``False`` here is the silent wrong answer: it is what plain
    string comparison already does, and it is indistinguishable from a genuine
    mismatch.
    """
    with pytest.raises(ShaWidthError):
        same_commit(SHA_A[:7], SHA_A)


def test_comparison_is_symmetric_in_the_width_it_refuses():
    """A guard on one argument only is half a guard."""
    with pytest.raises(ShaWidthError):
        same_commit(SHA_A, SHA_A[:7])


def test_two_full_shas_that_match_compare_equal():
    """Keeps the guard from being satisfied by refusing everything."""
    assert same_commit(SHA_A, SHA_A) is True


def test_two_full_shas_that_differ_compare_unequal():
    """The other half of the same control: a real mismatch still reads False."""
    assert same_commit(SHA_A, SHA_B) is False


def test_membership_against_a_window_set_refuses_a_short_probe():
    """Set membership is its own code path, and it is the one that actually bit.

    A scalar guard says nothing about ``short in {full, full, ...}``, which is
    where a window search silently finds nothing.
    """
    window = full_sha_set([SHA_A, SHA_B])
    with pytest.raises(ShaWidthError):
        in_window(SHA_A[:7], window)


def test_membership_accepts_a_full_probe_and_answers_both_ways():
    """The membership guard must still answer, not merely refuse."""
    window = full_sha_set([SHA_A, SHA_B])
    assert in_window(SHA_A, window) is True
    assert in_window(SHA_C, window) is False


def test_building_a_window_set_refuses_an_abbreviated_member():
    """An abbreviated value must not reach the set either, only the probe."""
    with pytest.raises(ShaWidthError):
        full_sha_set([SHA_A, SHA_B[:7]])


@pytest.mark.parametrize("value", ["4100d14x", "", "HEAD", "main", None, 40, SHA_A.upper()])
def test_a_malformed_sha_is_refused_rather_than_padded(value):
    """No normalization path may quietly accept a ref name or a wrong alphabet."""
    with pytest.raises(ShaWidthError):
        same_commit(value, SHA_A)


# ---------------------------------------------------------------------------
# B. Rows to failures
# ---------------------------------------------------------------------------


def test_rows_dedupe_to_failures_by_chain_head():
    """Three versions of one failure reduce to the row nothing supersedes.

    The rows arrive oldest-first, so the head is **last** in input order, and
    that ordering is the discriminating part of the fixture. With the head
    first, a rule that ignores the supersession graph entirely and takes the
    first row per id produces the same answer as the graph rule, and every
    assertion here passes against it. The store promises no order that would
    rule that rival out in production, so the fixture has to.
    """
    rows = [
        _row("F1", doc_id="v1", lifecycle="archived"),
        _row("F1", doc_id="v2", lifecycle="archived"),
        _row("F1", doc_id="v3", lifecycle="active"),
    ]
    edges = [("v3", "v2"), ("v2", "v1")]
    result = chain_heads(rows, edges)
    assert result.failure_count == 1
    assert result.heads["F1"].doc_id == "v3"
    assert result.row_count == 3


def test_the_head_is_the_row_no_supersedes_edge_points_at():
    """The graph decides, not the lifecycle -- pinned on a chain that disagrees.

    The head here is archived and its predecessor is active, which no ordinary
    chain looks like. That is the point: on an ordinary chain the lifecycle
    shortcut gives the same answer, so only an inverted fixture can tell the
    two rules apart.
    """
    rows = [
        _row("F2", doc_id="head", lifecycle="archived"),
        _row("F2", doc_id="pred", lifecycle="active"),
    ]
    result = chain_heads(rows, [("head", "pred")])
    assert result.heads["F2"].doc_id == "head"


def test_the_lifecycle_proxy_is_reported_when_it_disagrees():
    """A disagreement between the two rules is named, never silently resolved."""
    rows = [
        _row("F2", doc_id="head", lifecycle="archived"),
        _row("F2", doc_id="pred", lifecycle="active"),
    ]
    result = chain_heads(rows, [("head", "pred")])
    assert result.disagreements == ("F2",)
    assert result.edge_head_count == 1
    assert result.proxy_head_count == 1


def test_a_failure_id_with_two_heads_is_flagged_not_absorbed():
    """Two unlinked rows sharing an id are a collision, not a silent overwrite."""
    rows = [
        _row("F3", doc_id="one"),
        _row("F3", doc_id="two"),
    ]
    result = chain_heads(rows, [])
    assert result.multiple_heads == ("F3",)


def test_an_unsuperseded_singleton_is_its_own_head():
    rows = [_row("F4", doc_id="only")]
    result = chain_heads(rows, [])
    assert result.heads["F4"].doc_id == "only"
    assert result.multiple_heads == ()


def test_inverting_the_preference_selects_the_archived_row():
    """The census probe: how much of the count rests on the preference alone."""
    rows = [
        _row("F5", doc_id="new", lifecycle="active"),
        _row("F5", doc_id="old", lifecycle="archived"),
    ]
    result = chain_heads(rows, [("new", "old")], prefer_archived=True)
    assert result.heads["F5"].doc_id == "old"


def test_edges_outside_the_failure_record_set_are_ignored():
    """Edge enumeration is vault-wide; the intersection happens here.

    A ``supersedes`` edge between two documents of another type must not
    suppress a failure row that happens to share neither endpoint -- and must
    not suppress one that shares a *target* id by coincidence either.
    """
    rows = [_row("F6", doc_id="only")]
    foreign = [("steering_v2", "steering_v1"), ("steering_v1", "only")]
    result = chain_heads(rows, foreign)
    assert result.heads["F6"].doc_id == "only"


def test_ids_order_by_parsed_integer_not_string():
    """Ids are unpadded, so a string sort puts F100 before F2."""
    ordered = sorted(["F89", "F100", "F9", "F2"], key=failure_id_sort_key)
    assert ordered == ["F2", "F9", "F89", "F100"]


# ---------------------------------------------------------------------------
# C. Branch to merge
# ---------------------------------------------------------------------------


def test_a_commit_already_on_main_maps_to_itself():
    merged = (_merged(SHA_A),)
    repo = FakeRepo(resolve={"aaaaaaa": SHA_A})
    outcome = map_to_merged_commit("aaaaaaa", repo, merged)
    assert (outcome.merged_sha, outcome.reason) == (SHA_A, ON_MAIN)


def test_a_branch_commit_maps_by_subject_prefix():
    """A squash merge keeps the branch head's subject and appends its own suffix."""
    merged = (_merged(SHA_A, "Name the thing once (T-0001) (#42)"),)
    repo = FakeRepo(
        resolve={"bbbbbbb": SHA_B},
        subjects={SHA_B: "Name the thing once (T-0001)"},
    )
    outcome = map_to_merged_commit("bbbbbbb", repo, merged)
    assert (outcome.merged_sha, outcome.reason) == (SHA_A, SUBJECT_PREFIX)


def test_a_mid_branch_commit_maps_by_ticket_id():
    """A mid-branch subject the squash did not inherit still carries the id."""
    merged = (_merged(SHA_A, "Name the thing once (T-0001) (#42)"),)
    repo = FakeRepo(
        resolve={"bbbbbbb": SHA_B},
        subjects={SHA_B: "Fix up the helper (T-0001)"},
    )
    outcome = map_to_merged_commit("bbbbbbb", repo, merged)
    assert (outcome.merged_sha, outcome.reason) == (SHA_A, TICKET_ID)


def test_a_probe_commit_maps_by_first_parent_walk():
    """Neither prefix nor id: resolve through the nearest ancestor that does."""
    merged = (_merged(SHA_A, "Name the thing once (T-0001) (#42)"),)
    repo = FakeRepo(
        resolve={"ccccccc": SHA_C},
        subjects={SHA_C: "wip", SHA_B: "Name the thing once (T-0001)"},
        parents={SHA_C: SHA_B},
    )
    outcome = map_to_merged_commit("ccccccc", repo, merged)
    assert (outcome.merged_sha, outcome.reason) == (SHA_A, FIRST_PARENT)


def test_tier_order_is_prefix_then_ticket_then_ancestry():
    """The two tiers are made to disagree, because agreeing fixtures pin nothing.

    Subject prefix names one merged commit and the ticket id names another.
    The conventions put prefix first, so prefix must win.

    Order matters in the fixture: the commit that only the ticket-id tier can
    match comes *first*, so a scan that consults that tier first returns it and
    the two answers genuinely differ. With the prefix-matching commit first,
    both tiers return the same commit and the ordering is not constrained at
    all -- an agreeing fixture pins nothing.
    """
    merged = (
        _merged(SHA_D, "Some other work (T-0001) (#43)"),
        _merged(SHA_A, "Carry the change (T-0001) (#42)"),
    )
    repo = FakeRepo(
        resolve={"bbbbbbb": SHA_B},
        subjects={SHA_B: "Carry the change (T-0001)"},
    )
    outcome = map_to_merged_commit("bbbbbbb", repo, merged)
    assert outcome.merged_sha == SHA_A
    assert outcome.reason == SUBJECT_PREFIX


def test_an_unresolvable_sha_is_reported_not_dropped():
    """A garbage-collected branch SHA is an outcome, never a silent skip."""
    repo = FakeRepo(resolve={})
    outcome = map_to_merged_commit("deadbee", repo, (_merged(SHA_A),))
    assert (outcome.merged_sha, outcome.reason) == (None, UNRESOLVABLE)
    assert outcome.stored == "deadbee"


def test_a_prose_introduction_marker_is_classified_not_parsed():
    """A drift marker never reaches git, which would blur two unresolved states."""
    repo = FakeRepo(resolve={})
    outcome = map_to_merged_commit("(multiple, Apr 16-May 27)", repo, (_merged(SHA_A),))
    assert outcome.reason == PROSE
    assert repo.rev_parse_calls == []


def test_a_null_introduction_commit_is_classified_not_parsed():
    """Null and prose are distinct outcomes; only one names a real cluster."""
    repo = FakeRepo(resolve={})
    outcome = map_to_merged_commit(None, repo, (_merged(SHA_A),))
    assert outcome.reason == NULL
    assert repo.rev_parse_calls == []


def test_the_ancestry_walk_stops_at_the_branch_point():
    """Falling off a branch onto its base is a miss, not an attribution.

    The probe's parent is the commit the branch was cut from. That commit is on
    the default branch, but it is not the squash that carried this work --
    returning it would attribute the failure to whatever landed just before the
    branch was opened. Reporting the miss is the only honest answer.
    """
    merged = (_merged(SHA_A, "Something earlier (#40)"),)
    repo = FakeRepo(
        resolve={"ccccccc": SHA_C},
        subjects={SHA_C: "wip"},
        parents={SHA_C: SHA_A},
    )
    outcome = map_to_merged_commit("ccccccc", repo, merged)
    assert outcome.merged_sha is None
    assert outcome.reason == UNRESOLVABLE


def test_the_search_space_is_the_history_not_the_window():
    """A window is where a result is *tested*, never where it is *searched for*.

    Searching only the window leaves a post-window failure with nothing to
    match, so the ancestry tier walks back into the window and returns an
    in-window commit -- a confident wrong attribution that inflates the very
    numerator the window exists to bound.
    """
    in_window_commit = _merged(SHA_A, "Older work (T-0001) (#40)")
    post_window_commit = _merged(SHA_D, "Newer work (T-0002) (#99)")
    window = Window(
        base="base",
        tip="tip",
        merged=(in_window_commit,),
        commit_count=1,
        tip_date=date(2026, 9, 3),
        history=(post_window_commit, in_window_commit),
    )
    repo = FakeRepo(
        resolve={"bbbbbbb": SHA_B},
        subjects={SHA_B: "Newer work (T-0002)"},
    )
    outcome = map_to_merged_commit("bbbbbbb", repo, window.search_space)
    assert outcome.merged_sha == SHA_D

    # And the window still excludes it, which is the whole point of separating
    # the two: found honestly, then counted out.
    assert in_window(outcome.merged_sha, window.merged_shas) is False


def test_the_first_parent_walk_terminates_on_a_cycle():
    """A malformed parent chain terminates rather than hanging the run."""
    repo = FakeRepo(
        resolve={"ccccccc": SHA_C},
        subjects={SHA_C: "wip", SHA_B: "wip"},
        parents={SHA_C: SHA_B, SHA_B: SHA_C},
    )
    outcome = map_to_merged_commit("ccccccc", repo, (_merged(SHA_A, "unrelated"),))
    assert outcome.reason == UNRESOLVABLE


# ---------------------------------------------------------------------------
# D. Metric arithmetic
# ---------------------------------------------------------------------------


def _mapped_repo() -> FakeRepo:
    return FakeRepo(
        resolve={SHA_A[:7]: SHA_A, SHA_B[:7]: SHA_B, SHA_C[:7]: SHA_C, SHA_D[:7]: SHA_D}
    )


def test_the_introduction_denominator_is_unchanged_by_nulls():
    """An unattributable failure is not evidence of a clean commit.

    It leaves the numerator, and it must not also leave the denominator --
    dropping it there would understate the rate.
    """
    window = _window((_merged(SHA_A), _merged(SHA_B), _merged(SHA_C)), commit_count=10)
    heads = {
        "F1": _row("F1", introduction=SHA_A[:7]),
        "F2": _row("F2", introduction=SHA_B[:7]),
        "F3": _row("F3", introduction=SHA_C[:7]),
        "F4": _row("F4", introduction=None),
    }
    metrics = compute_metrics(heads, window, _mapped_repo())
    assert len(metrics.introduced_failures) == 3
    assert metrics.window_commits == 10
    assert metrics.introduction_rate_per_failure == pytest.approx(0.3)
    assert metrics.null_introduction == ("F4",)


def test_two_failures_on_one_merged_commit_count_once_per_commit():
    """Both variants are carried, because the baselines compare them like for like."""
    window = _window((_merged(SHA_A),), commit_count=10)
    heads = {
        "F1": _row("F1", introduction=SHA_A[:7]),
        "F2": _row("F2", introduction=SHA_A[:7]),
    }
    metrics = compute_metrics(heads, window, _mapped_repo())
    assert len(metrics.introduced_failures) == 2
    assert metrics.introduced_distinct_commits == 1
    assert metrics.introduction_rate_per_failure == pytest.approx(0.2)
    assert metrics.introduction_rate_per_commit == pytest.approx(0.1)


def test_observed_in_window_is_the_calendar_span_not_the_commit_set():
    """The gate-catch denominator is date-scoped; the introduction one is not.

    This record's introducing commit is outside the window entirely, and it is
    still observed in it.
    """
    window = _window((_merged(SHA_A),), commit_count=10)
    heads = {"F1": _row("F1", introduction=SHA_D[:7], when=date(2026, 8, 20), caught=True)}
    metrics = compute_metrics(heads, window, _mapped_repo())
    assert metrics.introduced_failures == ()
    assert metrics.observed_in_window == ("F1",)
    assert metrics.caught_by_gate == ("F1",)
    assert metrics.gate_catch_rate == pytest.approx(1.0)


def test_the_calendar_span_is_derived_from_the_tip_and_span_days():
    """Twenty-eight days back from the tip, not from the excluded base commit.

    The base is merely the last excluded commit and can sit anywhere before the
    span; deriving the span from its date would open the window early and admit
    records the published one did not.
    """
    window = _window((_merged(SHA_A),), tip_date=date(2026, 9, 3), span_days=28)
    assert window.span_start == date(2026, 8, 6)
    assert window.covers_date(date(2026, 8, 6)) is True
    assert window.covers_date(date(2026, 8, 5)) is False
    assert window.covers_date(date(2026, 9, 3)) is True
    assert window.covers_date(date(2026, 9, 4)) is False


def test_containment_derives_from_the_mapping_not_the_stored_flag():
    """One squash carried both the defect and its fix, whatever the row claims.

    The stored flag says the defect shipped; the mapping says both commits
    resolve to the same merge. The derivation wins and the disagreement is
    reported, because the stored value is a hygiene readout, not an input.
    """
    repo = FakeRepo(
        resolve={SHA_B[:7]: SHA_B, SHA_C[:7]: SHA_C},
        subjects={SHA_B: "Work (T-0001)", SHA_C: "Work (T-0001)"},
    )
    window = _window((_merged(SHA_A, "Work (T-0001) (#1)"),), commit_count=10)
    heads = {
        "F1": _row("F1", introduction=SHA_B[:7], fix=SHA_C[:7], reached_main=True),
    }
    intro = map_to_merged_commit(SHA_B[:7], repo, window.search_space)
    assert derive_containment(heads["F1"], intro, repo, window) == CONTAINED

    metrics = compute_metrics(heads, window, repo)
    assert metrics.contained == ("F1",)
    assert metrics.escaped == ()
    assert metrics.hygiene.stored_vs_derived_disagreement == ("F1",)


def test_a_null_fix_commit_leaves_containment_undetermined():
    """An unrecorded fix and an unfixed defect are indistinguishable from the row."""
    window = _window((_merged(SHA_A),), commit_count=10)
    row = _row("F1", introduction=SHA_A[:7], fix=None)
    verdict = derive_containment(
        row, map_to_merged_commit(SHA_A[:7], _mapped_repo(), window.merged), _mapped_repo(), window
    )
    assert verdict == UNDETERMINED


def test_a_null_introduction_resolves_to_escaped_before_the_fix_is_consulted():
    """Two rules could apply at once, and the precedence is fixed here.

    A failure with no introducing commit was observed against the working
    system, and the working system is the default branch -- so it escaped,
    whether or not a fix is recorded. That reasoning does not need the fix, so
    it outranks the null-fix rule.
    """
    window = _window((_merged(SHA_A),), commit_count=10)
    repo = _mapped_repo()
    row = _row("F1", introduction=None, fix=None)
    verdict = derive_containment(row, map_to_merged_commit(None, repo, window.merged), repo, window)
    assert verdict == ESCAPED


def test_records_omitting_reached_main_are_outside_the_population():
    """A subject that never ships through a pull request answers neither metric.

    Counting it as contained would credit the gates with a containment they
    never earned; counting it as escaped would debit a merge that never carried
    it. It is excluded, and reported as excluded rather than as undetermined.
    """
    window = _window((_merged(SHA_A),), commit_count=10)
    repo = _mapped_repo()
    row = _row("F1", introduction=SHA_A[:7], fix=SHA_A[:7], reached_main_present=False)
    verdict = derive_containment(
        row, map_to_merged_commit(SHA_A[:7], repo, window.merged), repo, window
    )
    assert verdict == OUT_OF_POPULATION

    metrics = compute_metrics({"F1": row}, window, repo)
    assert metrics.out_of_population == ("F1",)
    assert metrics.undetermined == ()
    assert metrics.contained == ()


def test_the_escape_denominator_is_merged_prs_not_records():
    """The one figure whose denominator does not move with the observation channel."""
    # Distinct in their first seven characters as well as overall: a fixture
    # whose abbreviations collide silently maps every record to one commit.
    merged = tuple(_merged(f"{i:x}" * 40) for i in range(1, 11))
    assert len({commit.sha[:7] for commit in merged}) == 10
    window = _window(merged, commit_count=10)
    repo = FakeRepo(resolve={commit.sha[:7]: commit.sha for commit in merged})
    heads = {
        f"F{i}": _row(
            f"F{i}",
            introduction=merged[i - 1].sha[:7],
            fix=merged[i].sha[:7],
            reached_main=True,
        )
        for i in range(1, 4)
    }
    metrics = compute_metrics(heads, window, repo)
    assert len(metrics.escapes_attributed_to_window) == 3
    assert metrics.merged_prs == 10
    assert metrics.escape_rate == pytest.approx(0.3)


def test_recurrence_after_gate_is_a_within_window_share():
    """Counts and shares of the post-gate population -- never a rate over time."""
    window = _window((_merged(SHA_A),), commit_count=10)
    heads = {
        "F1": _row("F1", when=date(2026, 9, 1), failure_class="gate_integrity_gap"),
        "F2": _row("F2", when=date(2026, 9, 2), failure_class="gate_integrity_gap"),
        "F3": _row("F3", when=date(2026, 9, 2), failure_class="remediation_scope_gap"),
        "F4": _row("F4", when=date(2026, 8, 10), failure_class="gate_integrity_gap"),
    }
    metrics = compute_metrics(heads, window, _mapped_repo(), gate_date=DEFAULT_GATE_DATE)
    assert metrics.post_gate_population == 3
    table = {
        entry.failure_class: (entry.count, entry.share) for entry in metrics.recurrence_after_gate
    }
    assert table["gate_integrity_gap"] == (2, pytest.approx(2 / 3))
    assert table["remediation_scope_gap"] == (1, pytest.approx(1 / 3))


def test_no_per_time_rate_appears_anywhere_in_the_report_payload():
    """The refusal is enforced, not merely promised in a docstring.

    A per-30-day form computed across the gate's adoption measures what reached
    the log rather than what the code did, so no such key may exist for a
    reader to quote out of context.
    """
    window = _window((_merged(SHA_A),), commit_count=10)
    heads = {"F1": _row("F1", introduction=SHA_A[:7], when=date(2026, 9, 1))}
    dedup = chain_heads(list(heads.values()), [])
    metrics = compute_metrics(heads, window, _mapped_repo())
    payload = report_payload(dedup, window, metrics)

    # Read the payload before searching it. Without this the absence check
    # below passes just as well over an empty payload, and the fixture above
    # would be decoration rather than a control.
    assert payload["introduction"]["failures"] == ["F1"]
    assert payload["gate_catch"]["observed"] == ["F1"]

    serialized = repr(payload)
    for banned in ("per_30", "per_day", "per_week", "per_month", "_30d", "per_time"):
        assert banned not in serialized


def test_lifecycle_hygiene_flags_an_active_row_carrying_a_fix_commit():
    """A landed fix on a row still active is a write-discipline readout."""
    window = _window((_merged(SHA_A),), commit_count=10)
    heads = {
        "F1": _row("F1", lifecycle="active", fix=SHA_A[:7], introduction=SHA_A[:7]),
        "F2": _row("F2", lifecycle="completed", fix=SHA_A[:7], introduction=SHA_A[:7]),
    }
    metrics = compute_metrics(heads, window, _mapped_repo())
    assert metrics.hygiene.active_with_fix_commit == ("F1",)


def test_reached_main_null_and_absent_are_counted_separately():
    """A filter cannot tell a stored null from an absent key, so the caller does.

    They mean different things: a null is undetermined, an absent key is a
    record outside the population entirely.
    """
    window = _window((_merged(SHA_A),), commit_count=10)
    heads = {
        "F1": _row("F1", reached_main=None, reached_main_present=True),
        "F2": _row("F2", reached_main=None, reached_main_present=False),
    }
    metrics = compute_metrics(heads, window, _mapped_repo())
    assert metrics.hygiene.reached_main_null == ("F1",)
    assert metrics.hygiene.reached_main_absent == ("F2",)


def test_a_baseline_row_joins_no_census():
    """Measurement rows describe no defect and pollute every count they enter."""
    window = _window((_merged(SHA_A),), commit_count=10)
    heads = {
        "BASELINE-2": _row(
            "BASELINE-2", introduction=None, when=date(2026, 9, 1), tags=("baseline",)
        ),
        "F1": _row("F1", introduction=SHA_A[:7], when=date(2026, 9, 1)),
    }
    metrics = compute_metrics(heads, window, _mapped_repo())
    assert metrics.null_introduction == ()
    assert metrics.observed_in_window == ("F1",)
    assert metrics.post_gate_population == 1


# ---------------------------------------------------------------------------
# F. What the frozen check treats as a fault
# ---------------------------------------------------------------------------


def _verification(**overrides) -> Verification:
    base = dict(
        rows=(
            VerificationRow(
                failure_id="F43",
                expected_stored="4100d14",
                expected_merged="4100d14",
                actual_merged="4100d14" + "0" * 33,
                reason=ON_MAIN,
            ),
        ),
        missing_failures=(),
        actual_commits=BASELINE_2_COMMITS,
        actual_introduced=BASELINE_2_INTRODUCED,
        actual_observed=BASELINE_2_OBSERVED,
        actual_caught=BASELINE_2_CAUGHT,
        dedup_disagreements=(),
    )
    base.update(overrides)
    return Verification(**base)


def test_records_added_to_a_closed_window_are_growth_not_divergence():
    """Otherwise the check is red forever and therefore never read.

    A window closes; records about it keep being written. That raises the
    observation counts permanently and says nothing about the mapping.
    """
    verification = _verification(
        observed_added=("F64", "F65"),
        caught_added=("F64", "F65"),
        actual_observed=BASELINE_2_OBSERVED + 2,
        actual_caught=BASELINE_2_CAUGHT + 2,
    )
    assert verification.diverged is False
    assert verification.grew is True
    assert "reproduced" in render_verification(verification)


def test_a_record_leaving_the_observed_set_is_divergence():
    """Growth and loss are opposite findings and must not share a count.

    A totals-only check reads a record joining and a record leaving as the same
    number, which is precisely the compensating error the row-by-row diff
    exists to prevent.
    """
    verification = _verification(
        observed_missing=("F44",),
        observed_within_frozen_population=BASELINE_2_OBSERVED - 1,
    )
    assert verification.diverged is True
    assert "NO LONGER" in render_verification(verification)


def test_a_gate_catch_that_shrinks_inside_the_frozen_population_is_divergence():
    """The numerator is checked over the population the baseline measured.

    Restricting it there is what lets the raw count grow with the corpus while
    the reproduction stays exact -- and what keeps a genuine loss visible
    underneath that growth, which a raw-count comparison would hide.
    """
    verification = _verification(
        caught_added=("F64", "F65", "F66"),
        actual_caught=BASELINE_2_CAUGHT + 2,
        caught_within_frozen_population=BASELINE_2_CAUGHT - 1,
    )
    assert verification.diverged is True


def test_a_tally_row_resolving_differently_is_divergence():
    """The mapping changing its answer about settled history is the fault."""
    verification = _verification(
        rows=(
            VerificationRow(
                failure_id="F48",
                expected_stored="9a8e6c5",
                expected_merged="4556360",
                actual_merged="deadbee" + "0" * 33,
                reason=SUBJECT_PREFIX,
            ),
        )
    )
    assert verification.tally_reproduced is False
    assert verification.diverged is True


def test_a_tally_row_that_cannot_resolve_at_all_is_divergence():
    """An unresolvable stored SHA is a divergence, not a quiet omission."""
    verification = _verification(
        rows=(
            VerificationRow(
                failure_id="F48",
                expected_stored="9a8e6c5",
                expected_merged="4556360",
                actual_merged=None,
                reason=UNRESOLVABLE,
            ),
        )
    )
    assert verification.diverged is True


def test_a_moved_introduction_total_is_divergence_even_with_a_clean_tally():
    """The tally names 17 rows; the count is checked independently of them."""
    verification = _verification(actual_introduced=BASELINE_2_INTRODUCED + 1)
    assert verification.tally_reproduced is True
    assert verification.diverged is True


# ---------------------------------------------------------------------------
# E. The command-line contract
# ---------------------------------------------------------------------------


def test_parse_args_defaults_the_vault_the_gate_date_and_the_span():
    args = _parse_args([])
    assert args.vault_id == DEFAULT_VAULT_ID
    assert args.gate_date == DEFAULT_GATE_DATE
    assert args.span_days == DEFAULT_SPAN_DAYS
    assert args.verify_baseline_2 is False
    assert args.dedup_invert is False


def test_a_malformed_window_is_rejected_at_parse_time():
    """Better a usage error than a window silently resolving to nothing."""
    with pytest.raises(SystemExit) as excinfo:
        _parse_args(["--window", "notarange"])
    assert excinfo.value.code == 2


def test_a_malformed_gate_date_is_rejected_at_parse_time():
    with pytest.raises(SystemExit) as excinfo:
        _parse_args(["--gate-date", "not-a-date"])
    assert excinfo.value.code == 2


def test_the_report_renderer_is_pure_and_names_the_withheld_normalization():
    """The renderer builds a string, and says why no per-time figure appears."""
    window = _window((_merged(SHA_A),), commit_count=10)
    heads = {"F1": _row("F1", introduction=SHA_A[:7], when=date(2026, 9, 1))}
    dedup = chain_heads(list(heads.values()), [])
    metrics = compute_metrics(heads, window, _mapped_repo())
    rendered = render_report(dedup, window, metrics)
    assert isinstance(rendered, str)
    assert "per-30-day form is deliberately not computed" in rendered
    assert "catalog rows" in rendered
