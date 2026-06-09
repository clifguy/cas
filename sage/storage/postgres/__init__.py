"""Postgres storage engine for SAGE (CAS-ADR-042).

The networked relational engine both deployment targets externalize durable
graph and content state to. This package carries the canonical schema/DDL
bootstrap (:mod:`sage.storage.postgres.schema`) and the async driver + connection
pool (:mod:`sage.storage.postgres.pool`) that the store adapters share. The
embedded SQLite/LanceDB stores are retained as a fallback binding.

The modules here import their database driver lazily so the package stays
importable without it, and they depend on no upstream SAGE layer.
"""
