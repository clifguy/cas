"""Structural and security-posture gate for the APIM facade module.

Locks the shape of ``infra/modules/apim.bicep`` and its versioned policy XML
under ``infra/policies/`` — the public edge for SAGE in the CAS cloud
deployment profile (CAS-ADR-042). The API Management service fronts SAGE's
REST and MCP surfaces: it validates Entra-issued JWTs, serves the MCP OAuth
discovery handshake the bare container ingress cannot, and validates that JWT
uniformly across every surface — the maintenance mount included. These checks
keep that contract intact as the module evolves.

The checks read the tracked Bicep and policy text only — they need no Azure or
Bicep tooling, so they run in the ordinary Python test job. The authoritative
compile + lint of the module is the infra workflow's ``validate`` job
(``az bicep build`` under the error-level ``bicepconfig.json`` rules); a local
fast-path compile is provided here, skipped when neither CLI is present.

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
APIM: Final[Path] = INFRA_DIR / "modules" / "apim.bicep"
POLICIES_DIR: Final[Path] = INFRA_DIR / "policies"

# The API Management resource and its children the facade must declare.
_APIM_SERVICE_TYPE: Final[str] = "Microsoft.ApiManagement/service"
_APIM_API_TYPE: Final[str] = "Microsoft.ApiManagement/service/apis"
_APIM_BACKEND_TYPE: Final[str] = "Microsoft.ApiManagement/service/backends"

# The stable API Management API version every resource must pin. A bare
# 2023-05-01 exists only in its ``-preview`` form (the stable form raises BCP081
# against the public type index); 2022-08-01 is the real stable version.
_APIM_STABLE_API_VERSION: Final[str] = "2022-08-01"
_APIM_NONEXISTENT_API_VERSION: Final[str] = "2023-05-01"

# The maintenance mount. It routes through the facade under the same JWT
# validation as the ordinary surface — the policy must not single it out.
_ADMIN_MOUNT: Final[str] = "/mcp_admin"

# The catch-all forwarding contract. APIM does not honor a literal ``*`` HTTP
# method via ARM/Bicep — such an operation deploys and shows in the portal but
# never matches a request, so the gateway answers its generic 404. The working
# pattern is one operation per explicit method, each with the ``/{*path}``
# wildcard template (and a declared ``path`` parameter). These are the verbs
# the facade must route; HEAD/OPTIONS may also be declared but are not required.
_EXPECTED_HTTP_METHODS: Final[tuple[str, ...]] = ("GET", "POST", "PUT", "PATCH", "DELETE")
_CATCH_ALL_URL_TEMPLATE: Final[str] = "/{*path}"

# The Entra authority host belongs in the versioned policy XML (loaded via
# loadTextContent, which the Bicep linter does not introspect), never in a
# ``.bicep`` — keeping the module clean against ``no-hardcoded-env-urls``.
_ENTRA_AUTHORITY_HOST: Final[str] = "login.microsoftonline.com"

# A subscription / tenant / client id is a GUID; none may be hardcoded into the
# module — identity coordinates arrive as deployment parameters or are derived
# from deploy-time ARM functions.
_GUID_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b"
)

# Substrings that betray a secret leaking through a module output. The APIM
# subscription/gateway keys (``listSecrets`` / ``listKeys``) are the likely
# accidental leak.
_SECRET_TOKENS: Final[tuple[str, ...]] = (
    "listkeys",
    "listsecrets",
    "primarykey",
    "secretref",
    "sharedkey",
)


# ---------------------------------------------------------------------------
# Detectors (pure text functions — exercised by the control tests below)
# ---------------------------------------------------------------------------


def _strip_line_comments(text: str) -> str:
    """Return ``text`` with ``//`` line comments removed.

    Keeps the structure checks from passing on commented-out scaffolding. The
    negative lookbehind spares the ``//`` inside a URL scheme (``https://``):
    this module's backend url is ``https://${sageBackendHostname}``, so a naive
    ``//`` strip would swallow the interpolation it is supposed to assert on.
    """
    return "\n".join(re.sub(r"(?<!:)//.*$", "", line) for line in text.splitlines())


def _declares_resource_type(text: str, resource_type: str) -> bool:
    """True iff ``text`` declares a resource of ``resource_type``.

    Matches the Bicep ``resource <symbol> '<type>@<version>'`` declaration
    form, not a bare mention in a comment or string literal.
    """
    pattern = re.compile(r"resource\s+\w+\s+'" + re.escape(resource_type) + r"@[0-9A-Za-z-]+'")
    return pattern.search(_strip_line_comments(text)) is not None


def _apim_resource_api_versions(text: str) -> list[str]:
    """Return the ``@<version>`` of every ``Microsoft.ApiManagement/...`` resource
    declaration, in source order.

    Reads the comment-stripped text so a commented-out declaration does not count.
    """
    pattern = re.compile(r"resource\s+\w+\s+'Microsoft\.ApiManagement/[^']*@([0-9A-Za-z-]+)'")
    return pattern.findall(_strip_line_comments(text))


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


def _loaded_policy_paths(text: str) -> list[str]:
    """Return the ``.xml`` paths the module loads via ``loadTextContent``."""
    pattern = re.compile(r"loadTextContent\(\s*'([^']+\.xml)'")
    return pattern.findall(_strip_line_comments(text))


def _hardcoded_https_hosts(text: str) -> list[str]:
    """Return every ``https://`` URL whose host is a literal (not a ``${...}``
    interpolation). A parameterized backend (``https://${sageBackendHostname}``)
    is clean; a concrete host is a hardcoded-endpoint smell.
    """
    return re.findall(r"https://(?!\$\{)[^'\"\s]+", _strip_line_comments(text))


def _policy_special_cases_path(policy: str, path: str) -> bool:
    """True iff a ``<when>`` branch condition singles out ``path``.

    A path is special-cased when it is intercepted by its own ``<when>`` branch
    before reaching the ``<otherwise>`` (JWT-validate + route-to-backend) branch
    — the shape of the removed maintenance-mount deny. A path handled uniformly
    by ``<otherwise>`` does not appear in any branch condition.
    """
    return bool(re.search(r"<when[^>]*condition=[^>]*" + re.escape(path), policy))


def _policy_text() -> str:
    """Concatenated text of every policy XML the module loads.

    Loaded paths are resolved relative to the module file (the same base
    ``loadTextContent`` uses), so the gate reads exactly what compiles in.
    """
    module_text = APIM.read_text(encoding="utf-8")
    parts: list[str] = []
    for rel in _loaded_policy_paths(module_text):
        parts.append((APIM.parent / rel).resolve().read_text(encoding="utf-8"))
    return "\n".join(parts)


def _strip_xml_comments(text: str) -> str:
    """Return ``text`` with XML ``<!-- ... -->`` comments removed.

    The fragility gate scans live policy attributes only; an explanatory comment
    that *names* the discouraged single-quoted encoding (the inbound policy's own
    comment does) must not trip it.
    """
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


# A single-quote-delimited attribute whose value carries a literal inner double
# quote — ``name='...".."...'``. This is the encoding that does not survive the
# loadTextContent -> ARM -> APIM round-trip: APIM normalizes the attribute
# delimiters to double quotes and the now-unescaped inner quotes corrupt the
# value. The robust form is a double-quoted attribute with the inner quotes
# escaped as ``&quot;``.
_FRAGILE_SINGLE_QUOTED_ATTR_RE: Final[re.Pattern[str]] = re.compile(
    r"[\w:.-]+\s*=\s*'([^']*\"[^']*)'"
)


def _fragile_single_quoted_attrs(policy: str) -> list[str]:
    """Return every single-quoted attribute value carrying a literal inner double
    quote, after stripping XML comments.

    The match values are the round-trip-fragile encodings the gate forbids.
    """
    return _FRAGILE_SINGLE_QUOTED_ATTR_RE.findall(_strip_xml_comments(policy))


def _uses_literal_wildcard_method(text: str) -> bool:
    """True iff a ``method: '*'`` literal appears (the broken catch-all form).

    A loop variable (``method: method``) or an explicit verb (``method: 'GET'``)
    is the working form and must not trip this.
    """
    return "method: '*'" in _strip_line_comments(text)


def _catch_all_url_templates(text: str) -> list[str]:
    """Return every ``urlTemplate: '<value>'`` value, in source order.

    Reads the comment-stripped text so a commented-out operation does not count.
    Distinguishes the broken ``/*`` from the working ``/{*path}``.
    """
    pattern = re.compile(r"urlTemplate:\s*'([^']*)'")
    return pattern.findall(_strip_line_comments(text))


def _declares_path_wildcard_param(text: str) -> bool:
    """True iff a template parameter named ``path`` is declared.

    The ``/{*path}`` wildcard template requires the matching ``path`` parameter;
    an empty ``templateParameters: []`` is the broken form.
    """
    return re.search(r"name:\s*'path'", _strip_line_comments(text)) is not None


def _operation_method_values(text: str) -> set[str]:
    """Return the HTTP method literals the catch-all operations route.

    Reads the ``[...]`` array assigned to a ``var`` whose name contains
    ``method`` (the per-method loop source), falling back to bare ``method:``
    literals if the loop form is not used. Comment-stripped so a commented
    declaration does not count.
    """
    stripped = _strip_line_comments(text)
    array = re.search(r"var\s+\w*[Mm]ethod\w*\s*=\s*\[([^\]]*)\]", stripped)
    if array:
        return set(re.findall(r"'([A-Za-z]+)'", array.group(1)))
    return set(re.findall(r"method:\s*'([A-Za-z]+)'", stripped))


# ---------------------------------------------------------------------------
# Structural / posture gates
# ---------------------------------------------------------------------------


def test_apim_module_exists() -> None:
    """The APIM module file the orchestrator wires must exist."""
    assert APIM.is_file(), "infra/modules/apim.bicep missing"


def test_main_bicep_wires_apim_module() -> None:
    """The orchestrator wires the APIM module live and scopes it to the rg."""
    text = _strip_line_comments(MAIN_BICEP.read_text(encoding="utf-8"))
    assert re.search(r"module\s+\w+\s+'modules/apim\.bicep'\s*=", text), (
        "main.bicep must wire a live module from modules/apim.bicep"
    )
    assert re.search(r"scope:\s*rg", text), "the apim module must be scoped to rg"


def test_apim_declares_apim_service() -> None:
    """The module declares the API Management service (the public edge)."""
    text = APIM.read_text(encoding="utf-8")
    assert _declares_resource_type(text, _APIM_SERVICE_TYPE), (
        f"apim.bicep must declare a {_APIM_SERVICE_TYPE} resource"
    )


def test_apim_declares_api_and_backend() -> None:
    """The facade declares the SAGE API definition and the backend that routes
    to the SAGE container app — the wiring acceptance criterion.
    """
    text = APIM.read_text(encoding="utf-8")
    assert _declares_resource_type(text, _APIM_API_TYPE), (
        f"apim.bicep must declare a {_APIM_API_TYPE} resource (the SAGE API)"
    )
    assert _declares_resource_type(text, _APIM_BACKEND_TYPE), (
        f"apim.bicep must declare a {_APIM_BACKEND_TYPE} resource (the SAGE backend)"
    )


def test_apim_catch_all_avoids_wildcard_method() -> None:
    """The catch-all must not use a literal ``method: '*'``. APIM does not honor a
    wildcard method via ARM — the operation deploys but never matches a request,
    so every path 404s at the gateway. Each verb must be declared explicitly.
    """
    text = APIM.read_text(encoding="utf-8")
    assert not _uses_literal_wildcard_method(text), (
        "apim.bicep must not declare a wildcard `method: '*'` operation; "
        "APIM never matches it and the gateway 404s every path"
    )


def test_apim_catch_all_uses_path_wildcard_template() -> None:
    """The catch-all operations forward every path via the ``/{*path}`` wildcard
    template — never the non-matching ``/*``.
    """
    templates = _catch_all_url_templates(APIM.read_text(encoding="utf-8"))
    assert templates, "apim.bicep must declare at least one catch-all operation urlTemplate"
    assert all(t == _CATCH_ALL_URL_TEMPLATE for t in templates), (
        f"every catch-all urlTemplate must be '{_CATCH_ALL_URL_TEMPLATE}'; found: {templates}"
    )
    assert "/*" not in templates, "the non-matching '/*' template must not appear"


def test_apim_catch_all_declares_path_parameter() -> None:
    """The ``/{*path}`` template requires its matching ``path`` template parameter;
    without it the operation fails ARM validation and does not route.
    """
    assert _declares_path_wildcard_param(APIM.read_text(encoding="utf-8")), (
        "apim.bicep must declare a templateParameters entry named 'path' for the /{*path} wildcard"
    )


def test_apim_catch_all_covers_rest_methods() -> None:
    """The catch-all routes every HTTP verb SAGE's REST and MCP surfaces use."""
    methods = _operation_method_values(APIM.read_text(encoding="utf-8"))
    missing = sorted(set(_EXPECTED_HTTP_METHODS) - methods)
    assert not missing, (
        f"the catch-all must route every required method; missing: {missing} "
        f"(found: {sorted(methods)})"
    )


def test_apim_resources_use_existing_stable_api_version() -> None:
    """Every API Management resource pins the real stable API version. The
    previously used @2023-05-01 has no stable form (only ``-preview``), so it
    raises BCP081 and must appear nowhere in the module.
    """
    text = APIM.read_text(encoding="utf-8")
    versions = _apim_resource_api_versions(text)
    assert versions, "expected at least one Microsoft.ApiManagement resource declaration"
    offenders = sorted({v for v in versions if v != _APIM_STABLE_API_VERSION})
    assert not offenders, (
        f"every APIM resource must use @{_APIM_STABLE_API_VERSION}; "
        f"found other version(s): {offenders}"
    )
    assert _APIM_NONEXISTENT_API_VERSION not in _strip_line_comments(text), (
        f"the non-existent stable @{_APIM_NONEXISTENT_API_VERSION} must not appear "
        "in apim.bicep (it raises BCP081)"
    )


def test_apim_backend_targets_parameterized_sage_host() -> None:
    """The backend routes to the SAGE container app by a parameterized hostname
    (resolved at deploy time), never a hardcoded host.
    """
    text = APIM.read_text(encoding="utf-8")
    assert re.search(r"param\s+sageBackendHostname\s+string", text), (
        "apim.bicep must take a `sageBackendHostname` string parameter"
    )
    assert "${sageBackendHostname}" in _strip_line_comments(text), (
        "the backend url must interpolate ${sageBackendHostname}"
    )
    assert not _hardcoded_https_hosts(text), (
        f"backend/endpoints must be parameterized; hardcoded https host(s): "
        f"{_hardcoded_https_hosts(text)}"
    )


def test_apim_sku_is_parameterized() -> None:
    """The SKU is a parameter with an allowed set including Consumption, and the
    capacity is derived from it (Consumption requires capacity 0).
    """
    text = _strip_line_comments(APIM.read_text(encoding="utf-8"))
    assert re.search(r"param\s+apimSku\s+string", text), (
        "apim.bicep must take an `apimSku` string parameter"
    )
    assert re.search(r"@allowed\(\[[^\]]*'Consumption'[^\]]*\]\)", text, re.DOTALL), (
        "apimSku must carry an @allowed list including 'Consumption'"
    )
    assert re.search(r"apimSku\s*==\s*'Consumption'\s*\?\s*0", text), (
        "capacity must be conditional on the SKU (Consumption => 0)"
    )


def test_apim_binds_sage_custom_domain() -> None:
    """The facade binds the ``sage`` custom domain on the gateway endpoint: a
    hostnameConfigurations entry of type 'Proxy', hostName from the
    ``sageCustomDomain`` parameter, with the certificate sourced from Key Vault.
    """
    text = _strip_line_comments(APIM.read_text(encoding="utf-8"))
    assert re.search(r"param\s+sageCustomDomain\s+string", text), (
        "apim.bicep must take a `sageCustomDomain` string parameter"
    )
    assert "hostnameConfigurations" in text, (
        "apim.bicep must declare hostnameConfigurations (the custom domain binding)"
    )
    assert re.search(r"type:\s*'Proxy'", text), (
        "the hostname binding must be the gateway endpoint (type: 'Proxy')"
    )
    assert re.search(r"hostName:\s*sageCustomDomain", text), (
        "the hostname binding must use the sageCustomDomain parameter"
    )
    assert re.search(r"certificateSource:\s*'KeyVault'", text), (
        "the hostname binding must source its certificate from Key Vault"
    )


def test_apim_custom_domain_references_keyvault_cert() -> None:
    """The hostname binding's certificate is a Key Vault secret URL from a
    parameter — never a literal host, so it stays versionless and rotates.
    """
    text = _strip_line_comments(APIM.read_text(encoding="utf-8"))
    assert re.search(r"param\s+tlsCertSecretUri\s+string", text), (
        "apim.bicep must take a `tlsCertSecretUri` string parameter"
    )
    assert re.search(r"keyVaultId:\s*tlsCertSecretUri", text), (
        "the hostname binding's keyVaultId must come from the tlsCertSecretUri parameter"
    )
    assert not _hardcoded_https_hosts(text), (
        f"the cert URL must be parameterized; hardcoded https host(s): "
        f"{_hardcoded_https_hosts(text)}"
    )


def test_apim_assigns_user_assigned_identity() -> None:
    """The service carries a user-assigned managed identity (so it can read the
    certificate from Key Vault), keyed by the ``sageIdentityId`` parameter, and
    the hostname binding names that identity's client id for the Key Vault GET.
    """
    text = _strip_line_comments(APIM.read_text(encoding="utf-8"))
    assert re.search(r"param\s+sageIdentityId\s+string", text), (
        "apim.bicep must take a `sageIdentityId` string parameter"
    )
    assert re.search(r"param\s+sageIdentityClientId\s+string", text), (
        "apim.bicep must take a `sageIdentityClientId` string parameter"
    )
    assert re.search(r"type:\s*'UserAssigned'", text), (
        "the service must declare a UserAssigned managed identity"
    )
    assert "userAssignedIdentities" in text and "sageIdentityId" in text, (
        "the userAssignedIdentities map must reference the sageIdentityId parameter"
    )
    assert re.search(r"identityClientId:\s*sageIdentityClientId", text), (
        "the hostname binding must name the sageIdentityClientId for the Key Vault GET"
    )


def test_apim_loads_versioned_policy_xml() -> None:
    """APIM policies are authored as versioned XML under infra/ and loaded with
    loadTextContent — not embedded as inline Bicep string literals.
    """
    text = APIM.read_text(encoding="utf-8")
    loaded = _loaded_policy_paths(text)
    assert loaded, "apim.bicep must load policy XML via loadTextContent('...xml')"
    for rel in loaded:
        resolved = (APIM.parent / rel).resolve()
        assert resolved.is_file(), f"loaded policy file missing: {rel}"
        assert POLICIES_DIR.resolve() in resolved.parents, (
            f"policy XML must live under infra/policies/, got {rel}"
        )


def test_apim_policy_validates_jwt_issuer_and_audience() -> None:
    """The inbound policy validates the Entra JWT against the SAGE audience and
    an OpenID issuer config — the public surface rejects foreign tokens.
    """
    policy = _policy_text()
    assert "<validate-jwt" in policy, "policy must include a <validate-jwt> element"
    assert "<openid-config" in policy, (
        "validate-jwt must reference an <openid-config> (the Entra issuer metadata)"
    )
    assert "{{sage-audience}}" in policy, (
        "validate-jwt must check the {{sage-audience}} named value"
    )


def test_apim_policy_serves_oauth_discovery() -> None:
    """The policy serves the MCP OAuth discovery handshake: the
    protected-resource-metadata document and the WWW-Authenticate challenge.
    """
    policy = _policy_text()
    assert "/.well-known/oauth-protected-resource" in policy, (
        "policy must serve the /.well-known/oauth-protected-resource path"
    )
    assert "return-response" in policy, (
        "the discovery document must be served via a return-response policy"
    )
    assert "authorization_servers" in policy and "scopes_supported" in policy, (
        "the resource-metadata document must carry authorization_servers and scopes_supported"
    )
    assert "WWW-Authenticate" in policy and "resource_metadata" in policy, (
        "a 401 must carry a WWW-Authenticate challenge pointing at resource_metadata"
    )


def test_apim_policy_has_no_fragile_single_quoted_attribute() -> None:
    """No policy attribute uses single-quote delimiters around a literal inner
    double quote — the encoding the loadTextContent -> ARM -> APIM round-trip
    corrupts. That corruption silently broke the discovery-doc exemption: the
    ``Contains(...)`` argument no longer matched any path, so every request —
    including the unauthenticated discovery doc and ``/health`` — fell through to
    ``validate-jwt`` and 401'd. The robust encoding is a double-quoted attribute
    with the inner string delimiters escaped as ``&quot;``.
    """
    offenders = _fragile_single_quoted_attrs(_policy_text())
    assert not offenders, (
        "policy attributes must not use single-quote delimiters around a literal "
        "inner double quote (the IaC round-trip corrupts them); re-encode as a "
        f"double-quoted attribute with &quot;-escaped inner quotes. Offenders: {offenders}"
    )


def test_apim_policy_discovery_condition_uses_entity_escaped_quotes() -> None:
    """The discovery ``<when>`` condition matches the protected-resource path with
    ``&quot;``-escaped string delimiters inside a double-quoted attribute — the
    round-trip-safe encoding of the C# string literal (APIM policy expressions are
    C#, where string literals require double quotes).
    """
    policy = _policy_text()
    assert "Contains(&quot;/.well-known/oauth-protected-resource&quot;)" in policy, (
        "the discovery condition must wrap the path in &quot; entities — "
        "Contains(&quot;/.well-known/oauth-protected-resource&quot;) — so the C# "
        "string literal survives the loadTextContent -> ARM -> APIM round-trip"
    )


def test_apim_policy_routes_admin_mount_through_jwt() -> None:
    """The maintenance mount /mcp_admin routes through the facade under the same
    JWT validation as the ordinary surface — it is no longer denied at the edge.

    Authorization is uniform across surfaces: the policy must not intercept the
    admin mount with its own branch; it flows down the ``<otherwise>`` branch
    that validates the JWT and routes to the backend, like every other path.
    """
    policy = _policy_text()
    assert not _policy_special_cases_path(policy, _ADMIN_MOUNT), (
        f"the policy must not single out {_ADMIN_MOUNT} in a <when> branch; it "
        "routes uniformly through the JWT-validating <otherwise> branch"
    )
    assert re.search(r"set-backend-service\s+backend-id=\"sage-backend\"", policy), (
        "the <otherwise> branch must route forwarded requests to the sage-backend "
        f"(the path {_ADMIN_MOUNT} now flows down)"
    )
    # Routing stays via the existing catch-all operation + policy: the module
    # must not declare a per-path operation for the admin mount.
    module_text = _strip_line_comments(APIM.read_text(encoding="utf-8"))
    assert _ADMIN_MOUNT not in module_text, (
        f"apim.bicep must not declare a per-path operation for {_ADMIN_MOUNT}; it "
        "routes via the catch-all operation and the inbound policy"
    )


def test_apim_no_hardcoded_identity_or_env_url_in_bicep() -> None:
    """The tenant is derived from a deploy-time ARM function (not a literal
    GUID), and no Entra authority URL is baked into the Bicep — the authority
    host belongs to the versioned policy XML only.
    """
    text = APIM.read_text(encoding="utf-8")
    assert "subscription().tenantId" in _strip_line_comments(text), (
        "the tenant id must be derived from subscription().tenantId, not hardcoded"
    )
    assert not _GUID_RE.search(text), "apim.bicep must not hardcode an identity GUID"
    assert _ENTRA_AUTHORITY_HOST not in text, (
        f"the {_ENTRA_AUTHORITY_HOST} authority host must live in the policy XML, "
        "not in the Bicep (keeps no-hardcoded-env-urls clean)"
    )


def test_apim_outputs_contain_no_secrets() -> None:
    """No module output exposes an APIM key/secret or a hardcoded identity GUID —
    a local mirror of the bicep ``outputs-should-not-contain-secrets`` rule.
    """
    violations = _output_secret_violations(APIM.read_text(encoding="utf-8"))
    assert not violations, f"secret-bearing outputs: {violations}"


def test_apim_parameterizes_location() -> None:
    """Location is a parameter (not a hardcoded region) — mirrors
    ``no-hardcoded-location``.
    """
    text = APIM.read_text(encoding="utf-8")
    assert re.search(r"param\s+location\s+string", text), (
        "apim.bicep must take a `location` string parameter"
    )


def test_apim_is_not_subscription_scoped() -> None:
    """The module is resource-group scoped (the Bicep default): the orchestrator
    deploys it with ``scope: rg``, so a broader targetScope would break wiring.
    """
    text = _strip_line_comments(APIM.read_text(encoding="utf-8"))
    assert not re.search(r"targetScope\s*=\s*'subscription'", text), (
        "apim.bicep is a resource-group module; it must not target the subscription"
    )
    assert not re.search(r"targetScope\s*=\s*'(managementGroup|tenant)'", text), (
        "apim.bicep must not target the management-group or tenant scope"
    )


@pytest.mark.skipif(
    shutil.which("bicep") is None and shutil.which("az") is None,
    reason="bicep/az CLI absent; the infra workflow validate job is authoritative",
)
def test_apim_module_compiles(tmp_path: Path) -> None:
    """The APIM module compiles to ARM JSON with no error (local fast check; the
    infra workflow validate job is the authoritative gate).
    """
    outfile = tmp_path / "apim.json"
    if shutil.which("bicep") is not None:
        cmd = ["bicep", "build", str(APIM), "--outfile", str(outfile)]
    else:
        cmd = ["az", "bicep", "build", "--file", str(APIM), "--outfile", str(outfile)]
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
    declared = "resource svc 'Microsoft.ApiManagement/service@2023-05-01' = {\n  name: 'x'\n}\n"
    commented = "// resource svc 'Microsoft.ApiManagement/service@2023-05-01' = {\n"
    assert _declares_resource_type(declared, _APIM_SERVICE_TYPE)
    assert not _declares_resource_type(commented, _APIM_SERVICE_TYPE)


def test_apim_api_version_detector_controls() -> None:
    """``_apim_resource_api_versions`` extracts only APIM resource versions, in
    source order, and ignores non-APIM resources and commented declarations — so
    the version gate cannot pass coincidentally on an empty or mis-scoped match.
    """
    sample = (
        "resource a 'Microsoft.ApiManagement/service@2022-08-01' = {}\n"
        "resource b 'Microsoft.ApiManagement/service/apis@2023-05-01' = {}\n"
        "resource c 'Microsoft.Storage/storageAccounts@2023-01-01' = {}\n"
    )
    assert _apim_resource_api_versions(sample) == ["2022-08-01", "2023-05-01"]
    commented = "// resource d 'Microsoft.ApiManagement/service@2022-08-01' = {}\n"
    assert _apim_resource_api_versions(commented) == []


def test_secret_output_detector_controls() -> None:
    """The secret scan flags a ``listSecrets()`` output, passes a clean one."""
    leak = "output k string = svc.listSecrets().primaryKey\n"
    clean = "output u string = svc.properties.gatewayUrl\n"
    assert _output_secret_violations(leak), "secret detector failed to flag a listSecrets output"
    assert not _output_secret_violations(clean), "secret detector false-positived on a clean output"


def test_comment_stripper_controls() -> None:
    """``_strip_line_comments`` removes a commented module stub, keeps a live one,
    and does not swallow the ``//`` inside a URL scheme.
    """
    commented = "  // module apim 'modules/apim.bicep' = {"
    assert "module apim" not in _strip_line_comments(commented)
    live = "module apim 'modules/apim.bicep' = {"
    assert "module apim" in _strip_line_comments(live)
    # A URL-bearing line is preserved; a trailing comment on it is still removed.
    url_line = "    url: 'https://${sageBackendHostname}' // route to SAGE"
    stripped = _strip_line_comments(url_line)
    assert "https://${sageBackendHostname}" in stripped
    assert "route to SAGE" not in stripped


def test_loaded_policy_paths_detector_controls() -> None:
    """``_loaded_policy_paths`` returns a real loaded xml, ignores comments/non-xml."""
    real = "value: loadTextContent('../policies/sage-api-policy.xml')\n"
    commented = "// value: loadTextContent('../policies/old.xml')\n"
    non_xml = "value: loadTextContent('../policies/notes.txt')\n"
    assert _loaded_policy_paths(real) == ["../policies/sage-api-policy.xml"]
    assert _loaded_policy_paths(commented) == []
    assert _loaded_policy_paths(non_xml) == []


def test_policy_special_case_detector_controls() -> None:
    """``_policy_special_cases_path`` fires on a branch that singles out a path,
    and passes a policy that handles it only via ``<otherwise>``.
    """
    denied = (
        "<choose>"
        "<when condition='@(context.Request.Url.Path.Contains(\"/mcp_admin\"))'>"
        '<return-response><set-status code="404" /></return-response></when>'
        '<otherwise><set-backend-service backend-id="sage-backend" /></otherwise>'
        "</choose>"
    )
    uniform = (
        "<choose>"
        "<when condition='@(context.Request.Url.Path.Contains(\"/.well-known\"))'>"
        '<return-response><set-status code="200" /></return-response></when>'
        '<otherwise><set-backend-service backend-id="sage-backend" /></otherwise>'
        "</choose>"
    )
    assert _policy_special_cases_path(denied, _ADMIN_MOUNT), (
        "detector failed to flag a <when> branch singling out the admin mount"
    )
    assert not _policy_special_cases_path(uniform, _ADMIN_MOUNT), (
        "detector false-positived on a policy that routes the admin mount via <otherwise>"
    )


def test_fragile_single_quoted_attr_detector_controls() -> None:
    """``_fragile_single_quoted_attrs`` fires on the round-trip-fragile
    single-quoted-inner-double-quote form, clears on the ``&quot;``-escaped
    double-quoted form, clears on a single-quoted attribute with no inner double
    quote, and does not scan inside XML comments — so the fragility gate cannot
    pass coincidentally on a regex that matches nothing.
    """
    fragile = "<when condition='@(context.Request.Url.Path.Contains(\"/x\"))'>"
    fixed = '<when condition="@(context.Request.Url.Path.Contains(&quot;/x&quot;))">'
    plain = "<param name='path' />"
    commented = "<!-- <when condition='@(Contains(\"/x\"))'> -->"
    assert _fragile_single_quoted_attrs(fragile), (
        "detector failed to flag the fragile single-quoted inner-double-quote attribute"
    )
    assert not _fragile_single_quoted_attrs(fixed), (
        "detector false-positived on the &quot;-escaped double-quoted form"
    )
    assert not _fragile_single_quoted_attrs(plain), (
        "detector false-positived on a single-quoted attribute with no inner double quote"
    )
    assert not _fragile_single_quoted_attrs(commented), "detector must not scan inside XML comments"


def test_hardcoded_https_host_detector_controls() -> None:
    """``_hardcoded_https_hosts`` flags a literal host, passes an interpolation."""
    literal = "url: 'https://sage.example.com'\n"
    interpolated = "url: 'https://${sageBackendHostname}'\n"
    assert _hardcoded_https_hosts(literal) == ["https://sage.example.com"]
    assert _hardcoded_https_hosts(interpolated) == []


def test_wildcard_method_detector_controls() -> None:
    """``_uses_literal_wildcard_method`` fires on the broken ``*`` literal, clears
    on a loop variable and on an explicit verb.
    """
    assert _uses_literal_wildcard_method("method: '*'")
    assert not _uses_literal_wildcard_method("method: method")
    assert not _uses_literal_wildcard_method("method: 'GET'")


def test_catch_all_template_detector_controls() -> None:
    """``_catch_all_url_templates`` distinguishes the working ``/{*path}`` from the
    broken ``/*`` and ignores commented declarations.
    """
    assert _catch_all_url_templates("urlTemplate: '/{*path}'") == ["/{*path}"]
    assert _catch_all_url_templates("urlTemplate: '/*'") == ["/*"]
    assert _catch_all_url_templates("// urlTemplate: '/{*path}'") == []


def test_path_param_detector_controls() -> None:
    """``_declares_path_wildcard_param`` fires on a ``name: 'path'`` parameter,
    clears on an empty parameter list.
    """
    declared = "templateParameters: [\n  {\n    name: 'path'\n    type: 'string'\n  }\n]"
    assert _declares_path_wildcard_param(declared)
    assert not _declares_path_wildcard_param("templateParameters: []")


def test_operation_method_values_detector_controls() -> None:
    """``_operation_method_values`` reads the per-method loop array, falls back to
    bare method literals, and ignores comments.
    """
    loop = "var sageHttpMethods = ['GET', 'POST', 'DELETE']\n"
    assert _operation_method_values(loop) == {"GET", "POST", "DELETE"}
    bare = "method: 'GET'\nmethod: 'POST'\n"
    assert _operation_method_values(bare) == {"GET", "POST"}
    assert _operation_method_values("// var sageHttpMethods = ['GET']\n") == set()
