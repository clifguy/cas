"""Collect the external image references a Dockerfile resolves at build time.

Shared by the two Dockerfile structural gates. A reference that names a mutable
tag lets two builds of the same commit resolve different bytes, which breaks the
one property the deploy path depends on: that the image CI smoked and the image
the deploy pushes are the same artifact.

The scan deliberately covers three sites, not one. ``FROM`` alone would miss
every reference this repository actually floats -- both Dockerfiles indirect
their bases through ``ARG *_IMAGE`` defaults, and both pull the uv binary from a
registry via ``COPY --from=``.
"""

from __future__ import annotations

import re
from typing import Final

_ARG_IMAGE_RE: Final[re.Pattern[str]] = re.compile(
    r"^ARG\s+([A-Za-z_][A-Za-z0-9_]*IMAGE)=(\S+)", re.MULTILINE
)
_COPY_FROM_RE: Final[re.Pattern[str]] = re.compile(r"^COPY\s+--from=(\S+)", re.MULTILINE)
_FROM_RE: Final[re.Pattern[str]] = re.compile(r"^FROM\s+(\S+)", re.MULTILINE)


def _names_a_registry_image(value: str) -> bool:
    """True for a registry coordinate, false for a build-arg or a stage name.

    ``COPY --from=builder`` names an earlier stage and resolves to nothing
    external; ``FROM ${PYTHON_IMAGE}`` defers to the ARG the scan already reads.
    A registry coordinate always carries a tag or a registry host, so it always
    carries a ``:`` or a ``/``.
    """
    if value.startswith("$"):
        return False
    return ":" in value or "/" in value


def external_image_refs(dockerfile_text: str) -> dict[str, str]:
    """Map a human-readable source site to the image reference it resolves."""
    refs: dict[str, str] = {}
    for name, value in _ARG_IMAGE_RE.findall(dockerfile_text):
        refs[f"ARG {name}"] = value
    for value in _COPY_FROM_RE.findall(dockerfile_text):
        if _names_a_registry_image(value):
            refs[f"COPY --from={value}"] = value
    for value in _FROM_RE.findall(dockerfile_text):
        if _names_a_registry_image(value):
            refs[f"FROM {value}"] = value
    return refs


def unpinned(refs: dict[str, str]) -> dict[str, str]:
    """The subset of ``refs`` that carries no immutable digest."""
    return {site: ref for site, ref in refs.items() if "@sha256:" not in ref}


def digest_without_readable_tag(refs: dict[str, str]) -> dict[str, str]:
    """Digest-pinned refs whose readable tag was dropped.

    ``node:24-slim@sha256:...`` and ``node@sha256:...`` pull identical bytes, but
    only the first leaves the major version legible to a reader -- and to the
    frontend Node-version gate, which reads the tag out of the ARG default.
    """
    offenders: dict[str, str] = {}
    for site, ref in refs.items():
        if "@sha256:" not in ref:
            continue
        name = ref.split("@", 1)[0]
        if ":" not in name.rsplit("/", 1)[-1]:
            offenders[site] = ref
    return offenders
