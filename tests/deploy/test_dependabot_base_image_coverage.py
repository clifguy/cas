"""Every pinned base image sits where Dependabot's docker ecosystem can read it.

The two root Dockerfiles digest-pin every external image they resolve, and the
``docker`` ecosystem in ``.github/dependabot.yml`` exists to move those pins
rather than let them age. The pinning gates in the sibling container-image
modules assert a digest is *present*; nothing there asserts it is *current*, and
a pin the updater cannot see stops receiving upstream security fixes silently.

That silence is the whole problem. An unreachable pin produces no error, no
stale-dependency alert, and no failing check -- the ecosystem simply reports
nothing to do. This module closes the gap structurally instead: it asserts that
every reference the image scan collects is declared on a line the updater
actually parses, so a future edit that moves a base somewhere the updater cannot
follow fails here rather than going quiet for a release cycle.
"""

import re
from pathlib import Path

import pytest

from tests.deploy._image_refs import external_image_refs

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The number of external references each file resolves. Pinned so an image scan
# that quietly stopped matching cannot satisfy the visibility assertion below by
# returning nothing.
_EXPECTED_REF_COUNTS = {"Dockerfile": 2, "Dockerfile.bff": 3}

# --------------------------------------------------------------------------
# A port of the upstream parser's line matcher, read 2026-09-07 from
# dependabot-core:
#
#   docker/lib/dependabot/docker/file_parser.rb
#       FROM_LINE, and the parse loop: `next unless FROM_LINE.match?(line)`
#       followed by `next unless version`.
#   docker/lib/dependabot/shared/shared_file_parser.rb
#       REGISTRY, IMAGE, TAG, NAME, and `version_from` (tag or digest).
#
# Those two files are the entirety of what the ecosystem reads out of a
# Dockerfile. There is no ARG handling and no build-arg substitution anywhere in
# the parser, which is why a base reached through `FROM ${SOME_ARG}` is invisible
# to it -- the interpolation never resolves, and the line matches nothing.
#
# This is a copy and can drift from upstream. It drifts in the safe direction: if
# the parser widens to read more shapes, this port stays stricter and the gate
# still passes. The controls at the bottom of this module are what keep the copy
# honest about which shapes it currently rejects -- without them a matcher that
# accepted everything would satisfy the visibility test no matter how the
# Dockerfiles were written.
#
# Three sub-patterns are transcription rather than coverage, named here so they
# are not mistaken for controls. Deleting any of them leaves every assertion in
# this module green, which was confirmed by deletion rather than assumed:
#   * `_PLATFORM` -- no control declares `FROM --platform=...`.
#   * `_STAGE` -- the trailing `AS <name>` is optional and the pattern is not
#     anchored at its end, so it can never change a verdict.
#   * `_REGISTRY` -- `_IMAGE` already matches `ghcr.io/astral-sh/uv` on its own,
#     since `ghcr.io` satisfies a name component, so the registry alternative is
#     subsumed for every reference this repository resolves.
# They are kept so the port stays diffable against the upstream regex. `_TAG` and
# `_DIGEST` are the two that are genuinely exercised.
# --------------------------------------------------------------------------

_DOMAIN_COMPONENT = r"(?:[a-zA-Z0-9]|[a-zA-Z0-9][a-zA-Z0-9-]*[a-zA-Z0-9])"
_DOMAIN = rf"(?:{_DOMAIN_COMPONENT}(?:\.{_DOMAIN_COMPONENT})+)"
_REGISTRY = rf"(?P<registry>{_DOMAIN}(?::\d+)?)"
_NAME_COMPONENT = r"(?:[a-z\d]+(?:(?:[._]|__|[-]*)[a-z\d]+)*)"
_IMAGE = rf"(?P<image>{_NAME_COMPONENT}(?:/{_NAME_COMPONENT})*)"
_TAG = r":(?P<tag>[\w][\w.-]{0,127})"
_DIGEST = r"(?P<digest>[0-9a-f]{64})"
_STAGE = r"\s+AS\s+(?P<name>[\w-]+)"
_PLATFORM = r"--platform\=(?P<platform>\S+)"

# Each optional piece is wrapped in its own `(?: ... )?` rather than written as
# `{_TAG}?`. Upstream can use the bare form because Ruby interpolates a Regexp
# into another as a wrapped group, so `#{TAG}?` makes the whole of `:<tag>`
# optional. Python interpolates plain text, so `{_TAG}?` instead binds the `?` to
# the trailing group alone and leaves the `:` mandatory -- which silently narrows
# the port to lines carrying both a tag and an `AS` stage, and reads a legitimate
# `FROM name@sha256:...` or a bare `FROM debian` as something the ecosystem
# cannot see. The controls below pin each of those shapes for that reason.
_FROM_LINE = re.compile(
    rf"^(?i:FROM)\s+(?:{_PLATFORM}\s+)?(?:{_REGISTRY}/)?"
    rf"{_IMAGE}(?:{_TAG})?(?:@sha256:{_DIGEST})?(?:{_STAGE})?",
    re.VERBOSE,
)


def _dependabot_version(line: str) -> str | None:
    """The version the docker ecosystem would take from this line, else ``None``.

    ``None`` covers both ways upstream drops a line: the matcher not matching at
    all, and matching with neither a tag nor a digest to key an update on. A
    dropped line is a reference the ecosystem will never open a pull request for.
    """
    match = _FROM_LINE.match(line)
    if match is None:
        return None
    return match.group("tag") or match.group("digest")


def _declaring_line(text: str, site: str) -> str:
    """The source line the image scan collected ``site`` from.

    Site keys are built from the line's own opening (``FROM <ref>``,
    ``COPY --from=<ref>``, ``ARG <NAME>``), so a prefix match recovers the line
    for every shape the scan recognizes -- including the shapes this gate exists
    to reject, which is what lets it report them rather than skip them.
    """
    for line in text.splitlines():
        if line.startswith(site):
            return line
    raise AssertionError(f"no line in the Dockerfile declares the collected site {site!r}")


@pytest.mark.parametrize("dockerfile_name", sorted(_EXPECTED_REF_COUNTS))
def test_every_external_image_ref_is_dependabot_visible(dockerfile_name: str) -> None:
    """Each pinned base is declared where the docker ecosystem will find it.

    A reference the updater cannot parse keeps its digest and keeps passing the
    pinning gate while quietly ageing out of upstream security fixes. Binding the
    scan's output to the updater's own matcher is what turns that silence into a
    failure.
    """
    text = (_REPO_ROOT / dockerfile_name).read_text()
    refs = external_image_refs(text)

    # Non-vacuity. "Every reference is visible" is trivially true of no
    # references, so a scan that stopped reaching these files would otherwise
    # report clean.
    assert len(refs) == _EXPECTED_REF_COUNTS[dockerfile_name], (
        f"the image-reference scan lost a known site in {dockerfile_name}; expected "
        f"{_EXPECTED_REF_COUNTS[dockerfile_name]} external references, got {sorted(refs)}"
    )

    invisible = {
        site: line
        for site in refs
        if _dependabot_version(line := _declaring_line(text, site)) is None
    }
    assert not invisible, (
        f"these {dockerfile_name} image references are pinned but invisible to the docker\n"
        "Dependabot ecosystem, so nothing will ever bump them -- declare each one on a\n"
        f"`FROM <image>:<tag>@sha256:<digest>` line instead: {invisible}"
    )


@pytest.mark.parametrize(
    ("shape", "line", "expected"),
    [
        (
            "tag + digest + stage (what both Dockerfiles use)",
            f"FROM python:3.14-slim@sha256:{'c' * 64} AS python-base",
            "3.14-slim",
        ),
        (
            "registry-qualified",
            f"FROM ghcr.io/astral-sh/uv:0.12.10@sha256:{'2' * 64} AS uv",
            "0.12.10",
        ),
        (
            "no stage alias",
            f"FROM python:3.14-slim@sha256:{'c' * 64}",
            "3.14-slim",
        ),
        ("tag only", "FROM python:3.14-slim AS python-base", "3.14-slim"),
        ("digest only, no readable tag", f"FROM python@sha256:{'e' * 64}", "e" * 64),
    ],
)
def test_ported_matcher_reads_every_visible_from_shape(
    shape: str, line: str, expected: str
) -> None:
    """Positive controls: each shape upstream can key an update on.

    Only the first two occur in this repository today, and restricting the
    controls to those is what let a real defect sit here undetected: the port
    silently required *both* a tag and an ``AS`` stage, so the three shapes below
    them read as invisible. Every reference in both Dockerfiles happens to carry
    both, so the visibility test above stayed green on a matcher that would have
    reported a legitimate future edit as unreachable.

    The last case also exercises the digest fallback, which nothing else reaches
    -- every other line here carries a tag, and the tag wins first.
    """
    assert _dependabot_version(line) == expected, f"the port cannot read a {shape} reference"


def test_ported_matcher_reports_no_version_for_an_unpinned_image() -> None:
    """A bare ``FROM debian`` is matched but yields nothing to key an update on.

    Upstream drops such a line at ``next unless version``, so this gate correctly
    reports no version. That is not a coverage hole: an unpinned reference is the
    *pinning* gate's business, and ``unpinned`` in the sibling modules is what
    fails it. Pinned here so the two gates' division of labour is asserted rather
    than assumed -- and so a later edit cannot quietly make this gate the one
    that reports floating tags, which it is not equipped to do.
    """
    assert _dependabot_version("FROM debian AS extras") is None


@pytest.mark.parametrize(
    ("shape", "line"),
    [
        ("ARG default", f"ARG PYTHON_IMAGE=python:3.14-slim@sha256:{'c' * 64}"),
        ("build-arg indirection", "FROM ${PYTHON_IMAGE} AS builder"),
        (
            "COPY --from",
            f"COPY --from=ghcr.io/astral-sh/uv:0.12.10@sha256:{'2' * 64} /uv /usr/local/bin/uv",
        ),
    ],
)
def test_ported_matcher_rejects_declarations_the_ecosystem_cannot_read(
    shape: str, line: str
) -> None:
    """Negative control, and the load-bearing test in this module.

    Without it the visibility assertion above proves nothing: a matcher that
    returned a version for any line at all would pass it however the Dockerfiles
    were written, which is precisely the false clean this gate exists to prevent.
    Each shape here carries a real digest and would satisfy the pinning gate,
    while upstream reads none of them.
    """
    assert _dependabot_version(line) is None, (
        f"the ported matcher accepted a {shape} declaration; upstream reads only FROM "
        "lines, so a port that accepts more than that cannot detect an invisible pin"
    )


def test_ported_matcher_skips_stage_aliases() -> None:
    """A stage alias carries no version, so the ecosystem passes over it.

    Both Dockerfiles point several stages at one pinned base through an alias.
    Those lines are matched by the FROM regex but yield no tag and no digest, so
    upstream's ``next unless version`` drops them -- it does not go looking for a
    registry image named ``python-base``.
    """
    assert _dependabot_version("FROM python-base AS builder") is None
