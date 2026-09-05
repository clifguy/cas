"""Structural and security-posture gate for the custom-domain bindings.

Locks the shape of ``infra/modules/custom-domains.bicep`` and its operator
runbook ``docs/process/custom-domains-dns.md`` — the custom-domain + TLS layer
of the CAS cloud deployment profile (CAS-ADR-042). The module imports the owned
wildcard certificate into the Azure Container Apps environment from Key Vault
(no committed certificate material, no ACME validation); the ``sage`` hostname
binding on the API Management facade lives in ``apim.bicep`` and is gated by
``test_apim_module.py``. The DNS zone is in AWS Route 53, so record publication
is a manual operator step the runbook documents, including the cross-cloud
ordering.

These checks read the tracked Bicep and Markdown text only — they need no Azure
or Bicep tooling, so they run in the ordinary Python test job. The authoritative
compile + lint of the module is the infra workflow's ``validate`` job (``az bicep
build`` under the error-level ``bicepconfig.json`` rules); a local fast-path
compile is provided here, skipped when neither CLI is present.

Detector logic lives in small pure helpers so the control tests can prove each
detector actually fires — a text-assertion gate is only meaningful if its
matchers fail on the regressions they target. The most important regression this
gate guards is the wildcard certificate's private material leaking into the
committed module as an inline PFX instead of a Key Vault reference.
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
CUSTOM_DOMAINS: Final[Path] = INFRA_DIR / "modules" / "custom-domains.bicep"
RUNBOOK: Final[Path] = REPO_ROOT / "docs" / "process" / "custom-domains-dns.md"

# The ACA environment certificate the module imports, and the managed
# environment it references (as `existing`, never re-declared).
_ACA_CERT_TYPE: Final[str] = "Microsoft.App/managedEnvironments/certificates"
_ACA_ENV_TYPE: Final[str] = "Microsoft.App/managedEnvironments"

# A subscription / tenant / principal / client id is a GUID; none may be a
# hardcoded identity coordinate — they arrive as parameters from the identity
# and Key Vault modules through the orchestrator.
_GUID_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b"
)

# Substrings that betray a secret leaking through a module output.
_SECRET_TOKENS: Final[tuple[str, ...]] = (
    "listkeys",
    "listsecrets",
    "sharedkey",
    "primarykey",
    "secretref",
)


# ---------------------------------------------------------------------------
# Detectors (pure text functions — exercised by the control tests below)
# ---------------------------------------------------------------------------


def _strip_line_comments(text: str) -> str:
    """Return ``text`` with ``//`` line comments removed.

    The negative lookbehind spares the ``//`` inside a URL scheme so a host
    literal is not silently swallowed before the hardcoded-URL check sees it.
    """
    return "\n".join(re.sub(r"(?<!:)//.*$", "", line) for line in text.splitlines())


def _declares_resource_type(text: str, resource_type: str) -> bool:
    """True iff ``text`` declares a (non-``existing``) resource of ``resource_type``."""
    pattern = re.compile(
        r"resource\s+\w+\s+'" + re.escape(resource_type) + r"@[0-9A-Za-z-]+'\s*=(?!\s*\w)"
    )
    return pattern.search(_strip_line_comments(text)) is not None


def _references_existing_type(text: str, resource_type: str) -> bool:
    """True iff ``text`` references ``resource_type`` with the ``existing`` keyword."""
    pattern = re.compile(
        r"resource\s+\w+\s+'" + re.escape(resource_type) + r"@[0-9A-Za-z-]+'\s+existing\b"
    )
    return pattern.search(_strip_line_comments(text)) is not None


def _resource_block(text: str, symbol: str) -> str:
    """Return the body of the ``resource <symbol> '...' = {...}`` declaration.

    Slices to the next top-level declaration. The module declares the ACA
    environment as ``existing`` alongside the certificate it binds, so a property
    asserted over the whole module can be satisfied by the wrong resource; the
    certificate gates below must read the certificate's own body. Returns ``""``
    when the symbol is not declared.
    """
    stripped = _strip_line_comments(text)
    start = re.search(rf"^resource\s+{re.escape(symbol)}\b", stripped, re.MULTILINE)
    if start is None:
        return ""
    rest = stripped[start.end() :]
    nxt = re.search(r"^(?:resource|output|module|param|var)\s+\w+", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def _module_block(text: str, module_path: str) -> str:
    """Return the body of the ``module <symbol> '<module_path>' = {...}`` call.

    Slices from the module declaration to the next top-level declaration. The
    orchestrator wires nine modules and every one is scoped to the resource
    group, so an assertion made over the whole file is satisfied by any one of
    them; the wiring gate below must read this module's own call body.
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


def _hardcoded_https_hosts(text: str) -> list[str]:
    """Return every ``https://`` URL whose host is a literal (not a ``${...}``
    interpolation). The cert's Key Vault URL must arrive as a parameter, so a
    concrete ``https://`` host in the module is a hardcoded-endpoint smell.
    """
    return re.findall(r"https://(?!\$\{)[^'\"\s]+", _strip_line_comments(text))


# ---------------------------------------------------------------------------
# Structural / posture gates — the module
# ---------------------------------------------------------------------------


def test_custom_domains_module_exists() -> None:
    """The custom-domain module the orchestrator wires must exist."""
    assert CUSTOM_DOMAINS.is_file(), "infra/modules/custom-domains.bicep missing"


def test_main_bicep_wires_custom_domains_module() -> None:
    """The orchestrator wires the module live, scopes it to the rg, and feeds it
    the ACA environment name, the cert Key Vault secret URI, the certificate
    name, and the BFF managed identity — all composed through module outputs.

    Every assertion reads this module's own call body. Read over the whole file
    they would be satisfied by a neighbour: every module is scoped to the rg,
    and the BFF identity id is passed to three other modules besides this one.
    """
    block = _module_block(MAIN_BICEP.read_text(encoding="utf-8"), "modules/custom-domains.bicep")
    assert block, "main.bicep must wire a live module from modules/custom-domains.bicep"
    assert re.search(r"scope:\s*rg\b", block), "the custom-domains module must be scoped to rg"
    assert "foundation.outputs.acaEnvironmentName" in block, (
        "custom-domains must receive the ACA environment name from foundation.outputs"
    )
    assert "identity.outputs.bffIdentityId" in block, (
        "custom-domains must receive the BFF identity id from identity.outputs"
    )
    assert "keyvault.outputs.tlsCertificateName" in block, (
        "custom-domains must receive the TLS certificate name from keyvault.outputs"
    )


def test_custom_domains_declares_aca_env_certificate() -> None:
    """The module declares the ACA managed-environment certificate (the
    environment-level binding of the wildcard cert).
    """
    text = CUSTOM_DOMAINS.read_text(encoding="utf-8")
    assert _declares_resource_type(text, _ACA_CERT_TYPE), (
        f"custom-domains.bicep must declare a {_ACA_CERT_TYPE} resource"
    )


def test_custom_domains_cert_sources_from_keyvault() -> None:
    """The certificate is sourced from Key Vault by reference: it carries
    ``certificateKeyVaultProperties`` with a ``keyVaultUrl`` from a parameter
    (no literal host) and an ``identity`` from the BFF-identity parameter.

    Read out of the certificate's own body: the module also declares the ACA
    environment, so a property found anywhere in the module says nothing about
    which resource carries it.
    """
    text = _strip_line_comments(CUSTOM_DOMAINS.read_text(encoding="utf-8"))
    cert = _resource_block(text, "wildcardCertificate")
    assert cert, "custom-domains.bicep must declare the wildcardCertificate resource"
    assert "certificateKeyVaultProperties" in cert, (
        "the certificate must use certificateKeyVaultProperties (Key Vault reference)"
    )
    assert re.search(r"keyVaultUrl:\s*\S", cert), "the cert must set keyVaultUrl"
    assert re.search(r"identity:\s*\w*[Ii]dentity", cert), (
        "certificateKeyVaultProperties.identity must reference an identity parameter"
    )
    assert not _hardcoded_https_hosts(text), (
        f"the Key Vault URL must be parameterized; hardcoded https host(s): "
        f"{_hardcoded_https_hosts(text)}"
    )


def test_custom_domains_commits_no_cert_material() -> None:
    """No certificate private material is committed: the certificate is a Key
    Vault reference, never an inline PFX (``value`` + ``password``) and never a
    ``@secure()`` parameter. This is the highest-severity regression this gate
    guards.
    """
    text = _strip_line_comments(CUSTOM_DOMAINS.read_text(encoding="utf-8"))
    assert "@secure()" not in text, (
        "custom-domains.bicep must take no @secure() parameter; the cert is a KV reference"
    )
    assert not re.search(r"\bpassword:", text), (
        "custom-domains.bicep must not set a certificate password (it is a KV reference)"
    )
    assert not re.search(r"\bvalue:\s*'MI", text), (
        "custom-domains.bicep must not embed an inline PFX/PEM blob"
    )


def test_custom_domains_references_env_as_existing() -> None:
    """The module references the ACA environment as ``existing`` — it binds a
    certificate to the environment foundation created, never re-declaring (and
    so clobbering) the environment itself.
    """
    text = CUSTOM_DOMAINS.read_text(encoding="utf-8")
    assert _references_existing_type(text, _ACA_ENV_TYPE), (
        f"custom-domains.bicep must reference {_ACA_ENV_TYPE} with the `existing` keyword"
    )
    assert not _declares_resource_type(text, _ACA_ENV_TYPE), (
        "custom-domains.bicep must not re-declare the managed environment"
    )


def test_custom_domains_no_hardcoded_identity_or_url() -> None:
    """No identity GUID and no Azure host literal is baked into the module —
    identity coordinates and the Key Vault URL arrive as parameters.
    """
    text = CUSTOM_DOMAINS.read_text(encoding="utf-8")
    assert not _GUID_RE.search(text), "custom-domains.bicep must not hardcode an identity GUID"
    stripped = _strip_line_comments(text)
    for host_literal in (".vault.azure.net", ".azurecontainerapps.io"):
        assert host_literal not in stripped, (
            f"custom-domains.bicep must not hardcode the Azure host {host_literal!r}"
        )


def test_custom_domains_outputs_contain_no_secrets() -> None:
    """No module output exposes secret material or a literal identity GUID — a
    local mirror of the bicep ``outputs-should-not-contain-secrets`` rule.
    """
    violations = _output_secret_violations(CUSTOM_DOMAINS.read_text(encoding="utf-8"))
    assert not violations, f"secret-bearing outputs: {violations}"


def test_custom_domains_parameterizes_location() -> None:
    """Location is a parameter (not a hardcoded region) — mirrors
    ``no-hardcoded-location``.
    """
    text = CUSTOM_DOMAINS.read_text(encoding="utf-8")
    assert re.search(r"param\s+location\s+string", text), (
        "custom-domains.bicep must take a `location` string parameter"
    )


def test_custom_domains_is_resource_group_scoped() -> None:
    """The module is resource-group scoped (the Bicep default); the orchestrator
    deploys it with ``scope: rg``.
    """
    text = _strip_line_comments(CUSTOM_DOMAINS.read_text(encoding="utf-8"))
    assert not re.search(r"targetScope\s*=\s*'(subscription|managementGroup|tenant)'", text), (
        "custom-domains.bicep is a resource-group module; it must not retarget the scope"
    )


@pytest.mark.skipif(
    shutil.which("bicep") is None and shutil.which("az") is None,
    reason="bicep/az CLI absent; the infra workflow validate job is authoritative",
)
def test_custom_domains_module_compiles(tmp_path: Path) -> None:
    """The custom-domains module compiles to ARM JSON with no error (local fast
    check; the infra workflow validate job is the authoritative gate).
    """
    outfile = tmp_path / "custom-domains.json"
    if shutil.which("bicep") is not None:
        cmd = ["bicep", "build", str(CUSTOM_DOMAINS), "--outfile", str(outfile)]
    else:
        cmd = ["az", "bicep", "build", "--file", str(CUSTOM_DOMAINS), "--outfile", str(outfile)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"bicep build failed:\n{proc.stderr}"


# ---------------------------------------------------------------------------
# Structural gates — the operator runbook
# ---------------------------------------------------------------------------


def test_custom_domains_runbook_exists() -> None:
    """The Route 53 operator runbook the module's binding depends on must exist."""
    assert RUNBOOK.is_file(), "docs/process/custom-domains-dns.md missing"


def test_runbook_covers_both_hostnames() -> None:
    """The runbook addresses both custom hostnames the profile binds."""
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "cas" in text and "sage" in text, "the runbook must cover both cas and sage"


def test_runbook_documents_route53_records() -> None:
    """The runbook documents the manual AWS Route 53 records: CNAMEs to the Azure
    FQDNs and the Azure domain-ownership-validation TXT record.
    """
    text = RUNBOOK.read_text(encoding="utf-8")
    lowered = text.lower()
    assert "route 53" in lowered or "route53" in lowered, "the runbook must name AWS Route 53"
    assert "cname" in lowered, "the runbook must document the CNAME records"
    assert "txt" in lowered and "asuid" in lowered, (
        "the runbook must document the domain-ownership TXT (asuid) record"
    )


def test_runbook_documents_cross_cloud_ordering() -> None:
    """The runbook captures the cross-cloud ordering the ticket calls out: Bicep
    emits the FQDN + validation token, the operator publishes the records, the
    binding completes.
    """
    text = RUNBOOK.read_text(encoding="utf-8")
    lowered = text.lower()
    assert "order" in lowered or "sequenc" in lowered, (
        "the runbook must have an ordering/sequencing section"
    )
    assert "validation" in lowered or "verification" in lowered, (
        "the runbook must reference the domain-ownership validation token"
    )
    # The defer-to-deploy boundary: the cas records resolve only once the BFF
    # container app exists (the deploy ticket), not in this binding ticket.
    assert "deploy" in lowered, (
        "the runbook must note that the cas records resolve at the deploy step"
    )


def test_runbook_has_no_hardcoded_domain_or_identity() -> None:
    """The base domain is a templated placeholder, never a literal registrable
    domain, and no identity GUID is baked in — mirrors the templating discipline
    of the Entra registrations runbook.
    """
    text = RUNBOOK.read_text(encoding="utf-8")
    assert not _GUID_RE.search(text), "the runbook must not hardcode an identity GUID"
    assert "<BASE_DOMAIN>" in text or "${" in text, (
        "the runbook must template the owned domain (<BASE_DOMAIN>), not hardcode it"
    )


# ---------------------------------------------------------------------------
# Anti-coincidental-pass controls
#
# These verify the detectors above actually fire on the regressions they
# target, NOT that any specific module text is clean. Without them, a broken
# regex would let every structural gate pass coincidentally.
# ---------------------------------------------------------------------------


def test_resource_type_detector_controls() -> None:
    """``_declares_resource_type`` catches a real declaration, rejects a comment
    and rejects an ``existing`` reference (which is not a fresh declaration).
    """
    declared = "resource c 'Microsoft.App/managedEnvironments/certificates@2025-01-01' = {\n}\n"
    commented = "// resource c 'Microsoft.App/managedEnvironments/certificates@2025-01-01' = {\n"
    existing = "resource e 'Microsoft.App/managedEnvironments@2024-03-01' existing = {\n}\n"
    assert _declares_resource_type(declared, _ACA_CERT_TYPE)
    assert not _declares_resource_type(commented, _ACA_CERT_TYPE)
    assert not _declares_resource_type(existing, _ACA_ENV_TYPE)


def test_existing_reference_detector_controls() -> None:
    """``_references_existing_type`` flags an ``existing`` reference, rejects a
    fresh declaration of the same type.
    """
    existing = "resource e 'Microsoft.App/managedEnvironments@2024-03-01' existing = {\n}\n"
    declared = "resource e 'Microsoft.App/managedEnvironments@2024-03-01' = {\n}\n"
    assert _references_existing_type(existing, _ACA_ENV_TYPE)
    assert not _references_existing_type(declared, _ACA_ENV_TYPE)


def test_no_cert_material_detector_controls() -> None:
    """The committed-material checks flag an inline PFX / password / @secure(),
    and pass a clean Key Vault reference block.
    """
    inline = "value: 'MIIKEQIBAzCC'\npassword: certPassword\n"
    secure = "@secure()\nparam certPassword string\n"
    clean = "certificateKeyVaultProperties: {\n identity: bffIdentityId\n keyVaultUrl: certUri\n}\n"
    assert re.search(r"\bvalue:\s*'MI", inline) and re.search(r"\bpassword:", inline)
    assert "@secure()" in secure
    assert "@secure()" not in clean
    assert not re.search(r"\bpassword:", clean)
    assert not re.search(r"\bvalue:\s*'MI", clean)


def test_secret_output_detector_controls() -> None:
    """The secret scan flags a ``listKeys()`` output, passes a clean cert-id one."""
    leak = "output k string = kv.listKeys().value\n"
    clean = "output id string = wildcardCert.id\n"
    assert _output_secret_violations(leak), "secret detector failed to flag a listKeys output"
    assert not _output_secret_violations(clean), "secret detector false-positived on a clean output"


def test_hardcoded_https_host_detector_controls() -> None:
    """``_hardcoded_https_hosts`` flags a literal host, passes an interpolation."""
    literal = "keyVaultUrl: 'https://kv.vault.azure.net/secrets/wildcard-tls'\n"
    interpolated = "keyVaultUrl: tlsCertSecretUri\n"
    assert _hardcoded_https_hosts(literal) == ["https://kv.vault.azure.net/secrets/wildcard-tls"]
    assert _hardcoded_https_hosts(interpolated) == []


def test_comment_stripper_controls() -> None:
    """``_strip_line_comments`` removes a commented stub, keeps a live line, and
    does not swallow a URL scheme.
    """
    commented = "  // resource c 'Microsoft.App/managedEnvironments/certificates@2025-01-01' = {"
    assert "resource c" not in _strip_line_comments(commented)
    live = "resource c 'Microsoft.App/managedEnvironments/certificates@2025-01-01' = {"
    assert "resource c" in _strip_line_comments(live)
    url_line = "    keyVaultUrl: 'https://kv.vault.azure.net' // the cert"
    stripped = _strip_line_comments(url_line)
    assert "https://kv.vault.azure.net" in stripped
    assert "the cert" not in stripped


def test_module_block_detector_controls() -> None:
    """``_module_block`` returns only the named module's own call body.

    This is what makes the wiring gate load-bearing: every module in the
    orchestrator carries ``scope: rg`` and three of them are handed the same BFF
    identity id, so a whole-file search is satisfied by a neighbour even when
    this module's own call has lost the line.

    The neighbouring module is declared *after* the target: a helper that finds
    the target but never truncates would still leak it, and only this ordering
    catches that. (A contaminant placed before the target is excluded by the
    forward search alone and proves nothing.)
    """
    two_modules = (
        "module customDomains 'modules/custom-domains.bicep' = {\n"
        "  scope: rg\n"
        "  params: {\n    acaEnvironmentName: foundation.outputs.acaEnvironmentName\n  }\n}\n"
        "module other 'modules/other.bicep' = {\n"
        "  scope: rg\n"
        "  params: {\n    sentinel: true\n  }\n}\n"
    )
    block = _module_block(two_modules, "modules/custom-domains.bicep")
    assert block, "the detector must find the custom-domains module call"
    assert "acaEnvironmentName" in block, "the block must carry the call's own parameters"
    assert "sentinel" not in block, (
        "the block must truncate at the next declaration, not leak the following "
        "module's parameter list"
    )
    assert _module_block(two_modules, "modules/absent.bicep") == ""


def test_resource_block_detector_controls() -> None:
    """``_resource_block`` returns only the named resource's own body.

    This is what makes the certificate gates load-bearing: the module declares
    the ACA environment beside the certificate, so a property found anywhere in
    the module could belong to either resource.

    The neighbouring resource is declared *after* the target: a helper that finds
    the target but never truncates would still leak it, and only this ordering
    catches that. (A contaminant placed before the target is excluded by the
    forward search alone and proves nothing.)
    """
    sample = (
        f"resource wildcardCertificate '{_ACA_CERT_TYPE}@2025-01-01' = {{\n"
        "  properties: {\n"
        "    certificateKeyVaultProperties: {\n"
        "      keyVaultUrl: tlsCertSecretUri\n"
        "    }\n  }\n}\n"
        f"resource acaEnvironment '{_ACA_ENV_TYPE}@2024-03-01' existing = {{\n"
        "  identity: bffIdentityId\n"
        "  sentinel: true\n"
        "}\n"
    )
    block = _resource_block(sample, "wildcardCertificate")
    assert block, "the detector must find the wildcardCertificate declaration"
    assert "keyVaultUrl" in block, "the block must carry the certificate's own properties"
    assert "identity:" not in block, (
        "the block must truncate at the next declaration, not borrow the environment's "
        "identity binding"
    )
    assert "sentinel" not in block, "the block must not bleed into the following resource"
    assert _resource_block(sample, "absentSymbol") == ""
