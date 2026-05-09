# CAS Reference Documents

The Word and Markdown reference documents that previously lived in this
directory have been ingested into the CAS vault in SAGE. To read or query them,
route through SAGE rather than the file system.

## Where to find them

- Vault id: `cas`
- Storage root on disk: `~/sage_vaults/cas/sources`
- MCP access: configured in `~/.claude/settings.json`. Use the `sage_discover`,
  `sage_get_document`, `sage_read_section`, and `sage_traverse` tools against
  `vault_id: "cas"`.

## What was in this directory

The following REF document families now live in the vault:

- App-Spec (`doc_type: specification`)
- Deployment-Model (`doc_type: reference_document`)
- Formatting-Standards (`doc_type: reference_document`)
- Overview (`doc_type: reference_document`)
- ROOT-Harness-Architecture (`doc_type: reference_document`)
- SAGE-Architecture (`doc_type: reference_document`)
- System-Architecture-Diagrams (`doc_type: reference_document`)

Each family carries a supersedes-edge version chain, so the active head and its
full lineage are queryable through SAGE.

## Why files were moved out of the repository

Storing reference documentation as ordinary files in the repository worked
against SAGE's role as the canonical state substrate for CAS. With the
documents in the vault, the active head of each family is authoritatively
identifiable, supersedes edges are maintained as documents are ingested, and
review or query happens through the same retrieval surface that other CAS
components use.
