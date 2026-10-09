"""The process-wide source-adapter registry."""

from functools import cache

from sage.models.enums import SourceType
from sage.source_adapters.base import SourceAdapter
from sage.source_adapters.docx_adapter import DocxAdapter
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from sage.source_adapters.pdf_adapter import PdfAdapter
from sage.source_adapters.pptx_adapter import PptxAdapter
from sage.source_adapters.structured_data_adapter import StructuredDataAdapter
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
        SourceType.STRUCTURED_DATA: StructuredDataAdapter(),
    }


@cache
def registered_source_extensions() -> frozenset[str]:
    """Every file extension a registered source adapter reads, lowercased.

    The union of the adapters' ``EXTENSIONS``. Ingest does not limit stored
    sources to it, since an explicit ``source_type`` outranks the extension,
    so a consumer that must stay within the formats SAGE reads checks a
    stored path against this set itself.
    """
    return frozenset(
        extension.lower()
        for adapter in build_source_adapter_registry().values()
        for extension in adapter.EXTENSIONS
    )
