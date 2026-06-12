"""Structural and security-posture gate for the foundation hosting module.

Locks the shape of ``infra/modules/foundation.bicep`` — the first
hosting-environment module in the CAS cloud deployment profile (CAS-ADR-042) —
so the virtual network, the VNet-integrated Azure Container Apps environment,
the Log Analytics workspace wired to it, and the Azure Container Registry stay
present, correctly wired, and free of leaked secrets as the module evolves.

These checks read the tracked Bicep text only — they need no Azure or Bicep
tooling, so they run in the ordinary Python test job. The authoritative
compile + lint of the module is the infra workflow's ``validate`` job
(``az bicep build`` under the error-level ``bicepconfig.json`` rules); a local
fast-path compile is provided here, skipped when neither CLI is present.

Detector logic lives in small pure helpers (``_declares_resource_type``,
``_has_delegation``, ``_output_secret_violations``, ``_strip_line_comments``)
so the control tests can prove each detector actually fires — a text-assertion
gate is only meaningful if its matchers fail on the regressions they target.
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
FOUNDATION: Final[Path] = INFRA_DIR / "modules" / "foundation.bicep"

# Resource provider types the foundation must declare.
_VNET_TYPE: Final[str] = "Microsoft.Network/virtualNetworks"
_ACA_ENV_TYPE: Final[str] = "Microsoft.App/managedEnvironments"
_LOG_ANALYTICS_TYPE: Final[str] = "Microsoft.OperationalInsights/workspaces"
_ACR_TYPE: Final[str] = "Microsoft.ContainerRegistry/registries"

# Subnet delegations the foundation must establish: the ACA infrastructure
# subnet for the managed environment, and the delegated subnet that a managed
# Postgres Flexible Server integrates into for private connectivity.
_ACA_DELEGATION: Final[str] = "Microsoft.App/environments"
_POSTGRES_DELEGATION: Final[str] = "Microsoft.DBforPostgreSQL/flexibleServers"

# A subscription / tenant / client id is a GUID; none may be hardcoded into the
# module — identity coordinates arrive as deployment parameters.
_GUID_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b"
)

# Substrings that betray a secret leaking through a module output. The
# Log Analytics shared key (``listKeys().primarySharedKey``) is the most likely
# accidental leak; ACR admin and Postgres password forms round out the set.
_SECRET_TOKENS: Final[tuple[str, ...]] = (
    "listkeys",
    "sharedkey",
    "primarykey",
    "secretref",
    "administratorloginpassword",
    "adminpassword",
)


# ---------------------------------------------------------------------------
# Detectors (pure text functions — exercised by the control tests below)
# ---------------------------------------------------------------------------


def _strip_line_comments(text: str) -> str:
    """Return ``text`` with ``//`` line comments removed.

    Keeps the structure checks from passing on a commented-out stub (the
    scaffold ships a commented ``module foundation`` example). Naive but
    sufficient: the string literals these gates inspect — resource types,
    delegation service names, output expressions — never contain ``//``.
    """
    return "\n".join(re.sub(r"//.*$", "", line) for line in text.splitlines())


def _declares_resource_type(text: str, resource_type: str) -> bool:
    """True iff ``text`` declares a resource of ``resource_type``.

    Matches the Bicep ``resource <symbol> '<type>@<version>'`` declaration
    form, not a bare mention in a comment or string literal.
    """
    pattern = re.compile(r"resource\s+\w+\s+'" + re.escape(resource_type) + r"@[0-9A-Za-z-]+'")
    return pattern.search(_strip_line_comments(text)) is not None


def _has_delegation(text: str, service_name: str) -> bool:
    """True iff a subnet delegation names ``serviceName: '<service_name>'``."""
    pattern = re.compile(r"serviceName:\s*'" + re.escape(service_name) + r"'")
    return pattern.search(_strip_line_comments(text)) is not None


def _output_lines(text: str) -> list[tuple[str, str]]:
    """Return ``(name, rhs)`` for every ``output <name> <type> = <rhs>`` line."""
    pattern = re.compile(r"^\s*output\s+(\w+)\s+\w+\s*=\s*(.+?)\s*$", re.MULTILINE)
    return [(m.group(1), m.group(2)) for m in pattern.finditer(_strip_line_comments(text))]


def _output_secret_violations(text: str) -> list[tuple[str, str]]:
    """Return ``(output_name, offending_token)`` for outputs that expose a secret."""
    violations: list[tuple[str, str]] = []
    for name, rhs in _output_lines(text):
        lowered = rhs.lower()
        for token in _SECRET_TOKENS:
            if token in lowered:
                violations.append((name, token))
        if _GUID_RE.search(rhs):
            violations.append((name, "guid"))
    return violations


# ---------------------------------------------------------------------------
# Structural / posture gates
# ---------------------------------------------------------------------------


def test_foundation_module_exists() -> None:
    """The foundation module file the orchestrator wires must exist."""
    assert FOUNDATION.is_file(), "infra/modules/foundation.bicep missing"


def test_foundation_declares_all_required_resources() -> None:
    """The four foundation resources are all declared (none silently dropped)."""
    text = FOUNDATION.read_text(encoding="utf-8")
    for resource_type in (_VNET_TYPE, _ACA_ENV_TYPE, _LOG_ANALYTICS_TYPE, _ACR_TYPE):
        assert _declares_resource_type(text, resource_type), (
            f"foundation.bicep must declare a {resource_type} resource"
        )


def test_foundation_aca_environment_is_vnet_integrated() -> None:
    """The ACA environment binds an infrastructure subnet delegated to it —
    the VNet integration that is the prerequisite for private connectivity.
    """
    text = FOUNDATION.read_text(encoding="utf-8")
    assert "infrastructureSubnetId" in _strip_line_comments(text), (
        "the ACA environment must set vnetConfiguration.infrastructureSubnetId"
    )
    assert _has_delegation(text, _ACA_DELEGATION), (
        f"the ACA infrastructure subnet must delegate to {_ACA_DELEGATION}"
    )


def test_foundation_provisions_postgres_delegated_subnet() -> None:
    """The foundation owns the delegated subnet a managed Postgres Flexible
    Server integrates into — a downstream storage module consumes its id.
    """
    text = FOUNDATION.read_text(encoding="utf-8")
    assert _has_delegation(text, _POSTGRES_DELEGATION), (
        f"foundation.bicep must provision a subnet delegated to {_POSTGRES_DELEGATION}"
    )


def test_foundation_logs_wired_to_workspace() -> None:
    """The ACA environment routes application logs to the Log Analytics
    workspace (the workspace is not provisioned then left unwired).
    """
    text = _strip_line_comments(FOUNDATION.read_text(encoding="utf-8"))
    assert "appLogsConfiguration" in text, "the ACA environment must declare appLogsConfiguration"
    assert re.search(r"destination:\s*'log-analytics'", text), (
        "appLogsConfiguration.destination must be 'log-analytics'"
    )
    assert "customerId" in text, "appLogsConfiguration must wire the workspace customerId"


def test_foundation_acr_admin_user_disabled() -> None:
    """The ACR keeps the admin user disabled — auth is via managed identity /
    RBAC, not a stored admin credential (the OIDC, no-stored-secret posture).
    """
    text = _strip_line_comments(FOUNDATION.read_text(encoding="utf-8"))
    assert re.search(r"adminUserEnabled:\s*false", text), "the ACR must set adminUserEnabled: false"
    assert not re.search(r"adminUserEnabled:\s*true", text), (
        "the ACR must not enable the admin user"
    )


def test_foundation_exposes_required_outputs() -> None:
    """Downstream modules compose through the foundation's outputs: ACA env id
    and default domain, ACR login server, and both subnet ids.
    """
    names = [name for name, _ in _output_lines(FOUNDATION.read_text(encoding="utf-8"))]
    lowered = [n.lower() for n in names]
    assert any("environment" in n and "id" in n for n in lowered), (
        f"missing an ACA environment id output; have {names}"
    )
    assert any("domain" in n for n in lowered), (
        f"missing an ACA default-domain output; have {names}"
    )
    assert any("loginserver" in n for n in lowered), (
        f"missing an ACR login-server output; have {names}"
    )
    subnet_outputs = [n for n in lowered if "subnet" in n]
    assert len(subnet_outputs) >= 2, (
        f"expected >=2 subnet-id outputs (ACA + Postgres); have {names}"
    )


def test_foundation_exposes_vnet_id_output() -> None:
    """The foundation exposes the virtual network's resource id — the relational
    store module needs it to link its private DNS zone to the VNet, and a
    consumer should compose through a named output rather than string-splitting a
    subnet id.
    """
    names = [name for name, _ in _output_lines(FOUNDATION.read_text(encoding="utf-8"))]
    lowered = [n.lower() for n in names]
    assert any("vnet" in n and "id" in n for n in lowered), (
        f"missing a VNet id output; have {names}"
    )


def test_foundation_outputs_contain_no_secrets() -> None:
    """No module output exposes a secret (shared key, admin password) or a
    hardcoded identity GUID — a local mirror of the bicep
    ``outputs-should-not-contain-secrets`` rule.
    """
    violations = _output_secret_violations(FOUNDATION.read_text(encoding="utf-8"))
    assert not violations, f"secret-bearing outputs: {violations}"


def test_foundation_parameterizes_location_no_hardcoded_identity() -> None:
    """Location is a parameter (not a hardcoded region) and no identity GUID is
    baked into the module — mirrors ``no-hardcoded-location`` and the scaffold
    identity check.
    """
    text = FOUNDATION.read_text(encoding="utf-8")
    assert re.search(r"param\s+location\s+string", text), (
        "foundation.bicep must take a `location` string parameter"
    )
    assert not _GUID_RE.search(text), "foundation.bicep must not hardcode an identity GUID"


def test_foundation_is_not_subscription_scoped() -> None:
    """The module is resource-group scoped (the Bicep default): the orchestrator
    deploys it with ``scope: rg``, so a subscription/MG/tenant targetScope here
    would break the wiring.
    """
    text = _strip_line_comments(FOUNDATION.read_text(encoding="utf-8"))
    assert not re.search(r"targetScope\s*=\s*'subscription'", text), (
        "foundation.bicep is a resource-group module; it must not target the subscription"
    )
    assert not re.search(r"targetScope\s*=\s*'(managementGroup|tenant)'", text), (
        "foundation.bicep must not target the management-group or tenant scope"
    )


def test_main_bicep_wires_foundation_module() -> None:
    """The orchestrator wires the foundation module live (not the commented
    stub) and scopes it to the resource group.
    """
    text = _strip_line_comments(MAIN_BICEP.read_text(encoding="utf-8"))
    assert re.search(r"module\s+\w+\s+'modules/foundation\.bicep'\s*=", text), (
        "main.bicep must wire a live module from modules/foundation.bicep"
    )
    assert re.search(r"scope:\s*rg", text), "the foundation module must be scoped to rg"


@pytest.mark.skipif(
    shutil.which("bicep") is None and shutil.which("az") is None,
    reason="bicep/az CLI absent; the infra workflow validate job is authoritative",
)
def test_foundation_module_compiles(tmp_path: Path) -> None:
    """The foundation module compiles to ARM JSON with no error (local fast
    check; the infra workflow validate job is the authoritative gate).
    """
    outfile = tmp_path / "foundation.json"
    if shutil.which("bicep") is not None:
        cmd = ["bicep", "build", str(FOUNDATION), "--outfile", str(outfile)]
    else:
        cmd = ["az", "bicep", "build", "--file", str(FOUNDATION), "--outfile", str(outfile)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"bicep build failed:\n{proc.stderr}"


# ---------------------------------------------------------------------------
# Anti-coincidental-pass controls
#
# These verify the detectors above actually fire on the regressions they
# target, NOT that any specific module text is clean. Without them, a broken
# regex would let every structural gate pass coincidentally.
# ---------------------------------------------------------------------------


def test_resource_type_detector_controls() -> None:
    """``_declares_resource_type`` catches a real declaration, rejects a comment."""
    declared = "resource cae 'Microsoft.App/managedEnvironments@2024-03-01' = {\n  name: 'x'\n}\n"
    commented = "// resource cae 'Microsoft.App/managedEnvironments@2024-03-01' = {\n"
    assert _declares_resource_type(declared, _ACA_ENV_TYPE)
    assert not _declares_resource_type(commented, _ACA_ENV_TYPE)


def test_delegation_detector_controls() -> None:
    """``_has_delegation`` catches a named delegation, rejects an empty list."""
    with_deleg = (
        "delegations: [ { name: 'd', properties: "
        "{ serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers' } } ]"
    )
    without = "delegations: []"
    assert _has_delegation(with_deleg, _POSTGRES_DELEGATION)
    assert not _has_delegation(without, _POSTGRES_DELEGATION)


def test_secret_output_detector_controls() -> None:
    """The secret scan flags a ``listKeys()`` output, passes a clean one."""
    leak = "output k string = la.listKeys().primarySharedKey\n"
    clean = "output d string = env.properties.defaultDomain\n"
    assert _output_secret_violations(leak), "secret detector failed to flag a listKeys output"
    assert not _output_secret_violations(clean), "secret detector false-positived on a clean output"


def test_comment_stripper_controls() -> None:
    """``_strip_line_comments`` removes a commented module stub, keeps a live one."""
    commented = "  // module foundation 'modules/foundation.bicep' = {"
    assert "module foundation" not in _strip_line_comments(commented)
    live = "module foundation 'modules/foundation.bicep' = {"
    assert "module foundation" in _strip_line_comments(live)
