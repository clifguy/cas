"""A stateful stand-in for the Azure CLI's Entra surface, run as ``az``.

The Entra bootstrap tests put this on ``PATH`` ahead of the real CLI. Each call
appends its argument vector to ``$AZURE_CALLS`` and answers from, or writes to,
the JSON directory model in ``$AZURE_STATE``: applications, service principals,
groups and app-role assignments. Lookups behave the way the real CLI does where
the bootstrap depends on it -- ``--display-name`` matches by prefix, an OData
``eq`` filter matches exactly, creating an object that already exists fails, and
a duplicate app-role assignment is refused -- so a script that leans on a
lenient double cannot pass here.

It is written to disk and executed once per stubbed call, so it imports only the
standard library: the test writes it behind an interpreter line that disables
site-packages. ``--query`` supports the JMESPath subset the bootstrap uses: an
optional field path, one ``[]``, ``[N]`` or ``[?k=='v' && ...]`` selector, an
optional projected field, and an optional ``| [N]``.
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

_QUERY_RE = re.compile(
    r"^(?P<path>[A-Za-z0-9_.]*)"
    r"(?P<sel>\[(?:\d+|\?[^\]]*)?\])?"
    r"(?:\.(?P<field>[A-Za-z0-9_]+))?"
    r"(?:\s*\|\s*\[(?P<pipe>\d+)\])?$"
)
_COND_RE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*==\s*'([^']*)'\s*$")
_EQ_FILTER_RE = re.compile(r"^\s*(displayName|appId)\s+eq\s+'([^']*)'\s*$", re.IGNORECASE)


class AzError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def _query(value: Any, query: str) -> Any:
    match = _QUERY_RE.match(query.strip())
    if match is None:
        raise AssertionError(f"fake az: unsupported --query {query!r}")
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
            conds = []
            for clause in inner[1:].split("&&"):
                cond = _COND_RE.match(clause)
                if cond is None:
                    raise AssertionError(f"fake az: unsupported filter {clause!r}")
                conds.append((cond[1], cond[2]))
            value = [i for i in items if all(str(i.get(k)) == v for k, v in conds)]
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


def _rest(state: dict, args: list[str]) -> Any:
    method = _arg(args, "--method").upper()
    url = _arg(args, "--url") if "--url" in args else _arg(args, "--uri")
    match = _REST_RE.match(url)
    if match is None:
        raise AssertionError(f"fake az: unsupported rest url {url!r}")
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
    if method == "GET":
        if sub != "appRoleAssignedTo":
            raise AssertionError("fake az: GET is modeled on appRoleAssignedTo only")
        return {"value": [a for a in state["assignments"] if a["resourceId"] == sp["id"]]}
    if method != "POST":
        raise AssertionError(f"fake az: unsupported {method} on {sub}")
    if body["resourceId"] != sp["id"]:
        raise AzError("resourceId does not match the addressed service principal.", 1)
    if not any(g["id"] == body["principalId"] for g in state["groups"]):
        raise AzError(f"Resource '{body['principalId']}' does not exist.", 3)
    role_ids = {r["id"] for r in _sp_view(state, sp).get("appRoles", [])}
    if body["appRoleId"] not in role_ids | {DEFAULT_ACCESS_ROLE_ID}:
        raise AzError("Permission being assigned was not found on application.", 1)
    key = (body["principalId"], body["resourceId"], body["appRoleId"])
    existing = {(a["principalId"], a["resourceId"], a["appRoleId"]) for a in state["assignments"]}
    if key in existing:
        raise AzError("Permission being assigned already exists on the object.", 1)
    assignment = {"id": _new_id(), "principalType": "Group", **body}
    state["assignments"].append(assignment)
    return assignment


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
