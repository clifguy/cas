"""Cross-store migration tooling for the SAGE storage engine (CAS-ADR-042).

Copies a vault's derived and curated state from the embedded stores (SQLite
graph + LanceDB content) into a per-vault Postgres schema through the adapter
ports, then reconciles the copy for provenance integrity.
"""
