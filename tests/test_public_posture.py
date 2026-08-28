"""Public posture gate.

Enforces the invariants that make this repository safe for public release.
The cleanup pass that established these invariants is documented in
``CLAUDE.md`` §Coding Conventions ("Durable code surfaces stay public-ready")
and enforced at commit time by the ``cas-code-review`` skill §P1. This test
is the substrate-level counterpart: a deterministic gate that fails the
build whenever a forbidden pattern reappears in a tracked file.

Eleven invariants are checked. The numbering is the gate's own and is not
contiguous: T9 has never been assigned, and T11 is described below but
has no test function yet.

  T1. Use-case-specific terms (PIM, theology, patent, prosecution) must
      not appear in ``.py`` / ``.yaml`` / ``.yml`` / ``.json`` / ``.md``
      / ``.ts`` / ``.tsx`` files in scope (see :data:`_EXCLUDED_TOP_LEVEL_DIRS`).
      Regex tuned per-term: PIM uses both word boundaries because the
      three-letter token would otherwise match ``pimple``/``pimento``;
      ``\\btheolog`` and ``\\b(patent|prosecution)`` drop the trailing
      boundary so compound forms (``theological``, ``patent_draft``)
      are caught. Case-insensitive throughout. ``tier3_metadata`` /
      ``doc_type=ticket`` literals do not match any pattern by
      construction (SAGE-as-ticketing architectural terms).

  T2. ``T-NNNN`` ticket refs must not appear in ``.py`` docstrings or
      ``#`` comments. Distinguished from string literals by AST + tokenize
      parsing. ``CAS-ADR-N`` is preserved (regex does not overlap).

  T3. ``/Users/clifguy/`` filesystem paths must not appear in any tracked
      file in scope (see :data:`_EXCLUDED_TOP_LEVEL_DIRS`).

  T4. SDLC scaffolding phrases ("decision sheet", "dispatcher prompt",
      "subagent contract", "work plan") must not appear in ``.py``
      docstrings or ``#`` comments.

  T12. Ticket ids and use-case terms must not appear in tracked file or
       directory *names*. The counterpart to T1 and T2, which check the
       same two classes in file contents. Ticket ids reach a name in
       forms the prose regex does not accept (``t0074_load_probe.py``),
       so T12 carries its own pattern; use-case terms reuse T1's
       patterns unchanged. The other two content classes are omitted
       because neither can structurally match a path component — see
       ``_name_violations`` for the reasoning.

  T13. Ticket ids must not appear in Python *identifiers* — the names of
       functions, async functions, and classes — at any nesting depth.
       The third face of the rule T2 and T12 enforce on file contents and
       on file and directory names: an identifier is code, so the
       prohibition always covered it and only the enforcement was
       missing. Shares T12's ``NAME_TICKET_REF_RE`` for the same reason
       T12 needs it — a hyphen is not legal in a Python name, so an id
       reaches an identifier as ``t0157`` or ``t_0148`` and the prose
       pattern matches neither. Use-case terms are not re-checked here:
       T1 already scans the whole file, and an identifier is file
       contents.

Scope — top-level directories excluded from T1/T3/T4/T12/T13:

- ``domains/`` is the executor-defined home for use-case-specific configs.
  The establishing cleanup deletes ``domains/pim_health/`` entirely;
  any future domain subtree is expected to be use-case-specific by
  design, so the gate scopes around it.
- ``.claude/`` is Claude Code workspace tooling — skill definitions,
  settings, agent prompts. It is tracked, but its content includes
  documents like ``cas-code-review/SKILL.md`` that must quote the
  forbidden patterns in order to explain what the skill catches. The
  commit-time enforcement provided by the ``cas-code-review`` skill
  still applies to ``.claude/`` files because it reads the diff
  regardless of path — this gate is the substrate backstop for product
  code, not workspace tooling.

  T5. ``README.md`` exists at repo root.

  T6. ``LICENSE`` exists at repo root and carries the Apache 2.0 header.

  T7. Build artifacts (``.coverage``, ``coverage.xml``,
      ``repo_file_inventory.xlsx``) are not tracked.

  T8. ``domains/pim_health/`` subtree does not exist.

  T10. ``docs/process/branch_protection.md``, if present, does not leak
       GitHub-internal ids (ruleset id 16612539, actor_id 5). ``@clifguy``
       references are NOT checked — public GitHub handle is fine to ship.

  T11. ``CLAUDE.md`` is the public stub (under 4096 bytes).

The five allowlist constants near the top of the module follow the
pattern of ``KNOWN_VIOLATIONS`` in ``tests/sage/test_typed_alias_coverage.py``
and ``KNOWN_ARG_DRIFT`` in ``tests/sage/test_mcp_tool_conformance.py``.
All five are empty at the close of the establishing cleanup. Every
entry added later requires a 1-line rationale.

Anti-coincidental-pass coverage:

- ``test_t1_regex_word_boundary_negative_case`` confirms the use-case
  regex catches ``pim_health`` but not ``compilation``, and catches
  ``patent`` but not ``compatibility``.
- ``test_t2_tokenizer_distinguishes_string_from_comment`` confirms a
  ``T-NNNN`` token inside a string literal is NOT extracted, while the
  same token inside ``#`` comment or function docstring IS extracted.
- ``test_t12_ticket_pattern_matches_every_id_form`` and
  ``test_t12_use_case_pattern_matches_names`` confirm the name patterns
  match each form a token reaches a name in, and reject the near-misses
  (a bare year behind a ``t``, ``CAS-ADR-NNNN``, ``compilation``).
- ``test_t12_scan_reaches_directory_components_and_basenames`` confirms
  the scan walks every path component rather than the basename alone,
  and attributes a hit to the component that owns it. These three
  matter more than the usual anti-coincidental margin: the tree carries
  no name-side violation, so T12's gate test passes trivially against a
  no-op implementation and proves nothing on its own.
- ``test_t13_detector_reaches_nested_definitions`` and
  ``test_t13_detector_covers_all_three_node_types`` confirm the
  identifier walk reaches a method inside a class, a def inside a
  function, and a class name — the three positions the tree has almost
  no natural coverage for. Of the definitions the establishing cleanup
  renamed, all but seven sat at module level and none was a class, so a
  detector that walked only ``tree.body``, or skipped ``ClassDef``,
  would be indistinguishable from a correct one on the live tree alone.
- ``test_t13_detector_matches_identifier_id_forms_and_rejects_near_misses``
  confirms the detector consults ``NAME_TICKET_REF_RE`` and not the
  prose-side ``TICKET_REF_RE``, which matches no identifier at all and
  would yield a gate that passes on any tree. It also pins the inherited
  lookbehind: an id buried mid-CamelCase is out of reach, which is the
  same boundary that keeps ``abstract_2024`` from matching.
- ``test_t13_scan_scope_is_non_empty_and_honours_exclusions`` guards the
  failure mode where the gate passes because it enumerated nothing.

A whole-test anti-coincidental probe (manually introduce one violation
per category, confirm the gate fails with a precise message naming
file/line, revert) is a step in the cleanup verification plan, not an
automated test here.
"""

from __future__ import annotations

import ast
import re
import subprocess
import textwrap
import tokenize
from collections.abc import Iterable
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

# Suffixes scanned by T1 (use-case terms). ``.ts`` / ``.tsx`` are included
# so PIM-filename leaks in the HTML5 client's mock fixtures are gate-checked.
_USE_CASE_SUFFIXES: Final[tuple[str, ...]] = (
    ".py",
    ".yaml",
    ".yml",
    ".json",
    ".md",
    ".ts",
    ".tsx",
)

# Regex design — different terms need different boundary discipline:
#
# - ``pim`` is a three-letter token that would otherwise match ``pimple``,
#   ``pimento``, ``lapimba``, etc. Both ``\b`` boundaries are required,
#   and compound forms (``pim_health``, ``pim-health``) are enumerated as
#   explicit alternates.
# - ``theolog`` is a distinctive prefix with no common false positives.
#   Drop the trailing boundary so ``theology``, ``theological``,
#   ``theologian`` all match uniformly.
# - ``patent`` / ``prosecution`` would silently miss ``patent_draft`` or
#   ``prosecution_record`` if the trailing ``\b`` were kept, because
#   ``_`` is a regex word character. Drop the trailing boundary; no common
#   false positives outside intellectual-property contexts.
#
# All patterns are case-insensitive.
USE_CASE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\b(pim|pim_health|pim-health)\b", re.IGNORECASE),
    re.compile(r"\btheolog", re.IGNORECASE),
    re.compile(r"\b(patent|prosecution)", re.IGNORECASE),
)

# Substring match (case-insensitive). The four phrases below have no
# legitimate code-surface use; "checklist", "cohort", and "batch" are
# excluded because they ARE used in production senses (e.g.,
# ``batch_inference`` service, ``instantiated_from`` checklist instances).
SDLC_SCAFFOLDING_PHRASES: Final[tuple[str, ...]] = (
    "decision sheet",
    "dispatcher prompt",
    "subagent contract",
    "work plan",
)

# T-NNNN ticket reference. Does not overlap ``CAS-ADR-\d+``, which is
# the only sanctioned durable-surface anchor.
TICKET_REF_RE: Final[re.Pattern[str]] = re.compile(r"\bT-\d{4}\b")

# Ticket reference as it appears in a *name* rather than in prose.
#
# ``TICKET_REF_RE`` is hyphenated-only, which is right for prose but
# matches almost nothing in a filename: path components rarely carry the
# canonical spacing, so ids arrive as ``t0074_load_probe.py`` or
# ``test_t0037_pre_merge_metadata.py``. This pattern accepts all four
# forms — ``T-0452``, ``T0452``, ``t0074``, ``t_0037`` — case-insensitively
# via the explicit ``[tT]`` class.
#
# Boundary discipline:
#
# - The left lookbehind is required. Without it the ``t`` in
#   ``abstract_2024`` matches, and any name ending in ``t`` before a
#   four-digit year or count becomes a false positive.
# - The right lookahead rejects longer digit runs, so a timestamp like
#   ``t20260825`` is not read as a ticket id.
# - A bare number with no adjacent ``t`` (``report_0074.py``) is NOT
#   treated as a ticket id. Four digits alone are as likely a year or a
#   count, and the token this gate exists to catch always carries the
#   prefix.
NAME_TICKET_REF_RE: Final[re.Pattern[str]] = re.compile(r"(?<![0-9A-Za-z])[tT][-_]?\d{4}(?![0-9])")

# Tracked build artifacts that should be .gitignore'd and never committed.
_BUILD_ARTIFACTS: Final[frozenset[str]] = frozenset(
    {".coverage", "coverage.xml", "repo_file_inventory.xlsx"}
)

# Maximum number of violations to enumerate in a single pytest.fail message.
_MAX_REPORTED_VIOLATIONS: Final[int] = 30


# ---------------------------------------------------------------------------
# Allowlists
#
# All four are empty at the close of the establishing cleanup. Each entry
# added later requires a 1-line rationale alongside it. Pattern matches
# ``KNOWN_VIOLATIONS`` in tests/sage/test_typed_alias_coverage.py.
# ---------------------------------------------------------------------------

# path (relative to repo root) → list of forbidden terms allowlisted in
# that file.
USE_CASE_TERM_ALLOWLIST: Final[dict[str, list[str]]] = {}

# path (relative to repo root) → list of line numbers where a T-NNNN ref
# is allowlisted.
TICKET_REF_ALLOWLIST: Final[dict[str, list[int]]] = {}

# path (relative to repo root) → list of line numbers where a
# /Users/clifguy/ path is allowlisted.
PERSONAL_PATH_ALLOWLIST: Final[dict[str, list[int]]] = {}

# path (relative to repo root) → 1-line rationale for a name-token
# exemption. Keyed by the path whose component offends: the file's own
# path for a basename hit, the directory's path for a directory hit. A
# name has no line number, so this allowlist carries prose where the
# three above carry line lists.
NAME_TOKEN_ALLOWLIST: Final[dict[str, str]] = {}

# path (relative to repo root) → names of definitions in that file whose
# ticket-id-shaped identifier is exempted. Keyed by path and name rather
# than reusing ``NAME_TOKEN_ALLOWLIST`` above: that one exempts a *path
# component*, so sharing it would let a single filename exemption
# silently exempt every identifier the file declares. Shape matches
# ``ORPHANED_TEST_ALLOWLIST`` in tests/test_collection_integrity.py.
IDENTIFIER_TOKEN_ALLOWLIST: Final[dict[str, list[str]]] = {}


# ---------------------------------------------------------------------------
# Tracked-file enumeration
# ---------------------------------------------------------------------------


def _tracked_files() -> list[Path]:
    """Every file tracked by git, as absolute Path objects.

    Mirrors what the public repo actually ships (vs. a filesystem walk that
    would include untracked working-tree files).
    """
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [REPO_ROOT / line for line in result.stdout.splitlines() if line]


# Top-level directory names that are out of scope for the public-posture
# gate.
#
# - ``domains/`` is the executor-defined home for use-case-specific
#   configs. The establishing cleanup deletes ``domains/pim_health/``
#   entirely; any future domain subtree is expected to be use-case-
#   specific by design, so the gate scopes around it.
# - ``.claude/`` is Claude Code workspace tooling — skill definitions,
#   settings, agent prompts. It is tracked (so it ships in the public
#   repo), but its content includes documents like
#   ``cas-code-review/SKILL.md`` that must quote the forbidden patterns
#   in order to explain what the skill catches. The commit-time
#   enforcement provided by the ``cas-code-review`` skill still applies
#   to ``.claude/`` files because it reads the diff regardless of path —
#   this gate is the substrate backstop for product code, not workspace
#   tooling.
_EXCLUDED_TOP_LEVEL_DIRS: Final[frozenset[str]] = frozenset({"domains", ".claude"})

# The gate test file itself is self-referential — it MUST contain the
# forbidden patterns (in its regex constants, its module docstring
# documenting what it catches, and its negative-case fixtures) to do its
# job. Exclude the gate test from its own scanning.
_EXCLUDED_FILES: Final[frozenset[str]] = frozenset({"tests/test_public_posture.py"})


def _is_excluded(path: Path) -> bool:
    """Is path under an out-of-scope top-level directory, or is it the
    self-referential gate-test file itself?
    """
    try:
        rel = path.relative_to(REPO_ROOT)
    except ValueError:
        return False
    parts = rel.parts
    if not parts:
        return False
    if parts[0] in _EXCLUDED_TOP_LEVEL_DIRS:
        return True
    return str(rel) in _EXCLUDED_FILES


def _tracked_files_with_suffixes(suffixes: tuple[str, ...]) -> list[Path]:
    return [p for p in _tracked_files() if p.suffix in suffixes and not _is_excluded(p)]


# ---------------------------------------------------------------------------
# Docstring + comment extraction
# ---------------------------------------------------------------------------


def _extract_docstring_and_comment_spans(path: Path) -> list[tuple[int, str]]:
    """Return ``(line_number, text)`` for every docstring + ``#`` comment in a .py file.

    Docstrings: extracted via ``ast`` (module, class, function, async
    function). The line number is the line of the docstring's string-
    literal expression, not the enclosing def.

    Comments: extracted via ``tokenize`` ``COMMENT`` tokens.

    String literals at non-docstring positions are intentionally excluded.
    This is the discipline that lets the gate distinguish ``# T-0001`` in
    a comment (in scope) from ``"T-0001"`` in a test-fixture string
    literal (out of scope).
    """
    spans: list[tuple[int, str]] = []

    try:
        source_bytes = path.read_bytes()
    except OSError:
        return spans

    # AST docstrings. Treat unparseable .py files as having no docstrings.
    try:
        tree = ast.parse(source_bytes)
    except SyntaxError:
        tree = None

    if tree is not None:
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue
            if not node.body:
                continue
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                spans.append((first.lineno, first.value.value))

    # Tokenize comments.
    try:
        with open(path, "rb") as fh:
            for tok in tokenize.tokenize(fh.readline):
                if tok.type == tokenize.COMMENT:
                    spans.append((tok.start[0], tok.string))
    except (tokenize.TokenizeError, SyntaxError, UnicodeDecodeError):
        # Best-effort: a tokenize failure shouldn't crash the whole gate.
        pass

    return spans


def _format_violations(
    violations: list[tuple[str, int, str]],
    *,
    header: str,
    sep: str = " → ",
) -> str:
    """Render a violation list as a pytest.fail-friendly message."""
    head = violations[:_MAX_REPORTED_VIOLATIONS]
    body = "\n".join(f"  {path}:{line}{sep}{repr(detail)}" for path, line, detail in head)
    overflow = len(violations) - len(head)
    tail = f"\n  ... and {overflow} more" if overflow > 0 else ""
    return f"{header} ({len(violations)} violation(s)):\n{body}{tail}"


def _format_name_violations(violations: list[tuple[str, str]], *, header: str) -> str:
    """Render a name-violation list as a pytest.fail-friendly message.

    Sibling of :func:`_format_violations` with the same cap-and-overflow
    behaviour. A separate function because a name has no line number, so
    the three-tuple shape would have to carry a meaningless ``0``.
    """
    head = violations[:_MAX_REPORTED_VIOLATIONS]
    body = "\n".join(f"  {path} → {detail}" for path, detail in head)
    overflow = len(violations) - len(head)
    tail = f"\n  ... and {overflow} more" if overflow > 0 else ""
    return f"{header} ({len(violations)} violation(s)):\n{body}{tail}"


# ---------------------------------------------------------------------------
# Name scanning
# ---------------------------------------------------------------------------


def _name_violations(rel_paths: Iterable[str]) -> list[tuple[str, str]]:
    """Find forbidden tokens in the *names* along each repo-relative path.

    Every component is inspected, so a directory name is checked exactly
    as a file name is; a hit on a directory is attributed to that
    directory's path rather than to whichever file happened to surface
    it, and is reported once no matter how many files sit beneath it.

    Two of the four classes the content side checks are omitted, because
    neither can structurally match a path component rather than because
    they were overlooked:

    - ``SDLC_SCAFFOLDING_PHRASES`` are space-separated ("work plan"), and
      a path component cannot contain a space in this repo's conventions.
    - The personal-path check looks for the absolute prefix
      ``/Users/clifguy/``, which cannot appear inside a repo-relative
      path.

    Returns ``(offending_path, "'token' (class)")`` pairs, deduplicated.
    """
    violations: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for rel_path in rel_paths:
        parts = Path(rel_path).parts
        for depth, component in enumerate(parts, start=1):
            # The path this component names: the directory prefix for an
            # interior component, the file itself for the last one.
            owner = str(Path(*parts[:depth]))
            if owner in NAME_TOKEN_ALLOWLIST:
                continue

            matches: list[str] = []
            ticket = NAME_TICKET_REF_RE.search(component)
            if ticket:
                matches.append(f"{ticket.group(0)!r} (ticket-id)")
            for pattern in USE_CASE_PATTERNS:
                use_case = pattern.search(component)
                if use_case:
                    matches.append(f"{use_case.group(0)!r} (use-case-term)")

            for detail in matches:
                key = (owner, detail)
                if key not in seen:
                    seen.add(key)
                    violations.append(key)

    return violations


# ---------------------------------------------------------------------------
# Identifier scanning
# ---------------------------------------------------------------------------


def _identifier_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, name)`` for every definition whose *name* carries a
    ticket id.

    Covers ``FunctionDef``, ``AsyncFunctionDef``, and ``ClassDef`` at any
    nesting depth: a method inside a class and a closure inside a function
    are reached exactly as a module-level ``def`` is. Depth matters more
    than it looks — the overwhelming majority of definitions in this
    repository sit at module level, so a walk over ``tree.body`` alone
    behaves identically on almost every real file and diverges only on the
    handful this helper exists to catch.

    Uses ``NAME_TICKET_REF_RE`` rather than ``TICKET_REF_RE`` for the same
    reason ``_name_violations`` does: the hyphenated prose form
    (``T-0452``) is not how an id reaches an identifier. Underscores are
    legal in a Python name, so the id arrives as ``t0157`` or ``t_0148``,
    and the content-side pattern matches neither.

    Pure: takes a parsed tree, consults no allowlist, touches no
    filesystem. Mirrors ``_orphaned_test_functions`` in
    tests/test_collection_integrity.py, which factors its walk the same way
    so the unit tests can drive it against synthetic source.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if NAME_TICKET_REF_RE.search(node.name):
            found.append((node.lineno, node.name))
    return found


# ---------------------------------------------------------------------------
# T1 — Use-case-specific terms outside domains/
# ---------------------------------------------------------------------------


def test_no_use_case_terms_outside_domains() -> None:
    """T1: PIM/theology/patent/prosecution must not appear in tracked
    ``.py``/``.yaml``/``.yml``/``.json``/``.md`` files outside ``domains/``.
    """
    violations: list[tuple[str, int, str]] = []
    for path in _tracked_files_with_suffixes(_USE_CASE_SUFFIXES):
        rel = str(path.relative_to(REPO_ROOT))
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        allowed_terms = {t.lower() for t in USE_CASE_TERM_ALLOWLIST.get(rel, [])}
        for pattern in USE_CASE_PATTERNS:
            for match in pattern.finditer(text):
                term = match.group(0).lower()
                if term in allowed_terms:
                    continue
                line_no = text.count("\n", 0, match.start()) + 1
                violations.append((rel, line_no, match.group(0)))

    if violations:
        pytest.fail(_format_violations(violations, header="T1 use-case-term"))


# ---------------------------------------------------------------------------
# T2 — T-NNNN refs in .py docstrings + # comments
# ---------------------------------------------------------------------------


def test_no_ticket_refs_in_py_durable_surfaces() -> None:
    """T2: ``T-NNNN`` must not appear in ``.py`` docstrings or ``#`` comments.

    String literals (test IDs, fixture names) are out of scope.
    ``CAS-ADR-N`` references are preserved (regex does not match them).
    """
    violations: list[tuple[str, int, str]] = []
    for path in _tracked_files_with_suffixes((".py",)):
        rel = str(path.relative_to(REPO_ROOT))
        allowed_lines = set(TICKET_REF_ALLOWLIST.get(rel, []))
        for line_no, text in _extract_docstring_and_comment_spans(path):
            if line_no in allowed_lines:
                continue
            for match in TICKET_REF_RE.finditer(text):
                violations.append((rel, line_no, match.group(0)))

    if violations:
        pytest.fail(_format_violations(violations, header="T2 ticket-ref"))


# ---------------------------------------------------------------------------
# T3 — Personal filesystem paths
# ---------------------------------------------------------------------------


def test_no_personal_filesystem_paths() -> None:
    """T3: ``/Users/clifguy/`` must not appear in any tracked file outside
    the excluded top-level directories (``domains/``, ``.claude/``).
    """
    pathspec_excludes = [f":(exclude){d}/" for d in sorted(_EXCLUDED_TOP_LEVEL_DIRS)]
    pathspec_excludes += [f":(exclude){f}" for f in sorted(_EXCLUDED_FILES)]
    result = subprocess.run(
        ["git", "grep", "-n", "-F", "/Users/clifguy/", "--", *pathspec_excludes],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    # git grep returns 0 on match, 1 on no match, >1 on error.
    if result.returncode > 1:
        pytest.fail(f"git grep failed (rc={result.returncode}): {result.stderr.strip()}")
    if result.returncode == 1:
        return  # zero hits → pass

    violations: list[tuple[str, int, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        path, lineno_str, match = parts
        try:
            line_no = int(lineno_str)
        except ValueError:
            continue
        if line_no in PERSONAL_PATH_ALLOWLIST.get(path, []):
            continue
        violations.append((path, line_no, match.strip()[:120]))

    if violations:
        pytest.fail(_format_violations(violations, header="T3 personal-path"))


# ---------------------------------------------------------------------------
# T4 — SDLC scaffolding phrases
# ---------------------------------------------------------------------------


def test_no_sdlc_scaffolding_terms_in_py_durable_surfaces() -> None:
    """T4: 'decision sheet', 'dispatcher prompt', 'subagent contract',
    'work plan' must not appear in ``.py`` docstrings or ``#`` comments.
    """
    violations: list[tuple[str, int, str]] = []
    for path in _tracked_files_with_suffixes((".py",)):
        rel = str(path.relative_to(REPO_ROOT))
        for line_no, text in _extract_docstring_and_comment_spans(path):
            lowered = text.lower()
            for phrase in SDLC_SCAFFOLDING_PHRASES:
                if phrase in lowered:
                    violations.append((rel, line_no, phrase))

    if violations:
        pytest.fail(_format_violations(violations, header="T4 SDLC-scaffolding"))


# ---------------------------------------------------------------------------
# T5–T8, T10, T11 — Structural state
# ---------------------------------------------------------------------------


def test_readme_exists() -> None:
    """T5: ``README.md`` at repo root."""
    assert (REPO_ROOT / "README.md").is_file(), "README.md missing at repo root"


def test_license_is_apache() -> None:
    """T6: ``LICENSE`` at repo root, Apache 2.0 header."""
    license_path = REPO_ROOT / "LICENSE"
    assert license_path.is_file(), "LICENSE missing at repo root"
    content = license_path.read_text(encoding="utf-8", errors="replace")
    assert "Apache License" in content, (
        "LICENSE does not appear to be Apache 2.0 ('Apache License' header not found)"
    )


def test_build_artifacts_not_tracked() -> None:
    """T7: ``.coverage``, ``coverage.xml``, ``repo_file_inventory.xlsx``
    must not be tracked.
    """
    tracked_names = {p.name for p in _tracked_files()}
    leaked = sorted(_BUILD_ARTIFACTS & tracked_names)
    assert not leaked, f"Build artifacts still tracked: {leaked}"


def test_domains_pim_health_absent() -> None:
    """T8: ``domains/pim_health/`` subtree must not exist."""
    assert not (REPO_ROOT / "domains" / "pim_health").exists(), "domains/pim_health/ still present"


def test_branch_protection_md_no_github_internal_ids() -> None:
    """T10: ``docs/process/branch_protection.md`` (if present) must not
    leak GitHub-internal ids. ``@clifguy`` is a public GitHub handle and
    is NOT checked.
    """
    path = REPO_ROOT / "docs" / "process" / "branch_protection.md"
    if not path.exists():
        pytest.skip("branch_protection.md absent (acceptable per ticket)")
    text = path.read_text(encoding="utf-8", errors="replace")
    leaks = []
    if "16612539" in text:
        leaks.append("ruleset id 16612539")
    if '"actor_id": 5' in text:
        leaks.append('"actor_id": 5')
    assert not leaks, f"branch_protection.md GitHub-internal id leak(s): {leaks}"


# ---------------------------------------------------------------------------
# T12 — Forbidden tokens in file and directory names
# ---------------------------------------------------------------------------


def test_no_ticket_or_use_case_terms_in_tracked_names() -> None:
    """T12: no tracked file or directory name carries a ticket id or a
    use-case term.

    The counterpart to T1/T2, which scan the same two classes in file
    *contents*. Scope matches T1/T3/T4 — ``domains/`` and ``.claude/``
    are excluded, as is this gate file, whose own name is fine but whose
    exclusion keeps the scan uniform.
    """
    rel_paths = [
        str(path.relative_to(REPO_ROOT)) for path in _tracked_files() if not _is_excluded(path)
    ]
    violations = _name_violations(rel_paths)
    if violations:
        pytest.fail(_format_name_violations(violations, header="T12 name-token"))


# ---------------------------------------------------------------------------
# T13 — Ticket ids in Python identifiers
# ---------------------------------------------------------------------------


def test_no_ticket_ids_in_python_identifiers() -> None:
    """T13: no tracked ``.py`` file may define a function or class whose
    *name* carries a ticket id.

    The third face of the same rule T2 and T12 enforce on file contents and
    on file and directory names. An identifier is code, so the prohibition
    already covered it; only the enforcement was missing.

    Scope matches T1/T3/T4/T12 via ``_tracked_files_with_suffixes``, which
    excludes ``domains/``, ``.claude/``, and this gate file. Unparseable
    modules are skipped: a file that cannot be parsed fails its own
    collection loudly and is a different problem.
    """
    violations: list[tuple[str, int, str]] = []
    for path in _tracked_files_with_suffixes((".py",)):
        rel = str(path.relative_to(REPO_ROOT))
        try:
            tree = ast.parse(path.read_bytes(), filename=str(path))
        except SyntaxError:
            continue
        allowed = set(IDENTIFIER_TOKEN_ALLOWLIST.get(rel, []))
        for line_no, name in _identifier_violations(tree):
            if name in allowed:
                continue
            violations.append((rel, line_no, name))

    if violations:
        pytest.fail(_format_violations(violations, header="T13 identifier-token"))


# ---------------------------------------------------------------------------
# Anti-coincidental-pass checks
#
# These are the safety net the test-first methodology calls for: they
# verify that the regex and the tokenize-vs-string discipline above are
# correctly configured, NOT that any specific production file is clean.
# A whole-test probe (introduce a single violation per category, confirm
# the gate fails with a precise message, revert) is a step in the cleanup
# verification plan, not an automated test here.
# ---------------------------------------------------------------------------


def test_t1_regex_word_boundary_negative_case() -> None:
    """T1 regex catches compound forms (``pim_health``, ``patent_draft``,
    ``theological``) while rejecting substring false positives
    (``pimple``, ``compatibility``).
    """
    pim_re = USE_CASE_PATTERNS[0]
    # Compound form matched via explicit alternate.
    assert pim_re.search("pim_health is the vault") is not None
    assert pim_re.search("PIM in uppercase") is not None
    # The bare ``pim`` token requires both ``\b`` boundaries — the
    # three-letter token would otherwise match common English words.
    assert pim_re.search("compilation pimple lapimba") is None

    theolog_re = USE_CASE_PATTERNS[1]
    # Trailing boundary dropped — matches all forms uniformly.
    assert theolog_re.search("theological framework") is not None
    assert theolog_re.search("theology vault") is not None
    assert theolog_re.search("theologian Karl Barth") is not None
    # No common English false positives for ``\btheolog``.
    assert theolog_re.search("compatibility is preserved") is None

    patent_re = USE_CASE_PATTERNS[2]
    # Compound forms — ``_`` is a regex word character, so trailing
    # boundary had to be dropped to catch these.
    assert patent_re.search("patent_draft is in the docstring") is not None
    assert patent_re.search("prosecution_record") is not None
    assert patent_re.search("Patent Specification") is not None
    # No common English false positives — ``compatibility`` does not
    # contain ``patent`` as a substring; ``supplant`` does not start
    # with ``patent``.
    assert patent_re.search("compatibility is preserved") is None
    assert patent_re.search("supplant proceedings") is None


def test_t2_tokenizer_distinguishes_string_from_comment(tmp_path: Path) -> None:
    """T2 tokenizer extraction catches ``# T-NNNN`` but NOT a ``"T-NNNN"``
    string literal.
    """
    fixture = tmp_path / "fixture.py"
    fixture.write_text(
        '"""Module docstring without ticket refs."""\n'
        'TICKET_LITERAL = "T-0001"  # leading-comment must be flagged\n'
        "# T-0002 in a standalone comment must be flagged\n"
        "def foo():\n"
        '    """T-0003 in a function docstring must be flagged."""\n'
        "    return TICKET_LITERAL\n"
    )

    spans = _extract_docstring_and_comment_spans(fixture)
    flat_text = " ".join(span_text for _, span_text in spans)

    # The string literal "T-0001" must NOT be extracted — it sits at a
    # non-docstring position and is a STRING token, not a COMMENT token.
    assert "T-0001" not in flat_text, (
        "string-literal T-NNNN was extracted; tokenize/AST discipline broken"
    )
    # The inline ``#`` comment, the standalone ``#`` comment, and the
    # function docstring all MUST be extracted.
    assert "T-0002" in flat_text, "standalone # comment was not extracted"
    assert "T-0003" in flat_text, "function docstring was not extracted"


def test_t12_ticket_pattern_matches_every_id_form() -> None:
    """T12 name regex catches all four ways a ticket id reaches a name,
    including one carried by a directory rather than a file, while
    rejecting the ``t``-before-digits false positive.

    The content-side ``TICKET_REF_RE`` is hyphenated-only and would match
    none of the positives below; this test is what fails if the name
    scan is wired to that pattern instead.
    """
    positives = {
        "scripts/t0074_load_probe.py": "unhyphenated, lowercase, at component start",
        "tests/sage/test_t0037_x.py": "unhyphenated, mid-component after an underscore",
        "docs/T-0452_notes.md": "canonical hyphenated form",
        "docs/T0452.md": "hyphenless uppercase form",
        "docs/t0099/notes.md": "carried by a directory, not the basename",
    }
    for rel_path, why in positives.items():
        assert _name_violations([rel_path]), f"missed a ticket id ({why}): {rel_path}"

    negatives = {
        "sage/abstract_2024.py": "the ``t`` closing ``abstract`` precedes a bare year",
        "docs/CAS-ADR-042.md": "the only sanctioned durable-surface anchor",
        "scripts/benchmark_abstraction.py": "ordinary script name",
        "tests/test_public_posture.py": "this gate's own name",
        "sage/storage/t20260825_snapshot.py": "a timestamp, not a four-digit id",
    }
    for rel_path, why in negatives.items():
        assert not _name_violations([rel_path]), f"false positive ({why}): {rel_path}"


def test_t12_use_case_pattern_matches_names() -> None:
    """T12 matches use-case terms in a name with the same per-term
    boundary tuning T1 applies to contents.

    The assertions are behavioural, so they exclude an implementation
    that dropped the use-case classes or retuned their boundaries — but
    not one that inlined equivalent copies of the patterns instead of
    consulting ``USE_CASE_PATTERNS``. That variant would pass here and
    drift from T1 later; only reading the helper catches it.
    """
    assert _name_violations(["app/src/pim_health.ts"]), "missed a use-case term in a basename"
    assert _name_violations(["docs/patent_notes/x.md"]), (
        "missed a use-case term in a directory name"
    )
    # The ``pim``-in-``compilation`` trap that
    # test_t1_regex_word_boundary_negative_case guards on the content side.
    assert not _name_violations(["sage/compilation_utils.py"]), (
        "``pim`` matched inside ``compilation``; word-boundary tuning was lost"
    )

    # Inherited boundary, asserted so it is a documented property rather
    # than a surprise: the ``pim`` alternates carry a trailing ``\b``, so
    # the token is caught only when a non-word character follows it.
    # ``pim_health.ts`` and ``pim-health-mock.ts`` match; ``pim_fixture``
    # does not, because ``_`` is a regex word character. This is a
    # property of the shared content-side pattern, not of the name scan —
    # T1 reads the same way inside file contents. Widening it would
    # change T1's behaviour too and belongs in its own change.
    assert _name_violations(["app/src/pim-health-mock.ts"])
    assert not _name_violations(["app/src/pim_fixture.ts"])


def test_t12_scan_reaches_directory_components_and_basenames() -> None:
    """T12 inspects every path component and attributes a hit to the
    component that owns it.

    An implementation that looked only at ``Path.name`` would miss the
    directory entirely; one that matched against the whole path string
    would report the file rather than the directory, and would drag the
    clean sibling in alongside it.
    """
    violations = _name_violations(["a/t0099/b.py", "scripts/clean.py"])

    assert len(violations) == 1, f"expected exactly one violation, got {violations}"
    owner, detail = violations[0]
    assert owner == "a/t0099", f"hit attributed to {owner!r}, not the offending directory"
    assert "t0099" in detail

    # The same directory under many files is reported once, not per file.
    repeated = _name_violations(["a/t0099/b.py", "a/t0099/c.py", "a/t0099/d/e.py"])
    assert len(repeated) == 1, f"directory hit not deduplicated: {repeated}"

    # ...but two DIFFERENT directories that happen to share a name are two
    # violations, not one. Deduplicating on the component name alone
    # collapses them, and is indistinguishable from the correct
    # implementation on every other case in this file — including the
    # assertion directly above, which it satisfies exactly.
    distinct = _name_violations(["a/t0099/b.py", "z/t0099/c.py"])
    assert len(distinct) == 2, f"distinct directories collapsed by name: {distinct}"
    assert {owner for owner, _ in distinct} == {"a/t0099", "z/t0099"}


# ---------------------------------------------------------------------------
# T13 detector self-tests
#
# These carry more weight than the usual anti-coincidental margin, for the
# reason T12's do: once the tree is clean, ``test_no_ticket_ids_in_python_
# identifiers`` passes against a detector that returns nothing at all. The
# synthetic sources below are the only thing standing between a real gate
# and a decorative one.
#
# Kept as strings so this module's own AST — and the gate's walk over any
# file that is not excluded — never sees the offending defs as real code.
# ---------------------------------------------------------------------------

# One offender at each of the four nesting positions a definition can
# occupy: module level, class body, function body, and a class whose own
# name is the offender.
_SYNTHETIC_NESTED_IDENTIFIER_SOURCE: Final[str] = textwrap.dedent(
    """
    def test_t0001_module_level():
        assert True

    class TestThing:
        async def test_t0002_class_method(self):
            assert True

    def outer():
        def test_t0003_inside_a_function():
            assert True
        return test_t0003_inside_a_function

    class T0004Config:
        pass
    """
)

# Only clean names, including ones that sit at the same four positions.
_SYNTHETIC_CLEAN_IDENTIFIER_SOURCE: Final[str] = textwrap.dedent(
    """
    def test_module_level():
        assert True

    class TestThing:
        async def test_class_method(self):
            assert True

    def outer():
        def inner_helper():
            assert True
        return inner_helper

    class Config:
        pass
    """
)


def test_t13_detector_reaches_nested_definitions() -> None:
    """T13 walks to every nesting depth, not just module level.

    This is the assertion the gate cannot make for itself. 200 of the
    definitions the establishing cleanup renamed sat at module level, so a
    detector that iterated ``tree.body`` alone would have produced an
    almost-identical violation list against the pre-cleanup tree and an
    identical (empty) one after — indistinguishable from the correct
    implementation on the only evidence the gate test consults.

    A ``tree.body``-only walk finds two of the four below; one that skips
    ``ClassDef`` finds three.
    """
    tree = ast.parse(_SYNTHETIC_NESTED_IDENTIFIER_SOURCE)
    names = {name for _, name in _identifier_violations(tree)}

    assert "test_t0001_module_level" in names, "missed a module-level def"
    assert "test_t0002_class_method" in names, "missed a method inside a class"
    assert "test_t0003_inside_a_function" in names, "missed a def inside a function"
    assert "T0004Config" in names, "missed a class whose own name carries the id"
    assert len(names) == 4, f"unexpected extra hits: {names}"

    # The same walk over an equivalently-shaped clean tree finds nothing,
    # so the four hits above are the names and not the shape.
    assert not _identifier_violations(ast.parse(_SYNTHETIC_CLEAN_IDENTIFIER_SOURCE))


def test_t13_detector_covers_all_three_node_types() -> None:
    """T13 flags ``def``, ``async def``, and ``class`` alike.

    The tree carries no offending class at all, so the ``ClassDef`` arm has
    no natural coverage anywhere else: it can only be exercised here.
    """
    tree = ast.parse(
        textwrap.dedent(
            """
            def test_t0010_sync():
                assert True

            async def test_t0011_async():
                assert True

            class T0012Case:
                pass
            """
        )
    )
    names = {name for _, name in _identifier_violations(tree)}
    assert names == {"test_t0010_sync", "test_t0011_async", "T0012Case"}, (
        f"a node type was dropped: {names}"
    )


def test_t13_detector_matches_identifier_id_forms_and_rejects_near_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T13 uses the name-shaped ticket pattern, not the prose one, and reads
    it from the module rather than carrying its own copy.

    Anti-coincidental-pass. Three rivals:

    - Wired to ``TICKET_REF_RE``: an identifier cannot carry the canonical
      ``T-0452`` spacing, since a hyphen is not legal in a Python name, so
      every positive below is invisible to the prose pattern. That rival
      matches nothing and passes on any clean tree.
    - Boundary retuned: the near-misses below fail.
    - **Pattern inlined** rather than looked up. This one passes every
      case above and below, and drifts from T12 the moment either pattern
      moves. The closing monkeypatch is what excludes it: swapping the
      module attribute must change the detector's answer, which is only
      true if the detector reads the attribute. The sibling T12 test
      documents this rival in prose and notes that "only reading the
      helper catches it" — it is mechanically checkable, so check it.
    """
    positives = {
        "test_t0157_target_edges": "unhyphenated, mid-name after an underscore",
        "test_t_0148_catalog_default": "underscore-separated form",
        "t0076_retrieval_service": "at the start of the name",
        "T0452Case": "uppercase and hyphenless, opening a class name",
    }
    for name, why in positives.items():
        source = f"class {name}:\n    pass\n" if name[0].isupper() else f"def {name}():\n    pass\n"
        assert _identifier_violations(ast.parse(source)), f"missed a ticket id ({why}): {name}"
        # The prose pattern this must NOT be wired to.
        assert not TICKET_REF_RE.search(name), (
            f"{name!r} is matched by TICKET_REF_RE; it no longer discriminates the two patterns"
        )

    negatives = {
        "test_abstract_2024_rollup": "the ``t`` closing ``abstract`` precedes a bare year",
        "test_t20260825_snapshot": "a timestamp, not a four-digit id",
        "test_report_0074_shape": "four digits with no adjacent ``t``",
        "test_cas_adr_042_anchor": "the only sanctioned durable-surface anchor",
        "test_t1_light_strips_document": "a one-digit case tag, not a ticket id",
    }
    for name, why in negatives.items():
        assert not _identifier_violations(ast.parse(f"def {name}():\n    pass\n")), (
            f"false positive ({why}): {name}"
        )

    # Inherited boundary, asserted so it is a documented property rather
    # than a surprise — the same treatment the ``pim_fixture`` case gets in
    # ``test_t12_use_case_pattern_matches_names``.
    #
    # ``NAME_TICKET_REF_RE`` opens with ``(?<![0-9A-Za-z])``, so the id must
    # be preceded by a non-alphanumeric character or start the name. An id
    # buried mid-CamelCase (``TestT0452Case``, ``TicketT0452``) is therefore
    # NOT caught. The boundary is what keeps ``abstract_2024`` from
    # matching, and it is shared with T12, where the same lookbehind reads
    # against path components — widening it would change T12's behaviour
    # too and belongs in its own change. Snake_case is this repository's
    # convention for the functions that carry ids, and no class in the tree
    # carries one in any form, so nothing is escaping through this today.
    assert not _identifier_violations(ast.parse("class TestT0452Case:\n    pass\n"))
    assert _identifier_violations(ast.parse("class Test_T0452_Case:\n    pass\n"))

    # Excludes the inlined-copy rival: point the module attribute at a
    # pattern that matches something else entirely, and the detector's
    # answer must follow it in both directions.
    monkeypatch.setitem(globals(), "NAME_TICKET_REF_RE", re.compile(r"zzmarker"))
    assert not _identifier_violations(ast.parse("def test_t0157_target_edges():\n    pass\n")), (
        "detector still matched a ticket id after the module pattern was swapped out; "
        "it carries its own copy instead of consulting NAME_TICKET_REF_RE"
    )
    assert _identifier_violations(ast.parse("def zzmarker_probe():\n    pass\n")), (
        "detector did not pick up the swapped-in pattern; it is not reading the module attribute"
    )


def test_t13_detector_reports_the_offending_line_and_name() -> None:
    """T13 attributes a hit to the definition that owns it, once per
    definition.

    An implementation that reported the enclosing module, the first line of
    the file, or the whole file would satisfy a bare
    ``assert _identifier_violations(tree)`` and give no way to find the
    offender in a 4000-line test module.

    Two further rivals, neither of which an undecorated single-offender
    fixture can separate:

    - Reporting the *decorator's* line instead of the ``def``'s. Nearly
      every offending definition in this repository carries a
      ``@pytest.mark`` or ``@pytest.fixture``, so this rival would
      misreport almost every real hit while passing against a bare def.
      ``ast.FunctionDef.lineno`` is the ``def`` line, not the decorator's.
    - Deduplicating by name. The sibling ``_name_violations`` helper dedupes
      by design, so mirroring it here is the likelier mistake than
      inventing it — and one file can legitimately declare the same
      offending name twice, in two different class bodies.
    """
    source = "def clean_helper():\n    pass\n\n\ndef test_t0020_offender():\n    pass\n"
    violations = _identifier_violations(ast.parse(source))

    assert len(violations) == 1, f"expected exactly one violation, got {violations}"
    line_no, name = violations[0]
    assert name == "test_t0020_offender"
    assert line_no == 5, f"hit attributed to line {line_no}, not the def's own line"

    # A decorated definition: the hit belongs to the ``def`` line (4), not
    # to the decorator above it (3).
    decorated = ast.parse(
        textwrap.dedent(
            """
            import pytest

            @pytest.mark.asyncio
            async def test_t0021_decorated():
                pass
            """
        )
    )
    decorated_hits = _identifier_violations(decorated)
    assert len(decorated_hits) == 1
    assert decorated_hits[0] == (5, "test_t0021_decorated"), (
        f"decorated def attributed to {decorated_hits[0]}; likely reporting the decorator's line"
    )

    # The same offending name in two class bodies is two violations, not
    # one — a name-keyed dedup collapses them and under-reports the tree.
    repeated = ast.parse(
        textwrap.dedent(
            """
            class TestA:
                def test_t0022_shared(self):
                    pass

            class TestB:
                def test_t0022_shared(self):
                    pass
            """
        )
    )
    assert len(_identifier_violations(repeated)) == 2, "two same-named definitions collapsed to one"


def test_t13_scan_scope_is_non_empty_and_honours_exclusions() -> None:
    """T13's file enumeration reaches real files and skips the right ones.

    A gate over an empty file list passes for the wrong reason and looks
    exactly like a gate over a clean tree. The scope assertions below also
    pin the exclusions, so widening them silently is not possible.
    """
    scanned = {str(p.relative_to(REPO_ROOT)) for p in _tracked_files_with_suffixes((".py",))}

    assert len(scanned) > 100, f"T13 would scan only {len(scanned)} file(s); enumeration is broken"
    assert "tests/test_collection_integrity.py" in scanned, "a real test module is out of scope"
    assert not any(rel.startswith("domains/") for rel in scanned)
    assert not any(rel.startswith(".claude/") for rel in scanned)
    assert "tests/test_public_posture.py" not in scanned, "the gate must not scan itself"
