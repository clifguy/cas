# PIM Health Domain Instantiation

Patent portfolio management domain. First and most complex CAS domain instantiation.

## Files

Four ROOT Harness YAML configuration files validated against Formal Substrate v1.0 schemas:

- `pipeline.yaml` — pipeline stages, transitions, gatekeeper bindings, preconditions
- `agents.yaml` — agent registrations (stewards and orchestrators)
- `policies.yaml` — behavioral constraints per agent role
- `workflows.yaml` — LangGraph workflow definitions

The SAGE vault configuration lives at `~/sage_vaults/pim_health/vault_config.yaml` (outside the repository). The vault is the source of truth for its own configuration.

## Domain-Specific Terms

- **patent_origin** — PIM-specific field (renamed from `source_type` to avoid overloading SAGE's adapter type field). Derived from the document code prefix but the metadata_extraction config has no derived-field mechanism to populate it automatically. This is a known gap flagged for future schema work.
- Pipeline stages follow the patent prosecution lifecycle (drafting, review, filing, prosecution, maintenance).

## Editing Conventions

- YAML is the canonical format. Do not create Word documents for this domain instantiation.
- Comments in YAML serve as documentation for human readers. Preserve and update them when modifying config values.
- After modifying any YAML file, validate it against the corresponding schema in `docs/fs/`.
