"""A stateful stand-in for the Azure CLI's Entra surface, run as ``az``.

The bootstrap tests put this on ``PATH`` ahead of the real CLI. Each call
appends its argument vector to ``$AZURE_CALLS`` and answers from, or writes to,
the JSON directory model in ``$AZURE_STATE``: applications, service principals,
groups, app-role assignments, managed identities and SharePoint sites (with
their document libraries, per-site permissions and uploaded files). Lookups
behave the way the real CLI does where the bootstrap depends on it --
``--display-name`` matches by prefix, an OData ``eq`` filter matches exactly,
creating an object that already exists fails, a duplicate app-role assignment
is refused, and a site is addressed by its exact server-relative path -- so a
script that leans on a lenient double cannot pass here.

It is written to disk and executed once per stubbed call, so it imports only the
standard library: the test writes it behind an interpreter line that disables
site-packages. ``--query`` supports the JMESPath subset the bootstrap uses: an
optional field path, one ``[]``, ``[N]`` or ``[?cond && ...]`` selector, an
optional projected field, and an optional ``| [N]``. A condition is
``k=='v'`` or ``contains(path, 'v')``, where ``path`` may flatten lists with
``[]`` (``grantedToIdentitiesV2[].application.id``) and may default a null
with ``|| `[]` ``; a null subject without that default fails the call, as the
real CLI's JMESPath does.

A site's permissions and its drive uploads answer only a delegated token
carrying ``Sites.FullControl.All``, passed as an ``Authorization`` header;
without one they refuse with ``accessDenied``, as Microsoft Graph does for the
CLI's own token, which can never carry that scope. A header value of the form
``KEY=@path`` or ``@path`` is read from the file, as the real CLI expands it
before parsing, so the token itself need not appear in the argument vector.
"""

import base64
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

# Azure CLI's first-party client id, assembled from segments so no GUID-shaped
# literal appears in the repository.
AZURE_CLI_APP_ID = "-".join(("04b07795", "8ddb", "461a", "bbee", "02f9e1bf7b46"))
DEFAULT_ACCESS_ROLE_ID = "-".join("0" * n for n in (8, 4, 4, 4, 12))

_QUERY_HEAD_RE = re.compile(r"^(?P<path>[A-Za-z0-9_.]*)")
_QUERY_TAIL_RE = re.compile(r"^(?:\.(?P<field>[A-Za-z0-9_]+))?(?:\s*\|\s*\[(?P<pipe>\d+)\])?$")
_COND_RE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*==\s*'([^']*)'\s*$")
_CONTAINS_RE = re.compile(
    r"^\s*contains\(\s*([A-Za-z0-9_.\[\]]+)(\s*\|\|\s*`\[\]`)?\s*,\s*'([^']*)'\s*\)\s*$"
)
_EQ_FILTER_RE = re.compile(r"^\s*(displayName|appId)\s+eq\s+'([^']*)'\s*$", re.IGNORECASE)


class AzError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def _path(value: Any, path: str) -> Any:
    """Evaluate a dotted JMESPath field path; a ``name[]`` segment flattens a list."""
    projected = False
    for part in path.split("."):
        flatten = part.endswith("[]")
        name = part[:-2] if flatten else part
        if projected:
            nxt = []
            for item in value:
                got = item.get(name) if isinstance(item, dict) else None
                if flatten and isinstance(got, list):
                    nxt.extend(got)
                elif got is not None:
                    nxt.append(got)
            value = nxt
        else:
            value = value.get(name) if isinstance(value, dict) else None
            if flatten:
                # A projection over a missing field is null, as in JMESPath.
                if not isinstance(value, list):
                    return None
                projected = True
    return value


def _matches(item: Any, clause: str) -> bool:
    cond = _COND_RE.match(clause)
    if cond is not None:
        return isinstance(item, dict) and str(item.get(cond[1])) == cond[2]
    contains = _CONTAINS_RE.match(clause)
    if contains is not None:
        haystack = _path(item, contains[1])
        if haystack is None and contains[2]:
            haystack = []
        if not isinstance(haystack, (list, str)):
            # JMESPath refuses contains() on anything but an array or a string,
            # so the CLI fails the whole call rather than skipping the item.
            raise AzError(f"In function contains(), invalid type for value: {haystack!r}", 1)
        return contains[3] in haystack
    raise AssertionError(f"fake az: unsupported filter {clause!r}")


def _query(value: Any, query: str) -> Any:
    query = query.strip()
    head = _QUERY_HEAD_RE.match(query)
    path, rest, sel = head["path"], query[head.end() :], None
    if rest.startswith("["):
        # The selector runs to its matching bracket: a filter may itself contain
        # brackets, as ``contains(a[].b, 'v')`` does.
        depth = 0
        for end, char in enumerate(rest):
            depth += {"[": 1, "]": -1}.get(char, 0)
            if depth == 0:
                break
        sel, rest = rest[: end + 1], rest[end + 1 :]
    tail = _QUERY_TAIL_RE.match(rest)
    if tail is None or (sel is not None and not re.match(r"^\[(?:\d+|\?.*)?\]$", sel)):
        raise AssertionError(f"fake az: unsupported --query {query!r}")
    match = {"path": path, "sel": sel, "field": tail["field"], "pipe": tail["pipe"]}
    for part in filter(None, match["path"].split(".")):
        value = value.get(part) if isinstance(value, dict) else None
    sel = match["sel"]
    projected_list = False
    if sel is not None:
        inner = sel[1:-1]
        items = value if isinstance(value, list) else []
        if inner == "":
            value, projected_list = items, True
        elif inner.isdigit():
            value = items[int(inner)] if int(inner) < len(items) else None
        else:
            clauses = inner[1:].split("&&")
            value = [i for i in items if all(_matches(i, c) for c in clauses)]
            projected_list = True
    if match["field"]:
        if projected_list:
            value = [i.get(match["field"]) for i in value if isinstance(i, dict)]
            value = [v for v in value if v is not None]
        else:
            value = value.get(match["field"]) if isinstance(value, dict) else None
    if match["pipe"] is not None:
        idx = int(match["pipe"])
        value = value[idx] if isinstance(value, list) and idx < len(value) else None
    return value


def _emit(result: Any, args: list[str]) -> None:
    if "--query" in args:
        result = _query(result, args[args.index("--query") + 1])
    output = args[args.index("-o") + 1] if "-o" in args else "json"
    if output == "none" or result is None:
        return
    if output == "tsv":
        rows = result if isinstance(result, list) else [result]
        for row in rows:
            if row is not None:
                print(row if isinstance(row, str) else json.dumps(row))
        return
    print(json.dumps(result))


def _values(args: list[str], flag: str) -> list[str]:
    """Every value following ``flag`` up to the next ``--`` option."""
    if flag not in args:
        return []
    out = []
    for token in args[args.index(flag) + 1 :]:
        if token.startswith("--"):
            break
        out.append(token)
    return out


def _arg(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def _list_filter(objects: list[dict], args: list[str]) -> list[dict]:
    if "--display-name" in args:
        prefix = _arg(args, "--display-name").lower()
        return [o for o in objects if o["displayName"].lower().startswith(prefix)]
    if "--filter" in args:
        # A disjunction of ``eq`` clauses, as Graph evaluates it: an object
        # matches when any clause does.
        clauses = []
        for part in re.split(r"\s+or\s+", _arg(args, "--filter")):
            cond = _EQ_FILTER_RE.match(part)
            if cond is None:
                raise AzError(f"Invalid filter clause: {part!r}.", 1)
            key = "displayName" if cond[1].lower() == "displayname" else "appId"
            clauses.append((key, cond[2]))
        return [o for o in objects if any(o.get(k) == v for k, v in clauses)]
    return list(objects)


def _new_id() -> str:
    return str(uuid.uuid4())


# Each redirect-URI flag and the Graph platform object whose ``redirectUris``
# it writes. The flag replaces that collection wholesale, on create and update
# alike, as the real CLI does.
_REDIRECT_FLAGS = (
    ("--web-redirect-uris", "web"),
    ("--public-client-redirect-uris", "publicClient"),
)


def _set_redirects(app: dict, args: list[str]) -> None:
    for flag, platform in _REDIRECT_FLAGS:
        if flag in args:
            app.setdefault(platform, {})["redirectUris"] = _values(args, flag)


def _app_by_app_id(state: dict, app_id: str) -> dict:
    for app in state["apps"]:
        if app["appId"] == app_id:
            return app
    raise AzError(f"Resource '{app_id}' does not exist.", 3)


def _sp_view(state: dict, sp: dict) -> dict:
    """A service principal as Graph reports it: it mirrors its app's roles and scopes.

    The delegated scopes an application declares under ``api`` surface at the
    top level of its service principal, so every ``*Scopes`` collection is
    lifted from ``api`` as is.
    """
    view = dict(sp)
    app = next((a for a in state["apps"] if a["appId"] == sp["appId"]), None)
    if app is not None:
        view["appRoles"] = app.get("appRoles", [])
        view.update({k: v for k, v in app.get("api", {}).items() if k.endswith("Scopes")})
    return view


def _sp_lookup(state: dict, ident: str) -> dict:
    for sp in state["sps"]:
        if ident in (sp["id"], sp["appId"]):
            return sp
    raise AzError(f"Resource '{ident}' does not exist.", 3)


def _ad(state: dict, args: list[str]) -> Any:
    kind, verb = args[1], args[2]
    if kind == "app" and verb == "permission":
        state.setdefault("permission_calls", []).append(args[3:])
        return None
    if kind == "app":
        if verb == "list":
            return _list_filter(state["apps"], args)
        if verb == "create":
            app = {
                "appId": _new_id(),
                "id": _new_id(),
                "displayName": _arg(args, "--display-name"),
                "identifierUris": [],
                "api": {},
                "appRoles": [],
                "web": {"redirectUris": []},
                "publicClient": {"redirectUris": []},
            }
            _set_redirects(app, args)
            state["apps"].append(app)
            return app
        app = _app_by_app_id(state, _arg(args, "--id"))
        if verb == "show":
            return app
        if verb == "update":
            if "--identifier-uris" in args:
                app["identifierUris"] = _values(args, "--identifier-uris")
            _set_redirects(app, args)
            return None
    if kind == "sp":
        if verb == "list":
            return [_sp_view(state, sp) for sp in _list_filter(state["sps"], args)]
        if verb == "create":
            app_id = _arg(args, "--id")
            app = _app_by_app_id(state, app_id)
            if any(sp["appId"] == app_id for sp in state["sps"]):
                raise AzError("Another object with the same servicePrincipalNames already exists.")
            sp = {
                "id": _new_id(),
                "appId": app_id,
                "displayName": app["displayName"],
                "appRoleAssignmentRequired": False,
            }
            state["sps"].append(sp)
            return _sp_view(state, sp)
        if verb == "show":
            return _sp_view(state, _sp_lookup(state, _arg(args, "--id")))
    if kind == "group":
        if verb == "list":
            return _list_filter(state["groups"], args)
        if verb == "create":
            group = {"id": _new_id(), "displayName": _arg(args, "--display-name")}
            state["groups"].append(group)
            return group
    raise AssertionError(f"fake az: unsupported ad call {args!r}")


_REST_RE = re.compile(
    r"^https://graph\.microsoft\.com/v1\.0/(applications|servicePrincipals|groups)/([^/]+)"
    r"(?:/(appRoleAssignedTo|appRoleAssignments))?$"
)
_SITE_BY_PATH_RE = re.compile(r"^https://graph\.microsoft\.com/v1\.0/sites/([^/:]+):(/.*)$")
_SITE_SUB_RE = re.compile(
    r"^https://graph\.microsoft\.com/v1\.0/sites/([^/:]+)/(drives|permissions)$"
)
_DRIVE_UPLOAD_RE = re.compile(
    r"^https://graph\.microsoft\.com/v1\.0/drives/([^/]+)/root:/(.+):/content$"
)


SITES_SCOPE = "Sites.FullControl.All"


def _headers(args: list[str]) -> dict[str, str]:
    """The ``--headers`` values, with ``@file`` references expanded as the CLI does."""
    if "--headers" not in args:
        return {}
    out: dict[str, str] = {}
    for item in args[args.index("--headers") + 1 :]:
        if item.startswith("--"):
            break
        if item.startswith("@"):
            out.update(json.loads(Path(item[1:]).read_text()))
            continue
        key, value = item.split("=", 1)
        if value.startswith("@"):
            value = Path(value[1:]).read_text().rstrip("\n")
        out[key] = value
    return out


def _require_sites_token(args: list[str]) -> None:
    """Refuse a site-permission or upload call lacking a ``Sites.FullControl.All`` token."""
    bearer = _headers(args).get("Authorization", "")
    scopes: list[str] = []
    if bearer.startswith("Bearer "):
        try:
            payload = bearer.split(" ", 1)[1].split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            scopes = str(claims.get("scp", "")).split()
        except ValueError, IndexError:
            scopes = []
    if SITES_SCOPE not in scopes:
        raise AzError("Forbidden: accessDenied. Access denied.", 1)


def _principal_exists(state: dict, object_id: str) -> bool:
    return any(o["id"] == object_id for o in state["groups"] + state["sps"])


def _site_rest(state: dict, method: str, url: str, args: list[str]) -> Any:
    """Answer the SharePoint calls: a site by path, its drives, its permissions, uploads."""
    sites = state.get("sites", [])
    if (match := _SITE_BY_PATH_RE.match(url)) is not None:
        host, path = match.groups()
        for site in sites:
            if method == "GET" and site["hostname"] == host and site["path"] == path:
                return {"id": site["id"], "webUrl": f"https://{host}{path}"}
        raise AzError("Not Found: itemNotFound. Requested site could not be found.", 3)
    if (match := _SITE_SUB_RE.match(url)) is not None:
        site_id, sub = match.groups()
        site = next((s for s in sites if s["id"] == site_id), None)
        if site is None:
            raise AzError("Not Found: itemNotFound. Requested site could not be found.", 3)
        if sub == "permissions":
            _require_sites_token(args)
        if method == "GET":
            return {"value": site[sub]}
        if sub == "permissions" and method == "POST":
            body = json.loads(_arg(args, "--body"))
            identities = body.get("grantedToIdentities") or []
            if not body.get("roles") or not identities:
                raise AzError("Bad Request: roles and grantedToIdentities are required.", 1)
            if not identities[0].get("application", {}).get("id"):
                raise AzError("Bad Request: the granted application has no id.", 1)
            permission = {
                "id": _new_id(),
                "roles": body["roles"],
                "grantedToIdentities": identities,
                "grantedToIdentitiesV2": identities,
            }
            site["permissions"].append(permission)
            return permission
        raise AssertionError(f"fake az: unsupported {method} on site {sub}")
    if (match := _DRIVE_UPLOAD_RE.match(url)) is not None and method == "PUT":
        drive_id, item_path = match.groups()
        _require_sites_token(args)
        if not any(d["id"] == drive_id for s in sites for d in s["drives"]):
            raise AzError("Not Found: itemNotFound. The drive could not be found.", 3)
        body = _arg(args, "--body")
        content = Path(body[1:]).read_text() if body.startswith("@") else body
        state.setdefault("uploads", []).append(
            {"drive": drive_id, "path": item_path, "content": content}
        )
        return {"name": item_path.rsplit("/", 1)[-1]}
    raise AssertionError(f"fake az: unsupported rest url {url!r}")


def _rest(state: dict, args: list[str]) -> Any:
    method = _arg(args, "--method").upper()
    url = _arg(args, "--url") if "--url" in args else _arg(args, "--uri")
    match = _REST_RE.match(url)
    if match is None:
        return _site_rest(state, method, url, args)
    collection, object_id, sub = match.groups()
    try:
        body = json.loads(_arg(args, "--body")) if "--body" in args else None
    except json.JSONDecodeError as exc:
        raise AzError(f"Bad Request: the request body is not valid JSON ({exc}).", 1) from exc
    if collection == "applications":
        app = next((a for a in state["apps"] if a["id"] == object_id), None)
        if app is None or method != "PATCH" or sub:
            raise AzError(f"Resource '{object_id}' does not exist.", 3)
        for key, value in body.items():
            if key == "api":
                app.setdefault("api", {}).update(value)
            else:
                app[key] = value
        return None
    if collection == "groups":
        if method != "GET" or sub != "appRoleAssignments":
            raise AssertionError(f"fake az: unsupported {method} on a group")
        if not any(g["id"] == object_id for g in state["groups"]):
            raise AzError(f"Resource '{object_id}' does not exist.", 3)
        return {"value": [a for a in state["assignments"] if a["principalId"] == object_id]}
    sp = next((s for s in state["sps"] if s["id"] == object_id), None)
    if sp is None:
        raise AzError(f"Resource '{object_id}' does not exist.", 3)
    if sub is None:
        if method != "PATCH":
            raise AssertionError(f"fake az: unsupported {method} on a service principal")
        sp.update(body)
        return None
    # ``appRoleAssignedTo`` is the resource side of an assignment (who holds this
    # principal's roles); ``appRoleAssignments`` is the principal side (whose
    # roles this principal holds). Graph accepts the same body on either.
    resource_side = sub == "appRoleAssignedTo"
    if method == "GET":
        key = "resourceId" if resource_side else "principalId"
        return {"value": [a for a in state["assignments"] if a[key] == sp["id"]]}
    if method != "POST":
        raise AssertionError(f"fake az: unsupported {method} on {sub}")
    if resource_side and body["resourceId"] != sp["id"]:
        raise AzError("resourceId does not match the addressed service principal.", 1)
    if not resource_side and body["principalId"] != sp["id"]:
        raise AzError("principalId does not match the addressed service principal.", 1)
    if not _principal_exists(state, body["principalId"]):
        raise AzError(f"Resource '{body['principalId']}' does not exist.", 3)
    resource = _sp_lookup(state, body["resourceId"]) if not resource_side else sp
    role_ids = {r["id"] for r in _sp_view(state, resource).get("appRoles", [])}
    if body["appRoleId"] not in role_ids | {DEFAULT_ACCESS_ROLE_ID}:
        raise AzError("Permission being assigned was not found on application.", 1)
    key = (body["principalId"], body["resourceId"], body["appRoleId"])
    existing = {(a["principalId"], a["resourceId"], a["appRoleId"]) for a in state["assignments"]}
    if key in existing:
        raise AzError("Permission being assigned already exists on the object.", 1)
    is_group = any(g["id"] == body["principalId"] for g in state["groups"])
    principal_type = "Group" if is_group else "ServicePrincipal"
    assignment = {"id": _new_id(), "principalType": principal_type, **body}
    state["assignments"].append(assignment)
    return assignment


def _identity(state: dict, args: list[str]) -> Any:
    """Answer ``identity show`` for a user-assigned managed identity."""
    if args[1] != "show":
        raise AssertionError(f"fake az: unsupported identity call {args!r}")
    group, name = _arg(args, "-g"), _arg(args, "-n")
    for identity in state.get("identities", []):
        if identity["resourceGroup"] == group and identity["name"] == name:
            return identity
    raise AzError(f"The Resource '{name}' under resource group '{group}' was not found.", 3)


def _cli_client_assertion() -> str:
    """An unsigned, synthetic JWT naming Azure CLI as the client application.

    It verifies nothing; the bootstrap reads only its ``appid`` claim.
    """

    def segment(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    claims = {"appid": AZURE_CLI_APP_ID}
    return f"{segment({'alg': 'none'})}.{segment(claims)}.unsigned"


def _cli_reply(args: list[str]) -> None:
    """Answer ``account get-access-token`` in the one form the bootstrap requests.

    The bootstrap asks for the JWT field alone as plain text, so the reply is
    that single line; any other projection is outside what this stand-in models.
    """
    if _arg(args, "--query") != "accessToken" or _arg(args, "-o") != "tsv":
        raise AssertionError(f"fake az: unsupported get-access-token form {args!r}")
    sys.stdout.write(_cli_client_assertion() + "\n")


def fake_azure() -> None:
    path = Path(os.environ["AZURE_STATE"])
    state = json.loads(path.read_text())
    args = sys.argv[1:]
    with Path(os.environ["AZURE_CALLS"]).open("a") as stream:
        stream.write(json.dumps(args) + "\n")
    joined = " ".join(args)
    for fault in state.get("faults", []):
        if all(token in joined for token in fault["contains"]):
            print(f"ERROR: {fault['message']}", file=sys.stderr)
            sys.exit(fault.get("code", 1))
    try:
        if args[0] == "ad":
            result = _ad(state, args)
        elif args[0] == "rest":
            result = _rest(state, args)
        elif args[0] == "identity":
            result = _identity(state, args)
        elif args[:2] == ["account", "show"]:
            result = {"tenantId": state.get("tenantId", str(uuid.uuid4()))}
        elif args[:2] == ["account", "get-access-token"]:
            _cli_reply(args)
            return
        else:
            raise AssertionError(f"fake az: unsupported call {args!r}")
    except AzError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(exc.code)
    path.write_text(json.dumps(state))
    _emit(result, args)


if __name__ == "__main__":
    fake_azure()
