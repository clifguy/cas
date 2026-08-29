"""Misplaced top-level metadata on ``ingest_document``, and source_type inference.

Two behaviors are pinned here, both reachable only through the published
tool schema.

**Misplaced metadata.** ``ingest_document`` takes caller metadata nested
under ``metadata={...}``. A caller who spells those fields at the top
level instead (``title=...``, ``tags=[...]``) previously had them
discarded in silence: the MCP client coerces arguments to the published
schema and strips unknown properties before dispatch, so the
``extra="forbid"`` framework guard (CAS-ADR-037,
``sage._fastmcp_strict_args``) never saw them. Publishing the flat
spellings is therefore a precondition for reacting to them at all --
an unpublished parameter cannot be rejected, only dropped. Once
published, a body-level guard raises the structured
``misplaced_metadata`` envelope, mirroring the ``legacy_form`` guard
that covers the analogous wrong-shape case on ``update_metadata``.

**source_type inference.** With ``source_type`` omitted, the source
path's extension is matched against the registered adapters'
``EXTENSIONS`` declarations. Explicit values are never overridden.

Transport matters. Tests that exercise the guard route through
``mcp.call_tool`` rather than calling the tool function in process:
the in-process path bypasses FastMCP's per-tool argument model
entirely, so an in-process-only test would pass whether or not the
parameters are actually published -- leaving the real client-side
stripping fully live behind a green suite.
``test_misplaced_metadata_keys_are_published_in_the_tool_schema``
asserts the publication independently for the same reason.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.types import TextContent

from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.config import VaultConfig
from sage.mcp_server import _vaults as _mcp_vaults
from sage.mcp_server import ingest_document, mcp, search
from sage.models.enums import SourceType
from tests.sage.conftest import initialize_services_for_test

# The closed set of caller-supplied metadata keys ``ingest_document``
# recognizes inside ``metadata``. Each is published as a top-level
# parameter solely so a misplaced spelling reaches the guard instead of
# being stripped client-side; none is a functional argument.
MISPLACED_KEYS = (
    "title",
    "version_label",
    "project",
    "doc_type",
    "authority_scope",
    "document_date",
    "tags",
)

# Representative wrong-level values, one per key. ``tags`` carries a list
# to exercise the permissive annotation; the rest are scalars.
MISPLACED_VALUES: dict[str, object] = {
    "title": "Renamed",
    "version_label": "v11",
    "project": "CAS",
    "doc_type": "steering_document",
    "authority_scope": "cas",
    "document_date": "2026-08-28",
    "tags": ["alpha", "beta"],
}


@pytest.fixture
async def vault_services(minimal_vault_config_dict, tmp_vault_dir):
    """Initialize SAGE services and register them on the MCP vault registry.

    Mirrors the fixture in
    ``tests/sage/test_update_metadata_legacy_form_via_transport.py``;
    duplicated so this transport-level module stays self-contained.
    Registers under ``vault_id="test_vault"`` so the ``get_vault`` lookup
    inside each MCP tool resolves to the stub-backed services.
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    async with initialize_services_for_test(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        _mcp_vaults["test_vault"] = services

        sources = tmp_vault_dir / "sources"
        test_dir = sources / "test"
        test_dir.mkdir(parents=True, exist_ok=True)
        (test_dir / "sample.md").write_text("# Sample Document\n\nSample content.")
        (test_dir / "second.md").write_text("# Second Document\n\nOther content.")
        # Markdown content under an extension no adapter claims: the only
        # shape that separates "explicit wins" from "inference always wins".
        (test_dir / "note.txt").write_text("# Untyped Document\n\nContent.")

        try:
            yield services
        finally:
            await asyncio.sleep(0.5)
            _mcp_vaults.pop("test_vault", None)


def _parse(result):
    """Parse an in-process tool result (dict or JSON string) to a dict."""
    if isinstance(result, dict):
        return result
    return json.loads(result)


def _decode_envelope(result):
    """Extract the SAGE envelope dict from a ``[TextContent]`` ``mcp.call_tool`` return."""
    assert isinstance(result, list), f"Expected list result; got {type(result)}"
    assert len(result) == 1, f"Expected single TextContent; got {len(result)}"
    block = result[0]
    assert isinstance(block, TextContent), f"Expected TextContent; got {type(block)}"
    return json.loads(block.text)


# ---------------------------------------------------------------------------
# Published schema
# ---------------------------------------------------------------------------


def test_misplaced_metadata_keys_are_published_in_the_tool_schema():
    """Every recognized metadata key is published as an optional top-level property.

    This is the assertion that pins the root-cause fix. The client's
    ``additionalProperties: false`` coercion strips properties absent
    from the published schema, so a server-side guard over an
    unpublished parameter is unreachable -- the value never arrives.
    Publication is what converts a silent drop into a rejectable call.

    Anti-coincidental-pass: a body-level guard tested only in process
    would pass with none of these published. This test fails in exactly
    that case.
    """
    tool = mcp._tool_manager.get_tool("ingest_document")  # noqa: SLF001
    schema = tool.parameters
    props = schema.get("properties", {})
    required = schema.get("required", [])

    missing = [key for key in MISPLACED_KEYS if key not in props]
    assert not missing, (
        f"ingest_document must publish these metadata keys as top-level "
        f"properties so the client does not strip them before dispatch; "
        f"missing: {missing}"
    )
    still_required = [key for key in MISPLACED_KEYS if key in required]
    assert not still_required, (
        f"Tripwire parameters must be optional; these are required: {still_required}"
    )
    # The strict-args substrate invariant the stripping behavior depends on.
    assert schema.get("additionalProperties") is False


# ---------------------------------------------------------------------------
# Rejection over transport
# ---------------------------------------------------------------------------


async def test_misplaced_title_returns_misplaced_metadata_via_transport(vault_services):
    """A top-level ``title`` is rejected, not silently dropped.

    Routed through ``mcp.call_tool`` so FastMCP's per-tool argument
    model runs first, exactly as it does for a real MCP client.

    Anti-coincidental-pass: asserts the specific ``misplaced_metadata``
    envelope and that the offending field is named, not merely that some
    error occurred. Without the published parameter this fails with
    ``unknown_parameter`` -- the server side of the transport does see
    the stray kwarg and refuses it. A real client never gets that far,
    having stripped the field first, which is why the caller saw silent
    success. Publishing the parameter fixes both faces of the same gap.
    """
    result = await mcp.call_tool(
        "ingest_document",
        {
            "vault_id": "test_vault",
            "source": "test/sample.md",
            "source_type": "markdown",
            "title": "Renamed",
        },
    )
    envelope = _decode_envelope(result)
    assert envelope["error"] == "misplaced_metadata"
    assert envelope["detail"]["fields"] == ["title"]
    # The corrected shape must be shown, not just named.
    assert "metadata" in envelope["detail"]["example"]
    assert "title" in envelope["detail"]["example"]


@pytest.mark.parametrize("key", MISPLACED_KEYS)
async def test_every_recognized_metadata_key_is_guarded(vault_services, key):
    """Each recognized metadata key is rejected at the top level.

    Regression guard against a guard that only checks the one field the
    original field report happened to name.
    """
    result = await mcp.call_tool(
        "ingest_document",
        {
            "vault_id": "test_vault",
            "source": "test/sample.md",
            "source_type": "markdown",
            key: MISPLACED_VALUES[key],
        },
    )
    envelope = _decode_envelope(result)
    assert envelope["error"] == "misplaced_metadata", f"{key} was not guarded"
    assert envelope["detail"]["fields"] == [key]


async def test_multiple_misplaced_fields_are_reported_together(vault_services):
    """All misplaced fields come back in one envelope, not one per round-trip.

    This is the literal call shape from the field report: ``title``,
    ``tags``, and ``version_label`` supplied at the top level together.
    Reporting them as a set lets the caller repair in a single retry.
    """
    result = await mcp.call_tool(
        "ingest_document",
        {
            "vault_id": "test_vault",
            "source": "test/sample.md",
            "source_type": "markdown",
            "title": "Renamed",
            "tags": ["alpha", "beta"],
            "version_label": "v11",
        },
    )
    envelope = _decode_envelope(result)
    assert envelope["error"] == "misplaced_metadata"
    # Order is the canonical key order, not caller-supplied order.
    assert envelope["detail"]["fields"] == ["title", "version_label", "tags"]


async def test_no_document_is_created_when_the_guard_fires(vault_services):
    """The guard rejects before any pipeline work; the vault stays empty.

    Anti-coincidental-pass: separates "rejects" from "rejects before
    committing". A guard placed after the ingest await would still
    satisfy the rejection tests above while failing here.
    """
    result = await mcp.call_tool(
        "ingest_document",
        {
            "vault_id": "test_vault",
            "source": "test/sample.md",
            "source_type": "markdown",
            "title": "Renamed",
            "tags": ["alpha", "beta"],
            "version_label": "v11",
        },
    )
    assert _decode_envelope(result)["error"] == "misplaced_metadata"

    catalog = _parse(await search("test_vault", mode="catalog", limit=50))
    assert catalog["total_available"] == 0, (
        f"Guard must reject before any document is committed; found {catalog['total_available']}"
    )


async def test_nested_metadata_still_works(vault_services):
    """Negative control: the correct nested shape is unaffected.

    Proves the guard rejects *misplacement* rather than the fields
    themselves. This test passes both before and after the change; if it
    ever fails, the guard is over-broad.
    """
    result = _parse(
        await ingest_document(
            "test_vault",
            "test/sample.md",
            "markdown",
            metadata={"title": "Proper", "tags": ["alpha", "beta"]},
        )
    )
    assert result["title"] == "Proper"
    assert set(result["tags"]) == {"alpha", "beta"}


async def test_rejection_is_consistent_across_create_and_supersession(vault_services):
    """The create and supersession paths reject identically.

    The drop was originally observed on the supersession path
    (``predecessor_id`` set). Both legs must produce the same envelope,
    or callers learn one rule and get caught by the other.
    """
    predecessor = _parse(
        await ingest_document(
            "test_vault", "test/sample.md", "markdown", metadata={"title": "Predecessor"}
        )
    )

    create_leg = _decode_envelope(
        await mcp.call_tool(
            "ingest_document",
            {
                "vault_id": "test_vault",
                "source": "test/second.md",
                "source_type": "markdown",
                "title": "Renamed",
            },
        )
    )
    supersede_leg = _decode_envelope(
        await mcp.call_tool(
            "ingest_document",
            {
                "vault_id": "test_vault",
                "source": "test/second.md",
                "source_type": "markdown",
                "predecessor_id": predecessor["id"],
                "title": "Renamed",
            },
        )
    )

    assert create_leg["error"] == "misplaced_metadata"
    assert supersede_leg["error"] == "misplaced_metadata"
    assert create_leg["detail"]["fields"] == supersede_leg["detail"]["fields"]


# ---------------------------------------------------------------------------
# source_type inference
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("notes.md", SourceType.MARKDOWN),
        ("notes.markdown", SourceType.MARKDOWN),
        ("report.docx", SourceType.DOCX),
        ("template.dotx", SourceType.DOCX),
        ("scan.pdf", SourceType.PDF),
        ("book.xlsx", SourceType.XLSX),
        ("deck.pptx", SourceType.PPTX),
        ("deck.potx", SourceType.PPTX),
        # Case-insensitive: callers paste paths from Finder and Windows alike.
        ("NOTES.MD", SourceType.MARKDOWN),
    ],
)
async def test_source_type_is_inferred_from_extension(vault_services, path, expected):
    """Each registered adapter's declared extensions resolve to its source type.

    Anti-coincidental-pass: parametrized across every adapter, so an
    implementation hardcoded to one type fails on the others.
    """
    assert vault_services.ingestion_service.infer_source_type(path) == expected


@pytest.mark.parametrize("path", ["notes.txt", "message.eml", "README", "archive.tar.gz"])
async def test_unmatched_extension_infers_nothing(vault_services, path):
    """An unrecognized or absent extension yields None rather than a guess.

    Adapters declaring no ``EXTENSIONS`` (email, onenote, teams_chat)
    must never be inferred into: a wrong guess would route bytes to the
    wrong adapter, which is worse than the existing explicit error.
    """
    assert vault_services.ingestion_service.infer_source_type(path) is None


async def test_inference_is_driven_by_the_registered_adapters(vault_services):
    """Only registered adapters are inferred into.

    Pins the property the implementation claims -- that resolution reads
    the adapters actually present, not a fixed table. A hardcoded
    extension map produces identical answers for every other test in this
    module, so without this one the claim is undefended: the same
    ``.docx`` path must resolve when the docx adapter is registered and
    stop resolving when it is not.
    """
    service = vault_services.ingestion_service
    registry = service._adapters  # noqa: SLF001 -- no public accessor for the registry
    assert service.infer_source_type("report.docx") == SourceType.DOCX

    reduced = {k: v for k, v in registry.items() if k is not SourceType.DOCX}
    service._adapters = reduced  # noqa: SLF001
    try:
        assert service.infer_source_type("report.docx") is None, (
            "Inference must read the registered adapters, not a fixed table."
        )
        # The still-registered types keep resolving, so the reduction did
        # not simply break inference wholesale.
        assert service.infer_source_type("notes.md") == SourceType.MARKDOWN
    finally:
        service._adapters = registry  # noqa: SLF001


async def test_explicit_source_type_overrides_a_disagreeing_extension(vault_services):
    """An explicit ``source_type`` wins even when the extension says otherwise.

    The discriminating case for fallback-vs-override. Every other
    inference test uses a path where inference and the caller agree, or
    where inference yields nothing -- both of which an
    "inference wins whenever it resolves" implementation
    (``inferred or source_type``) satisfies. Only a disagreement
    separates them: a ``.md`` path declared ``pdf`` must be routed to the
    pdf adapter and fail there, never silently corrected to markdown.
    """
    envelope = _decode_envelope(
        await mcp.call_tool(
            "ingest_document",
            {
                "vault_id": "test_vault",
                "source": "test/sample.md",
                "source_type": "pdf",
            },
        )
    )
    assert envelope.get("source_type") != "markdown", (
        "The caller's explicit source_type was silently replaced by inference."
    )


async def test_explicit_source_type_survives_an_unmatched_extension(vault_services):
    """Inference is a fallback, never an override.

    Uses a ``.txt`` path that no adapter claims, so inference alone
    yields nothing: the ingest can only succeed if the caller's explicit
    ``source_type`` is honored. Asserting against a ``.md`` path instead
    would prove nothing, because inference would independently arrive at
    the same answer and the test would pass under an implementation that
    ignored the caller entirely.

    Anti-coincidental-pass: an implementation that infers
    unconditionally satisfies the omitted-source_type test but fails here.
    """
    result = _decode_envelope(
        await mcp.call_tool(
            "ingest_document",
            {
                "vault_id": "test_vault",
                "source": "test/note.txt",
                "source_type": "markdown",
            },
        )
    )
    assert result.get("error") is None, (
        f"An explicit source_type must be honored even when the extension "
        f"is unrecognized; got {result}"
    )
    assert result["source_type"] == "markdown"


async def test_omitted_source_type_succeeds_via_transport(vault_services):
    """A ``.md`` ingest with no ``source_type`` succeeds over real transport.

    The paper cut from the field report, tested at the surface the
    caller actually uses.
    """
    result = _decode_envelope(
        await mcp.call_tool(
            "ingest_document",
            {"vault_id": "test_vault", "source": "test/sample.md"},
        )
    )
    assert result.get("error") is None, f"Expected success; got {result}"
    assert result["source_type"] == "markdown"
