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

- **Architectural decisions:** cas SAGE vault, `doc_type=adr` (28 ADRs). Use `sage_discover` with `filters={"doc_type": "adr"}` and `sage_get_document` to read full rationale. Cross-references between ADRs are graph edges (`supersedes`, `references`); follow with `sage_traverse`. Single source of truth for CAS architecture decisions per CAS-ADR-024. Authoring scope (CAS-only, not generic methodology) is governed by the *CAS ADR Authoring Conventions* steering document (`doc_type=steering_document`) — read it before proposing an ADR.
- **Work tickets:** cas SAGE vault, `doc_type=ticket`. Conventions (metadata shape — Tier-1 fields plus `tier3_metadata` with `ticket_id` / `ticket_type` / `ticket_priority` per CAS-ADR-028 — write discipline, naming, granularity) and the canonical query patterns (open tickets, by-priority, by-id, dependencies, expressed as `filters={"tier3": {...}}` clauses on `sage_discover`) live in the *CAS Ticket Conventions* steering document in the same vault — read it before authoring or querying tickets. Vault-resident; no filesystem source. Replaces the legacy project-tracker workflow.
- **Failure log:** cas SAGE vault, `doc_type=failure_record`. Schema (Tier-1 fields plus `tier3_metadata` with `failure_id` / `failure_class` / `severity` / `observed_by` / `caught_by_gate` / commit fields per CAS-ADR-028), write discipline, and the canonical query patterns (open failures, by-class, by-severity, by-id, baselines, expressed as `filters={"tier3": {...}}` clauses) live in the *CAS Failure Record Conventions* steering document — read it before authoring or querying failure records. Per-commit measurement; failure-introduction rate and gate-catch rate are the two headline metrics. Seed entries F1–F5 derive from the *AI-First SDLC Tooling Survey* (`doc_type=reference_document`) and are linked to it via `references` edges.
- **Schema inventory:** `docs/fs/manifest.json` — what schemas exist, their versions, and roles.
- **Architecture documents:** Ingested into a dedicated SAGE vault for CAS portfolio documentation. Use SAGE MCP tools (`sage_discover`, `sage_get_document`, `sage_read_section`, `sage_traverse`) to read or query them. Source `.docx` files are no longer in the repo; see `docs/ref/README.md` for vault id and access details.

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

Python (venv, pip) with FastAPI + uvicorn for both SAGE Core API and ROOT Harness Orchestration API; LangGraph for workflow orchestration; LanceDB for vector/full-text content; SQLite for SAGE graph store and ROOT Harness checkpoint store; nomic-embed-text (local) for embeddings; Qwen3 via MLX (local) for semantic abstract generation; JSON Schema for schemas, OpenAPI for API specs, Pydantic v2 for runtime models, pytest for testing. Specific versions live in `pyproject.toml` and `docs/fs/manifest.json`.

## Local Environment

This is a single-developer Mac setup, not a portable Linux service. Adapt commands and assumptions accordingly.

- **Platform.** macOS 26.4.1 (Darwin 25.4) on Apple Silicon (Mac16,11), 14 cores, 64 GB unified memory. Homebrew at `/opt/homebrew`. No `apt`/`yum`/`/proc`. Use `pbcopy`/`pbpaste`, `sysctl`, `launchctl`. Default shell is zsh.
- **Python.** 3.14.3 via Homebrew. **Always use the project venv at `.venv/`** — invoke as `.venv/bin/python` or activate it; never the system Python. The CAS package is installed editable (`__editable__.cas-0.1.0.pth`), so edits under `sage/`, `root_harness/`, `app/` take effect without reinstall. Re-run `pip install -e ".[test,mlx]"` only after changing `pyproject.toml` or adding new top-level packages.
- **MCP setup.** The SAGE MCP server is configured in `~/.claude/settings.json` and runs from `.venv/bin/python -m sage.mcp_server`, auto-discovering vaults under `~/sage_vaults/` (the `test` vault is used by the SAGE test suite). **Edits to SAGE MCP server code require a Claude Code restart** to take effect — the running server holds the old import.
- **RAM budget.** With Qwen3-30B loaded via MLX and in active use, the full Python process (model, LanceDB, SQLite, uvicorn) runs around 38 GB. The 64 GB total leaves roughly 26 GB for the rest of the system — macOS, browser, IDE, MCP servers, other tools. Workable but not generous. **Flag any suggestion that would spin up a second concurrent model, hold two model contexts simultaneously, or move to a significantly larger model** — there is no budget for it.
- **Stale-state pitfalls.** LanceDB and SQLite stores live outside the repo (paths in `sage/config.yaml`); `git reset`/`git clean` won't touch them. SAGE vault configs at `~/sage_vaults/{vault_id}/vault_config.yaml` are outside both the repo and cloud sync. Long-running uvicorn (and eventually ROOT Harness) processes hold imports in memory — restart after touching anything in their import path.

## Coding Conventions

- Type hints on all function signatures.
- Pydantic models for all API request/response bodies.
- Keep SAGE and ROOT Harness imports cleanly separated. ROOT Harness imports SAGE's Core API in Phase 1 (in-process), but the code should be structured as if they communicate over HTTP.
- Database files (SQLite, LanceDB) must never be stored inside cloud-synced directories or inside the Git repository. Paths are configured in `sage/config.yaml` and `root_harness/config.yaml`.
- API keys are environment variables, never committed. `.env` files are gitignored.

## Workflow

- **Proposal-then-approval.** Present what you plan to do; wait for `;;yp` before executing.
- **Test specs before code.** Write test specifications before implementation. Sequence: schemas/contracts → test specs → implementation.
- **Claude edits, user directs.** The user does not edit content directly; Claude does. Optimize CAS workflows for MCP-call ergonomics, not editor ergonomics. Prefer vault-resident content over file-then-ingest where the access pattern is Claude-mediated.
- **Downstream impact identification.** When changing a schema, API contract, or architectural element, identify all affected files across the repo.
- **ADR capture.** When a design decision resolves an open question or establishes a new architectural constraint, flag it with `;;flag` for ADR capture as a `doc_type=adr` document in the cas vault.
- **Multi-session work.** If a task spans more than one session, capture it as a ticket (`doc_type=ticket`) in the cas vault. See the *CAS Ticket Conventions* steering document for write discipline.
