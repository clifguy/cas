"""A config value an adapter cannot use is refused as ``adapter_config_invalid`` (AD-181 to AD-185).

The adapter refuses the value before reading the source; the ingestion service
translates that refusal into a typed 400 on every request surface that projects a
source: ``ingest_document`` over MCP and HTTP, the batch ingest's per-file
errors, and ``recompute_pipeline``. A source the adapter cannot read is not a
config refusal and keeps the reporting it had.

A per-request config reaches the adapter only through ``ingest_document``; the
batch and ``recompute_pipeline`` carry the vault's ``adapter_defaults`` alone, so
their arms refuse a vault default rather than a request value. The write paths
refuse such a default, so those arms hold it the one way it still reaches a
running vault: stored, and loaded leniently (CAS-ADR-047).
"""

from __future__ import annotations

import asyncio
import copy
import io
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import openpyxl
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

import sage.mcp_server as _mcp
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.app import _initialize_services, create_app
from sage.config import STORED_CONFIG_CONTEXT, VaultConfig
from sage.mcp_init import SAGEServices
from sage.mcp_server import get_document, ingest_document, recompute_pipeline, search
from tests.helpers.pipeline_wait import await_tool_idle
from tests.sage.conftest import initialize_services_for_test

_VAULT = "test_vault"
_XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# A value the xlsx adapter cannot use, held where only the batch and
# recompute_pipeline read it: the vault's adapter_defaults.
_REFUSED_PREVIEW_ROWS = {"source_type": "xlsx", "key": "preview_rows", "value": "ten"}


def _parse(result: str | dict) -> dict:
    return result if isinstance(result, dict) else json.loads(result)


def _workbook_bytes() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["ID", "Value"])
    for i in range(1, 4):
        ws.append([i, f"val_{i}"])
    buffer = io.BytesIO()
    wb.save(buffer)
    wb.close()
    return buffer.getvalue()


_CORRUPT_DOCX = b"PK\x03\x04this is not a valid OPC package at all\n"


def _with_xlsx_defaults(config_dict: dict, defaults: dict) -> dict:
    widened = copy.deepcopy(config_dict)
    widened["adapter_defaults"] = {"xlsx": defaults}
    return widened


def _stored_with_refused_xlsx_default(config_dict: dict) -> dict:
    """A configuration holding the refused default, as a stored one may.

    The write paths refuse it, which is asserted here so an arm built on it
    cannot pass by reading a value the configuration had silently dropped.
    """
    stored = _with_xlsx_defaults(config_dict, {"preview_rows": "ten"})
    with pytest.raises(ValidationError, match="adapter_defaults.xlsx.preview_rows"):
        VaultConfig.model_validate(stored)
    return stored


def _loaded(config_dict: dict) -> VaultConfig:
    # Loaded as a stored configuration is, so a refused default still reaches
    # the adapter that reads it.
    return VaultConfig.model_validate(config_dict, context=STORED_CONFIG_CONTEXT)


def _write_source(tmp_vault_dir: Path, relative: str, body: bytes) -> str:
    path = tmp_vault_dir / "sources" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return relative


@asynccontextmanager
async def _mcp_vault(config_dict: dict) -> AsyncIterator[SAGEServices]:
    config = _loaded(config_dict)
    async with initialize_services_for_test(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        _mcp._vaults[_VAULT] = services
        try:
            yield services
        finally:
            await asyncio.sleep(0.1)
            _mcp._vaults.pop(_VAULT, None)


@asynccontextmanager
async def _http_app(config_dict: dict, monkeypatch) -> AsyncIterator[object]:
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = _loaded(config_dict)
    app = create_app(config=config)
    await _initialize_services(app, config, content_store_factory=lambda _brain: StubContentStore())
    try:
        yield app
    finally:
        await asyncio.sleep(0.05)
        registry: dict[str, SAGEServices] = app.state.vault_registry
        if _VAULT in registry:
            registry[_VAULT].close_timing()
            await registry[_VAULT].graph_store.close()
        _mcp._vaults.clear()


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _document_count() -> int:
    return _parse(await search(_VAULT, mode="catalog", limit=0))["total_available"]


def _summary_of(text: str) -> dict:
    events = [
        json.loads(line.replace("data: ", "", 1))
        for line in text.strip().split("\n")
        if line.startswith("data: ")
    ]
    return next(e for e in events if e["event_type"] == "summary")


def _batch_metadata(*source_types: str) -> str:
    return json.dumps(
        {
            "infer_edges": False,
            "files": [
                {"source_type": source_type, "parsed_metadata": {"title": f"File {i}"}}
                for i, source_type in enumerate(source_types)
            ],
        }
    )


async def test_ad_181_ingest_document_reports_a_refused_config_value(
    minimal_vault_config_dict, tmp_vault_dir
):
    """AD-181: ingest_document reports a refused config value as adapter_config_invalid.

    Today's untyped propagation returns ``internal_error``; the exact detail
    comparison rules out a message that merely mentions the key.
    """
    source = _write_source(tmp_vault_dir, "test/dialect.md", b"# A\n\nBody.\n")

    async with _mcp_vault(minimal_vault_config_dict):
        before = await _document_count()
        result = _parse(
            await ingest_document(_VAULT, source, "markdown", config={"dialect": "pandc"})
        )

        assert result["error"] == "adapter_config_invalid", result
        assert result["detail"] == {"source_type": "markdown", "key": "dialect", "value": "pandc"}
        assert await _document_count() == before


async def test_ad_182_core_api_ingest_reports_a_refused_config_value_as_400(
    minimal_vault_config_dict, tmp_vault_dir, monkeypatch
):
    """AD-182: The Core API ingest route reports a refused config value as a 400."""
    source = _write_source(tmp_vault_dir, "test/dialect.md", b"# A\n\nBody.\n")

    async with _http_app(minimal_vault_config_dict, monkeypatch) as app, _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{_VAULT}/documents",
            json={"source": source, "source_type": "markdown", "config": {"dialect": "pandc"}},
        )

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "adapter_config_invalid", body
    assert body["detail"] == {"source_type": "markdown", "key": "dialect", "value": "pandc"}


async def test_ad_183_batch_ingest_reports_a_refused_vault_default_per_file(
    minimal_vault_config_dict, monkeypatch
):
    """AD-183: A batch ingest reports a refused vault default per file.

    The markdown file in the same batch must ingest: it is the control that the
    refusal is the workbook's own and not a batch-wide rejection.
    """
    config_dict = _stored_with_refused_xlsx_default(minimal_vault_config_dict)

    async with _http_app(config_dict, monkeypatch) as app, _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{_VAULT}/documents:batch",
            files=[
                ("files", ("book.xlsx", _workbook_bytes(), _XLSX_TYPE)),
                ("files", ("note.md", b"# Note\n\nBody.\n", "text/markdown")),
            ],
            data={"metadata": _batch_metadata("xlsx", "markdown")},
        )

    assert resp.status_code == 200, resp.text
    summary = _summary_of(resp.text)
    assert summary["documents_created"]["new"] == 1, summary
    (error,) = summary["errors"]
    assert error["file_index"] == 0, error
    assert error.get("code") == "adapter_config_invalid", error
    assert error.get("detail") == _REFUSED_PREVIEW_ROWS, error


async def test_ad_184_recompute_pipeline_reports_a_refused_vault_default(
    minimal_vault_config_dict, tmp_vault_dir
):
    """AD-184: recompute_pipeline reports a refused vault default as adapter_config_invalid.

    The ingest's per-request ``preview_rows`` overrides the vault's refused
    value, so the document exists; re-projection reads the vault default alone.
    A second call returning the same error shows the claim was released.
    """
    config_dict = _stored_with_refused_xlsx_default(minimal_vault_config_dict)
    source = _write_source(tmp_vault_dir, "test/book.xlsx", _workbook_bytes())

    async with _mcp_vault(config_dict) as services:
        ingested = _parse(await ingest_document(_VAULT, source, "xlsx", config={"preview_rows": 5}))
        assert "error" not in ingested, ingested
        doc_id = ingested["id"]

        async def fetch():
            return _parse(await get_document(_VAULT, doc_id))

        await await_tool_idle(fetch, doc_id, service=services.ingestion_service)

        first = _parse(await recompute_pipeline(_VAULT, doc_id))
        second = _parse(await recompute_pipeline(_VAULT, doc_id))

    assert first.get("error") == "adapter_config_invalid", first
    assert first["detail"] == _REFUSED_PREVIEW_ROWS, first
    assert second.get("error") == "adapter_config_invalid", second


async def test_ad_186_a_refused_config_value_retains_nothing(
    minimal_vault_config_dict, tmp_vault_dir, tmp_path
):
    """AD-186: A refused config value retains nothing.

    An absolute source is copied into the vault when it is retained, so a refusal
    raised below retention leaves that copy behind with no document.
    """
    external = tmp_path / "external"
    external.mkdir()
    source = external / "outside.md"
    source.write_bytes(b"# Outside\n\nBody.\n")

    async with _mcp_vault(minimal_vault_config_dict):
        result = _parse(
            await ingest_document(_VAULT, str(source), "markdown", config={"dialect": "pandc"})
        )

    assert result["error"] == "adapter_config_invalid", result
    assert list((tmp_vault_dir / "sources").rglob("outside*")) == []


async def test_ad_187_a_dry_run_reports_a_refused_config_value(
    minimal_vault_config_dict, tmp_vault_dir
):
    """AD-187: A dry run reports a refused config value.

    The preview projects nothing, so without a refusal above it a dry run would
    preview an ingest the real call refuses.
    """
    source = _write_source(tmp_vault_dir, "test/dialect.md", b"# A\n\nBody.\n")

    async with _mcp_vault(minimal_vault_config_dict):
        result = _parse(
            await ingest_document(
                _VAULT, source, "markdown", config={"dialect": "pandc"}, dry_run=True
            )
        )

    assert result.get("error") == "adapter_config_invalid", result
    assert result["detail"] == {"source_type": "markdown", "key": "dialect", "value": "pandc"}


async def test_ad_185_a_malformed_source_is_not_a_config_refusal_in_a_batch(
    minimal_vault_config_dict, monkeypatch
):
    """AD-185: A malformed source is not reported as a config refusal on the batch leg.

    Guards against translating every projection ``ValueError`` into
    ``adapter_config_invalid``: a corrupt package is the source's fault, not the
    config's, and is reported as ``source_unreadable``.
    """
    async with _http_app(minimal_vault_config_dict, monkeypatch) as app, _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{_VAULT}/documents:batch",
            files=[
                ("files", ("broken.docx", _CORRUPT_DOCX, _DOCX_TYPE)),
                ("files", ("note.md", b"# Note\n\nBody.\n", "text/markdown")),
            ],
            data={"metadata": _batch_metadata("docx", "markdown")},
        )

    assert resp.status_code == 200, resp.text
    summary = _summary_of(resp.text)
    assert summary["documents_created"]["new"] == 1, summary
    (error,) = summary["errors"]
    assert error["file_index"] == 0, error
    assert error.get("code") == "source_unreadable", error
    assert error.get("detail") == {"source_type": "docx", "source_path": "broken.docx"}, error
    assert not error["message"].startswith("adapter config"), error


async def test_ad_185_a_malformed_source_is_not_a_config_refusal_on_ingest_document(
    minimal_vault_config_dict, tmp_vault_dir
):
    """AD-185: A malformed source is not reported as a config refusal on ingest_document."""
    source = _write_source(tmp_vault_dir, "test/broken.docx", _CORRUPT_DOCX)

    async with _mcp_vault(minimal_vault_config_dict):
        result = _parse(await ingest_document(_VAULT, source, "docx"))

    assert result["error"] == "source_unreadable", result


@pytest.fixture(autouse=True)
def _clear_mcp_registry():
    yield
    _mcp._vaults.pop(_VAULT, None)
