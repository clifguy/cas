"""T-0081: SAGE projection emit preserves ATX heading marks.

`_chunk_projection` previously stored only the body text of each heading
in `chunk.content`, so `read_projection` reconstructed prose with no
heading marks and round-trip lost the heading hierarchy. The fix
prepends the ATX heading line to chunk content at chunk creation; this
suite is the regression guard for the seven test cases enumerated in
the ticket (TC-1..TC-6 from the acceptance criteria plus a read_section
symmetry guard).
"""

import asyncio

import pytest

from sage.models.enums import SourceType
from sage.models.schemas import IngestRequest
from sage.services.utilities import UtilitiesService


@pytest.fixture
def utilities_service(graph_store, stub_content_store, stub_embedding_provider, minimal_config):
    return UtilitiesService(
        graph_store=graph_store,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        config=minimal_config,
    )


async def _ingest_markdown(ingestion_service, tmp_vault_dir, rel_path: str, source_text: str):
    """Write `source_text` to `tmp_vault_dir/sources/<rel_path>` and ingest it.

    Returns the ingested Document. Waits for the background pipeline to
    quiesce so subsequent reads see all chunks.
    """
    target = tmp_vault_dir / "sources" / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source_text)

    result = await ingestion_service.ingest(
        IngestRequest(source=rel_path, source_type=SourceType.MARKDOWN),
    )
    await asyncio.sleep(0.5)
    return result.document


async def _round_trip(
    ingestion_service,
    utilities_service,
    tmp_vault_dir,
    name: str,
    source_text: str,
):
    """Ingest -> read_projection -> re-ingest the emitted text.

    Returns (first_projection_text, second_heading_paths).
    """
    first = await _ingest_markdown(
        ingestion_service, tmp_vault_dir, f"{name}/original.md", source_text
    )
    projection = await utilities_service.read_projection(first.id)
    first_text = projection.projection_text

    # Re-ingest the emitted text under a sibling source path.
    second = await _ingest_markdown(
        ingestion_service, tmp_vault_dir, f"{name}/roundtrip.md", first_text
    )

    listing = await utilities_service.list_headings(second.id)
    return first_text, list(listing.headings)


# ---------------------------------------------------------------------------
# TC-1: H1 -> H2 -> H3 nesting
# ---------------------------------------------------------------------------


async def test_tc1_h1_h2_h3_nesting_roundtrip(ingestion_service, utilities_service, tmp_vault_dir):
    source = "# A\n\nbody-a\n\n## B\n\nbody-b\n\n### C\n\nbody-c\n"
    first_text, second_paths = await _round_trip(
        ingestion_service, utilities_service, tmp_vault_dir, "tc1", source
    )

    # ATX marks present at correct levels in the emitted projection.
    assert "\n# A" in "\n" + first_text
    assert "\n## B" in "\n" + first_text
    assert "\n### C" in "\n" + first_text
    # Bodies preserved.
    assert "body-a" in first_text
    assert "body-b" in first_text
    assert "body-c" in first_text

    # Round-trip preserves the heading hierarchy.
    assert second_paths == ["A", "A > B", "A > B > C"]


# ---------------------------------------------------------------------------
# TC-2: H1 only
# ---------------------------------------------------------------------------


async def test_tc2_h1_only_roundtrip(ingestion_service, utilities_service, tmp_vault_dir):
    source = "# Only Heading\n\nsome body\n"
    first_text, second_paths = await _round_trip(
        ingestion_service, utilities_service, tmp_vault_dir, "tc2", source
    )

    assert "\n# Only Heading" in "\n" + first_text
    assert "some body" in first_text
    assert second_paths == ["Only Heading"]


# ---------------------------------------------------------------------------
# TC-3: two sibling H2s under one H1 -- no false H1 promotion
# ---------------------------------------------------------------------------


async def test_tc3_sibling_h2s_under_one_h1(ingestion_service, utilities_service, tmp_vault_dir):
    source = "# Top\n\n## Left\n\nleft-body\n\n## Right\n\nright-body\n"
    first_text, second_paths = await _round_trip(
        ingestion_service, utilities_service, tmp_vault_dir, "tc3", source
    )

    # Exactly one `# ` and two `## ` lines.
    lines = first_text.splitlines()
    h1_lines = [ln for ln in lines if ln.startswith("# ")]
    h2_lines = [ln for ln in lines if ln.startswith("## ")]
    assert h1_lines == ["# Top"]
    assert h2_lines == ["## Left", "## Right"]

    assert second_paths == ["Top", "Top > Left", "Top > Right"]


# ---------------------------------------------------------------------------
# TC-4: body-internal `---` thematic break survives and does not promote
# ---------------------------------------------------------------------------


async def test_tc4_body_internal_thematic_break_preserved(
    ingestion_service, utilities_service, tmp_vault_dir
):
    source = "# Sec\n\nbefore-rule\n\n---\n\nafter-rule\n"
    first_text, second_paths = await _round_trip(
        ingestion_service, utilities_service, tmp_vault_dir, "tc4", source
    )

    assert "\n# Sec" in "\n" + first_text
    # Body `---` survives as a literal line, not consumed as setext underline.
    assert "\n---" in "\n" + first_text
    assert "before-rule" in first_text
    assert "after-rule" in first_text

    # Round-trip must produce exactly one heading, not promote `---` to a heading.
    assert second_paths == ["Sec"]


# ---------------------------------------------------------------------------
# TC-5: fenced code block containing ATX-shaped lines is preserved as code
# ---------------------------------------------------------------------------


async def test_tc5_fenced_code_block_with_hashes_does_not_promote(
    ingestion_service, utilities_service, tmp_vault_dir
):
    source = "# Real\n\nbefore\n\n```\n## Phantom Inside Fence\n```\n\nafter\n"
    first_text, second_paths = await _round_trip(
        ingestion_service, utilities_service, tmp_vault_dir, "tc5", source
    )

    assert "\n# Real" in "\n" + first_text
    # The phantom heading text survives inside the fence as code content.
    assert "## Phantom Inside Fence" in first_text

    # T-0070 interlock: the phantom heading does not become a real heading.
    assert second_paths == ["Real"]


# ---------------------------------------------------------------------------
# TC-6: YAML frontmatter stripped; headings below preserved
# ---------------------------------------------------------------------------


async def test_tc6_yaml_frontmatter_stripped_headings_below_preserved(
    ingestion_service, utilities_service, tmp_vault_dir
):
    source = "---\nname: x\ndescription: y\n---\n\n# Real\n\nbody\n"
    first_text, second_paths = await _round_trip(
        ingestion_service, utilities_service, tmp_vault_dir, "tc6", source
    )

    # Frontmatter is stripped from the emitted projection.
    assert "name: x" not in first_text
    assert "description: y" not in first_text
    # Heading below frontmatter survives.
    assert "\n# Real" in "\n" + first_text
    assert "body" in first_text

    # T-0071 interlock: round-trip preserves the heading below frontmatter.
    assert second_paths == ["Real"]


# ---------------------------------------------------------------------------
# TC-7: read_section emits its own ATX heading line (read_section symmetry)
# ---------------------------------------------------------------------------


async def test_tc7_read_section_emits_own_heading_line(
    ingestion_service, utilities_service, tmp_vault_dir
):
    source = "# Top\n\ntop-body\n\n## Sub\n\nsub-body\n"
    doc = await _ingest_markdown(ingestion_service, tmp_vault_dir, "tc7/doc.md", source)

    section = await utilities_service.read_section(doc.id, "Top > Sub")

    # The H2's own ATX line is the first non-empty line of the section text.
    first_nonempty = next(ln for ln in section.section_text.splitlines() if ln.strip())
    assert first_nonempty == "## Sub"
    assert "sub-body" in section.section_text
