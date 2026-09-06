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

1. A read of the name ``_VAULTS_ROOT``.
2. A string literal naming the default vault directory -- ``sage_vaults`` as a
   whole segment, or a ``~/sage_vaults`` path. Docstrings are exempt, so prose
   describing the default is free; a literal a caller could build a path from
   is not.

The sibling directories ``sage_vaults_deletions`` and ``sage_vaults_backups``
are deliberately outside the vault root and are not matched: the segment test is
an equality, not a prefix.

Allowlist contract, following ``test_typed_alias_coverage.py``:

- A new unallowlisted site fails the suite (new-drift).
- An allowlist entry naming a site that no longer exists fails the suite
  (phantom-entry), so remediating a site without draining its entry cannot
  leave the allowlist stale.

The gate exists because prose could not hold this invariant. Two sweeps of this
mechanism each closed the sites they touched and left one standing; the third
enumerated the readers of the *constant* and missed four sites that named the
directory as a literal instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNED_TREES = ("sage", "scripts")

# (module path relative to the repo root, enclosing function) -> why it is allowed.
KNOWN_ROOT_NAMERS: dict[tuple[str, str], str] = {
    ("sage/vault_management.py", "default_vault_root"): (
        "The single read of the module-level constant; every other consumer "
        "resolves through this function or through bound_vault_root."
    ),
    ("sage/vault_management.py", "<module>"): (
        "The constant's own definition, which the function above reads."
    ),
    ("sage/__main__.py", "_resolve_vault_root"): (
        "Establishes the binding before it is published; resolving through the "
        "published root would be circular."
    ),
}


def _is_root_naming_literal(value: str) -> bool:
    """True when a string literal names the default vault directory itself.

    A whole-segment match, so the deliberately-separate ``sage_vaults_backups``
    and ``sage_vaults_deletions`` siblings do not match, and a URL path segment
    (``/sage_vaults/{vault_id}``) does not either -- it is neither the bare
    segment nor a home-relative path.
    """
    stripped = value.strip()
    if stripped == "sage_vaults":
        return True
    return stripped == "~/sage_vaults" or stripped.startswith("~/sage_vaults/")


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


def _find_sites() -> set[tuple[str, str]]:
    """Every (module, function) that reads the constant or names the directory."""
    sites: set[tuple[str, str]] = set()
    for tree_name in SCANNED_TREES:
        for path in sorted((REPO_ROOT / tree_name).rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            tree = ast.parse(path.read_text(), filename=rel)
            docstrings = _docstring_nodes(tree)
            owner = _enclosing_functions(tree)
            for node in ast.walk(tree):
                hit = False
                if isinstance(node, ast.Name) and node.id == "_VAULTS_ROOT":
                    hit = True
                elif (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstrings
                    and _is_root_naming_literal(node.value)
                ):
                    hit = True
                if hit:
                    sites.add((rel, owner.get(id(node), "<module>")))
    return sites


def test_no_unallowlisted_vault_root_namer() -> None:
    """New-drift: a site that names the vault root must be allowlisted.

    Anti-coincidental-pass: the gate would pass vacuously if the scan found
    nothing at all, so ``test_gate_scan_reaches_the_known_sites`` pins that the
    sanctioned sites are actually reached. Together they separate "no drift"
    from "no scan".
    """
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

    Without this, remediating a site and leaving its entry behind lets the
    allowlist grow claims that no longer hold, which is how a prose enumeration
    decays into a list nobody can trust.
    """
    stale = sorted(set(KNOWN_ROOT_NAMERS) - _find_sites())
    assert not stale, f"These allowlist entries name no remaining site; drain them: {stale}"


def test_gate_scan_reaches_the_known_sites() -> None:
    """The scan reaches real sites, so a green gate means no drift, not no scan.

    Deleting the walk, pointing it at an empty tree, or narrowing the literal
    test to nothing all leave the two gates above passing. This one reds on any
    of them, and names the two structural sites specifically rather than
    asserting a count, so it also fails if the scan reaches only one of them.
    """
    sites = _find_sites()
    assert ("sage/vault_management.py", "default_vault_root") in sites
    assert ("sage/__main__.py", "_resolve_vault_root") in sites
