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

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Final
from urllib.parse import urlparse

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

# Resource-level diagnostic settings route the gateway's platform metrics to the
# foundation Log Analytics workspace (CAS-ADR-042). Metrics are the only signal
# the resource-log plane carries on the deployed (Consumption) tier — that tier
# collects no resource logs at all, so no log category may be routed here: a
# routed-but-never-emitted category reads as coverage it does not provide.
_DIAGNOSTIC_SETTINGS_TYPE: Final[str] = "Microsoft.Insights/diagnosticSettings"
_METRICS_CATEGORY: Final[str] = "AllMetrics"
_WORKSPACE_PARAM: Final[str] = "logAnalyticsWorkspaceId"

# Edge request observability rides the Application Insights telemetry plane,
# which every APIM tier supports: a workspace-based Application Insights
# resource linked to the same foundation workspace, an ``applicationInsights``
# logger authenticated by the facade's user-assigned managed identity (no
# instrumentation key, no connection-string literal), and a service-level
# diagnostic named ``applicationinsights`` (the reserved instance id) binding it.
_APIM_LOGGER_TYPE: Final[str] = "Microsoft.ApiManagement/service/loggers"
_APIM_DIAGNOSTIC_TYPE: Final[str] = "Microsoft.ApiManagement/service/diagnostics"
_APP_INSIGHTS_TYPE: Final[str] = "Microsoft.Insights/components"
_APP_INSIGHTS_LOGGER_TYPE: Final[str] = "applicationInsights"
_APP_INSIGHTS_DIAGNOSTIC_NAME: Final[str] = "applicationinsights"
_ROLE_ASSIGNMENT_TYPE: Final[str] = "Microsoft.Authorization/roleAssignments"
# Built-in Azure role: Monitoring Metrics Publisher (Entra-authenticated
# telemetry ingestion). A fixed, public Azure constant — not an environment
# identity coordinate — and the only GUID the module may carry.
_METRICS_PUBLISHER_ROLE: Final[str] = "3913510d-42f4-4e42-8a64-420c390055eb"

# The stable API Management API version every resource must pin. A bare
# 2023-05-01 exists only in its ``-preview`` form (the stable form raises BCP081
# against the public type index); 2022-08-01 is the real stable version.
_APIM_STABLE_API_VERSION: Final[str] = "2022-08-01"
_APIM_NONEXISTENT_API_VERSION: Final[str] = "2023-05-01"

# The maintenance mount. It routes through the facade under the same JWT
# validation as the ordinary surface — the policy must not single it out.
_MAINT_MOUNT: Final[str] = "/mcp_maint"
# The maintenance mount's pre-rename alias path, kept serving with no
# scheduled removal.
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

# The schema document, served unauthenticated so each deployment describes
# itself: a caller that must already hold a token to read the document naming
# the token endpoint has no entry point. Like /health it is a backend
# passthrough, so what reaches a caller is the running process's own document
# rather than a canned copy the edge would have to keep in step.
_OPENAPI_PATH: Final[str] = "/openapi.json"

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
DISCOVERY_MCP_OP_POLICY: Final[Path] = POLICIES_DIR / "sage-discovery-mcp-operation-policy.xml"
DISCOVERY_MCP_MAINT_OP_POLICY: Final[Path] = (
    POLICIES_DIR / "sage-discovery-mcp-maint-operation-policy.xml"
)
DISCOVERY_MCP_ADMIN_OP_POLICY: Final[Path] = (
    POLICIES_DIR / "sage-discovery-mcp-admin-operation-policy.xml"
)
HEALTH_OP_POLICY: Final[Path] = POLICIES_DIR / "sage-health-operation-policy.xml"
OPENAPI_OP_POLICY: Final[Path] = POLICIES_DIR / "sage-openapi-operation-policy.xml"
UPLOAD_OP_POLICY: Final[Path] = POLICIES_DIR / "sage-upload-operation-policy.xml"
DOWNLOAD_OP_POLICY: Final[Path] = POLICIES_DIR / "sage-download-operation-policy.xml"
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


def _resource_block(text: str, resource_type: str) -> str:
    """Return the body of the ``resource <symbol> '<resource_type>@...' = {`` block.

    Slices from the resource declaration to the next top-level declaration
    (``resource`` / ``module`` / ``output`` at column 0) or end of file, so an
    assertion is scoped to a single resource rather than satisfied by an unrelated
    one elsewhere in the module. Returns ``""`` when the type is not declared.
    """
    stripped = _strip_line_comments(text)
    start = re.search(
        r"^resource\s+\w+\s+'" + re.escape(resource_type) + r"@[0-9A-Za-z-]+'",
        stripped,
        re.MULTILINE,
    )
    if start is None:
        return ""
    rest = stripped[start.end() :]
    nxt = re.search(r"^(?:resource|module|output)\b", rest, re.MULTILINE)
    return rest if nxt is None else rest[: nxt.start()]


def _module_block(text: str, module_rel_path: str) -> str:
    """Return the body of the ``module <symbol> '<module_rel_path>' = {`` block.

    The same top-level slice as :func:`_resource_block`, keyed by a module's source
    path — scopes a wiring assertion to a single module call so a bare substring
    check cannot pass against the wrong call. Returns ``""`` when no module wires
    that path.
    """
    stripped = _strip_line_comments(text)
    start = re.search(
        r"^module\s+\w+\s+'" + re.escape(module_rel_path) + r"'\s*=",
        stripped,
        re.MULTILINE,
    )
    if start is None:
        return ""
    rest = stripped[start.end() :]
    nxt = re.search(r"^(?:resource|module|output)\b", rest, re.MULTILINE)
    return rest if nxt is None else rest[: nxt.start()]


def _declares_param(text: str, name: str) -> bool:
    """True iff ``text`` declares ``param <name> <type>`` (no default)."""
    stripped = _strip_line_comments(text)
    with_default = re.search(rf"^param\s+{re.escape(name)}\s+\w+\s*=", stripped, re.MULTILINE)
    declared = re.search(rf"^param\s+{re.escape(name)}\s+\w+\s*$", stripped, re.MULTILINE)
    return declared is not None and with_default is None


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


def test_apim_declares_openapi_operation() -> None:
    """``/openapi.json`` is served by its own explicit GET operation.

    The schema document is published unauthenticated so each deployment
    describes itself; without a dedicated literal-path operation the request
    falls to the ``/{*path}`` catch-all and its API-level validate-jwt, so the
    app-layer exemption alone would still 401 at the edge.
    """
    assert _declares_literal_get_operation(APIM.read_text(encoding="utf-8"), _OPENAPI_PATH), (
        f"apim.bicep must declare a dedicated GET operation with urlTemplate '{_OPENAPI_PATH}'"
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


def _set_body_json(policy_path: Path) -> dict[str, object]:
    """Parse the JSON document a return-response ``<set-body>`` emits, with APIM
    named-value tokens (``{{...}}``) neutralised to a placeholder so the body is
    valid JSON. Lets a test assert on the *shape* the edge actually returns.
    """
    xml = policy_path.read_text(encoding="utf-8")
    match = re.search(r"<set-body>(.*?)</set-body>", xml, re.S)
    assert match, f"{policy_path.name}: no <set-body> to parse"
    neutralised = re.sub(r"\{\{[^}]+\}\}", "NV", match.group(1))
    parsed = json.loads(neutralised)
    assert isinstance(parsed, dict)
    return parsed


def test_apim_register_response_carries_valid_redirect_uris() -> None:
    """The ``/register`` response must carry a non-empty ``redirect_uris`` array
    of parseable http(s) URLs.

    An RFC 7591 client parses this response against its full client-information
    schema, in which ``redirect_uris`` is a required, non-empty array of valid
    URLs (the MCP reference client's ``OAuthClientInformationFullSchema`` — a
    strict parse that throws when the field is absent). A response that omits it
    makes a standards MCP client (Claude Desktop, the claude.ai connector) report
    "couldn't register" before it ever reaches ``/authorize``, even though the
    201 and the echoed client_id are correct.
    """
    body = _set_body_json(REGISTER_OP_POLICY)
    uris = body.get("redirect_uris")
    assert isinstance(uris, list) and uris, (
        "the /register response must carry a non-empty redirect_uris array — the "
        "MCP client's registration-response schema requires it, and omitting it "
        "fails the client's strict parse before /authorize"
    )
    for uri in uris:
        assert isinstance(uri, str), "each redirect_uris entry must be a string URL"
        parsed = urlparse(uri)
        assert parsed.scheme in {"http", "https"} and parsed.netloc, (
            f"redirect_uris entry {uri!r} must be a parseable http(s) URL "
            "(the client validates each against a safe-URL schema)"
        )


def test_apim_advertised_scopes_are_resource_url_qualified() -> None:
    """Every edge-advertised scope must be ``{{sage-resource-url}}/Sage.Access``
    — the https custom-domain identity — never the bare ``Sage.Access`` and
    never the ``{{sage-audience}}`` (api://<app-id>) form.

    A standards MCP client composes its ``/authorize`` scope parameter directly
    from the advertised value, and sends an RFC 8707 ``resource`` parameter (its
    server URL) alongside it. Each wrong form fails a different way, and each
    reached production:

    * the bare scope name leaves Entra unable to resolve which resource it
      belongs to; it defaults to Microsoft Graph and rejects the request
      (AADSTS650053: the scope "doesn't exist on the resource").
    * the api://<app-id>-qualified form resolves, but can never be consistent
      with the client's https ``resource`` parameter — Entra rejects the request
      pre-authentication (AADSTS9010010, ``invalid_target``) before the login
      page renders.

    Only the {{sage-resource-url}} (https custom-domain) prefix satisfies both
    Entra's resource↔scope consistency check and the client's own RFC 9728
    origin validation; the custom domain is registered as an identifier URI on
    the Entra resource app so the scope resolves.
    """
    for policy in (DISCOVERY_OP_POLICY, AS_METADATA_OP_POLICY, REGISTER_OP_POLICY):
        xml = policy.read_text(encoding="utf-8")
        assert "{{sage-resource-url}}/Sage.Access" in xml, (
            f"{policy.name}: the advertised scope must be qualified with the "
            "https custom-domain identity ({{sage-resource-url}}/Sage.Access)"
        )
        assert '"Sage.Access"' not in xml and "'Sage.Access'" not in xml, (
            f"{policy.name}: the bare, unqualified 'Sage.Access' scope must not "
            "appear — Entra can't resolve it to the SAGE resource (AADSTS650053)"
        )
        assert "{{sage-audience}}/Sage.Access" not in xml, (
            f"{policy.name}: the api://-form ({{{{sage-audience}}}}) scope prefix "
            "must not appear — it can never match the client's https RFC 8707 "
            "resource parameter and Entra rejects /authorize pre-authentication "
            "(AADSTS9010010, invalid_target)"
        )


def test_apim_resource_url_named_value_is_custom_domain() -> None:
    """The ``sage-resource-url`` named value must be built from the
    ``sageCustomDomain`` parameter — the public identity an MCP client connects
    to — never the gateway's default ``*.azure-api.net`` address.

    An MCP client validates that the protected-resource metadata's ``resource``
    matches the server origin it connected to (RFC 9728 confused-deputy
    protection: the reference client throws "Protected resource ... does not
    match expected" on a mismatch), and composes its RFC 8707 ``resource``
    authorize parameter from it. Advertising the internal gateway host breaks
    both — verified live on cor-prod.
    """
    text = _strip_line_comments(APIM.read_text(encoding="utf-8"))
    block = re.search(r"name:\s*'sage-resource-url'.*?value:\s*([^\n]+)", text, re.S)
    assert block, "apim.bicep must declare the 'sage-resource-url' named value"
    value = block.group(1).strip()
    assert "sageCustomDomain" in value, (
        "sage-resource-url must be built from the sageCustomDomain parameter "
        f"(the public host), got: {value}"
    )
    assert "gatewayUrl" not in value, (
        "sage-resource-url must not advertise the gateway's default "
        "*.azure-api.net address — an MCP client rejects a resource that does "
        "not match the origin it connected to (RFC 9728)"
    )


def test_apim_declares_path_inserted_discovery_operations() -> None:
    """apim.bicep declares a dedicated literal GET operation for each MCP
    mount's RFC 9728 path-inserted protected-resource-metadata document.

    The catch-all 401 challenge steers a denied MCP client to its mount's
    document; without a literal operation the path falls through to the
    catch-all and validate-jwt 401s the (necessarily unauthenticated) discovery
    fetch.
    """
    text = APIM.read_text(encoding="utf-8")
    for path in (
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-protected-resource/mcp_maint",
        "/.well-known/oauth-protected-resource/mcp_admin",
    ):
        assert _declares_literal_get_operation(text, path), (
            f"apim.bicep must declare a dedicated GET operation with urlTemplate '{path}'"
        )


def test_apim_mount_discovery_docs_advertise_path_carrying_resource() -> None:
    """Each mount's path-inserted metadata document advertises the
    PATH-CARRYING mount URI as its ``resource`` — never the bare host.

    A standards MCP client normalizes the advertised resource through a URL
    object and sends its serialized form as the RFC 8707 ``resource``
    /authorize parameter. A bare-origin URL serializes WITH a trailing slash
    (``https://host`` -> ``https://host/``), a form Entra can neither match
    against a registered identifier URI (the match is byte-for-byte;
    AADSTS9010010) nor register as one (rejected as an invalid alias) — so a
    client steered to a bare-host resource dead-ends at /authorize with no
    registrable fix. A path-carrying URL serializes byte-identically, and the
    mount URIs are registered by the bootstrap. Verified live on cor-prod.
    """
    for policy, mount in (
        (DISCOVERY_MCP_OP_POLICY, "/mcp"),
        (DISCOVERY_MCP_MAINT_OP_POLICY, "/mcp_maint"),
        (DISCOVERY_MCP_ADMIN_OP_POLICY, "/mcp_admin"),
    ):
        body = _set_body_json(policy)
        # _set_body_json neutralises {{...}} tokens to NV, so the mount doc's
        # resource "{{sage-resource-url}}/mcp" parses as "NV/mcp".
        assert body.get("resource") == f"NV{mount}", (
            f"{policy.name}: the document's resource must be the path-carrying "
            f"mount URI ({{{{sage-resource-url}}}}{mount}), got {body.get('resource')!r} "
            "— a bare-host resource serializes to a trailing-slash form Entra "
            "can never match (AADSTS9010010)"
        )
        assert body.get("authorization_servers") == ["NV"], (
            f"{policy.name}: authorization_servers must be the facade root "
            "({{sage-resource-url}}), where the AS metadata well-known lives"
        )
        assert body.get("scopes_supported") == ["NV/Sage.Access", "offline_access"], (
            f"{policy.name}: the advertised scopes are the host-qualified "
            "{{sage-resource-url}}/Sage.Access plus the bare OIDC offline_access "
            "scope — the latter is what makes Entra mint a refresh token, so an "
            "expired access token renews without a fresh /authorize round trip"
        )
        xml = policy.read_text(encoding="utf-8")
        inbound = _inbound_section(xml)
        assert "validate-jwt" not in inbound and "<base" not in inbound, (
            f"{policy.name}: the mount discovery operation must be unauthenticated "
            "(no validate-jwt, no <base/> inheriting the API-level policy)"
        )
        assert "<cors" in inbound, (
            f"{policy.name}: the anonymous operation must carry its own <cors> "
            "so the actual response a browser reads returns Access-Control-Allow-*"
        )


def test_apim_edge_documents_advertise_offline_access() -> None:
    """Every edge document that advertises a requestable scope must include the
    OIDC ``offline_access`` scope, so a standards MCP client asks for it at
    ``/authorize`` and Entra mints a refresh token. Without it the client's v2
    access token (60–90 min) expires with no way to renew the session but a fresh
    ``/authorize`` round trip. ``offline_access`` is a bare OIDC scope, never
    resource-qualified (CAS-ADR-042).

    The scope is advertised uniformly across all six documents so that whichever
    one the client composes its scope from — a mount protected-resource metadata
    document, the root document, the authorization-server metadata, or the DCR
    ``/register`` registration — offline_access is present.
    """
    for policy in (
        DISCOVERY_OP_POLICY,
        DISCOVERY_MCP_OP_POLICY,
        DISCOVERY_MCP_MAINT_OP_POLICY,
        DISCOVERY_MCP_ADMIN_OP_POLICY,
        AS_METADATA_OP_POLICY,
    ):
        scopes = _set_body_json(policy).get("scopes_supported")
        assert isinstance(scopes, list) and "offline_access" in scopes, (
            f"{policy.name}: scopes_supported must include the bare 'offline_access' "
            f"OIDC scope so the client requests a refresh token, got {scopes!r}"
        )
    # The /register response carries one space-delimited OAuth scope string; a DCR
    # client requests exactly the scope it was registered with.
    scope = _set_body_json(REGISTER_OP_POLICY).get("scope")
    assert isinstance(scope, str) and "offline_access" in scope.split(), (
        f"{REGISTER_OP_POLICY.name}: the /register scope string must include "
        f"'offline_access' as a space-delimited token, got {scope!r}"
    )


def test_apim_as_metadata_advertises_both_grants() -> None:
    """The authorization-server metadata advertises exactly the two grants the
    edge honors: ``authorization_code`` for the interactive browser flow and
    ``client_credentials`` for machine callers bearing the ``Sage.Reader`` app
    role (CAS-ADR-042).

    A conformant OAuth client reads discovery before it acts and will not
    attempt a grant the resource does not advertise, so an unadvertised
    ``client_credentials`` is indistinguishable from an unsupported one.

    The assertion is exact-set equality, not containment: a containment check
    (``"authorization_code" in grants``) is satisfied both before and after the
    grant is added and would gate nothing. An *extra* grant is its own failure
    -- advertising one the edge cannot honor leads a client to attempt the flow
    and fail at the issuer rather than fall back.
    """
    body = _set_body_json(AS_METADATA_OP_POLICY)
    grants = body.get("grant_types_supported")
    assert isinstance(grants, list), (
        f"{AS_METADATA_OP_POLICY.name}: grant_types_supported must be a list, got {grants!r}"
    )
    assert set(grants) == {"authorization_code", "client_credentials"}, (
        f"{AS_METADATA_OP_POLICY.name}: grant_types_supported must advertise exactly "
        "authorization_code (the interactive browser flow) and client_credentials "
        f"(machine callers bearing the Sage.Reader app role), got {grants!r}"
    )
    assert len(grants) == 2, (
        f"{AS_METADATA_OP_POLICY.name}: grant_types_supported must carry no duplicate "
        f"or extra grant beyond the two the edge honors, got {grants!r}"
    )
    # _set_body_json neutralises {{sage-resource-url}} to NV. The advertised scope
    # set is untouched by the grant change: the machine leg authorizes on the
    # Sage.Reader *role*, not on a new scope, so a scopes_supported edit here
    # would be an unintended widening.
    assert body.get("scopes_supported") == ["NV/Sage.Access", "offline_access"], (
        f"{AS_METADATA_OP_POLICY.name}: scopes_supported must remain the host-qualified "
        "{{sage-resource-url}}/Sage.Access plus the bare OIDC offline_access -- the "
        "client-credentials leg authorizes on the Sage.Reader role, not a new scope, "
        f"got {body.get('scopes_supported')!r}"
    )


def test_apim_as_metadata_auth_methods_cover_the_advertised_grants() -> None:
    """Every advertised grant has a client-authentication method that can carry it.

    RFC 8414 reads ``token_endpoint_auth_methods_supported`` as the set of client
    authentication methods the token endpoint accepts, and RFC 6749 4.4 requires a
    client_credentials request to authenticate the client. Advertising that grant
    beside a sole ``none`` therefore contradicts itself: a strict client finds the
    grant offered and no admissible way to authenticate for it.

    ``none`` must survive -- the DCR facade registers a public PKCE client, whose
    token request carries no client authentication at all (CAS-ADR-042). The set
    is narrowed to what CAS provisions rather than mirroring the issuer, the same
    discipline this document already applies to response_types_supported and
    code_challenge_methods_supported: the issuer's token endpoint also accepts
    private_key_jwt and self_signed_tls_client_auth, and CAS provisions neither.
    """
    body = _set_body_json(AS_METADATA_OP_POLICY)
    methods = body.get("token_endpoint_auth_methods_supported")
    assert isinstance(methods, list), (
        f"{AS_METADATA_OP_POLICY.name}: token_endpoint_auth_methods_supported must be "
        f"a list, got {methods!r}"
    )
    assert set(methods) == {"none", "client_secret_post", "client_secret_basic"}, (
        f"{AS_METADATA_OP_POLICY.name}: token_endpoint_auth_methods_supported must "
        "advertise none (the public PKCE client) plus the secret-bearing methods a "
        "confidential machine caller uses, and no method CAS does not provision; "
        f"got {methods!r}"
    )
    # The coherence invariant itself, asserted against the grant set rather than
    # restated as a literal: a future grant edit that reintroduces the mismatch
    # fails here even if the method list above were relaxed.
    grants = body.get("grant_types_supported")
    if isinstance(grants, list) and "client_credentials" in grants:
        assert set(methods) - {"none"}, (
            f"{AS_METADATA_OP_POLICY.name}: client_credentials is advertised but the "
            "only client-authentication method is 'none' -- RFC 6749 4.4 requires the "
            "client to authenticate, so a strict client can form no valid request"
        )


def test_apim_challenge_is_path_aware() -> None:
    """The catch-all 401 challenge steers each MCP mount's clients to that
    mount's path-inserted metadata document, with the root document as the
    fallback for every other path.

    The /mcp_maint and /mcp_admin branches must be tested BEFORE /mcp — /mcp
    is their string prefix, so in the reverse order every maintenance-mount
    request would be steered to the ordinary mount's document. (The two
    maintenance paths are not prefixes of each other, so their relative order
    is free.) The path conditions must use the round-trip-safe &quot;-escaped
    double-quoted attribute encoding (the loadTextContent -> ARM -> APIM
    pipeline corrupts the single-quote-inner-double form).
    """
    on_error = _on_error_section(API_POLICY.read_text(encoding="utf-8"))
    root = 'resource_metadata="{{sage-resource-url}}/.well-known/oauth-protected-resource"'
    mcp = 'resource_metadata="{{sage-resource-url}}/.well-known/oauth-protected-resource/mcp"'
    maint = (
        'resource_metadata="{{sage-resource-url}}/.well-known/oauth-protected-resource/mcp_maint"'
    )
    admin = (
        'resource_metadata="{{sage-resource-url}}/.well-known/oauth-protected-resource/mcp_admin"'
    )
    for challenge, label in (
        (root, "root fallback"),
        (mcp, "/mcp mount"),
        (maint, "/mcp_maint mount"),
        (admin, "/mcp_admin alias mount"),
    ):
        assert challenge in on_error, (
            f"the on-error challenge must carry the {label} resource_metadata pointer"
        )
    maint_cond = "StartsWith(&quot;/mcp_maint&quot;)"
    admin_cond = "StartsWith(&quot;/mcp_admin&quot;)"
    mcp_cond = "StartsWith(&quot;/mcp&quot;)"
    assert maint_cond in on_error and admin_cond in on_error and mcp_cond in on_error, (
        "the path conditions must use the round-trip-safe &quot;-escaped encoding"
    )
    for cond, label in ((maint_cond, "/mcp_maint"), (admin_cond, "/mcp_admin")):
        assert on_error.index(cond) < on_error.index(mcp_cond), (
            f"the {label} branch must be tested before /mcp — /mcp is its string "
            "prefix, so the reverse order steers maintenance clients to the wrong document"
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
    stray = [
        g
        for g in _GUID_RE.findall(APIM.read_text(encoding="utf-8"))
        if g.lower() != _METRICS_PUBLISHER_ROLE
    ]
    assert not stray, f"apim.bicep must not hardcode the mcp client id as a literal GUID: {stray}"


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


def test_apim_openapi_operation_policy_routes_to_backend_unauthenticated() -> None:
    """The /openapi.json operation routes to the SAGE backend unauthenticated.

    A real backend passthrough, not a canned return-response: the document a
    caller receives is the running process's own, so it cannot drift from the
    deployed routes and the edge has no second copy to maintain.
    """
    xml = OPENAPI_OP_POLICY.read_text(encoding="utf-8")
    inbound = _inbound_section(xml)
    assert re.search(r"set-backend-service\s+backend-id=\"sage-backend\"", inbound), (
        "the /openapi.json operation inbound must route to the sage-backend"
    )
    assert "validate-jwt" not in inbound, "the /openapi.json operation must not validate the JWT"
    assert "<base" not in inbound, (
        "the /openapi.json operation inbound must not call <base/> — that would inherit the "
        "API-level validate-jwt and 401 the schema document"
    )
    assert "return-response" not in inbound, (
        "/openapi.json must be a real backend passthrough, not a canned edge document — "
        "a canned copy would drift from the routes the deployment actually serves"
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
    op_policies = (
        DISCOVERY_OP_POLICY,
        DISCOVERY_MCP_OP_POLICY,
        DISCOVERY_MCP_MAINT_OP_POLICY,
        DISCOVERY_MCP_ADMIN_OP_POLICY,
        HEALTH_OP_POLICY,
        OPENAPI_OP_POLICY,
        AS_METADATA_OP_POLICY,
        REGISTER_OP_POLICY,
        UPLOAD_OP_POLICY,
        DOWNLOAD_OP_POLICY,
    )
    for policy in op_policies:
        assert policy.name in loaded_names, (
            f"apim.bicep must loadTextContent the operation policy '{policy.name}'"
        )
        assert policy.is_file(), (
            f"operation policy XML must exist under infra/policies/: {policy.name}"
        )


def test_apim_policy_routes_maintenance_mounts_through_jwt() -> None:
    """Both maintenance mount paths route through the facade under the same
    JWT validation as the ordinary surface — neither is denied at the edge.

    Authorization is uniform across surfaces: the policy must not intercept a
    maintenance mount with its own branch; each flows down the ``<otherwise>``
    branch that validates the JWT and routes to the backend, like every other
    path.
    """
    # Uniform AUTHORIZATION is an inbound property: no <when> branch in any
    # loaded policy's <inbound> may single out a maintenance mount (the shape
    # of the removed maintenance-mount deny). The <on-error> block legitimately
    # branches on the path — the 401 challenge points each mount's clients at
    # its own RFC 9728 path-inserted metadata document — and that varies only
    # WHICH discovery URL a denied client reads, never whether a request is
    # validated or routed.
    policy = _policy_text()
    for mount in (_MAINT_MOUNT, _ADMIN_MOUNT):
        for match in re.finditer(r"<inbound>.*?</inbound>", policy, re.DOTALL):
            assert not _policy_special_cases_path(match.group(0), mount), (
                f"no <inbound> may single out {mount} in a <when> branch; it "
                "routes uniformly through the JWT-validating <otherwise> branch"
            )
    assert re.search(r"set-backend-service\s+backend-id=\"sage-backend\"", policy), (
        "the <otherwise> branch must route forwarded requests to the sage-backend "
        f"(the paths {_MAINT_MOUNT} and {_ADMIN_MOUNT} flow down)"
    )
    # Routing stays via the existing catch-all operation + policy: the module
    # must not declare an operation whose urlTemplate is a maintenance MOUNT
    # itself. (The path-inserted discovery operations /.well-known/.../mcp_maint
    # and .../mcp_admin serve metadata documents about the mounts; they do not
    # route mount traffic.)
    module_text = _strip_line_comments(APIM.read_text(encoding="utf-8"))
    for mount in (_MAINT_MOUNT, _ADMIN_MOUNT):
        assert not re.search(rf"urlTemplate:\s*'{mount}'", module_text), (
            f"apim.bicep must not declare a per-path operation routing {mount}; "
            "it routes via the catch-all operation and the inbound policy"
        )


def test_apim_no_hardcoded_identity_or_env_url_in_bicep() -> None:
    """The tenant is derived from a deploy-time ARM function (not a literal
    GUID), no identity GUID is hardcoded, and no Entra authority URL is baked
    into the Bicep — the authority host belongs to the versioned policy XML only.

    The one sanctioned GUID is the Monitoring Metrics Publisher role id: a
    fixed, public Azure constant naming a built-in role, not an identity
    coordinate. Any other GUID-shaped literal fails the gate.
    """
    text = APIM.read_text(encoding="utf-8")
    assert "subscription().tenantId" in _strip_line_comments(text), (
        "the tenant id must be derived from subscription().tenantId, not hardcoded"
    )
    stray = [g for g in _GUID_RE.findall(text) if g.lower() != _METRICS_PUBLISHER_ROLE]
    assert not stray, f"apim.bicep must not hardcode an identity GUID: {stray}"
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
    # The schema document is fetched cross-origin by browser-based API
    # explorers and codegen UIs pointed at a deployment's URL, so it needs the
    # Allow-Origin header on its own response the way /health does not.
    OPENAPI_OP_POLICY,
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
    and the preflight ``OPTIONS`` itself. The Streamable HTTP transport adds two
    protocol headers a browser-context client sends after initialize
    (``Mcp-Session-Id``, ``MCP-Protocol-Version``) and one response header the
    client must be able to read (``Mcp-Session-Id``, via ``<expose-headers>``) —
    a stateless server never mints a session id, but a client blocked from even
    sending the header would break the moment the transport turns stateful.
    """
    cors = _cors_block(API_POLICY.read_text(encoding="utf-8"))
    assert cors, "the API-level policy must carry a <cors> block"
    for header in ("Authorization", "Content-Type", "Mcp-Session-Id", "MCP-Protocol-Version"):
        assert f"<header>{header}</header>" in cors, (
            f"the <cors> allowed-headers must admit {header!r} (a browser MCP client sends it)"
        )
    for method in ("GET", "POST", "OPTIONS"):
        assert f"<method>{method}</method>" in cors, (
            f"the <cors> allowed-methods must admit {method!r}"
        )
    expose = re.search(r"<expose-headers>.*?</expose-headers>", cors, re.DOTALL)
    assert expose is not None and "<header>Mcp-Session-Id</header>" in expose.group(0), (
        "the <cors> block must expose Mcp-Session-Id so a browser-context MCP "
        "client can read the session id off the initialize response"
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


# ---------------------------------------------------------------------------
# Diagnostic settings — gateway logs + metrics to the foundation workspace
# ---------------------------------------------------------------------------


def test_apim_declares_log_analytics_workspace_param() -> None:
    """The module takes the Log Analytics workspace id as a required parameter.

    The workspace is provisioned by the foundation module; its id is wired in by
    the orchestrator (see :func:`test_main_bicep_wires_workspace_into_apim`). A
    required param (no default) keeps the id out of the module — and the
    error-level ``no-unused-params`` lint rule forces it to actually be consumed
    by the diagnostic-settings resource below.
    """
    text = APIM.read_text(encoding="utf-8")
    assert _declares_param(text, _WORKSPACE_PARAM), (
        f"apim.bicep must declare a required 'param {_WORKSPACE_PARAM} string' (no default)"
    )


def test_apim_declares_diagnostic_settings_to_workspace() -> None:
    """The APIM service routes platform metrics — and only metrics — to the workspace.

    Resource-level ``Microsoft.Insights/diagnosticSettings`` scoped to the APIM
    service, with ``workspaceId`` bound to the workspace param and the
    ``AllMetrics`` category enabled. No log category may be routed: the deployed
    (Consumption) tier collects no resource logs at all, so a ``logs`` entry —
    or the ``logAnalyticsDestinationType`` that only matters for one — is inert
    config masquerading as coverage. Request-level observability rides the
    Application Insights plane instead (gates below).
    """
    text = APIM.read_text(encoding="utf-8")
    assert _declares_resource_type(text, _DIAGNOSTIC_SETTINGS_TYPE), (
        f"apim.bicep must declare a {_DIAGNOSTIC_SETTINGS_TYPE} resource"
    )
    block = _resource_block(text, _DIAGNOSTIC_SETTINGS_TYPE)
    assert "scope: apimService" in block, (
        "the diagnostic settings must be scoped to the APIM service (scope: apimService)"
    )
    assert f"workspaceId: {_WORKSPACE_PARAM}" in block, (
        f"the diagnostic settings must bind workspaceId to the {_WORKSPACE_PARAM} param"
    )
    assert f"category: '{_METRICS_CATEGORY}'" in block, (
        f"the diagnostic settings must route the {_METRICS_CATEGORY} metrics category"
    )
    assert "enabled: true" in block, "the metrics category must be enabled: true"
    for inert in ("logs:", "GatewayLogs", "logAnalyticsDestinationType"):
        assert inert not in block, (
            f"the diagnostic settings must not carry '{inert}' — resource logs are "
            "never collected on the deployed tier, so routing them is inert config"
        )


def test_apim_diagnostic_settings_has_no_retention_override() -> None:
    """Retention follows the workspace, not a per-setting override.

    For a Log Analytics destination the diagnostic-setting ``retentionPolicy`` is
    deprecated — retention is governed by the workspace's own
    ``logAnalyticsRetentionDays``. The block must therefore carry no retention
    override.
    """
    block = _resource_block(APIM.read_text(encoding="utf-8"), _DIAGNOSTIC_SETTINGS_TYPE)
    assert block, "no diagnostic settings block found to check for a retention override"
    assert "retentionPolicy" not in block, (
        "the diagnostic settings must not set a retentionPolicy — retention follows "
        "the workspace's logAnalyticsRetentionDays"
    )
    assert "retentionInDays" not in block, (
        "the diagnostic settings must not set retentionInDays — retention follows the workspace"
    )


def test_apim_declares_app_insights_resource() -> None:
    """The module provisions the workspace-based Application Insights resource
    the gateway's request telemetry lands in.

    ``WorkspaceResourceId`` binds to the workspace param — workspace-based, not
    classic, so telemetry lands in the same foundation workspace as everything
    else. ``DisableLocalAuth: true`` forces Entra-authenticated ingestion: were
    instrumentation-key ingestion left open, telemetry could flow even with a
    broken role grant, and a live check would prove nothing about the
    managed-identity path.
    """
    text = APIM.read_text(encoding="utf-8")
    assert _declares_resource_type(text, _APP_INSIGHTS_TYPE), (
        f"apim.bicep must declare a {_APP_INSIGHTS_TYPE} resource"
    )
    block = _resource_block(text, _APP_INSIGHTS_TYPE)
    assert f"WorkspaceResourceId: {_WORKSPACE_PARAM}" in block, (
        f"the Application Insights resource must bind WorkspaceResourceId to the "
        f"{_WORKSPACE_PARAM} param (workspace-based, not classic)"
    )
    assert "DisableLocalAuth: true" in block, (
        "the Application Insights resource must set DisableLocalAuth: true so only "
        "Entra-authenticated ingestion is accepted"
    )
    assert "Application_Type: 'web'" in block, (
        "the Application Insights resource must declare Application_Type: 'web'"
    )


def test_apim_declares_app_insights_logger() -> None:
    """The facade's logger is an ``applicationInsights`` logger authenticated by
    the managed identity — no instrumentation key, no connection-string literal.

    ``connectionString`` must be a symbolic reference to the Application Insights
    resource's own property (never a literal), and ``identityClientId`` must bind
    the identity param so the gateway acquires its ingestion token via the
    user-assigned managed identity. An ``instrumentationKey`` credential anywhere
    in the block signals key-based auth — a secret in source — and fails the gate.
    """
    text = APIM.read_text(encoding="utf-8")
    assert _declares_resource_type(text, _APIM_LOGGER_TYPE), (
        f"apim.bicep must declare a {_APIM_LOGGER_TYPE} resource"
    )
    block = _resource_block(text, _APIM_LOGGER_TYPE)
    assert f"loggerType: '{_APP_INSIGHTS_LOGGER_TYPE}'" in block, (
        f"the logger must carry loggerType: '{_APP_INSIGHTS_LOGGER_TYPE}'"
    )
    assert "parent: apimService" in block, (
        "the logger must be a child of the APIM service (parent: apimService)"
    )
    assert re.search(r"connectionString:\s*\w+\.properties\.ConnectionString", block), (
        "credentials.connectionString must be a symbolic reference to the Application "
        "Insights resource's ConnectionString property, never a literal"
    )
    assert "identityClientId: sageIdentityClientId" in block, (
        "credentials.identityClientId must bind the sageIdentityClientId param so "
        "ingestion authenticates via the user-assigned managed identity"
    )
    assert "instrumentationKey" not in block, (
        "the logger must not carry an instrumentationKey — key-based auth puts a "
        "secret in source; ingestion authenticates via the managed identity"
    )


def test_apim_declares_app_insights_diagnostic() -> None:
    """The service-level diagnostic that makes the gateway emit per-request
    telemetry through the logger above.

    The instance name must be the reserved ``applicationinsights``; ``loggerId``
    must be a symbolic reference to the logger resource — never a hardcoded
    literal id. Sampling is pinned to 100% with ``allErrors`` so every request
    (and every error regardless of sampling) produces telemetry — the guarantee
    any live emission check rests on.
    """
    text = APIM.read_text(encoding="utf-8")
    assert _declares_resource_type(text, _APIM_DIAGNOSTIC_TYPE), (
        f"apim.bicep must declare a {_APIM_DIAGNOSTIC_TYPE} resource"
    )
    block = _resource_block(text, _APIM_DIAGNOSTIC_TYPE)
    assert f"name: '{_APP_INSIGHTS_DIAGNOSTIC_NAME}'" in block, (
        f"the diagnostic instance must be named '{_APP_INSIGHTS_DIAGNOSTIC_NAME}' "
        "(the reserved Application-Insights diagnostic id)"
    )
    assert "parent: apimService" in block, (
        "the diagnostic must be a child of the APIM service (parent: apimService)"
    )
    assert re.search(r"loggerId:\s*\w+\.id", block), (
        "loggerId must be a symbolic reference to the logger resource, never a hardcoded literal id"
    )
    assert "alwaysLog: 'allErrors'" in block, (
        "the diagnostic must set alwaysLog: 'allErrors' so errors emit regardless of sampling"
    )
    assert "samplingType: 'fixed'" in block and "percentage: 100" in block, (
        "sampling must be pinned to fixed 100% so every request produces telemetry"
    )


def test_apim_grants_metrics_publisher_to_identity() -> None:
    """The facade's managed identity may publish telemetry to the Application
    Insights resource — the grant Entra-authenticated ingestion depends on.

    Scoped to the Application Insights resource; the principal binds the
    principal-id param, never the client id (the two are distinct coordinates,
    and an assignment against the client id silently matches no principal); the
    role arrives through ``subscriptionResourceId`` with a ``guid()``-derived
    idempotent name.
    """
    text = APIM.read_text(encoding="utf-8")
    assert _declares_resource_type(text, _ROLE_ASSIGNMENT_TYPE), (
        f"apim.bicep must declare a {_ROLE_ASSIGNMENT_TYPE} resource"
    )
    block = _resource_block(text, _ROLE_ASSIGNMENT_TYPE)
    assert re.search(r"scope:\s*appInsights\b", block), (
        "the role assignment must be scoped to the Application Insights resource "
        "(scope: appInsights)"
    )
    assert "principalId: sageIdentityPrincipalId" in block, (
        "the role assignment must bind principalId to the sageIdentityPrincipalId "
        "param (the principal id, not the client id)"
    )
    assert "principalType: 'ServicePrincipal'" in block, (
        "the role assignment must declare principalType: 'ServicePrincipal'"
    )
    assert "subscriptionResourceId('Microsoft.Authorization/roleDefinitions'" in block, (
        "roleDefinitionId must be derived via subscriptionResourceId over the "
        "built-in role constant"
    )
    assert re.search(r"name:\s*guid\(", block), (
        "the role assignment name must be guid()-derived for idempotent redeploys"
    )


def test_apim_declares_identity_principal_param() -> None:
    """The identity's principal id arrives as a required parameter.

    The role assignment needs the service principal's object id; a required
    param (no default) keeps the coordinate out of the module, and the
    error-level ``no-unused-params`` lint rule forces it to be consumed.
    """
    text = APIM.read_text(encoding="utf-8")
    assert _declares_param(text, "sageIdentityPrincipalId"), (
        "apim.bicep must declare a required 'param sageIdentityPrincipalId string' (no default)"
    )


def test_main_bicep_wires_workspace_into_apim() -> None:
    """The orchestrator passes the foundation workspace id into the APIM module.

    Scoped to the ``apim`` module call so the wire cannot be satisfied by some
    other module receiving a foundation output. A symbolic reference to
    ``foundation.outputs.*`` also gives APIM an implicit ``dependsOn`` on the
    foundation, regardless of module declaration order.
    """
    block = _module_block(MAIN_BICEP.read_text(encoding="utf-8"), "modules/apim.bicep")
    assert block, "main.bicep declares no apim module call"
    assert re.search(
        rf"{_WORKSPACE_PARAM}:\s*foundation\.outputs\.logAnalyticsWorkspaceId", block
    ), (
        f"the apim module must receive {_WORKSPACE_PARAM} from "
        "foundation.outputs.logAnalyticsWorkspaceId"
    )


def test_main_bicep_wires_identity_principal_into_apim() -> None:
    """The orchestrator passes the identity's principal id into the APIM module.

    Scoped to the ``apim`` module call and pinned to the identity module's
    ``sageIdentityPrincipalId`` output — the principal id, not the client id,
    which a bare substring check would accept and which produces a role
    assignment that matches no principal.
    """
    block = _module_block(MAIN_BICEP.read_text(encoding="utf-8"), "modules/apim.bicep")
    assert block, "main.bicep declares no apim module call"
    assert re.search(
        r"sageIdentityPrincipalId:\s*identity\.outputs\.sageIdentityPrincipalId", block
    ), (
        "the apim module must receive sageIdentityPrincipalId from "
        "identity.outputs.sageIdentityPrincipalId"
    )


def test_apim_diagnostic_detectors_control() -> None:
    """The diagnostic detectors fire on the regressions they target and clear on
    the correct form — so the gates above cannot pass coincidentally.

    ``_resource_block`` must return "" when the resource is absent and the block
    otherwise; ``_declares_param`` must reject a defaulted param and a bare mention
    while accepting the required declaration; and a ``retentionPolicy`` inside the
    block must be visible to the no-override check.
    """
    absent = "resource other 'Microsoft.ApiManagement/service@2022-08-01' = {\n  name: 'x'\n}\n"
    assert _resource_block(absent, _DIAGNOSTIC_SETTINGS_TYPE) == "", (
        "detector must not find a diagnostic settings block when none is declared"
    )
    present = (
        "resource d 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {\n"
        "  scope: apimService\n"
        "  properties: {\n"
        "    workspaceId: logAnalyticsWorkspaceId\n"
        "    retentionPolicy: { days: 7, enabled: true }\n"
        "  }\n"
        "}\n"
        "output x string = d.id\n"
    )
    block = _resource_block(present, _DIAGNOSTIC_SETTINGS_TYPE)
    assert "workspaceId: logAnalyticsWorkspaceId" in block
    assert "retentionPolicy" in block, (
        "a retention override must be visible to the no-override gate"
    )
    # Param detector: required accepted; defaulted and comment-only rejected.
    assert _declares_param("param logAnalyticsWorkspaceId string\n", _WORKSPACE_PARAM)
    assert not _declares_param("param logAnalyticsWorkspaceId string = ''\n", _WORKSPACE_PARAM)
    assert not _declares_param("// param logAnalyticsWorkspaceId string\n", _WORKSPACE_PARAM)

    # Inert resource-log config is visible to the metrics-only assertions: a
    # block carrying a log category or a destination type exposes every substring
    # the gate forbids, and stripping them clears the gate while the metrics
    # category stays detectable.
    inert = (
        "resource d 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {\n"
        "  scope: apimService\n"
        "  properties: {\n"
        "    workspaceId: logAnalyticsWorkspaceId\n"
        "    logAnalyticsDestinationType: 'Dedicated'\n"
        "    logs: [\n"
        "      {\n"
        "        category: 'GatewayLogs'\n"
        "        enabled: true\n"
        "      }\n"
        "    ]\n"
        "    metrics: [\n"
        "      {\n"
        "        category: 'AllMetrics'\n"
        "        enabled: true\n"
        "      }\n"
        "    ]\n"
        "  }\n"
        "}\n"
        "output x string = d.id\n"
    )
    inert_block = _resource_block(inert, _DIAGNOSTIC_SETTINGS_TYPE)
    for token in ("logs:", "GatewayLogs", "logAnalyticsDestinationType"):
        assert token in inert_block, f"inert-config token '{token}' must be visible"
    clean = (
        "resource d 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {\n"
        "  scope: apimService\n"
        "  properties: {\n"
        "    workspaceId: logAnalyticsWorkspaceId\n"
        "    metrics: [\n"
        "      {\n"
        "        category: 'AllMetrics'\n"
        "        enabled: true\n"
        "      }\n"
        "    ]\n"
        "  }\n"
        "}\n"
        "output x string = d.id\n"
    )
    clean_block = _resource_block(clean, _DIAGNOSTIC_SETTINGS_TYPE)
    for token in ("logs:", "GatewayLogs", "logAnalyticsDestinationType"):
        assert token not in clean_block
    assert f"category: '{_METRICS_CATEGORY}'" in clean_block


def test_apim_app_insights_detectors_control() -> None:
    """The telemetry-plane detectors distinguish the prefix-sharing APIM types,
    isolate adjacent blocks, and surface the malformed credential shapes — so
    the gates above cannot pass by accident.

    ``Microsoft.ApiManagement/service``, ``.../service/loggers``, and
    ``.../service/diagnostics`` share a prefix; the ``@``-anchored declaration
    match must treat them as distinct, and ``_resource_block`` must not bleed one
    resource's body into the next when several are adjacent (the real module
    declares them back-to-back). The credential assertions are substring/regex
    checks, controlled here so their presence/absence is proven meaningful.
    """
    present = (
        "resource s 'Microsoft.ApiManagement/service@2022-08-01' = {\n"
        "  name: 'x'\n"
        "}\n"
        "resource ai 'Microsoft.Insights/components@2020-02-02' = {\n"
        "  name: 'appi-x'\n"
        "  properties: {\n"
        "    WorkspaceResourceId: logAnalyticsWorkspaceId\n"
        "    DisableLocalAuth: true\n"
        "  }\n"
        "}\n"
        "resource ra 'Microsoft.Authorization/roleAssignments@2022-04-01' = {\n"
        "  scope: ai\n"
        "  name: guid(ai.id)\n"
        "  properties: {\n"
        "    principalId: sageIdentityPrincipalId\n"
        "  }\n"
        "}\n"
        "resource lg 'Microsoft.ApiManagement/service/loggers@2022-08-01' = {\n"
        "  parent: s\n"
        "  name: 'appinsights'\n"
        "  properties: {\n"
        "    loggerType: 'applicationInsights'\n"
        "    credentials: {\n"
        "      connectionString: ai.properties.ConnectionString\n"
        "      identityClientId: sageIdentityClientId\n"
        "    }\n"
        "  }\n"
        "}\n"
        "resource dg 'Microsoft.ApiManagement/service/diagnostics@2022-08-01' = {\n"
        "  parent: s\n"
        "  name: 'applicationinsights'\n"
        "  properties: {\n"
        "    loggerId: lg.id\n"
        "  }\n"
        "}\n"
        "output x string = s.id\n"
    )
    # The @-anchor distinguishes every declared type, prefix-sharing or not.
    assert _declares_resource_type(present, _APIM_SERVICE_TYPE)
    assert _declares_resource_type(present, _APIM_LOGGER_TYPE)
    assert _declares_resource_type(present, _APIM_DIAGNOSTIC_TYPE)
    assert _declares_resource_type(present, _APP_INSIGHTS_TYPE)
    assert _declares_resource_type(present, _ROLE_ASSIGNMENT_TYPE)

    # Adjacent-block isolation: the logger carries the credentials, the
    # diagnostic the loggerId, the role assignment the principalId — none bleeds
    # into another, so no assertion can be satisfied by the wrong resource.
    logger_block = _resource_block(present, _APIM_LOGGER_TYPE)
    assert re.search(r"connectionString:\s*\w+\.properties\.ConnectionString", logger_block)
    assert "loggerId" not in logger_block
    diag_block = _resource_block(present, _APIM_DIAGNOSTIC_TYPE)
    assert re.search(r"loggerId:\s*\w+\.id", diag_block)
    assert "credentials" not in diag_block
    ra_block = _resource_block(present, _ROLE_ASSIGNMENT_TYPE)
    assert "principalId: sageIdentityPrincipalId" in ra_block
    assert "connectionString" not in ra_block
    ai_block = _resource_block(present, _APP_INSIGHTS_TYPE)
    assert "DisableLocalAuth: true" in ai_block
    assert "principalId" not in ai_block

    # Malformed credential shapes are visible to the logger gate: a key-based
    # credential, a wrong logger type, and a stripped identityClientId must each
    # fail the corresponding assertion.
    keyed = present.replace(
        "      connectionString: ai.properties.ConnectionString\n",
        "      instrumentationKey: 'deadbeef'\n",
    )
    keyed_block = _resource_block(keyed, _APIM_LOGGER_TYPE)
    assert "instrumentationKey" in keyed_block
    assert not re.search(r"connectionString:\s*\w+\.properties\.ConnectionString", keyed_block)
    wrong_type = present.replace("loggerType: 'applicationInsights'", "loggerType: 'azureMonitor'")
    assert f"loggerType: '{_APP_INSIGHTS_LOGGER_TYPE}'" not in _resource_block(
        wrong_type, _APIM_LOGGER_TYPE
    )
    no_identity = present.replace("      identityClientId: sageIdentityClientId\n", "")
    assert "identityClientId" not in _resource_block(no_identity, _APIM_LOGGER_TYPE)

    # Absent: no telemetry-plane type resolves to a block when only the service
    # exists.
    absent = "resource s 'Microsoft.ApiManagement/service@2022-08-01' = {\n  name: 'x'\n}\n"
    assert _resource_block(absent, _APIM_LOGGER_TYPE) == ""
    assert _resource_block(absent, _APIM_DIAGNOSTIC_TYPE) == ""
    assert _resource_block(absent, _APP_INSIGHTS_TYPE) == ""
    assert _resource_block(absent, _ROLE_ASSIGNMENT_TYPE) == ""

    # The GUID allowlist: the sanctioned role constant clears the stray filter;
    # any other GUID-shaped literal fires it.
    sanctioned = f"var roleId = '{_METRICS_PUBLISHER_ROLE}'"
    assert not [g for g in _GUID_RE.findall(sanctioned) if g.lower() != _METRICS_PUBLISHER_ROLE]
    unsanctioned = "var other = '00000000-0000-0000-0000-000000000001'"
    assert [g for g in _GUID_RE.findall(unsanctioned) if g.lower() != _METRICS_PUBLISHER_ROLE]


# ---------------------------------------------------------------------------
# Transfer endpoints (caller-local byte channel)
# ---------------------------------------------------------------------------

_UPLOAD_PATH: Final[str] = "/upload"
_DOWNLOAD_PATH: Final[str] = "/download/{transferId}"


def test_apim_declares_transfer_upload_operation() -> None:
    """``/upload`` is served by its own dedicated PUT operation ahead of the
    catch-all, so its no-``<base/>`` policy can skip validate-jwt: the curl
    byte leg carries only the one-time transfer token, never an Entra bearer.
    """
    assert _declares_literal_operation(APIM.read_text(encoding="utf-8"), _UPLOAD_PATH, "PUT"), (
        f"apim.bicep must declare a dedicated PUT operation with urlTemplate '{_UPLOAD_PATH}'"
    )


def test_apim_declares_transfer_download_operation() -> None:
    """``/download/{transferId}`` is served by its own dedicated GET operation
    with a declared template parameter -- an undeclared parameter is the
    silent-404 failure mode the catch-all's declared ``path`` parameter guards
    against, reproduced here for the templated transfer path.
    """
    text = APIM.read_text(encoding="utf-8")
    assert _declares_literal_operation(text, _DOWNLOAD_PATH, "GET"), (
        f"apim.bicep must declare a dedicated GET operation with urlTemplate '{_DOWNLOAD_PATH}'"
    )
    stripped = _strip_line_comments(text)
    op_match = re.search(
        r"urlTemplate:\s*'" + re.escape(_DOWNLOAD_PATH) + r"'[\s\S]*?templateParameters:"
        r"[\s\S]*?name:\s*'transferId'",
        stripped,
    )
    assert op_match, (
        "the /download/{transferId} operation must declare its 'transferId' "
        "template parameter -- an undeclared parameter never matches and the "
        "gateway answers a generic 404"
    )


@pytest.mark.parametrize(
    "policy_path", [UPLOAD_OP_POLICY, DOWNLOAD_OP_POLICY], ids=lambda p: p.name
)
def test_apim_transfer_operation_policy_routes_to_backend_unauthenticated(
    policy_path: Path,
) -> None:
    """Each transfer operation routes to the SAGE backend with no JWT and no
    ``<base/>`` (which would inherit the API-level validate-jwt and 401 the
    tokenless curl), as a real backend passthrough -- the one-time transfer
    token is validated by the SAGE app, not the edge.
    """
    xml = policy_path.read_text(encoding="utf-8")
    inbound = _inbound_section(xml)
    assert re.search(r"set-backend-service\s+backend-id=\"sage-backend\"", inbound), (
        f"{policy_path.name}: inbound must route to the sage-backend"
    )
    assert "validate-jwt" not in inbound, (
        f"{policy_path.name}: must not validate the JWT (the transfer token is "
        "the sole credential, checked by the SAGE app)"
    )
    assert "<base" not in inbound, (
        f"{policy_path.name}: inbound must not call <base/> -- that would "
        "inherit the API-level validate-jwt and 401 the tokenless curl leg"
    )
    assert "return-response" not in inbound, (
        f"{policy_path.name}: must be a real backend passthrough, not a canned edge response"
    )
