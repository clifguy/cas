# CAS — Clif's Agentic System

CAS is a personal, experimental agentic ecosystem for working with personal-scale knowledge graphs and the LLM-orchestrated workflows that consume them. It is not a product — it is a single-developer research codebase, made public so others can read, fork, and adapt.

## Components

CAS has three loosely-coupled pieces:

- **SAGE** (Salience-Aware Graph Engine) — knowledge graph, document store, and retrieval subsystem. Built on SQLite for the graph store and LanceDB for vector / full-text content. Exposes a Core API (FastAPI) and an MCP server.
- **ROOT Harness** (Runtime for Orchestration, Operations, and Testing) — LangGraph-based orchestration layer for stewards (agents that own canonical artifacts) and orchestrators (agents that own coordination lifecycles). Calls into SAGE via the Core API.
- **CAS Application** — HTML5 web client (React + TypeScript) for human oversight, approval, and browsing of the graph and pipeline state.

## Repository layout

```
docs/
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

Requires Python 3.14, a working venv at `.venv/`, and an Apple-Silicon Mac for the MLX-accelerated abstraction model (Qwen3 via MLX). Setup is approximately:

```
python -m venv .venv
.venv/bin/pip install -e ".[test,mlx]"
```

See `pyproject.toml` for pinned versions.

The SAGE MCP surface is split across two stdio servers. The **ordinary** server (`sage.mcp_server`) is always enabled and carries the read spine plus the everyday mutation spine. The **maintenance** server (`sage.mcp_server_admin`) is opt-in and additive — it registers the vault- and stack-level administrative tools (the `admin_*` tools) and does not duplicate the read spine; a maintenance session enables both servers and reads through the ordinary one. Server enablement in the client's MCP settings is the only role declaration.

Configure them in your MCP client (e.g. Claude Code `settings.json`). The default case needs only `sage`; add `sage_admin` when you need maintenance tools:

```json
{
  "mcpServers": {
    "sage": {
      "command": ".venv/bin/python",
      "args": ["-m", "sage.mcp_server"]
    },
    "sage_admin": {
      "command": ".venv/bin/python",
      "args": ["-m", "sage.mcp_server_admin"]
    }
  }
}
```

Restart the client after editing files in the MCP import path — the running server holds the old import.

## Status

Experimental. The architecture is real and the system runs day-to-day for the maintainer, but interfaces churn, test coverage is uneven outside the SAGE core, and there is no public release cadence. Issues and PRs are welcome but no SLA is implied.

## License

Apache 2.0. See `LICENSE`.
