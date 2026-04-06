# CAS: Clif's Agentic System

A personal experimental agentic ecosystem with three core components:

- **SAGE** (Salience-Aware Graph Engine) — knowledge graph, document store, and retrieval subsystem.
- **ROOT Harness** (Runtime for Orchestration, Operations, and Testing) — LangGraph-based orchestration and agent management.
- **CAS Application** — HTML5 human-facing web client for oversight, approval, and browsing.

## Repository Layout

```
docs/
  fs/              # Formal Substrate: JSON Schema + OpenAPI specs (CAS-ADR-008)
    manifest.json  # Schema inventory and version tracking
    sage/          # SAGE config schemas + Core API OpenAPI
    root_harness/  # ROOT Harness config schemas + Orchestration API OpenAPI
  ref/             # Architecture reference documents (.docx)
domains/
  pim_health/      # PIM Health ROOT Harness configs (agents, pipeline, policies, workflows)
sage/              # SAGE source code (Core API, adapters, storage layer)
root_harness/      # ROOT Harness source code (orchestration, agents, workflows)
app/               # CAS Application (HTML5 client)
tests/             # Test plan and test code
```

SAGE vault configurations live outside the repository at `~/sage_vaults/{vault_id}/vault_config.yaml`. Each vault is self-describing; its config is the source of truth for vault identity, document types, lifecycle, adapters, metadata extraction, and edge inference.

## Reference Data (Read On Demand)

Do not memorize these; read them when you need current project state:

- **Project status:** `CAS_Project_Tracker.md` (root directory) — artifact versions, pending work, sequence, open considerations.
- **Architectural decisions:** `docs/cas_adr_store.json` — all CAS ADRs with full rationale. Single source of truth for design decisions.
- **Schema inventory:** `docs/fs/manifest.json` — what schemas exist, their versions, and roles.
- **Architecture documents:** `docs/ref/*.docx` — read with `pandoc -t plain` when you need architectural detail. These are Word files; do not attempt to edit them in Claude Code.

## Standing Architectural Principles

These govern all implementation decisions:

1. **Single source of truth.** Every concept has one authoritative home. Do not duplicate data across files.
2. **Pointer direction.** Architecture documents point to the Formal Substrate and ADR store. Neither points back.
3. **Format follows access pattern.** Choose storage format based on the primary consumer. JSON Schema for validators/code generators. YAML for agents/humans reading configuration. Word for reference documentation.
4. **SAGE indexes everything and owns nothing.** SAGE is the state substrate. It does not contain business logic or orchestration decisions.
5. **Boundary rule.** Stewards call SAGE for artifact operations. Orchestrators access artifacts through stewards. Orchestrators access SAGE directly only for their own working state.
6. **Two-type agent taxonomy.** Stewards own canonical artifacts. Orchestrators own coordination lifecycles. Everything else is a tool.
7. **Planning-then-execution separation.** Do not mix planning and execution in a single context. Pre-execution plan critique is sound; post-completion self-evaluation is a known failure mode.
8. **Pydantic models derived from schemas.** Never author Pydantic models independently; derive them from the JSON Schema files in `docs/fs/`.

## Technology Stack

- **Python 3.12+**, venv, pip
- **FastAPI** + uvicorn for both SAGE Core API and ROOT Harness Orchestration API
- **LangGraph** for workflow orchestration (checkpoint-and-resume with separate SQLite store)
- **LanceDB** (embedded) for vector/full-text content store
- **SQLite** for SAGE graph store and ROOT Harness checkpoint store
- **nomic-embed-text** via sentence-transformers (768-dimensional embeddings, local)
- **Qwen3-30B-A3B-Instruct-2507** via MLX for semantic abstract generation (local)
- **JSON Schema draft 2020-12** for all schemas
- **OpenAPI 3.1.0** for API specifications
- **Pydantic v2** for runtime models (derived from schemas)
- **pytest** for testing

## Coding Conventions

- Type hints on all function signatures.
- Pydantic models for all API request/response bodies.
- Keep SAGE and ROOT Harness imports cleanly separated. ROOT Harness imports SAGE's Core API in Phase 1 (in-process), but the code should be structured as if they communicate over HTTP.
- Database files (SQLite, LanceDB) must never be stored inside cloud-synced directories or inside the Git repository. Paths are configured in `sage/config.yaml` and `root_harness/config.yaml`.
- API keys are environment variables, never committed. `.env` files are gitignored.

## Workflow

- **Proposal-then-approval.** Present what you plan to do; wait for `;;yp` before executing.
- **Downstream impact identification.** When changing a schema, API contract, or architectural element, identify all affected files across the repo.
- **ADR capture.** When a design decision resolves an open question or establishes a new architectural constraint, flag it with `;;flag` for ADR capture.
- **Structured handoffs.** If a task spans multiple sessions, update the project tracker before ending.
