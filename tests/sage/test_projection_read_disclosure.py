"""The published contract tells a caller the projection is not the document.

A document's projection is derived text: it keeps what the source adapter
extracts and drops the rest, so a binary source's link targets, styles and
images are absent from it while present in the retained source. A caller
that reads only the published contract must learn that, and where the
lossless original is, from every surface it reads: the MCP tool
descriptions, the REST operation summary, and the refusal raised when it
asks for a binary source inline.

These assertions read what a client receives -- the catalog the servers
publish and the specification served over HTTP -- not the Python docstrings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from sage.api.errors import BinaryContentRefusedError
from scripts.dump_mcp_catalog import build_catalog

_SPEC = Path(__file__).resolve().parents[2] / "docs" / "fs" / "sage" / "sage_core_api.openapi.yaml"


@pytest.fixture(scope="module")
def descriptions() -> dict[str, str]:
    """Each ``sage``-surface tool's published description, whitespace-collapsed, by tool name."""
    catalog: dict[str, Any] = build_catalog()
    return {
        tool["name"]: " ".join(tool["description"].split()) for tool in catalog["surfaces"]["sage"]
    }


def test_read_projection_discloses_that_the_projection_is_lossy(descriptions: dict[str, str]):
    """The description names the loss and the lossless read, not a "full text" read."""
    text = descriptions["read_projection"]
    assert "lossy" in text
    assert "binary formats (docx, pdf, pptx, xlsx) can lose" in text
    assert "Absence from the projection is not absence from the document." in text
    assert "Read the original with ``get_document``, using ``write_to_path``" in text
    assert "full text" not in text


def test_no_projection_routes_to_the_repair_that_accepts_it(descriptions: dict[str, str]):
    """``recompute_abstract`` refuses a document with no projection; the pipeline rerun does not."""
    text = descriptions["read_projection"]
    assert "``recompute_pipeline`` may restore it" in text
    assert "recompute_abstract" not in text


def test_read_projection_does_not_equate_its_delivery_with_get_document(
    descriptions: dict[str, str],
):
    """Shared delivery is not shared content: the two write different bytes."""
    text = descriptions["read_projection"]
    assert "Mirrors" not in text
    assert "not source bytes" in text


def test_get_document_names_the_binary_refusal_and_the_lossy_read(descriptions: dict[str, str]):
    """The original read lists the refusal a binary source draws and points at the text read."""
    text = descriptions["get_document"]
    assert "``binary_content_refused``" in text
    assert "``read_projection``" in text


def test_binary_refusal_names_the_route_to_the_original_bytes():
    """The refusal offers the lossless route alongside the extracted-text read."""
    err = BinaryContentRefusedError("doc_x", "docx")
    assert "read_projection" in err.message
    assert "write_to_path" in err.message


def test_rest_projection_operation_does_not_claim_the_full_document():
    """The HTTP operation describes a lossy projection and names the read for the original."""
    spec = yaml.safe_load(_SPEC.read_text(encoding="utf-8"))
    operation = spec["paths"]["/sage_vaults/{vault_id}/documents/{document_id}/projection"]["get"]
    description = " ".join(operation["description"].split())
    assert "full" not in operation["summary"]
    assert "lossy" in operation["summary"]
    assert "full document" not in description
    assert "binary formats (docx, pdf, pptx, xlsx) can lose" in description
    assert "Absence from the projection is not absence from the document." in description
    assert "Read the original with `get_document`, using `write_to_path`" in description


def _rest_description(path: str, method: str = "get") -> str:
    spec = yaml.safe_load(_SPEC.read_text(encoding="utf-8"))
    return " ".join(spec["paths"][path][method]["description"].split())


def test_get_document_names_the_hash_its_bytes_match(descriptions: dict[str, str]):
    """Delivered bytes are the retained copy, which a rewriting binding changes at rest."""
    text = descriptions["get_document"]
    assert "they match ``stored_content_hash`` (if null, ``source_content_hash``)" in text
    assert "``source_content_hash`` (the bytes ingested)" in text
    assert "rewrites the copy at rest" in text
    assert "source as ingested" not in text


def test_rest_get_document_names_the_hash_its_bytes_match():
    """The HTTP operation makes the same hash claims as the tool."""
    text = _rest_description("/sage_vaults/{vault_id}/documents/{document_id}")
    assert "they match `stored_content_hash` (if null, `source_content_hash`)" in text
    assert "`source_content_hash` (the bytes ingested)" in text
    assert "rewrites the copy at rest" in text
    assert "source as ingested" not in text
    assert "original source file bytes" not in text


def test_rest_content_route_names_the_stored_hash():
    """The raw byte channel says which recorded hash its stream matches."""
    text = _rest_description("/sage_vaults/{vault_id}/documents/{document_id}/content")
    assert "`stored_content_hash`" in text
