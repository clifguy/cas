"""Behavioral gate for the preflight's edge checks.

A local stub server stands in for the tenant and the real script runs against
it. The load-bearing scenarios are *blanket-404* (a dead edge must not
coincidentally pass) and *one-failure-does-not-mask-others* (the independence
guarantee).
"""

from __future__ import annotations

import glob
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.deploy._preflight_harness import (
    _DISCOVERY_BODY,
    _HTTP_CHECKS,
    _NEEDS_RUNTIME,
    _base_env,
    _detail,
    _green,
    _run,
    _verdicts,
    _write_stub_cmd,
    serve,
)


@_NEEDS_RUNTIME
def test_all_green_passes() -> None:
    with serve(_green) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS=_HTTP_CHECKS))
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode == 0, f"healthy tenant must pass:\n{proc.stdout}\n{proc.stderr}"
    assert all(v == "PASS" for v in verdicts.values()), verdicts
    assert set(verdicts) == set(_HTTP_CHECKS.split(",")), verdicts


@_NEEDS_RUNTIME
def test_ocr_capability_passes_when_all_true() -> None:
    """/health advertising ocr with all three binaries true -> PASS. This is the
    positive case: the cloud image ships the OCR toolchain and the container
    computed the capability at startup."""
    with serve(_green) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="liveness,ocr_capability"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("ocr_capability") == "PASS", f"{verdicts}\n{proc.stdout}"


@_NEEDS_RUNTIME
def test_ocr_capability_fails_on_stale_image() -> None:
    """THE anti-coincidental control: a stale image predating the ocr field
    returns a healthy 2-field /health (status+version) with NO ocr block. That
    must FAIL -- a blanket 200 must not read as green. This is exactly the
    regression the gate exists to catch (the image that shipped without OCR)."""

    def stale(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0] == "/health":
            return 200, '{"status":"ok","version":"2.0.0"}', {}
        return _green(method, path, body)

    with serve(stale) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="liveness,ocr_capability"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("liveness") == "PASS", "the process is still up"
    assert verdicts.get("ocr_capability") == "FAIL", f"{verdicts}\n{proc.stdout}"


@_NEEDS_RUNTIME
def test_ocr_capability_fails_on_missing_binary() -> None:
    """/health advertises the ocr block but a binary is false (image built with
    the ocr Python extra but without the tesseract apt package) -> FAIL. Proves
    the check reads the per-binary booleans, not merely the block's presence."""

    def missing_tesseract(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0] == "/health":
            return (
                200,
                '{"status":"ok","version":"2.0.0",'
                '"ocr":{"ocrmypdf":true,"tesseract":false,"ghostscript":true}}',
                {},
            )
        return _green(method, path, body)

    with serve(missing_tesseract) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="liveness,ocr_capability"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("ocr_capability") == "FAIL", f"{verdicts}\n{proc.stdout}"
    assert "tesseract" in _detail(proc.stdout, "ocr_capability").lower()


@_NEEDS_RUNTIME
def test_blanket_404_fails_edge() -> None:
    """THE anti-coincidental test: when every path 404s, the bare /mcp '401'
    look must NOT be credited -- its discovery-200 control failed.
    """

    def blanket(_m: str, _p: str, _b: bytes) -> tuple[int, str, dict[str, str]]:
        return 404, '{"error":"not_found"}', {}

    with serve(blanket) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_discovery,edge_mcp_unauth"))
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode != 0, "a dead edge must fail the run"
    assert verdicts.get("edge_discovery") == "FAIL", verdicts
    assert verdicts.get("edge_mcp_unauth") == "FAIL", (
        "a 404 (not 401) with a failed discovery control must not pass as auth-gating"
    )


@_NEEDS_RUNTIME
def test_discovery_broken_but_mcp_401_fails_edge() -> None:
    """THE discriminating anti-coincidental test: /mcp answers 401 (the
    'as-predicted' look) while the discovery doc is broken (404). A naive check
    that credits the bare 401 would PASS on a dead edge; the discovery-200
    control must reject it.
    """

    def discovery_broken(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        p = path.split("?", 1)[0]
        if p == "/.well-known/oauth-protected-resource":
            return 404, '{"error":"not_found"}', {}
        if p == "/mcp":
            return 401, "", {"WWW-Authenticate": 'Bearer resource_metadata="x"'}
        return _green(method, path, body)

    with serve(discovery_broken) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_discovery,edge_mcp_unauth"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_discovery") == "FAIL", verdicts
    assert verdicts.get("edge_mcp_unauth") == "FAIL", (
        "a 401 must NOT be credited when the discovery-200 control failed -- "
        f"this is the blanket-edge coincidental-pass trap: {verdicts}"
    )
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_mcp_open_fails_edge() -> None:
    """Discovery live but /mcp answers 200 (auth not enforced) -> edge FAIL."""

    def mcp_open(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0] == "/mcp":
            return 200, '{"oops":"unauthenticated reached backend"}', {}
        return _green(method, path, body)

    with serve(mcp_open) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_discovery,edge_mcp_unauth"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_discovery") == "PASS", verdicts
    assert verdicts.get("edge_mcp_unauth") == "FAIL", "an open /mcp must fail the auth-gating check"
    assert proc.returncode != 0


def _redirect_stub(
    browser_status: int, machine_status: int, discovery_status: int = 200
) -> Callable[[str, str, bytes, str], "tuple[int, str, dict[str, str]]"]:
    """A 4-arg (Accept-sensitive) stub for the browser-redirect check: /mcp answers
    ``browser_status`` for an ``Accept: text/html`` request and ``machine_status``
    otherwise; the discovery doc answers ``discovery_status`` (200 = edge live).
    """

    def stub(_method: str, path: str, _body: bytes, accept: str) -> tuple[int, str, dict[str, str]]:
        p = path.split("?", 1)[0]
        if p == "/.well-known/oauth-protected-resource":
            return discovery_status, (_DISCOVERY_BODY if discovery_status == 200 else "{}"), {}
        if p == "/mcp":
            if "text/html" in accept:
                return browser_status, "", {"Location": "https://cas.test.invalid/"}
            return machine_status, "", {"WWW-Authenticate": 'Bearer resource_metadata="x"'}
        return 404, '{"error":"not_found"}', {}

    return stub


@_NEEDS_RUNTIME
def test_browser_redirect_passes() -> None:
    """Browser (Accept: text/html) -> 302 while the machine Accept still 401s, edge
    live -> edge_browser_redirect PASS. The deploy-verified half of criterion #5.
    """
    with serve(_redirect_stub(browser_status=302, machine_status=401)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_browser_redirect"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_browser_redirect") == "PASS", (proc.stdout, proc.stderr)
    assert proc.returncode == 0


@_NEEDS_RUNTIME
def test_browser_redirect_no_302_fails() -> None:
    """THE criterion-#5 regression: the policy applies cleanly but the Accept-match
    silently never fires, so a browser still gets 401. The check must FAIL -- not
    pass on the healthy-looking 401 (the &quot; round-trip silently broke).
    """
    with serve(_redirect_stub(browser_status=401, machine_status=401)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_browser_redirect"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_browser_redirect") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_browser_redirect_blanket_302_fails() -> None:
    """A blanket 302 (every Accept redirected, machine clients included) must FAIL --
    it would break the MCP OAuth handshake. Proves the check discriminates by Accept,
    not merely that *some* 302 appears.
    """
    with serve(_redirect_stub(browser_status=302, machine_status=302)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_browser_redirect"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_browser_redirect") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_browser_redirect_dead_edge_fails() -> None:
    """Discovery doc broken (404): a browser 302 must NOT be credited -- the
    discovery-200 control rejects it (the blanket-edge coincidental-pass trap).
    """
    with serve(_redirect_stub(browser_status=302, machine_status=401, discovery_status=404)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_browser_redirect"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_browser_redirect") == "FAIL", verdicts
    assert proc.returncode != 0


def _cors_stub(
    preflight_status: int, with_cors_headers: bool = True, discovery_status: int = 200
) -> Callable[[str, str, bytes], "tuple[int, str, dict[str, str]]"]:
    """A stub for the CORS-preflight check: a preflight ``OPTIONS`` (to any path)
    answers ``preflight_status`` and, when ``with_cors_headers``, carries the
    ``Access-Control-Allow-*`` headers APIM's ``<cors>`` policy emits; the
    discovery doc answers ``discovery_status`` (200 = edge live).
    """

    def stub(method: str, path: str, _body: bytes) -> tuple[int, str, dict[str, str]]:
        p = path.split("?", 1)[0]
        if p == "/.well-known/oauth-protected-resource":
            return discovery_status, (_DISCOVERY_BODY if discovery_status == 200 else "{}"), {}
        if method == "OPTIONS":
            headers: dict[str, str] = {}
            if with_cors_headers:
                headers = {
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
                    "Access-Control-Allow-Headers": "Authorization,Content-Type",
                }
            return preflight_status, "", headers
        return 404, '{"error":"not_found"}', {}

    return stub


@_NEEDS_RUNTIME
def test_cors_preflight_passes() -> None:
    """Preflight OPTIONS answers 2xx with Access-Control-Allow-* and the edge is
    live -> edge_cors_preflight PASS. The deploy-verified source of criterion #6.
    """
    with serve(_cors_stub(preflight_status=200, with_cors_headers=True)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_cors_preflight"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_cors_preflight") == "PASS", (proc.stdout, proc.stderr)
    assert proc.returncode == 0


@_NEEDS_RUNTIME
def test_cors_preflight_401_fails() -> None:
    """THE preflight-gate regression: validate-jwt 401s the preflight OPTIONS and emits no
    CORS headers, so the browser blocks the call. The check must FAIL -- catching
    the exact production defect this ticket fixes, not passing on a bare status.
    """
    with serve(_cors_stub(preflight_status=401, with_cors_headers=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_cors_preflight"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_cors_preflight") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_cors_preflight_missing_headers_fails() -> None:
    """A 2xx preflight that carries NO Access-Control-Allow-Origin must FAIL -- a
    browser still blocks the call. Proves the check asserts on the CORS header,
    not merely on a 2xx status (the coincidental-pass trap this criterion guards).
    """
    with serve(_cors_stub(preflight_status=200, with_cors_headers=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_cors_preflight"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_cors_preflight") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_cors_preflight_dead_edge_fails() -> None:
    """Discovery doc broken (404): a CORS-carrying preflight must NOT be credited
    -- the discovery-200 control rejects it (the blanket-edge coincidental-pass trap).
    """
    with serve(
        _cors_stub(preflight_status=200, with_cors_headers=True, discovery_status=404)
    ) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_cors_preflight"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_cors_preflight") == "FAIL", verdicts
    assert proc.returncode != 0


def _dcr_stub(
    with_redirect_uris: bool = True,
    qualified_scope: bool = True,
    status: int = 201,
    discovery_status: int = 200,
) -> Callable[[str, str, bytes], "tuple[int, str, dict[str, str]]"]:
    """A stub for the DCR-registration check: ``POST /register`` answers ``status``
    with a body that (optionally) carries a non-empty ``redirect_uris`` array and
    a (optionally) resource-qualified ``scope``; the discovery doc answers
    ``discovery_status`` (200 = edge live). Toggling either field off reproduces a
    production regression the check must catch.
    """

    def stub(method: str, path: str, _body: bytes) -> tuple[int, str, dict[str, str]]:
        p = path.split("?", 1)[0]
        if p == "/.well-known/oauth-protected-resource":
            return discovery_status, (_DISCOVERY_BODY if discovery_status == 200 else "{}"), {}
        if method == "POST" and p == "/register":
            fields = ['"client_id": "NV"', '"token_endpoint_auth_method": "none"']
            if with_redirect_uris:
                fields.append('"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]')
            scope = (
                "api://sage-app-id/Sage.Access offline_access" if qualified_scope else "Sage.Access"
            )
            fields.append(f'"scope": "{scope}"')
            return status, "{" + ", ".join(fields) + "}", {}
        return 404, '{"error":"not_found"}', {}

    return stub


@_NEEDS_RUNTIME
def test_dcr_registration_passes() -> None:
    """A /register 201 whose body carries a non-empty redirect_uris array and a
    resource-qualified scope, edge live -> edge_dcr_registration PASS. This is the
    live DCR sign-in leg the earlier edge work left deploy-only-verifiable.
    """
    with serve(_dcr_stub()) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_dcr_registration"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_dcr_registration") == "PASS", (proc.stdout, proc.stderr)
    assert proc.returncode == 0


@_NEEDS_RUNTIME
def test_dcr_registration_missing_redirect_uris_fails() -> None:
    """THE reported regression: /register returns a correct 201 + client_id but no
    redirect_uris, so a standards MCP client's registration-response parse throws
    ("couldn't register") before /authorize. The check must FAIL on this body, not
    pass on the 201.
    """
    with serve(_dcr_stub(with_redirect_uris=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_dcr_registration"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_dcr_registration") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_dcr_registration_bare_scope_fails() -> None:
    """A /register 201 that advertises the bare "Sage.Access" scope must FAIL --
    Entra can't bind the unqualified scope to the SAGE resource and rejects
    /authorize (AADSTS650053). Proves the check asserts on scope qualification,
    not merely on a 2xx + redirect_uris.
    """
    with serve(_dcr_stub(qualified_scope=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_dcr_registration"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_dcr_registration") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_dcr_registration_dead_edge_fails() -> None:
    """Discovery doc broken (404): a well-formed /register body must NOT be
    credited -- the discovery-200 control rejects it (the blanket-edge
    coincidental-pass trap).
    """
    with serve(_dcr_stub(discovery_status=404)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_dcr_registration"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_dcr_registration") == "FAIL", verdicts
    assert proc.returncode != 0


def _resource_identity_stub(
    resource_matches: bool = True,
    scope_matches: bool = True,
    discovery_status: int = 200,
) -> Callable[[str, str, bytes], "tuple[int, str, dict[str, str]]"]:
    """A stub for the advertised-identity check. When ``resource_matches`` /
    ``scope_matches``, the discovery and /register bodies advertise the stub's
    own base URL (via the ``{{BASE_URL}}`` seam); otherwise they advertise a
    foreign internal-gateway host or the api://-form scope prefix — each a
    production regression the check must catch.
    """
    resource = "{{BASE_URL}}" if resource_matches else "https://apim-foreign.azure-api.net"
    scope_prefix = "{{BASE_URL}}" if scope_matches else "api://sage-app-id"

    def stub(method: str, path: str, _body: bytes) -> tuple[int, str, dict[str, str]]:
        p = path.split("?", 1)[0]
        if p == "/.well-known/oauth-protected-resource":
            body = (
                "{"
                f'"resource": "{resource}", '
                f'"authorization_servers": ["{resource}"], '
                f'"scopes_supported": ["{scope_prefix}/Sage.Access", "offline_access"], '
                '"bearer_methods_supported": ["header"]'
                "}"
            )
            return discovery_status, (body if discovery_status == 200 else "{}"), {}
        if method == "POST" and p == "/register":
            body = (
                "{"
                '"client_id": "NV", '
                '"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"], '
                '"token_endpoint_auth_method": "none", '
                f'"scope": "{scope_prefix}/Sage.Access offline_access"'
                "}"
            )
            return 201, body, {}
        return 404, '{"error":"not_found"}', {}

    return stub


@_NEEDS_RUNTIME
def test_resource_identity_passes() -> None:
    """Discovery resource, scopes_supported, and the /register scope all carry
    the public base URL -> edge_resource_identity PASS: a standards MCP client
    accepts the RFC 9728 origin match and Entra accepts its RFC 8707 resource
    parameter.
    """
    with serve(_resource_identity_stub()) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_resource_identity"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_resource_identity") == "PASS", (proc.stdout, proc.stderr)
    assert proc.returncode == 0


@_NEEDS_RUNTIME
def test_resource_identity_foreign_resource_fails() -> None:
    """THE internal-gateway regression: the discovery doc advertises the
    *.azure-api.net gateway host instead of the public custom domain. Every
    endpoint answers 200, but a standards client rejects the RFC 9728 origin
    mismatch — the check must FAIL rather than credit the healthy statuses.
    """
    with serve(_resource_identity_stub(resource_matches=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_resource_identity"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_resource_identity") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_resource_identity_api_form_scope_fails() -> None:
    """THE api://-scope regression: the resource is the public host but the
    advertised scope prefix is the api://<app-id> audience URI, which can never
    be consistent with the client's https RFC 8707 resource parameter
    (AADSTS9010010 pre-authentication). The check must FAIL on the scope leg
    even though the resource leg matches.
    """
    with serve(_resource_identity_stub(scope_matches=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_resource_identity"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_resource_identity") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_resource_identity_dead_edge_fails() -> None:
    """Discovery doc broken (404): coherent-looking bodies must NOT be credited
    -- the discovery-200 control rejects them (the blanket-edge
    coincidental-pass trap).
    """
    with serve(_resource_identity_stub(discovery_status=404)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_resource_identity"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_resource_identity") == "FAIL", verdicts
    assert proc.returncode != 0


def _offline_access_stub(
    prm_advertises: bool = True,
    register_advertises: bool = True,
    discovery_status: int = 200,
) -> Callable[[str, str, bytes], "tuple[int, str, dict[str, str]]"]:
    """A stub for the offline_access-advertisement check. When healthy, the
    protected-resource-metadata ``scopes_supported`` AND the ``/register`` scope
    string both carry the bare OIDC ``offline_access`` token alongside the
    resource-qualified ``Sage.Access`` scope; the discovery doc answers
    ``discovery_status`` (200 = edge live). Toggling ``prm_advertises`` /
    ``register_advertises`` off drops offline_access from one leg while the other
    stays healthy — the dropped-advertisement regression the check must catch.
    """
    prm_scopes = (
        '["{{BASE_URL}}/Sage.Access"' + (', "offline_access"' if prm_advertises else "") + "]"
    )
    reg_scope = "{{BASE_URL}}/Sage.Access" + (" offline_access" if register_advertises else "")

    def stub(method: str, path: str, _body: bytes) -> tuple[int, str, dict[str, str]]:
        p = path.split("?", 1)[0]
        if p == "/.well-known/oauth-protected-resource":
            body = (
                "{"
                '"resource": "{{BASE_URL}}", '
                '"authorization_servers": ["{{BASE_URL}}"], '
                f'"scopes_supported": {prm_scopes}, '
                '"bearer_methods_supported": ["header"]'
                "}"
            )
            return discovery_status, (body if discovery_status == 200 else "{}"), {}
        if method == "POST" and p == "/register":
            body = (
                "{"
                '"client_id": "NV", '
                '"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"], '
                '"token_endpoint_auth_method": "none", '
                f'"scope": "{reg_scope}"'
                "}"
            )
            return 201, body, {}
        return 404, '{"error":"not_found"}', {}

    return stub


@_NEEDS_RUNTIME
def test_advertises_offline_access_passes() -> None:
    """scopes_supported AND the /register scope both carry offline_access, edge
    live -> edge_advertises_offline_access PASS: a standards MCP client requests
    the refresh-token scope and Entra issues a refresh token.
    """
    with serve(_offline_access_stub()) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_advertises_offline_access"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_advertises_offline_access") == "PASS", (proc.stdout, proc.stderr)
    assert proc.returncode == 0


@_NEEDS_RUNTIME
def test_advertises_offline_access_missing_from_metadata_fails() -> None:
    """THE dropped-advertisement regression on the metadata leg: the discovery
    doc omits offline_access from scopes_supported (the /register scope still
    carries it). The check must FAIL on the metadata leg, proving it asserts the
    actual token in scopes_supported, not merely a 200 + a healthy /register.
    """
    with serve(_offline_access_stub(prm_advertises=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_advertises_offline_access"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_advertises_offline_access") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_advertises_offline_access_missing_from_register_fails() -> None:
    """THE dropped-advertisement regression on the /register leg: the discovery
    scopes_supported still carries offline_access but the /register scope string
    drops it. The check must FAIL on the /register leg, proving it asserts the
    token in both legs independently.
    """
    with serve(_offline_access_stub(register_advertises=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_advertises_offline_access"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_advertises_offline_access") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_advertises_offline_access_dead_edge_fails() -> None:
    """Discovery doc broken (404): fully-advertised bodies must NOT be credited
    -- the discovery-200 control rejects them (the blanket-edge
    coincidental-pass trap).
    """
    with serve(_offline_access_stub(discovery_status=404)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_advertises_offline_access"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_advertises_offline_access") == "FAIL", verdicts
    assert proc.returncode != 0


def _grant_types_stub(
    advertises_authorization_code: bool = True,
    advertises_client_credentials: bool = True,
    discovery_status: int = 200,
) -> Callable[[str, str, bytes], "tuple[int, str, dict[str, str]]"]:
    """A stub for the advertised-grant-set check. When healthy, the
    authorization-server metadata's ``grant_types_supported`` carries BOTH the
    interactive ``authorization_code`` grant and the machine ``client_credentials``
    grant; the protected-resource discovery doc answers ``discovery_status``
    (200 = edge live). Toggling either grant off drops it while the other stays
    healthy -- the dropped-grant regression the check must catch, one grant at a
    time so a check matching the array as one blob cannot survive.
    """
    grants = []
    if advertises_authorization_code:
        grants.append('"authorization_code"')
    if advertises_client_credentials:
        grants.append('"client_credentials"')
    grant_array = "[" + ", ".join(grants) + "]"

    def stub(method: str, path: str, _body: bytes) -> tuple[int, str, dict[str, str]]:
        p = path.split("?", 1)[0]
        if p == "/.well-known/oauth-protected-resource":
            body = (
                "{"
                '"resource": "{{BASE_URL}}", '
                '"authorization_servers": ["{{BASE_URL}}"], '
                '"scopes_supported": ["{{BASE_URL}}/Sage.Access", "offline_access"], '
                '"bearer_methods_supported": ["header"]'
                "}"
            )
            return discovery_status, (body if discovery_status == 200 else "{}"), {}
        if p == "/.well-known/oauth-authorization-server":
            body = (
                "{"
                '"issuer": "{{BASE_URL}}", '
                '"registration_endpoint": "{{BASE_URL}}/register", '
                '"response_types_supported": ["code"], '
                f'"grant_types_supported": {grant_array}, '
                '"code_challenge_methods_supported": ["S256"], '
                '"scopes_supported": ["{{BASE_URL}}/Sage.Access", "offline_access"]'
                "}"
            )
            return 200, body, {}
        return 404, '{"error":"not_found"}', {}

    return stub


@_NEEDS_RUNTIME
def test_advertises_grant_types_passes() -> None:
    """Both grants advertised, edge live -> edge_advertises_grant_types PASS: a
    conformant client reading discovery finds the machine-to-machine
    client_credentials leg alongside the interactive one.
    """
    with serve(_grant_types_stub()) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_advertises_grant_types"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_advertises_grant_types") == "PASS", (proc.stdout, proc.stderr)
    assert proc.returncode == 0


@_NEEDS_RUNTIME
def test_advertises_grant_types_missing_client_credentials_fails() -> None:
    """THE dropped-grant regression this check exists for: the metadata still
    advertises authorization_code (so every other edge check stays green) but
    client_credentials is gone. The check must FAIL, proving it asserts the
    machine grant specifically rather than a healthy-looking 200.
    """
    with serve(_grant_types_stub(advertises_client_credentials=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_advertises_grant_types"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_advertises_grant_types") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_advertises_grant_types_missing_authorization_code_fails() -> None:
    """The mirror regression: client_credentials survives but the interactive
    authorization_code grant is dropped. The check must FAIL, proving the two
    grants are asserted independently -- a single match over the whole array
    would credit this and let the browser flow silently stop being advertised.
    """
    with serve(_grant_types_stub(advertises_authorization_code=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_advertises_grant_types"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_advertises_grant_types") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_advertises_grant_types_dead_edge_fails() -> None:
    """Discovery doc broken (404) while the authorization-server metadata is
    fully healthy: the advertised grant set must NOT be credited -- the
    discovery-200 control rejects it (the blanket-edge coincidental-pass trap).
    """
    with serve(_grant_types_stub(discovery_status=404)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_advertises_grant_types"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_advertises_grant_types") == "FAIL", verdicts
    assert proc.returncode != 0


def _openapi_spec_stub(
    spec_status: int = 200,
    title: str = "SAGE Core API",
    declares_scheme: bool = True,
    carries_prose: bool = True,
    gated_status: int = 401,
    discovery_status: int = 200,
    cors_on_get: bool = True,
    cors_on_options: bool = True,
) -> Callable[[str, str, bytes], "tuple[int, str, dict[str, str]]"]:
    """A stub for the schema-document check.

    When healthy the document is served unauthenticated, is SAGE's own,
    declares the bearer scheme, carries the authored prose, and answers a
    cross-origin GET with Access-Control-Allow-Origin; the gated MCP surface
    still answers 401. Each knob turns off exactly one of those, so a test can
    isolate the regression it targets.

    ``cors_on_get`` and ``cors_on_options`` are separate because the operation
    policy's CORS block has to reach the GET response a browser-context reader
    actually consumes -- the preflight OPTIONS is answered by a different
    operation, so a header present there says nothing about this one.
    """

    def responder(method: str, path: str, body: bytes) -> "tuple[int, str, dict[str, str]]":
        p = path.split("?", 1)[0]
        if p == "/.well-known/oauth-protected-resource":
            return discovery_status, _DISCOVERY_BODY, {}
        if p == "/openapi.json":
            if method == "OPTIONS":
                return 200, "", ({"Access-Control-Allow-Origin": "*"} if cors_on_options else {})
            if spec_status != 200:
                return spec_status, "", {"WWW-Authenticate": 'Bearer resource_metadata="x"'}
            components = (
                '"securitySchemes":{"entraBearer":{"type":"http","scheme":"bearer"}}'
                if declares_scheme
                else '"schemas":{}'
            )
            # The generated skeleton's one-line description, versus the authored
            # block an image carrying the specifications publishes.
            description = (
                "Vault-scoped knowledge infrastructure. Documents are never deleted."
                if carries_prose
                else "Salience-Aware Graph Engine - Core API"
            )
            return (
                200,
                f'{{"openapi":"3.1.0","info":{{"title":"{title}","version":"2.0.0",'
                f'"description":"{description}"}},'
                f'"paths":{{"/sage_vaults":{{"get":{{}}}}}},"components":{{{components}}}}}',
                {"Access-Control-Allow-Origin": "*"} if cors_on_get else {},
            )
        if p in ("/mcp", "/mcp_maint"):
            return gated_status, "", {"WWW-Authenticate": 'Bearer resource_metadata="x"'}
        return 404, '{"error":"not_found"}', {}

    return responder


@_NEEDS_RUNTIME
def test_serves_openapi_spec_passes() -> None:
    """Document 200 unauthenticated, SAGE's own, declaring the bearer scheme,
    with the gate still holding elsewhere -> edge_serves_openapi_spec PASS.
    An external caller can now generate a client without the repository.
    """
    with serve(_openapi_spec_stub()) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_serves_openapi_spec"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_serves_openapi_spec") == "PASS", (proc.stdout, proc.stderr)
    assert proc.returncode == 0


@_NEEDS_RUNTIME
def test_serves_openapi_spec_still_gated_fails() -> None:
    """The document still behind the bearer gate -> FAIL. This is the state the
    change exists to end: a caller needs a token to read the document that says
    how to obtain one. Either leg regressing (the app exemption or the APIM
    operation) lands here.
    """
    with serve(_openapi_spec_stub(spec_status=401)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_serves_openapi_spec"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_serves_openapi_spec") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_serves_openapi_spec_without_security_scheme_fails() -> None:
    """A valid 200 document that declares no bearer scheme -> FAIL.

    An image predating the declaration serves a perfectly well-formed document
    that never mentions authentication, which leaves the caller exactly as
    stuck as a 401 would. The scheme grep misses and the check fails: the
    anti-coincidental control on a blanket 200.
    """
    with serve(_openapi_spec_stub(declares_scheme=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_serves_openapi_spec"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_serves_openapi_spec") == "FAIL", verdicts
    assert "entrabearer" in _detail(proc.stdout, "edge_serves_openapi_spec").lower()


@_NEEDS_RUNTIME
def test_serves_openapi_spec_without_authored_prose_fails() -> None:
    """A 200 document carrying only the generated skeleton -> FAIL.

    The prose is read out of the committed specifications at startup, so an
    image built without them publishes every path with every explanation
    missing. Nothing about the status code, the title, or the security scheme
    distinguishes that document from a healthy one, which is why the check
    greps the body for prose that only the authored block contains.
    """
    with serve(_openapi_spec_stub(carries_prose=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_serves_openapi_spec"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_serves_openapi_spec") == "FAIL", verdicts
    assert "prose" in _detail(proc.stdout, "edge_serves_openapi_spec").lower()


@_NEEDS_RUNTIME
def test_serves_openapi_spec_foreign_document_fails() -> None:
    """A 200 carrying some other service's document -> FAIL. Proves the check
    reads what was served rather than crediting any 200 at that path.
    """
    with serve(_openapi_spec_stub(title="Some Other API")) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_serves_openapi_spec"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_serves_openapi_spec") == "FAIL", verdicts


@_NEEDS_RUNTIME
def test_serves_openapi_spec_collapsed_gate_fails() -> None:
    """The document 200s but so does the gated MCP surface -> FAIL.

    Publishing one document must not widen into an open edge. If validate-jwt
    were lost altogether, every path would 200 and the document's own 200 would
    mean nothing; the gated-surface control rejects that reading.
    """
    with serve(_openapi_spec_stub(gated_status=200)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_serves_openapi_spec"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_serves_openapi_spec") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_serves_openapi_spec_dead_edge_fails() -> None:
    """Discovery doc broken (404) while the document itself is fully healthy:
    the publication must NOT be credited -- the discovery-200 control rejects
    it (the blanket-edge coincidental-pass trap).
    """
    with serve(_openapi_spec_stub(discovery_status=404)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_serves_openapi_spec"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_serves_openapi_spec") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_serves_openapi_spec_without_cors_header_fails() -> None:
    """The document is published, well-formed, and unauthenticated -- but the
    GET response carries no Access-Control-Allow-Origin, so a browser-context
    reader cannot load it. The operation's whole reason for carrying its own
    CORS block is that it omits <base /> and so never runs the API-level one;
    losing that block leaves every other assertion in this check green.
    """
    with serve(_openapi_spec_stub(cors_on_get=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_serves_openapi_spec"))
    detail = _detail(proc.stdout, "edge_serves_openapi_spec")
    assert _verdicts(proc.stdout).get("edge_serves_openapi_spec") == "FAIL", proc.stdout
    assert "Access-Control-Allow-Origin" in detail, detail
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_serves_openapi_spec_cors_on_options_only_fails() -> None:
    """The preflight OPTIONS carries the header and the actual GET does not.

    A browser reads the header off the real response, not off the preflight, and
    the two are answered by different APIM operations -- so a check that settled
    for a preflight header would credit exactly the configuration this one
    exists to reject.
    """
    with serve(_openapi_spec_stub(cors_on_get=False, cors_on_options=True)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_serves_openapi_spec"))
    assert _verdicts(proc.stdout).get("edge_serves_openapi_spec") == "FAIL", proc.stdout


@_NEEDS_RUNTIME
def test_serves_openapi_spec_probes_with_an_origin_header() -> None:
    """The CORS assertion is made against a request that actually carries an
    Origin. A header returned to an origin-less request proves nothing about
    cross-origin behaviour, so the probe has to send one.
    """
    healthy = _openapi_spec_stub()
    seen: list[tuple[str, str, str]] = []

    def recording(
        method: str, path: str, body: bytes, accept: str, origin: str
    ) -> tuple[int, str, dict[str, str]]:
        seen.append((method, path.split("?", 1)[0], origin))
        return healthy(method, path, body)

    with serve(recording) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_serves_openapi_spec"))
    assert _verdicts(proc.stdout).get("edge_serves_openapi_spec") == "PASS", proc.stdout
    assert any(m == "GET" and p == "/openapi.json" and origin for m, p, origin in seen), (
        f"no Origin-bearing GET of the schema document was issued: {seen}"
    )


def _mount_discovery_stub(
    pathful_resource: bool = True,
    pathful_challenge: bool = True,
    discovery_status: int = 200,
) -> Callable[[str, str, bytes], "tuple[int, str, dict[str, str]]"]:
    """A stub for the mount-discovery check. When healthy, a 401 on an MCP
    mount carries a challenge pointing at that mount's path-inserted metadata
    document, and the document advertises the path-carrying mount URI as its
    resource. Toggling ``pathful_resource`` off makes the mount documents
    advertise the bare host (the trailing-slash dead-end regression); toggling
    ``pathful_challenge`` off points every challenge at the root document.
    """

    def stub(method: str, path: str, _body: bytes) -> tuple[int, str, dict[str, str]]:
        p = path.split("?", 1)[0]
        if p == "/.well-known/oauth-protected-resource":
            return discovery_status, (_DISCOVERY_BODY if discovery_status == 200 else "{}"), {}
        # Path-specific mounts first: /mcp is a string prefix of both.
        for mount in ("/mcp_maint", "/mcp"):
            if p == f"/.well-known/oauth-protected-resource{mount}":
                resource = "{{BASE_URL}}" + (mount if pathful_resource else "")
                body = (
                    "{"
                    f'"resource": "{resource}", '
                    '"authorization_servers": ["{{BASE_URL}}"], '
                    '"scopes_supported": ["{{BASE_URL}}/Sage.Access", "offline_access"], '
                    '"bearer_methods_supported": ["header"]'
                    "}"
                )
                return 200, body, {}
            if p == mount:
                pointer = mount if pathful_challenge else ""
                challenge = (
                    "Bearer resource_metadata="
                    f'"{{{{BASE_URL}}}}/.well-known/oauth-protected-resource{pointer}"'
                )
                return 401, "", {"WWW-Authenticate": challenge}
        return 404, '{"error":"not_found"}', {}

    return stub


@_NEEDS_RUNTIME
def test_mount_discovery_passes() -> None:
    """Each mount 401s with a challenge pointing at its path-inserted metadata
    document, and each document advertises the path-carrying mount URI ->
    edge_mount_discovery PASS.
    """
    with serve(_mount_discovery_stub()) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_mount_discovery"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_mount_discovery") == "PASS", (proc.stdout, proc.stderr)
    assert proc.returncode == 0


@_NEEDS_RUNTIME
def test_mount_discovery_bare_host_resource_fails() -> None:
    """THE trailing-slash dead-end regression: the mount documents 200 but
    advertise the bare host as resource. A client's URL serializer turns that
    into https://host/ -- a form Entra can neither match nor register -- so the
    check must FAIL on the document shape, not credit the healthy statuses.
    """
    with serve(_mount_discovery_stub(pathful_resource=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_mount_discovery"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_mount_discovery") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_mount_discovery_root_challenge_fails() -> None:
    """The challenge on an MCP mount points at the ROOT document instead of the
    mount's path-inserted one: the client then reads the bare-host resource and
    dead-ends. The check must FAIL on the challenge pointer even though the
    mount documents themselves are correct.
    """
    with serve(_mount_discovery_stub(pathful_challenge=False)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_mount_discovery"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_mount_discovery") == "FAIL", verdicts
    assert proc.returncode != 0


@_NEEDS_RUNTIME
def test_mount_discovery_dead_edge_fails() -> None:
    """Discovery doc broken (404): coherent-looking challenges and documents
    must NOT be credited -- the discovery-200 control rejects them.
    """
    with serve(_mount_discovery_stub(discovery_status=404)) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="edge_mount_discovery"))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get("edge_mount_discovery") == "FAIL", verdicts
    assert proc.returncode != 0


_ARR_CHECK = "edge_advertised_resources_registered"


def _advertised_resources_stub(
    discovery_status: int = 200,
    maint_doc_status: int = 200,
    maint_advertises_ordinary: bool = False,
    root_resource_suffix: str = "",
) -> Callable[[str, str, bytes], "tuple[int, str, dict[str, str]]"]:
    """A stub for the advertised-resources registration check: the root and the
    two per-mount metadata documents each 200 and advertise a ``{{BASE_URL}}``
    resource (bare for the root, path-carrying for the mounts) -- the three
    identities a real edge steers clients to. ``maint_doc_status`` breaks just
    the /mcp_maint document so the unreadable-advertisement leg can be
    exercised; ``discovery_status`` breaks the root (the blanket-edge control);
    ``maint_advertises_ordinary`` makes the maintenance mount's document advertise the
    ordinary resource instead of its own path form, so a check
    that constructs the probe set from the known mount paths -- rather than
    reading it out of the documents -- can be told apart.
    ``root_resource_suffix`` appends to the root document's advertised
    resource, so a scenario can advertise a URI the edge would never mean --
    one carrying a shell metacharacter -- without disturbing the mounts.
    """

    def stub(method: str, path: str, _body: bytes) -> tuple[int, str, dict[str, str]]:
        p = path.split("?", 1)[0]
        if p == "/.well-known/oauth-protected-resource":
            if discovery_status != 200:
                return discovery_status, "{}", {}
            root = "{{BASE_URL}}" + root_resource_suffix
            return 200, f'{{"resource": "{root}", "scopes_supported": ["offline_access"]}}', {}
        for mount in ("/mcp_maint", "/mcp"):
            if p == f"/.well-known/oauth-protected-resource{mount}":
                if mount == "/mcp_maint" and maint_doc_status != 200:
                    return maint_doc_status, '{"error":"not_found"}', {}
                advertised = mount
                if mount == "/mcp_maint" and maint_advertises_ordinary:
                    advertised = "/mcp"
                body = "{" + f'"resource": "{{{{BASE_URL}}}}{advertised}"' + "}"
                return 200, body, {}
        return 404, '{"error":"not_found"}', {}

    return stub


def _write_resource_probe_stub(tmp_path: Path, refuse_suffix: str = "") -> str:
    """A token-probe seam stub: exits 0 (token minted) for every resource,
    except one whose URI ends in ``refuse_suffix``, which is refused the way
    Entra refuses an unregistered identifier URI -- a verbose AADSTS500011
    diagnostic on stderr and a non-zero exit. Nothing goes to stdout, matching
    the real probe's discarded-token contract.
    """
    body = ""
    if refuse_suffix:
        body += (
            f'case "$1" in\n  *{refuse_suffix})\n'
            '    echo "AADSTS500011: The resource principal named $1 was not found'
            ' in the tenant. TraceID: deadbeef CorrelationID: cafe" >&2\n'
            "    exit 1 ;;\nesac\n"
        )
    body += "exit 0\n"
    return _write_stub_cmd(tmp_path, "resprobe", body)


@_NEEDS_RUNTIME
def test_advertised_resources_registered_passes(tmp_path: Path) -> None:
    """Every advertised resource mints a token scoped ``<resource>/.default``
    -> PASS: each identity the edge steers a client to is registered in the
    directory as an identifier URI.
    """
    probe = _write_resource_probe_stub(tmp_path)
    with serve(_advertised_resources_stub()) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_CHECKS=_ARR_CHECK, PREFLIGHT_RESOURCE_TOKEN_PROBE_CMD=probe)
        )
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get(_ARR_CHECK) == "PASS", (proc.stdout, proc.stderr)
    assert proc.returncode == 0


@_NEEDS_RUNTIME
def test_advertised_resources_registered_unregistered_mount_fails(tmp_path: Path) -> None:
    """THE invalid_target regression: the edge advertises a maintenance-mount
    resource whose identifier URI the directory does not hold. Every document
    200s and every app-scoped probe stays green -- only the mint carrying the
    resource is refused (AADSTS500011) -- so the check must FAIL, naming the
    unregistered URI and the refusal code without echoing the raw diagnostic.
    """
    probe = _write_resource_probe_stub(tmp_path, refuse_suffix="/mcp_maint")
    with serve(_advertised_resources_stub()) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_CHECKS=_ARR_CHECK, PREFLIGHT_RESOURCE_TOKEN_PROBE_CMD=probe)
        )
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get(_ARR_CHECK) == "FAIL", (proc.stdout, proc.stderr)
    assert proc.returncode != 0
    detail = _detail(proc.stdout, _ARR_CHECK)
    assert "/mcp_maint" in detail, f"detail must name the unregistered URI: {detail!r}"
    assert "AADSTS500011" in detail, f"detail must carry the refusal code: {detail!r}"
    # Bounded extraction: the code, never the probe's raw diagnostic line.
    assert "TraceID" not in detail, f"raw probe output leaked into the detail: {detail!r}"


@_NEEDS_RUNTIME
def test_advertised_resources_registered_probes_each_advertised_resource(
    tmp_path: Path,
) -> None:
    """The check must put the discriminating question to the authorization
    server once per distinct advertised resource -- a check that greps the
    documents but never mints, or probes a hardcoded subset, cannot see an
    unregistered identifier URI. The recording stub observes the mint attempts.
    """
    record = tmp_path / "probed.txt"
    probe = _write_stub_cmd(tmp_path, "resprobe", f"printf '%s\\n' \"$1\" >> {record}\nexit 0\n")
    with serve(_advertised_resources_stub()) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_CHECKS=_ARR_CHECK, PREFLIGHT_RESOURCE_TOKEN_PROBE_CMD=probe)
        )
        expected = [url, f"{url}/mcp", f"{url}/mcp_maint"]
    assert _verdicts(proc.stdout).get(_ARR_CHECK) == "PASS", (proc.stdout, proc.stderr)
    probed = record.read_text(encoding="utf-8").split()
    assert sorted(probed) == sorted(expected), f"probed set != advertised set: {probed}"


@_NEEDS_RUNTIME
def test_advertised_resources_registered_probes_what_documents_advertise(
    tmp_path: Path,
) -> None:
    """The probed set must come from the DOCUMENTS, not from the known mount
    paths: with the maintenance document advertising the ordinary
    resource, the distinct advertised set is two URIs -- probed
    once each, and the maintenance path form (which no document advertises) not at
    all. A check that constructs <base>+<mount> for each known mount produces
    the same three-URI set as the healthy case and cannot pass here.
    """
    record = tmp_path / "probed.txt"
    probe = _write_stub_cmd(tmp_path, "resprobe", f"printf '%s\\n' \"$1\" >> {record}\nexit 0\n")
    with serve(_advertised_resources_stub(maint_advertises_ordinary=True)) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_CHECKS=_ARR_CHECK, PREFLIGHT_RESOURCE_TOKEN_PROBE_CMD=probe)
        )
        expected = [url, f"{url}/mcp"]
    assert _verdicts(proc.stdout).get(_ARR_CHECK) == "PASS", (proc.stdout, proc.stderr)
    probed = record.read_text(encoding="utf-8").split()
    assert sorted(probed) == sorted(expected), (
        f"probed set must be the documents' distinct advertised set: {probed}"
    )


@_NEEDS_RUNTIME
def test_advertised_resources_registered_probes_the_uri_not_a_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probed URI is the one the document advertised, never one assembled
    from the working directory.

    The accumulated resource set is read back through an *unquoted* expansion,
    which is how the set is split into words -- and pathname expansion applies
    to that same expansion. A resource carrying a glob character is therefore
    replaced by whatever matches it on disk, so the check mints for a URI no
    document advertised and credits (or refuses) the wrong identity.

    A URL is not obviously a glob pattern, which is what makes the defect easy
    to miss, but it is one: the shell reads ``//`` as a single separator and the
    trailing component as a pattern, so ``<base>/mcp?`` matches a file at
    ``./http:/<host>:<port>/mcpX``. The bait is planted here for exactly that
    reason -- without it the pattern matches nothing, stays literal, and the
    scenario would be satisfied by the very expansion it is meant to exclude.

    The advertised suffix is deliberate rather than realistic: the shape has to
    be one the runner's directory can satisfy. What is realistic is the
    surrounding condition, since the harness runs from a checkout and probes
    whatever URIs the tenant happens to advertise.
    """
    record = tmp_path / "probed.txt"
    probe = _write_stub_cmd(tmp_path, "resprobe", f"printf '%s\\n' \"$1\" >> {record}\nexit 0\n")
    with serve(_advertised_resources_stub(root_resource_suffix="/mcp?")) as url:
        scheme, host_port = url.split("://", 1)
        bait = tmp_path / f"{scheme}:" / host_port
        bait.mkdir(parents=True)
        (bait / "mcpX").touch()
        monkeypatch.chdir(tmp_path)
        # Positive control on the bait, as in the vault-load scenario: a bait
        # that does not match leaves this test satisfied by an expanding loop.
        assert glob.glob(f"{scheme}:/{host_port}/mcp?"), (
            "the bait is not a live glob match; the scenario is inert"
        )
        proc = _run(
            _base_env(url, PREFLIGHT_CHECKS=_ARR_CHECK, PREFLIGHT_RESOURCE_TOKEN_PROBE_CMD=probe)
        )
        advertised, expanded = f"{url}/mcp?", f"{url}/mcpX"
    assert _verdicts(proc.stdout).get(_ARR_CHECK) == "PASS", (proc.stdout, proc.stderr)
    probed = record.read_text(encoding="utf-8").split()
    assert advertised in probed, f"the advertised URI was not the one probed: {probed}"
    assert expanded not in probed, f"a filename beside the run reached the probe: {probed}"


@_NEEDS_RUNTIME
def test_advertised_resources_registered_dead_edge_fails(tmp_path: Path) -> None:
    """Discovery doc broken (404): a green token probe must NOT be credited --
    the discovery-200 control rejects the blanket-edge coincidental pass, and
    the detail must say the CONTROL failed (edge not live), not misdiagnose a
    dead edge as one unreadable metadata document.
    """
    probe = _write_resource_probe_stub(tmp_path)
    with serve(_advertised_resources_stub(discovery_status=404)) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_CHECKS=_ARR_CHECK, PREFLIGHT_RESOURCE_TOKEN_PROBE_CMD=probe)
        )
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get(_ARR_CHECK) == "FAIL", verdicts
    assert proc.returncode != 0
    detail = _detail(proc.stdout, _ARR_CHECK)
    assert "control failed" in detail, f"dead edge must be named as the control leg: {detail!r}"


@_NEEDS_RUNTIME
def test_advertised_resources_registered_missing_mount_doc_fails(tmp_path: Path) -> None:
    """One mount's metadata document is unreadable (404): the advertised set
    cannot be fully read, and a check that silently shrank to the readable
    subset would credit exactly the mount most likely to be broken -- so FAIL,
    even though every readable resource mints fine.
    """
    probe = _write_resource_probe_stub(tmp_path)
    with serve(_advertised_resources_stub(maint_doc_status=404)) as url:
        proc = _run(
            _base_env(url, PREFLIGHT_CHECKS=_ARR_CHECK, PREFLIGHT_RESOURCE_TOKEN_PROBE_CMD=probe)
        )
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get(_ARR_CHECK) == "FAIL", verdicts
    assert proc.returncode != 0
    assert "mcp_maint" in _detail(proc.stdout, _ARR_CHECK)


@_NEEDS_RUNTIME
def test_advertised_resources_registered_skips_when_probe_seam_unset() -> None:
    """No token-probe seam (an operator run without an az session): the check
    must SKIP -- neither FAIL (the edge may be perfectly healthy) nor PASS
    (registration was not verified) -- and say which seam arms it.
    """
    with serve(_advertised_resources_stub()) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS=_ARR_CHECK))
    verdicts = _verdicts(proc.stdout)
    assert verdicts.get(_ARR_CHECK) == "SKIP", (proc.stdout, proc.stderr)
    assert proc.returncode == 0
    assert "PREFLIGHT_RESOURCE_TOKEN_PROBE_CMD" in _detail(proc.stdout, _ARR_CHECK)


@_NEEDS_RUNTIME
def test_one_failure_does_not_mask_others() -> None:
    """Independence: a check that fails in the MIDDLE of the run must not abort
    it -- the checks ordered after the failure still run and report. (A naive
    ``set -e``-abort-on-first-failure would drop everything after ``liveness``.)
    """

    def health_down(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0] == "/health":
            return 500, '{"error":"unavailable"}', {}
        return _green(method, path, body)

    with serve(health_down) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS=_HTTP_CHECKS))
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode != 0
    assert verdicts.get("liveness") == "FAIL", verdicts
    # Every selected check produced a verdict -- the mid-run failure masked none.
    assert set(verdicts) == set(_HTTP_CHECKS.split(",")), f"a check was masked: {verdicts}"
    # The checks ordered AFTER the failing one still ran and passed.
    for check in ("edge_discovery", "vault_load", "retrieval_pg"):
        assert verdicts.get(check) == "PASS", f"{check} should be unaffected: {verdicts}"


@_NEEDS_RUNTIME
def test_retrieval_pg_fails_on_postgres_down() -> None:
    """A Postgres-down /discover 500 fails retrieval_pg specifically."""

    def pg_down(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0].endswith("/discover"):
            return 500, '{"error":"storage_unavailable"}', {}
        return _green(method, path, body)

    with serve(pg_down) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS="vault_load,retrieval_pg"))
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode != 0
    assert verdicts.get("vault_load") == "PASS", verdicts
    assert verdicts.get("retrieval_pg") == "FAIL", verdicts


@_NEEDS_RUNTIME
def test_kv_anthropic_skips_when_vault_load_fails() -> None:
    """/health green but /sage_vaults empty == startup aborted: vault_load FAILs,
    and its downstream qualifiers SKIP (not FAIL) so one root cause does not
    over-paint the matrix.
    """

    def empty_vaults(method: str, path: str, body: bytes) -> tuple[int, str, dict[str, str]]:
        if path.split("?", 1)[0] == "/sage_vaults":
            return 200, '{"vaults":[],"count":0}', {}
        return _green(method, path, body)

    checks = "liveness,vault_load,kv_anthropic,sharepoint_discovery"
    with serve(empty_vaults) as url:
        proc = _run(_base_env(url, PREFLIGHT_CHECKS=checks))
    verdicts = _verdicts(proc.stdout)
    assert proc.returncode != 0, "an empty registry must fail the run via vault_load"
    assert verdicts.get("liveness") == "PASS", verdicts
    assert verdicts.get("vault_load") == "FAIL", verdicts
    assert verdicts.get("kv_anthropic") == "SKIP", "anthropic-key rides vault load, not /health"
    assert verdicts.get("sharepoint_discovery") == "SKIP", verdicts
