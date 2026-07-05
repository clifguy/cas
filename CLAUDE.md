# CAS development guide

A personal experimental agentic ecosystem. See `README.md` for the public-facing description.

**Built today:**

- **SAGE** (Salience-Aware Graph Engine) — knowledge graph, document store, and retrieval subsystem. FastAPI Core API and an MCP server over a pluggable storage backend.
- **CAS Application** — HTML5 web client for oversight, approval, and browsing, served through a backend-for-frontend (BFF).

**Planned (designed in the ADR store, not yet implemented):**

- **ROOT Harness** (Runtime for Orchestration, Operations, and Testing) — LangGraph-based orchestration for stewards and orchestrators.

## Tech stack

Python 3.14+, FastAPI + uvicorn, Pydantic v2, pytest. JSON Schema for schemas, OpenAPI for API specs. Embeddings via nomic-embed-text; semantic abstracts via a local MLX model. LangGraph is planned for ROOT Harness. Exact versions are pinned in `pyproject.toml`.

## Storage & deployment profiles

Storage is a single binding (CAS-ADR-042): **Postgres** (with pgvector) is the storage port's sole durable store for both graph and content state — a local socket on a workstation, a managed endpoint in the cloud. SAGE runs under a **local** or **cloud** profile; the cloud profile targets Azure Container Apps / APIM / Entra ID / Key Vault / Postgres. Infrastructure-as-code is in `infra/` (Bicep); container images and deploy scripts in `deploy/`.

## Environment & setup

Python venv at `.venv/`; always invoke `.venv/bin/python`, never the system Python. Install with `uv sync --extra test --extra mlx --extra dev` (needs `uv` — `brew install uv`); this creates `.venv/` and installs the project editable from the committed `uv.lock`, so local, CI, and the deployed server resolve byte-identical builds. The `dev` extra provides `ruff` and `pre-commit`.

Dependencies are lockfile-pinned: `pyproject.toml` carries abstract compatibility ranges; `uv.lock` carries the exact resolved versions and is the source of truth. Move versions deliberately with `uv lock --upgrade` (everything) or `uv lock --upgrade-package <name>` (one package), then `uv sync`, run the suite, and commit the updated lock. `uv lock --check` verifies the lock against `pyproject.toml`; CI installs with `uv sync --locked`, so a drifted lock fails the build.

SAGE's durable store lives outside the repository, in Postgres (connection details in `sage/config.yaml`), and is not touched by `git reset` or `git clean`.

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
- Pydantic models for all API request and response bodies, derived from the JSON Schema files in `docs/fs/`.
- Keep subsystem imports cleanly separated.
- Database files must never be stored inside cloud-synced directories or inside the Git repository.
- API keys are environment variables, never committed. `.env` files are gitignored.
