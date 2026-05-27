# CAS development guide

A personal experimental agentic ecosystem with three components:

- **SAGE** (Salience-Aware Graph Engine) — knowledge graph, document store, retrieval subsystem. SQLite graph store, LanceDB content store, FastAPI Core API, MCP server.
- **ROOT Harness** (Runtime for Orchestration, Operations, and Testing) — LangGraph-based orchestration for stewards and orchestrators.
- **CAS Application** — HTML5 web client for oversight, approval, and browsing.

See `README.md` for the public-facing project description.

## Tech stack

Python 3.14 with FastAPI + uvicorn (SAGE Core API and ROOT Harness Orchestration API), LangGraph for workflow orchestration, LanceDB for vector and full-text content, SQLite for graph and checkpoint stores, nomic-embed-text for embeddings, Qwen3 via MLX for semantic abstract generation. JSON Schema for schemas, OpenAPI for API specs, Pydantic v2 for runtime models, pytest for testing. Versions are pinned in `pyproject.toml`.

## Local environment

macOS on Apple Silicon. Python venv at `.venv/`; always invoke as `.venv/bin/python` (never the system Python). Install with `pip install -e ".[test,mlx]"`.

The SAGE MCP server runs from `.venv/bin/python -m sage.mcp_server`. Edits to SAGE MCP server code require a Claude Code restart to take effect — the running server holds the old import.

Database files (SQLite, LanceDB) live outside the repository at paths configured in `sage/config.yaml`. They are not touched by `git reset` or `git clean`.

## SAGE vault discipline — non-negotiable

**Everything inside a SAGE vault tree (`~/sage_vaults/<vault_id>/`, including `sources/`, `imports/`, the graph store, and the content store) is exclusively SAGE-managed.** Never plan, propose, or execute a direct create, read, write, move, or delete against any file or directory inside a vault — neither with `Read`/`Edit`/`Write`/`Bash` tools nor by suggesting the user do so — unless the user has explicitly authorized the specific operation in the current turn. This rule applies in planning, in subagent dispatch, in skill execution, and in autonomous loops; no skill, cohort, or workflow may override it.

- **Create or update a document.** Stage the source under `/tmp/...` and call `ingest_document` (with `predecessor_id` for supersessions). SAGE copies into `imports/` itself.
- **Read a document.** Use SAGE MCP tools — `get_document`, `read_section`, `read_projection`, `search`. Do not `cat`/`Read` files under `~/sage_vaults/`.
- **Inspect graph or content stores.** Use SAGE MCP tools. Never open the SQLite or LanceDB files directly.

Direct mutation breaks provenance (source hashes, chain-head invariants, supersession edges) and silently desynchronizes the graph and content stores. Direct reads bypass projections and lifecycle filtering and return stale or misleading content. The "explicitly authorized" carveout exists for genuine forensics; assume it does not apply by default — when in doubt, ask before touching the vault tree.

## Harness permissions

`.claude/settings.json` (checked in) carries the project-scoped Claude Code permission rules. It currently denies bare `git push --force:*` and narrowly allows `git push --force-with-lease:*` so the `/merge` skill's force-with-lease push runs without a per-invocation prompt while plain force-pushes stay blocked. User-specific overrides belong in `.claude/settings.local.json` (gitignored), not here.

## Standing architectural principles

1. **Single source of truth.** Every concept has one authoritative home. Do not duplicate data across files.
2. **Pointer direction.** Architecture documents point to the Formal Substrate (`docs/fs/`) and the ADR store. Neither points back.
3. **Format follows access pattern.** JSON Schema for validators and code generators, YAML for human-edited configuration, Markdown for narrative documentation.
4. **SAGE indexes everything and owns nothing.** SAGE is the state substrate. It does not contain business logic or orchestration decisions.
5. **Boundary rule.** Stewards call SAGE for artifact operations. Orchestrators access artifacts through stewards. Orchestrators access SAGE directly only for their own working state.
6. **Two-type agent taxonomy.** Stewards own canonical artifacts. Orchestrators own coordination lifecycles. Everything else is a tool.
7. **Planning-then-execution separation.** Do not mix planning and execution in a single context.
8. **Pydantic models derived from schemas.** Never author Pydantic models independently; derive them from the JSON Schema files in `docs/fs/`.

## Coding conventions

- Type hints on all function signatures.
- Pydantic models for all API request and response bodies.
- Keep SAGE and ROOT Harness imports cleanly separated.
- Database files must never be stored inside cloud-synced directories or inside the Git repository.
- API keys are environment variables, never committed. `.env` files are gitignored.
