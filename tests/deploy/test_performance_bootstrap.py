"""Bootstrap must be real scoped preparation, not an invented foundation receipt."""

import copy
import json
import uuid
from datetime import timedelta

import pytest

from deploy.performance_trial import provision, validate_plan
from tests.deploy.test_performance_trial import NOW, plan

PRINCIPAL = "22222222-2222-2222-2222-222222222222"
ENVIRONMENT = "experiment"


def test_bootstrapped_empty_groups_enter_real_foundation(tmp_path, monkeypatch):
    """Exercise the old refusal through the actual provision entry point."""
    from deploy import performance_trial as trial

    p = validate_plan(plan(), NOW)
    p["candidate"]["abstraction_model"] = "model"
    receipt = {
        "schema_version": 1,
        "plan": p,
        "operator_principal": PRINCIPAL,
        "environment": ENVIRONMENT,
        "spec_sha256": "a" * 64,
        "assignments": [],
        "status": "ready",
    }
    (tmp_path / "bootstrap-receipt.json").write_text(json.dumps(receipt))
    effects = []
    # The bootstrap boundary's live inventory is covered separately below.
    monkeypatch.setattr(
        trial, "verify_bootstrap", lambda *a, **k: effects.append("verify"), raising=False
    )

    def apply(*args, preview=False, **kwargs):
        effects.append((args[2]["parameters"]["deployStack"]["value"], preview))
        return {"properties": {"outputs": {"automationName": {"value": "owned"}}}}

    provision(
        p,
        lambda *a: True,
        None,
        principal=PRINCIPAL,
        ingress_key="key",
        output=tmp_path,
        precheck=lambda *a: None,
        apply=apply,
        enable=lambda *a: effects.append("expiry") or {"verified": True},
    )
    assert effects == [
        "verify",
        (False, True),
        (False, False),
        (False, True),
        (False, False),
        "expiry",
        (True, True),
        (True, False),
    ]
    assert json.loads((tmp_path / "provision-receipt.json").read_text())["phase"] == "stack_created"


def test_unreceipted_groups_still_refused(tmp_path):
    p = validate_plan(plan(), NOW)
    p["candidate"]["abstraction_model"] = "model"
    with pytest.raises(ValueError, match="partial foundation"):
        provision(
            p,
            lambda *a: True,
            None,
            principal=PRINCIPAL,
            ingress_key="key",
            output=tmp_path,
            precheck=lambda *a: None,
            apply=lambda *a, **k: pytest.fail("must not apply"),
        )


class Cloud:
    """Effect recorder for actual bootstrap, inventory and conditional role checks."""

    def __init__(self, output):
        self.output = output
        self.groups = {}
        self.resources = {}
        self.assignments = []
        self.definitions = {}
        self.effects = []
        self.fail_assignment = False

    def read_role_definition(self, definition):
        rows = self.definitions.get(definition["Id"], [])
        assert len(rows) <= 1
        return copy.deepcopy(rows[0]) if rows else None

    def __call__(self, *args):
        from deploy.performance_bootstrap import assignment_shape

        def value(flag):
            return args[args.index(flag) + 1]

        if args[:2] == ("account", "show"):
            return {"id": "11111111-1111-1111-1111-111111111111", "tenantId": "tenant"}
        if args[:3] == ("ad", "sp", "show"):
            return {"id": PRINCIPAL, "appId": "app", "servicePrincipalType": "Application"}
        if args[:4] == ("ad", "app", "federated-credential", "list"):
            return [
                {
                    "issuer": "https://token.actions.githubusercontent.com",
                    "subject": "repo:clifguy/cas:environment:" + ENVIRONMENT,
                    "audiences": ["api://AzureADTokenExchange"],
                }
            ]
        if args[:2] == ("group", "exists"):
            return value("-n") in self.groups
        if args[:2] == ("group", "show"):
            return copy.deepcopy(self.groups[value("-n")])
        if args[:2] == ("resource", "list"):
            return copy.deepcopy(self.resources.get(value("-g"), []))
        if args[:3] == ("role", "definition", "list"):
            return copy.deepcopy(self.definitions.get(value("--name"), []))
        if args[:3] == ("role", "assignment", "list"):
            return copy.deepcopy(self.assignments)
        # Every effect must occur after durable intent. Do not let a mock omit this.
        receipt = json.loads((self.output / "bootstrap-receipt.json").read_text())
        assert receipt["status"] == "intent"
        self.effects.append(args)
        if args[:2] == ("group", "create"):
            name = value("-n")
            tags = dict(a.split("=", 1) for a in args[args.index("--tags") + 1 :])
            self.groups[name] = {
                "id": "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/" + name,
                "name": name,
                "location": value("-l"),
                "tags": tags,
            }
            return self.groups[name]
        if args[:3] == ("role", "definition", "create"):
            definition = json.loads(value("--role-definition"))
            generated = str(uuid.uuid4())
            row = {
                "id": "/subscriptions/11111111-1111-1111-1111-111111111111"
                + "/providers/Microsoft.Authorization/roleDefinitions/"
                + generated,
                "name": generated,
                "roleName": definition["Name"],
                "permissions": [
                    {
                        k[0].lower() + k[1:]: definition[k]
                        for k in ("Actions", "NotActions", "DataActions", "NotDataActions")
                    }
                ],
                "assignableScopes": definition["AssignableScopes"],
                "roleType": "CustomRole",
            }
            row["permissions"][0].update(condition=None, conditionVersion=None)
            self.definitions[generated] = [row]
            return row
        if args[:1] == ("rest",) and value("--method") == "put":
            url = value("--url")
            assert url.startswith("https://management.azure.com/subscriptions/")
            rid, query = url.removeprefix("https://management.azure.com").split("?")
            assert query == "api-version=2022-04-01"
            body = json.loads(value("--body"))
            properties = body["properties"]
            row = {
                "id": rid,
                "name": rid.rsplit("/", 1)[1],
                "roleName": properties["roleName"],
                "permissions": properties["permissions"],
                "assignableScopes": properties["assignableScopes"],
                "roleType": properties["type"],
            }
            row["permissions"][0].update(condition=None, conditionVersion=None)
            self.definitions[row["name"]] = [copy.deepcopy(row)]
            return {"id": rid, "name": row["name"], "properties": copy.deepcopy(properties)}
        if args[:3] == ("role", "assignment", "create"):
            if self.fail_assignment:
                raise TimeoutError("uncertain assignment")
            row = {
                "id": value("--scope")
                + "/providers/Microsoft.Authorization/roleAssignments/"
                + value("--name"),
                "scope": value("--scope"),
                "roleDefinitionId": value("--role"),
                "principalId": value("--assignee-object-id"),
                "principalType": value("--assignee-principal-type"),
                "condition": value("--condition") if "--condition" in args else None,
                "conditionVersion": "2.0" if "--condition" in args else None,
            }
            self.assignments.append(row)
            return assignment_shape(row)
        raise AssertionError(args)


def bootstrap_input(tmp_path):
    import hashlib

    raw_plan = plan()
    raw_plan.pop("created_at")
    raw_plan.pop("expires_at")
    raw_plan["candidate"]["abstraction_model"] = "model"
    source = tmp_path / "spec.json"
    source.write_text(json.dumps(raw_plan))
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    output = tmp_path / "bootstrap"
    cloud = Cloud(output)
    return source, sha, output, cloud


@pytest.fixture
def bootstrapped(tmp_path):
    from deploy.performance_bootstrap import bootstrap

    source, sha, output, cloud = bootstrap_input(tmp_path)
    receipt = bootstrap(
        source, sha, ENVIRONMENT, PRINCIPAL, output, cloud, role_reader=cloud.read_role_definition
    )
    return source, sha, output, cloud, receipt


def test_actual_bootstrap_is_scoped_and_repeat_preserves_clock(bootstrapped):
    from deploy.performance_bootstrap import CONTRIBUTOR, RBAC, bootstrap
    from deploy.performance_trial import utc

    source, sha, output, cloud, receipt = bootstrapped
    assert len(cloud.groups) == 2
    assert all(not cloud.resources.get(g) for g in cloud.groups)
    assert receipt["status"] == "ready"
    assert len(receipt["assignments"]) == 13
    for row in receipt["assignments"]:
        if row["roleDefinitionId"].endswith((CONTRIBUTOR, RBAC)):
            assert row["scope"] != "/subscriptions/" + receipt["plan"]["subscription_id"]
        if row["roleDefinitionId"].endswith(RBAC):
            assert row["condition"] and row["conditionVersion"] == "2.0"
    before = len(cloud.effects)
    result = bootstrap(
        source,
        sha,
        ENVIRONMENT,
        PRINCIPAL,
        output,
        cloud,
        now=utc(receipt["plan"]["created_at"]) + timedelta(minutes=5),
        role_reader=cloud.read_role_definition,
    )
    assert result == receipt
    assert len(cloud.effects) == before
    assert not (output / "provision-receipt.json").exists()


@pytest.mark.parametrize(
    "mutation",
    ["foreign-group", "resource", "role-extra", "role-condition", "definition", "group-extra-role"],
)
def test_live_bootstrap_drift_blocks_real_provision_before_any_apply(bootstrapped, mutation):
    from deploy.performance_bootstrap import RBAC

    _, _, output, cloud, receipt = bootstrapped
    p = receipt["plan"]
    group = p["resource_group"]
    if mutation == "foreign-group":
        cloud.groups[group]["tags"]["benchmarkRun"] = "foreign"
    elif mutation == "resource":
        cloud.resources[group] = [
            {
                "id": cloud.groups[group]["id"] + "/providers/Microsoft.App/jobs/foreign",
                "tags": cloud.groups[group]["tags"],
            }
        ]
    elif mutation == "role-extra":
        extra = copy.deepcopy(cloud.assignments[0])
        extra["id"] += "x"
        cloud.assignments.append(extra)
    elif mutation == "role-condition":
        next(r for r in cloud.assignments if r["roleDefinitionId"].endswith(RBAC))["condition"] = (
            None
        )
    elif mutation == "definition":
        next(iter(cloud.definitions.values()))[0]["permissions"][0]["actions"].append("*")
    else:
        extra = copy.deepcopy(
            next(r for r in cloud.assignments if r["scope"] == cloud.groups[group]["id"])
        )
        extra["id"] += "x"
        extra["principalId"] = "foreign"
        cloud.assignments.append(extra)
    before = len(cloud.effects)
    with pytest.raises(ValueError):
        provision(
            p,
            cloud,
            None,
            principal=PRINCIPAL,
            ingress_key="key",
            output=output,
            precheck=lambda *a: None,
            apply=lambda *a, **k: pytest.fail("unsafe apply"),
        )
    assert len(cloud.effects) == before
    assert not (output / "provision-receipt.json").exists()


def test_handoff_preserves_original_plan_and_creates_no_foundation_receipt(bootstrapped, tmp_path):
    import hashlib

    from deploy.performance_bootstrap import materialize

    source, sha, output, _, receipt = bootstrapped
    path = output / "bootstrap-receipt.json"
    dest = tmp_path / "ci"
    result = materialize(
        path,
        hashlib.sha256(path.read_bytes()).hexdigest(),
        source,
        sha,
        ENVIRONMENT,
        PRINCIPAL,
        dest,
    )
    assert result == receipt["plan"]
    assert json.loads((dest / "runtime-plan.json").read_text()) == receipt["plan"]
    assert (dest / "bootstrap-receipt.json").read_bytes() == path.read_bytes()
    assert not (dest / "provision-receipt.json").exists()


@pytest.mark.parametrize(
    "field",
    [
        "principal",
        "environment",
        "spec",
        "source-bytes",
        "receipt-digest",
        "clock",
        "assignments",
        "partial",
        "expired",
    ],
)
def test_tampered_or_partial_handoff_writes_nothing(bootstrapped, tmp_path, field):
    import hashlib
    from datetime import UTC, datetime

    from deploy.performance_bootstrap import materialize

    source, sha, output, _, receipt = bootstrapped
    principal, environment = PRINCIPAL, ENVIRONMENT
    path = output / "bootstrap-receipt.json"
    if field == "principal":
        principal = "33333333-3333-3333-3333-333333333333"
    if field == "environment":
        environment = "production"
    if field == "spec":
        sha = "f" * 64
    if field == "source-bytes":
        source.write_bytes(source.read_bytes() + b"\n")
    if field == "clock":
        receipt["plan"]["expires_at"] = (datetime.now(UTC) + timedelta(hours=5)).isoformat()
    if field == "assignments":
        receipt["assignments"].pop()
    if field == "partial":
        receipt["status"] = "intent"
    if field == "expired":
        receipt["plan"]["created_at"] = NOW.isoformat()
        receipt["plan"]["expires_at"] = (NOW + timedelta(seconds=1)).isoformat()
    path.write_text(json.dumps(receipt))
    digest = (
        hashlib.sha256(path.read_bytes()).hexdigest() if field != "receipt-digest" else "f" * 64
    )
    dest = tmp_path / "ci"
    with pytest.raises(ValueError):
        materialize(path, digest, source, sha, environment, principal, dest)
    assert not dest.exists()


def test_partial_assignment_is_retained_and_never_replayed(bootstrapped, tmp_path):
    from deploy.performance_bootstrap import bootstrap

    source, sha, _, _, _ = bootstrapped
    output = tmp_path / "uncertain"
    cloud = Cloud(output)
    cloud.fail_assignment = True
    with pytest.raises(TimeoutError):
        bootstrap(
            source,
            sha,
            ENVIRONMENT,
            PRINCIPAL,
            output,
            cloud,
            role_reader=cloud.read_role_definition,
        )
    assert json.loads((output / "bootstrap-receipt.json").read_text())["status"] == "intent"
    before = len(cloud.effects)
    with pytest.raises(ValueError, match="readiness"):
        bootstrap(
            source,
            sha,
            ENVIRONMENT,
            PRINCIPAL,
            output,
            cloud,
            role_reader=cloud.read_role_definition,
        )
    assert len(cloud.effects) == before


def test_verified_bootstrap_reaches_foundation_and_real_expiry_gate(bootstrapped):
    _, _, output, cloud, receipt = bootstrapped
    effects = []

    def apply(p, az, parameters, path, preview=False, **kwargs):
        effects.append((parameters["parameters"]["deployStack"]["value"], preview))
        return {"properties": {"outputs": {"automationName": {"value": "owned"}}}}

    def expiry(*a):
        effects.append("expiry-refused")
        raise ValueError("expiry unavailable")

    with pytest.raises(ValueError, match="expiry unavailable"):
        provision(
            receipt["plan"],
            cloud,
            None,
            principal=PRINCIPAL,
            ingress_key="key",
            output=output,
            precheck=lambda *a: None,
            apply=apply,
            enable=expiry,
        )
    assert effects == [
        (False, True),
        (False, False),
        (False, True),
        (False, False),
        "expiry-refused",
    ]
    assert not (output / "expiry-receipt.json").exists()


@pytest.mark.parametrize("bad", ["repository", "ref", "event", "principal", "digest"])
def test_actual_handoff_cli_refuses_wrong_invocation(bootstrapped, tmp_path, monkeypatch, bad):
    import base64
    import hashlib
    import sys

    from deploy import performance_bootstrap as module
    from deploy import performance_trial as trial

    source, sha, output, _, _ = bootstrapped
    path = output / "bootstrap-receipt.json"
    dest = tmp_path / "cli"
    for k, v in {
        "GITHUB_REPOSITORY": "clifguy/cas",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
    }.items():
        monkeypatch.setenv(k, v)
    for label, key in [
        ("repository", "GITHUB_REPOSITORY"),
        ("ref", "GITHUB_REF"),
        ("event", "GITHUB_EVENT_NAME"),
    ]:
        if bad == label:
            monkeypatch.setenv(key, "wrong")
    claims = {"oid": PRINCIPAL if bad != "principal" else "wrong"}
    token = "x." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".x"
    monkeypatch.setattr(trial.Rest, "token", lambda *a: token)
    digest = hashlib.sha256(path.read_bytes()).hexdigest() if bad != "digest" else "f" * 64
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bootstrap",
            "handoff",
            "--spec",
            str(source),
            "--approved-sha256",
            sha,
            "--receipt",
            str(path),
            "--approved-bootstrap-sha256",
            digest,
            "--environment",
            ENVIRONMENT,
            "--principal",
            PRINCIPAL,
            "--output",
            str(dest),
        ],
    )
    with pytest.raises(ValueError):
        module.main()
    assert not dest.exists()


def test_production_federation_refused_before_bootstrap_effects(bootstrapped, tmp_path):
    from deploy.performance_bootstrap import bootstrap

    source, sha, _, _, _ = bootstrapped
    output = tmp_path / "wrong-federation"
    cloud = Cloud(output)

    def az(*args):
        if args[:4] == ("ad", "app", "federated-credential", "list"):
            return [
                {
                    "issuer": "https://token.actions.githubusercontent.com",
                    "subject": "repo:clifguy/cas:environment:cor-prod",
                    "audiences": ["api://AzureADTokenExchange"],
                }
            ]
        return cloud(*args)

    with pytest.raises(ValueError, match="federation"):
        bootstrap(
            source, sha, ENVIRONMENT, PRINCIPAL, output, az, role_reader=cloud.read_role_definition
        )
    assert cloud.effects == []
    assert not (output / "bootstrap-receipt.json").exists()


def test_actual_effects_do_not_grant_custom_subscription_writes(bootstrapped):
    """Inspect actual custom definitions and assignments against an independent policy oracle."""
    _, _, _, cloud, _ = bootstrapped
    allowed_deployment_actions = {
        "microsoft.resources/deployments/delete",
        "microsoft.resources/deployments/operations/read",
        "microsoft.resources/deployments/operationstatuses/read",
        "microsoft.resources/deployments/read",
        "microsoft.resources/deployments/validate/action",
        "microsoft.resources/deployments/whatif/action",
        "microsoft.resources/deployments/write",
    }
    for row in cloud.assignments:
        if "/resourceGroups/" in row["scope"]:
            continue
        definition = cloud.definitions[row["roleDefinitionId"].rsplit("/", 1)[1]][0]
        for permission in definition["permissions"]:
            assert permission["dataActions"] == []
            assert permission["notActions"] == []
            assert permission["notDataActions"] == []
            for action in permission["actions"]:
                assert "*" not in action
                assert (
                    action.lower().endswith("/read") or action.lower() in allowed_deployment_actions
                )
    # This asserts both request and resource restrictions; Azure evaluates the
    # condition language on deploy, not this local effect recorder.
    from deploy.performance_bootstrap import RBAC

    for row in cloud.assignments:
        if row["roleDefinitionId"].endswith(RBAC):
            assert (
                "@Request[Microsoft.Authorization/roleAssignments:PrincipalType]"
                in row["condition"]
            )
            assert (
                "@Resource[Microsoft.Authorization/roleAssignments:PrincipalType]"
                in row["condition"]
            )
            assert "ServicePrincipal" in row["condition"]
            assert row["conditionVersion"] == "2.0"

    # Resolve the emitted custom roles by their labels, then compare every grant
    # against the independently enumerated scope/role policy below.
    import re

    definitions = [
        {
            "Name": body["properties"]["roleName"],
            "Id": effect[effect.index("--url") + 1].split("?")[0].rsplit("/", 1)[1],
            "IsCustom": body["properties"]["type"] == "CustomRole",
            "AssignableScopes": body["properties"]["assignableScopes"],
            **{
                key[0].upper() + key[1:]: value
                for key, value in body["properties"]["permissions"][0].items()
            },
        }
        for effect in cloud.effects
        if effect[:1] == ("rest",) and effect[effect.index("--method") + 1] == "put"
        for body in [json.loads(effect[effect.index("--body") + 1])]
    ]
    custom = {d["Name"].split(" (")[0]: d for d in definitions}
    assert set(custom) == {
        "CAS performance orchestrator",
        "CAS performance reference-reader",
        "CAS performance quota-reader",
    }
    p = bootstrapped[4]["plan"]
    sub = "/subscriptions/" + p["subscription_id"]
    source_group = sub + "/resourceGroups/" + p["source"]["resource_group"]
    source_app = source_group + "/providers/Microsoft.App/containerApps/" + p["source"]["app"]
    orchestrator = custom["CAS performance orchestrator"]
    reference = custom["CAS performance reference-reader"]
    quota = custom["CAS performance quota-reader"]
    assert set(orchestrator["Actions"]) == {
        "Microsoft.Resources/deployments/" + suffix
        for suffix in (
            "delete",
            "operations/read",
            "operationstatuses/read",
            "read",
            "validate/action",
            "whatIf/action",
            "write",
        )
    }
    assert set(reference["Actions"]) == {
        "Microsoft.App/containerApps/listSecrets/action",
        "Microsoft.App/containerApps/read",
    }
    assert set(quota["Actions"]) == {
        "Microsoft.App/locations/usages/read",
        "Microsoft.App/locations/availableManagedEnvironmentsWorkloadProfileTypes/read",
        "Microsoft.DBforPostgreSQL/locations/capabilities/read",
        "Microsoft.Resources/subscriptions/read",
        "Microsoft.Resources/subscriptions/providers/read",
        "Microsoft.Authorization/roleAssignments/read",
        "Microsoft.Authorization/roleDefinitions/read",
    }
    for definition, scope in [(orchestrator, sub), (reference, source_group), (quota, sub)]:
        assert definition["AssignableScopes"] == [scope]
        assert definition["IsCustom"] is True
        assert definition["NotActions"] == []
        assert definition["DataActions"] == []
        assert definition["NotDataActions"] == []
    contributor = "b24988ac-6180-42a0-ab88-20f7382dd24c"
    rbac = "f58310d9-a9f6-439a-9e8d-f62e7b41a168"
    reader = "acdd72a7-3385-48ef-bd42-f606fba81ae7"
    acr_pull = "7f951dda-4ed3-4680-a7ca-43fe172d538d"
    vault_user = "4633458b-17de-408a-b874-0445c86b69e6"
    blob = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
    registry, vault = p["binding"]["registry_id"], p["binding"]["key_vault_id"]
    expected = [
        (sub, orchestrator["Id"], None),
        (sub, quota["Id"], None),
        (source_group, orchestrator["Id"], None),
        (source_app, reference["Id"], None),
        (registry, reader, None),
        (registry, acr_pull, None),
        (registry, rbac, acr_pull),
        (vault, reader, None),
        (vault + "/secrets/anthropic-api-key", rbac, vault_user),
        (sub + "/resourceGroups/" + p["resource_group"], contributor, None),
        (sub + "/resourceGroups/" + p["resource_group"], rbac, contributor),
        (sub + "/resourceGroups/" + p["controller_group"], contributor, None),
        (sub + "/resourceGroups/" + p["controller_group"], rbac, blob),
    ]
    observed = []
    for row in cloud.assignments:
        role = row["roleDefinitionId"].rsplit("/", 1)[1]
        assert row["principalId"] == PRINCIPAL
        assert row["principalType"] == "ServicePrincipal"
        permitted = None
        if role == rbac:
            ids = re.findall(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", row["condition"])
            assert len(ids) == 2 and ids[0] == ids[1]
            permitted = ids[0]
            bindings = re.findall(
                r"@(Request|Resource)\[Microsoft.Authorization/roleAssignments:RoleDefinitionId\]"
                r"\s+ForAnyOfAnyValues:GuidEquals\s+\{([^}]*)\}",
                row["condition"],
            )
            assert [(kind, values.strip()) for kind, values in bindings] == [
                ("Request", permitted),
                ("Resource", permitted),
            ]
            # This complete policy expression is independent of the production
            # condition builder; token presence cannot constrain Boolean logic.
            expected_condition = (
                "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR "
                "(@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] "
                "ForAnyOfAnyValues:GuidEquals {" + permitted + "} AND "
                "@Request[Microsoft.Authorization/roleAssignments:PrincipalType] "
                "ForAnyOfAnyValues:StringEqualsIgnoreCase {'ServicePrincipal'})) AND "
                "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR "
                "(@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] "
                "ForAnyOfAnyValues:GuidEquals {" + permitted + "} AND "
                "@Resource[Microsoft.Authorization/roleAssignments:PrincipalType] "
                "ForAnyOfAnyValues:StringEqualsIgnoreCase {'ServicePrincipal'}))"
            )
            assert row["condition"] == expected_condition
        else:
            assert row["condition"] is None and row["conditionVersion"] is None
        observed.append((row["scope"], role, permitted))
    assert sorted(observed, key=repr) == sorted(expected, key=repr)


@pytest.mark.parametrize(
    "drift",
    [
        None,
        "actions",
        "notActions",
        "dataActions",
        "notDataActions",
        "condition",
        "conditionVersion",
        "roleType",
        "scope",
        "id",
        "name",
        "roleName",
    ],
)
def test_existing_role_uses_real_azure_shape_and_rejects_privilege_drift(
    bootstrapped, tmp_path, drift
):
    from deploy.performance_bootstrap import bootstrap

    source, sha, _, original, _ = bootstrapped
    output = tmp_path / "existing-role"
    cloud = Cloud(output)
    cloud.definitions = copy.deepcopy(original.definitions)
    row = next(iter(cloud.definitions.values()))[0]
    if drift in ("actions", "notActions", "dataActions", "notDataActions"):
        row["permissions"][0][drift].append("*")
    elif drift in ("condition", "conditionVersion"):
        row["permissions"][0][drift] = "unexpected"
    elif drift == "roleType":
        row["roleType"] = "BuiltInRole"
    elif drift == "scope":
        row["assignableScopes"] = ["/foreign"]
    elif drift in ("id", "name", "roleName"):
        row[drift] = "foreign"
    if drift:
        with pytest.raises(ValueError, match="existing custom role"):
            bootstrap(
                source,
                sha,
                ENVIRONMENT,
                PRINCIPAL,
                output,
                cloud,
                role_reader=cloud.read_role_definition,
            )
        assert cloud.assignments == []
        assert json.loads((output / "bootstrap-receipt.json").read_text())["status"] == "intent"
    else:
        result = bootstrap(
            source,
            sha,
            ENVIRONMENT,
            PRINCIPAL,
            output,
            cloud,
            role_reader=cloud.read_role_definition,
        )
        assert result["status"] == "ready"
        assert len(cloud.assignments) == 13


def test_bootstrap_role_ids_match_every_assignment_and_readback(tmp_path):
    from deploy.performance_bootstrap import bootstrap

    source, sha, output, cloud = bootstrap_input(tmp_path)
    receipt = bootstrap(
        source, sha, ENVIRONMENT, PRINCIPAL, output, cloud, role_reader=cloud.read_role_definition
    )
    puts = [effect for effect in cloud.effects if effect[:1] == ("rest",)]
    assert len(puts) == 3
    assert not any(effect[:3] == ("role", "definition", "create") for effect in cloud.effects)
    for effect in puts:
        rid = (
            effect[effect.index("--url") + 1]
            .split("?")[0]
            .removeprefix("https://management.azure.com")
        )
        role = rid.rsplit("/", 1)[1]
        row = cloud.definitions[role][0]
        assert row["id"] == rid and row["name"] == role
        assert any(assignment["roleDefinitionId"] == rid for assignment in cloud.assignments)
    assert receipt["status"] == "ready"


@pytest.mark.parametrize("phase", ["response", "readback"])
@pytest.mark.parametrize("drift", ["id", "name", "roleName", "actions", "scope"])
def test_created_role_drift_blocks_assignments_and_ready_receipt(tmp_path, phase, drift):
    from deploy.performance_bootstrap import bootstrap

    source, sha, output, cloud = bootstrap_input(tmp_path)

    def change(row):
        if drift in ("id", "name"):
            row[drift] = "foreign"
        else:
            props = row.get("properties", row)
            if drift == "roleName":
                props["roleName"] = "foreign"
            elif drift == "actions":
                props["permissions"][0]["actions"].append("*")
            else:
                props["assignableScopes"] = ["/foreign"]

    def az(*args):
        result = cloud(*args)
        if args[:1] == ("rest",):
            if phase == "response":
                change(result)
            else:
                change(next(iter(cloud.definitions.values()))[0])
        return result

    with pytest.raises(ValueError, match="custom role"):
        bootstrap(
            source, sha, ENVIRONMENT, PRINCIPAL, output, az, role_reader=cloud.read_role_definition
        )
    assert any(effect[:1] == ("rest",) for effect in cloud.effects)
    assert cloud.assignments == []
    assert json.loads((output / "bootstrap-receipt.json").read_text())["status"] == "intent"


@pytest.mark.parametrize("drift", [False, True])
def test_omitted_listing_never_overwrites_existing_role(bootstrapped, tmp_path, drift):
    from deploy.performance_bootstrap import bootstrap

    source, sha, _, original, _ = bootstrapped
    output = tmp_path / "omitted-listing"
    cloud = Cloud(output)
    cloud.definitions = copy.deepcopy(original.definitions)
    role = next(iter(cloud.definitions))
    if drift:
        cloud.definitions[role][0]["permissions"][0]["actions"] = [
            "Microsoft.Resources/deployments/read"
        ]
    before = copy.deepcopy(cloud.definitions)

    def az(*args):
        if args[:3] == ("role", "definition", "list") and args[-1] == role:
            return []
        return cloud(*args)

    if drift:
        assert az("role", "definition", "list", "--name", role) == []
        with pytest.raises(ValueError, match="existing custom role"):
            bootstrap(
                source,
                sha,
                ENVIRONMENT,
                PRINCIPAL,
                output,
                az,
                role_reader=cloud.read_role_definition,
            )
        assert cloud.assignments == []
        assert json.loads((output / "bootstrap-receipt.json").read_text())["status"] == "intent"
    else:
        # Hide only the initial list; final access verification still needs a complete inventory.
        def visible_az(*args):
            if args[:3] == ("role", "definition", "list") and not cloud.assignments:
                return []
            return cloud(*args)

        assert visible_az("role", "definition", "list", "--name", role) == []
        result = bootstrap(
            source,
            sha,
            ENVIRONMENT,
            PRINCIPAL,
            output,
            visible_az,
            role_reader=cloud.read_role_definition,
        )
        assert result["status"] == "ready" and len(cloud.assignments) == 13
    assert cloud.definitions == before
    assert not any(effect[:1] == ("rest",) for effect in cloud.effects)


@pytest.mark.parametrize("error", ["forbidden", "server", "transport"])
def test_unknown_role_read_stops_before_role_writes(tmp_path, error):
    from deploy.performance_bootstrap import bootstrap
    from deploy.performance_trial import RemoteError

    source, sha, output, cloud = bootstrap_input(tmp_path)
    failure = (
        TimeoutError("transport")
        if error == "transport"
        else RemoteError(403 if error == "forbidden" else 500)
    )

    def unreadable(definition):
        raise failure

    with pytest.raises(type(failure)):
        bootstrap(source, sha, ENVIRONMENT, PRINCIPAL, output, cloud, role_reader=unreadable)
    assert cloud.assignments == [] and cloud.definitions == {}
    assert not any(effect[:1] == ("rest",) for effect in cloud.effects)
    assert json.loads((output / "bootstrap-receipt.json").read_text())["status"] == "intent"


@pytest.mark.parametrize("status", [200, 404, 403, 500, "timeout"])
def test_exact_role_get_only_treats_not_found_as_absent(monkeypatch, status):
    import httpx

    from deploy import performance_trial as trial
    from deploy.performance_bootstrap import read_role_definition

    definition = {
        "Id": "11111111-1111-1111-1111-111111111111",
        "AssignableScopes": ["/subscriptions/22222222-2222-2222-2222-222222222222"],
    }
    seen = []
    body = {"id": "stored-role"}

    def respond(request):
        seen.append(request)
        if status == "timeout":
            raise httpx.ReadTimeout("unknown", request=request)
        return httpx.Response(status, json=body)

    rest = trial.Rest()
    rest.client.close()
    rest.client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(rest, "token", lambda scope: "fake-token")
    monkeypatch.setattr(trial, "Rest", lambda: rest)
    try:
        if status in (200, 404):
            assert read_role_definition(definition) == (body if status == 200 else None)
        elif status == "timeout":
            with pytest.raises(httpx.ReadTimeout):
                read_role_definition(definition)
        else:
            with pytest.raises(trial.RemoteError) as failure:
                read_role_definition(definition)
            assert failure.value.status == status
        assert len(seen) == 1
        assert seen[0].method == "GET"
        assert str(seen[0].url) == (
            "https://management.azure.com/subscriptions/22222222-2222-2222-2222-222222222222"
            "/providers/Microsoft.Authorization/roleDefinitions/11111111-1111-1111-1111-111111111111"
            "?api-version=2022-04-01"
        )
    finally:
        rest.client.close()


@pytest.mark.parametrize("body", [None, [], False, 17, "invalid"])
@pytest.mark.parametrize("phase", ["initial", "readback"])
def test_malformed_success_role_reads_do_not_authorize_writes(tmp_path, monkeypatch, body, phase):
    import httpx

    from deploy import performance_trial as trial
    from deploy.performance_bootstrap import bootstrap

    source, sha, output, cloud = bootstrap_input(tmp_path)
    seen = []

    def respond(request):
        seen.append(request)
        if phase == "readback" and len(seen) == 1:
            return httpx.Response(404, json={"error": {"code": "RoleDefinitionDoesNotExist"}})
        return httpx.Response(200, content=json.dumps(body).encode())

    rest = trial.Rest()
    rest.client.close()
    rest.client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(rest, "token", lambda scope: "fake-token")
    monkeypatch.setattr(trial, "Rest", lambda: rest)
    try:
        with pytest.raises(ValueError, match="invalid.*response"):
            bootstrap(source, sha, ENVIRONMENT, PRINCIPAL, output, cloud)
        assert len(seen) == (1 if phase == "initial" else 2)
        assert all(request.method == "GET" for request in seen)
        assert len(cloud.definitions) == (0 if phase == "initial" else 1)
        assert cloud.assignments == []
        puts = [effect for effect in cloud.effects if effect[:1] == ("rest",)]
        assert len(puts) == (0 if phase == "initial" else 1)
        assert json.loads((output / "bootstrap-receipt.json").read_text())["status"] == "intent"
    finally:
        rest.client.close()


@pytest.mark.parametrize("existing", [False, True])
def test_default_http_reader_creates_or_reuses_exact_roles(
    bootstrapped, tmp_path, monkeypatch, existing
):
    import httpx

    from deploy import performance_trial as trial
    from deploy.performance_bootstrap import bootstrap

    source, sha, _, original, _ = bootstrapped
    output = tmp_path / "default-reader"
    cloud = Cloud(output)
    if existing:
        cloud.definitions = copy.deepcopy(original.definitions)
    before = copy.deepcopy(cloud.definitions)
    seen = []

    def respond(request):
        seen.append(request)
        role = request.url.path.rsplit("/", 1)[1]
        rows = cloud.definitions.get(role, [])
        if not rows:
            return httpx.Response(404, json={"error": {"code": "RoleDefinitionDoesNotExist"}})
        row = rows[0]
        return httpx.Response(
            200,
            json={
                "id": row["id"],
                "name": row["name"],
                "properties": {
                    "roleName": row["roleName"],
                    "type": row["roleType"],
                    "assignableScopes": row["assignableScopes"],
                    "permissions": row["permissions"],
                },
            },
        )

    rest = trial.Rest()
    rest.client.close()
    rest.client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(rest, "token", lambda scope: "fake-token")
    monkeypatch.setattr(trial, "Rest", lambda: rest)
    try:
        receipt = bootstrap(source, sha, ENVIRONMENT, PRINCIPAL, output, cloud)
        assert receipt["status"] == "ready" and len(cloud.assignments) == 13
        assert len(seen) == (3 if existing else 6)
        assert all(request.method == "GET" for request in seen)
        puts = [effect for effect in cloud.effects if effect[:1] == ("rest",)]
        assert len(puts) == (0 if existing else 3)
        if existing:
            assert cloud.definitions == before
        for rows in cloud.definitions.values():
            assert any(
                assignment["roleDefinitionId"] == rows[0]["id"] for assignment in cloud.assignments
            )
    finally:
        rest.client.close()
