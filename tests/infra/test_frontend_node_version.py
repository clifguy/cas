"""Frontend Node-major consistency gate.

The CAS SPA is built and tested by several tools that each name a Node major
independently: the ``actions/setup-node`` pins in ``.github/workflows/ci.yml``,
the ``node:<major>-slim`` base in ``Dockerfile.bff`` (the SPA-builder stage of
the deployment-profile container image, CAS-ADR-042), and the ``@types/node``
floor in ``app/package.json`` (whose Dependabot semver-major ignore in
``.github/dependabot.yml`` holds the type definitions to the build/test runtime
major). Nothing forces those sites to agree, so they can silently drift apart
-- and a contributor reasoning about "the" Node version can then pick the wrong
one. ``@types/node`` ahead of the lowest build/test runtime is the real hazard:
it lets code reference newer-Node-only APIs that pass ``tsc`` but do not exist
where the bundle is actually built.

This gate reads the tracked config only (no Actions runner, no Docker daemon)
and asserts every one of those sites resolves to a single canonical Node major,
in the structural-gate style of ``tests/infra/test_build_images_reusable.py``
and ``tests/deploy/test_bff_container_image.py``. Each check also proves it
actually located its site (non-empty pin list / matched arg / present key),
so a parser that silently matches nothing cannot pass vacuously.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Final

import yaml

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

CI_WORKFLOW: Final[Path] = REPO_ROOT / ".github" / "workflows" / "ci.yml"
DOCKERFILE_BFF: Final[Path] = REPO_ROOT / "Dockerfile.bff"
APP_PACKAGE_JSON: Final[Path] = REPO_ROOT / "app" / "package.json"
DEPENDABOT: Final[Path] = REPO_ROOT / ".github" / "dependabot.yml"

# The one Node major the whole frontend toolchain resolves to. Chosen as the
# Active-LTS line that already backs the container image, so CI and the type
# definitions rise to meet the container rather than the reverse. Raise this in
# lockstep with the four sites the tests below cover when the toolchain moves.
CANONICAL_NODE_MAJOR: Final[int] = 24


def _node_major(spec: str) -> int:
    """Return the integer Node major from a version spec.

    Accepts a bare CI pin (``'24'``, ``'24.1'``), a semver-range floor
    (``'^24.13.2'``, ``'~24.0.0'``), or a leading-``v`` form. The leading
    range/``v`` sigils are stripped and the integer before the first dot is
    parsed; a spec with no leading integer raises ``ValueError`` (a pin we
    cannot resolve to a major must fail loudly, not resolve to a default).
    """
    return int(str(spec).strip().lstrip("^~=v").split(".")[0])


def _ci_setup_node_versions() -> list[str]:
    """Every ``node-version`` pinned by an ``actions/setup-node`` step in CI."""
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    jobs = (workflow or {}).get("jobs") or {}
    versions: list[str] = []
    for job in jobs.values():
        for step in (job or {}).get("steps") or []:
            uses = str((step or {}).get("uses", ""))
            if uses.startswith("actions/setup-node"):
                pin = ((step or {}).get("with") or {}).get("node-version")
                if pin is not None:
                    versions.append(str(pin))
    return versions


def test_ci_setup_node_pins_resolve_to_canonical_major() -> None:
    """Every ``actions/setup-node`` pin in CI resolves to the canonical major.

    Guards the frontend CI jobs (eslint, vitest, playwright-e2e) against
    drifting off the one major -- individually or as a group. Asserts the pin
    list is non-empty first, so a walk that finds no ``setup-node`` steps fails
    loudly instead of passing over an empty set.
    """
    pins = _ci_setup_node_versions()
    assert pins, f"no actions/setup-node node-version pins found in {CI_WORKFLOW}"
    majors = {_node_major(p) for p in pins}
    assert majors == {CANONICAL_NODE_MAJOR}, (
        f"CI setup-node pins must all be Node {CANONICAL_NODE_MAJOR}; "
        f"got pins {pins} (majors {sorted(majors)})."
    )


def test_dockerfile_bff_node_image_is_canonical_major() -> None:
    """The BFF SPA-builder Node base is the canonical major.

    Regression anchor: this site is expected to already be canonical, so the
    check exists to catch a future edit that moves the container base off the
    major CI and the type definitions are pinned to.
    """
    text = DOCKERFILE_BFF.read_text(encoding="utf-8")
    match = re.search(r"ARG NODE_IMAGE=node:(\d+)", text)
    assert match is not None, f"no `ARG NODE_IMAGE=node:<major>` found in {DOCKERFILE_BFF}"
    assert int(match.group(1)) == CANONICAL_NODE_MAJOR, (
        f"Dockerfile.bff NODE_IMAGE base must be Node {CANONICAL_NODE_MAJOR}; "
        f"got node:{match.group(1)}."
    )


def test_types_node_floor_is_canonical_major() -> None:
    """The ``@types/node`` devDependency floor is the canonical major.

    The type definitions must not outrun the lowest build/test runtime; pinning
    the floor to the canonical major keeps them level with it.
    """
    package = json.loads(APP_PACKAGE_JSON.read_text(encoding="utf-8"))
    dev_deps = package.get("devDependencies") or {}
    assert "@types/node" in dev_deps, (
        f"@types/node not found in devDependencies of {APP_PACKAGE_JSON}"
    )
    floor = dev_deps["@types/node"]
    assert _node_major(floor) == CANONICAL_NODE_MAJOR, (
        f"@types/node floor must be the Node {CANONICAL_NODE_MAJOR} line; got {floor!r}."
    )


def test_dependabot_types_node_comment_names_canonical_major() -> None:
    """The Dependabot ``@types/node`` note names the live major, not a stale one.

    The ignore rule's comment is what a contributor reads to learn which Node
    major the type definitions track; if it names an outdated major it is the
    exact footgun this gate exists to remove. Structural (non-comment) fields of
    the config are guarded separately by ``tests/test_dependabot_config.py``.
    """
    text = DEPENDABOT.read_text(encoding="utf-8")
    assert f"Node {CANONICAL_NODE_MAJOR}" in text, (
        f"dependabot.yml @types/node note must name Node {CANONICAL_NODE_MAJOR}."
    )
    assert "Node 20" not in text, (
        "dependabot.yml still references the EOL'd 'Node 20'; update the "
        f"@types/node note to Node {CANONICAL_NODE_MAJOR}."
    )
