"""Operator-only maintenance scripts (permanently out-of-band per CAS-ADR-029).

This package is excluded from the SAGE Core API and MCP server surfaces by
architectural invariant: no module under ``sage.mcp_server`` or
``sage.api`` may import ``sage.maintenance`` (directly or transitively).
The boundary is enforced by ``tests/test_maintenance_isolation.py`` (T-0107).
"""
