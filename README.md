# CAS — Clif's Agentic System

CAS is an agentic ecosystem for working with knowledge graphs and the LLM-orchestrated workflows that consume them. SAGE, its knowledge-graph and retrieval subsystem, is in commercial production. The source is public so others can read, fork, and adapt it.

## Components

CAS has three loosely-coupled pieces. Two are built; the third is designed but not yet implemented.

- **SAGE** (Salience-Aware Graph Engine) — knowledge graph, document store, and retrieval subsystem. Built on Postgres with pgvector for the graph store and vector / full-text content. Exposes a Core API (FastAPI) and an MCP server.
- **CAS Application** — HTML5 web client (React + TypeScript) for human oversight, approval, and browsing of the graph and pipeline state.
- **ROOT Harness** (Runtime for Orchestration, Operations, and Testing) — *planned, not yet built.* A LangGraph-based orchestration layer for stewards (agents that own canonical artifacts) and orchestrators (agents that own coordination lifecycles), which will call into SAGE via the Core API. Its API contract is specified in the Formal Substrate.

## Repository layout

```
docs/
  api/             Client-integration notes for the deployed REST surface
  fs/              JSON Schema + OpenAPI specs (the Formal Substrate)
  process/         Process and governance documentation
domains/           Use-case-specific orchestrator configs (empty by default)
sage/              SAGE source code
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

The SAGE MCP surface is served over the MCP Streamable HTTP transport by the `python -m sage` uvicorn process (default bind `127.0.0.1:8000`) and is split across two surfaces. The **ordinary** mount (`/mcp`) carries the read spine plus the everyday mutation spine. The **maintenance** mount (`/mcp_maint`) is opt-in and additive — it serves the vault- and stack-level maintenance tools and does not duplicate the read spine; a maintenance session connects to both mounts and reads through the ordinary one. Which mount a tool lives on is decided by its row in the surface-assignment table, not by anything in its name; vault enumeration (`list_vaults`) lives on the ordinary mount so an ordinary session can discover which vaults its `vault_id` arguments may name. The split exists to keep the everyday catalog small; it carries no privilege distinction — authorization is uniform across both surfaces. All mounts run in the one process, sharing a single vault registry and abstraction model; mount selection in the client's MCP settings is the only role declaration.

The maintenance surface is `sage_maint` at `/mcp_maint`. The SAGE Admin transition has ended: `/mcp_admin` and `admin_*` tool aliases are no longer served. Update clients to `/mcp_maint` and the canonical bare tool names (for example `get_vault_config`). The retained `maint_*` tool aliases still dispatch to their bare counterparts on whichever surface registers them; these aliases cannot help catalog-validating clients, whose configurations must use the advertised names. The six maintenance REST operations now use `/sage_vaults/{vault_id}/maintenance/*`; the former `/admin/*` paths are not served.

Configure the mounts in your MCP client. For Claude Code working in this repository, that is the project's untracked `.claude/settings.local.json`, which the tracked `.gitignore` keeps out of commits. The default case needs only `sage`; add `sage_maint` when you need maintenance tools:

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

In production. SAGE and the CAS Application run on a cloud deployment; ROOT Harness is not yet built.

Since release 3.0, the published contract follows a compatibility rule. Every contract change is classified as a patch or a minor release, and anything callers use (an operation, tool, parameter, response field or default) is deprecated at least 30 days and one release before it is removed or changed. The only exceptions are fixes for a security exposure or a data-integrity defect.

Test coverage is uneven outside the SAGE core. Issues and PRs are welcome but no SLA is implied.

## License

Apache 2.0. See `LICENSE`.
