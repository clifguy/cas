"""Least-privilege secret access and the APIM-only SAGE ingress (CAS-ADR-042).

Each workload identity reads exactly the Key Vault secrets it consumes, through
role assignments scoped to those secrets -- never to the vault, where a single
grant reads every secret and certificate it holds. The API Management gateway
runs as an identity of its own, holding only the TLS certificate and the
ingress key. SAGE refuses any request that does not carry the ingress key the
gateway injects, so its public container ingress cannot be used to bypass the
gateway's checks.

These checks read the Bicep and policy sources. Whether the deployed role
assignments, named value and container secret resolve is a live fact proven by
a deploy, not here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

from sage.auth import INGRESS_KEY_ENV_VAR, INGRESS_KEY_HEADER

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
INFRA: Final[Path] = REPO_ROOT / "infra"
KEYVAULT: Final[Path] = INFRA / "modules" / "keyvault.bicep"
IDENTITY: Final[Path] = INFRA / "modules" / "identity.bicep"
APIM: Final[Path] = INFRA / "modules" / "apim.bicep"
CONTAINER_APPS: Final[Path] = INFRA / "modules" / "container-apps.bicep"
MAIN: Final[Path] = INFRA / "main.bicep"
POLICIES: Final[Path] = INFRA / "policies"

_SECRETS_USER_ROLE: Final[str] = "4633458b-17de-408a-b874-0445c86b69e6"
_CERTIFICATE_USER_ROLE: Final[str] = "db79e9a7-68ee-4b58-9aeb-b90e7c24fcba"

# (principal parameter, secret name): the complete set of data-plane grants.
_EXPECTED_GRANTS: Final[set[tuple[str, str]]] = {
    ("sagePrincipalId", "anthropic-api-key"),
    ("sagePrincipalId", "sage-ingress-key"),
    ("bffPrincipalId", "bff-client-secret"),
    ("bffPrincipalId", "wildcard-tls"),
    ("apimPrincipalId", "wildcard-tls"),
    ("apimPrincipalId", "sage-ingress-key"),
}


def _strip_comments(text: str) -> str:
    return re.sub(r"//[^\n]*", "", text)


def _blocks(text: str, type_prefix: str) -> list[tuple[str, str]]:
    """``(symbol, body)`` of every resource whose type starts with ``type_prefix``."""
    out = []
    for match in re.finditer(rf"resource\s+(\w+)\s+'{re.escape(type_prefix)}@[^']+'", text):
        start = text.index("{", match.end())
        depth = 0
        for end in range(start, len(text)):
            depth += {"{": 1, "}": -1}.get(text[end], 0)
            if depth == 0:
                break
        out.append((match.group(1), text[match.start() : end + 1]))
    return out


def _vars(text: str) -> dict[str, str]:
    return dict(re.findall(r"var\s+(\w+)\s*=\s*'([^']*)'", text))


def _secret_symbols(text: str) -> dict[str, str]:
    """Map each existing-secret resource symbol to the secret name it addresses."""
    names = _vars(text)
    out = {}
    for symbol, body in _blocks(text, "Microsoft.KeyVault/vaults/secrets"):
        assert " existing " in body, f"{symbol} must reference an existing secret, not create one"
        name = re.search(r"name:\s*(\w+|'[^']*')", body.split("{", 1)[1]).group(1)
        out[symbol] = name.strip("'") if name.startswith("'") else names[name]
    return out


def _grants(text: str) -> list[tuple[str, str, str]]:
    """``(scope symbol, principal, role literal)`` of every role assignment."""
    names = _vars(text)
    out = []
    for _, body in _blocks(text, "Microsoft.Authorization/roleAssignments"):
        scope = re.search(r"scope:\s*(\w+)", body).group(1)
        principal = re.search(r"principalId:\s*(\w+)", body).group(1)
        role_var = re.search(r"roleDefinitions',\s*(\w+)\)", body).group(1)
        out.append((scope, principal, names[role_var]))
    return out


def test_every_secret_grant_is_scoped_to_its_secret() -> None:
    """Every Key Vault data-plane grant names one secret, and together they are
    exactly the reads the workloads perform.

    A grant at vault scope reads every secret and certificate in the vault --
    Certificate User included, which carries ``secrets/getSecret`` -- so no
    assignment may be scoped to the vault itself.
    """
    text = _strip_comments(KEYVAULT.read_text(encoding="utf-8"))
    secrets = _secret_symbols(text)
    grants = _grants(text)
    vault_scoped = [g for g in grants if g[0] not in secrets]
    assert not vault_scoped, f"grants not scoped to a single secret: {vault_scoped}"
    assert {(principal, secrets[scope]) for scope, principal, _ in grants} == _EXPECTED_GRANTS
    assert len(grants) == len(_EXPECTED_GRANTS), "a grant is declared twice"
    assert {role for _, _, role in grants} == {_SECRETS_USER_ROLE}
    assert _CERTIFICATE_USER_ROLE not in text, "Certificate User reads every secret it can reach"


def test_secret_grants_wait_for_the_loaded_secrets() -> None:
    """A secret-scoped grant needs its secret to exist, and secrets are loaded
    after the first deployment, so every grant is conditional on the
    ``keyVaultSecretsLoaded`` switch, which the orchestrator threads through.
    """
    text = _strip_comments(KEYVAULT.read_text(encoding="utf-8"))
    for symbol, body in _blocks(text, "Microsoft.Authorization/roleAssignments"):
        assert re.search(r"=\s*if\s*\(\s*keyVaultSecretsLoaded\s*\)", body), (
            f"{symbol} must be conditional on keyVaultSecretsLoaded"
        )
    main = _strip_comments(MAIN.read_text(encoding="utf-8"))
    assert re.search(r"param\s+keyVaultSecretsLoaded\s+bool\s*=\s*true", main)
    assert main.count("keyVaultSecretsLoaded: keyVaultSecretsLoaded") == 3, (
        "the switch must reach the keyvault, apim and container-apps modules"
    )


def test_apim_runs_as_its_own_identity() -> None:
    """The gateway authenticates to Key Vault and Application Insights as a
    dedicated identity; nothing in the APIM module names the SAGE identity.
    """
    identity = _strip_comments(IDENTITY.read_text(encoding="utf-8"))
    assert "'id-apim-${environmentName}'" in identity
    for output in ("apimIdentityId", "apimIdentityPrincipalId", "apimIdentityClientId"):
        assert re.search(rf"output\s+{output}\s+string", identity), output
    apim = _strip_comments(APIM.read_text(encoding="utf-8"))
    assert "sageIdentity" not in apim, "APIM must not run as, or grant, the SAGE identity"
    assert re.search(r"'\$\{apimIdentityId\}':\s*\{\}", apim)
    assert len(re.findall(r"identityClientId:\s*apimIdentityClientId", apim)) == 3, (
        "the certificate binding, the telemetry logger and the ingress-key named value "
        "must each authenticate as the APIM identity"
    )
    assert re.search(r"principalId:\s*apimIdentityPrincipalId", apim), (
        "the telemetry-publisher grant must name the APIM identity"
    )
    main = _strip_comments(MAIN.read_text(encoding="utf-8"))
    assert "apimPrincipalId: identity.outputs.apimIdentityPrincipalId" in main


def test_every_forwarding_policy_injects_the_ingress_key() -> None:
    """Every policy that forwards to the SAGE backend sets the ingress-key
    header from the secret named value, overriding anything the caller sent.

    The operation policies that omit ``<base />`` skip the API-level inbound
    policy, so each must set the header itself.
    """
    expected = re.compile(
        rf'<set-header\s+name="{re.escape(INGRESS_KEY_HEADER)}"\s+exists-action="override">\s*'
        r"<value>\{\{sage-ingress-key\}\}</value>\s*</set-header>\s*"
        r'<set-backend-service backend-id="sage-backend" />'
    )
    forwarding = [
        p for p in sorted(POLICIES.glob("*.xml")) if "set-backend-service" in p.read_text()
    ]
    assert len(forwarding) == 5, [p.name for p in forwarding]
    missing = [p.name for p in forwarding if not expected.search(p.read_text())]
    assert not missing, f"policies forwarding without the ingress key: {missing}"


def test_ingress_key_named_value_is_key_vault_backed() -> None:
    """Once secrets are loaded the gateway reads the ingress key from Key Vault
    as its own identity; before then a placeholder stands in, which SAGE --
    holding no key -- does not check.
    """
    apim = _strip_comments(APIM.read_text(encoding="utf-8"))
    (body,) = [
        b
        for s, b in _blocks(apim, "Microsoft.ApiManagement/service/namedValues")
        if "'sage-ingress-key'" in b
    ]
    assert re.search(r"secret:\s*true", body)
    assert re.search(r"secretIdentifier:\s*sageIngressKeySecretUri", body)
    assert re.search(r"keyVaultSecretsLoaded\s*\?", body)


def test_sage_reads_the_ingress_key_from_key_vault() -> None:
    """The SAGE app resolves the ingress key into its environment from a Key
    Vault reference read as the SAGE identity, once secrets are loaded.
    """
    text = _strip_comments(CONTAINER_APPS.read_text(encoding="utf-8"))
    (body,) = [b for s, b in _blocks(text, "Microsoft.App/containerApps") if s == "sageApp"]
    assert re.search(
        r"name:\s*'sage-ingress-key'\s*keyVaultUrl:\s*sageIngressKeySecretUri\s*"
        r"identity:\s*sageIdentityId",
        body,
    ), "SAGE must reference the ingress key in Key Vault as its own identity"
    assert re.search(rf"name:\s*'{INGRESS_KEY_ENV_VAR}'\s*secretRef:\s*'sage-ingress-key'", body), (
        f"SAGE must receive the key as {INGRESS_KEY_ENV_VAR}"
    )
    assert body.count("keyVaultSecretsLoaded ?") == 2, (
        "both the secret and its environment variable wait for the loaded secrets"
    )


def test_least_privilege_detectors_discriminate() -> None:
    """Controls for the parsers above: a vault-scoped grant is reported as not
    secret-scoped, and an existing-secret declaration resolves through a var.
    """
    sample = """
var a = 'anthropic-api-key'
var r = '4633458b-17de-408a-b874-0445c86b69e6'
resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = { name: 'x' }
resource s 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: kv
  name: a
}
resource g1 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (keyVaultSecretsLoaded) {
  scope: s
  name: guid('1')
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r)
    principalId: sagePrincipalId
  }
}
resource g2 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: kv
  name: guid('2')
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r)
    principalId: bffPrincipalId
  }
}
"""
    assert _secret_symbols(sample) == {"s": "anthropic-api-key"}
    assert _grants(sample) == [
        ("s", "sagePrincipalId", _SECRETS_USER_ROLE),
        ("kv", "bffPrincipalId", _SECRETS_USER_ROLE),
    ]
