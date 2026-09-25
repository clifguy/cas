"""A source its adapter cannot read is refused as ``source_unreadable``.

A malformed, truncated or encrypted file is the caller's to correct, so it is
reported as a typed 400 naming the source type and the caller's own spelling of
the source, rather than as an untyped internal error on MCP and a bare 500 on
the Core API. Each adapter says so by raising ``SourceReadError``; the ingestion
service translates it on every request surface that projects a source:
``ingest_document`` over both protocols, each file of a batch ingest, and
``recompute_pipeline``.

The translation is scoped to that one type. An adapter failure that is not a
read failure -- a missing OCR dependency, a defect -- keeps its reporting, so a
server fault is never presented to a caller as something to fix in the file.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import sage.mcp_server as _mcp
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.app import _initialize_services, create_app
from sage.config import VaultConfig
from sage.mcp_server import get_document, ingest_document, recompute_pipeline, search
from sage.source_adapters.base import SourceReadError
from sage.source_adapters.docx_adapter import DocxAdapter
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from sage.source_adapters.pdf_adapter import PdfAdapter
from sage.source_adapters.pptx_adapter import PptxAdapter
from sage.source_adapters.structured_data_adapter import StructuredDataAdapter
from sage.source_adapters.xlsx_adapter import XlsxAdapter
from tests.helpers.pipeline_wait import await_tool_idle, drain_vaults

_VAULT_ID = "test_vault"
_INGEST = f"/sage_vaults/{_VAULT_ID}/documents"
_BATCH = f"/sage_vaults/{_VAULT_ID}/documents:batch"
_DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_CORRUPT_ZIP = b"PK\x03\x04this is not a valid OPC package at all\n"


def _package(main_type: str) -> bytes:
    """A zip whose content-types part names ``main_type`` and holds nothing else.

    Enough for an adapter's own package handling to succeed, so a patched library
    open is the first thing to fail.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr(
            "[Content_Types].xml", f'<Types><Override ContentType="{main_type}"/></Types>'
        )
    return buffer.getvalue()


_PPTX_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
_POTX_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml"


def _parse(result: str | dict) -> dict:
    return result if isinstance(result, dict) else json.loads(result)


def _summary_of(text: str) -> dict:
    events = [
        json.loads(line.replace("data: ", "", 1))
        for line in text.strip().split("\n")
        if line.startswith("data: ")
    ]
    return next(e for e in events if e["event_type"] == "summary")


@pytest.fixture
async def vault(minimal_vault_config_dict):
    """One initialized vault, shared between the HTTP app and the MCP tools."""
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    application = create_app(config=config)
    await _initialize_services(
        application,
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    )
    try:
        yield application, Path(config.vault.storage_root)
    finally:
        try:
            await drain_vaults(application.state.vault_registry)
        finally:
            for services in application.state.vault_registry.values():
                services.close_timing()
                await services.close_storage()
            _mcp._vaults.pop(_VAULT_ID, None)


@pytest.fixture
async def client(vault):
    application, _root = vault
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as c:
        yield c


def _write(root: Path, relative: str, body: bytes) -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return relative


async def _document_count() -> int:
    return _parse(await search(_VAULT_ID, mode="catalog", limit=0))["total_available"]


# ---------------------------------------------------------------------------
# The adapter contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("adapter", "name", "body"),
    [
        (PdfAdapter, "broken.pdf", b"# not a pdf\n"),
        (DocxAdapter, "broken.docx", _CORRUPT_ZIP),
        (DocxAdapter, "broken.dotx", _CORRUPT_ZIP),
        (PptxAdapter, "broken.pptx", _CORRUPT_ZIP),
        (PptxAdapter, "plain.pptx", b"plain text, no package magic\n"),
        (XlsxAdapter, "broken.xlsx", _CORRUPT_ZIP),
        (XlsxAdapter, "plain.xlsx", b"plain text, no package magic\n"),
        (MarkdownAdapter, "latin1.md", b"# Caf\xe9\n"),
        (StructuredDataAdapter, "broken.json", b'{"a": '),
        (StructuredDataAdapter, "broken.xml", b"<a><b></a>"),
    ],
    ids=[
        "pdf",
        "docx",
        "dotx",
        "pptx-package",
        "pptx-magic",
        "xlsx-package",
        "xlsx-plain",
        "md",
        "structured-data",
        "structured-data-xml",
    ],
)
async def test_each_adapter_reports_an_unreadable_source_as_a_read_error(
    tmp_path, adapter, name, body
):
    """Every adapter raises ``SourceReadError`` for bytes it cannot read.

    Parametrized across every adapter, including the two whose library failure
    escaped unwrapped (xlsx's ``BadZipFile``, markdown's ``UnicodeDecodeError``),
    so an adapter left raising a bare ``ValueError`` or its library's own type
    fails here. The error stays a ``ValueError``, so no caller catching that
    type changes behaviour.
    """
    path = tmp_path / name
    path.write_bytes(body)

    with pytest.raises(SourceReadError) as caught:
        await adapter().project(path, None)

    assert isinstance(caught.value, ValueError)
    assert str(path) in str(caught.value)


@pytest.mark.parametrize(
    ("adapter", "module", "name"),
    [
        (DocxAdapter, "sage.source_adapters.docx_adapter", "template.dotx"),
        (PptxAdapter, "sage.source_adapters.pptx_adapter", "deck.pptx"),
    ],
    ids=["docx-template", "pptx"],
)
async def test_a_filesystem_failure_opening_a_package_is_not_a_read_error(
    tmp_path, monkeypatch, adapter, module, name
):
    """An ``OSError`` opening the package is this process's fault, not the file's.

    The same ``except`` clause catches ``BadZipFile``, which is a read error, so
    the clause splits by type. The paired arm raises ``BadZipFile`` through the
    identical patched call and must come back as ``SourceReadError``; without it,
    an adapter that never raised ``SourceReadError`` from this clause would pass.
    """
    path = tmp_path / name
    path.write_bytes(_CORRUPT_ZIP)

    def refuse(error: Exception):
        def _zip(*args, **kwargs):
            raise error

        return _zip

    monkeypatch.setattr(f"{module}.zipfile.ZipFile", refuse(PermissionError("denied")))
    with pytest.raises(ValueError) as filesystem:
        await adapter().project(path, None)

    monkeypatch.setattr(f"{module}.zipfile.ZipFile", refuse(zipfile.BadZipFile("bad")))
    with pytest.raises(ValueError) as package:
        await adapter().project(path, None)

    assert type(filesystem.value) is ValueError, filesystem.value
    assert type(package.value) is SourceReadError, package.value


class _PagesRaise:
    """A reader whose page list raises the error it is given, and is never encrypted."""

    is_encrypted = False

    def __init__(self, error: Exception) -> None:
        self._error = error

    @property
    def pages(self):
        raise self._error


@pytest.mark.parametrize(
    ("adapter", "name", "target", "make", "body"),
    [
        (PdfAdapter, "doc.pdf", "sage.source_adapters.pdf_adapter.pypdf.PdfReader", None, None),
        (
            PdfAdapter,
            "doc.pdf",
            "sage.source_adapters.pdf_adapter.pypdf.PdfReader",
            _PagesRaise,
            None,
        ),
        (PdfAdapter, "doc.pdf", "sage.source_adapters.pdf_adapter.pdfplumber.open", None, None),
        (DocxAdapter, "doc.docx", "sage.source_adapters.docx_adapter.Document", None, None),
        (
            DocxAdapter,
            "doc.dotx",
            "sage.source_adapters.docx_adapter.Document",
            None,
            _package("application/vnd.openxmlformats-officedocument.wordprocessingml.template"),
        ),
        (
            PptxAdapter,
            "deck.pptx",
            "sage.source_adapters.pptx_adapter.Presentation",
            None,
            _package(_PPTX_TYPE),
        ),
        (
            PptxAdapter,
            "deck.potx",
            "sage.source_adapters.pptx_adapter.Presentation",
            None,
            _package(_POTX_TYPE),
        ),
    ],
    ids=[
        "pdf-open",
        "pdf-page-count",
        "pdf-extract",
        "docx-open",
        "docx-template-open",
        "pptx-open",
        "pptx-template-open",
    ],
)
async def test_a_filesystem_failure_in_a_library_open_is_not_a_read_error(
    tmp_path, monkeypatch, adapter, name, target, make, body
):
    """An ``OSError`` past the hash read is this process's fault; anything else is the file's.

    Each site catches every exception its library raises, so the split is by type.
    The paired arm raises a non-OS error through the identical patched call and
    must come back as ``SourceReadError``, so a site that stopped raising the read
    error at all fails too.
    """
    path = tmp_path / name
    path.write_bytes(body if body is not None else b"%PDF-1.4 placeholder\n")
    if target.endswith("pdfplumber.open"):
        # Extraction runs only once a real reader reports at least one page.
        class _OnePage:
            is_encrypted = False
            pages = [object()]

        monkeypatch.setattr(
            "sage.source_adapters.pdf_adapter.pypdf.PdfReader", lambda *a, **k: _OnePage()
        )

    async def raised(error: Exception) -> type:
        if make is None:

            def _raise(*args, **kwargs):
                raise error

            monkeypatch.setattr(target, _raise)
        else:
            monkeypatch.setattr(target, lambda *args, **kwargs: make(error))
        with pytest.raises(ValueError) as caught:
            await adapter().project(path, None)
        return type(caught.value)

    assert await raised(PermissionError("denied")) is ValueError
    assert await raised(RuntimeError("library could not parse")) is SourceReadError


# ---------------------------------------------------------------------------
# The request surfaces
# ---------------------------------------------------------------------------


async def test_ingest_document_refuses_an_unreadable_source_identically_on_both_surfaces(
    vault, client
):
    """MCP and the Core API refuse the same malformed file with one envelope.

    The detail names the source as the caller sent it. The message is the
    adapter's own, respelled: the retained copy's absolute path never appears.
    Nothing is created.
    """
    _application, root = vault
    source = _write(root, "test/broken.docx", _CORRUPT_ZIP)
    before = await _document_count()

    mcp = _parse(await ingest_document(_VAULT_ID, source=source))
    rest = await client.post(_INGEST, json={"source": source})

    assert rest.status_code == 400, rest.text
    envelope = rest.json()
    assert envelope["code"] == "source_unreadable", envelope
    assert envelope["detail"] == {"source_type": "docx", "source_path": source}
    assert str(root) not in envelope["message"]
    assert source in envelope["message"]
    assert mcp.get("error") == envelope["code"], mcp
    assert mcp["message"] == envelope["message"]
    assert mcp["detail"] == envelope["detail"]
    assert await _document_count() == before


async def test_batch_ingest_reports_an_unreadable_source_per_file(client):
    """A batch reports the malformed upload as its own typed entry, by upload name.

    The markdown file in the same batch must ingest: the refusal is the one
    file's, not the batch's.
    """
    metadata = json.dumps(
        {
            "infer_edges": False,
            "files": [
                {"source_type": "docx", "parsed_metadata": {"title": "Broken"}},
                {"source_type": "markdown", "parsed_metadata": {"title": "Note"}},
            ],
        }
    )

    resp = await client.post(
        _BATCH,
        files=[
            ("files", ("broken.docx", _CORRUPT_ZIP, _DOCX_TYPE)),
            ("files", ("note.md", b"# Note\n\nBody.\n", "text/markdown")),
        ],
        data={"metadata": metadata},
    )

    assert resp.status_code == 200, resp.text
    summary = _summary_of(resp.text)
    assert summary["documents_created"]["new"] == 1, summary
    (error,) = summary["errors"]
    assert error["file_index"] == 0, error
    assert error.get("code") == "source_unreadable", error
    assert error.get("detail") == {"source_type": "docx", "source_path": "broken.docx"}, error


async def test_recompute_pipeline_refuses_an_unreadable_source(vault):
    """Re-projecting a document whose retained source no longer reads is refused typed.

    The detail names the record's vault-relative source path, the spelling a
    caller who named a document can relate to. A second call answering the same
    shows the claim was released.
    """
    application, root = vault
    source = _write(root, "test/recompute.md", b"# Recompute\n\nBody.\n")
    ingested = _parse(await ingest_document(_VAULT_ID, source=source))
    assert "error" not in ingested, ingested
    doc_id = ingested["id"]
    services = application.state.vault_registry[_VAULT_ID]

    async def fetch():
        return _parse(await get_document(_VAULT_ID, doc_id))

    await await_tool_idle(fetch, doc_id, service=services.ingestion_service)
    (root / ingested["source_path"]).write_bytes(b"# Caf\xe9\n")

    first = _parse(await recompute_pipeline(_VAULT_ID, doc_id))
    second = _parse(await recompute_pipeline(_VAULT_ID, doc_id))

    assert first.get("error") == "source_unreadable", first
    assert first["detail"] == {"source_type": "markdown", "source_path": ingested["source_path"]}
    assert second.get("error") == "source_unreadable", second


async def test_an_adapter_failure_that_is_not_a_read_error_keeps_its_reporting(
    vault, client, monkeypatch
):
    """Only ``SourceReadError`` is translated; any other adapter failure is left alone.

    The negative control for a translation widened to ``ValueError``: a plain
    ``ValueError`` -- the shape of the missing-OCR-dependency failure -- must not
    reach a caller as a fault in its file.
    """
    _application, root = vault
    source = _write(root, "test/environment.md", b"# Environment\n")

    async def failing_project(self, source_path, config=None):
        raise ValueError(f"OCR requires a binary this server lacks: {source_path}")

    monkeypatch.setattr(MarkdownAdapter, "project", failing_project)

    mcp = _parse(await ingest_document(_VAULT_ID, source=source))

    assert mcp.get("error") != "source_unreadable", mcp
    assert mcp.get("error") == "internal_error", mcp
