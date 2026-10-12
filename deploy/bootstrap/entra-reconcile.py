#!/usr/bin/env python3
"""Targeted Entra reconciliation for entra-app-registrations.sh.

Only the standard library is used. All I/O goes through the operator's Azure
CLI session (including guarded CLI wrappers); this does not acquire credentials.
Each phase has a 120-second deadline, including subprocess time and backoff.
"""

import json
import os
import re
import subprocess
import sys
import time
import uuid
from urllib.parse import urlencode, urlsplit

ROOT = "https://graph.microsoft.com/v1.0/"
DEADLINE_SECONDS = 120


class DirectoryError(Exception):
    """A sanitized, non-retryable directory/identity failure."""


class TransientError(DirectoryError):
    """A narrowly recognized retryable Graph/transport failure."""


def require(condition, message):
    if not condition:
        raise DirectoryError(message)


def rows(value):
    require(
        isinstance(value, list) and all(isinstance(x, dict) for x in value),
        "malformed directory collection",
    )
    return value


def identity(value):
    require(isinstance(value, str) and bool(value), "missing directory identity")
    # Object/app/scope/role IDs are UUIDs; delegated grant IDs are opaque and are
    # validated separately before use in a URL.
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise DirectoryError("malformed directory identity") from exc


def equivalent(actual, expected):
    """Graph may reorder collections or materialize null optional fields."""

    def normalized(value):
        if isinstance(value, dict):
            return {k: normalized(v) for k, v in value.items() if v is not None and k != "origin"}
        if isinstance(value, list):
            return sorted(
                (normalized(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True)
            )
        return value

    return normalized(actual) == normalized(expected)


class Phase:
    def __init__(self, label):
        self.label = label
        self.end = time.monotonic() + DEADLINE_SECONDS
        self.delay = 1
        self.retry_after = 0

    def remaining(self):
        left = self.end - time.monotonic()
        require(left > 0, f"{self.label}: readiness deadline exhausted; rerun to reconcile")
        return left

    def pause(self):
        time.sleep(min(max(self.delay, self.retry_after), self.remaining()))
        self.retry_after = 0
        self.delay = min(10, self.delay * 2)

    def request(self, method, path, body=None):
        url = path if path.startswith("https://") else ROOT + path
        parsed = urlsplit(url)
        require(
            parsed.scheme == "https"
            and parsed.netloc == "graph.microsoft.com"
            and parsed.path.startswith("/v1.0/")
            and not parsed.fragment,
            "refused unexpected Graph continuation URL",
        )
        args = ["az", "rest", "--method", method, "--url", url, "-o", "json"]
        if body is not None:
            args += ["--headers", "Content-Type=application/json", "--body", json.dumps(body)]
        try:
            result = subprocess.run(  # noqa: S603 -- fixed az executable, argument vector; no shell
                args, capture_output=True, text=True, timeout=min(30, self.remaining()), check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise TransientError(f"{self.label}: Azure CLI response timed out") from exc
        if result.returncode:
            error = result.stderr
            # Do not print tokens, response bodies, or arbitrary CLI diagnostics.
            transient = (
                "TooManyRequests",
                "ServiceUnavailable",
                "GatewayTimeout",
                "InternalServerError",
                "Request_ResourceNotFound",
            )
            code = next((c for c in transient if c in error), None)
            if code:
                retry = re.search(r"(?i)retry-after[\s:]+(\d+)", error)
                self.retry_after = int(retry[1]) if retry else 0
                raise TransientError(f"{self.label}: {code}")
            if (
                "InvalidValue" in error
                and "delegatedPermissionIds" in error
                and "AppPermissions" in error
            ):
                raise TransientError(f"{self.label}: persisted scope not yet usable")
            if "already exists" in error or "Conflict" in error:
                raise TransientError(f"{self.label}: conflicting write; reconcile by readback")
            code = re.search(
                r"\b(?:Authorization_RequestDenied|InvalidAuthenticationToken|InvalidValue|Request_BadRequest|Forbidden)\b",
                error,
            )
            raise DirectoryError(
                f"{self.label}: {method} refused ({code[0] if code else 'Azure CLI failed'})"
            )
        try:
            value = json.loads(result.stdout) if result.stdout.strip() else None
        except ValueError as exc:
            raise DirectoryError(f"{self.label}: malformed JSON response") from exc
        return value

    def read(self, path):
        while True:
            try:
                value = self.request("GET", path)
                require(isinstance(value, dict), f"{self.label}: malformed object response")
                return value
            except TransientError:
                self.pause()

    def wait(self, read, ready):
        while True:
            value = read()
            if ready(value):
                return value
            self.pause()

    def patch(self, path, body):
        while True:
            try:
                self.request("PATCH", path, body)
                return
            except TransientError:
                # PATCH only assigns previously calculated, stable desired values.
                self.pause()


def owned_entry(collection, value, supplied, defaults):
    matches = [x for x in collection if x.get("value") == value]
    require(len(matches) <= 1, f"ambiguous {value} identity")
    current = matches[0] if matches else {}
    selected = identity(current.get("id") or supplied or str(uuid.uuid4()))
    require(not supplied or identity(supplied) == selected, f"conflicting {value} ID override")
    require(
        not any(x.get("id") == selected and x.get("value") != value for x in collection),
        f"{value} ID belongs to another permission",
    )
    # Keep supported optional consent/role fields, but never echo origin (read-only).
    allowed = set(defaults) | {"userConsentDisplayName", "userConsentDescription"}
    desired = {k: v for k, v in current.items() if k in allowed}
    desired.update(defaults, id=selected, value=value, isEnabled=True)
    return selected, [desired if x is current else x for x in collection] + (
        [] if current else [desired]
    )


def resource(app_id, sp_id, cli_id):
    app_path, sp_path = f"applications/{identity(app_id)}", f"servicePrincipals/{identity(sp_id)}"
    phase = Phase("SAGE scope/role persistence")
    app = phase.read(app_path)
    api = app.get("api", {})
    require(isinstance(api, dict), "malformed application api")
    scope_id, scopes = owned_entry(
        rows(api.get("oauth2PermissionScopes", [])),
        "Sage.Access",
        os.environ.get("ACCESS_SCOPE_ID"),
        {
            "type": "Admin",
            "adminConsentDisplayName": "Access SAGE",
            "adminConsentDescription": "Access SAGE on behalf of the signed-in user.",
        },
    )
    role_id, roles = owned_entry(
        rows(app.get("appRoles", [])),
        "Sage.Reader",
        os.environ.get("SAGE_READER_ROLE_ID"),
        {
            "allowedMemberTypes": ["User", "Application"],
            "displayName": "Sage.Reader",
            "description": "Read across the SAGE REST and MCP surfaces.",
        },
    )
    # origin is read-only on returned appRole entries, including unrelated roles.
    roles = [{k: v for k, v in role.items() if k != "origin"} for role in roles]
    body = {
        "api": {"requestedAccessTokenVersion": 2, "oauth2PermissionScopes": scopes},
        "appRoles": roles,
    }

    def persisted(obj):
        require(isinstance(obj.get("api"), dict), "malformed application api")
        return (
            obj["api"].get("requestedAccessTokenVersion") == 2
            and equivalent(rows(obj["api"].get("oauth2PermissionScopes", [])), scopes)
            and equivalent(rows(obj.get("appRoles", [])), roles)
        )

    if not persisted(app):
        phase.patch(app_path, body)
    app = phase.wait(lambda: phase.read(app_path), persisted)

    phase = Phase("Azure CLI preauthorization")
    pre = rows(app.get("api", {}).get("preAuthorizedApplications", []))
    cli_id = identity(cli_id)
    matches = [p for p in pre if p.get("appId") == cli_id]
    require(len(matches) <= 1, "ambiguous Azure CLI preauthorization")
    existing = matches[0] if matches else {"appId": cli_id, "delegatedPermissionIds": []}
    ids = existing.get("delegatedPermissionIds")
    require(
        isinstance(ids, list) and all(isinstance(i, str) for i in ids),
        "malformed preauthorization scopes",
    )
    desired = {"appId": cli_id, "delegatedPermissionIds": list(dict.fromkeys(ids + [scope_id]))}
    merged = [desired if p is existing else p for p in pre] + ([] if matches else [desired])
    if not equivalent(merged, pre):
        # Only this collection is patched; the first phase's API settings remain.
        phase.patch(app_path, {"api": {"preAuthorizedApplications": merged}})
    phase.wait(
        lambda: phase.read(app_path),
        lambda a: (
            persisted(a) and equivalent(a.get("api", {}).get("preAuthorizedApplications"), merged)
        ),
    )
    phase = Phase("SAGE service-principal permission visibility")

    def visible(sp):
        return any(
            s.get("id") == scope_id
            and s.get("value") == "Sage.Access"
            and s.get("isEnabled") is True
            for s in rows(sp.get("oauth2PermissionScopes", []))
        ) and any(
            r.get("id") == role_id
            and r.get("value") == "Sage.Reader"
            and r.get("isEnabled") is True
            and set(r.get("allowedMemberTypes", [])) >= {"User", "Application"}
            for r in rows(sp.get("appRoles", []))
        )

    phase.wait(lambda: phase.read(sp_path), visible)
    print(scope_id, role_id)


def declarations(current, resource_id, scope_id):
    merged = {}
    for entry in rows(current):
        key = identity(entry.get("resourceAppId"))
        target = merged.setdefault(key, [])
        for permission in rows(entry.get("resourceAccess")):
            item = {"id": identity(permission.get("id")), "type": permission.get("type")}
            require(item["type"] in ("Scope", "Role"), "malformed permission type")
            if item not in target:
                target.append(item)
    item = {"id": identity(scope_id), "type": "Scope"}
    target = merged.setdefault(identity(resource_id), [])
    if item not in target:
        target.append(item)
    return [{"resourceAppId": key, "resourceAccess": value} for key, value in merged.items()]


def grant_rows(phase, client):
    path = "oauth2PermissionGrants?" + urlencode({"$filter": f"clientId eq '{client}'"})
    seen, result = set(), []
    while path:
        require(path not in seen, "cyclic grant pagination")
        seen.add(path)
        page = phase.read(path)
        result.extend(rows(page.get("value")))
        path = page.get("@odata.nextLink")
        require(
            path is None
            or isinstance(path, str)
            and path.startswith(ROOT + "oauth2PermissionGrants?"),
            "refused unexpected grant continuation URL",
        )
    return result


def consent(client, resource_id, scope):
    phase = Phase(f"delegated consent {client} / {scope}")

    def find():
        matches = [
            g
            for g in grant_rows(phase, client)
            if g.get("clientId") == client
            and g.get("resourceId") == resource_id
            and g.get("consentType") == "AllPrincipals"
            and g.get("principalId") is None
        ]
        require(len(matches) <= 1, "ambiguous AllPrincipals grant; resolve duplicate records")
        return matches[0] if matches else None

    grant = find()
    if grant is None:
        body = {
            "clientId": client,
            "resourceId": resource_id,
            "consentType": "AllPrincipals",
            "principalId": None,
            "scope": scope,
        }
        try:
            phase.request("POST", "oauth2PermissionGrants", body)
        except TransientError:
            pass
        # An uncertain or successful create is never replayed. Wait for the
        # exact relationship; lack of readback fails the phase, allowing rerun.
        grant = phase.wait(find, lambda g: g is not None)
    require(isinstance(grant.get("scope"), str), "malformed grant scopes")
    merged = " ".join(dict.fromkeys(grant["scope"].split() + [scope]))
    grant_id = grant.get("id")
    require(
        isinstance(grant_id, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]+", grant_id)),
        "malformed grant ID",
    )
    if merged != grant["scope"]:
        phase.patch(f"oauth2PermissionGrants/{grant_id}", {"scope": merged})
    phase.wait(
        find, lambda g: g is not None and g.get("id") == grant_id and g.get("scope") == merged
    )


def permission(app_id, client, resource_app, resource_sp, scope_id, scope):
    app_path = f"applications/{identity(app_id)}"
    client, resource_sp = identity(client), identity(resource_sp)
    phase = Phase(f"permission declaration {app_id} / {scope}")
    app = phase.read(app_path)
    desired = declarations(app.get("requiredResourceAccess", []), resource_app, scope_id)
    if not equivalent(desired, app.get("requiredResourceAccess", [])):
        phase.patch(app_path, {"requiredResourceAccess": desired})
    phase.wait(
        lambda: phase.read(app_path), lambda a: equivalent(a.get("requiredResourceAccess"), desired)
    )
    consent(client, resource_sp, scope)


def main():
    try:
        command, *args = sys.argv[1:]
        if command == "resource" and len(args) == 3:
            resource(*args)
        elif command == "permission" and len(args) == 6:
            permission(*args)
        else:
            raise DirectoryError("invalid reconciliation invocation")
    except DirectoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
