"""Operator-only empty-group bootstrap for the isolated, committed CI trial.

The owner performs this one-time directory/RBAC floor from merged code. CI gets
only deployment orchestration and exact resource scopes, never subscription-wide
write/role administration. A retained intent precedes every write; retry uses the
same clock. Unknown outcomes require read-only reconciliation before resumption.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

CONTRIBUTOR = "b24988ac-6180-42a0-ab88-20f7382dd24c"
RBAC = "f58310d9-a9f6-439a-9e8d-f62e7b41a168"
ACR = "7f951dda-4ed3-4680-a7ca-43fe172d538d"
VAULT_USER = "4633458b-17de-408a-b874-0445c86b69e6"
BLOB = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
READER = "acdd72a7-3385-48ef-bd42-f606fba81ae7"
DEPLOY_ACTIONS = sorted(
    [
        "Microsoft.Resources/deployments/delete",
        "Microsoft.Resources/deployments/operations/read",
        "Microsoft.Resources/deployments/operationstatuses/read",
        "Microsoft.Resources/deployments/read",
        "Microsoft.Resources/deployments/validate/action",
        "Microsoft.Resources/deployments/whatIf/action",
        "Microsoft.Resources/deployments/write",
    ]
)
REFERENCE_ACTIONS = [
    "Microsoft.App/containerApps/listSecrets/action",
    "Microsoft.App/containerApps/read",
]
QUOTA_ACTIONS = [
    "Microsoft.App/locations/usages/read",
    "Microsoft.App/locations/availableManagedEnvironmentsWorkloadProfileTypes/read",
    "Microsoft.DBforPostgreSQL/locations/capabilities/read",
    "Microsoft.Resources/subscriptions/read",
    "Microsoft.Resources/subscriptions/providers/read",
    "Microsoft.Authorization/roleAssignments/read",
    "Microsoft.Authorization/roleDefinitions/read",
]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def group_scope(plan, name):
    return "/subscriptions/" + plan["subscription_id"] + "/resourceGroups/" + name


def condition(roles):
    ids = ", ".join(sorted(roles))
    return (
        "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR "
        "(@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] "
        "ForAnyOfAnyValues:GuidEquals {"
        + ids
        + "} AND @Request[Microsoft.Authorization/roleAssignments:PrincipalType] "
        "ForAnyOfAnyValues:StringEqualsIgnoreCase {'ServicePrincipal'})) AND "
        "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR "
        "(@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] "
        "ForAnyOfAnyValues:GuidEquals {"
        + ids
        + "} AND @Resource[Microsoft.Authorization/roleAssignments:PrincipalType] "
        "ForAnyOfAnyValues:StringEqualsIgnoreCase {'ServicePrincipal'}))"
    )


def role_definitions(plan):
    sub = "/subscriptions/" + plan["subscription_id"]
    reference = group_scope(plan, plan["source"]["resource_group"])
    definitions = []
    role_specs = [
        ("orchestrator", DEPLOY_ACTIONS, [sub]),
        ("reference-reader", REFERENCE_ACTIONS, [reference]),
        ("quota-reader", QUOTA_ACTIONS, [sub]),
    ]
    if plan.get("phase") == "embedding_canary":
        role_specs = [
            (
                "embedding-orchestrator",
                sorted(
                    set(
                        DEPLOY_ACTIONS
                        + [
                            "Microsoft.App/locations/usages/read",
                            "Microsoft.Resources/subscriptions/read",
                            "Microsoft.Resources/subscriptions/resourceGroups/read",
                        ]
                    )
                ),
                [sub],
            )
        ]
    for name, actions, scopes in role_specs:
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, sub + "/cas-performance/" + name))
        definitions.append(
            {
                "Name": "CAS performance " + name + " (" + plan["subscription_id"] + ")",
                "Id": identity,
                "IsCustom": True,
                "Description": "Bounded performance experiment bootstrap.",
                "Actions": actions,
                "NotActions": [],
                "DataActions": [],
                "NotDataActions": [],
                "AssignableScopes": scopes,
            }
        )
    return definitions


def expected_assignments(plan, principal):
    if not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", principal):
        raise ValueError("invalid experiment principal")
    sub = "/subscriptions/" + plan["subscription_id"]
    if plan.get("phase") == "embedding_canary":
        orchestration = role_definitions(plan)[0]["Id"]
        bindings = [
            (sub, orchestration, None),
            (group_scope(plan, plan["source"]["resource_group"]), orchestration, None),
            (plan["binding"]["registry_id"], READER, None),
            (plan["binding"]["registry_id"], ACR, None),
            (plan["binding"]["registry_id"], RBAC, condition([ACR])),
        ]
        proof = plan.get("expiry_proof")
        if proof:
            run = proof.get("run_id", "")
            if not re.fullmatch(r"[a-z][a-z0-9-]{2,19}", run) or run == plan["run_id"]:
                raise ValueError("invalid distinct expiry rehearsal identity")
            bindings.append(
                (
                    group_scope(plan, "rg-ingest-perf-control-" + run)
                    + "/providers/Microsoft.Automation/automationAccounts/aa-perf-"
                    + run,
                    READER,
                    None,
                )
            )
        for key, permitted in (("resource_group", [CONTRIBUTOR]), ("controller_group", [BLOB])):
            scope = group_scope(plan, plan[key])
            bindings.extend([(scope, CONTRIBUTOR, None), (scope, RBAC, condition(permitted))])
    else:
        roles = role_definitions(plan)
        app = (
            group_scope(plan, plan["source"]["resource_group"])
            + "/providers/Microsoft.App/containerApps/"
            + plan["source"]["app"]
        )
        bindings = [
            (sub, roles[0]["Id"], None),
            (sub, roles[2]["Id"], None),
            (group_scope(plan, plan["source"]["resource_group"]), roles[0]["Id"], None),
            (app, roles[1]["Id"], None),
            (plan["binding"]["registry_id"], READER, None),
            (plan["binding"]["registry_id"], ACR, None),
            (plan["binding"]["registry_id"], RBAC, condition([ACR])),
            (plan["binding"]["key_vault_id"], READER, None),
            (
                plan["binding"]["key_vault_id"] + "/secrets/anthropic-api-key",
                RBAC,
                condition([VAULT_USER]),
            ),
        ]
        for group, permitted in [
            (plan["resource_group"], [CONTRIBUTOR]),
            (plan["controller_group"], [BLOB]),
        ]:
            scope = group_scope(plan, group)
            bindings.extend([(scope, CONTRIBUTOR, None), (scope, RBAC, condition(permitted))])
    result = []
    for scope, role, constraint in bindings:
        name = str(uuid.uuid5(uuid.NAMESPACE_URL, scope + "/" + principal + "/" + role))
        result.append(
            {
                "id": scope + "/providers/Microsoft.Authorization/roleAssignments/" + name,
                "scope": scope,
                "roleDefinitionId": sub
                + "/providers/Microsoft.Authorization/roleDefinitions/"
                + role,
                "principalId": principal,
                "principalType": "ServicePrincipal",
                "condition": constraint,
                "conditionVersion": "2.0" if constraint else None,
            }
        )
    return result


def assignment_shape(row):
    return {
        k: row.get(k)
        for k in (
            "id",
            "scope",
            "roleDefinitionId",
            "principalId",
            "principalType",
            "condition",
            "conditionVersion",
        )
    }


def validate_receipt(
    receipt, spec, spec_sha, environment, principal, *, now=None, allow_expired=False
):
    from deploy.performance_trial import validate_plan

    if (
        receipt.get("schema_version") != 1
        or receipt.get("spec_sha256") != spec_sha
        or receipt.get("environment") != environment
        or receipt.get("operator_principal") != principal
        or receipt.get("status") != "ready"
    ):
        raise ValueError("bootstrap receipt identity or readiness differs")
    expected = json.loads(json.dumps(spec))
    expected.update({k: receipt["plan"][k] for k in ("created_at", "expires_at")})
    plan = validate_plan(expected, now=now, allow_expired=allow_expired)
    if receipt["plan"] != plan or receipt["assignments"] != expected_assignments(plan, principal):
        raise ValueError("bootstrap plan or assignment inventory differs")
    return plan


def verify_groups(plan, az, *, empty):
    from deploy.performance_trial import owned_inventory

    for group in (plan["resource_group"], plan["controller_group"]):
        row = az("group", "show", "-n", group)
        resources = az("resource", "list", "-g", group)
        owned_inventory({**plan, "resource_group": group}, row, resources)
        if row.get("location", "").casefold() != plan["location"].casefold() or (
            empty and resources
        ):
            raise ValueError("bootstrap group is populated or in a different region")


def role_definition_id(definition):
    subscription = definition["AssignableScopes"][0].split("/")[2]
    return (
        "/subscriptions/"
        + subscription
        + "/providers/Microsoft.Authorization/roleDefinitions/"
        + definition["Id"]
    )


def read_role_definition(definition):
    """Only an exact resource GET returning 404 establishes role absence."""
    from deploy.performance_trial import RemoteError, Rest

    try:
        row = Rest().arm("GET", role_definition_id(definition), api="2022-04-01").json()
        if not isinstance(row, dict):
            raise ValueError("invalid custom role response; reconcile intent")
        return row
    except RemoteError as error:
        if error.status != 404:
            raise
        return None


def role_definition_matches(row, definition):
    """Bind identity and privileges in ARM and flattened CLI readbacks."""
    properties = row.get("properties", row)
    role_type = properties.get("type") if "properties" in row else properties.get("roleType")
    expected = [
        {
            k[0].lower() + k[1:]: definition[k]
            for k in ("Actions", "NotActions", "DataActions", "NotDataActions")
        }
    ]
    permissions = []
    for original in properties.get("permissions", []):
        permission = dict(original)
        for key in ("condition", "conditionVersion"):
            if permission.pop(key, None) is not None:
                return False
        permissions.append(permission)
    return (
        row.get("id", "").casefold() == role_definition_id(definition).casefold()
        and row.get("name", "").casefold() == definition["Id"].casefold()
        and properties.get("roleName") == definition["Name"]
        and permissions == expected
        and properties.get("assignableScopes") == definition["AssignableScopes"]
        and role_type == "CustomRole"
    )


def verify_access(plan, principal, az, *, empty_groups):
    expected = expected_assignments(plan, principal)
    sub = "/subscriptions/" + plan["subscription_id"]
    # Exhaustive --all plus ancestor inventory; do not infer scope from names.
    rows = az("role", "assignment", "list", "--all", "--fill-principal-name", "false")
    inherited = az(
        "role",
        "assignment",
        "list",
        "--scope",
        sub,
        "--include-inherited",
        "--fill-principal-name",
        "false",
    )
    merged = {r["id"].casefold(): r for r in [*rows, *inherited]}
    actual = {
        r["id"].casefold(): assignment_shape(r)
        for r in merged.values()
        if r.get("principalId", "").casefold() == principal.casefold()
    }
    wanted = {r["id"].casefold(): r for r in expected}
    if actual != wanted:
        raise ValueError("bootstrap operator has missing, extra or altered access")
    if empty_groups:
        scopes = [
            group_scope(plan, plan[k]).casefold() for k in ("resource_group", "controller_group")
        ]
        direct = {
            r["id"].casefold()
            for r in rows
            if any(
                r["scope"].casefold() == s or r["scope"].casefold().startswith(s + "/")
                for s in scopes
            )
        }
        wanted_direct = {r["id"].casefold() for r in expected if r["scope"].casefold() in scopes}
        if direct != wanted_direct:
            raise ValueError("unexpected bootstrap group role assignment")
    for definition in role_definitions(plan):
        rows = az("role", "definition", "list", "--name", definition["Id"])
        if len(rows) != 1:
            raise ValueError("bootstrap custom role unresolved")
        r = rows[0]
        if not role_definition_matches(r, definition):
            raise ValueError("bootstrap custom role definition drift")


def verify_bootstrap(plan, az, output, principal):
    """Called before accepting empty groups; no local receipt substitutes for reads."""
    receipt = json.loads((output / "bootstrap-receipt.json").read_text())
    if (
        receipt["plan"] != plan
        or receipt["operator_principal"] != principal
        or receipt["status"] != "ready"
    ):
        raise ValueError("bootstrap runtime binding drift")
    verify_groups(plan, az, empty=True)
    verify_access(plan, principal, az, empty_groups=True)


def bootstrap(
    spec_path, approved_sha, environment, principal, output, az, *, now=None, role_reader=None
):
    """Create empty groups and scoped assignments, retaining intent before mutation.

    A partial receipt is deliberately not replayable: its operator must reconcile
    recorded effects, then explicitly resume only after the exact empty inventory
    and completed assignment set verifies. No loop retries uncertain mutations.
    """
    from deploy.performance_trial import load_spec, write_json

    if not re.fullmatch(r"[a-z][a-z0-9-]{2,39}", environment):
        raise ValueError("invalid experiment environment")
    output.mkdir(parents=True, exist_ok=True)
    path = output / "bootstrap-receipt.json"
    raw = spec_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != approved_sha:
        raise ValueError("spec digest differs")
    spec = json.loads(raw)
    if path.exists():
        receipt = json.loads(path.read_text())
        plan = validate_receipt(receipt, spec, approved_sha, environment, principal, now=now)
        verify_bootstrap(plan, az, output, principal)
        return receipt
    plan = load_spec(spec_path, approved_sha, materialize=True)
    expected = expected_assignments(plan, principal)
    account = az("account", "show")
    if (
        account["id"] != plan["subscription_id"]
        or account["tenantId"] != plan["binding"]["tenant_id"]
    ):
        raise ValueError("bootstrap account differs")
    if any(az("group", "exists", "-n", plan[k]) for k in ("resource_group", "controller_group")):
        raise ValueError("unreceipted existing group")
    principal_row = az("ad", "sp", "show", "--id", principal)
    if (
        principal_row["id"] != principal
        or principal_row.get("servicePrincipalType") != "Application"
    ):
        raise ValueError("bootstrap principal is not the separate application")
    credentials = az("ad", "app", "federated-credential", "list", "--id", principal_row["appId"])
    expected_credential = {
        "issuer": "https://token.actions.githubusercontent.com",
        "subject": "repo:clifguy/cas:environment:" + environment,
        "audiences": ["api://AzureADTokenExchange"],
    }
    if (
        len(credentials) != 1
        or any(credentials[0].get(k) != v for k, v in expected_credential.items())
        or credentials[0].get("claimsMatchingExpression")
    ):
        raise ValueError("experiment identity federation differs")
    receipt = {
        "schema_version": 1,
        "spec_sha256": approved_sha,
        "environment": environment,
        "operator_principal": principal,
        "plan": plan,
        "assignments": expected,
        "status": "intent",
    }
    write_json(path, receipt)
    for group in (plan["resource_group"], plan["controller_group"]):
        az(
            "group",
            "create",
            "-n",
            group,
            "-l",
            plan["location"],
            "--tags",
            "benchmarkRun=" + plan["run_id"],
            "expiresAt=" + plan["expires_at"],
            "purpose=ingest-performance",
        )
    verify_groups(plan, az, empty=True)
    role_reader = role_reader or read_role_definition
    for definition in role_definitions(plan):
        existing = role_reader(definition)
        if existing is None:
            # CLI role-definition create generates its own ID, ignoring supplied Id.
            # ARM PUT binds the identity used by every later assignment/readback.
            properties = {
                "roleName": definition["Name"],
                "description": definition["Description"],
                "type": "CustomRole",
                "permissions": [
                    {
                        key[0].lower() + key[1:]: definition[key]
                        for key in ("Actions", "NotActions", "DataActions", "NotDataActions")
                    }
                ],
                "assignableScopes": definition["AssignableScopes"],
            }
            created = az(
                "rest",
                "--method",
                "put",
                "--url",
                "https://management.azure.com"
                + role_definition_id(definition)
                + "?api-version=2022-04-01",
                "--body",
                json.dumps({"properties": properties}),
            )
            if not role_definition_matches(created, definition):
                raise ValueError("created custom role differs; reconcile intent")
            readback = role_reader(definition)
            if readback is None or not role_definition_matches(readback, definition):
                raise ValueError("created custom role readback differs; reconcile intent")
        else:
            # Existing role bytes must be checked before any assignment uses it.
            if not role_definition_matches(existing, definition):
                raise ValueError("existing custom role differs")
    for row in expected:
        args = [
            "role",
            "assignment",
            "create",
            "--name",
            row["id"].rsplit("/", 1)[1],
            "--assignee-object-id",
            principal,
            "--assignee-principal-type",
            "ServicePrincipal",
            "--role",
            row["roleDefinitionId"],
            "--scope",
            row["scope"],
        ]
        if row["condition"]:
            args += ["--condition", row["condition"], "--condition-version", "2.0"]
        result = az(*args)
        if assignment_shape(result) != row:
            raise ValueError("role assignment write/readback differs; reconcile intent")
    verify_access(plan, principal, az, empty_groups=True)
    receipt["status"] = "ready"
    write_json(path, receipt)
    return receipt


def materialize(
    receipt_path, approved_bootstrap_sha, spec_path, spec_sha, environment, principal, output
):
    """Digest-bound CI handoff; never create a fresh clock or foundation receipt."""
    from deploy.performance_trial import write_json

    raw = receipt_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != approved_bootstrap_sha:
        raise ValueError("bootstrap receipt digest differs")
    source = spec_path.read_bytes()
    if hashlib.sha256(source).hexdigest() != spec_sha:
        raise ValueError("spec digest differs")
    receipt = json.loads(raw)
    plan = validate_receipt(receipt, json.loads(source), spec_sha, environment, principal)
    output.mkdir(parents=True, exist_ok=True)
    for name, value in [("runtime-plan.json", plan), ("bootstrap-receipt.json", receipt)]:
        target = output / name
        if target.exists():
            if json.loads(target.read_text()) != value:
                raise ValueError("existing runtime handoff differs")
        else:
            write_json(target, value)
    return plan


def cleanup_operator(
    receipt_path,
    approved_bootstrap_sha,
    spec_path,
    spec_sha,
    environment,
    principal,
    output,
    az,
    rest,
):
    """Revoke only the approved receipt's exact grants after resource absence."""
    from deploy.performance_trial import RemoteError, write_json

    raw = receipt_path.read_bytes()
    spec_raw = spec_path.read_bytes()
    if (
        hashlib.sha256(raw).hexdigest() != approved_bootstrap_sha
        or hashlib.sha256(spec_raw).hexdigest() != spec_sha
    ):
        raise ValueError("operator cleanup specification/receipt digest differs")
    receipt = json.loads(raw)
    plan = validate_receipt(
        receipt, json.loads(spec_raw), spec_sha, environment, principal, allow_expired=True
    )
    for key in ("resource_group", "controller_group"):
        if az("group", "exists", "-n", plan[key]):
            raise ValueError(
                "finish retained evidence and resource cleanup before operator grant removal"
            )
    expected = expected_assignments(plan, receipt["operator_principal"])
    if (
        receipt.get("plan") != plan
        or receipt.get("assignments") != expected
        or receipt.get("status") != "ready"
    ):
        raise ValueError("bootstrap grants differ; reconcile without broad revocation")
    output.mkdir(parents=True, exist_ok=True)
    intent = {
        "spec_sha256": spec_sha,
        "bootstrap_sha256": approved_bootstrap_sha,
        "original_created_at": plan["created_at"],
        "original_expires_at": plan["expires_at"],
        "assignment_ids": [row["id"] for row in expected],
    }
    intent_path = output / "operator-cleanup-intent.json"
    if intent_path.exists() and json.loads(intent_path.read_text()) != intent:
        raise ValueError("operator cleanup intent differs")
    write_json(intent_path, intent)
    for assignment in expected:
        rid = assignment["id"]
        try:
            row = rest.arm("GET", rid, api="2022-04-01").json()
        except RemoteError as error:
            if error.status == 404:
                continue
            raise
        live = {
            "id": row["id"],
            **row["properties"],
            "scope": rid.rsplit("/providers/Microsoft.Authorization/roleAssignments/", 1)[0],
        }
        if assignment_shape(live) != assignment:
            raise ValueError("operator grant drift; refuse revocation")
        rest.arm("DELETE", rid, api="2022-04-01")
        try:
            rest.arm("GET", rid, api="2022-04-01")
        except RemoteError as error:
            if error.status == 404:
                continue
            raise
        raise ValueError("operator grant deletion unconfirmed")
    write_json(
        output / "operator-grants-revoked.json",
        {
            "assignment_ids": [r["id"] for r in expected],
            "absent": True,
            "verified_at": datetime.now(UTC).isoformat(),
        },
    )


def main():
    from deploy.performance_trial import Azure

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["bootstrap", "handoff", "cleanup"])
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--approved-bootstrap-sha256")
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--approved-sha256", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--principal", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.operation == "cleanup":
        from deploy.performance_trial import Rest

        if (
            os.environ.get("GITHUB_ACTIONS")
            or not args.receipt
            or not args.approved_bootstrap_sha256
        ):
            raise ValueError("exact operator receipt cleanup requires the human operator, not CI")
        cleanup_operator(
            args.receipt,
            args.approved_bootstrap_sha256,
            args.spec,
            args.approved_sha256,
            args.environment,
            args.principal,
            args.output,
            Azure(),
            Rest(),
        )
        print(
            "Exact operator grant absence verified; "
            "federation/environment cleanup remains separate."
        )
        return
    if args.operation == "handoff":
        import base64

        from deploy.performance_trial import Rest

        if (
            os.environ.get("GITHUB_REPOSITORY") != "clifguy/cas"
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or not args.receipt
            or not args.approved_bootstrap_sha256
        ):
            raise ValueError("handoff requires committed manual main invocation")
        claims = json.loads(
            base64.urlsafe_b64decode(
                Rest().token("https://management.azure.com/.default").split(".")[1] + "==="
            )
        )
        if claims["oid"] != args.principal:
            raise ValueError("signed-in trial operator differs")
        materialize(
            args.receipt,
            args.approved_bootstrap_sha256,
            args.spec,
            args.approved_sha256,
            args.environment,
            args.principal,
            args.output,
        )
        return
    if os.environ.get("GITHUB_ACTIONS"):
        raise ValueError("one-time bootstrap requires the human operator, not CI")
    bootstrap(
        args.spec, args.approved_sha256, args.environment, args.principal, args.output, Azure()
    )
    print("Scoped bootstrap ready; retain its exact receipt digest for committed CI dispatch.")


if __name__ == "__main__":
    main()
