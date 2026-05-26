# ROOT Harness Contract Tests

Tier 1 contract tests derived from the ROOT Harness formal substrate schemas and API specification.
Each test validates a structural constraint; behavioral semantics are covered in tier 2.

---

## pipeline.schema.json

### TEST-RH-PL-001: Valid minimal pipeline

**Artifact:** `docs/fs/root_harness/pipeline.schema.json`
**Category:** valid
**Constraint:** Minimal valid pipeline with one stage and one transition

**Input:**
```yaml
stages:
  - value: ingested
    label: "Ingested"
transitions:
  - from_stage: "(entry)"
    to_stage: ingested
```

**Expected:** PASS
**Rationale:** Smallest valid pipeline configuration.

### TEST-RH-PL-002: Valid pipeline with gatekeeper and preconditions

**Artifact:** `docs/fs/root_harness/pipeline.schema.json`
**Category:** valid
**Constraint:** Full transition with gatekeeper binding, preconditions, and human approval

**Input:**
```yaml
stages:
  - value: ingested
    label: "Ingested"
  - value: review
    label: "Review"
    applicable_doc_types: ["design_spec"]
transitions:
  - from_stage: "(entry)"
    to_stage: ingested
  - from_stage: ingested
    to_stage: review
    gatekeeper:
      agent_id: design_steward
      escalation_policy: escalate_to_human
    preconditions:
      - check: lifecycle_status
        description: "Document must be active"
        parameters:
          required_status: active
      - check: metadata_fields_populated
        description: "Required metadata present"
        parameters:
          fields: ["doc_type", "project"]
    requires_human_approval: true
```

**Expected:** PASS
**Rationale:** Exercises gatekeeper, all precondition check types, and human approval flag.

### TEST-RH-PL-003: Example Portfolio pipeline validates

**Artifact:** `docs/fs/root_harness/pipeline.schema.json`
**Category:** valid
**Constraint:** Real domain config validates

**Input:** Load `domains/example_vault/pipeline.yaml`

**Expected:** PASS
**Rationale:** Proves the schema handles a complex 14-stage, 24-transition pipeline.

### TEST-RH-PL-004: Empty stages array

**Artifact:** `docs/fs/root_harness/pipeline.schema.json`
**Category:** invalid
**Constraint:** `stages` has `minItems: 1`

**Input:**
```yaml
stages: []
transitions:
  - from_stage: "(entry)"
    to_stage: ingested
```

**Expected:** FAIL -- minItems violation
**Rationale:** Pipeline requires at least one stage.

### TEST-RH-PL-005: Stage value pattern violation

**Artifact:** `docs/fs/root_harness/pipeline.schema.json`
**Category:** invalid
**Constraint:** `stages[].value` must match `^[a-z][a-z0-9_]*$`

**Input:**
```yaml
stages:
  - value: "Attorney-Handoff"
    label: "Attorney Handoff"
transitions:
  - from_stage: "(entry)"
    to_stage: "Attorney-Handoff"
```

**Expected:** FAIL -- pattern mismatch
**Rationale:** Hyphens not allowed in stage identifiers.

### TEST-RH-PL-006: Invalid precondition check enum

**Artifact:** `docs/fs/root_harness/pipeline.schema.json`
**Category:** invalid
**Constraint:** `check` must be one of [lifecycle_status, required_edges_exist, editor_registered, dependent_documents_complete, metadata_fields_populated, custom]

**Input:** Precondition with `check: "approval_received"`

**Expected:** FAIL -- enum violation
**Rationale:** Only defined check types are accepted.

### TEST-RH-PL-007: Invalid escalation_policy enum

**Artifact:** `docs/fs/root_harness/pipeline.schema.json`
**Category:** invalid
**Constraint:** `escalation_policy` must be one of [block_and_report, escalate_to_human, skip_with_warning]

**Input:** Gatekeeper with `escalation_policy: "auto_approve"`

**Expected:** FAIL -- enum violation
**Rationale:** Only defined escalation behaviors are accepted.

### TEST-RH-PL-008: Missing required transition fields

**Artifact:** `docs/fs/root_harness/pipeline.schema.json`
**Category:** invalid
**Constraint:** Transitions require from_stage and to_stage

**Input:**
```yaml
stages:
  - value: ingested
    label: "Ingested"
transitions:
  - to_stage: ingested
```

**Expected:** FAIL -- `'from_stage' is a required property`
**Rationale:** Every transition must declare source and target.

### TEST-RH-PL-009: Additional property on stage

**Artifact:** `docs/fs/root_harness/pipeline.schema.json`
**Category:** invalid
**Constraint:** `additionalProperties: false` on stage items

**Input:** Stage with extra key `order: 1`

**Expected:** FAIL -- additional property not allowed
**Rationale:** Ordering is implied by array position, not a field.

---

## agent.schema.json

### TEST-RH-AG-001: Valid steward with owned_artifact

**Artifact:** `docs/fs/root_harness/agent.schema.json`
**Category:** valid
**Constraint:** Steward agent with all required fields

**Input:**
```yaml
agents:
  - agent_id: glossary_steward
    display_name: "Glossary Steward"
    agent_type: steward
    owned_artifact: glossary
    autonomy_level: 3
    capabilities: ["terminology_governance", "definition_sync"]
    policy_ref: glossary_steward_policy
```

**Expected:** PASS
**Rationale:** Minimal valid steward registration.

### TEST-RH-AG-002: Valid orchestrator with null owned_artifact

**Artifact:** `docs/fs/root_harness/agent.schema.json`
**Category:** valid
**Constraint:** Orchestrator with null owned_artifact and LLM config

**Input:**
```yaml
agents:
  - agent_id: pipeline_orchestrator
    display_name: "Pipeline Orchestrator"
    agent_type: orchestrator
    owned_artifact: null
    autonomy_level: 3
    capabilities: ["pipeline_dispatch", "progress_tracking"]
    policy_ref: pipeline_orchestrator_policy
    llm_config:
      provider: anthropic
      model: claude-opus-4-6
      temperature: 0.0
      max_tokens: 8192
      system_prompt_ref: "prompts/pipeline_orchestrator.md"
```

**Expected:** PASS
**Rationale:** Full orchestrator with LLM configuration.

### TEST-RH-AG-003: Example Portfolio agents validate

**Artifact:** `docs/fs/root_harness/agent.schema.json`
**Category:** valid
**Constraint:** Real domain config validates

**Input:** Load `domains/example_vault/agents.yaml`

**Expected:** PASS
**Rationale:** Validates 16-agent registration with mixed steward/orchestrator types.

### TEST-RH-AG-004: Missing required agent fields

**Artifact:** `docs/fs/root_harness/agent.schema.json`
**Category:** invalid
**Constraint:** Required: agent_id, display_name, agent_type, autonomy_level, capabilities, policy_ref

**Input:**
```yaml
agents:
  - agent_id: test_agent
    display_name: "Test"
```

**Expected:** FAIL -- multiple required properties missing
**Rationale:** All identity and configuration fields are mandatory.

### TEST-RH-AG-005: Invalid agent_type enum

**Artifact:** `docs/fs/root_harness/agent.schema.json`
**Category:** invalid
**Constraint:** `agent_type` must be one of [steward, orchestrator]

**Input:** Agent with `agent_type: "tool"`

**Expected:** FAIL -- enum violation
**Rationale:** Two-type taxonomy per CAS-ADR-010.

### TEST-RH-AG-006: Autonomy level out of range -- too low

**Artifact:** `docs/fs/root_harness/agent.schema.json`
**Category:** invalid
**Constraint:** `autonomy_level` minimum 1, maximum 5

**Input:** Agent with `autonomy_level: 0`

**Expected:** FAIL -- minimum violation
**Rationale:** Level 0 is not defined in the five-level scale.

### TEST-RH-AG-007: Autonomy level out of range -- too high

**Artifact:** `docs/fs/root_harness/agent.schema.json`
**Category:** invalid
**Constraint:** `autonomy_level` maximum is 5

**Input:** Agent with `autonomy_level: 6`

**Expected:** FAIL -- maximum violation
**Rationale:** Level 5 (fully autonomous) is the highest defined level.

### TEST-RH-AG-008: Empty capabilities array

**Artifact:** `docs/fs/root_harness/agent.schema.json`
**Category:** invalid
**Constraint:** `capabilities` has `minItems: 1`

**Input:** Agent with `capabilities: []`

**Expected:** FAIL -- minItems violation
**Rationale:** An agent without capabilities cannot be dispatched.

### TEST-RH-AG-009: LLM temperature out of range

**Artifact:** `docs/fs/root_harness/agent.schema.json`
**Category:** invalid
**Constraint:** `temperature` minimum 0.0, maximum 2.0

**Input:** Agent with `llm_config.temperature: 2.5`

**Expected:** FAIL -- maximum violation
**Rationale:** Temperature above 2.0 produces degenerate output.

### TEST-RH-AG-010: Empty agents array

**Artifact:** `docs/fs/root_harness/agent.schema.json`
**Category:** invalid
**Constraint:** `agents` has `minItems: 1`

**Input:**
```yaml
agents: []
```

**Expected:** FAIL -- minItems violation
**Rationale:** A domain instantiation must register at least one agent.

---

## policy.schema.json

### TEST-RH-PO-001: Valid minimal policy

**Artifact:** `docs/fs/root_harness/policy.schema.json`
**Category:** valid
**Constraint:** Policy with required fields only

**Input:**
```yaml
policies:
  - policy_id: read_only_policy
    label: "Read-Only Policy"
    permitted_operations: ["discover", "traverse"]
```

**Expected:** PASS
**Rationale:** Simplest valid policy.

### TEST-RH-PO-002: Valid policy with scope and approval rules

**Artifact:** `docs/fs/root_harness/policy.schema.json`
**Category:** valid
**Constraint:** Full policy with scope restrictions and approval policy

**Input:**
```yaml
policies:
  - policy_id: design_steward_policy
    label: "Report Steward Policy"
    description: "Governs report steward agent behavior"
    permitted_operations: ["discover", "ingest", "traverse", "set_lifecycle", "update_metadata"]
    scope_restrictions:
      doc_types: ["design_spec", "technical_disclosure"]
      lifecycle_states: ["active"]
    approval_policy:
      default_action: allow
      rules:
        - operation: set_lifecycle
          action: require_approval
          condition: "lifecycle_action == archive"
        - operation: ingest
          action: require_approval
      denial_response_format: structured_feedback
```

**Expected:** PASS
**Rationale:** Exercises all optional policy features.

### TEST-RH-PO-003: Example Portfolio policies validate

**Artifact:** `docs/fs/root_harness/policy.schema.json`
**Category:** valid
**Constraint:** Real domain config validates

**Input:** Load `domains/example_vault/policies.yaml`

**Expected:** PASS
**Rationale:** Validates 7-policy production configuration.

### TEST-RH-PO-004: Empty policies array

**Artifact:** `docs/fs/root_harness/policy.schema.json`
**Category:** invalid
**Constraint:** `policies` has `minItems: 1`

**Input:**
```yaml
policies: []
```

**Expected:** FAIL -- minItems violation
**Rationale:** At least one policy must exist for agent governance.

### TEST-RH-PO-005: Invalid permitted_operations enum

**Artifact:** `docs/fs/root_harness/policy.schema.json`
**Category:** invalid
**Constraint:** Operations must be from the 13-value enum

**Input:** Policy with `permitted_operations: ["discover", "delete"]`

**Expected:** FAIL -- enum violation on "delete"
**Rationale:** "delete" is not a SAGE Core API operation (no-delete invariant).

### TEST-RH-PO-006: Invalid approval default_action enum

**Artifact:** `docs/fs/root_harness/policy.schema.json`
**Category:** invalid
**Constraint:** `default_action` must be one of [allow, require_approval]

**Input:** `approval_policy.default_action: "deny"`

**Expected:** FAIL -- enum violation
**Rationale:** Default action is binary: allow or gate. Deny is per-rule only.

### TEST-RH-PO-007: policy_id pattern violation

**Artifact:** `docs/fs/root_harness/policy.schema.json`
**Category:** invalid
**Constraint:** `policy_id` must match `^[a-z][a-z0-9_-]*$`

**Input:** Policy with `policy_id: "Report Steward"`

**Expected:** FAIL -- pattern mismatch
**Rationale:** Policy IDs are machine identifiers.

### TEST-RH-PO-008: Approval rule invalid action enum

**Artifact:** `docs/fs/root_harness/policy.schema.json`
**Category:** invalid
**Constraint:** Rule `action` must be one of [allow, require_approval, deny]

**Input:** Approval rule with `action: "warn"`

**Expected:** FAIL -- enum violation
**Rationale:** Only allow, require_approval, and deny are valid rule actions.

---

## workflow.schema.json

### TEST-RH-WF-001: Valid minimal workflow

**Artifact:** `docs/fs/root_harness/workflow.schema.json`
**Category:** valid
**Constraint:** Minimal workflow with entry node, action node, and end node

**Input:**
```yaml
workflows:
  - workflow_id: simple_workflow
    label: "Simple Workflow"
    entry_node: start
    nodes:
      - node_id: start
        label: "Start"
        node_type: action
        agent_ref: vault_steward
      - node_id: done
        label: "Done"
        node_type: end
        agent_ref: null
    edges:
      - from_node: start
        to_node: done
    state_schema:
      fields:
        - name: document_id
          type: string
          required: true
```

**Expected:** PASS
**Rationale:** Smallest valid workflow graph.

### TEST-RH-WF-002: Valid workflow with interrupt and router

**Artifact:** `docs/fs/root_harness/workflow.schema.json`
**Category:** valid
**Constraint:** Workflow with interrupt node, router, conditional edges

**Input:**
```yaml
workflows:
  - workflow_id: approval_workflow
    label: "Approval Workflow"
    entry_node: evaluate
    nodes:
      - node_id: evaluate
        label: "Evaluate"
        node_type: action
        agent_ref: design_steward
      - node_id: route
        label: "Route"
        node_type: router
        agent_ref: design_steward
      - node_id: approve
        label: "Approve"
        node_type: interrupt
        agent_ref: null
        interrupt_config:
          prompt_template: "Approve transition for {document_title}?"
          response_options:
            - value: approved
              label: "Approve"
              routes_to: execute
            - value: rejected
              label: "Reject"
              routes_to: report
          evidence_fields: ["document_title", "current_stage"]
          timeout_minutes: 0
      - node_id: execute
        label: "Execute"
        node_type: action
        agent_ref: design_steward
      - node_id: report
        label: "Report"
        node_type: action
        agent_ref: design_steward
      - node_id: done
        label: "Done"
        node_type: end
        agent_ref: null
    edges:
      - from_node: evaluate
        to_node: route
      - from_node: route
        to_node: approve
        condition: "requires_human_approval == true"
        label: "Needs approval"
      - from_node: route
        to_node: execute
        condition: "requires_human_approval == false"
        label: "Auto-approve"
      - from_node: execute
        to_node: done
      - from_node: report
        to_node: done
    state_schema:
      fields:
        - name: document_id
          type: string
          required: true
        - name: document_title
          type: string
        - name: current_stage
          type: string
        - name: requires_human_approval
          type: boolean
          required: true
```

**Expected:** PASS
**Rationale:** Exercises interrupt config, router with conditions, and state schema types.

### TEST-RH-WF-003: Valid workflow with fan_out and merge

**Artifact:** `docs/fs/root_harness/workflow.schema.json`
**Category:** valid
**Constraint:** Parallel execution pattern

**Input:**
```yaml
workflows:
  - workflow_id: parallel_workflow
    label: "Parallel Workflow"
    entry_node: prepare
    nodes:
      - node_id: prepare
        label: "Prepare"
        node_type: action
        agent_ref: vault_steward
      - node_id: fan
        label: "Fan Out"
        node_type: fan_out
        agent_ref: null
        fan_out_config:
          parallel_nodes: ["task_a", "task_b"]
          merge_node: collect
      - node_id: task_a
        label: "Task A"
        node_type: action
        agent_ref: design_steward
      - node_id: task_b
        label: "Task B"
        node_type: action
        agent_ref: design_steward
      - node_id: collect
        label: "Collect"
        node_type: merge
        agent_ref: null
      - node_id: done
        label: "Done"
        node_type: end
        agent_ref: null
    edges:
      - from_node: prepare
        to_node: fan
      - from_node: fan
        to_node: task_a
      - from_node: fan
        to_node: task_b
      - from_node: task_a
        to_node: collect
      - from_node: task_b
        to_node: collect
      - from_node: collect
        to_node: done
    state_schema:
      fields:
        - name: results
          type: array
```

**Expected:** PASS
**Rationale:** Tests fan_out_config and merge node structural validity.

### TEST-RH-WF-004: Example Portfolio workflows validate

**Artifact:** `docs/fs/root_harness/workflow.schema.json`
**Category:** valid
**Constraint:** Real domain config validates

**Input:** Load `domains/example_vault/workflows.yaml`

**Expected:** PASS
**Rationale:** Validates 5-workflow production configuration.

### TEST-RH-WF-005: Invalid node_type enum

**Artifact:** `docs/fs/root_harness/workflow.schema.json`
**Category:** invalid
**Constraint:** `node_type` must be one of [action, router, interrupt, fan_out, merge, end]

**Input:** Node with `node_type: "decision"`

**Expected:** FAIL -- enum violation
**Rationale:** Only defined node types are accepted.

### TEST-RH-WF-006: Missing required workflow fields

**Artifact:** `docs/fs/root_harness/workflow.schema.json`
**Category:** invalid
**Constraint:** Required: workflow_id, label, entry_node, nodes, edges, state_schema

**Input:**
```yaml
workflows:
  - workflow_id: incomplete
    label: "Incomplete"
```

**Expected:** FAIL -- multiple required properties missing
**Rationale:** All structural fields are mandatory.

### TEST-RH-WF-007: Invalid state field type enum

**Artifact:** `docs/fs/root_harness/workflow.schema.json`
**Category:** invalid
**Constraint:** State field `type` must be one of [string, integer, boolean, array, object, null]

**Input:** State field with `type: "float"`

**Expected:** FAIL -- enum violation
**Rationale:** Only JSON types are valid for state dictionary fields.

### TEST-RH-WF-008: Empty nodes array

**Artifact:** `docs/fs/root_harness/workflow.schema.json`
**Category:** invalid
**Constraint:** `nodes` has `minItems: 1`

**Input:** Workflow with `nodes: []`

**Expected:** FAIL -- minItems violation
**Rationale:** A workflow needs at least one node.

### TEST-RH-WF-009: Empty edges array

**Artifact:** `docs/fs/root_harness/workflow.schema.json`
**Category:** invalid
**Constraint:** `edges` has `minItems: 1`

**Input:** Workflow with `edges: []`

**Expected:** FAIL -- minItems violation
**Rationale:** A workflow with nodes but no edges is disconnected.

### TEST-RH-WF-010: workflow_id pattern violation

**Artifact:** `docs/fs/root_harness/workflow.schema.json`
**Category:** invalid
**Constraint:** `workflow_id` must match `^[a-z][a-z0-9_]*$`

**Input:** Workflow with `workflow_id: "My-Workflow"`

**Expected:** FAIL -- pattern mismatch
**Rationale:** Workflow IDs are machine identifiers.

---

## event_stream.schema.json

### TEST-RH-ES-001: Valid workflow.started event

**Artifact:** `docs/fs/root_harness/event_stream.schema.json`
**Category:** valid
**Constraint:** Base required fields plus workflow.started conditional fields

**Input:**
```json
{
  "event_id": "evt-001",
  "event_type": "workflow.started",
  "timestamp": "2026-03-30T14:00:00Z",
  "workflow_execution_id": "wf-exec-001",
  "agent_id": "pipeline_orchestrator",
  "agent_type": "orchestrator",
  "workflow_name": "document_ingestion",
  "input_params": {"source_path": "/docs/test.md"}
}
```

**Expected:** PASS
**Rationale:** Tests workflow lifecycle event with conditional required fields.

### TEST-RH-ES-002: Valid tool.call event

**Artifact:** `docs/fs/root_harness/event_stream.schema.json`
**Category:** valid
**Constraint:** tool.call requires node_id, tool_name, operation, arguments

**Input:**
```json
{
  "event_id": "evt-010",
  "event_type": "tool.call",
  "timestamp": "2026-03-30T14:01:00Z",
  "workflow_execution_id": "wf-exec-001",
  "agent_id": "vault_steward",
  "agent_type": "steward",
  "node_id": "extract_metadata",
  "tool_name": "discover",
  "operation": "discover",
  "arguments": {"query": "test", "mode": "semantic"}
}
```

**Expected:** PASS
**Rationale:** Tests tool interaction event category.

### TEST-RH-ES-003: Valid interrupt.raised event

**Artifact:** `docs/fs/root_harness/event_stream.schema.json`
**Category:** valid
**Constraint:** interrupt.raised requires node_id, interrupt_id, interrupt_type, interrupt_description, response_options

**Input:**
```json
{
  "event_id": "evt-020",
  "event_type": "interrupt.raised",
  "timestamp": "2026-03-30T14:02:00Z",
  "workflow_execution_id": "wf-exec-001",
  "agent_id": "design_steward",
  "agent_type": "steward",
  "node_id": "approve_transition",
  "interrupt_id": "int-001",
  "interrupt_type": "checkpoint_review",
  "interrupt_description": "Approve transition to filing_preparation",
  "response_options": [
    {"option_id": "approve", "label": "Approve"},
    {"option_id": "reject", "label": "Reject"}
  ]
}
```

**Expected:** PASS
**Rationale:** Tests interrupt lifecycle events with nested InterruptOption.

### TEST-RH-ES-004: Valid governance.mutation_proposed event

**Artifact:** `docs/fs/root_harness/event_stream.schema.json`
**Category:** valid
**Constraint:** governance.mutation_proposed requires node_id, document_id, operation, mutation_description, requires_approval

**Input:**
```json
{
  "event_id": "evt-030",
  "event_type": "governance.mutation_proposed",
  "timestamp": "2026-03-30T14:03:00Z",
  "workflow_execution_id": "wf-exec-001",
  "agent_id": "glossary_steward",
  "agent_type": "steward",
  "node_id": "propagate_definitions",
  "document_id": "doc-pv06",
  "operation": "update_metadata",
  "mutation_description": "Sync glossary definitions v8.4 into PV06",
  "requires_approval": false
}
```

**Expected:** PASS
**Rationale:** Tests steward governance event category.

### TEST-RH-ES-005: Valid llm.response event

**Artifact:** `docs/fs/root_harness/event_stream.schema.json`
**Category:** valid
**Constraint:** llm.response requires node_id, model, purpose, prompt_tokens, completion_tokens, duration_ms

**Input:**
```json
{
  "event_id": "evt-040",
  "event_type": "llm.response",
  "timestamp": "2026-03-30T14:04:00Z",
  "workflow_execution_id": "wf-exec-001",
  "agent_id": "vault_steward",
  "agent_type": "steward",
  "node_id": "generate_abstract",
  "model": "Qwen3-30B-A3B-Instruct-2507",
  "purpose": "generate semantic abstract",
  "prompt_tokens": 1200,
  "completion_tokens": 350,
  "duration_ms": 4500
}
```

**Expected:** PASS
**Rationale:** Tests LLM cycle events with token counts.

### TEST-RH-ES-006: Valid checkpoint.saved event

**Artifact:** `docs/fs/root_harness/event_stream.schema.json`
**Category:** valid
**Constraint:** checkpoint.* requires node_id, checkpoint_id

**Input:**
```json
{
  "event_id": "evt-050",
  "event_type": "checkpoint.saved",
  "timestamp": "2026-03-30T14:05:00Z",
  "workflow_execution_id": "wf-exec-001",
  "agent_id": "pipeline_orchestrator",
  "agent_type": "orchestrator",
  "node_id": "execute_transition",
  "checkpoint_id": "cp-001"
}
```

**Expected:** PASS
**Rationale:** Tests checkpoint operation events.

### TEST-RH-ES-007: Missing base required fields

**Artifact:** `docs/fs/root_harness/event_stream.schema.json`
**Category:** invalid
**Constraint:** Base required: event_id, event_type, timestamp, workflow_execution_id, agent_id, agent_type

**Input:**
```json
{
  "event_id": "evt-099",
  "event_type": "workflow.started"
}
```

**Expected:** FAIL -- multiple required properties missing
**Rationale:** Every event must have full provenance.

### TEST-RH-ES-008: Invalid event_type enum

**Artifact:** `docs/fs/root_harness/event_stream.schema.json`
**Category:** invalid
**Constraint:** `event_type` must be one of the 15 defined types

**Input:** Event with `event_type: "agent.spawned"`

**Expected:** FAIL -- enum violation
**Rationale:** Only defined event types are accepted.

### TEST-RH-ES-009: tool.call missing conditional fields

**Artifact:** `docs/fs/root_harness/event_stream.schema.json`
**Category:** invalid
**Constraint:** tool.call requires node_id, tool_name, operation, arguments per allOf/if-then

**Input:** Event with `event_type: "tool.call"` but no `tool_name` or `arguments`.

**Expected:** FAIL -- conditional required fields missing
**Rationale:** Each event type has specific field requirements.

### TEST-RH-ES-010: workflow.failed missing error field

**Artifact:** `docs/fs/root_harness/event_stream.schema.json`
**Category:** invalid
**Constraint:** workflow.failed requires error and duration_ms per allOf/if-then

**Input:** Event with `event_type: "workflow.failed"`, `workflow_name` present, but no `error`.

**Expected:** FAIL -- `'error' is a required property`
**Rationale:** Failure events must carry error details.

---

## interrupt.schema.json

Note: This schema uses `$defs` only. Tests use `validate_sub_schema()`.

### TEST-RH-INT-001: Valid InterruptDescriptor

**Artifact:** `docs/fs/root_harness/interrupt.schema.json`
**Category:** valid
**Constraint:** All required InterruptDescriptor fields present
**Definition:** `InterruptDescriptor`

**Input:**
```json
{
  "interrupt_id": "int-001",
  "workflow_execution_id": "wf-exec-001",
  "workflow_name": "design_pipeline_advance",
  "agent_id": "design_steward",
  "agent_type": "steward",
  "node_id": "approve_transition",
  "origin": "interrupt_node",
  "interrupt_type": "checkpoint_review",
  "description": "Approve transition from content_development to attorney_handoff",
  "response_options": [
    {
      "option_id": "approve",
      "label": "Approve",
      "effect": "resume",
      "is_default": true
    },
    {
      "option_id": "reject",
      "label": "Reject with Remediation",
      "effect": "reject",
      "requires_comment": true
    }
  ],
  "raised_at": "2026-03-30T14:00:00Z",
  "status": "pending"
}
```

**Expected:** PASS
**Rationale:** Full interrupt descriptor with both response option variants.

### TEST-RH-INT-002: Valid ApproveRequest

**Artifact:** `docs/fs/root_harness/interrupt.schema.json`
**Category:** valid
**Constraint:** ApproveRequest with required fields
**Definition:** `ApproveRequest`

**Input:**
```json
{
  "interrupt_id": "int-001",
  "resolution": "approve",
  "comment": "Looks good, proceed to attorney handoff"
}
```

**Expected:** PASS
**Rationale:** Resolution with optional comment.

### TEST-RH-INT-003: Valid ApprovalPolicy

**Artifact:** `docs/fs/root_harness/interrupt.schema.json`
**Category:** valid
**Constraint:** ApprovalPolicy with rules
**Definition:** `ApprovalPolicy`

**Input:**
```json
{
  "rules": [
    {
      "operations": ["set_lifecycle", "ingest"],
      "action": "require_approval",
      "description": "Lifecycle and ingestion operations require human approval"
    }
  ],
  "default_action": "allow"
}
```

**Expected:** PASS
**Rationale:** Typical steward approval policy.

### TEST-RH-INT-004: Valid EvidenceItem

**Artifact:** `docs/fs/root_harness/interrupt.schema.json`
**Category:** valid
**Constraint:** EvidenceItem with all required fields
**Definition:** `EvidenceItem`

**Input:**
```json
{
  "type": "precondition_result",
  "label": "Precondition Check Results",
  "content": "All 5 preconditions satisfied",
  "document_id": "doc-pv06"
}
```

**Expected:** PASS
**Rationale:** Tests evidence structure with optional document reference.

### TEST-RH-INT-005: Invalid origin enum

**Artifact:** `docs/fs/root_harness/interrupt.schema.json`
**Category:** invalid
**Constraint:** `origin` must be one of [interrupt_node, approval_callback]
**Definition:** `InterruptDescriptor`

**Input:** InterruptDescriptor with `origin: "manual"`

**Expected:** FAIL -- enum violation
**Rationale:** Only two interrupt origins exist.

### TEST-RH-INT-006: Invalid status enum

**Artifact:** `docs/fs/root_harness/interrupt.schema.json`
**Category:** invalid
**Constraint:** `status` must be one of [pending, approved, rejected, expired]
**Definition:** `InterruptDescriptor`

**Input:** InterruptDescriptor with `status: "cancelled"`

**Expected:** FAIL -- enum violation
**Rationale:** "cancelled" is a workflow execution status, not an interrupt status.

### TEST-RH-INT-007: Invalid evidence type enum

**Artifact:** `docs/fs/root_harness/interrupt.schema.json`
**Category:** invalid
**Constraint:** Evidence `type` must be one of [document_excerpt, precondition_result, decision_log_entry, metric, diff, free_text]
**Definition:** `EvidenceItem`

**Input:** EvidenceItem with `type: "screenshot"`

**Expected:** FAIL -- enum violation
**Rationale:** Only defined evidence types are accepted.

### TEST-RH-INT-008: Invalid effect enum on InterruptOption

**Artifact:** `docs/fs/root_harness/interrupt.schema.json`
**Category:** invalid
**Constraint:** `effect` must be one of [resume, reject, defer, modify]
**Definition:** `InterruptOption`

**Input:** InterruptOption with `effect: "cancel"`

**Expected:** FAIL -- enum violation
**Rationale:** "cancel" is not a defined interrupt effect.

---

## orchestration_api.openapi.yaml

### TEST-RH-API-001: OpenAPI spec loads as valid YAML

**Artifact:** `docs/fs/root_harness/orchestration_api.openapi.yaml`
**Category:** valid
**Constraint:** File parses as YAML with openapi version 3.1.0

**Input:** Load `docs/fs/root_harness/orchestration_api.openapi.yaml`

**Expected:** `openapi` == `"3.1.0"`, `info.title` == `"ROOT Harness Orchestration API"`
**Rationale:** Basic structural validity.

### TEST-RH-API-002: All 9 operations present

**Artifact:** `docs/fs/root_harness/orchestration_api.openapi.yaml`
**Category:** valid
**Constraint:** All operations defined in ROOT Harness Architecture Reference Section 2.5

**Input:** Load and enumerate all path+method combinations.

**Expected:** Exactly these path/method pairs exist:
- `POST /workflows`
- `GET /workflows/{execution_id}`
- `POST /workflows/{execution_id}/approve`
- `GET /workflows/pending`
- `GET /workflows/{execution_id}/events`
- `POST /agents`
- `GET /agents/{agent_id}`
- `GET /agents/{agent_id}/history`
- `GET /pipelines/{pipeline_id}/status`
**Rationale:** Every operation in the architecture reference must have an API endpoint.

### TEST-RH-API-003: SSE event stream endpoint documented

**Artifact:** `docs/fs/root_harness/orchestration_api.openapi.yaml`
**Category:** valid
**Constraint:** Events endpoint uses text/event-stream content type

**Input:** Check response content type for `GET /workflows/{execution_id}/events`.

**Expected:** Response content type includes `text/event-stream`
**Rationale:** Event stream uses Server-Sent Events protocol.
