"""Fail-closed inventory review for the disposable experiment's ARM what-if."""

from __future__ import annotations

import re
from collections import Counter
from urllib.parse import urlsplit

from deploy.performance_trial import ROLE_IDS

ACR, SECRET_ROLE, BLOB = ROLE_IDS["acr"], ROLE_IDS["secret"], ROLE_IDS["blob"]
CONTRIBUTOR = "b24988ac-6180-42a0-ab88-20f7382dd24c"


def inventory(plan, result, parameters, identities):
    """Inspect actual predicted mutations, not just whether ARM accepts the template.

    Untagged role/deployment child objects are checked by exact scope/name and
    role/principal pairs instead. Unknown mutation types or opaque IDs halt.
    """
    if result.get("status") != "Succeeded" or result.get("error"):
        raise ValueError("ARM what-if did not produce a successful inventory")
    p = {k: v["value"] for k, v in parameters["parameters"].items()}
    prefix = "/subscriptions/" + plan["subscription_id"] + "/resourceGroups/"
    groups = {prefix + plan["resource_group"], prefix + plan["controller_group"]}
    trial, controller = prefix + plan["resource_group"], prefix + plan["controller_group"]
    run = re.escape(plan["run_id"])
    patterns = {
        "Microsoft.ManagedIdentity/userAssignedIdentities": (rf"id-perf-(?:pg-)?{run}", 2),
        "Microsoft.Automation/automationAccounts": (rf"aa-perf-{run}", 1),
        "Microsoft.Storage/storageAccounts": (r"stperf[a-z0-9]{13}", 1),
        "Microsoft.Storage/storageAccounts/blobServices": (r"stperf[a-z0-9]{13}/default", 1),
        "Microsoft.Storage/storageAccounts/blobServices/containers": (
            r"stperf[a-z0-9]{13}/default/evidence",
            1,
        ),
    }
    if p["deployStack"]:
        pg = rf"psql-perf-{run}-[a-z0-9]{{13}}"
        patterns.update(
            {
                "Microsoft.Network/virtualNetworks": (rf"vnet-perf-{run}", 1),
                "Microsoft.OperationalInsights/workspaces": (rf"log-perf-{run}", 1),
                "Microsoft.App/managedEnvironments": (rf"cae-perf-{run}", 1),
                "Microsoft.Network/privateDnsZones": (
                    rf"perf-{run}\.private\.postgres\.database\.azure\.com",
                    1,
                ),
                "Microsoft.Network/privateDnsZones/virtualNetworkLinks": (
                    rf"perf-{run}\.private\.postgres\.database\.azure\.com/link-perf-{run}",
                    1,
                ),
                "Microsoft.DBforPostgreSQL/flexibleServers": (pg, 1),
                "Microsoft.DBforPostgreSQL/flexibleServers/configurations": (
                    pg + "/azure.extensions",
                    1,
                ),
                "Microsoft.DBforPostgreSQL/flexibleServers/databases": (pg + "/sage", 1),
                "Microsoft.DBforPostgreSQL/flexibleServers/administrators": (
                    pg + "/" + re.escape(identities["bootstrapPrincipalId"]),
                    1,
                ),
                "Microsoft.App/jobs": (rf"(?:pg-perf|bench)-{run}", 2),
            }
        )
    if p["activateService"]:
        patterns["Microsoft.App/containerApps"] = (rf"ingest-perf-{run}", 1)
        patterns["Microsoft.App/jobs"] = (rf"(?:pg-perf|bench|verify)-{run}", 3)
    deployments = {
        trial: {
            f"perf-identities-{plan['run_id']}",
            f"perf-cleanup-access-{plan['run_id']}",
            f"perf-stack-{plan['run_id']}",
            "perf-postgres",
            "perf-pg-bootstrap",
        },
        controller: {f"perf-cleanup-{plan['run_id']}", f"perf-evidence-access-{plan['run_id']}"},
        prefix + plan["source"]["resource_group"]: {f"perf-source-access-{plan['run_id']}"},
    }
    permitted_roles = set()
    if p["configureAccess"]:
        storage = urlsplit(identities["exportUrl"]).hostname.split(".")[0]
        container = (
            controller
            + "/providers/Microsoft.Storage/storageAccounts/"
            + storage
            + "/blobServices/default/containers/evidence"
        )
        service = identities["servicePrincipalId"]
        bootstrap = identities["bootstrapPrincipalId"]
        permitted_roles = {
            (trial, CONTRIBUTOR, identities["expiryPrincipalId"]),
            (container, BLOB, service),
            (container, BLOB, p["operatorPrincipalId"]),
            (plan["binding"]["registry_id"], ACR, service),
            (plan["binding"]["registry_id"], ACR, bootstrap),
            (plan["binding"]["key_vault_id"] + "/secrets/anthropic-api-key", SECRET_ROLE, service),
        }
    roles = {
        (scope.casefold(), role, principal.casefold()) for scope, role, principal in permitted_roles
    }
    seen_roles = Counter()
    seen = Counter()
    retained = []
    for change in result.get("changes", []):
        action, rid = change["changeType"], change["resourceId"]
        if action in ("Ignore", "NoChange"):
            retained.append({"resource_id": rid, "change_type": action})
            continue
        if action not in ("Create", "Modify", "Deploy") or not rid.startswith(prefix):
            raise ValueError("foreign, destructive or unknown predicted mutation")
        after = change.get("after") or {}
        if rid in groups:
            if (
                after.get("tags", {}).get("benchmarkRun") != plan["run_id"]
                or after.get("tags", {}).get("expiresAt") != plan["expires_at"]
            ):
                raise ValueError("predicted group lacks ownership")
        elif "/providers/Microsoft.Authorization/roleAssignments/" in rid:
            scope, assignment = rid.rsplit("/providers/Microsoft.Authorization/roleAssignments/", 1)
            props = after.get("properties", {})
            pair = (
                scope.casefold(),
                props.get("roleDefinitionId", "").rsplit("/", 1)[-1],
                props.get("principalId", "").casefold(),
            )
            if not re.fullmatch(r"[0-9a-f-]{36}", assignment) or pair not in roles:
                raise ValueError("predicted role scope, privilege or principal is unapproved")
            seen_roles[pair] += 1
            if seen_roles[pair] > 1:
                raise ValueError("extra predicted role assignment")
        else:
            group, separator, resource = rid.partition("/providers/")
            if not separator:
                raise ValueError("unrecognized predicted resource ID")
            parts = resource.split("/")
            kind = parts[0] + "/" + "/".join(parts[1::2])
            name = "/".join(parts[2::2])
            if kind == "Microsoft.Resources/deployments":
                if name not in deployments.get(group, set()):
                    raise ValueError("unapproved nested deployment")
            else:
                allowed = patterns.get(kind)
                control_kind = kind.startswith(("Microsoft.Automation/", "Microsoft.Storage/"))
                expected_group = controller if control_kind else trial
                if group != expected_group or not allowed or not re.fullmatch(allowed[0], name):
                    raise ValueError("extra or unapproved predicted resource")
                props = after.get("properties", {})
                if kind == "Microsoft.App/managedEnvironments":
                    profiles = props.get("workloadProfiles", [])
                    if {(x["name"], x["workloadProfileType"]) for x in profiles} != {
                        ("Consumption", "Consumption"),
                        ("D8", "D8"),
                    } or any(
                        x.get("minimumCount") != 1 or x.get("maximumCount") != 1
                        for x in profiles
                        if x["name"] == "D8"
                    ):
                        raise ValueError("unapproved predicted dedicated-node allocation")
                if kind == "Microsoft.DBforPostgreSQL/flexibleServers":
                    if (
                        after.get("sku", {}).get("name") != "Standard_B2s"
                        or props.get("storage", {}).get("storageSizeGB") != 32
                    ):
                        raise ValueError("unapproved predicted database allocation")
                if kind == "Microsoft.App/containerApps":
                    template = props.get("template", {})
                    containers = template.get("containers", [])
                    if (
                        len(containers) != 1
                        or containers[0].get("image") != plan["candidate"]["image"]
                        or containers[0].get("resources")
                        != {"cpu": int(p["cpu"]), "memory": p["memory"]}
                        or props.get("workloadProfileName") != p["profile"]
                        or template.get("scale", {}).get("minReplicas") != 1
                        or template.get("scale", {}).get("maxReplicas") != 1
                    ):
                        raise ValueError("unapproved predicted service allocation")
                seen[kind] += 1
                if seen[kind] > allowed[1]:
                    raise ValueError("predicted resource count exceeds approved inventory")
                # Container child resources do not support ownership tags.
                if kind not in {
                    "Microsoft.Storage/storageAccounts/blobServices/containers",
                    "Microsoft.Storage/storageAccounts/blobServices",
                    "Microsoft.DBforPostgreSQL/flexibleServers/configurations",
                    "Microsoft.DBforPostgreSQL/flexibleServers/databases",
                    "Microsoft.DBforPostgreSQL/flexibleServers/administrators",
                }:
                    if (
                        after.get("tags", {}).get("benchmarkRun") != plan["run_id"]
                        or after.get("tags", {}).get("expiresAt") != plan["expires_at"]
                    ):
                        raise ValueError("predicted resource lacks ownership")
        retained.append({"resource_id": rid, "change_type": action})
    if not retained:
        raise ValueError("empty what-if inventory cannot authorize provisioning")
    return retained
