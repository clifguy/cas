"""The process-wide source-adapter registry."""

from sage.models.enums import SourceType
from sage.source_adapters.base import SourceAdapter
from sage.source_adapters.docx_adapter import DocxAdapter
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from sage.source_adapters.pdf_adapter import PdfAdapter
from sage.source_adapters.pptx_adapter import PptxAdapter
from sage.source_adapters.xlsx_adapter import XlsxAdapter


def build_source_adapter_registry() -> dict[SourceType, SourceAdapter]:
    """Build the process-wide source-adapter registry.

    Adapter selection during ingestion resolves against this mapping, so a
    source type absent here raises ``adapter_not_found``. Vault
    configuration declares no adapters at all (CAS-ADR-046); availability
    is process-wide capability fixed by the installed implementations.
    """
    return {
        SourceType.MARKDOWN: MarkdownAdapter(),
        SourceType.DOCX: DocxAdapter(),
        SourceType.XLSX: XlsxAdapter(),
        SourceType.PDF: PdfAdapter(),
        SourceType.PPTX: PptxAdapter(),
    }
