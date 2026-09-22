# CAS development guide

A personal experimental agentic ecosystem. See `README.md` for the public-facing description.

**Built today:**

- **SAGE** (Salience-Aware Graph Engine) — knowledge graph, document store, and retrieval subsystem. FastAPI Core API and an MCP server over a pluggable storage backend.
- **CAS Application** — HTML5 web client for oversight, approval, and browsing, served through a backend-for-frontend (BFF).

**Planned (designed in the ADR store, not yet implemented):**

- **ROOT Harness** (Runtime for Orchestration, Operations, and Testing) — LangGraph-based orchestration for stewards and orchestrators.

## Tech stack

Python 3.14+, FastAPI + uvicorn, Pydantic v2, pytest. JSON Schema for schemas, OpenAPI for API specs. Embeddings via nomic-embed-text; semantic abstracts via a local MLX model. LangGraph is planned for ROOT Harness. Exact dependency versions are pinned in `pyproject.toml`.

The versions that several toolchains have to agree on — the Postgres majors, the Python version, the Node major — are declared once in `versions.json` and read from there by the infrastructure template, the workflows, and the test suite. Sites that cannot read a file restate them and are held to that declaration by a gate. Change a version there, not at the site that happens to be in front of you; see `docs/process/shared-version-parity.md` for the scope of the rule and the one change that also needs the live branch ruleset edited.

## Storage & deployment profiles

Storage is a single binding (CAS-ADR-042): **Postgres** (with pgvector) is the storage port's sole durable store for both graph and content state — a local socket on a workstation, a managed endpoint in the cloud. SAGE runs under a **local** or **cloud** profile; the cloud profile targets Azure Container Apps / APIM / Entra ID / Key Vault / Postgres. Infrastructure-as-code is in `infra/` (Bicep); container images and deploy scripts in `deploy/`.

## Environment & setup

Python venv at `.venv/`; always invoke `.venv/bin/python`, never the system Python. Install with `uv sync --extra test --extra mlx --extra dev --extra ocr` (needs `uv` — `brew install uv`; OCR needs `brew install tesseract ghostscript`); this creates `.venv/` and installs the project editable from the committed `uv.lock`, so local, CI, and the deployed server resolve byte-identical builds. The `dev` extra provides `ruff` and `pre-commit`.

Running the test suite requires `SAGE_TEST_PG_DSN` — a TCP DSN naming a maintenance database on a Postgres server the suite may use. At session start the harness derives a per-process throwaway database from it on the same server (dropped at session end), so the suite cannot touch live vault data. Leaving it unset does not skip the storage-backed tests: they fail fast at connect against a deliberately unresolvable sentinel host — by design, rather than silently opening a live local-socket Postgres. The default run is parallel (`-n auto --dist loadfile` from `addopts`, sized by CPU count bounded by the Postgres connection ceiling; `-n 0` for serial) and excludes the tests that load the real Qwen3 model; those form an opt-in tier behind `SAGE_TEST_REAL_MODELS=1`, to run when a change touches the Qwen3 adapter. See `docs/process/postgres-local-runtime.md` §5.

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

- Type hints on all function signatures. Ruff enforces return annotations throughout
  `sage/` (including private, special, static, class, nested, and generator functions)
  with ANN201, ANN202, ANN204, ANN205, and ANN206. This gate does not enable
  parameter-annotation or `Any` restrictions, and does not cover other directories.
- Pydantic models for all API request and response bodies, derived from the JSON Schema files in `docs/fs/`.
- Keep subsystem imports cleanly separated.
- Database files must never be stored inside cloud-synced directories or inside the Git repository.
- API keys are environment variables, never committed. `.env` files are gitignored.

## Development workflow selection

The coordinated adoption candidate selects `docs/development/project-policy.md`
and `.development-skills/project.json` (v3 bindings). Use the complete compatible
repo-local `.agents/skills` shared bundle, including its `next` and `smoke-test`
routers, and the project-owned deploy, Azure review and batch components. Do not
fall back to a same-named global skill when a selected dependency is missing.
The canonical `.claude/skills/cas-code-review/SKILL.md` and its existing pointer
remain authoritative for code review.

Operational use requires `.development-skills/activation-receipt.md`, a required
binding intentionally absent from this candidate. It records the owner's exact
activation decision, installed manifest identity, canonical source identity and
fresh live authority heads/readbacks after the coordinated cutover. Missing or
unverified receipt blocks use, not preparation or independent review under the
still-effective pre-adoption policy. Source presence and package verification do
not establish activation. See `docs/development/authority-transition.md`.

The one-time publication and merge of this exact coordinated activation candidate
may also use the retained pre-adoption workflow, when separately authorized by the
owner. Refresh its still-effective live authorities and satisfy its review/check
requirements for the exact candidate before acting. This exception publishes the
transition source only; it does not authorize installation, SAGE supersession or
normal work through the selected new workflow. Those routes still require the
verified activation receipt. Publication and merge are not authorized by this text.

For `/next` only, resolve `ticketing` and add the required source-backed supplement
`cas-triage` with responsibility `triage-procedure`, composition `supplement`,
source `docs/development/operations/triage.md`, and `declared_in` this guide.
Read that authority and preserve its hash in discovery evidence. This explicit
read-path binding does not select triage for ticket writes or invent a `next`
resolver operation. The shared smoke-test router resolves the sole canonical
`docs/development/operations/smoke-test.md` binding.
