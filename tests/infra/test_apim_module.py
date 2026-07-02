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

# The two paths served by their own dedicated GET operation (round-trip-safe:
# no inline path-string literal in a <when condition> for the IaC pipeline to
# double-encode). Discovery returns a canned metadata doc; /health is an
# unauthenticated backend passthrough so the liveness probe reaches the process.
_DISCOVERY_PATH: Final[str] = "/.well-known/oauth-protected-resource"
_HEALTH_PATH: Final[str] = "/health"

# The DCR-compatibility facade (CAS-ADR-042): two more dedicated,
# unauthenticated operations. The authorization-server metadata doc advertises
# Entra's real authorize/token endpoints plus a registration_endpoint pointing
# at the facade's own /register; /register answers every registration attempt
# with the single pre-provisioned public client id — no dynamic registration
# ever occurs.
_AS_METADATA_PATH: Final[str] = "/.well-known/oauth-authorization-server"
_REGISTER_PATH: Final[str] = "/register"
_MCP_CLIENT_ID_NAMED_VALUE: Final[str] = "mcp-client-id"

# The browser-redirect contract on the catch-all <on-error> 401 branch: a
# tokenless human browser (Accept: text/html) is 302'd to the CAS app via the
# {{cas-app-url}} named value, while every machine client keeps the byte-identical
# WWW-Authenticate challenge below. The Accept test is a quoted string literal in
# a <when> condition — encoded in the round-trip-safe &quot;-escaped double-quoted
# form (never the single-quote-inner-double form the IaC pipeline corrupts).
_CAS_APP_URL_TOKEN: Final[str] = "{{cas-app-url}}"
_BROWSER_ACCEPT_CONDITION_FRAGMENT: Final[str] = "Contains(&quot;text/html&quot;)"
# The machine 401 challenge, frozen byte-for-byte: a reword/reorder/token change
# (anything that breaks the RFC 9728 / MCP discovery handshake) must fail the gate.
_WWW_AUTH_CHALLENGE: Final[str] = (
    'Bearer resource_metadata="{{sage-resource-url}}/.well-known/oauth-protected-resource"'
)

# Policy XML files under infra/policies/: the API-level policy plus the
# operation-scoped policies the dedicated operations load.
API_POLICY: Final[Path] = POLICIES_DIR / "sage-api-policy.xml"
DISCOVERY_OP_POLICY: Final[Path] = POLICIES_DIR / "sage-discovery-operation-policy.xml"
HEALTH_OP_POLICY: Final[Path] = POLICIES_DIR / "sage-health-operation-policy.xml"
AS_METADATA_OP_POLICY: Final[Path] = POLICIES_DIR / "sage-authorization-server-operation-policy.xml"
REGISTER_OP_POLICY: Final[Path] = POLICIES_DIR / "sage-register-operation-policy.xml"

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


def _inbound_section(xml: str) -> str:
    """Return the *live* text inside the first ``<inbound>...</inbound>`` block.

    XML comments are stripped first, so an explanatory comment that names a
    forbidden token (``<when>``, ``validate-jwt``, ``<base/>``) does not trip a
    gate that scans this section — the gate asserts on live policy, not prose.
    Scoped to ``<inbound>`` so the ``<on-error>`` block (which legitimately
    carries a round-trip-safe ``== 401`` ``<when>``) is excluded.
    """
    match = re.search(r"<inbound>(.*?)</inbound>", _strip_xml_comments(xml), re.DOTALL)
    return match.group(1) if match else ""


def _on_error_section(xml: str) -> str:
    """Return the *live* text inside the first ``<on-error>...</on-error>`` block.

    XML comments are stripped first (so an explanatory comment naming a token does
    not trip a gate scanning this section), and the result is scoped to
    ``<on-error>`` so an ``<inbound>`` construct cannot satisfy an on-error
    assertion. This is where the catch-all's 401 challenge — and the browser
    redirect — live; the inbound stays string-literal-free.
    """
    match = re.search(r"<on-error>(.*?)</on-error>", _strip_xml_comments(xml), re.DOTALL)
    return match.group(1) if match else ""


def _declares_literal_get_operation(text: str, url_template: str) -> bool:
    """True iff a dedicated ``method: 'GET'`` operation with ``urlTemplate:
    '<url_template>'`` is declared (the two co-occurring within one ``{...}``
    properties block, order-independent), after stripping line comments.

    The catch-all loop uses ``method: method`` (a variable), so a *literal*
    ``method: 'GET'`` is unique to a hand-declared operation. The bounded
    ``[^{}]*?`` keeps the match inside a single operation's properties block,
    so a GET on one operation cannot pair with a urlTemplate on another.
    """
    stripped = _strip_line_comments(text)
    esc = re.escape(url_template)
    forward = re.search(r"method:\s*'GET'[^{}]*?urlTemplate:\s*'" + esc + r"'", stripped)
    backward = re.search(r"urlTemplate:\s*'" + esc + r"'[^{}]*?method:\s*'GET'", stripped)
    return bool(forward or backward)


def _declares_literal_operation(text: str, url_template: str, method: str) -> bool:
    """True iff a dedicated ``method: '<method>'`` operation with ``urlTemplate:
    '<url_template>'`` is declared (the two co-occurring within one ``{...}``
    properties block, order-independent), after stripping line comments.

    Generalizes ``_declares_literal_get_operation`` to an arbitrary verb — the
    DCR facade's ``/register`` operation is a dedicated ``POST``, not a
    ``GET``, so a GET-only detector would silently pass a mis-declared operation.
    """
    stripped = _strip_line_comments(text)
    esc = re.escape(url_template)
    method_esc = re.escape(method)
    forward = re.search(
        r"method:\s*'" + method_esc + r"'[^{}]*?urlTemplate:\s*'" + esc + r"'", stripped
    )
    backward = re.search(
        r"urlTemplate:\s*'" + esc + r"'[^{}]*?method:\s*'" + method_esc + r"'", stripped
    )
    return bool(forward or backward)


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
    """The catch-all operations forward every unmatched path via the ``/{*path}``
    wildcard template — never the non-matching ``/*``. Dedicated operations
    (discovery, /health) legitimately carry their own literal templates, so the
    wildcard must be *present* and ``/*`` *absent* — not the only template.
    """
    templates = _catch_all_url_templates(APIM.read_text(encoding="utf-8"))
    assert _CATCH_ALL_URL_TEMPLATE in templates, (
        f"apim.bicep must declare the catch-all '{_CATCH_ALL_URL_TEMPLATE}' template; "
        f"found: {templates}"
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


def test_apim_policy_accepts_v2_guid_audience() -> None:
    """The JWT policy accepts the resource's bare application-id GUID audience in
    addition to its App ID URI. A v2.0 access token (requestedAccessTokenVersion
    2 -- coupled to the v2 issuer both validators require) carries the bare GUID
    as its ``aud``, not ``api://<app-id>``; without the second audience the edge
    401s every authenticated request. Asserts the dedicated ``<audience>``
    element, not the named-value token alone (which also appears in prose).
    """
    policy = _policy_text()
    assert "<audience>{{sage-audience}}</audience>" in policy, (
        "the App ID URI audience must remain an accepted <audience>"
    )
    assert "<audience>{{sage-audience-guid}}</audience>" in policy, (
        "validate-jwt must also accept the {{sage-audience-guid}} bare-GUID "
        "audience (the form a v2.0 access token carries)"
    )


def test_apim_defines_guid_audience_named_value() -> None:
    """apim.bicep declares the ``sage-audience-guid`` named value, derived from
    the App ID URI by stripping the ``api://`` scheme -- no second parameter.
    The JWT policy references it as ``{{sage-audience-guid}}``.
    """
    text = _strip_line_comments(APIM.read_text(encoding="utf-8"))
    assert "name: 'sage-audience-guid'" in text, (
        "apim.bicep must declare a 'sage-audience-guid' named value"
    )
    assert "replace(sageAudience, 'api://', '')" in text, (
        "the GUID audience must derive from sageAudience by stripping 'api://' "
        "(no second parameter)"
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


def test_apim_declares_discovery_operation() -> None:
    """The OAuth discovery doc is served by its own explicit GET operation
    (``/.well-known/oauth-protected-resource``), not by a path-string ``<when>``
    condition in the API-level policy — the inline quoted literal the
    loadTextContent -> ARM -> APIM round-trip double-encodes. APIM routes the
    literal path to this operation ahead of the ``/{*path}`` catch-all.
    """
    assert _declares_literal_get_operation(APIM.read_text(encoding="utf-8"), _DISCOVERY_PATH), (
        f"apim.bicep must declare a dedicated GET operation with urlTemplate '{_DISCOVERY_PATH}'"
    )


def test_apim_declares_health_operation() -> None:
    """``/health`` is served by its own explicit GET operation routed to the
    backend unauthenticated, so the post-deploy ``liveness`` preflight — which
    reaches /health through the APIM edge (``sage.<domain>`` CNAMEs to the
    gateway) — gets 200 without a token, mirroring SAGE's own /health auth-exemption.
    """
    assert _declares_literal_get_operation(APIM.read_text(encoding="utf-8"), _HEALTH_PATH), (
        f"apim.bicep must declare a dedicated GET operation with urlTemplate '{_HEALTH_PATH}'"
    )


def test_apim_declares_authorization_server_operation() -> None:
    """The DCR facade's authorization-server metadata is served by its own
    dedicated GET operation (``/.well-known/oauth-authorization-server``), the
    same round-trip-safe shape as the discovery and /health operations.
    """
    assert _declares_literal_operation(
        APIM.read_text(encoding="utf-8"), _AS_METADATA_PATH, "GET"
    ), f"apim.bicep must declare a dedicated GET operation with urlTemplate '{_AS_METADATA_PATH}'"


def test_apim_declares_register_operation() -> None:
    """The DCR facade's static registration response is served by its own
    dedicated POST operation (``/register``) — a GET here would never match the
    client's registration request.
    """
    assert _declares_literal_operation(APIM.read_text(encoding="utf-8"), _REGISTER_PATH, "POST"), (
        f"apim.bicep must declare a dedicated POST operation with urlTemplate '{_REGISTER_PATH}'"
    )


def test_apim_discovery_operation_policy_serves_doc_unauthenticated() -> None:
    """The discovery operation's policy returns the protected-resource-metadata
    document directly (return-response) and does NOT validate the JWT, so the
    handshake doc is reachable unauthenticated. The operation inbound must carry
    no ``<base/>`` — that would inherit the API-level validate-jwt and 401 the doc.
    """
    xml = DISCOVERY_OP_POLICY.read_text(encoding="utf-8")
    inbound = _inbound_section(xml)
    assert "<return-response" in inbound, "discovery operation must serve a return-response"
    assert "authorization_servers" in xml and "scopes_supported" in xml, (
        "the resource-metadata document must carry authorization_servers and scopes_supported"
    )
    assert "validate-jwt" not in inbound, (
        "the discovery operation must not validate the JWT (the doc is unauthenticated)"
    )
    assert "<base" not in inbound, (
        "the discovery operation inbound must not call <base/> — that would inherit the "
        "API-level validate-jwt and 401 the unauthenticated discovery doc"
    )


def test_apim_discovery_authorization_servers_point_at_facade() -> None:
    """The protected-resource-metadata's ``authorization_servers`` points at the
    facade's own authorization-server metadata (``{{sage-resource-url}}``), not
    directly at the raw Entra issuer.

    A default MCP client resolves ``authorization_servers`` and fetches *that*
    URL's ``/.well-known/oauth-authorization-server``. Pointing straight at
    Entra hands the client Entra's real AS metadata — which carries no
    ``registration_endpoint`` — and the DCR leg this ticket exists to unblock
    dead-ends exactly as it does today.
    """
    xml = DISCOVERY_OP_POLICY.read_text(encoding="utf-8")
    assert "{{sage-resource-url}}" in xml, (
        "the resource-metadata authorization_servers must interpolate {{sage-resource-url}} "
        "(the facade), not the raw Entra issuer"
    )
    assert _ENTRA_AUTHORITY_HOST not in xml, (
        f"the discovery doc must not reference {_ENTRA_AUTHORITY_HOST} directly — "
        "authorization_servers must route through the facade's own AS metadata"
    )


def test_apim_as_metadata_policy_serves_doc_unauthenticated() -> None:
    """The authorization-server metadata operation returns Entra's real
    authorize/token/JWKS endpoints plus the facade's ``registration_endpoint``,
    unauthenticated (no JWT, no ``<base/>``) — the discovery leg of the DCR facade.
    """
    xml = AS_METADATA_OP_POLICY.read_text(encoding="utf-8")
    inbound = _inbound_section(xml)
    assert "<return-response" in inbound, "the AS metadata operation must serve a return-response"
    for field in (
        "authorization_endpoint",
        "token_endpoint",
        "jwks_uri",
        "registration_endpoint",
        "code_challenge_methods_supported",
    ):
        assert field in xml, f"the AS metadata document must carry {field!r}"
    assert "{{entra-tenant-id}}" in xml and "{{sage-resource-url}}" in xml, (
        "the AS metadata document must interpolate entra-tenant-id (the real Entra "
        "endpoints) and sage-resource-url (the facade's own registration_endpoint)"
    )
    assert '"registration_endpoint": "{{sage-resource-url}}/register"' in xml, (
        "registration_endpoint must resolve to the facade's own /register path"
    )
    assert "validate-jwt" not in inbound, (
        "the AS metadata operation must not validate the JWT (unauthenticated discovery)"
    )
    assert "<base" not in inbound, (
        "the AS metadata operation inbound must not call <base/> — that would inherit "
        "the API-level validate-jwt and 401 the unauthenticated discovery doc"
    )


def test_apim_register_policy_serves_static_client_unauthenticated() -> None:
    """The ``/register`` operation answers every registration attempt with the
    single pre-provisioned public client id and ``token_endpoint_auth_method:
    none`` — no dynamic registration occurs, and no secret is ever minted.
    """
    xml = REGISTER_OP_POLICY.read_text(encoding="utf-8")
    inbound = _inbound_section(xml)
    assert "<return-response" in inbound, "the /register operation must serve a return-response"
    assert "{{" + _MCP_CLIENT_ID_NAMED_VALUE + "}}" in xml, (
        f"the registration response must interpolate the {{{{{_MCP_CLIENT_ID_NAMED_VALUE}}}}} "
        "named value"
    )
    assert '"token_endpoint_auth_method": "none"' in xml, (
        "the static registration response must declare token_endpoint_auth_method: none "
        "(a public client, no secret)"
    )
    assert "client_secret" not in xml, (
        "the static registration response must never mint or echo a client_secret"
    )
    assert "validate-jwt" not in inbound, (
        "the /register operation must not validate the JWT (unauthenticated registration)"
    )
    assert "<base" not in inbound, (
        "the /register operation inbound must not call <base/> — that would inherit "
        "the API-level validate-jwt and 401 the unauthenticated registration call"
    )


def test_apim_declares_mcp_client_id_named_value() -> None:
    """apim.bicep declares the ``mcp-client-id`` named value from an
    ``mcpClientId`` parameter — the pre-provisioned public client id the
    ``/register`` facade echoes back, never a literal GUID.
    """
    text = _strip_line_comments(APIM.read_text(encoding="utf-8"))
    assert re.search(r"param\s+mcpClientId\s+string", text), (
        "apim.bicep must take an `mcpClientId` string parameter"
    )
    assert f"name: '{_MCP_CLIENT_ID_NAMED_VALUE}'" in text, (
        f"apim.bicep must declare a '{_MCP_CLIENT_ID_NAMED_VALUE}' named value"
    )
    assert re.search(r"value:\s*mcpClientId", text), (
        "the mcp-client-id named value must come from the mcpClientId parameter"
    )
    assert not _GUID_RE.search(APIM.read_text(encoding="utf-8")), (
        "apim.bicep must not hardcode the mcp client id as a literal GUID"
    )


def test_apim_health_operation_policy_routes_to_backend_unauthenticated() -> None:
    """The /health operation routes to the SAGE backend unauthenticated (no JWT),
    mirroring SAGE's own /health auth-exemption. It is a real backend passthrough,
    not a canned return-response — so ``liveness`` proves the SAGE process is up,
    not merely that APIM is up.
    """
    xml = HEALTH_OP_POLICY.read_text(encoding="utf-8")
    inbound = _inbound_section(xml)
    assert re.search(r"set-backend-service\s+backend-id=\"sage-backend\"", inbound), (
        "the /health operation inbound must route to the sage-backend"
    )
    assert "validate-jwt" not in inbound, "the /health operation must not validate the JWT"
    assert "<base" not in inbound, (
        "the /health operation inbound must not call <base/> — that would inherit the "
        "API-level validate-jwt and 401 the liveness probe"
    )
    assert "return-response" not in inbound, (
        "/health must be a real backend passthrough, not a canned edge 200 — "
        "liveness must reach the SAGE process, not stop at APIM"
    )


def test_apim_api_policy_inbound_has_no_path_string_condition() -> None:
    """The API-level inbound policy no longer routes on a path-string ``<when>``
    condition — the inline quoted literal the loadTextContent -> ARM -> APIM
    round-trip double-encodes (``&quot;`` -> ``&amp;quot;``). Discovery and /health
    moved to dedicated operations; the inbound is now validate-jwt + route-to-backend.
    The only surviving ``<when>`` is the on-error ``== 401`` challenge (no string
    literal, round-trip-safe), which lives outside ``<inbound>``.
    """
    inbound = _inbound_section(API_POLICY.read_text(encoding="utf-8"))
    assert "<when" not in inbound, (
        "the API-level inbound must contain no <when> path condition; route discovery "
        "and /health via dedicated operations instead"
    )
    assert "Contains(" not in inbound, (
        "the API-level inbound must contain no path-string Contains(...) literal "
        "(the round-trip-fragile form this design replaces)"
    )


def test_apim_policy_redirects_browser_to_app() -> None:
    """A tokenless human browser (Accept: text/html) is 302-redirected to the CAS app.

    The redirect lives in the catch-all policy's <on-error> 401 branch: a nested
    <when> that tests the Accept header for text/html, then a return-response with a
    302 status and a Location header pointing at the {{cas-app-url}} named value.
    Scoped to the <on-error> section so an inbound construct cannot satisfy it.
    """
    on_error = _on_error_section(API_POLICY.read_text(encoding="utf-8"))
    assert "<when" in on_error and "Accept" in on_error, (
        "the on-error 401 branch must test the Accept header in a <when> condition"
    )
    assert _BROWSER_ACCEPT_CONDITION_FRAGMENT in on_error, (
        "the Accept test must use the round-trip-safe encoding "
        f"{_BROWSER_ACCEPT_CONDITION_FRAGMENT!r}"
    )
    assert "<return-response" in on_error, (
        "the browser branch must short-circuit with a return-response"
    )
    assert re.search(r'set-status\s+code="302"', on_error), (
        "the browser redirect must set a 302 status"
    )
    assert re.search(r'name="Location"', on_error) and _CAS_APP_URL_TOKEN in on_error, (
        "the redirect must set a Location header to the {{cas-app-url}} named value"
    )


def test_apim_policy_preserves_machine_401_challenge() -> None:
    """A tokenless machine client (non-browser Accept) keeps the byte-identical 401
    WWW-Authenticate challenge — the RFC 9728 / MCP OAuth discovery handshake.

    The challenge must be unchanged and live in the <otherwise> (machine) branch, not
    the browser <when>, so adding the redirect did not alter the machine contract.
    """
    on_error = _on_error_section(API_POLICY.read_text(encoding="utf-8"))
    assert _WWW_AUTH_CHALLENGE in on_error, (
        "the machine 401 WWW-Authenticate challenge must remain byte-identical"
    )
    assert "<otherwise>" in on_error, (
        "the machine challenge must sit in the <otherwise> branch (browser is the <when>)"
    )
    assert on_error.index(_WWW_AUTH_CHALLENGE) > on_error.index("<otherwise>"), (
        "the WWW-Authenticate challenge must be in the <otherwise> (machine) branch, "
        "not the browser redirect <when>"
    )


def test_apim_browser_redirect_uses_robust_quote_encoding() -> None:
    """The Accept-header literal uses the round-trip-safe &quot;-escaped double-quoted
    form, never the single-quote-inner-double form the loadTextContent -> ARM -> APIM
    pipeline corrupts. This is the encoding half of the browser-redirect acceptance
    criterion; the surviving round-trip itself is deploy-verified by the preflight.
    """
    policy = _policy_text()
    assert _BROWSER_ACCEPT_CONDITION_FRAGMENT in policy, (
        "the browser Accept test must be present in the &quot;-escaped form"
    )
    assert not _fragile_single_quoted_attrs(policy), (
        "the browser redirect condition must not use the round-trip-fragile "
        "single-quoted-inner-double-quote encoding"
    )


def test_apim_defines_cas_app_url_named_value() -> None:
    """apim.bicep declares the ``cas-app-url`` named value from a ``casAppUrl``
    parameter (the browser-redirect target), with no hardcoded URL in the module.
    """
    text = _strip_line_comments(APIM.read_text(encoding="utf-8"))
    assert re.search(r"param\s+casAppUrl\s+string", text), (
        "apim.bicep must take a `casAppUrl` string parameter"
    )
    assert "name: 'cas-app-url'" in text, "apim.bicep must declare a 'cas-app-url' named value"
    assert re.search(r"value:\s*casAppUrl", text), (
        "the cas-app-url named value must come from the casAppUrl parameter"
    )
    assert not _hardcoded_https_hosts(APIM.read_text(encoding="utf-8")), (
        "the redirect target must be parameterized; no hardcoded https host in the module"
    )


def test_main_bicep_passes_cas_app_url_to_apim() -> None:
    """main.bicep passes the CAS app URL to the apim module, interpolated from the cas
    custom-domain hostname — tenant-agnostic, never a literal domain.
    """
    text = _strip_line_comments(MAIN_BICEP.read_text(encoding="utf-8"))
    match = re.search(r"casAppUrl:\s*([^\n]+)", text)
    assert match, "main.bicep must pass `casAppUrl:` to the apim module"
    rhs = match.group(1)
    assert "${casHostname}" in rhs, (
        "casAppUrl must interpolate the cas custom-domain hostname (${casHostname})"
    )
    assert not _hardcoded_https_hosts(rhs), f"casAppUrl must not embed a literal https host: {rhs}"


def test_apim_operation_policies_loaded_from_versioned_xml() -> None:
    """Every dedicated operation policy is loaded from versioned XML under
    infra/policies/ via loadTextContent — the same discipline as the API-level
    policy, extended to operation scope (discovery, /health, and the DCR
    facade's authorization-server metadata and /register).
    """
    loaded_names = {Path(p).name for p in _loaded_policy_paths(APIM.read_text(encoding="utf-8"))}
    op_policies = (DISCOVERY_OP_POLICY, HEALTH_OP_POLICY, AS_METADATA_OP_POLICY, REGISTER_OP_POLICY)
    for policy in op_policies:
        assert policy.name in loaded_names, (
            f"apim.bicep must loadTextContent the operation policy '{policy.name}'"
        )
        assert policy.is_file(), (
            f"operation policy XML must exist under infra/policies/: {policy.name}"
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


def test_inbound_section_detector_controls() -> None:
    """``_inbound_section`` returns the inbound body, distinguishes a ``<base/>``-
    bearing inbound from one without, and excludes the ``<on-error>`` block (whose
    legitimate ``== 401`` ``<when>`` must not be mistaken for an inbound condition).
    """
    with_base = (
        "<policies><inbound><base /><validate-jwt /></inbound>"
        '<on-error><choose><when condition="@(... == 401)" /></choose></on-error></policies>'
    )
    without_base = (
        "<policies><inbound><return-response /></inbound><on-error><base /></on-error></policies>"
    )
    assert "<base" in _inbound_section(with_base)
    assert "validate-jwt" in _inbound_section(with_base)
    assert "<base" not in _inbound_section(without_base)
    # The on-error block (and its <when>) is excluded from the inbound section.
    assert "when" not in _inbound_section(with_base)
    assert "on-error" not in _inbound_section(without_base)
    assert _inbound_section("<policies></policies>") == ""
    # A comment naming a forbidden token does not leak into the live section —
    # so a gate scanning the inbound cannot be fooled (or tripped) by prose.
    commented = (
        "<policies><inbound><!-- no <when> condition here; never validate-jwt -->"
        '<set-backend-service backend-id="sage-backend" /></inbound></policies>'
    )
    assert "<when" not in _inbound_section(commented)
    assert "validate-jwt" not in _inbound_section(commented)
    assert "set-backend-service" in _inbound_section(commented)


def test_on_error_section_detector_controls() -> None:
    """``_on_error_section`` returns the on-error body, excludes ``<inbound>``, returns
    "" when absent, and does not leak commented tokens — so an on-error gate (the
    browser-redirect checks) cannot pass coincidentally on an empty or mis-scoped match.
    """
    xml = (
        "<policies><inbound><base /><validate-jwt /></inbound>"
        '<on-error><base /><choose><when condition="@(... == 401)">'
        "<return-response /></when></choose></on-error></policies>"
    )
    section = _on_error_section(xml)
    assert "when" in section and "return-response" in section
    assert "validate-jwt" not in section, "the inbound block must be excluded from on-error"
    assert _on_error_section("<policies><inbound /></policies>") == ""
    commented = "<on-error><!-- <return-response/> redirect here --><base /></on-error>"
    assert "return-response" not in _on_error_section(commented), (
        "commented tokens must not leak into the live on-error section"
    )


def test_literal_get_operation_detector_controls() -> None:
    """``_declares_literal_get_operation`` fires on a dedicated ``method: 'GET'``
    operation carrying the target urlTemplate, clears on the ``method: method``
    loop form (the catch-all, not a literal GET), and clears on a commented
    declaration — so the operation gate cannot pass on a stray or scaffolded match.
    """
    dedicated = (
        "resource op 'Microsoft.ApiManagement/service/apis/operations@2022-08-01' = {\n"
        "  properties: {\n"
        "    method: 'GET'\n"
        "    urlTemplate: '/health'\n"
        "  }\n"
        "}\n"
    )
    loop_form = "    method: method\n    urlTemplate: '/health'\n"
    commented = "// method: 'GET'\n// urlTemplate: '/health'\n"
    assert _declares_literal_get_operation(dedicated, "/health")
    assert not _declares_literal_get_operation(loop_form, "/health")
    assert not _declares_literal_get_operation(commented, "/health")
    # A GET on one operation must not pair with a urlTemplate on a *different* one.
    cross = (
        "properties: {\n  method: 'GET'\n  urlTemplate: '/{*path}'\n}\n"
        "properties: {\n  method: method\n  urlTemplate: '/health'\n}\n"
    )
    assert not _declares_literal_get_operation(cross, "/health")


def test_literal_operation_detector_controls() -> None:
    """``_declares_literal_operation`` generalizes the GET-only detector to an
    arbitrary verb: fires on a dedicated ``POST`` operation carrying the target
    urlTemplate, clears when the method is wrong (the DCR facade's ``/register``
    must be POST, not GET — a mis-declared GET would silently 404 every real
    registration attempt), clears on the loop form, and clears on a comment.
    """
    post_op = (
        "resource op 'Microsoft.ApiManagement/service/apis/operations@2022-08-01' = {\n"
        "  properties: {\n"
        "    method: 'POST'\n"
        "    urlTemplate: '/register'\n"
        "  }\n"
        "}\n"
    )
    wrong_method = (
        "resource op 'Microsoft.ApiManagement/service/apis/operations@2022-08-01' = {\n"
        "  properties: {\n"
        "    method: 'GET'\n"
        "    urlTemplate: '/register'\n"
        "  }\n"
        "}\n"
    )
    loop_form = "    method: method\n    urlTemplate: '/register'\n"
    commented = "// method: 'POST'\n// urlTemplate: '/register'\n"
    assert _declares_literal_operation(post_op, "/register", "POST")
    assert not _declares_literal_operation(wrong_method, "/register", "POST")
    assert not _declares_literal_operation(loop_form, "/register", "POST")
    assert not _declares_literal_operation(commented, "/register", "POST")


# ---------------------------------------------------------------------------
# CORS preflight (CAS-ADR-042 / CAS-ADR-034)
# ---------------------------------------------------------------------------
#
# APIM runs validate-jwt on every method — including the CORS preflight OPTIONS —
# so a browser-context MCP client's preflight 401s before the JWT gate can be
# skipped, no Access-Control-Allow-* headers ever return, and the browser blocks
# the real call. The fix answers the preflight anonymously with a <cors> policy
# placed AHEAD of validate-jwt in the API-level inbound (all preflights route to
# the catch-all operation, which runs that policy), and repeats the <cors> block
# on the dedicated anonymous operations so their own actual responses carry the
# Allow-Origin header a browser needs to read them (CAS-ADR-042 / CAS-ADR-034).

# The anonymous operations whose actual GET/POST responses a browser client must
# read cross-origin. /health is excluded: it is a liveness probe, never fetched
# from a browser origin.
_ANONYMOUS_OP_POLICIES: Final[tuple[Path, ...]] = (
    REGISTER_OP_POLICY,
    DISCOVERY_OP_POLICY,
    AS_METADATA_OP_POLICY,
)


def _cors_block(policy: str) -> str:
    """Return the live text of the first ``<cors>...</cors>`` block (XML comments
    stripped first, so a comment naming ``<cors>`` cannot satisfy a gate).

    Empty string when no live ``<cors>`` element is present — the current,
    pre-fix state of every policy.
    """
    match = re.search(r"<cors\b.*?</cors>", _strip_xml_comments(policy), re.DOTALL)
    return match.group(0) if match else ""


def _cors_precedes_validate_jwt(inbound: str) -> bool:
    """True iff a live ``<cors>`` element is present in ``inbound`` AND appears
    before ``<validate-jwt>``.

    Position is load-bearing: a ``<cors>`` after ``validate-jwt`` never runs on a
    preflight the JWT gate has already 401'd, so it would not answer the preflight
    anonymously. ``inbound`` is expected to be the comment-stripped body from
    ``_inbound_section``.
    """
    cors = inbound.find("<cors")
    jwt = inbound.find("<validate-jwt")
    return cors != -1 and jwt != -1 and cors < jwt


def test_apim_api_policy_answers_cors_preflight_before_jwt() -> None:
    """The API-level inbound answers the CORS preflight ahead of the JWT gate.

    All preflight ``OPTIONS`` (to ``/register``, ``/mcp``, anything) fall through
    to the catch-all ``/{*path}`` operation, which runs this policy. A ``<cors>``
    positioned before ``<validate-jwt>`` short-circuits the preflight anonymously;
    placed after (or absent), the preflight 401s — the live browser-client failure.
    """
    inbound = _inbound_section(API_POLICY.read_text(encoding="utf-8"))
    assert _cors_precedes_validate_jwt(inbound), (
        "the API-level inbound must carry a <cors> element BEFORE <validate-jwt> so the "
        "preflight OPTIONS is answered anonymously ahead of the JWT gate"
    )


def test_apim_cors_allows_oauth_client_headers() -> None:
    """The API-level ``<cors>`` admits the request headers and methods a
    browser-context OAuth/MCP client sends.

    The preflight advertises ``Authorization`` and ``Content-Type`` request
    headers; omitting either fails the preflight even when the status is 2xx. The
    OAuth/MCP legs are ``GET`` (discovery), ``POST`` (``/register``, ``/mcp``),
    and the preflight ``OPTIONS`` itself.
    """
    cors = _cors_block(API_POLICY.read_text(encoding="utf-8"))
    assert cors, "the API-level policy must carry a <cors> block"
    for header in ("Authorization", "Content-Type"):
        assert f"<header>{header}</header>" in cors, (
            f"the <cors> allowed-headers must admit {header!r} (a browser MCP client "
            "sends it on the preflight)"
        )
    for method in ("GET", "POST", "OPTIONS"):
        assert f"<method>{method}</method>" in cors, (
            f"the <cors> allowed-methods must admit {method!r}"
        )


@pytest.mark.parametrize("policy_path", _ANONYMOUS_OP_POLICIES, ids=lambda p: p.name)
def test_apim_anonymous_operations_carry_cors(policy_path: Path) -> None:
    """Each anonymous operation policy carries its own ``<cors>``.

    The dedicated ``/register`` and ``.well-known/*`` operations omit ``<base/>``
    (that is how they skip ``validate-jwt``), so the API-level ``<cors>`` is never
    evaluated for them. Without a ``<cors>`` of their own, their actual GET/POST
    responses carry no ``Access-Control-Allow-Origin`` and a browser cannot read
    them — the coverage AC-2 requires beyond the catch-all preflight.
    """
    inbound = _inbound_section(policy_path.read_text(encoding="utf-8"))
    assert "<cors" in inbound, (
        f"{policy_path.name} must carry a <cors> element in its inbound so the anonymous "
        "operation's actual response carries Access-Control-Allow-* for a browser client"
    )


def test_apim_cors_detector_controls() -> None:
    """The CORS detectors fire on the regressions they target and clear on the
    correct form — so the gates above cannot pass coincidentally.

    ``_cors_precedes_validate_jwt`` must reject both a missing ``<cors>`` and a
    ``<cors>`` that follows ``validate-jwt``; ``_cors_block`` must return "" when
    no live ``<cors>`` is present and the block otherwise.
    """
    assert not _cors_precedes_validate_jwt("<base /><validate-jwt />"), "no <cors> -> False"
    assert not _cors_precedes_validate_jwt("<validate-jwt /><cors />"), "<cors> after jwt -> False"
    assert _cors_precedes_validate_jwt("<base /><cors /><validate-jwt />"), "correct order -> True"
    assert _cors_block("<cors><allowed-origins><origin>*</origin></allowed-origins></cors>")
    assert _cors_block("no cors here at all") == ""
    # A comment naming <cors> must not satisfy the block detector.
    assert _cors_block("<!-- add a <cors> element here -->") == ""
