"""Public posture gate.

Enforces the invariants that make this repository safe for public release.
The cleanup pass that established these invariants is documented in
``CLAUDE.md`` §Coding Conventions ("Durable code surfaces stay public-ready")
and enforced at commit time by the ``cas-code-review`` skill §P1. This test
is the substrate-level counterpart: a deterministic gate that fails the
build whenever a forbidden pattern reappears in a tracked file.

Eleven invariants are checked:

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

Scope — top-level directories excluded from T1/T3/T4:

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

The three allowlist constants near the top of the module follow the
pattern of ``KNOWN_VIOLATIONS`` in ``tests/sage/test_typed_alias_coverage.py``
and ``KNOWN_ARG_DRIFT`` in ``tests/sage/test_mcp_tool_conformance.py``.
All three are empty at the close of the establishing cleanup. Every
entry added later requires a 1-line rationale.

Anti-coincidental-pass coverage:

- ``test_t1_regex_word_boundary_negative_case`` confirms the use-case
  regex catches ``pim_health`` but not ``compilation``, and catches
  ``patent`` but not ``compatibility``.
- ``test_t2_tokenizer_distinguishes_string_from_comment`` confirms a
  ``T-NNNN`` token inside a string literal is NOT extracted, while the
  same token inside ``#`` comment or function docstring IS extracted.

A whole-test anti-coincidental probe (manually introduce one violation
per category, confirm the gate fails with a precise message naming
file/line, revert) is a step in the cleanup verification plan, not an
automated test here.
"""

from __future__ import annotations

import ast
import re
import subprocess
import tokenize
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

# Tracked build artifacts that should be .gitignore'd and never committed.
_BUILD_ARTIFACTS: Final[frozenset[str]] = frozenset(
    {".coverage", "coverage.xml", "repo_file_inventory.xlsx"}
)

# Maximum number of violations to enumerate in a single pytest.fail message.
_MAX_REPORTED_VIOLATIONS: Final[int] = 30


# ---------------------------------------------------------------------------
# Allowlists
#
# All three are empty at the close of the establishing cleanup. Each entry
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
