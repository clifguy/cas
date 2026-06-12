"""Structural and security-posture gate for the Postgres storage module.

Locks the shape of ``infra/modules/postgres.bicep`` — the relational-store
module in the CAS cloud deployment profile (CAS-ADR-042) — so the managed
PostgreSQL Flexible Server it provisions stays privately networked, Entra-only
for authentication, and configured with the extensions and database the SAGE
storage adapters require.

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
POSTGRES: Final[Path] = INFRA_DIR / "modules" / "postgres.bicep"

# Resource provider types the module must declare.
_SERVER_TYPE: Final[str] = "Microsoft.DBforPostgreSQL/flexibleServers"
_DATABASE_TYPE: Final[str] = "Microsoft.DBforPostgreSQL/flexibleServers/databases"
_CONFIG_TYPE: Final[str] = "Microsoft.DBforPostgreSQL/flexibleServers/configurations"
_ADMIN_TYPE: Final[str] = "Microsoft.DBforPostgreSQL/flexibleServers/administrators"
_DNS_ZONE_TYPE: Final[str] = "Microsoft.Network/privateDnsZones"
_DNS_LINK_TYPE: Final[str] = "Microsoft.Network/privateDnsZones/virtualNetworkLinks"

# The database SAGE connects to (StackPostgresConfig.database default) and the
# extensions the schema bootstrap enables (StackPostgresConfig.extensions). The
# managed server refuses ``CREATE EXTENSION`` unless the extension is named in
# the ``azure.extensions`` server parameter, so the module must allowlist them.
_SAGE_DATABASE: Final[str] = "sage"
_REQUIRED_EXTENSIONS: Final[tuple[str, ...]] = ("vector", "pgstattuple")
_EXTENSIONS_PARAM: Final[str] = "azure.extensions"

# A subscription / tenant / object id is a GUID; none may be hardcoded into the
# module — the Entra admin principal arrives as a deployment parameter.
_GUID_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b"
)

# Substrings that betray a secret leaking through a module output.
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

    Keeps the structure checks from passing on a commented-out stub. Naive but
    sufficient: the string literals these gates inspect — resource types,
    config values, output expressions — never contain ``//``.
    """
    return "\n".join(re.sub(r"//.*$", "", line) for line in text.splitlines())


def _declares_resource_type(text: str, resource_type: str) -> bool:
    """True iff ``text`` declares a resource of ``resource_type``.

    Matches the Bicep ``resource <symbol> '<type>@<version>'`` declaration
    form, not a bare mention in a comment or string literal.
    """
    pattern = re.compile(r"resource\s+\w+\s+'" + re.escape(resource_type) + r"@[0-9A-Za-z-]+'")
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


def test_postgres_module_exists() -> None:
    """The storage module file the orchestrator wires must exist."""
    assert POSTGRES.is_file(), "infra/modules/postgres.bicep missing"


def test_postgres_declares_flexible_server() -> None:
    """The module declares a managed PostgreSQL Flexible Server."""
    text = POSTGRES.read_text(encoding="utf-8")
    assert _declares_resource_type(text, _SERVER_TYPE), (
        f"postgres.bicep must declare a {_SERVER_TYPE} resource"
    )


def test_postgres_creates_sage_database() -> None:
    """The module creates the database SAGE connects to — a server with no
    database would leave the storage adapters pointed at nothing.
    """
    text = POSTGRES.read_text(encoding="utf-8")
    assert _declares_resource_type(text, _DATABASE_TYPE), (
        f"postgres.bicep must declare a {_DATABASE_TYPE} resource"
    )
    assert f"'{_SAGE_DATABASE}'" in _strip_line_comments(text), (
        f"postgres.bicep must create the '{_SAGE_DATABASE}' database"
    )


def test_postgres_allowlists_required_extensions() -> None:
    """The server allowlists pgvector and pgstattuple via the ``azure.extensions``
    server parameter — without it the schema bootstrap's ``CREATE EXTENSION``
    fails on a managed server and the content store is dead on arrival.
    """
    text = _strip_line_comments(POSTGRES.read_text(encoding="utf-8"))
    assert _declares_resource_type(text, _CONFIG_TYPE), (
        f"postgres.bicep must declare a {_CONFIG_TYPE} resource"
    )
    assert f"'{_EXTENSIONS_PARAM}'" in text, (
        f"the configuration resource must set the {_EXTENSIONS_PARAM!r} server parameter"
    )
    lowered = text.lower()
    for ext in _REQUIRED_EXTENSIONS:
        assert ext in lowered, f"the {_EXTENSIONS_PARAM!r} value must allowlist {ext!r}"


def test_postgres_private_vnet_integration() -> None:
    """The server integrates into the delegated subnet with a private DNS zone
    and is not publicly reachable (private connectivity, AC of the storage
    module).
    """
    text = _strip_line_comments(POSTGRES.read_text(encoding="utf-8"))
    assert "delegatedSubnetResourceId" in text, (
        "the server must bind network.delegatedSubnetResourceId (VNet integration)"
    )
    assert "privateDnsZoneArmResourceId" in text, (
        "the server must bind network.privateDnsZoneArmResourceId (private DNS)"
    )
    assert not re.search(r"publicNetworkAccess:\s*'Enabled'", text), (
        "the server must not enable public network access"
    )


def test_postgres_private_dns_zone_and_link() -> None:
    """The module owns the private DNS zone and links it to the VNet so the
    server FQDN resolves privately from inside the network.
    """
    text = POSTGRES.read_text(encoding="utf-8")
    assert _declares_resource_type(text, _DNS_ZONE_TYPE), (
        f"postgres.bicep must declare a {_DNS_ZONE_TYPE} resource"
    )
    assert _declares_resource_type(text, _DNS_LINK_TYPE), (
        f"postgres.bicep must declare a {_DNS_LINK_TYPE} resource (zone-to-VNet link)"
    )
    assert "postgres.database.azure.com" in _strip_line_comments(text), (
        "the private DNS zone must be a *.postgres.database.azure.com zone"
    )


def test_postgres_entra_only_auth() -> None:
    """Authentication is Entra-only: Active Directory auth enabled, password
    auth disabled, and no SQL admin login or password anywhere in the module.
    """
    text = _strip_line_comments(POSTGRES.read_text(encoding="utf-8"))
    assert re.search(r"activeDirectoryAuth:\s*'Enabled'", text), (
        "authConfig.activeDirectoryAuth must be 'Enabled'"
    )
    assert re.search(r"passwordAuth:\s*'Disabled'", text), (
        "authConfig.passwordAuth must be 'Disabled' (no stored DB password)"
    )
    lowered = text.lower()
    assert "administratorlogin" not in lowered, (
        "Entra-only auth must not declare a SQL administratorLogin"
    )


def test_postgres_declares_aad_administrator() -> None:
    """The module binds an Entra administrator for the server (the principal
    that AAD-only access is granted to).
    """
    text = POSTGRES.read_text(encoding="utf-8")
    assert _declares_resource_type(text, _ADMIN_TYPE), (
        f"postgres.bicep must declare a {_ADMIN_TYPE} resource"
    )


def test_postgres_exposes_required_outputs() -> None:
    """The cloud profile config consumes the server FQDN and database name, so
    both must surface as module outputs.
    """
    names = [name for name, _ in _output_lines(POSTGRES.read_text(encoding="utf-8"))]
    lowered = [n.lower() for n in names]
    assert any("fqdn" in n for n in lowered), f"missing a server-FQDN output; have {names}"
    assert any("database" in n for n in lowered), f"missing a database-name output; have {names}"


def test_postgres_outputs_contain_no_secrets() -> None:
    """No module output exposes a secret or a hardcoded identity GUID — a local
    mirror of the bicep ``outputs-should-not-contain-secrets`` rule.
    """
    violations = _output_secret_violations(POSTGRES.read_text(encoding="utf-8"))
    assert not violations, f"secret-bearing outputs: {violations}"


def test_postgres_parameterizes_location_no_hardcoded_identity() -> None:
    """Location is a parameter (not a hardcoded region) and no identity GUID is
    baked into the module — the Entra admin principal arrives as a parameter.
    """
    text = POSTGRES.read_text(encoding="utf-8")
    assert re.search(r"param\s+location\s+string", text), (
        "postgres.bicep must take a `location` string parameter"
    )
    assert not _GUID_RE.search(text), "postgres.bicep must not hardcode an identity GUID"


def test_postgres_is_not_subscription_scoped() -> None:
    """The module is resource-group scoped (the Bicep default): the orchestrator
    deploys it with ``scope: rg``.
    """
    text = _strip_line_comments(POSTGRES.read_text(encoding="utf-8"))
    assert not re.search(r"targetScope\s*=\s*'subscription'", text), (
        "postgres.bicep is a resource-group module; it must not target the subscription"
    )
    assert not re.search(r"targetScope\s*=\s*'(managementGroup|tenant)'", text), (
        "postgres.bicep must not target the management-group or tenant scope"
    )


def test_main_bicep_wires_postgres_module() -> None:
    """The orchestrator wires the postgres module live, scopes it to the resource
    group, and feeds it the foundation's delegated-subnet and VNet ids.
    """
    text = _strip_line_comments(MAIN_BICEP.read_text(encoding="utf-8"))
    assert re.search(r"module\s+\w+\s+'modules/postgres\.bicep'\s*=", text), (
        "main.bicep must wire a live module from modules/postgres.bicep"
    )
    assert "foundation.outputs.postgresSubnetId" in text, (
        "the postgres module must consume the foundation's postgresSubnetId output"
    )
    assert "foundation.outputs.vnetId" in text, (
        "the postgres module must consume the foundation's vnetId output"
    )


@pytest.mark.skipif(
    shutil.which("bicep") is None and shutil.which("az") is None,
    reason="bicep/az CLI absent; the infra workflow validate job is authoritative",
)
def test_postgres_module_compiles(tmp_path: Path) -> None:
    """The postgres module compiles to ARM JSON with no error (local fast check;
    the infra workflow validate job is the authoritative gate).
    """
    outfile = tmp_path / "postgres.json"
    if shutil.which("bicep") is not None:
        cmd = ["bicep", "build", str(POSTGRES), "--outfile", str(outfile)]
    else:
        cmd = ["az", "bicep", "build", "--file", str(POSTGRES), "--outfile", str(outfile)]
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
    declared = "resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {\n}\n"
    commented = "// resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {\n"
    assert _declares_resource_type(declared, _SERVER_TYPE)
    assert not _declares_resource_type(commented, _SERVER_TYPE)


def test_extension_allowlist_detector_controls() -> None:
    """The comment stripper keeps a live ``azure.extensions`` value but drops a
    commented one — the basis for the extension-allowlist gate.
    """
    live = "    name: 'azure.extensions'\n    value: 'VECTOR,PGSTATTUPLE'"
    commented = "    // name: 'azure.extensions'  value: 'VECTOR,PGSTATTUPLE'"
    assert "azure.extensions" in _strip_line_comments(live)
    assert "azure.extensions" not in _strip_line_comments(commented)


def test_secret_output_detector_controls() -> None:
    """The secret scan flags a password-bearing output, passes a clean one."""
    leak = "output c string = server.properties.administratorLoginPassword\n"
    clean = "output f string = server.properties.fullyQualifiedDomainName\n"
    assert _output_secret_violations(leak), "secret detector failed to flag a password output"
    assert not _output_secret_violations(clean), "secret detector false-positived on a clean output"


def test_comment_stripper_controls() -> None:
    """``_strip_line_comments`` removes a commented module stub, keeps a live one."""
    commented = "  // module postgres 'modules/postgres.bicep' = {"
    assert "module postgres" not in _strip_line_comments(commented)
    live = "module postgres 'modules/postgres.bicep' = {"
    assert "module postgres" in _strip_line_comments(live)
