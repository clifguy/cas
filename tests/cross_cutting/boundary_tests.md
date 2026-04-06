# Cross-Cutting Boundary Tests

Tier 2 behavioral tests for cross-schema reference integrity and SAGE/ROOT Harness
boundary rule enforcement.

Design decisions made 2026-04-05 during SAGE tier 2 specification.

---

## SAGE / ROOT Harness Boundary Rule

### TEST-XCUT-BH-001: Agent registration flows through ROOT Harness

**Artifact:** ROOT Harness `register_agent` -> SAGE `register_user`
**Category:** boundary
**Decision:** ROOT Harness is authoritative for agent registration.

**Precondition:** Vault initialized. ROOT Harness running.

**Input:** Call ROOT Harness `register_agent` for a steward agent.

**Expected:**
- ROOT Harness internally calls SAGE `register_user` with `type: "agent"`
- ROOT Harness agent record contains `sage_user_id` matching the SAGE user record
- Provenance fields on documents use the SAGE user_id (not a ROOT Harness-specific ID)

**Rationale:** Single entry point for agent identity. ROOT Harness calls into SAGE
(boundary rule direction). SAGE's user table is the provenance authority.

### TEST-XCUT-BH-002: Stewards call SAGE Core API for artifact operations

**Artifact:** SAGE Core API, ROOT Harness boundary rule
**Category:** boundary
**Decision:** Stewards access SAGE directly; orchestrators access artifacts through stewards.

**Precondition:** A steward agent and an orchestrator agent registered. Document ingested.

**Input:** Orchestrator attempts to call SAGE `update_metadata` directly (bypassing steward).

**Expected:** The API does not reject based on agent type (SAGE doesn't know about
the steward/orchestrator distinction). The boundary rule is enforced by ROOT Harness
policy configuration, not by SAGE access control. Policy tests (tier 2 ROOT Harness)
verify that orchestrator policies don't include `update_metadata` in `permitted_operations`.

**Rationale:** SAGE indexes everything and owns nothing. The boundary rule is a ROOT
Harness governance concern, not a SAGE enforcement concern.

### TEST-XCUT-BH-003: Orchestrators access SAGE for working state only

**Artifact:** SAGE Core API, ROOT Harness boundary rule
**Category:** boundary
**Decision:** Orchestrators may call SAGE directly for their own working state
(decision logs, workflow checkpoints) but not for artifact mutations.

**Precondition:** Orchestrator registered. Decision log document ingested (doc_type: decision_log equivalent).

**Input:** Orchestrator calls SAGE `update_metadata` on a decision log it owns.

**Expected:** Succeeds (SAGE allows it; the orchestrator is in the editor list for
its own working-state documents). Policy enforcement at the ROOT Harness level
permits this specific operation class.

**Rationale:** Working-state documents (decision logs, checkpoint artifacts) are
owned by orchestrators. The boundary rule permits orchestrator-to-SAGE calls for
these, while blocking orchestrator access to steward-managed artifacts.


---

## Lifecycle + Pipeline State Coordination

### TEST-XCUT-BH-004: Lifecycle and pipeline are independent state dimensions

**Artifact:** `sage/sage_core_api.openapi.yaml` (Document schema)
**Category:** state_coordination
**Decision:** Lifecycle transitions do not affect pipeline_status; pipeline
advancement does not affect lifecycle_status.

**Precondition:** Document in `active` state, `pipeline_status: indexing_in_progress`.

**Input:** `set_lifecycle(action: "archive")`

**Expected:**
- `lifecycle_status: "archived"`
- `pipeline_status: "indexing_in_progress"` (unchanged)
- Response includes pipeline warning

**Rationale:** Two independent state machines. Neither overwrites the other.

### TEST-XCUT-BH-005: set_lifecycle does not implicitly advance pipeline

**Artifact:** SAGE lifecycle + pipeline interaction
**Category:** state_coordination
**Decision:** No implicit side effects between state machines.

**Precondition:** Document in `active` state, `pipeline_status: projection_complete`.

**Input:** `set_lifecycle(action: "complete")`

**Expected:**
- `lifecycle_status: "completed"`
- `pipeline_status: "projection_complete"` (still waiting for async indexing)

**Rationale:** Each state machine advances through its own explicit operations only.

### TEST-XCUT-BH-006: Pipeline failure does not block lifecycle transitions

**Artifact:** SAGE lifecycle + pipeline interaction
**Category:** state_coordination
**Decision:** Allow lifecycle transitions even on failed documents (with warning).

**Precondition:** Document in `active` state, `pipeline_status: failed`.

**Input:** `set_lifecycle(action: "archive")`

**Expected:**
- HTTP 200
- `lifecycle_status: "archived"`
- `pipeline_status: "failed"` (unchanged)
- Response includes warning about failed pipeline

**Rationale:** A failed document might reasonably be archived (cleanup) or
superseded (replace with a working version). Blocking lifecycle would strand
failed documents.


---

## Cross-Schema Reference Integrity (Runtime)

### TEST-XCUT-BH-007: Agent policy_ref resolution at runtime

**Artifact:** `root_harness/agent.schema.json`, `root_harness/policy.schema.json`
**Category:** reference_integrity
**Status:** Stub -- requires ROOT Harness tier 2 behavioral decisions.

**Description:** When a ROOT Harness workflow dispatches an agent, the agent's
`policy_ref` must resolve to a valid policy document. Test that a missing or
invalid policy_ref produces a clear error at dispatch time, not at configuration
load time.

### TEST-XCUT-BH-008: Workflow node agent_ref dispatch verification

**Artifact:** `root_harness/workflow.schema.json`, `root_harness/agent.schema.json`
**Category:** reference_integrity
**Status:** Stub -- requires ROOT Harness tier 2 behavioral decisions.

**Description:** When a workflow reaches a node with an `agent_ref`, the referenced
agent must be registered and available. Test that an unregistered agent_ref produces
a clear dispatch error.

### TEST-XCUT-BH-009: Pipeline gatekeeper agent_id availability check

**Artifact:** `root_harness/pipeline.schema.json`, `root_harness/agent.schema.json`
**Category:** reference_integrity
**Status:** Stub -- requires ROOT Harness tier 2 behavioral decisions.

**Description:** Pipeline transitions governed by a gatekeeper require the gatekeeper
agent to be registered. Test that an unregistered gatekeeper produces a clear error
when the pipeline transition is attempted.


---

## Error Semantics (Cross-Cutting)

### TEST-XCUT-BH-010: Policy violation error structure

**Artifact:** ROOT Harness policy enforcement
**Category:** error_semantics
**Status:** Stub -- requires ROOT Harness tier 2 behavioral decisions.

**Description:** When an agent attempts an operation not in its permitted_operations
list, the ROOT Harness must produce a clear error. Error structure TBD.

### TEST-XCUT-BH-011: Precondition failure during pipeline advancement

**Artifact:** ROOT Harness pipeline + SAGE check_preconditions
**Category:** error_semantics
**Status:** Stub -- requires ROOT Harness tier 2 behavioral decisions.

**Description:** When a pipeline stage transition has preconditions (checked via
SAGE `check_preconditions`) and they fail, the ROOT Harness must produce a clear
error indicating which preconditions were unsatisfied.

### TEST-XCUT-BH-012: Concurrent workflow access to same document

**Artifact:** SAGE per-document locking + ROOT Harness workflow dispatch
**Category:** concurrency
**Decision (partial):** SAGE uses per-document locking (decided 2026-04-05).
ROOT Harness behavior when two workflows target the same document is TBD.

**Description:** Two workflows both attempt to modify the same document's metadata
concurrently. SAGE serializes via per-document lock. The ROOT Harness behavior
(fail-fast, queue, or allow) is a tier 2 ROOT Harness decision.
