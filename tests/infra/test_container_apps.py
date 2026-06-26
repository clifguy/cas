"""Structural and security-posture gate for the container-apps module.

Locks the shape of ``infra/modules/container-apps.bicep`` — the two Azure
Container Apps (SAGE and the CAS BFF) for the cloud deployment profile
(CAS-ADR-042). Each app pulls its image from the container registry by its
user-assigned managed identity (an ``AcrPull`` role assignment), is fronted by
its own ingress (SAGE behind the API Management facade, the BFF on external
container ingress with its custom domain), and receives its cloud-profile
runtime configuration as a mounted YAML file plus a small set of non-secret
environment coordinates. No secret value is carried in the image, the template,
or the environment — the confidential credentials resolve from Key Vault via the
container's managed identity.

These checks read the tracked Bicep text only — they need no Azure or Bicep
tooling, so they run in the ordinary Python test job. The authoritative compile
+ lint of the module is the infra workflow's ``validate`` job (``az bicep
build`` under the error-level ``bicepconfig.json`` rules); a local fast-path
compile is provided here, skipped when neither CLI is present.

Detector logic lives in small pure helpers so the control tests can prove each
detector actually fires — a text-assertion gate is only meaningful if its
matchers fail on the regressions they target. Two drift guards carry the most
weight: the injected config keys must stay a subset of the SAGE core config
schema, and the injected environment-variable names must stay a subset of the
names the runtime actually reads.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
INFRA_DIR: Final[Path] = REPO_ROOT / "infra"
MAIN_BICEP: Final[Path] = INFRA_DIR / "main.bicep"
CONTAINER_APPS: Final[Path] = INFRA_DIR / "modules" / "container-apps.bicep"
FOUNDATION: Final[Path] = INFRA_DIR / "modules" / "foundation.bicep"
CONFIG_SCHEMA: Final[Path] = REPO_ROOT / "docs" / "fs" / "sage" / "sage_core_config.schema.json"

_CONTAINER_APP_TYPE: Final[str] = "Microsoft.App/containerApps"
_ROLE_ASSIGNMENT_TYPE: Final[str] = "Microsoft.Authorization/roleAssignments"

# Built-in Azure role: AcrPull (data-plane pull from a container registry). A
# fixed, public Azure constant — not an environment identity coordinate.
_ACR_PULL_ROLE: Final[str] = "7f951dda-4ed3-4680-a7ca-43fe172d538d"

# A subscription / tenant / principal id is a GUID; none may be hardcoded as an
# identity coordinate. (Role-definition GUIDs are public Azure constants and are
# allowed — this gate forbids a literal GUID only where an identity coordinate
# belongs.)
_GUID_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b"
)

# Substrings that betray a secret value materialized inline rather than referenced.
_SECRET_VALUE_TOKENS: Final[tuple[str, ...]] = (
    "listkeys",
    "sharedkey",
    "primarykey",
    "administratorloginpassword",
    "adminpassword",
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


def _injected_env_names(text: str) -> set[str]:
    """Names of the environment variables the module injects into the containers.

    Container-app env entries read ``{ name: 'SAGE_KEY_VAULT_URI', value: ... }``;
    every injected env name is upper-snake-case, which no resource/secret/volume
    name (lower-kebab) matches — so an upper-snake ``name:`` literal is uniquely an
    env-var name.
    """
    return set(re.findall(r"name:\s*'([A-Z][A-Z0-9_]+)'", _strip_line_comments(text)))


def _config_keys(text: str) -> set[str]:
    """Config keys the module writes into the assembled cloud-config YAML.

    The cloud config is assembled as an array of single-quoted YAML lines joined
    with newlines; each ``'<indent>key: ...'`` line names one config key. The
    leading quote distinguishes a config key from a Bicep property assignment
    (whose key is unquoted).
    """
    return set(re.findall(r"^\s*'\s*([a-z_]+):", _strip_line_comments(text), re.MULTILINE))


def _inline_secret_violations(text: str) -> list[str]:
    """Return env/secret ``value:`` lines that materialize a secret expression.

    A secret must be referenced (``secretRef`` for env, ``keyVaultUrl`` for an ACA
    secret), never read inline. This flags a ``value:`` whose right-hand side names
    a key/password-extraction function.
    """
    violations: list[str] = []
    for line in _strip_line_comments(text).splitlines():
        m = re.search(r"value:\s*(.+)$", line)
        if not m:
            continue
        lowered = m.group(1).lower()
        for token in _SECRET_VALUE_TOKENS:
            if token in lowered:
                violations.append(line.strip())
    return violations


def _schema_property_names(schema: dict) -> set[str]:
    """All property names declared anywhere in the JSON Schema (any nesting)."""
    names: set[str] = set()

    def walk(node: dict) -> None:
        props = node.get("properties")
        if isinstance(props, dict):
            for key, value in props.items():
                names.add(key)
                if isinstance(value, dict):
                    walk(value)

    walk(schema)
    return names


# ---------------------------------------------------------------------------
# Structural / posture gates
# ---------------------------------------------------------------------------


def test_container_apps_module_exists() -> None:
    """The container-apps module the orchestrator wires must exist."""
    assert CONTAINER_APPS.is_file(), "infra/modules/container-apps.bicep missing"


def test_declares_two_container_apps() -> None:
    """The module declares exactly two container apps — SAGE and the CAS BFF."""
    count = _count_resource_type(CONTAINER_APPS.read_text(encoding="utf-8"), _CONTAINER_APP_TYPE)
    assert count == 2, f"expected exactly 2 {_CONTAINER_APP_TYPE} (SAGE + BFF); found {count}"


def test_images_pinned_to_immutable_tag() -> None:
    """Each image is pinned to the immutable ``{registry}/{repo}:{tag}`` form built
    from the ACR login server and the deploy-time image tag — never ``:latest``.
    """
    text = _strip_line_comments(CONTAINER_APPS.read_text(encoding="utf-8"))
    image_lines = [m.group(0) for m in re.finditer(r"image:\s*'[^']*'", text)]
    assert image_lines, "no container `image:` declaration found"
    for line in image_lines:
        assert ":latest'" not in line, f"image must not use the mutable :latest tag: {line}"
        assert "${imageTag}" in line and "${acrLoginServer}" in line, (
            f"image must interpolate the ACR login server and the immutable tag: {line}"
        )


def test_registry_pull_authenticates_by_identity() -> None:
    """The registry pull authenticates by managed identity — a ``registries`` block
    binding the app identity, never a username/password credential.
    """
    text = _strip_line_comments(CONTAINER_APPS.read_text(encoding="utf-8"))
    assert re.search(r"registries:\s*\[", text), "each app must declare a registries block"
    assert re.search(r"server:\s*acrLoginServer", text), (
        "the registries block must point at the ACR login server"
    )
    assert "IdentityId" in text, "the registries block must authenticate by the app's identity"
    assert "passwordSecretRef" not in text and "username:" not in text, (
        "the registry pull must use managed identity, not a stored credential"
    )


def test_acrpull_role_assignments_present() -> None:
    """Each app identity is granted ``AcrPull`` on the registry through a role
    assignment, mirroring the Key Vault module's grant pattern.
    """
    text = CONTAINER_APPS.read_text(encoding="utf-8")
    count = _count_resource_type(text, _ROLE_ASSIGNMENT_TYPE)
    assert count >= 2, f"expected >=2 AcrPull role assignments (SAGE + BFF); found {count}"
    assert _ACR_PULL_ROLE in text, "a role assignment must reference the AcrPull role id"
    stripped = _strip_line_comments(text)
    assert re.search(r"principalType:\s*'ServicePrincipal'", stripped), (
        "AcrPull assignments must set principalType: 'ServicePrincipal'"
    )
    bound = [m.group(1) for m in re.finditer(r"principalId:\s*(\S+)", stripped)]
    assert "sageIdentityPrincipalId" in bound, "SAGE identity must be granted AcrPull"
    assert "bffIdentityPrincipalId" in bound, "BFF identity must be granted AcrPull"
    literal = [v for v in bound if _GUID_RE.search(v)]
    assert not literal, f"principalId must come from a parameter, not a literal GUID: {literal}"


def test_sage_ingress_is_external_on_8000() -> None:
    """SAGE takes external container ingress on its service port 8000 (the APIM
    facade routes to its resulting FQDN).
    """
    text = _strip_line_comments(CONTAINER_APPS.read_text(encoding="utf-8"))
    assert re.search(r"targetPort:\s*8000", text), "SAGE ingress must target port 8000"
    assert re.search(r"external:\s*true", text), "the apps must take external ingress"


def test_bff_ingress_binds_custom_domain() -> None:
    """The BFF takes external container ingress on port 8001 and attaches its custom
    domain via the environment certificate the custom-domains module produced.
    """
    text = _strip_line_comments(CONTAINER_APPS.read_text(encoding="utf-8"))
    assert re.search(r"targetPort:\s*8001", text), "BFF ingress must target port 8001"
    assert re.search(r"customDomains:\s*\[", text), "BFF ingress must declare customDomains"
    assert "casCertificateId" in text, "BFF custom domain must bind casCertificateId"
    assert "casHostname" in text, "BFF custom domain must bind the cas hostname"


def test_sage_injects_its_runtime_coordinates() -> None:
    """SAGE receives its config-path, Key Vault URI, and managed-identity client id;
    the schema-keyed coordinates (profile, Postgres, audience) ride in the mounted
    config, not the environment.
    """
    names = _injected_env_names(CONTAINER_APPS.read_text(encoding="utf-8"))
    for required in ("SAGE_CONFIG_PATH", "SAGE_KEY_VAULT_URI", "AZURE_CLIENT_ID"):
        assert required in names, f"SAGE must receive {required}; have {sorted(names)}"
    text = _strip_line_comments(CONTAINER_APPS.read_text(encoding="utf-8"))
    assert "keyVaultUri" in text, "SAGE_KEY_VAULT_URI must be bound to the keyVaultUri param"


def test_sage_config_selects_document_store_vault_source() -> None:
    """The assembled SAGE cloud config selects the document-store vault-source
    binding and carries the SharePoint coordinates threaded from the module params
    (CAS-ADR-043), so a cloud vault's declaration is durable across a restart.

    Anti-coincidental-pass: assert both the ``document_store`` selection *and* the
    coordinate block bound to the params — a config that flipped the selector but
    omitted the block (or vice versa) would leave the binding unconfigured.
    """
    keys = _config_keys(CONTAINER_APPS.read_text(encoding="utf-8"))
    assert "document_store" in keys, "the SAGE config must carry a document_store block"
    text = _strip_line_comments(CONTAINER_APPS.read_text(encoding="utf-8"))
    assert re.search(r"'vault_source_backend:\s*document_store'", text), (
        "the SAGE config must select vault_source_backend: document_store"
    )
    assert "${sharepointSiteId}" in text, "site_id must bind the sharepointSiteId param"
    assert "${sharepointDriveId}" in text, "drive_id must bind the sharepointDriveId param"
    assert "${vaultSourceRootPath}" in text, "root_path must bind the vaultSourceRootPath param"


def test_main_threads_sharepoint_coordinates_into_container_apps() -> None:
    """main.bicep wires the SharePoint coordinate params into the container-apps
    module, so the single source of each coordinate flows end-to-end.
    """
    text = _strip_line_comments(MAIN_BICEP.read_text(encoding="utf-8"))
    for param in ("sharepointSiteId", "sharepointDriveId", "vaultSourceRootPath"):
        assert re.search(rf"{param}:\s*{param}", text), (
            f"main.bicep must thread {param} into the container-apps module"
        )


def test_bff_injects_its_runtime_coordinates() -> None:
    """The BFF receives its Entra client coordinates and the SAGE upstream; its
    confidential client secret is a Key Vault reference, never an inline value.
    """
    text = CONTAINER_APPS.read_text(encoding="utf-8")
    names = _injected_env_names(text)
    for required in (
        "CAS_BFF_TENANT_ID",
        "CAS_BFF_CLIENT_ID",
        "CAS_BFF_SAGE_APP_ID_URI",
        "CAS_BFF_SAGE_BASE_URL",
    ):
        assert required in names, f"BFF must receive {required}; have {sorted(names)}"
    stripped = _strip_line_comments(text)
    secret_env = re.search(r"name:\s*'CAS_BFF_CLIENT_SECRET'\s*\n?\s*secretRef:", stripped)
    assert secret_env, "CAS_BFF_CLIENT_SECRET must be injected as a secretRef, not a value"


def test_no_secret_value_materialized_inline() -> None:
    """No secret is read inline (a listKeys/key expression on a ``value:``); secrets
    are referenced (secretRef for env, keyVaultUrl for an ACA secret).
    """
    violations = _inline_secret_violations(CONTAINER_APPS.read_text(encoding="utf-8"))
    assert not violations, f"inline secret expressions: {violations}"
    text = _strip_line_comments(CONTAINER_APPS.read_text(encoding="utf-8"))
    assert "keyVaultUrl:" in text, (
        "the BFF client secret must be sourced from Key Vault (keyVaultUrl reference)"
    )


def test_bff_client_secret_url_built_from_param() -> None:
    """The BFF client-secret Key Vault URL is assembled from a ``bffClientSecretName``
    parameter (single-sourced by the keyvault module), not a hardcoded literal name —
    so the name the operator load step must match lives in exactly one place. The
    ACA-internal secret ``name``/``secretRef`` may stay local literals; only the Key
    Vault URL must be param-built.
    """
    text = _strip_line_comments(CONTAINER_APPS.read_text(encoding="utf-8"))
    assert re.search(r"param\s+bffClientSecretName\s+string", text), (
        "container-apps.bicep must take a `bffClientSecretName` string parameter"
    )
    uri = re.search(r"bffClientSecretUri\s*=\s*'([^']*)'", text)
    assert uri, "expected a bffClientSecretUri assignment building the Key Vault URL"
    rhs = uri.group(1)
    assert "${bffClientSecretName}" in rhs, (
        f"the BFF client-secret Key Vault URL must interpolate the param; got {rhs!r}"
    )
    assert "secrets/bff-client-secret" not in rhs, (
        f"the BFF client-secret Key Vault URL must not hardcode the secret name; got {rhs!r}"
    )


def test_main_threads_bff_client_secret_name() -> None:
    """The orchestrator threads the keyvault module's ``bffClientSecretName`` output
    into the container-apps module, so the single source of the secret name flows
    end-to-end.
    """
    text = _strip_line_comments(MAIN_BICEP.read_text(encoding="utf-8"))
    assert re.search(r"bffClientSecretName:\s*keyvault\.outputs\.bffClientSecretName", text), (
        "main.bicep must thread keyvault.outputs.bffClientSecretName into container-apps"
    )


def test_outputs_expose_sage_fqdn_and_no_secrets() -> None:
    """The module exposes SAGE's container-app FQDN (the value the APIM backend
    resolves from) and leaks no secret or literal identity GUID through an output.
    """
    outputs = _output_lines(CONTAINER_APPS.read_text(encoding="utf-8"))
    names = [n for n, _ in outputs]
    assert any("sage" in n.lower() and "fqdn" in n.lower() for n in names), (
        f"missing a SAGE FQDN output; have {names}"
    )
    for name, rhs in outputs:
        lowered = rhs.lower()
        assert not any(tok in lowered for tok in _SECRET_VALUE_TOKENS), (
            f"output {name} exposes a secret expression: {rhs}"
        )
        assert not _GUID_RE.search(rhs), f"output {name} exposes a literal GUID: {rhs}"


def test_no_hardcoded_identity_guid() -> None:
    """No subscription/tenant/principal GUID is baked into the module — identity
    coordinates arrive as parameters. The only literal GUID allowed is the public
    AcrPull role-definition constant.
    """
    text = _strip_line_comments(CONTAINER_APPS.read_text(encoding="utf-8"))
    found = set(_GUID_RE.findall(text))
    unexpected = found - {_ACR_PULL_ROLE}
    assert not unexpected, (
        f"unexpected literal GUID(s) (only the AcrPull role id is allowed): {unexpected}"
    )


def test_module_is_resource_group_scoped() -> None:
    """The module is resource-group scoped (the Bicep default); the orchestrator
    deploys it with ``scope: rg``.
    """
    text = _strip_line_comments(CONTAINER_APPS.read_text(encoding="utf-8"))
    assert not re.search(r"targetScope\s*=\s*'(subscription|managementGroup|tenant)'", text), (
        "container-apps.bicep is a resource-group module; it must not retarget the scope"
    )


# ---------------------------------------------------------------------------
# Drift guards
# ---------------------------------------------------------------------------


def test_injected_config_keys_subset_of_schema() -> None:
    """DRIFT GUARD — every config key the module writes into the mounted cloud
    config is a key the SAGE core config schema defines. A typo or a key the schema
    dropped/renamed turns this red rather than shipping a config the runtime rejects.
    """
    written = _config_keys(CONTAINER_APPS.read_text(encoding="utf-8"))
    assert written, "no assembled cloud-config keys found in the module"
    schema = json.loads(CONFIG_SCHEMA.read_text(encoding="utf-8"))
    allowed = _schema_property_names(schema)
    drift = written - allowed
    assert not drift, (
        f"config keys not in {CONFIG_SCHEMA.name}: {sorted(drift)} (schema keys: {sorted(allowed)})"
    )


def test_injected_env_names_subset_of_runtime_contract() -> None:
    """DRIFT GUARD — every environment-variable name the module injects is a name
    the runtime actually reads. Catches an injected-name typo (e.g. ``SAGE_KEYVAULT_URI``)
    that would silently leave the coordinate unread.
    """
    from app.backend.auth.config import (
        _AUTHORITY_HOST_ENV,
        _CLIENT_ID_ENV,
        _CLIENT_SECRET_ENV,
        _POST_LOGIN_REDIRECT_ENV,
        _SAGE_APP_ID_URI_ENV,
        _SAGE_BASE_URL_ENV,
        _TENANT_ENV,
    )

    # SAGE-side coordinates the cloud profile reads:
    #   SAGE_CONFIG_PATH   -> sage/mcp_init.py (_STACK_CONFIG_PATH_ENV)
    #   SAGE_KEY_VAULT_URI -> sage/secrets/key_vault.py
    #   AZURE_CLIENT_ID    -> DefaultAzureCredential (azure-identity)
    #   SAGE_VAULT_ROOT    -> sage vault discovery root
    sage_runtime_env = {
        "SAGE_CONFIG_PATH",
        "SAGE_KEY_VAULT_URI",
        "AZURE_CLIENT_ID",
        "SAGE_VAULT_ROOT",
    }
    bff_runtime_env = {
        _TENANT_ENV,
        _CLIENT_ID_ENV,
        _CLIENT_SECRET_ENV,
        _SAGE_APP_ID_URI_ENV,
        _AUTHORITY_HOST_ENV,
        _POST_LOGIN_REDIRECT_ENV,
        _SAGE_BASE_URL_ENV,
    }
    runtime_contract = sage_runtime_env | bff_runtime_env
    injected = _injected_env_names(CONTAINER_APPS.read_text(encoding="utf-8"))
    drift = injected - runtime_contract
    assert not drift, (
        f"injected env names the runtime does not read: {sorted(drift)} "
        f"(runtime contract: {sorted(runtime_contract)})"
    )


# ---------------------------------------------------------------------------
# Orchestrator wiring
# ---------------------------------------------------------------------------


def test_main_wires_container_apps_and_resolves_apim_backend() -> None:
    """The orchestrator wires the module live (scoped to rg), resolves the APIM
    backend from the SAGE container-app FQDN rather than a hand-substituted
    placeholder param, and exposes that FQDN as an orchestrator output.
    """
    text = _strip_line_comments(MAIN_BICEP.read_text(encoding="utf-8"))
    assert re.search(r"module\s+\w+\s+'modules/container-apps\.bicep'\s*=", text), (
        "main.bicep must wire a live module from modules/container-apps.bicep"
    )
    assert re.search(r"sageBackendHostname:\s*containerApps\.outputs\.\w+", text), (
        "apim's sageBackendHostname must resolve from the container-apps SAGE FQDN output"
    )
    assert not re.search(r"param\s+sageBackendHostname\s+string", text), (
        "the hand-substituted sageBackendHostname param must be gone"
    )
    assert re.search(r"output\s+\w*[Ss]age\w*[Ff]qdn\w*\s+string", text), (
        "main.bicep must expose the SAGE container-app FQDN as an output"
    )


def test_foundation_exposes_acr_name() -> None:
    """The foundation module exposes the ACR name so the container-apps module can
    reference the registry (existing) to scope its AcrPull grants.
    """
    names = [n for n, _ in _output_lines(FOUNDATION.read_text(encoding="utf-8"))]
    assert any(n.lower() == "acrname" for n in names), (
        f"foundation.bicep must output acrName; have {names}"
    )


@pytest.mark.skipif(
    shutil.which("bicep") is None and shutil.which("az") is None,
    reason="bicep/az CLI absent; the infra workflow validate job is authoritative",
)
def test_main_bicep_compiles(tmp_path: Path) -> None:
    """The orchestrator (which reaches this module) compiles to ARM JSON with no
    error (local fast check; the infra workflow validate job is authoritative).
    """
    outfile = tmp_path / "main.json"
    if shutil.which("bicep") is not None:
        cmd = ["bicep", "build", str(MAIN_BICEP), "--outfile", str(outfile)]
    else:
        cmd = ["az", "bicep", "build", "--file", str(MAIN_BICEP), "--outfile", str(outfile)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"bicep build failed:\n{proc.stderr}"


# ---------------------------------------------------------------------------
# Anti-coincidental-pass controls
#
# These verify the detectors above actually fire on the regressions they
# target, NOT that any specific module text is clean. Without them, a broken
# regex would let every structural gate pass coincidentally.
# ---------------------------------------------------------------------------


def test_container_app_count_detector_controls() -> None:
    """``_count_resource_type`` counts container-app declarations exactly."""
    one = "resource a 'Microsoft.App/containerApps@2024-03-01' = {}\n"
    three = one * 3
    assert _count_resource_type(one, _CONTAINER_APP_TYPE) == 1
    assert _count_resource_type(three, _CONTAINER_APP_TYPE) == 3
    assert (
        _count_resource_type(
            "resource v 'Microsoft.App/managedEnvironments@2024-03-01' = {}", _CONTAINER_APP_TYPE
        )
        == 0
    )


def test_image_tag_detector_controls() -> None:
    """A ``:latest`` image is caught; an interpolated immutable tag passes."""
    latest = "image: '${acrLoginServer}/sage:latest'"
    pinned = "image: '${acrLoginServer}/sage:${imageTag}'"
    assert ":latest'" in latest and ":latest'" not in pinned
    assert "${imageTag}" in pinned and "${imageTag}" not in latest


def test_env_name_detector_controls() -> None:
    """``_injected_env_names`` picks up upper-snake env names, ignores resource names."""
    text = "name: 'SAGE_KEY_VAULT_URI'\nname: 'cas-bff'\nname: 'sage-cloud-config'\n"
    assert _injected_env_names(text) == {"SAGE_KEY_VAULT_URI"}


def test_inline_secret_detector_controls() -> None:
    """The inline-secret scan flags a listKeys ``value:``, passes a secretRef."""
    leak = "value: keyVault.listKeys().value"
    referenced = "secretRef: 'bff-client-secret'"
    assert _inline_secret_violations(leak), "inline-secret detector failed to flag a listKeys value"
    assert not _inline_secret_violations(referenced), (
        "inline-secret detector false-positived on a secretRef"
    )


def test_config_key_drift_detector_controls() -> None:
    """The config-key extractor + subset check fire on a stale/typo'd config key."""
    sample = "  '  database: ${db}'\n  '  databse: ${db}'\n"
    keys = _config_keys(sample)
    assert "database" in keys and "databse" in keys
    schema = json.loads(CONFIG_SCHEMA.read_text(encoding="utf-8"))
    allowed = _schema_property_names(schema)
    assert "database" in allowed, "expected the real key to be a schema property"
    assert "databse" not in allowed, "the typo'd key must not be a schema property"
    assert keys - allowed == {"databse"}, "the subset check must isolate the drifted key"


def test_env_name_drift_detector_controls() -> None:
    """A misspelled injected env name is isolated by the subset check."""
    injected = {"SAGE_KEY_VAULT_URI", "SAGE_KEYVAULT_URI"}
    runtime_contract = {"SAGE_KEY_VAULT_URI", "AZURE_CLIENT_ID"}
    assert injected - runtime_contract == {"SAGE_KEYVAULT_URI"}
