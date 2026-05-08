# Deployment Model v2.0 Drift Inventory

**Date:** 2026-05-08
**Author:** Clif (with Cowork assistance)
**Status:** Draft for review before any rewrite plan or .docx edits begin
**Predecessor under audit:** `docs/ref/2026-03-31_CAS_REF_Deployment-Model_v1_0.docx`

## Purpose

This inventory identifies the gap between (a) the Deployment Model v1.0 as authored on 2026-03-31 and (b) the current state of the local CAS deployment as of 2026-05-08. It is the audit deliverable that gates the v2.0 rewrite: no rewrite plan or prose drafting begins until this inventory is reviewed and accepted.

The standards established for the SAGE Arch v2.0 rewrite (self-contained narrative, no historical framing in the body, architectural-vs-deployment distinction) apply uniformly. The Deployment Model is the document where the architectural-vs-deployment distinction matters most: the deployment-laden content extracted from SAGE Arch v2.0 has been pre-staged at `docs/audit/2026-05-06_deployment-notes-source-material.md` and is the principal new material to incorporate.

## Audit basis

| Source | State at audit time |
|---|---|
| Deployment Model v1.0 (.docx) | Issued 2026-03-31; promoted to v1.0 on that date |
| ADR store (`docs/cas_adr_store.json`) | 21 ADRs through ADR-021 (2026-05-01); CAS-ADR-022 and CAS-ADR-023 drafted but in `proposed` status outside the store |
| Formal Substrate manifest (`docs/fs/manifest.json`) | substrate v1.14, 2026-05-02 (per prior drift inventory) |
| Running deployment (system inspection) | The live CAS environment. Source of empirical deployment facts: OS and Python versions, Homebrew prefix, vault directory layout, RAM behavior under MLX-loaded process, MCP server configuration, behavior of the installed editable package |
| Deployment-notes source material (`docs/audit/2026-05-06_deployment-notes-source-material.md`) | Pre-staged from SAGE Arch v2.0 rewrite. Contains the three reclassified ADR drafts (sequential ingestion pipeline, lazy abstraction model loading, single-process Phase 1 topology) plus the deployment-laden body content extracted from SAGE Arch v2.0 (§3.2.1-3, §3.3.4-5, §3.4.3, §3.5.2, §9, plus the trailing paragraph of §3.5.1) |
| SAGE Architecture Reference v2.0 (.docx) | Issued 2026-05-06; section references in this inventory point to v2.0 numbering |

**Citation discipline.** REF .docx files are authoritative for the architecture and deployment record; this inventory follows the same discipline. CLAUDE.md is reserved for steering Claude and is not cited as a source. Deployment-environment facts that surface during this audit are documented in the eventual v2.0 on the REF doc's own authority, ascertained by direct inspection of the running deployment, ADR store, OpenAPI specs, source code, or vault configurations.

**Scope discipline.** The Deployment Model describes the CAS deployment: hardware, OS, runtime, SAGE infrastructure, ROOT Harness infrastructure, the CAS Application's process and serving topology, source-control conventions, setup, and operation. Tools that consume CAS's APIs or MCP transport — including MCP clients (Claude Code, Cowork, Cline, ChatGPT desktop, custom clients) and REST callers — are out of scope except where named as non-defining examples. The choice of any particular MCP client or development tool by any particular human user is irrelevant to CAS. v1.0's §8 ("Development Environment") privileges specific tools (Claude Code, Claude Cowork) as if they were CAS deployment components; the v2.0 rewrite should remove those subsections and retain only Git as a source-control concern about where the CAS codebase can live.

ADRs that post-date the v1.0 Deployment Model: ADR-015 (2026-04-16), ADR-016 (2026-04-16), ADR-017 (2026-04-18), ADR-018 (2026-04-25), ADR-019 (2026-04-29), ADR-020 (2026-05-01), ADR-021 (2026-05-01). Of these, ADR-018 and ADR-021 carry direct deployment implications; ADR-020 carries indirect deployment implications via the abstraction-prompt configuration; the others are primarily architectural.

## Classification key

For each item, drift is classified as:

- **D — Drift.** The document claims X; reality is Y. Resolution: rewrite to reflect reality.
- **R — Missing rationale.** The decision is reflected in operation but the why-it-is-so was captured only in an ADR, the source code, or commit history. Resolution: incorporate into the prose with a citation when an ADR or formal-substrate artifact is the source; document on the REF doc's own authority when the source is the running deployment.
- **S — Missing structure.** A deployment-relevant component, constraint, or pitfall that a self-contained reader needs to understand the running system but that v1.0 does not cover. Resolution: write a new section.
- **N — No drift.** Verified consistent with current state.

Remediation classifications: **replace**, **incorporate-with-citation**, **new-section**, **no-change**, **shrink-and-cite** (when the architecture document now carries the substance and the deployment doc should reduce to operational specifics).

## Summary

Overall severity: **medium-high**. The v1.0 was current to 2026-03-31; subsequent build-out of the CAS Application, the SAGE pipeline maturation through ADRs 015-021, and the consolidation of operational practice (settled in the running deployment but not yet captured in the REF docs) have produced drift across roughly half the sections, plus three to four new subsections needed to absorb the deployment-notes source material.

| §  | Section | Currency | Principal drift |
|---|---|---|---|
| 1 | Introduction | Mostly current | Cross-references to SAGE Arch sections need v2.0 alignment |
| 2 | Hardware | Current | Minor: model identifier (Mac16,11) and OS version not explicit |
| 3 | OS and Platform | Mostly current | Add macOS 26.4.1; Homebrew prefix at /opt/homebrew |
| 4.1 | Python | Stale | 3.12+ → 3.14.3; editable install pattern; `.[test,mlx]` extras |
| 4.2 | Node.js | Stale-low | Required to build the CAS Application's React SPA; v1.0 understates by treating Node.js as optional |
| 5 | SAGE Infrastructure | Stale | MCP transport row inverted (SSE-mounted is preferred, not stdio); ADR-018 file-watcher exclusion to add; section refs to SAGE Arch v2.0 |
| 5.1 | Vault Storage | Stale | Vaults at ~/sage_vaults/{vault_id}/, vault config self-describing, not under sage/config.yaml's vault root |
| 5.2 | Embedding Model | Stale-low | Add v1.5, L2-normalized, CPU device, max_seq_length=2048, batch_size=8, MPS-contention rationale |
| 5.3 | Local LLM Runtime | Stale-medium | Add lazy loading; greedy decoding + ADR-020 prompt design; total RAM budget |
| 6 | ROOT Harness Infrastructure | Forward-looking, mostly current | Minor MCP transport alignment; ROOT Harness still unbuilt |
| 6.1 | LLM Provider Config | Mostly current | Optional: Sonnet 4.6 / Haiku 4.5 model name update |
| 6.2 | Checkpoint Storage Rationale | Current | No drift |
| 7 | CAS Application | Stale-medium | Generic "HTML5 web app" framing; reality is React SPA + FastAPI app backend at /app/* + integrated MCP at /mcp, all in one uvicorn process |
| 7.1 | API Endpoints | Stale-low | Missing CAS App backend as explicit component layered between SPA and SAGE |
| 7.2 | Serving Strategy | Stale (TBD-replaced) | Single-process topology established; replace TBD framing |
| 8.1 | Claude Code | Out of scope | Remove subsection per scope discipline (MCP-client choice is not a CAS fact) |
| 8.2 | Claude Cowork | Out of scope | Remove subsection per scope discipline |
| 8.3 | Git | Current | No material drift; consider promoting to be the only §8 content (or moving Git to §9 Directory Structure) |
| 9 | Directory Structure | Stale-low | Verify shared/ row; add ~/sage_vaults/{vault_id}/; foreshadow CAS-ADR-022 trajectory |
| 10.1 | Prerequisites | Stale-low | Bump Python prerequisite to match §4.1 |
| 10.2 | Python Environment | Stale-low | Add `pip install -e ".[test,mlx]"` install command |
| 10.3 | SAGE Setup | Stale-medium | Vault config pattern updated to ~/sage_vaults/{vault_id}/ form; SAGE MCP server start command (`python -m sage.mcp_server`) and SSE-mounted transport |
| 10.4 | ROOT Harness Setup | Forward-looking, current | No build-state drift |
| 10.5 | CAS Application Setup | Stale | App is built; setup is npm/build, not "initial HTML page" |
| 10.6 | Development Tools | Out of scope (mostly) | Remove Claude-Code and Cowork install steps per scope discipline; CLAUDE.md placement is not a CAS deployment concern |
| 10.7 | Verification | Mostly current | Could add specific pytest commands |
| 11.1 | Backup Strategy | Current | No material drift |
| 11.2 | API Key Management | Current | No drift |
| 11.3 | Known Phase 1 Constraints | Stale-high | Missing major constraints: sequential ingestion pipeline, lazy abstraction loading, total RAM budget, service restart after code edits, stale graph/vector state pitfall, SSE-vs-stdio MCP transport choice |
| 11.4 | Troubleshooting | Mostly current | Optional: concrete entries from operational experience |
| App. A | CAS ADR Index | Stale | Add deployment-relevant ADRs since v1.0 (015, 016, 018, 020, 021); note CAS-ADR-022 trajectory |
| App. B | Revision History | n/a | New v2.0 entry to append |

**New sections expected:** at least three, possibly four (Single-Process Topology, MCP Transport, SAGE Pipeline Constraints, Stale-State Pitfalls). See "New structure recommendations" below.

## Per-section inventory

### §1 Introduction

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.1.1 | D | Cross-reference to "SAGE Architecture Reference (Section 3.2)" and "ROOT Harness Architecture Reference (Section 6)" reflects pre-v2.0 numbering. SAGE Arch v2.0 reorganized; verify the corresponding section in v2.0 for "deployment models described" reference. ROOT Harness Arch v1.0 §6 is current. | Replace section reference path with SAGE Arch v2.0 numbering. Verify and update. |
| 1.1.2 | N | Phase 1 single-machine framing; only external traffic to LLM API providers; CAS-ADR-006 cited correctly. | No-change. |

### §2 Hardware

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.2.1 | R | "Apple M4 Pro" identifier is correct. The deployment specification additionally carries the Mac16,11 model identifier and 14-core count (verifiable via `sysctl hw.model` and `sysctl hw.ncpu`); both useful for someone provisioning a comparable environment. | Incorporate. Add model identifier and core count to the Hardware table. |

### §3 Operating System and Platform

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.3.1 | R | macOS 26.4.1 (Darwin 25.4) is the running OS (verifiable via `sw_vers` and `uname -r`); the v1.0 table simply says "macOS" without a version. | Incorporate. Add version. |
| 1.3.2 | R | Homebrew prefix on Apple Silicon is /opt/homebrew (verifiable via `brew --prefix`). Useful for path-dependent setup steps. | Incorporate. Add to the Operating System / Package Manager row notes. |

### §4 Runtime Stack

#### §4.1 Python

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.4.1 | D | "Python 3.12+ (installed via Homebrew)". Actual: Python 3.14.3 via Homebrew (verifiable via `python3 --version`). | Replace version. Use the live version with a "minimum 3.12" floor if a floor matters for setup; otherwise just state 3.14.3. |
| 1.4.2 | S | Editable-install pattern: the CAS package is installed editable (`__editable__.cas-0.1.0.pth` in the venv site-packages); edits under `sage/`, `root_harness/`, `app/` take effect without reinstall. v1.0 does not document this. | New-section or extension to §4.1. |
| 1.4.3 | S | Install command and extras: `pip install -e ".[test,mlx]"` (extras defined in `pyproject.toml`). v1.0's setup section lists deps as bullets but does not give the install command. | Incorporate. Reference from §4.1 and use in §10.2. |
| 1.4.4 | N | Single venv shared by SAGE and ROOT Harness; venv-vs-conda-vs-uv rationale; current. | No-change. |

#### §4.2 Node.js

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.4.5 | D | "Node.js is not required for CAS core components" understates: Node.js is required to build the CAS Application's React SPA (npm-driven build). v1.0's "may be needed" phrasing reads as optional. | Replace. State that Node.js is required to build the CAS Application's SPA. The built SPA itself is static assets and does not require Node.js at runtime. No MCP-client tooling is in scope. |

### §5 SAGE Infrastructure

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.5.1 | D | "MCP Adapter | stdio transport | Phase 1 only. Phase 2 moves to SSE or streamable HTTP." Reality (per source material §3.5.1 and §3.5.2): Phase 1 already exposes the MCP server as FastMCP SSE mounted at `/mcp` on the FastAPI server; a standalone-stdio mode is also supported. The SSE-mounted form is preferred at the deployment level because it shares model loads, database connections, and the in-process vault registry across all MCP callers, REST callers, and the CAS Application backend rather than duplicating them per client process. | Replace. Document SSE-mounted-at-/mcp as the Phase 1 default; standalone-stdio as the fallback. |
| 1.5.2 | S | ADR-018 (2026-04-25): ingestion is exclusively intentional; no file watcher, no polling loop, no scheduled scan. Source change detection happens only at re-ingest time via hash comparison. v1.0 does not address this. | New content; cite ADR-018. May fit in §5 or in a new "SAGE Pipeline Constraints" subsection. |
| 1.5.3 | D | Cross-references to "SAGE Architecture Reference Section 3.3" (storage architecture) and "Section 3.2.1" (Phase 1 constraints) reflect pre-v2.0 numbering. | Replace cross-references with SAGE Arch v2.0 section numbers. |
| 1.5.4 | R | Sequential ingestion pipeline (per source material). Pre-staged content. | Incorporate. Pre-staged source material reformats as operational description. New subsection candidate. |

#### §5.1 Vault Storage

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.5.5 | D | "The vault storage root is specified in SAGE's component configuration file (sage/config.yaml)." Reality: SAGE vault configurations live outside the repository at `~/sage_vaults/{vault_id}/vault_config.yaml`, and each vault is self-describing (verifiable by `ls ~/sage_vaults/` and inspection of any `vault_config.yaml`). The path convention is established as `~/sage_vaults/{vault_id}/`, and the vault_config.yaml is the source of truth for vault identity, document types, lifecycle, adapters, metadata extraction, and edge inference. | Replace. Document the `~/sage_vaults/` convention explicitly and the self-describing vault config pattern. |
| 1.5.6 | S | Stale-state pitfall: LanceDB and SQLite stores live outside the repo; `git reset` / `git clean` do not touch them; stale graph/vector state persists across branch switches. Vault configs at `~/sage_vaults/{vault_id}/vault_config.yaml` survive everything except manual deletion. v1.0 does not document this. | New content. Possibly a new "Stale-State Pitfalls" subsection in §11. |

#### §5.2 Embedding Model Configuration

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.5.7 | R | v1.0: "768-dimensional embeddings" only. Source material §3.3.5 specifies: nomic-embed-text **v1.5**, **L2-normalized**, **CPU device**, **max_seq_length=2048, batch_size=8**. Tuning rationale: at default `max_seq_length=8192` the attention matrix grows quadratically and competes with the abstraction model's MLX/MPS unified-memory footprint on Apple Silicon. The current values are the smallest configuration preserving embedding quality on the project's representative document set without forcing model swap-out under bulk ingest. | Incorporate-with-citation (source material). Substantive deployment-environment rationale. |

#### §5.3 Local LLM Runtime

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.5.8 | S | Lazy abstraction model loading (per source material §2). The model loads on first invocation of `AbstractionProvider.generate_abstract()`, not at service startup. Process baseline memory drops by approximately 16-20 GB for processes that have not yet abstracted. Multi-vault processes share the loaded model. v1.0 does not address load timing. | New content. Pre-staged source material reformats as operational description. |
| 1.5.9 | S | Greedy decoding required for deterministic abstracts (TEST-SAGE-AD-029). v1.0 does not mention. Relevant to deployment because it constrains how the abstraction call is configured. | Incorporate-with-citation. Brief mention. |
| 1.5.10 | n/a | ADR-020 abstraction prompt design (descriptive third-person voice, doc_type as parameterized input, anti-fabrication clauses) is built into SAGE source code, not configured per-deployment. Belongs in SAGE Architecture Reference, not the Deployment Model. | Out of scope for Deployment Model; flag for SAGE Arch revision tracker if not already covered. |
| 1.5.11 | S | RAM budget: with Qwen3-30B loaded via MLX and in active use, the full Python process (model, LanceDB, SQLite, uvicorn) runs around 38 GB. The 64 GB total leaves roughly 26 GB for the rest of the system, which is workable but not generous. Verifiable empirically via process monitoring during a representative workload. Includes an explicit constraint: do not run a second concurrent model on the same machine, do not hold two model contexts simultaneously, do not move to a significantly larger model. | New content. Critical operational constraint. New subsection candidate. |

### §6 ROOT Harness Infrastructure

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.6.1 | D | "MCP Server | stdio transport | Phase 2 moves to SSE or streamable HTTP." Forward-looking statement remains structurally correct because ROOT Harness MCP is not built; align with the SSE-mounted Phase 1 default established for SAGE so the eventual ROOT Harness MCP transport mirrors the same pattern. | Replace. Bring transport language into alignment with §5 / source material §3.5.1. |
| 1.6.2 | N | LangGraph orchestration; SQLite checkpoint; LangSmith optional; Anthropic API. ROOT Harness is unbuilt; v1.0's forward-looking framing is appropriate. | No-change. |

#### §6.1 LLM Provider Configuration

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.6.3 | R | Default Model row says "Claude Opus 4.6"; current model lineup includes Claude Sonnet 4.6 and Claude Haiku 4.5 as well. v1.0's per-agent guidance (Sonnet for moderate, Haiku for mechanical) holds; the model names are still current. | Optional refresh: confirm model name strings are current and the per-agent guidance still maps cleanly to the available model tier names. |

#### §6.2 Checkpoint Storage Rationale

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.6.4 | N | Separate SQLite per boundary rule; phase-2 PostgreSQL move on independent timeline. Still accurate; ROOT Harness still unbuilt. | No-change. |

### §7 CAS Application

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.7.1 | D | "an HTML5 web application (with JavaScript as needed)". Reality: React SPA, served as static assets at the root path by the same uvicorn process serving SAGE Core API and the CAS App backend (per source material §3.5.2). The application is built; the framing should reflect it. | Replace. Document the React SPA + FastAPI app backend architecture concretely. |
| 1.7.2 | S | The CAS Application is now three things at the deployment layer: the React SPA, the CAS App backend (FastAPI mounted at `/app/*`), and the integrated MCP transport (FastMCP SSE mounted at `/mcp`). v1.0 implicitly conflates the SPA with the entire application. | New content. Rewrite §7 to lay out the three components explicitly. |

#### §7.1 API Endpoints

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.7.3 | D | "The application consumes two API surfaces: ROOT Harness Orchestration API; SAGE Core API." Reality: the CAS Application has its own backend (FastAPI at `/app/*`) layered between the SPA and SAGE; the SPA also calls the SAGE Core API directly for some operations; ROOT Harness is unbuilt so the Orchestration API consumption is forward-looking only. | Replace. Document the actual three-API consumption pattern (CAS App backend, SAGE Core API directly, ROOT Harness Orchestration API forward-looking). |

#### §7.2 Serving Strategy

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.7.4 | D | "In Phase 1, the application is a set of static HTML, CSS, and JavaScript files. The serving approach will be determined during implementation. Options include serving static files through FastAPI..." Reality (per source material §3.5.2 and §3.2.1): single uvicorn process serves SPA at root, `/app/*`, `/sage_vaults/*`, `/mcp`. All four interfaces share `app.state.vault_registry` and the same SAGE service instances per vault. The decision has been made. | Replace. Document the established single-process topology. Forward-compatibility properties (URL-prefix boundary, separate OpenAPI specs, no direct cross-component imports) are part of the architecture, not an artifact of co-location. |

### §8 Development Environment

#### §8.1 Claude Code

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.8.1 | D | Per scope discipline, the choice of MCP client is not a CAS deployment fact. Claude Code is one of many possible clients consuming CAS's MCP server; treating it as a privileged CAS deployment component is a category error. | Remove §8.1 subsection. The fact that CAS exposes an MCP server (and how) belongs in §5 (SAGE Infrastructure) on the server side. Client-side configuration mechanisms vary by client and are not the Deployment Model's concern. |

#### §8.2 Claude Cowork

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.8.2 | D | Per scope discipline, Cowork is one of many possible MCP clients and is not a CAS deployment component. v1.0's placeholder is correctly out of scope; the right move is to remove the subsection rather than fill it in. | Remove §8.2 subsection. |

#### §8.3 Git

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.8.5 | N | Repository root at `~/repos/`; iCloud/OneDrive incompatibility rationale; GitHub remote with SSH ed25519; ZSH aliases. Current. | No-change. |
| 1.8.6 | S (optional) | Branch model note: single-developer, mostly main; brief feature-branch usage when isolated work warrants. Optional; helps a self-contained reader understand the workflow. | Optional incorporate. |

### §9 Directory Structure

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.9.1 | D | v1.0 lists `~/repos/cas/shared/` as a row. Verify whether the `shared/` directory exists in the current repo; if not present, remove the row. | Verify and replace if needed. |
| 1.9.2 | S | v1.0 directory structure shows in-repo paths only. The deployment story includes `~/sage_vaults/{vault_id}/` (vault data, vault_config.yaml, LanceDB, graph SQLite) and the ROOT Harness checkpoint store path (configured via `root_harness/config.yaml`). v1.0 mentions these in §5 and §6 but does not reflect them in the directory-structure section. | New content. Add an "Outside-repo data directories" subsection or extend §9's table. |
| 1.9.3 | S | CAS-ADR-022 trajectory: documentation will move from `docs/ref/` into a `cas` SAGE vault; the current directory structure is transitional. v1.0 cannot foreshadow this because it predates the ADR. v2.0 should briefly note the trajectory while documenting the current structure. | Incorporate-with-citation (CAS-ADR-022 in `proposed` status). Brief footnote. |

### §10 Setup Procedures

#### §10.1 Prerequisites

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.10.1 | D | "Python 3.12+" prerequisite. Bump to 3.13+ or 3.14+ to match §4.1's revised version statement. | Replace version floor. |

#### §10.2 Python Environment

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.10.2 | R | "Install SAGE dependencies", "Install ROOT Harness dependencies", "Install shared dependencies" as three bullet groups. Actual install command: `pip install -e ".[test,mlx]"` (editable install of the CAS package with test and MLX extras; defined in `pyproject.toml`). | Replace. Use the actual install command; explain what the extras pull in. |

#### §10.3 SAGE Setup

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.10.3 | D | "Copy sage/config.yaml.example to sage/config.yaml and configure the vault storage root path." The vault storage root is configured per-vault under `~/sage_vaults/{vault_id}/vault_config.yaml`, not under `sage/config.yaml`. The component config (sage/config.yaml) likely still configures component-level settings (like the vault registry root or the abstraction model path); confirm during rewrite. | Replace setup instructions to reflect the current config split. |
| 1.10.4 | D | v1.0's setup step "Connect an MCP client (Claude) and verify tool discovery" privileges a specific client. Replace with CAS-side setup: how to start the SAGE MCP server (`python -m sage.mcp_server` against configured vaults; SSE transport mounted at `/mcp` when running in the integrated FastAPI process); verification by hitting `/mcp` with any compliant MCP client. Client choice is out of scope. | Replace. |

#### §10.4 ROOT Harness Setup

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.10.5 | N | ROOT Harness is unbuilt; setup steps remain forward-looking. | No-change. |

#### §10.5 CAS Application Setup

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.10.6 | D | "Create the app/ directory with an initial HTML page" — application is built; setup is "install npm dependencies", "build the SPA", "run the integrated FastAPI server on the configured port", "open the SPA in a browser". | Replace. Document actual build/run steps; reference the integrated single-process topology. |

#### §10.6 Development Tools

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.10.7 | D | Per scope discipline, the Claude-Code and Cowork install/configuration steps in v1.0 §10.6 are out of scope for the Deployment Model. v2.0's setup procedures should describe what is required to run CAS and to develop CAS source code (Python venv, pip install, FastAPI server start, MCP server start, vault provisioning), not how to install or configure any particular MCP client. | Replace. Remove Claude-Code and Cowork install steps. Retain only setup steps that are specific to running CAS or to managing the CAS source repository. |
| 1.10.8 | D | CLAUDE.md placement and CPML symlink instructions concern the steering of Claude as an author, not the running CAS deployment. Out of scope. | Remove from setup procedures. |

#### §10.7 Verification

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.10.9 | R (optional) | Concrete pytest command (`.venv/bin/python -m pytest tests/sage tests/app`) plus expected pass count would let a reader verify the install. v1.0 lists smoke tests in prose only. | Optional incorporate. |

### §11 Operational Notes

#### §11.1 Backup Strategy

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.11.1 | N | GitHub for source-controlled; Time Machine for vaults/checkpoints; cloud-sync incompatibility rationale. Current. | No-change. |

#### §11.2 API Key Management

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.11.2 | N | Env vars in shell profile; .env file pattern alternative; never committed. Current. | No-change. |

#### §11.3 Known Phase 1 Constraints

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.11.3 | S | Sequential ingestion pipeline (source material §1): Phase 1 runs the SAGE ingestion pipeline sequentially because of the 64 GB unified-memory envelope. Bulk ingest is per-document synchronous. Throughput cost accepted because bulk ingest is occasional, not a hot path. v1.0 does not enumerate this. | New content. Critical Phase 1 constraint. |
| 1.11.4 | S | Lazy abstraction model loading (source material §2): forward reference to §5.3 is fine, but the constraint should appear in this enumerated list as a "Phase 1 model-loading discipline". | New content. |
| 1.11.5 | S | RAM headroom constraint: ~26 GB free after Qwen3-30B + Python process; do not run a second concurrent model, do not hold two model contexts, do not move to a significantly larger model. | New content. Drawn from §5.3. |
| 1.11.6 | S | Service restart after code edits: edits to SAGE Python source under `sage/` do not take effect until the running SAGE service (Core API, MCP server, or any in-process consumer) is restarted, because the running interpreter holds the old imports in memory. Generic Python-service behavior, but worth enumerating as a Phase 1 operational constraint. | New content. |
| 1.11.7 | S | Stale graph/vector state pitfall: LanceDB/SQLite stores outside repo; `git reset`/`git clean` do not touch them; vault configs survive everything except manual deletion. Branch switches do not produce a clean slate. | New content. |
| 1.11.8 | S | MCP transport choice: SSE-mounted (preferred) avoids duplicating model loads and database connections across processes; standalone-stdio is fallback. | New content. |
| 1.11.9 | N | "Sequential workflow execution" constraint about LangGraph remains forward-looking. | No-change. |
| 1.11.10 | N | Single user; local observability optional via LangSmith; no auth in Phase 1. Current. | No-change. |

#### §11.4 Troubleshooting

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.11.11 | R (optional) | Concrete entries from operational experience: MCP transport timeouts on bulk ingest (per ADR-020 backfill notes; affects any MCP client connecting via the synchronous transport ceiling, not specific to any one client); LanceDB compaction after vault-rewrite operations. v1.0's living-document framing accommodates additions; populating with two or three concrete entries gives the reader real material to work with. | Optional incorporate. |

### Appendix A: CAS ADR Index

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.AA.1 | S | v1.0 lists ADRs 001, 006, 008, 011. Add deployment-relevant ADRs since: ADR-015 (metadata extraction as SAGE-level capability — minor deployment relevance via FilenameParser availability and the parse-filename endpoint), ADR-016 (UI metadata normalization on ingest — minor deployment relevance), ADR-018 (ingestion exclusively intentional; no file watcher — direct deployment relevance), ADR-020 (abstraction prompt — deployment relevance via prompt configuration), ADR-021 (caller-owned metadata, /parse-filename endpoint, MCP tool surface change requiring server restart — direct deployment relevance). ADR-017 (chain-scoped edge resolution) carries minor deployment relevance via the per-vault edge-type registry; consider citing. | Replace. Update ADR Index appendix to reflect deployment-relevant ADRs current as of v2.0. |
| 1.AA.2 | S | CAS-ADR-022 (proposed): documentation as a SAGE vault. Brief reference appropriate even though `proposed`, since the trajectory affects future revisions of this document. | Incorporate as forward-looking footnote. |

### Appendix B: Revision History

| ID | Type | Item | Remediation |
|---|---|---|---|
| 1.AB.1 | n/a | New v2.0 entry to append following the established format. | Append. |

## New structure recommendations

The drift inventory surfaces three to four candidate new subsections; the rewrite plan should decide whether to land them as standalone subsections or fold them into existing sections.

1. **Single-Process Topology.** Currently in source material §3.5.2; affects §5 (SAGE), §6 (ROOT Harness placement), and §7 (CAS App). Candidate placement: a new subsection in §7, or a new §3a "Process Topology" subsection placed after §3 OS and before §4 Runtime Stack. Rationale: the single-process topology spans multiple existing sections and warrants a single place where the deployment reader can see the full process surface.

2. **MCP Transport.** Currently in source material §3.5.1 trailing paragraph. Affects §5, §6, §8.1. Candidate placement: a new subsection in §5 (since SAGE is the principal MCP server in Phase 1) with cross-references from §6 and §8.1.

3. **SAGE Pipeline Constraints.** Sequential ingestion + lazy abstraction model loading (source material §§1-2). Affects §5.3 and §11.3. Candidate placement: a new subsection in §5 covering pipeline constraints; constraints enumerated in §11.3 by reference.

4. **Stale-State Pitfalls.** New §11.5 collecting the stale graph/vector state pitfall, vault config persistence, and the service-restart-after-code-edit constraint. Some of these double as Troubleshooting entries; the inventory recommends collecting them once in §11.5 and cross-referencing from §11.4.

The four together absorb most of the new content. The rewrite plan will sequence and frame these landings.

## Open questions for the rewrite plan

These are decisions the rewrite plan must resolve before prose drafting begins.

1. **CAS-ADR-022 trajectory in v2.0.** How explicit should v2.0 be about the docs-as-SAGE-vault future state? My recommendation: brief footnote in §9 and a sentence in §1 noting that the document structure is transitional. The detail of vault provisioning waits for ratification.

2. **ROOT Harness coverage depth.** ROOT Harness is unbuilt. v1.0 already treats it forward-looking. Should v2.0 reduce ROOT Harness sections to a placeholder paragraph and cross-reference the ROOT Harness Architecture Reference? My recommendation: keep §6, §6.1, §6.2 at v1.0 depth (they are concise and serviceable as forward-looking statements); update language for consistency with the v2.0 standard but do not contract.

3. **Phase 2 / Phase 3 deployment topology.** Source material §3.2.2 and §3.2.3 contain Phase 2 and Phase 3 narrative extracted from SAGE Arch v2.0. SAGE Arch v2.0 retained the architectural treatment of multi-phase deployment topology. Should the Deployment Model carry the operational detail for Phases 2 and 3 even though they are not yet relevant? My recommendation: brief one-paragraph sketches of Phase 2 and Phase 3 retained in v2.0 (as v1.0 did, more or less), with the architectural detail living in SAGE Arch v2.0 by reference.

4. **Reference-numbering policy for new subsections.** If §5.4 (MCP Transport), §5.5 (Pipeline Constraints), §11.5 (Stale-State Pitfalls), and a Process Topology subsection are added, the existing section numbering in cross-references shifts. With §8.1 and §8.2 also dropping, the §8 boundary itself shifts. The standard for the v2.0 rewrite is no historical framing, so we do not call out "renumbered from §X". The rewrite plan should note the new numbering and verify cross-references at conformance time.

5. **§8 disposition.** With §8.1 (Claude Code) and §8.2 (Claude Cowork) removed per scope discipline, §8 contains only Git. Options: keep §8 with a renamed heading ("Source Control"); fold the Git content into §9 (Directory Structure) or a new repository-management subsection; promote Git's iCloud/OneDrive incompatibility constraint into §11.5 (Stale-State Pitfalls) since it shares the "data outside cloud sync" theme. The rewrite plan should pick one.

## Closing

This inventory enumerates the drift across 33 sections and appendices of v1.0. Severity is medium-high overall, with the largest concentrations in §5 (SAGE Infrastructure), §7 (CAS Application), and §11.3 (Phase 1 Constraints). Two whole subsections (§8.1 Claude Code and §8.2 Claude Cowork) are removed per scope discipline: the Deployment Model describes CAS, not the developer's choice of MCP client. The pre-staged source material substantially reduces the drafting load: roughly six of the new subsections' worth of content already exists in `2026-05-06_deployment-notes-source-material.md` and reformats rather than rewrites. The rewrite plan that follows should sequence the rewrites by section, identify which v1.0 prose is preserved versus replaced, identify what is removed, and flag any cross-references requiring downstream coordination.

End of inventory.
