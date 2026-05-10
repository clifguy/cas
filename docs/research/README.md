# CAS Research Documents

Research and methodology surveys that previously lived in this directory have
been ingested into the CAS vault in SAGE. To read or query them, route through
SAGE rather than the file system.

## Where to find them

- Vault id: `cas`
- Storage root on disk: `~/sage_vaults/cas/sources`
- MCP access: configured in `~/.claude/settings.json`. Use the `sage_discover`,
  `sage_get_document`, `sage_read_section`, and `sage_traverse` tools against
  `vault_id: "cas"`.

## What was in this directory

- *AI-First SDLC Tooling Survey (Draft v0.1)* — `doc_type: reference_document`,
  `tags: ["research", "methodology", "sdlc", "tooling", "phase-2"]`. The seed
  authority for the CAS Phase 2 §11 adoption sequence and for the F1–F5 entries
  in the failure log (`doc_type: failure_record`).

## Why files were moved out of the repository

Storing research documentation as ordinary files in the repository works
against SAGE's role as the canonical state substrate for CAS, and creates a
two-source-of-truth problem when failure records and tickets cite the
research. With the documents in the vault, citing artifacts (failure records,
tickets, ADRs) carry `references` edges to the cited document, and the active
head of each research family is authoritatively identifiable.
