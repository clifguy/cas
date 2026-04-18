# Vault Management Test Specifications

Tier 2 behavioral tests for vault-management MCP tools and the REST
force-gate refinement to `PUT /sage_vaults/{vault_id}/config`.

Three new MCP tools are added to the SAGE MCP server:

- `sage_create_vault` — create a new vault (convenience or full-config form)
- `sage_get_vault_config` — read the current config
- `sage_update_vault_config` — section-level update with destructive-change
  gate

Design decisions encoded here:

- **No delete tool.** Destroying vaults is out of scope for agentic callers.
- **Two-mode create:** `sage_create_vault` accepts either the convenience
  triple (`vault_id`, `name`, `owner`) or a full `config` dict, but never
  both. The convenience branch builds a default config via the shared
  `_get_default_config(...)` helper so agents never have to assemble the
  nested structure by hand. The full-config branch is the escape hatch for
  callers that already have a complete dict.
- **Echo-back on create.** The tool return value includes the full written
  config. This eliminates an extra read round-trip when an agent wants to
  follow up with `sage_update_vault_config` to adjust specific sections.
- **Full-section replacement.** `sage_update_vault_config` replaces provided
  top-level sections wholesale; omitted sections are preserved unchanged.
  Partial-section merges are not supported because list-valued fields
  (doc_types, lifecycle states, transitions, adapters) have no unambiguous
  merge semantics. The tool description states this prominently.
- **Destructive-change gate.** If the merged config removes a doc_type or
  lifecycle state that still has documents, the update is blocked by
  default. Callers must pass `force=True` to proceed; the original
  warnings are returned alongside the success response. This aligns with
  SAGE's halt-and-report posture for other preconditions.
- **Identity is immutable.** Changing `vault.id` is rejected
  unconditionally regardless of `force`. Creating a new vault is the
  correct path.
- **REST parity.** The same force-gate is applied to
  `PUT /sage_vaults/{vault_id}/config` via a `force=true` query parameter.
  Both surfaces behave identically.

Shared helpers for config validation, atomic YAML write, default-config
construction, and destructive-change detection are extracted to
`sage/vault_management.py` so the REST router and MCP tools call the same
code.

---

## 1. sage_create_vault

### TEST-APP-MCP-030: convenience mode creates vault with default config

**Artifact:** `sage/mcp_server.py`, `sage/sage_api_tools.py`, `sage/vault_management.py`
**Category:** mcp_tool, sage_api

**Decision:** Passing `vault_id`, `name`, and `owner` (with `config=None`)
generates a minimal valid default config via the shared helper, creates
the vault directory tree under `~/sage_vaults/{vault_id}/`, writes
`vault_config.yaml` atomically, initializes services, and registers the
vault in the MCP registry. The return value includes the full written
config.

**Precondition:** Empty or non-colliding MCP vault registry. A temp
vaults-root override in place so the test does not touch the real
`~/sage_vaults/`.

**Input:** Call `sage_create_vault(vault_id="new_vault", name="New Vault", owner="testuser")`.

**Expected:**
- Return value is a dict with `vault_id == "new_vault"`, `name == "New Vault"`, `storage_root` populated, and a `config` key containing the full written config.
- The returned `config` passes `VaultConfig.model_validate(...)`.
- A subsequent `sage_list_vaults()` includes the new vault.
- The file `vault_config.yaml` exists at the vault directory and matches the echoed config.

**Rationale:** Agents need a zero-effort path to create a scratch or
project vault mid-conversation without hand-assembling a config dict.

### TEST-APP-MCP-031: convenience mode rejects duplicate vault_id

**Artifact:** `sage/sage_api_tools.py`
**Category:** mcp_tool, sage_api

**Decision:** If the target `vault_id` is already registered, the tool
returns a `vault_already_exists` structured error instead of overwriting.

**Precondition:** A vault `existing` is registered.

**Input:** Call `sage_create_vault(vault_id="existing", name="x", owner="x")`.

**Expected:**
- Return value is an error dict with `error == "vault_already_exists"`.
- The existing vault's config is unchanged.

**Rationale:** Accidental re-creation must not silently overwrite.
Creation is distinct from update.

### TEST-APP-MCP-032: rejects mixed-mode call (convenience args + config)

**Artifact:** `sage/sage_api_tools.py`
**Category:** mcp_tool, sage_api

**Decision:** If the caller passes both a `config` dict and any of the
convenience arguments (`vault_id`, `name`, `owner`), the tool returns a
validation error. Two sources of truth for the same field are a trap.

**Precondition:** None.

**Input:** Call `sage_create_vault(vault_id="v", name="n", owner="o", config={...})`.

**Expected:**
- Return value is an error dict with `error` indicating the mode
  conflict.
- No vault is created or registered.

**Rationale:** Explicit over implicit. The caller must commit to one
mode.

### TEST-APP-MCP-033: full-config mode creates vault from dict

**Artifact:** `sage/sage_api_tools.py`, `sage/vault_management.py`
**Category:** mcp_tool, sage_api

**Decision:** Passing only a `config` dict (with all convenience args
`None`) validates the dict, creates the directory tree from
`config.vault.id`, writes YAML, initializes services, registers, and
bootstraps the owner. The return value echoes the written config.

**Precondition:** Empty registry. `config.vault.id` is unique.

**Input:** Call `sage_create_vault(config=minimal_full_config_dict)`.

**Expected:**
- Return value has `vault_id == config["vault"]["id"]` and echoes the
  full config.
- Vault is listed by `sage_list_vaults()`.

**Rationale:** Full-config callers (e.g. a management script restoring a
known-good dict) need a path that bypasses defaults.

### TEST-APP-MCP-034: full-config mode rejects invalid config

**Artifact:** `sage/sage_api_tools.py`, `sage/vault_management.py`
**Category:** mcp_tool, sage_api

**Decision:** Invalid configs produce a `vault_config_validation_error`
with a list of concrete validation failures. No partial side effects
(no directory created, no registry mutation).

**Precondition:** None.

**Input:** Call `sage_create_vault(config={"vault": {"id": "bad"}})` (missing required sections).

**Expected:**
- Return value is an error dict with `error == "vault_config_validation_error"`.
- The error's `detail.errors` is a non-empty list of field paths.
- No vault directory is created.

**Rationale:** Agents can inspect the error list to understand what to
add to the config.

---

## 2. sage_get_vault_config

### TEST-APP-MCP-035: returns full config; errors on unknown vault

**Artifact:** `sage/sage_api_tools.py`
**Category:** mcp_tool, sage_api

**Decision:** `sage_get_vault_config(vault_id)` returns
`services.config.model_dump()`. Unknown `vault_id` returns an
`unknown_vault` error.

**Precondition:** A vault `test_vault` is registered.

**Input:**
- Call `sage_get_vault_config("test_vault")`.
- Call `sage_get_vault_config("nonexistent")`.

**Expected:**
- First call returns a dict containing all required sections
  (`vault`, `document_types`, `lifecycle`, `source_adapters`,
  `metadata_extraction`, `edge_inference`).
- `result["vault"]["id"] == "test_vault"`.
- Second call returns an error dict with `error == "unknown_vault"`.

**Rationale:** Agents need to inspect current config before proposing
an update.

---

## 3. sage_update_vault_config

### TEST-APP-MCP-036: updates a section and preserves others

**Artifact:** `sage/sage_api_tools.py`, `sage/vault_management.py`
**Category:** mcp_tool, sage_api

**Decision:** Passing `sections={"document_types": {...}}` replaces the
`document_types` section wholesale. All other sections remain unchanged.
The tool rewrites `vault_config.yaml` atomically and reloads the vault
in the registry.

**Precondition:** Vault `test_vault` registered with a known
`lifecycle` section.

**Input:** Call `sage_update_vault_config("test_vault", sections={"document_types": {"doc_types": [{"value": "note", "label": "Note"}, {"value": "memo", "label": "Memo"}, {"value": "extra", "label": "Extra"}]}})`.

**Expected:**
- Return value is `{"status": "updated", "vault_id": "test_vault", "warnings": []}`.
- Subsequent `sage_get_vault_config("test_vault")` shows the new
  doc_types list.
- The `lifecycle`, `source_adapters`, `metadata_extraction`,
  `edge_inference` sections are byte-equal to their pre-update values.

**Rationale:** Verifies the wholesale-section-replace semantic and the
preservation of untouched sections.

### TEST-APP-MCP-037: blocks destructive change without force

**Artifact:** `sage/sage_api_tools.py`, `sage/vault_management.py`
**Category:** mcp_tool, sage_api

**Decision:** If the merged config removes a doc_type or lifecycle
state that has existing documents and `force` is not set, the tool
returns a `destructive_config_change` error that lists the affected
items and counts. No YAML write, no registry reload.

**Precondition:** Vault `test_vault` has at least one document with
`doc_type == "note"`.

**Input:** Call `sage_update_vault_config("test_vault", sections={"document_types": {"doc_types": [{"value": "memo", "label": "Memo"}]}})` (removes `note`).

**Expected:**
- Return value is an error dict with `error == "destructive_config_change"`.
- `detail.warnings` (or equivalent) is a list containing at least one
  entry mentioning `note` and the document count.
- The on-disk `vault_config.yaml` is unchanged.
- A follow-up `sage_get_vault_config` still shows the old doc_types list.

**Rationale:** Silent removal of in-use doc_types is how metadata rot
accumulates. The default must be safe.

### TEST-APP-MCP-038: force=True proceeds past destructive change

**Artifact:** `sage/sage_api_tools.py`, `sage/vault_management.py`
**Category:** mcp_tool, sage_api

**Decision:** With `force=True`, the same update proceeds. The response
includes the warnings that would have blocked the default path.

**Precondition:** Same as MCP-037.

**Input:** Call `sage_update_vault_config("test_vault", sections={...removes note...}, force=True)`.

**Expected:**
- Return value is `{"status": "updated", "vault_id": "test_vault", "warnings": [...]}`.
- `warnings` is a non-empty list mentioning `note`.
- `sage_get_vault_config` shows the new (narrower) doc_types list.

**Rationale:** The caller has explicitly accepted the consequences.
Evidence of the destructive change is still surfaced in the response
for audit.

### TEST-APP-MCP-039: vault.id change rejected even with force

**Artifact:** `sage/sage_api_tools.py`, `sage/vault_management.py`
**Category:** mcp_tool, sage_api

**Decision:** `vault.id` cannot be changed via update at all. Renaming
is conceptually a new vault. `force` does not override this.

**Precondition:** Vault `test_vault` registered.

**Input:** Call `sage_update_vault_config("test_vault", sections={"vault": {"id": "different_id", ...}}, force=True)`.

**Expected:**
- Return value is a `vault_config_validation_error` with a message
  mentioning `vault.id`.
- No YAML rewrite, no registry change.

**Rationale:** Vault identity is a primary key; mutating it in place
would desynchronize the registry key, the YAML, and references in
other systems.

### TEST-APP-MCP-040: invalid section rejected

**Artifact:** `sage/sage_api_tools.py`
**Category:** mcp_tool, sage_api

**Decision:** If the merged config fails Pydantic validation (e.g.
`lifecycle.states` is not a list), the tool returns
`vault_config_validation_error` with field-path error messages. No
YAML write.

**Precondition:** Vault `test_vault` registered.

**Input:** Call `sage_update_vault_config("test_vault", sections={"lifecycle": {"states": "not_a_list"}})`.

**Expected:**
- Error dict with `error == "vault_config_validation_error"`.
- `detail.errors` is a non-empty list.

**Rationale:** Schema violations must never reach disk.

---

## 4. REST endpoint parity

These tests update the existing
`tests/sage/test_vault_config_api.py` to verify the force-gate on
`PUT /sage_vaults/{vault_id}/config`.

### TEST-SAGE-VM-REST-001: destructive update without force returns 409

**Artifact:** `sage/api/routers/vaults.py`
**Category:** rest_api, sage_api

**Decision:** When the update would remove an in-use doc_type or
lifecycle state and the `force` query parameter is absent or false,
the endpoint returns HTTP 409 Conflict with a
`destructive_config_change` error body listing affected items. No YAML
write.

**Replaces:** `test_update_config_warns_orphan` (current behavior:
warns-but-proceeds).

**Precondition:** Vault `test_vault` has a document with doc_type `note`.

**Input:** `PUT /sage_vaults/test_vault/config` with body removing `note` from doc_types; no `force` query param.

**Expected:**
- Response status 409.
- Body `code == "destructive_config_change"`.
- Body `detail.warnings` lists the affected doc_types/states with
  counts.
- Follow-up `GET /sage_vaults/test_vault/config` returns the old
  doc_types list.

**Rationale:** REST and MCP must behave identically. Warnings-only was
inconsistent with the halt-and-report posture elsewhere in the API.

### TEST-SAGE-VM-REST-002: destructive update with force=true returns 200 + warnings

**Artifact:** `sage/api/routers/vaults.py`
**Category:** rest_api, sage_api

**Decision:** With `?force=true`, the destructive update proceeds and
returns 200 with the warnings in the response body.

**Precondition:** Same as REST-001.

**Input:** `PUT /sage_vaults/test_vault/config?force=true` with body removing `note`.

**Expected:**
- Response status 200.
- `body["status"] == "updated"`.
- `body["warnings"]` is non-empty and mentions `note`.
- Follow-up `GET` shows the new (narrower) list.

**Rationale:** Parity with MCP-038.

### TEST-SAGE-VM-REST-003: non-destructive update ignores force flag

**Artifact:** `sage/api/routers/vaults.py`
**Category:** rest_api, sage_api

**Decision:** A benign update (e.g. renaming the vault, adding a new
doc_type) returns 200 with empty warnings regardless of whether
`force=true` is passed. The flag is a safety gate, not a mode
selector.

**Precondition:** Vault `test_vault`.

**Input:** `PUT /sage_vaults/test_vault/config` and `?force=true`,
both with body renaming the vault.

**Expected:**
- Both calls return 200.
- Both responses have `warnings == []`.

**Rationale:** `force` is ignored when there is nothing destructive to
force.
