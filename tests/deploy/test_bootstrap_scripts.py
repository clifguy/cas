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

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
BOOTSTRAP_DIR: Final[Path] = REPO_ROOT / "deploy" / "bootstrap"
ENTRA: Final[Path] = BOOTSTRAP_DIR / "entra-app-registrations.sh"
KEY_VAULT: Final[Path] = BOOTSTRAP_DIR / "load-key-vault-secrets.sh"
VAULT_SEED: Final[Path] = BOOTSTRAP_DIR / "seed-vault-source.sh"
DNS: Final[Path] = BOOTSTRAP_DIR / "emit-dns-records.sh"
DEPLOY_IDENTITY: Final[Path] = BOOTSTRAP_DIR / "deploy-identity-roles.sh"
SCRIPTS: Final[tuple[Path, ...]] = (ENTRA, KEY_VAULT, VAULT_SEED, DNS, DEPLOY_IDENTITY)

PROCESS_DIR: Final[Path] = REPO_ROOT / "docs" / "process"
STAGES_DOC: Final[Path] = PROCESS_DIR / "cloud-deploy-stages.md"

# Each runbook documents a step whose executable substance is its codified
# script (Cloud Deployment Discipline, Principle 3).
_RUNBOOK_TO_SCRIPT: Final[dict[str, str]] = {
    "entra-app-registrations.md": "entra-app-registrations.sh",
    "key-vault-secrets.md": "load-key-vault-secrets.sh",
    "sharepoint-vault-source.md": "seed-vault-source.sh",
    "custom-domains-dns.md": "emit-dns-records.sh",
    "azure-deployment.md": "deploy-identity-roles.sh",
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
    except subprocess.CalledProcessError, FileNotFoundError:
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
    assert any(
        '"${MCP_CLIENT_APP_ID}"' in call for call in _uncommented_invocations(text, "ensure_sp")
    ), "entra script must ensure a service principal for the public MCP client too"
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
    """The public MCP client's required redirect set is built from the
    env-resolved ``MCP_CLIENT_REDIRECT_URI`` entries plus the desktop loopback
    (``http://localhost/callback``) a browser-context client needs for its
    auth-code/PKCE callback (CAS-ADR-042), and every
    ``--public-client-redirect-uris`` invocation writes a set derived from it:
    the required set itself on create, its union with the live set on update.

    The executed bootstrap tests observe the registered URIs; this anchors the
    codified form the runbook mirrors.
    """
    text = _text(ENTRA)
    required = re.search(
        r"<<<\"\$\(unique_lines \"\$\{MCP_REQUIRED_INPUT\[@\]\}\" \"http://localhost/callback\"\)\"",
        text,
    )
    assert required, (
        "the required MCP redirect set must be the MCP_CLIENT_REDIRECT_URI entries "
        "plus the http://localhost/callback loopback"
    )
    assert '<<<"${MCP_CLIENT_REDIRECT_URI},"' in text, (
        "MCP_REQUIRED_INPUT must be split from MCP_CLIENT_REDIRECT_URI"
    )
    invocations = _uncommented_invocations(text, "--public-client-redirect-uris")
    assert len(invocations) == 2, "expected one create and one update invocation"
    create, update = invocations
    assert create.startswith('--public-client-redirect-uris "${MCP_REQUIRED_REDIRECTS[@]}"'), (
        "a new MCP client registration must receive exactly the required set"
    )
    assert update.startswith('--public-client-redirect-uris "${MCP_REDIRECTS[@]}"'), (
        "an existing MCP client registration must receive the merged set"
    )
    assert re.search(
        r'merged_redirect_uris "\$\{MCP_CLIENT_APP_ID\}" publicClient'
        r'\s*\\?\s*"\$\{MCP_REQUIRED_REDIRECTS\[@\]\}"',
        text,
    ), "the merged set must union the live public-client set with the required set"


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
    }
    forbidden = ("/mcp/sse", "/mcp_maint/sse", "/mcp_admin")
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


def _gate_invocations(text: str) -> list[str]:
    """The uncommented ``ensure_group_gate`` calls, excluding its definition."""
    return [
        call
        for call in _uncommented_invocations(text, "ensure_group_gate")
        if not call.startswith("ensure_group_gate()")
    ]


def _gate_helper_body(text: str) -> str:
    match = re.search(r"^ensure_group_gate\(\) \{\n(.*?)^\}", text, re.MULTILINE | re.DOTALL)
    assert match, "entra script must define the ensure_group_gate helper"
    return match.group(1)


def test_entra_script_gates_bff_on_group() -> None:
    """The BFF confidential client is gated by the same provisioning group as
    the public MCP client and the SAGE resource (CAS-ADR-044), through the one
    gate helper: the group is assigned to the BFF's default-access role on its
    appRoleAssignedTo collection, then app-role assignment is required -- in
    that order, so the gate never locks out an empty allowlist.

    The executed bootstrap tests observe the resulting directory state and the
    call order; this anchors the codified form the runbook mirrors.
    """
    text = _text(ENTRA)
    assert any(
        '"${BFF_SP_ID}"' in call and '"${DEFAULT_ACCESS_APP_ROLE_ID}"' in call
        for call in _gate_invocations(text)
    ), "entra script must gate the BFF service principal on its default-access role"
    body = _gate_helper_body(text)
    # Anchor on the request body, never prose: the assignment carries the
    # principalId/resourceId/appRoleId triple, targeting the shared group.
    assert "appRoleAssignedTo" in body, "the gate must assign via appRoleAssignedTo"
    for key in ("principalId", "resourceId", "appRoleId", "PROVISIONING_GROUP_ID"):
        assert key in body, f"the gate's assignment body must carry {key}"
    assert re.search(r'appRoleAssignmentRequired"\s*:\s*true', body), (
        'the gate must PATCH "appRoleAssignmentRequired": true'
    )
    assert body.index("--method POST") < body.index("appRoleAssignmentRequired"), (
        "the group assignment must precede appRoleAssignmentRequired, so the "
        "gate never engages before its allowlist exists"
    )


def test_entra_script_gates_public_client_on_group() -> None:
    """The public MCP client is gated by the same provisioning group as the
    browser client and the SAGE resource (CAS-ADR-044), through the same gate
    helper on its default-access role.
    """
    text = _text(ENTRA)
    assert any(
        '"${MCP_CLIENT_SP_ID}"' in call and '"${DEFAULT_ACCESS_APP_ROLE_ID}"' in call
        for call in _gate_invocations(text)
    ), "entra script must gate the public MCP client's service principal on its default-access role"
    assert any(
        '"${SAGE_SP_ID}"' in call and '"${SAGE_READER_ROLE_ID}"' in call
        for call in _gate_invocations(text)
    ), "entra script must gate the SAGE resource service principal on the Sage.Reader role"


def test_entra_script_emits_mcp_client_id_coordinate() -> None:
    """The Entra script emits the ``mcpClientId`` coordinate for the public MCP
    client it created, alongside sageAudience and bffOidcClientId, so the
    operator pastes it straight into the parameter set.
    """
    echoed = "\n".join(
        line.strip() for line in _text(ENTRA).splitlines() if line.lstrip().startswith("echo")
    )
    assert "mcpClientId" in echoed, "entra script must echo the mcpClientId coordinate"


# --- Executed Entra bootstrap -------------------------------------------------
#
# The structural checks above read the script; these run it, under bash, against
# a stateful stand-in for the Azure CLI's Entra surface (``_fake_entra_az.py``)
# that holds a model directory: applications, service principals, groups and
# app-role assignments. Assertions read the final directory state and the
# recorded call sequence, so they observe what a run actually does to a tenant.

FAKE_ENTRA_AZ: Final[Path] = Path(__file__).with_name("_fake_entra_az.py")
_SAGE_APP: Final[str] = "sage-resource-server"
_BFF_APP: Final[str] = "cas-bff"
_MCP_APP: Final[str] = "cas-mcp-client"
_GROUP: Final[str] = "cas-sage-users"


def _seed_directory() -> dict:
    """A tenant holding only Microsoft Graph's first-party service principal."""
    return {
        "apps": [],
        "sps": [
            {
                "id": str(uuid.uuid4()),
                "appId": str(uuid.uuid4()),
                "displayName": "Microsoft Graph",
                "appRoleAssignmentRequired": False,
                "oauth2PermissionScopes": [{"id": str(uuid.uuid4()), "value": "offline_access"}],
            }
        ],
        "groups": [],
        "assignments": [],
        "faults": [],
    }


def _run_entra(
    tmp_path: Path, state: dict, env: dict[str, str] | None = None, *, bare_ids: bool = False
) -> tuple:
    """Run the Entra bootstrap against ``state``; return (result, calls, final state).

    ``state`` is persisted between runs in ``tmp_path``, so calling this twice
    re-runs the bootstrap against the directory the first run left behind. The
    scope and role ids are passed in the environment, as an operator following
    the runbook does, unless ``bare_ids`` leaves the script to resolve them.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    az = bin_dir / "az"
    az.write_text(f"#!{sys.executable} -IS\n" + FAKE_ENTRA_AZ.read_text())
    az.chmod(0o755)
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state))
    calls_path = tmp_path / "calls.jsonl"
    calls_path.write_text("")
    ids_path = tmp_path / "ids.json"
    if not ids_path.exists():
        ids_path.write_text(json.dumps({"scope": str(uuid.uuid4()), "role": str(uuid.uuid4())}))
    ids = json.loads(ids_path.read_text())
    result = subprocess.run(
        ["bash", str(ENTRA)],
        cwd=REPO_ROOT,
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "AZURE_STATE": str(state_path),
            "AZURE_CALLS": str(calls_path),
            "BFF_HOSTNAME": "bff.example.test",
            "MCP_CLIENT_REDIRECT_URI": "http://127.0.0.1/callback",
            "SAGE_PUBLIC_HOSTNAME": "sage.example.test",
            **({} if bare_ids else {"ACCESS_SCOPE_ID": ids["scope"]}),
            **({} if bare_ids else {"SAGE_READER_ROLE_ID": ids["role"]}),
            **(env or {}),
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    return result, calls, json.loads(state_path.read_text())


def _app(state: dict, name: str) -> dict:
    matches = [a for a in state["apps"] if a["displayName"] == name]
    assert len(matches) == 1, f"expected exactly one {name} application, found {len(matches)}"
    return matches[0]


def _sp_of(state: dict, name: str) -> dict:
    app_id = _app(state, name)["appId"]
    matches = [s for s in state["sps"] if s["appId"] == app_id]
    assert len(matches) == 1, f"expected exactly one {name} service principal, found {len(matches)}"
    return matches[0]


def _group(state: dict, name: str = _GROUP) -> dict:
    matches = [g for g in state["groups"] if g["displayName"] == name]
    assert len(matches) == 1, f"expected exactly one {name} group, found {len(matches)}"
    return matches[0]


def _rest_calls(calls: list[list[str]], method: str, url_suffix: str) -> list[int]:
    """Indices of ``az rest`` calls with ``method`` whose URL ends in ``url_suffix``."""
    out = []
    for i, call in enumerate(calls):
        if call[:1] != ["rest"]:
            continue
        url = call[call.index("--url") + 1] if "--url" in call else call[call.index("--uri") + 1]
        if call[call.index("--method") + 1].upper() == method and url.endswith(url_suffix):
            out.append(i)
    return out


def _requirement_patches(calls: list[list[str]], sp_id: str) -> list[int]:
    return [
        i
        for i in _rest_calls(calls, "PATCH", f"/servicePrincipals/{sp_id}")
        if "appRoleAssignmentRequired" in calls[i][calls[i].index("--body") + 1]
    ]


def _assignment_posts(calls: list[list[str]], sp_id: str) -> list[int]:
    return _rest_calls(calls, "POST", f"/servicePrincipals/{sp_id}/appRoleAssignedTo") + (
        _rest_calls(calls, "POST", f"/servicePrincipals/{sp_id}/appRoleAssignments")
    )


def test_entra_bootstrap_gates_sage_resource_on_group(tmp_path: Path) -> None:
    """The access-group gate holds at the SAGE resource itself (CAS-ADR-044).

    Gating only the two client registrations leaves every other client --
    Azure CLI, which the bootstrap pre-authorizes, or any app a user consents
    to -- able to obtain a token for the SAGE audience. A run must leave the
    SAGE service principal requiring assignment, with the access group
    assigned to the Sage.Reader role (the role it declares for users). On every
    gated principal the assignment lands before the requirement, so the gate
    never engages ahead of its allowlist.
    """
    result, calls, state = _run_entra(tmp_path, _seed_directory())
    assert result.returncode == 0, result.stderr
    sage_sp = _sp_of(state, _SAGE_APP)
    group = _group(state)
    reader_role = next(
        r["id"] for r in _app(state, _SAGE_APP)["appRoles"] if r["value"] == "Sage.Reader"
    )
    assert sage_sp["appRoleAssignmentRequired"] is True, (
        "the SAGE resource service principal must require app-role assignment"
    )
    assert any(
        a["resourceId"] == sage_sp["id"]
        and a["principalId"] == group["id"]
        and a["appRoleId"] == reader_role
        for a in state["assignments"]
    ), "the access group must be assigned to the SAGE resource's Sage.Reader role"
    for name in (_SAGE_APP, _BFF_APP, _MCP_APP):
        sp = _sp_of(state, name)
        assert sp["appRoleAssignmentRequired"] is True, f"{name} must require assignment"
        posts, patches = _assignment_posts(calls, sp["id"]), _requirement_patches(calls, sp["id"])
        assert posts and patches, f"{name}: expected an assignment and a requirement call"
        assert max(posts) < min(patches), (
            f"{name}: the group assignment must land before assignment is required"
        )


def test_entra_bootstrap_rerun_converges(tmp_path: Path) -> None:
    """A re-run reconciles by lookup, not by tolerating failures.

    The second run against the directory the first left behind succeeds,
    creates no further applications, service principals, groups or
    assignments, and issues no assignment POST at all -- it finds each
    assignment already present rather than retrying it and discarding the
    error.
    """
    first, _, state = _run_entra(tmp_path, _seed_directory())
    assert first.returncode == 0, first.stderr
    second, calls, rerun = _run_entra(tmp_path, state)
    assert second.returncode == 0, second.stderr
    for key in ("apps", "sps", "groups", "assignments"):
        assert len(rerun[key]) == len(state[key]), f"a re-run must not add {key}"
    posts = [i for i in _rest_calls(calls, "POST", "") if "appRoleAssign" in " ".join(calls[i])]
    assert posts == [], "a re-run must find existing assignments instead of re-posting them"


def test_entra_bootstrap_assigns_the_intended_role_beside_another(tmp_path: Path) -> None:
    """An assignment of the group to a different role does not count as the gate.

    On a tenant where the group already holds some other role on the SAGE
    principal, the run must still assign it ``Sage.Reader``: the existence
    check matches the role as well as the principal and resource.
    """
    first, _, built = _run_entra(tmp_path, _seed_directory())
    assert first.returncode == 0, first.stderr
    sage_sp = _sp_of(built, _SAGE_APP)
    reader_role = next(
        r["id"] for r in _app(built, _SAGE_APP)["appRoles"] if r["value"] == "Sage.Reader"
    )
    for assignment in built["assignments"]:
        if assignment["resourceId"] == sage_sp["id"]:
            assignment["appRoleId"] = "-".join("0" * n for n in (8, 4, 4, 4, 12))
    result, _, final = _run_entra(tmp_path, built)
    assert result.returncode == 0, result.stderr
    roles = [a["appRoleId"] for a in final["assignments"] if a["resourceId"] == sage_sp["id"]]
    assert reader_role in roles, "the group must hold Sage.Reader on the SAGE principal"


def test_entra_bootstrap_warns_without_application_reader(tmp_path: Path) -> None:
    """The run reports when no application principal holds Sage.Reader.

    Requiring assignment on the resource stops an application without the
    role from obtaining a token, so a tenant whose deploy identity was never
    granted gets a warning; one whose deploy identity holds the role does not.
    The warning does not fail the run, because a fresh tenant grants the
    deploy identity only after this bootstrap.
    """
    first, _, built = _run_entra(tmp_path / "fresh", _seed_directory())
    assert first.returncode == 0, first.stderr
    assert "no application principal holds Sage.Reader" in first.stderr

    sage_sp = _sp_of(built, _SAGE_APP)
    reader_role = next(
        r["id"] for r in _app(built, _SAGE_APP)["appRoles"] if r["value"] == "Sage.Reader"
    )
    application = {
        "id": str(uuid.uuid4()),
        "principalId": str(uuid.uuid4()),
        "principalType": "ServicePrincipal",
        "resourceId": sage_sp["id"],
        "appRoleId": "-".join("0" * n for n in (8, 4, 4, 4, 12)),
    }
    built["assignments"].append(application)
    (tmp_path / "fresh" / "ids.json").rename(tmp_path / "ids.json")
    other_role, _, built = _run_entra(tmp_path, built)
    assert other_role.returncode == 0, other_role.stderr
    assert "no application principal holds Sage.Reader" in other_role.stderr, (
        "an application holding some other role does not satisfy the check"
    )

    next(a for a in built["assignments"] if a["id"] == application["id"])["appRoleId"] = reader_role
    granted, _, _ = _run_entra(tmp_path, built)
    assert granted.returncode == 0, granted.stderr
    assert "no application principal holds Sage.Reader" not in granted.stderr


def test_entra_bootstrap_rerun_keeps_scope_and_role_ids(tmp_path: Path) -> None:
    """A re-run without the ids in the environment keeps the live ones.

    The registration PATCH replaces the scope and role collections, so minting
    fresh ids on a re-run would orphan the access group's Sage.Reader
    assignment and every consent grant. The second run must reuse the ids the
    registration already carries, leaving the existing assignment valid.
    """
    first, _, state = _run_entra(tmp_path, _seed_directory(), bare_ids=True)
    assert first.returncode == 0, first.stderr
    app = _app(state, _SAGE_APP)
    scope_id = app["api"]["oauth2PermissionScopes"][0]["id"]
    role_id = app["appRoles"][0]["id"]
    second, _, rerun = _run_entra(tmp_path, state, bare_ids=True)
    assert second.returncode == 0, second.stderr
    app = _app(rerun, _SAGE_APP)
    assert [s["id"] for s in app["api"]["oauth2PermissionScopes"]] == [scope_id]
    assert [r["id"] for r in app["appRoles"]] == [role_id]
    sage_sp = _sp_of(rerun, _SAGE_APP)
    assert [a["appRoleId"] for a in rerun["assignments"] if a["resourceId"] == sage_sp["id"]] == [
        role_id
    ], "the group's Sage.Reader assignment must still name the live role"


def test_entra_bootstrap_ignores_lookalike_display_names(tmp_path: Path) -> None:
    """Directory objects are selected by exact display name.

    A prefix match would pick ``sage-resource-server-old`` or
    ``cas-sage-users-legacy`` on a tenant that holds them; the run must leave
    such look-alikes untouched and act only on the exactly-named objects.
    """
    state = _seed_directory()
    lookalike_app = {
        "appId": str(uuid.uuid4()),
        "id": str(uuid.uuid4()),
        "displayName": f"{_SAGE_APP}-old",
        "identifierUris": [],
        "api": {},
        "appRoles": [],
    }
    lookalike_group = {"id": str(uuid.uuid4()), "displayName": f"{_GROUP}-legacy"}
    state["apps"].append(lookalike_app)
    state["groups"].append(lookalike_group)
    before = dict(lookalike_app)

    result, calls, final = _run_entra(tmp_path, state)
    assert result.returncode == 0, result.stderr
    after = next(a for a in final["apps"] if a["id"] == lookalike_app["id"])
    assert after == before, "the look-alike application must not be modified"
    assert not any(a["principalId"] == lookalike_group["id"] for a in final["assignments"]), (
        "the look-alike group must not be assigned to anything"
    )
    _app(final, _SAGE_APP)
    group = _group(final)
    assert any(a["principalId"] == group["id"] for a in final["assignments"])


def test_entra_bootstrap_refuses_duplicate_exact_names(tmp_path: Path) -> None:
    """Two objects with the same exact name stop the run before any gate write.

    Picking either one could gate SAGE on the wrong group, so the run exits
    non-zero, names the ambiguity, and records no assignment or
    assignment-requirement call.
    """
    state = _seed_directory()
    state["groups"] += [
        {"id": str(uuid.uuid4()), "displayName": _GROUP},
        {"id": str(uuid.uuid4()), "displayName": _GROUP},
    ]
    result, calls, _ = _run_entra(tmp_path, state)
    assert result.returncode != 0, "duplicate exact-name groups must stop the run"
    assert _GROUP in result.stderr and "exactly one" in result.stderr, result.stderr
    gate_calls = [c for c in calls if c[:1] == ["rest"] and ("appRoleAssign" in " ".join(c))]
    assert gate_calls == [], f"no gate write may happen on an ambiguous lookup: {gate_calls}"


def test_entra_bootstrap_rejects_quoted_group_name(tmp_path: Path) -> None:
    """A group name carrying a single quote is refused before any lookup uses it.

    The name is spliced into a quoted OData ``eq`` literal, so a quote would end
    the literal and let the rest of the value rewrite the filter -- for example
    selecting a different, existing group. The run stops before any group
    lookup and before any gate write.
    """
    state = _seed_directory()
    other = {"id": str(uuid.uuid4()), "displayName": "other-group"}
    state["groups"].append(other)
    result, calls, _ = _run_entra(
        tmp_path,
        state,
        env={"PROVISIONING_GROUP_NAME": "missing' or displayName eq 'other-group"},
    )
    assert result.returncode != 0, "a quoted group name must stop the run"
    assert "single quote" in result.stderr, result.stderr
    assert not [c for c in calls if c[:3] == ["ad", "group", "list"]], (
        "the quoted name must never reach a group lookup"
    )
    assert not [c for c in calls if "appRoleAssign" in " ".join(c)]


def test_entra_bootstrap_stops_on_unexpected_error(tmp_path: Path) -> None:
    """An assignment failure aborts the run; it is never swallowed.

    The BFF's group assignment is refused with an authorization error. The run
    must exit non-zero and must not go on to require assignment on that
    principal, since the gate's allowlist was never written.
    """
    # Build a converged tenant, then withdraw the BFF's gate and refuse its
    # re-assignment, so the re-run reaches that write and is denied there.
    first, _, built = _run_entra(tmp_path, _seed_directory())
    assert first.returncode == 0, first.stderr
    bff_sp = _sp_of(built, _BFF_APP)
    built["assignments"] = [a for a in built["assignments"] if a["resourceId"] != bff_sp["id"]]
    bff_sp["appRoleAssignmentRequired"] = False
    built["faults"] = [
        {
            "contains": ["POST", f"/servicePrincipals/{bff_sp['id']}/appRoleAssign"],
            "message": "Authorization_RequestDenied: Insufficient privileges.",
            "code": 1,
        }
    ]
    result, calls, final = _run_entra(tmp_path, built)
    assert result.returncode != 0, "a refused assignment must fail the run"
    assert _sp_of(final, _BFF_APP)["appRoleAssignmentRequired"] is False
    assert _requirement_patches(calls, bff_sp["id"]) == [], (
        "assignment must not be required after its allowlist write failed"
    )


def test_entra_bootstrap_stops_when_a_lookup_fails(tmp_path: Path) -> None:
    """A failed lookup stops the run; it never reads as "not found".

    On a converged tenant the lookup for the BFF registration fails. Treating
    the empty output as absence would create a second ``cas-bff``; the run must
    exit non-zero instead and leave the directory's applications unchanged.
    """
    first, _, built = _run_entra(tmp_path, _seed_directory())
    assert first.returncode == 0, first.stderr
    built["faults"] = [
        {
            "contains": ["ad", "app", "list", "displayName eq 'cas-bff'"],
            "message": "Service unavailable.",
            "code": 1,
        }
    ]
    result, _, final = _run_entra(tmp_path, built)
    assert result.returncode != 0, "a failed lookup must fail the run"
    assert [a["displayName"] for a in final["apps"]] == [a["displayName"] for a in built["apps"]], (
        "a failed lookup must not lead to creating the application again"
    )


def test_entra_bootstrap_sage_access_is_admin_consent_only(tmp_path: Path) -> None:
    """``Sage.Access`` is admin-consent-only.

    Every supported client is either admin-consented (the BFF and the public
    MCP client) or pre-authorized (Azure CLI), so none needs a user to consent.
    A user-consentable scope would let any member grant an arbitrary app
    delegated SAGE access.
    """
    result, _, state = _run_entra(tmp_path, _seed_directory())
    assert result.returncode == 0, result.stderr
    scopes = _app(state, _SAGE_APP)["api"]["oauth2PermissionScopes"]
    access = [s for s in scopes if s["value"] == "Sage.Access"]
    assert len(access) == 1 and access[0]["type"] == "Admin", access


_LOOPBACK: Final[str] = "http://localhost/callback"
_BFF_CALLBACK: Final[str] = "https://bff.example.test/app/auth/callback"


def _redirects(state: dict, name: str, platform: str) -> list[str]:
    return _app(state, name)[platform]["redirectUris"]


def test_entra_script_default_callback_path_matches_the_bff() -> None:
    """The bootstrap's default ``AUTH_CALLBACK_PATH`` is the path the BFF serves.

    The BFF's OIDC callback route is ``CALLBACK_PATH`` in its auth config. A
    default that differs registers a callback the BFF never answers, and since
    a re-run keeps every live redirect URI, a wrong one would accumulate
    beside the right one rather than replace it.
    """
    from app.backend.auth.config import CALLBACK_PATH

    match = re.search(
        r'^AUTH_CALLBACK_PATH="\$\{AUTH_CALLBACK_PATH:-([^}]*)\}"$', _text(ENTRA), re.M
    )
    assert match, "entra script must default AUTH_CALLBACK_PATH"
    assert "/" + match.group(1) == CALLBACK_PATH, (
        f"the default {match.group(1)!r} must be the BFF's CALLBACK_PATH {CALLBACK_PATH!r} "
        "without its leading slash"
    )


def test_entra_bootstrap_first_run_registers_exact_redirect_set(tmp_path: Path) -> None:
    """On an empty tenant each client receives exactly the redirect URIs it requires.

    The public MCP client gets its configured redirect and the
    ``http://localhost/callback`` loopback; the BFF gets its single OIDC
    callback. Nothing else is registered on either platform.
    """
    result, _, state = _run_entra(tmp_path, _seed_directory())
    assert result.returncode == 0, result.stderr
    assert _redirects(state, _MCP_APP, "publicClient") == ["http://127.0.0.1/callback", _LOOPBACK]
    assert _redirects(state, _MCP_APP, "web") == [], "the MCP client must stay a public client"
    assert _redirects(state, _BFF_APP, "web") == [_BFF_CALLBACK]
    assert _redirects(state, _BFF_APP, "publicClient") == []


def test_entra_bootstrap_rerun_keeps_existing_redirect_uris(tmp_path: Path) -> None:
    """A re-run adds the required redirect URIs and removes none.

    The redirect-URI flags replace their whole collection, so a re-run that
    wrote only the required set would delete every URI a client registered
    later -- such as the per-port loopbacks a desktop MCP client adds. The
    re-run must keep those, keep the required URIs, and list none twice.
    """
    first, _, built = _run_entra(tmp_path, _seed_directory())
    assert first.returncode == 0, first.stderr
    extra_mcp = [
        "http://127.0.0.1:53682/callback/first",
        "http://127.0.0.1:61017/callback/second",
    ]
    extra_bff = "https://bff-old.example.test/app/auth/callback"
    _app(built, _MCP_APP)["publicClient"]["redirectUris"] += extra_mcp
    _app(built, _BFF_APP)["web"]["redirectUris"].append(extra_bff)
    result, _, rerun = _run_entra(tmp_path, built)
    assert result.returncode == 0, result.stderr
    mcp = _redirects(rerun, _MCP_APP, "publicClient")
    assert mcp == ["http://127.0.0.1/callback", _LOOPBACK, *extra_mcp], mcp
    bff = _redirects(rerun, _BFF_APP, "web")
    assert bff == [_BFF_CALLBACK, extra_bff], bff


def test_entra_bootstrap_rerun_restores_a_missing_required_redirect(tmp_path: Path) -> None:
    """A re-run adds back a required redirect URI that is missing, beside the extras."""
    first, _, built = _run_entra(tmp_path, _seed_directory())
    assert first.returncode == 0, first.stderr
    extra = "http://127.0.0.1:53682/callback/first"
    _app(built, _MCP_APP)["publicClient"]["redirectUris"] = [extra]
    result, _, rerun = _run_entra(tmp_path, built)
    assert result.returncode == 0, result.stderr
    assert _redirects(rerun, _MCP_APP, "publicClient") == [
        extra,
        "http://127.0.0.1/callback",
        _LOOPBACK,
    ]


def test_entra_bootstrap_splits_comma_separated_mcp_redirects(tmp_path: Path) -> None:
    """A comma-separated ``MCP_CLIENT_REDIRECT_URI`` registers one entry per URI.

    Whitespace around each entry is trimmed, and a URI repeated in the list,
    or equal to the loopback the script adds itself, is registered once.
    """
    result, _, state = _run_entra(
        tmp_path,
        _seed_directory(),
        {
            "MCP_CLIENT_REDIRECT_URI": (
                "https://claude.ai/api/mcp/auth_callback, http://127.0.0.1/callback,"
                f"{_LOOPBACK},https://claude.ai/api/mcp/auth_callback"
            )
        },
    )
    assert result.returncode == 0, result.stderr
    assert _redirects(state, _MCP_APP, "publicClient") == [
        "https://claude.ai/api/mcp/auth_callback",
        "http://127.0.0.1/callback",
        _LOOPBACK,
    ]


def test_entra_bootstrap_rejects_empty_mcp_redirect_entry(tmp_path: Path) -> None:
    """An empty entry in ``MCP_CLIENT_REDIRECT_URI`` stops the run before any az call.

    A doubled or trailing comma is a typo, not a request to register an empty
    URI, and a multi-line value would otherwise be cut at its first newline;
    the run refuses both before it calls the directory at all.
    """
    for value in (
        "http://127.0.0.1/callback,,https://claude.ai/api/mcp/auth_callback",
        "a, ",
        "http://127.0.0.1/callback\nhttps://claude.ai/api/mcp/auth_callback",
    ):
        result, calls, final = _run_entra(
            tmp_path, _seed_directory(), {"MCP_CLIENT_REDIRECT_URI": value}
        )
        assert result.returncode != 0, f"{value!r} must be refused"
        assert "MCP_CLIENT_REDIRECT_URI" in result.stderr, result.stderr
        assert calls == [], f"{value!r}: no az call may happen"
        assert final["apps"] == []


def test_entra_bootstrap_stops_when_redirect_read_fails(tmp_path: Path) -> None:
    """A failed read of the live redirect set stops the run; it never reads as empty.

    Reading the set as empty would write back the required URIs alone and
    delete every other one -- the loss the merge exists to prevent.
    """
    first, _, built = _run_entra(tmp_path, _seed_directory())
    assert first.returncode == 0, first.stderr
    extra = "http://127.0.0.1:53682/callback/first"
    _app(built, _MCP_APP)["publicClient"]["redirectUris"].append(extra)
    built["faults"] = [
        {
            "contains": ["ad", "app", "show", "--query publicClient"],
            "message": "Service unavailable.",
            "code": 1,
        }
    ]
    result, calls, final = _run_entra(tmp_path, built)
    assert result.returncode != 0, "a failed redirect read must fail the run"
    assert extra in _redirects(final, _MCP_APP, "publicClient")
    assert not _public_client_writes(calls), (
        "the public-client set must not be written after its read failed"
    )


def _public_client_writes(calls: list[list[str]]) -> list[list[str]]:
    return [
        c
        for c in calls
        if c[:3] == ["ad", "app", "update"] and "--public-client-redirect-uris" in c
    ]


@pytest.mark.parametrize(
    "platform",
    [None, {}, {"redirectUris": None}, {"redirectUris": "http://127.0.0.1/callback"}],
    ids=["absent", "no-redirect-uris", "null-list", "not-a-list"],
)
def test_entra_bootstrap_refuses_an_unreadable_redirect_platform(
    tmp_path: Path, platform: dict | None
) -> None:
    """A read that yields no redirect-URI list stops the run; it never reads as empty.

    The read itself succeeds here, so only the shape of what it returns can
    tell an empty platform from a field the query did not find. A query path
    that matches nothing exits 0 with no output, and writing the required set
    back over it would delete every live URI.
    """
    first, _, built = _run_entra(tmp_path, _seed_directory())
    assert first.returncode == 0, first.stderr
    app = _app(built, _MCP_APP)
    if platform is None:
        del app["publicClient"]
    else:
        app["publicClient"] = platform
    result, calls, _ = _run_entra(tmp_path, built)
    assert result.returncode != 0, "an unreadable redirect platform must fail the run"
    assert "refusing to overwrite" in result.stderr, result.stderr
    assert not _public_client_writes(calls), "the public-client set must not be written"


def test_kv_secrets_script_reads_secrets_from_env_not_args() -> None:
    """Secret material comes from the environment, is ``unset`` after use, and
    reaches the CLI through a file, never as a command-line argument: no secret
    is passed with ``--value``, and the bundle password only as ``@<file>``.
    The executed tests below observe the argument vectors themselves.
    """
    text = _text(KEY_VAULT)
    assert not re.search(r"--value\b", text), "secrets must not be passed with --value"
    passwords = re.findall(r"--password\s+(\S+)", text)
    assert passwords, "the certificate import must take its password"
    for token in passwords:
        assert token.startswith('"@'), f"the bundle password must be passed as @<file>: {token!r}"
    assert not re.search(r"-passin\s+\"?pass:", text), (
        "OpenSSL must not read the password from argv"
    )
    # Anchor on the unset statements, not the prose that describes them.
    unset_stmts = [line for line in text.splitlines() if line.lstrip().startswith("unset ")]
    assert unset_stmts, "secret env vars must be unset after use"


def test_kv_secrets_script_loads_the_three_artifacts() -> None:
    """The loader sets the secrets, generates the ingress key, and imports the
    wildcard certificate, under the fixed names the Key Vault module's outputs
    pin.
    """
    text = _text(KEY_VAULT)
    for name in ("anthropic-api-key", "bff-client-secret", "sage-ingress-key"):
        assert re.search(rf"^\s*set_secret {name} ", text, re.MULTILINE), f"loader must set {name}"
    assert "wildcard-tls" in text, "loader must import the wildcard-tls certificate"
    assert "keyvault certificate import" in text, "loader must use certificate import"
    assert "keyvault secret set" in text, "the secrets must be set through the CLI"


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
    """The seed upload is create-or-replace and each grant is looked up before it
    is posted, so a re-run converges without swallowing a failed call.

    A ``|| true`` guard would make a refused grant indistinguishable from one
    already in place; the executed tests below hold the behaviour, this one
    holds the shape: every POST is preceded by a GET of the collection it writes.
    """
    text = _text(VAULT_SEED)
    uri_lines = [line for line in text.splitlines() if "--uri" in line]
    assert any(":/content" in line for line in uri_lines), (
        "seed upload must be the create-or-replace :/content PUT"
    )
    assert "|| true" not in text, "seed script must not tolerate a failed call with `|| true`"
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "--method POST" not in line:
            continue
        target = next(
            (
                found
                for j in range(i, min(i + 3, len(lines)))
                if (found := re.search(r"/v1\.0/(\S+?)\"", lines[j])) is not None
            ),
            None,
        )
        assert target is not None, f"POST at line {i + 1} has no --uri"
        collection = target.group(1)
        assert any(
            "--method GET" in lines[k] and collection in "\n".join(lines[k : k + 3])
            for k in range(i)
        ), f"POST to {collection} must be preceded by a GET of the same collection"


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


# --- Executed vault-source seed -----------------------------------------------
#
# The seed script runs, under every available bash, against the same stateful
# stand-in for the Azure CLI, here modelling the SAGE managed identity, Microsoft
# Graph's service principal and one SharePoint site with its document library.
# Assertions read the final tenant state and the recorded calls.

_SEED_RG: Final[str] = "rg-seed-test"
_SEED_IDENTITY: Final[str] = "id-sage-seed-test"
_SEED_HOST: Final[str] = "contoso.sharepoint.com"
_SEED_SITE_PATH: Final[str] = "/sites/cas-vaults"
_SEED_LIBRARY: Final[str] = "Documents"


def _bash_interpreters() -> list[str]:
    found = [shutil.which("bash")]
    if Path("/bin/bash").exists():
        found.append("/bin/bash")
    return sorted({b for b in found if b})


SEED_BASHES = pytest.mark.parametrize("bash", _bash_interpreters())


def _graph_sp(name: str = "Microsoft Graph") -> dict:
    return {
        "id": str(uuid.uuid4()),
        "appId": str(uuid.uuid4()),
        "displayName": name,
        "appRoleAssignmentRequired": False,
        "appRoles": [
            {"id": str(uuid.uuid4()), "value": "Sites.Read.All"},
            {"id": str(uuid.uuid4()), "value": "Sites.Selected"},
        ],
    }


def _seed_tenant() -> dict:
    """A tenant holding the SAGE identity, Microsoft Graph and one SharePoint site."""
    client_id = str(uuid.uuid4())
    sage_sp = {
        "id": str(uuid.uuid4()),
        "appId": client_id,
        "displayName": _SEED_IDENTITY,
        "appRoleAssignmentRequired": False,
    }
    return {
        "apps": [],
        "sps": [sage_sp, _graph_sp()],
        "groups": [],
        "assignments": [],
        "identities": [
            {
                "name": _SEED_IDENTITY,
                "resourceGroup": _SEED_RG,
                "clientId": client_id,
                "principalId": sage_sp["id"],
            }
        ],
        "sites": [
            {
                "id": f"{_SEED_HOST},{uuid.uuid4()},{uuid.uuid4()}",
                "hostname": _SEED_HOST,
                "path": _SEED_SITE_PATH,
                "drives": [
                    {"id": f"b!{uuid.uuid4().hex}", "name": "Site Assets"},
                    {"id": f"b!{uuid.uuid4().hex}", "name": _SEED_LIBRARY},
                ],
                "permissions": [],
            }
        ],
        "uploads": [],
        "faults": [],
    }


def _sites_token(scopes: str = "Sites.FullControl.All") -> str:
    """An unsigned stand-in for a delegated Graph token carrying ``scopes``.

    It verifies nothing; the seed and the fake ``az`` read only its ``scp`` claim.
    """

    def segment(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{segment({'alg': 'none'})}.{segment({'scp': scopes})}.unsigned"


def _run_seed(
    tmp_path: Path, state: dict, bash: str = "bash", sites_token: str | None = None
) -> tuple:
    """Run the seed script against ``state``; return (result, calls, final state).

    ``state`` persists in ``tmp_path`` between runs, so a second call re-runs the
    script against the tenant the first left behind. The operator-supplied
    ``GRAPH_SITES_TOKEN`` defaults to one carrying ``Sites.FullControl.All``,
    so no run signs in.
    """
    if sites_token is None:
        sites_token = _sites_token()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    az = bin_dir / "az"
    az.write_text(f"#!{sys.executable} -IS\n" + FAKE_ENTRA_AZ.read_text())
    az.chmod(0o755)
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state))
    calls_path = tmp_path / "calls.jsonl"
    calls_path.write_text("")
    result = subprocess.run(
        [bash, str(VAULT_SEED)],
        cwd=REPO_ROOT,
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "AZURE_STATE": str(state_path),
            "AZURE_CALLS": str(calls_path),
            "RG": _SEED_RG,
            "SAGE_IDENTITY_NAME": _SEED_IDENTITY,
            "SITE_HOSTNAME": _SEED_HOST,
            "SITE_PATH": _SEED_SITE_PATH,
            "LIBRARY_NAME": _SEED_LIBRARY,
            "GRAPH_SITES_TOKEN": sites_token,
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    return result, calls, json.loads(state_path.read_text())


def _sage_identity(state: dict) -> dict:
    return state["identities"][0]


def _real_graph(state: dict) -> dict:
    matches = [s for s in state["sps"] if s["displayName"] == "Microsoft Graph"]
    assert len(matches) == 1
    return matches[0]


def _sites_selected(sp: dict) -> str:
    return next(r["id"] for r in sp["appRoles"] if r["value"] == "Sites.Selected")


def _grant_posts(calls: list[list[str]]) -> list[int]:
    return [i for i, c in enumerate(calls) if c[:1] == ["rest"] and "POST" in c]


def _write_grants(state: dict) -> list[dict]:
    client_id = _sage_identity(state)["clientId"]
    return [
        p
        for p in state["sites"][0]["permissions"]
        if "write" in p.get("roles", [])
        and any(
            g.get("application", {}).get("id") == client_id
            for g in p.get("grantedToIdentitiesV2", [])
        )
    ]


@SEED_BASHES
def test_vault_seed_converges_on_a_fresh_tenant(tmp_path: Path, bash: str) -> None:
    """A first run grants Sites.Selected on Graph, grants write on the one site,
    seeds the committed config, and emits the site and drive coordinates.
    """
    state = _seed_tenant()
    result, _, final = _run_seed(tmp_path, state, bash)
    assert result.returncode == 0, result.stderr
    graph = _real_graph(final)
    identity = _sage_identity(final)
    assert [(a["principalId"], a["resourceId"], a["appRoleId"]) for a in final["assignments"]] == [
        (identity["principalId"], graph["id"], _sites_selected(graph))
    ]
    assert len(_write_grants(final)) == 1
    site = final["sites"][0]
    library = next(d for d in site["drives"] if d["name"] == _SEED_LIBRARY)
    seed = (REPO_ROOT / "deploy" / "test-vault" / "vault_config.yaml").read_text()
    assert final["uploads"] == [
        {
            "drive": library["id"],
            "path": "vaults/cloud_validation/vault_config.yaml",
            "content": seed,
        }
    ]
    assert f"param sharepointSiteId = '{site['id']}'" in result.stdout
    assert f"param sharepointDriveId = '{library['id']}'" in result.stdout


@SEED_BASHES
def test_vault_seed_grants_write_despite_other_site_grants(tmp_path: Path, bash: str) -> None:
    """Only a write grant to the SAGE identity counts as already in place.

    The site already grants write to another application and read to the SAGE
    identity; neither satisfies the check, so the write grant is still posted.
    It also holds a grant carrying neither an identity list nor roles, which
    the check must step over rather than fail on.
    """
    state = _seed_tenant()
    client_id = _sage_identity(state)["clientId"]

    def grant(app_id: str, role: str) -> dict:
        who = [{"application": {"id": app_id, "displayName": "app"}}]
        return {
            "id": str(uuid.uuid4()),
            "roles": [role],
            "grantedToIdentities": who,
            "grantedToIdentitiesV2": who,
        }

    state["sites"][0]["permissions"] = [
        grant(str(uuid.uuid4()), "write"),
        grant(client_id, "read"),
        {"id": str(uuid.uuid4())},
    ]
    result, _, final = _run_seed(tmp_path, state, bash)
    assert result.returncode == 0, result.stderr
    assert len(_write_grants(final)) == 1


@SEED_BASHES
def test_vault_seed_ignores_a_look_alike_graph_principal(tmp_path: Path, bash: str) -> None:
    """A principal whose name merely starts with "Microsoft Graph" is never
    selected. It is listed first, so a prefix match taking the first hit would
    grant its role instead of Graph's.
    """
    state = _seed_tenant()
    state["sps"].insert(0, _graph_sp("Microsoft Graph Connector Agent"))
    result, _, final = _run_seed(tmp_path, state, bash)
    assert result.returncode == 0, result.stderr
    graph = _real_graph(final)
    assert [(a["resourceId"], a["appRoleId"]) for a in final["assignments"]] == [
        (graph["id"], _sites_selected(graph))
    ]


@SEED_BASHES
def test_vault_seed_stops_on_duplicate_graph_principals(tmp_path: Path, bash: str) -> None:
    """Two principals named exactly "Microsoft Graph" stop the run before any
    grant: the script cannot tell which one to grant against.
    """
    state = _seed_tenant()
    state["sps"].append(_graph_sp())
    result, calls, final = _run_seed(tmp_path, state, bash)
    assert result.returncode != 0, "duplicate Graph principals must stop the run"
    assert "exactly one" in result.stderr, result.stderr
    assert _grant_posts(calls) == []
    assert final["uploads"] == []


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("no identity principal", lambda s: s["sps"].pop(0)),
        ("no Sites.Selected role", lambda s: s["sps"][1]["appRoles"].pop()),
        ("no document library", lambda s: s["sites"][0]["drives"].pop()),
    ],
)
@SEED_BASHES
def test_vault_seed_stops_when_a_lookup_resolves_empty(
    tmp_path: Path, bash: str, label: str, mutate: object
) -> None:
    """A lookup that finds nothing stops the run instead of posting an empty id."""
    state = _seed_tenant()
    mutate(state)  # type: ignore[operator]
    result, calls, final = _run_seed(tmp_path, state, bash)
    assert result.returncode != 0, f"{label} must stop the run"
    assert "ERROR" in result.stderr, result.stderr
    assert _grant_posts(calls) == []
    assert final["uploads"] == []


@pytest.mark.parametrize(
    "contains",
    [
        ["ad", "sp", "list", "appId eq"],
        ["ad", "sp", "list", "displayName eq 'Microsoft Graph'"],
        ["rest", "GET", "/appRoleAssignments"],
        ["rest", "GET", "/permissions"],
    ],
)
@SEED_BASHES
def test_vault_seed_stops_when_a_lookup_fails(
    tmp_path: Path, bash: str, contains: list[str]
) -> None:
    """A failed lookup inside a command substitution stops the run; it never
    reads as "absent" and goes on to post a grant or seed the vault.
    """
    state = _seed_tenant()
    state["faults"] = [{"contains": contains, "message": "Service unavailable.", "code": 1}]
    result, calls, final = _run_seed(tmp_path, state, bash)
    assert result.returncode != 0, f"a failed {contains} lookup must stop the run"
    failed = next(i for i, c in enumerate(calls) if all(t in " ".join(c) for t in contains))
    assert failed == len(calls) - 1, "nothing may run after the failed lookup"
    assert final["uploads"] == []


@pytest.mark.parametrize("grant", ["/appRoleAssignments", "/permissions"])
@SEED_BASHES
def test_vault_seed_stops_when_a_grant_is_refused(tmp_path: Path, bash: str, grant: str) -> None:
    """A refused grant fails the run before the vault is seeded."""
    state = _seed_tenant()
    state["faults"] = [
        {
            "contains": ["rest", "POST", grant],
            "message": "Authorization_RequestDenied: Insufficient privileges.",
            "code": 1,
        }
    ]
    result, _, final = _run_seed(tmp_path, state, bash)
    assert result.returncode != 0, f"a refused {grant} grant must fail the run"
    assert final["uploads"] == []


@SEED_BASHES
def test_vault_seed_rerun_is_idempotent(tmp_path: Path, bash: str) -> None:
    """A re-run against a converged tenant succeeds without posting either grant."""
    first, _, built = _run_seed(tmp_path, _seed_tenant(), bash)
    assert first.returncode == 0, first.stderr
    result, calls, final = _run_seed(tmp_path, built, bash)
    assert result.returncode == 0, result.stderr
    assert _grant_posts(calls) == [], "a converged tenant must not be granted again"
    assert final["assignments"] == built["assignments"]
    assert final["sites"][0]["permissions"] == built["sites"][0]["permissions"]
    assert len(final["uploads"]) == 2, "the config upload is create-or-replace on every run"


def _site_calls(calls: list[list[str]]) -> list[list[str]]:
    """The calls on a site's permissions or a drive upload -- steps 3 and 4."""
    return [c for c in calls if any("/permissions" in a or ":/content" in a for a in c)]


def _auth_header(call: list[str]) -> str | None:
    if "--headers" not in call:
        return None
    for item in call[call.index("--headers") + 1 :]:
        if item.startswith("--"):
            break
        if item.startswith("Authorization="):
            return item
    return None


@SEED_BASHES
def test_vault_seed_site_steps_use_the_sites_token_off_the_command_line(
    tmp_path: Path, bash: str
) -> None:
    """Steps 3 and 4 carry the Sites.FullControl.All token; nothing else does.

    The token reaches ``az`` through a private file the CLI expands
    (``Authorization=@path``), never as an argument any local process can read,
    and the file is gone once the run ends. The directory and site-lookup calls
    keep the CLI's own token.
    """
    token = _sites_token()
    result, calls, _ = _run_seed(tmp_path, _seed_tenant(), bash, token)
    assert result.returncode == 0, result.stderr
    site_calls = _site_calls(calls)
    assert {c[c.index("--method") + 1] for c in site_calls} == {"GET", "POST", "PUT"}
    headers = {_auth_header(c) for c in site_calls}
    assert len(headers) == 1 and next(iter(headers), "").startswith("Authorization=@"), headers
    token_file = Path(next(iter(headers)).split("@", 1)[1])
    assert not token_file.exists(), "the token file outlives the run"
    assert all(_auth_header(c) is None for c in calls if c not in site_calls)
    assert all(token not in " ".join(c) for c in calls), "the token reached a command line"


@SEED_BASHES
def test_vault_seed_stops_without_the_full_control_scope(tmp_path: Path, bash: str) -> None:
    """A token lacking Sites.FullControl.All stops the run before step 3.

    The site write scope is not enough: Graph refuses the site-permission calls
    without full control, so the run names the missing scope instead of failing
    on a 403 halfway through.
    """
    token = _sites_token("Sites.ReadWrite.All Files.ReadWrite.All")
    result, calls, final = _run_seed(tmp_path, _seed_tenant(), bash, token)
    assert result.returncode != 0, "a token without full control must stop the run"
    assert "Sites.FullControl.All" in result.stderr, result.stderr
    assert _site_calls(calls) == []
    assert final["sites"][0]["permissions"] == [] and final["uploads"] == []


@SEED_BASHES
def test_vault_seed_stops_when_the_tenant_lookup_fails(tmp_path: Path, bash: str) -> None:
    """The run resolves the signed-in tenant, which a minted token is bound to;
    a failed lookup stops it before any site call.
    """
    state = _seed_tenant()
    state["faults"] = [
        {"contains": ["account", "show"], "message": "Please run 'az login'.", "code": 1}
    ]
    result, calls, final = _run_seed(tmp_path, state, bash)
    assert result.returncode != 0, "a failed tenant lookup must stop the run"
    assert calls[-1][:2] == ["account", "show"], "nothing may run after the failed lookup"
    assert _site_calls(calls) == []
    assert final["uploads"] == []


# --- Executed Key Vault secret load -------------------------------------------
#
# The loader runs, under every available bash, against stand-ins for ``az`` and
# ``openssl`` that record each call's argument vector. Secret values must reach
# the CLI through files, never on a command line, where any local process can
# read them.


_FAKE_KV_TOOLS: Final[str] = r"""
import json, os, sys
from pathlib import Path

state_path = Path(os.environ["KV_STATE"])
state = json.loads(state_path.read_text())
tool = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["KV_CALLS"], "a") as stream:
    stream.write(json.dumps([tool, *args]) + "\n")
joined = " ".join([tool, *args])
for fault in state.get("faults", []):
    if all(token in joined for token in fault):
        sys.exit("ERROR: injected failure")

def arg(flag):
    return args[args.index(flag) + 1]

if tool == "openssl":
    if args[:2] == ["rand", "-hex"]:
        print("ab" * int(args[2]))
    elif args[0] == "pkcs12":
        source = arg("-passin")
        assert source.startswith("file:"), source
        if Path(source[5:]).read_text() != state["pfx_password"]:
            sys.exit("Mac verify error: invalid password?")
        print("-----BEGIN CERTIFICATE-----\nleaf\n-----END CERTIFICATE-----")
        print("-----BEGIN CERTIFICATE-----\nintermediate\n-----END CERTIFICATE-----")
    else:
        sys.exit(f"fake openssl: unsupported {args}")
elif args[:3] == ["keyvault", "secret", "set"]:
    assert "--value" not in args, "a secret value was passed on the command line"
    assert arg("--encoding") == "utf-8"
    state["secrets"][arg("--name")] = Path(arg("--file")).read_text()
elif args[:3] == ["keyvault", "secret", "list"]:
    assert arg("--query") == "[?name=='sage-ingress-key'].id"
    if "sage-ingress-key" in state["secrets"]:
        print("https://kv.example/secrets/sage-ingress-key")
elif args[:3] == ["keyvault", "certificate", "import"]:
    password = arg("--password")
    assert password.startswith("@"), "the bundle password was passed on the command line"
    if Path(password[1:]).read_text() != state["pfx_password"]:
        sys.exit("ERROR: the bundle password is wrong")
    state["certificates"][arg("--name")] = arg("--file")
else:
    sys.exit(f"fake az: unsupported {args}")
state_path.write_text(json.dumps(state))
"""

_KV_INPUTS: Final[dict[str, str]] = {
    "ANTHROPIC_API_KEY": "sk-anthropic-sentinel-0001",  # gitleaks:allow test sentinel
    "BFF_CLIENT_SECRET": "bff-secret-sentinel-0002",  # gitleaks:allow test sentinel
    "WILDCARD_TLS_PFX_PASSWORD": "pfx-password-sentinel-0003",  # gitleaks:allow test sentinel
}


def _run_kv_load(tmp_path: Path, state: dict, bash: str = "bash") -> tuple:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for tool in ("az", "openssl"):
        path = bin_dir / tool
        path.write_text(f"#!{sys.executable} -IS\n" + _FAKE_KV_TOOLS)
        path.chmod(0o755)
    state_path = tmp_path / "kv-state.json"
    state_path.write_text(json.dumps(state))
    calls_path = tmp_path / "kv-calls.jsonl"
    calls_path.write_text("")
    scratch = tmp_path / "tmp"
    scratch.mkdir(exist_ok=True)
    pfx = tmp_path / "wildcard.pfx"
    pfx.write_bytes(b"pfx-bytes")
    result = subprocess.run(
        [bash, str(KEY_VAULT)],
        cwd=REPO_ROOT,
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "TMPDIR": str(scratch),
            "KV_STATE": str(state_path),
            "KV_CALLS": str(calls_path),
            "KEY_VAULT_NAME": "kv-test",
            "WILDCARD_TLS_PFX_PATH": str(pfx),
            **_KV_INPUTS,
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    return result, calls, json.loads(state_path.read_text()), scratch


def _kv_state(**extra: object) -> dict:
    return {
        "secrets": {},
        "certificates": {},
        "pfx_password": _KV_INPUTS["WILDCARD_TLS_PFX_PASSWORD"],
        "faults": [],
        **extra,
    }


@SEED_BASHES
def test_key_vault_load_keeps_secrets_off_the_command_line(tmp_path: Path, bash: str) -> None:
    """Every secret and the bundle password reach the CLI through files; no
    value appears in any recorded argument vector, and the vault holds exactly
    the supplied values. The scratch files are removed when the run ends.
    """
    result, calls, final, scratch = _run_kv_load(tmp_path, _kv_state(), bash)
    assert result.returncode == 0, result.stderr
    argv = "\n".join(" ".join(call) for call in calls)
    for name, value in _KV_INPUTS.items():
        assert value not in argv, f"{name} appeared on a command line"
    assert final["secrets"]["anthropic-api-key"] == _KV_INPUTS["ANTHROPIC_API_KEY"]
    assert final["secrets"]["bff-client-secret"] == _KV_INPUTS["BFF_CLIENT_SECRET"]
    assert "wildcard-tls" in final["certificates"]
    generated = final["secrets"]["sage-ingress-key"]
    assert re.fullmatch(r"[0-9a-f]{64}", generated), generated
    assert generated not in argv, "the generated ingress key appeared on a command line"
    assert list(scratch.iterdir()) == [], "scratch files outlived the run"


@SEED_BASHES
def test_key_vault_load_keeps_an_existing_ingress_key(tmp_path: Path, bash: str) -> None:
    """A re-run leaves the ingress key in place, so the gateway and SAGE keep
    agreeing on it; rotating it is a deliberate step, not a side effect.
    """
    state = _kv_state(secrets={"sage-ingress-key": "existing"})
    result, calls, final, _ = _run_kv_load(tmp_path, state, bash)
    assert result.returncode == 0, result.stderr
    assert final["secrets"]["sage-ingress-key"] == "existing"
    assert not [c for c in calls if "sage-ingress-key" in c and "set" in c]


@SEED_BASHES
def test_key_vault_load_stops_when_the_ingress_key_lookup_fails(tmp_path: Path, bash: str) -> None:
    """A failed lookup stops the run; it never reads as "absent" and replaces
    a key the gateway and SAGE already share.
    """
    state = _kv_state(secrets={"sage-ingress-key": "existing"}, faults=[["secret", "list"]])
    result, _, final, scratch = _run_kv_load(tmp_path, state, bash)
    assert result.returncode != 0, "a failed lookup must stop the run"
    assert final["secrets"]["sage-ingress-key"] == "existing"
    assert list(scratch.iterdir()) == [], "scratch files outlived the failed run"
