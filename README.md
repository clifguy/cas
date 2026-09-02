# CAS — Clif's Agentic System

CAS is a personal, experimental agentic ecosystem for working with personal-scale knowledge graphs and the LLM-orchestrated workflows that consume them. It is not a product — it is a single-developer research codebase, made public so others can read, fork, and adapt.

## Components

CAS has three loosely-coupled pieces:

- **SAGE** (Salience-Aware Graph Engine) — knowledge graph, document store, and retrieval subsystem. Built on Postgres with pgvector for the graph store and vector / full-text content. Exposes a Core API (FastAPI) and an MCP server.
- **ROOT Harness** (Runtime for Orchestration, Operations, and Testing) — LangGraph-based orchestration layer for stewards (agents that own canonical artifacts) and orchestrators (agents that own coordination lifecycles). Calls into SAGE via the Core API.
- **CAS Application** — HTML5 web client (React + TypeScript) for human oversight, approval, and browsing of the graph and pipeline state.

## Repository layout

```
docs/
  api/             Client-integration notes for the deployed REST surface
  fs/              JSON Schema + OpenAPI specs (the Formal Substrate)
  process/         Process and governance documentation
domains/           Use-case-specific orchestrator configs (empty by default)
sage/              SAGE source code
root_harness/      ROOT Harness source code
app/               CAS web client source code
tests/             Test suite and test specifications
scripts/           One-off operational scripts
```

SAGE vault configurations live outside the repository at `~/sage_vaults/{vault_id}/vault_config.yaml`. Each vault is self-describing.

## Local development

Requires Python 3.14, [uv](https://docs.astral.sh/uv/), and an Apple-Silicon Mac for the MLX-accelerated abstraction model (a Qwen model family via MLX). Dependencies are resolved from a committed `uv.lock`, so every environment installs identical builds. Setup is:

```
brew install uv
uv sync --extra test --extra mlx --extra dev
```

`uv sync` creates `.venv/` and installs the project editable from the lockfile. The `dev` extra provides `ruff` and `pre-commit` (the repo uses pre-commit hooks). `pyproject.toml` carries the abstract compatibility ranges; `uv.lock` carries the exact resolved versions.

The SAGE MCP surface is served over the MCP Streamable HTTP transport by the `python -m sage` uvicorn process (default bind `127.0.0.1:8000`) and is split across two surfaces. The **ordinary** mount (`/mcp`) carries the read spine plus the everyday mutation spine. The **maintenance** mount (`/mcp_maint`) is opt-in and additive — it serves the vault- and stack-level maintenance tools and does not duplicate the read spine; a maintenance session connects to both mounts and reads through the ordinary one. Maintenance tools carry the `maint_` prefix by naming convention; which mount a tool lives on is decided by its row in the surface-assignment table, and the one prefixed tool on the ordinary mount is vault enumeration (`maint_list_vaults`), so an ordinary session can discover which vaults its `vault_id` arguments may name. The split exists to keep the everyday catalog small; it carries no privilege distinction — authorization is uniform across both surfaces. All mounts run in the one process, sharing a single vault registry and abstraction model; mount selection in the client's MCP settings is the only role declaration.

The maintenance surface's pre-rename names remain working aliases with no scheduled removal: the path `/mcp_admin` serves the identical roster, and each `admin_*` tool name dispatches as its `maint_*` counterpart on whichever mount registers that counterpart. New configurations should use the `maint` names.

Configure the mounts in your MCP client (e.g. Claude Code `settings.json`). The default case needs only `sage`; add `sage_maint` when you need maintenance tools:

```json
{
  "mcpServers": {
    "sage": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp"
    },
    "sage_maint": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp_maint"
    }
  }
}
```

Restart the uvicorn process after editing files in its import path — the running server holds the old import. If the server is down when an MCP client session starts, the SAGE tools are simply absent from that session; start the server and reconnect.

## Status

Experimental. The architecture is real and the system runs day-to-day for the maintainer, but interfaces churn, test coverage is uneven outside the SAGE core, and there is no public release cadence. Issues and PRs are welcome but no SLA is implied.

## License

Apache 2.0. See `LICENSE`.
