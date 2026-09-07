"""Collect the external image references a Dockerfile resolves at build time.

Shared by the two Dockerfile structural gates. A reference that names a mutable
tag lets two builds of the same commit resolve different bases, which breaks the
property the deploy path leans on: that the image CI smoked and the image the
deploy pushes were built from the same bases and the same locked dependency
sets. That is weaker than byte-equality and is deliberately so -- the apt and
model-weight layers float regardless, and are shared only while the layer cache
serves them.

The scan deliberately covers three sites, not one, and keeps doing so even
though both Dockerfiles now declare every base on a ``FROM`` line. They declare
them there because Dependabot's docker ecosystem reads FROM lines and nothing
else, so a pin parked on an ``ARG *_IMAGE`` default or a ``COPY --from=`` keeps
its digest while silently ageing out of upstream security fixes.

That is exactly why those two arms stay. They are what lets the coverage gate
*report* a regression instead of missing it: a base moved back to either shape is
still collected here, so it still reaches the pinning checks below and still
fails ``test_dependabot_base_image_coverage``. A ``FROM``-only scan would drop
such a reference on the floor and leave every gate reporting clean on a file that
had quietly lost the property.
"""

from __future__ import annotations

import re
from typing import Final

# Dockerfile instruction keywords are case-insensitive, and Dependabot's own
# parser reads them that way (`FROM = /FROM/i`). A case-sensitive scan drops a
# lowercase `from` pin entirely: neither the pinning check nor the visibility
# check ever sees it, and the per-file reference counts notice only a *converted*
# known site, never an *added* one in an unrecognized case. `_ARG_IMAGE_RE` folds
# its keyword for the same reason but keeps its captured name case-sensitive,
# because build-arg names -- unlike instruction keywords -- are.
_ARG_IMAGE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?i:ARG)\s+([A-Za-z_][A-Za-z0-9_]*IMAGE)=(\S+)", re.MULTILINE
)
_COPY_FROM_RE: Final[re.Pattern[str]] = re.compile(
    r"^COPY\s+--from=(\S+)", re.MULTILINE | re.IGNORECASE
)
_FROM_RE: Final[re.Pattern[str]] = re.compile(r"^FROM\s+(\S+)", re.MULTILINE | re.IGNORECASE)
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


def image_names(refs: dict[str, str]) -> set[str]:
    """The registry coordinates in ``refs``, with tag and digest stripped.

    Lets a gate name the images a file is expected to resolve without restating
    their digests, which move on every Dependabot bump and would otherwise red
    the gate on a routine update.
    """
    names: set[str] = set()
    for ref in refs.values():
        name = ref.split("@", 1)[0]
        # Only a colon in the final path segment is a tag. An earlier one is a
        # registry port (``localhost:5000/foo``) and has to survive the strip.
        prefix, _, last = name.rpartition("/")
        last = last.split(":", 1)[0]
        names.add(f"{prefix}/{last}" if prefix else last)
    return names


def unpinned(refs: dict[str, str]) -> dict[str, str]:
    """The subset of ``refs`` that carries no immutable digest."""
    return {site: ref for site, ref in refs.items() if "@sha256:" not in ref}


def digest_without_readable_tag(refs: dict[str, str]) -> dict[str, str]:
    """Digest-pinned refs whose readable tag was dropped.

    ``node:24-slim@sha256:...`` and ``node@sha256:...`` pull identical bytes, but
    only the first leaves the major version legible to a reader -- and to the
    frontend Node-version gate, which reads the tag off the SPA-builder's
    ``FROM`` line.
    """
    offenders: dict[str, str] = {}
    for site, ref in refs.items():
        if "@sha256:" not in ref:
            continue
        name = ref.split("@", 1)[0]
        if ":" not in name.rsplit("/", 1)[-1]:
            offenders[site] = ref
    return offenders
