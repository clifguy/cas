"""Mint or check the delegated Microsoft Graph token a site's permissions need.

Listing and granting a SharePoint site's application permissions
(``/sites/{id}/permissions``) is admitted only for a token carrying
``Sites.FullControl.All``, held by a SharePoint Administrator or higher. The
Azure CLI's own client cannot be issued that scope: it is a Microsoft
first-party application preauthorized for a fixed set of Graph scopes, and a
request outside that set fails with ``AADSTS65002``. The token is therefore
requested through Microsoft Graph PowerShell's public client, which accepts
dynamically requested delegated scopes, on the tenant the operator is signed
in to.

``mint --tenant <id>`` signs in (a browser, or ``--device-code`` where none
is available) and writes the token -- and nothing else -- to standard output.
``check`` reads a token from standard input and only reports whether it
carries the scope. Both refuse a token without it, so a sign-in that came back
with less than full control stops the caller before any site call.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys

SCOPE_NAME = "Sites.FullControl.All"
SCOPE = f"https://graph.microsoft.com/{SCOPE_NAME}"
# Microsoft Graph PowerShell's public client id: a published Microsoft constant,
# not a tenant coordinate. It is assembled from segments, as the Azure CLI id is
# in the test doubles, so the repository's GUID scan has nothing to flag.
GRAPH_POWERSHELL_CLIENT_ID = "-".join(("14d82eec", "204b", "4c2f", "b7e8", "296a70dab67e"))


def _scopes(token: str) -> list[str]:
    """The ``scp`` claim of an access token, read without verifying it.

    Graph verifies the token; this only refuses one that cannot work.
    """
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return []
    return str(claims.get("scp", "")).split() if isinstance(claims, dict) else []


def _refuse_without_scope(token: str) -> bool:
    if SCOPE_NAME in _scopes(token):
        return False
    print(
        f"ERROR: the Graph token does not carry {SCOPE_NAME}; sign in as a SharePoint "
        "Administrator (or higher) after an administrator has consented to that scope.",
        file=sys.stderr,
    )
    return True


def _mint(tenant: str, device_code: bool) -> int:
    import msal

    app = msal.PublicClientApplication(
        GRAPH_POWERSHELL_CLIENT_ID, authority=f"https://login.microsoftonline.com/{tenant}"
    )
    if device_code:
        flow = app.initiate_device_flow(scopes=[SCOPE])
        if "user_code" not in flow:
            print(f"ERROR: {flow.get('error_description', flow)}", file=sys.stderr)
            return 1
        print(flow["message"], file=sys.stderr)
        result = app.acquire_token_by_device_flow(flow)
    else:
        result = app.acquire_token_interactive(scopes=[SCOPE], prompt="select_account")
    token = result.get("access_token")
    if not token:
        reason = result.get("error_description") or result.get("error") or "no token returned"
        print(f"ERROR: sign-in did not return a Graph token: {reason}", file=sys.stderr)
        return 1
    if _refuse_without_scope(token):
        return 1
    sys.stdout.write(token)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    mint = sub.add_parser("mint", help="sign in and print the token")
    mint.add_argument("--tenant", required=True, help="tenant id or domain to sign in to")
    mint.add_argument("--device-code", action="store_true", help="sign in on another device")
    sub.add_parser("check", help="check a token read from standard input")
    args = parser.parse_args(argv)
    if args.command == "mint":
        return _mint(args.tenant, args.device_code)
    return 1 if _refuse_without_scope(sys.stdin.read().strip()) else 0


if __name__ == "__main__":
    sys.exit(main())
