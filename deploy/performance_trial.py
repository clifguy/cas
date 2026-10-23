"""Opt-in isolated Azure performance-stack controller; never a production deploy.

All Azure writes require a validated plan, an exact approved-manifest digest,
current external preconditions and a separately running expiry controller.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from deploy.performance_limits import remaining

ROOT = Path(__file__).resolve().parents[1]
AZURE_CLI = shutil.which("az")
GITHUB_CLI = shutil.which("gh")
ROLE_IDS = {
    "acr": "7f951dda-4ed3-4680-a7ca-43fe172d538d",
    "secret": "4633458b-17de-408a-b874-0445c86b69e6",  # gitleaks:allow -- public Azure role ID
    "blob": "ba92f5b4-2d11-453d-a403-e96b0029c9fe",
}


class RemoteError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__("Scoped remote request failed: " + str(status))


def utc(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result.astimezone(UTC)


def validate_plan(plan, now=None, *, allow_expired=False):
    """Validate and derive all mutable coordinates; never trust supplied target names."""
    from deploy.performance_archive import public_key

    p = json.loads(json.dumps(plan))
    public_key(p["evidence_public_key_pem"])
    now = now or datetime.now(UTC)
    run = p["run_id"]
    if p.get("schema_version") != 1 or not re.fullmatch(r"[a-z][a-z0-9-]{2,19}", run):
        raise ValueError("invalid bounded run identity")
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", p["subscription_id"]):
        raise ValueError("invalid subscription identity")
    created, expiry = utc(p["created_at"]), utc(p["expires_at"])
    if (
        created > now
        or (not allow_expired and now >= expiry)
        or not 0 < (expiry - created).total_seconds() <= 14400
    ):
        raise ValueError("expired, future or overlong trial")
    limits = p["limits"]
    for key, maximum in [
        ("window_seconds", 14400),
        ("per_run_seconds", 900),
        ("incremental_usd", 50),
        ("provider_calls", 30),
    ]:
        value = limits[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
            raise ValueError("invalid or expanded trial limit")
    if (
        type(limits["provider_calls"]) is not int or type(limits["per_run_seconds"]) is not int
    ) or (expiry - created).total_seconds() > limits["window_seconds"]:
        raise ValueError("invalid lifetime or provider limit")
    derived = {
        "resource_group": "rg-ingest-perf-" + run,
        "controller_group": "rg-ingest-perf-control-" + run,
        "vault_id": "perf_" + run.replace("-", "_"),
        "vault_source_root": "performance/" + run,
    }
    for key, value in derived.items():
        if key in p and p[key] != value:
            raise ValueError("supplied coordinate differs from derived isolated target")
        p[key] = value
    if p["source"]["resource_group"].casefold() in (
        p["resource_group"].casefold(),
        p["controller_group"].casefold(),
    ):
        raise ValueError("production reference cannot be a trial group")
    image = p["candidate"]["image"]
    if not re.fullmatch(
        r"[a-z0-9]+\.azurecr\.io/sage@sha256:[a-f0-9]{64}", image
    ) or not re.fullmatch(r"[a-f0-9]{40}", p["candidate"]["commit"]):
        raise ValueError("immutable candidate identity required")
    expected = [("Consumption", 2, "4Gi"), ("D8", 2, "8Gi"), ("D8", 4, "8Gi")]
    if [(a["profile"], a["cpu"], a["memory"]) for a in p["arms"]] != expected:
        raise ValueError("fixed-memory/resource-size comparison changed")
    for key in ("site_id", "drive_id", "audience", "tenant_id"):
        if not isinstance(p["binding"].get(key), str) or not p["binding"][key].strip():
            raise ValueError("missing explicit cloud binding")
    prefix = (
        "/subscriptions/"
        + p["subscription_id"]
        + "/resourceGroups/"
        + p["source"]["resource_group"]
        + "/providers/"
    )
    for key, kind in [
        ("registry_id", "Microsoft.ContainerRegistry/registries/"),
        ("key_vault_id", "Microsoft.KeyVault/vaults/"),
    ]:
        rid = p["binding"][key]
        if (
            not rid.casefold().startswith((prefix + kind).casefold())
            or "/" in rid[len(prefix + kind) :]
        ):
            raise ValueError("external access outside exact source resource group")
    return p


def owned_inventory(plan, group, resources):
    """Refuse deleting any group/resource without exact subscription and ownership."""
    rid = "/subscriptions/" + plan["subscription_id"] + "/resourceGroups/" + plan["resource_group"]
    if group["id"].casefold() != rid.casefold() or group["name"] != plan["resource_group"]:
        raise ValueError("group identity drift")
    for row in [group, *resources]:
        if (
            row["tags"].get("benchmarkRun") != plan["run_id"]
            or row["tags"].get("expiresAt") != plan["expires_at"]
        ):
            raise ValueError("unowned or altered expiry resource")
        if row is not group and not row["id"].casefold().startswith(rid.casefold() + "/providers/"):
            raise ValueError("resource outside approved group")
    return resources


def renew_signin():
    remaining(120)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        # Keep the helper's masking directives visible to the Actions runner.
        subprocess.run(  # noqa: S603 - fixed reviewed executable and arguments, no shell
            [str(ROOT / "deploy/azure-federated-signin.sh"), "--if-stale"],
            check=True,
            timeout=remaining(120),
        )


class Azure:
    """Bounded CLI calls; diagnostics never echo arguments or secret-bearing output."""

    def __call__(self, *args, timeout=180):
        remaining(180)
        renew_signin()
        result = subprocess.run(  # noqa: S603 - argv only, validated Azure coordinates
            [AZURE_CLI, *args, "-o", "json", "--only-show-errors"],
            capture_output=True,
            text=True,
            timeout=remaining(timeout),
        )
        if result.returncode:
            raise RuntimeError(
                "Azure command failed: " + " ".join(args[:2]) + "; review the scoped CI step"
            )
        return json.loads(result.stdout) if result.stdout.strip() else None


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2))


def wait_job(az, group, job, execution, deadline):
    while time.monotonic() < deadline:
        row = az(
            "containerapp",
            "job",
            "execution",
            "show",
            "-g",
            group,
            "-n",
            job,
            "--job-execution-name",
            execution,
        )
        status = row["properties"]["status"]
        if status == "Succeeded":
            return row
        if status in ("Failed", "Degraded", "Stopped"):
            raise RuntimeError("trial job did not succeed")
        if status not in ("Running", "Processing", "Pending", "Unknown"):
            raise RuntimeError("unrecognized trial job state")
        time.sleep(10)
    raise TimeoutError("trial job deadline exceeded; reconcile execution before retry")


def manifest_parameters(
    plan, principal, ingress_key, *, stack=False, service=False, arm=None, access=False
):
    """Construct one parameter set. Identity coordinates are runtime data only."""
    arm = arm or plan["arms"][0]
    values = {
        "runId": plan["run_id"],
        "location": plan["location"],
        "expiresAt": plan["expires_at"],
        "imageDigest": plan["candidate"]["image"],
        "sourceGroup": plan["source"]["resource_group"],
        "acrName": plan["binding"]["registry_id"].rsplit("/", 1)[1],
        "keyVaultName": plan["binding"]["key_vault_id"].rsplit("/", 1)[1],
        "siteId": plan["binding"]["site_id"],
        "driveId": plan["binding"]["drive_id"],
        "audience": plan["binding"]["audience"],
        "tenantId": plan["binding"]["tenant_id"],
        "abstractionModel": plan["candidate"]["abstraction_model"],
        "maxProviderCalls": plan["limits"]["provider_calls"],
        "perRunSeconds": plan["limits"]["per_run_seconds"],
        "operatorPrincipalId": principal,
        "ingressKey": ingress_key,
        "deployStack": stack,
        "configureAccess": access or stack or service,
        "activateService": service,
        "profile": arm["profile"],
        "cpu": str(arm["cpu"]),
        "memory": arm["memory"],
    }
    return {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {k: {"value": v} for k, v in values.items()},
    }


class Rest:
    def __init__(self):
        import httpx

        self.client = httpx.Client(timeout=90, follow_redirects=False)
        self.tokens = {}

    def token(self, scope):
        remaining(60)
        cached = self.tokens.get(scope)
        if cached and cached[1] > time.time() + 180:
            return cached[0]
        remaining(180)
        renew_signin()
        result = subprocess.run(  # noqa: S603 - fixed CLI and scope supplied as one argument
            [
                AZURE_CLI,
                "account",
                "get-access-token",
                "--scope",
                scope,
                "--query",
                "accessToken",
                "-o",
                "tsv",
                "--only-show-errors",
            ],
            capture_output=True,
            text=True,
            timeout=remaining(60),
        )
        if result.returncode:
            raise RuntimeError("scoped Azure token unavailable")
        token = result.stdout.strip()
        claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==="))
        self.tokens[scope] = (token, float(claims["exp"]))
        return token

    def arm(self, method, path, *, body=None, raw=None, api="2024-10-23"):
        if not path.startswith("/subscriptions/") or "://" in path or ".." in path:
            raise ValueError("invalid ARM path")
        response = self.client.request(
            method,
            "https://management.azure.com" + path,
            params={"api-version": api},
            json=body if raw is None else None,
            content=raw,
            headers={
                "Authorization": "Bearer " + self.token("https://management.azure.com/.default"),
                "Content-Type": "text/powershell" if raw is not None else "application/json",
            },
            timeout=remaining(90),
        )
        if response.status_code >= 400:
            raise RemoteError(response.status_code)
        return response

    def graph(self, method, path, body=None):
        if (
            not path.startswith(("/servicePrincipals", "/sites/", "/drives/"))
            or ".." in path
            or "://" in path
        ):
            raise ValueError("invalid scoped Graph path")
        r = self.client.request(
            method,
            "https://graph.microsoft.com/v1.0" + path,
            json=body,
            headers={
                "Authorization": "Bearer " + self.token("https://graph.microsoft.com/.default")
            },
            timeout=remaining(90),
        )
        if r.status_code >= 400:
            raise RemoteError(r.status_code)
        return r.json() if r.content else None

    def blob(self, method, url, *, content=None, headers=None, params=None):
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not re.fullmatch(r"stperf[a-z0-9]+\.blob\.core\.windows\.net", parsed.hostname)
            or not parsed.path.startswith("/evidence/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid scoped export URL")
        r = self.client.request(
            method,
            url,
            content=content,
            params=params,
            headers={
                "Authorization": "Bearer " + self.token("https://storage.azure.com/.default"),
                "x-ms-version": "2023-11-03",
                "x-ms-date": datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S GMT"),
                **(headers or {}),
            },
            timeout=remaining(90),
        )
        r.raise_for_status()
        return r


def preflight(plan, az, rest):
    """Read-only dependencies are checked before resource creation."""
    import base64

    account = az("account", "show")
    if (
        account["id"] != plan["subscription_id"]
        or account["tenantId"] != plan["binding"]["tenant_id"]
    ):
        raise ValueError("Azure subscription/tenant differs from approved plan")
    graph_claims = json.loads(
        base64.urlsafe_b64decode(
            rest.token("https://graph.microsoft.com/.default").split(".")[1] + "==="
        )
    )
    granted = set(graph_claims.get("roles", [])) | set(graph_claims.get("scp", "").split())
    if not {"AppRoleAssignment.ReadWrite.All", "Sites.FullControl.All"}.issubset(granted):
        raise ValueError(
            "trial provisioning requires scoped Graph grant capability before resources are created"
        )
    group, app = plan["source"]["resource_group"], plan["source"]["app"]
    source = az("containerapp", "show", "-g", group, "-n", app)
    if source["id"].split("/")[2] != plan["subscription_id"]:
        raise ValueError("source subscription differs")
    containers = source["properties"]["template"]["containers"]
    if len(containers) != 1:
        raise ValueError("unexpected source topology")
    config = az(
        "containerapp",
        "secret",
        "list",
        "-g",
        group,
        "-n",
        app,
        "--show-values",
        "--query",
        "[?name=='sage-cloud-config'].value | [0]",
    )
    import yaml

    cfg = yaml.safe_load(config)
    binding = plan["binding"]
    if (
        cfg["profile"] != "cloud"
        or cfg["document_store"]["site_id"] != binding["site_id"]
        or cfg["document_store"]["drive_id"] != binding["drive_id"]
        or cfg["auth"]["audience"] != binding["audience"]
        or cfg["auth"]["tenant_id"] != binding["tenant_id"]
        or cfg["abstraction"]["model"] != plan["candidate"]["abstraction_model"]
    ):
        raise ValueError("reference cloud binding/model drift")
    registry = binding["registry_id"].rsplit("/", 1)[1]
    image = plan["candidate"]["image"]
    if image.split("/")[0] != registry + ".azurecr.io":
        raise ValueError("candidate registry differs")
    metadata = az(
        "acr", "manifest", "show-metadata", "--registry", registry, "--name", image.split("/", 1)[1]
    )
    if metadata["digest"] != image.split("@")[1]:
        raise ValueError("candidate digest unavailable")
    runs = json.loads(
        subprocess.check_output(  # noqa: S603 - fixed gh argv and validated commit SHA
            [
                GITHUB_CLI,
                "api",
                "repos/clifguy/cas/actions/workflows/ci.yml/runs?branch=main&event=push&head_sha="
                + plan["candidate"]["commit"]
                + "&per_page=100",
            ],
            text=True,
        )
    )["workflow_runs"]
    matching = [
        x
        for x in runs
        if x["head_sha"] == plan["candidate"]["commit"]
        and x["event"] == "push"
        and x["head_branch"] == "main"
    ]
    if (
        not matching
        or matching[0]["status"] != "completed"
        or matching[0]["conclusion"] != "success"
    ):
        raise ValueError("baseline exact-main CI is not green")
    return {
        "source_id": source["id"],
        "candidate_ci": matching[0]["html_url"],
        "source_image": containers[0]["image"],
        "checked_at": datetime.now(UTC).isoformat(),
    }


def apply_template(plan, az, parameters, path, *, preview=False, output=None, identities=None):
    import tempfile

    from deploy.performance_preview import inventory

    if output is None:
        raise ValueError("retained preview output is required")
    values = {key: value["value"] for key, value in parameters["parameters"].items()}
    phase = (
        "service-" + values["profile"] + "-" + values["cpu"]
        if values["activateService"]
        else "stack"
        if values["deployStack"]
        else "access"
        if values["configureAccess"]
        else "foundation"
    )
    digest = hashlib.sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()
    receipt = output / ("what-if-" + phase + ".json")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        os.chmod(handle.name, 0o600)
        json.dump(parameters, handle)
        name = handle.name
    common = (
        "--name",
        "perf-" + plan["run_id"],
        "--location",
        plan["location"],
        "--template-file",
        str(path),
        "--parameters",
        "@" + name,
    )
    try:
        remaining = (utc(plan["expires_at"]) - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise TimeoutError("refusing deployment after approved expiry")
        if preview:
            az("deployment", "sub", "validate", *common, timeout=min(2400, remaining))
            result = az(
                "deployment",
                "sub",
                "what-if",
                *common,
                "--no-pretty-print",
                "--result-format",
                "FullResourcePayloads",
                timeout=min(2400, remaining),
            )
            changes = inventory(plan, result, parameters, identities or {})
            # Only the inventory and digest are retained: provider payloads can
            # contain secure parameter values. No secret-bearing payload is logged.
            write_json(
                receipt,
                {
                    "phase": phase,
                    "parameter_sha256": digest,
                    "verified_at": datetime.now(UTC).isoformat(),
                    "raw_result_sha256": hashlib.sha256(
                        json.dumps(result, sort_keys=True).encode()
                    ).hexdigest(),
                    "inventory": changes,
                },
            )
            return result
        saved = json.loads(receipt.read_text())
        if (
            saved["parameter_sha256"] != digest
            or (datetime.now(UTC) - utc(saved["verified_at"])).total_seconds() > 300
        ):
            raise ValueError("deployment preview is stale or differs from exact parameters")
        return az("deployment", "sub", "create", *common, timeout=min(2400, remaining))
    finally:
        Path(name).unlink(missing_ok=True)


def arm_outputs(deployment):
    return {k: v["value"] for k, v in deployment["properties"]["outputs"].items()}


def controller_path(plan, outputs):
    return (
        "/subscriptions/"
        + plan["subscription_id"]
        + "/resourceGroups/"
        + plan["controller_group"]
        + "/providers/Microsoft.Automation/automationAccounts/"
        + outputs["automationName"]
    )


def enable_expiry(plan, outputs, rest, *, clock=time.monotonic, sleep=time.sleep):
    """Upload/read back/publish the committed script; test it before scheduling."""
    import uuid

    root = controller_path(plan, outputs)
    source = (ROOT / "deploy/performance_cleanup.ps1").read_bytes()
    rest.arm(
        "PUT",
        root + "/runbooks/expire",
        body={
            "location": plan["location"],
            "tags": {"benchmarkRun": plan["run_id"], "expiresAt": plan["expires_at"]},
            "properties": {"runbookType": "PowerShell", "logProgress": False, "logVerbose": False},
        },
    )
    rest.arm("PUT", root + "/runbooks/expire/draft/content", raw=source)
    if rest.arm("GET", root + "/runbooks/expire/draft/content").content != source:
        raise RuntimeError("runbook content readback differs")
    rest.arm("POST", root + "/runbooks/expire/publish")
    published_deadline = clock() + 180
    while clock() < published_deadline:
        state = rest.arm("GET", root + "/runbooks/expire").json()["properties"]["state"]
        if state == "Published":
            break
        sleep(5)
    else:
        raise TimeoutError("runbook publication not observed")
    if rest.arm("GET", root + "/runbooks/expire/content").content != source:
        raise RuntimeError("published runbook bytes differ")
    params = {
        "SubscriptionId": plan["subscription_id"],
        "TrialGroup": plan["resource_group"],
        "RunId": plan["run_id"],
        "ExpiresAt": plan["expires_at"],
        "Action": "Inspect",
    }
    job = str(uuid.uuid4())
    rest.arm(
        "PUT",
        root + "/jobs/" + job,
        body={"properties": {"runbook": {"name": "expire"}, "parameters": params}},
    )
    deadline = clock() + 600
    while clock() < deadline:
        status = rest.arm("GET", root + "/jobs/" + job).json()["properties"]["status"]
        if status == "Completed":
            break
        if status in ("Failed", "Stopped", "Suspended"):
            raise RuntimeError("independent cleanup preflight failed")
        sleep(10)
    else:
        raise TimeoutError("independent cleanup preflight timed out")
    params["Action"] = "Expire"
    rest.arm(
        "PUT",
        root + "/schedules/expiry",
        body={
            "properties": {
                "startTime": plan["expires_at"],
                "frequency": "OneTime",
                "isEnabled": True,
                "timeZone": "UTC",
            }
        },
    )
    link = str(uuid.uuid5(uuid.NAMESPACE_URL, root + "/expiry"))
    rest.arm(
        "PUT",
        root + "/jobSchedules/" + link,
        body={
            "properties": {
                "schedule": {"name": "expiry"},
                "runbook": {"name": "expire"},
                "parameters": params,
            }
        },
    )
    schedule = rest.arm("GET", root + "/schedules/expiry").json()
    linked = rest.arm("GET", root + "/jobSchedules/" + link).json()["properties"]
    if (
        schedule["properties"]["isEnabled"] is not True
        or utc(schedule["properties"]["startTime"]) != utc(plan["expires_at"])
        or linked["parameters"] != params
        or linked["runbook"]["name"] != "expire"
    ):
        raise RuntimeError("independent expiry is not enabled with exact parameters")
    return {
        "runbook_sha256": hashlib.sha256(source).hexdigest(),
        "inspection_job": job,
        "schedule": root + "/schedules/expiry",
        "link": root + "/jobSchedules/" + link,
        "expires_at": plan["expires_at"],
    }


def provision(
    plan,
    az,
    rest,
    *,
    principal,
    ingress_key,
    output,
    precheck=preflight,
    apply=apply_template,
    enable=enable_expiry,
):
    """The sole stack-creation path; injectable boundaries exercise actual ordering."""
    precheck(plan, az, rest)
    existing = az("group", "exists", "-n", plan["resource_group"])
    controller = az("group", "exists", "-n", plan["controller_group"])
    template = ROOT / "infra/performance.bicep"
    parameters = manifest_parameters(plan, principal, ingress_key)
    foundation = {}
    plan_hash = plan_digest(plan)
    if existing or controller:
        receipt = output / "provision-receipt.json"
        if not (existing and controller and receipt.exists()):
            raise ValueError("partial foundation requires explicit inventory reconciliation")
        saved = json.loads(receipt.read_text())
        if saved.get("plan_sha256") != plan_hash or saved["phase"] not in (
            "controller_created",
            "stack_created",
        ):
            raise ValueError("populated or mismatched trial cannot be replayed")
        for group in (plan["resource_group"], plan["controller_group"]):
            owned_inventory(
                {**plan, "resource_group": group},
                az("group", "show", "-n", group),
                az("resource", "list", "-g", group),
            )
        # Reapply only infrastructure before any service/source mutation.
        foundation = saved["outputs"]
    else:
        apply(
            plan,
            az,
            parameters,
            template,
            preview=True,
            output=output,
            identities=foundation,
        )
        foundation = arm_outputs(
            apply(
                plan,
                az,
                parameters,
                template,
                output=output,
                identities=foundation,
            )
        )
        write_json(
            output / "provision-receipt.json",
            {
                "phase": "controller_created",
                "outputs": foundation,
                "plan_sha256": plan_hash,
            },
        )
    parameters = manifest_parameters(plan, principal, ingress_key, access=True)
    apply(plan, az, parameters, template, preview=True, output=output, identities=foundation)
    foundation = arm_outputs(
        apply(plan, az, parameters, template, output=output, identities=foundation)
    )
    write_json(
        output / "provision-receipt.json",
        {
            "phase": "controller_created",
            "outputs": foundation,
            "plan_sha256": plan_hash,
        },
    )
    # An independent cloud execution of the exact published runbook is required;
    # a caller-local finally block or a mere schedule name is insufficient.
    expiry = enable(plan, foundation, rest)
    write_json(output / "expiry-receipt.json", expiry)
    parameters = manifest_parameters(plan, principal, ingress_key, stack=True)
    apply(
        plan,
        az,
        parameters,
        template,
        preview=True,
        output=output,
        identities=foundation,
    )
    created = arm_outputs(
        apply(plan, az, parameters, template, output=output, identities=foundation)
    )
    write_json(
        output / "provision-receipt.json",
        {
            "phase": "stack_created",
            "outputs": created,
            "expiry": expiry,
            "plan_sha256": plan_digest(plan),
        },
    )
    return created


def graph_rows(rest, path):
    rows = []
    while path:
        page = rest.graph("GET", path)
        rows.extend(page["value"])
        link = page.get("@odata.nextLink")
        if link and not link.startswith("https://graph.microsoft.com/v1.0/"):
            raise ValueError("foreign Graph pagination")
        path = link.removeprefix("https://graph.microsoft.com/v1.0") if link else None
    return rows


def grant_site(plan, outputs, rest, output):
    """Grant only the new trial principal on the explicitly approved source site."""
    graph_app = "00000003" + "-0000-0000-c000-" + "000000000000"  # public Microsoft Graph resource
    graph = graph_rows(rest, "/servicePrincipals?$filter=appId eq '" + graph_app + "'")
    if len(graph) != 1:
        raise ValueError("Microsoft Graph identity unresolved")
    role = next(x["id"] for x in graph[0]["appRoles"] if x["value"] == "Sites.Selected")
    principal = outputs["servicePrincipalId"]
    assignments = graph_rows(rest, "/servicePrincipals/" + principal + "/appRoleAssignments")
    match = [x for x in assignments if x["resourceId"] == graph[0]["id"] and x["appRoleId"] == role]
    if len(match) > 1:
        raise ValueError("ambiguous trial Graph assignment")
    assignment = (
        match[0]
        if match
        else rest.graph(
            "POST",
            "/servicePrincipals/" + principal + "/appRoleAssignments",
            {"principalId": principal, "resourceId": graph[0]["id"], "appRoleId": role},
        )
    )
    site = plan["binding"]["site_id"]
    permissions = graph_rows(rest, "/sites/" + site + "/permissions")

    def own(row):
        identities = row.get("grantedToIdentitiesV2", row.get("grantedToIdentities", []))
        return any(
            x.get("application", {}).get("id") == outputs["serviceClientId"] for x in identities
        )

    matches = [x for x in permissions if own(x)]
    if len(matches) > 1:
        raise ValueError("ambiguous trial site permission")
    permission = (
        matches[0]
        if matches
        else rest.graph(
            "POST",
            "/sites/" + site + "/permissions",
            {
                "roles": ["write"],
                "grantedToIdentities": [
                    {
                        "application": {
                            "id": outputs["serviceClientId"],
                            "displayName": "Isolated ingest performance trial",
                        }
                    }
                ],
            },
        )
    )
    if set(permission["roles"]) != {"write"} or not own(permission):
        raise ValueError("site permission differs from exact trial principal")
    receipt = {
        "principal_id": principal,
        "client_id": outputs["serviceClientId"],
        "assignment_id": assignment["id"],
        "graph_resource_id": graph[0]["id"],
        "graph_role_id": role,
        "site_id": site,
        "site_permission_id": permission["id"],
    }
    write_json(output / "graph-grants.json", receipt)
    return receipt


def cleanup(plan, az, rest, output, *, export_receipt_sha256=None):
    """Delete exact owned resources only after durable evidence export.

    Unknown/unowned inventory is a halt. Keep the independent controller whenever
    export, access revocation, trial deletion or its readback is incomplete.
    """
    provision_receipt = json.loads((output / "provision-receipt.json").read_text())
    outputs = provision_receipt["outputs"]
    if provision_receipt.get("plan_sha256") != plan_digest(plan):
        raise ValueError("cleanup provision receipt differs from runtime plan")
    verified = verify_export(plan, outputs, output, export_receipt_sha256)
    write_json(
        output / "cleanup-export-authorization.json",
        {
            "receipt_sha256": export_receipt_sha256,
            "attempt_id": verified["attempt_id"],
            "run_id": plan["run_id"],
            "verified_at": datetime.now(UTC).isoformat(),
        },
    )
    if az("group", "exists", "-n", plan["resource_group"]):
        live = az("group", "show", "-n", plan["resource_group"])
        owned_inventory(plan, live, az("resource", "list", "-g", plan["resource_group"]))
        for name, key in [
            ("id-perf-", "servicePrincipalId"),
            ("id-perf-pg-", "bootstrapPrincipalId"),
        ]:
            identity = az(
                "identity", "show", "-g", plan["resource_group"], "-n", name + plan["run_id"]
            )
            if identity["principalId"] != outputs[key]:
                raise ValueError("cleanup identity receipt drift")

    def present_graph(path):
        try:
            return rest.graph("GET", path)
        except RemoteError as exc:
            if exc.status == 404:
                return None
            raise

    receipt_path = output / "graph-grants.json"
    if receipt_path.exists():
        grant = json.loads(receipt_path.read_text())
        if grant["site_id"] != plan["binding"]["site_id"]:
            raise ValueError("grant receipt scope differs")
        if (
            grant["principal_id"] != outputs["servicePrincipalId"]
            or grant["client_id"] != outputs["serviceClientId"]
        ):
            raise ValueError("Graph grant principal drift")
        live = present_graph(
            "/sites/" + grant["site_id"] + "/permissions/" + grant["site_permission_id"]
        )
        identities = (live or {}).get(
            "grantedToIdentitiesV2", (live or {}).get("grantedToIdentities", [])
        )
        if live and not any(
            x.get("application", {}).get("id") == grant["client_id"] for x in identities
        ):
            raise ValueError("site permission now belongs elsewhere")
        if live:
            rest.graph(
                "DELETE",
                "/sites/" + grant["site_id"] + "/permissions/" + grant["site_permission_id"],
            )
        assignment = present_graph(
            "/servicePrincipals/"
            + grant["principal_id"]
            + "/appRoleAssignments/"
            + grant["assignment_id"],
        )
        if assignment and (
            assignment["resourceId"] != grant["graph_resource_id"]
            or assignment["appRoleId"] != grant["graph_role_id"]
        ):
            raise ValueError("Graph assignment drift")
        if assignment:
            rest.graph(
                "DELETE",
                "/servicePrincipals/"
                + grant["principal_id"]
                + "/appRoleAssignments/"
                + grant["assignment_id"],
            )
    principals = {outputs["servicePrincipalId"], outputs["bootstrapPrincipalId"]}
    allowed = {
        plan["binding"]["registry_id"].casefold(),
        (plan["binding"]["key_vault_id"] + "/secrets/anthropic-api-key").casefold(),
    }
    for role_id in outputs["sourceRoleIds"]:
        scopes = [
            x
            for x in allowed
            if role_id.casefold().startswith(
                x + "/providers/microsoft.authorization/roleassignments/"
            )
        ]
        if len(scopes) != 1:
            raise ValueError("external assignment outside exact approved scopes")
        try:
            role = rest.arm("GET", role_id, api="2022-04-01").json()["properties"]
        except RemoteError as exc:
            if exc.status == 404:
                continue
            raise
        expected_role = ROLE_IDS["secret"] if "/secrets/" in scopes[0] else ROLE_IDS["acr"]
        if (
            role["principalId"] not in principals
            or role["roleDefinitionId"].rsplit("/", 1)[1] != expected_role
        ):
            raise ValueError("external role identity drift")
        rest.arm("DELETE", role_id, api="2022-04-01")
    groups = [plan["resource_group"], plan["controller_group"]]
    for group in groups:
        if az("group", "exists", "-n", group):
            live = az("group", "show", "-n", group)
            resources = az("resource", "list", "-g", group)
            scoped = {**plan, "resource_group": group}
            owned_inventory(scoped, live, resources)
            az("group", "delete", "-n", group, "--yes", "--no-wait")
            deadline = time.monotonic() + 1800
            while az("group", "exists", "-n", group):
                if time.monotonic() >= deadline:
                    raise TimeoutError("deletion unconfirmed; retain controller")
                time.sleep(10)
        write_json(
            output / ("deleted-" + group + ".json"),
            {"group": group, "absent": True, "verified_at": datetime.now(UTC).isoformat()},
        )


def upload_inputs(plan, outputs, rest):
    rows = json.loads((ROOT / "deploy/performance/source-manifest.json").read_text())
    for row in rows:
        raw = (ROOT / "deploy/performance/sources" / row["name"]).read_bytes()
        if len(raw) != row["bytes"] or hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise ValueError("frozen source drift")
        rest.blob(
            "PUT",
            outputs["exportUrl"] + "/sources/" + row["name"],
            content=raw,
            headers={"x-ms-blob-type": "BlockBlob", "If-None-Match": "*"},
        )
    return rows


def materialize_trial(plan, outputs, az, output):
    """Pin actual serving identity, secret hash and wrapper entry after each arm."""
    live = az(
        "containerapp", "show", "-g", plan["resource_group"], "-n", "ingest-perf-" + plan["run_id"]
    )
    config = az(
        "containerapp",
        "secret",
        "list",
        "-g",
        plan["resource_group"],
        "-n",
        live["name"],
        "--show-values",
        "--query",
        "[?name=='sage-cloud-config'].value | [0]",
    )
    target = {
        "resource_group": plan["resource_group"],
        "app_resource_id": live["id"],
        "vault_id": plan["vault_id"],
        "environment_resource_id": outputs["stackOutputs"]["environmentId"],
        "postgres_resource_id": outputs["stackOutputs"]["postgresId"],
        "postgres_database": "sage",
        "stack_config_sha256": hashlib.sha256(config.encode()).hexdigest(),
        "config_secret_name": "sage-cloud-config",
        "vault_source_root": plan["vault_source_root"],
        "origin": outputs["stackOutputs"]["origin"],
        "revision_name": live["properties"]["latestRevisionName"],
        "restart_before_trial": True,
        "image_digest": plan["candidate"]["image"],
        "cpu": live["properties"]["template"]["containers"][0]["resources"]["cpu"],
        "memory": live["properties"]["template"]["containers"][0]["resources"]["memory"],
        "command": ["python", "/etc/perf/performance_runtime.py"],
        "args": live["properties"]["template"]["containers"][0].get("args"),
        "location": plan["location"],
        "runtime_env": {
            "PERF_RUN_ID": plan["run_id"],
            "PERF_EXPIRES_AT": plan["expires_at"],
            "PERF_EXPORT_URL": outputs["exportUrl"],
            "PERF_PROVIDER_LIMIT": str(plan["limits"]["provider_calls"]),
        },
        "mounted_script_paths": {
            "trial-runtime": "/etc/perf/performance_runtime.py",
            "trial-reconcile": "/etc/perf/reconcile.py",
        },
        "mounted_script_hashes": {
            "trial-runtime": hashlib.sha256(
                (ROOT / "deploy/performance_runtime.py").read_bytes()
            ).hexdigest(),
            "trial-reconcile": hashlib.sha256(
                (ROOT / "deploy/performance_reconcile.py").read_bytes()
            ).hexdigest(),
        },
    }
    result = {
        "schema_version": 1,
        "run_id": plan["run_id"],
        "source": {
            "resource_group": plan["source"]["resource_group"],
            "app_resource_id": "/subscriptions/"
            + plan["subscription_id"]
            + "/resourceGroups/"
            + plan["source"]["resource_group"]
            + "/providers/Microsoft.App/containerApps/"
            + plan["source"]["app"],
        },
        "target": target,
        "proposed_limits": plan["limits"],
        "acceptance_pending": [
            "cloud resource correlation",
            "fault/recovery controls",
            "vector/source reconciliation",
        ],
    }
    write_json(output / "trial-manifest.json", result)
    return result


def plan_digest(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()


def verify_export(plan, outputs, output, expected_digest):
    if not isinstance(expected_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_digest):
        raise ValueError("exact successful export receipt digest required")
    receipt_path = output / "export-receipt.json"
    state_path = output / "export-state.json"
    if not receipt_path.is_file() or not state_path.is_file():
        raise ValueError("latest verified export is missing; retain controller")
    raw = receipt_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    receipt, state = json.loads(raw), json.loads(state_path.read_bytes())
    if (
        digest != expected_digest
        or state.get("status") != "complete"
        or state.get("receipt_sha256") != digest
        or state.get("attempt_id") != receipt.get("attempt_id")
        or not re.fullmatch(r"[a-f0-9]{32}", receipt.get("attempt_id", ""))
        or receipt.get("run_id") != plan["run_id"]
        or receipt.get("plan_sha256") != plan_digest(plan)
        or state.get("plan_sha256") != plan_digest(plan)
        or receipt.get("export_url_sha256")
        != hashlib.sha256(outputs["exportUrl"].encode()).hexdigest()
    ):
        raise ValueError("latest export receipt identity/state drift; retain controller")
    files = receipt.get("files", [])
    if not files or receipt.get("blobs") != len(files):
        raise ValueError("incomplete export inventory; retain controller")
    seen = set()
    total = 0
    for row in files:
        path = row["path"]
        posix = PurePosixPath(path)
        if (
            posix.is_absolute()
            or str(posix) != path
            or ".." in posix.parts
            or len(posix.parts) < 3
            or posix.parts[:2] != ("cloud-evidence", plan["run_id"])
            or path in seen
        ):
            raise ValueError("foreign or duplicate export path")
        seen.add(path)
        target = output.joinpath(*posix.parts)
        if any(
            output.joinpath(*posix.parts[:i]).is_symlink() for i in range(1, len(posix.parts) + 1)
        ):
            raise ValueError("symlink in export inventory")
        if not target.is_file() or target.stat().st_size != row["bytes"]:
            raise ValueError("export file missing or changed")
        if hashlib.sha256(target.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("export file hash mismatch")
        total += row["bytes"]
        if total > 512 * 1024**2:
            raise ValueError("export allowance exceeded")
    return receipt


def export_evidence(plan, outputs, rest, output):
    """Replace export authorization only after the latest full attempt succeeds."""
    from urllib.parse import quote

    from defusedxml import ElementTree as ET

    receipt_path = output / "export-receipt.json"
    state_path = output / "export-state.json"
    state = {"attempt_id": uuid.uuid4().hex, "plan_sha256": plan_digest(plan), "status": "started"}
    # Independently revoke both proof channels before any remote work. A failed
    # unlink must not suppress state invalidation, or vice versa.
    try:
        write_json(state_path, state)
    finally:
        receipt_path.unlink(missing_ok=True)
    try:
        url = outputs["exportUrl"]
        container = url.rsplit("/", 1)[0]
        marker = None
        seen_markers = set()
        files = []
        names = set()
        total = 0
        while True:
            params = {"restype": "container", "comp": "list", "prefix": plan["run_id"] + "/"}
            if marker:
                params["marker"] = marker
            listing = rest.blob("GET", container + "/", params=params)
            root = ET.fromstring(listing.content)
            for item in root.findall("./Blobs/Blob"):
                name = item.findtext("Name")
                if (
                    not name
                    or not name.startswith(plan["run_id"] + "/")
                    or name in names
                    or any(x in ("", "..", ".") for x in name.split("/"))
                ):
                    raise ValueError("foreign or duplicate export blob")
                names.add(name)
                if len(names) > 4096:
                    raise ValueError("export file inventory exceeds archive allowance")
                response = rest.blob("GET", container + "/" + quote(name, safe="/"))
                raw = response.content
                total += len(raw)
                if total > 512 * 1024**2:
                    raise ValueError("export exceeds bounded evidence allowance")
                target = output / "cloud-evidence" / name
                if any(p.is_symlink() for p in [target, *target.parents] if p != output.parent):
                    raise ValueError("symlink in export target")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
                files.append(
                    {
                        "path": str(target.relative_to(output)),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "bytes": len(raw),
                    }
                )
            marker = root.findtext("NextMarker")
            if not marker:
                break
            if marker in seen_markers:
                raise ValueError("repeated export continuation token")
            seen_markers.add(marker)
        if not files:
            raise ValueError("missing durable cloud evidence")
        receipt = {
            "run_id": plan["run_id"],
            "attempt_id": state["attempt_id"],
            "plan_sha256": state["plan_sha256"],
            "export_url_sha256": hashlib.sha256(url.encode()).hexdigest(),
            "blobs": len(files),
            "exported_at": datetime.now(UTC).isoformat(),
            "files": files,
        }
        write_json(receipt_path, receipt)
        digest = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        write_json(state_path, {**state, "status": "complete", "receipt_sha256": digest})
        verify_export(plan, outputs, output, digest)
        return receipt
    except BaseException as exc:
        try:
            write_json(
                state_path, {**state, "status": "failed", "exception_class": type(exc).__name__}
            )
        finally:
            receipt_path.unlink(missing_ok=True)
        raise


def start_template_job(plan, az, rest, job_name, template, output, label):
    """Start once, pin the returned execution, and wait through configured retries."""
    group = plan["resource_group"]
    job = az("containerapp", "job", "show", "-g", group, "-n", job_name)
    if (
        job.get("tags", {}).get("benchmarkRun") != plan["run_id"]
        or job["id"].split("/")[2] != plan["subscription_id"]
    ):
        raise ValueError("foreign job target")
    if template != job["properties"]["template"]:
        raise ValueError(
            "job template differs; update and verify full stored template before start"
        )
    before = az("containerapp", "job", "execution", "list", "-g", group, "-n", job_name)
    known = {x["id"].casefold() for x in before}
    row = rest.arm("POST", job["id"] + "/start", api="2025-07-01")
    execution = row.json() if row.status_code == 200 and row.content else None
    if row.status_code == 202:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            rows = az("containerapp", "job", "execution", "list", "-g", group, "-n", job_name)
            new = [x for x in rows if x["id"].casefold() not in known]
            if len(new) > 1:
                raise ValueError("ambiguous job execution; reconcile without redispatch")
            if new:
                execution = new[0]
                break
            time.sleep(5)
    if execution is None:
        write_json(
            output / (label + "-dispatch.json"),
            {"outcome": "uncertain_dispatch", "job": job["id"], "status": row.status_code},
        )
        raise RuntimeError("job dispatch lacks unique execution identity; reconcile, never retry")
    name = execution.get("name")
    if (
        not name
        or execution.get("id", "").casefold() != (job["id"] + "/executions/" + name).casefold()
    ):
        raise ValueError("execution identity mismatch")
    write_json(
        output / (label + "-dispatch.json"),
        {"job": job["id"], "execution": execution, "template": template},
    )
    cfg = job["properties"]["configuration"]
    budget = cfg["replicaTimeout"] * (cfg["replicaRetryLimit"] + 1) + 180
    result = wait_job(az, group, job_name, name, time.monotonic() + budget)
    write_json(output / (label + "-completion.json"), result)
    return result


def run_microbenchmarks(plan, outputs, az, rest, output):
    """No service app exists during this serial fixed-memory comparison."""
    group = plan["resource_group"]
    name = outputs["stackOutputs"]["benchmarkJobName"]
    cases = []
    for cpu, threads in [(2, (1, 2)), (4, (1, 2, 4))]:
        for thread in threads:
            for shape in ("short", "long", "mixed"):
                for batch in (1, 2, 4, 8):
                    cases.append(
                        {
                            "cpu": cpu,
                            "threads": thread,
                            "shape": shape,
                            "batch": batch,
                            "mode": "direct",
                        }
                    )
                cases.append(
                    {"cpu": cpu, "threads": thread, "shape": shape, "batch": 1, "mode": "fair"}
                )
        cases.append(
            {
                "cpu": cpu,
                "threads": cpu,
                "shape": "mixed",
                "batch": 1,
                "mode": "fair",
                "uninstrumented": True,
            }
        )
    # Large-batch short/mixed pilots precede the full matrix. Any failed/OOM
    # execution stops the experiment; no discarded failure can become success.
    pilots = [
        {"cpu": 2, "threads": 2, "shape": s, "batch": b, "mode": "direct", "pilot": True}
        for s, b in [("short", 1), ("mixed", 8)]
    ]
    for index, case in enumerate(pilots + cases):
        if (utc(plan["expires_at"]) - datetime.now(UTC)).total_seconds() < 3600:
            write_json(
                output / "pruned-matrix.json",
                {
                    "reason": "reserve_service_export_cleanup_window",
                    "remaining": (pilots + cases)[index:],
                },
            )
            return False
        job = az("containerapp", "job", "show", "-g", group, "-n", name)
        container = job["properties"]["template"]["containers"][0]
        container["resources"] = {"cpu": case["cpu"], "memory": "8Gi"}
        env = [x for x in container["env"] if x["name"] != "PERF_BENCH_CASE"]
        container["env"] = [*env, {"name": "PERF_BENCH_CASE", "value": json.dumps(case)}]
        desired = {
            "containers": [container],
            **{
                key: value
                for key, value in job["properties"]["template"].items()
                if key != "containers"
            },
        }
        rest.arm("PATCH", job["id"], body={"properties": {"template": desired}}, api="2025-07-01")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            stored = az("containerapp", "job", "show", "-g", group, "-n", name)
            if (
                stored["properties"].get("provisioningState") == "Succeeded"
                and stored["properties"]["template"] == desired
            ):
                break
            time.sleep(5)
        else:
            raise TimeoutError("full benchmark template update not observed")
        start_template_job(plan, az, rest, name, desired, output, f"benchmark-{index:03d}")
    return True


def wait_serving(plan, az, arm, timeout=600):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app = az(
            "containerapp",
            "show",
            "-g",
            plan["resource_group"],
            "-n",
            "ingest-perf-" + plan["run_id"],
        )
        props = app["properties"]
        container = props["template"]["containers"][0]
        if (
            app.get("tags", {}).get("benchmarkRun") != plan["run_id"]
            or container["image"] != plan["candidate"]["image"]
        ):
            raise ValueError("serving identity drift")
        revisions = az(
            "containerapp",
            "revision",
            "list",
            "-g",
            plan["resource_group"],
            "-n",
            app["name"],
            "--all",
        )
        active = [x for x in revisions if x["properties"].get("active")]
        if (
            props.get("latestReadyRevisionName") == props.get("latestRevisionName")
            and len(active) == 1
            and active[0]["properties"].get("healthState") == "Healthy"
        ):
            actual = active[0]["properties"]["template"]["containers"][0]["resources"]
            if (
                actual["cpu"] == arm["cpu"]
                and actual["memory"] == arm["memory"]
                and props.get("workloadProfileName") == arm["profile"]
            ):
                return app
        time.sleep(10)
    raise TimeoutError("approved serving arm did not become healthy")


def create_source_root(plan, rest):
    """Create only the not-yet-a-vault trial root; vault files use SAGE APIs."""
    from urllib.parse import quote

    drive = quote(plan["binding"]["drive_id"], safe="")
    for parent, name in [("", "performance"), ("performance", plan["run_id"])]:
        path = (
            "/drives/" + drive + "/root" + (":/" + parent + ":/children" if parent else "/children")
        )
        children = graph_rows(rest, path)
        existing = [x for x in children if x["name"] == name]
        if len(existing) > 1:
            raise ValueError("ambiguous trial source directory")
        if existing:
            if name == plan["run_id"]:
                raise ValueError("trial source root already exists; reconcile before reuse")
            if "folder" not in existing[0]:
                raise ValueError("source parent is not a folder")
        else:
            rest.graph(
                "POST",
                path,
                {"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
            )


def capacity_and_cost(plan, az):
    """Read live regional availability and retail meters; never infer spare quota."""
    import httpx

    profiles = az(
        "containerapp", "env", "workload-profile", "list-supported", "--location", plan["location"]
    )
    if not any(
        x["name"] == "D8" and x["properties"]["cores"] == 8 and x["properties"]["memoryGiB"] == 32
        for x in profiles
    ):
        raise ValueError("fixed-memory D8 control unavailable")
    usages = az(
        "rest",
        "--method",
        "get",
        "--url",
        "https://management.azure.com/subscriptions/"
        + plan["subscription_id"]
        + "/providers/Microsoft.App/locations/"
        + plan["location"]
        + "/usages?api-version=2025-07-01",
    )["value"]
    environments = next(x for x in usages if x["name"]["value"] == "ManagedEnvironmentCount")
    if environments["currentValue"] >= environments["limit"]:
        raise ValueError("no managed environment quota")
    pg = az("postgres", "flexible-server", "list-skus", "--location", plan["location"])[0]
    burst = next(x for x in pg["supportedServerEditions"] if x["name"] == "Burstable")
    if not any(x["name"] == "Standard_B2s" for x in burst["supportedServerSkus"]) or not any(
        str(x["name"]) == "17" for x in pg["supportedServerVersions"]
    ):
        raise ValueError("representative PostgreSQL tier/major unavailable")
    prices = []
    for service in ("Azure Container Apps", "Azure Database for PostgreSQL", "Log Analytics"):
        url = "https://prices.azure.com/api/retail/prices"
        params = {
            "$filter": "serviceName eq '"
            + service
            + "' and armRegionName eq '"
            + plan["location"]
            + "' and priceType eq 'Consumption'"
        }
        for _ in range(10):
            response = httpx.get(url, params=params, timeout=30, follow_redirects=False)
            response.raise_for_status()
            page = response.json()
            prices.extend(page["Items"])
            url = page.get("NextPageLink")
            params = None
            if not url:
                break
            if not url.startswith("https://prices.azure.com/api/retail/prices"):
                raise ValueError("foreign price pagination")
        else:
            raise ValueError("retail meter enumeration incomplete")

    def rate(meter, product=None):
        rows = [
            x
            for x in prices
            if x["meterName"] == meter and (not product or x["productName"] == product)
        ]
        if not rows:
            raise ValueError("required live retail meter missing: " + meter)
        return max(x["retailPrice"] for x in rows)

    hours = plan["limits"]["window_seconds"] / 3600
    estimate = hours * (
        8 * rate("Dedicated vCPU Usage")
        + 32 * rate("Dedicated Memory Usage")
        + rate("Dedicated Plan Management")
    )
    estimate += (
        hours
        * 3600
        * (4 * rate("Standard vCPU Active Usage") + 8 * rate("Standard Memory Active Usage"))
    )
    estimate += (
        hours * rate("B2S")
        + 32 * rate("Storage Data Stored", "Azure Database for PostgreSQL Flex Server Storage")
        + rate("Analytics Logs Data Ingestion")
    )
    # Conservative separately bounded allowances: API calls/input, bootstrap/
    # verification compute, private DNS, secrets, controller and <1 GiB export.
    estimate += 12
    if estimate > plan["limits"]["incremental_usd"]:
        raise ValueError("priced resource envelope exceeds approved spend")
    return {
        "estimate_usd": round(estimate, 4),
        "allowances_usd": 12,
        "hours": hours,
        "prices": prices,
        "regional_usages": usages,
        "profiles": profiles,
        "new_environment_quota": "must_verify_after_creation",
        "financial_cap": "not_azure_enforced",
    }


def capture_active_quotas(plan, az, outputs):
    env = outputs["stackOutputs"]["environmentId"].rsplit("/", 1)[1]
    values = az("containerapp", "env", "list-usages", "-g", plan["resource_group"], "-n", env)[
        "value"
    ]
    expected = {"ManagedEnvironmentGeneralPurposeCores": 8, "ManagedEnvironmentConsumptionCores": 5}
    for key, needed in expected.items():
        row = next(x for x in values if x["name"]["value"] == key)
        if row["limit"] < needed:
            raise ValueError("trial environment quota insufficient")
    return values


def load_spec(path, approved_digest, *, materialize=False):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != approved_digest:
        raise ValueError("manifest bytes differ from approved digest")
    plan = json.loads(raw)
    if materialize:
        if "created_at" in plan or "expires_at" in plan:
            raise ValueError("dispatch spec uses a duration, not pre-existing runtime timestamps")
        from datetime import timedelta

        now = datetime.now(UTC)
        plan["created_at"] = now.isoformat()
        plan["expires_at"] = (now + timedelta(seconds=plan["limits"]["window_seconds"])).isoformat()
    return validate_plan(plan)


def retained_plan(spec, approved_digest, path, *, allow_expired=False):
    raw = spec.read_bytes()
    if hashlib.sha256(raw).hexdigest() != approved_digest:
        raise ValueError("spec digest mismatch")
    saved = json.loads(path.read_text())
    expected = json.loads(raw)
    expected.update({key: saved[key] for key in ("created_at", "expires_at")})
    plan = validate_plan(expected, allow_expired=allow_expired)
    if plan != saved:
        raise ValueError("retained runtime plan differs from exact approved spec")
    return plan


def main():
    import base64
    import os
    import secrets

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["validate", "run", "export", "cleanup"])
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--approved-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--export-receipt-sha256")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    retained = args.output / "runtime-plan.json"
    if args.operation in ("export", "cleanup"):
        plan = retained_plan(args.spec, args.approved_sha256, retained, allow_expired=True)
        provision_receipt = json.loads((args.output / "provision-receipt.json").read_text())
        if provision_receipt.get("plan_sha256") != plan_digest(plan):
            raise ValueError("provision receipt differs from runtime plan")
        if args.operation == "export":
            export_evidence(plan, provision_receipt["outputs"], Rest(), args.output)
        else:
            cleanup(
                plan, Azure(), Rest(), args.output, export_receipt_sha256=args.export_receipt_sha256
            )
        return 0
    if retained.exists():
        plan = retained_plan(args.spec, args.approved_sha256, retained)
    else:
        plan = load_spec(args.spec, args.approved_sha256, materialize=True)
    if args.operation == "run" and (
        os.environ.get("GITHUB_REF") != "refs/heads/main"
        or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or os.environ.get("GITHUB_REPOSITORY") != "clifguy/cas"
    ):
        raise ValueError("resource writes require the committed manual CI path on main")
    az, rest = Azure(), Rest()
    write_json(args.output / "runtime-plan.json", plan)
    try:
        write_json(args.output / "preflight.json", preflight(plan, az, rest))
        write_json(args.output / "cost-capacity.json", capacity_and_cost(plan, az))
        if args.operation == "validate":
            return 0
        claims = json.loads(
            base64.urlsafe_b64decode(
                rest.token("https://management.azure.com/.default").split(".")[1] + "==="
            )
        )
        principal = claims["oid"]
        key = secrets.token_urlsafe(32)
        outputs = provision(
            plan, az, rest, principal=principal, ingress_key=key, output=args.output
        )
        grant_site(plan, outputs, rest, args.output)
        create_source_root(plan, rest)
        upload_inputs(plan, outputs, rest)
        write_json(args.output / "active-quotas.json", capture_active_quotas(plan, az, outputs))
        job = az(
            "containerapp",
            "job",
            "show",
            "-g",
            plan["resource_group"],
            "-n",
            outputs["stackOutputs"]["bootstrapJob"],
        )
        start_template_job(
            plan, az, rest, job["name"], job["properties"]["template"], args.output, "bootstrap"
        )
        matrix_complete = run_microbenchmarks(plan, outputs, az, rest, args.output)
        # Full ARM provider validation for both fixed-memory app allocations is
        # now resolvable against the new environment; no app is serving yet.
        for arm in plan["arms"]:
            apply_template(
                plan,
                az,
                manifest_parameters(plan, principal, key, stack=True, service=True, arm=arm),
                ROOT / "infra/performance.bicep",
                preview=True,
                output=args.output,
                identities=outputs,
            )
        activated = apply_template(
            plan,
            az,
            manifest_parameters(plan, principal, key, stack=True, service=True),
            ROOT / "infra/performance.bicep",
            output=args.output,
            identities=outputs,
        )
        outputs = arm_outputs(activated)
        write_json(
            args.output / "provision-receipt.json",
            {
                "phase": "service_active",
                "outputs": outputs,
                "plan_sha256": plan_digest(plan),
            },
        )
        wait_serving(plan, az, plan["arms"][0])
        from deploy.performance_driver import run_services

        asyncio = __import__("asyncio")
        asyncio.run(run_services(plan, outputs, az, rest, args.output))
        verify = az(
            "containerapp",
            "job",
            "show",
            "-g",
            plan["resource_group"],
            "-n",
            outputs["stackOutputs"]["verifyJobName"],
        )
        start_template_job(
            plan,
            az,
            rest,
            verify["name"],
            verify["properties"]["template"],
            args.output,
            "reconcile",
        )
        export_evidence(plan, outputs, rest, args.output)
        reconciliation = json.loads(
            (args.output / "cloud-evidence" / plan["run_id"] / "reconciliation.json").read_text()
        )
        if reconciliation["outcome"] != "complete":
            raise ValueError("reconciliation incomplete")
        write_json(
            args.output / "outcome.json",
            {
                "outcome": "measurements_complete" if matrix_complete else "partial_matrix",
                "matrix_complete": matrix_complete,
                "reconciliation": reconciliation,
                "ticket_acceptance": "requires_report_and_owner_judgment",
            },
        )
        return 0 if matrix_complete else 1
    except BaseException as exc:
        write_json(
            args.output / "outcome.json",
            {
                "outcome": "partial_or_not_started",
                "exception_class": type(exc).__name__,
                "recovery": (
                    "Inspect exact receipts and live inventory before any retry; "
                    "preserve expiry controller"
                ),
            },
        )
        raise
    finally:
        from deploy.performance_report import write_report

        receipt = args.output / "provision-receipt.json"
        if receipt.exists():
            try:
                outputs = json.loads(receipt.read_text())["outputs"]
                export_evidence(plan, outputs, rest, args.output)
            except BaseException as exc:
                write_json(
                    args.output / "export-failure.json",
                    {"exception_class": type(exc).__name__, "controller_retained": True},
                )
        write_report(args.output, plan)


if __name__ == "__main__":
    raise SystemExit(main())
