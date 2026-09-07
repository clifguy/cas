"""Collect the external image references a Dockerfile resolves at build time.

Shared by the two Dockerfile structural gates. A reference that names a mutable
tag lets two builds of the same commit resolve different bases, which breaks the
property the deploy path leans on: that the image CI smoked and the image the
deploy pushes were built from the same bases and the same locked dependency
sets. That is weaker than byte-equality and is deliberately so -- the apt and
model-weight layers float regardless, and are shared only while the layer cache
serves them.

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
_STAGE_RE: Final[re.Pattern[str]] = re.compile(
    r"^FROM\s+\S+\s+AS\s+(\S+)", re.MULTILINE | re.IGNORECASE
)


def _stage_names(dockerfile_text: str) -> set[str]:
    """The named build stages, lowercased -- stage references are case-folded."""
    return {name.lower() for name in _STAGE_RE.findall(dockerfile_text)}


def _is_external(value: str, stage_names: set[str]) -> bool:
    """True for a reference the build resolves from a registry.

    Stated as an exclusion rather than as a recognizer. The recognizer form --
    "a registry coordinate always carries a ``:`` or a ``/``" -- reads as
    equivalent and is not: it silently drops the one shape that floats hardest,
    a bare ``FROM debian`` or ``COPY --from=alpine``, which carries neither and
    resolves to ``:latest``. Excluding the three things that are demonstrably
    not registry references, and treating everything else as one, cannot lose a
    site that way; it can only over-collect, which fails loudly.
    """
    if value.startswith("$"):  # deferred to an ARG this scan reads directly
        return False
    if value.lower() == "scratch":  # the empty base, not a registry image
        return False
    return value.lower() not in stage_names  # an earlier stage, not external


def external_image_refs(dockerfile_text: str) -> dict[str, str]:
    """Map a human-readable source site to the image reference it resolves."""
    stage_names = _stage_names(dockerfile_text)
    refs: dict[str, str] = {}
    for name, value in _ARG_IMAGE_RE.findall(dockerfile_text):
        refs[f"ARG {name}"] = value
    for value in _COPY_FROM_RE.findall(dockerfile_text):
        if _is_external(value, stage_names):
            refs[f"COPY --from={value}"] = value
    for value in _FROM_RE.findall(dockerfile_text):
        if _is_external(value, stage_names):
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
