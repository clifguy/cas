#!/usr/bin/env python3
"""Delete API Management logger-credential named values that no logger references.

API Management stores a logger credential given as a plain value in an
auto-generated secret named value (display name ``Logger-Credentials--<hex>``)
and keeps a ``{{name}}`` reference to it in the logger. Each write of such a
credential mints a new value and leaves the replaced one behind. This removes
only those leftovers: auto-generated, secret, not Key Vault-backed, and
referenced by no logger, by name or display name.

Preview is the default; ``--apply`` deletes. Any failed or malformed read, an
empty logger list, or a logger reference that resolves to no named value stops
the run before anything is deleted. Run it when no deployment is in flight.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from typing import Any

LOGGER_CREDENTIAL_PREFIX = "Logger-Credentials--"
API_VERSION = "2022-08-01"
_REFERENCE_RE = re.compile(r"\{\{([^{}]+)\}\}")


def az(*args: str) -> Any:
    """Run Azure CLI without a shell; failed reads never mean absence.

    A failure raises after echoing the CLI's own error, so the operator sees why.
    """
    executable = shutil.which("az")
    if executable is None:
        raise RuntimeError("Azure CLI is required")
    # Operator arguments are passed as argv to a fixed executable, without a shell.
    result = subprocess.run(  # noqa: S603
        [executable, *args, "--only-show-errors", "--output", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr
        )
    return json.loads(result.stdout) if result.stdout.strip() else None


def _named_values(resource_group: str, service: str) -> list[dict[str, Any]]:
    result = az("apim", "nv", "list", "--resource-group", resource_group, "--service-name", service)
    if not isinstance(result, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("name"), str)
        or not isinstance(item.get("displayName"), str)
        for item in result
    ):
        raise RuntimeError("APIM named-value list returned an invalid shape")
    return result


def _loggers(service_id: str) -> list[dict[str, Any]]:
    url: str | None = f"https://management.azure.com{service_id}/loggers?api-version={API_VERSION}"
    loggers: list[dict[str, Any]] = []
    while url:
        page = az("rest", "--method", "get", "--url", url)
        if not isinstance(page, dict) or not isinstance(page.get("value"), list):
            raise RuntimeError("APIM logger list returned an invalid shape")
        for logger in page["value"]:
            properties = logger.get("properties") if isinstance(logger, dict) else None
            if not isinstance(properties, dict) or not isinstance(logger.get("name"), str):
                raise RuntimeError("APIM logger list returned an invalid shape")
            loggers.append(logger)
        url = page.get("nextLink")
    if not loggers:
        raise RuntimeError("APIM returned no loggers; refusing to treat every credential as unused")
    return loggers


def _references(loggers: list[dict[str, Any]]) -> set[str]:
    """Every ``{{name}}`` a logger's credentials refer to."""
    found: set[str] = set()
    for logger in loggers:
        credentials = logger["properties"].get("credentials") or {}
        if not isinstance(credentials, dict):
            raise RuntimeError(f"logger {logger['name']} has credentials of an invalid shape")
        for value in credentials.values():
            if isinstance(value, str):
                found.update(_REFERENCE_RE.findall(value))
    return found


def _logger_credentials(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [v for v in values if v["displayName"].startswith(LOGGER_CREDENTIAL_PREFIX)]


def _unreferenced(values: list[dict[str, Any]], references: set[str]) -> list[str]:
    known = {v["name"] for v in values} | {v["displayName"] for v in values}
    unresolved = sorted(references - known)
    if unresolved:
        raise RuntimeError(f"logger references unresolved named values: {', '.join(unresolved)}")
    return sorted(
        v["name"]
        for v in _logger_credentials(values)
        if v.get("secret") is True
        and not v.get("keyVault")
        and v["name"] not in references
        and v["displayName"] not in references
    )


def cleanup(resource_group: str, service: str, apply: bool) -> list[str]:
    """Delete (or, without ``apply``, list) the unreferenced logger credentials."""
    service_id = az(
        "apim", "show", "--resource-group", resource_group, "--name", service, "--query", "id"
    )
    if not isinstance(service_id, str) or not service_id.startswith("/subscriptions/"):
        raise RuntimeError("APIM service id returned an invalid shape")
    values = _named_values(resource_group, service)
    references = _references(_loggers(service_id))
    candidates = _unreferenced(values, references)
    kept = sorted(v["name"] for v in _logger_credentials(values) if v["name"] not in candidates)
    print(f"Keeping {len(kept)} logger-credential named value(s) a logger references or that")
    print("are not auto-generated secrets:")
    for name in kept:
        print(f"  {name}")
    verb = "Deleting" if apply else "Would delete"
    print(f"{verb} {len(candidates)} unreferenced logger-credential named value(s)")
    for name in candidates:
        print(f"  {name}")
    if not apply or not candidates:
        return candidates
    for name in candidates:
        az(
            "apim", "nv", "delete", "--resource-group", resource_group,
            "--service-name", service, "--named-value-id", name, "--yes",
        )  # fmt: skip
    remaining = {v["name"] for v in _named_values(resource_group, service)}
    if survivors := sorted(remaining & set(candidates)):
        raise RuntimeError(f"named values still exist after delete: {', '.join(survivors)}")
    print("Deleted named values absent (readback verified)")
    return candidates


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--service-name", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    cleanup(args.resource_group, args.service_name, args.apply)


if __name__ == "__main__":
    main()
