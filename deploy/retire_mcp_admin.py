#!/usr/bin/env python3
"""Remove only the retired MCP Admin edge operation or directory identity.

Preview is the default. Apply only after deploying the retirement release;
APIM and Entra modes intentionally use separate operator permissions.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from typing import Any

ADMIN_OPERATION = "oauth-protected-resource-mcp-admin"


def az(*args: str) -> Any:
    """Run Azure CLI without a shell; failed reads never mean absence."""
    executable = shutil.which("az")
    if executable is None:
        raise RuntimeError("Azure CLI is required")
    # Operator arguments are passed as argv to a fixed executable, without a shell.
    result = subprocess.run(  # noqa: S603
        [executable, *args, "--only-show-errors", "--output", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


def retire_apim(resource_group: str, service: str, api: str, apply: bool) -> None:
    scope = ("--resource-group", resource_group, "--service-name", service, "--api-id", api)

    def operations() -> list[dict[str, Any]]:
        result = az("apim", "api", "operation", "list", *scope)
        if not isinstance(result, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in result
        ):
            raise RuntimeError("APIM operation list returned an invalid shape")
        return result

    before = operations()
    if not any(item["name"] == ADMIN_OPERATION for item in before):
        print("APIM retired operation already absent (read verified)")
        return
    print(f"{'Removing' if apply else 'Would remove'} APIM operation {ADMIN_OPERATION}")
    if not apply:
        return
    az("apim", "api", "operation", "delete", *scope, "--operation-id", ADMIN_OPERATION)
    if any(item["name"] == ADMIN_OPERATION for item in operations()):
        raise RuntimeError("retired APIM operation still exists after delete")
    print("APIM retired operation absent (readback verified)")


def retire_entra(app_id: str, hostname: str, apply: bool) -> None:
    if not hostname or any(char in hostname for char in "/:?#"):
        raise ValueError("hostname must be a DNS hostname, without scheme or path")
    retired = f"https://{hostname}/mcp_admin"

    def identifiers() -> list[str]:
        result = az("ad", "app", "show", "--id", app_id, "--query", "identifierUris")
        if not isinstance(result, list) or any(not isinstance(uri, str) for uri in result):
            raise RuntimeError("Entra identifierUris returned an invalid shape")
        return result

    before = identifiers()
    if retired not in before:
        print("Entra retired identifier already absent (read verified)")
        return
    after = [uri for uri in before if uri != retired]
    if not after:
        raise RuntimeError("refusing to remove the app's only identifier URI")
    print(
        f"{'Removing' if apply else 'Would remove'} Entra identifier {retired}; "
        f"preserving {len(after)} others"
    )
    if not apply:
        return
    az("ad", "app", "update", "--id", app_id, "--identifier-uris", *after)
    if sorted(identifiers()) != sorted(after):
        raise RuntimeError("Entra identifierUris readback differs from the intended set")
    print("Entra retired identifier absent; other identifiers preserved (readback verified)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    apim = commands.add_parser("apim")
    apim.add_argument("--resource-group", required=True)
    apim.add_argument("--service-name", required=True)
    apim.add_argument("--api-id", required=True)
    apim.add_argument("--apply", action="store_true")
    entra = commands.add_parser("entra")
    entra.add_argument("--app-id", required=True)
    entra.add_argument("--hostname", required=True)
    entra.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.mode == "apim":
        retire_apim(args.resource_group, args.service_name, args.api_id, args.apply)
    else:
        retire_entra(args.app_id, args.hostname, args.apply)


if __name__ == "__main__":
    main()
