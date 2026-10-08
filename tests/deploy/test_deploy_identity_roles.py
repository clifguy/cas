"""Gate for the deploy identity's Azure role assignments.

The deploy workflows sign in as one workload identity per tenant, and what that
identity may reach is the blast radius of anything that can mint its federated
token. ``deploy/bootstrap/deploy-identity-roles.sh`` codifies the grant: a
deployments-only custom role at subscription scope (the orchestrator template
deploys there and creates the resource group), and everything else at the
tenant's resource group -- Contributor, a lock role for the database server's
delete lock, and Role Based Access Control Administrator constrained by
condition to the role definitions the templates themselves assign.

Two lists in the script have to track the templates: the roles the condition
admits, and the resource providers registered on the identity's behalf. A
module that starts assigning a new role or deploying a new resource type would
otherwise fail its next deploy on an authorization error, or tempt someone to
widen the grant by hand; the drift cases below fail first and name it.

The script runs here against ``_fake_rbac_az.py``, a stateful stand-in for the
CLI, so the assertions read the assignments a run leaves behind rather than
the script's text. The condition is compared whole: it is the security property
of the grant, and its structure -- which action each arm guards, how the arms
combine, the principal-type clause -- matters as much as the ids it names.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import pytest

from tests.deploy._fake_rbac_az import builtin_role_id

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
SCRIPT: Final[Path] = REPO_ROOT / "deploy" / "bootstrap" / "deploy-identity-roles.sh"
FAKE_AZ: Final[Path] = Path(__file__).with_name("_fake_rbac_az.py")
INFRA_DIR: Final[Path] = REPO_ROOT / "infra"
RUNBOOK: Final[Path] = REPO_ROOT / "docs" / "process" / "azure-deployment.md"

# Built-in Azure role ids the templates assign, mapped to the role names the
# script resolves at run time. Fixed, public Azure constants -- not environment
# identity coordinates. A template that assigns a role absent here fails
# test_allowlist_tracks_the_templates.
_TEMPLATE_ROLES: Final[dict[str, str]] = {
    "3913510d-42f4-4e42-8a64-420c390055eb": "Monitoring Metrics Publisher",
    "7f951dda-4ed3-4680-a7ca-43fe172d538d": "AcrPull",
    "4633458b-17de-408a-b874-0445c86b69e6": "Key Vault Secrets User",
    "db79e9a7-68ee-4b58-9aeb-b90e7c24fcba": "Key Vault Certificate User",
}

_BUILTINS: Final[list[str]] = [
    "Owner",
    "Contributor",
    "User Access Administrator",
    "Role Based Access Control Administrator",
    "AcrPush",
    *_TEMPLATE_ROLES.values(),
]

# Namespaces every subscription has registered; nothing needs to register them.
_ALWAYS_REGISTERED: Final[frozenset[str]] = frozenset(
    {"Microsoft.Authorization", "Microsoft.Resources"}
)

_APP_ID: Final[str] = "deploy-app"
_SUB: Final[str] = "/subscriptions/sub-1"
_RG: Final[str] = "/subscriptions/sub-1/resourceGroups/rg-tenant"
_REGISTRY: Final[str] = _RG + "/providers/Microsoft.ContainerRegistry/registries/acr"
_MANAGEMENT_GROUP: Final[str] = "/providers/Microsoft.Management/managementGroups/mg"
_ORCHESTRATOR: Final[str] = "CAS deployment orchestrator (sub-1)"
_LOCK: Final[str] = "CAS deployment lock operator (sub-1)"
_RBAC_ADMIN: Final[str] = "Role Based Access Control Administrator"
_ISSUER: Final[str] = "https://token.actions.githubusercontent.com"
_ENV_CREDENTIAL: Final[dict[str, str]] = {
    "subject": "repo:owner/repo:environment:tenant-prod",
    "issuer": _ISSUER,
}

_ORCHESTRATOR_ACTIONS: Final[list[str]] = sorted(
    f"Microsoft.Resources/deployments/{action}"
    for action in (
        "read",
        "write",
        "delete",
        "validate/action",
        "whatIf/action",
        "operations/read",
        "operationstatuses/read",
    )
)
_LOCK_ACTIONS: Final[list[str]] = sorted(
    f"Microsoft.Authorization/locks/{action}" for action in ("read", "write", "delete")
)

_NARROWED: Final[set[tuple[str, str]]] = {
    (_ORCHESTRATOR, _SUB),
    ("Contributor", _RG),
    (_RBAC_ADMIN, _RG),
    (_LOCK, _RG),
}

_ROLE_VAR_RE: Final[re.Pattern[str]] = re.compile(
    r"roleDefinitionId:\s*subscriptionResourceId\(\s*'Microsoft\.Authorization/roleDefinitions',"
    r"\s*([A-Za-z0-9_]+)\s*\)"
)
_VAR_RE: Final[str] = r"^var\s+{name}\s*=\s*'([0-9a-fA-F-]{{36}})'"
_RESOURCE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*resource\s+\w+\s+'([A-Za-z]+\.[A-Za-z]+)/", re.MULTILINE
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assignment(role: str, scope: str, condition: str | None = None) -> dict[str, Any]:
    return {
        "id": f"pre-{role}-{scope}",
        "assignee": _APP_ID,
        "role": role,
        "roleDefinitionId": builtin_role_id(role),
        "scope": scope,
        "condition": condition,
    }


def _legacy_state() -> dict[str, Any]:
    """The pre-narrowing grant: Contributor and User Access Administrator at
    subscription scope, and an environment-only federated credential."""
    return {
        "builtin_roles": _BUILTINS,
        "custom_roles": [],
        "assignments": [
            _assignment("Contributor", _SUB),
            _assignment("User Access Administrator", _SUB),
        ],
        "credentials": [dict(_ENV_CREDENTIAL)],
        "groups": ["rg-tenant"],
        # The operator's CLI points at another subscription, so every read
        # that does not name the tenant's subscription looks in the wrong place.
        "current_subscription": "/subscriptions/elsewhere",
    }


def _run(
    tmp_path: Path, state: dict[str, Any], *args: str
) -> tuple[subprocess.CompletedProcess[str], list[list[str]], dict[str, Any]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    az = bin_dir / "az"
    az.write_text(f"#!{sys.executable} -IS\n" + FAKE_AZ.read_text(encoding="utf-8"))
    az.chmod(0o755)
    # The script waits between custom-role lookups; this records each wait as a
    # call instead of sleeping through it.
    sleep = bin_dir / "sleep"
    sleep.write_text('#!/bin/sh\nprintf \'["sleep", "%s"]\\n\' "$1" >> "$AZURE_CALLS"\n')
    sleep.chmod(0o755)
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state))
    calls_path = tmp_path / "calls.jsonl"
    calls_path.write_text("")
    result = subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=REPO_ROOT,
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "AZURE_STATE": str(state_path),
            "AZURE_CALLS": str(calls_path),
            "APP_ID": _APP_ID,
            "SUBSCRIPTION_ID": "sub-1",
            "RESOURCE_GROUP_NAME": "rg-tenant",
            "LOCATION": "region-1",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    return result, calls, json.loads(state_path.read_text())


def _lookup_attempts() -> int:
    """How many times the script lists a custom role before taking it as absent."""
    match = re.search(r"^ROLE_LOOKUP_ATTEMPTS=(\d+)$", SCRIPT.read_text(), re.MULTILINE)
    assert match, "ROLE_LOOKUP_ATTEMPTS not found in the script"
    return int(match.group(1))


def _role_id_lookups(calls: list[list[str]], role: str) -> list[int]:
    """Positions of the custom-role id lookups for ``role`` in the call log."""
    return [
        i
        for i, c in enumerate(calls)
        if c[:3] == ["role", "definition", "list"]
        and "--custom-role-only" in c
        and c[c.index("--name") + 1] == role
        and c[c.index("--query") + 1] == "[0].name"
    ]


def _role_creates(calls: list[list[str]], role: str) -> list[int]:
    """Positions of the create calls for custom role ``role`` in the call log."""
    return [
        i
        for i, c in enumerate(calls)
        if c[:3] == ["role", "definition", "create"]
        and json.loads(c[c.index("--role-definition") + 1])["Name"] == role
    ]


def _held(state: dict[str, Any]) -> set[tuple[str, str]]:
    return {(a["role"], a["scope"]) for a in state["assignments"]}


def _granted(tmp_path: Path) -> dict[str, Any]:
    """The state after a successful apply over the legacy grant."""
    result, _, state = _run(tmp_path, _legacy_state())
    assert result.returncode == 0, result.stderr
    return state


def _expected_condition() -> str:
    """The exact condition the RBAC administrator grant must carry."""
    ids = ", ".join(builtin_role_id(name) for name in _TEMPLATE_ROLES.values())
    spn = "ForAnyOfAnyValues:StringEqualsIgnoreCase {'ServicePrincipal'}"
    attribute = "Microsoft.Authorization/roleAssignments"
    return (
        f"((!(ActionMatches{{'{attribute}/write'}})) OR "
        f"(@Request[{attribute}:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {{{ids}}} "
        f"AND @Request[{attribute}:PrincipalType] {spn})) AND "
        f"((!(ActionMatches{{'{attribute}/delete'}})) OR "
        f"(@Resource[{attribute}:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {{{ids}}} "
        f"AND @Resource[{attribute}:PrincipalType] {spn}))"
    )


def _template_role_ids() -> set[str]:
    """Every role-definition id a template assigns, resolved through its var."""
    ids: set[str] = set()
    for bicep in sorted(INFRA_DIR.rglob("*.bicep")):
        text = bicep.read_text(encoding="utf-8")
        for name in _ROLE_VAR_RE.findall(text):
            match = re.search(_VAR_RE.format(name=re.escape(name)), text, re.MULTILINE)
            assert match, f"{bicep.name}: role id variable {name} is not a literal"
            ids.add(match.group(1).lower())
        assert text.count("roleDefinitionId:") == len(_ROLE_VAR_RE.findall(text)), (
            f"{bicep.name} assigns a role in a form this gate does not parse"
        )
    return ids


def _template_namespaces() -> set[str]:
    """Every resource-provider namespace a template declares a resource in."""
    return {
        namespace
        for bicep in INFRA_DIR.rglob("*.bicep")
        for namespace in _RESOURCE_RE.findall(bicep.read_text(encoding="utf-8"))
    } - _ALWAYS_REGISTERED


def _script_array(name: str) -> list[str]:
    """The quoted members of a bash array the script declares."""
    match = re.search(rf"^{name}=\(\n(.*?)\n\)", SCRIPT.read_text(), re.MULTILINE | re.DOTALL)
    assert match, f"the script must declare {name} as a bash array"
    return re.findall(r'"([^"]+)"', match.group(1))


# ---------------------------------------------------------------------------
# The lists track the templates
# ---------------------------------------------------------------------------


def test_allowlist_tracks_the_templates() -> None:
    """A1: the condition admits exactly the roles the templates assign."""
    template_ids = _template_role_ids()

    assert template_ids, "found no role assignments in the templates; the parse is broken"
    unknown = template_ids - set(_TEMPLATE_ROLES)
    assert not unknown, (
        f"a template assigns role id(s) {sorted(unknown)} the deploy identity's "
        "condition does not admit; add the role to ASSIGNABLE_ROLES and to this map"
    )
    assert sorted(_script_array("ASSIGNABLE_ROLES")) == sorted(
        _TEMPLATE_ROLES[i] for i in template_ids
    )


def test_allowlist_gate_sees_a_new_template_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A2: control -- a module assigning an unlisted role is found by the parse."""
    (tmp_path / "m.bicep").write_text(
        "var ownerRoleId = '8e3af657-a8ff-443c-a75c-2fe8c4bcb635'\n"
        "resource r 'Microsoft.Authorization/roleAssignments@2022-04-01' = {\n"
        "  properties: {\n"
        "    roleDefinitionId: subscriptionResourceId("
        "'Microsoft.Authorization/roleDefinitions', ownerRoleId)\n"
        "  }\n}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "INFRA_DIR", tmp_path)

    assert _template_role_ids() - set(_TEMPLATE_ROLES) == {"8e3af657-a8ff-443c-a75c-2fe8c4bcb635"}


def test_registered_providers_track_the_templates() -> None:
    """A3: every namespace a template deploys is registered by the script."""
    namespaces = _template_namespaces()

    assert namespaces, "found no resources in the templates; the parse is broken"
    assert sorted(_script_array("RESOURCE_PROVIDERS")) == sorted(namespaces)


def test_provider_gate_sees_a_new_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A4: control -- a module using a new namespace is found by the parse."""
    (tmp_path / "m.bicep").write_text(
        "resource s 'Microsoft.Storage/storageAccounts@2023-05-01' = {}\n"
        "resource l 'Microsoft.Authorization/locks@2020-05-01' = {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "INFRA_DIR", tmp_path)

    assert _template_namespaces() == {"Microsoft.Storage"}


# ---------------------------------------------------------------------------
# Applying the grant
# ---------------------------------------------------------------------------


def test_apply_registers_the_template_providers(tmp_path: Path) -> None:
    """G0: a namespace the subscription has never used is registered under the
    operator's rights, since the identity can no longer register one."""
    state = _granted(tmp_path)

    assert sorted(state["providers"]) == sorted(_template_namespaces())


def test_apply_grants_the_narrowed_set(tmp_path: Path) -> None:
    """G1: one deployments-only role at subscription scope; the rest at the
    resource group; no new User Access Administrator anywhere."""
    state = _granted(tmp_path)

    assert _held(state) - _held(_legacy_state()) == _NARROWED


def test_apply_creates_an_absent_resource_group_first(tmp_path: Path) -> None:
    """G1b: the narrowed grants sit on the resource group, so a first-time
    tenant's group is created (in LOCATION) before anything is assigned on it,
    and an existing group is left alone."""
    state = _legacy_state()
    state["groups"] = []

    result, calls, after = _run(tmp_path, state)

    assert result.returncode == 0, result.stderr
    assert after["groups"] == ["rg-tenant"]
    create = next(i for i, c in enumerate(calls) if c[:2] == ["group", "create"])
    assert calls[create][calls[create].index("--location") + 1] == "region-1"
    first_rg_grant = next(
        i for i, c in enumerate(calls) if c[1:3] == ["assignment", "create"] and _RG in c
    )
    assert create < first_rg_grant

    _, again, _ = _run(tmp_path, after)
    assert not [c for c in again if c[:2] == ["group", "create"]]


def test_custom_roles_carry_only_their_actions(tmp_path: Path) -> None:
    """G2: the subscription-scope role can do nothing but deployments -- and
    cannot cancel one -- and the lock role nothing but locks. Both names carry
    the subscription, since role names are unique per directory."""
    state = _granted(tmp_path)

    actions = {d["roleName"]: sorted(d["actions"]) for d in state["custom_roles"]}
    assert actions == {_ORCHESTRATOR: _ORCHESTRATOR_ACTIONS, _LOCK: _LOCK_ACTIONS}
    assert all(d["assignableScopes"] == [_SUB] for d in state["custom_roles"])


@pytest.mark.parametrize(
    ("field", "value", "reported"),
    [
        ("actions", ["Microsoft.Resources/deployments/cancel/action"], "cancel/action"),
        ("dataActions", ["Microsoft.Storage/storageAccounts/*"], "storageAccounts"),
        ("notDataActions", ["Microsoft.KeyVault/vaults/*"], "vaults"),
        ("assignableScopes", ["/subscriptions/other"], "/subscriptions/other"),
        ("blocks", 2, "2|"),
    ],
    ids=["action", "data-action", "not-data-action", "assignable-scope", "second-block"],
)
def test_existing_widened_role_is_refused(
    tmp_path: Path, field: str, value: Any, reported: str
) -> None:
    """G2b: a custom role already present is verified, not trusted; one changed
    by hand -- in its actions, data actions, scopes or permission blocks -- stops
    the run before anything is assigned with it."""
    state = _granted(tmp_path)
    for definition in state["custom_roles"]:
        if definition["roleName"] == _ORCHESTRATOR:
            if isinstance(value, list):
                definition[field] = [*definition.get(field, []), *value]
            else:
                definition[field] = value
    state["assignments"] = [a for a in state["assignments"] if a["role"] != _ORCHESTRATOR]

    result, _, after = _run(tmp_path, state)

    assert result.returncode != 0
    assert reported in result.stderr
    assert (_ORCHESTRATOR, _SUB) not in _held(after)


def test_role_assignment_admin_carries_the_exact_condition(tmp_path: Path) -> None:
    """G3: the condition guards both write and delete, each arm requiring a
    template role id and a service principal, joined so both must hold."""
    state = _granted(tmp_path)

    (admin,) = [a for a in state["assignments"] if a["role"] == _RBAC_ADMIN]
    assert admin["scope"] == _RG
    assert admin["conditionVersion"] == "2.0"
    assert admin["condition"] == _expected_condition()


def test_apply_is_idempotent(tmp_path: Path) -> None:
    """G4: a second run creates and deletes nothing."""
    first = _granted(tmp_path)
    result, calls, second = _run(tmp_path, first)

    assert result.returncode == 0, result.stderr
    assert second["assignments"] == first["assignments"]
    assert second["custom_roles"] == first["custom_roles"]
    assert not [c for c in calls if c[1:3] in (["assignment", "create"], ["definition", "create"])]
    assert not [c for c in calls if c[1:3] == ["assignment", "delete"]]


def test_apply_replaces_a_stale_condition(tmp_path: Path) -> None:
    """G4b: an RBAC administrator grant whose condition differs -- an older
    allowlist, or one edited by hand -- is replaced with the current one."""
    state = _granted(tmp_path)
    for assignment in state["assignments"]:
        if assignment["role"] == _RBAC_ADMIN:
            assignment["condition"] = "stale"

    result, _, after = _run(tmp_path, state)

    assert result.returncode == 0, result.stderr
    (admin,) = [a for a in after["assignments"] if a["role"] == _RBAC_ADMIN]
    assert admin["condition"] == _expected_condition()


@pytest.mark.parametrize(
    ("credential", "reported"),
    [
        ({"subject": "repo:owner/repo:ref:refs/heads/main", "issuer": _ISSUER}, "refs/heads/main"),
        ({"subject": "repo:owner/repo:pull_request", "issuer": _ISSUER}, "pull_request"),
        (
            {"subject": "", "issuer": _ISSUER, "expression": "claims['sub'] matches 'repo:*'"},
            "expression",
        ),
        ({"subject": "", "issuer": _ISSUER}, "not an environment subject"),
        (
            {"subject": "repo:owner/repo:environment:x", "issuer": "https://elsewhere"},
            "issuer",
        ),
    ],
    ids=["branch", "pull-request", "claims-expression", "empty-subject", "foreign-issuer"],
)
def test_apply_refuses_a_non_environment_credential(
    tmp_path: Path, credential: dict[str, str], reported: str
) -> None:
    """G5: any credential but an environment subject from the GitHub issuer
    could admit a token minted outside the environment gate, so the grant is
    refused while one exists."""
    state = _legacy_state()
    state["credentials"].append(credential)

    result, _, after = _run(tmp_path, state)

    assert result.returncode != 0
    assert reported in result.stderr
    assert _held(after) == _held(_legacy_state())


def test_apply_refuses_without_a_credential(tmp_path: Path) -> None:
    """G5b: an application with no federated credential is not a deploy identity."""
    state = _legacy_state()
    state["credentials"] = []

    result, _, after = _run(tmp_path, state)

    assert result.returncode != 0
    assert _held(after) == _held(_legacy_state())


def test_apply_stops_on_a_failed_call(tmp_path: Path) -> None:
    """G6: an az failure aborts the run rather than continuing half-granted."""
    state = _legacy_state()
    state["fail_on"] = "assignment create --assignee deploy-app --role Role Based"

    result, _, after = _run(tmp_path, state)

    assert result.returncode != 0
    assert (_LOCK, _RG) not in _held(after)


def test_rerun_survives_a_missed_role_lookup(tmp_path: Path) -> None:
    """G7: the role-definition listing is eventually consistent, so a role that
    exists can list as absent for a while. A re-run that meets such a miss
    retries the lookup instead of trying to create the role again."""
    first = _granted(tmp_path)
    first["lookup_misses"] = 2

    result, calls, second = _run(tmp_path, first)

    assert result.returncode == 0, result.stderr
    assert not [c for c in calls if c[:3] == ["role", "definition", "create"]]
    assert second["custom_roles"] == first["custom_roles"]
    assert _held(second) >= _NARROWED


def test_rerun_survives_a_missed_shape_lookup(tmp_path: Path) -> None:
    """G7b: the verification read of an existing role retries a miss too, rather
    than refusing the role as reshaped."""
    first = _granted(tmp_path)
    first["shape_misses"] = 2

    result, calls, second = _run(tmp_path, first)

    assert result.returncode == 0, result.stderr
    assert not [c for c in calls if c[:3] == ["role", "definition", "create"]]
    assert _held(second) >= _NARROWED


def test_create_hitting_an_existing_name_re_resolves(tmp_path: Path) -> None:
    """G7c: when every lookup misses and the create then reports that the name
    is taken, the role exists after all; the run resolves it and carries on
    without a second definition."""
    state = _granted(tmp_path)
    state["assignments"] = [a for a in state["assignments"] if a["role"] != _ORCHESTRATOR]
    state["lookup_misses"] = _lookup_attempts()

    result, calls, after = _run(tmp_path, state)

    assert result.returncode == 0, result.stderr
    assert len(_role_creates(calls, _ORCHESTRATOR)) == 1
    assert after["custom_roles"] == state["custom_roles"]
    assert _held(after) >= _NARROWED


def test_absent_role_is_created_after_bounded_lookups(tmp_path: Path) -> None:
    """G7d: a role that genuinely does not exist is still created, once, after
    a bounded number of lookups and a wait between each."""
    result, calls, state = _run(tmp_path, _legacy_state())

    assert result.returncode == 0, result.stderr
    attempts = _lookup_attempts()
    for role in (_ORCHESTRATOR, _LOCK):
        (create,) = _role_creates(calls, role)
        before = [i for i in _role_id_lookups(calls, role) if i < create]
        assert len(before) == attempts
        waits = [c for c in calls[before[0] : create] if c[0] == "sleep"]
        assert len(waits) == attempts - 1
    assert sorted(d["roleName"] for d in state["custom_roles"]) == sorted([_LOCK, _ORCHESTRATOR])


def test_created_role_that_lists_late_is_resolved(tmp_path: Path) -> None:
    """G7e: a role the run has just created can also be slow to list; the
    lookup after creation retries rather than reporting it missing."""
    state = _legacy_state()
    state["lookup_misses"] = _lookup_attempts() + 2

    result, calls, after = _run(tmp_path, state)

    assert result.returncode == 0, result.stderr
    assert len(_role_creates(calls, _ORCHESTRATOR)) == 1
    assert _held(after) >= _NARROWED


def test_role_create_failure_other_than_duplicate_stops(tmp_path: Path) -> None:
    """G7f: only a name collision is taken as proof that the role exists; any
    other create failure stops the run before anything is assigned."""
    state = _legacy_state()
    state["fail_on"] = "definition create"

    result, _, after = _run(tmp_path, state)

    assert result.returncode != 0
    assert after["custom_roles"] == []
    assert not _held(after) & _NARROWED


def test_rerun_survives_a_missed_assignment_lookup(tmp_path: Path) -> None:
    """G8: the role-assignment listing is eventually consistent too. A re-run
    that meets a miss retries the lookup instead of assigning again, which
    Resource Manager would refuse."""
    first = _granted(tmp_path)
    first["assignment_misses"] = 2

    result, calls, second = _run(tmp_path, first)

    assert result.returncode == 0, result.stderr
    assert not [c for c in calls if c[1:3] == ["assignment", "create"]]
    assert second["assignments"] == first["assignments"]


def test_assignment_that_exists_unlisted_is_kept(tmp_path: Path) -> None:
    """G8b: when every lookup misses and the create then reports that the
    assignment exists, the run carries on without replacing it -- including
    the conditioned grant, whose condition is then read and found current."""
    first = _granted(tmp_path)
    # Enough misses to exhaust the lookups for the first three assignments.
    first["assignment_misses"] = 3 * _lookup_attempts()

    result, calls, second = _run(tmp_path, first)

    assert result.returncode == 0, result.stderr
    creates = [c for c in calls if c[1:3] == ["assignment", "create"]]
    assert len(creates) == 3
    assert any("--condition" in c for c in creates)
    assert not [c for c in calls if c[1:3] == ["assignment", "delete"]]
    assert second["assignments"] == first["assignments"]


def _with_admin_condition(state: dict[str, Any], condition: str | None) -> dict[str, Any]:
    for assignment in state["assignments"]:
        if assignment["role"] == _RBAC_ADMIN:
            assignment["condition"] = condition
    return state


def test_unlisted_assignment_with_a_stale_condition_is_replaced(tmp_path: Path) -> None:
    """G8c: an assignment found only through the create's collision still has
    its condition checked, and a stale one is replaced with the current one."""
    state = _with_admin_condition(_granted(tmp_path), "stale")
    state["assignment_misses"] = 3 * _lookup_attempts()

    result, calls, after = _run(tmp_path, state)

    assert result.returncode == 0, result.stderr
    assert len([c for c in calls if c[1:3] == ["assignment", "delete"]]) == 1
    (admin,) = [a for a in after["assignments"] if a["role"] == _RBAC_ADMIN]
    assert admin["condition"] == _expected_condition()


def test_failed_condition_replacement_names_the_step(tmp_path: Path) -> None:
    """G8d: when the listing still misses after a collision, the delete finds
    nothing and the re-create collides again; the run stops and says so rather
    than exiting without a reason."""
    attempts = _lookup_attempts()
    state = _with_admin_condition(_granted(tmp_path), "stale")
    # Three id lookups and the re-read after the collision all exhaust, and
    # the delete's own listing misses.
    state["assignment_misses"] = 4 * attempts + 1

    result, _, after = _run(tmp_path, state)

    assert result.returncode != 0
    assert f"assignment of {_RBAC_ADMIN} at {_RG} still exists after deleting it" in result.stderr
    (admin,) = [a for a in after["assignments"] if a["role"] == _RBAC_ADMIN]
    assert admin["condition"] == "stale"


def test_unconditioned_admin_grant_is_replaced_without_waiting(tmp_path: Path) -> None:
    """G8e: an existing RBAC administrator grant with no condition lists as a
    row, so it is replaced at once instead of being retried as missing."""
    state = _with_admin_condition(_granted(tmp_path), None)

    result, calls, after = _run(tmp_path, state)

    assert result.returncode == 0, result.stderr
    assert not [c for c in calls if c[0] == "sleep"]
    (admin,) = [a for a in after["assignments"] if a["role"] == _RBAC_ADMIN]
    assert admin["condition"] == _expected_condition()


def test_remove_legacy_rereads_an_incomplete_listing(tmp_path: Path) -> None:
    """G8f: a listing that is missing part of the narrowed set is read again
    before the run refuses, so an incomplete answer does not stop retirement."""
    state = _granted(tmp_path)
    state["assignment_misses"] = 1

    result, _, after = _run(tmp_path, state, "--remove-legacy")

    assert result.returncode == 0, result.stderr
    assert _held(after) == _NARROWED


def test_show_rereads_an_incomplete_listing(tmp_path: Path) -> None:
    """G8g: --show reads the listing again while it is missing part of the
    narrowed set, so an incomplete answer cannot hide a grant outside it."""
    state = _granted(tmp_path)
    state["assignments"] = [
        a for a in state["assignments"] if (a["role"], a["scope"]) in _NARROWED
    ] + [_assignment("Owner", _RG)]
    state["assignment_misses"] = 1

    result, _, _ = _run(tmp_path, state, "--show")

    assert result.returncode != 0
    assert f"OUTSIDE THE NARROWED SET: Owner at {_RG}" in result.stderr


# ---------------------------------------------------------------------------
# Retiring everything outside the narrowed set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "legacy",
    [
        [("Contributor", _SUB), ("User Access Administrator", _SUB)],
        [("Owner", _SUB)],
        [("Contributor", _RG), ("User Access Administrator", _RG)],
        [("AcrPush", _REGISTRY)],
    ],
    ids=["subscription-pair", "owner", "resource-group-pair", "acr-push"],
)
def test_remove_legacy_leaves_exactly_the_narrowed_set(
    tmp_path: Path, legacy: list[tuple[str, str]]
) -> None:
    """R1: every grant a tenant may hold from an earlier bootstrap -- the
    subscription pair, Owner, the resource-group pair, AcrPush -- is removed,
    leaving the four narrowed assignments and nothing else."""
    state = _legacy_state()
    state["assignments"] = [_assignment(role, scope) for role, scope in legacy]
    result, _, granted = _run(tmp_path, state)
    assert result.returncode == 0, result.stderr
    assert set(legacy) <= _held(granted)

    result, _, after = _run(tmp_path, granted, "--remove-legacy")

    assert result.returncode == 0, result.stderr
    assert _held(after) == _NARROWED


@pytest.mark.parametrize(
    "drop",
    sorted(_NARROWED) + [("stale-condition", _RG)],
    ids=lambda d: d[0].split(" (")[0].replace(" ", "-"),
)
def test_remove_legacy_refuses_until_the_narrowed_set_is_complete(
    tmp_path: Path, drop: tuple[str, str]
) -> None:
    """R2: removing the old grant before every narrowed assignment -- including
    the RBAC administrator's exact condition -- is in place would leave the
    identity unable to deploy."""
    state = _granted(tmp_path)
    if drop[0] == "stale-condition":
        for assignment in state["assignments"]:
            if assignment["role"] == _RBAC_ADMIN:
                assignment["condition"] = "stale"
    else:
        state["assignments"] = [a for a in state["assignments"] if (a["role"], a["scope"]) != drop]
    before = _held(state)

    result, _, after = _run(tmp_path, state, "--remove-legacy")

    assert result.returncode != 0
    assert _held(after) == before


def test_remove_legacy_reports_an_inherited_grant_without_touching_it(tmp_path: Path) -> None:
    """R3: a grant inherited from a management group cannot be removed at the
    subscription; it is reported and fails the run, while the subscription's
    own legacy grants are still removed."""
    granted = _granted(tmp_path)
    granted["assignments"].append(_assignment("Owner", _MANAGEMENT_GROUP))

    result, _, after = _run(tmp_path, granted, "--remove-legacy")

    assert result.returncode != 0
    assert _MANAGEMENT_GROUP in result.stderr
    assert _held(after) == _NARROWED | {("Owner", _MANAGEMENT_GROUP)}


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def test_show_fails_while_legacy_grants_remain_and_passes_after(tmp_path: Path) -> None:
    """S1: --show lists the credentials and assignments, changes nothing, and
    exits non-zero while anything outside the narrowed set is held."""
    granted = _granted(tmp_path)
    shown, _, unchanged = _run(tmp_path, granted, "--show")
    assert shown.returncode != 0
    assert "User Access Administrator" in shown.stderr
    assert _ENV_CREDENTIAL["subject"] in shown.stdout
    assert unchanged == granted

    _, _, narrowed = _run(tmp_path, granted, "--remove-legacy")
    clean, _, _ = _run(tmp_path, narrowed, "--show")
    assert clean.returncode == 0, clean.stderr
    for role, scope in _NARROWED:
        assert f"{role} at {scope}" in clean.stdout


def test_show_fails_on_an_inherited_grant(tmp_path: Path) -> None:
    """S2: a grant inherited from above the subscription widens the identity
    as much as one held in it, so --show does not pass over it."""
    granted = _granted(tmp_path)
    _, _, narrowed = _run(tmp_path, granted, "--remove-legacy")
    narrowed["assignments"].append(_assignment("Owner", _MANAGEMENT_GROUP))

    shown, _, _ = _run(tmp_path, narrowed, "--show")

    assert shown.returncode != 0
    assert _MANAGEMENT_GROUP in shown.stderr


@pytest.mark.parametrize("listing", ["--all", "--include-inherited"])
def test_show_fails_when_the_listing_fails(tmp_path: Path, listing: str) -> None:
    """S3: a listing that errors -- either of the two -- is not an identity
    with nothing to remove."""
    granted = _granted(tmp_path)
    _, _, narrowed = _run(tmp_path, granted, "--remove-legacy")
    narrowed["fail_on"] = listing

    shown, _, _ = _run(tmp_path, narrowed, "--show")

    assert shown.returncode != 0


# ---------------------------------------------------------------------------
# The runbook
# ---------------------------------------------------------------------------


def test_runbook_uses_the_script_not_the_subscription_pair() -> None:
    """D1: the runbook's grant step is the script, and it no longer instructs
    a subscription-scope User Access Administrator grant."""
    text = RUNBOOK.read_text(encoding="utf-8")

    assert "deploy/bootstrap/deploy-identity-roles.sh" in text
    assert '--role "User Access Administrator"' not in text
    assert '--scope "/subscriptions/${SUBSCRIPTION_ID}"' not in text
    assert "environment:" in text
