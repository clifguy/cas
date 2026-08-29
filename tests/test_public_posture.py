"""Public posture gate.

Enforces the invariants that make this repository safe for public release.
The cleanup pass that established these invariants is documented in
``CLAUDE.md`` §Coding Conventions ("Durable code surfaces stay public-ready")
and enforced at commit time by the ``cas-code-review`` skill §P1. This test
is the substrate-level counterpart: a deterministic gate that fails the
build whenever a forbidden pattern reappears in a tracked file.

Twelve invariants are checked. The numbering is the gate's own and is not
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

  T14. Ticket ids must not appear in Python *bindings* — the names a
       variable, constant, or parameter is bound to — at any nesting
       depth. The fourth face of the rule T2, T12, and T13 enforce on file
       contents, on file and directory names, and on definitions. T13's
       accepted scope was definitions; a constant is code exactly as a
       function name is, so the prohibition already covered bindings and
       only the enforcement was missing. Kept separate from T13 rather
       than folded into it: the two walk different node families, this one
       must descend into unpacking targets and argument lists, and
       separate allowlists let one be drained without loosening the other.
       Shares ``NAME_TICKET_REF_RE`` with T12 and T13 for the same reason
       they need it. Accepted scope is assignment targets and function
       parameters; ``for`` targets, ``with ... as``, ``except ... as``,
       walrus, comprehension targets, import aliases, and attribute or
       subscript targets are documented exclusions rather than oversights
       — see ``_binding_violations``.

Scope — top-level directories excluded from T1/T3/T4/T12/T13/T14:

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

The six allowlist constants near the top of the module follow the
pattern of ``KNOWN_VIOLATIONS`` in ``tests/sage/test_typed_alias_coverage.py``
and ``KNOWN_ARG_DRIFT`` in ``tests/sage/test_mcp_tool_conformance.py``.
All six are empty at the close of the establishing cleanup. Every
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
- ``test_t14_detector_reaches_every_assignment_form`` and
  ``test_t14_detector_covers_every_parameter_kind`` carry more weight than
  any bullet above. Every binding the establishing change renamed was a
  module-level plain ``Assign``: the tree held no annotated or augmented
  assignment, no unpacking target, no class-body or function-local
  constant, and — once T13's fixture renames landed — no parameter
  carrying an id. A detector covering only module-level plain ``Assign``
  therefore reds identically against the pre-change tree and passes
  identically after it. Every other arm is proven by synthetic source or
  not at all.
- ``test_t14_detector_matches_binding_id_forms_and_rejects_near_misses``
  is T13's sibling and excludes the same three rivals, including the
  inlined-copy one, via the same monkeypatch in both directions — asserted
  against the parameter arm as well as the assignment arm, since either
  could carry its own pattern.
- ``test_t14_detector_reports_the_offending_line_and_name`` pins
  attribution to the bound name's own line rather than the statement's,
  which is the only thing that separates a correct hit from a useless one
  inside a multi-line unpacking target, and pins the absence of name-keyed
  dedup.
- ``test_t14_detector_ignores_non_name_targets_and_unbound_forms`` asserts
  the documented exclusions are the implementation's actual behaviour, and
  closes with a positive control so a detector that returns nothing at all
  cannot satisfy an all-negative test.

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

# Keyword-argument names that mark a string literal as *published*
# documentation wherever the call appears: Pydantic ``Field(description=)``
# renders into the OpenAPI spec and the MCP tool schemas; argparse
# ``description`` / ``help`` / ``epilog`` render into ``--help`` output.
# The published set is defined by these destination markers, not by which
# callable receives them — a ``description=`` kwarg is documentation on
# any call.
_PUBLISHED_STRING_KWARGS: Final[frozenset[str]] = frozenset({"description", "help", "epilog"})

# Attribute names that mark a call as a logging emission whose message
# text reaches operators. Matches the stdlib ``logging`` method surface.
_LOGGING_METHOD_NAMES: Final[frozenset[str]] = frozenset(
    {"debug", "info", "warning", "error", "exception", "critical", "log"}
)

# Tracked build artifacts that should be .gitignore'd and never committed.
_BUILD_ARTIFACTS: Final[frozenset[str]] = frozenset(
    {".coverage", "coverage.xml", "repo_file_inventory.xlsx"}
)

# Maximum number of violations to enumerate in a single pytest.fail message.
_MAX_REPORTED_VIOLATIONS: Final[int] = 30


# ---------------------------------------------------------------------------
# Allowlists
#
# All seven are empty at the close of the establishing cleanup. Each entry
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

# path (relative to repo root) -> names of bindings in that file whose
# ticket-id-shaped name is exempted. Separate from
# ``IDENTIFIER_TOKEN_ALLOWLIST`` above for the reason that one is separate
# from ``NAME_TOKEN_ALLOWLIST``: a definition and a binding are different
# node families, so an exemption granted to one must not silently cover the
# other.
BINDING_TOKEN_ALLOWLIST: Final[dict[str, list[str]]] = {}

# path (relative to repo root) → list of line numbers where a T-NNNN ref
# in a *published* string is allowlisted. Shared by both T15 arms (the
# ``.py`` sink detector and the substrate text scan); line-keyed like
# ``TICKET_REF_ALLOWLIST`` because both arms attribute hits to lines.
PUBLISHED_TICKET_REF_ALLOWLIST: Final[dict[str, list[int]]] = {}


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
# Binding scanning
# ---------------------------------------------------------------------------


def _binding_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, name)`` for every *binding* whose name carries a
    ticket id.

    The binding-side counterpart to :func:`_identifier_violations`, which
    covers definitions. Two families are in scope — the two ways a name is
    bound outside a ``def`` or ``class`` header:

    - assignment targets: ``Assign`` (every target of a chained
      ``A = B = value``), ``AnnAssign``, and ``AugAssign``, descending
      through ``Tuple``, ``List``, and ``Starred`` so an offending element
      of an unpacking target is reached;
    - parameters: all five groups of an ``arguments`` node
      (``posonlyargs``, ``args``, ``kwonlyargs``, ``vararg``, ``kwarg``), on
      ``FunctionDef``, ``AsyncFunctionDef``, and ``Lambda`` alike.

    Nesting depth is not consulted: a constant in a class body and a local
    inside a function are reached exactly as a module-level constant is. A
    constant is code in every one of those positions, and the walk that
    reaches the outermost reaches them all.

    Deliberately out of reach, recorded here so the next node family over is
    a visible decision rather than a later discovery:

    - ``Attribute`` and ``Subscript`` targets (``self.x = ...``,
      ``d["k"] = ...``). These bind an attribute or a key, not a name.
    - ``for`` targets, ``with ... as``, ``except ... as``, walrus
      (``NamedExpr``), comprehension targets, and ``import ... as`` aliases.
      Each does bind a name; each is a distinct node family whose inclusion
      belongs to its own change.
    - ``global`` / ``nonlocal`` declarations, which rebind nothing on their
      own and name a binding caught where it is assigned.

    Uses ``NAME_TICKET_REF_RE`` rather than ``TICKET_REF_RE``, and reads it
    from the module rather than carrying a copy, for the reasons
    :func:`_identifier_violations` gives: a hyphen is not legal in a Python
    name, so the prose pattern matches no binding at all, and an inlined
    copy drifts from T12 and T13 the moment either pattern moves.

    Line numbers come from the ``ast.Name`` or ``ast.arg`` node itself
    rather than from the enclosing statement, so an offender inside a
    multi-line unpacking target is attributed to its own line. Not
    deduplicated: two bindings of the same offending name are two
    violations, as they are for definitions.

    Pure: takes a parsed tree, consults no allowlist, touches no filesystem.
    Mirrors :func:`_identifier_violations`, which factors its walk the same
    way so the unit tests can drive it against synthetic source.
    """

    def _name_leaves(target: ast.expr) -> Iterable[ast.Name]:
        """Yield every ``ast.Name`` an assignment target binds."""
        if isinstance(target, ast.Name):
            yield target
        elif isinstance(target, ast.Starred):
            yield from _name_leaves(target.value)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                yield from _name_leaves(element)

    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for target in targets:
            for leaf in _name_leaves(target):
                if NAME_TICKET_REF_RE.search(leaf.id):
                    found.append((leaf.lineno, leaf.id))

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            spec = node.args
            parameters = [
                *spec.posonlyargs,
                *spec.args,
                *spec.kwonlyargs,
                *([spec.vararg] if spec.vararg is not None else []),
                *([spec.kwarg] if spec.kwarg is not None else []),
            ]
            for parameter in parameters:
                if NAME_TICKET_REF_RE.search(parameter.arg):
                    found.append((parameter.lineno, parameter.arg))

    return found


# ---------------------------------------------------------------------------
# Published-string scanning
# ---------------------------------------------------------------------------


def _published_string_violations(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, matched_ref)`` for every ticket id in a *published*
    string literal.

    The string-literal counterpart to T2, scoped by destination rather than
    by file tree: a literal is in scope when it is written directly at a
    sink that renders it to a reader outside the process. Three sink
    families:

    - documentation keywords: a keyword argument named in
      ``_PUBLISHED_STRING_KWARGS`` on any call — Pydantic
      ``Field(description=)`` (rendered into the OpenAPI spec and MCP tool
      schemas) and argparse ``description`` / ``help`` / ``epilog``
      (rendered into ``--help`` text);
    - raise messages: every literal argument of the call in a ``raise``
      statement, positional or keyword — error text is API response and
      console surface;
    - logging messages: literal positional arguments of a call whose
      method name is in ``_LOGGING_METHOD_NAMES`` — log text reaches
      operators.

    A literal is either a plain ``Constant`` (implicit concatenation folds
    to one at parse time) or the ``Constant`` fragments of a ``JoinedStr``
    (an f-string). A multi-line concatenated literal is attributed to its
    first line.

    Deliberately out of reach, recorded here so the boundary is a visible
    decision rather than a later discovery:

    - **indirect construction** — a string reaching a sink through a
      variable, attribute, or concatenation expression
      (``raise ValueError(msg)``, ``Field(description=DETAIL)``,
      ``"..." + reason``). Following dataflow is a different detector; the
      literal-at-sink scope matches how every measured real offender was
      written.
    - unpublished literals — fixture values, dict payloads, non-sink
      keyword arguments. The whole rule is the distinction between these
      and the sinks above.

    Uses ``TICKET_REF_RE`` — the hyphenated prose pattern, since a
    published string is prose — and reads it from the module rather than
    carrying a copy, so it cannot drift from T2's notion of a ticket ref.

    Pure: takes a parsed tree, consults no allowlist, touches no
    filesystem. Mirrors :func:`_identifier_violations` and
    :func:`_binding_violations`, which factor their walks the same way so
    the unit tests can drive them against synthetic source.
    """
    found: list[tuple[int, str]] = []

    def _scan(expr: ast.expr) -> None:
        texts: list[tuple[int, str]] = []
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            texts.append((expr.lineno, expr.value))
        elif isinstance(expr, ast.JoinedStr):
            for part in expr.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    texts.append((part.lineno, part.value))
        for lineno, text in texts:
            for match in TICKET_REF_RE.finditer(text):
                found.append((lineno, match.group(0)))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in _PUBLISHED_STRING_KWARGS:
                    _scan(keyword.value)
            if isinstance(node.func, ast.Attribute) and node.func.attr in _LOGGING_METHOD_NAMES:
                for arg in node.args:
                    _scan(arg)
        elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            for arg in node.exc.args:
                _scan(arg)
            for keyword in node.exc.keywords:
                # A documentation keyword on a raised call is already
                # scanned by the Call arm above; skipping it here keeps
                # one physical literal from being reported twice.
                if keyword.arg not in _PUBLISHED_STRING_KWARGS:
                    _scan(keyword.value)

    return found


def _published_string_py_paths() -> list[Path]:
    """Tracked ``.py`` files in scope for T15's published-string arm.

    The T13/T14 scope minus the test tree. The exclusion is the point, not
    an accident: the rule is scoped by destination, and nothing in
    ``tests/`` is published — its raise messages are simulated-error
    fixtures and its literals are fixture values, so a sink-scoped walk
    over the test tree would flag text no reader outside the process ever
    sees. Asserted in the scope self-test so the decision stays visible.
    """
    return [
        path
        for path in _tracked_files_with_suffixes((".py",))
        if not str(path.relative_to(REPO_ROOT)).startswith("tests/")
    ]


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
# T14 — Ticket ids in Python bindings
# ---------------------------------------------------------------------------


def test_no_ticket_ids_in_python_bindings() -> None:
    """T14: no tracked ``.py`` file may bind a variable, constant, or
    parameter whose *name* carries a ticket id.

    The binding-side counterpart to T13, whose accepted scope was
    definitions. A constant is code exactly as a function name is, so the
    prohibition already covered bindings and only the enforcement was
    missing — the same gap T13 closed, one AST node family over.

    Kept as its own invariant rather than folded into T13: T13's name,
    docstring, and failure header all say "definitions"; the two detectors
    walk different node families, one of which must descend into unpacking
    targets and argument lists; and separate allowlists let one be drained
    without loosening the other.

    Scope matches T1/T3/T4/T12/T13 via ``_tracked_files_with_suffixes``,
    which excludes ``domains/``, ``.claude/``, and this gate file.
    Unparseable modules are skipped: a file that cannot be parsed fails its
    own collection loudly and is a different problem.
    """
    violations: list[tuple[str, int, str]] = []
    for path in _tracked_files_with_suffixes((".py",)):
        rel = str(path.relative_to(REPO_ROOT))
        try:
            tree = ast.parse(path.read_bytes(), filename=str(path))
        except SyntaxError:
            continue
        allowed = set(BINDING_TOKEN_ALLOWLIST.get(rel, []))
        for line_no, name in _binding_violations(tree):
            if name in allowed:
                continue
            violations.append((rel, line_no, name))

    if violations:
        pytest.fail(_format_violations(violations, header="T14 binding-token"))


# ---------------------------------------------------------------------------
# T15 — Ticket ids in published strings
# ---------------------------------------------------------------------------


def test_no_ticket_refs_in_published_py_strings() -> None:
    """T15a: no in-scope ``.py`` file may carry a ticket id in a string
    literal written at a published sink — a documentation keyword
    (``description`` / ``help`` / ``epilog``), a raise message, or a
    logging message.

    T2 gates docstrings and ``#`` comments and excludes string literals so
    a fixture value is not confused with a comment. That exclusion is right
    for literals that stay inside the process, but a ``Field`` description
    is rendered into the OpenAPI spec, argparse text into ``--help``
    output, and error text into API responses — those literals are durable
    public surfaces exactly as a docstring is. The rule is scoped by
    destination, not by tree.

    Scope is the T13/T14 file set minus ``tests/``
    (:func:`_published_string_py_paths`); unparseable modules are skipped
    for the reason T13 gives.
    """
    violations: list[tuple[str, int, str]] = []
    for path in _published_string_py_paths():
        rel = str(path.relative_to(REPO_ROOT))
        try:
            tree = ast.parse(path.read_bytes(), filename=str(path))
        except SyntaxError:
            continue
        allowed = set(PUBLISHED_TICKET_REF_ALLOWLIST.get(rel, []))
        for line_no, ref in _published_string_violations(tree):
            if line_no in allowed:
                continue
            violations.append((rel, line_no, ref))

    if violations:
        pytest.fail(_format_violations(violations, header="T15 published-string ticket-ref"))


def test_no_ticket_refs_in_published_substrate_files() -> None:
    """T15b: no tracked ``.yaml`` / ``.yml`` / ``.json`` file may carry a
    ticket id anywhere in its text.

    The substrate arm of T15. The committed Formal Substrate — OpenAPI
    ``description:`` and ``summary:`` blocks, JSON Schema descriptions, the
    manifest's revision history — is published in its entirety, so the scan
    is whole-text rather than key-scoped: there is no unpublished position
    inside these files for a literal to be exempt from. Scope and line
    attribution match T1's walk of the same suffixes.
    """
    violations: list[tuple[str, int, str]] = []
    for path in _tracked_files_with_suffixes((".yaml", ".yml", ".json")):
        rel = str(path.relative_to(REPO_ROOT))
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        allowed = set(PUBLISHED_TICKET_REF_ALLOWLIST.get(rel, []))
        for match in TICKET_REF_RE.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            if line_no in allowed:
                continue
            violations.append((rel, line_no, match.group(0)))

    if violations:
        pytest.fail(_format_violations(violations, header="T15 substrate ticket-ref"))


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


# ---------------------------------------------------------------------------
# T14 detector self-tests
#
# These carry T13's burden and then some. The change that introduced T14
# also drained the tree of every offending binding, so afterwards
# ``test_no_ticket_ids_in_python_bindings`` passes against a detector that
# returns nothing at all. Worse than T13's case: every one of the twenty
# bindings that change renamed was a module-level plain ``Assign``. The
# tree held no annotated or augmented assignment, no unpacking target, no
# class-body or function-local constant, and — once T13's own fixture
# renames landed — no parameter carrying an id. So a detector covering only
# module-level plain ``Assign`` behaves identically to a correct one on the
# only evidence the gate test consults, in both directions. Every other arm
# is proven below or not at all.
#
# Kept as strings so this module's own AST — and the gate's walk over any
# file that is not excluded — never sees the offending bindings as real
# code.
# ---------------------------------------------------------------------------

# One offender in each assignment shape: module level, class body, function
# body, annotated, augmented, and a tuple-unpacking target where only one
# element offends.
_SYNTHETIC_BINDING_SOURCE: Final[str] = textwrap.dedent(
    """
    from typing import Final

    _T0001_MODULE_LEVEL = "module level"

    class Holder:
        _T0002_CLASS_BODY = "class body"

    def enclosing():
        _t0003_local = "function body"
        return _t0003_local

    _T0004_ANNOTATED: Final[str] = "annotated"

    _T0005_AUGMENTED = 0
    _T0005_AUGMENTED += 1

    clean_head, _T0006_UNPACKED, clean_tail = (1, 2, 3)

    clean_lead, *_T0007_STARRED_REST = (1, 2, 3)

    [_T0008_LIST_TARGET, clean_second] = (4, 5)
    """
)

# The same eight shapes, all cleanly named.
_SYNTHETIC_CLEAN_BINDING_SOURCE: Final[str] = textwrap.dedent(
    """
    from typing import Final

    MODULE_LEVEL = "module level"

    class Holder:
        CLASS_BODY = "class body"

    def enclosing():
        local_value = "function body"
        return local_value

    ANNOTATED: Final[str] = "annotated"

    AUGMENTED = 0
    AUGMENTED += 1

    clean_head, unpacked, clean_tail = (1, 2, 3)

    clean_lead, *starred_rest = (1, 2, 3)

    [list_target, clean_second] = (4, 5)
    """
)


def test_t14_detector_reaches_every_assignment_form() -> None:
    """T14 reaches every assignment shape, not just the one the tree has.

    This is the assertion the gate cannot make for itself. All twenty
    bindings the establishing change renamed were module-level plain
    ``Assign``, so a detector that handled only that shape would have
    produced an identical violation list against the pre-change tree and an
    identical (empty) one after — indistinguishable from a correct
    implementation on the only evidence the gate test consults.

    Of the six below, a module-level-plain-``Assign``-only detector finds
    one; one that skips ``AnnAssign`` and ``AugAssign`` finds four; one that
    treats a ``Tuple`` target as opaque finds five.
    """
    violations = _binding_violations(ast.parse(_SYNTHETIC_BINDING_SOURCE))
    names = {name for _, name in violations}
    expected = {
        "_T0001_MODULE_LEVEL",
        "_T0002_CLASS_BODY",
        "_t0003_local",
        "_T0004_ANNOTATED",
        "_T0005_AUGMENTED",
        "_T0006_UNPACKED",
        "_T0007_STARRED_REST",
        "_T0008_LIST_TARGET",
    }
    assert names == expected, f"assignment shapes missed or over-matched: {expected ^ names}"

    # ``_T0005_AUGMENTED`` is bound twice — once by the plain ``Assign``
    # that creates it, once by the ``AugAssign`` that rebinds it. Asserting
    # the *name* is present cannot tell those apart, so a detector missing
    # the ``AugAssign`` arm entirely still finds it via the ``Assign``
    # above. Only the site count separates them.
    augmented = [line for line, name in violations if name == "_T0005_AUGMENTED"]
    assert len(augmented) == 2, (
        f"the augmented-assignment arm is not reached: {len(augmented)} site(s), expected 2"
    )

    # The same eight shapes with clean names find nothing, so the eight hits
    # above are the names and not the shape.
    assert not _binding_violations(ast.parse(_SYNTHETIC_CLEAN_BINDING_SOURCE))


def test_t14_detector_covers_every_parameter_kind() -> None:
    """T14 flags all five parameter groups, and a lambda's alike.

    The tree carries no parameter offender at all — T13's fixture renames
    removed the last of them — so this arm has no natural coverage anywhere
    else and can only be exercised here. A detector that reads
    ``node.args.args`` alone, the obvious first cut, finds one of the six
    below; one that handles ``FunctionDef`` but not ``Lambda`` finds five.
    """
    tree = ast.parse(
        textwrap.dedent(
            """
            def takes_every_kind(
                t0010_positional_only,
                /,
                t0011_ordinary,
                *t0012_varargs,
                t0013_keyword_only,
                **t0014_kwargs
            ):
                return None

            lambda_holder = lambda t0015_lambda_param: t0015_lambda_param

            async def takes_async_kind(t0016_async_param):
                return t0016_async_param
            """
        )
    )
    names = {name for _, name in _binding_violations(tree)}
    assert names == {
        "t0010_positional_only",
        "t0011_ordinary",
        "t0012_varargs",
        "t0013_keyword_only",
        "t0014_kwargs",
        "t0015_lambda_param",
        "t0016_async_param",
    }, f"a parameter group or definition kind was dropped: {names}"


def test_t14_detector_matches_binding_id_forms_and_rejects_near_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T14 uses the name-shaped ticket pattern, not the prose one, and reads
    it from the module rather than carrying its own copy.

    T13's sibling, and the same three rivals apply:

    - Wired to ``TICKET_REF_RE``: a binding cannot carry the canonical
      hyphenated spacing, so every positive below is invisible to the prose
      pattern and that rival returns empty on any tree.
    - Boundary retuned: the near-misses below fail.
    - **Pattern inlined** rather than looked up. This one passes every case
      above and below and drifts from T12 and T13 the moment either pattern
      moves. The closing monkeypatch is what excludes it, and it is applied
      to the parameter arm as well as the assignment arm — the detector has
      two call sites, and either could carry its own copy.
    """
    positives = {
        "_T0124_EDGE_ID": "uppercase, opening the name behind a leading underscore",
        "t_0148_default": "underscore-separated form",
        "t0076_service": "at the start of the name",
        "_ABSTRACT_T0153_PROBE": "mid-name, behind an underscore",
    }
    for name, why in positives.items():
        assert _binding_violations(ast.parse(f"{name} = 1\n")), (
            f"missed a ticket id ({why}): {name}"
        )
        # The prose pattern this must NOT be wired to.
        assert not TICKET_REF_RE.search(name), (
            f"{name!r} is matched by TICKET_REF_RE; it no longer discriminates the two patterns"
        )

    negatives = {
        "abstract_2024_rollup": "the ``t`` closing ``abstract`` precedes a bare year",
        "t20260825_snapshot": "a timestamp, not a four-digit id",
        "report_0074_shape": "four digits with no adjacent ``t``",
        "cas_adr_042_anchor": "the only sanctioned durable-surface anchor",
        "t1_light_strips_document": "a one-digit case tag, not a ticket id",
    }
    for name, why in negatives.items():
        assert not _binding_violations(ast.parse(f"{name} = 1\n")), (
            f"false positive ({why}): {name}"
        )

    # Inherited boundary, asserted so it is a documented property rather
    # than a surprise, exactly as T13 asserts it. ``NAME_TICKET_REF_RE``
    # opens with a non-alphanumeric lookbehind, so an id buried mid-CamelCase
    # is out of reach. That boundary is what keeps ``abstract_2024`` from
    # matching and is shared with T12, where the same lookbehind reads
    # against path components — widening it belongs in its own change.
    assert not _binding_violations(ast.parse("TicketT0452Sentinel = 1\n"))
    assert _binding_violations(ast.parse("Ticket_T0452_Sentinel = 1\n"))

    # Excludes the inlined-copy rival: point the module attribute at a
    # pattern that matches something else entirely, and both arms of the
    # detector must follow it in both directions.
    monkeypatch.setitem(globals(), "NAME_TICKET_REF_RE", re.compile(r"zzmarker"))
    assert not _binding_violations(ast.parse("_T0124_EDGE_ID = 1\n")), (
        "assignment arm still matched a ticket id after the module pattern was swapped out; "
        "it carries its own copy instead of consulting NAME_TICKET_REF_RE"
    )
    assert not _binding_violations(ast.parse("def probe(t0076_service):\n    pass\n")), (
        "parameter arm still matched a ticket id after the module pattern was swapped out; "
        "it carries its own copy instead of consulting NAME_TICKET_REF_RE"
    )
    assert _binding_violations(ast.parse("zzmarker_probe = 1\n")), (
        "assignment arm did not pick up the swapped-in pattern"
    )
    assert _binding_violations(ast.parse("def probe(zzmarker_param):\n    pass\n")), (
        "parameter arm did not pick up the swapped-in pattern"
    )


def test_t14_detector_reports_the_offending_line_and_name() -> None:
    """T14 attributes a hit to the bound name that owns it, once per binding.

    An implementation that reported the enclosing statement, the first line
    of the file, or the whole file would satisfy a bare
    ``assert _binding_violations(tree)`` and give no way to find the
    offender in a 4000-line module.

    Two rivals a single-offender fixture cannot separate:

    - Reporting the enclosing *statement's* line rather than the name's.
      Indistinguishable on a one-line assignment, and wrong on every
      multi-line unpacking target — where the statement's line is the open
      parenthesis and the offender may be anywhere below it.
    - Deduplicating by name. The sibling ``_name_violations`` dedupes by
      design, so mirroring it here is the likelier mistake than inventing
      it — and one file can legitimately bind the same offending name twice,
      in two different class bodies.
    """
    source = "clean_first = 1\n\n\n_T0020_OFFENDER = 2\n"
    violations = _binding_violations(ast.parse(source))

    assert len(violations) == 1, f"expected exactly one violation, got {violations}"
    assert violations[0] == (4, "_T0020_OFFENDER")

    # A multi-line unpacking target: the hit belongs to the offending
    # element's own line (4), not to the statement's opening line (2).
    multiline = _binding_violations(
        ast.parse(
            textwrap.dedent(
                """
                (
                    clean_head,
                    _T0021_MIDDLE,
                    clean_tail,
                ) = (1, 2, 3)
                """
            )
        )
    )
    assert len(multiline) == 1
    assert multiline[0] == (4, "_T0021_MIDDLE"), (
        f"unpacked hit attributed to {multiline[0]}; likely reporting the statement's line"
    )

    # A multi-line signature: the parameter hit belongs to the parameter's
    # own line (4), not to the ``def`` line (2). Every offending parameter
    # in this repository sat on a single-line signature, where the two are
    # indistinguishable, so a detector reporting ``node.lineno`` for
    # parameters passes against the whole tree.
    signature = _binding_violations(
        ast.parse(
            textwrap.dedent(
                """
                def probe(
                    clean_param,
                    t0024_offender,
                ):
                    return None
                """
            )
        )
    )
    assert signature == [(4, "t0024_offender")], (
        f"parameter hit attributed to {signature}; likely reporting the def's line"
    )

    # A chained assignment binds both targets; both offend, so both report.
    chained = _binding_violations(ast.parse("_T0022_FIRST = _T0022_SECOND = 3\n"))
    assert len(chained) == 2, f"a chained assignment reported {len(chained)} of its 2 targets"

    # The same offending name in two class bodies is two violations, not
    # one — a name-keyed dedup collapses them and under-reports the tree.
    repeated = ast.parse(
        textwrap.dedent(
            """
            class HolderA:
                _T0023_SHARED = 1

            class HolderB:
                _T0023_SHARED = 2
            """
        )
    )
    assert len(_binding_violations(repeated)) == 2, "two same-named bindings collapsed to one"


def test_t14_detector_ignores_non_name_targets_and_unbound_forms() -> None:
    """T14's exclusions are the implementation's behaviour, not a claim.

    ``_binding_violations`` accepts assignment targets and function
    parameters. The forms below also bind, or look like they bind, and are
    out of scope by decision rather than oversight — each is a distinct node
    family whose inclusion belongs to its own change. Asserting the
    exclusions here means widening the detector cannot happen silently: the
    entry that stops being excluded fails this test and has to be moved
    deliberately.

    Note where the line falls. A function-*local* assignment IS in scope
    (see ``test_t14_detector_reaches_every_assignment_form``); a
    function-local *loop target* is not. Depth is not the criterion; the
    node family is.
    """
    excluded = {
        "attribute target": (
            "class Holder:\n    def __init__(self):\n        self.t0030_field = 1\n"
        ),
        "subscript target": "registry = {}\nregistry['t0031'] = 1\n",
        "for target": "for t0032_item in range(3):\n    pass\n",
        "with-as target": "with open('f') as t0033_handle:\n    pass\n",
        "except-as target": "try:\n    pass\nexcept ValueError as t0034_error:\n    pass\n",
        "walrus target": "values = [1]\nif (t0035_seen := len(values)):\n    pass\n",
        "comprehension target": "squares = [t0036_n * t0036_n for t0036_n in range(3)]\n",
        "import alias": "import json as t0037_alias\n",
    }
    for why, source in excluded.items():
        assert not _binding_violations(ast.parse(source)), (
            f"{why} was flagged; T14's accepted scope is assignment targets and parameters"
        )

    # Positive control. Without it every assertion above is satisfied by a
    # detector that returns nothing at all — the exact rival this block of
    # self-tests exists to exclude.
    assert _binding_violations(ast.parse("_T0038_SENTINEL = 1\n")), (
        "the detector finds nothing even in a plain module-level assignment; "
        "the exclusions above prove nothing"
    )


def test_t14_scan_scope_is_non_empty_and_allowlist_ships_empty() -> None:
    """T14's file enumeration reaches real files, and nothing is exempted.

    A gate over an empty file list passes for the wrong reason and looks
    exactly like a gate over a clean tree. The exclusions themselves are
    pinned in full by ``test_t13_scan_scope_is_non_empty_and_honours_
    exclusions``, which reads the same helper; repeated here is only the
    non-emptiness guard and the one exclusion whose absence would make the
    gate scan its own synthetic sources.

    The allowlist assertion is T14's own: it ships empty, and every entry
    added later owes a 1-line rationale. An allowlist that quietly acquires
    entries is how a drained gate stops being one.
    """
    scanned = {str(p.relative_to(REPO_ROOT)) for p in _tracked_files_with_suffixes((".py",))}

    assert len(scanned) > 100, f"T14 would scan only {len(scanned)} file(s); enumeration is broken"
    # Membership across two trees, not a bare count. A scan narrowed to
    # ``tests/`` alone still clears 100 files, so the count by itself does
    # not distinguish a whole-repo enumeration from a partial one.
    assert "tests/test_collection_integrity.py" in scanned, "a real test module is out of scope"
    assert "sage/models/schemas.py" in scanned, "production code is out of scope"
    assert "tests/test_public_posture.py" not in scanned, "the gate must not scan itself"
    assert BINDING_TOKEN_ALLOWLIST == {}, (
        f"T14's allowlist is no longer empty: {BINDING_TOKEN_ALLOWLIST}"
    )


# ---------------------------------------------------------------------------
# T15 detector self-tests
#
# These carry the T13/T14 burden in its sharpest form yet. Of the real
# offenders the establishing sweep drained, all but four sat in a single
# form in a single file — ``Field(description=)`` in
# sage/models/schemas.py — so a detector handling only that one arm reds
# identically against the pre-sweep tree and passes identically after it.
# Every other sink arm (argparse keywords, raise messages, both logging
# shapes, the f-string path) is unfalsifiable against production code and
# is proven below or not at all.
#
# Kept as strings so this module's own AST — and the gate's walk over any
# file that is not excluded — never sees the offending literals as real
# code.
# ---------------------------------------------------------------------------

# One offender at each published sink form: a Pydantic Field description,
# the three argparse documentation keywords, a plain raise, an
# implicit-concatenation raise, an f-string raise, a bound-logger call,
# and a chained ``logging.getLogger(...)`` call.
_SYNTHETIC_PUBLISHED_SINK_SOURCE: Final[str] = textwrap.dedent(
    """
    import argparse
    import logging

    from pydantic import BaseModel, Field

    logger = logging.getLogger("probe")


    class Request(BaseModel):
        limit: int = Field(default=10, description="Page-size cap (T-0301).")


    parser = argparse.ArgumentParser(
        description="Reprojects staged documents (T-0302).",
        epilog="Safe to re-run (T-0303).",
    )
    parser.add_argument("--all", help="Process every vault (T-0304).")


    def check(count: int) -> None:
        if count < 0:
            raise ValueError("negative count is unsupported (T-0305)")
        if count == 0:
            raise RuntimeError(
                "empty batch: nothing to reproject; stage documents "
                "first (T-0306)"
            )
        raise LookupError(f"count {count} exceeds the shard budget (T-0307)")


    def report() -> None:
        logger.warning("falling back to serial mode (T-0308)")
        logging.getLogger("audit").error("shard drift detected (T-0309)")
    """
)

# The same id shapes at unpublished positions only: module constants, dict
# and list fixture values, a parameter default, and non-sink keyword
# arguments. A detector that scans every string literal — the rival T2's
# original exclusion exists to rule out — flags all of these.
_SYNTHETIC_UNPUBLISHED_LITERAL_SOURCE: Final[str] = textwrap.dedent(
    """
    FIXTURE_ID = "T-0310"
    PROBES = {"ticket_id": "T-0311"}
    ROWS = ["T-0312", "T-0313"]


    def build(name: str = "T-0314") -> dict:
        payload = {"summary": "simulated failure for T-0315 atomicity test"}
        return make_record(name="T-0316", tag="T-0317", payload=payload)


    def message() -> str:
        msg = "deferred guard tripped (T-0318)"
        return msg
    """
)

# Offending text that reaches a sink only *indirectly* — through a name,
# a module constant, or a concatenation expression. Deliberately out of
# the detector's scope; this fixture is what pins that decision.
_SYNTHETIC_INDIRECT_SINK_SOURCE: Final[str] = textwrap.dedent(
    """
    from pydantic import Field

    DETAIL = "projection stale (T-0320)"


    def fail(reason: str) -> None:
        msg = "reprojection halted (T-0321)"
        raise RuntimeError(msg)


    def concat(reason: str) -> None:
        raise ValueError("shard missing (T-0322): " + reason)


    def declare():
        return Field(default=None, description=DETAIL)
    """
)


def test_t15_detector_flags_each_published_sink_form() -> None:
    """T15 reaches every published sink form, not just Field descriptions.

    This is the concentration risk named at the top of this section: the
    real tree exercises one arm almost exclusively, so each of the other
    arms — argparse keywords, all three raise shapes (plain,
    implicit-concatenation, f-string), and both logging shapes (bound
    logger, chained ``getLogger``) — can only be proven here. The exact-set
    assertion also fails on double-reporting: a raise whose literal is
    scanned by two arms would surface a duplicate id.
    """
    tree = ast.parse(_SYNTHETIC_PUBLISHED_SINK_SOURCE)
    refs = [ref for _, ref in _published_string_violations(tree)]

    expected = {f"T-03{i:02d}" for i in range(1, 10)}
    assert set(refs) == expected, f"sink arms missed or over-reached: {sorted(refs)}"
    assert len(refs) == len(expected), f"a literal was reported more than once: {sorted(refs)}"


def test_t15_detector_ignores_unpublished_literals_in_same_source() -> None:
    """T15 flags nothing in a file whose ticket ids all sit at unpublished
    positions.

    The whole rule is this distinction. T2 excluded string literals so a
    fixture value is not confused with a comment; T15 re-includes only the
    published subset, and the rival it must exclude is the blanket scan
    that would sweep 170 test-tree fixture literals for no public-posture
    gain.
    """
    tree = ast.parse(_SYNTHETIC_UNPUBLISHED_LITERAL_SOURCE)
    assert not _published_string_violations(tree), (
        "an unpublished literal was flagged; the destination scoping is broken"
    )


def test_t15_detector_ignores_indirect_construction() -> None:
    """T15 does not follow a string through a variable, a constant, or a
    concatenation expression to a sink.

    A deliberate scope decision, asserted so it is a documented property
    rather than a later discovery: the detector is literal-at-sink only,
    matching how every measured real offender was written. Widening to
    dataflow is its own change.
    """
    tree = ast.parse(_SYNTHETIC_INDIRECT_SINK_SOURCE)
    assert not _published_string_violations(tree), (
        "an indirectly-constructed string was flagged; the literal-at-sink scope moved"
    )


def test_t15_detector_matches_id_forms_and_rejects_near_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T15 uses the hyphenated prose pattern, reads it from the module, and
    rejects near-misses.

    Anti-coincidental-pass. Two rivals:

    - Boundary retuned or pattern widened: the near-misses below fail.
      A published string is prose, so the name-shaped pattern T12/T13/T14
      share would over-match here (``t0157`` appears legitimately in
      prose-adjacent identifiers quoted inside descriptions).
    - **Pattern inlined** rather than looked up. Passes every fixed case
      and drifts from T2 the moment the pattern moves. The closing
      monkeypatch excludes it: swapping the module attribute must change
      the detector's answer in both directions.
    """
    near_misses = 'raise ValueError("CAT-0001 T-123 T-01234 CAS-ADR-042")\n'
    assert not _published_string_violations(ast.parse(near_misses)), (
        "a near-miss token was flagged; the prose pattern's boundaries moved"
    )

    # Line attribution: the hit belongs to the literal's own line.
    source = 'def f():\n    raise ValueError("boom (T-0330)")\n'
    violations = _published_string_violations(ast.parse(source))
    assert violations == [(2, "T-0330")], (
        f"expected [(2, 'T-0330')], got {violations}; line attribution is broken"
    )

    # Excludes the inlined-copy rival: point the module attribute at a
    # pattern that matches something else entirely, and the detector's
    # answer must follow it in both directions.
    monkeypatch.setitem(globals(), "TICKET_REF_RE", re.compile(r"zzmarker"))
    assert not _published_string_violations(ast.parse('raise ValueError("boom (T-0330)")\n')), (
        "detector still matched a ticket id after the module pattern was swapped out; "
        "it carries its own copy instead of consulting TICKET_REF_RE"
    )
    assert _published_string_violations(ast.parse('raise ValueError("zzmarker probe")\n')), (
        "detector did not pick up the swapped-in pattern; it is not reading the module attribute"
    )


def test_t15_scan_scope_is_non_empty_and_honours_exclusions() -> None:
    """T15's two enumerations reach real files, skip the right ones, and
    exempt nothing.

    A gate over an empty file list passes for the wrong reason. The
    membership assertions pin the two scope decisions T15 adds over the
    shared enumeration: the ``.py`` arm excludes the test tree (nothing in
    ``tests/`` is published), and the substrate arm includes the manifest —
    its revision-history summaries are published ledger prose, decided in
    scope rather than carved out.
    """
    py_scanned = {str(p.relative_to(REPO_ROOT)) for p in _published_string_py_paths()}

    assert len(py_scanned) > 50, (
        f"T15a would scan only {len(py_scanned)} file(s); enumeration is broken"
    )
    assert "sage/models/schemas.py" in py_scanned, "production code is out of scope"
    # Membership across trees, not just one: the argparse sinks the rule
    # exists to reach live under scripts/, so an enumeration that quietly
    # dropped that tree would pass every other assertion here.
    assert "scripts/reproject_active_documents.py" in py_scanned, "scripts/ is out of scope"
    assert not any(rel.startswith("tests/") for rel in py_scanned), (
        "the test tree is in scope; fixture and simulated-error literals would be swept"
    )
    assert not any(rel.startswith("domains/") for rel in py_scanned)
    assert not any(rel.startswith(".claude/") for rel in py_scanned)

    substrate_scanned = {
        str(p.relative_to(REPO_ROOT))
        for p in _tracked_files_with_suffixes((".yaml", ".yml", ".json"))
    }

    assert "docs/fs/sage/sage_core_api.openapi.yaml" in substrate_scanned, (
        "the OpenAPI spec is out of scope"
    )
    assert "docs/fs/manifest.json" in substrate_scanned, (
        "the manifest is out of scope; its revision history was decided IN scope"
    )
    assert "docs/fs/sage/vault_config.schema.json" in substrate_scanned
    assert not any(rel.startswith((".claude/", "domains/")) for rel in substrate_scanned)

    assert PUBLISHED_TICKET_REF_ALLOWLIST == {}, (
        f"T15's allowlist is no longer empty: {PUBLISHED_TICKET_REF_ALLOWLIST}"
    )
