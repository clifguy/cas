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


def _resource_blocks(text: str, resource_type: str) -> list[str]:
    """Return the body of every ``resource <symbol> '<resource_type>@…'`` block.

    Keyed by resource *type* rather than by symbol, so a symbol rename cannot
    silently blank the list and pass a gate vacuously. Each body runs to the next
    top-level declaration. The module declares one grant per app identity, so a
    posture asserted over the whole module is satisfied when only one of them
    carries it; the grant gate below must read each assignment's own body.
    """
    stripped = _strip_line_comments(text)
    pattern = re.compile(
        r"^resource\s+\w+\s+'" + re.escape(resource_type) + r"@[0-9A-Za-z-]+'", re.MULTILINE
    )
    blocks: list[str] = []
    for m in pattern.finditer(stripped):
        rest = stripped[m.end() :]
        nxt = re.search(r"^(?:@|resource|output|module|param|var)\s*\w*", rest, re.MULTILINE)
        blocks.append(rest[: nxt.start()] if nxt else rest)
    return blocks


def _module_block(text: str, module_path: str) -> str:
    """Return the body of the ``module <symbol> '<module_path>' = {...}`` call.

    Slices from the module declaration to the next top-level declaration. The
    orchestrator wires nine modules and hands several of them the same
    parameters — the SharePoint coordinates go to the maintenance job as well —
    so an assertion made over the whole file is satisfied by a neighbour; the
    orchestrator-wiring gates below must read one module's own call body.
    """
    stripped = _strip_line_comments(text)
    start = re.search(
        r"^module\s+\w+\s+'" + re.escape(module_path) + r"'\s*=", stripped, re.MULTILINE
    )
    if start is None:
        return ""
    rest = stripped[start.end() :]
    nxt = re.search(r"^(?:@|resource|output|module|param|var)\s*\w*", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


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


def _sage_app_block(text: str) -> str:
    """Return the text of the ``sageApp`` container-app declaration only.

    Scopes a resource assertion to SAGE without matching the sibling BFF app: the
    block runs from ``resource sageApp`` up to the next top-level resource
    declaration (``resource bffApp ...``), or to end-of-text if SAGE is last.
    """
    bounded = re.search(r"resource\s+sageApp\b.*?(?=\nresource\s+\w+\s+')", text, re.DOTALL)
    if bounded:
        return bounded.group(0)
    tail = re.search(r"resource\s+sageApp\b.*", text, re.DOTALL)
    return tail.group(0) if tail else ""


def _container_cpu_memory(block: str) -> tuple[float | None, float | None]:
    """Parse ``(cpu_vcpu, memory_gib)`` from a container ``resources`` block.

    ACA expresses cpu as ``cpu: json('2.0')`` (Bicep has no fractional number
    literal) and memory as the quantity string ``memory: '4Gi'``. Either element
    is ``None`` when its key is absent.
    """
    cpu_m = re.search(r"cpu:\s*json\('([\d.]+)'\)", block) or re.search(r"cpu:\s*([\d.]+)", block)
    mem_m = re.search(r"memory:\s*'([\d.]+)\s*Gi'", block)
    cpu = float(cpu_m.group(1)) if cpu_m else None
    mem = float(mem_m.group(1)) if mem_m else None
    return cpu, mem


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

    Asserted on every container app, as the message says. The module declares two
    with identical shapes, so read once over the module each check is satisfied by
    whichever app still has it: either could lose its whole ``registries`` block
    with the module still compiling and every gate green, and the image pull would
    fail only at deploy. The credential absences stay module-wide — a stored
    credential is wrong wherever it appears.
    """
    text = _strip_line_comments(CONTAINER_APPS.read_text(encoding="utf-8"))
    apps = _resource_blocks(text, _CONTAINER_APP_TYPE)
    assert len(apps) >= 2, f"expected >=2 container apps (SAGE + BFF); found {len(apps)}"
    for i, app in enumerate(apps):
        assert re.search(r"registries:\s*\[", app), f"app {i} must declare a registries block"
        assert re.search(r"server:\s*acrLoginServer", app), (
            f"app {i}'s registries block must point at the ACR login server"
        )
        assert re.search(r"identity:\s*\w*IdentityId\b", app), (
            f"app {i}'s registries block must authenticate by that app's identity"
        )
    assert "passwordSecretRef" not in text and "username:" not in text, (
        "the registry pull must use managed identity, not a stored credential"
    )


def test_acrpull_role_assignments_present() -> None:
    """Each app identity is granted ``AcrPull`` on the registry through a role
    assignment, mirroring the Key Vault module's grant pattern.

    Both the role and ``principalType`` are asserted per assignment. Read once over
    the whole module, the role is satisfied by the ``var acrPullRoleId`` declaration
    whether or not any grant binds it, and one assignment's ``principalType``
    satisfies the check for every other — so a grant could lose either and leave an
    identity unable to pull its image, which surfaces at deploy time rather than to
    any source gate.
    """
    text = CONTAINER_APPS.read_text(encoding="utf-8")
    count = _count_resource_type(text, _ROLE_ASSIGNMENT_TYPE)
    assert count >= 2, f"expected >=2 AcrPull role assignments (SAGE + BFF); found {count}"
    stripped = _strip_line_comments(text)
    blocks = _resource_blocks(text, _ROLE_ASSIGNMENT_TYPE)
    assert len(blocks) == count, "the role-assignment slicer lost a declaration"
    role_var = re.search(rf"var\s+(\w+)\s*=\s*'{re.escape(_ACR_PULL_ROLE)}'", stripped)
    assert role_var, f"the module must declare the AcrPull role id {_ACR_PULL_ROLE}"
    unbound = [
        b for b in blocks if not re.search(rf"roleDefinitionId:.*\b{role_var.group(1)}\b", b)
    ]
    assert not unbound, (
        f"every AcrPull assignment must bind the AcrPull role id to its roleDefinitionId; "
        f"{len(unbound)} of {count} do not"
    )
    missing = [b for b in blocks if not re.search(r"principalType:\s*'ServicePrincipal'", b)]
    assert not missing, (
        f"every AcrPull assignment must set principalType: 'ServicePrincipal'; "
        f"{len(missing)} of {count} do not"
    )
    bound = [m.group(1) for m in re.finditer(r"principalId:\s*(\S+)", stripped)]
    assert "sageIdentityPrincipalId" in bound, "SAGE identity must be granted AcrPull"
    assert "bffIdentityPrincipalId" in bound, "BFF identity must be granted AcrPull"
    literal = [v for v in bound if _GUID_RE.search(v)]
    assert not literal, f"principalId must come from a parameter, not a literal GUID: {literal}"


def test_sage_ingress_is_external_on_8000() -> None:
    """SAGE takes external container ingress on its service port 8000 (the APIM
    facade routes to its resulting FQDN).

    ``external`` is asserted on every app, as the message says: read once over a
    module declaring two, the check is satisfied by whichever app still has it, so
    either could fall to internal ingress with every gate green. SAGE's port is
    read from SAGE's own block rather than anywhere in the module.
    """
    text = _strip_line_comments(CONTAINER_APPS.read_text(encoding="utf-8"))
    assert re.search(r"targetPort:\s*8000", _sage_app_block(text)), (
        "SAGE ingress must target port 8000"
    )
    apps = _resource_blocks(text, _CONTAINER_APP_TYPE)
    assert len(apps) >= 2, f"expected >=2 container apps (SAGE + BFF); found {len(apps)}"
    internal = [i for i, app in enumerate(apps) if not re.search(r"external:\s*true", app)]
    assert not internal, (
        f"every app must take external ingress; app(s) {internal} of {len(apps)} do not"
    )


def test_bff_ingress_binds_custom_domain() -> None:
    """The BFF takes external container ingress on port 8001 and attaches its custom
    domain via the environment certificate the custom-domains module produced.
    """
    text = _strip_line_comments(CONTAINER_APPS.read_text(encoding="utf-8"))
    assert re.search(r"targetPort:\s*8001", text), "BFF ingress must target port 8001"
    assert re.search(r"customDomains:\s*\[", text), "BFF ingress must declare customDomains"
    assert "casCertificateId" in text, "BFF custom domain must bind casCertificateId"
    assert "casHostname" in text, "BFF custom domain must bind the cas hostname"


def test_apps_pin_a_warm_minimum_replica() -> None:
    """Both container apps pin a warm minimum replica via a parameterized
    ``scale.minReplicas``, so the post-deploy preflight never probes a
    scaled-to-zero (cold-start) replica — ACA treats an unset ``minReplicas`` as 0.

    Anti-coincidental-pass: assert the ``minReplicas`` param defaults to a warm
    value (>=1) *and* that each app's scale block binds that param. A scale block
    bound to a param defaulting to 0, a hardcoded literal, or only one of the two
    apps pinned would each reopen the cold window this guards against.
    """
    text = _strip_line_comments(CONTAINER_APPS.read_text(encoding="utf-8"))
    default = re.search(r"param\s+minReplicas\s+int\s*=\s*(\d+)", text)
    assert default, "the module must declare `param minReplicas int = <default>`"
    assert int(default.group(1)) >= 1, (
        f"minReplicas must default to a warm replica (>=1), not {default.group(1)}"
    )
    assert len(re.findall(r"scale:\s*\{", text)) == 2, (
        "both apps (SAGE + BFF) must declare a scale block in their template"
    )
    assert len(re.findall(r"minReplicas:\s*minReplicas", text)) == 2, (
        "both scale blocks must bind minReplicas to the module param, not a literal"
    )


def test_sage_container_right_sized_for_embedding_model_load() -> None:
    """SAGE declares an explicit container ``resources`` block sized to load the
    embedding model at startup without an OOM SIGKILL — 2.0 vCPU / 4.0 GiB, a
    valid ACA Consumption combo. An unset resources block falls back to the ACA
    default 0.5 vCPU / 1 GiB, at which the model load (lazy, during lifespan
    startup) is OOM-killed (exit 137) and startup never completes (CAS-ADR-042).

    Anti-coincidental-pass: assert the block exists *and* parses to the 2.0/4.0
    floor. A module with no resources block fails on the missing block; a block
    re-stating the 0.5/1Gi default, or sized on only one axis, would reopen the
    OOM crash-loop and is rejected by the value assertions.
    """
    block = _sage_app_block(_strip_line_comments(CONTAINER_APPS.read_text(encoding="utf-8")))
    assert block, "could not isolate the sageApp resource declaration"
    assert "resources:" in block, (
        "SAGE container must declare an explicit resources block; ACA defaults to "
        "0.5 vCPU / 1 GiB, which OOM-kills the embedding-model load at startup"
    )
    cpu, memory = _container_cpu_memory(block)
    assert cpu == 2.0, f"SAGE container cpu must be 2.0 vCPU; got {cpu}"
    assert memory == 4.0, f"SAGE container memory must be 4 GiB; got {memory}"


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

    Read within this module's own call body: the maintenance job is handed all
    three coordinates identically, so a whole-file search stays green even when
    the container-apps call has dropped them.
    """
    block = _module_block(MAIN_BICEP.read_text(encoding="utf-8"), "modules/container-apps.bicep")
    assert block, "main.bicep must wire a live module from modules/container-apps.bicep"
    for param in ("sharepointSiteId", "sharepointDriveId", "vaultSourceRootPath"):
        assert re.search(rf"{param}:\s*{param}", block), (
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
    end-to-end. Read within this module's own call body, so the assertion cannot
    be satisfied by another module picking the output up later.
    """
    block = _module_block(MAIN_BICEP.read_text(encoding="utf-8"), "modules/container-apps.bicep")
    assert block, "main.bicep must wire a live module from modules/container-apps.bicep"
    assert re.search(r"bffClientSecretName:\s*keyvault\.outputs\.bffClientSecretName", block), (
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


def test_module_exports_container_app_names() -> None:
    """The module exposes both container-app resource names so the deploy pipeline
    can converge (restart) each app by name after the in-VNet bootstrap job runs.
    """
    outputs = dict(_output_lines(CONTAINER_APPS.read_text(encoding="utf-8")))
    assert "sageContainerAppName" in outputs, "module must output sageContainerAppName"
    assert "bffContainerAppName" in outputs, "module must output bffContainerAppName"
    assert outputs["sageContainerAppName"] == "sageApp.name", (
        f"sageContainerAppName must be the SAGE app name; got {outputs['sageContainerAppName']!r}"
    )
    assert outputs["bffContainerAppName"] == "bffApp.name", (
        f"bffContainerAppName must be the BFF app name; got {outputs['bffContainerAppName']!r}"
    )


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

    The backend resolution is ``apim``'s parameter, so it is read within the apim
    call body rather than anywhere in the file. The output and absent-parameter
    claims are genuinely file-level and stay that way.
    """
    text = _strip_line_comments(MAIN_BICEP.read_text(encoding="utf-8"))
    assert _module_block(text, "modules/container-apps.bicep"), (
        "main.bicep must wire a live module from modules/container-apps.bicep"
    )
    apim_block = _module_block(text, "modules/apim.bicep")
    assert apim_block, "main.bicep must wire a live module from modules/apim.bicep"
    assert re.search(r"sageBackendHostname:\s*containerApps\.outputs\.\w+", apim_block), (
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


def test_sage_app_block_detector_controls() -> None:
    """``_sage_app_block`` isolates the SAGE app and excludes the sibling BFF app."""
    sample = (
        "resource sageApp 'Microsoft.App/containerApps@2024-03-01' = {\n"
        "  cpu: json('2.0')\n"
        "  memory: '4Gi'\n"
        "}\n"
        "resource bffApp 'Microsoft.App/containerApps@2024-03-01' = {\n"
        "  cpu: json('0.5')\n"
        "  memory: '1Gi'\n"
        "}\n"
    )
    block = _sage_app_block(sample)
    assert "sageApp" in block and "bffApp" not in block
    assert _container_cpu_memory(block) == (2.0, 4.0)


def test_container_resource_parse_controls() -> None:
    """``_container_cpu_memory`` parses the ACA cpu/memory idiom; None when absent."""
    assert _container_cpu_memory("cpu: json('2.0')\nmemory: '4Gi'") == (2.0, 4.0)
    assert _container_cpu_memory("cpu: json('0.5')\nmemory: '1Gi'") == (0.5, 1.0)
    assert _container_cpu_memory("name: 'sage'") == (None, None)


def test_sage_config_carries_transfer_public_base_url() -> None:
    """The assembled SAGE cloud config declares the transfer channel's public
    base URL bound to the edge hostname param, so minted recipes embed the URL
    the caller's environment can actually reach (the APIM custom domain), not
    the container's internal FQDN.

    Anti-coincidental-pass: assert both the ``transfer`` block and the
    hostname binding -- a config carrying the block with a hardcoded or
    internal host would mint recipes whose byte legs dead-end.
    """
    raw = CONTAINER_APPS.read_text(encoding="utf-8")
    keys = _config_keys(raw)
    assert "transfer" in keys, "the SAGE config must carry a transfer block"
    # Searched in the raw text: the line-comment stripper would truncate the
    # config line at the `//` inside the URL scheme.
    assert re.search(r"'  public_base_url:\s*https://\$\{sageHostname\}'", raw), (
        "transfer.public_base_url must bind https://${sageHostname} (the edge domain)"
    )


def test_sage_scale_pinned_to_single_replica() -> None:
    """The SAGE app pins ``maxReplicas: 1``: the process holds in-memory state
    that does not survive horizontal scale (document locks, the ingestion
    queue, pending transfer tokens), so a scale-out would silently split that
    state across replicas.

    Anti-coincidental-pass: exactly one ``maxReplicas`` pin must exist -- the
    BFF externalizes its session state to the relational store and stays
    scalable, so pinning both apps (or neither) is a drift in opposite
    directions.
    """
    text = _strip_line_comments(CONTAINER_APPS.read_text(encoding="utf-8"))
    pins = re.findall(r"maxReplicas:\s*1", text)
    assert len(pins) == 1, f"exactly the SAGE app must pin maxReplicas: 1 (found {len(pins)} pins)"
    sage_block = _sage_app_block(text)
    assert re.search(r"maxReplicas:\s*1", sage_block), (
        "the maxReplicas: 1 pin must live in the SAGE app's scale block"
    )


def test_module_block_detector_controls() -> None:
    """``_module_block`` returns only the named module's own call body.

    This is what makes the orchestrator-wiring gates load-bearing. The
    maintenance job is handed the SharePoint coordinates in exactly the shape
    the container-apps call uses, so a whole-file search would be satisfied by
    that neighbour even after the container-apps call has dropped them.

    The neighbouring module is declared *after* the target: a helper that finds
    the target but never truncates would still leak it, and only this ordering
    catches that. (A contaminant placed before the target is excluded by the
    forward search alone and proves nothing.)
    """
    two_modules = (
        "module containerApps 'modules/container-apps.bicep' = {\n"
        "  params: {\n    acrName: foundation.outputs.acrName\n  }\n}\n"
        "module maintenanceJob 'modules/maintenance-job.bicep' = {\n"
        "  params: {\n    sharepointSiteId: sharepointSiteId\n  }\n}\n"
    )
    block = _module_block(two_modules, "modules/container-apps.bicep")
    assert block, "the detector must find the container-apps module call"
    assert "acrName" in block, "the block must carry the call's own parameters"
    assert "sharepointSiteId" not in block, (
        "the block must truncate at the next declaration, not borrow the maintenance "
        "job's identical coordinate threading"
    )
    assert _module_block(two_modules, "modules/absent.bicep") == ""


def test_resource_blocks_detector_controls() -> None:
    """``_resource_blocks`` returns one body per declaration of a type, each
    truncated at the next declaration.

    This is what makes the AcrPull grant gate load-bearing: the module declares
    one assignment per app identity, so a single match says nothing about the
    other, and the role id sits in a module-level ``var`` that satisfies a
    containment check whether or not any grant binds it.

    The markers live on the *second* assignment and the assertion is that the
    *first* block does not carry them. That direction is the load-bearing one: a
    helper that never truncates leaks forward, so every block runs to end of text
    and swallows each later declaration. Marking the first block and asserting the
    second is clean cannot detect that — the second block never gains the earlier
    one's content whether the helper truncates or not.
    """
    sample = (
        f"resource sageAcrPull '{_ROLE_ASSIGNMENT_TYPE}@2022-04-01' = {{\n"
        "  properties: {\n    principalId: sageIdentityPrincipalId\n  }\n}\n"
        f"resource bffAcrPull '{_ROLE_ASSIGNMENT_TYPE}@2022-04-01' = {{\n"
        "  properties: {\n"
        "    roleDefinitionId: subscriptionResourceId('x', acrPullRoleId)\n"
        "    principalType: 'ServicePrincipal'\n  }\n}\n"
    )
    blocks = _resource_blocks(sample, _ROLE_ASSIGNMENT_TYPE)
    assert len(blocks) == 2, f"expected one block per assignment; got {len(blocks)}"
    assert "sageIdentityPrincipalId" in blocks[0], "the first block must carry its own properties"
    assert "acrPullRoleId" not in blocks[0], (
        "the first block must truncate at the next declaration, not borrow the second "
        "assignment's role binding — the coincidence the per-assignment gate defeats"
    )
    assert "principalType" not in blocks[0], (
        "the first block must not borrow the second assignment's principalType"
    )
    assert "principalType" in blocks[1], "the second block must carry its own properties"
    assert _resource_blocks(sample, "Microsoft.Absent/things") == []
