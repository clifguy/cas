"""Structural gate for the user-assigned managed-identity module.

Locks the shape of ``infra/modules/identity.bicep`` — the module that
provisions the two user-assigned managed identities (one for SAGE, one for the
CAS BFF) the cloud deployment profile (CAS-ADR-042) consumes. Those identities
are created once and shared: the Key Vault module grants them data-plane read,
the relational-store module grants the SAGE identity a database role, and the
container apps attach them at deploy time. Keeping both identities present and
their ids exposed as outputs is what lets every downstream module compose
against a stable principal.

These checks read the tracked Bicep text only — they need no Azure or Bicep
tooling, so they run in the ordinary Python test job. The authoritative compile
+ lint of the module is the infra workflow's ``validate`` job (``az bicep
build`` under the error-level ``bicepconfig.json`` rules); a local fast-path
compile is provided here, skipped when neither CLI is present.

Detector logic lives in small pure helpers so the control tests can prove each
detector actually fires — a text-assertion gate is only meaningful if its
matchers fail on the regressions they target.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
INFRA_DIR: Final[Path] = REPO_ROOT / "infra"
MAIN_BICEP: Final[Path] = INFRA_DIR / "main.bicep"
IDENTITY: Final[Path] = INFRA_DIR / "modules" / "identity.bicep"

# The resource type each application identity is declared as.
_UAMI_TYPE: Final[str] = "Microsoft.ManagedIdentity/userAssignedIdentities"

# A principal / client / subscription / tenant id is a GUID; a runtime value
# like a principalId must reach an output as an expression, never a literal.
_GUID_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b"
)


# ---------------------------------------------------------------------------
# Detectors (pure text functions — exercised by the control tests below)
# ---------------------------------------------------------------------------


def _strip_line_comments(text: str) -> str:
    """Return ``text`` with ``//`` line comments removed."""
    return "\n".join(re.sub(r"//.*$", "", line) for line in text.splitlines())


def _count_resource_type(text: str, resource_type: str) -> int:
    """Number of ``resource <symbol> '<type>@<version>'`` declarations of a type."""
    pattern = re.compile(r"resource\s+\w+\s+'" + re.escape(resource_type) + r"@[0-9A-Za-z-]+'")
    return len(pattern.findall(_strip_line_comments(text)))


def _output_lines(text: str) -> list[tuple[str, str]]:
    """Return ``(name, rhs)`` for every ``output <name> <type> = <rhs>`` line."""
    pattern = re.compile(r"^\s*output\s+(\w+)\s+\w+\s*=\s*(.+?)\s*$", re.MULTILINE)
    return [(m.group(1), m.group(2)) for m in pattern.finditer(_strip_line_comments(text))]


# ---------------------------------------------------------------------------
# Structural gates
# ---------------------------------------------------------------------------


def test_identity_module_exists() -> None:
    """The identity module the orchestrator wires must exist."""
    assert IDENTITY.is_file(), "infra/modules/identity.bicep missing"


def test_identity_declares_two_user_assigned_identities() -> None:
    """Exactly two user-assigned identities are declared — the SAGE one and the
    CAS BFF one. Neither may be silently dropped; downstream modules grant and
    attach both.
    """
    count = _count_resource_type(IDENTITY.read_text(encoding="utf-8"), _UAMI_TYPE)
    assert count == 2, (
        f"identity.bicep must declare exactly two {_UAMI_TYPE} resources; found {count}"
    )


def test_identity_exposes_principal_and_id_outputs() -> None:
    """Each identity exposes its principal id (for role grants), resource id (for
    container-app attachment), and client id (for runtime token acquisition).
    """
    names = [n.lower() for n, _ in _output_lines(IDENTITY.read_text(encoding="utf-8"))]
    principal = [n for n in names if "principalid" in n]
    client = [n for n in names if "clientid" in n]
    resource_id = [n for n in names if n.endswith("identityid")]
    assert len(principal) >= 2, f"expected >=2 principalId outputs (SAGE + BFF); have {names}"
    assert len(client) >= 2, f"expected >=2 clientId outputs (SAGE + BFF); have {names}"
    assert len(resource_id) >= 2, f"expected >=2 identity resource-id outputs; have {names}"


def test_identity_parameterizes_location() -> None:
    """Location is a parameter and the identities deploy into it — mirrors the
    ``no-hardcoded-location`` bicep rule.
    """
    text = IDENTITY.read_text(encoding="utf-8")
    assert re.search(r"param\s+location\s+string", text), (
        "identity.bicep must take a `location` string parameter"
    )
    assert re.search(r"location:\s*location", text), (
        "each identity must deploy into the `location` parameter"
    )


def test_identity_is_resource_group_scoped() -> None:
    """The module is resource-group scoped (the Bicep default); the orchestrator
    deploys it with ``scope: rg``.
    """
    text = _strip_line_comments(IDENTITY.read_text(encoding="utf-8"))
    assert not re.search(r"targetScope\s*=\s*'(subscription|managementGroup|tenant)'", text), (
        "identity.bicep is a resource-group module; it must not retarget the scope"
    )


def test_identity_outputs_contain_no_literal_guid() -> None:
    """No output bakes in a literal identity GUID — principal/client ids reach the
    output as runtime expressions (``<identity>.properties.principalId``).
    """
    violations = [
        (name, rhs)
        for name, rhs in _output_lines(IDENTITY.read_text(encoding="utf-8"))
        if _GUID_RE.search(rhs)
    ]
    assert not violations, f"outputs must not contain a literal GUID: {violations}"


def test_main_bicep_wires_identity_module() -> None:
    """The orchestrator wires the identity module live and scopes it to the
    resource group.
    """
    text = _strip_line_comments(MAIN_BICEP.read_text(encoding="utf-8"))
    assert re.search(r"module\s+\w+\s+'modules/identity\.bicep'\s*=", text), (
        "main.bicep must wire a live module from modules/identity.bicep"
    )
    assert re.search(r"scope:\s*rg", text), "the identity module must be scoped to rg"


@pytest.mark.skipif(
    shutil.which("bicep") is None and shutil.which("az") is None,
    reason="bicep/az CLI absent; the infra workflow validate job is authoritative",
)
def test_identity_module_compiles(tmp_path: Path) -> None:
    """The identity module compiles to ARM JSON with no error (local fast check;
    the infra workflow validate job is the authoritative gate).
    """
    outfile = tmp_path / "identity.json"
    if shutil.which("bicep") is not None:
        cmd = ["bicep", "build", str(IDENTITY), "--outfile", str(outfile)]
    else:
        cmd = ["az", "bicep", "build", "--file", str(IDENTITY), "--outfile", str(outfile)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"bicep build failed:\n{proc.stderr}"


# ---------------------------------------------------------------------------
# Anti-coincidental-pass controls
#
# These verify the detectors above actually fire on the regressions they
# target, NOT that any specific module text is clean. Without them, a broken
# regex would let every structural gate pass coincidentally.
# ---------------------------------------------------------------------------


def test_resource_count_detector_controls() -> None:
    """``_count_resource_type`` counts two declarations, one, and zero (commented)."""
    two = (
        "resource a 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {}\n"
        "resource b 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {}\n"
    )
    one = "resource a 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {}\n"
    none = "// resource a 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {}\n"
    assert _count_resource_type(two, _UAMI_TYPE) == 2
    assert _count_resource_type(one, _UAMI_TYPE) == 1
    assert _count_resource_type(none, _UAMI_TYPE) == 0


def test_output_guid_detector_controls() -> None:
    """``_GUID_RE`` flags a literal GUID, passes a property-reference expression."""
    literal = "11111111-2222-3333-4444-555555555555"
    expr = "sageIdentity.properties.principalId"
    assert _GUID_RE.search(literal)
    assert not _GUID_RE.search(expr)
