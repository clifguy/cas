"""A stateful stand-in for the Azure CLI's role-based access control surface.

The deploy-identity role tests put this on ``PATH`` as ``az``. Each call appends
its argument vector to ``$AZURE_CALLS`` and answers from, or writes to, the JSON
model in ``$AZURE_STATE``: built-in role definitions by name, custom role
definitions, role assignments, resource groups, registered resource providers
and the deploy application's federated credentials. Built-in role ids are
derived from the role name, so this stand-in carries no GUID-shaped literal and
the test can still recompute them.

``--query`` is answered for exactly the expressions the script sends, each
rendered the way the real CLI renders it with ``-o tsv``; an unrecognised query
fails the call rather than being answered loosely.

It imports only the standard library: the test runs it behind an interpreter
line that disables site-packages.
"""

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

NAMESPACE = uuid.UUID(int=0)

_CREDENTIAL_ROWS = (
    "[].join('|', [subject || '', issuer || '', claimsMatchingExpression.value || ''])"
)
_ASSIGNMENT_ROWS = "[].join('|', [id, roleDefinitionName || '', scope, condition || ''])"
_ROLE_SHAPE = (
    "[0].join('|', [to_string(length(permissions)), "
    "join(',', sort(permissions[0].actions)), join(',', permissions[0].notActions), "
    "join(',', permissions[0].dataActions), join(',', permissions[0].notDataActions), "
    "join(',', assignableScopes)])"
)


def builtin_role_id(name: str) -> str:
    """The id the fake reports for a built-in role, stable per name."""
    return str(uuid.uuid5(NAMESPACE, "builtin:" + name))


def _opt(args: list[str], flag: str) -> str | None:
    if flag in args:
        index = args.index(flag)
        if index + 1 < len(args):
            return args[index + 1]
    return None


def _lines(values: list[str]) -> None:
    for value in values:
        print(value)


def _role_matches(assignment: dict[str, Any], role: str) -> bool:
    return role in (assignment["role"], assignment["roleDefinitionId"])


def _role_id(role: str, state: dict[str, Any]) -> str:
    for definition in state["custom_roles"]:
        if role in (definition["roleName"], definition["name"]):
            return definition["name"]
    return builtin_role_id(role)


def _role_name(role: str, state: dict[str, Any]) -> str:
    for definition in state["custom_roles"]:
        if role in (definition["roleName"], definition["name"]):
            return definition["roleName"]
    for name in state["builtin_roles"]:
        if role in (name, builtin_role_id(name)):
            return name
    return role


def _unknown(argv: list[str]) -> int:
    print(f"ERROR: fake az has no handler for: {' '.join(argv)}", file=sys.stderr)
    return 2


def main(argv: list[str]) -> int:
    state_path = Path(os.environ["AZURE_STATE"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    with open(os.environ["AZURE_CALLS"], "a", encoding="utf-8") as calls:
        calls.write(json.dumps(argv) + "\n")

    def save() -> None:
        state_path.write_text(json.dumps(state), encoding="utf-8")

    if state.get("fail_on") and state["fail_on"] in " ".join(argv):
        print("ERROR: injected failure", file=sys.stderr)
        return 1

    group = " ".join(argv[:3])
    query = _opt(argv, "--query")

    if group == "role definition list":
        name = _opt(argv, "--name")
        if "--custom-role-only" in argv:
            # A custom role is visible only from a scope it is assignable at;
            # without --scope the CLI looks from its current subscription.
            visible_from = _opt(argv, "--scope") or state["current_subscription"]
            hits = [
                d
                for d in state["custom_roles"]
                if d["roleName"] == name and visible_from in d["assignableScopes"]
            ]
            if query == "[0].name":
                _lines([hits[0]["name"]] if hits else [])
            elif query == _ROLE_SHAPE:
                if hits:
                    d = hits[0]
                    _lines(
                        [
                            "|".join(
                                [
                                    str(d.get("blocks", 1)),
                                    ",".join(sorted(d["actions"])),
                                    ",".join(d.get("notActions", [])),
                                    ",".join(d.get("dataActions", [])),
                                    ",".join(d.get("notDataActions", [])),
                                    ",".join(d["assignableScopes"]),
                                ]
                            )
                        ]
                    )
            else:
                return _unknown(argv)
        elif query == "[0].name":
            _lines([builtin_role_id(name)] if name in state["builtin_roles"] else [])
        else:
            return _unknown(argv)
        return 0

    if group == "role definition create":
        definition = json.loads(_opt(argv, "--role-definition") or "{}")
        if any(d["roleName"] == definition["Name"] for d in state["custom_roles"]):
            print("ERROR: RoleDefinitionWithSameNameExists", file=sys.stderr)
            return 1
        state["custom_roles"].append(
            {
                "name": str(uuid.uuid4()),
                "roleName": definition["Name"],
                "actions": definition["Actions"],
                "assignableScopes": definition["AssignableScopes"],
            }
        )
        save()
        return 0

    if group == "role assignment list":
        assignee = _opt(argv, "--assignee")
        role = _opt(argv, "--role")
        scope = _opt(argv, "--scope")
        if "--all" in argv:
            # --all reaches one subscription and everything below it: the named
            # one, or the CLI's current subscription when none is named.
            named = _opt(argv, "--subscription")
            sub = f"/subscriptions/{named}" if named else state["current_subscription"]

            def in_reach(s: str) -> bool:
                return s == sub or s.startswith(sub + "/")
        elif "--include-inherited" in argv:

            def in_reach(s: str) -> bool:
                return s == scope or not s.startswith("/subscriptions/")
        else:

            def in_reach(s: str) -> bool:
                return s == scope

        hits = [
            a
            for a in state["assignments"]
            if a["assignee"] == assignee
            and in_reach(a["scope"])
            and (role is None or _role_matches(a, role))
        ]
        if query in ("[0].id", "[0].condition"):
            value = hits[0][query.split(".", 1)[1]] if hits else None
            _lines([value] if value is not None else [])
        elif query == _ASSIGNMENT_ROWS:
            _lines(
                ["|".join([a["id"], a["role"], a["scope"], a.get("condition") or ""]) for a in hits]
            )
        else:
            return _unknown(argv)
        return 0

    if group == "role assignment create":
        role = _opt(argv, "--role") or ""
        state["assignments"].append(
            {
                "id": str(uuid.uuid4()),
                "assignee": _opt(argv, "--assignee"),
                "role": _role_name(role, state),
                "roleDefinitionId": _role_id(role, state),
                "scope": _opt(argv, "--scope"),
                "condition": _opt(argv, "--condition"),
                "conditionVersion": _opt(argv, "--condition-version"),
            }
        )
        save()
        return 0

    if group == "role assignment delete":
        ids = _opt(argv, "--ids")
        if ids:
            state["assignments"] = [a for a in state["assignments"] if a["id"] != ids]
        else:
            assignee = _opt(argv, "--assignee")
            scope = _opt(argv, "--scope")
            role = _opt(argv, "--role") or ""
            state["assignments"] = [
                a
                for a in state["assignments"]
                if not (
                    a["assignee"] == assignee and a["scope"] == scope and _role_matches(a, role)
                )
            ]
        save()
        return 0

    if group.startswith("provider register"):
        state.setdefault("providers", []).append(_opt(argv, "--namespace"))
        save()
        return 0

    if group.startswith("group exists"):
        print("true" if _opt(argv, "--name") in state.setdefault("groups", []) else "false")
        return 0

    if group.startswith("group create"):
        state.setdefault("groups", []).append(_opt(argv, "--name"))
        save()
        return 0

    if " ".join(argv[:4]) == "ad app federated-credential list":
        if query != _CREDENTIAL_ROWS:
            return _unknown(argv)
        _lines(
            [
                "|".join([c.get("subject") or "", c.get("issuer") or "", c.get("expression") or ""])
                for c in state["credentials"]
            ]
        )
        return 0

    return _unknown(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
