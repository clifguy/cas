"""Conformance gate: the vault root is resolved, never named.

Every consumer that needs a vault root asks ``bound_vault_root()`` (the root
this process is bound to) or ``default_vault_root()`` (the default one), so the
root has a single redirect point and a redirected process cannot be served a
path it is not bound to (CAS-ADR-043). Two sites are exempt, and both are
structural rather than historical: ``default_vault_root`` is where the
module-level constant is read, and ``_resolve_vault_root`` is where the default
is established before it is published, which cannot resolve through the
published root without circularity.

Two shapes are detected, because the mechanism has appeared as both:

1. **A read of the constant.** Three spellings, all counted: the bare name
   ``_VAULTS_ROOT``, an attribute access ``<module>._VAULTS_ROOT``, and a
   ``from ... import _VAULTS_ROOT`` (which also catches the aliased form, at the
   import line). The attribute spelling matters most: it is what every test in
   this repo uses to reach the constant, so it is what a maintainer copying from
   a test would write.
2. **A literal naming the directory.** A string carrying ``sage_vaults`` as a
   whole path segment. Docstrings are exempt and so is any literal containing
   whitespace, so prose describing the default is free; a literal a caller could
   build a path from is not. Absolute literals are exempt too, which is what
   keeps the ``/sage_vaults/{vault_id}`` URL segments out. The sibling
   directories ``sage_vaults_deletions`` and ``sage_vaults_backups`` are
   deliberately outside the vault root and do not match, because the segment
   test is an equality on a whole part rather than a substring.

Allowlist contract, following ``test_typed_alias_coverage.py``:

- A new unallowlisted site fails the suite (new-drift).
- An allowlist entry naming a site that no longer exists fails the suite
  (phantom-entry), so remediating a site without draining its entry cannot leave
  the allowlist stale.

Entries are keyed by ``(module, function, shape)`` rather than by
``(module, function)``. The extra element is what makes the phantom check a
control on the *detector*: each shape reaches at least one allowlisted site
alone, so narrowing any branch of the scan orphans an entry and reds the gate.
Keyed only by module and function, the two shapes that meet at the constant's
definition line masked each other, and the literal branch could be deleted with
the gate still green.

Known residual granularity: a key admits a *second* offender in the same
function -- a new module-level literal in ``vault_management`` would pass under
the existing ``<module>`` entry -- and methods are named without their class, so
same-named methods in two classes share a key. Both are acceptable at this
allowlist's size and are the reason the entries carry a stated rationale rather
than a bare pin.

The gate exists because prose could not hold this invariant. Two sweeps of this
mechanism each closed the sites they touched and left one standing; the third
enumerated the readers of the *constant* and missed four sites that named the
directory as a literal instead.
"""

from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNED_TREES = ("sage", "scripts")

CONSTANT = "_VAULTS_ROOT"
DIRECTORY_SEGMENT = "sage_vaults"

# (module path relative to the repo root, enclosing function, shape) -> why allowed.
KNOWN_ROOT_NAMERS: dict[tuple[str, str, str], str] = {
    ("sage/vault_management.py", "default_vault_root", "constant"): (
        "The single read of the module-level constant; every other consumer "
        "resolves through this function or through bound_vault_root."
    ),
    ("sage/vault_management.py", "<module>", "constant"): ("The constant's own assignment target."),
    ("sage/vault_management.py", "<module>", "literal"): (
        "The default the constant is defined as; the one place the directory is spelled out."
    ),
    ("sage/__main__.py", "_resolve_vault_root", "literal"): (
        "Establishes the binding before it is published; resolving through the "
        "published root would be circular."
    ),
}


def _is_root_naming_literal(value: str) -> bool:
    """True when a string literal names the default vault directory itself.

    Three exclusions, each carrying its own case:

    - **Whitespace** -- prose mentioning the default (argparse help, an error
      message) is describing the resolution, not performing it.
    - **Absolute paths** -- the ``/sage_vaults/{vault_id}`` URL prefixes are
      route templates, not filesystem paths.
    - **Whole-segment matching** -- ``sage_vaults_backups`` and
      ``sage_vaults_deletions`` are deliberately separate directories.

    Matching on a path *part* rather than a prefix is what reaches the
    multi-segment relative forms (``"sage_vaults/cas"``, and the constant part
    ``"sage_vaults/"`` of an f-string) that a prefix test misses.
    """
    stripped = value.strip()
    if not stripped or any(ch.isspace() for ch in stripped):
        return False
    if stripped.startswith("/"):
        return False
    return DIRECTORY_SEGMENT in PurePosixPath(stripped).parts


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Ids of the Constant nodes that serve as docstrings.

    Prose describing the default root is not a site: the gate is about literals
    a caller could build a path from.
    """
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                if isinstance(first.value.value, str):
                    out.add(id(first.value))
    return out


def _enclosing_functions(tree: ast.AST) -> dict[int, str]:
    """Map each node id to the name of the function enclosing it, or ``<module>``."""
    owner: dict[int, str] = {}

    def walk(node: ast.AST, current: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = current
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            owner[id(child)] = name
            walk(child, name)

    owner[id(tree)] = "<module>"
    walk(tree, "<module>")
    return owner


def _shape_of(node: ast.AST, docstrings: set[int]) -> str | None:
    """Which detected shape this node is, or None when it is not a site."""
    if isinstance(node, ast.Name) and node.id == CONSTANT:
        return "constant"
    if isinstance(node, ast.Attribute) and node.attr == CONSTANT:
        return "constant"
    if isinstance(node, ast.ImportFrom):
        if any(alias.name == CONSTANT for alias in node.names):
            return "constant"
        return None
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and _is_root_naming_literal(node.value)
    ):
        return "literal"
    return None


def _find_sites() -> set[tuple[str, str, str]]:
    """Every (module, function, shape) that reads the constant or names the directory."""
    sites: set[tuple[str, str, str]] = set()
    for tree_name in SCANNED_TREES:
        for path in sorted((REPO_ROOT / tree_name).rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            tree = ast.parse(path.read_text(), filename=rel)
            docstrings = _docstring_nodes(tree)
            owner = _enclosing_functions(tree)
            for node in ast.walk(tree):
                shape = _shape_of(node, docstrings)
                if shape is not None:
                    sites.add((rel, owner.get(id(node), "<module>"), shape))
    return sites


def test_no_unallowlisted_vault_root_namer() -> None:
    """New-drift: a site that names the vault root must be allowlisted."""
    unexpected = sorted(_find_sites() - set(KNOWN_ROOT_NAMERS))
    assert not unexpected, (
        "These sites name the vault root directly instead of resolving it "
        "through bound_vault_root() / default_vault_root(); a redirected "
        "process would be served a path it is not bound to (CAS-ADR-043). "
        "Resolve them, or add an allowlist entry stating why the site is "
        f"structural: {unexpected}"
    )


def test_no_stale_allowlist_entry() -> None:
    """Phantom-entry: an allowlist entry must name a site that still exists.

    Doubles as the detector's own control, which is why entries carry a shape.
    Each shape reaches an allowlisted site that no other shape reaches, so
    narrowing any branch of the scan orphans an entry and reds this test naming
    it. Verified by mutation on every branch: dropping the ``ast.Name`` check,
    the ``ast.Attribute`` check, or the literal test each red this.
    """
    stale = sorted(set(KNOWN_ROOT_NAMERS) - _find_sites())
    assert not stale, f"These allowlist entries name no remaining site; drain them: {stale}"


def test_gate_scan_reaches_the_known_sites() -> None:
    """The scan reaches real sites, so a green gate means no drift, not no scan.

    Deleting the walk or pointing it at an empty tree leaves
    ``test_no_unallowlisted_vault_root_namer`` passing; this reds. It names the
    structural sites specifically rather than asserting a count, so it also
    fails if the scan reaches only some of them. The phantom-entry test above
    reds on those mutations too -- the two overlap deliberately, since a gate
    whose only liveness check is one test has the same single-observable
    weakness it exists to catch.
    """
    sites = _find_sites()
    assert ("sage/vault_management.py", "default_vault_root", "constant") in sites
    assert ("sage/__main__.py", "_resolve_vault_root", "literal") in sites


def test_detector_reaches_every_spelling_it_claims() -> None:
    """The detector matches each spelling the module docstring promises.

    A direct control on the classifier, because the repo happens to contain only
    some of these spellings: a branch with no live site is invisible to the
    site-based tests above, and would be deleted with the gate still green. Each
    negative case is a form the gate must NOT claim -- a false positive here
    would make the gate unusable and get it disabled, which is the other way a
    gate dies.
    """
    matched = [
        "sage_vaults",
        "~/sage_vaults",
        "~/sage_vaults/cas",
        "sage_vaults/cas",
        "sage_vaults/",
    ]
    for value in matched:
        assert _is_root_naming_literal(value), f"should match: {value!r}"

    not_matched = [
        "sage_vaults_backups",
        "sage_vaults_deletions",
        "/sage_vaults/{vault_id}",
        "Defaults to $SAGE_VAULT_ROOT, then ~/sage_vaults.",
        "else ~/sage_vaults/).",
        "",
    ]
    for value in not_matched:
        assert not _is_root_naming_literal(value), f"should not match: {value!r}"

    def shapes(src: str) -> set[str | None]:
        tree = ast.parse(src)
        docstrings = _docstring_nodes(tree)
        return {_shape_of(node, docstrings) for node in ast.walk(tree)}

    assert "constant" in shapes("x = _VAULTS_ROOT / vault_id")
    assert "constant" in shapes("x = vault_management._VAULTS_ROOT / vault_id")
    assert "constant" in shapes("from sage.vault_management import _VAULTS_ROOT as ROOT")
    assert "constant" not in shapes("x = SOMETHING_ELSE / vault_id")
