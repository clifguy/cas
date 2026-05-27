#!/usr/bin/env python3
"""acceptance benchmark: bulk_update_metadata vs sequential calls.

Compares wall-clock for two paths that apply a tag-add metadata patch to
N documents:

1. N sequential ``update_metadata`` MCP calls (baseline).
2. One ``bulk_update_metadata`` MCP call with N items.

Two run modes:

- ``--mode stdio`` (default): launches ``python -m sage.mcp_server`` as
  a subprocess and runs both paths over the real MCP stdio transport
  using the official ``mcp`` Python client SDK. This is the canonical
  acceptance measurement; the ticket gate is calibrated against
  it.
- ``--mode fastmcp``: dispatches through ``FastMCP.call_tool`` against
  the in-process MCP server. Exercises the FastMCP routing / argument
  parsing / content serialization path but skips the OS pipe and the
  JSON-RPC framing on it. Useful as a fast feedback signal but not the
  acceptance number — the framing overhead is a meaningful contributor
  to the ticket's performance win.

Acceptance gate (per the ticket): the bulk wall-clock must be
< 30% of the sequential wall-clock at N=500 on a representative vault.
The script exits non-zero when the ratio gate fails.

Usage:

    SAGE_TEST_STUB_PROVIDERS=1.venv/bin/python \\
        -m scripts.t0088_bulk_metadata_load_probe --n 500
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from sage import mcp_server
from sage.adapters.stubs import StubContentStore
from sage.config import VaultConfig
from sage.mcp_init import initialize_services
from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import Document


def _decode_call_tool_result(result) -> dict:
    """Extract the JSON payload from a FastMCP call_tool result.

    Production shape is ``[TextContent(text=<json>)]``; defensive shapes
    (raw dict, CallToolResult) are also recognized here to match the
    handling in ``mcp_server._envelope_error_kind``.
    """
    if isinstance(result, dict):
        return result
    if isinstance(result, (list, tuple)) and result:
        block = result[0]
        text = getattr(block, "text", None)
        if isinstance(text, str):
            return json.loads(text)
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    raise RuntimeError(f"unrecognized call_tool result shape: {type(result)!r}")


def _vault_config_dict(vault_id: str, brain_root: Path, storage_root: Path) -> dict:
    return {
        "vault": {
            "id": vault_id,
            "name": f"T-0088 Probe Vault {vault_id}",
            "owner": "benchmark",
            "visibility": "personal",
            "brain_root": str(brain_root),
            "storage_root": str(storage_root),
        },
        "document_types": {
            "doc_types": [{"value": "note", "label": "Note"}],
        },
        "lifecycle": {
            "base_states_required": True,
            "states": [
                {"value": "active", "label": "Active"},
                {"value": "archived", "label": "Archived", "is_terminal": True},
            ],
            "transitions": [
                {"from_state": "(new)", "action": "ingest", "to_state": "active"},
                {"from_state": "active", "action": "archive", "to_state": "archived"},
            ],
        },
        "source_adapters": {"adapters": [{"source_type": "markdown", "enabled": True}]},
        "metadata_extraction": {},
        "edge_inference": {"tier_assignments": []},
    }


def _doc_id(i: int) -> str:
    """8-hex-prefix + slug, matching DocumentIdStr regex."""
    return f"{hashlib.sha256(f'probe-{i}'.encode()).hexdigest()[:8]}_probe_{i}"


def _make_doc(doc_id: str) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=doc_id,
        title=f"Probe doc {doc_id}",
        source_type=SourceType.MARKDOWN,
        source_path=f"probe/{doc_id}.md",
        source_content_hash="sha256:" + hashlib.sha256(doc_id.encode()).hexdigest(),
        adapter_version="0.1.0",
        created_by="benchmark",
        created_at=now,
        last_modified_by="benchmark",
        updated_at=now,
        projected_at=now,
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
    )


async def _seed_vault_inprocess(
    vault_id: str, brain_root: Path, storage_root: Path, n: int
) -> tuple[str, list[str], object]:
    """In-process seed used by the fastmcp run mode (returns live services)."""
    cfg_dict = _vault_config_dict(vault_id, brain_root, storage_root)
    config = VaultConfig.model_validate(cfg_dict)
    services = await initialize_services(
        config,
        content_store_factory=lambda _brain: StubContentStore(),
    )
    doc_ids = [_doc_id(i) for i in range(n)]
    for doc_id in doc_ids:
        await services.graph_store.insert_document(_make_doc(doc_id))
        await services.graph_store.update_document(doc_id, {"doc_type": "note"})
    mcp_server._vaults[config.vault.id] = services
    return config.vault.id, doc_ids, services


async def _build_vault_on_disk(vault_root: Path, vault_id: str, n: int) -> tuple[str, list[str]]:
    """Build a vault directory on disk with N seeded active documents, then
    close all services so a subprocess MCP server can open it.

    Returns ``(vault_id, doc_ids)``. The caller spawns the subprocess
    pointing at ``vault_root``.
    """
    vault_dir = vault_root / vault_id
    vault_dir.mkdir()
    brain_root = vault_dir / "brain"
    storage_root = vault_dir / "sources"
    brain_root.mkdir()
    storage_root.mkdir()

    cfg_dict = _vault_config_dict(vault_id, brain_root, storage_root)
    (vault_dir / "vault_config.yaml").write_text(yaml.safe_dump(cfg_dict))

    config = VaultConfig.model_validate(cfg_dict)
    services = await initialize_services(
        config,
        content_store_factory=lambda _brain: StubContentStore(),
    )
    doc_ids = [_doc_id(i) for i in range(n)]
    try:
        for doc_id in doc_ids:
            await services.graph_store.insert_document(_make_doc(doc_id))
            await services.graph_store.update_document(doc_id, {"doc_type": "note"})
    finally:
        await services.graph_store.close()
    return vault_id, doc_ids


async def _run_sequential_fastmcp(vault_id: str, doc_ids: list[str]) -> float:
    """In-process FastMCP.call_tool dispatch per item."""
    mcp = mcp_server.mcp
    start = time.perf_counter()
    for doc_id in doc_ids:
        raw = await mcp.call_tool(
            "update_metadata",
            {
                "vault_id": vault_id,
                "document_id": doc_id,
                "tags": {"add": ["bulk_test"]},
            },
        )
        payload = _decode_call_tool_result(raw)
        if "error" in payload:
            raise RuntimeError(f"sequential call failed on {doc_id}: {payload!r}")
    return time.perf_counter() - start


async def _run_bulk_fastmcp(vault_id: str, doc_ids: list[str]) -> float:
    """Single in-process FastMCP.call_tool dispatch for bulk_update_metadata."""
    mcp = mcp_server.mcp
    items = [{"document_id": d, "tags": {"add": ["bulk_test"]}} for d in doc_ids]
    start = time.perf_counter()
    raw = await mcp.call_tool("bulk_update_metadata", {"vault_id": vault_id, "items": items})
    elapsed = time.perf_counter() - start
    payload = _decode_call_tool_result(raw)
    if "error" in payload:
        raise RuntimeError(f"bulk call failed: {payload!r}")
    if payload["error_count"] != 0:
        raise RuntimeError(f"bulk call reported per-item errors: {payload['error_count']}")
    return elapsed


def _parse_call_tool_response(resp) -> dict:
    """Extract the JSON dict from an mcp.types.CallToolResult."""
    structured = getattr(resp, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    content = getattr(resp, "content", None)
    if content:
        block = content[0]
        text = getattr(block, "text", None)
        if isinstance(text, str):
            return json.loads(text)
    raise RuntimeError(f"unrecognized CallToolResult shape: {resp!r}")


async def _run_sequential_stdio(session: ClientSession, vault_id: str, doc_ids: list[str]) -> float:
    start = time.perf_counter()
    for doc_id in doc_ids:
        resp = await session.call_tool(
            "update_metadata",
            {
                "vault_id": vault_id,
                "document_id": doc_id,
                "tags": {"add": ["bulk_test"]},
            },
        )
        payload = _parse_call_tool_response(resp)
        if "error" in payload:
            raise RuntimeError(f"sequential call failed on {doc_id}: {payload!r}")
    return time.perf_counter() - start


async def _run_bulk_stdio(session: ClientSession, vault_id: str, doc_ids: list[str]) -> float:
    items = [{"document_id": d, "tags": {"add": ["bulk_test"]}} for d in doc_ids]
    start = time.perf_counter()
    resp = await session.call_tool("bulk_update_metadata", {"vault_id": vault_id, "items": items})
    elapsed = time.perf_counter() - start
    payload = _parse_call_tool_response(resp)
    if "error" in payload:
        raise RuntimeError(f"bulk call failed: {payload!r}")
    if payload["error_count"] != 0:
        raise RuntimeError(f"bulk call reported per-item errors: {payload['error_count']}")
    return elapsed


async def _bench_fastmcp(n: int) -> tuple[float, float]:
    """In-process FastMCP dispatch benchmark."""
    with tempfile.TemporaryDirectory(prefix="t0088_probe_") as tmp:
        tmp_path = Path(tmp)
        brain_a = tmp_path / "brain_seq"
        store_a = tmp_path / "storage_seq"
        brain_a.mkdir()
        store_a.mkdir()
        # Each path gets a fresh vault so the tag-add precondition (tag
        # NOT present) holds; reusing the same one would tag_add_conflict
        # on the second pass.
        vault_a, ids_a, svc_a = await _seed_vault_inprocess("t0088_probe_seq", brain_a, store_a, n)
        try:
            sequential_seconds = await _run_sequential_fastmcp(vault_a, ids_a)
        finally:
            await svc_a.graph_store.close()
            mcp_server._vaults.pop(vault_a, None)

        brain_b = tmp_path / "brain_bulk"
        store_b = tmp_path / "storage_bulk"
        brain_b.mkdir()
        store_b.mkdir()
        vault_b, ids_b, svc_b = await _seed_vault_inprocess("t0088_probe_bulk", brain_b, store_b, n)
        try:
            bulk_seconds = await _run_bulk_fastmcp(vault_b, ids_b)
        finally:
            await svc_b.graph_store.close()
            mcp_server._vaults.pop(vault_b, None)
    return sequential_seconds, bulk_seconds


async def _bench_stdio(n: int) -> tuple[float, float]:
    """Spawn the MCP server as a subprocess and benchmark over real stdio."""
    with tempfile.TemporaryDirectory(prefix="t0088_probe_stdio_") as tmp:
        vault_root = Path(tmp) / "vault_root"
        vault_root.mkdir()
        # Two vaults under one root so the subprocess discovers both and we
        # can route sequential traffic to one + bulk traffic to the other.
        vault_seq, ids_seq = await _build_vault_on_disk(vault_root, "t0088_probe_seq", n)
        vault_bulk, ids_bulk = await _build_vault_on_disk(vault_root, "t0088_probe_bulk", n)

        env = os.environ.copy()
        env["SAGE_VAULT_ROOT"] = str(vault_root)
        env["SAGE_TEST_STUB_PROVIDERS"] = "1"
        env["MLX_DISABLE_METAL"] = env.get("MLX_DISABLE_METAL", "1")

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "sage.mcp_server"],
            env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                sequential_seconds = await _run_sequential_stdio(session, vault_seq, ids_seq)
                bulk_seconds = await _run_bulk_stdio(session, vault_bulk, ids_bulk)
    return sequential_seconds, bulk_seconds


async def _bench(mode: str, n: int, gate: float) -> int:
    if mode == "stdio":
        sequential_seconds, bulk_seconds = await _bench_stdio(n)
    elif mode == "fastmcp":
        sequential_seconds, bulk_seconds = await _bench_fastmcp(n)
    else:
        raise ValueError(f"unknown mode: {mode!r}")

    ratio = bulk_seconds / sequential_seconds if sequential_seconds > 0 else float("inf")
    print(f"mode = {mode}")
    print(f"N = {n}")
    print(f"  sequential: {sequential_seconds:8.3f} s  ({n} MCP calls)")
    print(f"  bulk:       {bulk_seconds:8.3f} s  (1 MCP call)")
    print(f"  ratio:      {ratio:.3f}  (gate: < {gate:.2f})")
    if ratio >= gate:
        print(
            f"FAIL: bulk/sequential ratio {ratio:.3f} >= gate {gate:.2f}; "
            "expected the bulk path to be at least 70% faster.",
            file=sys.stderr,
        )
        return 1
    print(f"PASS: bulk wall-clock is {(1 - ratio) * 100:.1f}% faster than sequential.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=["stdio", "fastmcp"],
        default="stdio",
        help=(
            "stdio (default): spawn the MCP server as a subprocess and call "
            "tools over real stdio. fastmcp: in-process FastMCP dispatch only "
            "(faster feedback; not the acceptance number)."
        ),
    )
    parser.add_argument("--n", type=int, default=500, help="Batch size (default 500).")
    parser.add_argument(
        "--gate", type=float, default=0.30, help="Max bulk/sequential ratio (default 0.30)."
    )
    args = parser.parse_args()
    return asyncio.run(_bench(args.mode, args.n, args.gate))


if __name__ == "__main__":
    raise SystemExit(main())
