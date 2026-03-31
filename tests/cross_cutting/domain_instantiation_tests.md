# Domain Instantiation Tests

Tier 3 integration tests validating that domain configuration files validate against
their schemas and maintain cross-reference integrity.

---

## Schema Validation Smoke Tests

### TEST-DI-SM-001: PIM Health sage_vault_config.yaml validates

**Artifact:** `docs/fs/sage/vault_config.schema.json`
**Input:** Load `domains/pim_health/sage_vault_config.yaml`
**Expected:** PASS
**Rationale:** Root schema with $ref composition against production config.

### TEST-DI-SM-002: PIM Health pipeline.yaml validates

**Artifact:** `docs/fs/root_harness/pipeline.schema.json`
**Input:** Load `domains/pim_health/pipeline.yaml`
**Expected:** PASS

### TEST-DI-SM-003: PIM Health agents.yaml validates

**Artifact:** `docs/fs/root_harness/agent.schema.json`
**Input:** Load `domains/pim_health/agents.yaml`
**Expected:** PASS

### TEST-DI-SM-004: PIM Health policies.yaml validates

**Artifact:** `docs/fs/root_harness/policy.schema.json`
**Input:** Load `domains/pim_health/policies.yaml`
**Expected:** PASS

### TEST-DI-SM-005: PIM Health workflows.yaml validates

**Artifact:** `docs/fs/root_harness/workflow.schema.json`
**Input:** Load `domains/pim_health/workflows.yaml`
**Expected:** PASS

---

## Cross-Reference Integrity Tests

### TEST-DI-XR-001: Every agent policy_ref resolves to a policy_id

**Artifacts:** `domains/pim_health/agents.yaml`, `domains/pim_health/policies.yaml`
**Constraint:** For every agent in agents.yaml, `agent.policy_ref` must match a `policy.policy_id` in policies.yaml.

**Input:** Load both files. Extract all `policy_ref` values from agents. Extract all `policy_id` values from policies.
**Expected:** Every policy_ref is present in the policy_id set.
**Rationale:** Dangling policy references would leave agents ungoverned.

### TEST-DI-XR-002: Every pipeline gatekeeper agent_id exists in agents

**Artifacts:** `domains/pim_health/pipeline.yaml`, `domains/pim_health/agents.yaml`
**Constraint:** For every transition with a `gatekeeper.agent_id`, that ID must match an `agent.agent_id` in agents.yaml.

**Input:** Load both files. Extract all gatekeeper agent_ids from transitions. Extract all agent_ids from agents.
**Expected:** Every gatekeeper agent_id is present in the agent_id set.
**Rationale:** Unregistered gatekeepers cannot be dispatched.

### TEST-DI-XR-003: Every workflow node agent_ref exists in agents

**Artifacts:** `domains/pim_health/workflows.yaml`, `domains/pim_health/agents.yaml`
**Constraint:** For every node with a non-null `agent_ref`, that value must match an `agent.agent_id` in agents.yaml.

**Input:** Load both files. Extract all non-null agent_refs from all workflow nodes. Extract all agent_ids from agents.
**Expected:** Every agent_ref is present in the agent_id set.
**Rationale:** Workflow nodes referencing unregistered agents cannot execute.

### TEST-DI-XR-004: Pipeline doc_types reference valid document types

**Artifacts:** `domains/pim_health/pipeline.yaml`, `domains/pim_health/sage_vault_config.yaml`
**Constraint:** Every `applicable_doc_types` value in pipeline stages must match a `doc_type.value` in sage_vault_config.yaml.

**Input:** Load both files. Extract all applicable_doc_types values. Extract all doc_type values.
**Expected:** Every applicable doc type is present in the doc_type value set.
**Rationale:** Pipeline stages referencing undefined doc types would silently match nothing.

### TEST-DI-XR-005: Policy scope doc_types reference valid document types

**Artifacts:** `domains/pim_health/policies.yaml`, `domains/pim_health/sage_vault_config.yaml`
**Constraint:** Every `scope_restrictions.doc_types` value must match a `doc_type.value` in sage_vault_config.yaml.

**Input:** Load both files. Extract all scope doc_types. Extract all doc_type values.
**Expected:** Every scope doc type is present in the doc_type value set.
**Rationale:** Scope restrictions referencing undefined types would silently match nothing.

### TEST-DI-XR-006: Policy permitted_operations are valid SAGE Core API operations

**Artifacts:** `domains/pim_health/policies.yaml`
**Constraint:** Every `permitted_operations` value must be in the 13-value enum defined by policy.schema.json.

**Input:** Load policies. Extract all permitted_operations values.
**Expected:** All values are within the enum. (This is also enforced by schema validation in TEST-DI-SM-004, but this test makes the constraint explicit.)
**Rationale:** Defense-in-depth for operation naming consistency.
