"""Structural and idempotency gate for the per-tenant bootstrap scripts.

Locks the shape of ``deploy/bootstrap/*.sh`` — the idempotent operator scripts
that codify the one-time per-tenant cloud bring-up that is not expressible as
subscription Bicep: the Entra app registrations and admin consent, the Key
Vault secret and certificate load, the document-store vault seed (CAS-ADR-043),
and the provider-agnostic DNS record emission. The cloud deployment profile
these scripts bring up is recorded in CAS-ADR-042.

The scripts replace hand-run runbook procedures with executable code that
converges on re-run. These checks read the tracked scripts only — no Azure
tooling and no live tenant — so they run in the ordinary Python test job. They
assert the scripts carry the right verbs and idempotency guards; executing them
against a tenant is out of scope for CI, exactly as the runbook gate
``tests/infra/test_entra_registrations.py`` is.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
BOOTSTRAP_DIR: Final[Path] = REPO_ROOT / "deploy" / "bootstrap"
ENTRA: Final[Path] = BOOTSTRAP_DIR / "entra-app-registrations.sh"
KEY_VAULT: Final[Path] = BOOTSTRAP_DIR / "load-key-vault-secrets.sh"
VAULT_SEED: Final[Path] = BOOTSTRAP_DIR / "seed-vault-source.sh"
DNS: Final[Path] = BOOTSTRAP_DIR / "emit-dns-records.sh"
SCRIPTS: Final[tuple[Path, ...]] = (ENTRA, KEY_VAULT, VAULT_SEED, DNS)

PROCESS_DIR: Final[Path] = REPO_ROOT / "docs" / "process"
STAGES_DOC: Final[Path] = PROCESS_DIR / "cloud-deploy-stages.md"

# Each runbook documents a step whose executable substance is its codified
# script (Cloud Deployment Discipline, Principle 3).
_RUNBOOK_TO_SCRIPT: Final[dict[str, str]] = {
    "entra-app-registrations.md": "entra-app-registrations.sh",
    "key-vault-secrets.md": "load-key-vault-secrets.sh",
    "sharepoint-vault-source.md": "seed-vault-source.sh",
    "custom-domains-dns.md": "emit-dns-records.sh",
}

_GUID_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b"
)

# DNS-provider API surfaces the emitter must never call — it computes records
# and the operator publishes them in whatever provider the tenant uses.
_DNS_PROVIDER_TOKENS: Final[tuple[str, ...]] = (
    r"route\s*53",
    r"\baws\b",
    r"az network dns",
    r"\bcloudflare\b",
    r"resolve-dnsname",
    r"gcloud dns",
)


def _git_owner() -> str | None:
    """Derive the repository owner from the origin remote, or ``None``."""
    try:
        url = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    match = re.search(r"[:/]([^/]+)/[^/]+?(?:\.git)?$", url)
    return match.group(1) if match else None


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _uncommented_invocations(text: str, token: str) -> list[str]:
    """Every real command invocation of ``token``, each joined across its
    trailing-backslash line continuations into a single string.

    A ``#`` comment that merely names ``token`` in prose (e.g.
    ``like --identifier-uris earlier``) is not a command and is excluded — a
    line-oriented scan, not a bare ``re.findall`` for the token, is what keeps
    such a mention from being mistaken for an actual use of the flag.
    """
    lines = text.splitlines()
    invocations: list[str] = []
    for i, line in enumerate(lines):
        if token not in line or line.lstrip().startswith("#"):
            continue
        parts = [line]
        j = i
        while lines[j].rstrip().endswith("\\") and j + 1 < len(lines):
            j += 1
            parts.append(lines[j])
        invocations.append(" ".join(part.strip().rstrip("\\").strip() for part in parts))
    return invocations


def test_all_four_scripts_exist_and_are_executable() -> None:
    """The bootstrap surface every later orchestration step assumes."""
    for script in SCRIPTS:
        assert script.is_file(), f"{script.relative_to(REPO_ROOT)} missing"
        assert os.access(script, os.X_OK), f"{script.name} is not executable (chmod +x)"


def test_scripts_have_strict_bash_preamble() -> None:
    """Each script is bash with strict-mode error handling, so a failed ``az``
    call aborts rather than silently continuing.
    """
    for script in SCRIPTS:
        text = _text(script)
        assert text.startswith("#!/usr/bin/env bash"), (
            f"{script.name} must start with #!/usr/bin/env bash"
        )
        assert "set -euo pipefail" in text, f"{script.name} must `set -euo pipefail`"


def test_scripts_parse_under_bash_n() -> None:
    """Every script parses without executing — catches syntax breakage."""
    bash = shutil.which("bash")
    assert bash, "bash not found (required to validate the bootstrap scripts)"
    for script in SCRIPTS:
        proc = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True)
        assert proc.returncode == 0, f"{script.name} fails bash -n:\n{proc.stderr}"


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck absent")
def test_scripts_lint_clean() -> None:
    """Deeper static safety when shellcheck is available."""
    for script in SCRIPTS:
        proc = subprocess.run(["shellcheck", str(script)], capture_output=True, text=True)
        assert proc.returncode == 0, f"{script.name} shellcheck findings:\n{proc.stdout}"


def test_scripts_have_no_hardcoded_identity_or_secret() -> None:
    """No GUID, personal path, or repository-owner literal lives in any script —
    identity is resolved at run time and secrets arrive through the environment.
    """
    owner = _git_owner()
    for script in SCRIPTS:
        text = _text(script)
        assert not _GUID_RE.search(text), f"{script.name} hardcodes a GUID; resolve it at run time"
        assert "/Users/" not in text, f"{script.name} hardcodes a personal path"
        if owner:
            assert owner.lower() not in text.lower(), (
                f"{script.name} hardcodes the repository owner"
            )


def test_entra_script_is_idempotent_lookup_then_create() -> None:
    """The Entra script looks up an existing registration before creating one
    and guards the create with an emptiness test, so a re-run reconciles rather
    than duplicating, and it grants admin consent.
    """
    text = _text(ENTRA)
    assert "az ad app list" in text, "entra script must look up existing registrations first"
    assert "az ad app create" in text, "entra script must create the registrations"
    assert text.index("az ad app list") < text.index("az ad app create"), (
        "entra script must look up before creating (idempotent guard)"
    )
    assert re.search(r"if\s+\[\s+-z\s+", text), (
        "entra script must guard the create with an emptiness test (lookup-then-create)"
    )
    assert "az ad app permission admin-consent" in text, "entra script must grant admin consent"


def test_entra_script_sets_v2_access_token_version() -> None:
    """The SAGE resource-server PATCH pins the app registration to access-token
    **v2** (``requestedAccessTokenVersion: 2``).

    Without it, a token minted for the SAGE audience via the v2 ``/.default``
    endpoint — which the post-deploy preflight probe and the BFF on-behalf-of
    exchange both use — is still issued as v1, whose ``sts.windows.net`` issuer
    APIM and the SAGE backend reject. Codifying the setting here keeps a
    fresh-tenant bring-up or a re-provision from regressing (CAS-ADR-042).
    """
    text = _text(ENTRA)
    # Anchor on the JSON ``key: 2`` form so a prose mention ("set it to 2") cannot
    # satisfy the gate; the optional backslash absorbs the script's escaped quote.
    assert re.search(r'requestedAccessTokenVersion\\?"\s*:\s*2', text), (
        'entra script must set "requestedAccessTokenVersion": 2 in the SAGE api PATCH '
        "so the /.default (and on-behalf-of) token is issued as v2"
    )


def test_entra_script_emits_parameter_coordinates() -> None:
    """The Entra script emits the ``sageAudience`` and ``bffOidcClientId`` it
    produced, so the operator feeds them straight into the parameter set instead
    of hand-copying GUIDs.
    """
    # Anchor on the emit statements, not prose: a header comment that *mentions*
    # the coordinates must not let a script that never emits them pass.
    echoed = "\n".join(
        line.strip() for line in _text(ENTRA).splitlines() if line.lstrip().startswith("echo")
    )
    assert "sageAudience" in echoed and "api://" in echoed, (
        "entra script must echo the sageAudience coordinate (api://<app-id>)"
    )
    assert "bffOidcClientId" in echoed, "entra script must echo the bffOidcClientId coordinate"


def test_entra_script_creates_public_client_registration() -> None:
    """The Entra script provisions a third registration for the MCP client:
    auth-code + PKCE, no secret (CAS-ADR-042). It follows the same
    lookup-then-create idempotency guard as the SAGE and BFF registrations, uses
    the public-client redirect-uri platform (never the confidential/web one a
    secret-bearing client would use), creates its service principal, and is
    granted + admin-consented the same delegated SAGE.Access scope.
    """
    text = _text(ENTRA)
    assert "az ad app list" in text and "az ad app create" in text, (
        "entra script must look up existing registrations before creating one"
    )
    # A third lookup-then-create pair distinct from the SAGE/BFF ones above:
    # at least three list/create/emptiness-guard occurrences in total.
    assert text.count("az ad app list") >= 3, (
        "entra script must look up a third registration (the public MCP client)"
    )
    assert text.count("az ad app create") >= 3, (
        "entra script must create a third registration (the public MCP client)"
    )
    assert len(re.findall(r"if\s+\[\s+-z\s+", text)) >= 3, (
        "the public-client create must be guarded by its own emptiness test"
    )
    assert "--public-client-redirect-uris" in text, (
        "the MCP client must register via --public-client-redirect-uris "
        "(auth-code + PKCE, no secret) — not --web-redirect-uris"
    )
    assert text.count("az ad sp create") >= 2, (
        "entra script must create a service principal for the public MCP client too"
    )
    assert text.count("az ad app permission add") >= 2, (
        "the public MCP client must be granted the delegated SAGE.Access scope"
    )
    assert text.count("az ad app permission admin-consent") >= 2, (
        "the public MCP client's permission grant must be admin-consented"
    )


def test_entra_script_grants_offline_access_to_mcp_client() -> None:
    """The public MCP client is granted the OIDC ``offline_access`` permission so
    Entra issues a refresh token — without it the client's v2 access token
    (60–90 min) expires and the only way to renew the session is a fresh
    ``/authorize`` round trip (CAS-ADR-042). ``offline_access`` is a Microsoft
    Graph delegated permission, so it is granted against Graph's first-party
    service principal, and both Graph's app id and the scope id are resolved from
    the tenant at run time — never hardcoded, matching the no-GUID-literal
    discipline the GUID gate enforces.
    """
    text = _text(ENTRA)
    assert "offline_access" in text, (
        "the MCP client must be granted the offline_access delegated permission"
    )
    assert "OFFLINE_ACCESS_SCOPE_ID" in text, (
        "the offline_access scope id must be resolved at run time, not hardcoded"
    )
    assert "displayName eq 'Microsoft Graph'" in text, (
        "offline_access is a Microsoft Graph permission — resolve Graph's own "
        "service principal to grant it, not the SAGE resource server"
    )


def test_entra_script_explicitly_grants_offline_access_consent() -> None:
    """Requesting ``offline_access`` is not enough — it must be consented. Granting
    admin consent with ``az ad app permission admin-consent`` alone does NOT
    record the delegated consent grant for the Graph ``offline_access`` scope
    (verified live: the call returns 0 yet creates no ``oauth2PermissionGrant``
    for it), leaving the public MCP client without a refresh token on a fresh
    bring-up. The script must follow the ``offline_access`` ``permission add``
    with an explicit ``az ad app permission grant ... --scope offline_access``,
    the form that actually consents it as an ``AllPrincipals`` grant on Graph
    (CAS-ADR-042).
    """
    text = _text(ENTRA)
    # Anchor on a real command invocation, never a prose/comment mention: the
    # pre-fix script mentioned "offline_access" and "grant" in comments but
    # issued no `az ad app permission grant` at all, so a substring check would
    # pass by accident. _uncommented_invocations skips `#` comments and joins the
    # command across its backslash continuations (there is exactly one such grant).
    grants = _uncommented_invocations(text, "az ad app permission grant")
    assert grants, (
        "entra script must issue `az ad app permission grant` for the MCP client — "
        "admin-consent alone does not consent the Graph offline_access scope"
    )
    flat = grants[0]
    assert "--scope offline_access" in flat, (
        "the explicit grant must consent the offline_access scope (--scope offline_access)"
    )
    assert "GRAPH_APP_ID" in flat, (
        "the grant must target Graph's runtime-resolved app id (${GRAPH_APP_ID}), "
        "not a hardcoded GUID"
    )
    assert "MCP_CLIENT_APP_ID" in flat, (
        "the explicit grant must be issued for the public MCP client (${MCP_CLIENT_APP_ID})"
    )


def test_entra_script_registers_mcp_loopback_redirect() -> None:
    """The public MCP client registers the Desktop loopback redirect
    (``http://localhost/callback``) alongside its primary env-resolved redirect
    URI, so a re-bootstrap converges on the full canonical set and never drops the
    loopback a browser-context Desktop client needs for its auth-code/PKCE callback
    (CAS-ADR-042). ``--public-client-redirect-uris`` is a declarative full-set
    replace, so both must be passed together on every invocation.
    """
    text = _text(ENTRA)
    assert "http://localhost/callback" in text, (
        "entra script must register the Desktop loopback redirect http://localhost/callback "
        "on the public MCP client, so a re-bootstrap does not drop the URI added live"
    )
    redirect_lines = [
        line
        for line in text.splitlines()
        if "--public-client-redirect-uris" in line and not line.lstrip().startswith("#")
    ]
    assert redirect_lines, "entra script must pass --public-client-redirect-uris"
    assert all(
        "MCP_CLIENT_REDIRECT_URI" in line and "localhost/callback" in line
        for line in redirect_lines
    ), (
        "every --public-client-redirect-uris invocation (create and update) must register "
        "both the primary ${MCP_CLIENT_REDIRECT_URI} and the http://localhost/callback loopback"
    )


def test_entra_script_declares_all_sage_identifier_uris() -> None:
    """The SAGE resource server declares all FOUR identifier URIs in one update:
    the ``api://<app-id>`` audience URI, the ``https://<SAGE_PUBLIC_HOSTNAME>``
    custom-domain identity, and the two MCP-mount forms of that identity — with
    the hostname arriving as required environment.

    The https identities are what let a standards MCP client through Entra: the
    client sends an RFC 8707 ``resource`` parameter with ``/authorize``, and
    Entra rejects the request (AADSTS9010010, ``invalid_target``) unless that
    parameter IS a registered identifier URI of the scope's app — same-origin is
    not enough; the match is byte-for-byte (verified live: the bare host minted
    a code while the unregistered ``/mcp`` form was rejected, then ``/mcp``
    after registration). A client is steered to the mount URI by the mount's
    protected-resource metadata, so the mount forms are the resources clients
    actually request; the bare host remains the scope prefix. The retired SSE
    transport's ``/sse`` endpoint forms must stay OUT of the set: the endpoints
    no longer exist, and a registered identity for a dead path is an identity
    a client can acquire a token for but never use. ``--identifier-uris`` is a
    declarative full-set replace, so every URI must be passed together on every
    invocation or a re-bootstrap drops some (and re-running the updated script
    is itself the live-tenant trim).
    """
    text = _text(ENTRA)
    assert re.search(r':\s*"\$\{SAGE_PUBLIC_HOSTNAME:\?', text), (
        "entra script must require SAGE_PUBLIC_HOSTNAME in the environment "
        "(the public sage custom domain the https identifier URIs are built from)"
    )
    # Comment-aware and continuation-aware: gather each real (non-``#``)
    # ``--identifier-uris`` invocation, joined across its backslash-continued
    # lines. A prose comment that merely names the flag (e.g. "like
    # --identifier-uris earlier") is not an invocation and must not be counted
    # as one — that phantom match previously failed this gate on a comment.
    uri_invocations = _uncommented_invocations(text, "--identifier-uris")
    assert uri_invocations, "entra script must pass --identifier-uris"
    required = {
        "api://${SAGE_APP_ID}": (
            "the api://<app-id> audience URI (the BFF OBO and preflight token target)"
        ),
        '"https://${SAGE_PUBLIC_HOSTNAME}"': (
            "the bare-host https identity (the scope prefix the facade advertises)"
        ),
        '"https://${SAGE_PUBLIC_HOSTNAME}/mcp"': (
            "the /mcp mount identity (the path-inserted PRM resource and the "
            "canonical server URI a spec-following client sends)"
        ),
        '"https://${SAGE_PUBLIC_HOSTNAME}/mcp_maint"': (
            "the /mcp_maint mount identity (the maintenance surface's canonical server URI)"
        ),
        '"https://${SAGE_PUBLIC_HOSTNAME}/mcp_admin"': (
            "the /mcp_admin mount identity (the maintenance surface's pre-rename "
            "alias path, registered for as long as the edge serves it)"
        ),
    }
    forbidden = ("/mcp/sse", "/mcp_maint/sse", "/mcp_admin/sse")
    for flat in uri_invocations:
        for needle, why in required.items():
            assert needle in flat, (
                f"every --identifier-uris invocation must declare {why} — the "
                "flag is a full-set replace, so omitting it here regresses the "
                "tenant to AADSTS9010010 at /authorize"
            )
        for dead in forbidden:
            assert dead not in flat, (
                f"identifier URI for the retired SSE endpoint {dead!r} must not "
                "be re-registered — the transport is gone and the full-set "
                "replace is how the live tenant stays trimmed"
            )


def test_identifier_uris_gate_ignores_comment_mentions() -> None:
    """The invocation scan behind the identifier-uris gate counts a real command
    line as an invocation and a ``#`` comment that merely names the flag as
    prose — the distinction that keeps a comment like ``like --identifier-uris
    earlier`` from failing the gate as a phantom invocation, while still joining
    a real invocation across its backslash continuations.
    """
    text = (
        "# replace, like --identifier-uris earlier -- a future addition\n"
        'az ad app update --id "$X" \\\n'
        '  --identifier-uris "api://$X" "https://$H" \\\n'
        '    "https://$H/mcp"\n'
    )
    invocations = _uncommented_invocations(text, "--identifier-uris")
    assert invocations == ['--identifier-uris "api://$X" "https://$H" "https://$H/mcp"'], (
        "the comment mention must be excluded and the real invocation joined"
    )


def test_entra_script_provisions_group_idempotently() -> None:
    """The Entra script provisions the single ADR-044 provisioning group
    lookup-then-create, so it converges regardless of whether a companion
    bootstrap step already created the group on a prior run (whichever lands
    first creates it; the other reconciles).
    """
    text = _text(ENTRA)
    assert "az ad group list" in text, "entra script must look up an existing provisioning group"
    assert "az ad group create" in text, "entra script must create the provisioning group"
    assert text.index("az ad group list") < text.index("az ad group create"), (
        "entra script must look up the group before creating it (idempotent guard)"
    )
    assert re.search(r"if\s+\[\s+-z\s+.*\n(?:.*\n){0,4}?.*az ad group create", text), (
        "the group create must be guarded by an emptiness test on the lookup result"
    )


def test_entra_script_shares_default_access_role_id() -> None:
    """The default-access app-role id is computed once and reused by every
    client-gating step (BFF and public MCP client alike) — no GUID-shaped
    literal, and no duplicated computation to drift out of sync.
    """
    text = _text(ENTRA)
    assert text.count("DEFAULT_ACCESS_APP_ROLE_ID=") == 1, (
        "the default-access app-role id must be computed exactly once and reused"
    )
    assert text.count("${DEFAULT_ACCESS_APP_ROLE_ID}") >= 2, (
        "both the BFF and MCP-client gates must reference the shared DEFAULT_ACCESS_APP_ROLE_ID"
    )


def test_entra_script_gates_bff_on_group() -> None:
    """The BFF confidential client is gated by the same provisioning group as
    the public MCP client (CAS-ADR-044): its service principal requires
    app-role assignment, and the provisioning group is assigned to it before
    the requirement engages, so the gate never locks out an empty allowlist.
    """
    text = _text(ENTRA)
    assert text.count("appRoleAssignmentRequired") >= 2, (
        "entra script must gate both the BFF and the public MCP client on appRoleAssignmentRequired"
    )
    assert "appRoleAssignedTo" in text, (
        "entra script must assign the provisioning group to the BFF's "
        "default-access app role via the servicePrincipals appRoleAssignedTo endpoint"
    )
    # Anchor on the request body, never prose: the assignment carries the
    # principalId/resourceId/appRoleId triple, targeting the shared group. The
    # body is a multi-line JSON literal, so window a few lines after the
    # endpoint reference rather than requiring all three keys on one line.
    lines = text.splitlines()
    idx = next(i for i, line in enumerate(lines) if "appRoleAssignedTo" in line)
    block = "\n".join(lines[idx : idx + 8])
    assert "principalId" in block and "resourceId" in block and "appRoleId" in block, (
        "the BFF assignment body must carry the principalId/resourceId/appRoleId triple"
    )
    assert "PROVISIONING_GROUP_ID" in block, "the BFF assignment must target PROVISIONING_GROUP_ID"
    # Fail safe on a live tenant: assign the group BEFORE requiring assignment,
    # so a fresh tenant is never locked out by an empty allowlist.
    assert text.index("appRoleAssignedTo") < text.index("appRoleAssignmentRequired"), (
        "the BFF's group assignment must precede appRoleAssignmentRequired, so the "
        "gate never engages before its allowlist exists"
    )


def test_entra_script_gates_public_client_on_group() -> None:
    """The public MCP client is gated by the same provisioning group as the
    browser client (CAS-ADR-044): its service principal requires app-role
    assignment, and the provisioning group is assigned to it.
    """
    text = _text(ENTRA)
    assert "appRoleAssignmentRequired" in text, (
        "entra script must set appRoleAssignmentRequired on the public MCP client's "
        "service principal"
    )
    assert "true" in re.search(r"appRoleAssignmentRequired[^\n]*", text).group(0), (
        "appRoleAssignmentRequired must be set to true"
    )
    assert "appRoleAssignments" in text, (
        "entra script must assign the provisioning group to the public MCP client's "
        "default-access app role"
    )


def test_entra_script_emits_mcp_client_id_coordinate() -> None:
    """The Entra script emits the ``mcpClientId`` coordinate for the public MCP
    client it created, alongside sageAudience and bffOidcClientId, so the
    operator pastes it straight into the parameter set.
    """
    echoed = "\n".join(
        line.strip() for line in _text(ENTRA).splitlines() if line.lstrip().startswith("echo")
    )
    assert "mcpClientId" in echoed, "entra script must echo the mcpClientId coordinate"


def test_kv_secrets_script_reads_secrets_from_env_not_args() -> None:
    """Every secret and certificate password the Key Vault loader passes is an
    environment-variable expansion, never a literal, and the variables are
    ``unset`` after use.
    """
    text = _text(KEY_VAULT)
    expansions = list(re.finditer(r"--(?:value|password)\s+(\S+)", text))
    assert expansions, "load script must pass secret material via --value/--password"
    for match in expansions:
        token = match.group(1)
        assert token.startswith('"$') or token.startswith("$"), (
            f"secret material must be an env-var expansion, not a literal: {token!r}"
        )
    # Anchor on the unset statements, not the prose that describes them.
    unset_stmts = [line for line in text.splitlines() if line.lstrip().startswith("unset ")]
    assert unset_stmts, "secret env vars must be unset after use"


def test_kv_secrets_script_loads_the_three_artifacts() -> None:
    """The loader sets the two secrets and imports the wildcard certificate,
    under the fixed names the Key Vault module's outputs pin.
    """
    text = _text(KEY_VAULT)
    assert "anthropic-api-key" in text, "loader must set anthropic-api-key"
    assert "bff-client-secret" in text, "loader must set bff-client-secret"
    assert "wildcard-tls" in text, "loader must import the wildcard-tls certificate"
    assert "keyvault certificate import" in text, "loader must use certificate import"
    assert text.count("keyvault secret set") >= 2, "loader must set both secrets"


def test_kv_secrets_script_rejects_leaf_only_pfx() -> None:
    """The loader verifies the wildcard PFX carries a full chain (leaf +
    intermediate) BEFORE importing it. Azure Container Apps serves the bound
    environment certificate's PFX bytes verbatim, so a leaf-only bundle makes the
    BFF custom domain fail strict TLS clients (curl error 60) while APIM masks it
    for SAGE by rebuilding the chain -- the loader must refuse it rather than ship
    an endpoint that silently fails verification.
    """
    text = _text(KEY_VAULT)
    # Anchor on the actual guard command lines, never a prose comment: the PFX is
    # read with `openssl pkcs12 -nokeys`, the embedded certs are counted, and a
    # bundle with fewer than two certificates aborts the load.
    assert "openssl pkcs12" in text and "-nokeys" in text, (
        "loader must inspect the PFX chain with `openssl pkcs12 -nokeys`"
    )
    assert "BEGIN CERTIFICATE" in text, "loader must count the certificates in the PFX"
    assert re.search(r"-lt\s+2", text), (
        "loader must require >= 2 certificates (leaf + intermediate) before importing"
    )
    assert re.search(r"\bexit\s+1\b", text), "the leaf-only guard must fail the load (exit 1)"
    # Fail closed: the chain guard must run BEFORE the certificate import.
    assert text.index("openssl pkcs12") < text.index("keyvault certificate import"), (
        "the full-chain guard must precede the certificate import"
    )


def test_vault_seed_script_grants_and_seeds() -> None:
    """The vault-seed script grants the site-scoped Microsoft Graph permission
    and seeds the committed test-vault config into the document library, with no
    site/drive GUID baked in (CAS-ADR-043).
    """
    text = _text(VAULT_SEED)
    assert "Sites.Selected" in text, "seed script must assign the Sites.Selected app role"
    assert "appRoleAssignments" in text, "seed script must POST the app-role assignment"
    assert "/permissions" in text, "seed script must grant the per-site write permission"
    # Anchor the seed upload on its command lines, not the prose that describes
    # it: a comment mentioning the config path or :/content must not pass alone.
    body_lines = [line for line in text.splitlines() if "--body" in line]
    uri_lines = [line for line in text.splitlines() if "--uri" in line]
    assert any("deploy/test-vault/vault_config.yaml" in line for line in body_lines), (
        "seed script must PUT the committed test-vault config as the request body"
    )
    assert any(":/content" in line for line in uri_lines), (
        "seed upload uri must target :/content (create-or-replace)"
    )
    assert not _GUID_RE.search(text), "seed script hardcodes a GUID; resolve site/drive at run time"


def test_vault_seed_script_is_idempotent() -> None:
    """The seed upload is create-or-replace and the script tolerates a
    pre-existing grant, so a re-run converges.
    """
    text = _text(VAULT_SEED)
    uri_lines = [line for line in text.splitlines() if "--uri" in line]
    assert any(":/content" in line for line in uri_lines), (
        "seed upload must be the create-or-replace :/content PUT"
    )
    assert "|| true" in text, "seed script must tolerate an already-present grant on re-run"


def test_dns_script_emits_all_three_records() -> None:
    """The DNS emitter prints the two CNAMEs and the domain-verification TXT —
    dropping the ``asuid`` TXT would silently break ownership proof.
    """
    # Anchor on the echo statements that actually emit records, not the header
    # comment that describes them.
    echoed = "\n".join(
        line.strip().lower() for line in _text(DNS).splitlines() if line.lstrip().startswith("echo")
    )
    assert "cname" in echoed, "DNS script must echo CNAME records"
    assert "txt" in echoed, "DNS script must echo the verification TXT record"
    assert "asuid" in echoed, "DNS script must echo the asuid domain-ownership TXT"


def test_dns_script_is_provider_agnostic() -> None:
    """The emitter calls no DNS-provider API: it computes records for the
    operator to publish in whatever provider the tenant uses.
    """
    lowered = _text(DNS).lower()
    for token in _DNS_PROVIDER_TOKENS:
        assert not re.search(token, lowered), (
            f"DNS script must not call a DNS-provider API (matched {token!r})"
        )


def test_dns_script_resolves_coordinates_at_runtime() -> None:
    """Hostnames come from deployment outputs, not literals — so the emitter
    carries no tenant FQDN or GUID of its own.
    """
    text = _text(DNS)
    assert ("az deployment sub show" in text) or ("az containerapp show" in text), (
        "DNS script must resolve hosts from deployment outputs"
    )
    assert not _GUID_RE.search(text), "DNS script hardcodes a GUID"


def test_staged_ordering_doc_sequences_the_bringup() -> None:
    """The staged-ordering doc references the four scripts in bring-up order and
    names the terminal preflight stage — making the staged ordering explicit
    rather than discovered by repeated re-runs.
    """
    assert STAGES_DOC.is_file(), "docs/process/cloud-deploy-stages.md missing"
    text = STAGES_DOC.read_text(encoding="utf-8")
    refs = []
    for script in (ENTRA, KEY_VAULT, VAULT_SEED, DNS):
        rel = f"deploy/bootstrap/{script.name}"
        assert rel in text, f"staged-ordering doc must reference {rel}"
        refs.append(text.index(rel))
    assert refs == sorted(refs), (
        "scripts must appear in bring-up stage order: entra, key-vault, vault-seed, dns"
    )
    assert "preflight" in text.lower(), "staged-ordering doc must name the preflight stage"


def test_runbooks_point_to_their_scripts() -> None:
    """Each runbook points to its codified script — the script is the executable
    substance, the runbook documents it (Cloud Deployment Discipline, Principle 3).
    """
    for runbook, script in _RUNBOOK_TO_SCRIPT.items():
        text = (PROCESS_DIR / runbook).read_text(encoding="utf-8")
        assert f"deploy/bootstrap/{script}" in text, (
            f"{runbook} must point to its codified script deploy/bootstrap/{script}"
        )
