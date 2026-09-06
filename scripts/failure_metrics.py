#!/usr/bin/env python3
"""Compute the failure-log metrics from the vault and git, reproducibly.

Every failure-log measurement before this script was derived by a throwaway
program that lives nowhere, so each new baseline re-derived the same commit
attribution from scratch and could not be checked against the last one. This
script is the single durable computation: a baseline becomes a run of it plus
interpretation, rather than a re-derivation.

What it computes, all read-only:

* **Chain-head dedup.** The catalog stores one row per version, so a row count
  is not a failure count. A row is a chain head exactly when no ``supersedes``
  edge points at it (CAS-ADR-031 scopes the substrate's uniqueness constraint
  the same way). The lifecycle-based shortcut -- "the row that is not archived"
  -- agrees on ordinary chains and disagrees on a chain whose head was archived
  outright, so both censuses are computed and any disagreement is reported
  rather than silently resolved.
* **Branch-to-merge SHA mapping.** A stored commit field normally holds a
  pre-squash branch SHA, because the record is written at disposition time,
  before the pull request merges. That SHA is not an ancestor of the default
  branch, and a walk of the default branch does not error when it fails to find
  one -- it simply reports zero introductions on a window full of them. Each
  stored SHA is therefore mapped to the commit that carries it, by subject
  prefix, then by ticket id, then by walking first-parent ancestry.
* **Escape rate (log-wide) and containment rate.** Whether a defect ever
  shipped, derived from the mapping rather than read from the stored flag. The
  escape rate is the one figure whose denominator is merged pull requests
  counted from git rather than records counted from the log, so it does not
  move when the log's observation channel changes.
* **Recurrence after a gate, by failure class**, as a within-window count and
  share.
* **Lifecycle hygiene** -- rows left active while carrying a fix commit, and
  the undetermined-escape census.

Two deliberate refusals, both of which would otherwise produce a
plausible-looking wrong answer:

**Full 40-character SHAs are compared throughout, and a mixed-width comparison
raises.** Git emits full SHAs where the stored fields and ``%h`` are seven
characters, and Python compares the two widths as unequal without complaint, so
every set-membership test silently reads false.

**No per-unit-time rate is emitted.** Adopting an independent review gate
changed what reaches the log, so a per-30-day figure computed across that
boundary measures the observation channel and not the code. The within-window
counts and shares below are the defensible form, and the confounded one is not
offered at any flag.

Usage::

    .venv/bin/python scripts/failure_metrics.py                      # cas vault
    .venv/bin/python scripts/failure_metrics.py --window BASE..TIP
    .venv/bin/python scripts/failure_metrics.py --verify-baseline-2
    .venv/bin/python scripts/failure_metrics.py --json report.json

Exit code 0 on a completed run; 1 when ``--verify-baseline-2`` diverges from
the frozen expectation; 2 when the vault config cannot be found or the window
cannot be resolved in this clone.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol

# Ensure project root on path when run directly
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sage.adapters.stubs import StubAbstractionProvider, StubEmbeddingProvider  # noqa: E402
from sage.config import load_vault_config  # noqa: E402
from sage.mcp_init import initialize_services  # noqa: E402
from sage.vault_management import config_path_for_vault  # noqa: E402

DEFAULT_VAULT_ID = "cas"
FAILURE_DOC_TYPE = "failure_record"

#: The independent review gate's adoption date. The boundary the
#: recurrence-after-gate split is taken at, and the date past which a
#: cross-boundary per-time rate stops meaning anything.
DEFAULT_GATE_DATE = date(2026, 8, 31)

#: Four weeks, the longer leg of the "30-commit or four-week, whichever is
#: longer" window rule. The calendar span opens this many days before the
#: window tip's date -- not at the base commit's date, which is merely the
#: last excluded commit and can sit anywhere before the span.
DEFAULT_SPAN_DAYS = 28

_FULL_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_SHORT_SHA_RE = re.compile(r"\A[0-9a-f]{7,40}\Z")
_TICKET_ID_RE = re.compile(r"\bT-[0-9]{4}\b")
_FAILURE_ID_NUM_RE = re.compile(r"([0-9]+)\Z")

# Mapping outcomes. The four resolved reasons name which tier resolved the
# SHA; the three unresolved ones are reported rather than dropped, because a
# failure that could not be attributed is not evidence of a clean commit.
ON_MAIN = "on_main"
SUBJECT_PREFIX = "subject_prefix"
TICKET_ID = "ticket_id"
FIRST_PARENT = "first_parent"
UNRESOLVABLE = "unresolvable"
PROSE = "prose"
NULL = "null"

RESOLVED_REASONS = frozenset({ON_MAIN, SUBJECT_PREFIX, TICKET_ID, FIRST_PARENT})

# Containment verdicts, derived from the mapping rather than read from the
# stored flag.
CONTAINED = "contained"
ESCAPED = "escaped"
UNDETERMINED = "undetermined"
OUT_OF_POPULATION = "out_of_population"


class ShaWidthError(ValueError):
    """A commit SHA was not the full 40 hexadecimal characters.

    Raised rather than returning ``False`` on purpose. A seven-character
    stored value compared against a full one from ``git rev-list`` is unequal
    under ordinary string comparison, so the guarded operations here would
    otherwise report a confidently wrong answer over a window they never
    actually searched.
    """


# ---------------------------------------------------------------------------
# SHA discipline
# ---------------------------------------------------------------------------


def require_full_sha(value: object, *, label: str = "sha") -> str:
    """Return ``value`` if it is a full 40-character SHA, else raise.

    Accepts nothing else -- not a short SHA, not a ref name, not ``None``.
    Every comparison in this module goes through here, so an abbreviated value
    cannot reach a comparison at all.
    """
    if not isinstance(value, str) or not _FULL_SHA_RE.match(value):
        raise ShaWidthError(
            f"{label} must be a full 40-character SHA, got {value!r}; "
            "abbreviated values compare unequal against git output without erroring"
        )
    return value


def same_commit(left: object, right: object) -> bool:
    """Compare two commit SHAs, refusing any abbreviated spelling."""
    return require_full_sha(left, label="left") == require_full_sha(right, label="right")


def full_sha_set(values: Iterable[object]) -> frozenset[str]:
    """Build a comparison set, validating every member's width on the way in."""
    return frozenset(require_full_sha(value, label="window member") for value in values)


def in_window(sha: object, window: Collection[str]) -> bool:
    """Test membership of a commit in a window, refusing an abbreviated probe.

    Set membership is a separate code path from scalar comparison and is the
    one that actually bit: ``short_sha in {full, full, ...}`` is a silent
    ``False`` for every element.
    """
    return require_full_sha(sha, label="probe") in window


# ---------------------------------------------------------------------------
# Catalog rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FailureRow:
    """One catalog row -- one *version* of a failure record, not one failure."""

    doc_id: str
    failure_id: str
    lifecycle_status: str
    document_date: date
    failure_class: str | None = None
    severity: str | None = None
    observed_by: str | None = None
    caught_by_gate: bool | None = None
    introduction_commit: str | None = None
    discovery_commit: str | None = None
    fix_commit: str | None = None
    reached_main: bool | None = None
    #: False when the key is absent entirely. A filter cannot distinguish an
    #: absent key from a stored null, so the partition happens here: an absent
    #: key marks a record whose subject is not repository code and which is
    #: outside both escape metrics, while a stored null marks one still
    #: undetermined.
    reached_main_present: bool = False
    tags: tuple[str, ...] = ()

    @property
    def is_baseline(self) -> bool:
        """Baseline rows are measurement, not failures, and join no by-class count."""
        return "baseline" in self.tags or self.failure_id.startswith("BASELINE")


def failure_id_sort_key(failure_id: str) -> tuple[int, int, str]:
    """Order failure ids by their parsed numeric suffix, never as strings.

    Ids are not zero-padded, so a lexicographic sort puts ``F100`` before
    ``F2`` and reports ``F99`` as the highest allocated id from the moment
    ``F100`` exists.
    """
    match = _FAILURE_ID_NUM_RE.search(failure_id)
    if match is None:
        return (1, 0, failure_id)
    prefix_len = match.start()
    return (0, int(match.group(1)), failure_id[:prefix_len])


# ---------------------------------------------------------------------------
# Chain-head dedup
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DedupResult:
    """The row-to-failure reduction, with both censuses and their disagreements."""

    heads: dict[str, FailureRow]
    row_count: int
    edge_head_count: int
    proxy_head_count: int
    #: Failure ids where the edge rule and the lifecycle shortcut pick
    #: different rows, or where only one rule finds a head at all.
    disagreements: tuple[str, ...] = ()
    #: Failure ids carrying more than one chain head -- a collision the
    #: substrate's uniqueness constraint would refuse, surfaced rather than
    #: absorbed by a last-write-wins dict build.
    multiple_heads: tuple[str, ...] = ()

    @property
    def failure_count(self) -> int:
        return len(self.heads)


def chain_heads(
    rows: Sequence[FailureRow],
    supersedes_edges: Iterable[tuple[str, str]],
    *,
    prefer_archived: bool = False,
) -> DedupResult:
    """Reduce version rows to failures.

    The definitional rule is the edge one: a row is a chain head exactly when
    no ``supersedes`` edge points at it. Edges whose endpoints are not both in
    ``rows`` are ignored, because edge enumeration cannot be filtered by
    document type in one call and the vault-wide sweep carries every other
    document's chains too.

    The lifecycle shortcut -- the row that is not archived -- is computed
    alongside and compared, never substituted. ``prefer_archived`` inverts that
    shortcut, which is the probe that shows how much of a census rests on the
    preference rather than on the graph.

    Pure core: storage access happens in ``run``, so this is exercisable
    against a synthetic catalog.
    """
    by_doc_id = {row.doc_id: row for row in rows}
    superseded = {
        target for source, target in supersedes_edges if source in by_doc_id and target in by_doc_id
    }

    edge_heads: dict[str, list[FailureRow]] = {}
    for row in rows:
        if row.doc_id not in superseded:
            edge_heads.setdefault(row.failure_id, []).append(row)

    proxy_heads: dict[str, FailureRow] = {}
    for row in rows:
        archived = row.lifecycle_status == "archived"
        if archived is prefer_archived:
            proxy_heads.setdefault(row.failure_id, row)

    multiple = tuple(
        sorted(
            (fid for fid, candidates in edge_heads.items() if len(candidates) > 1),
            key=failure_id_sort_key,
        )
    )

    if prefer_archived:
        selected = dict(proxy_heads)
    else:
        selected = {fid: candidates[0] for fid, candidates in edge_heads.items()}

    disagreements = tuple(
        sorted(
            (
                fid
                for fid in set(edge_heads) | set(proxy_heads)
                if _head_doc_id(edge_heads.get(fid)) != _proxy_doc_id(proxy_heads.get(fid))
            ),
            key=failure_id_sort_key,
        )
    )

    return DedupResult(
        heads=selected,
        row_count=len(rows),
        edge_head_count=len(edge_heads),
        proxy_head_count=len(proxy_heads),
        disagreements=disagreements,
        multiple_heads=multiple,
    )


def _head_doc_id(candidates: list[FailureRow] | None) -> str | None:
    return candidates[0].doc_id if candidates else None


def _proxy_doc_id(row: FailureRow | None) -> str | None:
    return row.doc_id if row is not None else None


# ---------------------------------------------------------------------------
# Branch-to-merge mapping
# ---------------------------------------------------------------------------


class GitRepo(Protocol):
    """The git reads the mapping needs, narrowed so tests can supply a fake."""

    def rev_parse(self, rev: str) -> str | None:
        """Full 40-character SHA for a revision, or None if it is unreachable."""

    def subject(self, sha: str) -> str | None:
        """First line of a commit message, or None if the commit is unreadable."""

    def first_parent(self, sha: str) -> str | None:
        """Full SHA of a commit's first parent, or None at a root commit."""

    def commit_timestamp(self, sha: str) -> int | None:
        """Committer time in epoch seconds, or None when unreadable.

        Used to exclude merge candidates that precede the commit they would
        claim to carry. Epoch seconds rather than a rendered date, so that two
        squashes landing the same day still order, and so that a committer east
        of UTC does not compare backwards against one west of it.
        """


@dataclass(frozen=True)
class MergedCommit:
    """One squash-merged commit on the default branch -- one pull request."""

    sha: str
    subject: str
    commit_date: date
    #: Committer time as Unix epoch seconds. The ordering and exclusion key,
    #: kept separate from ``commit_date`` because that one answers a calendar
    #: question (which window a record falls in) while this one answers a
    #: sequencing question (which squash could have carried a commit). A date
    #: cannot separate two squashes that landed the same day -- fifteen such
    #: groups exist on this branch -- and a rendered date carries the
    #: committer's own UTC offset, so two commits an hour apart across
    #: midnight in different zones compare backwards. Epoch seconds have
    #: neither problem.
    commit_timestamp: int = 0


@dataclass(frozen=True)
class MappingOutcome:
    """Where a stored commit field landed on the default branch, and by which tier."""

    stored: str | None
    merged_sha: str | None
    reason: str

    @property
    def resolved(self) -> bool:
        return self.reason in RESOLVED_REASONS


def map_to_merged_commit(
    stored: str | None,
    repo: GitRepo,
    merged: Sequence[MergedCommit],
) -> MappingOutcome:
    """Map a stored commit field to the commit that carries it on the default branch.

    Tier order is fixed and load-bearing: subject prefix, which a squash merge
    preserves; then ticket id, for a mid-branch commit whose subject the squash
    did not inherit; then first-parent ancestry, for a probe or fixup carrying
    neither. Later tiers are consulted only when the earlier ones find nothing,
    so a commit that two tiers would resolve differently resolves the way the
    conventions prescribe.

    ``merged`` must be the whole first-parent history being searched, not one
    window of it. Restricting it to a window makes the ancestry tier report
    every unmatched commit as the nearest in-window ancestor, which is a
    confident wrong answer rather than a miss; window membership is a question
    asked of the *result*, afterwards.

    The ancestry walk stops when it reaches a commit that is itself on the
    default branch, because that is the branch point rather than the squash
    that carried the work. Only the stored commit itself may resolve by being
    on the branch already.

    Both text tiers prefer the **oldest** candidate that could actually carry
    the stored commit, rather than the newest match in history order. A squash
    cannot precede the branch commit it carries, so a candidate merged before
    the stored commit's own committer date is excluded outright; among the rest
    the oldest is the one the work landed in. Fourteen ticket ids on this
    repository's default branch are carried by more than one squash, so
    newest-wins is not a tie-break but a wrong answer for every one of them
    whose earlier pull request is the one being attributed.
    """
    if stored is None:
        return MappingOutcome(stored=None, merged_sha=None, reason=NULL)
    candidate = stored.strip()
    if not _SHORT_SHA_RE.match(candidate):
        # A drift-class marker such as "(multiple, Apr 16-May 27)". Classified,
        # never handed to git, which would report it as an unknown revision and
        # blur two different unresolved states into one.
        return MappingOutcome(stored=stored, merged_sha=None, reason=PROSE)

    by_sha = {commit.sha: commit for commit in merged}
    visited: set[str] = set()
    cursor: str | None = repo.rev_parse(candidate)
    if cursor is None:
        return MappingOutcome(stored=stored, merged_sha=None, reason=UNRESOLVABLE)

    first = True
    while cursor is not None and cursor not in visited:
        visited.add(cursor)
        if cursor in by_sha:
            if first:
                return MappingOutcome(stored=stored, merged_sha=cursor, reason=ON_MAIN)
            # Walked off the branch onto its base. The base carries the branch
            # but is not the squash that carried this work, so reporting it
            # would attribute the failure to whatever landed before it.
            return MappingOutcome(stored=stored, merged_sha=None, reason=UNRESOLVABLE)
        subject = repo.subject(cursor)
        if subject:
            # Deferred: most ancestry steps match no candidate at all, and a
            # git invocation per step to compute a floor nothing uses is the
            # walk's dominant cost.
            def not_before(sha: str = cursor) -> int | None:
                return repo.commit_timestamp(sha)

            hit = _match_by_subject_prefix(subject, merged, not_before)
            if hit is not None:
                return MappingOutcome(
                    stored=stored,
                    merged_sha=hit,
                    reason=SUBJECT_PREFIX if first else FIRST_PARENT,
                )
            hit = _match_by_ticket_id(subject, merged, not_before)
            if hit is not None:
                return MappingOutcome(
                    stored=stored,
                    merged_sha=hit,
                    reason=TICKET_ID if first else FIRST_PARENT,
                )
        cursor = repo.first_parent(cursor)
        first = False

    return MappingOutcome(stored=stored, merged_sha=None, reason=UNRESOLVABLE)


def _oldest_carrier(
    candidates: Iterable[MergedCommit], not_before: Callable[[], int | None]
) -> str | None:
    """The earliest candidate that could carry the commit being matched.

    A squash merge cannot precede the branch commit it carries, so anything
    merged earlier is excluded outright. Among what survives, the earliest is
    the pull request the work landed in; taking the newest instead misattributes
    every ticket carried by more than one squash to its most recent one.

    Ordering and exclusion are on epoch seconds. A calendar date cannot
    separate two squashes that landed on the same day, and falling back to a
    lexical tie-break on the SHA orders by an accident of hashing -- on this
    branch that picks the *newer* squash in four of the fifteen same-day
    duplicate-ticket groups, which is the answer this function exists to avoid.

    ``not_before`` is a callable so the git lookup happens only once a candidate
    actually exists: the ancestry walk consults this on every step, and most
    steps match nothing.  A null result -- an unreadable commit -- drops the
    exclusion and keeps the oldest, which is what the filter would have given
    whenever it was vacuous.
    """
    eligible = list(candidates)
    if not eligible:
        return None
    floor = not_before()
    if floor is not None:
        eligible = [commit for commit in eligible if commit.commit_timestamp >= floor]
        if not eligible:
            return None
    return min(eligible, key=lambda commit: (commit.commit_timestamp, commit.sha)).sha


def _match_by_subject_prefix(
    subject: str,
    merged: Sequence[MergedCommit],
    not_before: Callable[[], int | None] = lambda: None,
) -> str | None:
    """A squash merge keeps the branch head's subject and appends its own suffix."""
    return _oldest_carrier(
        (commit for commit in merged if commit.subject.startswith(subject)), not_before
    )


def _match_by_ticket_id(
    subject: str,
    merged: Sequence[MergedCommit],
    not_before: Callable[[], int | None] = lambda: None,
) -> str | None:
    """The ticket id survives into the squash even when the subject does not."""
    ids = set(_TICKET_ID_RE.findall(subject))
    if not ids:
        return None
    return _oldest_carrier(
        (commit for commit in merged if ids & set(_TICKET_ID_RE.findall(commit.subject))),
        not_before,
    )


# ---------------------------------------------------------------------------
# Window and metrics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """A commit range on the default branch, plus the calendar span it spans."""

    base: str
    tip: str
    merged: tuple[MergedCommit, ...]
    commit_count: int
    tip_date: date
    span_days: int = DEFAULT_SPAN_DAYS
    #: The whole first-parent history the mapping searches, of which ``merged``
    #: is one slice. Kept separate because searching only the window turns
    #: every unmatched commit into its nearest in-window ancestor -- a
    #: confident wrong attribution rather than a reported miss. Defaults to the
    #: window itself, which is the right search space only when the two
    #: genuinely coincide.
    history: tuple[MergedCommit, ...] = ()
    #: The date of the window's earliest commit, when it is known. The span
    #: never opens later than this: the rule is "30 commits or four weeks,
    #: whichever is longer", so a window wider than the nominal span widens the
    #: span rather than silently dropping its own early observations from the
    #: gate-catch denominator while still counting those commits in the
    #: introduction denominator.
    oldest_commit_date: date | None = None

    @property
    def search_space(self) -> tuple[MergedCommit, ...]:
        return self.history or self.merged

    @property
    def span_start(self) -> date:
        nominal = self.tip_date - timedelta(days=self.span_days)
        if self.oldest_commit_date is None:
            return nominal
        return min(nominal, self.oldest_commit_date)

    @property
    def span_widened(self) -> bool:
        """True when the window outran its nominal span and the span followed."""
        return self.span_start != self.tip_date - timedelta(days=self.span_days)

    @property
    def merged_shas(self) -> frozenset[str]:
        return full_sha_set(commit.sha for commit in self.merged)

    def covers_date(self, when: date) -> bool:
        return self.span_start <= when <= self.tip_date


@dataclass(frozen=True)
class ClassRecurrence:
    """One failure class's share of the post-gate population, counted not rated."""

    failure_class: str
    count: int
    share: float


@dataclass(frozen=True)
class Hygiene:
    """Lifecycle findings that are readouts of write discipline, not of code."""

    active_with_fix_commit: tuple[str, ...] = ()
    reached_main_null: tuple[str, ...] = ()
    reached_main_absent: tuple[str, ...] = ()
    stored_vs_derived_disagreement: tuple[str, ...] = ()
    multiple_chain_heads: tuple[str, ...] = ()


@dataclass(frozen=True)
class Metrics:
    """Everything one window's computation produces."""

    window_commits: int
    merged_prs: int
    introduced_failures: tuple[str, ...] = ()
    introduced_distinct_commits: int = 0
    null_introduction: tuple[str, ...] = ()
    prose_introduction: tuple[str, ...] = ()
    unresolvable_introduction: tuple[str, ...] = ()
    observed_in_window: tuple[str, ...] = ()
    caught_by_gate: tuple[str, ...] = ()
    contained: tuple[str, ...] = ()
    escaped: tuple[str, ...] = ()
    undetermined: tuple[str, ...] = ()
    out_of_population: tuple[str, ...] = ()
    escapes_attributed_to_window: tuple[str, ...] = ()
    recurrence_after_gate: tuple[ClassRecurrence, ...] = ()
    post_gate_population: int = 0
    hygiene: Hygiene = field(default_factory=Hygiene)

    @property
    def introduction_rate_per_failure(self) -> float | None:
        return _ratio(len(self.introduced_failures), self.window_commits)

    @property
    def introduction_rate_per_commit(self) -> float | None:
        return _ratio(self.introduced_distinct_commits, self.window_commits)

    @property
    def gate_catch_rate(self) -> float | None:
        return _ratio(len(self.caught_by_gate), len(self.observed_in_window))

    @property
    def containment_rate(self) -> float | None:
        return _ratio(len(self.contained), len(self.contained) + len(self.escaped))

    @property
    def escape_rate(self) -> float | None:
        return _ratio(len(self.escapes_attributed_to_window), self.merged_prs)


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def compute_metrics(
    heads: Mapping[str, FailureRow],
    window: Window,
    repo: GitRepo,
    *,
    gate_date: date = DEFAULT_GATE_DATE,
    dedup: DedupResult | None = None,
) -> Metrics:
    """Compute one window's figures from the deduped heads plus git.

    Pure with respect to storage: everything it needs from the vault arrives in
    ``heads``, and everything it needs from git arrives through ``repo``.
    """
    merged_shas = window.merged_shas

    introduced: list[str] = []
    introduced_commits: set[str] = set()
    nulls: list[str] = []
    prose: list[str] = []
    unresolvable: list[str] = []

    contained: list[str] = []
    escaped: list[str] = []
    undetermined: list[str] = []
    out_of_population: list[str] = []
    escapes_in_window: list[str] = []
    stored_disagreement: list[str] = []

    observed: list[str] = []
    caught: list[str] = []
    post_gate: list[FailureRow] = []
    active_with_fix: list[str] = []
    reached_main_null: list[str] = []
    reached_main_absent: list[str] = []

    for failure_id in sorted(heads, key=failure_id_sort_key):
        row = heads[failure_id]
        if row.is_baseline:
            # A measurement row describes no defect. It joins no census here --
            # not the introduction tally, not the null list, not the by-class
            # table -- because every one of them would then carry a row that
            # names no failure.
            continue

        intro = map_to_merged_commit(row.introduction_commit, repo, window.search_space)

        if intro.reason == NULL:
            nulls.append(failure_id)
        elif intro.reason == PROSE:
            prose.append(failure_id)
        elif intro.reason == UNRESOLVABLE:
            unresolvable.append(failure_id)
        elif intro.merged_sha is not None and in_window(intro.merged_sha, merged_shas):
            introduced.append(failure_id)
            introduced_commits.add(intro.merged_sha)

        if window.covers_date(row.document_date):
            observed.append(failure_id)
            if row.caught_by_gate:
                caught.append(failure_id)
            if row.document_date >= gate_date:
                post_gate.append(row)

        verdict = derive_containment(row, intro, repo, window)
        if verdict == CONTAINED:
            contained.append(failure_id)
        elif verdict == ESCAPED:
            escaped.append(failure_id)
            if intro.merged_sha is not None and in_window(intro.merged_sha, merged_shas):
                escapes_in_window.append(failure_id)
        elif verdict == UNDETERMINED:
            undetermined.append(failure_id)
        else:
            out_of_population.append(failure_id)

        if verdict in (CONTAINED, ESCAPED) and row.reached_main is not None:
            stored_escaped = row.reached_main
            if stored_escaped != (verdict == ESCAPED):
                stored_disagreement.append(failure_id)

        if row.lifecycle_status == "active" and row.fix_commit is not None:
            active_with_fix.append(failure_id)
        if row.reached_main_present and row.reached_main is None:
            reached_main_null.append(failure_id)
        if not row.reached_main_present:
            reached_main_absent.append(failure_id)

    return Metrics(
        window_commits=window.commit_count,
        merged_prs=len(window.merged),
        introduced_failures=tuple(introduced),
        introduced_distinct_commits=len(introduced_commits),
        null_introduction=tuple(nulls),
        prose_introduction=tuple(prose),
        unresolvable_introduction=tuple(unresolvable),
        observed_in_window=tuple(observed),
        caught_by_gate=tuple(caught),
        contained=tuple(contained),
        escaped=tuple(escaped),
        undetermined=tuple(undetermined),
        out_of_population=tuple(out_of_population),
        escapes_attributed_to_window=tuple(escapes_in_window),
        recurrence_after_gate=_recurrence_by_class(post_gate),
        post_gate_population=len(post_gate),
        hygiene=Hygiene(
            active_with_fix_commit=tuple(active_with_fix),
            reached_main_null=tuple(reached_main_null),
            reached_main_absent=tuple(reached_main_absent),
            stored_vs_derived_disagreement=tuple(stored_disagreement),
            multiple_chain_heads=dedup.multiple_heads if dedup else (),
        ),
    )


def derive_containment(
    row: FailureRow,
    intro: MappingOutcome,
    repo: GitRepo,
    window: Window,
) -> str:
    """Decide whether a defect ever shipped, from the mapping not the stored flag.

    A record that omits ``reached_main`` entirely has a subject that never
    ships through a pull request, and is outside both metrics' populations --
    writing it in either direction would credit or debit a merge that never
    carried it.

    Precedence, where two rules could both apply: a null or prose introduction
    resolves to escaped before the fix is consulted, because the failure was
    observed against the working system and the working system is the default
    branch. That reasoning does not need a fix to exist, so it outranks the
    null-fix rule below it.
    """
    if not row.reached_main_present:
        return OUT_OF_POPULATION
    if intro.reason in (NULL, PROSE):
        return ESCAPED
    if not intro.resolved:
        return UNDETERMINED
    if row.fix_commit is None:
        return UNDETERMINED
    fix = map_to_merged_commit(row.fix_commit, repo, window.search_space)
    if not fix.resolved or fix.merged_sha is None or intro.merged_sha is None:
        return UNDETERMINED
    return CONTAINED if same_commit(intro.merged_sha, fix.merged_sha) else ESCAPED


def _recurrence_by_class(post_gate: Sequence[FailureRow]) -> tuple[ClassRecurrence, ...]:
    """Post-gate recurrence per class, as a within-window count and share.

    No per-unit-time form is produced here or anywhere downstream. Dividing
    these counts by the window's length compares two classes across a boundary
    at which the log's observation channel changed, which reads as a difference
    between the classes and is a difference in what got written down.
    """
    counts: dict[str, int] = {}
    for row in post_gate:
        counts[row.failure_class or "unclassified"] = (
            counts.get(row.failure_class or "unclassified", 0) + 1
        )
    total = len(post_gate)
    return tuple(
        ClassRecurrence(
            failure_class=name,
            count=count,
            share=(count / total) if total else 0.0,
        )
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    )


# ---------------------------------------------------------------------------
# The frozen expectation
# ---------------------------------------------------------------------------

#: The second baseline's window and its published tally, frozen so the mapping
#: logic has a regression check that does not decay as the corpus grows. Only
#: the failure id and the two SHAs are carried: the mapping is SHA-to-SHA, so
#: nothing else in the published table constrains this computation.
BASELINE_2_BASE = "c8c059f"
BASELINE_2_TIP = "fdbfce7"
BASELINE_2_COMMITS = 109
BASELINE_2_INTRODUCED = 17
BASELINE_2_OBSERVED = 22
BASELINE_2_CAUGHT = 16
BASELINE_2_TALLY: tuple[tuple[str, str, str], ...] = (
    ("F43", "4100d14", "4100d14"),
    ("F45", "9418d4b", "9418d4b"),
    ("F48", "9a8e6c5", "4556360"),
    ("F49", "fd435cd", "f8170da"),
    ("F50", "907030b", "a6a3c35"),
    ("F51", "fcfcb91", "d43e0be"),
    ("F52", "fcfcb91", "d43e0be"),
    ("F53", "e882d0f", "b3e59ff"),
    ("F54", "63d95d0", "b3e59ff"),
    ("F56", "1e526e0", "3ce78b7"),
    ("F57", "f6aed73", "56074f5"),
    ("F58", "f6aed73", "56074f5"),
    ("F59", "56074f5", "56074f5"),
    ("F60", "357b0a7", "cb9a9e4"),
    ("F61", "058c43e", "e35b14b"),
    ("F62", "f7868e9", "d65060d"),
    ("F63", "c66e219", "fdbfce7"),
)

#: Observed in that window but introduced before it -- the tally above counts
#: introductions, and the gate-catch denominator counts observations, so the
#: two populations differ by exactly these.
BASELINE_2_OBSERVED_ONLY: tuple[str, ...] = ("F42", "F44", "F46", "F47", "F55")

# The gate-catch numerator is compared as a count restricted to the observed
# set above, not as an enumeration. That baseline published the count and the
# discovery-surface distribution, and those two are not the same population:
# `observed_by` names the surface that surfaced the failure while
# `caught_by_gate` records whether a gate fired at all, so a suite discovery
# can carry a true flag and a review discovery a false one. Deriving the
# numerator's membership from the surface column would freeze an inference the
# baseline never made.


@dataclass(frozen=True)
class VerificationRow:
    """One row of the frozen tally, checked against today's computation."""

    failure_id: str
    expected_stored: str
    expected_merged: str
    actual_merged: str | None
    reason: str

    @property
    def agrees(self) -> bool:
        """The one deliberate prefix comparison in this module.

        Everywhere else an abbreviated SHA is refused outright, because a
        mixed-width comparison against git output is silently false. Here the
        widths differ by construction and legitimately: the frozen table stores
        the seven characters the baseline published, and the computation
        produces forty. Nothing is being tested for membership in a set of full
        SHAs -- this asks whether today's answer starts with the answer that was
        published -- so the guard would have nothing to protect and would reject
        the frozen table itself.
        """
        return self.actual_merged is not None and self.actual_merged.startswith(
            self.expected_merged
        )


@dataclass(frozen=True)
class Verification:
    """The frozen expectation checked against the live corpus and this clone.

    Two kinds of difference are separated, because only one is a fault.

    **Corpus growth** -- a record written against the already-closed window
    after the baseline was published -- raises all three whole-corpus counts
    forever, on the introduced axis as much as on the observed and caught ones.
    Treating it as failure would leave this check permanently red and therefore
    unread.

    **A regression** -- a frozen tally row resolving differently, a record
    leaving a set it was in, the window's commit count moving, or any of the
    three figures moving *over the frozen population* -- means the mapping
    changed its answer about settled history, and that is what the check exists
    to catch.
    """

    rows: tuple[VerificationRow, ...]
    missing_failures: tuple[str, ...]
    actual_commits: int
    actual_introduced: int
    actual_observed: int
    actual_caught: int
    dedup_disagreements: tuple[str, ...]
    observed_added: tuple[str, ...] = ()
    observed_missing: tuple[str, ...] = ()
    caught_added: tuple[str, ...] = ()
    introduced_added: tuple[str, ...] = ()
    introduced_missing: tuple[str, ...] = ()
    #: All three headline figures recomputed over the population the baseline
    #: measured, rather than over today's whole corpus. The introduced axis
    #: needs this exactly as much as the other two: a record written after
    #: publication whose introducing commit resolves back into the closed
    #: window is growth of the same kind, and comparing a raw count would turn
    #: this check permanently red on the first one.
    caught_within_frozen_population: int = BASELINE_2_CAUGHT
    observed_within_frozen_population: int = BASELINE_2_OBSERVED
    introduced_within_frozen_population: int = BASELINE_2_INTRODUCED

    @property
    def tally_reproduced(self) -> bool:
        return not self.missing_failures and all(row.agrees for row in self.rows)

    @property
    def grew(self) -> bool:
        return bool(self.observed_added or self.caught_added or self.introduced_added)

    @property
    def diverged(self) -> bool:
        return bool(
            not self.tally_reproduced
            or self.observed_missing
            or self.introduced_missing
            or self.actual_commits != BASELINE_2_COMMITS
            or self.introduced_within_frozen_population != BASELINE_2_INTRODUCED
            or self.observed_within_frozen_population != BASELINE_2_OBSERVED
            or self.caught_within_frozen_population != BASELINE_2_CAUGHT
        )


def verify_baseline_2(
    heads: Mapping[str, FailureRow],
    window: Window,
    repo: GitRepo,
    metrics: Metrics,
    dedup: DedupResult,
) -> Verification:
    """Recompute the frozen tally row by row and report every divergence.

    A totals-only check would pass on a pair of compensating mapping errors,
    which is the coincidental pass the width rule above exists to prevent, so
    each row is recomputed and diffed individually.
    """
    rows: list[VerificationRow] = []
    missing: list[str] = []
    for failure_id, stored, expected_merged in BASELINE_2_TALLY:
        row = heads.get(failure_id)
        if row is None:
            missing.append(failure_id)
            continue
        outcome = map_to_merged_commit(row.introduction_commit, repo, window.search_space)
        rows.append(
            VerificationRow(
                failure_id=failure_id,
                expected_stored=stored,
                expected_merged=expected_merged,
                actual_merged=outcome.merged_sha,
                reason=outcome.reason,
            )
        )
    expected_introduced = {row[0] for row in BASELINE_2_TALLY}
    expected_observed = expected_introduced | set(BASELINE_2_OBSERVED_ONLY)
    actual_observed = set(metrics.observed_in_window)
    actual_caught = set(metrics.caught_by_gate)
    actual_introduced = set(metrics.introduced_failures)

    return Verification(
        rows=tuple(rows),
        missing_failures=tuple(missing),
        actual_commits=metrics.window_commits,
        actual_introduced=len(metrics.introduced_failures),
        actual_observed=len(metrics.observed_in_window),
        actual_caught=len(metrics.caught_by_gate),
        dedup_disagreements=dedup.disagreements,
        observed_added=_sorted_ids(actual_observed - expected_observed),
        observed_missing=_sorted_ids(expected_observed - actual_observed),
        caught_added=_sorted_ids(actual_caught - expected_observed),
        introduced_added=_sorted_ids(actual_introduced - expected_introduced),
        introduced_missing=_sorted_ids(expected_introduced - actual_introduced),
        # All three headline figures are compared over the population the
        # baseline actually measured, so a record written later raises the raw
        # counts without touching the reproduction.
        caught_within_frozen_population=len(actual_caught & expected_observed),
        observed_within_frozen_population=len(actual_observed & expected_observed),
        introduced_within_frozen_population=len(actual_introduced & expected_introduced),
    )


def _sorted_ids(ids: Collection[str]) -> tuple[str, ...]:
    return tuple(sorted(ids, key=failure_id_sort_key))


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _fmt_rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def render_report(
    dedup: DedupResult,
    window: Window,
    metrics: Metrics,
    *,
    gate_date: date = DEFAULT_GATE_DATE,
) -> str:
    """Render the computed figures as fixed-width text. Pure: builds a string."""
    lines: list[str] = [
        f"Failure-log metrics over {window.base}..{window.tip}",
        "",
        f"  window commits      {window.commit_count}",
        f"  merged pull requests {len(window.merged)}",
        f"  calendar span       {window.span_start.isoformat()} .. {window.tip_date.isoformat()}"
        f"  ({window.span_days} nominal days"
        f"{'; widened to the window' if window.span_widened else ''})",
        f"  gate boundary       {gate_date.isoformat()}",
        "",
        "Chain-head dedup",
        f"  catalog rows                {dedup.row_count}",
        f"  failures (edge rule)        {dedup.edge_head_count}",
        f"  failures (lifecycle proxy)  {dedup.proxy_head_count}",
        f"  rules disagree on           {len(dedup.disagreements)}{_listing(dedup.disagreements)}",
        f"  ids with multiple heads     {len(dedup.multiple_heads)}"
        f"{_listing(dedup.multiple_heads)}",
        "",
        "Failure introduction (within-window diagnostic, not a headline figure)",
        f"  introduced (per failure)          {len(metrics.introduced_failures)}"
        f" / {metrics.window_commits} = {_fmt_rate(metrics.introduction_rate_per_failure)}",
        f"  introduced (per distinct commit)  {metrics.introduced_distinct_commits}"
        f" / {metrics.window_commits} = {_fmt_rate(metrics.introduction_rate_per_commit)}",
        f"  null introduction commit          {len(metrics.null_introduction)}"
        f"{_listing(metrics.null_introduction)}",
        f"  prose introduction marker         {len(metrics.prose_introduction)}"
        f"{_listing(metrics.prose_introduction)}",
        f"  unresolvable stored SHA           {len(metrics.unresolvable_introduction)}"
        f"{_listing(metrics.unresolvable_introduction)}",
        "",
        "Gate catch",
        f"  observed in window  {len(metrics.observed_in_window)}",
        f"  caught by a gate    {len(metrics.caught_by_gate)}"
        f" = {_fmt_rate(metrics.gate_catch_rate)}",
        "",
        "Escape and containment (derived from the mapping, not the stored flag)",
        f"  contained            {len(metrics.contained)}",
        f"  escaped              {len(metrics.escaped)}",
        f"  undetermined         {len(metrics.undetermined)}",
        f"  outside population   {len(metrics.out_of_population)}",
        f"  containment rate     {_fmt_rate(metrics.containment_rate)}",
        f"  escape rate log-wide {len(metrics.escapes_attributed_to_window)}"
        f" / {metrics.merged_prs} = {_fmt_rate(metrics.escape_rate)}",
        "",
        f"Recurrence after the gate, by class ({metrics.post_gate_population} post-gate records)",
    ]
    if metrics.recurrence_after_gate:
        lines.append(f"  {'class':<32}{'count':>7}{'share':>9}")
        for entry in metrics.recurrence_after_gate:
            lines.append(f"  {entry.failure_class:<32}{entry.count:>7}{entry.share:>9.3f}")
    else:
        lines.append("  (no post-gate records in this window)")
    lines += [
        "",
        "  Counts and shares only. A per-30-day form is deliberately not computed:",
        "  the review gate changed what reaches the log partway through, so dividing",
        "  these counts by elapsed time compares observation channels rather than",
        "  classes.",
        "",
        "Lifecycle hygiene",
        f"  active rows carrying a fix commit  {len(metrics.hygiene.active_with_fix_commit)}"
        f"{_listing(metrics.hygiene.active_with_fix_commit)}",
        f"  reached_main stored null           {len(metrics.hygiene.reached_main_null)}",
        f"  reached_main key absent            {len(metrics.hygiene.reached_main_absent)}",
        f"  stored disagrees with derived      "
        f"{len(metrics.hygiene.stored_vs_derived_disagreement)}"
        f"{_listing(metrics.hygiene.stored_vs_derived_disagreement)}",
    ]
    return "\n".join(lines)


def _listing(ids: Sequence[str], limit: int = 8) -> str:
    if not ids:
        return ""
    shown = ", ".join(ids[:limit])
    suffix = ", ..." if len(ids) > limit else ""
    return f"  [{shown}{suffix}]"


def render_verification(verification: Verification) -> str:
    """Render the frozen-tally diff, naming the reason for every divergence."""
    lines = [
        "",
        "Verification against the frozen second-baseline tally",
        f"  {'failure':<9}{'stored':<10}{'expected':<10}{'actual':<10}{'tier':<16}verdict",
    ]
    for row in verification.rows:
        actual = (row.actual_merged or "-")[:7]
        verdict = "ok" if row.agrees else "DIVERGED"
        lines.append(
            f"  {row.failure_id:<9}{row.expected_stored:<10}{row.expected_merged:<10}"
            f"{actual:<10}{row.reason:<16}{verdict}"
        )
    if verification.missing_failures:
        lines.append(f"  absent from the live corpus: {', '.join(verification.missing_failures)}")
    lines += [
        "",
        f"  window commits    expected {BASELINE_2_COMMITS:>4}   actual "
        f"{verification.actual_commits:>4}",
        f"  introduced        expected {BASELINE_2_INTRODUCED:>4}   actual "
        f"{verification.introduced_within_frozen_population:>4}"
        f"   (whole corpus today: {verification.actual_introduced})",
        f"  observed          expected {BASELINE_2_OBSERVED:>4}   actual "
        f"{verification.observed_within_frozen_population:>4}"
        f"   (whole corpus today: {verification.actual_observed})",
        f"  caught by a gate  expected {BASELINE_2_CAUGHT:>4}   actual "
        f"{verification.caught_within_frozen_population:>4}"
        f"   (whole corpus today: {verification.actual_caught})",
    ]
    if verification.dedup_disagreements:
        lines.append(
            "  dedup rules disagree on "
            f"{len(verification.dedup_disagreements)} ids, which can move these totals"
        )

    lines.append("")
    if verification.observed_added:
        lines.append(
            "  observed since published:  "
            f"{', '.join(verification.observed_added)} -- records written against a"
        )
        lines.append("                            closed window, which raises the count and is")
        lines.append("                            not a divergence")
    if verification.caught_added:
        lines.append(f"  caught since published:   {', '.join(verification.caught_added)}")
    if verification.introduced_added:
        lines.append(
            "  introduced since published: "
            f"{', '.join(verification.introduced_added)} -- later records whose introducing"
        )
        lines.append("                            commit resolves back into the closed window")
    if verification.introduced_missing:
        lines.append(f"  introduced NO LONGER found: {', '.join(verification.introduced_missing)}")
    if verification.observed_missing:
        lines.append(f"  observed NO LONGER found: {', '.join(verification.observed_missing)}")

    lines.append("")
    if verification.diverged:
        lines.append("  DIVERGED -- the mapping's answer about settled history changed")
    elif verification.grew:
        lines.append(
            "  reproduced -- tally, window and all three frozen-population figures "
            "exact; whole-corpus counts higher by later records only"
        )
    else:
        lines.append("  reproduced")
    return "\n".join(lines)


def report_payload(dedup: DedupResult, window: Window, metrics: Metrics) -> dict[str, object]:
    """Serializable form of the report. Carries no per-unit-time rate, by design."""
    return {
        "window": {
            "base": window.base,
            "tip": window.tip,
            "commits": window.commit_count,
            "merged_prs": len(window.merged),
            "span_start": window.span_start.isoformat(),
            "span_end": window.tip_date.isoformat(),
        },
        "dedup": {
            "rows": dedup.row_count,
            "failures_edge_rule": dedup.edge_head_count,
            "failures_lifecycle_proxy": dedup.proxy_head_count,
            "disagreements": list(dedup.disagreements),
            "multiple_heads": list(dedup.multiple_heads),
        },
        "introduction": {
            "failures": list(metrics.introduced_failures),
            "distinct_commits": metrics.introduced_distinct_commits,
            "rate_per_failure": metrics.introduction_rate_per_failure,
            "rate_per_distinct_commit": metrics.introduction_rate_per_commit,
            "null": list(metrics.null_introduction),
            "prose": list(metrics.prose_introduction),
            "unresolvable": list(metrics.unresolvable_introduction),
        },
        "gate_catch": {
            "observed": list(metrics.observed_in_window),
            "caught": list(metrics.caught_by_gate),
            "rate": metrics.gate_catch_rate,
        },
        "escape": {
            "contained": list(metrics.contained),
            "escaped": list(metrics.escaped),
            "undetermined": list(metrics.undetermined),
            "out_of_population": list(metrics.out_of_population),
            "containment_rate": metrics.containment_rate,
            "escape_rate_log_wide": metrics.escape_rate,
        },
        "recurrence_after_gate": {
            "population": metrics.post_gate_population,
            "classes": [
                {"failure_class": entry.failure_class, "count": entry.count, "share": entry.share}
                for entry in metrics.recurrence_after_gate
            ],
        },
        "hygiene": {
            "active_with_fix_commit": list(metrics.hygiene.active_with_fix_commit),
            "reached_main_null": list(metrics.hygiene.reached_main_null),
            "reached_main_absent": list(metrics.hygiene.reached_main_absent),
            "stored_vs_derived_disagreement": list(metrics.hygiene.stored_vs_derived_disagreement),
            "multiple_chain_heads": list(metrics.hygiene.multiple_chain_heads),
        },
    }


# --- I/O edge ---------------------------------------------------------------


class SubprocessGitRepo:
    """A ``GitRepo`` backed by the git binary, scoped to one working tree."""

    def __init__(self, repo_root: Path) -> None:
        self._root = repo_root

    def _git(self, *args: str) -> str | None:
        proc = subprocess.run(
            ["git", "-C", str(self._root), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip()

    def rev_parse(self, rev: str) -> str | None:
        out = self._git("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
        return out or None

    def subject(self, sha: str) -> str | None:
        return self._git("log", "-1", "--format=%s", sha) or None

    def first_parent(self, sha: str) -> str | None:
        return self._git("rev-parse", "--verify", "--quiet", f"{sha}^1^{{commit}}") or None

    def commit_timestamp(self, sha: str) -> int | None:
        out = self._git("log", "-1", "--format=%ct", sha)
        return int(out) if out else None

    def is_ancestor(self, candidate: str, of: str) -> bool:
        return self._git("merge-base", "--is-ancestor", candidate, of) is not None

    def merged_commits(self, base: str | None, tip: str) -> list[MergedCommit]:
        """Every first-parent commit in the range -- one per merged pull request.

        A ``base`` of None walks the whole first-parent history reachable from
        ``tip``, which is what the mapping searches.
        """
        revs = f"{base}..{tip}" if base else tip
        out = self._git("log", "--first-parent", "--format=%H%x1f%cs%x1f%ct%x1f%s", revs)
        if out is None:
            return []
        commits: list[MergedCommit] = []
        for line in out.splitlines():
            parts = line.split("\x1f", 3)
            if len(parts) != 4:
                continue
            sha, when, stamp, subject = parts
            commits.append(
                MergedCommit(
                    sha=sha,
                    subject=subject,
                    commit_date=date.fromisoformat(when),
                    commit_timestamp=int(stamp),
                )
            )
        return commits


DEFAULT_HISTORY_REF = "origin/main"


def build_window(
    repo: SubprocessGitRepo,
    base: str,
    tip: str,
    span_days: int,
    *,
    history_ref: str = DEFAULT_HISTORY_REF,
) -> Window | None:
    """Resolve a window from git, or None when either anchor is unreachable here.

    The search space is the whole first-parent history reachable from
    ``history_ref``, not the window: a failure introduced after the window
    still has a real merge to resolve to, and denying it one makes the ancestry
    tier attribute it to the nearest in-window ancestor instead.

    ``history_ref`` names the **default branch**, never the checked-out one.
    Records are written before their pull request merges, so a run from the
    branch that is being measured would find those unmerged commits in the
    history and resolve them to themselves: a defect introduced and fixed on one
    branch then reads as having escaped before the merge and as contained after
    it, from the same corpus. Anchoring on the remote default branch makes the
    answer a property of the repository rather than of the working tree the
    script happened to run in. The override exists for a clone with no remote.
    """
    base_full = repo.rev_parse(base)
    tip_full = repo.rev_parse(tip)
    if base_full is None or tip_full is None:
        return None
    merged = repo.merged_commits(base_full, tip_full)
    if not merged:
        return None

    history_full = repo.rev_parse(history_ref)
    if history_full is None:
        print(
            f"history ref {history_ref!r} does not resolve; "
            "falling back to the window itself, which narrows attribution to it",
            file=sys.stderr,
        )
        history = list(merged)
    else:
        history = repo.merged_commits(None, history_full)
        if not repo.is_ancestor(tip_full, history_full):
            print(
                f"warning: the window tip is not an ancestor of {history_ref!r}, "
                "so the window being measured is not on the branch being searched",
                file=sys.stderr,
            )

    return Window(
        base=base,
        tip=tip,
        merged=tuple(merged),
        commit_count=len(merged),
        tip_date=merged[0].commit_date,
        span_days=span_days,
        history=tuple(history) or tuple(merged),
        oldest_commit_date=merged[-1].commit_date,
    )


def row_from_document(document: object) -> FailureRow | None:
    """Project one catalog document onto the fields the metrics read.

    Returns None for a document carrying no failure id, which cannot join any
    census keyed on one.
    """
    tier3 = getattr(document, "tier3_metadata", None) or {}
    failure_id = tier3.get("failure_id")
    if not failure_id:
        return None
    raw_date = getattr(document, "document_date", None)
    parsed = _parse_document_date(raw_date)
    if parsed is None:
        return None
    return FailureRow(
        doc_id=getattr(document, "id", ""),
        failure_id=str(failure_id),
        lifecycle_status=str(getattr(document, "lifecycle_status", "")),
        document_date=parsed,
        failure_class=tier3.get("failure_class"),
        severity=tier3.get("severity"),
        observed_by=tier3.get("observed_by"),
        caught_by_gate=tier3.get("caught_by_gate"),
        introduction_commit=tier3.get("introduction_commit"),
        discovery_commit=tier3.get("discovery_commit"),
        fix_commit=tier3.get("fix_commit"),
        reached_main=tier3.get("reached_main"),
        reached_main_present="reached_main" in tier3,
        tags=tuple(getattr(document, "tags", ()) or ()),
    )


def _parse_document_date(raw: object) -> date | None:
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str) and raw:
        return date.fromisoformat(raw[:10])
    return None


async def load_catalog(services: object) -> tuple[list[FailureRow], list[tuple[str, str]]]:
    """Read every failure-record row and every supersedes edge. Reads only."""
    graph = services.graph_store  # type: ignore[attr-defined]
    documents, _ = await graph.query_documents(
        filters={"doc_type": FAILURE_DOC_TYPE},
        limit=100_000,
        default_exclude_failed=False,
    )
    rows = [row for row in (row_from_document(doc) for doc in documents) if row is not None]
    edge_rows, _ = await graph.query_edges(
        filters={"edge_type": "supersedes"},
        limit=100_000,
    )
    # A retracted edge is the vault's correction path for a mis-anchored one.
    # Honouring it would drop a genuine chain head and then blame the
    # disagreement column for the gap.
    edges = [
        (row.edge.source_id, row.edge.target_id) for row in edge_rows if row.retracted_at is None
    ]
    return rows, edges


async def run(args: argparse.Namespace) -> int:
    config_path = config_path_for_vault(args.vault_id)
    if not config_path.exists():
        print(f"vault config not found: {config_path}", file=sys.stderr)
        return 2

    repo = SubprocessGitRepo(_REPO_ROOT)
    base, tip = args.window
    window = build_window(repo, base, tip, args.span_days, history_ref=args.history_ref)
    if window is None:
        print(
            f"window {base}..{tip} does not resolve in this clone; "
            "branch objects are garbage-collected and a fresh clone will not carry them",
            file=sys.stderr,
        )
        return 2

    config = load_vault_config(config_path)

    # Stub both providers so services initialization loads no model: this run
    # reads stored rows and regenerates nothing.
    print(f"Loading SAGE services for vault {args.vault_id!r}...", flush=True)
    services = await initialize_services(
        config,
        config_path=config_path,
        abstraction_provider=StubAbstractionProvider(),
        embedding_provider=StubEmbeddingProvider(),
    )

    try:
        print("Enumerating failure records and supersession edges...", flush=True)
        rows, edges = await load_catalog(services)
    finally:
        await services.graph_store.close()

    dedup = chain_heads(rows, edges, prefer_archived=args.dedup_invert)
    metrics = compute_metrics(dedup.heads, window, repo, gate_date=args.gate_date, dedup=dedup)

    print()
    print(render_report(dedup, window, metrics, gate_date=args.gate_date))

    if args.json is not None:
        args.json.write_text(json.dumps(report_payload(dedup, window, metrics), indent=2))

    if args.verify_baseline_2:
        verification = verify_baseline_2(dedup.heads, window, repo, metrics, dedup)
        print(render_verification(verification))
        if verification.diverged:
            return 1
    return 0


def _window_arg(value: str) -> tuple[str, str]:
    base, sep, tip = value.partition("..")
    if not sep or not base or not tip:
        raise argparse.ArgumentTypeError(f"expected BASE..TIP, got {value!r}")
    return base, tip


def _date_arg(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an ISO date, got {value!r}") from exc


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compute the failure-log metrics from a vault and git history."
    )
    parser.add_argument(
        "vault_id",
        nargs="?",
        default=DEFAULT_VAULT_ID,
        help=f"Vault to measure (default: {DEFAULT_VAULT_ID})",
    )
    parser.add_argument(
        "--window",
        type=_window_arg,
        default=(BASELINE_2_BASE, BASELINE_2_TIP),
        help="Commit range BASE..TIP on the default branch; BASE is excluded.",
    )
    parser.add_argument(
        "--gate-date",
        type=_date_arg,
        default=DEFAULT_GATE_DATE,
        dest="gate_date",
        help="Gate adoption date the recurrence split is taken at.",
    )
    parser.add_argument(
        "--span-days",
        type=int,
        default=DEFAULT_SPAN_DAYS,
        dest="span_days",
        help="Calendar span, counted back from the window tip's date.",
    )
    parser.add_argument(
        "--history-ref",
        default=DEFAULT_HISTORY_REF,
        dest="history_ref",
        help=(
            "Ref whose first-parent history the mapping searches. Defaults to the "
            "remote default branch so the answer does not depend on the checked-out "
            "branch; override only in a clone with no remote."
        ),
    )
    parser.add_argument("--json", type=Path, default=None, help="Write the payload here.")
    parser.add_argument(
        "--verify-baseline-2",
        action="store_true",
        dest="verify_baseline_2",
        help="Recompute the frozen second-baseline tally and diff it row by row.",
    )
    parser.add_argument(
        "--dedup-invert",
        action="store_true",
        dest="dedup_invert",
        help="Select archived rows instead of live ones, as a census probe.",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = _parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
