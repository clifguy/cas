"""Dependabot version-update coverage guards.

``.github/dependabot.yml`` is the source of truth for which package
ecosystems get automated version-update PRs. Two posture invariants matter
enough to guard against silent regression:

  1. The npm ecosystem (the frontend dependency tree under ``app/``) has
     coverage with a non-zero pull-request limit, so frontend dependencies
     get ongoing version updates and CVE-fixed releases surface as PRs
     rather than accumulating unnoticed.

  2. The uv ecosystem stays *disabled* (``open-pull-requests-limit: 0``).
     The uv ecosystem rewrites ``pyproject.toml`` floors/pins rather than
     doing lockfile-only updates (dependabot-core#12162), so its
     version-update PRs are deliberately off; uv currency is a manual
     ``uv lock --upgrade`` (see ``CLAUDE.md``). That defect is uv-specific
     and does not apply to npm, which is why npm coverage is safe to enable.

These guards mirror ``tests/sage/test_dependency_pins.py``, which reads
``.github/workflows/ci.yml`` to assert a CI-install posture.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import yaml

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DEPENDABOT_PATH: Final[Path] = REPO_ROOT / ".github" / "dependabot.yml"


def _load_updates() -> list[dict]:
    """Parse ``dependabot.yml`` and return its ``updates`` list."""
    config = yaml.safe_load(DEPENDABOT_PATH.read_text(encoding="utf-8"))
    assert isinstance(config, dict), "dependabot.yml did not parse to a mapping."
    updates = config.get("updates")
    assert isinstance(updates, list), "dependabot.yml has no `updates` list."
    return updates


def _blocks_for(ecosystem: str) -> list[dict]:
    return [b for b in _load_updates() if b.get("package-ecosystem") == ecosystem]


def test_dependabot_yml_present_and_parseable() -> None:
    """The Dependabot config exists, declares version 2, and lists updates.

    Structural guard: a missing or malformed config silently disables all
    Dependabot coverage rather than failing loudly.
    """
    assert DEPENDABOT_PATH.is_file(), f"dependabot.yml not found at {DEPENDABOT_PATH}."
    config = yaml.safe_load(DEPENDABOT_PATH.read_text(encoding="utf-8"))
    assert config.get("version") == 2, "dependabot.yml must declare `version: 2`."
    assert _load_updates(), "dependabot.yml `updates` list is empty."


def test_dependabot_covers_npm_for_app() -> None:
    """The npm ecosystem is covered for the ``app/`` manifest with a live limit.

    Coverage guard: asserts not merely that an npm block exists, but that it
    points at the ``app/`` manifest directory AND carries a non-zero
    pull-request limit -- the two ways an npm block could be present yet
    deliver no coverage (pointed at the wrong directory, or zeroed out the
    way the uv block is).
    """
    npm_blocks = _blocks_for("npm")
    assert len(npm_blocks) == 1, (
        f"expected exactly one npm package-ecosystem block, found {len(npm_blocks)}."
    )
    block = npm_blocks[0]

    directory = str(block.get("directory", "")).strip("/")
    assert directory == "app", (
        f"npm block `directory` must point at the app manifest dir (`/app`), "
        f"got {block.get('directory')!r}."
    )

    limit = block.get("open-pull-requests-limit")
    assert isinstance(limit, int) and limit > 0, (
        f"npm block `open-pull-requests-limit` must be a non-zero int to enable "
        f"version updates, got {limit!r}."
    )


def test_uv_ecosystem_stays_disabled() -> None:
    """The uv ecosystem keeps ``open-pull-requests-limit: 0``.

    Scope fence: enabling npm coverage must not accidentally re-enable uv
    version-update PRs, which hit the floor-rewrite defect (see module
    docstring). Guards the uv block's disabled state explicitly.
    """
    uv_blocks = _blocks_for("uv")
    assert len(uv_blocks) == 1, (
        f"expected exactly one uv package-ecosystem block, found {len(uv_blocks)}."
    )
    limit = uv_blocks[0].get("open-pull-requests-limit")
    assert limit == 0, (
        f"uv version-update PRs must stay disabled (limit 0); got {limit!r}. "
        "uv currency is a manual `uv lock --upgrade` (see CLAUDE.md)."
    )
