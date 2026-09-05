"""Structural and security-posture gate for the Key Vault module.

Locks the shape of ``infra/modules/keyvault.bicep`` — the secrets vault for the
cloud deployment profile (CAS-ADR-042). The vault holds the hosted abstraction
provider's API key and the owned wildcard TLS certificate; its access model
grants the SAGE and CAS BFF managed identities data-plane read via Azure RBAC.
Secret *values* are loaded out of band by a documented operator step, so the
module commits no secret material and the database connection authenticates by
managed identity rather than a stored password.

These checks read the tracked Bicep text only — they need no Azure or Bicep
tooling, so they run in the ordinary Python test job. The authoritative compile
+ lint of the module is the infra workflow's ``validate`` job (``az bicep
build`` under the error-level ``bicepconfig.json`` rules); a local fast-path
compile is provided here, skipped when neither CLI is present.

Detector logic lives in small pure helpers so the control tests can prove each
detector actually fires — a text-assertion gate is only meaningful if its
matchers fail on the regressions they target. The most important regression
this gate guards is a secret value leaking into the committed module.
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
KEYVAULT: Final[Path] = INFRA_DIR / "modules" / "keyvault.bicep"

_KV_TYPE: Final[str] = "Microsoft.KeyVault/vaults"
_KV_SECRET_CHILD_TYPE: Final[str] = "Microsoft.KeyVault/vaults/secrets"
_ROLE_ASSIGNMENT_TYPE: Final[str] = "Microsoft.Authorization/roleAssignments"

# Built-in Azure role: Key Vault Secrets User (data-plane read of secret values).
# A fixed, public Azure constant — not an environment identity coordinate.
_KV_SECRETS_USER_ROLE: Final[str] = "4633458b-17de-408a-b874-0445c86b69e6"

# A subscription / tenant / principal id is a GUID; none may be hardcoded as an
# identity coordinate. (Role-definition GUIDs are public Azure constants and are
# allowed — this gate only forbids literal GUIDs in outputs and bound to a
# principalId, never the role-definition variables.)
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
    """Return ``text`` with ``//`` line comments removed."""
    return "\n".join(re.sub(r"//.*$", "", line) for line in text.splitlines())


def _declares_resource_type(text: str, resource_type: str) -> bool:
    """True iff ``text`` declares a resource of exactly ``resource_type``."""
    pattern = re.compile(r"resource\s+\w+\s+'" + re.escape(resource_type) + r"@[0-9A-Za-z-]+'")
    return pattern.search(_strip_line_comments(text)) is not None


def _count_resource_type(text: str, resource_type: str) -> int:
    """Number of ``resource <symbol> '<type>@<version>'`` declarations of a type."""
    pattern = re.compile(r"resource\s+\w+\s+'" + re.escape(resource_type) + r"@[0-9A-Za-z-]+'")
    return len(pattern.findall(_strip_line_comments(text)))


def _module_block(text: str, module_path: str) -> str:
    """Return the body of the ``module <symbol> '<module_path>' = {...}`` call.

    Slices from the module declaration to the next top-level declaration. The
    orchestrator wires nine modules, so an assertion made over the whole file
    is satisfied by any one of them; the argument-threading gates below must
    read this module's own parameter list.
    """
    stripped = _strip_line_comments(text)
    start = re.search(
        r"^module\s+\w+\s+'" + re.escape(module_path) + r"'\s*=", stripped, re.MULTILINE
    )
    if start is None:
        return ""
    rest = stripped[start.end() :]
    nxt = re.search(r"^(?:resource|output|module|param|var)\s+\w+", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


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


def test_keyvault_module_exists() -> None:
    """The Key Vault module the orchestrator wires must exist."""
    assert KEYVAULT.is_file(), "infra/modules/keyvault.bicep missing"


def test_keyvault_declares_vault() -> None:
    """The module declares a Key Vault."""
    assert _declares_resource_type(KEYVAULT.read_text(encoding="utf-8"), _KV_TYPE), (
        f"keyvault.bicep must declare a {_KV_TYPE} resource"
    )


def test_keyvault_uses_rbac_authorization() -> None:
    """The access model is Azure RBAC (``enableRbacAuthorization: true``), not the
    legacy vault access-policy array — the no-stored-credential posture.
    """
    text = _strip_line_comments(KEYVAULT.read_text(encoding="utf-8"))
    assert re.search(r"enableRbacAuthorization:\s*true", text), (
        "the vault must set enableRbacAuthorization: true"
    )
    assert not re.search(r"accessPolicies:\s*\[\s*\{", text), (
        "the vault must use RBAC, not a populated accessPolicies array"
    )


def test_keyvault_grants_secrets_user_to_both_identities() -> None:
    """The vault grants the SAGE and CAS BFF identities data-plane read through
    Azure role assignments referencing the Key Vault Secrets User role.
    """
    text = KEYVAULT.read_text(encoding="utf-8")
    count = _count_resource_type(text, _ROLE_ASSIGNMENT_TYPE)
    assert count >= 2, f"expected >=2 role assignments (SAGE + BFF); found {count}"
    assert _KV_SECRETS_USER_ROLE in text, (
        "a role assignment must reference the Key Vault Secrets User role id"
    )
    assert re.search(r"principalType:\s*'ServicePrincipal'", _strip_line_comments(text)), (
        "role assignments must set principalType: 'ServicePrincipal'"
    )


def test_keyvault_principal_ids_are_parameters() -> None:
    """The granted principals arrive as parameters from the identity module — no
    principalId is a hardcoded literal GUID.
    """
    text = KEYVAULT.read_text(encoding="utf-8")
    assert re.search(r"param\s+sagePrincipalId\s+string", text), (
        "keyvault.bicep must take a `sagePrincipalId` string parameter"
    )
    assert re.search(r"param\s+bffPrincipalId\s+string", text), (
        "keyvault.bicep must take a `bffPrincipalId` string parameter"
    )
    stripped = _strip_line_comments(text)
    bound = [m.group(1) for m in re.finditer(r"principalId:\s*(\S+)", stripped)]
    literal = [v for v in bound if _GUID_RE.search(v)]
    assert not literal, f"principalId must come from a parameter, not a literal GUID: {literal}"


def test_keyvault_stores_no_committed_secret() -> None:
    """No secret material is committed: the module declares no vault ``secrets``
    child resource (each would require an inline value) and takes no ``@secure()``
    parameter. Secret values are loaded out of band by the documented operator step.
    """
    text = KEYVAULT.read_text(encoding="utf-8")
    secret_children = _count_resource_type(text, _KV_SECRET_CHILD_TYPE)
    assert secret_children == 0, (
        f"keyvault.bicep must not declare a {_KV_SECRET_CHILD_TYPE} resource "
        f"(secrets are loaded out of band); found {secret_children}"
    )
    assert "@secure()" not in text, (
        "keyvault.bicep must take no @secure() parameter; secret values load out of band"
    )


def test_keyvault_outputs_vault_uri_and_name() -> None:
    """The module exposes the vault uri and name, plus the canonical secret and
    certificate names, so downstream modules build Key Vault references.
    """
    names = [n.lower() for n, _ in _output_lines(KEYVAULT.read_text(encoding="utf-8"))]
    assert any("uri" in n for n in names), f"missing a vault uri output; have {names}"
    assert any("vault" in n and "name" in n for n in names), (
        f"missing a vault name output; have {names}"
    )
    assert any("anthropic" in n for n in names), (
        f"missing the API-key secret-name output; have {names}"
    )
    assert any("cert" in n or "tls" in n for n in names), (
        f"missing the TLS certificate-name output; have {names}"
    )


def test_keyvault_outputs_bff_client_secret_name() -> None:
    """The module owns the BFF confidential-client secret name as an output, so the
    container-apps consumer and the operator load step single-source it rather than
    each spelling the literal (mirrors ``anthropicSecretName`` / ``tlsCertificateName``).
    """
    outputs = _output_lines(KEYVAULT.read_text(encoding="utf-8"))
    matches = [(n, rhs) for n, rhs in outputs if "bff" in n.lower() and "secret" in n.lower()]
    assert matches, f"missing the BFF client-secret-name output; have {[n for n, _ in outputs]}"
    _name, rhs = matches[0]
    assert rhs == "'bff-client-secret'", (
        f"the BFF client-secret-name output must be the canonical 'bff-client-secret'; got {rhs}"
    )


def test_keyvault_outputs_contain_no_secrets() -> None:
    """No output exposes secret material (a listKeys/secretref expression) or a
    literal identity GUID — a local mirror of ``outputs-should-not-contain-secrets``.
    """
    violations = _output_secret_violations(KEYVAULT.read_text(encoding="utf-8"))
    assert not violations, f"secret-bearing outputs: {violations}"


def test_keyvault_is_resource_group_scoped() -> None:
    """The module is resource-group scoped (the Bicep default); the orchestrator
    deploys it with ``scope: rg``.
    """
    text = _strip_line_comments(KEYVAULT.read_text(encoding="utf-8"))
    assert not re.search(r"targetScope\s*=\s*'(subscription|managementGroup|tenant)'", text), (
        "keyvault.bicep is a resource-group module; it must not retarget the scope"
    )


def test_main_bicep_wires_keyvault_module() -> None:
    """The orchestrator wires the Key Vault module live, scopes it to the resource
    group, and feeds it the identity module's principal ids through the
    orchestrator (composed through outputs, not a cross-module reach).
    """
    text = _strip_line_comments(MAIN_BICEP.read_text(encoding="utf-8"))
    assert re.search(r"module\s+\w+\s+'modules/keyvault\.bicep'\s*=", text), (
        "main.bicep must wire a live module from modules/keyvault.bicep"
    )
    assert re.search(r"scope:\s*rg", text), "the keyvault module must be scoped to rg"
    assert re.search(r"sagePrincipalId:", text), "main.bicep must pass sagePrincipalId to keyvault"
    assert re.search(r"bffPrincipalId:", text), "main.bicep must pass bffPrincipalId to keyvault"
    assert "identity.outputs" in text, (
        "keyvault must consume the identity module's principal ids through the orchestrator"
    )


def test_main_bicep_threads_purge_protection_to_keyvault() -> None:
    """Purge protection is reachable from the deployment surface: the orchestrator
    declares the parameter and passes it into the module. Declared but unpassed,
    the setting can only be changed by editing the module — which is a stack edit,
    not the per-tenant parameter change it ought to be.
    """
    text = MAIN_BICEP.read_text(encoding="utf-8")
    assert re.search(
        r"param\s+enableKeyVaultPurgeProtection\s+bool\b", _strip_line_comments(text)
    ), "main.bicep must declare an `enableKeyVaultPurgeProtection bool` parameter"
    block = _module_block(text, "modules/keyvault.bicep")
    assert block, "main.bicep must wire a live module from modules/keyvault.bicep"
    assert re.search(r"enablePurgeProtection:\s*enableKeyVaultPurgeProtection\b", block), (
        "the keyvault module call must pass enablePurgeProtection: enableKeyVaultPurgeProtection"
    )


def test_purge_protection_defaults_off_on_both_surfaces() -> None:
    """Both the orchestrator parameter and the module parameter default to false.

    Azure refuses ``false`` for purge protection once the setting is applied and
    the setting is vault-wide, so a silent flip to ``true`` is irreversible and
    binds every workload sharing the vault. A default is the only mechanical
    guard against that.
    """
    main_text = _strip_line_comments(MAIN_BICEP.read_text(encoding="utf-8"))
    module_text = _strip_line_comments(KEYVAULT.read_text(encoding="utf-8"))
    assert re.search(r"param\s+enableKeyVaultPurgeProtection\s+bool\s*=\s*false", main_text), (
        "main.bicep's enableKeyVaultPurgeProtection must default to false"
    )
    assert re.search(r"param\s+enablePurgeProtection\s+bool\s*=\s*false", module_text), (
        "keyvault.bicep's enablePurgeProtection must default to false"
    )


def test_purge_protection_rationale_is_current() -> None:
    """The parameter's description states the real constraint: enabling purge
    protection is irreversible and vault-wide, so on a vault whose secrets are
    shared with another workload it binds that workload too. The older
    "recreatable" rationale does not survive that case and must not return.
    """
    text = KEYVAULT.read_text(encoding="utf-8")
    m = re.search(r"@description\('([^']*)'\)\s*\nparam\s+enablePurgeProtection\b", text)
    assert m is not None, "enablePurgeProtection must carry an @description"
    description = m.group(1).lower()
    assert "recreatable" not in description, (
        "the 'recreatable in the experimental profile' rationale does not hold for a "
        "vault shared with another workload"
    )
    assert "irreversible" in description, (
        "the description must state that enabling purge protection is irreversible"
    )
    assert "vault-wide" in description, (
        "the description must state that the setting is vault-wide, so it binds every "
        "workload sharing the vault and not only this one"
    )


def test_keyvault_soft_delete_is_unconditional() -> None:
    """Soft delete is bound to the literal ``true``, never to a parameter.

    With purge protection off by decision, soft delete is the only remaining
    recovery window for a deleted vault. Parameterizing it would turn a
    considered trade-off into a posture that can be switched to unrecoverable.
    """
    text = _strip_line_comments(KEYVAULT.read_text(encoding="utf-8"))
    m = re.search(r"enableSoftDelete:\s*(.+)", text)
    assert m is not None, "keyvault.bicep must set enableSoftDelete"
    assert m.group(1).strip() == "true", (
        f"enableSoftDelete must be the literal true, not {m.group(1).strip()!r}"
    )


@pytest.mark.skipif(
    shutil.which("bicep") is None and shutil.which("az") is None,
    reason="bicep/az CLI absent; the infra workflow validate job is authoritative",
)
def test_keyvault_module_compiles(tmp_path: Path) -> None:
    """The Key Vault module compiles to ARM JSON with no error (local fast check;
    the infra workflow validate job is the authoritative gate).
    """
    outfile = tmp_path / "keyvault.json"
    if shutil.which("bicep") is not None:
        cmd = ["bicep", "build", str(KEYVAULT), "--outfile", str(outfile)]
    else:
        cmd = ["az", "bicep", "build", "--file", str(KEYVAULT), "--outfile", str(outfile)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"bicep build failed:\n{proc.stderr}"


# ---------------------------------------------------------------------------
# Anti-coincidental-pass controls
#
# These verify the detectors above actually fire on the regressions they
# target, NOT that any specific module text is clean. Without them, a broken
# regex would let every structural gate pass coincidentally.
# ---------------------------------------------------------------------------


def test_secret_child_detector_controls() -> None:
    """``_count_resource_type`` flags a vault secret child, passes a bare vault."""
    child = "resource s 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = { name: 'x' }\n"
    clean = "resource v 'Microsoft.KeyVault/vaults@2023-07-01' = { name: 'x' }\n"
    assert _count_resource_type(child, _KV_SECRET_CHILD_TYPE) == 1
    assert _count_resource_type(clean, _KV_SECRET_CHILD_TYPE) == 0


def test_role_assignment_detector_controls() -> None:
    """``_count_resource_type`` counts a role assignment, returns zero without one."""
    with_ra = "resource r 'Microsoft.Authorization/roleAssignments@2022-04-01' = {}\n"
    without = "resource v 'Microsoft.KeyVault/vaults@2023-07-01' = {}\n"
    assert _count_resource_type(with_ra, _ROLE_ASSIGNMENT_TYPE) == 1
    assert _count_resource_type(without, _ROLE_ASSIGNMENT_TYPE) == 0


def test_secret_output_detector_controls() -> None:
    """The secret scan flags a ``listKeys()`` output, passes a clean vault-uri one."""
    leak = "output k string = kv.listKeys().value\n"
    clean = "output u string = kv.properties.vaultUri\n"
    assert _output_secret_violations(leak), "secret detector failed to flag a listKeys output"
    assert not _output_secret_violations(clean), "secret detector false-positived on a clean output"


def test_module_block_isolation_controls() -> None:
    """``_module_block`` returns only the named module's own call body.

    This is what makes the argument-threading gate load-bearing. A whole-file
    search would be satisfied by a neighbouring module's parameter list, or by
    the parameter merely being declared and never passed — exactly the shape the
    threading gate exists to reject.

    The neighbouring module is declared *after* the target: a helper that finds
    the target but never truncates would still leak it, and only this ordering
    catches that. (A contaminant placed before the target is excluded by the
    forward search alone and proves nothing.)
    """
    two_modules = (
        "module keyvault 'modules/keyvault.bicep' = {\n"
        "  params: {\n    sagePrincipalId: identity.outputs.sageIdentityPrincipalId\n  }\n}\n"
        "module other 'modules/other.bicep' = {\n"
        "  params: {\n    enablePurgeProtection: enableKeyVaultPurgeProtection\n  }\n}\n"
    )
    block = _module_block(two_modules, "modules/keyvault.bicep")
    assert block, "the detector must find the keyvault module call"
    assert "sagePrincipalId" in block, "the block must carry the keyvault call's own parameters"
    assert "enablePurgeProtection" not in block, (
        "the block must truncate at the next declaration, not leak the following "
        "module's parameter list"
    )
    assert _module_block(two_modules, "modules/absent.bicep") == ""
