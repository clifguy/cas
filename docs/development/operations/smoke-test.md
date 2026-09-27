# CAS smoke-test procedure

Selected candidate procedure. The required activation receipt blocks operational use until the coordinated cutover; see [authority transition](../authority-transition.md).

Resolve the intended PR/commit first from the explicit request or verified task
context; read its cumulative change and acceptance behavior. Classify testability
before probing. Docs-only work uses relevant document/check evidence. Application
work is not excluded because the original SAGE smoke entry point omitted it.

| Surface | Mapping and independent provenance |
|---|---|
| Local SAGE | Discover actual Core/MCP endpoints; freshly read running build and resolve ancestry against intended commit; verify client registration separately |
| Cloud SAGE | Resolve configured tenant and served endpoint; correlate deployed revision/image digest, serving revision and live functional behavior |
| Local app | Resolve actual frontend and BFF endpoints; verify served asset/build identity and backend identity separately, then observe browser behavior |
| Cloud app | Resolve tenant frontend/BFF; correlate deployed assets and serving backend independently, then browser/network evidence |

Determine each selected surface's ready/pending/inaccessible state independently.
Run useful ready probes even while another surface awaits deployment. Configured
image tags, build artifacts, CI success and copied banners do not prove a process is
serving that artifact. Resolve Git objects before ancestry checks: exit 1 means
absent ancestry; missing object or command failure means unknown. A stale client
schema is distinct from stale server code. Read-only alternate transport can help
discriminate; never replay a mutating probe through a second transport merely to
confirm client freshness.

For SAGE use served contracts, current vault configuration and the SAGE operations
capability. For app behavior preserve UI observations, matching network requests and
responses, and console evidence. Exercise the changed path, an ordinary valid path
and neighboring controls that distinguish a targeted fix from blanket failure.

## Standing authorization for validation vaults

For an owner-initiated smoke test, the owner grants standing authorization for
all operations necessary to verify the selected PR/commit within these exact
deployment/vault pairs, except deleting the vault itself:

| Deployment | Literal vault ID | Authorized smoke-test effects |
|---|---|---|
| Local SAGE | `test` | All necessary in-vault verification operations except vault deletion |
| Hosted SAGE at `https://sage.cor.org` | `cloud_validation` | All necessary in-vault verification operations except vault deletion |

This general grant is not an exhaustive operation allowlist. It includes document
and lifecycle mutations, edge staging/confirmation/dismissal, production-edge
creation/removal, configuration changes, migrations, deletion of vault contents,
and necessary fixture preparation and cleanup. An unlisted operation is covered
when its actual effects are confined to the selected vault and necessary to verify
the selected change. No additional approval is required per operation, run, fixture,
batch or necessary repeat probe. Never delete the vault itself, including as part
of delete-and-recreate or cleanup.
Smoke-test initiation still follows CAS Invocation Authority. A narrower current
caller instruction limits this grant, including a read-only instruction.

Before every mutation, resolve the actual deployment and literal vault identity
from current configuration, live inventory and the connection being used; a name
match alone is insufficient. Do not transfer the grant to the same name on another
deployment or to another vault. Read the served contract and live target config
before configuration-sensitive work. Prefer supported service operations; evaluate
any required direct storage fixture preparation under the same actual-effect and
exact-vault limits below. Record created, changed and deleted objects, configuration
changes, retained state and cleanup outcomes.

Necessary distinct probes and repeat probes within this grant may proceed. Reconcile
an ambiguous prior write through supported readback before replay; if its outcome
cannot be established, stop the dependent retry and report uncertainty. A confirmed
successful write followed by stale client behavior calls for readback, not another
mutation through a second transport.

Necessary in-vault cleanup is included in this grant. This grant does not cover
other vaults, unrelated effects or effects outside the selected vault, such as
shared-service restarts, client-registration refresh, deployment or global
identity/access-control and Azure control-plane permission changes. Classify the
actual target and effects, not just the operation name or transport. Other writes
require an applicable current owner/project grant for their exact deployment,
target and effect.

Report intended revision and each surface's observed running provenance, client
freshness evidence, actual probe/control observations and expected outcomes. Classify
each as passed, failed assertion, pending or inaccessible/unknown. Keep missing rows
visible. Name untested surfaces and why. Distinguish a behavior defect from an
unavailable verification mechanism; never turn an unreachable surface into success.


## Execution sequence and caller evidence

Read [Runtime selection](references/runtime.md) and [dependencies](references/dependencies.md).
An explicit PR/commit selection overrides recent conversation pointers. With multiple
changes, identify every selected change separately and map each diff to its surfaces.
Read current cumulative diff, acceptance criteria, merge SHA and deployment status.
If all selected changes are documentation-only, report document validation and stop
before any service or infrastructure probe.

1. Discover actual endpoints, tenant, identity, audience and resource IDs from current
   configuration and served discovery. Never use a copied URL or resource name as
   proof. For SAGE inspect live tools/list or served OpenAPI and the target vault's
   configuration. Keep secrets out of receipts.
2. Read the running build freshly through the actual connection; old session banners
   are hints only. Resolve the intended and running Git objects, then establish
   ancestry. Distinguish absent ancestry (exit 1) from command/object failure (unknown).
   Cloud image tags need serving revision/digest correlation and live behavior.
3. Classify readiness per surface. A local-ready/cloud-pending pair permits local
   probes now. Frontend assets and BFF identity are separate rows: a current BFF does
   not prove current JavaScript is served. Unknown backend identity does not erase
   frontend evidence, nor permit a whole-app passed claim.
4. Select the smallest discriminating probe set. A refusal needs the changed input,
   an ordinary valid call, and the nearest valid neighbor as separate observations.
   For additions exercise the ordinary path too. Tool disclosure changes need the
   dispatched schema/response; removed names need the expected old-name refusal and
   valid replacement. App changes require actual UI action, request/response and
   console evidence, tied to the served asset/backend identities.
5. When client behavior differs, compare the registered client schema with the served
   contract. Use a read-only direct transport against the same server to discriminate
   client staleness from server behavior. Do not replay a non-idempotent call merely
   to get a second transport result; execute the authorized write once and read back
   the resulting state. An ambiguous write is reconciled before any retry. A stale
   client with correct direct behavior is client-registration pending, not a failed fix.
   A reconnect/restart is a remedy to report and requires its own authorization.

## Cloud evidence and safe fixture boundaries

Cloud writes covered by the standing authorization above may proceed without
per-run confirmation. Other cloud verification remains read-only unless an applicable
current grant covers the selected deployment, target and effect. Read that grant in
full and verify identity before every write; local permission does not transfer to
cloud, and this vault-local grant never implies Azure control-plane access.

Fixture preparation needed to verify the selected change, including deliberate
retained-source corruption or direct graph/content storage setup, is covered when
its actual effects are confined to the exact authorized vault. Resolve and verify
the specific store location, schema or other storage boundary before writing;
announce the fixture mutation and its expected effects. Use disposable fixtures
where possible. Do not infer confinement from a vault-like path or schema name
alone. If the target or effects cannot be established, leave that branch untested
and report the blocker. Never delete the vault itself. Necessary fixture cleanup
may proceed under the same grant; leave an object ledger showing its outcome and
any intentionally retained state.

For cloud SAGE, authenticated MCP or Core REST reads exercise managed bindings.
For a source-integrity change, the live contract's source-file/hash verification
operation provides stronger evidence than a stored hash field alone. MCP/REST
read agreement is useful transport control. Unauthenticated health/discovery and
expected authentication refusal establish routing/auth only; they do not prove
vault binding or served revision. Resolve the relevant deployment run by tenant
and exact revision; its preflight report corroborates deploy-time state, not current
serving behavior.

For Azure log evidence, resolve the workspace customerId GUID and inspect actual
diagnostic settings before choosing AzureDiagnostics or a dedicated destination
table. Metrics are not request logs. Allow a bounded ingestion delay and one
recheck (about fifteen minutes when appropriate); continued empty results require
inspection of active routing/emission settings, not an infinite wait or an outage
claim. Permission, wrong-table and unavailable-mechanism failures remain unknown.

## Report contract

Open with selected PR/commit identities and current provenance for each selected
surface, then client freshness evidence where relevant. Use separate probe and
control rows with actual status, verbatim caller-visible messages and relevant
payload keys; label expected/observed agreement on every row. Preserve rows that
could not run and name the blocker. Report passed, failed assertion, pending or
inaccessible/unknown independently. Name surfaces not exercised and the diff-based
reason. Include the applicable authorization source, a ledger of created, changed
and deleted objects, configuration changes and their final retained values,
retained state and necessary cleanup outcomes. Keep incidental unrelated
observations separate. Distinguish intended behavior from what the controls prove
about unaffected neighbors. Report defects;
repository source fixes, project work-tracking ticket writes outside the selected
validation vault, shared-service restarts, client reconnects and deployments require
separate scope. Necessary probe-document writes or retained-source repairs confined
to the authorized vault remain covered. A test-suite run is not a live smoke test.
