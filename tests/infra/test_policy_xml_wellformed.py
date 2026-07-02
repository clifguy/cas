"""Well-formedness gate for the APIM policy XML under ``infra/policies/``.

APIM policy documents are XML (CAS-ADR-042), and one XML rule has no forgiving
reading: a comment body must not contain the literal sequence ``--``, and must
not end in ``-``. A source file that violates this is not caught by Bicep's
``what-if``/validate — those stages treat the policy as an opaque
``loadTextContent`` string — and is not caught by a grep for ``--`` either,
since that string also appears legitimately in comment delimiters
(``<!--``/``-->``) and would need careful boundary handling to avoid false
positives. The only check that is both sound and complete is parsing the file
as XML: this is exactly what a real APIM deployment does when it applies the
policy, so a source file that fails to parse here is guaranteed to fail the
live ``az deployment sub create`` apply too — a defect that no source-level
check short of an actual XML parse can catch ahead of that live apply.

This gate therefore parses every tracked policy file with the stdlib XML
parser rather than pattern-matching its text, so it cannot pass coincidentally
on a matcher that fails to actually cover the XML comment grammar.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
POLICIES_DIR: Final[Path] = REPO_ROOT / "infra" / "policies"


def _policy_files() -> list[Path]:
    return sorted(POLICIES_DIR.glob("*.xml"))


@pytest.mark.parametrize("policy_path", _policy_files(), ids=lambda p: p.name)
def test_policy_xml_is_well_formed(policy_path: Path) -> None:
    """Every tracked policy file parses as well-formed XML.

    A comment containing a literal ``--`` (a stray em-dash-as-double-hyphen in
    prose is the recurring source) parses locally with any tool that treats
    the file as text, but APIM's own XML parser rejects it at deploy time with
    ``An XML comment cannot contain '--'``. Parsing here reproduces that same
    check before the file ever reaches a live deploy.
    """
    try:
        ET.parse(policy_path)  # noqa: S314 -- repo-tracked policy file, not untrusted input
    except ET.ParseError as exc:
        pytest.fail(f"{policy_path.name} is not well-formed XML: {exc}")


def test_policy_files_discovered() -> None:
    """The glob that drives the parametrized gate actually finds files.

    Guards against the gate silently covering zero files if ``POLICIES_DIR``
    is ever renamed or emptied.
    """
    assert _policy_files(), f"no policy XML files found under {POLICIES_DIR}"


def test_well_formedness_detector_controls() -> None:
    """The stdlib XML parser fires on a comment containing a literal ``--``,
    clears on the same comment rewritten with an em-dash, and does not
    false-positive on ordinary ``<!--``/``-->`` delimiters — so this gate
    cannot pass coincidentally on a parser that does not actually enforce the
    comment grammar.
    """
    double_hyphen = "<policies><!-- prose -- more prose --></policies>"
    em_dash = "<policies><!-- prose — more prose --></policies>"
    with pytest.raises(ET.ParseError):
        ET.fromstring(double_hyphen)  # noqa: S314 -- inline literal, not untrusted input
    ET.fromstring(em_dash)  # noqa: S314 -- must not raise; inline literal, not untrusted input
