"""Section links in the operator runbooks resolve to a heading.

The runbooks under ``docs/process/`` link to one another's sections by heading
anchor. Renaming a heading changes its anchor, and the links to it then land at
the top of the page with nothing to say so. This holds every such link to a
heading that exists, under the anchor GitHub renders for it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
PROCESS_DIR: Final[Path] = REPO_ROOT / "docs" / "process"

#: An inline Markdown link whose target carries a fragment: ``[text](path#frag)``
#: or ``[text](#frag)``. External URLs are excluded below.
_LINK_RE: Final[re.Pattern[str]] = re.compile(r"\]\(([^)\s]*)#([^)\s]+)\)")
_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE: Final[re.Pattern[str]] = re.compile(r"^\s*(```|~~~)")


def github_slug(heading: str) -> str:
    """The anchor GitHub renders for a heading's text: inline code marks and
    link syntax dropped to their text, lower-cased, every character but a
    letter, digit, space, hyphen or underscore removed, and each space made a
    hyphen. Runs are not collapsed, so a removed character between two spaces
    leaves a double hyphen."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = text.replace("`", "").strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    """Every heading anchor a Markdown file renders, with GitHub's ``-1``,
    ``-2`` suffixes on repeats. Headings inside fenced code are not headings."""
    seen: dict[str, int] = {}
    out: set[str] = set()
    fenced = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if _FENCE_RE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = _HEADING_RE.match(line)
        if not match:
            continue
        slug = github_slug(match.group(2))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        out.add(slug if count == 0 else f"{slug}-{count}")
    return out


def _links() -> list[tuple[Path, Path, str]]:
    """``(source, target, fragment)`` for each repository-local section link
    in a runbook."""
    found: list[tuple[Path, Path, str]] = []
    for source in sorted(PROCESS_DIR.glob("*.md")):
        fenced = False
        for line in source.read_text(encoding="utf-8").splitlines():
            if _FENCE_RE.match(line):
                fenced = not fenced
                continue
            if fenced:
                continue
            for target, fragment in _LINK_RE.findall(line):
                if re.match(r"^[a-z][a-z0-9+.-]*:", target):
                    continue  # an external URL
                path = (source.parent / target).resolve() if target else source
                found.append((source, path, fragment))
    return found


@pytest.mark.parametrize(
    ("heading", "slug"),
    [
        ("Renewing the wildcard certificate", "renewing-the-wildcard-certificate"),
        (
            "Email the certificate's owner before expiry",
            "email-the-certificates-owner-before-expiry",
        ),
        (
            "4. Public MCP client registration (auth code + PKCE, no secret)",
            "4-public-mcp-client-registration-auth-code--pkce-no-secret",
        ),
        ("Per-tenant setup (one-time)", "per-tenant-setup-one-time"),
        ("The `sage_maint` mount", "the-sage_maint-mount"),
    ],
)
def test_github_slug(heading: str, slug: str) -> None:
    assert github_slug(heading) == slug


def test_repeated_headings_take_numbered_anchors(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("# Verify\n\n```\n# Verify\n```\n\n## Verify\n\n### Verify\n", encoding="utf-8")
    assert anchors(doc) == {"verify", "verify-1", "verify-2"}


def test_runbook_section_links_resolve() -> None:
    links = _links()
    # The scan is only evidence while it finds links to check.
    assert len(links) >= 5, links
    broken = [
        f"{source.relative_to(REPO_ROOT)} -> {target.name}#{fragment}"
        for source, target, fragment in links
        if not target.is_file() or fragment not in anchors(target)
    ]
    assert not broken, "section links that resolve to no heading:\n" + "\n".join(broken)
