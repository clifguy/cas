"""PDF-extracted U+0000 must never reach Postgres text or JSONB storage."""

import hashlib
import shutil
from pathlib import Path

import pdfplumber
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.models.enums import SourceType
from sage.models.schemas import IngestRequest
from sage.services.ingestion import IngestionService
from sage.source_adapters import pdf_adapter
from tests.helpers.pipeline_wait import drain_abstraction_queue


@pytest.fixture
def pdf_source(tmp_path):
    def make(name="source.pdf", *, body=True, title=True, outline=True, blank=False):
        path = tmp_path / name
        writer = PdfWriter()
        page = writer.add_blank_page(612, 792)
        cmap = DecodedStreamObject()
        cmap.set_data(
            b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            b"/CMapName /NullMap def\n/CMapType 2 def\n"
            b"1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
            b"1 beginbfchar\n<58> <0000>\nendbfchar\nendcmap\n"
            b"CMapName currentdict /CMap defineresource pop\nend\nend"
        )
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
                NameObject("/ToUnicode"): writer._add_object(cmap),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        if not blank:
            stream = DecodedStreamObject()
            text = b"XbeforeXXafterX" if body else b"beforeafter"
            stream.set_data(b"BT /F1 12 Tf 72 720 Td (" + text + b") Tj ET")
            page[NameObject("/Contents")] = writer._add_object(stream)
        writer.add_metadata({"/Title": "Title\x00end" if title else "Titleend"})
        writer.add_outline_item("Heading\x00end" if outline else "Headingend", 0)
        writer.write(path)
        return path

    return make


@pytest.mark.parametrize("ocr", [False, True])
async def test_pdf_nulls_removed_on_native_and_ocr_paths(pdf_source, monkeypatch, ocr):
    extracted = pdf_source("extracted.pdf")
    with pdfplumber.open(extracted) as pdf:
        assert pdf.pages[0].extract_text() == "\x00before\x00\x00after\x00"
    source = extracted
    if ocr:
        source = pdf_source("scan.pdf", blank=True, title=False, outline=False)
        derivative = extracted.with_name("ocr-temp.pdf")
        shutil.copyfile(extracted, derivative)
        monkeypatch.setattr(pdf_adapter, "_ocr_to_tempfile", lambda _: derivative)
    original = source.read_bytes()
    projection = await pdf_adapter.PdfAdapter().project(source)
    assert projection.text == "beforeafter"
    assert projection.title == "Titleend"
    assert [(h.text, h.path, h.content) for h in projection.headings] == [
        ("Headingend", "Headingend", "beforeafter")
    ]
    assert projection.content_hash == hashlib.sha256(original).hexdigest()
    assert source.read_bytes() == original
    if ocr:
        assert "pdf:ocr_applied" in projection.metadata["adapter_tags"]
        assert not derivative.exists()


@pytest.mark.parametrize("text", ["\x00a\x00\x00b\x00", "Café Ελληνικά\tline\n\\u0000", "clean"])
async def test_pdf_preserves_every_character_except_nul(pdf_source, monkeypatch, text):
    source = pdf_source(title=False, outline=False)
    # Only the extraction library is controlled; the real adapter builds the projection.
    monkeypatch.setattr(pdfplumber.page.Page, "extract_text", lambda *a, **kw: text)
    projection = await pdf_adapter.PdfAdapter().project(source)
    assert projection.text == text.replace("\x00", "")
    assert projection.headings[0].content == text.replace("\x00", "")


@pytest.mark.parametrize("field", ["body", "title", "outline"])
async def test_pdf_nulls_ingest_and_retrieve_in_postgres(
    pdf_source,
    field,
    pg_pool,
    graph_store,
    lock_manager,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_config,
    lifecycle_service,
):
    source = pdf_source(**{name: name == field for name in ("body", "title", "outline")})
    original = source.read_bytes()
    with pdfplumber.open(source) as pdf:
        raw_body = pdf.pages[0].extract_text()
    reader = PdfReader(source)
    raw_fields = {
        "body": raw_body,
        "title": reader.metadata.title,
        "outline": reader.outline[0].title,
    }
    assert "\x00" in raw_fields[field]
    store = PostgresContentStore(pg_pool)
    service = IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=stub_abstraction_provider,
        config=minimal_config,
        source_adapters={SourceType.PDF: pdf_adapter.PdfAdapter()},
        lifecycle_service=lifecycle_service,
    )
    result = await service.ingest(IngestRequest(source=str(source), source_type=SourceType.PDF))
    await drain_abstraction_queue(service)
    document = await graph_store.get_document(result.document.id)
    assert document.pipeline_status.value == "abstraction_complete"
    assert document.title == "Titleend"
    coverage, chunks, abstract = await store.read_processing_artifacts(document.id)
    assert chunks
    assert any("beforeafter" in chunk.content for chunk in chunks)
    assert all("\x00" not in chunk.content + chunk.heading_path for chunk in chunks)
    assert all(chunk.heading_path == "Headingend" for chunk in chunks)
    assert r"\u0000" not in coverage.model_dump_json()
    assert abstract and "\x00" not in abstract
    assert document.source_content_hash == "sha256:" + hashlib.sha256(original).hexdigest()
    retained = Path(minimal_config.vault.storage_root) / document.source_path
    assert retained.read_bytes() == original
