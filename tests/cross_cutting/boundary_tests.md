# Cross-Cutting Boundary Tests

Tier 2 behavioral tests for cross-schema reference integrity and SAGE/ROOT Harness
boundary rule enforcement.

**Status:** Stub. Tests will be specified as behavioral design decisions are made,
one subsystem ahead of implementation.

---

## Planned Test Categories

### Cross-Schema Reference Integrity
- Agent policy_ref resolution at runtime (not just config validation)
- Workflow node agent_ref dispatch verification
- Pipeline gatekeeper agent_id availability check

### SAGE / ROOT Harness Boundary Rule
- Stewards call SAGE Core API for artifact operations
- Orchestrators access artifacts through stewards, not directly
- Orchestrators access SAGE directly only for their own working state
- Policy enforcement: operations blocked when agent lacks permission

### Lifecycle + Pipeline State Coordination
- Lifecycle state transitions and pipeline stage transitions are independent but coordinated
- set_lifecycle does not implicitly advance pipeline stage
- Pipeline stage advancement does not implicitly change lifecycle state
- Filed lifecycle state and filed pipeline stage relationship

### Error Semantics (Tier 2 -- requires design decisions)
- Invalid lifecycle transition: what HTTP status and error structure?
- Policy violation: what HTTP status and error structure?
- Precondition failure during pipeline advancement
- Concurrent workflow access to same document
- LLM unavailability during abstraction generation
