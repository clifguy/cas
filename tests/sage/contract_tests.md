# SAGE Contract Tests

Tier 1 contract tests derived from the SAGE formal substrate schemas and API specification.
Each test validates a structural constraint; behavioral semantics are covered in tier 2.

---

## vault_config.schema.json

### TEST-SAGE-VC-001: Valid minimal vault config

**Artifact:** `docs/fs/sage/vault_config.schema.json`
**Category:** valid
**Constraint:** All five required top-level keys present with minimal valid sub-schemas

**Input:**
```yaml
vault:
  id: test_vault
  name: "Test Vault"
  owner: testuser
  storage_root: "/tmp/test/sources"
  brain_root: "/tmp/test/brain"
  visibility: personal
document_types:
  doc_types:
    - value: note
      label: "Note"
lifecycle:
  states:
    - value: active
      label: "Active"
  transitions:
    - from_state: "(new)"
      action: ingest
      to_state: active
  base_states_required: false
metadata_extraction:
  review_required: false
edge_inference:
  tier_assignments:
    - edge_type: supersedes
      tier: 1
      inference_rules:
        - method: version_chain
```

**Expected:** PASS
**Rationale:** Establishes the minimal valid baseline for vault configuration.
A single-state lifecycle is a replacement lifecycle, so it declares
`base_states_required: false`; with the flag true the loader requires the
full base state set and base actions.

### TEST-SAGE-VC-002: Valid full vault config with optional fields

**Artifact:** `docs/fs/sage/vault_config.schema.json`
**Category:** valid
**Constraint:** Optional fields (members, access_control_defaults, abstraction) accepted

**Input:**
```yaml
vault:
  id: full_vault
  name: "Full Vault"
  owner: admin
  storage_root: "/data/sources"
  brain_root: "/data/brain"
  visibility: team
  members:
    - user_id: user1
      role: editor
    - user_id: user2
      role: reader
document_types:
  doc_types:
    - value: report
      label: "Report"
      description: "Quarterly report"
lifecycle:
  states:
    - value: active
      label: "Active"
  transitions:
    - from_state: "(new)"
      action: ingest
      to_state: active
  base_states_required: false
adapter_defaults:
  docx:
    heading_style_map:
      "Custom Section": 1
metadata_extraction:
  review_required: true
edge_inference:
  tier_assignments:
    - edge_type: supersedes
      tier: 1
      inference_rules:
        - method: version_chain
access_control_defaults:
  new_documents_restricted: true
abstraction:
  enabled: true
  model: "test-model"
  max_abstract_tokens: 500
```

**Expected:** PASS
**Rationale:** Validates all optional sections are accepted when present.

### TEST-SAGE-VC-003: Example Portfolio config validates

**Artifact:** `docs/fs/sage/vault_config.schema.json`
**Category:** valid
**Constraint:** Real domain config validates against root schema with $ref composition

**Input:** Load `~/sage_vaults/example_vault/vault_config.yaml`

**Expected:** PASS
**Rationale:** Proves $ref composition works end-to-end with a production config.

### TEST-SAGE-VC-004: Missing required top-level key (document_types)

**Artifact:** `docs/fs/sage/vault_config.schema.json`
**Category:** invalid
**Constraint:** All five top-level keys are required

**Input:** Valid vault config with `document_types` key removed.

**Expected:** FAIL -- `'document_types' is a required property`
**Rationale:** Each of the five required keys must be enforced. Test one representative; fixture files cover the rest.

### TEST-SAGE-VC-005: vault.id pattern violation -- uppercase

**Artifact:** `docs/fs/sage/vault_config.schema.json`
**Category:** invalid
**Constraint:** `vault.id` must match `^[a-z][a-z0-9_-]*$`

**Input:** `vault.id: "MyVault"`

**Expected:** FAIL -- pattern mismatch
**Rationale:** Vault IDs are machine identifiers; uppercase breaks filesystem and URL conventions.

### TEST-SAGE-VC-006: vault.id pattern violation -- starts with number

**Artifact:** `docs/fs/sage/vault_config.schema.json`
**Category:** invalid
**Constraint:** `vault.id` must start with lowercase letter

**Input:** `vault.id: "1vault"`

**Expected:** FAIL -- pattern mismatch
**Rationale:** IDs starting with numbers create ambiguity in URL paths and query parsing.

### TEST-SAGE-VC-007: vault.visibility invalid enum

**Artifact:** `docs/fs/sage/vault_config.schema.json`
**Category:** invalid
**Constraint:** `vault.visibility` must be one of [personal, team, org]

**Input:** `vault.visibility: "public"`

**Expected:** FAIL -- enum violation
**Rationale:** "public" is not a valid visibility level.

### TEST-SAGE-VC-008: member role invalid enum

**Artifact:** `docs/fs/sage/vault_config.schema.json`
**Category:** invalid
**Constraint:** `vault.members[].role` must be one of [reader, editor, admin]

**Input:** `vault.members: [{user_id: "u1", role: "owner"}]`

**Expected:** FAIL -- enum violation
**Rationale:** "owner" is set at vault level, not as a member role.

### TEST-SAGE-VC-009: max_abstract_tokens below minimum

**Artifact:** `docs/fs/sage/vault_config.schema.json`
**Category:** invalid
**Constraint:** `abstraction.max_abstract_tokens` minimum is 50

**Input:** `abstraction.max_abstract_tokens: 49`

**Expected:** FAIL -- minimum violation
**Rationale:** Abstracts shorter than 50 tokens are not useful for retrieval.

### TEST-SAGE-VC-010: Additional property at top level

**Artifact:** `docs/fs/sage/vault_config.schema.json`
**Category:** invalid
**Constraint:** `additionalProperties: false` at root

**Input:** Valid config plus `extra_field: "value"` at root level.

**Expected:** FAIL -- additional property not allowed
**Rationale:** Strict schema prevents typos and drift.

### TEST-SAGE-VC-011: Additional property on vault object

**Artifact:** `docs/fs/sage/vault_config.schema.json`
**Category:** invalid
**Constraint:** `additionalProperties: false` on vault object

**Input:** `vault` object with extra key `vault.description: "test"`

**Expected:** FAIL -- additional property not allowed
**Rationale:** Vault object is closed; new fields require schema evolution.

### TEST-SAGE-VC-012: Missing required vault fields

**Artifact:** `docs/fs/sage/vault_config.schema.json`
**Category:** invalid
**Constraint:** vault requires id, name, owner, storage_root, brain_root, visibility

**Input:** `vault` with only `id` and `name` present.

**Expected:** FAIL -- multiple required properties missing
**Rationale:** All identity and storage fields are mandatory.

---

## document_types.schema.json

### TEST-SAGE-DT-001: Valid single doc type

**Artifact:** `docs/fs/sage/document_types.schema.json`
**Category:** valid
**Constraint:** Minimal valid doc_types array

**Input:**
```yaml
doc_types:
  - value: note
    label: "Note"
```

**Expected:** PASS
**Rationale:** Minimum viable document type configuration.

### TEST-SAGE-DT-002: Valid multiple doc types with descriptions

**Artifact:** `docs/fs/sage/document_types.schema.json`
**Category:** valid
**Constraint:** Multiple entries with optional description field

**Input:**
```yaml
doc_types:
  - value: design_spec
    label: "Design Spec"
    description: "Provisional design application"
  - value: glossary
    label: "Glossary"
    description: "Terminology authority"
```

**Expected:** PASS
**Rationale:** Normal multi-type configuration.

### TEST-SAGE-DT-003: Empty doc_types array

**Artifact:** `docs/fs/sage/document_types.schema.json`
**Category:** invalid
**Constraint:** `minItems: 1`

**Input:**
```yaml
doc_types: []
```

**Expected:** FAIL -- minItems violation
**Rationale:** A vault with no document types cannot classify anything.

### TEST-SAGE-DT-004: Missing required value field

**Artifact:** `docs/fs/sage/document_types.schema.json`
**Category:** invalid
**Constraint:** `value` and `label` are required

**Input:**
```yaml
doc_types:
  - label: "Note"
```

**Expected:** FAIL -- `'value' is a required property`
**Rationale:** Machine identifier is mandatory for metadata and pipeline routing.

### TEST-SAGE-DT-005: Value pattern violation -- uppercase

**Artifact:** `docs/fs/sage/document_types.schema.json`
**Category:** invalid
**Constraint:** `value` must match `^[a-z][a-z0-9_]*$`

**Input:**
```yaml
doc_types:
  - value: "BadPatternValue"
    label: "Design Spec"
```

**Expected:** FAIL -- pattern mismatch
**Rationale:** Consistent lowercase identifiers prevent case-sensitivity bugs.

### TEST-SAGE-DT-006: Additional property on doc type object

**Artifact:** `docs/fs/sage/document_types.schema.json`
**Category:** invalid
**Constraint:** `additionalProperties: false` on doc type items

**Input:**
```yaml
doc_types:
  - value: note
    label: "Note"
    category: "general"
```

**Expected:** FAIL -- additional property not allowed
**Rationale:** Extra fields suggest configuration intended for a different schema.

---

## lifecycle.schema.json

### TEST-SAGE-LC-001: Valid base states plus domain extension

**Artifact:** `docs/fs/sage/lifecycle.schema.json`
**Category:** valid
**Constraint:** Base states present with optional domain-specific additions

**Input:**
```yaml
states:
  - value: active
    label: "Active"
  - value: completed
    label: "Completed"
  - value: archived
    label: "Archived"
    is_terminal: true
  - value: filed
    label: "Filed"
    is_terminal: true
    description: "Submitted to USPTO"
transitions:
  - from_state: "(new)"
    action: ingest
    to_state: active
  - from_state: active
    action: supersede
    to_state: archived
    creates_edge: supersedes
  - from_state: active
    action: complete
    to_state: completed
  - from_state: active
    action: archive
    to_state: archived
  - from_state: archived
    action: reactivate
    to_state: active
  - from_state: active
    action: file
    to_state: filed
base_states_required: true
```

**Expected:** PASS
**Rationale:** Tests both base set and domain extension with creates_edge.

### TEST-SAGE-LC-002: Empty states array

**Artifact:** `docs/fs/sage/lifecycle.schema.json`
**Category:** invalid
**Constraint:** `states` has `minItems: 1`

**Input:**
```yaml
states: []
transitions:
  - from_state: "(new)"
    action: ingest
    to_state: active
base_states_required: true
```

**Expected:** FAIL -- minItems violation
**Rationale:** At least one state must exist.

### TEST-SAGE-LC-003: Empty transitions array

**Artifact:** `docs/fs/sage/lifecycle.schema.json`
**Category:** invalid
**Constraint:** `transitions` has `minItems: 1`

**Input:**
```yaml
states:
  - value: active
    label: "Active"
transitions: []
base_states_required: true
```

**Expected:** FAIL -- minItems violation
**Rationale:** A state machine without transitions is inert.

### TEST-SAGE-LC-004: State value pattern violation

**Artifact:** `docs/fs/sage/lifecycle.schema.json`
**Category:** invalid
**Constraint:** `states[].value` must match `^[a-z][a-z0-9_]*$`

**Input:**
```yaml
states:
  - value: "In-Review"
    label: "In Review"
transitions:
  - from_state: "(new)"
    action: ingest
    to_state: "In-Review"
base_states_required: true
```

**Expected:** FAIL -- pattern mismatch on state value
**Rationale:** Hyphens are not allowed in state identifiers (underscores only).

### TEST-SAGE-LC-005: Action pattern violation

**Artifact:** `docs/fs/sage/lifecycle.schema.json`
**Category:** invalid
**Constraint:** `transitions[].action` must match `^[a-z][a-z0-9_]*$`

**Input:** Transition with `action: "Do-Something"`

**Expected:** FAIL -- pattern mismatch on action
**Rationale:** Action identifiers follow the same naming rules as states.

### TEST-SAGE-LC-006: Missing required transition field

**Artifact:** `docs/fs/sage/lifecycle.schema.json`
**Category:** invalid
**Constraint:** Transitions require from_state, action, to_state

**Input:**
```yaml
states:
  - value: active
    label: "Active"
transitions:
  - from_state: "(new)"
    to_state: active
base_states_required: true
```

**Expected:** FAIL -- `'action' is a required property`
**Rationale:** Every transition must name its triggering action.

### TEST-SAGE-LC-007: Additional property on state object

**Artifact:** `docs/fs/sage/lifecycle.schema.json`
**Category:** invalid
**Constraint:** `additionalProperties: false` on state items

**Input:** State with extra key `color: "green"`.

**Expected:** FAIL -- additional property not allowed
**Rationale:** Display properties belong elsewhere; schema is structural.

### TEST-SAGE-LC-008: Duplicate ingestion transitions

**Artifact:** `docs/fs/sage/lifecycle.schema.json`
**Category:** invalid
**Constraint:** `transitions` allows exactly one `(new)` row (`maxContains: 1`)

**Input:** Two transitions with `from_state: "(new)"`, both `action: ingest`,
landing in different states.

**Expected:** FAIL -- too many items match the `(new)` contains schema
**Rationale:** The ingest landing state is read from the single `(new)` row;
two rows would make it ambiguous.

### TEST-SAGE-LC-009: Missing ingestion transition

**Artifact:** `docs/fs/sage/lifecycle.schema.json`
**Category:** invalid
**Constraint:** `transitions` requires one `(new)` row (`minContains: 1`)

**Input:** Transitions carrying only `active -> archive -> archived`, no
`(new)` row.

**Expected:** FAIL -- no item matches the `(new)` contains schema
**Rationale:** Without the `(new)` row the ingest landing state is undefined;
the engine refuses to guess one.

### TEST-SAGE-LC-010: Ingest action outside the ingestion transition

**Artifact:** `docs/fs/sage/lifecycle.schema.json`
**Category:** invalid
**Constraint:** `action: ingest` is reserved for the `(new)` row (item conditional)

**Input:**
```yaml
states:
  - value: active
    label: "Active"
  - value: archived
    label: "Archived"
    is_terminal: true
transitions:
  - from_state: "(new)"
    action: ingest
    to_state: active
  - from_state: archived
    action: ingest
    to_state: active
base_states_required: false
```

**Expected:** FAIL -- `from_state` must be `(new)` where `action` is `ingest`
**Rationale:** A user-invocable `ingest` row would let callers re-run the
ingestion transition on existing documents.

### TEST-SAGE-LC-011: Dependency-satisfaction declaration accepted

**Artifact:** `docs/fs/sage/lifecycle.schema.json`
**Category:** valid
**Constraint:** `states[].satisfies_dependency` accepts true, false, and null

**Input:** The LC-001 lifecycle with `satisfies_dependency: true` on `filed`,
`satisfies_dependency: false` on `completed`, and the field omitted elsewhere.

**Expected:** PASS
**Rationale:** The declaration drives the depends_on satisfaction set; omitted
or null defers to the engine default.

---

## metadata_extraction.schema.json

### TEST-SAGE-ME-001: Valid filename extraction with code_to_doc_type

**Artifact:** `docs/fs/sage/metadata_extraction.schema.json`
**Category:** valid
**Constraint:** Full filename extraction pipeline

**Input:**
```yaml
review_required: false
filename_extraction:
  pattern: "{date}_{project}_{code}_{title}_{version}"
  separator: "_"
  segment_fields:
    date: date
    project: project
    code: code
    title: title
    version: version_label
  code_to_doc_type:
    - code: "REF"
      title_contains: "Glossary"
      doc_type: glossary
    - code: "REF"
      doc_type: reference_document
```

**Expected:** PASS
**Rationale:** Tests compound key precedence pattern in code_to_doc_type.

### TEST-SAGE-ME-002: Valid content extraction rule

**Artifact:** `docs/fs/sage/metadata_extraction.schema.json`
**Category:** valid
**Constraint:** Content extraction with each method enum value

**Input:**
```yaml
review_required: true
content_extraction:
  - source_type: markdown
    target_field: title
    extraction_method: yaml_frontmatter_key
    extraction_key: title
  - source_type: docx
    target_field: version_label
    extraction_method: running_header
  - source_type: pdf
    target_field: author
    extraction_method: pdf_property
    extraction_key: Author
```

**Expected:** PASS
**Rationale:** Validates multiple extraction methods coexist.

### TEST-SAGE-ME-003: Invalid extraction_method enum

**Artifact:** `docs/fs/sage/metadata_extraction.schema.json`
**Category:** invalid
**Constraint:** `extraction_method` must be one of [yaml_frontmatter_key, running_header, pdf_property, regex, heading_content]

**Input:**
```yaml
review_required: false
content_extraction:
  - source_type: docx
    target_field: title
    extraction_method: "xpath_query"
```

**Expected:** FAIL -- enum violation
**Rationale:** Only supported extraction methods are accepted.

### TEST-SAGE-ME-004: Content extraction missing required fields

**Artifact:** `docs/fs/sage/metadata_extraction.schema.json`
**Category:** invalid
**Constraint:** Content extraction items require source_type, target_field, extraction_method

**Input:**
```yaml
review_required: false
content_extraction:
  - target_field: title
    extraction_method: running_header
```

**Expected:** FAIL -- `'source_type' is a required property`
**Rationale:** Extraction rules are adapter-specific; source_type scopes the rule.

### TEST-SAGE-ME-005: Missing required review_required

**Artifact:** `docs/fs/sage/metadata_extraction.schema.json`
**Category:** valid (note: review_required has default)
**Constraint:** review_required is the only required field; check that schema requires it

**Input:**
```yaml
filename_extraction:
  pattern: "{name}"
```

**Expected:** FAIL -- `'review_required' is a required property`
**Rationale:** Explicit decision about metadata trust is mandatory.

### TEST-SAGE-ME-006: code_to_doc_type entry missing doc_type

**Artifact:** `docs/fs/sage/metadata_extraction.schema.json`
**Category:** invalid
**Constraint:** code_to_doc_type items require doc_type

**Input:**
```yaml
review_required: false
filename_extraction:
  code_to_doc_type:
    - code: "REF"
```

**Expected:** FAIL -- `'doc_type' is a required property`
**Rationale:** Every mapping rule must specify the target doc_type.

---

## edge_inference.schema.json

### TEST-SAGE-EI-001: Valid tier 1 with inference rules

**Artifact:** `docs/fs/sage/edge_inference.schema.json`
**Category:** valid
**Constraint:** Tier 1 assignment with required inference rules

**Input:**
```yaml
tier_assignments:
  - edge_type: supersedes
    tier: 1
    inference_rules:
      - method: version_chain
```

**Expected:** PASS
**Rationale:** Tier 1 auto-create with deterministic method.

### TEST-SAGE-EI-002: Valid tier 3 without inference rules

**Artifact:** `docs/fs/sage/edge_inference.schema.json`
**Category:** valid
**Constraint:** Tier 3 manual-only does not require inference_rules

**Input:**
```yaml
tier_assignments:
  - edge_type: derived_from
    tier: 3
```

**Expected:** PASS
**Rationale:** Manual edges need no inference rules.

### TEST-SAGE-EI-003: Valid full config with all three tiers

**Artifact:** `docs/fs/sage/edge_inference.schema.json`
**Category:** valid
**Constraint:** Mixed tier assignments with staging config

**Input:**
```yaml
tier_assignments:
  - edge_type: supersedes
    tier: 1
    inference_rules:
      - method: version_chain
  - edge_type: covers
    tier: 2
    inference_rules:
      - method: filename_code_match
        source_segment: code
        target_segment: code
  - edge_type: derived_from
    tier: 3
staging_review_grouping: by_source_document
```

**Expected:** PASS
**Rationale:** Realistic multi-tier configuration with staging review grouping.

### TEST-SAGE-EI-004: Invalid edge_type enum

**Artifact:** `docs/fs/sage/edge_inference.schema.json`
**Category:** invalid
**Constraint:** `edge_type` must be one of [supersedes, derived_from, instantiated_from, covers, references, bundles_with, authoritative_for, depends_on, sync_target, retracts, merged_from]

**Input:**
```yaml
tier_assignments:
  - edge_type: "related_to"
    tier: 1
    inference_rules:
      - method: version_chain
```

**Expected:** FAIL -- enum violation
**Rationale:** Only defined edge types are accepted.

### TEST-SAGE-EI-005: Invalid tier enum

**Artifact:** `docs/fs/sage/edge_inference.schema.json`
**Category:** invalid
**Constraint:** `tier` must be one of [1, 2, 3]

**Input:**
```yaml
tier_assignments:
  - edge_type: supersedes
    tier: 4
```

**Expected:** FAIL -- enum violation
**Rationale:** Only three tiers exist.

### TEST-SAGE-EI-006: Invalid inference method enum

**Artifact:** `docs/fs/sage/edge_inference.schema.json`
**Category:** invalid
**Constraint:** `method` must be one of [version_chain, re_ingestion, filename_code_match, filename_co_location, content_reference]

**Input:**
```yaml
tier_assignments:
  - edge_type: supersedes
    tier: 1
    inference_rules:
      - method: "semantic_similarity"
```

**Expected:** FAIL -- enum violation
**Rationale:** Only supported inference methods are accepted.

### TEST-SAGE-EI-007: Invalid staging_review_grouping enum

**Artifact:** `docs/fs/sage/edge_inference.schema.json`
**Category:** invalid
**Constraint:** `staging_review_grouping` must be one of [by_edge_type, by_source_document, by_ingestion_batch]

**Input:**
```yaml
tier_assignments:
  - edge_type: supersedes
    tier: 1
    inference_rules:
      - method: version_chain
staging_review_grouping: "by_date"
```

**Expected:** FAIL -- enum violation
**Rationale:** Only defined grouping strategies are accepted.

---

## edge_type_registry.schema.json

### TEST-SAGE-ER-001: Valid registry covering every EdgeType value

**Artifact:** `docs/fs/sage/edge_type_registry.schema.json`
**Category:** valid
**Constraint:** Registry accepts an entry for every EdgeType with a legal ResolutionPolicy

**Input:**
```yaml
entries:
  - edge_type: supersedes
    resolution_policy: none
  - edge_type: retracts
    resolution_policy: none
  - edge_type: merged_from
    resolution_policy: none
  - edge_type: derived_from
    resolution_policy: transitive_source
  - edge_type: instantiated_from
    resolution_policy: transitive_both
  - edge_type: covers
    resolution_policy: transitive_both
  - edge_type: references
    resolution_policy: transitive_both
  - edge_type: bundles_with
    resolution_policy: transitive_both
  - edge_type: depends_on
    resolution_policy: transitive_both
  - edge_type: authoritative_for
    resolution_policy: TBD
  - edge_type: sync_target
    resolution_policy: TBD
```

**Expected:** PASS
**Rationale:** The frozen CAS-ADR-017 assignment set must validate.

### TEST-SAGE-ER-002: Invalid edge_type value

**Artifact:** `docs/fs/sage/edge_type_registry.schema.json`
**Category:** invalid
**Constraint:** `edge_type` must match EdgeType enum

**Input:** Entry with `edge_type: "related_to"`.

**Expected:** FAIL -- enum violation.
**Rationale:** Registry must stay in sync with the authoritative EdgeType enum.

### TEST-SAGE-ER-003: Invalid resolution_policy value

**Artifact:** `docs/fs/sage/edge_type_registry.schema.json`
**Category:** invalid
**Constraint:** `resolution_policy` must be one of [none, transitive_source, transitive_target, transitive_both, TBD]

**Input:** Entry with `edge_type: covers, resolution_policy: "inherit"`.

**Expected:** FAIL -- enum violation.

### TEST-SAGE-ER-004: Missing required fields

**Artifact:** `docs/fs/sage/edge_type_registry.schema.json`
**Category:** invalid
**Constraint:** Each entry requires `edge_type` and `resolution_policy`

**Input:** Entry with only `edge_type: covers`.

**Expected:** FAIL -- `'resolution_policy' is a required property`.

### TEST-SAGE-ER-005: Empty entries array rejected

**Artifact:** `docs/fs/sage/edge_type_registry.schema.json`
**Category:** invalid
**Constraint:** `entries` has `minItems: 1`

**Input:** `{ "entries": [] }`

**Expected:** FAIL -- minItems violation.
**Rationale:** A registry with no entries is a configuration mistake, not a valid vault state.

### TEST-SAGE-ER-006: Additional property on registry entry rejected

**Artifact:** `docs/fs/sage/edge_type_registry.schema.json`
**Category:** invalid
**Constraint:** `additionalProperties: false` on entry items

**Input:** Entry with `edge_type: covers, resolution_policy: transitive_both, inherit_from: foo`.

**Expected:** FAIL -- additionalProperties violation.

---

## decision_log.schema.json

### TEST-SAGE-DL-001: Valid decision log with single entry

**Artifact:** `docs/fs/sage/decision_log.schema.json`
**Category:** valid
**Constraint:** All required fields present on log and entry

**Input:**
```yaml
agent_id: glossary_steward
agent_type: steward
owned_artifact_id: "doc-glossary-001"
entries:
  - entry_id: "DL-001"
    timestamp: "2026-03-30T14:00:00Z"
    category: not_acted
    summary: "Evaluated glossary update; no changes warranted"
    context: "Triggered by PV06 conformance audit"
    rationale: "All terms in PV06 already match glossary v8.4"
```

**Expected:** PASS
**Rationale:** Minimal valid steward decision log.

### TEST-SAGE-DL-002: Valid orchestrator log with cross-artifact reasoning

**Artifact:** `docs/fs/sage/decision_log.schema.json`
**Category:** valid
**Constraint:** Orchestrator with null owned_artifact_id and cross_artifact category

**Input:**
```yaml
agent_id: pipeline_orchestrator
agent_type: orchestrator
owned_artifact_id: null
entries:
  - entry_id: "DL-001"
    timestamp: "2026-03-30T15:00:00Z"
    category: cross_artifact_reasoning
    summary: "Sequenced PV06 before PV07 for conformance"
    context: "Both reports ready for conformance stage"
    rationale: "PV06 is closer to filing deadline; prioritize to clear the pipeline"
    related_document_ids: ["doc-pv06", "doc-pv07"]
    workflow_run_id: "wf-run-001"
    tags: ["pipeline-sequencing", "filing-readiness"]
```

**Expected:** PASS
**Rationale:** Tests optional fields: related_document_ids, workflow_run_id, tags.

### TEST-SAGE-DL-003: Invalid agent_type enum

**Artifact:** `docs/fs/sage/decision_log.schema.json`
**Category:** invalid
**Constraint:** `agent_type` must be one of [steward, orchestrator]

**Input:** `agent_type: "tool"`

**Expected:** FAIL -- enum violation
**Rationale:** Two-type agent taxonomy per CAS-ADR-010.

### TEST-SAGE-DL-004: Invalid category enum

**Artifact:** `docs/fs/sage/decision_log.schema.json`
**Category:** invalid
**Constraint:** `category` must be one of [not_acted, deferred, cross_artifact_reasoning]

**Input:** Entry with `category: "completed"`

**Expected:** FAIL -- enum violation
**Rationale:** Decision logs capture gaps, not completions.

### TEST-SAGE-DL-005: Missing required entry fields

**Artifact:** `docs/fs/sage/decision_log.schema.json`
**Category:** invalid
**Constraint:** Entries require entry_id, timestamp, category, summary, context, rationale

**Input:** Entry with only `entry_id` and `timestamp`.

**Expected:** FAIL -- multiple required properties missing
**Rationale:** Every decision must record why and in what context.

---

## sage_core_api.openapi.yaml

### TEST-SAGE-API-001: OpenAPI spec loads as valid YAML

**Artifact:** `docs/fs/sage/sage_core_api.openapi.yaml`
**Category:** valid
**Constraint:** File parses as YAML with openapi version 3.1.0

**Input:** Load `docs/fs/sage/sage_core_api.openapi.yaml`

**Expected:** `openapi` == `"3.1.0"`, `info.title` == `"SAGE Core API"`
**Rationale:** Basic structural validity.

### TEST-SAGE-API-002: RETIRED (was: All 14 operations present)

Asserted an exhaustive, hand-maintained list of fourteen path/method pairs.
The list duplicated a mechanical gate and drifted: the spec declares
forty-three operations, and two of the fourteen named per-document shapes
that the CAS-ADR-029 plural-noun rename collapsed into
`POST /sage_vaults/{vault_id}/lifecycles` and
`POST /sage_vaults/{vault_id}/metadata`. Operation coverage is carried by
`tests/sage/test_openapi_conformance.py::test_spec_covers_all_app_operations`,
which asserts that the spec's path/method set equals the FastAPI route set in
both directions, with `SPEC_FORWARD_DECLARATIONS` as the only allowlist. Any
future enumeration of the surface belongs in that module, never here.

### TEST-SAGE-API-003: DELETE declared only on the edge-unlink path

**Artifact:** `docs/fs/sage/sage_core_api.openapi.yaml`
**Category:** valid
**Constraint:** No-delete invariant for documents (SAGE Architecture Reference §6.4)

**Input:** Scan all paths for the `delete` method.

**Expected:** The only operation using `delete` is `DELETE /sage_vaults/{vault_id}/edges/{edge_id}`. No path under `/sage_vaults/{vault_id}/documents` declares `delete`.
**Rationale:** The no-delete invariant applies to documents, which are superseded or archived, never destroyed. Edges are not documents: an edge whose relationship is no longer correct is unlinked, and the spec header states the invariant in exactly those terms. `tests/sage/test_openapi_conformance.py::test_delete_is_declared_only_on_the_edge_unlink_path` pins the set of paths declaring `delete` to exactly that one, so a DELETE introduced on any other subtree fails the gate.

### TEST-SAGE-API-004: Vault-scoped paths, with the process-scoped exceptions enumerated

**Artifact:** `docs/fs/sage/sage_core_api.openapi.yaml`
**Category:** valid
**Constraint:** Vault isolation per architecture

**Input:** Scan all path keys.

**Expected:** All paths but three start with `/sage_vaults/{vault_id}/`. The exceptions are `/sage_vaults` (the vault collection: list and create) and the transfer endpoints `/upload` and `/download/{transfer_id}`, whose vault binding lives in the one-time transfer token rather than in the URL.
**Rationale:** Every document, graph, and maintenance operation is scoped to a single vault. `tests/sage/test_openapi_conformance.py::test_only_the_vault_collection_and_transfer_paths_sit_outside_vault_scope` asserts that the set of paths outside the prefix equals exactly these three, so a new top-level path and a dropped exception both fail that gate; `test_specs_respect_url_prefix_boundaries` separately keeps the transfer paths from migrating out of the SAGE Core API spec.

### TEST-SAGE-API-005: EdgeType enum includes chain-resolution meta-edges

**Artifact:** `docs/fs/sage/sage_core_api.openapi.yaml` (EdgeType schema)
**Category:** valid
**Constraint:** EdgeType enum is exactly [supersedes, derived_from, instantiated_from, covers, references, bundles_with, authoritative_for, depends_on, sync_target, retracts, merged_from]

**Input:** Load `components.schemas.EdgeType.enum`.

**Expected:** Enum set equals the constraint list (order-insensitive).
**Rationale:** CAS-ADR-017 adds `instantiated_from`, `retracts`, `merged_from`. Drift here breaks registry validation.

### TEST-SAGE-API-006: ResolutionPolicy enum defined

**Artifact:** `docs/fs/sage/sage_core_api.openapi.yaml` (ResolutionPolicy schema)
**Category:** valid
**Constraint:** ResolutionPolicy enum is exactly [none, transitive_source, transitive_target, transitive_both, TBD]

**Input:** Load `components.schemas.ResolutionPolicy.enum`.

**Expected:** Enum set equals the constraint list.
**Rationale:** Registry and Edge both reference this enum; drift corrupts resolution behavior.

### TEST-SAGE-API-007: Edge schema carries anchor + retraction fields

**Artifact:** `docs/fs/sage/sage_core_api.openapi.yaml` (Edge schema)
**Category:** valid
**Constraint:** Edge defines `resolution_policy` (nullable, not required), `source_valid_from_version`, `target_valid_from_version`, `valid_until_version`, `retracted_edge_id` (all nullable), and `target_id` is nullable.

**Input:** Load `components.schemas.Edge`.

**Expected:**
- `resolution_policy` exists, references `ResolutionPolicy`, and has `nullable: true`; it is absent from `required`. The row carries a frozen copy of the registry's policy at creation time, and the traversal-node Edge views are constructed without one.
- Properties `source_valid_from_version`, `target_valid_from_version`, `valid_until_version`, `retracted_edge_id` exist with `nullable: true`.
- `target_id` has `nullable: true`.

**Rationale:** CAS-ADR-017 depends on these fields being present at the contract boundary so Pydantic derivation and the migration script can key off a single source of truth.

### TEST-SAGE-API-008: LinkRequest carries anchor + retracted_edge_id fields

**Artifact:** `docs/fs/sage/sage_core_api.openapi.yaml` (LinkRequest schema)
**Category:** valid
**Constraint:** LinkRequest defines `source_valid_from_version`, `target_valid_from_version`, `retracted_edge_id` (all nullable); `target_id` is nullable; only `source_id` and `edge_type` remain required.

**Input:** Load `components.schemas.LinkRequest`.

**Expected:**
- `required` equals `["source_id", "edge_type"]`.
- The three anchor/retraction fields exist and are nullable.
- `target_id` is nullable.

**Rationale:** Retracts edges omit target_id; all other edges still require it via write-time validation.

### TEST-SAGE-API-009: TraverseRequest carries debug flag

**Artifact:** `docs/fs/sage/sage_core_api.openapi.yaml` (TraverseRequest schema)
**Category:** valid
**Constraint:** TraverseRequest.debug is a boolean with default false.

**Input:** Load `components.schemas.TraverseRequest`.

**Expected:** `properties.debug.type == "boolean"` and `default == false`.

### TEST-SAGE-API-010: TraverseResponse defines resolution_path and ResolutionPathEntry

**Artifact:** `docs/fs/sage/sage_core_api.openapi.yaml`
**Category:** valid
**Constraint:** TraverseResponse.resolution_path is a nullable array of ResolutionPathEntry; ResolutionPathEntry.event_type enum equals [anchor_hit, anchor_miss, retracts_applied, tombstone_applied].

**Input:** Load `components.schemas.TraverseResponse` and `components.schemas.ResolutionPathEntry`.

**Expected:** resolution_path is nullable array of `$ref: ResolutionPathEntry`; event_type enum matches exactly.
