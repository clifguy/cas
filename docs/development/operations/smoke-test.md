# CAS smoke-test procedure

**INACTIVE proposal.** See [authority transition](../authority-transition.md).

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

Every write requires current authorization for the exact target and operation.
Verify that target before every mutation. Use disposable fixtures where authorized;
record created objects and replay safety. Non-idempotent probes execute once unless
another execution is explicitly authorized and safe. Cleanup is a separate scoped
action. This procedure grants no production write, restart, registration refresh,
deployment, permission change or out-of-band database/filesystem modification.

Report intended revision and each surface's observed running provenance, client
freshness evidence, actual probe/control observations and expected outcomes. Classify
each as passed, failed assertion, pending or inaccessible/unknown. Keep missing rows
visible. Name untested surfaces and why. Distinguish a behavior defect from an
unavailable verification mechanism; never turn an unreachable surface into success.
