"""Read the repository's shared-component version manifest.

``versions.json`` at the repository root is the single declaration of each
version that more than one toolchain has to agree on: the Postgres majors, the
Python version, the Node major. Consumers that can read a file at the moment
they need the value do so directly -- the infrastructure template at compile
time, the workflows through a prelude job's outputs, the test suite through
this module. Consumers that structurally cannot (a Dockerfile ``FROM`` line, a
``pyproject.toml`` key, prose in a runbook) restate the value and are held to
the manifest by ``tests/infra/test_shared_version_parity.py`` instead.

See ``docs/process/shared-version-parity.md`` for which components are held in
parity and which differences are structural and therefore out of scope.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
VERSIONS_MANIFEST: Final[Path] = REPO_ROOT / "versions.json"


@cache
def declared_versions() -> dict[str, Any]:
    """The parsed manifest.

    Cached because every check in the parity gate reads it and the file cannot
    change within a run.
    """
    return json.loads(VERSIONS_MANIFEST.read_text(encoding="utf-8"))


def _declared(*path: str) -> str:
    """Return one manifest value by key path, failing loudly on a gap.

    A missing key is a manifest that no longer declares something a consumer
    depends on; returning a default there would let the parity gate pass
    against nothing.
    """
    node: Any = declared_versions()
    for key in path:
        assert isinstance(node, dict) and key in node, f"versions.json is missing {'.'.join(path)}"
        node = node[key]
    assert isinstance(node, str) and node.strip(), (
        f"versions.json {'.'.join(path)} must be a non-empty string; got {node!r}"
    )
    return node


def postgres_deploy_major() -> str:
    """Major PostgreSQL version of the deployed Flexible Server (CAS-ADR-042)."""
    return _declared("postgres", "deploy_major")


def postgres_dev_major() -> str:
    """Major PostgreSQL version the workstation and the CI service containers run."""
    return _declared("postgres", "dev_major")


def python_version() -> str:
    """The ``major.minor`` Python the project is developed and shipped on."""
    return _declared("python", "version")


def node_major() -> str:
    """Major Node version the frontend toolchain builds and tests on."""
    return _declared("node", "major")


def major_of(spec: str) -> int:
    """Return the integer major from a version spec.

    Accepts a bare pin (``'24'``, ``'24.1'``), a semver-range floor
    (``'^24.13.2'``, ``'~24.0.0'``), a leading-``v`` form, or a comparator
    floor (``'>=3.14'``). The sigils are stripped and the integer before the
    first dot is parsed; a spec with no leading integer raises ``ValueError``
    -- a pin that cannot be resolved to a major must fail loudly rather than
    resolve to a default.
    """
    return int(str(spec).strip().lstrip("^~=<>v ").split(".")[0])


def ruff_target_version(version: str) -> str:
    """Derive Ruff's ``target-version`` token from a ``major.minor`` string.

    ``'3.14'`` becomes ``'py314'``. Raises ``ValueError`` on a value that is not
    two dot-separated integers, so a malformed manifest cannot silently produce
    a plausible-looking token.
    """
    parts = version.strip().split(".")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError(f"expected a major.minor Python version; got {version!r}")
    return f"py{parts[0]}{parts[1]}"


def parse_ruff_target(token: str) -> tuple[int, int]:
    """Parse a Ruff ``target-version`` token into ``(major, minor)``.

    ``'py314'`` becomes ``(3, 14)``. Ruff writes the major as a single leading
    digit and the minor as the remainder, so the two cannot be split on a
    separator; parsing them as one integer compares ``312`` against ``3`` and is
    the mistake this exists to prevent. Raises ``ValueError`` on any other shape.
    """
    if not token.startswith("py") or not token[2:].isdigit() or len(token) < 4:
        raise ValueError(f"expected a Ruff target token like 'py314'; got {token!r}")
    digits = token[2:]
    return int(digits[0]), int(digits[1:])
